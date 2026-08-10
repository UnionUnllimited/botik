"""
PWA push-уведомления — низкоуровневая обёртка над pywebpush.

Хранит VAPID-ключи в settings (генерирует один раз при первом обращении).
Отправляет push-сообщения, чистит мёртвые подписки (410/404 от push-сервиса).

ВАЖНО: pywebpush — синхронная либа. Все её вызовы оборачиваем
asyncio.to_thread, чтобы не блокировать event loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

from web_admin.async_db import async_query_db, async_execute_db

logger = logging.getLogger(__name__)


# ── VAPID-ключи ───────────────────────────────────────────────────────────────

_VAPID_KEYS_CACHE: dict[str, str] | None = None


def _vapid_subject_default() -> str:
    """Дефолтный subject для VAPID. Push-сервис требует mailto: или https://."""
    return 'mailto:admin@xstore.local'


async def get_vapid_config(generate_if_missing: bool = False) -> dict[str, str] | None:
    """
    Достаёт VAPID-ключи из settings.
    Если их нет и generate_if_missing=True — генерирует и сохраняет.

    АВТОМИГРАЦИЯ: если приватный ключ сохранён в старом PEM-формате
    (содержит '-----BEGIN'), конвертируется в raw base64url (на лету,
    с пересохранением). Это нужно для pywebpush 2.x/py-vapid 1.9+,
    которые ожидают именно raw. Сами keypair'ы не меняются —
    подписки остаются валидными.

    Возвращает {'public': ..., 'private': ..., 'subject': ...} или None.
    """
    global _VAPID_KEYS_CACHE
    if _VAPID_KEYS_CACHE:
        return _VAPID_KEYS_CACHE

    rows = await async_query_db(
        "SELECT key, value FROM settings WHERE key IN "
        "('pwa_vapid_public_key', 'pwa_vapid_private_key', 'pwa_vapid_subject')",
        ()
    )
    cfg: dict[str, str] = {}
    for r in rows:
        r = dict(r)
        cfg[r['key']] = r['value'] or ''

    public  = cfg.get('pwa_vapid_public_key', '').strip()
    private = cfg.get('pwa_vapid_private_key', '').strip()
    subject = cfg.get('pwa_vapid_subject', '').strip() or _vapid_subject_default()

    # Авто-миграция PEM → raw base64url
    if private and ('BEGIN' in private and 'PRIVATE' in private):
        try:
            private = await asyncio.to_thread(_migrate_pem_to_raw, private)
            await _save_setting('pwa_vapid_private_key', private)
            logger.info("[PUSH] VAPID private key migrated PEM → raw base64url")
        except Exception as e:
            logger.error(f"[PUSH] Не удалось мигрировать PEM-ключ: {e}", exc_info=True)
            # Перегенерируем целиком — старая подписка станет невалидной, но это лучше чем не работающие пуши
            if generate_if_missing:
                public, private = await asyncio.to_thread(_generate_vapid_keypair)
                await _save_setting('pwa_vapid_public_key',  public)
                await _save_setting('pwa_vapid_private_key', private)
                # Чистим все подписки — они всё равно не сработают с новым ключом
                await async_execute_db("DELETE FROM pwa_push_subscriptions", ())
                logger.warning("[PUSH] Старые подписки удалены — нужно подписаться заново")

    if (not public or not private) and generate_if_missing:
        public, private = await asyncio.to_thread(_generate_vapid_keypair)
        await _save_setting('pwa_vapid_public_key',  public)
        await _save_setting('pwa_vapid_private_key', private)
        if not (await async_query_db(
            "SELECT 1 FROM settings WHERE key = 'pwa_vapid_subject'", (), one=True
        )):
            await _save_setting('pwa_vapid_subject', subject)

    if not public or not private:
        return None

    _VAPID_KEYS_CACHE = {'public': public, 'private': private, 'subject': subject}
    return _VAPID_KEYS_CACHE


def _invalidate_vapid_cache() -> None:
    global _VAPID_KEYS_CACHE
    _VAPID_KEYS_CACHE = None


async def _save_setting(key: str, value: str) -> None:
    await async_execute_db(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )


def _generate_vapid_keypair() -> tuple[str, str]:
    """
    Генерирует VAPID P-256 (secp256r1) public/private пару.
    Оба ключа — base64url БЕЗ паддинга (общепринятый формат для VAPID/Web Push).
      • Public  — uncompressed point X9.62 (65 байт → ~88 base64url-символов)
      • Private — raw scalar (32 байта  → ~43 base64url-символа)
    Этот формат напрямую принимают pywebpush 2.x / py-vapid 1.9+.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    priv = ec.generate_private_key(ec.SECP256R1())

    # Public — uncompressed point (X9.62), 65 байт.
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64url = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode('ascii')

    # Private — raw 32-байтовый big-endian scalar.
    priv_int = priv.private_numbers().private_value
    priv_bytes = priv_int.to_bytes(32, 'big')
    private_b64url = base64.urlsafe_b64encode(priv_bytes).rstrip(b'=').decode('ascii')

    return public_b64url, private_b64url


def _migrate_pem_to_raw(pem_str: str) -> str:
    """
    Конвертирует приватный ключ из PEM (старый формат) в raw base64url.
    Сам ECDSA-keypair при этом НЕ меняется — public-ключ и подписка устройства
    остаются валидными.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    priv = load_pem_private_key(pem_str.encode('utf-8'), password=None)
    priv_int = priv.private_numbers().private_value
    priv_bytes = priv_int.to_bytes(32, 'big')
    return base64.urlsafe_b64encode(priv_bytes).rstrip(b'=').decode('ascii')


# ── Подписки ─────────────────────────────────────────────────────────────────

async def upsert_subscription(
    admin_user_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
    events_mask: int = 1,
) -> int:
    """Создать или обновить подписку. Возвращает id."""
    now = datetime.now(timezone.utc).isoformat()
    # SQLite UPSERT
    await async_execute_db(
        """INSERT INTO pwa_push_subscriptions
            (admin_user_id, endpoint, p256dh_key, auth_key, user_agent,
             events_mask, created_at, last_seen_at, failed_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(endpoint) DO UPDATE SET
               admin_user_id = excluded.admin_user_id,
               p256dh_key    = excluded.p256dh_key,
               auth_key      = excluded.auth_key,
               user_agent    = excluded.user_agent,
               events_mask   = excluded.events_mask,
               last_seen_at  = excluded.last_seen_at,
               failed_count  = 0,
               last_error    = NULL""",
        (admin_user_id, endpoint, p256dh, auth, user_agent or '',
         int(events_mask or 1), now, now)
    )
    row = await async_query_db(
        "SELECT id FROM pwa_push_subscriptions WHERE endpoint = ?",
        (endpoint,), one=True
    )
    return int(dict(row)['id']) if row else 0


async def remove_subscription(endpoint: str) -> bool:
    """Удалить подписку по endpoint."""
    await async_execute_db(
        "DELETE FROM pwa_push_subscriptions WHERE endpoint = ?", (endpoint,)
    )
    return True


async def remove_subscription_by_id(sub_id: int, admin_user_id: str) -> bool:
    """Удалить подписку по id (только владельцем)."""
    await async_execute_db(
        "DELETE FROM pwa_push_subscriptions WHERE id = ? AND admin_user_id = ?",
        (sub_id, admin_user_id),
    )
    return True


async def list_subscriptions_for_admin(admin_user_id: str) -> list[dict]:
    rows = await async_query_db(
        "SELECT id, endpoint, user_agent, events_mask, created_at, last_seen_at, "
        "failed_count, last_error FROM pwa_push_subscriptions "
        "WHERE admin_user_id = ? ORDER BY created_at DESC",
        (admin_user_id,)
    )
    return [dict(r) for r in rows]


async def list_all_active_subscriptions(events_filter_bit: int = 1) -> list[dict]:
    """
    Все активные подписки, у которых битовая маска events_mask
    включает указанный бит (1 = payments по умолчанию).
    """
    rows = await async_query_db(
        "SELECT id, admin_user_id, endpoint, p256dh_key, auth_key, events_mask "
        "FROM pwa_push_subscriptions "
        "WHERE (events_mask & ?) = ? AND failed_count < 5",
        (events_filter_bit, events_filter_bit)
    )
    return [dict(r) for r in rows]


async def _mark_subscription_failed(sub_id: int, error: str) -> None:
    await async_execute_db(
        "UPDATE pwa_push_subscriptions SET failed_count = failed_count + 1, "
        "last_error = ?, last_seen_at = ? WHERE id = ?",
        (str(error)[:500], datetime.now(timezone.utc).isoformat(), sub_id)
    )


async def _mark_subscription_success(sub_id: int) -> None:
    await async_execute_db(
        "UPDATE pwa_push_subscriptions SET failed_count = 0, "
        "last_error = NULL, last_seen_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), sub_id)
    )


# ── Отправка push ─────────────────────────────────────────────────────────────

def _send_one_push_sync(
    sub_record: dict,
    payload_json: str,
    vapid_private_pem: str,
    vapid_subject: str,
    ttl: int = 3600,
) -> tuple[bool, int, str]:
    """
    Синхронный вызов pywebpush для одной подписки.
    Возвращает (ok, http_status, error_text).
    """
    from pywebpush import webpush, WebPushException  # noqa: WPS433

    sub_info = {
        'endpoint': sub_record['endpoint'],
        'keys': {
            'p256dh': sub_record['p256dh_key'],
            'auth':   sub_record['auth_key'],
        }
    }
    try:
        resp = webpush(
            subscription_info=sub_info,
            data=payload_json,
            vapid_private_key=vapid_private_pem,
            vapid_claims={'sub': vapid_subject},
            ttl=ttl,
            timeout=10,
        )
        status = getattr(resp, 'status_code', 201) if resp else 201
        return True, int(status), ''
    except WebPushException as e:
        status = 0
        try:
            status = int(e.response.status_code) if e.response is not None else 0
        except Exception:
            status = 0
        return False, status, str(e)
    except Exception as e:
        return False, 0, str(e)


async def send_to_admin_push(
    admin_user_id: str | None,
    payload: dict[str, Any],
    *,
    events_filter_bit: int = 1,
    ttl: int = 3600,
    only_subscription_ids: list[int] | None = None,
) -> dict[str, int]:
    """
    Шлёт push-уведомление всем активным подпискам.
    Если admin_user_id задан — только его подпискам, иначе всем.
    Если only_subscription_ids — только указанным id (для тест-пушей).

    payload: dict с полями {title, body, icon, badge, tag, data, ...}
    Возвращает статистику {'sent': N, 'failed': N, 'cleaned': N}.
    """
    cfg = await get_vapid_config(generate_if_missing=False)
    if not cfg:
        logger.warning("[PUSH] VAPID не настроен — пуш не отправлен")
        return {'sent': 0, 'failed': 0, 'cleaned': 0}

    if only_subscription_ids:
        if not only_subscription_ids:
            return {'sent': 0, 'failed': 0, 'cleaned': 0}
        ph = ','.join(['?'] * len(only_subscription_ids))
        rows = await async_query_db(
            f"SELECT id, admin_user_id, endpoint, p256dh_key, auth_key "
            f"FROM pwa_push_subscriptions WHERE id IN ({ph})",
            only_subscription_ids
        )
        subs = [dict(r) for r in rows]
    elif admin_user_id is not None:
        rows = await async_query_db(
            "SELECT id, admin_user_id, endpoint, p256dh_key, auth_key, events_mask "
            "FROM pwa_push_subscriptions "
            "WHERE admin_user_id = ? AND (events_mask & ?) = ? AND failed_count < 5",
            (admin_user_id, events_filter_bit, events_filter_bit)
        )
        subs = [dict(r) for r in rows]
    else:
        subs = await list_all_active_subscriptions(events_filter_bit)

    if not subs:
        return {'sent': 0, 'failed': 0, 'cleaned': 0}

    payload_json = json.dumps(payload, ensure_ascii=False)
    sent = 0
    failed = 0
    cleaned = 0

    # Шлём параллельно через to_thread (ограничим конкурентность)
    sem = asyncio.Semaphore(8)

    failure_samples: list[str] = []  # для подробного лога

    async def _task(s):
        nonlocal sent, failed, cleaned
        async with sem:
            ok, status, err = await asyncio.to_thread(
                _send_one_push_sync, s, payload_json, cfg['private'], cfg['subject'], ttl,
            )
            if ok:
                sent += 1
                await _mark_subscription_success(int(s['id']))
                return
            # 404/410 — подписка мертва, удаляем
            if status in (404, 410):
                await remove_subscription(s['endpoint'])
                cleaned += 1
                logger.info(
                    f"[PUSH] dead subscription removed: id={s['id']}, "
                    f"endpoint={_short_endpoint(s['endpoint'])}, status={status}"
                )
                return
            failed += 1
            await _mark_subscription_failed(int(s['id']), f"HTTP {status}: {err}")
            # Сохраняем краткий пример для агрегированного лога
            if len(failure_samples) < 3:
                failure_samples.append(
                    f"id={s['id']} status={status} {_short_endpoint(s['endpoint'])} "
                    f"err={(str(err) or '(empty)')[:200]}"
                )

    await asyncio.gather(*[_task(s) for s in subs], return_exceptions=True)

    if sent or failed or cleaned:
        if failed and failure_samples:
            logger.warning(
                f"[PUSH] sent={sent}, failed={failed}, cleaned_dead={cleaned} "
                f"(event_bit={events_filter_bit}). Failures: " + " | ".join(failure_samples)
            )
            # Подсказка для Apple-специфичной проблемы
            joined_failures = ' '.join(failure_samples).lower()
            if 'badjwttoken' in joined_failures or '"reason":"badjwttoken"' in joined_failures:
                cur_subj = cfg.get('subject', '') if cfg else ''
                logger.warning(
                    "[PUSH] HINT: Apple Push Service отверг JWT (BadJwtToken). "
                    "Текущий VAPID subject: %r. "
                    "Apple строго требует валидный mailto: с реальным доменом или https://-URL. "
                    "Поменяйте в /settings/push (поле «VAPID Subject»). "
                    ".local/.localhost-домены и пустые значения не проходят.",
                    cur_subj
                )
        else:
            logger.info(
                f"[PUSH] sent={sent}, failed={failed}, cleaned_dead={cleaned} (event_bit={events_filter_bit})"
            )
    return {'sent': sent, 'failed': failed, 'cleaned': cleaned}


def _short_endpoint(endpoint: str) -> str:
    """Короткое представление endpoint'а для логов (без длинного токена)."""
    if not endpoint:
        return '(empty)'
    try:
        from urllib.parse import urlparse
        u = urlparse(endpoint)
        host = u.hostname or '?'
        path = (u.path or '/').rstrip('/')
        # Показываем хост + первые 16 символов токена
        tail = path.rsplit('/', 1)[-1][:16]
        return f"{host}/.../{tail}…" if tail else host
    except Exception:
        return endpoint[:40] + '…' if len(endpoint) > 40 else endpoint


# ── Polling: проверка событий и отправка push ────────────────────────────────

# Биты в events_mask:
EVENT_BIT_PAYMENTS      = 1
EVENT_BIT_REGISTRATIONS = 2


async def _is_event_enabled(event_key: str) -> bool:
    """
    Проверяет глобальный тумблер события.
    event_key: 'payments' | 'registrations'
    """
    setting_key = f'pwa_event_{event_key}_enabled'
    row = await async_query_db(
        "SELECT value FROM settings WHERE key = ?", (setting_key,), one=True
    )
    if not row:
        # По умолчанию: payments=ON, registrations=OFF
        return event_key == 'payments'
    return dict(row).get('value') == '1'


async def _is_push_globally_enabled() -> bool:
    row = await async_query_db(
        "SELECT value FROM settings WHERE key = 'pwa_push_enabled'", (), one=True
    )
    return bool(row and dict(row).get('value') == '1')


async def poll_and_notify_payments(lookback_minutes: int = 60) -> dict[str, int]:
    """
    Ищет succeeded RUB-платежи, у которых pwa_notified=0 и
    created_at > now-lookback_minutes, шлёт push, помечает notified=1.

    Возвращает статистику.
    """
    # Достаём кандидатов
    rows = await async_query_db(
        f"""SELECT payment_id, telegram_id, amount, currency, metadata_json
            FROM payments
            WHERE status = 'succeeded'
              AND COALESCE(pwa_notified, 0) = 0
              AND created_at >= datetime('now', '-{int(lookback_minutes)} minutes')
            ORDER BY created_at ASC
            LIMIT 50""",
        ()
    )
    if not rows:
        return {'processed': 0, 'sent': 0, 'failed': 0, 'cleaned': 0}

    cfg = await get_vapid_config(generate_if_missing=False)
    push_enabled = await _is_push_globally_enabled()
    event_enabled = await _is_event_enabled('payments')
    can_send = bool(cfg and push_enabled and event_enabled)

    sent = failed = cleaned = 0
    processed_ids: list[str] = []

    for r in rows:
        r = dict(r)
        # Только RUB интересуют (для последовательности с /analytics и /payments)
        if (r.get('currency') or '').upper() != 'RUB':
            processed_ids.append(r['payment_id'])
            continue

        if can_send:
            payload = _build_payment_payload(r)
            stats = await send_to_admin_push(
                admin_user_id=None,
                payload=payload,
                events_filter_bit=EVENT_BIT_PAYMENTS,
                ttl=3600,
            )
            sent    += stats.get('sent', 0)
            failed  += stats.get('failed', 0)
            cleaned += stats.get('cleaned', 0)
        processed_ids.append(r['payment_id'])

    # Помечаем все обработанные платежи как notified (даже если push выключен —
    # чтобы при включении не вылетела куча старых уведомлений за прошлую неделю).
    if processed_ids:
        for i in range(0, len(processed_ids), 500):
            batch = processed_ids[i:i + 500]
            ph = ','.join(['?'] * len(batch))
            await async_execute_db(
                f"UPDATE payments SET pwa_notified = 1 WHERE payment_id IN ({ph})", batch
            )

    return {'processed': len(processed_ids), 'sent': sent, 'failed': failed, 'cleaned': cleaned}


def _build_payment_payload(payment: dict) -> dict[str, Any]:
    """
    Формирует payload с минимумом приватных данных:
    title = «💵 Новый платёж», body = тип покупки + сумма (без имени клиента).

    Иконки:
      • icon  — `notif-payment-192.png`  — большая, цветная (зелёный rounded ₽)
      • badge — `notif-payment-badge.png` — мелкая, монохром (для статус-бара Android)
    Пути отдаются относительно scope (без секретного префикса) — SW сам
    сконкатит их с SCOPE при отображении.
    """
    amount = float(payment.get('amount') or 0)
    md_j = payment.get('metadata_json')
    ptype = None
    extra_info = ''
    if md_j:
        try:
            md = json.loads(md_j)
            ptype = md.get('payment_type')
            if ptype == 'traffic_renewal':
                gb = md.get('traffic_to_add_gb')
                if gb: extra_info = f'+{gb} GB'
            elif ptype == 'device_limit_upgrade':
                ol = md.get('old_limit'); nl = md.get('new_limit')
                if ol and nl: extra_info = f'{ol} → {nl} устр.'
            else:
                days = md.get('subscription_days') or md.get('days')
                if days: extra_info = f'{days} дн.'
        except Exception:
            pass

    type_label = {
        'traffic_renewal':       'Докупка трафика',
        'device_limit_upgrade':  'Увеличение лимита',
    }.get(ptype, 'Продление подписки')

    body_parts = [type_label]
    if extra_info: body_parts.append(extra_info)
    body_parts.append(f'{int(amount)} ₽')
    body = ' · '.join(body_parts)

    return {
        'title': '\U0001F4B5 Новый платёж',  # 💵
        'body':  body,
        'icon':  'static/icons/notif-payment-192.png',
        'badge': 'static/icons/notif-payment-badge.png',
        'tag':   'payments',
        'renotify': True,
        'data': {
            'type':       'payment',
            'payment_id': payment.get('payment_id'),
            'url':        '/payments',
        },
    }
