"""Картинка над главным меню бота.

Показывается ссылкой на изображение, а не отдельным фото-сообщением.
Причина не в красоте: главный экран правится на месте (`edit_text`),
а превратить текстовое сообщение в сообщение с фото Telegram не даёт —
пришлось бы удалять старое и слать новое, и экран прыгал бы при каждом
возврате в главное меню.

Проверки по исходнику: `bot/main.py` тянет за собой половину бота
и без его окружения не импортируется.
"""

from __future__ import annotations

from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "bot"
MAIN = (BOT / "main.py").read_text(encoding="utf-8")


def _menu_block() -> str:
    """Кусок, где главное меню отправляется или правится.

    Якорь именно на `preview = ...`: строка с `target_message` встречается
    в файле дважды, и первая из них — экран заблокированного клиента,
    к меню отношения не имеющий.
    """
    start = MAIN.index("preview = main_menu_preview()")
    return MAIN[start : start + 1200]


class TestPreviewInsteadOfPhoto:
    def test_menu_uses_link_preview(self):
        assert "link_preview_options=preview" in _menu_block()

    def test_old_flag_is_gone_from_the_menu(self):
        """`disable_web_page_preview` и `link_preview_options` вместе aiogram
        не принимает — отправка упала бы с ошибкой прямо у клиента."""
        assert "disable_web_page_preview" not in _menu_block()

    def test_every_path_gets_the_same_preview(self):
        """Правка, отправка нового при неудаче и первый показ — три пути,
        и картинка должна быть на всех, иначе она то есть, то нет."""
        assert _menu_block().count("link_preview_options=preview") == 3


class TestPreviewIsOptional:
    def _builder(self) -> str:
        start = MAIN.index("def main_menu_preview")
        return MAIN[start : MAIN.index("async def show_main_menu")]

    def test_empty_setting_disables_preview(self):
        """Пустой адрес — законное значение: картинки просто нет."""
        body = self._builder()
        assert "is_disabled=True" in body
        assert body.index("is_disabled=True") < body.index("prefer_large_media")

    def test_image_is_large_and_above_text(self):
        body = self._builder()
        assert "prefer_large_media=True" in body
        assert "show_above_text=True" in body

    def test_setting_is_read_with_a_default(self):
        """Незаведённая настройка не должна ронять главное меню."""
        assert "app_conf.get('main_menu_photo_url', '')" in self._builder()


class TestOperatorCanSetIt:
    def test_setting_is_seeded(self):
        texts = (BOT / "src" / "shop_texts.py").read_text(encoding="utf-8")
        assert '"main_menu_photo_url"' in texts

    def test_page_has_the_field(self):
        page = (BOT / "web_admin" / "templates" / "catalog_settings.html").read_text(
            encoding="utf-8"
        )
        assert 'name="main_menu_photo_url"' in page

    def test_route_saves_it(self):
        route = (BOT / "web_admin" / "routes" / "catalog_shop.py").read_text(encoding="utf-8")
        body = route[route.index("async def catalog_settings_save") :]
        assert '"main_menu_photo_url"' in body

    def test_router_panel_url_became_editable_too(self):
        """Она читалась ботом, но не правилась нигде — правилась только в базе."""
        route = (BOT / "web_admin" / "routes" / "catalog_shop.py").read_text(encoding="utf-8")
        body = route[route.index("async def catalog_settings_save") :]
        assert '"router_panel_url"' in body
