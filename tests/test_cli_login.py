from __future__ import annotations

import getpass
import io
import json
import os
import select
import signal
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

from mcp_jumpserver_gui_sucks import cli, cli_login
from mcp_jumpserver_gui_sucks.auth_state import AuthState, CookieState
from mcp_jumpserver_gui_sucks.config import STATE_FILE_ENV_NAME


@pytest.fixture
def login_flow(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    captured: dict[str, Any] = {"passwords": [], "codes": [], "closed": False}
    monkeypatch.setenv(STATE_FILE_ENV_NAME, str(tmp_path / "auth-state.json"))

    class LoginFlow:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["created"] = True

        def bootstrap(self) -> None:
            pass

        def encrypt_password(self, password: str) -> str:
            captured["passwords"].append(password)
            return "encrypted-password"

        def establish_web_session(self, **kwargs: Any) -> None:
            captured["codes"].append(cli_login.prompt_mfa_code("login MFA code"))

        def get_confirm_descriptor(self, **kwargs: Any) -> dict[str, Any]:
            return {"confirm_type": "mfa", "content": [{"name": "otp"}]}

        def complete_confirmation(self, **kwargs: Any) -> None:
            captured["codes"].append(kwargs["secret_key"])

        def create_access_key(self) -> dict[str, str]:
            return {"id": "test-key", "secret": "test-access-key-secret"}

        def build_access_key_auth_state(self, **kwargs: Any) -> AuthState:
            return AuthState(
                base_url="https://jumpserver.example.com",
                access_key_id=kwargs["key_id"],
                access_key_secret=kwargs["secret"],
                cookies=[CookieState(name="jms_sessionid", value="test-cookie")],
            )

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli_login, "JumpServerCLILogin", LoginFlow)
    return captured


def login_args(*extra: str) -> list[str]:
    return [
        "login",
        "--base-url",
        "https://jumpserver.example.com",
        "--username",
        "alice",
        "--mfa-type",
        "otp",
        *extra,
    ]


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_piped_password_preserves_spaces_and_keeps_mfa_separate(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
    login_flow: dict[str, Any],
    ending: str,
) -> None:
    password = "  test '$password\\with spaces  "
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(password + ending))
    prompts: list[str] = []
    codes = iter(["123456", "654321"])

    def prompt(label: str) -> str:
        prompts.append(label)
        return next(codes)

    monkeypatch.setattr(cli_login.getpass, "getpass", prompt)
    assert cli.main(login_args("--password-stdin")) == 0
    assert login_flow["passwords"] == [password]
    assert login_flow["codes"] == ["123456", "654321"]
    assert all("Password:" not in label for label in prompts)
    assert login_flow["closed"]
    output = capsys.readouterr()
    assert json.loads(output.out)["durable_auth"] is True
    assert password not in output.out + output.err
    assert password not in (tmp_path / "auth-state.json").read_text()
    assert "123456" not in output.out + output.err
    assert "654321" not in output.out + output.err


def test_default_login_still_prompts_for_password(
    monkeypatch: Any,
    login_flow: dict[str, Any],
) -> None:
    replies = iter(["interactive-password", "123456", "654321"])
    monkeypatch.setattr(cli_login.getpass, "getpass", lambda label: next(replies))
    assert cli.main(login_args()) == 0
    assert login_flow["passwords"] == ["interactive-password"]
    assert login_flow["codes"] == ["123456", "654321"]


@pytest.mark.parametrize("password_input", ["", "\n", "\r\n"])
def test_empty_piped_password_fails_before_login(
    monkeypatch: Any,
    login_flow: dict[str, Any],
    password_input: str,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(password_input))
    assert cli.main(login_args("--password-stdin")) == 1
    assert "created" not in login_flow


def test_password_stdin_rejects_terminal_input(
    monkeypatch: Any,
    login_flow: dict[str, Any],
) -> None:
    class TerminalInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", TerminalInput("test-password\n"))
    assert cli.main(login_args("--password-stdin")) == 1
    assert "created" not in login_flow


def test_piped_password_never_falls_back_to_piped_mfa_without_terminal(
    monkeypatch: Any,
    capsys: Any,
    login_flow: dict[str, Any],
) -> None:
    stream = io.StringIO("test-password\nnot-a-terminal-mfa-code\n")
    monkeypatch.setattr(cli.sys, "stdin", stream)

    def no_terminal(label: str) -> str:
        warnings.warn("Cannot control echo on the terminal.", getpass.GetPassWarning)
        return stream.readline()

    monkeypatch.setattr(cli_login.getpass, "getpass", no_terminal)
    assert cli.main(login_args("--password-stdin")) == 1
    assert stream.readline() == "not-a-terminal-mfa-code\n"
    assert login_flow["closed"]
    output = capsys.readouterr()
    assert "test-password" not in output.out + output.err
    assert json.loads(output.out)["error_type"] == "GetPassWarning"


@pytest.mark.skipif(os.name != "posix", reason="Requires a POSIX controlling terminal")
def test_real_terminal_reads_mfa_while_password_comes_from_a_pipe(
    tmp_path: Path,
) -> None:
    import pty

    child_code = """
import getpass
from mcp_jumpserver_gui_sucks import cli
from mcp_jumpserver_gui_sucks.auth_state import AuthState
from mcp_jumpserver_gui_sucks.cli_login import CLILoginResult, prompt_mfa_code

def login(settings, **kwargs):
    assert kwargs["password"] == "piped-test-password"
    assert prompt_mfa_code("test MFA code") == "123456"
    assert getpass.getpass("Enter captcha answer: ") == "42"
    state = AuthState(base_url=kwargs["base_url"])
    return CLILoginResult(state, str(settings.state_file), "alice", [], False, False, False)

cli.run_cli_login = login
raise SystemExit(cli.main([
    "login", "--base-url", "https://jumpserver.example.com", "--username", "alice",
    "--password-stdin",
]))
"""
    password_read, password_write = os.pipe()
    pid, terminal = pty.fork()
    if pid == 0:
        os.close(password_write)
        os.dup2(password_read, 0)
        os.close(password_read)
        os.environ[STATE_FILE_ENV_NAME] = str(tmp_path / "auth-state.json")
        os.execv(sys.executable, [sys.executable, "-c", child_code])
    os.close(password_read)
    os.write(password_write, b"piped-test-password\n")
    os.close(password_write)
    output = b""
    answers = [
        (b"Enter test MFA code: ", b"123456\n"),
        (b"Enter captcha answer: ", b"42\n"),
    ]
    deadline = time.monotonic() + 10
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(terminal, 4096)
                except OSError:
                    chunk = b""
                output += chunk
                if answers and answers[0][0] in output:
                    os.write(terminal, answers.pop(0)[1])
            finished, child_status = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = child_status
                break
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
    assert status == 0, output.decode(errors="replace")
    assert not answers
    assert b"piped-test-password" not in output
    assert b"123456" not in output
    assert b"42" not in output
