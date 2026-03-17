from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_state_dir

APP_NAME = "mcp-jumpserver-gui-sucks"
BASE_URL_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_BASE_URL"
ORG_ID_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_ORG_ID"
STATE_DIR_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR"
STATE_FILE_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE"
LOG_LEVEL_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_LOG_LEVEL"
VERIFY_TLS_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS"
REQUEST_TIMEOUT_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_REQUEST_TIMEOUT_SECONDS"
TERMINAL_IDLE_TIMEOUT_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS"
TERMINAL_REAP_INTERVAL_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS"
MAX_TERMINAL_SESSIONS_ENV_NAME = "MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS"


def parse_bool(raw: str, *, default: bool) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def parse_float(raw: str, *, default: float, minimum: float) -> float:
    try:
        value = float(raw.strip())
    except (AttributeError, ValueError):
        return default
    return value if value >= minimum else default


def parse_int(raw: str, *, default: int, minimum: int) -> int:
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return default
    return value if value >= minimum else default


@dataclass(slots=True)
class Settings:
    base_url: str
    org_id: str
    state_dir: Path
    state_file: Path
    log_level: str
    verify_tls: bool
    request_timeout_seconds: float
    terminal_idle_timeout_seconds: float
    terminal_reap_interval_seconds: float
    max_terminal_sessions: int
    base_url_env_name: str = BASE_URL_ENV_NAME
    org_id_env_name: str = ORG_ID_ENV_NAME
    state_dir_env_name: str = STATE_DIR_ENV_NAME
    state_file_env_name: str = STATE_FILE_ENV_NAME

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = os.getenv(BASE_URL_ENV_NAME, "").strip().rstrip("/")
        org_id = os.getenv(ORG_ID_ENV_NAME, "").strip()
        raw_state_dir = os.getenv(STATE_DIR_ENV_NAME, "").strip()
        raw_state_file = os.getenv(STATE_FILE_ENV_NAME, "").strip()

        state_dir = (
            Path(raw_state_dir).expanduser()
            if raw_state_dir
            else Path(user_state_dir(APP_NAME, appauthor=False))
        )
        state_file = (
            Path(raw_state_file).expanduser()
            if raw_state_file
            else state_dir / "auth-state.json"
        )

        return cls(
            base_url=base_url,
            org_id=org_id,
            state_dir=state_dir,
            state_file=state_file,
            log_level=os.getenv(LOG_LEVEL_ENV_NAME, "INFO").strip().upper() or "INFO",
            verify_tls=parse_bool(os.getenv(VERIFY_TLS_ENV_NAME, "true"), default=True),
            request_timeout_seconds=parse_float(
                os.getenv(REQUEST_TIMEOUT_ENV_NAME, "20"),
                default=20.0,
                minimum=1.0,
            ),
            terminal_idle_timeout_seconds=parse_float(
                os.getenv(TERMINAL_IDLE_TIMEOUT_ENV_NAME, "3600"),
                default=3600.0,
                minimum=1.0,
            ),
            terminal_reap_interval_seconds=parse_float(
                os.getenv(TERMINAL_REAP_INTERVAL_ENV_NAME, "30"),
                default=30.0,
                minimum=1.0,
            ),
            max_terminal_sessions=parse_int(
                os.getenv(MAX_TERMINAL_SESSIONS_ENV_NAME, "8"),
                default=8,
                minimum=1,
            ),
        )
