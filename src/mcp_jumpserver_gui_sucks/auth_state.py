from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from time import time
from urllib.parse import urlparse

WEB_SESSION_COOKIE_NAME = "jms_sessionid"


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(slots=True)
class CookieState:
    name: str
    value: str
    domain: str | None = None
    path: str = "/"
    secure: bool = True
    http_only: bool = False
    expires: float | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CookieState":
        return cls(
            name=str(raw["name"]),
            value=str(raw["value"]),
            domain=raw.get("domain"),
            path=str(raw.get("path", "/")),
            secure=bool(raw.get("secure", True)),
            http_only=bool(raw.get("http_only", False)),
            expires=raw.get("expires"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "http_only": self.http_only,
            "expires": self.expires,
        }


@dataclass(slots=True)
class AuthState:
    schema_version: int = 2
    base_url: str = ""
    login_source: str = "unknown"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[CookieState] = field(default_factory=list)
    bearer_token: str = ""
    bearer_keyword: str = "Bearer"
    bearer_expires_at: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuthState":
        return cls(
            schema_version=int(raw.get("schema_version", 1)),
            base_url=str(raw.get("base_url", "")),
            login_source=str(raw.get("login_source", "unknown")),
            headers={str(k): str(v) for k, v in dict(raw.get("headers", {})).items()},
            cookies=[
                CookieState.from_dict(cookie)
                for cookie in list(raw.get("cookies", []))
            ],
            bearer_token=str(raw.get("bearer_token", "")),
            bearer_keyword=str(raw.get("bearer_keyword", "Bearer") or "Bearer"),
            bearer_expires_at=str(raw.get("bearer_expires_at", "")),
            access_key_id=str(raw.get("access_key_id", "")),
            access_key_secret=str(raw.get("access_key_secret", "")),
            metadata=dict(raw.get("metadata", {})),
            created_at=str(raw.get("created_at", utc_now())),
            updated_at=str(raw.get("updated_at", utc_now())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "base_url": self.base_url,
            "login_source": self.login_source,
            "headers": self.headers,
            "cookies": [cookie.to_dict() for cookie in self.cookies],
            "bearer_token": self.bearer_token,
            "bearer_keyword": self.bearer_keyword,
            "bearer_expires_at": self.bearer_expires_at,
            "access_key_id": self.access_key_id,
            "access_key_secret": self.access_key_secret,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def cookie_names(self) -> list[str]:
        return [cookie.name for cookie in self.cookies]

    def header_names(self) -> list[str]:
        return sorted(self.headers.keys())

    def cookie_lookup(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.cookies}

    def get_cookie(self, name: str) -> CookieState | None:
        for cookie in self.cookies:
            if cookie.name == name:
                return cookie
        return None

    def has_cookie_auth(self) -> bool:
        return any(cookie.name == WEB_SESSION_COOKIE_NAME and bool(cookie.value) for cookie in self.cookies)

    def has_bearer_auth(self) -> bool:
        return bool(self.bearer_token)

    def has_access_key_auth(self) -> bool:
        return bool(self.access_key_id and self.access_key_secret)

    def auth_modes(self) -> list[str]:
        modes: list[str] = []
        if self.has_access_key_auth():
            modes.append("access_key")
        if self.has_bearer_auth():
            modes.append("bearer")
        if self.has_cookie_auth():
            modes.append("cookie")
        return modes

    def preferred_auth_mode(self) -> str:
        if self.has_access_key_auth():
            return "access_key"
        if self.has_bearer_auth():
            return "bearer"
        if self.has_cookie_auth():
            return "cookie"
        return "none"

    def has_durable_auth(self) -> bool:
        return self.has_access_key_auth()

    def session_cookie_expires_epoch(self) -> float | None:
        cookie = self.get_cookie(WEB_SESSION_COOKIE_NAME)
        if cookie is None or cookie.expires in (None, ""):
            return None
        try:
            return float(cookie.expires)
        except (TypeError, ValueError):
            return None

    def session_cookie_expires_in_seconds(self, *, now_epoch: float | None = None) -> float | None:
        expires = self.session_cookie_expires_epoch()
        if expires is None:
            return None
        now = now_epoch if now_epoch is not None else time()
        return expires - now


def build_cookie_state_from_jar(cookie_jar: Any, base_url: str) -> list[CookieState]:
    host = urlparse(base_url).hostname
    cookies: list[CookieState] = []
    for cookie in cookie_jar:
        if host and cookie.domain and host not in cookie.domain:
            continue
        cookies.append(
            CookieState(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                secure=bool(cookie.secure),
                http_only=False,
                expires=float(cookie.expires) if cookie.expires else None,
            )
        )
    return cookies
