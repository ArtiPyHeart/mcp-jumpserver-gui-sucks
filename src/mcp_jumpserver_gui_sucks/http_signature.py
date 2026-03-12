from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from email.utils import format_datetime

DEFAULT_SIGNATURE_HEADERS = ["(request-target)", "date"]


def make_http_date(now: datetime | None = None) -> str:
    resolved = now or datetime.now(tz=UTC)
    return format_datetime(resolved, usegmt=True)


def build_signing_string(
    *,
    method: str,
    path_with_query: str,
    headers: dict[str, str],
    signed_headers: list[str],
) -> str:
    lower_headers = {key.lower(): value for key, value in headers.items()}
    lines: list[str] = []

    for name in signed_headers:
        lower_name = name.lower()
        if lower_name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path_with_query}")
            continue

        value = lower_headers.get(lower_name)
        if value is None:
            raise ValueError(f"Missing header required for signature: {name}")
        lines.append(f"{lower_name}: {value}")

    return "\n".join(lines)


def build_signature_authorization(
    *,
    key_id: str,
    secret: str,
    method: str,
    path_with_query: str,
    headers: dict[str, str],
    algorithm: str = "hmac-sha256",
    signed_headers: list[str] | None = None,
) -> str:
    resolved_headers = signed_headers or list(DEFAULT_SIGNATURE_HEADERS)
    signing_string = build_signing_string(
        method=method,
        path_with_query=path_with_query,
        headers=headers,
        signed_headers=resolved_headers,
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_string.encode("ascii"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    header_list = " ".join(name.lower() for name in resolved_headers)
    return (
        'Signature '
        f'keyId="{key_id}",'
        f'algorithm="{algorithm}",'
        f'headers="{header_list}",'
        f'signature="{signature}"'
    )
