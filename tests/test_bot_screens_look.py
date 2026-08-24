"""Как выглядят экраны бота: фото постом, значки, число кнопок.

Три требования заказчика от 24 августа 2026, и каждое ломается тихо.

Картинка должна выглядеть постом, а не ссылкой с превью. Значит фото-сообщение,
а значит старый экран приходится удалять — превратить текстовое сообщение
в сообщение с фото Telegram не даёт. Отсюда порядок «сначала отправить,
потом удалить»: наоборот сбой оставит клиента без экрана.

Экраны не должны нагромождаться: девять кнопок подряд читаются хуже пяти,
и главная среди них теряется.
"""

from __future__ import annotations

from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "bot"
CATALOG = (BOT / "src" / "router_catalog.py").read_text(encoding="utf-8")
REGISTRY = (BOT / "button_registry.py").read_text(encoding="utf-8")


def _photo_screen() -> str:
    start = CATALOG.index("async def photo_screen")
    return CATALOG[start : CATALOG.index("\n# --- Экраны каталога", start)]


def _my_router_keyboard() -> str:
    start = CATALOG.index("def my_router_keyboard")
    return CATALOG[start : CATALOG.index("\ndef ", start + 10)]


class TestPhotoLooksLikeAPost:
    def test_catalog_card_is_sent_as_a_photo(self):
        body = CATALOG[CATALOG.index("async def cq_item") :][:900]
        assert "photo_screen" in body

    def test_text_goes_into_the_caption(self):
        assert "caption=text" in _photo_screen()

    def test_send_comes_before_delete(self):
        """Наоборот — сбой отправки оставит клиента с пустым местом."""
        body = _photo_screen()
        assert body.index("answer_photo") < body.index("message.delete")

    def test_undeletable_message_is_not_a_failure(self):
        """Сообщение старше двух суток удалить нельзя, и это не повод падать."""
        body = _photo_screen()
        window = body[body.index("message.delete") :]
        assert "except Exception" in window and "pass" in window


class TestFallbackToText:
    def test_long_caption_falls_back(self):
        """Подпись Telegram обрезает — уедет часть характеристик."""
        body = _photo_screen()
        assert "CAPTION_LIMIT" in body
        assert "return False" in body

    def test_broken_photo_falls_back(self):
        body = _photo_screen()
        window = body[body.index("answer_photo") :]
        assert "except Exception" in window
        assert "return False" in window

    def test_card_still_has_the_text_path(self):
        """Товар без фото и слишком длинная карточка рисуются как раньше."""
        body = CATALOG[CATALOG.index("async def cq_item") :][:900]
        assert "if not shown" in body
        assert "card_preview" in body


class TestGlyphsBeforeLabels:
    """Значок перед подписью — просьба заказчика: голый текст читается
    как список, а не как меню."""

    def test_main_menu_buttons_have_glyphs(self):
        for key in (
            "btn_my_router",
            "btn_catalog",
            "btn_my_orders",
            "btn_renew_sub",
            "btn_support",
            "btn_about_service",
            "btn_referral",
        ):
            line = next(ln for ln in REGISTRY.splitlines() if f"'{key}'," in ln)
            caption = line.split("',")[-2].split("'")[-1]
            assert caption[:1] not in ("", " ") and not caption[0].isalpha(), (
                f"{key}: подпись начинается с буквы, значка нет — {caption!r}"
            )

    def test_glyphs_are_not_emoji(self):
        """Цветные эмодзи убирали намеренно: они шумят и по-разному
        выглядят на разных телефонах."""
        for line in REGISTRY.splitlines():
            if "_b('btn_" not in line:
                continue
            assert not any(ord(ch) > 0x1F000 for ch in line), f"эмодзи в реестре: {line.strip()}"


class TestMyRouterIsNotCrowded:
    def test_orders_button_left_the_screen(self):
        """Она есть в главном меню, а здесь стояла между роутером и выходом,
        не относясь ни к тому, ни к другому."""
        assert "btn_my_orders" not in _my_router_keyboard()

    def test_two_links_share_one_row(self):
        """Инструкция и админка — два внешних адреса, по отдельности они
        растягивали экран на девять кнопок."""
        body = _my_router_keyboard()
        assert "builder.row(*links)" in body

    def test_panel_link_only_with_a_router(self):
        """Без роутера админку открывать нечем — кнопка вела бы в никуда."""
        body = _my_router_keyboard()
        assert 'panel_url and data.get("router") is not None' in body


class TestClientDoesNotSeeTheHardwareModel:
    """Решение заказчика от 24 августа 2026: имя платы клиенту не показываем.

    Оно ему ничего не говорит и рекламирует чужого производителя. Свои
    роутеры он различает по номеру и MAC — их видно на экране.
    """

    def test_neutral_label_exists(self):
        assert "def router_label" in CATALOG
        assert 'f"Роутер {position}"' in CATALOG

    def test_model_is_not_rendered_anywhere(self):
        """Функция перевода кода платы убрана: пока она есть, её позовут."""
        assert "model_name(" not in CATALOG

    def test_switch_button_shows_the_number(self):
        body = _my_router_keyboard()
        assert "router_label(" in body

    def test_operator_still_sees_the_model(self):
        """В парке и карточке устройства модель нужна — там она и осталась."""
        fleet = (BOT / "web_admin" / "templates" / "routers_fleet.html").read_text(
            encoding="utf-8"
        )
        assert "item.model" in fleet
