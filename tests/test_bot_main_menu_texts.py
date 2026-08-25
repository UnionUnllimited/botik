"""Главное меню не должно падать из-за незаведённого текста.

Чужой продукт всегда ставился с готовой базой, и часть текстов в код так
и не попала. `app_conf.get()` отдаёт на них None, а дальше `.format()` или
склейка строк — и валится не текст, а всё меню: бот молча перестаёт отвечать
на `/start`, потому что исключение ловит глобальный обработчик.

Так и случилось с блоком подписки: ветка выполняется только при активной
подписке, а подписок в базе бота не было вовсе, пока мы не начали зеркалить
их туда. У первого же оплатившего клиента меню перестало открываться.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot"
MAIN = (BOT / "main.py").read_text(encoding="utf-8")
DB_HELPERS = (BOT / "db_helpers.py").read_text(encoding="utf-8")

# Ключи, которые главное меню форматирует или приклеивает к строке.
MENU_TEXT_KEYS = (
    "text_welcome_message",
    "text_subscription_info",
    "text_subscription_expired_main",
    "text_about_service",
    "text_promo_code_success",
)


class TestMenuTextsAreSeeded:
    @pytest.mark.parametrize("key", MENU_TEXT_KEYS)
    def test_key_has_a_default_in_the_database(self, key):
        """У ключа есть значение по умолчанию: на чистой базе меню откроется."""
        assert f"('{key}'," in DB_HELPERS, (
            f"{key} читается в главном меню, но никем не заводится — "
            "на свежей базе меню упадёт"
        )


class TestMenuTextsSurviveAnEmptySetting:
    """Настройку можно стереть на странице текстов — это не должно ронять меню."""

    @pytest.mark.parametrize("key", MENU_TEXT_KEYS)
    def test_read_site_has_a_fallback(self, key):
        reads = [ln for ln in MAIN.splitlines() if f"app_conf.get('{key}')" in ln]
        assert reads, f"{key} больше не читается в main.py — проверить тест"
        for line in reads:
            assert " or DEFAULT" in line, (
                f"{key} читается без запасного значения: {line.strip()!r}. "
                "Пустая настройка снова уронит главное меню."
            )

    def test_no_bare_format_on_a_setting(self):
        """`app_conf.get(...).format(...)` — это падение на первом же пустом ключе."""
        bare = re.findall(r"app_conf\.get\('[^']+'\)\.format\(", MAIN)
        assert not bare, f"нашлось {bare}: значение из базы форматируется без проверки"


class TestSubscriptionInfoPlaceholders:
    def test_default_uses_only_provided_names(self):
        """Лишнее имя в шаблоне — KeyError, то есть снова молчащее меню."""
        provided = {"status", "expiry_date", "limit_ip", "traffic", "sub_link"}
        match = re.search(
            r'DEFAULT_SUBSCRIPTION_INFO = """(.*?)"""', MAIN, re.DOTALL
        )
        assert match, "DEFAULT_SUBSCRIPTION_INFO не найден"
        used = set(re.findall(r"\{(\w+)\}", match.group(1)))
        assert used <= provided, f"в шаблоне неизвестные имена: {used - provided}"


class TestPremiumEmoji:
    """Премиум-эмодзи в текстах: `<tg-emoji emoji-id="…">🚀</tg-emoji>`.

    Telegram отдаёт их не всякому боту — только тем, кто купил дополнительный
    username на Fragment. Остальным он отказывает на всё сообщение целиком,
    и клиент остаётся без меню. Поэтому отправка идёт в два захода.
    """

    SOURCE = (Path(__file__).resolve().parents[1] / "bot/main.py").read_text(encoding="utf-8")

    def test_editor_allows_the_tag(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "bot/web_admin/templates/settings_texts.html"
        ).read_text(encoding="utf-8")
        assert "'TG-EMOJI'" in page
        assert 'data-fmt="tg-emoji"' in page, "вставлять id руками — лишний повод опечататься"

    def test_bot_strips_them_on_refusal(self):
        assert "def without_premium_emoji" in self.SOURCE
        assert "def is_premium_emoji_refusal" in self.SOURCE

    def test_fallback_keeps_the_plain_emoji(self):
        """Внутри тега стоит обычный эмодзи — после чистки текст не пустеет."""
        import re

        body = self.SOURCE[self.SOURCE.index("PREMIUM_EMOJI_RE = ") :]
        body = body[: body.index("def is_premium_emoji_refusal")]
        pattern = re.search(r'r"(<tg-emoji.+?)"', body).group(1)
        assert re.sub(pattern, r"\1", "<tg-emoji emoji-id='1'>🚀</tg-emoji>", flags=re.I) == "🚀"

    def test_menu_retries_without_them(self):
        """Отказ из-за эмодзи не должен оставлять клиента без меню."""
        for anchor in ("send_main_menu_photo", "show_main_menu"):
            assert anchor in self.SOURCE
        assert self.SOURCE.count("without_premium_emoji(") >= 3


class TestSupportKnowsTheRouter:
    """На экране поддержки есть MAC роутера — если он у клиента есть.

    По MAC оператор находит и клиента, и его подписку; без него разговор
    начинается с «а какой у вас роутер?».
    """

    SOURCE = (Path(__file__).resolve().parents[1] / "bot/main.py").read_text(encoding="utf-8")

    def test_variables_are_registered(self):
        from importlib.util import module_from_spec, spec_from_file_location

        path = Path(__file__).resolve().parents[1] / "bot/src/text_setting_vars.py"
        spec = spec_from_file_location("text_setting_vars_under_test", path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        names = {item["name"] for item in module.get_text_setting_variables("text_support")}
        assert {"user_id", "router_mac", "router_line"} <= names

    def test_empty_when_there_is_no_router(self):
        """Иначе «Роутер: {router_mac}» превратится в висящее «Роутер:»."""
        body = self.SOURCE[self.SOURCE.index("async def client_router_mac") :]
        body = body[: body.index("async def show_main_menu")]
        assert 'return ""' in body

    def test_screen_survives_a_broken_template(self):
        """Шаблон правит оператор, и незнакомая переменная в нём — вопрос
        времени. Экран поддержки открывают, когда уже что-то не работает."""
        body = self.SOURCE[self.SOURCE.index("support_text_template = ") :][:2000]
        assert "except (KeyError, ValueError, IndexError)" in body
