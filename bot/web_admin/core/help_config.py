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
    "📱 <b>Как подключить сервис</b>\n\n"
    "<b>1.</b> Установите приложение <b>Happ</b> — выберите магазин ниже.\n"
    "<b>2.</b> Нажмите <b>«Добавить подписку в Happ»</b> — подписка добавится автоматически.\n\n"
    "Готово ✅ Сервис заработает нажав кнопку подключиться."
)

# Кнопки инструкции. Ссылки на магазины приложений и «Добавить в Happ»
# вырезаны вместе с группой «Подключение»: они ставили клиент на телефон,
# а роутер получает подписку по SSH при активации. Экран помощи остался —
# текст и картинку оператор наполняет сам на странице «Доп возможности».
# kind: 'back' (в меню). url_key: None — ссылка не редактируется.
HELP_BUTTON_DEFS: list[dict[str, Any]] = [
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
