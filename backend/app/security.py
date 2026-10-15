"""PAN handling (PRD §14.5): encrypted at rest, HMAC hash for lookups, masked for display.

PAN never goes into logs or LLM prompts.
"""

import hashlib
import hmac
import re
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import BACKEND_DIR, get_settings

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_DEV_KEY_FILE = BACKEND_DIR / "data" / ".dev_field_key"


def normalise_pan(pan: str) -> str:
    pan = (pan or "").strip().upper()
    if not PAN_RE.match(pan):
        raise ValueError("PAN must look like ABCDE1234F")
    return pan


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().field_encryption_key
    if not key:
        # Dev only: a per-machine key so local data stays readable across restarts.
        path = Path(_DEV_KEY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
            path.chmod(0o600)
        key = path.read_bytes().decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_pan(pan: str) -> bytes:
    return _fernet().encrypt(normalise_pan(pan).encode())


def decrypt_pan(token: bytes) -> str:
    return _fernet().decrypt(token).decode()


def hash_pan(pan: str) -> str:
    pepper = get_settings().pan_hash_pepper.encode()
    return hmac.new(pepper, normalise_pan(pan).encode(), hashlib.sha256).hexdigest()


def mask_pan(pan: str) -> str:
    pan = normalise_pan(pan)
    return f"{pan[:5]}••••{pan[-1]}"
