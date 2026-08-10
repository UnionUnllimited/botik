"""
Модуль для управления лимитом устройств пользователей.

Меняет ТОЛЬКО значение в БД (колонка users.limit_ip).
До X-UI/Remnawave изменение не доносится — лимит будет применён
автоматически при следующем продлении/обновлении подписки.
"""
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


async def update_user_device_limit(
    telegram_id: int,
    limit_ip: int,
    xui_client_uuid: Optional[str] = None,  # оставлен для обратной совместимости, не используется
    servers: Optional[list] = None,         # оставлен для обратной совместимости, не используется
) -> Tuple[bool, Optional[str]]:
    """
    Обновляет лимит устройств для пользователя в БД.

    Args:
        telegram_id: Telegram ID пользователя
        limit_ip: Новый лимит устройств (0 = без лимита)

    Returns:
        Tuple[bool, Optional[str]]: (успех, сообщение об ошибке/предупреждение)
    """
    try:
        from web_admin.async_db import async_execute_db, async_query_db

        if limit_ip < 0:
            return False, "Лимит устройств не может быть отрицательным"

        # Текущее значение для записи в историю
        cur_row = await async_query_db(
            "SELECT limit_ip FROM users WHERE telegram_id = ?",
            (telegram_id,), one=True,
        )
        old_limit = int(dict(cur_row)['limit_ip']) if cur_row and dict(cur_row).get('limit_ip') is not None else 0

        await async_execute_db(
            "UPDATE users SET limit_ip = ? WHERE telegram_id = ?",
            (limit_ip, telegram_id),
        )

        # Запись в историю изменений (для аудита и отчётов)
        if old_limit != limit_ip:
            try:
                await async_execute_db(
                    "INSERT INTO device_limit_changes (telegram_id, old_limit, new_limit, source, payment_id, reason) "
                    "VALUES (?, ?, ?, 'admin', NULL, 'manual')",
                    (telegram_id, old_limit, limit_ip),
                )
            except Exception as audit_err:
                logger.warning(f"[LIMIT] Не удалось записать в device_limit_changes: {audit_err}")

        logger.info(
            f"[LIMIT] limit_ip пользователя {telegram_id} обновлён в БД: {old_limit} -> {limit_ip}"
        )
        return True, None

    except Exception as e:
        logger.error(
            f"[LIMIT] Ошибка обновления лимита устройств для {telegram_id}: {e}",
            exc_info=True,
        )
        return False, str(e)
