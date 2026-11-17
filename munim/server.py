"""HTTP + WebSocket server. Serves the PWA and runs one Session per browser."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import ROOT, Settings, get_settings
from .crypto import load_key
from .ledger import Ledger
from .llm import LLM
from .seed import seed
from .session import Session

log = logging.getLogger("munim")


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.ephemeral:
            log.warning("Serverless host: the ledger lives in %s and lasts only as long as this "
                        "function instance.", settings.data_dir)
        key = load_key(settings.enc_key, settings.data_dir)
        self.ledger = Ledger(settings.data_dir / "khata.db", key, settings.retention_hours)
        self.llm = LLM(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                       settings.llm_timeout_s, settings.llm_reasoning_effort) if settings.has_llm else None
        self.sessions: set[Session] = set()
        if settings.demo_seed and not self.ledger.customers():
            seed(self.ledger)

    async def broadcast_ledger(self) -> None:
        snap = {"type": "ledger", "data": self.ledger.snapshot()}
        await asyncio.gather(*(s.send(snap) for s in list(self.sessions)),
                             return_exceptions=True)

    async def reset_demo(self) -> None:
        """Wipe everything and reload the synthetic demo customers."""
        self.ledger.wipe_all()
        seed(self.ledger)
        await self.broadcast_ledger()

    def authorised(self, token: str | None) -> bool:
        want = self.settings.access_token
        return not want or hmac.compare_digest(token or "", want)


async def _purge_loop(app: App) -> None:
    """Delete transcripts past their retention window (DPDP: no longer than needed)."""
    while True:
        try:
            n = app.ledger.purge_transcripts()
            if n:
                log.info("purged %d expired transcripts", n)
        except Exception:
            log.exception("purge failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(api: FastAPI):
    if not logging.getLogger().handlers:        # imported directly by a host (Vercel)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    app = App(settings)
    api.state.app = app
    if not settings.has_speech:
        log.warning("GNANI_API_KEY is not set: running in text-only mode (no voice).")
    if app.llm:
        asyncio.create_task(app.llm.warm())
    else:
        log.warning("LLM_API_KEY is not set: only Hindi/Hinglish/English rules are active.")
    purge = asyncio.create_task(_purge_loop(app))
    yield
    purge.cancel()
    if app.llm:
        await app.llm.close()


api = FastAPI(title="Munim-ji", version=__version__, lifespan=lifespan)


def _app(request: Request) -> App:
    app: App = request.app.state.app
    if not app.authorised(request.query_params.get("k")):
        raise HTTPException(401, "missing or wrong access token")
    return app


@api.get("/api/health")
async def health(request: Request):
    app: App = request.app.state.app
    s = app.settings
    return {"ok": True, "version": __version__, "speech": s.has_speech, "llm": s.has_llm,
            "stt": s.stt_model, "tts": s.tts_model, "locked": bool(s.access_token),
            "storage": "temporary" if s.ephemeral else "disk"}


@api.get("/api/ledger")
async def ledger(request: Request):
    return _app(request).ledger.snapshot()


@api.get("/api/customer/{cid}")
async def customer(cid: int, request: Request):
    """One customer's page: every active line with the balance after it."""
    page = _app(request).ledger.customer_page(cid)
    if page is None:
        raise HTTPException(404, "no such customer")
    return page


@api.get("/api/export.csv")
async def export_csv(request: Request):
    """The whole khata as a spreadsheet file, for a backup or an accountant."""
    return Response(_app(request).ledger.export_csv(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="khata-{date.today().isoformat()}.csv"'})


@api.post("/api/demo/reset")
async def demo_reset(request: Request):
    """Wipe everything and reload the synthetic demo customers."""
    await _app(request).reset_demo()
    return {"ok": True}


@api.post("/api/purge")
async def purge(request: Request):
    """Delete every stored transcript now (run after judging)."""
    return {"deleted": _app(request).ledger.purge_transcripts(everything=True)}


@api.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    app: App = ws.app.state.app
    if not app.authorised(ws.query_params.get("k")):
        await ws.close(code=4401)
        return
    await ws.accept()
    session = Session(ws, app)
    app.sessions.add(session)
    try:
        await session.run()
    finally:
        app.sessions.discard(session)


@api.middleware("http")
async def no_cache_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/sw.js"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@api.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"error": "internal error"}, status_code=500)


api.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
