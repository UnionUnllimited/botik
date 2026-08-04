"""Аутентификация админки: сессии в Redis, 2FA, роли, CSRF.

Пароль проверяется Argon2, второй фактор — TOTP. Сессия живёт в Redis,
в куке лежит только подписанный идентификатор: угнанная кука без записи
в Redis бесполезна, а разлогинить можно мгновенно и централизованно.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any

import pyotp
import structlog
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip, get_session
from core.config import settings
from core.dates import utcnow
from core.enums import AdminRole
from core.models import AdminUser
from core.redis_client import RateLimiter, get_redis
from core.security import decrypt_secret, encrypt_secret, verify_password

log = structlog.get_logger("admin.auth")

SESSION_COOKIE = "rs_admin"
CSRF_FIELD = "csrf_token"

# Кто и что может. Owner видит всё, включая управление админами.
ROLE_SECTIONS: dict[AdminRole, set[str]] = {
    AdminRole.OWNER: {
        "dashboard",
        "orders",
        "clients",
        "console",
        "devices",
        "subscriptions",
        "nodes",
        "catalog",
        "promo",
        "settings",
        "audit",
        "admins",
    },
    AdminRole.ADMIN: {
        "dashboard",
        "orders",
        "clients",
        "console",
        "devices",
        "subscriptions",
        "nodes",
        "catalog",
        "promo",
        "settings",
        "audit",
    },
    AdminRole.SUPPORT: {"dashboard", "orders", "clients", "devices", "subscriptions"},
    AdminRole.LOGIST: {"dashboard", "orders", "devices"},
}


class AuthError(Exception):
    """Ошибка входа с текстом для формы."""


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """Заранее посчитанный хеш для выравнивания времени ответа."""
    global _DUMMY_HASH  # noqa: PLW0603 — считается один раз на процесс
    if _DUMMY_HASH is None:
        from core.security import hash_password

        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


@dataclass(slots=True)
class AdminSession:
    admin_id: int
    role: str
    login: str
    mfa_passed: bool
    csrf: str
    ip: str
    created_at: str
    router_port: int | None = None
    """Порт туннеля роутера, чью панель сейчас открыл администратор."""
    router_mac: str | None = None

    @property
    def role_enum(self) -> AdminRole:
        return AdminRole(self.role)

    def can(self, section: str) -> bool:
        return section in ROLE_SECTIONS.get(self.role_enum, set())


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.security.secret_key.get_secret_value(), salt="admin-session")


def _redis_key(session_id: str) -> str:
    return settings.redis.key("admin_session", session_id)


async def create_session(admin: AdminUser, *, ip: str, mfa_passed: bool) -> tuple[str, AdminSession]:
    session_id = secrets.token_urlsafe(32)
    data = AdminSession(
        admin_id=admin.id,
        role=str(admin.role),
        login=admin.login,
        mfa_passed=mfa_passed,
        csrf=secrets.token_urlsafe(24),
        ip=ip,
        created_at=utcnow().isoformat(),
    )
    await get_redis().set(
        _redis_key(session_id),
        json.dumps(asdict(data)),
        ex=settings.security.admin_session_ttl_hours * 3600,
    )
    return _serializer().dumps(session_id), data


async def load_session(request: Request) -> tuple[str, AdminSession] | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        session_id = _serializer().loads(raw)
    except BadSignature:
        log.warning("admin.session.bad_signature", ip=client_ip(request))
        return None
    stored = await get_redis().get(_redis_key(session_id))
    if stored is None:
        return None
    try:
        return session_id, AdminSession(**json.loads(stored))
    except (TypeError, ValueError):
        return None


async def update_session(session_id: str, data: AdminSession) -> None:
    await get_redis().set(
        _redis_key(session_id),
        json.dumps(asdict(data)),
        ex=settings.security.admin_session_ttl_hours * 3600,
    )


async def destroy_session(session_id: str) -> None:
    await get_redis().delete(_redis_key(session_id))


async def destroy_sessions_of(admin_id: int) -> int:
    """Разлогинить админа везде — например, после смены роли или блокировки."""
    redis = get_redis()
    pattern = settings.redis.key("admin_session", "*")
    removed = 0
    async for key in redis.scan_iter(match=pattern, count=200):
        stored = await redis.get(key)
        if not stored:
            continue
        try:
            payload = json.loads(stored)
        except ValueError:
            continue
        if payload.get("admin_id") == admin_id:
            await redis.delete(key)
            removed += 1
    return removed


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.security.admin_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.app.is_prod,
        samesite="lax",
        # Панель роутера открывается по корневым путям (/cgi-bin, /luci-static),
        # поэтому кука нужна на всём сайте, а не только на /admin.
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# --------------------------------------------------------------------- вход


async def authenticate(session: AsyncSession, *, login: str, password: str, ip: str) -> AdminUser:
    """Проверяет логин и пароль с учётом блокировки и rate-limit по IP."""
    limiter = RateLimiter()
    allowed, _ = await limiter.hit(f"admin_login:{ip}", limit=20, window_sec=300)
    if not allowed:
        log.warning("admin.login.rate_limited", ip=ip)
        raise AuthError("Слишком много попыток. Подождите пять минут.")

    admin = await session.scalar(select(AdminUser).where(AdminUser.login == login.strip().lower()))
    now = utcnow()

    if admin is None:
        # Проверяем пароль против заглушки: иначе несуществующий логин
        # отвечал бы заметно быстрее неверного пароля.
        verify_password(password, _dummy_hash())
        raise AuthError("Неверный логин или пароль")

    if not admin.is_active:
        raise AuthError("Учётная запись отключена")

    if admin.locked_until and admin.locked_until > now:
        minutes = max(int((admin.locked_until - now).total_seconds() // 60) + 1, 1)
        raise AuthError(f"Вход заблокирован ещё на {minutes} мин.")

    if not verify_password(password, admin.password_hash):
        admin.failed_attempts += 1
        if admin.failed_attempts >= settings.security.admin_login_max_attempts:
            admin.locked_until = now + dt.timedelta(minutes=settings.security.admin_lockout_minutes)
            admin.failed_attempts = 0
            log.warning("admin.login.locked", login=admin.login, ip=ip)
            raise AuthError(
                f"Слишком много неудачных попыток. Вход заблокирован на "
                f"{settings.security.admin_lockout_minutes} мин."
            )
        raise AuthError("Неверный логин или пароль")

    admin.failed_attempts = 0
    admin.locked_until = None
    admin.last_login_at = now
    admin.last_login_ip = ip
    log.info("admin.login.ok", login=admin.login, ip=ip)
    return admin


# ---------------------------------------------------------------------- 2FA


def totp_secret_for(admin: AdminUser) -> str | None:
    if not admin.totp_secret_enc:
        return None
    return decrypt_secret(admin.totp_secret_enc, aad=f"admin:{admin.id}")


def issue_totp_secret(admin: AdminUser) -> str:
    """Генерирует секрет и сразу кладёт его зашифрованным. Показывается один раз."""
    secret = pyotp.random_base32()
    admin.totp_secret_enc = encrypt_secret(secret, aad=f"admin:{admin.id}")
    admin.totp_enabled = False
    return secret


def totp_uri(admin: AdminUser, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=admin.login, issuer_name=f"{settings.app.brand} admin")


def verify_totp(secret: str, code: str) -> bool:
    """valid_window=1 — допускаем расхождение часов на один шаг (30 сек)."""
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) != 6:
        return False
    return pyotp.TOTP(secret).verify(cleaned, valid_window=1)


async def check_totp_replay(admin_id: int, code: str) -> bool:
    """Один и тот же код нельзя использовать дважды в пределах его окна."""
    key = settings.redis.key("totp_used", str(admin_id), code)
    return bool(await get_redis().set(key, "1", ex=90, nx=True))


# --------------------------------------------------------------- зависимости


class Principal:
    """Текущий администратор: запись из БД плюс данные сессии."""

    def __init__(self, admin: AdminUser, session_data: AdminSession, session_id: str) -> None:
        self.admin = admin
        self.session = session_data
        self.session_id = session_id

    @property
    def role(self) -> AdminRole:
        return self.admin.role

    def can(self, section: str) -> bool:
        return section in ROLE_SECTIONS.get(self.admin.role, set())

    def require(self, section: str) -> None:
        if not self.can(section):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")


class LoginRequired(HTTPException):
    def __init__(self, location: str = "/admin/login") -> None:
        super().__init__(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location})
        self.detail = "Требуется вход"


async def current_admin(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    loaded = await load_session(request)
    if loaded is None:
        raise LoginRequired()
    session_id, data = loaded

    admin = await session.get(AdminUser, data.admin_id)
    if admin is None or not admin.is_active:
        await destroy_session(session_id)
        raise LoginRequired()

    if not data.mfa_passed:
        raise LoginRequired("/admin/login/2fa" if admin.totp_enabled else "/admin/2fa/setup")

    if str(admin.role) != data.role:
        # Роль изменили — обновляем сессию, чтобы права применились сразу.
        data.role = str(admin.role)
        await update_session(session_id, data)

    return Principal(admin, data, session_id)


def require_section(section: str):
    """Зависимость проверки доступа к разделу."""

    async def dependency(principal: Principal = Depends(current_admin)) -> Principal:
        principal.require(section)
        return principal

    return dependency


async def verify_csrf(request: Request) -> None:
    """POST-запросы принимаются только со своим токеном из сессии."""
    loaded = await load_session(request)
    if loaded is None:
        raise LoginRequired()
    _, data = loaded
    form = await request.form()
    token = str(form.get(CSRF_FIELD, ""))
    if not token or not secrets.compare_digest(token, data.csrf):
        log.warning("admin.csrf.failed", path=request.url.path, ip=client_ip(request))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Сессия устарела")


def form_value(form: Any, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return str(value).strip() if value is not None else default
