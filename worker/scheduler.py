"""Планировщик фоновых задач на APScheduler.

Все задачи идемпотентны и оборачиваются в метрики: время выполнения и счётчик
ошибок. `coalesce=True` + `max_instances=1` не дают задаче наслаиваться на себя,
если предыдущий запуск затянулся.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import settings
from core.metrics import worker_job_errors_total, worker_job_seconds
from worker.tasks import maintenance, payments, subscriptions

log = structlog.get_logger("worker.scheduler")

JobFunc = Callable[[], Awaitable[Any]]


def instrumented(name: str, func: JobFunc) -> JobFunc:
    async def wrapper() -> Any:
        with worker_job_seconds.labels(job=name).time():
            try:
                return await func()
            except Exception as exc:
                worker_job_errors_total.labels(job=name).inc()
                log.exception("worker.job_failed", job=name, error=str(exc))
                return None

    wrapper.__name__ = f"job_{name}"
    return wrapper


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()},
        executors={"default": AsyncIOExecutor()},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )

    scheduler.add_job(
        instrumented("mark_offline_devices", maintenance.mark_offline_devices),
        IntervalTrigger(minutes=5),
        id="mark_offline_devices",
        name="Отметить устройства не в сети",
    )
    scheduler.add_job(
        instrumented("expire_stale_commands", maintenance.expire_stale_commands),
        IntervalTrigger(minutes=10),
        id="expire_stale_commands",
        name="Просроченные команды устройствам",
    )
    scheduler.add_job(
        instrumented("clear_expired_prev_tokens", maintenance.clear_expired_prev_tokens),
        IntervalTrigger(hours=1),
        id="clear_expired_prev_tokens",
        name="Погасить старые токены подписки",
    )
    scheduler.add_job(
        instrumented("fleet_summary", maintenance.log_fleet_summary),
        IntervalTrigger(hours=1),
        id="fleet_summary",
        name="Сводка по устройствам",
    )
    scheduler.add_job(
        instrumented("sync_pending_payments", payments.sync_pending_payments),
        IntervalTrigger(minutes=3),
        id="sync_pending_payments",
        name="Досмотр висящих платежей",
    )
    scheduler.add_job(
        instrumented("expire_payments", payments.expire_payments),
        IntervalTrigger(minutes=15),
        id="expire_payments",
        name="Погасить просроченные платёжные ссылки",
    )
    scheduler.add_job(
        instrumented("refresh_subscription_statuses", subscriptions.refresh_statuses),
        IntervalTrigger(minutes=30),
        id="refresh_subscription_statuses",
        name="Пересчёт статусов подписок",
    )
    scheduler.add_job(
        instrumented("subscription_reminders", subscriptions.send_reminders),
        CronTrigger(hour=7, minute=0),
        id="subscription_reminders",
        name="Напоминания об окончании подписки (10:00 МСК)",
    )
    scheduler.add_job(
        instrumented("expire_unactivated", subscriptions.expire_unactivated),
        CronTrigger(hour=4, minute=10),
        id="expire_unactivated",
        name="Сгорание неактивированных подписок",
    )
    scheduler.add_job(
        instrumented("cleanup_heartbeats", maintenance.cleanup_heartbeats),
        CronTrigger(hour=3, minute=20),
        id="cleanup_heartbeats",
        name="Чистка телеметрии",
    )
    scheduler.add_job(
        instrumented("cleanup_access_log", maintenance.cleanup_access_log),
        CronTrigger(hour=3, minute=40),
        id="cleanup_access_log",
        name="Чистка лога обращений за подпиской",
    )

    log.info(
        "worker.jobs_registered",
        jobs=[job.id for job in scheduler.get_jobs()],
        retention_days=settings.subscription.heartbeat_retention_days,
    )
    return scheduler
