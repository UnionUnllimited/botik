"""Маршруты: Инструменты → Массовые действия."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from quart import Response, jsonify, render_template, request, session

from loguru import logger

from web_admin.core.bulk_ops import (
    BulkOpsBusy,
    DevicesDbUnavailable,
    MAX_COMPENSATE_DAYS,
    MAX_EXPIRED_DAYS,
    MIN_COMPENSATE_DAYS,
    MIN_EXPIRED_DAYS,
    count_active_users,
    count_devices_db_records,
    count_empty_uuid_users,
    count_expired_users,
    count_retryable_failed_payments,
    get_bulk_progress,
    is_bulk_operation_running,
    run_bulk_compensate,
    run_bulk_delete_empty_uuid,
    run_bulk_delete_expired,
    run_bulk_retry_failed_payments,
    run_clear_devices_db,
    run_compensate_batch,
    COMPENSATE_BATCH_SIZE,
)

_bulk_cancel_events: dict[str, asyncio.Event] = {}


def _admin_only():
    if session.get("admin_role") == "moderator":
        return jsonify({"ok": False, "error": "Доступно только администратору."}), 403
    return None


def _parse_days(raw, *, minimum: int, maximum: int) -> Optional[int]:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    if days < minimum or days > maximum:
        return None
    return days


def attach_bulk_ops_routes(admin_bp_instance):
    @admin_bp_instance.route("/tools/bulk-actions")
    async def bulk_actions_page():
        denied = _admin_only()
        if denied:
            return denied
        return await render_template(
            "bulk_actions.html",
            min_expired_days=MIN_EXPIRED_DAYS,
            max_expired_days=MAX_EXPIRED_DAYS,
            min_compensate_days=MIN_COMPENSATE_DAYS,
            max_compensate_days=MAX_COMPENSATE_DAYS,
            compensate_batch_size=COMPENSATE_BATCH_SIZE,
        )

    @admin_bp_instance.route("/api/tools/bulk-actions/status")
    async def bulk_ops_status():
        denied = _admin_only()
        if denied:
            return denied
        return jsonify({
            "ok": True,
            "running": is_bulk_operation_running(),
            "progress": get_bulk_progress(),
        })

    @admin_bp_instance.route("/api/tools/bulk-actions/count-expired")
    async def bulk_count_expired():
        denied = _admin_only()
        if denied:
            return denied
        days = _parse_days(
            request.args.get("days", MIN_EXPIRED_DAYS),
            minimum=MIN_EXPIRED_DAYS,
            maximum=MAX_EXPIRED_DAYS,
        )
        if days is None:
            return jsonify({
                "ok": False,
                "error": f"Укажите дни от {MIN_EXPIRED_DAYS} до {MAX_EXPIRED_DAYS}.",
            }), 400
        cnt = await count_expired_users(days)
        return jsonify({"ok": True, "count": cnt, "days": days})

    @admin_bp_instance.route("/api/tools/bulk-actions/count-active")
    async def bulk_count_active():
        denied = _admin_only()
        if denied:
            return denied
        cnt = await count_active_users()
        return jsonify({"ok": True, "count": cnt})

    @admin_bp_instance.route("/api/tools/bulk-actions/count-empty-uuid")
    async def bulk_count_empty_uuid():
        denied = _admin_only()
        if denied:
            return denied
        cnt = await count_empty_uuid_users()
        return jsonify({"ok": True, "count": cnt})

    @admin_bp_instance.route("/api/tools/bulk-actions/count-devices")
    async def bulk_count_devices():
        denied = _admin_only()
        if denied:
            return denied
        try:
            cnt = await count_devices_db_records()
            return jsonify({"ok": True, "count": cnt})
        except DevicesDbUnavailable as e:
            return jsonify({"ok": False, "error": str(e)}), 404

    @admin_bp_instance.route("/api/tools/bulk-actions/clear-devices", methods=["POST"])
    async def bulk_clear_devices():
        denied = _admin_only()
        if denied:
            return denied
        try:
            result = await run_clear_devices_db()
            return jsonify({"ok": True, **result})
        except BulkOpsBusy as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        except DevicesDbUnavailable as e:
            return jsonify({"ok": False, "error": str(e)}), 404
        except Exception as e:
            logger.exception("[BULK OPS] Ошибка очистки devices.db")
            return jsonify({"ok": False, "error": str(e)}), 500

    SSE_HEARTBEAT_SEC = 15

    def _sse_response(coro_factory):
        async def generate_events():
            queue: asyncio.Queue = asyncio.Queue()
            done_event = asyncio.Event()

            async def on_progress(**kwargs):
                await queue.put({"type": "progress", **kwargs})

            async def runner():
                try:
                    result = await coro_factory(on_progress)
                    await queue.put({"type": "complete", **result})
                except BulkOpsBusy as e:
                    await queue.put({"type": "error", "message": str(e)})
                except asyncio.CancelledError:
                    logger.warning("[BULK OPS] Фоновая задача отменена")
                    raise
                except Exception as e:
                    logger.exception("[BULK OPS] Ошибка массовой операции")
                    await queue.put({"type": "error", "message": str(e)})
                finally:
                    done_event.set()
                    await queue.put(None)

            async def heartbeat():
                try:
                    while not done_event.is_set():
                        await asyncio.sleep(SSE_HEARTBEAT_SEC)
                        if not done_event.is_set():
                            await queue.put({"type": "ping"})
                except asyncio.CancelledError:
                    pass

            runner_task = asyncio.create_task(runner())
            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.005)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                # Не отменяем runner_task: при обрыве SSE операция дорабатывает в фоне.

        return Response(
            generate_events(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @admin_bp_instance.route("/api/tools/bulk-actions/delete-expired", methods=["POST"])
    async def bulk_delete_expired_stream():
        denied = _admin_only()
        if denied:
            return denied
        try:
            data = await request.get_json(force=True) or {}
        except Exception:
            data = {}
        days = _parse_days(
            data.get("days", MIN_EXPIRED_DAYS),
            minimum=MIN_EXPIRED_DAYS,
            maximum=MAX_EXPIRED_DAYS,
        )
        if days is None:
            return jsonify({
                "ok": False,
                "error": f"Укажите дни от {MIN_EXPIRED_DAYS} до {MAX_EXPIRED_DAYS}.",
            }), 400

        cancel_event = asyncio.Event()
        _bulk_cancel_events["delete"] = cancel_event

        async def factory(on_progress):
            return await run_bulk_delete_expired(
                days,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

        return _sse_response(factory)

    @admin_bp_instance.route("/api/tools/bulk-actions/delete-empty-uuid", methods=["POST"])
    async def bulk_delete_empty_uuid_stream():
        denied = _admin_only()
        if denied:
            return denied

        cancel_event = asyncio.Event()
        _bulk_cancel_events["delete_empty_uuid"] = cancel_event

        async def factory(on_progress):
            return await run_bulk_delete_empty_uuid(
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

        return _sse_response(factory)

    @admin_bp_instance.route("/api/tools/bulk-actions/compensate", methods=["POST"])
    async def bulk_compensate_stream():
        denied = _admin_only()
        if denied:
            return denied
        try:
            data = await request.get_json(force=True) or {}
        except Exception:
            data = {}
        days = _parse_days(
            data.get("days", MIN_COMPENSATE_DAYS),
            minimum=MIN_COMPENSATE_DAYS,
            maximum=MAX_COMPENSATE_DAYS,
        )
        if days is None:
            return jsonify({
                "ok": False,
                "error": f"Укажите дни от {MIN_COMPENSATE_DAYS} до {MAX_COMPENSATE_DAYS}.",
            }), 400

        cancel_event = asyncio.Event()
        _bulk_cancel_events["compensate"] = cancel_event

        async def factory(on_progress):
            return await run_bulk_compensate(
                days,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

        return _sse_response(factory)

    @admin_bp_instance.route("/api/tools/bulk-actions/count-failed-payments")
    async def bulk_count_failed_payments():
        denied = _admin_only()
        if denied:
            return denied
        cnt = await count_retryable_failed_payments()
        return jsonify({"ok": True, "count": cnt})

    @admin_bp_instance.route("/api/tools/bulk-actions/retry-failed-payments", methods=["POST"])
    async def bulk_retry_failed_payments_stream():
        denied = _admin_only()
        if denied:
            return denied

        cancel_event = asyncio.Event()
        _bulk_cancel_events["retry_failed_payments"] = cancel_event

        async def factory(on_progress):
            return await run_bulk_retry_failed_payments(
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

        return _sse_response(factory)

    @admin_bp_instance.route("/api/tools/bulk-actions/compensate-batch", methods=["POST"])
    async def bulk_compensate_batch():
        denied = _admin_only()
        if denied:
            return denied
        try:
            data = await request.get_json(force=True) or {}
        except Exception:
            data = {}
        days = _parse_days(
            data.get("days", MIN_COMPENSATE_DAYS),
            minimum=MIN_COMPENSATE_DAYS,
            maximum=MAX_COMPENSATE_DAYS,
        )
        if days is None:
            return jsonify({
                "ok": False,
                "error": f"Укажите дни от {MIN_COMPENSATE_DAYS} до {MAX_COMPENSATE_DAYS}.",
            }), 400
        try:
            offset = int(data.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        try:
            result = await run_compensate_batch(days, offset)
            return jsonify({"ok": True, **result})
        except BulkOpsBusy as e:
            return jsonify({"ok": False, "error": str(e)}), 409
