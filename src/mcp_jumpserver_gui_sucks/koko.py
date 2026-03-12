from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .auth_state import AuthState
from .client import JumpServerClient
from .config import Settings
from .errors import ConfigError, JumpServerAPIError, JumpServerMCPError

LOGGER = logging.getLogger(__name__)
KOKO_SUBPROTOCOL = "JMS-KOKO"
KOKO_WS_PATH = "/koko/ws/terminal/"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
OSC_ESCAPE_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")


class KoKoProbeError(JumpServerMCPError):
    """Raised when a KoKo websocket probe cannot start."""


@dataclass(slots=True)
class Transcript:
    text_chunks: list[str] = field(default_factory=list)
    control_types: list[str] = field(default_factory=list)
    control_payloads: list[dict[str, Any]] = field(default_factory=list)
    binary_frame_count: int = 0
    idle_timeout: bool = False
    connection_closed: bool = False
    close_reason: str = ""

    def append_text(self, text: str) -> None:
        if text:
            self.text_chunks.append(text)

    def raw_text(self) -> str:
        return "".join(self.text_chunks)


def build_cookie_header(auth_state: AuthState) -> str:
    pairs = [f"{cookie.name}={cookie.value}" for cookie in auth_state.cookies if cookie.value]
    if not pairs:
        raise KoKoProbeError(
            "The persisted auth state does not contain cookie material for KoKo."
        )
    return "; ".join(pairs)


def build_koko_terminal_ws_url(
    *,
    base_url: str,
    token_id: str,
    disable_auto_hash: bool = False,
) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        raise ConfigError(f"Invalid JumpServer base URL for KoKo probe: {base_url!r}")
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode(
        {
            "disableautohash": "true" if disable_auto_hash else "false",
            "token": token_id,
        }
    )
    return f"{ws_scheme}://{parsed.netloc}{KOKO_WS_PATH}?{query}"


def normalize_terminal_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "")


def strip_ansi_sequences(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", OSC_ESCAPE_RE.sub("", text))


def detect_shell_prompt(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1]


def strip_shell_prompt(text: str, prompt: str) -> str:
    if not prompt:
        return text
    suffix = "\n" if text.endswith("\n") else ""
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        current = line
        while current.startswith(prompt):
            current = current[len(prompt) :]
        cleaned_lines.append(current)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    return "\n".join(cleaned_lines) + suffix


def make_message(message_id: str, message_type: str, data: Any) -> str:
    return json.dumps({"id": message_id, "type": message_type, "data": data}, separators=(",", ":"))


class KoKoTerminalSession:
    def __init__(
        self,
        settings: Settings,
        auth_state: AuthState,
        *,
        asset_id: str,
        account: str,
        protocol: str = "ssh",
        connect_method: str = "web_cli",
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self._settings = settings
        self._auth_state = auth_state
        self._asset_id = asset_id
        self._account = account
        self._protocol = protocol
        self._connect_method = connect_method
        self._cols = cols
        self._rows = rows
        self._client = JumpServerClient(settings, auth_state)
        self._token_id = ""
        self._ws: Any = None
        self._terminal_id = ""
        self._session_id = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._cleanup_state = "not_attempted"
        self._cleanup_error = ""
        self._connect_info: dict[str, Any] = {}

    @property
    def token_id(self) -> str:
        return self._token_id

    @property
    def terminal_id(self) -> str:
        return self._terminal_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cleanup_state(self) -> str:
        return self._cleanup_state

    @property
    def cleanup_error(self) -> str:
        return self._cleanup_error

    @property
    def connect_info(self) -> dict[str, Any]:
        return self._connect_info

    async def open(self) -> None:
        if not self._auth_state.has_cookie_auth():
            raise KoKoProbeError(
                "KoKo terminal access requires a cookie-backed web session. Run login again first."
            )

        cookie_header = build_cookie_header(self._auth_state)
        token_payload = await self._client.create_connection_token(
            asset_id=self._asset_id,
            account=self._account,
            protocol=self._protocol,
            connect_method=self._connect_method,
            is_reusable=False,
        )
        self._token_id = str(token_payload.get("id", ""))
        if not self._token_id:
            raise KoKoProbeError("JumpServer did not return a connection-token ID.")

        ws_url = build_koko_terminal_ws_url(
            base_url=self._auth_state.base_url or self._settings.base_url,
            token_id=self._token_id,
        )
        LOGGER.info(
            "Opening KoKo terminal websocket for asset %s using connect method %s.",
            self._asset_id,
            self._connect_method,
        )
        self._ws = await websockets.connect(
            ws_url,
            origin=(self._auth_state.base_url or self._settings.base_url),
            subprotocols=[KOKO_SUBPROTOCOL],
            additional_headers={"Cookie": cookie_header},
            open_timeout=self._settings.request_timeout_seconds,
            close_timeout=5,
        )
        await self._await_connect()
        await self._send_init()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _await_connect(self) -> None:
        deadline = monotonic() + self._settings.request_timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise KoKoProbeError("Timed out waiting for the KoKo CONNECT message.")
            message = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            if isinstance(message, bytes):
                continue
            payload = self._parse_text_frame(message)
            if payload.get("type") != "CONNECT":
                continue
            self._terminal_id = str(payload.get("id", ""))
            if not self._terminal_id:
                raise KoKoProbeError("KoKo CONNECT did not include a terminal ID.")
            data = payload.get("data", "")
            if isinstance(data, str):
                with contextlib.suppress(ValueError):
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        self._connect_info = parsed
            break

    async def _send_init(self) -> None:
        if not self._terminal_id:
            raise KoKoProbeError("Cannot initialize KoKo before CONNECT completes.")
        payload = json.dumps({"cols": self._cols, "rows": self._rows, "code": ""}, separators=(",", ":"))
        await self._ws.send(make_message(self._terminal_id, "TERMINAL_INIT", payload))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(25)
            if self._ws is None:
                return
            try:
                if self._ws.state.name in {"CLOSED", "CLOSING"}:
                    return
            except Exception:
                return
            try:
                await self._ws.send(make_message("", "PING", ""))
            except Exception:
                return

    async def send_terminal_data(self, text: str) -> None:
        if self._ws is None:
            raise KoKoProbeError("KoKo websocket is not open.")
        message_id = self._terminal_id
        await self._ws.send(make_message(message_id, "TERMINAL_DATA", text))

    async def resize(self, *, cols: int, rows: int) -> None:
        if self._ws is None:
            raise KoKoProbeError("KoKo websocket is not open.")
        payload = json.dumps({"cols": cols, "rows": rows}, separators=(",", ":"))
        await self._ws.send(make_message(self._terminal_id, "TERMINAL_RESIZE", payload))

    async def drain_until_idle(
        self,
        *,
        idle_timeout_seconds: float,
        total_timeout_seconds: float,
    ) -> Transcript:
        transcript = Transcript()
        deadline = monotonic() + total_timeout_seconds
        close_grace_deadline: float | None = None
        while True:
            if close_grace_deadline is not None:
                deadline = min(deadline, close_grace_deadline)
            remaining_total = deadline - monotonic()
            if remaining_total <= 0:
                return transcript
            timeout = min(idle_timeout_seconds, remaining_total)
            try:
                message = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            except TimeoutError:
                transcript.idle_timeout = not transcript.connection_closed
                return transcript
            except ConnectionClosed as exc:
                transcript.connection_closed = True
                transcript.close_reason = f"{type(exc).__name__}: {exc}"
                return transcript

            if isinstance(message, bytes):
                transcript.binary_frame_count += 1
                transcript.append_text(self._decoder.decode(message))
                continue

            payload = self._parse_text_frame(message)
            message_type = str(payload.get("type", "UNKNOWN"))
            transcript.control_types.append(message_type)
            transcript.control_payloads.append(payload)
            if message_type == "TERMINAL_SESSION":
                self._session_id = self._extract_session_id(payload)
            if message_type in {"CLOSE", "ERROR", "TERMINAL_ERROR"}:
                transcript.connection_closed = True
                err = payload.get("err") or payload.get("data") or ""
                if err:
                    transcript.append_text(f"\n{err}\n")
                close_grace_deadline = monotonic() + 1.0
                continue
            if message_type == "CONNECT" and not self._terminal_id:
                self._terminal_id = str(payload.get("id", ""))

    def _parse_text_frame(self, message: str) -> dict[str, Any]:
        try:
            payload = json.loads(message)
        except ValueError:
            return {"type": "TEXT", "data": message}
        if not isinstance(payload, dict):
            return {"type": "TEXT", "data": message}
        return payload

    def _extract_session_id(self, payload: dict[str, Any]) -> str:
        data = payload.get("data", "")
        if not isinstance(data, str):
            return ""
        try:
            parsed = json.loads(data)
        except ValueError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        session = parsed.get("session")
        if not isinstance(session, dict):
            return ""
        return str(session.get("id", ""))

    async def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        if self._token_id:
            try:
                await self._client.expire_connection_token(self._token_id)
            except JumpServerAPIError as exc:
                if exc.status_code == 404:
                    self._cleanup_state = "already_consumed"
                else:
                    self._cleanup_state = "failed"
                    self._cleanup_error = str(exc)
            else:
                self._cleanup_state = "expired"


def build_exec_script(command: str, start_marker: str, end_marker: str) -> str:
    script = command
    if script.startswith("\n"):
        script = script.lstrip("\n")
    if not script.endswith("\n"):
        script = f"{script}\n"
    script = (
        "stty -echo\n"
        f"printf '{start_marker}\\n'\n"
        f"{script}"
        "status=$?\n"
        f"printf '\\n{end_marker}:%s\\n' \"$status\"\n"
        "stty echo\n"
    )
    script += "exit\n"
    return script


def extract_between_markers(
    text: str,
    *,
    start_marker: str,
    end_marker: str,
) -> tuple[int | None, str]:
    pattern = re.compile(
        rf"{re.escape(start_marker)}\n?(.*?){re.escape(end_marker)}:(\d+)",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None, text
    return int(match.group(2)), match.group(1)


async def probe_koko_terminal(
    settings: Settings,
    auth_state: AuthState,
    *,
    asset_id: str,
    account: str,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
    cols: int = 80,
    rows: int = 24,
    max_messages: int = 4,
    message_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    session = KoKoTerminalSession(
        settings,
        auth_state,
        asset_id=asset_id,
        account=account,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
    )
    result: dict[str, Any] = {
        "asset_id": asset_id,
        "account": account,
        "protocol": protocol,
        "connect_method": connect_method,
        "cookie_names": auth_state.cookie_names(),
        "ws_connected": False,
        "agreed_subprotocol": None,
        "received_types": [],
        "binary_frame_seen": False,
        "token_cleanup": "not_attempted",
    }
    try:
        await session.open()
        result["ws_connected"] = True
        if session._ws is not None:
            result["agreed_subprotocol"] = session._ws.subprotocol
        transcript = await session.drain_until_idle(
            idle_timeout_seconds=message_timeout_seconds,
            total_timeout_seconds=message_timeout_seconds * max_messages,
        )
        result["received_types"] = transcript.control_types
        result["binary_frame_seen"] = transcript.binary_frame_count > 0
        result["message_timeout"] = transcript.idle_timeout
        if session.session_id:
            result["session_id"] = session.session_id
    except InvalidStatus as exc:
        result["error"] = f"KoKo websocket handshake failed: {exc}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await session.close()
        result["token_cleanup"] = session.cleanup_state
        if session.cleanup_error:
            result["token_cleanup_error"] = session.cleanup_error
    return result


async def execute_koko_command(
    settings: Settings,
    auth_state: AuthState,
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
    if not command.strip():
        raise KoKoProbeError("The KoKo terminal command must not be empty.")

    session = KoKoTerminalSession(
        settings,
        auth_state,
        asset_id=asset_id,
        account=account,
        protocol=protocol,
        connect_method=connect_method,
        cols=cols,
        rows=rows,
    )
    start_marker = f"__MCP_JMS_COMMAND_START_{uuid4().hex}__"
    end_marker = f"__MCP_JMS_EXIT_STATUS_{uuid4().hex}__"
    result: dict[str, Any] = {
        "asset_id": asset_id,
        "account": account,
        "protocol": protocol,
        "connect_method": connect_method,
        "command": command,
        "cookie_names": auth_state.cookie_names(),
        "ws_connected": False,
        "command_sent": False,
        "exit_status": None,
        "token_cleanup": "not_attempted",
    }

    try:
        await session.open()
        result["ws_connected"] = True
        if session._ws is not None:
            result["agreed_subprotocol"] = session._ws.subprotocol

        startup = await session.drain_until_idle(
            idle_timeout_seconds=startup_idle_timeout_seconds,
            total_timeout_seconds=max(3.0, startup_idle_timeout_seconds * 4),
        )
        startup_raw = normalize_terminal_text(startup.raw_text())
        result["startup_control_types"] = startup.control_types
        result["startup_binary_frame_count"] = startup.binary_frame_count
        result["startup_output_raw"] = startup_raw
        result["startup_output_text"] = strip_ansi_sequences(startup_raw)
        prompt_text = detect_shell_prompt(result["startup_output_text"])
        result["shell_prompt"] = prompt_text

        script = build_exec_script(command, start_marker, end_marker)
        await session.send_terminal_data(script)
        result["command_sent"] = True

        command_transcript = await session.drain_until_idle(
            idle_timeout_seconds=command_idle_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
        )
        command_raw = normalize_terminal_text(command_transcript.raw_text())
        exit_status, command_without_marker = extract_between_markers(
            command_raw,
            start_marker=start_marker,
            end_marker=end_marker,
        )
        result["exit_status"] = exit_status
        result["command_control_types"] = command_transcript.control_types
        result["command_binary_frame_count"] = command_transcript.binary_frame_count
        result["command_output_raw"] = command_without_marker
        result["command_output_text"] = strip_ansi_sequences(command_without_marker)
        result["command_stdout_text"] = strip_shell_prompt(
            result["command_output_text"],
            prompt_text,
        )
        result["idle_timeout"] = command_transcript.idle_timeout
        result["connection_closed"] = command_transcript.connection_closed
        if command_transcript.close_reason:
            result["close_reason"] = command_transcript.close_reason
        if session.terminal_id:
            result["terminal_id"] = session.terminal_id
        if session.session_id:
            result["session_id"] = session.session_id
        if session.connect_info:
            result["connect_summary"] = {
                "asset_name": session.connect_info.get("asset", {}).get("name")
                if isinstance(session.connect_info.get("asset"), dict)
                else None,
                "platform": session.connect_info.get("platform"),
            }
    except InvalidStatus as exc:
        result["error"] = f"KoKo websocket handshake failed: {exc}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await session.close()
        result["token_cleanup"] = session.cleanup_state
        if session.cleanup_error:
            result["token_cleanup_error"] = session.cleanup_error

    return result
