"""Вход клиента на сайт: сессии в Redis, регистрация по почте, CSRF.

Устройство то же, что в админке (`api/admin/auth.py`): в куке лежит только
подписанный идентификатор, всё остальное — в Redis, поэтому угнанная кука без
записи в Redis бесполезна. Но сессии клиента и сотрудника раздельные во всём:
разные куки, разные ключи Redis, разная соль подписи. Сотрудник, зашедший
в админку, не должен оказаться заодно залогинен в чужой кабинет.

Учётку клиента после неудачных попыток не блокируем — в отличие от админской.
Логин админа знают несколько человек, а почта клиента написана на каждом заказе:
блокировка по попыткам превратилась бы в способ запереть человека снаружи.
Ограничение стоит на паре «IP + адрес».
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip, get_session
from core.config import settings
from core.dates import utcnow
from core.models import User
from core.redis_client import RateLimiter, get_redis
from core.security import hash_password, verify_password
from core.validators import clean_email, password_problem

log = structlog.get_logger("site.auth")

SESSION_COOKIE = "rs_client"
CSRF_FIELD = "csrf_token"

# Скрытое поле формы: человек его не видит и не заполняет, простой бот заполняет всё.
HONEYPOT_FIELD = "company"


class AuthError(Exception):
    """Ошибка входа или регистрации с текстом для формы."""


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """Хеш-заглушка, чтобы несуществующий адрес отвечал не быстрее неверного пароля."""
    global _DUMMY_HASH  # noqa: PLW0603 — считается один раз на процесс
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


@dataclass(slots=True)
class ClientSession:
    user_id: int
    email: str
    csrf: str
    created_at: str


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.security.secret_key.get_secret_value(), salt="client-session")


def _redis_key(session_id: str) -> str:
    return settings.redis.key("client_session", session_id)


def _ttl_seconds() -> int:
    return settings.security.client_session_ttl_days * 24 * 3600


async def create_session(user: User) -> tuple[str, ClientSession]:
    session_id = secrets.token_urlsafe(32)
    data = ClientSession(
        user_id=user.id,
        email=user.email or "",
        csrf=secrets.token_urlsafe(24),
        created_at=utcnow().isoformat(),
    )
    await get_redis().set(_redis_key(session_id), json.dumps(asdict(data)), ex=_ttl_seconds())
    return _serializer().dumps(session_id), data


async def load_session(request: Request) -> tuple[str, ClientSession] | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        session_id = _serializer().loads(raw)
    except BadSignature:
        log.warning("site.session.bad_signature", ip=client_ip(request))
        return None
    stored = await get_redis().get(_redis_key(session_id))
    if stored is None:
        return None
    try:
        return session_id, ClientSession(**json.loads(stored))
    except (TypeError, ValueError):
        return None


async def destroy_session(session_id: str) -> None:
    await get_redis().delete(_redis_key(session_id))


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=_ttl_seconds(),
        httponly=True,
        secure=settings.app.is_prod,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ------------------------------------------------------- регистрация и вход


async def _check_attempts(ip: str, email: str) -> None:
    limiter = RateLimiter()
    allowed, _ = await limiter.hit(
        f"client_login:{ip}:{email}",
        limit=settings.security.client_login_attempts_per_hour,
        window_sec=3600,
    )
    if not allowed:
        log.warning("site.login.rate_limited", ip=ip)
        raise AuthError("Слишком много попыток. Попробуйте через час.")


async def register(session: AsyncSession, *, email: str, password: str, ip: str) -> User:
    """Заводит клиента по почте и паролю. Адрес не подтверждается — так решено."""
    address = clean_email(email)
    if not address:
        raise AuthError("Проверьте адрес почты")
    problem = password_problem(password)
    if problem:
        raise AuthError(problem)

    await _check_attempts(ip, address)

    existing = await session.scalar(select(User).where(User.email == address))
    if existing is not None:
        # Отвечаем прямо, а не «письмо отправлено»: подтверждения почты нет,
        # и обтекаемый ответ оставил бы человека без объяснения, почему не вошёл.
        raise AuthError("Такая почта уже зарегистрирована. Войдите или смените адрес.")

    user = User(email=address, password_hash=hash_password(password), last_seen_at=utcnow())
    session.add(user)
    await session.flush()
    log.info("site.register.ok", user_id=user.id, ip=ip)
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str, ip: str) -> User:
    address = clean_email(email)
    if not address:
        raise AuthError("Неверная почта или пароль")

    await _check_attempts(ip, address)

    user = await session.scalar(select(User).where(User.email == address))
    if user is None or not user.password_hash:
        verify_password(password, _dummy_hash())
        raise AuthError("Неверная почта или пароль")

    if not verify_password(password, user.password_hash):
        raise AuthError("Неверная почта или пароль")

    if user.is_blocked:
        raise AuthError("Доступ к учётной записи закрыт. Напишите в поддержку.")

    user.last_seen_at = utcnow()
    log.info("site.login.ok", user_id=user.id, ip=ip)
    return user


# --------------------------------------------------------------- зависимости


class LoginRequired(HTTPException):
    """Редирект на вход вместо 401: клиент читает страницу, а не код ответа."""

    def __init__(self, location: str = "/login") -> None:
        super().__init__(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location})
        self.detail = "Требуется вход"


class Client:
    """Текущий клиент: запись из БД плюс данные сессии."""

    def __init__(self, user: User, session_data: ClientSession, session_id: str) -> None:
        self.user = user
        self.session = session_data
        self.session_id = session_id


async def current_client(request: Request, session: AsyncSession = Depends(get_session)) -> Client:
    loaded = await load_session(request)
    if loaded is None:
        raise LoginRequired()
    session_id, data = loaded

    user = await session.get(User, data.user_id)
    if user is None or user.is_blocked:
        await destroy_session(session_id)
        raise LoginRequired()

    return Client(user, data, session_id)


async def optional_client(request: Request, session: AsyncSession = Depends(get_session)) -> Client | None:
    """Для страниц, которые открыты всем, но здороваются с вошедшим."""
    try:
        return await current_client(request, session)
    except LoginRequired:
        return None


async def verify_csrf(request: Request) -> None:
    """POST принимается только со своим токеном из сессии."""
    loaded = await load_session(request)
    if loaded is None:
        raise LoginRequired()
    _, data = loaded
    form = await request.form()
    token = str(form.get(CSRF_FIELD, ""))
    if not token or not secrets.compare_digest(token, data.csrf):
        log.warning("site.csrf.failed", path=request.url.path, ip=client_ip(request))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Страница устарела, обновите её")


def form_value(form: Any, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return str(value).strip() if value is not None else default


def looks_like_bot(form: Any) -> bool:
    """Заполненная приманка — форму отправлял не человек."""
    return bool(form_value(form, HONEYPOT_FIELD))
