"""Помощь с роутером в боте: сайт, Wi-Fi, кто в сети, перезагрузка, отзыв.

Поток:
    [Мой роутер]
        → care_site:{id}       — «Не открывается сайт»: прислать адрес,
                                 получить ответ, что с ним, и что делать
        → care_menu:{id}       — «Wi-Fi и сеть», подменю:
            → care_wifi:{id}       — сети и пароль; care_wifi_set — сменить
            → care_devices:{id}    — кто сейчас в домашней сети
            → care_reboot:{id}     — перезагрузка с подтверждением
    [Сообщение «оцените роутер»] — ссылки `/start rate_<отзыв>_<оценка>`

Проверку, пароль и список устройств считает основное приложение — тот же
ответ получает и приложение в Telegram. Здесь только экраны и сбор ответов.
"""

from __future__ import annotations

import html

from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from button_helpers import btn
from src import shop_api
from src.router_catalog import NO_PREVIEW, edit_screen, for_client, format_text, send_renew, text

SITE_TEXTS = {
    "covered": "text_care_site_covered",
    "direct": "text_care_site_direct",
    "local": "text_care_site_local",
    "unknown": "text_care_site_unknown",
    "router_offline": "text_care_site_offline",
    "no_subscription": "text_care_site_no_sub",
    "service_off": "text_care_site_service_off",
}
"""Вердикт проверки → текст. Сам вердикт считает основное приложение: тот же
ответ получает и приложение в Telegram, а здесь только слова."""

REFUSALS = {
    "offline": "text_care_offline",
    "unreachable": "text_care_unreachable",
    "too_often": "text_care_too_often",
}

KIND_GLYPHS = {"phone": "📱", "computer": "💻", "tv": "📺", "other": "🔹"}
DEVICES_LIMIT = 30
"""Больше тридцати строк в одном сообщении не прочитает никто, а у Telegram
есть предел длины — дом с умными лампочками до него дотягивает."""


class SiteHelp(StatesGroup):
    site = State()


class WifiChange(StatesGroup):
    password = State()


class ReviewText(StatesGroup):
    text = State()


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _device_of(data: str | None) -> int:
    try:
        return int((data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _back(builder: InlineKeyboardBuilder, device_id: int) -> InlineKeyboardBuilder:
    builder.row(
        btn("btn_care_back", callback_data=f"shop_my_router:{device_id}" if device_id else "shop_my_router")
    )
    return builder


def _refusal(data: dict) -> str:
    """Текст отказа роутера: его собственный, если он объяснил, иначе наш."""
    if data.get("message"):
        # Экраны в HTML: знак «<» в ответе роутера уронил бы правку экрана.
        return _esc(data["message"])
    return text(REFUSALS.get(str(data.get("error")), "text_care_unreachable"))


# ── Не открывается сайт ──────────────────────────────────────────────────────


def site_screen(result: dict, device_id: int) -> tuple[str, InlineKeyboardMarkup]:
    verdict = str(result.get("verdict") or "")
    site = _esc(result.get("site") or result.get("value"))
    builder = InlineKeyboardBuilder()
    cb = f":{device_id}"

    # Уже передавали — не зовём передать ещё раз, а говорим, что ждём.
    # Иначе клиент жмёт «передать» на каждую проверку, и у оператора
    # копятся одинаковые просьбы.
    if result.get("requested") and verdict in ("covered", "direct", "local", "unknown"):
        builder.row(btn("btn_care_site_again", callback_data=f"care_site{cb}"))
        return format_text("text_care_site_already", site=site), _back(builder, device_id).as_markup()

    body = format_text(SITE_TEXTS.get(verdict, "text_care_site_unknown"), site=site)
    if verdict == "covered":
        builder.row(btn("btn_care_reboot", callback_data=f"care_reboot{cb}"))
        builder.row(btn("btn_care_site_still", callback_data=f"care_site_send{cb}"))
    elif verdict in ("direct", "local", "unknown"):
        builder.row(btn("btn_care_site_send", callback_data=f"care_site_send{cb}"))
    elif verdict == "no_subscription":
        builder.row(btn("btn_renew_sub", callback_data="shop_renew"))
    elif verdict == "service_off":
        builder.row(btn("btn_care_service_on", callback_data=f"care_svc_on{cb}"))
    elif verdict == "router_offline":
        builder.row(btn("btn_my_router_refresh", callback_data=f"shop_my_router:{device_id}"))
    builder.row(btn("btn_care_site_again", callback_data=f"care_site{cb}"))
    return body, _back(builder, device_id).as_markup()


# ── Wi-Fi и сеть ─────────────────────────────────────────────────────────────


def menu_keyboard(device_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_care_wifi", callback_data=f"care_wifi:{device_id}"))
    builder.row(btn("btn_care_devices", callback_data=f"care_devices:{device_id}"))
    builder.row(btn("btn_care_reboot", callback_data=f"care_reboot:{device_id}"))
    return _back(builder, device_id).as_markup()



def _group_networks(networks: list[dict]) -> list[dict]:
    """Одна домашняя сеть на двух диапазонах — одна строка, а не две
    одинаковые: «Titan · 2,4 ГГц и 5 ГГц»."""
    groups: dict[tuple, dict] = {}
    for network in networks:
        key = (network.get("ssid"), network.get("password"), bool(network.get("open")))
        group = groups.setdefault(key, {**network, "bands": []})
        if network.get("band") and network["band"] not in group["bands"]:
            group["bands"].append(network["band"])
    return list(groups.values())


def wifi_screen(networks: list[dict], device_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["<b>Wi-Fi</b>", ""]
    for group in _group_networks(networks):
        bands = " и ".join(group["bands"])
        head = f"<b>{_esc(group.get('ssid') or 'без имени')}</b>" + (f" · {bands}" if bands else "")
        lines.append(head)
        if group.get("open"):
            lines.append("без пароля")
        else:
            lines.append(f"Пароль: <tg-spoiler><code>{_esc(group.get('password'))}</code></tg-spoiler>")
        lines.append("")
    lines.append(f"<i>{text('text_care_wifi_hint')}</i>")

    builder = InlineKeyboardBuilder()
    if any(not network.get("open") for network in networks):
        builder.row(btn("btn_care_wifi_change", callback_data=f"care_wifi_set:{device_id}"))
    return "\n".join(lines), _back(builder, device_id).as_markup()


# ── Кто в сети ───────────────────────────────────────────────────────────────


def _device_line(item: dict) -> str:
    glyph = KIND_GLYPHS.get(str(item.get("kind")), KIND_GLYPHS["other"])
    name = item.get("name") or f"Без имени ··{str(item.get('mac') or '')[-5:]}"
    how = {True: " · Wi-Fi", False: " · кабель"}.get(item.get("wifi"), "") if item.get("active") else ""
    ip = f"\n      <code>{_esc(item.get('ip'))}</code>" if item.get("ip") else ""
    return f"{glyph} <b>{_esc(name)}</b>{how}{ip}"


def devices_screen(data: dict, device_id: int) -> tuple[str, InlineKeyboardMarkup]:
    found = data.get("devices") or []
    active = [item for item in found if item.get("active")]
    recent = [item for item in found if not item.get("active")]
    builder = InlineKeyboardBuilder()
    builder.row(btn("btn_my_router_refresh", callback_data=f"care_devices:{device_id}"))
    _back(builder, device_id)

    if not found:
        return f"<b>Кто в сети</b>\n\n{text('text_care_devices_empty')}", builder.as_markup()

    lines = [f"<b>Кто в сети</b> · сейчас {len(active)}", ""]
    shown = 0
    for item in active:
        if shown >= DEVICES_LIMIT:
            break
        lines.append(_device_line(item))
        shown += 1
    if recent and shown < DEVICES_LIMIT:
        lines += ["", "<i>Были недавно</i>"]
        for item in recent[: DEVICES_LIMIT - shown]:
            lines.append(_device_line(item))
    lines += ["", f"<i>{text('text_care_devices_hint')}</i>"]
    return "\n".join(lines), builder.as_markup()


# ── Отзыв ────────────────────────────────────────────────────────────────────


def _rating_payload(payload: str) -> tuple[int, int]:
    """`rate_<отзыв>_<оценка>` → (отзыв, оценка); что-то не так — нули."""
    parts = payload.split("_")
    if len(parts) != 3 or parts[0] != "rate":
        return 0, 0
    try:
        review_id, rating = int(parts[1]), int(parts[2])
    except ValueError:
        return 0, 0
    return (review_id, rating) if 1 <= rating <= 5 else (0, 0)


def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(btn("btn_back_to_main", callback_data="back_to_main")).as_markup()


async def start_rating(message: Message, state: FSMContext, payload: str) -> None:
    review_id, rating = _rating_payload(payload)
    if not review_id:
        await message.answer(text("text_review_unknown"), reply_markup=_menu_markup())
        return
    data, error = await shop_api.review_rate(message.from_user.id, review_id, rating)
    if error or not data.get("ok"):
        await message.answer(text("text_review_unknown"), reply_markup=_menu_markup())
        return
    await state.set_state(ReviewText.text)
    await state.update_data(care_review=review_id)
    await message.answer(
        text("text_review_thanks_low" if data.get("low") else "text_review_thanks_high"),
        reply_markup=InlineKeyboardBuilder().row(btn("btn_review_skip", callback_data="care_rev_skip")).as_markup(),
    )


async def handle_start_payload(message: Message, state: FSMContext, payload: str) -> bool:
    """Команды из ссылок под нашими сообщениями. True — разобрали сами,
    и главному обработчику `/start` дальше делать нечего."""
    if payload == "renew":
        await state.clear()
        await send_renew(message)
        return True
    if payload.startswith("rate_"):
        await state.clear()
        await start_rating(message, state, payload)
        return True
    return False


# ── Обработчики ──────────────────────────────────────────────────────────────


def register_router_care_handlers(dp: Dispatcher, check_user_blocked_func, send_blocked_message_func):
    async def blocked(event, is_query: bool = True) -> bool:
        if not await check_user_blocked_func(event.from_user.id):
            return False
        await send_blocked_message_func(event.from_user.id, event if is_query else None)
        return True

    # --- Сайт ---

    @dp.callback_query(F.data.startswith("care_site:"))
    async def cq_site(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        await state.set_state(SiteHelp.site)
        # Прошлый проверенный адрес забываем: «Передать» под старым экраном
        # иначе отправило бы его, а не тот, что клиент проверит сейчас.
        await state.update_data(care_device=device_id, care_site="")
        await edit_screen(
            query.message,
            text("text_care_site_ask"),
            reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup(),
        )
        await query.answer()

    @dp.message(StateFilter(SiteHelp.site), F.text, ~F.text.startswith("/"))
    async def msg_site(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            return
        device_id = int((await state.get_data()).get("care_device") or 0)
        site = message.text.strip()[:300]
        waiting = await message.answer(format_text("text_care_site_checking", site=_esc(site[:80])))
        data, error = await shop_api.site_check(message.from_user.id, device_id, site)
        if error:
            await waiting.edit_text(_esc(for_client(error)), reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())
            return
        if not data.get("ok"):
            # Не адрес — остаёмся на шаге: следующее сообщение тоже
            # читается как адрес, и человеку не надо искать кнопку заново.
            if data.get("error") == "bad_site":
                await waiting.edit_text(
                    f"{_esc(data.get('message'))}\n\n{text('text_care_site_ask')}",
                    reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup(),
                )
                return
            await waiting.edit_text(_refusal(data), reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())
            return
        await state.set_state(None)
        await state.update_data(care_device=device_id, care_site=data.get("value") or site)
        body, markup = site_screen(data, device_id)
        await waiting.edit_text(body, reply_markup=markup, link_preview_options=NO_PREVIEW)

    @dp.callback_query(F.data.startswith("care_site_send:"))
    async def cq_site_send(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        site = (await state.get_data()).get("care_site")
        if not site:
            # Бот перезапускался или клиент пришёл к старому сообщению:
            # адреса в памяти нет, и передавать нечего — спрашиваем заново.
            await state.set_state(SiteHelp.site)
            await state.update_data(care_device=device_id)
            await edit_screen(
                query.message,
                text("text_care_site_ask"),
                reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup(),
            )
            await query.answer("Пришлите адрес ещё раз.")
            return
        await query.answer("Передаём…")
        data, error = await shop_api.site_request(query.from_user.id, device_id, site)
        builder = InlineKeyboardBuilder().row(btn("btn_care_site_again", callback_data=f"care_site:{device_id}"))
        if error or not data.get("ok"):
            body = _esc(for_client(error)) if error else (_esc(data.get("message")) or _refusal(data))
        else:
            key = "text_care_site_sent" if data.get("created") else "text_care_site_already"
            body = format_text(key, site=_esc(data.get("site") or site))
            logger.info(f"[CARE] просьба о сайте {site} от {query.from_user.id}")
        await edit_screen(query.message, body, reply_markup=_back(builder, device_id).as_markup())

    @dp.callback_query(F.data.startswith("care_svc_on:"))
    async def cq_service_on(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        await query.answer("Включаем…")
        data, error = await shop_api.client_service_on(query.from_user.id, device_id)
        if error or not data.get("ok"):
            await query.message.answer(_esc(for_client(error)) if error else _refusal(data))
            return
        builder = InlineKeyboardBuilder().row(btn("btn_care_site_again", callback_data=f"care_site:{device_id}"))
        await edit_screen(query.message, text("text_care_service_on_done"), reply_markup=_back(builder, device_id).as_markup())

    # --- Wi-Fi и сеть: подменю ---

    @dp.callback_query(F.data.startswith("care_menu:"))
    async def cq_menu(query: CallbackQuery, state: FSMContext):
        """Пароль, устройства и перезагрузка — за одной кнопкой «Моего роутера»:
        тремя рядами там они растягивали экран, на котором и так тесно."""
        if await blocked(query):
            return
        await state.clear()
        device_id = _device_of(query.data)
        await edit_screen(query.message, text("text_care_menu"), reply_markup=menu_keyboard(device_id))
        await query.answer()

    # --- Перезагрузка ---

    @dp.callback_query(F.data.startswith("care_reboot:"))
    async def cq_reboot(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        builder = InlineKeyboardBuilder().row(
            btn("btn_care_reboot_confirm", callback_data=f"care_reboot_ok:{device_id}")
        )
        await edit_screen(query.message, text("text_care_reboot_confirm"), reply_markup=_back(builder, device_id).as_markup())
        await query.answer()

    @dp.callback_query(F.data.startswith("care_reboot_ok:"))
    async def cq_reboot_ok(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        # Гасим нажатие до похода к роутеру: команда идёт по SSH через
        # туннель, и Telegram держал бы кнопку «нажатой» всё это время.
        await query.answer("Отправляем…")
        data, error = await shop_api.client_reboot(query.from_user.id, device_id)
        if error:
            body = _esc(for_client(error))
        elif data.get("ok"):
            body = text("text_care_reboot_done")
        else:
            body = _refusal(data)
        await edit_screen(query.message, body, reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())

    # --- Wi-Fi ---

    async def show_wifi(query: CallbackQuery, device_id: int):
        await query.answer("Спрашиваем роутер…")
        data, error = await shop_api.router_wifi(query.from_user.id, device_id)
        if error or not data.get("ok"):
            body = _esc(for_client(error)) if error else _refusal(data)
            await edit_screen(query.message, body, reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())
            return
        body, markup = wifi_screen(data.get("networks") or [], device_id)
        await edit_screen(query.message, body, reply_markup=markup)

    @dp.callback_query(F.data.startswith("care_wifi:"))
    async def cq_wifi(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        await show_wifi(query, _device_of(query.data))

    @dp.callback_query(F.data.startswith("care_wifi_set:"))
    async def cq_wifi_set(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        await state.set_state(WifiChange.password)
        await state.update_data(care_device=device_id, care_password="")
        builder = InlineKeyboardBuilder().row(btn("btn_back", callback_data=f"care_wifi:{device_id}"))
        await edit_screen(query.message, text("text_care_wifi_ask"), reply_markup=builder.as_markup())
        await query.answer()

    @dp.message(StateFilter(WifiChange.password), F.text, ~F.text.startswith("/"))
    async def msg_wifi_password(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            return
        device_id = int((await state.get_data()).get("care_device") or 0)
        password = message.text.strip()
        back = InlineKeyboardBuilder().row(btn("btn_back", callback_data=f"care_wifi:{device_id}"))
        # Правило пароля одно — в основном приложении; здесь только спросить.
        data, error = await shop_api.router_wifi_change(message.from_user.id, device_id, password, check_only=True)
        if error or not data.get("ok"):
            await message.answer(_esc(for_client(error)) if error else _refusal(data), reply_markup=back.as_markup())
            return
        await state.update_data(care_password=password)
        builder = InlineKeyboardBuilder().row(
            btn("btn_care_wifi_confirm", callback_data=f"care_wifi_ok:{device_id}")
        ).row(btn("btn_back", callback_data=f"care_wifi:{device_id}"))
        await message.answer(
            format_text("text_care_wifi_confirm", password=_esc(password)), reply_markup=builder.as_markup()
        )

    @dp.callback_query(F.data.startswith("care_wifi_ok:"))
    async def cq_wifi_ok(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        device_id = _device_of(query.data)
        password = (await state.get_data()).get("care_password") or ""
        if not password:
            # Пароля в памяти нет (бот перезапускался) — спрашиваем заново.
            await state.set_state(WifiChange.password)
            await state.update_data(care_device=device_id, care_password="")
            builder = InlineKeyboardBuilder().row(btn("btn_back", callback_data=f"care_wifi:{device_id}"))
            await edit_screen(query.message, text("text_care_wifi_ask"), reply_markup=builder.as_markup())
            await query.answer("Пришлите новый пароль ещё раз.")
            return
        await query.answer("Меняем пароль…")
        data, error = await shop_api.router_wifi_change(query.from_user.id, device_id, password)
        if error or not data.get("ok"):
            # Пароль не забываем: «слишком часто» или «роутер молчит» —
            # повод нажать ещё раз через минуту, а не набирать заново.
            body = _esc(for_client(error)) if error else _refusal(data)
            await edit_screen(query.message, body, reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())
            return
        await state.clear()
        names = sorted({n.get("ssid") for n in data.get("networks") or [] if n.get("ssid") and not n.get("open")})
        logger.info(f"[CARE] клиент {query.from_user.id} сменил пароль Wi-Fi")
        await edit_screen(
            query.message,
            format_text("text_care_wifi_done", ssid=_esc(", ".join(names) or "—"), password=_esc(password)),
            reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup(),
        )

    # --- Кто в сети ---

    @dp.callback_query(F.data.startswith("care_devices:"))
    async def cq_devices(query: CallbackQuery, state: FSMContext):
        if await blocked(query):
            return
        await state.clear()
        device_id = _device_of(query.data)
        await query.answer("Спрашиваем роутер…")
        data, error = await shop_api.router_devices(query.from_user.id, device_id)
        if error or not data.get("ok"):
            body = _esc(for_client(error)) if error else _refusal(data)
            await edit_screen(query.message, body, reply_markup=_back(InlineKeyboardBuilder(), device_id).as_markup())
            return
        body, markup = devices_screen(data, device_id)
        await edit_screen(query.message, body, reply_markup=markup)

    # --- Отзыв ---

    @dp.message(StateFilter(ReviewText.text), F.text, ~F.text.startswith("/"))
    async def msg_review_text(message: Message, state: FSMContext):
        if await blocked(message, is_query=False):
            return
        review_id = int((await state.get_data()).get("care_review") or 0)
        await state.clear()
        if review_id:
            _, error = await shop_api.review_text(message.from_user.id, review_id, message.text[:2000])
            if error:
                logger.warning(f"[CARE] отзыв {review_id} не сохранился: {error}")
        await message.answer(text("text_review_text_done"), reply_markup=_menu_markup())

    @dp.callback_query(F.data == "care_rev_skip")
    async def cq_review_skip(query: CallbackQuery, state: FSMContext):
        await state.clear()
        await edit_screen(query.message, text("text_review_text_done"), reply_markup=_menu_markup())
        await query.answer()
