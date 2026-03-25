from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from mcp.server.fastmcp import FastMCP

from .service import (
    acquire_terminal_session_payload,
    build_paths_payload,
    build_status_payload,
    build_terminal_usage_guide_payload,
    create_connection_token_payload,
    expire_connection_token_payload,
    refresh_terminal_auth_payload,
    get_asset_payload,
    get_asset_access_payload,
    get_profile_payload,
    interrupt_terminal_session_payload,
    list_connection_tokens_payload,
    list_connect_methods_payload,
    list_assets_payload,
    list_nodes_payload,
    list_terminal_sessions_payload,
    probe_koko_terminal_payload,
    read_terminal_output_payload,
    resolve_terminal_target_payload,
    resize_terminal_session_payload,
    run_terminal_command_payload,
    send_terminal_input_payload,
    close_terminal_session_payload,
)
from .terminal_manager import get_terminal_session_manager

mcp = FastMCP(
    name="mcp-jumpserver-gui-sucks",
    instructions=(
        "JumpServer discovery and session-preparation tools backed by persisted "
        "CLI-derived authentication state. Use status and asset-discovery tools first. "
        "For terminal work, call jms_terminal_usage_guide first, then prefer "
        "jms_acquire_terminal_session so one shell is reused per target. "
        "Use jms_run_terminal_command for short command execution, "
        "jms_send_terminal_input plus jms_read_terminal_output for shell-style interaction, "
        "and jms_interrupt_terminal_session when a command needs to be stopped. "
        "Close managed shells with jms_close_terminal_session when the task is done."
    ),
    json_response=True,
)


@mcp.tool()
def jms_paths() -> dict[str, Any]:
    """Show runtime environment keys and resolved state paths."""
    return build_paths_payload()


@mcp.tool()
async def jms_status() -> dict[str, Any]:
    """Probe the local auth state and the current JumpServer session."""
    return await build_status_payload()


@mcp.tool()
async def jms_terminal_usage_guide() -> dict[str, Any]:
    """Return the recommended terminal workflow for coding agents using this MCP."""
    return await build_terminal_usage_guide_payload()


@mcp.tool()
async def jms_profile() -> dict[str, Any]:
    """Return the current JumpServer user profile."""
    return await get_profile_payload()


@mcp.tool()
async def jms_list_assets(
    asset: str = "",
    node: str = "",
    limit: int = 15,
    offset: int = 0,
) -> dict[str, Any]:
    """List assets through the observed perms endpoint used by the current GUI."""
    return await list_assets_payload(asset=asset, node=node, limit=limit, offset=offset)


@mcp.tool()
async def jms_get_asset(asset_id: str) -> dict[str, Any]:
    """Fetch one asset detail through the observed perms endpoint."""
    return await get_asset_payload(asset_id)


@mcp.tool()
async def jms_list_nodes() -> dict[str, Any]:
    """List the observed JumpServer node tree exposed to the current user."""
    return await list_nodes_payload()


@mcp.tool()
async def jms_list_connect_methods(protocol: str = "") -> dict[str, Any]:
    """List observed JumpServer connect methods, optionally narrowed to one protocol."""
    return await list_connect_methods_payload(protocol=protocol)


@mcp.tool()
async def jms_get_asset_access(asset_id: str) -> dict[str, Any]:
    """Summarize accounts, protocols, and connect methods available for one asset."""
    return await get_asset_access_payload(asset_id)


@mcp.tool()
async def jms_resolve_terminal_target(
    asset_ref: str,
    account_ref: str = "",
    protocol: str = "ssh",
) -> dict[str, Any]:
    """Resolve user-facing asset and account references into the concrete IDs needed by terminal entrypoints."""
    return await resolve_terminal_target_payload(
        asset_ref=asset_ref,
        account_ref=account_ref,
        protocol=protocol,
    )


@mcp.tool()
async def jms_list_connection_tokens(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """List active connection tokens without exposing the token secret value."""
    return await list_connection_tokens_payload(limit=limit, offset=offset)


@mcp.tool()
async def jms_create_connection_token(
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    reusable: bool = False,
) -> dict[str, Any]:
    """Create a connection token using a concrete account ID or a user-facing account reference."""
    return await create_connection_token_payload(
        asset_id=asset_id,
        account=account,
        protocol=protocol,
        connect_method=connect_method,
        reusable=reusable,
    )


@mcp.tool()
async def jms_expire_connection_token(token_id: str) -> dict[str, Any]:
    """Expire one connection token explicitly."""
    return await expire_connection_token_payload(token_id)


@mcp.tool()
async def jms_probe_koko_terminal(
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 80,
    rows: int = 24,
    max_messages: int = 4,
) -> dict[str, Any]:
    """Probe KoKo terminal access using a concrete account ID or a user-facing account reference."""
    return await probe_koko_terminal_payload(
        asset_id=asset_id,
        account=account,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
        max_messages=max_messages,
    )


@mcp.tool()
async def jms_acquire_terminal_session(
    asset_ref: str,
    account_ref: str = "",
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 120,
    rows: int = 32,
    startup_idle_timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    """Resolve a target and get or reuse one managed KoKo shell for that asset/account pair."""
    return await acquire_terminal_session_payload(
        asset_ref=asset_ref,
        account_ref=account_ref,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
        startup_idle_timeout_seconds=startup_idle_timeout_seconds,
    )


@mcp.tool()
async def jms_refresh_terminal_auth(force: bool = False) -> dict[str, Any]:
    """Refresh the persisted cookie-backed terminal session if it is still valid, or report that re-login is required."""
    return await refresh_terminal_auth_payload(force=force)


@mcp.tool()
async def jms_list_terminal_sessions() -> dict[str, Any]:
    """List active managed KoKo terminal sessions in the current MCP server process."""
    return await list_terminal_sessions_payload()


@mcp.tool()
async def jms_send_terminal_input(
    session_handle: str,
    data: str,
    append_newline: bool = False,
) -> dict[str, Any]:
    """Send raw input to a managed KoKo terminal session."""
    return await send_terminal_input_payload(
        session_handle=session_handle,
        data=data,
        append_newline=append_newline,
    )


@mcp.tool()
async def jms_read_terminal_output(
    session_handle: str,
    after_seq: int | None = None,
    idle_timeout_seconds: float = 1.0,
    total_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Read buffered output from a managed KoKo terminal session until idle or timeout."""
    return await read_terminal_output_payload(
        session_handle=session_handle,
        after_seq=after_seq,
        idle_timeout_seconds=idle_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
    )


@mcp.tool()
async def jms_run_terminal_command(
    session_handle: str,
    command: str,
    settle_timeout_seconds: float = 1.5,
    total_timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run one command through an already acquired managed KoKo shell."""
    return await run_terminal_command_payload(
        session_handle=session_handle,
        command=command,
        settle_timeout_seconds=settle_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
    )


@mcp.tool()
async def jms_interrupt_terminal_session(
    session_handle: str,
    signal: str = "ctrl_c",
    settle_timeout_seconds: float = 1.0,
    total_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Interrupt a running command in a managed KoKo terminal session."""
    return await interrupt_terminal_session_payload(
        session_handle=session_handle,
        signal=signal,
        settle_timeout_seconds=settle_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
    )


@mcp.tool()
async def jms_resize_terminal_session(
    session_handle: str,
    cols: int,
    rows: int,
) -> dict[str, Any]:
    """Resize a managed KoKo terminal session."""
    return await resize_terminal_session_payload(
        session_handle=session_handle,
        cols=cols,
        rows=rows,
    )


@mcp.tool()
async def jms_close_terminal_session(session_handle: str) -> dict[str, Any]:
    """Close a managed KoKo terminal session and expire its underlying connection token."""
    return await close_terminal_session_payload(session_handle)


def run_server(*, transport: str = "stdio") -> None:
    try:
        mcp.run(transport=transport)
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(
                get_terminal_session_manager().close_all_sessions(close_reason="process_exit")
            )
