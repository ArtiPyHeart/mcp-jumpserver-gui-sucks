from __future__ import annotations

import base64
import logging
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LOGGER = logging.getLogger(__name__)
AES_KEY_LENGTH = 16
ZERO_BYTE = b"\0"


def fill_aes_key(origin_key: str) -> bytes:
    raw = origin_key.encode("utf-8")
    if len(raw) >= AES_KEY_LENGTH:
        return raw[:AES_KEY_LENGTH]
    return raw.ljust(AES_KEY_LENGTH, ZERO_BYTE)


def zero_pad(raw: bytes) -> bytes:
    padding_length = (-len(raw)) % AES_KEY_LENGTH
    if padding_length == 0:
        return raw
    return raw + (ZERO_BYTE * padding_length)


def aes_encrypt_ecb(text: str, origin_key: str) -> str:
    key = fill_aes_key(origin_key)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(zero_pad(text.encode("utf-8"))) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def decode_public_key_cookie(value: str) -> bytes:
    normalized = value.strip().strip("'").strip('"')
    return base64.b64decode(normalized)


def rsa_encrypt(text: str, public_key_pem: bytes) -> str:
    public_key = serialization.load_pem_public_key(public_key_pem)
    encrypted = public_key.encrypt(
        text.encode("utf-8"),
        padding.PKCS1v15(),
    )
    return base64.b64encode(encrypted).decode("ascii")


def generate_aes_seed() -> str:
    return secrets.token_urlsafe(12)


def encrypt_password(password: str, public_key_cookie: str) -> str:
    if not password:
        return ""
    if not public_key_cookie:
        LOGGER.warning("Missing jms_public_key cookie, falling back to plain password payload.")
        return password

    aes_key = generate_aes_seed()
    public_key_pem = decode_public_key_cookie(public_key_cookie)
    key_cipher = rsa_encrypt(aes_key, public_key_pem)
    password_cipher = aes_encrypt_ecb(password, aes_key)
    return f"{key_cipher}:{password_cipher}"
