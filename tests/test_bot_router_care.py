"""Помощь с роутером в боте: экраны, кнопки и ссылки из напоминаний.

Бот живёт в чужом дереве и тянет свои зависимости, поэтому модули грузятся
с заглушками (как в `test_bot_my_router_button.py`): проверяются чистые
функции — какой экран на какой ответ сервера и куда ведут кнопки.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest
from aiogram.types import InlineKeyboardButton

BOT = Path(__file__).resolve().parents[1] / "bot"


def _stubs() -> dict[str, types.ModuleType]:
    loguru = types.ModuleType("loguru")
    loguru.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    app_config = types.ModuleType("app_config")
    app_config.app_conf = types.SimpleNamespace(get=lambda key, default=None: default)
    button_helpers = types.ModuleType("button_helpers")

    def _btn(key, *, text="", callback_data=None, url=None, **_kwargs):
        return InlineKeyboardButton(text=text or key, callback_data=callback_data, url=url)

    button_helpers.btn = _btn
    return {"loguru": loguru, "app_config": app_config, "button_helpers": button_helpers}


def _load_bot(name: str):
    """Модуль бота `src.<name>` с заглушками вместо его тяжёлых зависимостей.

    После загрузки `sys.modules` возвращается как был: модуль бота живёт
    в чужом дереве под плоским именем `src`, и оставленный там, он подменил
    бы собой чужие импорты в соседних тестах."""
    import importlib

    stubs = _stubs()
    root = str(BOT)
    added = root not in sys.path
    if added:
        sys.path.insert(0, root)
    names = [
        *stubs,
        "src",
        "src.router_catalog",
        "src.router_care",
        "src.shop_api",
        "src.shop_texts",
        "src.order_topics",
    ]
    saved = {key: sys.modules.get(key) for key in names}
    sys.modules.update(stubs)
    for key in names[len(stubs) :]:
        sys.modules.pop(key, None)
    try:
        return importlib.import_module(f"src.{name}")
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        if added:
            sys.path.remove(root)


@pytest.fixture(scope="module")
def care():
    return _load_bot("router_care")


@pytest.fixture(scope="module")
def order_topics_module():
    return _load_bot("order_topics")


@pytest.fixture(scope="module")
def catalog():
    return _load_bot("router_catalog")


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


class TestSiteScreen:
    def test_covered_offers_reboot_and_escalation(self, care):
        body, markup = care.site_screen({"verdict": "covered", "site": "youtube.com"}, 7)
        assert "youtube.com" in body
        assert _callbacks(markup)[:2] == ["care_reboot:7", "care_site_send:7"]
        assert "shop_my_router:7" in _callbacks(markup)

    def test_direct_offers_to_pass_it_on(self, care):
        _, markup = care.site_screen({"verdict": "direct", "site": "example.org"}, 7)
        assert _callbacks(markup)[0] == "care_site_send:7"

    def test_expired_goes_to_renewal(self, care):
        _, markup = care.site_screen({"verdict": "no_subscription", "site": "x.com"}, 7)
        assert "shop_renew" in _callbacks(markup)
        assert "care_site_send:7" not in _callbacks(markup)

    def test_switched_off_can_be_switched_on(self, care):
        _, markup = care.site_screen({"verdict": "service_off", "site": "x.com"}, 7)
        assert _callbacks(markup)[0] == "care_svc_on:7"

    def test_already_requested_does_not_ask_again(self, care):
        body, markup = care.site_screen({"verdict": "direct", "site": "x.com", "requested": True}, 7)
        assert "care_site_send:7" not in _callbacks(markup)
        assert "уже у специалиста" in body

    def test_site_name_is_escaped(self, care):
        body, _ = care.site_screen({"verdict": "direct", "site": "<b>x</b>"}, 7)
        assert "<b>x</b>" not in body and "&lt;b&gt;" in body

    def test_every_verdict_has_a_text(self, care):
        assert set(care.SITE_TEXTS.values()) <= _text_keys()


class TestWifiScreen:
    NETWORKS: ClassVar[list[dict]] = [
        {"id": "a", "ssid": "Titan", "band": "2,4 ГГц", "password": "Dacha-2024!", "open": False},
        {"id": "b", "ssid": "Titan", "band": "5 ГГц", "password": "Dacha-2024!", "open": False},
        {"id": "c", "ssid": "Guest", "band": "2,4 ГГц", "password": "", "open": True},
    ]

    def test_one_line_per_home_network(self, care):
        body, markup = care.wifi_screen(self.NETWORKS, 7)
        assert body.count("<b>Titan</b>") == 1
        assert "2,4 ГГц и 5 ГГц" in body
        assert "<tg-spoiler><code>Dacha-2024!</code></tg-spoiler>" in body
        assert "без пароля" in body
        assert "care_wifi_set:7" in _callbacks(markup)

    def test_open_only_has_nothing_to_change(self, care):
        _, markup = care.wifi_screen([self.NETWORKS[2]], 7)
        assert "care_wifi_set:7" not in _callbacks(markup)


class TestDevicesScreen:
    def test_active_first_and_recent_below(self, care):
        data = {
            "devices": [
                {
                    "mac": "AA:01",
                    "ip": "192.168.14.2",
                    "name": "iPhone",
                    "active": True,
                    "wifi": True,
                    "kind": "phone",
                },
                {
                    "mac": "AA:BB:CC:DD:EE:02",
                    "ip": "192.168.14.3",
                    "name": "",
                    "active": False,
                    "wifi": None,
                    "kind": "other",
                },
            ]
        }
        body, markup = care.devices_screen(data, 7)
        assert "сейчас 1" in body
        assert body.index("iPhone") < body.index("Были недавно") < body.index("Без имени ··EE:02")
        assert "care_devices:7" in _callbacks(markup)

    def test_nobody(self, care):
        body, _ = care.devices_screen({"devices": []}, 7)
        assert "не видит" in body


class TestStartLinks:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("rate_12_5", (12, 5)),
            ("rate_12_0", (0, 0)),
            ("rate_12_6", (0, 0)),
            ("rate_x_5", (0, 0)),
            ("rate_1", (0, 0)),
        ],
    )
    def test_rating_payload(self, care, payload, expected):
        assert care._rating_payload(payload) == expected

    async def test_unknown_payload_falls_through_to_normal_start(self, care):
        assert await care.handle_start_payload(object(), object(), "123456") is False
        assert await care.handle_start_payload(object(), object(), "buy_5") is False


def _registry_keys() -> set[str]:
    return set(re.findall(r"_b\('(btn_[a-z_]+)'", (BOT / "button_registry.py").read_text(encoding="utf-8")))


def _text_keys() -> set[str]:
    tree = ast.parse((BOT / "src" / "shop_texts.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "CATALOG_TEXTS":
            return {key for key, _value, _hint in ast.literal_eval(node.value)}
    raise AssertionError("CATALOG_TEXTS не нашёлся")


def test_every_button_is_registered():
    """Незарегистрированная кнопка показала бы клиенту имя ключа.

    В каталоге есть кнопки со сборной подписью (модель с ценой) — их в реестр
    не вносят намеренно, поэтому оттуда смотрим только новые, «care»."""
    used = set(
        re.findall(r'btn\(\s*"(btn_[a-z_]+)"', (BOT / "src" / "router_care.py").read_text(encoding="utf-8"))
    )
    catalog = (BOT / "src" / "router_catalog.py").read_text(encoding="utf-8")
    used |= set(re.findall(r'btn\(\s*"(btn_care_[a-z_]+)"', catalog))
    assert "btn_care_site" in used
    assert used - _registry_keys() == set()


def test_every_text_exists():
    source = (BOT / "src" / "router_care.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"(text_(?:care|review)_[a-z_]+)"', source))
    assert used and used - _text_keys() == set()


def test_registry_has_no_colour_emoji():
    for line in (BOT / "button_registry.py").read_text(encoding="utf-8").splitlines():
        if "_b('btn_care" in line or "_b('btn_review" in line:
            assert not any(ord(ch) > 0x1F000 for ch in line), line


def test_start_payloads_are_checked_before_the_main_flow():
    """`/start renew` из напоминания не должен проходить капчу и меню заново."""
    source = (BOT / "main.py").read_text(encoding="utf-8-sig")
    body = source[source.index("async def handle_start") :]
    assert body.index("care_start_payload") < body.index("Реферальная система")


@pytest.fixture(scope="module")
def outbox():
    stubs = {
        "src": types.ModuleType("src"),
        "src.shop_api": types.ModuleType("src.shop_api"),
        "src.order_topics": types.ModuleType("src.order_topics"),
        "loguru": _stubs()["loguru"],
    }
    stubs["src"].__path__ = [str(BOT / "src")]
    saved = {key: sys.modules.get(key) for key in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("_outbox_links_probe", BOT / "src" / "shop_outbox.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, was in saved.items():
            if was is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = was


class TestOutboxLinks:
    def test_start_button_becomes_a_link_to_us(self, outbox):
        markup = outbox._markup([{"text": "Продлить", "start": "renew"}], "titan_bot")
        assert markup.inline_keyboard[0][0].url == "https://t.me/titan_bot?start=renew"

    def test_without_our_name_the_button_is_dropped_not_broken(self, outbox):
        assert outbox._markup([{"text": "Продлить", "start": "renew"}], "") is None

    def test_rows_group_buttons(self, outbox):
        stars = [{"text": f"{n}", "start": f"rate_1_{n}", "row": 0} for n in range(1, 6)]
        markup = outbox._markup([*stars, {"text": "Сайт", "url": "https://x"}], "titan_bot")
        assert [len(row) for row in markup.inline_keyboard] == [5, 1]

    def test_bad_payload_is_not_turned_into_a_link(self, outbox):
        assert outbox._markup([{"text": "x", "start": "a b"}], "titan_bot") is None

    def test_plain_links_still_work(self, outbox):
        markup = outbox._markup([{"text": "Трек", "url": "https://track"}])
        assert markup.inline_keyboard[0][0].url == "https://track"


class TestOperatorButtonsOnlyFromTheWorkChat:
    """Кнопки оператора (`ord:`, `rtr:`, `site:`) — только из рабочего чата.

    Telegram не подписывает данные кнопки: клиент своим MTProto-клиентом
    может прислать `site:<id>:add` на сообщение бота у себя в личке и сам
    «одобрить» свой домен в список всего парка. Номер чата у сообщения
    ставит Telegram — по нему и проверяем."""

    @pytest.fixture
    def topics(self, order_topics_module, monkeypatch):
        module = order_topics_module

        async def _settings():
            return {"ok": True, "chat_id": "-100500"}, ""

        monkeypatch.setattr(module.shop_api, "order_topics_settings", _settings)
        monkeypatch.setitem(module._work_chat, "at", 0.0)
        return module

    class _Query:
        def __init__(self, chat_id):
            self.data = "site:5:add"
            self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id))
            self.from_user = types.SimpleNamespace(id=42)
            self.answers = []

        async def answer(self, text=None, **_kwargs):
            self.answers.append(text)

    async def test_work_chat_passes(self, topics):
        assert await topics.from_work_chat(self._Query(-100500)) is True

    async def test_private_chat_is_refused(self, topics):
        query = self._Query(42)
        assert await topics.from_work_chat(query) is False
        assert query.answers and "рабочем чате" in query.answers[0]

    async def test_no_work_chat_means_no_operator_buttons(self, topics, monkeypatch):
        async def _empty():
            return {"ok": True, "chat_id": ""}, ""

        monkeypatch.setattr(topics.shop_api, "order_topics_settings", _empty)
        assert await topics.from_work_chat(self._Query(-100500)) is False

    def test_every_operator_handler_checks_first(self):
        for name, marker in (
            ("order_topics.py", "async def on_button"),
            ("router_topics.py", "async def on_button"),
            ("router_topics.py", "async def on_site_button"),
        ):
            source = (BOT / "src" / name).read_text(encoding="utf-8")
            body = source[source.index(marker) :]
            body = body[: body.index("\n\n\n")] if "\n\n\n" in body else body
            first_call = body.index("from_work_chat(query)")
            assert first_call < body.index("query.data"), f"{name}:{marker} читает кнопку до проверки чата"


class TestMyRouterStaysCompact:
    """Заказчик просил не растягивать «Мой роутер»: девять кнопок на экране
    прятали главную. Помощь добавляет туда один ряд, остальное — в подменю."""

    FULL: ClassVar[dict] = {
        "instruction_url": "http://192.168.14.1/instruction",
        "routers": [{"id": 7, "mac": "AA", "online": True, "activated": True}],
        "router": {"id": 7, "mac": "AA", "online": True, "activated": True, "active": True},
    }

    def test_help_is_one_row(self, catalog):
        markup = catalog.my_router_keyboard(self.FULL)
        buttons = [button for row in markup.inline_keyboard for button in row]
        assert len(buttons) <= 8 and len(markup.inline_keyboard) <= 6
        assert ["care_site:7", "care_menu:7"] in [
            [button.callback_data for button in row] for row in markup.inline_keyboard
        ]

    def test_silent_router_keeps_only_the_site_check(self, catalog):
        data = {**self.FULL, "router": {**self.FULL["router"], "online": False}}
        callbacks = _callbacks(catalog.my_router_keyboard(data))
        assert "care_site:7" in callbacks
        assert "care_menu:7" not in callbacks

    def test_menu_holds_the_rest(self, care):
        assert _callbacks(care.menu_keyboard(7)) == [
            "care_wifi:7",
            "care_devices:7",
            "care_reboot:7",
            "shop_my_router:7",
        ]


async def test_outbox_holds_start_buttons_until_it_knows_its_name(outbox, monkeypatch):
    """Без имени бота кнопки «открыть бота» не собрать: сообщение не уходит
    и не подтверждается, а ждёт следующего круга — звёзды оценки иначе
    пропали бы навсегда."""
    sent, acks = [], []

    async def _outbox(limit=20):
        return {
            "messages": [
                {"id": 1, "tg_id": 7, "text": "Оцените", "buttons": [{"text": "5", "start": "rate_1_5"}]},
                {
                    "id": 2,
                    "tg_id": 7,
                    "text": "Заказ отправлен",
                    "buttons": [{"text": "Трек", "url": "https://t"}],
                },
            ]
        }, ""

    async def _ack(message_id, **kwargs):
        acks.append(message_id)
        return {}, ""

    class _Bot:
        async def me(self):
            raise RuntimeError("Telegram не ответил")

        async def send_message(self, chat_id, text, **_kwargs):
            sent.append(text)

    monkeypatch.setattr(outbox.shop_api, "outbox", _outbox, raising=False)
    monkeypatch.setattr(outbox.shop_api, "outbox_ack", _ack, raising=False)
    assert await outbox.deliver_once(_Bot()) == 1
    assert sent == ["Заказ отправлен"] and acks == [2]
