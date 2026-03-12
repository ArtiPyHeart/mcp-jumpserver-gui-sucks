from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from .auth_state import AuthState
from .config import Settings
from .errors import MissingAuthStateError
from .koko import (
    KoKoProbeError,
    KoKoTerminalSession,
    detect_shell_prompt,
    normalize_terminal_text,
    strip_ansi_sequences,
    strip_shell_prompt,
)

LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(slots=True)
class ManagedTerminalSession:
    handle: str
    terminal: KoKoTerminalSession
    asset_id: str
    account: str
    protocol: str
    connect_method: str
    cols: int
    rows: int
    idle_timeout_seconds: float
    shell_prompt: str = ""
    created_at: str = field(default_factory=utc_now)
    last_activity_at: str = field(default_factory=utc_now)
    remote_terminal_id: str = ""
    remote_session_id: str = ""
    connect_summary: dict[str, Any] = field(default_factory=dict)
    pending_inputs: list[str] = field(default_factory=list, repr=False)
    last_activity_monotonic: float = field(default_factory=monotonic, repr=False)
    close_reason: str = ""
    closed_at: str = ""
    token_cleanup: str = ""
    token_cleanup_error: str = ""
    closed: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.last_activity_at = utc_now()
        self.last_activity_monotonic = monotonic()
        self.remote_terminal_id = self.terminal.terminal_id or self.remote_terminal_id
        self.remote_session_id = self.terminal.session_id or self.remote_session_id
        if self.terminal.connect_info and not self.connect_summary:
            self.connect_summary = {
                "asset_name": self.terminal.connect_info.get("asset", {}).get("name")
                if isinstance(self.terminal.connect_info.get("asset"), dict)
                else None,
                "platform": self.terminal.connect_info.get("platform"),
            }

    def snapshot(self) -> dict[str, Any]:
        idle_seconds = max(0.0, monotonic() - self.last_activity_monotonic)
        expires_in_seconds = 0.0 if self.closed else max(0.0, self.idle_timeout_seconds - idle_seconds)
        return {
            "session_handle": self.handle,
            "asset_id": self.asset_id,
            "account": self.account,
            "protocol": self.protocol,
            "connect_method": self.connect_method,
            "cols": self.cols,
            "rows": self.rows,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "idle_seconds": round(idle_seconds, 3),
            "expires_in_seconds": round(expires_in_seconds, 3),
            "shell_prompt": self.shell_prompt or None,
            "remote_terminal_id": self.remote_terminal_id or None,
            "remote_session_id": self.remote_session_id or None,
            "connect_summary": self.connect_summary or None,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "closed": self.closed,
            "close_reason": self.close_reason or None,
            "closed_at": self.closed_at or None,
        }

    def idle_expired(self, now_monotonic: float) -> bool:
        return (now_monotonic - self.last_activity_monotonic) >= self.idle_timeout_seconds


class TerminalSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ManagedTerminalSession] = {}
        self._lock = asyncio.Lock()
        self._idle_timeout_seconds = 900.0
        self._reap_interval_seconds = 30.0
        self._max_sessions = 8
        self._opening_count = 0
        self._sweeper_task: asyncio.Task[None] | None = None

    async def prepare(self, settings: Settings) -> None:
        self._idle_timeout_seconds = settings.terminal_idle_timeout_seconds
        self._reap_interval_seconds = settings.terminal_reap_interval_seconds
        self._max_sessions = settings.max_terminal_sessions
        self._ensure_sweeper_running()
        await self._reap_idle_sessions()

    async def list_sessions(self) -> dict[str, Any]:
        async with self._lock:
            results = [session.snapshot() for session in self._sessions.values()]
        results.sort(key=lambda item: str(item.get("created_at", "")))
        return {
            "count": len(results),
            "opening_count": self._opening_count,
            "max_terminal_sessions": self._max_sessions,
            "terminal_idle_timeout_seconds": self._idle_timeout_seconds,
            "terminal_reap_interval_seconds": self._reap_interval_seconds,
            "results": results,
        }

    async def open_session(
        self,
        settings: Settings,
        auth_state: AuthState | None,
        *,
        asset_id: str,
        account: str,
        protocol: str = "ssh",
        connect_method: str = "web_cli",
        cols: int = 120,
        rows: int = 32,
        startup_idle_timeout_seconds: float = 1.5,
    ) -> dict[str, Any]:
        await self.prepare(settings)
        if auth_state is None:
            raise MissingAuthStateError("Open a terminal session requires persisted auth state.")
        await self._reserve_open_slot()
        terminal = KoKoTerminalSession(
            settings,
            auth_state,
            asset_id=asset_id,
            account=account,
            protocol=protocol,
            connect_method=connect_method,
            cols=cols,
            rows=rows,
        )
        try:
            await terminal.open()
            startup = await terminal.drain_until_idle(
                idle_timeout_seconds=startup_idle_timeout_seconds,
                total_timeout_seconds=max(3.0, startup_idle_timeout_seconds * 4),
            )
            startup_raw = normalize_terminal_text(startup.raw_text())
            startup_text = strip_ansi_sequences(startup_raw)
            shell_prompt = detect_shell_prompt(startup_text)
            handle = uuid4().hex
            session = ManagedTerminalSession(
                handle=handle,
                terminal=terminal,
                asset_id=asset_id,
                account=account,
                protocol=protocol,
                connect_method=connect_method,
                cols=cols,
                rows=rows,
                idle_timeout_seconds=self._idle_timeout_seconds,
                shell_prompt=shell_prompt,
            )
            session.touch()
            async with self._lock:
                self._sessions[handle] = session
                active_session_count = len(self._sessions)
            LOGGER.info(
                "Opened managed KoKo terminal session %s for asset %s.",
                handle,
                asset_id,
            )
            return {
                "opened": True,
                "active_session_count": active_session_count,
                "max_terminal_sessions": self._max_sessions,
                **session.snapshot(),
                "startup_control_types": startup.control_types,
                "startup_binary_frame_count": startup.binary_frame_count,
                "startup_output_raw": startup_raw,
                "startup_output_text": startup_text,
                "startup_stdout_text": strip_shell_prompt(startup_text, shell_prompt),
            }
        except Exception:
            await self._safe_close_terminal(terminal)
            raise
        finally:
            await self._release_open_slot()

    async def write_session(
        self,
        session_handle: str,
        *,
        data: str,
        append_newline: bool = False,
    ) -> dict[str, Any]:
        session = await self._require_session(session_handle)
        if append_newline:
            data = f"{data}\n"
        async with session._lock:
            await session.terminal.send_terminal_data(data)
            session.pending_inputs.append(normalize_terminal_text(data))
            session.touch()
        LOGGER.info(
            "Wrote %d characters to managed KoKo terminal session %s.",
            len(data),
            session_handle,
        )
        return {
            "written": True,
            "chars_sent": len(data),
            "max_terminal_sessions": self._max_sessions,
            **session.snapshot(),
        }

    async def read_session(
        self,
        session_handle: str,
        *,
        idle_timeout_seconds: float = 1.0,
        total_timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        session = await self._require_session(session_handle)
        async with session._lock:
            transcript = await session.terminal.drain_until_idle(
                idle_timeout_seconds=idle_timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
            )
            session.touch()
            raw = normalize_terminal_text(transcript.raw_text())
            text = strip_ansi_sequences(raw)
            prompt_stripped = strip_shell_prompt(text, session.shell_prompt)
            stdout, remaining_inputs = strip_pending_input_echoes(
                prompt_stripped,
                session.pending_inputs,
            )
            session.pending_inputs = remaining_inputs
            payload = {
                "session_closed": transcript.connection_closed,
                "idle_timeout": transcript.idle_timeout,
                "binary_frame_count": transcript.binary_frame_count,
                "control_types": transcript.control_types,
                "output_raw": raw,
                "output_text": text,
                "stdout_text": stdout,
                "max_terminal_sessions": self._max_sessions,
                **session.snapshot(),
            }
        if transcript.connection_closed:
            await self._drop_session(session_handle)
            closed_snapshot = await self._close_detached_session(
                session,
                close_reason="remote_closed",
            )
            payload["token_cleanup"] = closed_snapshot["token_cleanup"]
            payload["close_reason"] = closed_snapshot["close_reason"]
            payload["closed"] = closed_snapshot["closed"]
            payload["closed_at"] = closed_snapshot["closed_at"]
            if closed_snapshot["token_cleanup_error"]:
                payload["token_cleanup_error"] = closed_snapshot["token_cleanup_error"]
        LOGGER.info(
            "Read from managed KoKo terminal session %s: %d binary frames, controls=%s.",
            session_handle,
            transcript.binary_frame_count,
            transcript.control_types,
        )
        return payload

    async def resize_session(
        self,
        session_handle: str,
        *,
        cols: int,
        rows: int,
    ) -> dict[str, Any]:
        session = await self._require_session(session_handle)
        async with session._lock:
            await session.terminal.resize(cols=cols, rows=rows)
            session.cols = cols
            session.rows = rows
            session.touch()
        LOGGER.info(
            "Resized managed KoKo terminal session %s to %sx%s.",
            session_handle,
            cols,
            rows,
        )
        return {
            "resized": True,
            "max_terminal_sessions": self._max_sessions,
            **session.snapshot(),
        }

    async def close_session(self, session_handle: str) -> dict[str, Any]:
        session = await self._drop_session(session_handle)
        snapshot = await self._close_detached_session(session, close_reason="user_closed")
        payload = {
            "closed": True,
            "token_cleanup": snapshot["token_cleanup"],
            "close_reason": snapshot["close_reason"],
            "closed_at": snapshot["closed_at"],
            **snapshot,
        }
        if snapshot["token_cleanup_error"]:
            payload["token_cleanup_error"] = snapshot["token_cleanup_error"]
        return payload

    async def _require_session(self, session_handle: str) -> ManagedTerminalSession:
        async with self._lock:
            session = self._sessions.get(session_handle)
        if session is None:
            raise KoKoProbeError(f"Unknown or expired terminal session handle: {session_handle}")
        return session

    async def _reserve_open_slot(self) -> None:
        async with self._lock:
            active_sessions = len(self._sessions) + self._opening_count
            if active_sessions >= self._max_sessions:
                raise KoKoProbeError(
                    "The managed KoKo terminal session limit has been reached. "
                    f"Current limit: {self._max_sessions}."
                )
            self._opening_count += 1

    async def _release_open_slot(self) -> None:
        async with self._lock:
            self._opening_count = max(0, self._opening_count - 1)

    def _ensure_sweeper_running(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweeper_loop())

    async def _sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reap_interval_seconds)
            await self._reap_idle_sessions()

    async def _reap_idle_sessions(self) -> None:
        expired = await self._collect_expired_sessions()
        if not expired:
            return
        LOGGER.info("Reaping %d idle managed KoKo terminal session(s).", len(expired))
        for session in expired:
            await self._close_detached_session(session, close_reason="idle_timeout")

    async def _collect_expired_sessions(self) -> list[ManagedTerminalSession]:
        now_monotonic = monotonic()
        async with self._lock:
            expired_handles = [
                handle
                for handle, session in self._sessions.items()
                if session.idle_expired(now_monotonic) and not session._lock.locked()
            ]
            expired_sessions = [self._sessions.pop(handle) for handle in expired_handles]
        return expired_sessions

    async def _drop_session(self, session_handle: str) -> ManagedTerminalSession:
        async with self._lock:
            session = self._sessions.pop(session_handle, None)
        if session is None:
            raise KoKoProbeError(f"Unknown or expired terminal session handle: {session_handle}")
        return session

    async def _close_detached_session(
        self,
        session: ManagedTerminalSession,
        *,
        close_reason: str,
    ) -> dict[str, Any]:
        async with session._lock:
            if not session.closed:
                session.closed = True
                session.close_reason = close_reason
                session.closed_at = utc_now()
                await session.terminal.close()
                session.token_cleanup = session.terminal.cleanup_state
                session.token_cleanup_error = session.terminal.cleanup_error
            snapshot = session.snapshot()
        LOGGER.info(
            "Closed managed KoKo terminal session %s with reason %s and cleanup state %s.",
            session.handle,
            session.close_reason,
            session.token_cleanup,
        )
        return {
            **snapshot,
            "token_cleanup": session.token_cleanup,
            "token_cleanup_error": session.token_cleanup_error,
        }

    async def _safe_close_terminal(self, terminal: KoKoTerminalSession) -> None:
        try:
            await terminal.close()
        except Exception:
            LOGGER.exception("Failed to close KoKo terminal after an unsuccessful open attempt.")


_TERMINAL_SESSION_MANAGER = TerminalSessionManager()


def get_terminal_session_manager() -> TerminalSessionManager:
    return _TERMINAL_SESSION_MANAGER


def strip_pending_input_echoes(
    text: str,
    pending_inputs: list[str],
) -> tuple[str, list[str]]:
    if not text or not pending_inputs:
        return text, pending_inputs

    remaining_text = text
    remaining_inputs = list(pending_inputs)
    while remaining_inputs:
        candidate = normalize_terminal_text(remaining_inputs[0])
        if not candidate:
            remaining_inputs.pop(0)
            continue
        if not remaining_text.startswith(candidate):
            break
        remaining_text = remaining_text[len(candidate) :]
        remaining_inputs.pop(0)

    return remaining_text, remaining_inputs
