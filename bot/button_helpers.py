"""Хелпер `btn()` для сборки InlineKeyboardButton с поддержкой
кастомных стилей (`style`) и кастомных эмодзи (`icon_custom_emoji_id`),
которые читаются из таблицы `settings` (ключи `<key>__style`, `<key>__icon`).

Если для кнопки нет записей — берутся дефолты из `button_registry.py`.

Поля `style` и `icon_custom_emoji_id` — это extra-поля Pydantic-модели
`InlineKeyboardButton` (aiogram 3 пропускает их через `extra="allow"`).
Они применяются Bot API сервером, который их понимает; стандартный сервер
безопасно их игнорирует.
"""

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, WebAppInfo

from app_config import app_conf
from button_registry import (
    BUTTON_REGISTRY_MAP, ALLOWED_STYLES, ALLOWED_KINDS,
    style_meta_keys, kind_key, group_enabled_key, is_kind_aware,
    enabled_key, has_per_button_toggle,
)


def _resolve(key: str) -> tuple[str, str]:
    """Возвращает (style, icon) с учётом БД и дефолтов реестра.

    Пустые строки означают «не задано» — соответствующее поле не будет
    добавлено в InlineKeyboardButton.
    """
    reg = BUTTON_REGISTRY_MAP.get(key) or {}
    style_key, icon_key = style_meta_keys(key)
    # app_conf.get(default='') -> вернёт строку из settings, либо '' если нет.
    style = (app_conf.get(style_key, '') or '').strip().lower()
    icon = (app_conf.get(icon_key, '') or '').strip()
    if not style:
        style = (reg.get('default_style') or '').strip().lower()
    if not icon:
        icon = (reg.get('default_icon') or '').strip()
    if style not in ALLOWED_STYLES:
        style = ''
    return style, icon


def btn(
    key: str,
    *,
    text: Optional[str] = None,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    web_app: Any = None,
    login_url: Any = None,
    switch_inline_query: Optional[str] = None,
    switch_inline_query_current_chat: Optional[str] = None,
    pay: Optional[bool] = None,
    style: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> InlineKeyboardButton:
    """Собирает InlineKeyboardButton по реестру + override-параметрам.

    Поведение:
    - `text` — если передан, перебивает значение из настроек.
      Иначе берётся `app_conf.get(key, default_text)` из реестра.
    - `style`, `icon_custom_emoji_id` — если переданы явно, используются как есть
      (override). Иначе подтягиваются из настроек (`<key>__style`, `<key>__icon`)
      или из дефолтов реестра.

    Все необязательные kwargs (callback_data/url/web_app/...) прокидываются
    в InlineKeyboardButton без изменений.
    """
    reg = BUTTON_REGISTRY_MAP.get(key) or {}
    if text is None:
        text = app_conf.get(key, reg.get('default_text', key))

    if style is None or icon_custom_emoji_id is None:
        resolved_style, resolved_icon = _resolve(key)
        if style is None:
            style = resolved_style
        if icon_custom_emoji_id is None:
            icon_custom_emoji_id = resolved_icon

    kwargs: dict[str, Any] = {'text': text}
    if callback_data is not None:
        kwargs['callback_data'] = callback_data
    if url is not None:
        kwargs['url'] = url
    if web_app is not None:
        kwargs['web_app'] = web_app
    if login_url is not None:
        kwargs['login_url'] = login_url
    if switch_inline_query is not None:
        kwargs['switch_inline_query'] = switch_inline_query
    if switch_inline_query_current_chat is not None:
        kwargs['switch_inline_query_current_chat'] = switch_inline_query_current_chat
    if pay is not None:
        kwargs['pay'] = pay
    if style:
        kwargs['style'] = style
    if icon_custom_emoji_id:
        kwargs['icon_custom_emoji_id'] = icon_custom_emoji_id

    return InlineKeyboardButton(**kwargs)


def connect_btn(
    key: str,
    *,
    target_url: str,
    respect_group_toggle: bool = True,
    text: Optional[str] = None,
):
    """Сборщик кнопки группы «Подключение» (kind-aware).

    Учитывает режим открытия (`<key>__kind`: url|webapp), берёт стиль и иконку
    из настроек (как `btn()`), а также видимость кнопки.

    - `target_url` — целевая ссылка (используется и для url, и для webapp).
    - `respect_group_toggle=True` — если у группы есть тумблер
      (`group_<gid>_enabled`) и он выключен **или** у кнопки есть персональный
      тумблер (`<key>__enabled`) и он выключен, возвращает None.
      Для нотификаций (например, после revoke), где кнопка обязательна,
      передавайте `respect_group_toggle=False` — в этом случае оба тумблера
      игнорируются.
    - `text` — если передан, перебивает текст из настроек.

    Возвращает InlineKeyboardButton, либо None если кнопка скрыта.
    """
    if not is_kind_aware(key):
        # Для kind-неаффинных кнопок просто отдаём обычный btn() с url'ом
        return btn(key, url=target_url, text=text)

    reg = BUTTON_REGISTRY_MAP.get(key) or {}

    if respect_group_toggle:
        # Персональный тумблер видимости конкретной кнопки.
        if has_per_button_toggle(key):
            if str(app_conf.get(enabled_key(key), '1')) == '0':
                return None
        # Тумблер всей группы.
        gid = reg.get('group') or ''
        gk = group_enabled_key(gid)
        if gk and str(app_conf.get(gk, '1')) == '0':
            return None

    default_kind = (reg.get('default_kind') or 'url').lower()
    kind = (app_conf.get(kind_key(key), default_kind) or default_kind).strip().lower()
    if kind not in ALLOWED_KINDS:
        kind = default_kind

    if kind == 'webapp':
        return btn(key, web_app=WebAppInfo(url=target_url), text=text)
    return btn(key, url=target_url, text=text)


__all__ = ['btn', 'connect_btn']
