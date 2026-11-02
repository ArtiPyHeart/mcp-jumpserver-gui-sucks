import tempfile
import unittest
from pathlib import Path

import httpx

from mcp_jumpserver_gui_sucks.captcha import default_captcha_path
from mcp_jumpserver_gui_sucks.cli_login import JumpServerCLILogin
from mcp_jumpserver_gui_sucks.config import Settings


def make_settings() -> Settings:
    return Settings(
        base_url="http://jumpserver.example.com",
        org_id="",
        state_dir=Path(tempfile.gettempdir()),
        state_file=Path(tempfile.gettempdir()) / "jms-test-state.json",
        log_level="INFO",
        verify_tls=True,
        request_timeout_seconds=5.0,
        terminal_idle_timeout_seconds=3600.0,
        terminal_reap_interval_seconds=30.0,
        max_terminal_sessions=8,
    )


def make_flow() -> JumpServerCLILogin:
    return JumpServerCLILogin(
        make_settings(),
        base_url="http://jumpserver.example.com",
        org_id="",
    )


def make_request(method: str = "POST") -> httpx.Request:
    return httpx.Request(method, "http://jumpserver.example.com/api/v1/authentication/confirm/")


class DjangoCsrfCookieTokenTests(unittest.TestCase):
    def test_unquotes_percent_encoded_cookie(self) -> None:
        from mcp_jumpserver_gui_sucks.client import django_csrf_cookie_token

        self.assertEqual(django_csrf_cookie_token("aa%3Abb%2Bcc"), "aa:bb+cc")

    def test_leaves_plain_token_untouched(self) -> None:
        from mcp_jumpserver_gui_sucks.client import django_csrf_cookie_token

        self.assertEqual(django_csrf_cookie_token("LKpCOV7BHVZEVthkGqguJLJGEHn7dPbX"), "LKpCOV7BHVZEVthkGqguJLJGEHn7dPbX")


class CsrfHeaderSyncTests(unittest.TestCase):
    def test_sync_header_follows_rotated_cookie(self) -> None:
        flow = make_flow()
        flow._client.cookies.set(
            "jms_csrftoken", "token-a", domain="jumpserver.example.com", path="/"
        )
        request = make_request()
        flow._sync_csrf_header(request)
        self.assertEqual(request.headers["X-CSRFToken"], "token-a")

        flow._client.cookies.set(
            "jms_csrftoken", "token-b", domain="jumpserver.example.com", path="/"
        )
        rotated = make_request()
        flow._sync_csrf_header(rotated)
        self.assertEqual(rotated.headers["X-CSRFToken"], "token-b")

    def test_sync_header_falls_back_to_existing_header(self) -> None:
        flow = make_flow()
        flow._client.headers["X-CSRFToken"] = "bootstrap-token"
        request = make_request()
        flow._sync_csrf_header(request)
        self.assertEqual(request.headers["X-CSRFToken"], "bootstrap-token")

    def test_sync_header_unquotes_encoded_cookie(self) -> None:
        flow = make_flow()
        flow._client.cookies.set(
            "jms_csrftoken", "aa%3Abb", domain="jumpserver.example.com", path="/"
        )
        request = make_request()
        flow._sync_csrf_header(request)
        self.assertEqual(request.headers["X-CSRFToken"], "aa:bb")

    def test_sync_header_skips_safe_methods(self) -> None:
        flow = make_flow()
        flow._client.cookies.set(
            "jms_csrftoken", "token-a", domain="jumpserver.example.com", path="/"
        )
        for method in ("GET", "HEAD", "OPTIONS"):
            request = make_request(method)
            flow._sync_csrf_header(request)
            self.assertNotIn("X-CSRFToken", request.headers)


class AuthStateBuilderTests(unittest.TestCase):
    def test_web_session_state_prefers_cookie_over_stale_header(self) -> None:
        flow = make_flow()
        flow._client.headers["X-CSRFToken"] = "stale-bootstrap-token"
        flow._client.cookies.set(
            "jms_csrftoken", "fresh-token", domain="jumpserver.example.com", path="/"
        )
        state = flow.build_web_session_auth_state(
            username="tester",
            login_source="cli-web-session-cookie",
        )
        self.assertEqual(state.headers["X-CSRFToken"], "fresh-token")

    def test_access_key_state_prefers_cookie_over_stale_header(self) -> None:
        flow = make_flow()
        flow._client.headers["X-CSRFToken"] = "stale-bootstrap-token"
        flow._client.cookies.set(
            "jms_csrftoken", "fresh-token", domain="jumpserver.example.com", path="/"
        )
        state = flow.build_access_key_auth_state(
            username="tester",
            key_id="key-id",
            secret="key-secret",
            login_source="cli-access-key",
        )
        self.assertEqual(state.headers["X-CSRFToken"], "fresh-token")


class CaptchaPathTests(unittest.TestCase):
    def test_default_path_uses_system_tempdir(self) -> None:
        path = default_captcha_path("abc")
        self.assertEqual(path.parent, Path(tempfile.gettempdir()))
        self.assertEqual(path.name, "jumpserver-captcha-abc.png")


if __name__ == "__main__":
    unittest.main()
