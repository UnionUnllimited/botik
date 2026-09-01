"""
Инструменты (tools) для AI-ассистента.

AI может вызывать эти функции через OpenAI function calling.
Все функции работают ТОЛЬКО с данными того клиента, кто пишет в чат —
user_id передаётся системой, AI не может его подделать.

Безопасность:
  - Никаких записывающих операций (только GET)
  - Никаких ссылок на подписку (sub_page_link) в ответах AI —
    эти данные модель может УВИДЕТЬ внутренне через get_my_subscription,
    но в промпте явно сказано не отдавать клиенту, и фильтрация ниже
    дополнительно вычищает ссылки из tool-results.
  - Если admin_panel недоступен — функции возвращают вежливую ошибку,
    не падают.
"""

from __future__ import annotations

import json
import os as _os
from datetime import datetime, timezone
from typing import Any

import aiohttp as _aiohttp
from loguru import logger


# ============================================================
#  СПЕЦИФИКАЦИИ TOOLS ДЛЯ OpenAI (Function Calling)
# ============================================================
# Каждый tool описывается в формате OpenAI Chat Completion API.
# Все аргументы здесь — пустые объекты, потому что user_id передаётся
# системой автоматически, не моделью.

AI_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "get_my_subscription",
            "description": (
                "Получить информацию о подписке клиента: статус (активна/истекла), "
                "сколько дней осталось, дата окончания, дата активации, "
                "email если задан, заблокирован ли аккаунт. Подписка "
                "привязана к конкретному роутеру, а не к аккаунту."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_router",
            "description": (
                "Получить состояние роутера клиента: на связи он или молчит, "
                "когда последний раз выходил на связь, сколько устройств "
                "подключено к его Wi-Fi, версия прошивки, дата активации. "
                "Вызывай это когда клиент жалуется, что роутер не работает, "
                "нет интернета или не открываются сайты — прежде чем "
                "эскалировать, посмотри, на связи ли устройство."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_payments",
            "description": (
                "Получить историю платежей клиента — последние 10 транзакций. "
                "Каждый платёж содержит сумму, статус (успешный/отменён), дату, "
                "тариф (за сколько дней) и метод оплаты."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_traffic",
            "description": (
                "Получить информацию об использованном трафике клиента "
                "за текущий период подписки: сколько ГБ использовано, "
                "общий лимит трафика (или 'безлимит')."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def _bytes_to_gb(val: Any) -> float | None:
    """Байты → ГБ (float, 2 знака). None если невалидно или 0."""
    try:
        n = float(val)
        if n <= 0:
            return None
        return round(n / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return None


def _parse_dt(val: Any) -> datetime | None:
    """ISO-строка → datetime UTC. None если невалидно."""
    if not val:
        return None
    try:
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _get_payload_data(payload: dict | None) -> tuple[dict, dict, list]:
    """
    Унифицирует payload от admin_panel: возвращает (user, remnawave, payments).
    Пустые dict/list если поля нет.
    """
    if not payload or not isinstance(payload, dict):
        return {}, {}, []
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        return {}, {}, []
    user = data.get("user") or {}
    remna = data.get("remnawave") or {}
    payments = data.get("payments") or []
    if not isinstance(user, dict):
        user = {}
    if not isinstance(remna, dict):
        remna = {}
    if not isinstance(payments, list):
        payments = []
    return user, remna, payments


async def _fetch_user_payload(user_id: int) -> dict | None:
    """Получает payload клиента из админки. None при ошибке."""
    try:
        from app import admin_panel
        client = admin_panel.get_client()
        # Сбрасываем кеш чтобы AI видел свежие данные
        try:
            client.invalidate(user_id)
        except Exception:
            pass
        return await client.get_user(int(user_id))
    except Exception as e:
        logger.warning("ai_tools._fetch_user_payload({}): {}", user_id, e)
        return None


# ============================================================
#  РЕАЛИЗАЦИИ ИНСТРУМЕНТОВ
# ============================================================

async def get_my_subscription(user_id: int) -> dict:
    """Информация о подписке клиента."""
    payload = await _fetch_user_payload(user_id)
    if payload is None:
        return {"error": "Не удалось загрузить данные из админки. Подключите оператора."}

    user, _remna, _payments = _get_payload_data(payload)
    if not user:
        return {"error": "Клиент не найден в админке. Возможно ещё не зарегистрирован."}

    result: dict[str, Any] = {}

    # --- Срок подписки ---
    end_dt = _parse_dt(user.get("subscription_end_date"))
    if end_dt:
        now = datetime.now(tz=timezone.utc)
        if end_dt < now:
            days_ago = (now - end_dt).days
            result["status"] = "expired"
            result["expired_days_ago"] = days_ago
            result["subscription_end_date"] = end_dt.strftime("%d.%m.%Y %H:%M UTC")
        else:
            days_left = (end_dt - now).days
            result["status"] = "active"
            result["days_left"] = days_left
            result["subscription_end_date"] = end_dt.strftime("%d.%m.%Y %H:%M UTC")
    else:
        result["status"] = "unknown"

    # --- Регистрация ---
    reg_dt = _parse_dt(user.get("created_at"))
    if reg_dt:
        result["registered_at"] = reg_dt.strftime("%d.%m.%Y")

    # --- Лимит устройств ---
    limit_ip = user.get("limit_ip")
    if limit_ip is None or int(limit_ip or 0) == 0:
        result["device_limit"] = "безлимит"
    else:
        result["device_limit"] = int(limit_ip)

    # --- Email (если задан) ---
    if user.get("email"):
        result["email"] = str(user["email"])

    # --- Telegram username ---
    if user.get("real_username"):
        result["telegram_username"] = f"@{user['real_username']}"

    # --- Заблокирован? ---
    if user.get("is_blocked"):
        result["blocked"] = True
        result["blocked_note"] = "Подписка заблокирована"

    # ВАЖНО: sub_page_link сюда НЕ кладём — клиент получает ссылку только через оператора.
    # Это политика безопасности.
    return result


async def get_my_devices(user_id: int) -> dict:
    """Информация об устройствах."""
    payload = await _fetch_user_payload(user_id)
    if payload is None:
        return {"error": "Не удалось загрузить данные из админки."}

    user, remna, _payments = _get_payload_data(payload)
    if not user:
        return {"error": "Клиент не найден."}

    result: dict[str, Any] = {}
    limit_ip = user.get("limit_ip")
    if limit_ip is None or int(limit_ip or 0) == 0:
        result["limit"] = "безлимит"
    else:
        result["limit"] = int(limit_ip)

    # Количество подключённых устройств — у тебя в админке это
    # может приходить из разных мест. Попробуем несколько источников.
    # 1) remnawave.user_traffic.devices_count или активные сессии
    used = None
    if isinstance(remna, dict):
        traffic = remna.get("user_traffic") or {}
        if isinstance(traffic, dict):
            for key in ("active_devices_count", "devices_count", "online_count"):
                if key in traffic:
                    try:
                        used = int(traffic[key])
                        break
                    except (TypeError, ValueError):
                        pass

    if used is None:
        # 2) Если есть отдельное поле у user
        for key in ("active_devices", "devices_used"):
            if key in user:
                try:
                    used = int(user[key])
                    break
                except (TypeError, ValueError):
                    pass

    if used is not None:
        result["used"] = used
        if isinstance(result["limit"], int):
            result["free"] = max(0, result["limit"] - used)
    else:
        result["used"] = "неизвестно"
        result["note"] = "Точное количество подключённых устройств определить не удалось."

    return result


async def get_my_payments(user_id: int) -> dict:
    """Последние 10 платежей."""
    payload = await _fetch_user_payload(user_id)
    if payload is None:
        return {"error": "Не удалось загрузить данные из админки."}

    user, _remna, payments = _get_payload_data(payload)
    if not user:
        return {"error": "Клиент не найден."}

    if not payments:
        return {"payments": [], "note": "Платежей пока нет."}

    out_list = []
    for p in payments[:10]:
        if not isinstance(p, dict):
            continue
        item: dict[str, Any] = {}

        # Сумма + валюта
        amount = p.get("amount") or p.get("price")
        currency = p.get("currency") or "RUB"
        if amount is not None:
            try:
                item["amount"] = f"{float(amount):.2f} {currency}"
            except (TypeError, ValueError):
                item["amount"] = f"{amount} {currency}"

        # Статус
        status_raw = (p.get("status") or "").lower()
        status_map = {
            "success": "успешный",
            "succeeded": "успешный",
            "paid": "успешный",
            "completed": "успешный",
            "pending": "ожидание",
            "waiting": "ожидание",
            "canceled": "отменён",
            "cancelled": "отменён",
            "failed": "ошибка",
            "rejected": "отклонён",
        }
        item["status"] = status_map.get(status_raw, status_raw or "—")

        # Дата
        paid_dt = _parse_dt(p.get("created_at") or p.get("paid_at"))
        if paid_dt:
            item["date"] = paid_dt.strftime("%d.%m.%Y")

        # Тариф
        title = p.get("tariff_title")
        info = p.get("tariff_info") or {}
        if title:
            item["tariff"] = str(title)
        elif isinstance(info, dict):
            if info.get("days"):
                item["tariff"] = f"{info['days']} дн."
            elif info.get("traffic_gb"):
                item["tariff"] = f"+{info['traffic_gb']} GB"

        # Метод оплаты
        if p.get("payment_method"):
            item["method"] = str(p["payment_method"])

        out_list.append(item)

    return {"payments": out_list, "total_count": len(payments)}


async def get_my_traffic(user_id: int) -> dict:
    """Использованный трафик."""
    payload = await _fetch_user_payload(user_id)
    if payload is None:
        return {"error": "Не удалось загрузить данные из админки."}

    user, remna, _payments = _get_payload_data(payload)
    if not user:
        return {"error": "Клиент не найден."}

    result: dict[str, Any] = {}

    if isinstance(remna, dict):
        traffic = remna.get("user_traffic") or {}
        if isinstance(traffic, dict):
            # Текущий период
            used_gb = _bytes_to_gb(traffic.get("used_traffic_bytes"))
            if used_gb is not None:
                result["used_gb_current_period"] = used_gb

            # За всё время
            lifetime_gb = _bytes_to_gb(traffic.get("lifetime_used_traffic_bytes"))
            if lifetime_gb is not None:
                result["used_gb_lifetime"] = lifetime_gb

        # Лимит трафика
        limit_bytes = remna.get("traffic_limit_bytes")
        if limit_bytes:
            limit_gb = _bytes_to_gb(limit_bytes)
            if limit_gb is not None:
                result["traffic_limit_gb"] = limit_gb
            else:
                result["traffic_limit_gb"] = "безлимит"
        else:
            result["traffic_limit_gb"] = "безлимит"

    if not result:
        return {"note": "Информация о трафике недоступна для этого клиента."}

    return result


# ============================================================
#  DISPATCH: имя → функция
# ============================================================

TOOL_HANDLERS = {
    "get_my_subscription": get_my_subscription,
    "get_my_devices": get_my_devices,
    "get_my_payments": get_my_payments,
    "get_my_traffic": get_my_traffic,
}


# ============================================================
#  РОУТЕР КЛИЕНТА (парк устройств)
# ============================================================

FLEET_API_URL = _os.getenv("FLEET_API_URL", "").rstrip("/")
FLEET_API_TOKEN = _os.getenv("FLEET_API_TOKEN", "")


async def get_my_router(user_id: int) -> dict:
    """Состояние роутера клиента из парка устройств.

    Ходит в fleet API магазина, а не в старую панель: подписка там
    привязана к устройству, и «на связи или нет» знает только он.
    """
    if not FLEET_API_URL or not FLEET_API_TOKEN:
        return {"error": "Данные по роутеру сейчас недоступны."}

    url = f"{FLEET_API_URL}/routers?tg_id={int(user_id)}&per_page=5"
    headers = {"Authorization": f"Bearer {FLEET_API_TOKEN}"}
    try:
        timeout = _aiohttp.ClientTimeout(total=10)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    logger.warning("fleet: неверный токен")
                    return {"error": "Данные по роутеру сейчас недоступны."}
                if resp.status != 200:
                    return {"error": "Данные по роутеру сейчас недоступны."}
                data = await resp.json()
    except Exception as e:
        logger.warning("fleet: {}", e)
        return {"error": "Данные по роутеру сейчас недоступны."}

    items = data.get("items") or data.get("routers") or []
    if not items:
        return {
            "has_router": False,
            "note": "За клиентом не закреплён ни один роутер.",
        }

    out = []
    for it in items:
        out.append({
            "model": it.get("model") or "",
            "online": bool(it.get("online")),
            "status": it.get("status_label") or it.get("status") or "",
            "last_seen": it.get("last_seen"),
            "clients_connected": it.get("clients"),
            "firmware": it.get("fw_version") or "",
            "activated_at": it.get("activated_at"),
            "subscription_until": it.get("subscription_until"),
            "subscription_status": it.get("subscription_label")
            or it.get("subscription_status") or "",
        })
    return {"has_router": True, "routers": out}


# Регистрируем здесь, а не в словаре выше: функция объявлена ниже него,
# и прямая ссылка в литерале дала бы NameError при импорте модуля.
TOOL_HANDLERS["get_my_router"] = get_my_router


async def execute_tool(name: str, user_id: int,
                       arguments_json: str | None = None) -> str:
    """
    Выполняет вызов tool. Возвращает JSON-строку с результатом.
    arguments_json игнорируется — все наши tools не принимают аргументов
    (user_id фиксирован системой). Игнорируем то что прислала модель,
    чтобы она не могла подменить user_id.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    # Парсим аргументы для логирования (не для использования)
    if arguments_json:
        try:
            json.loads(arguments_json)  # валидация
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        result = await handler(user_id)
        # Защита: убираем потенциальные ссылки на подписку из ответов
        result_clean = _strip_sensitive_links(result)
        return json.dumps(result_clean, ensure_ascii=False)
    except Exception as e:
        logger.exception("ai_tools.execute_tool({}, {}): {}", name, user_id, e)
        return json.dumps({"error": "Внутренняя ошибка инструмента."})


def _strip_sensitive_links(obj):
    """
    Рекурсивно убирает потенциальные ссылки на подписку из результата.
    Защита на случай если кто-то добавит поле sub_page_link в payload —
    AI не получит его даже если в коде забыли отфильтровать.
    """
    SENSITIVE_KEYS = {
        "sub_page_link", "subscription_link", "sub_url", "subscription_url",
        "vless_link", "vmess_link", "subscription_token", "uuid",
    }
    if isinstance(obj, dict):
        return {
            k: _strip_sensitive_links(v)
            for k, v in obj.items()
            if k not in SENSITIVE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_sensitive_links(x) for x in obj]
    return obj
