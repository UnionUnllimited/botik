"""Криптографические примитивы проекта.

Правила хранения секретов:
  * токен подписки (`/sub/{token}`) — в БД только SHA-256, поиск идёт по хешу;
  * `device_secret` — шифруется AES-256-GCM, потому что сервер обязан знать
    исходное значение, чтобы пересчитать HMAC входящего запроса (хеш тут не подошёл бы);
  * пароль админа — Argon2id;
  * TOTP-секрет админа — AES-256-GCM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import settings

# Алфавит кодов активации: без 0/O/1/I/L — их путают при ручном вводе с коробки.
ACTIVATION_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ACTIVATION_GROUPS = 3
ACTIVATION_GROUP_LEN = 4

_password_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


# --------------------------------------------------------------------------- токены


def generate_token(nbytes: int = 32) -> str:
    """URL-safe токен (подписка, device_secret, csrf)."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Детерминированный SHA-256 hex — им ищем строку в БД."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def generate_activation_code() -> str:
    """Код с коробки в формате XXXX-XXXX-XXXX."""
    groups = [
        "".join(secrets.choice(ACTIVATION_ALPHABET) for _ in range(ACTIVATION_GROUP_LEN))
        for _ in range(ACTIVATION_GROUPS)
    ]
    return "-".join(groups)


def normalize_activation_code(raw: str) -> str:
    """Приводит пользовательский ввод к каноническому виду XXXX-XXXX-XXXX.

    Терпит пробелы, нижний регистр и отсутствие дефисов. Возвращает пустую
    строку, если код не соответствует формату — вызывающий код покажет
    клиенту понятную ошибку вместо похода в БД.
    """
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    if len(cleaned) != ACTIVATION_GROUPS * ACTIVATION_GROUP_LEN:
        return ""
    if any(ch not in ACTIVATION_ALPHABET for ch in cleaned):
        return ""
    return "-".join(
        cleaned[i : i + ACTIVATION_GROUP_LEN] for i in range(0, len(cleaned), ACTIVATION_GROUP_LEN)
    )


# --------------------------------------------------------------------------- пароли


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


# --------------------------------------------------------------------------- шифрование


class EncryptionKeyMissingError(RuntimeError):
    """SECURITY_ENCRYPTION_KEY не задан — шифровать нечем."""


@dataclass(frozen=True, slots=True)
class SecretBox:
    """AES-256-GCM с ключом из SECURITY_ENCRYPTION_KEY (base64, 32 байта)."""

    key: bytes

    @classmethod
    def from_settings(cls) -> SecretBox:
        raw = settings.security.encryption_key.get_secret_value()
        if not raw:
            raise EncryptionKeyMissingError(
                "SECURITY_ENCRYPTION_KEY не задан: сгенерируйте "
                '`python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`'
            )
        return cls(key=base64.b64decode(raw))

    def encrypt(self, plaintext: str, *, aad: str | None = None) -> str:
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(self.key)
        blob = cipher.encrypt(
            nonce,
            plaintext.encode("utf-8"),
            aad.encode("utf-8") if aad else None,
        )
        return base64.b64encode(nonce + blob).decode("ascii")

    def decrypt(self, payload: str, *, aad: str | None = None) -> str:
        raw = base64.b64decode(payload)
        nonce, blob = raw[:12], raw[12:]
        cipher = AESGCM(self.key)
        return cipher.decrypt(nonce, blob, aad.encode("utf-8") if aad else None).decode("utf-8")


_box: SecretBox | None = None


def secret_box() -> SecretBox:
    global _box  # noqa: PLW0603 — ленивый синглтон, ключ читается один раз
    if _box is None:
        _box = SecretBox.from_settings()
    return _box


def encrypt_secret(plaintext: str, *, aad: str | None = None) -> str:
    return secret_box().encrypt(plaintext, aad=aad)


def decrypt_secret(payload: str, *, aad: str | None = None) -> str:
    return secret_box().decrypt(payload, aad=aad)


# --------------------------------------------------------------------------- подпись запросов устройства


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_payload(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Канонический вид подписываемой строки: METHOD|PATH|TS|NONCE|SHA256(BODY)."""
    return "|".join((method.upper(), path, timestamp, nonce, body_digest(body)))


def compute_device_signature(
    device_secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    payload = signature_payload(method, path, timestamp, nonce, body)
    return hmac.new(
        device_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_device_signature(
    device_secret: str,
    signature: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bool:
    expected = compute_device_signature(device_secret, method, path, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature.strip().lower())


# --------------------------------------------------------------------------- MAC-адреса


def normalize_mac(raw: str) -> str:
    """`a0:b1c2-d3e4f5` -> `A0:B1:C2:D3:E4:F5`. Пустая строка, если MAC невалиден."""
    hex_only = "".join(ch for ch in raw.strip().upper() if ch in "0123456789ABCDEF")
    if len(hex_only) != 12:
        return ""
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
