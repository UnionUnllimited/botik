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


class TestOrderFunnelHasWayBack:
    """Каждый шаг оформления можно отыграть назад.

    Без этого опечатка в телефоне стоила клиенту всей воронки: отменить
    и пройти пять шагов заново — или бросить заказ, что он и делал.

    Проверка по исходнику: `router_catalog` тянет `loguru` и `aiogram`
    из окружения бота, а у тестов своё.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1] / "bot/src/router_catalog.py"
    ).read_text(encoding="utf-8")

    def test_every_step_offers_it(self):
        for step in ("name", "phone", "city", "where", "address", "promo"):
            assert f'"shop_back:{step}"' in self.SOURCE or f"shop_back:{step}" in self.SOURCE, (
                f"с шага дальше {step} вернуться некуда"
            )

    def test_handler_knows_all_steps(self):
        body = self.SOURCE[self.SOURCE.index("async def cq_step_back") :]
        body = body[: body.index("@dp.callback_query(F.data ==")]
        for step in ("name", "phone", "city", "where", "address", "promo"):
            assert f'"{step}"' in body

    def test_first_step_returns_to_the_card(self):
        """С первого шага назад — в карточку модели: отменять оформление,
        чтобы перечитать характеристики, клиент не должен."""
        body = self.SOURCE[self.SOURCE.index("async def ask_name") :]
        body = body[: body.index("async def ask_phone")]
        assert "shop_item:" in body

    def test_promo_skips_the_step_that_never_happened(self):
        """Доставки могло не быть: варианты не пришли, адрес не спрашивали.
        Возврат туда — тупик с вопросом, которого клиент не видел."""
        body = self.SOURCE[self.SOURCE.index("async def ask_promo") :]
        body = body[: body.index("async def show_confirm")]
        assert 'data.get("delivery_speed")' in body
        assert "shop_back:city" in body

    def test_steps_are_asked_in_one_place(self):
        """Экран возврата и экран первого прохода — один и тот же: два
        похожих разойдутся через месяц."""
        assert self.SOURCE.count("async def ask_step") == 1
        for name in ("ask_name", "ask_phone", "ask_city", "ask_address"):
            assert f"async def {name}" in self.SOURCE


class TestTotalIsOurPrice:
    """Под «Итого» сказано, что комиссию платёжной системы оно не включает."""

    SOURCE = (
        Path(__file__).resolve().parents[1] / "bot/src/router_catalog.py"
    ).read_text(encoding="utf-8")

    def test_note_is_next_to_the_total(self):
        body = self.SOURCE[self.SOURCE.index("def confirm_text") :]
        body = body[: body.index("def confirm_keyboard")]
        assert "text_order_total_note" in body
        assert body.index("Итого") < body.index("text_order_total_note")

    def test_note_is_editable(self):
        texts = (
            Path(__file__).resolve().parents[1] / "bot/src/shop_texts.py"
        ).read_text(encoding="utf-8")
        assert '"text_order_total_note"' in texts
        assert "комисси" in texts


class TestDeliveryWording:
    """Как отправляем — и чем пункт выдачи отличается от курьера."""

    TEXTS = (
        Path(__file__).resolve().parents[1] / "bot/src/shop_texts.py"
    ).read_text(encoding="utf-8")

    def test_speed_screen_says_we_send(self):
        """«Везём» звучит так, будто едем сами, — а отправляет перевозчик."""
        assert "Как отправляем?" in self.TEXTS

    def test_where_screen_explains_the_price(self):
        """Разницу в цене клиент должен видеть до выбора, а не из счёта."""
        block = self.TEXTS[self.TEXTS.index('"text_order_ask_where"') :][:400]
        assert "дешевле" in block and "дороже" in block

    def test_old_defaults_are_listed_for_reseed(self):
        """На сервере тексты уже в базе: без перепосева правка кода до них
        не доедет, а без нового номера отметки круг не пройдёт заново."""
        assert '"text_order_ask_speed": "🚚 Шаг 4 из 5. Как везём?"' in self.TEXTS
        assert "ui_redesign_2026_08_v2_applied" in self.TEXTS
