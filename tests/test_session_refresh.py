from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_jumpserver_gui_sucks import auth_refresh, cli
from mcp_jumpserver_gui_sucks.auth_state import AuthState, CookieState
from mcp_jumpserver_gui_sucks.config import STATE_FILE_ENV_NAME
from mcp_jumpserver_gui_sucks.session_store import SessionStore


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (httpx.Response(200, json={"ok": True}), 0),
        (httpx.Response(200, json={"ok": False}), 3),
        (httpx.Response(401, json={"detail": "Expired"}), 3),
        (httpx.Response(403, json={"detail": "Forbidden"}), 1),
        (httpx.Response(500, json={"detail": "Unavailable"}), 1),
        (httpx.Response(200, json={}), 1),
        (httpx.Response(200, json={"ok": "false"}), 1),
        (httpx.Response(200, json=[]), 1),
        (httpx.Response(200, text="not JSON"), 1),
        (httpx.ConnectError("Connection failed"), 1),
        (httpx.ReadTimeout("Request timed out"), 1),
    ],
)
def test_refresh_distinguishes_invalid_auth_from_other_failures(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
    response: httpx.Response | Exception,
    expected_status: int,
) -> None:
    state_file = tmp_path / "auth-state.json"
    monkeypatch.setenv(STATE_FILE_ENV_NAME, str(state_file))
    store = SessionStore(state_file)
    store.save(
        AuthState(
            base_url="https://jumpserver.example.com",
            access_key_id="test-key",
            access_key_secret="test-secret",
            cookies=[
                CookieState(
                    name="jms_sessionid",
                    value="test-cookie",
                    domain="jumpserver.example.com",
                )
            ],
        )
    )
    original_state = state_file.read_bytes()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        auth_refresh.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            **kwargs,
            transport=transport,
        ),
    )
    assert cli.main(["refresh-session", "--force"]) == expected_status
    payload = json.loads(capsys.readouterr().out)
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/authentication/user-session/"
    assert "Authorization" not in requests[0].headers
    if expected_status == 0:
        assert payload["terminal_auth"]["cookie_session_authenticated"] is True
        assert store.load().metadata["cookie_session_authenticated"] is True
        assert store.load().access_key_secret == "test-secret"
    else:
        assert "error_type" in payload
        assert state_file.read_bytes() == original_state


@pytest.mark.parametrize(
    "state", [None, AuthState(base_url="https://jumpserver.example.com")]
)
def test_missing_auth_or_cookies_requests_fresh_login(
    monkeypatch: Any,
    tmp_path: Path,
    state: AuthState | None,
) -> None:
    state_file = tmp_path / "auth-state.json"
    monkeypatch.setenv(STATE_FILE_ENV_NAME, str(state_file))
    if state is not None:
        SessionStore(state_file).save(state)
    assert cli.main(["refresh-session", "--force"]) == 3


def test_corrupt_state_does_not_request_fresh_login(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state_file = tmp_path / "auth-state.json"
    state_file.write_text("invalid JSON")
    monkeypatch.setenv(STATE_FILE_ENV_NAME, str(state_file))
    assert cli.main(["refresh-session", "--force"]) == 1
    assert state_file.read_text() == "invalid JSON"


@pytest.mark.parametrize(
    ("refresh_status", "login_status", "expected_status", "commands"),
    [
        (0, 0, 0, ["refresh-session"]),
        (3, 0, 0, ["refresh-session", "login"]),
        (3, 1, 1, ["refresh-session", "login"]),
        (1, 0, 1, ["refresh-session"]),
        (2, 0, 2, ["refresh-session"]),
        (130, 0, 130, ["refresh-session"]),
    ],
)
def test_login_script_only_reauthenticates_for_auth_failure(
    tmp_path: Path,
    refresh_status: int,
    login_status: int,
    expected_status: int,
    commands: list[str],
) -> None:
    stub = tmp_path / "uvx"
    stub.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
args = sys.argv[1:]
entry = {"command": args[1], "args": args, "password_exported": "JUMPSERVER_PASSWORD" in os.environ}
if args[1] == "login":
    entry["password"] = sys.stdin.readline().rstrip("\\n")
with open(os.environ["CALL_LOG"], "a") as handle:
    handle.write(json.dumps(entry) + "\\n")
sys.exit(int(os.environ["REFRESH_STATUS" if args[1] == "refresh-session" else "LOGIN_STATUS"]))
"""
    )
    stub.chmod(0o700)
    log_file = tmp_path / "calls.jsonl"
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        CALL_LOG=str(log_file),
        REFRESH_STATUS=str(refresh_status),
        LOGIN_STATUS=str(login_status),
    )
    env["JUMPSERVER_PASSWORD"] = "inherited-password-must-not-be-exported"
    script = Path(__file__).resolve().parents[1] / "examples" / "login.sh"
    result = subprocess.run(
        ["bash", "-x", str(script)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_status
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [call["command"] for call in calls] == commands
    assert all(not call["password_exported"] for call in calls)
    assert "replace-with-your-password" not in result.stdout + result.stderr
    if commands[-1] == "login":
        assert calls[-1]["password"] == "replace-with-your-password"
        assert "--password-stdin" in calls[-1]["args"]
        assert "replace-with-your-password" not in calls[-1]["args"]
