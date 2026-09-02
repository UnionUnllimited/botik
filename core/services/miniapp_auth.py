"""Вход в приложение Telegram: проверка подписи `initData`.

Telegram отдаёт странице строку `initData` — те же данные, что видит бот,
подписанные HMAC на ключе, выведенном из токена бота. Проверка этой подписи
и есть весь вход: если она сошлась, `user.id` внутри строки принадлежит тому,
кто открыл приложение, и подменить его нельзя.

Отсюда главное правило этого модуля: **`tg_id` берётся только отсюда**.
Ручки каталога принимают его параметром, и приняв такой параметр из браузера,
мы отдали бы чужой заказ любому, кто поправит адрес в строке.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

import structlog

log = structlog.get_logger("services.miniapp_auth")


class InitDataError(Exception):
    """Подпись не сошлась, протухла или строка не разобралась."""


@dataclass(frozen=True, slots=True)
class TelegramUser:
    """Кто открыл приложение. Ровно то, что подписал Telegram."""

    tg_id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    language: str = ""
    is_premium: bool = False

    @property
    def display_name(self) -> str:
        full = " ".join(part for part in (self.first_name, self.last_name) if part)
        return full or self.username or f"id{self.tg_id}"


def _secret_key(bot_token: str) -> bytes:
    """Ключ подписи по документации Telegram: HMAC от токена на слове WebAppData.

    Порядок аргументов тут неочевиден и перепутать его легко: ключом служит
    строка `WebAppData`, а сообщением — токен, а не наоборот.
    """
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def parse_init_data(raw: str, *, bot_token: str, max_age_sec: int = 86400) -> TelegramUser:
    """Разбирает и проверяет `initData`. Отказ — всегда исключение.

    Никакого «ну почти сошлось»: любая осечка означает, что перед нами не тот,
    за кого себя выдаёт открывший страницу.
    """
    if not bot_token:
        raise InitDataError("Токен бота не задан: проверить подпись нечем")
    if not raw:
        raise InitDataError("Пустая строка входа")

    # `strict_parsing` обязателен: без него мусор молча превращается в пустоту,
    # и строка без подписи выглядела бы просто строкой без подписи.
    try:
        pairs = dict(parse_qsl(raw, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError("Строка входа не разобралась") from exc

    presented = pairs.pop("hash", "")
    if not presented:
        raise InitDataError("В строке входа нет подписи")

    # Подпись считается по всем оставшимся полям, отсортированным по имени.
    # Именно по оставшимся, а не по известным нам: Telegram добавляет поля со
    # временем, и список «известных» пришлось бы догонять после каждой правки
    # их клиента, а несошедшаяся подпись выглядела бы как взлом.
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    expected = hmac.new(
        _secret_key(bot_token), check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, presented):
        raise InitDataError("Подпись не сошлась")

    # Свежесть проверяем после подписи: до неё `auth_date` — просто число,
    # которое написал кто угодно.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("Непонятное время входа") from exc
    age = int(time.time()) - auth_date
    if auth_date <= 0 or age > max_age_sec:
        raise InitDataError("Вход просрочен, откройте приложение заново")

    try:
        user = json.loads(pairs.get("user", "") or "{}")
    except json.JSONDecodeError as exc:
        raise InitDataError("Данные пользователя не разобрались") from exc
    tg_id = user.get("id")
    if not isinstance(tg_id, int) or tg_id <= 0:
        # Так бывает у входа из инлайн-режима: подпись верная, а пользователя
        # в ней нет. Показывать чужие заказы в таком режиме нечему.
        raise InitDataError("Во входе нет пользователя")

    return TelegramUser(
        tg_id=tg_id,
        username=str(user.get("username") or ""),
        first_name=str(user.get("first_name") or ""),
        last_name=str(user.get("last_name") or ""),
        language=str(user.get("language_code") or ""),
        is_premium=bool(user.get("is_premium")),
    )
