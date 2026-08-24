"""Картинка над главным меню бота.

Показывается фото-сообщением: картинка сверху, текст подписью — так это
выглядит обычным постом, а не ссылкой с превью.

Цена такого вида — экран переезжает вниз чата: превратить текстовое
сообщение в сообщение с фото Telegram не даёт, и старое приходится удалять.
Отсюда два требования, каждое из которых оставляет клиента без меню, если
его нарушить: сперва отправить новое и только потом удалять старое, и уметь
откатиться на текст, когда фото не уходит.

Проверки по исходнику: `bot/main.py` тянет за собой половину бота
и без его окружения не импортируется.
"""

from __future__ import annotations

from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "bot"
MAIN = (BOT / "main.py").read_text(encoding="utf-8")


def _sender() -> str:
    """Функция, которая шлёт меню фото-сообщением."""
    start = MAIN.index("async def send_main_menu_photo")
    return MAIN[start : MAIN.index("\nasync def ", start + 10)]


def _menu_block() -> str:
    """Кусок, где главное меню отправляется или правится.

    Якорь на `previous = ...`: строка с `target_message` встречается
    в файле дважды, и первая из них — экран заблокированного клиента,
    к меню отношения не имеющий.
    """
    start = MAIN.index("previous = target_message if edit_message")
    return MAIN[start : start + 1400]


class TestPhotoMessageNotPreview:
    def test_menu_is_sent_as_a_photo(self):
        assert "bot.send_photo" in _sender()

    def test_text_goes_into_the_caption(self):
        """Подписью под фото, а не отдельным сообщением: иначе это два
        сообщения подряд, и кнопки окажутся не под картинкой."""
        assert "caption=text" in _sender()

    def test_keyboard_stays_with_the_photo(self):
        assert "reply_markup=kbd" in _sender()


class TestOldMessageDiesLast:
    def test_send_comes_before_delete(self):
        """В обратном порядке сбой отправки оставил бы клиента вообще
        без меню: старое удалено, новое не пришло."""
        body = _sender()
        assert body.index("send_photo") < body.index("previous.delete")

    def test_undeletable_message_is_not_a_failure(self):
        """Сообщение старше двух суток удалить нельзя — новое меню уже
        отправлено, и падать из-за этого не за чем."""
        body = _sender()
        window = body[body.index("previous.delete") :]
        assert "except Exception" in window
        assert "pass" in window


class TestFallbackToText:
    def test_long_caption_falls_back(self):
        """Подпись Telegram обрезает — уедет часть меню. Лучше текстом."""
        body = _sender()
        assert "CAPTION_LIMIT" in body
        assert "return False" in body

    def test_broken_photo_falls_back(self):
        """Битая ссылка не должна оставлять клиента без меню."""
        body = _sender()
        window = body[body.index("send_photo") :]
        assert "except Exception" in window
        assert "return False" in window

    def test_menu_still_has_the_text_path(self):
        block = _menu_block()
        assert "link_preview_options=preview" in block

    def test_old_flag_is_gone_from_the_menu(self):
        """`disable_web_page_preview` и `link_preview_options` вместе aiogram
        не принимает — отправка упала бы с ошибкой прямо у клиента."""
        assert "disable_web_page_preview" not in _menu_block()

    def test_every_text_path_gets_the_preview(self):
        """Правка, отправка нового при неудаче и первый показ — три пути,
        и картинка должна быть на всех, иначе она то есть, то нет."""
        assert _menu_block().count("link_preview_options=preview") == 3


class TestSettingIsOptional:
    def test_empty_setting_means_no_photo(self):
        """Пустой адрес — законное значение: картинки просто нет."""
        assert "if not url or len(text) > CAPTION_LIMIT" in _sender()

    def test_setting_is_read_with_a_default(self):
        """Незаведённая настройка не должна ронять главное меню."""
        assert "app_conf.get('main_menu_photo_url', '')" in MAIN

    def test_preview_stays_disabled_without_a_photo(self):
        start = MAIN.index("def main_menu_preview")
        body = MAIN[start : MAIN.index("async def send_main_menu_photo")]
        assert "is_disabled=True" in body
        assert body.index("is_disabled=True") < body.index("prefer_large_media")


class TestOperatorCanSetIt:
    def test_setting_is_seeded(self):
        texts = (BOT / "src" / "shop_texts.py").read_text(encoding="utf-8")
        assert '"main_menu_photo_url"' in texts

    def test_page_has_upload_and_url(self):
        page = (BOT / "web_admin" / "templates" / "catalog_settings.html").read_text(
            encoding="utf-8"
        )
        assert 'name="main_menu_photo"' in page, "поле выбора файла"
        assert 'name="main_menu_photo_url"' in page, "и адрес вручную"

    def test_form_can_carry_a_file(self):
        """Без enctype браузер отправит имя файла, а не сам файл,
        и загрузка молча ничего не сделает."""
        page = (BOT / "web_admin" / "templates" / "catalog_settings.html").read_text(
            encoding="utf-8"
        )
        assert 'enctype="multipart/form-data"' in page

    def test_route_saves_it(self):
        route = (BOT / "web_admin" / "routes" / "catalog_shop.py").read_text(encoding="utf-8")
        body = route[route.index("async def catalog_settings_save") :]
        assert "upload_banner" in body
        assert '"main_menu_photo_url"' in body

    def test_router_panel_url_became_editable_too(self):
        """Она читалась ботом, но не правилась нигде — правилась только в базе."""
        route = (BOT / "web_admin" / "routes" / "catalog_shop.py").read_text(encoding="utf-8")
        body = route[route.index("async def catalog_settings_save") :]
        assert '"router_panel_url"' in body
