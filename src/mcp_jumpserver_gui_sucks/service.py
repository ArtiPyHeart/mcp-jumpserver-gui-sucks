from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .auth_state import AuthState
from .auth_refresh import (
    COOKIE_REFRESH_WINDOW_SECONDS,
    TerminalSessionRefreshRequiredError,
    maybe_refresh_terminal_cookie_session,
)
from .client import JumpServerClient
from .config import (
    APP_NAME,
    BASE_URL_ENV_NAME,
    LOG_LEVEL_ENV_NAME,
    MAX_TERMINAL_SESSIONS_ENV_NAME,
    ORG_ID_ENV_NAME,
    REQUEST_TIMEOUT_ENV_NAME,
    STATE_DIR_ENV_NAME,
    STATE_FILE_ENV_NAME,
    TERMINAL_IDLE_TIMEOUT_ENV_NAME,
    TERMINAL_REAP_INTERVAL_ENV_NAME,
    VERIFY_TLS_ENV_NAME,
    Settings,
)
from .errors import MissingAuthStateError, TargetResolutionError
from .koko import probe_koko_terminal
from .session_store import SessionStore
from .terminal_manager import get_terminal_session_manager


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def build_paths_payload(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or Settings.from_env()
    return {
        "app_name": APP_NAME,
        "env": {
            "base_url": BASE_URL_ENV_NAME,
            "log_level": LOG_LEVEL_ENV_NAME,
            "max_terminal_sessions": MAX_TERMINAL_SESSIONS_ENV_NAME,
            "org_id": ORG_ID_ENV_NAME,
            "request_timeout_seconds": REQUEST_TIMEOUT_ENV_NAME,
            "state_dir": STATE_DIR_ENV_NAME,
            "state_file": STATE_FILE_ENV_NAME,
            "terminal_idle_timeout_seconds": TERMINAL_IDLE_TIMEOUT_ENV_NAME,
            "terminal_reap_interval_seconds": TERMINAL_REAP_INTERVAL_ENV_NAME,
            "verify_tls": VERIFY_TLS_ENV_NAME,
        },
        "resolved": {
            "base_url": resolved.base_url or None,
            "log_level": resolved.log_level,
            "max_terminal_sessions": resolved.max_terminal_sessions,
            "org_id": resolved.org_id or None,
            "request_timeout_seconds": resolved.request_timeout_seconds,
            "state_dir": str(resolved.state_dir),
            "state_file": str(resolved.state_file),
            "terminal_idle_timeout_seconds": resolved.terminal_idle_timeout_seconds,
            "terminal_reap_interval_seconds": resolved.terminal_reap_interval_seconds,
            "verify_tls": resolved.verify_tls,
        },
    }


def load_runtime() -> tuple[Settings, SessionStore, AuthState | None]:
    settings = Settings.from_env()
    store = SessionStore(settings.state_file)
    auth_state = store.load()
    return settings, store, auth_state


def require_auth_state() -> tuple[Settings, SessionStore, AuthState]:
    settings, store, auth_state = load_runtime()
    if auth_state is None:
        raise MissingAuthStateError(
            "No persisted auth state was found. "
            f"Use {STATE_FILE_ENV_NAME} or {STATE_DIR_ENV_NAME} to point to a saved session."
        )
    return settings, store, auth_state


async def ensure_terminal_auth_state(
    *,
    force_refresh: bool = False,
) -> tuple[Settings, SessionStore, AuthState, dict[str, Any]]:
    settings, store, auth_state = require_auth_state()
    refresh_payload: dict[str, Any] = {
        "checked_at": utc_now(),
        "cookie_refresh_window_seconds": COOKIE_REFRESH_WINDOW_SECONDS,
        "refreshed": False,
    }
    if not auth_state.has_cookie_auth():
        raise TerminalSessionRefreshRequiredError(
            "No persisted cookie-backed web session is available for terminal access. "
            "Re-run the CLI login flow to renew the KoKo terminal session."
        )
    refresh_result = await maybe_refresh_terminal_cookie_session(
        settings,
        auth_state,
        refresh_window_seconds=COOKIE_REFRESH_WINDOW_SECONDS,
        force=force_refresh,
    )
    refreshed_auth_state = refresh_result.auth_state
    refresh_payload.update(refresh_result.to_dict())
    if refresh_result.refreshed:
        store.save(refreshed_auth_state)
    return settings, store, refreshed_auth_state, refresh_payload


async def build_status_payload() -> dict[str, Any]:
    settings, store, auth_state = load_runtime()
    payload: dict[str, Any] = {
        "checked_at": utc_now(),
        "project": APP_NAME,
        "paths": build_paths_payload(settings)["resolved"],
        "effective_base_url": settings.base_url or (auth_state.base_url if auth_state else None),
        "state": store.describe(),
        "authenticated": False,
    }

    if auth_state is None:
        payload["message"] = "No persisted auth state file was found."
        return payload

    payload["state"].update(
        {
            "access_key_configured": auth_state.has_access_key_auth(),
            "auth_modes": auth_state.auth_modes(),
            "bearer_expires_at": auth_state.bearer_expires_at or None,
            "base_url": auth_state.base_url or None,
            "cookie_names": auth_state.cookie_names(),
            "cookie_refresh_window_seconds": COOKIE_REFRESH_WINDOW_SECONDS,
            "cookie_session_expires_in_seconds": round(auth_state.session_cookie_expires_in_seconds(), 3)
            if auth_state.session_cookie_expires_in_seconds() is not None
            else None,
            "header_names": auth_state.header_names(),
            "login_source": auth_state.login_source,
            "preferred_auth_mode": auth_state.preferred_auth_mode(),
        }
    )

    expires_epoch = auth_state.session_cookie_expires_epoch()
    if expires_epoch is not None:
        payload["state"]["cookie_session_expires_at"] = datetime.fromtimestamp(
            expires_epoch,
            tz=UTC,
        ).isoformat()

    if auth_state.has_cookie_auth():
        try:
            refresh_result = await maybe_refresh_terminal_cookie_session(
                settings,
                auth_state,
                refresh_window_seconds=COOKIE_REFRESH_WINDOW_SECONDS,
            )
        except Exception as exc:
            payload["cookie_refresh_error"] = str(exc)
        else:
            payload["cookie_refresh"] = refresh_result.to_dict()
            auth_state = refresh_result.auth_state
            if refresh_result.refreshed:
                store.save(auth_state)
                payload["state"]["cookie_names"] = auth_state.cookie_names()
                payload["state"]["header_names"] = auth_state.header_names()
                refreshed_expires = auth_state.session_cookie_expires_epoch()
                if refreshed_expires is not None:
                    payload["state"]["cookie_session_expires_at"] = datetime.fromtimestamp(
                        refreshed_expires,
                        tz=UTC,
                    ).isoformat()
                    payload["state"]["cookie_session_expires_in_seconds"] = round(
                        auth_state.session_cookie_expires_in_seconds() or 0.0,
                        3,
                    )

    client = JumpServerClient(settings, auth_state)
    if auth_state.preferred_auth_mode() == "cookie":
        try:
            session_payload = await client.check_session()
        except Exception as exc:
            payload["session_probe_error"] = str(exc)
        else:
            payload["session_probe"] = session_payload
            payload["authenticated"] = bool(session_payload.get("ok"))
    else:
        payload["session_probe_skipped"] = (
            "The user-session probe is only reliable for cookie-backed auth."
        )

    if auth_state.has_cookie_auth():
        cookie_probe_state = AuthState(
            base_url=auth_state.base_url,
            headers=auth_state.headers,
            cookies=auth_state.cookies,
        )
        cookie_probe_client = JumpServerClient(settings, cookie_probe_state)
        try:
            cookie_session_payload = await cookie_probe_client.check_session()
        except Exception as exc:
            payload["cookie_session_probe_error"] = str(exc)
            payload["cookie_session_authenticated"] = False
        else:
            payload["cookie_session_probe"] = cookie_session_payload
            payload["cookie_session_authenticated"] = bool(cookie_session_payload.get("ok"))
    else:
        payload["cookie_session_probe_skipped"] = "No cookie-backed state is currently persisted."

    try:
        profile_payload = await client.get_profile()
    except Exception as exc:
        payload["profile_probe_error"] = str(exc)
    else:
        payload["profile_probe"] = {
            "id": profile_payload.get("id"),
            "mfa_enabled": profile_payload.get("mfa_enabled"),
            "name": profile_payload.get("name"),
            "username": profile_payload.get("username"),
        }
        payload["authenticated"] = True
    return payload


async def get_profile_payload() -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    return await client.get_profile()


def normalize_node_entry(raw: dict[str, Any]) -> dict[str, Any]:
    meta = raw.get("meta")
    meta_data = meta.get("data") if isinstance(meta, dict) else {}
    if not isinstance(meta_data, dict):
        meta_data = {}

    return {
        "tree_key": raw.get("id"),
        "parent_tree_key": raw.get("pId") or None,
        "node_id": meta_data.get("id"),
        "node_key": meta_data.get("key"),
        "name": meta_data.get("value") or raw.get("title") or raw.get("name"),
        "display_name": raw.get("name") or raw.get("title"),
        "title": raw.get("title") or raw.get("name"),
        "node_type": meta.get("type") if isinstance(meta, dict) else None,
        "is_parent": bool(raw.get("isParent")),
        "open": bool(raw.get("open")),
    }


def normalize_connect_method_entry(protocol: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": protocol,
        "component": raw.get("component"),
        "type": raw.get("type"),
        "endpoint_protocol": raw.get("endpoint_protocol"),
        "value": raw.get("value"),
        "label": raw.get("label"),
    }


def normalize_asset_account_entry(raw: dict[str, Any]) -> dict[str, Any]:
    actions = raw.get("actions")
    if not isinstance(actions, list):
        actions = []

    return {
        "id": raw.get("id"),
        "alias": raw.get("alias"),
        "name": raw.get("name"),
        "username": raw.get("username"),
        "secret_type": raw.get("secret_type"),
        "has_secret": bool(raw.get("has_secret")),
        "has_username": bool(raw.get("has_username")),
        "actions": actions,
        "action_values": [item.get("value") for item in actions if isinstance(item, dict)],
    }


def normalize_asset_protocol_entry(
    raw: dict[str, Any],
    *,
    connect_methods: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    protocol_name = raw.get("name")
    methods = connect_methods.get(protocol_name, [])
    return {
        "name": protocol_name,
        "port": raw.get("port"),
        "public": raw.get("public"),
        "setting": raw.get("setting") if isinstance(raw.get("setting"), dict) else {},
        "connect_methods": methods,
        "connect_method_values": [item.get("value") for item in methods],
    }


def normalize_connection_token_entry(raw: dict[str, Any]) -> dict[str, Any]:
    actions = raw.get("actions")
    if not isinstance(actions, list):
        actions = []

    connect_options = raw.get("connect_options")
    if not isinstance(connect_options, dict):
        connect_options = {}

    from_ticket_info = raw.get("from_ticket_info")
    if not isinstance(from_ticket_info, dict):
        from_ticket_info = {}

    return {
        "id": raw.get("id"),
        "user": raw.get("user"),
        "asset": raw.get("asset"),
        "account": raw.get("account"),
        "input_username": raw.get("input_username") or None,
        "connect_method": raw.get("connect_method"),
        "connect_options": connect_options,
        "protocol": raw.get("protocol"),
        "actions": actions,
        "action_values": [item.get("value") for item in actions if isinstance(item, dict)],
        "from_ticket": raw.get("from_ticket"),
        "from_ticket_info": from_ticket_info,
        "org_id": raw.get("org_id"),
        "org_name": raw.get("org_name"),
        "user_display": raw.get("user_display"),
        "asset_display": raw.get("asset_display"),
        "face_monitor_token_present": bool(raw.get("face_monitor_token")),
        "expire_time": raw.get("expire_time"),
        "is_active": raw.get("is_active"),
        "is_reusable": raw.get("is_reusable"),
        "date_expired": raw.get("date_expired"),
        "date_created": raw.get("date_created"),
        "date_updated": raw.get("date_updated"),
    }


def normalize_match_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def looks_like_uuid(value: str) -> bool:
    raw = value.strip()
    return len(raw) == 36 and raw.count("-") == 4


def build_asset_match_values(raw: dict[str, Any]) -> list[str]:
    return [
        normalize_match_text(raw.get("id")),
        normalize_match_text(raw.get("name")),
        normalize_match_text(raw.get("address")),
        normalize_match_text(raw.get("domain")),
    ]


def build_account_match_values(raw: dict[str, Any]) -> list[str]:
    return [
        normalize_match_text(raw.get("id")),
        normalize_match_text(raw.get("alias")),
        normalize_match_text(raw.get("name")),
        normalize_match_text(raw.get("username")),
    ]


def build_asset_resolution_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "address": raw.get("address"),
        "domain": raw.get("domain"),
        "org_name": raw.get("org_name"),
        "platform": raw.get("platform"),
    }


def build_account_resolution_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "alias": raw.get("alias"),
        "name": raw.get("name"),
        "username": raw.get("username"),
        "action_values": raw.get("action_values") or [],
    }


def resolve_unique_match(
    *,
    reference: str,
    candidates: list[dict[str, Any]],
    value_builder,
    label: str,
) -> tuple[dict[str, Any], str]:
    normalized_reference = normalize_match_text(reference)
    if not normalized_reference:
        raise TargetResolutionError(f"Missing {label} reference.")

    exact_matches = [
        candidate
        for candidate in candidates
        if normalized_reference in value_builder(candidate)
        and normalized_reference in {value for value in value_builder(candidate) if value}
    ]
    if len(exact_matches) == 1:
        return exact_matches[0], "exact"
    if len(exact_matches) > 1:
        raise TargetResolutionError(
            f"Multiple {label} candidates matched {reference!r} exactly."
        )

    partial_matches = [
        candidate
        for candidate in candidates
        if any(normalized_reference and normalized_reference in value for value in value_builder(candidate))
    ]
    if len(partial_matches) == 1:
        return partial_matches[0], "partial"
    if len(partial_matches) > 1:
        raise TargetResolutionError(
            f"Multiple {label} candidates matched {reference!r}. Narrow the reference."
        )
    raise TargetResolutionError(f"No {label} matched {reference!r}.")


async def list_assets_payload(
    *,
    asset: str = "",
    node: str = "",
    limit: int = 15,
    offset: int = 0,
) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    return await client.list_assets(asset=asset, node=node, limit=limit, offset=offset)


async def get_asset_payload(asset_id: str) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    return await client.get_asset(asset_id)


async def list_nodes_payload() -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    raw_nodes = await client.list_nodes_tree()
    results = [normalize_node_entry(item) for item in raw_nodes if isinstance(item, dict)]
    return {
        "count": len(results),
        "results": results,
    }


async def list_connect_methods_payload(protocol: str = "") -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    raw_map = await client.list_connect_methods()

    filtered_protocol = protocol.strip().lower()
    protocol_names = sorted(
        key for key, value in raw_map.items() if isinstance(key, str) and isinstance(value, list)
    )
    if filtered_protocol:
        protocol_names = [name for name in protocol_names if name == filtered_protocol]

    results = [
        {
            "protocol": name,
            "methods": [
                normalize_connect_method_entry(name, item)
                for item in raw_map.get(name, [])
                if isinstance(item, dict)
            ],
        }
        for name in protocol_names
    ]
    for item in results:
        item["method_count"] = len(item["methods"])
        item["method_values"] = [entry.get("value") for entry in item["methods"]]

    return {
        "protocol_count": len(results),
        "protocols": protocol_names,
        "results": results,
    }


async def get_asset_access_payload(asset_id: str) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)

    asset_payload = await client.get_asset(asset_id)
    connect_method_map = await client.list_connect_methods()
    normalized_connect_methods = {
        protocol: [
            normalize_connect_method_entry(protocol, item)
            for item in items
            if isinstance(item, dict)
        ]
        for protocol, items in connect_method_map.items()
        if isinstance(protocol, str) and isinstance(items, list)
    }

    raw_accounts = asset_payload.get("permed_accounts")
    if not isinstance(raw_accounts, list):
        raw_accounts = []

    raw_protocols = asset_payload.get("permed_protocols")
    if not isinstance(raw_protocols, list):
        raw_protocols = []

    return {
        "asset": {
            "id": asset_payload.get("id"),
            "name": asset_payload.get("name"),
            "address": asset_payload.get("address"),
            "domain": asset_payload.get("domain"),
            "org_name": asset_payload.get("org_name"),
            "platform": asset_payload.get("platform"),
            "nodes_display": asset_payload.get("nodes_display"),
        },
        "account_count": len(raw_accounts),
        "accounts": [
            normalize_asset_account_entry(item)
            for item in raw_accounts
            if isinstance(item, dict)
        ],
        "protocol_count": len(raw_protocols),
        "protocols": [
            normalize_asset_protocol_entry(item, connect_methods=normalized_connect_methods)
            for item in raw_protocols
            if isinstance(item, dict)
        ],
    }


async def resolve_terminal_target_payload(
    *,
    asset_ref: str,
    account_ref: str = "",
    protocol: str = "ssh",
) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)

    asset_candidates: list[dict[str, Any]] = []
    asset_match_strategy = ""
    if looks_like_uuid(asset_ref):
        try:
            asset_payload = await client.get_asset(asset_ref)
        except Exception:
            asset_payload = None
        if isinstance(asset_payload, dict) and asset_payload.get("id"):
            asset_candidates = [asset_payload]
            asset_match_strategy = "id"

    if not asset_candidates:
        asset_list_payload = await client.list_assets(asset=asset_ref, limit=100, offset=0)
        raw_asset_results = asset_list_payload.get("results")
        if not isinstance(raw_asset_results, list):
            raw_asset_results = []
        asset_candidates = [item for item in raw_asset_results if isinstance(item, dict)]
        if not asset_candidates:
            raise TargetResolutionError(f"No asset matched {asset_ref!r}.")
        resolved_asset, asset_match_strategy = resolve_unique_match(
            reference=asset_ref,
            candidates=asset_candidates,
            value_builder=build_asset_match_values,
            label="asset",
        )
        asset_candidates = [resolved_asset]

    resolved_asset = asset_candidates[0]
    asset_access = await get_asset_access_payload(str(resolved_asset.get("id")))
    accounts = list(asset_access.get("accounts", []))
    protocols = [
        item.get("name")
        for item in list(asset_access.get("protocols", []))
        if isinstance(item, dict) and item.get("name")
    ]
    if protocol and protocol not in protocols:
        raise TargetResolutionError(
            f"Asset {resolved_asset.get('name')!r} does not expose protocol {protocol!r}. "
            f"Available protocols: {protocols}"
        )

    account_match_strategy = ""
    if account_ref.strip():
        resolved_account, account_match_strategy = resolve_unique_match(
            reference=account_ref,
            candidates=accounts,
            value_builder=build_account_match_values,
            label="account",
        )
    else:
        if not accounts:
            raise TargetResolutionError(
                f"Asset {resolved_asset.get('name')!r} does not expose any permitted accounts."
            )
        if len(accounts) == 1:
            resolved_account = accounts[0]
            account_match_strategy = "single"
        else:
            raise TargetResolutionError(
                "The asset exposes multiple permitted accounts. Provide an account reference."
            )

    return {
        "asset_match_strategy": asset_match_strategy,
        "account_match_strategy": account_match_strategy,
        "asset": build_asset_resolution_summary(resolved_asset),
        "account": build_account_resolution_summary(resolved_account),
        "available_protocols": protocols,
    }


async def resolve_terminal_tool_target(
    *,
    asset_ref: str,
    account_ref: str,
    protocol: str = "ssh",
) -> tuple[str, str, dict[str, Any]]:
    """Resolve user-facing terminal inputs into concrete asset/account IDs."""
    resolved = await resolve_terminal_target_payload(
        asset_ref=asset_ref,
        account_ref=account_ref,
        protocol=protocol,
    )
    return (
        str(resolved["asset"]["id"]),
        str(resolved["account"]["id"]),
        resolved,
    )


async def list_connection_tokens_payload(
    *,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    payload = await client.list_connection_tokens(limit=limit, offset=offset)
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []

    return {
        "count": payload.get("count", len(raw_results)),
        "next": payload.get("next"),
        "previous": payload.get("previous"),
        "results": [
            normalize_connection_token_entry(item)
            for item in raw_results
            if isinstance(item, dict)
        ],
    }


async def create_connection_token_payload(
    *,
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    reusable: bool = False,
) -> dict[str, Any]:
    resolved_asset_id, resolved_account_id, resolved_target = await resolve_terminal_tool_target(
        asset_ref=asset_id,
        account_ref=account,
        protocol=protocol,
    )
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    payload = await client.create_connection_token(
        asset_id=resolved_asset_id,
        account=resolved_account_id,
        protocol=protocol,
        connect_method=connect_method,
        is_reusable=reusable,
    )
    return {
        "created": True,
        "resolved_target": resolved_target,
        "token": normalize_connection_token_entry(payload),
    }


async def expire_connection_token_payload(token_id: str) -> dict[str, Any]:
    settings, _, auth_state = require_auth_state()
    client = JumpServerClient(settings, auth_state)
    await client.expire_connection_token(token_id)
    return {
        "expired": True,
        "token_id": token_id,
    }


async def probe_koko_terminal_payload(
    *,
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 80,
    rows: int = 24,
    max_messages: int = 4,
) -> dict[str, Any]:
    settings, _, auth_state, terminal_auth = await ensure_terminal_auth_state(
        force_refresh=True,
    )
    resolved_asset_id, resolved_account_id, resolved_target = await resolve_terminal_tool_target(
        asset_ref=asset_id,
        account_ref=account,
        protocol=protocol,
    )
    payload = await probe_koko_terminal(
        settings,
        auth_state,
        asset_id=resolved_asset_id,
        account=resolved_account_id,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
        max_messages=max_messages,
    )
    payload["resolved_target"] = resolved_target
    payload["terminal_auth"] = terminal_auth
    return payload


async def execute_koko_command_payload(
    *,
    asset_id: str,
    account: str,
    command: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 120,
    rows: int = 32,
    startup_idle_timeout_seconds: float = 1.5,
    command_idle_timeout_seconds: float = 1.5,
    total_timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    settings, _, auth_state, terminal_auth = await ensure_terminal_auth_state(
        force_refresh=True,
    )
    resolved_asset_id, resolved_account_id, resolved_target = await resolve_terminal_tool_target(
        asset_ref=asset_id,
        account_ref=account,
        protocol=protocol,
    )
    manager = get_terminal_session_manager()
    payload = await manager.execute_command(
        settings,
        auth_state,
        asset_id=resolved_asset_id,
        account=resolved_account_id,
        command=command,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
        startup_idle_timeout_seconds=startup_idle_timeout_seconds,
        command_idle_timeout_seconds=command_idle_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        reuse_existing=True,
    )
    payload["resolved_target"] = resolved_target
    payload["terminal_auth"] = terminal_auth
    return payload


async def list_terminal_sessions_payload() -> dict[str, Any]:
    settings = Settings.from_env()
    manager = get_terminal_session_manager()
    await manager.prepare(settings)
    return await manager.list_sessions()


async def open_terminal_session_payload(
    *,
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 120,
    rows: int = 32,
    startup_idle_timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    settings, _, auth_state, terminal_auth = await ensure_terminal_auth_state(
        force_refresh=True,
    )
    resolved_asset_id, resolved_account_id, resolved_target = await resolve_terminal_tool_target(
        asset_ref=asset_id,
        account_ref=account,
        protocol=protocol,
    )
    manager = get_terminal_session_manager()
    payload = await manager.open_session(
        settings,
        auth_state,
        asset_id=resolved_asset_id,
        account=resolved_account_id,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
        startup_idle_timeout_seconds=startup_idle_timeout_seconds,
    )
    payload["resolved_target"] = resolved_target
    payload["terminal_auth"] = terminal_auth
    return payload


async def refresh_terminal_auth_payload(*, force: bool = False) -> dict[str, Any]:
    _, _, auth_state, refresh_payload = await ensure_terminal_auth_state(
        force_refresh=force,
    )
    return {
        "authenticated": True,
        "auth_modes": auth_state.auth_modes(),
        "base_url": auth_state.base_url or None,
        "login_source": auth_state.login_source,
        "terminal_auth": refresh_payload,
    }


async def write_terminal_session_payload(
    *,
    session_handle: str,
    data: str,
    append_newline: bool = False,
) -> dict[str, Any]:
    settings = Settings.from_env()
    manager = get_terminal_session_manager()
    await manager.prepare(settings)
    return await manager.write_session(
        session_handle,
        data=data,
        append_newline=append_newline,
    )


async def read_terminal_session_payload(
    *,
    session_handle: str,
    idle_timeout_seconds: float = 1.0,
    total_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    settings = Settings.from_env()
    manager = get_terminal_session_manager()
    await manager.prepare(settings)
    return await manager.read_session(
        session_handle,
        idle_timeout_seconds=idle_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
    )


async def resize_terminal_session_payload(
    *,
    session_handle: str,
    cols: int,
    rows: int,
) -> dict[str, Any]:
    settings = Settings.from_env()
    manager = get_terminal_session_manager()
    await manager.prepare(settings)
    return await manager.resize_session(
        session_handle,
        cols=cols,
        rows=rows,
    )


async def close_terminal_session_payload(session_handle: str) -> dict[str, Any]:
    settings = Settings.from_env()
    manager = get_terminal_session_manager()
    await manager.prepare(settings)
    return await manager.close_session(session_handle)
