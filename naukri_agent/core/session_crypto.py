"""Authenticated encryption for persisted Playwright session state."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SessionDecryptionError(ValueError):
    """Raised when persisted session data cannot be authenticated or decrypted."""


class SessionCipher:
    key_version = 1

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("BROWSER_SESSION_SECRET must contain at least 32 characters")
        derived = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, state: dict[str, Any]) -> bytes:
        payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt(self, token: bytes) -> dict[str, Any]:
        try:
            payload = self._fernet.decrypt(token)
            value = json.loads(payload)
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionDecryptionError("browser session could not be decrypted") from exc
        if not isinstance(value, dict):
            raise SessionDecryptionError("browser session payload is not an object")
        return value
