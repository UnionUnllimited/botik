"""Регистрация, вход и личный кабинет клиента."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import client_ip, get_session, get_transaction
from api.site.auth import (
    AuthError,
    Client,
    authenticate,
    clear_session_cookie,
    create_session,
    current_client,
    destroy_session,
    form_value,
    load_session,
    looks_like_bot,
    register,
    set_session_cookie,
    verify_csrf,
)
from api.site.templating import render
from core.config import settings
from core.dates import utcnow
from core.enums import SubscriptionStatus
from core.models import Plan
from core.services import activation
from core.services import orders as orders_service
from core.services import routers as routers_service
from core.services import subscriptions as subscriptions_service
from core.validators import PASSWORD_MIN_LENGTH

log = structlog.get_logger("site.account")

router = APIRouter(include_in_schema=False)


def _auth_page(
    request: Request,
    *,
    is_register: bool,
    error: str = "",
    email: str = "",
) -> HTMLResponse:
    return render(
        request,
        "auth_form.html",
        status_code=400 if error else 200,
        is_register=is_register,
        error=error,
        email=email,
        password_min=PASSWORD_MIN_LENGTH,
        action="/register" if is_register else "/login",
        title="Регистрация" if is_register else "Вход",
        subtitle=(
            "Почта и пароль — этого хватит, чтобы следить за подпиской"
            if is_register
            else "Войдите, чтобы открыть личный кабинет"
        ),
        submit="Зарегистрироваться" if is_register else "Войти",
    )


# ------------------------------------------------------------------ регистрация


@router.get("/register", response_class=HTMLResponse, response_model=None)
async def register_form(request: Request) -> HTMLResponse | RedirectResponse:
    if await load_session(request) is not None:
        return RedirectResponse("/cabinet", status_code=303)
    return _auth_page(request, is_register=True)


@router.post("/register", response_model=None)
async def register_submit(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    email = form_value(form, "email")
    password = str(form.get("password", ""))

    if looks_like_bot(form):
        # Молча уводим на вход: подсказывать роботу, чем он себя выдал, незачем.
        log.info("site.register.honeypot", ip=client_ip(request))
        return RedirectResponse("/login", status_code=303)

    try:
        user = await register(session, email=email, password=password, ip=client_ip(request))
    except AuthError as exc:
        return _auth_page(request, is_register=True, error=str(exc), email=email)

    token, _ = await create_session(user)
    response = RedirectResponse("/cabinet", status_code=303)
    set_session_cookie(response, token)
    return response


# ------------------------------------------------------------------------ вход


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(request: Request) -> HTMLResponse | RedirectResponse:
    if await load_session(request) is not None:
        return RedirectResponse("/cabinet", status_code=303)
    return _auth_page(request, is_register=False)


@router.post("/login", response_model=None)
async def login_submit(
    request: Request, session: AsyncSession = Depends(get_transaction)
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    email = form_value(form, "email")
    password = str(form.get("password", ""))

    try:
        user = await authenticate(session, email=email, password=password, ip=client_ip(request))
    except AuthError as exc:
        return _auth_page(request, is_register=False, error=str(exc), email=email)

    token, _ = await create_session(user)
    response = RedirectResponse("/cabinet", status_code=303)
    set_session_cookie(response, token)
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)], response_model=None)
async def logout(request: Request) -> RedirectResponse:
    loaded = await load_session(request)
    if loaded is not None:
        await destroy_session(loaded[0])
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


# -------------------------------------------------------------------- кабинет


async def _cabinet_page(
    request: Request,
    client: Client,
    session: AsyncSession,
    *,
    error: str = "",
    ok: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    user = client.user
    subscription = await subscriptions_service.get_current(session, user.id)
    # Тариф забираем отдельным запросом, а не через subscription.plan: ленивая
    # загрузка в async-сессии срабатывает уже при рендере шаблона и падает.
    plan = await session.get(Plan, subscription.plan_id) if subscription and subscription.plan_id else None
    device = await routers_service.active_device_of(session, user.id)
    orders = await orders_service.list_user_orders(session, user.id)

    # Активацию предлагаем и без привязанного устройства: MAC клиент видит
    # на наклейке, а привязать роутер к заказу логист мог не успеть.
    waiting = subscription is not None and subscription.status is SubscriptionStatus.PENDING
    activated = device is not None and device.activated_at is not None

    online = False
    last_seen = None
    if device is not None:
        online = device.frp_online or device.is_online(
            threshold_min=settings.subscription.heartbeat_offline_min, now=utcnow()
        )
        seen = (device.last_heartbeat_at, device.last_poll_at, device.frp_last_seen_at)
        last_seen = max((value for value in seen if value), default=None)

    return render(
        request,
        "cabinet.html",
        client,
        status_code=status_code,
        user=user,
        subscription=subscription,
        plan=plan,
        device=device,
        orders=orders,
        can_activate=waiting and not activated,
        activated=activated,
        online=online,
        last_seen=last_seen,
        error=error,
        ok=ok,
    )


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(
    request: Request,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return await _cabinet_page(request, client, session)


@router.post("/cabinet/activate", dependencies=[Depends(verify_csrf)], response_class=HTMLResponse)
async def activate_router(
    request: Request,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_transaction),
) -> HTMLResponse:
    """Активация роутера по MAC.

    Отвечаем страницей, а не редиректом: цепочка идёт до самого роутера по SSH
    и занимает до минуты, а её итог — длинный текст, который нельзя терять
    при перезагрузке. Клиент должен прочитать, что именно не получилось.
    """
    form = await request.form()
    raw_mac = form_value(form, "mac")

    try:
        device = await activation.activate(session, user=client.user, raw_mac=raw_mac)
    except activation.ActivationError as exc:
        return await _cabinet_page(request, client, session, error=str(exc), status_code=400)

    log.info("site.activation.ok", user_id=client.user.id, mac=device.mac)
    return await _cabinet_page(
        request,
        client,
        session,
        ok=f"Роутер {device.mac} активирован. Подписка запущена — доступ появится через минуту.",
    )
