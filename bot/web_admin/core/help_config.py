"""Конфигурация фичи «Помощь клиенту» (инструкция по подключению).

Единый источник дефолтов и логики мерджа для:
  - web_admin/routes/api.py  → send_client_help (отправка пользователю)
  - web_admin/routes/settings.py + шаблон settings_subscription.html (редактор)

Настройки в таблице settings:
  help_photo_file_id — Telegram file_id фото-инструкции (привязан к боту)
  help_photo_local   — относительный путь сохранённого фото для превью в админке
  help_text          — HTML-текст инструкции (подпись к фото)
  help_buttons       — JSON-переопределения кнопок (text / icon / style)
"""
from __future__ import annotations

import json
from typing import Any

# Фолбэк-фото (привязано к конкретному боту; при смене токена нужно перезалить).
DEFAULT_HELP_PHOTO_ID = (
    'AgACAgIAAxkBAAEptcpqFwkF7Qs1QPnStNoTYTLSisPYpgACliFrG0xFuEhk5SOmEUQRzAEAAwIAA3kAAzsE'
)

DEFAULT_HELP_TEXT = (
    "📱 <b>Как подключить VPN</b>\n\n"
    "<b>1.</b> Установите приложение <b>Happ</b> — выберите магазин ниже.\n"
    "<b>2.</b> Нажмите <b>«Добавить подписку в Happ»</b> — подписка добавится автоматически.\n\n"
    "Готово ✅ VPN заработает нажав кнопку подключиться."
)

# Кнопки инструкции. url_key — откуда берётся ссылка (настройка в «Ссылки приложений»):
#   None        → ссылка не редактируется (генерируется/системная)
#   'app_link_*'→ ссылка из соответствующей настройки
# kind: 'link_app' (URL из настройки), 'happ' (авто-ссылка Happ), 'back' (в меню)
HELP_BUTTON_DEFS: list[dict[str, Any]] = [
    {"id": "ios",        "label": "iPhone (RU)",       "kind": "link_app", "url_key": "app_link_ios",
     "text": "iPhone · 🇷🇺",            "icon": "5370722600668382252", "style": ""},
    {"id": "ios_global", "label": "iPhone (Global)",   "kind": "link_app", "url_key": "app_link_ios_global",
     "text": "iPhone · 🇪🇺",            "icon": "5370722600668382252", "style": ""},
    {"id": "android",    "label": "Android (Play)",    "kind": "link_app", "url_key": "app_link_android",
     "text": "Android",                  "icon": "5373130604147654226", "style": ""},
    {"id": "android_apk","label": "Android (APK)",     "kind": "link_app", "url_key": "app_link_android_apk",
     "text": "Android · APK",            "icon": "5346181118884331907", "style": ""},
    {"id": "happ",       "label": "Добавить в Happ",   "kind": "happ",     "url_key": None,
     "text": "Добавить подписку в Happ", "icon": "5438569593452926476", "style": "success"},
    {"id": "back",       "label": "В главное меню",    "kind": "back",     "url_key": None,
     "text": "⬅️ В главное меню",        "icon": "",                    "style": "danger"},
]

ALLOWED_STYLES = {"", "primary", "success", "danger"}


def _parse_buttons_raw(raw: str | None) -> dict[str, dict[str, str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def get_effective_buttons(buttons_raw: str | None) -> list[dict[str, Any]]:
    """Сливает дефолты кнопок с переопределениями из settings.help_buttons.

    Возвращает список словарей с полями id/label/kind/url_key/text/icon/style.
    """
    overrides = _parse_buttons_raw(buttons_raw)
    result: list[dict[str, Any]] = []
    for d in HELP_BUTTON_DEFS:
        ov = overrides.get(d["id"], {}) if isinstance(overrides.get(d["id"]), dict) else {}
        text = (ov.get("text") or "").strip() or d["text"]
        icon = (ov.get("icon") or "").strip()
        if not icon and "icon" not in ov:
            icon = d["icon"]
        style = ov.get("style", d["style"])
        if style not in ALLOWED_STYLES:
            style = d["style"]
        result.append({
            "id": d["id"],
            "label": d["label"],
            "kind": d["kind"],
            "url_key": d["url_key"],
            "text": text,
            "icon": icon,
            "style": style,
        })
    return result


def build_buttons_json(form_getter) -> str:
    """Собирает JSON help_buttons из полей формы.

    form_getter(name) → значение поля (str) или ''.
    Ожидаемые поля: help_btn_{id}_text / _icon / _style.
    """
    out: dict[str, dict[str, str]] = {}
    for d in HELP_BUTTON_DEFS:
        bid = d["id"]
        text = (form_getter(f"help_btn_{bid}_text") or "").strip()
        icon = (form_getter(f"help_btn_{bid}_icon") or "").strip()
        icon = "".join(ch for ch in icon if ch.isdigit())
        style = (form_getter(f"help_btn_{bid}_style") or "").strip()
        if style not in ALLOWED_STYLES:
            style = ""
        out[bid] = {"text": text, "icon": icon, "style": style}
    return json.dumps(out, ensure_ascii=False)
