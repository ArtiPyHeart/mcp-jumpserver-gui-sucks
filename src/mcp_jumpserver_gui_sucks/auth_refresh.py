from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .auth_state import AuthState, build_cookie_state_from_jar
from .config import Settings
from .errors import JumpServerAPIError, JumpServerMCPError

USER_SESSION_API_PATH = "/api/v1/authentication/user-session/"
COOKIE_REFRESH_WINDOW_SECONDS = 300.0


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


class TerminalSessionRefreshRequiredError(JumpServerMCPError):
    """Raised when terminal-only cookie auth cannot be refreshed anymore."""


@dataclass(slots=True)
class TerminalCookieRefreshResult:
    auth_state: AuthState
    refreshed: bool
    checked_at: str
    cookie_session_authenticated: bool
    cookie_expires_in_seconds: float | None
    cookie_expires_at: str | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "cookie_expires_at": self.cookie_expires_at,
            "cookie_expires_in_seconds": self.cookie_expires_in_seconds,
            "cookie_session_authenticated": self.cookie_session_authenticated,
            "reason": self.reason or None,
            "refreshed": self.refreshed,
        }


def build_cookie_only_auth_state(auth_state: AuthState) -> AuthState:
    return AuthState(
        schema_version=auth_state.schema_version,
        base_url=auth_state.base_url,
        login_source=auth_state.login_source,
        headers=dict(auth_state.headers),
        cookies=list(auth_state.cookies),
        metadata=dict(auth_state.metadata),
        created_at=auth_state.created_at,
        updated_at=auth_state.updated_at,
    )


def enrich_auth_state_with_cookies(
    auth_state: AuthState,
    *,
    settings: Settings,
    cookie_jar: Any,
    metadata_updates: dict[str, Any] | None = None,
) -> AuthState:
    cookies = build_cookie_state_from_jar(cookie_jar, auth_state.base_url or settings.base_url)
    cookie_lookup = {cookie.name: cookie.value for cookie in cookies}
    headers = dict(auth_state.headers)
    csrf_token = headers.get("X-CSRFToken") or cookie_lookup.get("jms_csrftoken")
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
    org_id = settings.org_id or headers.get("X-JMS-ORG") or cookie_lookup.get("X-JMS-ORG")
    if org_id:
        headers["X-JMS-ORG"] = org_id
    metadata = dict(auth_state.metadata)
    if metadata_updates:
        metadata.update(metadata_updates)
    return AuthState(
        schema_version=auth_state.schema_version,
        base_url=auth_state.base_url or settings.base_url,
        login_source=auth_state.login_source,
        headers=headers,
        cookies=cookies,
        bearer_token=auth_state.bearer_token,
        bearer_keyword=auth_state.bearer_keyword,
        bearer_expires_at=auth_state.bearer_expires_at,
        access_key_id=auth_state.access_key_id,
        access_key_secret=auth_state.access_key_secret,
        metadata=metadata,
        created_at=auth_state.created_at,
        updated_at=utc_now(),
    )


async def refresh_terminal_cookie_session(
    settings: Settings,
    auth_state: AuthState,
) -> TerminalCookieRefreshResult:
    if not auth_state.has_cookie_auth():
        raise TerminalSessionRefreshRequiredError(
            "No cookie-backed web session is persisted for terminal access. "
            "Re-run the CLI login flow before using KoKo terminal features."
        )

    cookie_only_state = build_cookie_only_auth_state(auth_state)
    headers = {
        "Accept": "application/json",
        **cookie_only_state.headers,
    }
    cookies = httpx.Cookies()
    for cookie in cookie_only_state.cookies:
        cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain,
            path=cookie.path,
        )

    async with httpx.AsyncClient(
        cookies=cookies,
        timeout=settings.request_timeout_seconds,
        verify=settings.verify_tls,
    ) as client:
        response = await client.get(
            f"{cookie_only_state.base_url or settings.base_url}{USER_SESSION_API_PATH}",
            headers=headers,
        )
        if response.status_code in {401, 403}:
            raise TerminalSessionRefreshRequiredError(
                "The persisted web session for KoKo terminal access has expired or is no longer valid. "
                "REST access-key authentication may still work, but terminal features require a fresh "
                "web login session. Re-run the CLI login command to renew it."
            )
        if response.status_code >= 400:
            detail = None
            try:
                payload = response.json()
            except ValueError:
                payload = response.text.strip()
            if isinstance(payload, dict):
                detail = str(payload)
            elif payload:
                detail = str(payload)
            raise JumpServerAPIError(
                method="GET",
                path=USER_SESSION_API_PATH,
                status_code=response.status_code,
                detail=detail,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JumpServerAPIError(
                method="GET",
                path=USER_SESSION_API_PATH,
                status_code=response.status_code,
                detail="Expected a JSON response.",
            ) from exc

        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path=USER_SESSION_API_PATH,
                status_code=response.status_code,
                detail="Expected a JSON object response.",
            )
        if not payload.get("ok"):
            raise TerminalSessionRefreshRequiredError(
                "JumpServer did not confirm the persisted terminal web session. "
                "Re-run the CLI login command to renew it."
            )

        refreshed_state = enrich_auth_state_with_cookies(
            auth_state,
            settings=settings,
            cookie_jar=client.cookies.jar,
            metadata_updates={
                "cookie_last_refreshed_at": utc_now(),
                "cookie_session_authenticated": True,
            },
        )
    expires_in_seconds = refreshed_state.session_cookie_expires_in_seconds()
    expires_at = None
    expires_epoch = refreshed_state.session_cookie_expires_epoch()
    if expires_epoch is not None:
        expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC).isoformat()
    return TerminalCookieRefreshResult(
        auth_state=refreshed_state,
        refreshed=True,
        checked_at=utc_now(),
        cookie_session_authenticated=True,
        cookie_expires_in_seconds=round(expires_in_seconds, 3) if expires_in_seconds is not None else None,
        cookie_expires_at=expires_at,
        reason="user-session keepalive",
    )


async def maybe_refresh_terminal_cookie_session(
    settings: Settings,
    auth_state: AuthState,
    *,
    refresh_window_seconds: float = COOKIE_REFRESH_WINDOW_SECONDS,
    force: bool = False,
) -> TerminalCookieRefreshResult:
    if not auth_state.has_cookie_auth():
        raise TerminalSessionRefreshRequiredError(
            "No cookie-backed web session is persisted for terminal access. "
            "Re-run the CLI login flow before using KoKo terminal features."
        )

    expires_in_seconds = auth_state.session_cookie_expires_in_seconds()
    expires_at = None
    expires_epoch = auth_state.session_cookie_expires_epoch()
    if expires_epoch is not None:
        expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC).isoformat()

    if not force and expires_in_seconds is not None and expires_in_seconds > refresh_window_seconds:
        return TerminalCookieRefreshResult(
            auth_state=auth_state,
            refreshed=False,
            checked_at=utc_now(),
            cookie_session_authenticated=True,
            cookie_expires_in_seconds=round(expires_in_seconds, 3),
            cookie_expires_at=expires_at,
            reason="refresh not needed yet",
        )

    return await refresh_terminal_cookie_session(settings, auth_state)
