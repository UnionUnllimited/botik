"""
Логика «Сбросить подписку» (revoke / regenerate) — только Remnawave.

Перед запуском обязательно проходит pre-flight проверка
инфраструктуры — см. `preflight_check`.

Контракт коллбэка прогресса:
    on_event(step: int, message: str | None = None,
             error: str | None = None,
             complete: bool = False,
             extra: dict | None = None)
        step  — номер этапа (0..6), 0 — pre-flight
        extra — произвольный JSON-словарь со структурированными данными
                (используется для preflight summary, лога ошибок и т.д.)
"""
from __future__ import annotations

import asyncio
import json
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from app_config import app_conf
from button_helpers import btn, connect_btn
from tg_sender import send_telegram_message
from web_admin.async_db import async_query_db, async_execute_db

from .server_health import (
    RemnawaveHealth,
    check_remnawave,
)


# ─────────────────────────────────────────────────────────────────────────
#                              Типы
# ─────────────────────────────────────────────────────────────────────────

OnEvent = Callable[..., Awaitable[None]]

REVOKE_TOTAL_STEPS = 6  # 1..6 (0 — pre-flight)


@dataclass
class ClientModeInfo:
    """Какого «типа» клиент с точки зрения revoke-логики."""
    mode: str           # 'no_limit_plus' | 'classic' | 'no_remnawave_record'
                        # | 'no_remnawave_provider' | 'unknown'
    label: str
    description: str
    no_limit_plus_feature_enabled: bool
    remnawave_user_exists: Optional[bool]  # None если не проверяли


@dataclass
class PreflightResult:
    ok: bool
    blocking_reason: Optional[str]
    remnawave: Optional[RemnawaveHealth]
    user_has_uuid: bool
    client_mode: ClientModeInfo

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ok': self.ok,
            'blocking_reason': self.blocking_reason,
            'user_provider': 'remnawave',
            'user_has_uuid': self.user_has_uuid,
            'xui': {
                'total': 0,
                'online': 0,
                'all_online': True,
                'is_empty': True,
                'offline': [],
            },
            'remnawave_required': True,
            'remnawave': (
                {
                    'ok': self.remnawave.ok,
                    'error': self.remnawave.error,
                    'ping_ms': self.remnawave.ping_ms,
                } if self.remnawave is not None else None
            ),
            'client_mode': {
                'mode': self.client_mode.mode,
                'label': self.client_mode.label,
                'description': self.client_mode.description,
                'no_limit_plus_feature_enabled': self.client_mode.no_limit_plus_feature_enabled,
                'remnawave_user_exists': self.client_mode.remnawave_user_exists,
            },
        }


@dataclass
class SubscriptionClass:
    """Классификация клиента для ветки в Remnawave-обновлении."""
    no_limit_plus_enabled: bool       # фича в админке настроена
    in_exhausted_squad: bool          # клиент сейчас в exhausted-сквде
    exhausted_squad_uuid: Optional[str]
    current_squad_uuids: List[str]    # все squads, к которым клиент привязан

    @property
    def use_no_limit_plus_branch(self) -> bool:
        return self.no_limit_plus_enabled and self.in_exhausted_squad


@dataclass
class RevokeResult:
    ok: bool
    new_uuid: Optional[str]
    old_uuid: Optional[str]
    failed_servers: List[str] = field(default_factory=list)
    updated_servers: List[str] = field(default_factory=list)
    used_no_limit_plus_branch: bool = False
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────
#                              Pre-flight
# ─────────────────────────────────────────────────────────────────────────

async def _load_user_for_revoke(telegram_id: int) -> Optional[Dict[str, Any]]:
    row = await async_query_db(
        "SELECT xui_client_uuid, remnawave_short_uuid, "
        "subscription_end_date, limit_ip, xui_client_email, username "
        "FROM users WHERE telegram_id = ?",
        (telegram_id,),
        one=True,
    )
    return dict(row) if row else None


def _is_no_limit_plus_enabled() -> bool:
    """Включён ли в админке режим 'No Limit+' (нужны все три параметра)."""
    return all([
        bool(str(app_conf.get('remnawave_webhook_enabled') or '').strip()),
        bool(str(app_conf.get('remnawave_webhook_secret') or '').strip()),
        bool(str(app_conf.get('remnawave_traffic_exhausted_squad_uuid') or '').strip()),
    ])


async def _fetch_remnawave_user_safe(
    client_uuid: str,
    timeout: float = 8.0,
) -> tuple[Any, Optional[str]]:
    """Аккуратно читает клиента из Remnawave.

    Возвращает (user_or_none, error_kind):
        ('not-found' — клиент 404)
        ('sdk-not-initialized')
        ('timeout')
        ('rw-error: ...')
        (None) — успех
    """
    try:
        from remnawave_manager import remnawave_manager_instance  # noqa: WPS433
        await remnawave_manager_instance._ensure_initialized()
        sdk = remnawave_manager_instance._sdk
        if sdk is None:
            return None, 'sdk-not-initialized'
        try:
            user = await asyncio.wait_for(
                sdk.users.get_user_by_uuid(client_uuid),
                timeout=timeout,
            )
            if user is None:
                return None, 'not-found'
            return user, None
        except asyncio.TimeoutError:
            return None, 'timeout'
        except Exception as e:
            err_text = str(e)
            low = err_text.lower()
            if (
                '404' in err_text
                or 'not found' in low
                or 'not_found' in low
                or 'a006' in low  # Remnawave: USER_NOT_FOUND
            ):
                return None, 'not-found'
            return None, f'rw-error: {type(e).__name__}: {err_text[:160]}'
    except Exception as e:
        return None, f'rw-error: {type(e).__name__}'


def _build_client_mode_info(
    *,
    rw_user: Any,
    rw_error: Optional[str],
    no_limit_plus_enabled: bool,
) -> ClientModeInfo:
    """Решает, какой режим у клиента и что покажет UI."""

    if rw_error == 'not-found' or (rw_user is None and rw_error is None):
        return ClientModeInfo(
            mode='no_remnawave_record',
            label='Не найден в Remnawave',
            description=(
                'Клиент с этим UUID отсутствует в Remnawave. '
                'Возможно, его удалили вручную в панели. '
                'Сброс невозможен — нечего пересоздавать.'
            ),
            no_limit_plus_feature_enabled=no_limit_plus_enabled,
            remnawave_user_exists=False,
        )

    if rw_user is None:
        # Remnawave недоступна или другая ошибка — без классификации
        return ClientModeInfo(
            mode='unknown',
            label='Не удалось определить',
            description='Remnawave недоступна — режим клиента будет определён в момент сброса.',
            no_limit_plus_feature_enabled=no_limit_plus_enabled,
            remnawave_user_exists=None,
        )

    cls = _classify_subscription(rw_user)
    if cls.use_no_limit_plus_branch:
        return ClientModeInfo(
            mode='no_limit_plus',
            label='No Limit+',
            description=(
                'Клиент находится в exhausted-скваде. '
                'После сброса будет пересоздан с безлимитным трафиком '
                'и сохранением всех текущих сквадов.'
            ),
            no_limit_plus_feature_enabled=no_limit_plus_enabled,
            remnawave_user_exists=True,
        )

    return ClientModeInfo(
        mode='classic',
        label='Классический',
        description=(
            'Стандартный клиент. После сброса остаток трафика '
            '(лимит − использовано) будет перенесён как новый лимит.'
        ),
        no_limit_plus_feature_enabled=no_limit_plus_enabled,
        remnawave_user_exists=True,
    )


async def preflight_check(
    telegram_id: int,
    *,
    remnawave_timeout: float = 8.0,
) -> PreflightResult:
    """Проверяет Remnawave перед revoke."""
    user = await _load_user_for_revoke(telegram_id)
    no_limit_plus_enabled = _is_no_limit_plus_enabled()

    def _empty_mode(label: str, desc: str, mode: str = 'unknown',
                    exists: Optional[bool] = None) -> ClientModeInfo:
        return ClientModeInfo(
            mode=mode, label=label, description=desc,
            no_limit_plus_feature_enabled=no_limit_plus_enabled,
            remnawave_user_exists=exists,
        )

    if not user:
        return PreflightResult(
            ok=False,
            blocking_reason='Пользователь не найден в БД',
            remnawave=None,
            user_has_uuid=False,
            client_mode=_empty_mode('Не определён', 'Пользователь отсутствует в базе.'),
        )

    has_uuid = bool(user.get('xui_client_uuid'))
    old_uuid = user.get('xui_client_uuid')

    if not has_uuid:
        return PreflightResult(
            ok=False,
            blocking_reason='У пользователя нет UUID — сбрасывать нечего',
            remnawave=None,
            user_has_uuid=False,
            client_mode=_empty_mode(
                'Без UUID',
                'У пользователя нет UUID подписки — нечего регенерировать.',
            ),
        )

    rw_health = await check_remnawave(timeout=remnawave_timeout)

    rw_user = None
    rw_user_error: Optional[str] = None
    if rw_health.ok and old_uuid:
        rw_user, rw_user_error = await _fetch_remnawave_user_safe(
            old_uuid, timeout=remnawave_timeout,
        )

    client_mode = _build_client_mode_info(
        rw_user=rw_user,
        rw_error=rw_user_error,
        no_limit_plus_enabled=no_limit_plus_enabled,
    )

    blocking_reason: Optional[str] = None

    if not rw_health.ok:
        blocking_reason = f'Remnawave недоступна: {rw_health.error}'
    elif client_mode.mode == 'no_remnawave_record':
        blocking_reason = (
            f'Клиент не найден в Remnawave (UUID {old_uuid}). '
            f'Возможно, был удалён в панели вручную.'
        )

    return PreflightResult(
        ok=blocking_reason is None,
        blocking_reason=blocking_reason,
        remnawave=rw_health,
        user_has_uuid=True,
        client_mode=client_mode,
    )


# ─────────────────────────────────────────────────────────────────────────
#                              Классификация клиента
# ─────────────────────────────────────────────────────────────────────────

def _classify_subscription(remnawave_user: Any) -> SubscriptionClass:
    """Понимает, в каком режиме клиента нужно пересоздавать."""
    no_limit_plus_enabled = _is_no_limit_plus_enabled()
    exhausted_squad_uuid = (
        str(app_conf.get('remnawave_traffic_exhausted_squad_uuid') or '').strip() or None
    )

    current_squads: List[str] = []
    try:
        squads_attr = getattr(remnawave_user, 'active_internal_squads', None) or []
        for s in squads_attr:
            uuid_value = getattr(s, 'uuid', None)
            if uuid_value is None:
                continue
            current_squads.append(str(uuid_value))
    except Exception as e:
        logger.warning(f"[REVOKE] Не удалось прочитать active_internal_squads: {e}")

    in_exhausted = bool(
        no_limit_plus_enabled
        and exhausted_squad_uuid
        and exhausted_squad_uuid.lower() in {u.lower() for u in current_squads}
    )

    return SubscriptionClass(
        no_limit_plus_enabled=no_limit_plus_enabled,
        in_exhausted_squad=in_exhausted,
        exhausted_squad_uuid=exhausted_squad_uuid,
        current_squad_uuids=current_squads,
    )


def _used_traffic_bytes(remnawave_user: Any) -> int:
    """Безопасное чтение использованного трафика (учитывает все варианты SDK)."""
    try:
        if hasattr(remnawave_user, 'used_traffic_bytes'):
            v = remnawave_user.used_traffic_bytes
            return int(float(v)) if v else 0
        if hasattr(remnawave_user, 'user_traffic'):
            ut = getattr(remnawave_user, 'user_traffic', None)
            if ut and hasattr(ut, 'used_traffic_bytes'):
                v = ut.used_traffic_bytes
                return int(float(v)) if v else 0
        v = getattr(remnawave_user, 'traffic_used_bytes', 0)
        return int(float(v)) if v else 0
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────
#                              Основная логика
# ─────────────────────────────────────────────────────────────────────────

async def _safe_emit(
    on_event: Optional[OnEvent],
    step: int,
    message: Optional[str] = None,
    error: Optional[str] = None,
    complete: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if on_event is None:
        return
    try:
        await on_event(step=step, message=message, error=error, complete=complete, extra=extra)
    except Exception as e:
        logger.warning(f"[REVOKE] on_event callback failed: {e}")


async def _recreate_remnawave_user(
    *,
    telegram_id: int,
    old_uuid: str,
    new_uuid: str,
    user_email: Optional[str],
    db_username: str,
    limit_ip: int,
    subscription_end_date: Optional[str],
    on_event: Optional[OnEvent],
) -> tuple[bool, bool, Optional[str]]:
    """Удаляет старого remnawave-юзера и создаёт нового.

    Возвращает:
        success: bool
        used_no_limit_plus_branch: bool
        error: str | None
    """
    from remnawave_manager import remnawave_manager_instance  # noqa: WPS433

    try:
        current_user = await remnawave_manager_instance._sdk.users.get_user_by_uuid(old_uuid)
    except Exception as e:
        logger.error(f"[REVOKE] Не удалось получить текущего юзера в Remnawave: {e}")
        return False, False, f'Remnawave: чтение клиента ({type(e).__name__})'

    if not current_user:
        logger.error(
            f"[REVOKE] Пользователь {telegram_id} не найден в Remnawave (UUID {old_uuid}). "
            f"Pre-flight должен был это поймать — отказываемся."
        )
        return False, False, (
            f'Клиент с UUID {old_uuid} отсутствует в Remnawave. '
            f'Запустите проверку заново.'
        )

    cls = _classify_subscription(current_user)

    # Имя пользователя в Remnawave (важно сохранить как было)
    rw_username = getattr(current_user, 'username', None)
    if not rw_username:
        rw_username = db_username if db_username and not db_username.startswith('tg') else f'tg{telegram_id}'

    # Лимит/использовано трафика
    traffic_limit_bytes = int(getattr(current_user, 'traffic_limit_bytes', 0) or 0)
    traffic_used_bytes = _used_traffic_bytes(current_user)

    # Срок
    expire_at = getattr(current_user, 'expire_at', None)
    if expire_at and isinstance(expire_at, str):
        from dateutil import parser  # noqa: WPS433
        expire_at = parser.parse(expire_at)
    elif not expire_at and subscription_end_date:
        from dateutil import parser  # noqa: WPS433
        expire_at = parser.parse(subscription_end_date)
    elif not expire_at:
        expire_at = datetime.now(timezone.utc)

    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    else:
        expire_at = expire_at.astimezone(timezone.utc)

    description = getattr(current_user, 'description', None) or f'Telegram ID: {telegram_id}'

    # ─────────────────── Ветка: «обычный клиент» ───────────────────
    # Передаём остаток трафика как новый лимит (Remnawave API не умеет
    # выставлять traffic_used_bytes при create).
    if traffic_limit_bytes > 0:
        remaining_bytes = max(0, traffic_limit_bytes - traffic_used_bytes)
        remaining_gb = remaining_bytes / (1024 ** 3)
        traffic_to_set_gb = max(0.1, remaining_gb) if remaining_gb > 0 else 0.1
    else:
        traffic_to_set_gb = 0  # был безлимит — оставляем безлимит

    # ─────────────────── Ветка: «No Limit+ исчерпанный» ───────────────────
    if cls.use_no_limit_plus_branch:
        await _safe_emit(on_event, 4,
                         'Клиент в exhausted-скваде (No Limit+) — пересоздаём с безлимитом')
        traffic_to_set_gb = 0  # 0 = безлимит
        target_squad_uuid = ','.join(cls.current_squad_uuids)  # сохраняем все текущие squads
    else:
        # Сохраняем все текущие squads клиента (никаких "перевешиваний" на default).
        # Если их нет — упадём в дефолтную логику create_user (она сама подхватит default).
        target_squad_uuid = ','.join(cls.current_squad_uuids) if cls.current_squad_uuids else None

    # Удаляем старого
    try:
        await remnawave_manager_instance.delete_user(old_uuid)
        logger.info(f"[REVOKE] Старый remnawave-юзер удалён: {old_uuid}")
    except Exception as e:
        logger.error(f"[REVOKE] Не удалось удалить старого remnawave-юзера: {e}")
        return False, cls.use_no_limit_plus_branch, f'Remnawave: удаление ({type(e).__name__})'

    # Создаём нового — через RemnawaveManager (он сам разрулит squads-CSV)
    now_utc = datetime.now(timezone.utc)
    days_left = max(1, (expire_at - now_utc).days)
    base_expiry_time = expire_at - timedelta(days=days_left)

    await app_conf.load_settings()

    try:
        created = await remnawave_manager_instance.create_user(
            telegram_id=telegram_id,
            username=rw_username,
            days_valid=days_left,
            total_gb=traffic_to_set_gb,
            description=description,
            internal_squad_uuid=target_squad_uuid,
            short_uuid=new_uuid,
            user_uuid=new_uuid,
            base_expiry=base_expiry_time,
        )
    except Exception as e:
        logger.error(f"[REVOKE] Ошибка create_user: {e}")
        return False, cls.use_no_limit_plus_branch, f'Remnawave: создание ({type(e).__name__})'

    if not created:
        logger.error(f"[REVOKE] create_user вернул None для {telegram_id}")
        return False, cls.use_no_limit_plus_branch, 'Remnawave вернул пустой ответ при создании'

    logger.success(
        f"[REVOKE] Remnawave: новый {new_uuid}, "
        f"days={days_left}, gb={traffic_to_set_gb}, "
        f"squads={target_squad_uuid or '(default)'}, "
        f"no_limit_plus={cls.use_no_limit_plus_branch}"
    )
    return True, cls.use_no_limit_plus_branch, None


async def _send_user_notification(telegram_id: int, new_uuid: str) -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo  # noqa: WPS433

    await app_conf.load_settings()
    connect_url = app_conf.get('sub_page_url', 'https://example.com')
    full_url = f"{connect_url.rstrip('/')}/sub/{new_uuid}"

    text = app_conf.get(
        'text_subscription_revoke',
        '⚠️ <b>Данные для подключения вашей подписки были сброшены</b>\n\n'
        'Это могло произойти из-за:\n'
        '• Нарушения правил сервиса\n'
        '• Вашего запроса на сброс\n\n'
        '🔑 <b>Новые данные для подключения готовы</b>\n'
        '📱 Лимит устройств и срок подписки не изменились\n'
        '📊 Использованный трафик сохранён\n\n'
        '⚠️ <b>Важно:</b> После добавления новой подписки обязательно удалите старую '
        'подписку из вашего клиента.\n\n'
        'Нажмите кнопку ниже, чтобы получить новые данные для подключения.',
    )

    # Кнопка «Подключиться» наследует настройки из реестра btn_connect:
    # текст, стиль, premium-эмодзи и режим открытия (url/webapp).
    # Тумблер группы «Подключение» здесь игнорируем — нотификация после
    # revoke обязательно должна давать пользователю способ открыть новую ссылку.
    connect_button = connect_btn('btn_connect', target_url=full_url, respect_group_toggle=False)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [connect_button],
        [btn('btn_back_to_main', callback_data='back_to_main')],
    ])
    await send_telegram_message(
        telegram_id, text, reply_markup=keyboard, disable_web_page_preview=True,
    )


async def revoke_subscription(
    telegram_id: int,
    *,
    on_event: Optional[OnEvent] = None,
    skip_preflight: bool = False,
) -> RevokeResult:
    """Главный публичный метод: pre-flight + 6 шагов revoke."""
    # ── 0. Pre-flight ────────────────────────────────────────────────
    await _safe_emit(on_event, 0, 'Проверка инфраструктуры…')
    pre = await preflight_check(telegram_id) if not skip_preflight else None
    if pre is not None:
        await _safe_emit(on_event, 0,
                         message=('Проверка пройдена' if pre.ok else None),
                         error=(pre.blocking_reason if not pre.ok else None),
                         extra={'preflight': pre.to_dict()})
        if not pre.ok:
            return RevokeResult(ok=False, new_uuid=None, old_uuid=None,
                                error=pre.blocking_reason or 'pre-flight failed')

    # ── 1. Чтение пользователя ───────────────────────────────────────
    await _safe_emit(on_event, 1, 'Получение данных пользователя…')
    user = await _load_user_for_revoke(telegram_id)
    if not user:
        await _safe_emit(on_event, 1, error='Пользователь не найден')
        return RevokeResult(ok=False, new_uuid=None, old_uuid=None, error='Пользователь не найден')

    old_uuid = user.get('xui_client_uuid')
    if not old_uuid:
        await _safe_emit(on_event, 1, error='У пользователя нет UUID для отзыва')
        return RevokeResult(ok=False, new_uuid=None, old_uuid=None, error='Нет UUID')

    user_email = user.get('xui_client_email')
    db_username = user.get('username') or f'tg{telegram_id}'
    limit_ip = int(user.get('limit_ip') or 0)
    subscription_end_date = user.get('subscription_end_date')

    # ── 2. Новый UUID ────────────────────────────────────────────────
    await _safe_emit(on_event, 2, 'Генерация нового UUID…')
    new_uuid = str(uuid_lib.uuid4()).lower()
    logger.info(f"[REVOKE] {telegram_id}: {old_uuid} → {new_uuid}")

    # ── 3. Remnawave ─────────────────────────────────────────────────
    await _safe_emit(on_event, 3, 'Обновление Remnawave…')
    ok_rw, used_no_limit_plus, rw_err = await _recreate_remnawave_user(
        telegram_id=telegram_id,
        old_uuid=old_uuid,
        new_uuid=new_uuid,
        user_email=user_email,
        db_username=db_username,
        limit_ip=limit_ip,
        subscription_end_date=subscription_end_date,
        on_event=on_event,
    )
    if not ok_rw:
        await _safe_emit(on_event, 3, error=rw_err or 'Remnawave: неизвестная ошибка')
        return RevokeResult(
            ok=False, new_uuid=None, old_uuid=old_uuid,
            used_no_limit_plus_branch=used_no_limit_plus,
            error=rw_err,
        )
    await _safe_emit(on_event, 3,
                     message=('Remnawave обновлён (No Limit+: безлимит)'
                              if used_no_limit_plus else 'Remnawave обновлён'))

    # ── 4. БД ─────────────────────────────────────────────────────────
    await _safe_emit(on_event, 4, 'Обновление базы данных…')
    try:
        await asyncio.wait_for(
            async_execute_db(
                "UPDATE users SET xui_client_uuid = ?, remnawave_short_uuid = ? "
                "WHERE telegram_id = ?",
                (new_uuid, new_uuid, telegram_id),
            ),
            timeout=10.0,
        )
        await _safe_emit(on_event, 4, 'База данных обновлена')
    except asyncio.TimeoutError:
        await _safe_emit(on_event, 4, error='Таймаут обновления БД')
        return RevokeResult(ok=False, new_uuid=None, old_uuid=old_uuid, error='БД: timeout')
    except Exception as e:
        logger.exception(f"[REVOKE] Ошибка обновления БД: {e}")
        await _safe_emit(on_event, 4, error=f'Ошибка БД: {e}')
        return RevokeResult(ok=False, new_uuid=None, old_uuid=old_uuid, error=f'БД: {e}')

    # ── 5. Уведомление в Telegram ────────────────────────────────────
    await _safe_emit(on_event, 5, 'Отправка уведомления пользователю…')
    try:
        await _send_user_notification(telegram_id, new_uuid)
    except Exception as e:
        logger.error(f"[REVOKE] Не удалось отправить уведомление {telegram_id}: {e}")
        # не валим процесс

    await _safe_emit(on_event, 5, complete=True, extra={
        'new_uuid': new_uuid,
        'old_uuid': old_uuid,
        'used_no_limit_plus_branch': used_no_limit_plus,
    })

    return RevokeResult(
        ok=True,
        new_uuid=new_uuid,
        old_uuid=old_uuid,
        used_no_limit_plus_branch=used_no_limit_plus,
    )
