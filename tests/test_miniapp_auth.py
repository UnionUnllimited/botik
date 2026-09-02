"""Вход в приложение Telegram держится на одной подписи.

Если её можно обойти, `tg_id` подставляет кто угодно и получает чужой заказ,
чужой роутер и чужую подписку. Поэтому здесь проверяется не только то, что
правильный вход проходит, но и что каждый неправильный — нет.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from core.services.miniapp_auth import InitDataError, parse_init_data

# Не секрет, а фикстура: подпись должна на чём-то считаться.
TOKEN = "123456:AAHfake-token-for-tests"


def make_init_data(*, token: str = TOKEN, auth_date: int | None = None, **overrides) -> str:
    """Собирает строку входа ровно так, как её собирает Telegram."""
    user = overrides.pop(
        "user",
        {"id": 614685408, "first_name": "Ник", "username": "union_unlimited"},
    )
    fields: dict[str, str] = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAEtest",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    fields.update({key: str(value) for key, value in overrides.items()})

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TestValidEntry:
    def test_user_is_read_from_the_signature(self):
        user = parse_init_data(make_init_data(), bot_token=TOKEN)
        assert user.tg_id == 614685408
        assert user.username == "union_unlimited"
        assert user.display_name == "Ник"

    def test_unknown_extra_fields_do_not_break_it(self):
        """Telegram добавляет поля со временем, и подпись считается по всем.

        Если бы мы считали её по списку известных нам полей, каждое новое поле
        в их клиенте выглядело бы как несошедшаяся подпись.
        """
        user = parse_init_data(
            make_init_data(chat_type="private", signature="whatever"), bot_token=TOKEN
        )
        assert user.tg_id == 614685408


class TestRejected:
    def test_tampered_user_is_rejected(self):
        """Главный случай: подменили id, чтобы открыть чужой кабинет."""
        raw = make_init_data()
        spoiled = raw.replace("614685408", "999999999")
        with pytest.raises(InitDataError):
            parse_init_data(spoiled, bot_token=TOKEN)

    def test_other_token_is_rejected(self):
        raw = make_init_data(token="999:OTHER")
        with pytest.raises(InitDataError):
            parse_init_data(raw, bot_token=TOKEN)

    def test_missing_hash_is_rejected(self):
        raw = urlencode({"auth_date": str(int(time.time())), "user": "{}"})
        with pytest.raises(InitDataError):
            parse_init_data(raw, bot_token=TOKEN)

    def test_expired_entry_is_rejected(self):
        """Перехваченная строка не должна открывать приложение вечно."""
        raw = make_init_data(auth_date=int(time.time()) - 90000)
        with pytest.raises(InitDataError):
            parse_init_data(raw, bot_token=TOKEN, max_age_sec=86400)

    def test_entry_without_user_is_rejected(self):
        raw = make_init_data(user={})
        with pytest.raises(InitDataError):
            parse_init_data(raw, bot_token=TOKEN)

    def test_empty_string_is_rejected(self):
        with pytest.raises(InitDataError):
            parse_init_data("", bot_token=TOKEN)

    def test_without_token_nothing_passes(self):
        """Не задан токен — проверить нечем, и пускать нельзя никого."""
        with pytest.raises(InitDataError):
            parse_init_data(make_init_data(), bot_token="")
