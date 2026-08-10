import asyncio

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from app_config import app_conf
from button_helpers import btn
import db_helpers


async def notify_expiring_subscriptions(bot: Bot) -> None:
    """
    Периодически проверяет пользователей, у которых подписка заканчивается через 1 день,
    и отправляет им напоминание в Telegram с кнопкой продления.
    """
    while True:
        try:
            users = await db_helpers.get_users_with_expiring_subscriptions(days_before=1)
            logger.info(f"Найдено {len(users)} пользователей с подпиской, заканчивающейся завтра")

            for user_id in users:
                try:
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                        [btn('btn_renew_sub', callback_data='renew_choose_payment')],
                        [btn('btn_referral_free_days', callback_data='referral_program')],
                        [btn('btn_back_to_main', callback_data='back_to_main')],
                    ])

                    await bot.send_message(
                        user_id,
                        app_conf.get('text_subscription_expiring', "⏰ Ваша подписка заканчивается завтра! Не забудьте продлить, чтобы не потерять доступ."),
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )

                    try:
                        await db_helpers.mark_notified_expiring(user_id)
                        logger.info(f"Отправлено уведомление о скором окончании подписки пользователю {user_id}, флаг notified_expiring установлен")
                    except Exception as mark_error:
                        logger.error(f"Ошибка при установке флага notified_expiring для пользователя {user_id}: {mark_error}")

                except TelegramAPIError as e:
                    error_text = str(e).lower()
                    if 'chat not found' in error_text or 'bot was blocked' in error_text or 'user is deactivated' in error_text:
                        logger.warning(f"Пользователь {user_id} заблокировал бота или удален. Деактивация (в notify_expiring).")
                        await db_helpers.deactivate_user(user_id)
                    else:
                        logger.error(f'Ошибка Telegram API при отправке напоминания {user_id}: {e}')
                except Exception as e:
                    logger.error(f'Неизвестная ошибка при отправке напоминания {user_id}: {e}')

        except Exception as e:
            logger.error(f"Глобальная ошибка в задаче notify_expiring_subscriptions: {e}")

        await asyncio.sleep(4 * 60 * 60)  # Каждые 4 часа


async def notify_expired_subscriptions(bot: Bot) -> None:
    """
    Периодически (каждые 10 минут) проверяет пользователей, у которых подписка истекла,
    и отправляет им уведомление, если оно не было отправлено ранее.
    """
    while True:
        try:
            users = await db_helpers.get_users_with_expired_subscriptions()

            for user_id in users:
                try:
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                        [btn('btn_renew_sub', callback_data='renew_choose_payment')],
                        [btn('btn_referral_free_days', callback_data='referral_program')],
                        [btn('btn_back_to_main', callback_data='back_to_main')],
                    ])

                    await bot.send_message(
                        user_id,
                        app_conf.get('text_subscription_expired', "😔 Ваша подписка истекла. Чтобы возобновить доступ, пожалуйста, продлите ее."),
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )

                    await db_helpers.mark_notified_expired(user_id)
                    logger.info(f"Отправлено уведомление об истечении подписки пользователю {user_id}")

                except TelegramAPIError as e:
                    error_text = str(e).lower()
                    if 'chat not found' in error_text or 'bot was blocked' in error_text or 'user is deactivated' in error_text:
                        logger.warning(f"Пользователь {user_id} заблокировал бота или удален. Деактивация (в notify_expired).")
                        await db_helpers.deactivate_user(user_id)
                    else:
                        logger.error(f'Ошибка Telegram API при отправке уведомления об истечении {user_id}: {e}')
                except Exception as e:
                    logger.error(f'Неизвестная ошибка при отправке уведомления об истечении {user_id}: {e}')

        except Exception as e:
            logger.error(f"Глобальная ошибка в задаче notify_expired_subscriptions: {e}")

        await asyncio.sleep(600)  # Каждые 10 минут


def start_notification_tasks(bot: Bot) -> None:
    """Запускает фоновые задачи уведомлений. Вызывать после старта event loop."""
    asyncio.create_task(notify_expiring_subscriptions(bot))
    asyncio.create_task(notify_expired_subscriptions(bot))
