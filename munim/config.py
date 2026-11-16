"""Runtime settings, read from the environment (.env in the repo root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LANGS = ("hi", "en", "kn", "ta", "te", "mr", "bn", "gu", "pa", "ml")


def _s(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


@dataclass(frozen=True)
class Settings:
    gnani_api_key: str
    stt_url: str
    tts_url: str
    stt_model: str
    tts_model: str
    stt_format: str
    tts_voice: str
    tts_speed: float
    tts_sample_rate: int
    vad_threshold: float
    min_silence_ms: int
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_s: float
    enc_key: str
    data_dir: Path
    shop_name: str
    access_token: str
    default_lang: str
    big_amount: int
    retention_hours: int
    demo_seed: bool
    host: str
    port: int

    @property
    def has_speech(self) -> bool:
        return bool(self.gnani_api_key)

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)


def get_settings() -> Settings:
    lang = _s("DEFAULT_LANG", "hi")
    return Settings(
        gnani_api_key=_s("GNANI_API_KEY"),
        stt_url=_s("GNANI_STT_URL", "wss://api.vachana.ai/stt/v3/stream"),
        tts_url=_s("GNANI_TTS_URL", "wss://api.vachana.ai/api/v1/tts"),
        # The challenge requires these two models; they are not configurable.
        stt_model="prisma-v3.0",
        tts_model="timbre-v3.0",
        stt_format=_s("GNANI_STT_FORMAT", "transcribe"),
        tts_voice=_s("GNANI_TTS_VOICE", "Avinash"),
        tts_speed=_f("GNANI_TTS_SPEED", 1.05),
        tts_sample_rate=_i("GNANI_TTS_SAMPLE_RATE", 24000),
        vad_threshold=_f("GNANI_VAD_THRESHOLD", 0.75),
        min_silence_ms=_i("GNANI_MIN_SILENCE_MS", 350),
        llm_api_key=_s("LLM_API_KEY"),
        llm_base_url=_s("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        llm_model=_s("LLM_MODEL", "llama-3.3-70b-versatile"),
        llm_timeout_s=_f("LLM_TIMEOUT_S", 4.0),
        enc_key=_s("KHATA_ENC_KEY"),
        data_dir=Path(_s("DATA_DIR") or ROOT / "data"),
        shop_name=_s("SHOP_NAME", "Kirana Store"),
        access_token=_s("ACCESS_TOKEN"),
        default_lang=lang if lang in LANGS else "hi",
        big_amount=_i("BIG_AMOUNT", 20000),
        retention_hours=_i("TRANSCRIPT_RETENTION_HOURS", 72),
        demo_seed=_s("DEMO_SEED", "1") not in ("0", "false", "no"),
        host=_s("HOST", "0.0.0.0"),
        port=_i("PORT", 8000),
    )
