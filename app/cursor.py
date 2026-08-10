"""Opaque pagination cursors for user-facing endpoints.

ledger.id is a single global sequence: handing raw ids to clients lets any
user measure platform-wide write volume by diffing ids over time. Encrypting
the cursor keeps pagination stateless without exposing the sequence. Admin
endpoints keep raw ids — admins are trusted and need them for support.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encode_cursor(last_id: int) -> str:
    return _fernet(get_settings().jwt_secret).encrypt(str(last_id).encode()).decode()


def decode_cursor(cursor: str) -> int | None:
    """Returns the id inside a cursor, or None for anything forged/garbled."""
    try:
        return int(_fernet(get_settings().jwt_secret).decrypt(cursor.encode()))
    except (InvalidToken, ValueError):
        return None
