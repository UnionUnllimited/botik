import asyncio
import random
import re
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from loguru import logger

from app_config import app_conf
import db_helpers
from remnawave_manager import remnawave_manager_instance


class RemnawaveUserNotFound(Exception):
    """Пользователь есть в БД, но отсутствует в Remnawave (без пересоздания)."""


# Блокировки на уровне пользователя для предотвращения race condition при создании/продлении подписки
_user_locks: Dict[int, asyncio.Lock] = {}
_user_locks_lock = asyncio.Lock()
_user_locks_last_used: Dict[int, float] = {}
_MAX_LOCKS_CACHE_SIZE = 10000


def _gen_partner_code(telegram_id: int) -> str:
    postfix = ''.join(random.choice(string.ascii_lowercase) for _ in range(5))
    return f"{telegram_id}{postfix}"


async def _ensure_partner_code(user_id: int) -> None:
    """Генерирует и сохраняет partner_ref_code если у пользователя его ещё нет."""
    try:
        async with db_helpers.get_db_connection_safe() as db:
            row = await (await db.execute(
                "SELECT partner_ref_code FROM users WHERE telegram_id = ?", (user_id,)
            )).fetchone()
            if row and row[0]:
                return
            for _ in range(10):
                code = _gen_partner_code(user_id)
                existing = await (await db.execute(
                    "SELECT 1 FROM users WHERE partner_ref_code = ?", (code,)
                )).fetchone()
                if not existing:
                    await db.execute(
                        "UPDATE users SET partner_ref_code = ? WHERE telegram_id = ?", (code, user_id)
                    )
                    await db.commit()
                    logger.info(f"[PARTNER] Партнёрский код '{code}' создан для {user_id}")
                    return
    except Exception as e:
        logger.error(f"[PARTNER] Ошибка генерации кода для {user_id}: {e}")


async def _get_user_lock(user_id: int) -> asyncio.Lock:
    import time
    current_time = time.time()

    async with _user_locks_lock:
        if len(_user_locks) > _MAX_LOCKS_CACHE_SIZE:
            cutoff_time = current_time - 3600
            to_remove = [
                uid for uid, last_used in _user_locks_last_used.items()
                if last_used < cutoff_time and not _user_locks[uid].locked()
            ]
            for uid in to_remove:
                _user_locks.pop(uid, None)
                _user_locks_last_used.pop(uid, None)
            if to_remove:
                logger.debug(f"[LOCKS] Очищено {len(to_remove)} неиспользуемых блокировок")

        if user_id not in _user_locks:
            _user_locks[user_id] = asyncio.Lock()
        _user_locks_last_used[user_id] = current_time
        return _user_locks[user_id]


def _is_remnawave_configured() -> bool:
    return bool(
        app_conf.get('remnawave_enabled', False)
        and app_conf.get('remnawave_base_url')
        and app_conf.get('remnawave_api_token')
    )


def _normalize_expiry(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prepare_uuid_and_identity(user_id: int, user_db_data) -> tuple[str, str, str]:
    """Возвращает (uuid, email, remnawave_username)."""
    generated_uuid = None
    if user_db_data:
        if user_db_data.get('remnawave_short_uuid'):
            generated_uuid = str(user_db_data['remnawave_short_uuid']).strip().lower()
        elif user_db_data.get('xui_client_uuid'):
            generated_uuid = str(user_db_data['xui_client_uuid']).strip().lower()

    if not generated_uuid:
        generated_uuid = str(uuid.uuid4()).lower()
        logger.info(f"[GRANT] Сгенерирован новый UUID для {user_id}: {generated_uuid}")

    if user_db_data and user_db_data.get('xui_client_email'):
        xui_client_email = user_db_data['xui_client_email']
    else:
        random_suffix = ''.join(random.choices('abcdef0123456789', k=6))
        email_domain = app_conf.get('email_domain', 'vpn.bot')
        xui_client_email = f"tg{user_id}_{random_suffix}@{email_domain}"

    remnawave_username = f"tg{user_id}"
    if xui_client_email and '@' in xui_client_email:
        email_username = xui_client_email.split('@')[0]
        if re.compile(r'^[a-zA-Z0-9_-]+$').match(email_username):
            remnawave_username = email_username

    return generated_uuid, xui_client_email, remnawave_username


async def _renew_remnawave_subscription(
    user_id: int,
    user_data,
    days_to_add: int,
    is_trial: bool,
    limit_ip: int,
    reset_traffic_on_renewal: bool,
    traffic_gb_to_add: int,
    current_expiry_from_db: Optional[datetime],
    *,
    fetch_sub_link: bool = True,
    recreate_if_missing: bool = True,
) -> Optional[Dict]:
    remnawave_uuid = user_data['xui_client_uuid']
    logger.info(f"Продление подписки Remnawave для {user_id} на {days_to_add} дней.")

    auto_traffic_renewal_enabled = app_conf.get('remnawave_auto_traffic_renewal_enabled', '1') == '1'
    default_traffic_limit_gb = app_conf.get('remnawave_default_traffic_limit_gb', 0)
    try:
        default_traffic_limit_gb = int(default_traffic_limit_gb) if default_traffic_limit_gb else 0
    except (ValueError, TypeError):
        default_traffic_limit_gb = 0

    traffic_to_add_gb = traffic_gb_to_add if traffic_gb_to_add > 0 else None
    if traffic_to_add_gb:
        logger.info(f"[GRANT] Будет добавлено {traffic_gb_to_add} GB при продлении подписки для {user_id}")

    apply_default_squad = not is_trial and reset_traffic_on_renewal
    updated_user_data = None
    try:
        updated_user_data = await remnawave_manager_instance.update_user_subscription(
            user_uuid=remnawave_uuid,
            days_to_add=days_to_add,
            traffic_limit_gb=None,
            traffic_to_add_gb=traffic_to_add_gb,
            current_expiry=current_expiry_from_db,
            apply_default_squad=apply_default_squad,
        )
    except Exception as e:
        logger.warning(f"[GRANT] Не удалось обновить подписку Remnawave для {user_id}: {e}")

    if not updated_user_data:
        if not recreate_if_missing:
            logger.warning(
                f"[GRANT] Пользователь {user_id} не найден в Remnawave (UUID {remnawave_uuid}), пропуск"
            )
            raise RemnawaveUserNotFound(user_id)
        logger.info(f"[GRANT] Пользователь {user_id} не найден в Remnawave, создаём заново с UUID {remnawave_uuid}")
        if not _is_remnawave_configured():
            logger.error(f"[GRANT] Remnawave не настроен, невозможно восстановить пользователя {user_id}")
            return None
        try:
            if traffic_gb_to_add > 0:
                traffic_limit_gb = traffic_gb_to_add
            else:
                traffic_limit_gb = app_conf.get('remnawave_default_traffic_limit_gb', 0)
                try:
                    traffic_limit_gb = int(traffic_limit_gb) if traffic_limit_gb else 0
                except (ValueError, TypeError):
                    traffic_limit_gb = 0

            internal_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
            if user_data.get('remnawave_username'):
                remnawave_username = user_data['remnawave_username']
            else:
                remnawave_username = f"tg{user_id}"

            now_utc = datetime.now(timezone.utc)
            if current_expiry_from_db and current_expiry_from_db > now_utc:
                base_expiry_time = current_expiry_from_db
            else:
                base_expiry_time = now_utc

            updated_user_data = await remnawave_manager_instance.create_user(
                telegram_id=user_id,
                username=remnawave_username,
                days_valid=days_to_add,
                total_gb=traffic_limit_gb,
                description=f"Telegram ID: {user_id}",
                internal_squad_uuid=internal_squad_uuid,
                short_uuid=remnawave_uuid,
                user_uuid=remnawave_uuid,
                base_expiry=base_expiry_time,
            )
            if not updated_user_data or not updated_user_data.get("uuid"):
                logger.error(f"[GRANT] Не удалось восстановить пользователя {user_id} в Remnawave")
                return None
            logger.success(f"[GRANT] Пользователь {user_id} восстановлен в Remnawave с UUID {remnawave_uuid}")
        except Exception as create_error:
            logger.error(f"[GRANT] Ошибка восстановления пользователя {user_id} в Remnawave: {create_error}")
            return None

    if (
        updated_user_data
        and auto_traffic_renewal_enabled
        and default_traffic_limit_gb > 0
        and reset_traffic_on_renewal
        and traffic_gb_to_add == 0
    ):
        try:
            await remnawave_manager_instance.reset_user_traffic(remnawave_uuid)
        except Exception as e:
            logger.error(f"[GRANT] Ошибка сброса трафика для {user_id}: {e}")

    remnawave_expiry_date = datetime.fromtimestamp(
        updated_user_data["expiry_timestamp_ms"] / 1000, tz=timezone.utc
    )

    if user_data.get('xui_client_email'):
        email_to_save = user_data['xui_client_email']
    else:
        email_to_save = f"tg{user_id}_{''.join(random.choices('abcdef0123456789', k=6))}@{app_conf.get('email_domain', 'vpn.bot')}"

    remnawave_username = (
        user_data['remnawave_username']
        if user_data.get('remnawave_username')
        else updated_user_data["username"]
    )
    has_existing_uuid = bool(user_data.get('xui_client_uuid'))
    remnawave_short_uuid_to_save = (
        str(user_data['xui_client_uuid']).lower()
        if has_existing_uuid
        else updated_user_data.get("short_uuid")
    )

    await db_helpers.update_user_subscription_remnawave(
        telegram_id=user_id,
        remnawave_user_uuid=remnawave_uuid,
        remnawave_username=remnawave_username,
        remnawave_short_uuid=remnawave_short_uuid_to_save,
        subscription_end_date=remnawave_expiry_date,
        is_trial=is_trial,
        preserve_xui_uuid=has_existing_uuid,
        limit_ip=limit_ip,
    )

    async with db_helpers.get_db_connection_safe() as db:
        await db.execute(
            "UPDATE users SET xui_client_email = ? WHERE telegram_id = ?",
            (email_to_save, user_id),
        )
        await db.commit()

    remnawave_sub_link = None
    if fetch_sub_link:
        remnawave_sub_link = await remnawave_manager_instance.get_subscription_link(
            updated_user_data["short_uuid"]
        )
    await _ensure_partner_code(user_id)

    return {
        "expiry_date": remnawave_expiry_date,
        "sub_link": remnawave_sub_link,
        "remnawave_user_uuid": remnawave_uuid,
        "remnawave_short_uuid": remnawave_short_uuid_to_save,
        "provider": "remnawave",
    }


async def _create_remnawave_subscription(
    user_id: int,
    user_db_data,
    days_to_add: int,
    is_trial: bool,
    limit_ip: int,
    traffic_gb_to_add: int,
) -> Optional[Dict]:
    if not _is_remnawave_configured():
        logger.error(f"[GRANT] Remnawave не настроен, невозможно создать подписку для {user_id}")
        return None

    generated_uuid, xui_client_email, remnawave_username = _prepare_uuid_and_identity(user_id, user_db_data)
    base_expiry_time = datetime.now(timezone.utc)

    if traffic_gb_to_add > 0:
        traffic_limit_gb = traffic_gb_to_add
    else:
        traffic_limit_gb = app_conf.get('remnawave_default_traffic_limit_gb', 0)
        try:
            traffic_limit_gb = int(traffic_limit_gb) if traffic_limit_gb else 0
        except (ValueError, TypeError):
            traffic_limit_gb = 0

    internal_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
    logger.info(f"[GRANT] Создание пользователя Remnawave для {user_id}, UUID={generated_uuid}")

    remnawave_user_data = await remnawave_manager_instance.create_user(
        telegram_id=user_id,
        username=remnawave_username,
        days_valid=days_to_add,
        total_gb=traffic_limit_gb,
        description=f"Telegram ID: {user_id}",
        internal_squad_uuid=internal_squad_uuid,
        short_uuid=generated_uuid,
        user_uuid=generated_uuid,
        base_expiry=base_expiry_time,
    )

    if not remnawave_user_data or not remnawave_user_data.get("uuid"):
        logger.error(f"[GRANT] Не удалось создать пользователя Remnawave для {user_id}")
        return None

    remnawave_expiry_date_dt = datetime.fromtimestamp(
        remnawave_user_data["expiry_timestamp_ms"] / 1000, tz=timezone.utc
    )

    await db_helpers.update_user_subscription(
        telegram_id=user_id,
        xui_client_uuid=generated_uuid,
        xui_client_email=xui_client_email,
        subscription_end_date=remnawave_expiry_date_dt,
        server_id=0,
        is_trial=is_trial,
        limit_ip=limit_ip,
    )
    await db_helpers.update_user_subscription_remnawave(
        telegram_id=user_id,
        remnawave_user_uuid=generated_uuid,
        remnawave_username=remnawave_username,
        remnawave_short_uuid=generated_uuid,
        subscription_end_date=remnawave_expiry_date_dt,
        is_trial=is_trial,
        preserve_xui_uuid=False,
        limit_ip=limit_ip,
    )

    remnawave_sub_link = await remnawave_manager_instance.get_subscription_link(generated_uuid)
    await _ensure_partner_code(user_id)
    logger.success(f"[GRANT] Подписка Remnawave создана для {user_id}")

    return {
        "expiry_date": remnawave_expiry_date_dt,
        "sub_link": remnawave_sub_link,
        "remnawave_user_uuid": remnawave_user_data["uuid"],
        "remnawave_short_uuid": generated_uuid,
        "provider": "remnawave",
    }


async def grant_subscription(
    user_id: int,
    days_to_add: int,
    is_trial: bool = False,
    limit_ip: int = 0,
    reset_traffic_on_renewal: bool = True,
    traffic_gb_to_add: int = 0,
    *,
    fetch_sub_link: bool = True,
    recreate_if_missing: bool = True,
) -> Optional[Dict]:
    user_lock = await _get_user_lock(user_id)

    async with user_lock:
        user_db_data = await db_helpers.get_user(user_id)
        if user_db_data and user_db_data['is_blocked']:
            logger.warning(f"Попытка выдать подписку заблокированному пользователю {user_id}")
            return None

        user_data = await db_helpers.get_last_subscription(user_id)
        current_expiry_from_db = _normalize_expiry(
            user_data.get('subscription_end_date') if user_data else None
        )

        if user_data and user_data.get('xui_client_uuid'):
            return await _renew_remnawave_subscription(
                user_id=user_id,
                user_data=user_data,
                days_to_add=days_to_add,
                is_trial=is_trial,
                limit_ip=limit_ip,
                reset_traffic_on_renewal=reset_traffic_on_renewal,
                traffic_gb_to_add=traffic_gb_to_add,
                current_expiry_from_db=current_expiry_from_db,
                fetch_sub_link=fetch_sub_link,
                recreate_if_missing=recreate_if_missing,
            )

        logger.info(f"Создание новой подписки Remnawave для {user_id} на {days_to_add} дней.")
        return await _create_remnawave_subscription(
            user_id=user_id,
            user_db_data=user_db_data,
            days_to_add=days_to_add,
            is_trial=is_trial,
            limit_ip=limit_ip,
            traffic_gb_to_add=traffic_gb_to_add,
        )


async def get_remnawave_subscription_config(user_id: int) -> Optional[Dict[str, Any]]:
    """Получает конфигурацию подписки Remnawave для пользователя."""
    user_data = await db_helpers.get_user(user_id)
    if not user_data or not user_data.get('xui_client_uuid'):
        return None

    remnawave_uuid = user_data['xui_client_uuid']
    await remnawave_manager_instance._ensure_initialized()
    sdk = remnawave_manager_instance.sdk

    try:
        user_by_uuid = await sdk.users.get_user_by_uuid(remnawave_uuid)
        if not user_by_uuid:
            return None
        return await remnawave_manager_instance.get_raw_subscription(user_by_uuid.short_uuid)
    except Exception as e:
        logger.error(f"Ошибка получения подписки Remnawave по UUID {remnawave_uuid}: {e}")
        return None
