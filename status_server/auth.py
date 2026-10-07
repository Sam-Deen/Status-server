"""Single-administrator sessions and CSRF checks."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

from .config import Settings
from .repository import SessionRepository


class InvalidCredentials(Exception):
    pass


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str


class AuthService:
    def __init__(self, settings: Settings, repository: SessionRepository):
        self.settings = settings
        self.repository = repository

    def login(self, data: object) -> tuple[str, str]:
        username = str(data.get("username", "")) if isinstance(data, dict) else ""
        password = str(data.get("password", "")) if isinstance(data, dict) else ""
        if (not hmac.compare_digest(username, self.settings.admin_username)
                or not hmac.compare_digest(password, self.settings.admin_password)):
            time.sleep(0.3)
            raise InvalidCredentials
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.repository.create(hashlib.sha256(token.encode()).hexdigest(), csrf,
                               int(time.time()) + self.settings.session_seconds)
        return token, csrf

    def authenticate(self, cookie_header: str, csrf_header: str | None = None) -> Session | None:
        try:
            cookies = SimpleCookie(cookie_header)
            token = cookies["status_session"].value
            if not token or len(token) > 128:
                return None
        except (KeyError, ValueError):
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.repository.get(token_hash)
        if row is None or row["expires"] <= time.time():
            return None
        if csrf_header is not None and not hmac.compare_digest(csrf_header, row["csrf"]):
            return None
        return Session(token_hash, row["csrf"])

    def logout(self, session: Session) -> None:
        self.repository.delete(session.token_hash)

    def cookie(self, token: str, clear: bool = False) -> str:
        secure = "; Secure" if self.settings.cookie_secure else ""
        age = 0 if clear else self.settings.session_seconds
        return f"status_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}"
