import asyncio
from datetime import timezone
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import pytz

from app_config import app_conf
import db_helpers
import keyboards
from button_helpers import btn, connect_btn
from subscription_manager import grant_subscription
from loguru import logger


def _trial_steps(trial_days: int) -> list[str]:
    return [
        "⏳ <b>Подготовка доступа</b>\n\n⬜ Инициализация\n⬜ Подготовка серверов\n⬜ Активация",
        "🌐 <b>Подготовка доступа</b>\n\n✅ Инициализация\n⏳ Подготовка серверов...\n⬜ Активация",
        f"🔧 <b>Подготовка доступа</b>\n\n✅ Инициализация\n✅ Подготовка серверов\n⏳ Активация на {trial_days} дн...",
    ]


async def show_trial_progress(message: Message, trial_days: int) -> None:
    """Отправляет новое сообщение и анимирует прогресс выдачи пробного доступа."""
    try:
        msg = await message.answer(_trial_steps(trial_days)[0], parse_mode="HTML")
        for step in _trial_steps(trial_days)[1:]:
            await asyncio.sleep(1.2)
            await msg.edit_text(step, parse_mode="HTML")
    except Exception:
        pass


async def show_trial_progress_edit(message: Message, trial_days: int) -> None:
    """Редактирует существующее сообщение и анимирует прогресс выдачи пробного доступа."""
    try:
        steps = _trial_steps(trial_days)
        await message.edit_text(steps[0], parse_mode="HTML")
        for step in steps[1:]:
            await asyncio.sleep(1.2)
            await message.edit_text(step, parse_mode="HTML")
    except Exception:
        pass


def register_subscription_handlers(dp: Dispatcher, check_user_blocked_func, send_blocked_message_func, show_main_menu_func=None):
    """Регистрация обработчиков подписок
    
    Args:
        dp: Dispatcher для регистрации обработчиков
        check_user_blocked_func: Функция проверки блокировки пользователя
        send_blocked_message_func: Функция отправки сообщения о блокировке
        show_main_menu_func: Функция показа главного меню (опционально)
    """
    
    @dp.callback_query(F.data == "admin_renew_subscription_free")
    async def cq_admin_renew_subscription_free(query: CallbackQuery):
        """Обработчик кнопки продления подписки бесплатно (из веб-админки)"""
        # Проверяем блокировку
        if await check_user_blocked_func(query.from_user.id):
            await send_blocked_message_func(query.from_user.id, query)
            return
        
        user_id = query.from_user.id
        
        try:
            # Проверяем, использовал ли пользователь уже бесплатное продление
            if await db_helpers.is_free_renewal_used(user_id):
                # Пользователь уже получил бесплатные дни - показываем главное меню с сообщением
                await query.answer("ℹ️ Вы уже получили бесплатные дни", show_alert=True)
                
                # Если передана функция show_main_menu, используем её
                if show_main_menu_func:
                    # Сначала показываем сообщение, затем главное меню
                    try:
                        await query.message.edit_text(
                            "ℹ️ <b>Вы уже получили бесплатные дни</b>\n\nВы уже использовали возможность бесплатного продления подписки.",
                            parse_mode="HTML"
                        )
                        # Небольшая задержка для отображения сообщения перед показом главного меню
                        import asyncio
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.warning(f"Ошибка при редактировании сообщения перед показом главного меню: {e}")
                    
                    # Показываем главное меню (оно заменит сообщение)
                    await show_main_menu_func(query, edit_message=True)
                else:
                    # Fallback: просто показываем сообщение с кнопкой "Назад"
                    await query.message.edit_text(
                        "ℹ️ <b>Вы уже получили бесплатные дни</b>\n\nВы уже использовали возможность бесплатного продления подписки.",
                        reply_markup=keyboards.get_back_to_main_keyboard(),
                        parse_mode="HTML"
                    )
                return
            
            # Получаем текущую подписку пользователя
            last_sub = await db_helpers.get_last_subscription(user_id)
            if not last_sub or not last_sub.get('xui_client_uuid'):
                await query.answer("❌ У вас нет активной подписки для продления.", show_alert=True)
                return
            
            # УСТАНОВКА ФЛАГА СРАЗУ ПРИ НАЖАТИИ - чтобы предотвратить множественные попытки
            # Если продление не удастся, флаг уже будет установлен (пользователь использовал попытку)
            await db_helpers.mark_free_renewal_used(user_id)
            logger.info(f"Флаг free_renewal_used установлен для пользователя {user_id} перед попыткой продления")
            
            # Получаем текущий лимит IP пользователя
            current_limit_ip = last_sub.get('limit_ip', 0) or 0
            
            # Получаем количество дней для бесплатного продления из настроек
            free_renewal_days = int(app_conf.get('free_renewal_days', 3))
            
            # Продлеваем подписку
            await query.message.edit_text("⏳ Продлеваем подписку...", reply_markup=None)
            
            subscription_data = await grant_subscription(
                user_id=user_id,
                days_to_add=free_renewal_days,
                is_trial=True,  # Помечаем как пробный период
                limit_ip=current_limit_ip  # Сохраняем текущий лимит
            )
            
            if not subscription_data:
                await query.message.edit_text(
                    "❌ <b>Ошибка продления подписки</b>\n\nНе удалось продлить подписку. Попробуйте позже или обратитесь в поддержку.",
                    reply_markup=keyboards.get_back_to_main_keyboard(),
                    parse_mode="HTML"
                )
                await query.answer("Ошибка продления", show_alert=True)
                return
            
            # Формируем сообщение об успехе
            try:
                moscow = pytz.timezone('Europe/Moscow')
                expiry_date = subscription_data['expiry_date']
                if expiry_date.tzinfo is None:
                    expiry_date = expiry_date.replace(tzinfo=timezone.utc)
                local_expiry_date = expiry_date.astimezone(moscow)
                
                months_ru = {
                    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
                    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
                }
                formatted_expiry = f"{local_expiry_date.day} {months_ru.get(local_expiry_date.month, '')} {local_expiry_date.year} г."
            except Exception:
                formatted_expiry = subscription_data['expiry_date'].strftime('%d.%m.%Y') if hasattr(subscription_data['expiry_date'], 'strftime') else ""
            
            limit_ip_display = "∞" if current_limit_ip == 0 else str(current_limit_ip)
            
            # Получаем UUID клиента для формирования ссылки Mini App (как в keyboards.py)
            # ВСЕГДА берем xui_client_uuid из БД, так как это основной идентификатор для страницы подписки
            sub_uuid = None
            try:
                # Получаем последнюю подписку из БД
                last_sub_db = await db_helpers.get_last_subscription(user_id)
                if last_sub_db and last_sub_db.get('xui_client_uuid'):
                    sub_uuid = last_sub_db['xui_client_uuid']
            except Exception as e:
                logger.warning(f"Не удалось получить xui_client_uuid из БД для пользователя {user_id}: {e}")
            
            success_text = (
                f"✅ <b>Ваша подписка успешно продлена!</b>\n\n"
                f"⏱ <b>Длительность:</b> {free_renewal_days} дней\n"
                f"📱 <b>Лимит устройств:</b> {limit_ip_display}\n"
                f"📅 <b>Действует до:</b> {formatted_expiry}\n\n"
                f"<blockquote>Нажмите: <b>\"🔗 Подключиться\"</b> ниже, чтобы мгновенно получить доступ.</blockquote>"
            )
            
            # Кнопка подключения (Mini App или URL — берётся из настроек btn_connect)
            # и «Назад в главное меню». Стили/иконки/режим — из button_registry.
            # respect_group_toggle=False — это нотификация об успехе, кнопка
            # подключения обязательна даже если группа «Подключение» скрыта.
            connect_url = app_conf.get('sub_page_url', 'https://vpn.example.com')
            if sub_uuid:
                full_url = f"{connect_url.rstrip('/')}/sub/{sub_uuid}"
            else:
                full_url = f"{connect_url.rstrip('/')}/sub/"
                logger.warning(f"UUID не найден для пользователя {user_id}, используется общая страница подключения")

            connect_button = connect_btn('btn_connect', target_url=full_url, respect_group_toggle=False)
            back_button = btn('btn_back_to_main', callback_data='back_to_main')

            buttons = []
            if connect_button is not None:
                buttons.append([connect_button])
            buttons.append([back_button])

            reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
            
            await query.message.edit_text(
                success_text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            await query.answer("✅ Подписка успешно продлена!", show_alert=True)
            
            # Флаг уже установлен в начале функции для предотвращения множественных попыток
            
        except Exception as e:
            logger.error(f"Ошибка при продлении подписки для {user_id}: {e}", exc_info=True)
            await query.message.edit_text(
                "❌ <b>Ошибка продления подписки</b>\n\nПроизошла ошибка при продлении подписки. Попробуйте позже или обратитесь в поддержку.",
                reply_markup=keyboards.get_back_to_main_keyboard(),
                parse_mode="HTML"
            )
            await query.answer("Ошибка", show_alert=True)

