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
    from_seq: int = 0
    next_seq: int = 0

    def append_text(self, text: str) -> None:
        if text:
            self.text_chunks.append(text)

    def raw_text(self) -> str:
        return "".join(self.text_chunks)


@dataclass(slots=True)
class BufferedTerminalEvent:
    seq: int
    text: str = ""
    control_type: str = ""
    control_payload: dict[str, Any] | None = None
    binary_frame_count: int = 0
    connection_closed: bool = False
    close_reason: str = ""


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
        self._reader_task: asyncio.Task[None] | None = None
        self._cleanup_state = "not_attempted"
        self._cleanup_error = ""
        self._connect_info: dict[str, Any] = {}
        self._condition = asyncio.Condition()
        self._events: list[BufferedTerminalEvent] = []
        self._next_seq = 1
        self._connection_closed = False
        self._close_reason = ""

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

    @property
    def close_reason(self) -> str:
        return self._close_reason

    @property
    def connection_closed(self) -> bool:
        return self._connection_closed

    def current_seq(self) -> int:
        return self._next_seq - 1

    def buffered_output_bytes(self) -> int:
        return sum(len(event.text.encode("utf-8", "replace")) for event in self._events)

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
        self._reader_task = asyncio.create_task(self._reader_loop())

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

    async def _reader_loop(self) -> None:
        try:
            while True:
                message = await self._ws.recv()
                await self._record_message(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            await self._mark_closed(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            await self._mark_closed(f"{type(exc).__name__}: {exc}")

    async def _record_message(self, message: str | bytes) -> None:
        event: BufferedTerminalEvent | None = None
        if isinstance(message, bytes):
            event = BufferedTerminalEvent(
                seq=self._next_seq,
                text=self._decoder.decode(message),
                binary_frame_count=1,
            )
        else:
            payload = self._parse_text_frame(message)
            message_type = str(payload.get("type", "UNKNOWN"))
            text = ""
            if message_type == "TERMINAL_SESSION":
                self._session_id = self._extract_session_id(payload)
            elif message_type == "CONNECT" and not self._terminal_id:
                self._terminal_id = str(payload.get("id", ""))
            elif message_type in {"CLOSE", "ERROR", "TERMINAL_ERROR"}:
                err = payload.get("err") or payload.get("data") or ""
                if err:
                    text = f"\n{err}\n"
                self._connection_closed = True
                self._close_reason = message_type
            event = BufferedTerminalEvent(
                seq=self._next_seq,
                text=text,
                control_type=message_type,
                control_payload=payload,
                connection_closed=self._connection_closed,
                close_reason=self._close_reason,
            )

        async with self._condition:
            self._events.append(event)
            self._next_seq += 1
            self._condition.notify_all()

    async def _mark_closed(self, close_reason: str) -> None:
        self._connection_closed = True
        self._close_reason = close_reason
        async with self._condition:
            self._condition.notify_all()

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
        after_seq: int | None = None,
    ) -> Transcript:
        start_seq = self.current_seq() if after_seq is None else max(0, after_seq)
        transcript = Transcript(from_seq=start_seq, next_seq=start_seq)
        deadline = monotonic() + total_timeout_seconds
        current_seq = start_seq

        while True:
            events = await self._events_after(current_seq)
            if events:
                for event in events:
                    transcript.next_seq = event.seq
                    if event.text:
                        transcript.append_text(event.text)
                    if event.binary_frame_count:
                        transcript.binary_frame_count += event.binary_frame_count
                    if event.control_type:
                        transcript.control_types.append(event.control_type)
                    if event.control_payload:
                        transcript.control_payloads.append(event.control_payload)
                    if event.connection_closed:
                        transcript.connection_closed = True
                        transcript.close_reason = event.close_reason
                current_seq = transcript.next_seq
                if transcript.connection_closed:
                    return transcript

            if self._connection_closed:
                transcript.connection_closed = True
                transcript.close_reason = self._close_reason
                return transcript

            remaining_total = deadline - monotonic()
            if remaining_total <= 0:
                return transcript

            got_activity = await self._wait_for_activity(
                after_seq=current_seq,
                timeout=min(idle_timeout_seconds, remaining_total),
            )
            if not got_activity:
                transcript.idle_timeout = not transcript.connection_closed
                return transcript

    async def _events_after(self, after_seq: int) -> list[BufferedTerminalEvent]:
        async with self._condition:
            return [event for event in self._events if event.seq > after_seq]

    async def _wait_for_activity(self, *, after_seq: int, timeout: float) -> bool:
        async with self._condition:
            if self._next_seq - 1 > after_seq or self._connection_closed:
                return True
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: (self._next_seq - 1) > after_seq or self._connection_closed
                    ),
                    timeout=timeout,
                )
                return True
            except TimeoutError:
                return False

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

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        await self._mark_closed(self._close_reason or "local_closed")

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


def build_exec_script(
    command: str,
    start_marker: str,
    end_marker: str,
    *,
    exit_shell: bool = True,
) -> str:
    script = command
    if script.startswith("\n"):
        script = script.lstrip("\n")
    if not script.endswith("\n"):
        script = f"{script}\n"
    script = (
        "trap 'stty echo' EXIT INT TERM HUP\n"
        "stty -echo\n"
        f"printf '{start_marker}\\n'\n"
        f"{script}"
        "status=$?\n"
        f"printf '\\n{end_marker}:%s\\n' \"$status\"\n"
        "stty echo\n"
        "trap - EXIT INT TERM HUP\n"
    )
    if exit_shell:
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
            after_seq=0,
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
            after_seq=0,
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

        script = build_exec_script(command, start_marker, end_marker, exit_shell=True)
        after_seq = session.current_seq()
        await session.send_terminal_data(script)
        result["command_sent"] = True

        command_transcript = await session.drain_until_idle(
            after_seq=after_seq,
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
