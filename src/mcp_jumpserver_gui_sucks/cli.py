from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Sequence
from urllib.parse import urlparse

from .auth_state import AuthState, CookieState
from .cli_login import run_cli_login
from .config import Settings
from .logging_utils import configure_logging
from .server import run_server
from .service import (
    build_paths_payload,
    build_status_payload,
    close_terminal_session_payload,
    probe_koko_terminal_payload,
    refresh_terminal_auth_payload,
    resolve_terminal_target_payload,
    resize_terminal_session_payload,
    acquire_terminal_session_payload,
    interrupt_terminal_session_payload,
    read_terminal_output_payload,
    run_terminal_command_payload,
    send_terminal_input_payload,
)
from .session_store import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-jumpserver-gui-sucks")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the MCP server.")
    serve_parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="FastMCP transport to expose.",
    )

    subparsers.add_parser("paths", help="Print resolved runtime paths and environment keys.")
    subparsers.add_parser("doctor", help="Probe local state and the current JumpServer session.")
    refresh_session_parser = subparsers.add_parser(
        "refresh-session",
        help="Refresh the persisted cookie-backed terminal web session if it is still valid.",
    )
    refresh_session_parser.add_argument(
        "--force",
        action="store_true",
        help="Force a user-session keepalive request even when the session cookie is not near expiry.",
    )

    login_parser = subparsers.add_parser(
        "login",
        help="Run CLI-first JumpServer login, complete MFA in the terminal, and persist auth state.",
    )
    login_parser.add_argument(
        "--base-url",
        help="JumpServer base URL. Defaults to the environment value.",
    )
    login_parser.add_argument(
        "--username",
        required=True,
        help="JumpServer username for CLI login.",
    )
    login_parser.add_argument(
        "--org-id",
        help="Explicit organization ID to persist and send on API requests.",
    )
    login_parser.add_argument(
        "--mfa-type",
        default="",
        help="Preferred MFA type for the primary login step.",
    )
    login_parser.add_argument(
        "--confirm-mfa-type",
        default="",
        help="Preferred MFA type for the durable access-key confirmation step.",
    )
    login_parser.add_argument(
        "--allow-ephemeral",
        action="store_true",
        help="Allow saving a cookie-backed web session when durable access-key setup is not possible.",
    )

    koko_probe_parser = subparsers.add_parser(
        "koko-probe",
        help="Create a short-lived token and probe the KoKo terminal websocket with the persisted web session.",
    )
    koko_probe_parser.add_argument("--asset-id", required=True, help="Target asset UUID.")
    koko_probe_parser.add_argument(
        "--account",
        required=True,
        help="Permitted account identifier returned by asset access discovery.",
    )
    koko_probe_parser.add_argument("--protocol", default="ssh", help="Target protocol. Defaults to ssh.")
    koko_probe_parser.add_argument(
        "--connect-method",
        default="web_cli",
        help="Connect method to use when creating the token. Defaults to web_cli.",
    )
    koko_probe_parser.add_argument("--cols", type=int, default=80, help="Initial terminal columns.")
    koko_probe_parser.add_argument("--rows", type=int, default=24, help="Initial terminal rows.")
    koko_probe_parser.add_argument(
        "--max-messages",
        type=int,
        default=4,
        help="How many early websocket frames to observe before stopping.",
    )

    terminal_exec_parser = subparsers.add_parser(
        "terminal-exec",
        help="Run one command through KoKo Web CLI using the persisted authenticated web session.",
    )
    terminal_exec_parser.add_argument("--asset-id", help="Target asset UUID.")
    terminal_exec_parser.add_argument(
        "--asset-ref",
        default="",
        help="User-facing asset reference, for example asset name or address.",
    )
    terminal_exec_parser.add_argument(
        "--account",
        default="",
        help="Permitted account identifier returned by asset access discovery.",
    )
    terminal_exec_parser.add_argument(
        "--account-ref",
        default="",
        help="User-facing account reference, for example alias, name, or username.",
    )
    terminal_exec_parser.add_argument(
        "--command",
        dest="remote_command",
        required=True,
        help="Command text to send through the remote shell.",
    )
    terminal_exec_parser.add_argument("--protocol", default="ssh", help="Target protocol. Defaults to ssh.")
    terminal_exec_parser.add_argument(
        "--connect-method",
        default="web_cli",
        help="Connect method to use when creating the token. Defaults to web_cli.",
    )
    terminal_exec_parser.add_argument("--cols", type=int, default=120, help="Initial terminal columns.")
    terminal_exec_parser.add_argument("--rows", type=int, default=32, help="Initial terminal rows.")
    terminal_exec_parser.add_argument(
        "--startup-idle-timeout-seconds",
        type=float,
        default=1.5,
        help="How long the client waits for startup output to go idle before sending the command.",
    )
    terminal_exec_parser.add_argument(
        "--command-idle-timeout-seconds",
        type=float,
        default=1.5,
        help="How long the client waits for command output to go idle before stopping.",
    )
    terminal_exec_parser.add_argument(
        "--total-timeout-seconds",
        type=float,
        default=20.0,
        help="Hard timeout for the command phase.",
    )

    terminal_shell_parser = subparsers.add_parser(
        "terminal-shell",
        help="Open a line-oriented interactive KoKo shell in the current CLI process.",
    )
    terminal_shell_parser.add_argument("--asset-id", help="Target asset UUID.")
    terminal_shell_parser.add_argument(
        "--asset-ref",
        default="",
        help="User-facing asset reference, for example asset name or address.",
    )
    terminal_shell_parser.add_argument(
        "--account",
        default="",
        help="Permitted account identifier returned by asset access discovery.",
    )
    terminal_shell_parser.add_argument(
        "--account-ref",
        default="",
        help="User-facing account reference, for example alias, name, or username.",
    )
    terminal_shell_parser.add_argument("--protocol", default="ssh", help="Target protocol. Defaults to ssh.")
    terminal_shell_parser.add_argument(
        "--connect-method",
        default="web_cli",
        help="Connect method to use when creating the token. Defaults to web_cli.",
    )
    terminal_shell_parser.add_argument("--cols", type=int, default=120, help="Initial terminal columns.")
    terminal_shell_parser.add_argument("--rows", type=int, default=32, help="Initial terminal rows.")
    terminal_shell_parser.add_argument(
        "--startup-idle-timeout-seconds",
        type=float,
        default=1.5,
        help="How long the client waits for startup output to go idle before entering the REPL.",
    )
    terminal_shell_parser.add_argument(
        "--read-idle-timeout-seconds",
        type=float,
        default=1.0,
        help="How long the client waits for shell output to go idle after each read.",
    )
    terminal_shell_parser.add_argument(
        "--read-total-timeout-seconds",
        type=float,
        default=10.0,
        help="Hard timeout for each shell read step.",
    )

    resolve_target_parser = subparsers.add_parser(
        "resolve-target",
        help="Resolve a user-facing asset reference and account reference into concrete terminal IDs.",
    )
    resolve_target_parser.add_argument(
        "--asset-ref",
        required=True,
        help="Asset reference, for example asset name, address, or UUID.",
    )
    resolve_target_parser.add_argument(
        "--account-ref",
        default="",
        help="Account reference, for example alias, name, username, or UUID.",
    )
    resolve_target_parser.add_argument(
        "--protocol",
        default="ssh",
        help="Desired protocol to validate against the asset access summary.",
    )

    save_state_parser = subparsers.add_parser(
        "save-state",
        help="Persist an auth state file from explicit headers and cookies.",
    )
    save_state_parser.add_argument("--base-url", help="JumpServer base URL. Defaults to the environment value.")
    save_state_parser.add_argument(
        "--org-id",
        default="",
        help="Explicit organization ID to persist as X-JMS-ORG.",
    )
    save_state_parser.add_argument(
        "--login-source",
        default="manual",
        help="How this state was acquired, for example manual or cli-login.",
    )
    save_state_parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Header to persist. Repeat as needed.",
    )
    save_state_parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Cookie to persist. Repeat as needed.",
    )
    save_state_parser.add_argument(
        "--cookie-domain",
        default="",
        help="Override cookie domain. Defaults to the host from the base URL.",
    )
    save_state_parser.add_argument(
        "--cookie-path",
        default="/",
        help="Cookie path to persist. Defaults to '/'.",
    )
    save_state_parser.add_argument(
        "--bearer-token",
        default="",
        help="Optional bearer token to persist for debugging or short-lived automation.",
    )
    save_state_parser.add_argument(
        "--bearer-keyword",
        default="Bearer",
        help="Bearer token keyword. Defaults to Bearer.",
    )
    save_state_parser.add_argument(
        "--bearer-expires-at",
        default="",
        help="Bearer token expiry timestamp to persist.",
    )
    save_state_parser.add_argument(
        "--access-key-id",
        default="",
        help="Optional durable access-key ID to persist.",
    )
    save_state_parser.add_argument(
        "--access-key-secret",
        default="",
        help="Optional durable access-key secret to persist.",
    )

    subparsers.add_parser("clear-state", help="Delete the persisted auth state file.")

    return parser


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_key_value(raw: str, *, label: str) -> tuple[str, str]:
    if "=" not in raw:
        raise ValueError(f"{label} entries must look like KEY=VALUE.")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"{label} keys must not be empty.")
    return key, value


def save_state_command(args: argparse.Namespace, settings: Settings) -> int:
    base_url = (args.base_url or settings.base_url).strip().rstrip("/")
    if not base_url:
        raise ValueError(
            "Missing base URL. Provide --base-url or set "
            f"{settings.base_url_env_name}."
        )

    parsed_url = urlparse(base_url)
    cookie_domain = args.cookie_domain.strip() or parsed_url.hostname

    headers = dict(parse_key_value(item, label="header") for item in args.header)
    cookies = [
        CookieState(
            name=name,
            value=value,
            domain=cookie_domain,
            path=args.cookie_path,
        )
        for name, value in (parse_key_value(item, label="cookie") for item in args.cookie)
    ]

    cookie_lookup = {cookie.name: cookie.value for cookie in cookies}
    if "X-CSRFToken" not in headers and "jms_csrftoken" in cookie_lookup:
        headers["X-CSRFToken"] = cookie_lookup["jms_csrftoken"]
    explicit_org_id = args.org_id.strip() or settings.org_id
    if explicit_org_id:
        headers["X-JMS-ORG"] = explicit_org_id
    elif "X-JMS-ORG" not in headers and "X-JMS-ORG" in cookie_lookup:
        headers["X-JMS-ORG"] = cookie_lookup["X-JMS-ORG"]

    auth_state = AuthState(
        base_url=base_url,
        login_source=args.login_source,
        headers=headers,
        cookies=cookies,
        bearer_token=args.bearer_token.strip(),
        bearer_keyword=args.bearer_keyword.strip() or "Bearer",
        bearer_expires_at=args.bearer_expires_at.strip(),
        access_key_id=args.access_key_id.strip(),
        access_key_secret=args.access_key_secret.strip(),
        metadata={"saved_by": "save-state"},
    )
    store = SessionStore(settings.state_file)
    store.save(auth_state)

    print_json(
        {
            "saved": True,
            "state_file": str(settings.state_file),
            "base_url": base_url,
            "cookie_domain": cookie_domain,
            "cookie_names": auth_state.cookie_names(),
            "header_names": auth_state.header_names(),
            "login_source": auth_state.login_source,
            "auth_modes": auth_state.auth_modes(),
        }
    )
    return 0


def clear_state_command(settings: Settings) -> int:
    store = SessionStore(settings.state_file)
    deleted = store.clear()
    print_json(
        {
            "cleared": deleted,
            "state_file": str(settings.state_file),
        }
    )
    return 0


def login_command(args: argparse.Namespace, settings: Settings) -> int:
    base_url = (args.base_url or settings.base_url).strip().rstrip("/")
    if not base_url:
        raise ValueError(
            "Missing base URL. Provide --base-url or set "
            f"{settings.base_url_env_name}."
        )

    result = run_cli_login(
        settings,
        base_url=base_url,
        username=args.username.strip(),
        org_id=(args.org_id or settings.org_id).strip(),
        login_mfa_type=args.mfa_type.strip(),
        confirm_mfa_type=args.confirm_mfa_type.strip(),
        allow_ephemeral=bool(args.allow_ephemeral),
    )
    SessionStore(settings.state_file).save(result.auth_state)
    print_json(result.to_dict())
    return 0


def koko_probe_command(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        probe_koko_terminal_payload(
            asset_id=args.asset_id,
            account=args.account,
            protocol=args.protocol,
            connect_method=args.connect_method,
            cols=args.cols,
            rows=args.rows,
            max_messages=args.max_messages,
        )
    )
    print_json(payload)
    return 0 if payload.get("ws_connected") else 1


def terminal_exec_command(args: argparse.Namespace) -> int:
    asset_id, account_id, resolved_target = asyncio.run(resolve_terminal_target_args(args))
    opened = asyncio.run(
        acquire_terminal_session_payload(
            asset_ref=asset_id,
            account_ref=account_id,
            protocol=args.protocol,
            connect_method=args.connect_method,
            cols=args.cols,
            rows=args.rows,
            startup_idle_timeout_seconds=args.startup_idle_timeout_seconds,
        )
    )
    session_handle = str(opened["session_handle"])
    try:
        payload = asyncio.run(
            run_terminal_command_payload(
                session_handle=session_handle,
                command=args.remote_command,
                settle_timeout_seconds=args.command_idle_timeout_seconds,
                total_timeout_seconds=args.total_timeout_seconds,
            )
        )
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(close_terminal_session_payload(session_handle))
    if resolved_target:
        payload["resolved_target"] = resolved_target
    print_json(payload)
    exit_status = payload.get("exit_status")
    if isinstance(exit_status, int):
        return exit_status
    return 0 if payload.get("command_completed") else 1


def print_terminal_text(text: str) -> None:
    if not text:
        return
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def print_terminal_shell_help() -> None:
    print(
        "Local commands: /help, /read, /ctrl-c, /resize <cols> <rows>, /exit",
        flush=True,
    )


async def interactive_terminal_shell(args: argparse.Namespace) -> int:
    asset_id, account_id, resolved_target = await resolve_terminal_target_args(args)
    opened = await acquire_terminal_session_payload(
        asset_ref=asset_id,
        account_ref=account_id,
        protocol=args.protocol,
        connect_method=args.connect_method,
        cols=args.cols,
        rows=args.rows,
        startup_idle_timeout_seconds=args.startup_idle_timeout_seconds,
    )
    if resolved_target:
        print_json({"resolved_target": resolved_target})
    session_handle = str(opened["session_handle"])
    startup_text = str(opened.get("startup_output_text") or "")
    if startup_text:
        print_terminal_text(startup_text)
    print_terminal_shell_help()

    try:
        while True:
            try:
                raw = input("mcp-jms> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                interrupted = await interrupt_terminal_session_payload(
                    session_handle=session_handle,
                    signal="ctrl_c",
                    settle_timeout_seconds=args.read_idle_timeout_seconds,
                    total_timeout_seconds=args.read_total_timeout_seconds,
                )
                print_terminal_text(
                    str(interrupted.get("stdout_text") or interrupted.get("output_text") or "")
                )
                if interrupted.get("connection_closed"):
                    return 0
                continue

            command = raw.rstrip("\n")
            stripped = command.strip()
            if not stripped:
                continue
            if stripped == "/help":
                print_terminal_shell_help()
                continue
            if stripped == "/exit":
                break
            if stripped == "/read":
                read_payload = await read_terminal_output_payload(
                    session_handle=session_handle,
                    idle_timeout_seconds=args.read_idle_timeout_seconds,
                    total_timeout_seconds=args.read_total_timeout_seconds,
                )
                print_terminal_text(
                    str(read_payload.get("stdout_text") or read_payload.get("output_text") or "")
                )
                if read_payload.get("session_closed"):
                    return 0
                continue
            if stripped == "/ctrl-c":
                read_payload = await interrupt_terminal_session_payload(
                    session_handle=session_handle,
                    signal="ctrl_c",
                    settle_timeout_seconds=args.read_idle_timeout_seconds,
                    total_timeout_seconds=args.read_total_timeout_seconds,
                )
                print_terminal_text(
                    str(read_payload.get("stdout_text") or read_payload.get("output_text") or "")
                )
                if read_payload.get("connection_closed"):
                    return 0
                continue
            if stripped.startswith("/resize "):
                parts = stripped.split()
                if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                    print("Usage: /resize <cols> <rows>", flush=True)
                    continue
                await resize_terminal_session_payload(
                    session_handle=session_handle,
                    cols=int(parts[1]),
                    rows=int(parts[2]),
                )
                print(f"Resized remote terminal to {parts[1]}x{parts[2]}.", flush=True)
                continue

            await send_terminal_input_payload(
                session_handle=session_handle,
                data=command,
                append_newline=True,
            )
            read_payload = await read_terminal_output_payload(
                session_handle=session_handle,
                idle_timeout_seconds=args.read_idle_timeout_seconds,
                total_timeout_seconds=args.read_total_timeout_seconds,
            )
            print_terminal_text(
                str(read_payload.get("stdout_text") or read_payload.get("output_text") or "")
            )
            if read_payload.get("session_closed"):
                return 0
    finally:
        try:
            await close_terminal_session_payload(session_handle)
        except Exception:
            pass
    return 0


def terminal_shell_command(args: argparse.Namespace) -> int:
    return asyncio.run(interactive_terminal_shell(args))


async def resolve_terminal_target_args(
    args: argparse.Namespace,
) -> tuple[str, str, dict | None]:
    asset_id = str(getattr(args, "asset_id", "") or "").strip()
    asset_ref = str(getattr(args, "asset_ref", "") or "").strip()
    account_id = str(getattr(args, "account", "") or "").strip()
    account_ref = str(getattr(args, "account_ref", "") or "").strip()
    protocol = str(getattr(args, "protocol", "ssh") or "ssh").strip()

    resolved = await resolve_terminal_target_payload(
        asset_ref=asset_id or asset_ref,
        account_ref=account_id or account_ref,
        protocol=protocol,
    )
    return str(resolved["asset"]["id"]), str(resolved["account"]["id"]), resolved


def resolve_target_command(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        resolve_terminal_target_payload(
            asset_ref=args.asset_ref,
            account_ref=args.account_ref,
            protocol=args.protocol,
        )
    )
    print_json(payload)
    return 0


def refresh_session_command(args: argparse.Namespace) -> int:
    payload = asyncio.run(refresh_terminal_auth_payload(force=bool(args.force)))
    print_json(payload)
    terminal_auth = payload.get("terminal_auth")
    if isinstance(terminal_auth, dict) and terminal_auth.get("cookie_session_authenticated"):
        return 0
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    try:
        if args.command in (None, "serve"):
            transport = getattr(args, "transport", "stdio")
            run_server(transport=transport)
            return 0

        if args.command == "paths":
            print_json(build_paths_payload(settings))
            return 0

        if args.command == "doctor":
            payload = asyncio.run(build_status_payload())
            print_json(payload)
            return 0 if payload.get("authenticated") else 1

        if args.command == "refresh-session":
            return refresh_session_command(args)

        if args.command == "login":
            return login_command(args, settings)

        if args.command == "koko-probe":
            return koko_probe_command(args)

        if args.command == "terminal-exec":
            return terminal_exec_command(args)

        if args.command == "terminal-shell":
            return terminal_shell_command(args)

        if args.command == "resolve-target":
            return resolve_target_command(args)

        if args.command == "save-state":
            return save_state_command(args, settings)

        if args.command == "clear-state":
            return clear_state_command(settings)

        parser.error(f"Unsupported command: {args.command}")
        return 2
    except KeyboardInterrupt:
        print_json(
            {
                "error": "Interrupted by user.",
                "error_type": "KeyboardInterrupt",
            }
        )
        return 130
    except Exception as exc:
        print_json(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
