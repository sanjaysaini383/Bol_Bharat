"""Field-level encryption at rest (AES-256-GCM)."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def load_key(env_value: str, data_dir: Path) -> bytes:
    """Key from KHATA_ENC_KEY, else a key file created once in data/."""
    if env_value:
        value = env_value.strip()
        try:
            # A key pasted without its trailing "=" is still the same key.
            raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        except ValueError:
            raw = b""
        if len(raw) != 32:
            raise ValueError(
                "KHATA_ENC_KEY must be 32 random bytes in base64 (44 characters). Generate one "
                "with: python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\" "
                "or leave it empty to use a key file in DATA_DIR.")
        return raw
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "khata.key"
    if path.exists():
        return base64.b64decode(path.read_text().strip())
    raw = os.urandom(32)
    path.write_text(base64.b64encode(raw).decode())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return raw


class Box:
    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    def seal(self, text: str | None) -> bytes | None:
        if text is None:
            return None
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, text.encode("utf-8"), None)

    def open(self, blob: bytes | None) -> str | None:
        if blob is None:
            return None
        return self._aes.decrypt(blob[:12], blob[12:], None).decode("utf-8")
