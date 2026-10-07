"""Runtime configuration for the application container."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: str
    host: str
    port: int
    admin_username: str
    admin_password: str
    cookie_secure: bool
    docker_proxy_url: str
    session_seconds: int = 12 * 3600
    retention_days: int = 35
    max_body_bytes: int = 8192

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        password = values.get("ADMIN_PASSWORD", "")
        if len(password) < 16 or password == "replace-with-a-long-random-password":
            raise ConfigurationError("Set ADMIN_PASSWORD to a unique value of at least 16 characters")
        username = values.get("ADMIN_USERNAME", "admin")
        if not username or len(username) > 80:
            raise ConfigurationError("ADMIN_USERNAME must contain 1–80 characters")
        cookie_value = values.get("COOKIE_SECURE", "true").lower()
        if cookie_value not in {"true", "false"}:
            raise ConfigurationError("COOKIE_SECURE must be true or false")
        db_path = values.get("DB_PATH", "/data/status.db")
        host = values.get("HOST", "0.0.0.0")
        if not db_path or not host:
            raise ConfigurationError("DB_PATH and HOST must not be empty")
        try:
            port = int(values.get("PORT", "8080"))
        except ValueError:
            raise ConfigurationError("PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be between 1 and 65535")
        proxy_url = values.get("DOCKER_PROXY_URL", "http://docker_proxy:8090/containers")
        try:
            parsed = urlsplit(proxy_url)
            valid_proxy = (parsed.scheme == "http" and bool(parsed.hostname) and parsed.port is not None
                           and 1 <= parsed.port <= 65535
                           and parsed.path == "/containers" and not parsed.query and not parsed.fragment
                           and not parsed.username and not parsed.password)
        except ValueError:
            valid_proxy = False
        if not valid_proxy:
            raise ConfigurationError("DOCKER_PROXY_URL must be an HTTP /containers URL without credentials")
        return cls(db_path, host, port, username, password, cookie_value == "true", proxy_url)
