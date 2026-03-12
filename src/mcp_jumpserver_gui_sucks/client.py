from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from .auth_state import AuthState
from .config import Settings
from .errors import ConfigError, JumpServerAPIError
from .http_signature import build_signature_authorization, make_http_date

LOGGER = logging.getLogger(__name__)


class JumpServerClient:
    def __init__(self, settings: Settings, auth_state: AuthState) -> None:
        self._settings = settings
        self._auth_state = auth_state
        self._base_url = self._resolve_base_url()

    def _resolve_base_url(self) -> str:
        if self._settings.base_url:
            return self._settings.base_url
        if self._auth_state.base_url:
            return self._auth_state.base_url.rstrip("/")
        raise ConfigError(
            "Missing JumpServer base URL. Set "
            f"{self._settings.base_url_env_name} or store it in the auth state file."
        )

    def _build_cookies(self) -> httpx.Cookies:
        cookies = httpx.Cookies()
        if self._auth_state.preferred_auth_mode() != "cookie":
            return cookies
        for cookie in self._auth_state.cookies:
            cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )
        return cookies

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            **self._auth_state.headers,
        }

        cookie_lookup = self._auth_state.cookie_lookup()

        if self._settings.org_id:
            headers["X-JMS-ORG"] = self._settings.org_id

        csrf_token = headers.get("X-CSRFToken") or cookie_lookup.get("jms_csrftoken")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token

        org_id = headers.get("X-JMS-ORG") or cookie_lookup.get("X-JMS-ORG")
        if org_id:
            headers["X-JMS-ORG"] = org_id

        return headers

    def _apply_request_auth(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> tuple[httpx.URL, dict[str, str]]:
        request_url = httpx.URL(f"{self._base_url}{path}", params=params)
        resolved_headers = dict(headers)
        auth_mode = self._auth_state.preferred_auth_mode()

        if auth_mode == "access_key":
            if "Date" not in resolved_headers:
                resolved_headers["Date"] = make_http_date(datetime.now(tz=UTC))
            resolved_headers["Authorization"] = build_signature_authorization(
                key_id=self._auth_state.access_key_id,
                secret=self._auth_state.access_key_secret,
                method=method,
                path_with_query=request_url.raw_path.decode("ascii"),
                headers=resolved_headers,
            )
        elif auth_mode == "bearer":
            resolved_headers["Authorization"] = (
                f"{self._auth_state.bearer_keyword} {self._auth_state.bearer_token}"
            )

        return request_url, resolved_headers

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        LOGGER.debug("Requesting JumpServer API %s %s", method, path)
        request_url, request_headers = self._apply_request_auth(
            method=method,
            path=path,
            params=params,
            headers=self._build_headers(),
        )
        async with httpx.AsyncClient(
            cookies=self._build_cookies(),
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_tls,
        ) as client:
            response = await client.request(
                method,
                request_url,
                headers=request_headers,
                json=json_body,
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
                method=method,
                path=path,
                status_code=response.status_code,
                detail=detail,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise JumpServerAPIError(
                method=method,
                path=path,
                status_code=response.status_code,
                detail="Expected a JSON response.",
            ) from exc

    async def check_session(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/api/v1/authentication/user-session/")
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/authentication/user-session/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def get_profile(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/api/v1/users/profile/")
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/users/profile/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def list_assets(
        self,
        *,
        asset: str = "",
        node: str = "",
        limit: int = 15,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "/api/v1/perms/users/self/assets/",
            params={
                "asset": asset,
                "node": node,
                "offset": offset,
                "limit": limit,
                "display": 1,
                "draw": 1,
            },
        )
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/perms/users/self/assets/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def get_asset(self, asset_id: str) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            f"/api/v1/perms/users/self/assets/{asset_id}/",
        )
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path=f"/api/v1/perms/users/self/assets/{asset_id}/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def list_nodes_tree(self) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            "/api/v1/perms/users/self/nodes/children/tree/",
        )
        if not isinstance(payload, list):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/perms/users/self/nodes/children/tree/",
                status_code=200,
                detail="Expected a JSON array response.",
            )
        return payload

    async def list_connect_methods(self) -> dict[str, list[dict[str, Any]]]:
        payload = await self._request_json(
            "GET",
            "/api/v1/terminal/components/connect-methods/",
        )
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/terminal/components/connect-methods/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def list_connection_tokens(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "/api/v1/authentication/connection-token/",
            params={
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/authentication/connection-token/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload

    async def create_connection_token(
        self,
        *,
        asset_id: str,
        account: str,
        protocol: str,
        connect_method: str,
        is_reusable: bool = False,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "/api/v1/authentication/connection-token/",
            json_body={
                "asset": asset_id,
                "account": account,
                "protocol": protocol,
                "connect_method": connect_method,
                "is_reusable": is_reusable,
            },
        )
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="POST",
                path="/api/v1/authentication/connection-token/",
                status_code=201,
                detail="Expected a JSON object response.",
            )
        return payload

    async def expire_connection_token(self, token_id: str) -> None:
        LOGGER.debug(
            "Requesting JumpServer API PATCH /api/v1/authentication/connection-token/%s/expire/",
            token_id,
        )
        request_url, request_headers = self._apply_request_auth(
            method="PATCH",
            path=f"/api/v1/authentication/connection-token/{token_id}/expire/",
            params=None,
            headers=self._build_headers(),
        )
        async with httpx.AsyncClient(
            cookies=self._build_cookies(),
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_tls,
        ) as client:
            response = await client.patch(request_url, headers=request_headers, json={})

        if response.status_code not in {200, 204}:
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
                method="PATCH",
                path=f"/api/v1/authentication/connection-token/{token_id}/expire/",
                status_code=response.status_code,
                detail=detail,
            )

    def get_profile_sync(self) -> dict[str, Any]:
        request_url, request_headers = self._apply_request_auth(
            method="GET",
            path="/api/v1/users/profile/",
            params=None,
            headers=self._build_headers(),
        )
        LOGGER.debug("Requesting JumpServer API GET /api/v1/users/profile/ [sync]")
        with httpx.Client(
            cookies=self._build_cookies(),
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_tls,
        ) as client:
            response = client.request("GET", request_url, headers=request_headers)

        if response.status_code >= 400:
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/users/profile/",
                status_code=response.status_code,
                detail=response.text.strip() or None,
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise JumpServerAPIError(
                method="GET",
                path="/api/v1/users/profile/",
                status_code=200,
                detail="Expected a JSON object response.",
            )
        return payload
