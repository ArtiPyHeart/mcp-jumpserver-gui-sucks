from __future__ import annotations

import getpass
import re
import logging
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx

from .auth_state import AuthState, build_cookie_state_from_jar
from .captcha import fetch_captcha_challenge, save_and_open_captcha_challenge
from .client import JumpServerClient
from .config import Settings
from .crypto import encrypt_password
from .errors import ConfigError, JumpServerMCPError

LOGGER = logging.getLogger(__name__)
LOGIN_PAGE_PATH = "/core/auth/login/"
LOGIN_GUARD_PATH = "/core/auth/login/guard/"
LOGIN_MFA_PAGE_PATH = "/core/auth/login/mfa/"
AUTH_API_PATH = "/api/v1/authentication/auth/"
MFA_SEND_CODE_PATH = "/api/v1/authentication/mfa/send-code/"
MFA_VERIFY_PATH = "/api/v1/authentication/mfa/verify/"
CONFIRM_API_PATH = "/api/v1/authentication/confirm/"
ACCESS_KEYS_API_PATH = "/api/v1/authentication/access-keys/"
PROFILE_API_PATH = "/api/v1/users/profile/"
USER_SESSION_API_PATH = "/api/v1/authentication/user-session/"
SUPPORTED_CODE_MFA_TYPES = {"otp", "sms", "email", "otp_radius", "mfa_custom"}
CHALLENGE_MFA_TYPES = {"sms", "email"}
UNSUPPORTED_CLI_MFA_TYPES = {"passkey", "face"}
REDACTED = "<redacted>"
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_ERROR_PATTERNS = (
    re.compile(r'<p[^>]*class="[^"]*red-fonts[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<p[^>]*class="[^"]*help-block[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL),
)
MFA_OPTION_RE = re.compile(r'<option value="([^"]+)"', re.IGNORECASE)
SENSITIVE_KEYS = {
    "access_key_secret",
    "authorization",
    "bearer_token",
    "cookie",
    "cookies",
    "password",
    "secret",
    "secret_key",
    "token",
}


class LoginFlowError(JumpServerMCPError):
    """Raised when the CLI login flow cannot complete."""


@dataclass(slots=True)
class CLILoginResult:
    auth_state: AuthState
    state_file: str
    username: str
    auth_modes: list[str]
    durable_auth: bool
    access_key_created: bool
    access_key_reused: bool
    warnings: list[str] = field(default_factory=list)
    org_id: str = ""
    bearer_expires_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "access_key_created": self.access_key_created,
            "access_key_reused": self.access_key_reused,
            "auth_modes": self.auth_modes,
            "base_url": self.auth_state.base_url,
            "bearer_expires_at": self.bearer_expires_at or None,
            "durable_auth": self.durable_auth,
            "header_names": self.auth_state.header_names(),
            "login_source": self.auth_state.login_source,
            "org_id": self.org_id or None,
            "state_file": self.state_file,
            "username": self.username,
            "warnings": self.warnings,
        }


def prompt_choice(title: str, choices: Iterable[str]) -> str:
    options = list(choices)
    if not options:
        raise LoginFlowError(f"No selectable options were returned for {title}.")
    if len(options) == 1:
        return options[0]

    print(title)
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option}")

    while True:
        raw = input("Select one option by number: ").strip()
        if not raw.isdigit():
            print("Please enter a valid number.")
            continue
        selected = int(raw)
        if 1 <= selected <= len(options):
            return options[selected - 1]
        print("Choice out of range.")


def build_cookie_state(client: httpx.Client, base_url: str):
    return build_cookie_state_from_jar(client.cookies.jar, base_url)


def extract_response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise LoginFlowError(
            f"Expected a JSON response from {response.request.method} {response.request.url.path}."
        ) from exc


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_KEYS:
                sanitized[str(key)] = REDACTED
            else:
                sanitized[str(key)] = sanitize_payload(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]

    return value


def raise_for_unexpected_status(response: httpx.Response, expected: set[int]) -> None:
    if response.status_code in expected:
        return
    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = response.text.strip()
    detail = payload if isinstance(payload, str) else str(sanitize_payload(payload))
    raise LoginFlowError(
        f"JumpServer login flow request failed: {response.request.method} "
        f"{response.request.url.path} -> HTTP {response.status_code} ({detail})"
    )


def strip_html(value: str) -> str:
    collapsed = HTML_TAG_RE.sub(" ", unescape(value))
    return " ".join(collapsed.split())


def extract_form_errors(html: str) -> list[str]:
    messages: list[str] = []
    for pattern in HTML_ERROR_PATTERNS:
        for match in pattern.findall(html):
            message = strip_html(match)
            if message and message not in messages:
                messages.append(message)
    return messages


def extract_mfa_options(html: str) -> list[str]:
    return list(dict.fromkeys(option for option in MFA_OPTION_RE.findall(html) if option))


class JumpServerCLILogin:
    def __init__(self, settings: Settings, *, base_url: str, org_id: str = "") -> None:
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._org_id = org_id.strip()
        self._last_login_page_html = ""
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_tls,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        if self._org_id:
            self._client.headers["X-JMS-ORG"] = self._org_id

    def close(self) -> None:
        self._client.close()

    def bootstrap(self) -> None:
        LOGGER.info("Bootstrapping CLI login session.")
        response = self._client.get(LOGIN_PAGE_PATH)
        raise_for_unexpected_status(response, {200})
        self._last_login_page_html = response.text

        cookie_names = {cookie.name for cookie in self._client.cookies.jar}
        if "jms_public_key" not in cookie_names:
            raise LoginFlowError(
                "The login bootstrap response did not provide the jms_public_key cookie."
            )

        csrf_token = self._client.cookies.get("jms_csrftoken")
        if csrf_token:
            self._client.headers["X-CSRFToken"] = csrf_token

        org_cookie = self._client.cookies.get("X-JMS-ORG")
        if org_cookie and not self._client.headers.get("X-JMS-ORG"):
            self._client.headers["X-JMS-ORG"] = org_cookie

    def _prompt_login_captcha_fields(self) -> dict[str, str]:
        if not self._last_login_page_html:
            return {}

        challenge = fetch_captcha_challenge(self._client, self._last_login_page_html)
        if challenge is None:
            return {}

        LOGGER.info("Captcha challenge detected for the CLI web login flow.")
        saved_path, open_error = save_and_open_captcha_challenge(challenge)
        print(f"Captcha challenge image saved to {saved_path}")
        if open_error:
            print(f"Could not open the captcha image automatically: {open_error}")
        else:
            print("Opened the captcha image in the system viewer.")

        captcha_code = input("Enter captcha: ").strip()
        if not captcha_code:
            raise LoginFlowError("Missing captcha value.")
        return {
            "captcha_0": challenge.key,
            "captcha_1": captcha_code,
        }

    def _absolute_url(self, path: str) -> str:
        return urljoin(f"{self._base_url}/", path.lstrip("/"))

    def _normalize_location(self, location: str) -> str:
        if not location:
            raise LoginFlowError("The JumpServer login flow did not return a redirect target.")
        if "://" in location:
            return location
        return self._absolute_url(location)

    def _form_headers(self, referer_path: str) -> dict[str, str]:
        return {
            "Origin": self._base_url,
            "Referer": self._absolute_url(referer_path),
        }

    def _csrf_token(self) -> str:
        csrf_token = self._client.cookies.get("jms_csrftoken") or self._client.headers.get(
            "X-CSRFToken", ""
        )
        if not csrf_token:
            raise LoginFlowError("The current login session does not have a CSRF token.")
        self._client.headers["X-CSRFToken"] = csrf_token
        return csrf_token

    def encrypt_password(self, password: str) -> str:
        public_key_cookie = self._client.cookies.get("jms_public_key", "")
        return encrypt_password(password, public_key_cookie)

    def submit_web_login_form(
        self,
        *,
        username: str,
        encrypted_password: str,
        auto_login: bool = True,
    ) -> str:
        LOGGER.info("Submitting the web login form for %s.", username)
        payload = {
            "csrfmiddlewaretoken": self._csrf_token(),
            "username": username,
            "password": encrypted_password,
        }
        payload.update(self._prompt_login_captcha_fields())
        if auto_login:
            payload["auto_login"] = "on"
        response = self._client.post(
            LOGIN_PAGE_PATH,
            data=payload,
            headers=self._form_headers(LOGIN_PAGE_PATH),
            follow_redirects=False,
        )
        raise_for_unexpected_status(response, {200, 302})
        if response.status_code == 200:
            self._last_login_page_html = response.text
            form_errors = extract_form_errors(response.text)
            detail = f" Errors: {form_errors}" if form_errors else ""
            raise LoginFlowError(f"Web login form was rejected.{detail}")
        return self._normalize_location(response.headers.get("location", ""))

    def fetch_web_mfa_options(self) -> list[str]:
        LOGGER.info("Loading the web MFA form.")
        response = self._client.get(
            LOGIN_MFA_PAGE_PATH,
            headers=self._form_headers(LOGIN_GUARD_PATH),
            follow_redirects=False,
        )
        raise_for_unexpected_status(response, {200, 302})
        if response.status_code == 302:
            location = response.headers.get("location", "")
            raise LoginFlowError(
                f"JumpServer redirected away from the MFA form unexpectedly: {location}"
            )
        options = extract_mfa_options(response.text)
        if not options:
            raise LoginFlowError("The web MFA form did not expose any selectable MFA options.")
        return options

    def submit_web_mfa(
        self,
        *,
        mfa_type: str,
        code: str,
    ) -> str:
        LOGGER.info("Submitting the web MFA form for type %s.", mfa_type)
        response = self._client.post(
            LOGIN_MFA_PAGE_PATH,
            data={
                "csrfmiddlewaretoken": self._csrf_token(),
                "mfa_type": mfa_type,
                "code": code,
            },
            headers=self._form_headers(LOGIN_MFA_PAGE_PATH),
            follow_redirects=False,
        )
        raise_for_unexpected_status(response, {200, 302})
        if response.status_code == 200:
            form_errors = extract_form_errors(response.text)
            detail = f" Errors: {form_errors}" if form_errors else ""
            raise LoginFlowError(f"Web MFA verification failed.{detail}")
        return self._normalize_location(response.headers.get("location", ""))

    def check_web_session(self) -> dict[str, Any]:
        LOGGER.info("Checking whether the current cookie jar is a real web session.")
        response = self._client.get(USER_SESSION_API_PATH)
        raise_for_unexpected_status(response, {200})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The user-session endpoint returned a non-object payload.")
        if not payload.get("ok"):
            raise LoginFlowError(f"JumpServer did not confirm the web session: {payload}")
        return payload

    def establish_web_session(
        self,
        *,
        username: str,
        encrypted_password: str,
        login_mfa_type: str = "",
    ) -> dict[str, Any]:
        location = self.submit_web_login_form(
            username=username,
            encrypted_password=encrypted_password,
            auto_login=True,
        )

        for _ in range(6):
            parsed = urlparse(location)
            path = parsed.path or "/"
            if path.startswith(LOGIN_GUARD_PATH):
                LOGGER.info("Following the JumpServer login guard.")
                response = self._client.get(
                    location,
                    headers=self._form_headers(LOGIN_PAGE_PATH),
                    follow_redirects=False,
                )
                raise_for_unexpected_status(response, {302})
                location = self._normalize_location(response.headers.get("location", ""))
                parsed = urlparse(location)
                path = parsed.path or "/"
                if path.startswith(LOGIN_MFA_PAGE_PATH):
                    available_types = self.fetch_web_mfa_options()
                    selected_mfa = choose_mfa_type(
                        requested=login_mfa_type,
                        available=available_types,
                        title="Select an MFA method for web-session login:",
                    )
                    ensure_cli_supported_mfa(selected_mfa)
                    if selected_mfa in CHALLENGE_MFA_TYPES:
                        self.send_mfa_code(mfa_type=selected_mfa)
                    login_code = prompt_mfa_code(f"MFA code for {selected_mfa}")
                    location = self.submit_web_mfa(
                        mfa_type=selected_mfa,
                        code=login_code,
                    )
                    continue
                if path.startswith(LOGIN_PAGE_PATH):
                    raise LoginFlowError(
                        f"JumpServer redirected the web login back to the login page: {location}"
                    )
                return self.check_web_session()

            if path.startswith(LOGIN_MFA_PAGE_PATH):
                available_types = self.fetch_web_mfa_options()
                selected_mfa = choose_mfa_type(
                    requested=login_mfa_type,
                    available=available_types,
                    title="Select an MFA method for web-session login:",
                )
                ensure_cli_supported_mfa(selected_mfa)
                if selected_mfa in CHALLENGE_MFA_TYPES:
                    self.send_mfa_code(mfa_type=selected_mfa)
                login_code = prompt_mfa_code(f"MFA code for {selected_mfa}")
                location = self.submit_web_mfa(
                    mfa_type=selected_mfa,
                    code=login_code,
                )
                continue

            return self.check_web_session()

        raise LoginFlowError(
            "The web-session login flow exceeded the expected redirect depth."
        )

    def start_auth(self, *, username: str, password: str) -> dict[str, Any]:
        LOGGER.info("Submitting primary authentication request for %s.", username)
        response = self._client.post(
            AUTH_API_PATH,
            json={"username": username, "password": password},
        )
        raise_for_unexpected_status(response, {200, 400})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The auth endpoint returned a non-object payload.")
        return payload

    def issue_bearer_token(self) -> dict[str, Any]:
        LOGGER.info("Requesting bearer token from the authenticated session.")
        response = self._client.post(AUTH_API_PATH, json={})
        raise_for_unexpected_status(response, {200, 201, 400})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The bearer-token endpoint returned a non-object payload.")
        if "token" not in payload:
            raise LoginFlowError(f"Did not receive a bearer token payload: {payload}")
        return payload

    def send_mfa_code(self, *, mfa_type: str) -> None:
        LOGGER.info("Requesting MFA challenge for type %s.", mfa_type)
        response = self._client.post(MFA_SEND_CODE_PATH, json={"type": mfa_type})
        raise_for_unexpected_status(response, {200, 201})
        payload = extract_response_json(response)
        if isinstance(payload, dict) and payload.get("error"):
            raise LoginFlowError(f"MFA challenge request failed: {sanitize_payload(payload)}")

    def verify_mfa(self, *, mfa_type: str, code: str) -> dict[str, Any]:
        LOGGER.info("Submitting MFA verification for type %s.", mfa_type)
        response = self._client.post(
            MFA_VERIFY_PATH,
            json={"type": mfa_type, "code": code},
        )
        raise_for_unexpected_status(response, {200, 401})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The MFA verify endpoint returned a non-object payload.")
        if payload.get("error"):
            raise LoginFlowError(f"MFA verification failed: {sanitize_payload(payload)}")
        return payload

    def get_profile(self, *, bearer_token: str = "", bearer_keyword: str = "Bearer") -> dict[str, Any]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"{bearer_keyword} {bearer_token}"

        response = self._client.get(PROFILE_API_PATH, headers=headers)
        raise_for_unexpected_status(response, {200})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The profile endpoint returned a non-object payload.")
        return payload

    def get_confirm_descriptor(
        self,
        *,
        bearer_token: str = "",
        bearer_keyword: str = "Bearer",
        confirm_type: str = "password",
    ) -> dict[str, Any]:
        LOGGER.info("Fetching confirmation descriptor for %s.", confirm_type)
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"{bearer_keyword} {bearer_token}"
        response = self._client.get(
            CONFIRM_API_PATH,
            params={"confirm_type": confirm_type},
            headers=headers,
        )
        raise_for_unexpected_status(response, {200, 400})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The confirm descriptor endpoint returned a non-object payload.")
        if payload.get("error"):
            raise LoginFlowError(f"Confirmation discovery failed: {sanitize_payload(payload)}")
        return payload

    def complete_confirmation(
        self,
        *,
        bearer_token: str = "",
        bearer_keyword: str = "Bearer",
        confirm_type: str,
        mfa_type: str = "",
        secret_key: str = "",
    ) -> None:
        LOGGER.info("Completing confirmation through backend %s.", confirm_type)
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"{bearer_keyword} {bearer_token}"
        response = self._client.post(
            CONFIRM_API_PATH,
            headers=headers,
            json={
                "confirm_type": "password",
                "mfa_type": mfa_type,
                "secret_key": secret_key,
            },
        )
        raise_for_unexpected_status(response, {200, 400})
        if response.status_code == 200 and response.text.strip('"') == "ok":
            return

        payload = extract_response_json(response)
        if isinstance(payload, dict) and payload.get("error"):
            raise LoginFlowError(f"Confirmation failed: {sanitize_payload(payload)}")
        raise LoginFlowError(f"Unexpected confirmation response: {sanitize_payload(payload)}")

    def create_access_key(
        self,
        *,
        bearer_token: str = "",
        bearer_keyword: str = "Bearer",
        ip_group: list[str] | None = None,
    ) -> dict[str, Any]:
        LOGGER.info("Creating a durable JumpServer access key.")
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"{bearer_keyword} {bearer_token}"
        response = self._client.post(
            ACCESS_KEYS_API_PATH,
            headers=headers,
            json={"ip_group": ip_group or ["*"]},
        )
        raise_for_unexpected_status(response, {201, 400})
        payload = extract_response_json(response)
        if not isinstance(payload, dict):
            raise LoginFlowError("The access-key endpoint returned a non-object payload.")
        if payload.get("error"):
            raise LoginFlowError(f"Access-key creation failed: {sanitize_payload(payload)}")
        return payload

    def build_ephemeral_auth_state(
        self,
        *,
        username: str,
        bearer_payload: dict[str, Any],
        login_source: str,
    ) -> AuthState:
        cookies = build_cookie_state(self._client, self._base_url)
        cookie_lookup = {cookie.name: cookie.value for cookie in cookies}
        headers: dict[str, str] = {}
        csrf_token = self._client.headers.get("X-CSRFToken") or cookie_lookup.get("jms_csrftoken")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        org_id = self._client.headers.get("X-JMS-ORG") or cookie_lookup.get("X-JMS-ORG") or self._org_id
        if org_id:
            headers["X-JMS-ORG"] = org_id

        return AuthState(
            base_url=self._base_url,
            login_source=login_source,
            headers=headers,
            cookies=cookies,
            bearer_token=str(bearer_payload.get("token", "")),
            bearer_keyword=str(bearer_payload.get("keyword", "Bearer") or "Bearer"),
            bearer_expires_at=str(bearer_payload.get("date_expired", "")),
            metadata={
                "saved_by": "login",
                "username": username,
                "durable_auth": False,
            },
        )

    def build_access_key_auth_state(
        self,
        *,
        username: str,
        key_id: str,
        secret: str,
        login_source: str,
        existing_headers: dict[str, str] | None = None,
    ) -> AuthState:
        headers = dict(existing_headers or {})
        cookies = build_cookie_state(self._client, self._base_url)
        cookie_lookup = {cookie.name: cookie.value for cookie in cookies}
        csrf_token = self._client.headers.get("X-CSRFToken") or cookie_lookup.get("jms_csrftoken")
        if csrf_token and "X-CSRFToken" not in headers:
            headers["X-CSRFToken"] = csrf_token
        if self._org_id and "X-JMS-ORG" not in headers:
            headers["X-JMS-ORG"] = self._org_id
        elif "X-JMS-ORG" not in headers:
            org_cookie = cookie_lookup.get("X-JMS-ORG")
            if org_cookie:
                headers["X-JMS-ORG"] = org_cookie

        return AuthState(
            base_url=self._base_url,
            login_source=login_source,
            headers=headers,
            cookies=cookies,
            access_key_id=key_id,
            access_key_secret=secret,
            metadata={
                "saved_by": "login",
                "username": username,
                "durable_auth": True,
                "cookie_session_authenticated": True,
                "session_cookies_captured": bool(cookies),
            },
        )

    def build_web_session_auth_state(
        self,
        *,
        username: str,
        login_source: str,
    ) -> AuthState:
        cookies = build_cookie_state(self._client, self._base_url)
        cookie_lookup = {cookie.name: cookie.value for cookie in cookies}
        headers: dict[str, str] = {}
        csrf_token = self._client.headers.get("X-CSRFToken") or cookie_lookup.get("jms_csrftoken")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        org_id = self._client.headers.get("X-JMS-ORG") or cookie_lookup.get("X-JMS-ORG") or self._org_id
        if org_id:
            headers["X-JMS-ORG"] = org_id

        return AuthState(
            base_url=self._base_url,
            login_source=login_source,
            headers=headers,
            cookies=cookies,
            metadata={
                "saved_by": "login",
                "username": username,
                "durable_auth": False,
                "cookie_session_authenticated": True,
            },
        )


def choose_mfa_type(
    *,
    requested: str,
    available: Iterable[str],
    title: str,
) -> str:
    choices = [choice for choice in available if choice]
    if requested:
        if requested not in choices:
            raise LoginFlowError(
                f"Requested MFA type {requested!r} is not available. Choices: {choices}"
            )
        return requested
    return prompt_choice(title, choices)


def ensure_cli_supported_mfa(mfa_type: str) -> None:
    if mfa_type in UNSUPPORTED_CLI_MFA_TYPES:
        raise LoginFlowError(
            f"The MFA type {mfa_type!r} is not supported in CLI-first mode."
        )
    if mfa_type not in SUPPORTED_CODE_MFA_TYPES:
        raise LoginFlowError(f"Unknown MFA type {mfa_type!r}.")


def prompt_mfa_code(label: str) -> str:
    code = getpass.getpass(f"Enter {label}: ").strip()
    if not code:
        raise LoginFlowError(f"Missing {label}.")
    return code


def run_cli_login(
    settings: Settings,
    *,
    base_url: str,
    username: str,
    org_id: str = "",
    login_mfa_type: str = "",
    confirm_mfa_type: str = "",
    allow_ephemeral: bool = False,
) -> CLILoginResult:
    if not username.strip():
        raise ConfigError("Missing username. Provide --username for CLI login.")

    password = getpass.getpass("Password: ")
    if not password:
        raise LoginFlowError("Password input was empty.")

    flow = JumpServerCLILogin(settings, base_url=base_url, org_id=org_id)
    warnings: list[str] = []
    try:
        flow.bootstrap()
        encrypted_password = flow.encrypt_password(password)
        flow.establish_web_session(
            username=username,
            encrypted_password=encrypted_password,
            login_mfa_type=login_mfa_type,
        )

        existing_auth_state = None
        existing_store_path = settings.state_file
        try:
            from .session_store import SessionStore

            existing_auth_state = SessionStore(existing_store_path).load()
        except Exception:
            existing_auth_state = None

        access_key_reused = False
        access_key_created = False
        final_auth_state: AuthState | None = None
        state_headers: dict[str, str] = {}
        if org_id:
            state_headers["X-JMS-ORG"] = org_id
        elif existing_auth_state and existing_auth_state.base_url.rstrip("/") == base_url.rstrip("/"):
            if existing_auth_state.headers.get("X-JMS-ORG"):
                state_headers["X-JMS-ORG"] = existing_auth_state.headers["X-JMS-ORG"]

        if existing_auth_state and existing_auth_state.has_access_key_auth():
            LOGGER.info("Checking whether the existing access key can be reused.")
            candidate = AuthState(
                base_url=base_url,
                headers=state_headers,
                access_key_id=existing_auth_state.access_key_id,
                access_key_secret=existing_auth_state.access_key_secret,
            )
            client = JumpServerClient(settings, candidate)
            try:
                profile = client.get_profile_sync()
            except Exception:
                LOGGER.info("The existing access key is not reusable anymore.")
            else:
                final_auth_state = flow.build_access_key_auth_state(
                    username=str(profile.get("username", username)),
                    key_id=existing_auth_state.access_key_id,
                    secret=existing_auth_state.access_key_secret,
                    login_source="cli-web-session-access-key",
                    existing_headers=state_headers,
                )
                final_auth_state.metadata["reused_existing_access_key"] = True
                access_key_reused = True

        if final_auth_state is None:
            try:
                confirm_descriptor = flow.get_confirm_descriptor(
                    confirm_type="password",
                )
                resolved_confirm_type = str(confirm_descriptor.get("confirm_type", ""))

                if resolved_confirm_type == "mfa":
                    confirm_choices = [
                        item.get("name", "")
                        for item in list(confirm_descriptor.get("content", []))
                        if not item.get("disabled", False)
                    ]
                    selected_confirm_mfa = choose_mfa_type(
                        requested=confirm_mfa_type or login_mfa_type,
                        available=confirm_choices,
                        title="Select an MFA method for durable access-key confirmation:",
                    )
                    ensure_cli_supported_mfa(selected_confirm_mfa)
                    if selected_confirm_mfa in CHALLENGE_MFA_TYPES:
                        flow.send_mfa_code(mfa_type=selected_confirm_mfa)
                    confirm_code = prompt_mfa_code(
                        f"confirmation MFA code for {selected_confirm_mfa}"
                    )
                    flow.complete_confirmation(
                        confirm_type=resolved_confirm_type,
                        mfa_type=selected_confirm_mfa,
                        secret_key=confirm_code,
                    )
                elif resolved_confirm_type == "password":
                    flow.complete_confirmation(
                        confirm_type=resolved_confirm_type,
                        secret_key=encrypted_password,
                    )
                else:
                    raise LoginFlowError(
                        "Unsupported confirmation backend for durable auth: "
                        f"{sanitize_payload(confirm_descriptor)}"
                    )

                key_payload = flow.create_access_key()
                key_id = str(key_payload.get("id", ""))
                secret = str(key_payload.get("secret", ""))
                if not key_id or not secret:
                    raise LoginFlowError(
                        "Access-key creation returned an incomplete payload: "
                        f"{sanitize_payload(key_payload)}"
                    )
                final_auth_state = flow.build_access_key_auth_state(
                    username=username,
                    key_id=key_id,
                    secret=secret,
                    login_source="cli-web-session-access-key",
                    existing_headers=state_headers,
                )
                access_key_created = True
            except LoginFlowError as exc:
                if not allow_ephemeral:
                    raise
                warnings.append(
                    "Durable access-key setup failed; saving only the authenticated web session. "
                    f"Reason: {exc}"
                )
                final_auth_state = flow.build_web_session_auth_state(
                    username=username,
                    login_source="cli-web-session-cookie",
                )

        durable_auth = final_auth_state.has_durable_auth()
        if not durable_auth and not allow_ephemeral:
            raise LoginFlowError(
                "CLI login completed, but a durable access key was not established. "
                "Re-run with --allow-ephemeral only if short-lived bearer auth is acceptable."
            )

        if not durable_auth:
            warnings.append(
                "The saved auth state is cookie-backed because no durable access key was created."
            )
            final_auth_state = flow.build_web_session_auth_state(
                username=username,
                login_source="cli-web-session-cookie",
            )

        return CLILoginResult(
            auth_state=final_auth_state,
            state_file=str(settings.state_file),
            username=username,
            auth_modes=final_auth_state.auth_modes(),
            durable_auth=durable_auth,
            access_key_created=access_key_created,
            access_key_reused=access_key_reused,
            warnings=warnings,
            org_id=final_auth_state.headers.get("X-JMS-ORG", ""),
            bearer_expires_at=final_auth_state.bearer_expires_at,
        )
    finally:
        flow.close()
