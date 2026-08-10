"""Массовые операции: удаление истёкших клиентов, компенсация и очистка devices.db."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

import aiosqlite
from loguru import logger
from remnawave.exceptions import NotFoundError

from app_config import app_conf
import db_helpers
from web_admin.async_db import async_execute_db, async_query_db, get_db_connection

MIN_EXPIRED_DAYS = 14
MAX_EXPIRED_DAYS = 3650
MIN_COMPENSATE_DAYS = 1
MAX_COMPENSATE_DAYS = 365

# Пауза между клиентами — снижает конкуренцию за SQLite и Remnawave API
DELETE_PAUSE_SEC = 0.08
RETRY_FAILED_PAUSE_SEC = 0.15
COMPENSATE_USER_TIMEOUT_SEC = 60
COMPENSATE_BATCH_SIZE = 50
COMPENSATE_CONCURRENCY = 15
COMPENSATE_PROGRESS_EVERY = 5
COMPENSATE_BATCH_PAUSE_SEC = 0.05
DB_RETRY_DELAYS_SEC = (0.15, 0.35, 0.75, 1.5, 3.0)

_bulk_ops_lock = asyncio.Lock()
_bulk_progress: Optional[Dict[str, Any]] = None
_compensate_session: Optional[Dict[str, Any]] = None

T = TypeVar("T")


class BulkOpsBusy(Exception):
    """Другая массовая операция уже выполняется."""


class DevicesDbUnavailable(Exception):
    """Модуль devices недоступен или devices.db не найдена."""


def is_bulk_operation_running() -> bool:
    return _bulk_ops_lock.locked()


def get_bulk_progress() -> Optional[Dict[str, Any]]:
    return _bulk_progress.copy() if _bulk_progress else None


def _update_bulk_progress(
    *,
    operation: str,
    processed: int,
    total: int,
    success: int,
    failed: int,
    current_id: Optional[int] = None,
    db_locked: int = 0,
    batch_num: Optional[int] = None,
    batch_total: Optional[int] = None,
    not_in_remnawave: int = 0,
) -> None:
    global _bulk_progress
    _bulk_progress = {
        "operation": operation,
        "processed": processed,
        "total": total,
        "success": success,
        "failed": failed,
        "current_id": current_id,
        "db_locked": db_locked,
        "batch_num": batch_num,
        "batch_total": batch_total,
        "not_in_remnawave": not_in_remnawave,
    }


def _clear_bulk_progress() -> None:
    global _bulk_progress
    _bulk_progress = None


def _blocked_filter() -> str:
    return "COALESCE(is_blocked, 0) = 0"


def _is_db_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        isinstance(exc, aiosqlite.OperationalError)
        and ("database is locked" in msg or "database table is locked" in msg)
    )


async def _retry_db_operation(
    operation: Callable[[], Awaitable[T]],
    *,
    label: str,
) -> T:
    last_exc: Optional[BaseException] = None
    for attempt, delay in enumerate(DB_RETRY_DELAYS_SEC, start=1):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            if _is_db_locked_error(exc) and attempt < len(DB_RETRY_DELAYS_SEC):
                logger.warning(
                    f"[BULK OPS] {label}: database locked, retry {attempt}/{len(DB_RETRY_DELAYS_SEC)} "
                    f"через {delay:.2f}s"
                )
                await asyncio.sleep(delay)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}: retry exhausted")


async def _acquire_bulk_slot() -> None:
    if _bulk_ops_lock.locked():
        raise BulkOpsBusy("Уже выполняется другая массовая операция. Дождитесь завершения.")
    await _bulk_ops_lock.acquire()


def _release_bulk_slot() -> None:
    if _bulk_ops_lock.locked():
        _bulk_ops_lock.release()


async def count_expired_users(days: int) -> int:
    row = await async_query_db(
        f"""
        SELECT COUNT(*) AS cnt FROM users
        WHERE {_blocked_filter()}
          AND subscription_end_date IS NOT NULL
          AND datetime(subscription_end_date) <= datetime('now', 'utc', ?)
        """,
        (f"-{int(days)} days",),
        one=True,
    )
    return int((row or {}).get("cnt") or 0)


async def count_active_users() -> int:
    """Активные клиенты с UUID — те же условия, что в статистике устройств и db_helpers."""
    return await db_helpers.count_active_subscription_users(require_uuid=True)


async def fetch_expired_user_ids(days: int) -> List[int]:
    rows = await async_query_db(
        f"""
        SELECT telegram_id FROM users
        WHERE {_blocked_filter()}
          AND subscription_end_date IS NOT NULL
          AND datetime(subscription_end_date) <= datetime('now', 'utc', ?)
        ORDER BY subscription_end_date ASC
        """,
        (f"-{int(days)} days",),
    )
    return [int(r["telegram_id"]) for r in (rows or [])]


async def fetch_active_user_ids() -> List[int]:
    users = await db_helpers.get_active_subscription_users(require_uuid=True)
    return [int(u["telegram_id"]) for u in users]


async def fetch_active_compensate_targets() -> List[Dict[str, Any]]:
    """Активные клиенты для компенсации — один запрос в начале операции."""
    users = await db_helpers.get_active_subscription_users(require_uuid=True)
    return [
        {
            "telegram_id": u["telegram_id"],
            "xui_client_uuid": u["uuid"],
            "subscription_end_date": u["subscription_end_date"],
        }
        for u in users
    ]


def _parse_subscription_end_utc(end_raw: Any) -> Optional[datetime]:
    if not end_raw:
        return None
    try:
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            return end_dt.replace(tzinfo=timezone.utc)
        return end_dt.astimezone(timezone.utc)
    except Exception:
        return None


async def count_empty_uuid_users() -> int:
    """Клиенты без привязки к Remnawave (пустой/NULL xui_client_uuid).

    Заблокированных пропускаем — их удалять не нужно (как и в остальных
    массовых операциях).
    """
    row = await async_query_db(
        f"SELECT COUNT(*) AS cnt FROM users "
        f"WHERE {_blocked_filter()} AND COALESCE(xui_client_uuid, '') = ''",
        (),
        one=True,
    )
    return int((row or {}).get("cnt") or 0)


async def fetch_empty_uuid_user_ids() -> List[int]:
    rows = await async_query_db(
        f"SELECT telegram_id FROM users "
        f"WHERE {_blocked_filter()} AND COALESCE(xui_client_uuid, '') = '' "
        f"ORDER BY telegram_id ASC",
        (),
    )
    return [int(r["telegram_id"]) for r in (rows or [])]


async def _delete_user_db_records(telegram_id: int, db: aiosqlite.Connection) -> None:
    """Все SQL-операции удаления в одной транзакции — меньше блокировок."""
    await db.execute(
        "UPDATE payments SET telegram_id = NULL WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "UPDATE promo_codes SET activated_by_telegram_id = NULL WHERE activated_by_telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM client_recreation_errors WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM promo_redemptions WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM user_enabled_servers WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM device_limit_changes WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM partner_accruals WHERE partner_id = ? OR payer_id = ?",
        (telegram_id, telegram_id),
    )
    await db.execute(
        "DELETE FROM referral_payment_bonus WHERE ref_user_id = ? OR invited_user_id = ?",
        (telegram_id, telegram_id),
    )
    await db.execute(
        "UPDATE users SET invited_by = NULL WHERE invited_by = ?",
        (telegram_id,),
    )
    await db.execute(
        "DELETE FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    await db.commit()


async def delete_user_full(telegram_id: int) -> Tuple[bool, Optional[str]]:
    """Полное удаление клиента (БД + Remnawave), как в разделе «Клиенты»."""
    user = await async_query_db(
        "SELECT *, COALESCE(subscription_mode, 'multi') AS subscription_mode "
        "FROM users WHERE telegram_id = ?",
        (telegram_id,),
        one=True,
    )
    if not user:
        return False, "not_found"

    user = dict(user)
    remnawave_error: Optional[str] = None

    if user.get("xui_client_uuid"):
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            await remnawave_manager_instance.delete_user(user["xui_client_uuid"])
            logger.info(f"[BULK DELETE] {telegram_id} удалён из Remnawave")
        except Exception as e:
            remnawave_error = str(e)
            logger.warning(f"[BULK DELETE] Remnawave {telegram_id}: {e}")

    async def _db_delete() -> None:
        async with get_db_connection() as db:
            try:
                await _delete_user_db_records(telegram_id, db)
            except Exception:
                await db.rollback()
                raise

    try:
        await _retry_db_operation(
            _db_delete,
            label=f"delete_user_db:{telegram_id}",
        )
    except Exception as e:
        if _is_db_locked_error(e):
            return False, "database_locked"
        return False, str(e)

    if remnawave_error:
        return True, f"db_ok_rw_warn:{remnawave_error}"
    return True, None


async def compensate_user_fast(
    target: Dict[str, Any],
    days: int,
) -> Tuple[bool, Optional[str]]:
    """Продление только даты: PATCH expire_at в Remnawave + UPDATE subscription_end_date в БД."""
    telegram_id = int(target["telegram_id"])
    user_uuid = (target.get("xui_client_uuid") or "").strip()
    if not user_uuid:
        return False, "no_uuid"

    end_dt = _parse_subscription_end_utc(target.get("subscription_end_date"))
    if not end_dt:
        return False, "bad_subscription_date"
    if end_dt <= datetime.now(timezone.utc):
        return False, "not_active"

    new_expire = end_dt + timedelta(days=int(days))

    try:
        await app_conf.load_settings()
        from remnawave_manager import remnawave_manager_instance

        rw_result = await remnawave_manager_instance.extend_user_expire_at(user_uuid, new_expire)
    except NotFoundError:
        return False, "not_in_remnawave"
    except Exception as e:
        if _is_db_locked_error(e):
            return False, "database_locked"
        return False, f"rw_error:{type(e).__name__}"

    if not rw_result:
        return False, "rw_update_failed"

    rw_expire = rw_result.get("expire_at")
    if isinstance(rw_expire, datetime):
        db_end = rw_expire.astimezone(timezone.utc)
    else:
        db_end = new_expire

    end_str = db_end.isoformat()

    async def _update_db() -> None:
        await async_execute_db(
            "UPDATE users SET subscription_end_date = ? WHERE telegram_id = ?",
            (end_str, telegram_id),
        )

    try:
        await _retry_db_operation(
            _update_db,
            label=f"compensate_db:{telegram_id}",
        )
    except Exception as e:
        if _is_db_locked_error(e):
            return False, "database_locked"
        return False, f"db_error:{type(e).__name__}"

    return True, None


async def compensate_user(telegram_id: int, days: int) -> Tuple[bool, Optional[str]]:
    """Продление только дней активной подписки, без уведомлений в Telegram."""
    user = await db_helpers.get_user(telegram_id)
    if not user or user.get("is_blocked"):
        return False, "blocked_or_missing"

    if not user.get("xui_client_uuid"):
        return False, "no_uuid"

    end_raw = user.get("subscription_end_date")
    if not end_raw:
        return False, "no_subscription"

    end_dt = _parse_subscription_end_utc(end_raw)
    if not end_dt:
        return False, "bad_subscription_date"
    if end_dt <= datetime.now(timezone.utc):
        return False, "not_active"

    target = {
        "telegram_id": telegram_id,
        "xui_client_uuid": user.get("xui_client_uuid"),
        "subscription_end_date": end_raw,
    }
    return await compensate_user_fast(target, days)


async def _emit_progress(
    on_progress: Optional[Any],
    *,
    operation: str,
    processed: int,
    total: int,
    success: int,
    failed: int,
    current_id: Optional[int] = None,
    db_locked: int = 0,
    batch_num: Optional[int] = None,
    batch_total: Optional[int] = None,
    not_in_remnawave: int = 0,
) -> None:
    _update_bulk_progress(
        operation=operation,
        processed=processed,
        total=total,
        success=success,
        failed=failed,
        current_id=current_id,
        db_locked=db_locked,
        batch_num=batch_num,
        batch_total=batch_total,
        not_in_remnawave=not_in_remnawave,
    )
    if on_progress:
        await on_progress(
            processed=processed,
            total=total,
            success=success,
            failed=failed,
            current_id=current_id,
            db_locked=db_locked,
            batch_num=batch_num,
            batch_total=batch_total,
            not_in_remnawave=not_in_remnawave,
        )


async def _process_compensate_chunk(
    chunk: List[Dict[str, Any]],
    days: int,
    *,
    offset: int,
    total: int,
    batch_num: int,
    batch_total: int,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[asyncio.Event] = None,
    cumulative_success: int = 0,
    cumulative_failed: int = 0,
    cumulative_db_locked: int = 0,
    cumulative_not_in_rw: int = 0,
) -> Tuple[int, int, int, int, List[str], Optional[int]]:
    """Параллельно обрабатывает порцию клиентов (до COMPENSATE_CONCURRENCY одновременно)."""
    success = failed = db_locked = not_in_remnawave = 0
    errors: List[str] = []
    last_id: Optional[int] = None
    done_in_chunk = 0
    state_lock = asyncio.Lock()
    sem = asyncio.Semaphore(COMPENSATE_CONCURRENCY)

    async def process_target(target: Dict[str, Any]) -> None:
        nonlocal success, failed, db_locked, not_in_remnawave, last_id, done_in_chunk
        if cancel_event and cancel_event.is_set():
            return
        telegram_id = int(target["telegram_id"])
        async with sem:
            try:
                ok, err = await asyncio.wait_for(
                    compensate_user_fast(target, days),
                    timeout=COMPENSATE_USER_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                ok, err = False, "timeout"
                logger.warning(
                    f"[BULK COMPENSATE] Таймаут {telegram_id} ({COMPENSATE_USER_TIMEOUT_SEC}s)"
                )

            async with state_lock:
                last_id = telegram_id
                done_in_chunk += 1
                global_idx = offset + done_in_chunk
                if ok:
                    success += 1
                else:
                    failed += 1
                    if err == "database_locked":
                        db_locked += 1
                    elif err == "not_in_remnawave":
                        not_in_remnawave += 1
                    if len(errors) < 20:
                        errors.append(f"{telegram_id}: {err or 'error'}")
                should_emit = (
                    done_in_chunk == len(chunk)
                    or done_in_chunk % COMPENSATE_PROGRESS_EVERY == 0
                )
                if should_emit:
                    await _emit_progress(
                        on_progress,
                        operation="compensate",
                        processed=global_idx,
                        total=total,
                        success=cumulative_success + success,
                        failed=cumulative_failed + failed,
                        current_id=telegram_id,
                        db_locked=cumulative_db_locked + db_locked,
                        batch_num=batch_num,
                        batch_total=batch_total,
                        not_in_remnawave=cumulative_not_in_rw + not_in_remnawave,
                    )

    await asyncio.gather(*(process_target(t) for t in chunk))
    return success, failed, db_locked, not_in_remnawave, errors, last_id


async def run_compensate_batch(
    days: int,
    offset: int = 0,
    *,
    batch_size: int = COMPENSATE_BATCH_SIZE,
) -> Dict[str, Any]:
    """Один пакет компенсации (по умолчанию 50 клиентов). Список активных кэшируется между пакетами."""
    global _compensate_session
    await _acquire_bulk_slot()
    try:
        if (
            offset == 0
            or _compensate_session is None
            or _compensate_session.get("days") != days
        ):
            targets = await fetch_active_compensate_targets()
            _compensate_session = {"days": days, "targets": targets}
        else:
            targets = _compensate_session["targets"]

        total = len(targets)
        offset = max(0, min(int(offset), total))
        chunk = targets[offset:offset + batch_size]
        batch_num = (offset // batch_size) + 1 if chunk else 0
        batch_total = max(1, (total + batch_size - 1) // batch_size) if total else 0

        logger.info(
            f"[BULK COMPENSATE] Пакет {batch_num}/{batch_total}: "
            f"клиенты {offset + 1}–{offset + len(chunk)} из {total}, +{days} дн."
        )

        success, failed, db_locked, not_in_remnawave, errors, last_id = await _process_compensate_chunk(
            chunk,
            days,
            offset=offset,
            total=total,
            batch_num=batch_num,
            batch_total=batch_total,
        )

        next_offset = offset + len(chunk)
        if next_offset >= total:
            _compensate_session = None

        result = {
            "total": total,
            "offset": offset,
            "next_offset": next_offset,
            "done": next_offset >= total,
            "batch_num": batch_num,
            "batch_total": batch_total,
            "batch_size": len(chunk),
            "batch_success": success,
            "batch_failed": failed,
            "db_locked": db_locked,
            "not_in_remnawave": not_in_remnawave,
            "last_id": last_id,
            "errors": errors,
        }
        logger.info(f"[BULK COMPENSATE] Пакет {batch_num}/{batch_total} готов: {result}")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()
        await asyncio.sleep(COMPENSATE_BATCH_PAUSE_SEC)


async def run_bulk_delete_expired(
    days: int,
    *,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    await _acquire_bulk_slot()
    try:
        user_ids = await fetch_expired_user_ids(days)
        total = len(user_ids)
        success = 0
        failed = 0
        db_locked = 0
        errors: List[str] = []
        logger.info(f"[BULK DELETE] Старт: {total} клиентов, порог {days} дн.")

        for idx, telegram_id in enumerate(user_ids, start=1):
            if cancel_event and cancel_event.is_set():
                break
            ok, err = await delete_user_full(telegram_id)
            if ok:
                success += 1
            else:
                failed += 1
                if err == "database_locked":
                    db_locked += 1
                if len(errors) < 50:
                    errors.append(f"{telegram_id}: {err or 'error'}")
            await _emit_progress(
                on_progress,
                operation="delete_expired",
                processed=idx,
                total=total,
                success=success,
                failed=failed,
                current_id=telegram_id,
                db_locked=db_locked,
            )
            if idx % 50 == 0 or idx == total:
                logger.info(
                    f"[BULK DELETE] {idx}/{total}: ok={success}, err={failed}, db_locked={db_locked}"
                )
            await asyncio.sleep(DELETE_PAUSE_SEC)

        result = {
            "total": total,
            "processed": success + failed,
            "success": success,
            "failed": failed,
            "db_locked": db_locked,
            "cancelled": bool(cancel_event and cancel_event.is_set()),
            "errors": errors,
        }
        logger.info(f"[BULK DELETE] Завершено: {result}")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()


async def run_bulk_delete_empty_uuid(
    *,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """Удаление клиентов с пустым UUID (нет клиента в Remnawave)."""
    await _acquire_bulk_slot()
    try:
        user_ids = await fetch_empty_uuid_user_ids()
        total = len(user_ids)
        success = 0
        failed = 0
        db_locked = 0
        errors: List[str] = []
        logger.info(f"[BULK DELETE EMPTY UUID] Старт: {total} клиентов с пустым UUID")

        for idx, telegram_id in enumerate(user_ids, start=1):
            if cancel_event and cancel_event.is_set():
                break
            ok, err = await delete_user_full(telegram_id)
            if ok:
                success += 1
            else:
                failed += 1
                if err == "database_locked":
                    db_locked += 1
                if len(errors) < 50:
                    errors.append(f"{telegram_id}: {err or 'error'}")
            await _emit_progress(
                on_progress,
                operation="delete_empty_uuid",
                processed=idx,
                total=total,
                success=success,
                failed=failed,
                current_id=telegram_id,
                db_locked=db_locked,
            )
            if idx % 50 == 0 or idx == total:
                logger.info(
                    f"[BULK DELETE EMPTY UUID] {idx}/{total}: ok={success}, err={failed}, db_locked={db_locked}"
                )
            await asyncio.sleep(DELETE_PAUSE_SEC)

        result = {
            "total": total,
            "processed": success + failed,
            "success": success,
            "failed": failed,
            "db_locked": db_locked,
            "cancelled": bool(cancel_event and cancel_event.is_set()),
            "errors": errors,
        }
        logger.info(f"[BULK DELETE EMPTY UUID] Завершено: {result}")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()


_FAILED_PAYMENTS_RETRY_WHERE = (
    "status = 'failed' AND "
    "COALESCE(json_extract(metadata_json, '$.payment_type'), '') "
    "NOT IN ('device_limit_upgrade')"
)


async def count_retryable_failed_payments() -> int:
    """failed-платежи: продление подписки и докупка GB (без device_limit_upgrade)."""
    row = await async_query_db(
        f"SELECT COUNT(*) AS cnt FROM payments WHERE {_FAILED_PAYMENTS_RETRY_WHERE}",
        (),
        one=True,
    )
    return int((row or {}).get("cnt") or 0)


async def fetch_retryable_failed_payment_ids() -> List[str]:
    rows = await async_query_db(
        f"SELECT payment_id FROM payments WHERE {_FAILED_PAYMENTS_RETRY_WHERE} "
        f"ORDER BY created_at ASC",
        (),
    )
    return [str(r["payment_id"]) for r in (rows or [])]


async def run_bulk_retry_failed_payments(
    *,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    from web_admin.core.payment_retry import retry_failed_payment

    await _acquire_bulk_slot()
    try:
        payment_ids = await fetch_retryable_failed_payment_ids()
        total = len(payment_ids)
        success = 0
        failed = 0
        errors: List[str] = []
        logger.info(f"[BULK RETRY FAILED] Старт: {total} платежей")

        for idx, payment_id in enumerate(payment_ids, start=1):
            if cancel_event and cancel_event.is_set():
                break
            ok, err = await retry_failed_payment(payment_id)
            if ok:
                success += 1
            else:
                failed += 1
                if len(errors) < 50:
                    errors.append(f"{payment_id}: {err or 'error'}")
            await _emit_progress(
                on_progress,
                operation="retry_failed_payments",
                processed=idx,
                total=total,
                success=success,
                failed=failed,
                current_id=None,
            )
            if idx % 20 == 0 or idx == total:
                logger.info(
                    f"[BULK RETRY FAILED] {idx}/{total}: ok={success}, err={failed}"
                )
            await asyncio.sleep(RETRY_FAILED_PAUSE_SEC)

        result = {
            "total": total,
            "processed": success + failed,
            "success": success,
            "failed": failed,
            "db_locked": 0,
            "cancelled": bool(cancel_event and cancel_event.is_set()),
            "errors": errors,
        }
        logger.info(f"[BULK RETRY FAILED] Завершено: {result}")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()


async def run_bulk_compensate(
    days: int,
    *,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """Полный прогон: один SELECT активных, затем пакеты по COMPENSATE_BATCH_SIZE параллельно."""
    await _acquire_bulk_slot()
    try:
        targets = await fetch_active_compensate_targets()
        total = len(targets)
        success = failed = db_locked = not_in_remnawave = 0
        errors: List[str] = []
        batch_total = max(1, (total + COMPENSATE_BATCH_SIZE - 1) // COMPENSATE_BATCH_SIZE) if total else 0

        logger.info(
            f"[BULK COMPENSATE] Старт: {total} клиентов, +{days} дн., "
            f"пакеты по {COMPENSATE_BATCH_SIZE}, параллельно до {COMPENSATE_CONCURRENCY}"
        )

        for batch_num, offset in enumerate(
            range(0, total, COMPENSATE_BATCH_SIZE),
            start=1,
        ):
            if cancel_event and cancel_event.is_set():
                break
            chunk = targets[offset:offset + COMPENSATE_BATCH_SIZE]
            bs, bf, bd, bn, batch_errors, _ = await _process_compensate_chunk(
                chunk,
                days,
                offset=offset,
                total=total,
                batch_num=batch_num,
                batch_total=batch_total,
                on_progress=on_progress,
                cancel_event=cancel_event,
                cumulative_success=success,
                cumulative_failed=failed,
                cumulative_db_locked=db_locked,
                cumulative_not_in_rw=not_in_remnawave,
            )
            success += bs
            failed += bf
            db_locked += bd
            not_in_remnawave += bn
            for err_line in batch_errors:
                if len(errors) < 50:
                    errors.append(err_line)
            if batch_num < batch_total:
                await asyncio.sleep(COMPENSATE_BATCH_PAUSE_SEC)
            logger.info(
                f"[BULK COMPENSATE] Пакет {batch_num}/{batch_total}: "
                f"ok={success}, err={failed}, not_in_rw={not_in_remnawave}"
            )

        result = {
            "total": total,
            "processed": success + failed,
            "success": success,
            "failed": failed,
            "db_locked": db_locked,
            "not_in_remnawave": not_in_remnawave,
            "cancelled": bool(cancel_event and cancel_event.is_set()),
            "errors": errors,
        }
        logger.info(f"[BULK COMPENSATE] Завершено: {result}")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()


def _devices_db_available() -> bool:
    try:
        from devices.config import DEVICES_DB_PATH

        return os.path.isfile(os.path.abspath(DEVICES_DB_PATH))
    except Exception:
        return False


async def count_devices_db_records() -> int:
    if not _devices_db_available():
        raise DevicesDbUnavailable(
            "База devices.db не найдена. Убедитесь, что модуль devices установлен и БД создана."
        )
    from devices.database import count_subscription_access_records

    return await count_subscription_access_records()


async def run_clear_devices_db() -> Dict[str, Any]:
    """Полная очистка subscription_access в devices.db."""
    if not _devices_db_available():
        raise DevicesDbUnavailable(
            "База devices.db не найдена. Убедитесь, что модуль devices установлен и БД создана."
        )

    await _acquire_bulk_slot()
    try:
        from devices.database import clear_all_subscription_access

        before = await count_devices_db_records()
        logger.info(f"[BULK CLEAR DEVICES] Старт: {before} записей в devices.db")

        deleted = await clear_all_subscription_access()

        result = {
            "total": before,
            "processed": deleted,
            "success": deleted,
            "failed": 0,
            "db_locked": 0,
            "cancelled": False,
            "errors": [],
        }
        logger.info(f"[BULK CLEAR DEVICES] Завершено: удалено {deleted} записей")
        return result
    finally:
        _clear_bulk_progress()
        _release_bulk_slot()
