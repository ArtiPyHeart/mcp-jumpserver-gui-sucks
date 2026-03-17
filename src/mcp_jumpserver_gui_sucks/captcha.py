from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
import re

import httpx

LOGGER = logging.getLogger(__name__)
CAPTCHA_IMAGE_RE = re.compile(r"(?P<path>/core/auth/captcha/image/(?P<key>[0-9a-f]+)/)")


@dataclass(slots=True)
class CaptchaChallenge:
    key: str
    image_path: str
    image_bytes: bytes


def has_no_captcha_challenge(html: str) -> bool:
    return "no-captcha-challenge" in html


def fetch_captcha_challenge(client: httpx.Client, html: str) -> CaptchaChallenge | None:
    match = CAPTCHA_IMAGE_RE.search(html)
    if not match:
        return None

    image_path = match.group("path")
    captcha_key = match.group("key")
    response = client.get(image_path)
    response.raise_for_status()
    return CaptchaChallenge(
        key=captcha_key,
        image_path=image_path,
        image_bytes=response.content,
    )


def default_captcha_path(captcha_key: str) -> Path:
    return Path("/private/tmp") / f"jumpserver-captcha-{captcha_key}.png"


def save_captcha_challenge(
    challenge: CaptchaChallenge,
    *,
    path: Path | None = None,
) -> Path:
    target_path = path or default_captcha_path(challenge.key)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(challenge.image_bytes)
    LOGGER.debug("Saved login captcha image to %s.", target_path)
    return target_path


def open_captcha_path(path: Path) -> str | None:
    command = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        return f"The image opener command is unavailable for this platform: {command[0]}"
    except subprocess.CalledProcessError as exc:
        return f"The image opener command failed with exit status {exc.returncode}."
    return None


def save_and_open_captcha_challenge(
    challenge: CaptchaChallenge,
    *,
    path: Path | None = None,
) -> tuple[Path, str | None]:
    saved_path = save_captcha_challenge(challenge, path=path)
    open_error = open_captcha_path(saved_path)
    if open_error:
        LOGGER.warning("Could not open the captcha image automatically: %s", open_error)
    else:
        LOGGER.debug("Opened the captcha image in the system viewer.")
    return saved_path, open_error
