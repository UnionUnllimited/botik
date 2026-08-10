"""
PWA push — эндпоинты:

  GET  /api/pwa/push/config     — public-ключ + настройки (для подписки в браузере)
  POST /api/pwa/push/subscribe  — сохранить подписку устройства
  POST /api/pwa/push/unsubscribe — отписать устройство
  POST /api/pwa/push/test       — отправить тестовый push текущему админу
  POST /api/pwa/push/devices/<id>/delete — удалить подписку из списка
  GET  /settings/push           — страница управления

  Также:
  POST /api/pwa/push/setup      — админ-only: сгенерировать VAPID ключи
                                  (вызывается один раз при включении функции)
"""
from __future__ import annotations

import json
import logging
from quart import render_template, request, jsonify, session, redirect, url_for

from web_admin.async_db import async_query_db, async_execute_db
from web_admin.core.push_sender import (
    get_vapid_config,
    upsert_subscription,
    remove_subscription,
    remove_subscription_by_id,
    list_subscriptions_for_admin,
    send_to_admin_push,
    _invalidate_vapid_cache,
)

logger = logging.getLogger(__name__)


def _admin_session_id() -> str | None:
    """ID админа из сессии (используется как идентификатор владельца подписки)."""
    return session.get('admin_user_id')


def attach_push_routes(admin_bp):

    # ── Страница настроек ────────────────────────────────────────────────────
    async def settings_push_view():
        cfg = await get_vapid_config(generate_if_missing=False)
        configured = bool(cfg)

        # Состояние глобального тумблера
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'pwa_push_enabled'", (), one=True
        )
        push_enabled = bool(row and (dict(row).get('value') == '1'))

        # VAPID subject (mailto: или https://-URL для Apple Push Service)
        vapid_subject = (cfg or {}).get('subject') if cfg else ''

        # Подписки админа
        admin_id = _admin_session_id() or ''
        devices = await list_subscriptions_for_admin(admin_id) if admin_id else []

        # VAPID public key для frontend
        public_key = (cfg or {}).get('public') if cfg else None

        return await render_template(
            'settings_push.html',
            configured=configured,
            push_enabled=push_enabled,
            public_key=public_key,
            vapid_subject=vapid_subject,
            devices=devices,
        )

    # ── Сгенерировать VAPID ключи (только админ; разовая операция) ──────────
    async def push_setup():
        if session.get('admin_role') != 'admin':
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        try:
            cfg = await get_vapid_config(generate_if_missing=True)
            if not cfg:
                return jsonify({'ok': False, 'error': 'failed_to_generate'}), 500
            return jsonify({'ok': True, 'public_key': cfg['public']})
        except Exception as e:
            logger.exception("[PUSH] setup failed")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ── Включить/выключить функцию push (админ) ─────────────────────────────
    async def push_toggle():
        if session.get('admin_role') != 'admin':
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        body = await request.get_json(silent=True) or {}
        enable = bool(body.get('enabled'))
        await async_execute_db(
            "INSERT INTO settings (key, value) VALUES ('pwa_push_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ('1' if enable else '0',)
        )
        if enable:
            cfg = await get_vapid_config(generate_if_missing=True)
            if not cfg:
                return jsonify({'ok': False, 'error': 'failed_to_generate_keys'}), 500
        _invalidate_vapid_cache()
        return jsonify({'ok': True, 'enabled': enable})

    # ── Тумблер конкретного типа события: payments | registrations ──────────
    async def push_event_toggle():
        if session.get('admin_role') != 'admin':
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        body = await request.get_json(silent=True) or {}
        event = (body.get('event') or '').strip().lower()
        if event not in ('payments', 'registrations'):
            return jsonify({'ok': False, 'error': 'invalid_event'}), 400
        enable = bool(body.get('enabled'))
        key = f'pwa_event_{event}_enabled'
        await async_execute_db(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, '1' if enable else '0')
        )
        return jsonify({'ok': True, 'event': event, 'enabled': enable})

    # ── Обновить VAPID subject ──────────────────────────────────────────────
    # Apple Push Service строго требует реальный mailto: или https://-URL.
    # Нестандартные значения (например, mailto:admin@xstore.local) приводят к
    # 403 BadJwtToken. Этот эндпоинт даёт админу поменять subject из UI.
    async def push_subject():
        if session.get('admin_role') != 'admin':
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        body = await request.get_json(silent=True) or {}
        subject = (body.get('subject') or '').strip()
        if not subject:
            return jsonify({'ok': False, 'error': 'empty_subject'}), 400
        if not (subject.startswith('mailto:') or subject.startswith('https://')):
            return jsonify({
                'ok': False,
                'error': 'invalid_format',
                'hint': 'subject должен начинаться с mailto: или https://',
            }), 400
        # Apple-специфичные ограничения: запрещаем .local TLD и пустой email
        lower_subject = subject.lower()
        if subject.startswith('mailto:'):
            email_part = subject[7:].strip()
            if '@' not in email_part or email_part.startswith('@') or email_part.endswith('@'):
                return jsonify({'ok': False, 'error': 'invalid_email'}), 400
            domain = email_part.split('@', 1)[1]
            if domain.endswith('.local') or domain.endswith('.localhost') or domain == 'localhost':
                return jsonify({
                    'ok': False,
                    'error': 'local_domain_not_allowed',
                    'hint': 'Apple Push Service отвергает .local/.localhost домены — '
                            'используйте реальный email или https://-URL.',
                }), 400
        await async_execute_db(
            "INSERT INTO settings (key, value) VALUES ('pwa_vapid_subject', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (subject,)
        )
        # Сбрасываем счётчик неудач — после смены subject шансы отправки растут
        await async_execute_db(
            "UPDATE pwa_push_subscriptions SET failed_count = 0, last_error = NULL "
            "WHERE failed_count > 0", ()
        )
        _invalidate_vapid_cache()
        return jsonify({'ok': True, 'subject': subject})

    # ── GET /api/pwa/push/config ────────────────────────────────────────────
    async def push_config():
        if not _admin_session_id():
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        row = await async_query_db(
            "SELECT value FROM settings WHERE key = 'pwa_push_enabled'", (), one=True
        )
        enabled = bool(row and (dict(row).get('value') == '1'))
        cfg = await get_vapid_config(generate_if_missing=False) if enabled else None
        return jsonify({
            'ok': True,
            'enabled': enabled,
            'public_key': (cfg or {}).get('public') if cfg else None,
        })

    # ── POST /api/pwa/push/subscribe ────────────────────────────────────────
    async def push_subscribe():
        admin_id = _admin_session_id()
        if not admin_id:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        body = await request.get_json(silent=True) or {}

        endpoint = (body.get('endpoint') or '').strip()
        keys     = body.get('keys') or {}
        p256dh   = (keys.get('p256dh') or '').strip()
        auth     = (keys.get('auth')   or '').strip()
        if not endpoint or not p256dh or not auth:
            return jsonify({'ok': False, 'error': 'invalid_subscription'}), 400

        ua = (request.headers.get('User-Agent') or '')[:500]
        events_mask = int(body.get('events_mask') or 1)

        sid = await upsert_subscription(
            admin_user_id=admin_id,
            endpoint=endpoint,
            p256dh=p256dh, auth=auth,
            user_agent=ua,
            events_mask=events_mask,
        )
        return jsonify({'ok': True, 'subscription_id': sid})

    # ── POST /api/pwa/push/unsubscribe ──────────────────────────────────────
    async def push_unsubscribe():
        if not _admin_session_id():
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        body = await request.get_json(silent=True) or {}
        endpoint = (body.get('endpoint') or '').strip()
        if not endpoint:
            return jsonify({'ok': False, 'error': 'invalid_endpoint'}), 400
        await remove_subscription(endpoint)
        return jsonify({'ok': True})

    # ── POST /api/pwa/push/test ─────────────────────────────────────────────
    async def push_test():
        admin_id = _admin_session_id()
        if not admin_id:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        cfg = await get_vapid_config(generate_if_missing=False)
        if not cfg:
            return jsonify({'ok': False, 'error': 'vapid_not_configured'}), 400

        stats = await send_to_admin_push(
            admin_user_id=admin_id,
            payload={
                'title': 'Тестовое уведомление',
                'body':  'Push-уведомления работают. Это тест.',
                'tag':   'test',
                'renotify': True,
                'data':  {'type': 'test', 'url': '/settings/push'},
            },
            events_filter_bit=1,
        )
        return jsonify({'ok': True, **stats})

    # ── POST /api/pwa/push/devices/<id>/delete ─────────────────────────────
    async def push_device_delete(sub_id: int):
        admin_id = _admin_session_id()
        if not admin_id:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        await remove_subscription_by_id(int(sub_id), admin_id)
        return jsonify({'ok': True})

    # ── Регистрация маршрутов ───────────────────────────────────────────────
    admin_bp.add_url_rule('/settings/push',
                          view_func=settings_push_view, endpoint='settings_push')
    admin_bp.add_url_rule('/api/pwa/push/setup',
                          view_func=push_setup, endpoint='push_setup', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/toggle',
                          view_func=push_toggle, endpoint='push_toggle', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/event-toggle',
                          view_func=push_event_toggle, endpoint='push_event_toggle', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/subject',
                          view_func=push_subject, endpoint='push_subject', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/config',
                          view_func=push_config, endpoint='push_config')
    admin_bp.add_url_rule('/api/pwa/push/subscribe',
                          view_func=push_subscribe, endpoint='push_subscribe', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/unsubscribe',
                          view_func=push_unsubscribe, endpoint='push_unsubscribe', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/test',
                          view_func=push_test, endpoint='push_test', methods=['POST'])
    admin_bp.add_url_rule('/api/pwa/push/devices/<int:sub_id>/delete',
                          view_func=push_device_delete, endpoint='push_device_delete', methods=['POST'])
