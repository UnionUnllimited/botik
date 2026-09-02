"""Приложение внутри Telegram: каталог, профиль, роутер, продление.

Данные берутся из тех же обработчиков, которыми живёт бот
(`api/routes/catalog_api.py`), — здесь только вход и оболочка. Дублировать
их логику нельзя: разъехавшись, экран бота и экран приложения показывали бы
клиенту разные вещи про один и тот же заказ.

Вход отличается принципиально. Каталожные ручки закрыты служебным токеном
и принимают `tg_id` параметром — так ходит бот, процесс, которому мы верим.
Из браузера так нельзя: токен уехал бы клиенту, а `tg_id` подставил бы
кто угодно. Поэтому здесь `tg_id` берётся исключительно из подписи Telegram
и подставляется в вызовы сам.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from api.routes import catalog_api
from core.config import settings
from core.services import landing as landing_service
from core.services.miniapp_auth import InitDataError, TelegramUser, parse_init_data

log = structlog.get_logger("api.miniapp")

router = APIRouter(prefix="/app", tags=["miniapp"], include_in_schema=False)

INIT_DATA_HEADER = "X-Telegram-Init-Data"


async def current_user(
    init_data: str = Header(default="", alias=INIT_DATA_HEADER),
) -> TelegramUser:
    """Кто открыл приложение. Единственный источник `tg_id` во всём модуле."""
    if not settings.miniapp.is_configured:
        # Не настроено — ручки как будто нет. Иначе выключенная возможность
        # молча отвечала бы всем, кто нашёл адрес.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    try:
        user = parse_init_data(
            init_data,
            bot_token=settings.miniapp.bot_token.get_secret_value(),
            max_age_sec=settings.miniapp.init_data_max_age_sec,
        )
    except InitDataError as exc:
        log.info("miniapp.entry_rejected", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    if not settings.miniapp.is_allowed(user.tg_id):
        # Обкатка: список закрыт, и отказ должен быть внятным — иначе первый же
        # позванный на тест решит, что приложение сломано.
        log.info("miniapp.not_in_allowlist", tg_id=user.tg_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Приложение пока открыто не всем.",
        )
    return user


PAGE_FILE = Path(__file__).resolve().parents[1] / "static" / "miniapp" / "index.html"
"""Оболочка лежит файлом рядом со стилями и скриптом, а не строкой в модуле.

Тридцать килобайт разметки внутри Python — это ни подсветки, ни поиска, ни
внятного `git diff`, зато обязательное экранирование фигурных скобок, на
котором ломается любой объект в JavaScript.
"""


@router.get("")
@router.get("/")
async def app_page() -> Response:
    """Оболочка приложения. Пускаем всех: данных здесь нет.

    Проверять вход на странице бессмысленно — она статическая, а подпись
    появляется только в браузере Telegram. Всё, что стоит денег и знает про
    клиента, лежит за `/app/api/*`, и там вход обязателен.
    """
    if not settings.miniapp.is_configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return FileResponse(PAGE_FILE, media_type="text/html; charset=utf-8")


@router.get("/api/home")
async def home(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Профиль: кто это, что с подпиской, есть ли роутер, последние заказы.

    Состояние подписки берём у ручки продления, а не считаем заново: она уже
    отвечает и «есть ли клиент», и сроком, и делает это ровно так, как экран
    продления. Своим запросом мы завели бы второй источник правды про одно
    и то же число.
    """
    state = await catalog_api.renew_state(tg_id=user.tg_id, session=session)
    available = await catalog_api.my_router_available(tg_id=user.tg_id, session=session)
    orders = (
        await catalog_api.list_orders(tg_id=user.tg_id, limit=5, session=session)
        if state.get("has_client")
        else {"orders": []}
    )

    return {
        "user": {
            "tg_id": user.tg_id,
            "name": user.display_name,
            "username": user.username,
        },
        "has_client": bool(state.get("has_client")),
        "subscription": state.get("subscription") or {},
        "router_available": bool(available.get("show")),
        "orders": orders.get("orders", []),
    }


@router.get("/api/catalog")
async def catalog(
    _: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Витрина. `include_hidden` передаём явно: у обработчика это значение
    из `Query(...)`, и без него в выборку уехал бы сам объект параметра."""
    return await catalog_api.list_products(session=session, include_hidden=False)


@router.get("/api/pitch")
async def pitch(
    _: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Продающая часть каталога: заголовок, выгоды, шаги, вопросы, тарифы.

    Это ровно то же, чем живёт витрина (`core/services/landing.py`), и берётся
    оттуда целиком. Написать для приложения свой текст значило бы завести
    второй набор обещаний: поправив цену или условие на сайте, оператор
    оставил бы в приложении прежние — и клиент увидел бы разные вещи в двух
    местах об одном товаре.
    """
    content = await landing_service.page_content(session)
    # Всё, что нужно только сайту (иконки, адрес бота, картинка шапки),
    # наружу не тащим: приложение уже внутри бота и открыто с телефона.
    return {
        "hero_title": content.get("hero_title", ""),
        "hero_subtitle": content.get("hero_subtitle", ""),
        "products": content.get("products", []),
        "plans": content.get("plans", []),
        "steps": content.get("steps", []),
        "features": content.get("features", []),
        "faq": content.get("faq", []),
        "support_contact": content.get("support_contact", ""),
    }


@router.get("/api/plans")
async def plans(
    _: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Сроки подписки для экрана покупки.

    Срок выбирается вместе с роутером и входит в стоимость заказа — так это
    описано на витрине и так считает `orders/quote`. Без выбора заказ уходил
    бы с одним роутером, и клиент получил бы устройство без подписки.
    """
    return await catalog_api.list_plans(session=session, include_hidden=False)


@router.get("/api/router")
async def my_router(
    device_id: int = 0,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Экран роутера — тот же, что в боте, вплоть до срока из панели."""
    return await catalog_api.my_router(
        tg_id=user.tg_id, device_id=device_id, session=session
    )


@router.get("/api/renew")
async def renew_state(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Сроки и цены продления."""
    return await catalog_api.renew_state(tg_id=user.tg_id, session=session)


@router.post("/api/renew")
async def renew_start(
    payload: dict,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_transaction),
) -> dict:
    """Ссылка на оплату продления.

    `tg_id` берётся из подписи и затирает то, что прислал браузер: иначе
    подставив чужой номер, можно было бы оплатить чужую подписку — или,
    что хуже, увидеть в ответе чужую платёжную ссылку.
    """
    return await catalog_api.renew_start(payload=_signed(payload, user), session=session)


def _signed(payload: dict | None, user: TelegramUser) -> dict:
    """Тело запроса с личностью из подписи вместо присланной браузером.

    Единственное место, где `tg_id` попадает в каталожные ручки. Возьми мы
    его из тела, подставивший чужой номер оплатил бы чужую подписку — или,
    что хуже, увидел бы в ответе чужую платёжную ссылку с адресом доставки.
    """
    safe = dict(payload or {})
    safe["tg_id"] = user.tg_id
    safe["username"] = user.username
    safe["first_name"] = user.first_name
    return safe


@router.get("/api/orders")
async def orders(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Все заказы клиента, а не пять последних: экран заказов — отдельный."""
    return await catalog_api.list_orders(tg_id=user.tg_id, limit=50, session=session)


@router.get("/api/orders/{order_id}")
async def order_card(
    order_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Карточка заказа. Чужой не покажется: обработчик сверяет владельца
    по `tg_id`, а он приходит из подписи."""
    return await catalog_api.order_card(order_id=order_id, tg_id=user.tg_id, session=session)


@router.post("/api/orders/{order_id}/pay")
async def order_pay(
    order_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_transaction),
) -> dict:
    """Свежая ссылка на оплату заказа: прежняя живёт пятнадцать минут,
    а клиент возвращается к заказу и через час."""
    return await catalog_api.order_payment_link(
        order_id=order_id, payload={"tg_id": user.tg_id}, session=session
    )


@router.get("/api/delivery")
async def delivery(
    _: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Скорости доставки и перевозчики для экрана оформления."""
    return await catalog_api.delivery_options(session=session)


@router.post("/api/validate")
async def validate(
    payload: dict,
    _: TelegramUser = Depends(current_user),
) -> dict:
    """Проверка одного поля заказа теми же правилами, что у бота.

    Повторять их в браузере нельзя: разъехавшись, они пропустят телефон,
    на который потом не дозвонится перевозчик.
    """
    return await catalog_api.validate_field(payload=payload)


@router.post("/api/orders/quote")
async def order_quote(
    payload: dict,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Суммы до оформления — экран подтверждения. Клиента не заводит."""
    return await catalog_api.quote_order(payload=_signed(payload, user), session=session)


@router.post("/api/orders")
async def order_create(
    payload: dict,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_transaction),
) -> dict:
    """Оформление заказа и ссылка на оплату."""
    return await catalog_api.create_order(payload=_signed(payload, user), session=session)


@router.post("/api/router/update")
async def router_update(
    payload: dict,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Просит роутер обновиться, не дожидаясь суточного круга.

    Номер устройства приходит из кнопки, но чужой роутер обновить нельзя:
    обработчик ищет устройство среди принадлежащих этому клиенту.
    """
    return await catalog_api.my_router_update(payload=_signed(payload, user), session=session)
