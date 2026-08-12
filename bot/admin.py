import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
 
 

from app_config import app_conf # Главный импорт
import db_helpers
import re
import aiosqlite
from config import DATABASE_NAME as ROOT_DB_PATH
 
from loguru import logger

class AdminStates(StatesGroup):
    waiting_for_broadcast_filter = State()
    waiting_for_broadcast_message = State()
    waiting_for_web_secret = State()
    waiting_for_web_password = State()

def is_admin(user_id: int) -> bool:
    from src.core.utils import is_bot_admin
    return is_bot_admin(user_id)

def get_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔁 Перезапуск сервисов", callback_data="admin_services_menu")
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ Настройки Веб‑админки", callback_data="admin_web_settings")
    )
    # Ссылка на веб‑админку (из настроек connect_page_url + admin_secret_path)
    try:
        base = (app_conf.get('connect_page_url', '') or '').strip().rstrip('/')
        secret = (app_conf.get('admin_secret_path', '') or '').strip().strip('/')
        if base and secret:
            admin_url = f"{base}/{secret}"
            builder.row(InlineKeyboardButton(text="🛠 Веб‑админка", url=admin_url))
    except Exception:
        pass
    builder.row(
        InlineKeyboardButton(text="📢 Отправить новость", callback_data="admin_broadcast")
    )
    # Новая кнопка для перезагрузки настроек
    builder.row(
        InlineKeyboardButton(text="🔄 Перезагрузить настройки", callback_data="admin_reload_settings")
    )
    # Кнопка для возврата в пользовательское меню
    builder.row(
        InlineKeyboardButton(text="🏠 На главную", callback_data="back_to_main")
    )
    return builder.as_markup()
    
 

def get_web_admin_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔐 Изменить пароль", callback_data="admin_web_set_password"))
    builder.row(InlineKeyboardButton(text="🛡 Изменить секретный путь", callback_data="admin_web_set_secret"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel_main"))
    return builder.as_markup()


async def cmd_admin_panel(message_or_query: Message | CallbackQuery):
    user_id = message_or_query.from_user.id
    if not is_admin(user_id):
        if isinstance(message_or_query, Message):
            await message_or_query.answer("⛔️ У вас нет доступа к админ-панели.")
        elif isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer("⛔️ Нет доступа", show_alert=True)
        return
    
    text = "👨‍💼 <b>Панель администратора</b>\n\nВыберите действие:"
    kbd = get_admin_keyboard()

    if isinstance(message_or_query, Message):
        await message_or_query.answer(text, reply_markup=kbd)
    elif isinstance(message_or_query, CallbackQuery):
        try:
            await message_or_query.message.edit_text(text, reply_markup=kbd)
            await message_or_query.answer()
        except Exception as e:
            logger.debug(f"Ошибка при редактировании сообщения в cmd_admin_panel: {e}")
            await message_or_query.answer()

def register_admin_handlers(dp: Dispatcher):
    
    # --- Новая команда для перезагрузки настроек ---
    @dp.message(Command("reload_settings"))
    async def cmd_reload_settings_msg(message: Message):
        if not is_admin(message.from_user.id): return
        await app_conf.load_settings()
        await message.answer("✅ Настройки и тексты успешно перезагружены из базы данных!")

    @dp.callback_query(F.data == "admin_reload_settings")
    async def cq_reload_settings(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await app_conf.load_settings()
        await query.answer("✅ Настройки и тексты успешно перезагружены из базы данных!", show_alert=True)
        # Обновим админ-панель на всякий случай
        await cmd_admin_panel(query)
    
    @dp.message(Command("admin"))
    async def cmd_show_admin_panel_msg(message: Message):
        await cmd_admin_panel(message)

    @dp.callback_query(F.data == "admin_panel_main")
    async def cq_show_admin_panel_cb(query: CallbackQuery):
        await cmd_admin_panel(query)

    # --- Перезапуск системных сервисов ---
    async def _exec_cmd(*args: str) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            return proc.returncode or 0, (out_b or b'').decode(errors='ignore'), (err_b or b'').decode(errors='ignore')
        except Exception as e:
            return 127, "", str(e)

    async def _restart_service(service_name: str) -> str:
        rc, out, err = await _exec_cmd("systemctl", "restart", service_name)
        if rc != 0:
            # Пытаемся через sudo
            rc, out, err = await _exec_cmd("sudo", "systemctl", "restart", service_name)
        # Проверяем статус
        rc2, out2, err2 = await _exec_cmd("systemctl", "is-active", service_name)
        status = (out2 or err2).strip() if rc2 == 0 else (err2 or out2).strip()
        if rc == 0:
            return f"✅ {service_name}: restarted (status: {status or 'unknown'})"
        return f"❌ {service_name}: restart failed. {err.strip() or 'no details'} (status: {status or 'n/a'})"

    def _services_kb() -> InlineKeyboardMarkup:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🤖 Бот (vpn-bot.service)", callback_data="svc_restart_vpnbot"))
        kb.row(InlineKeyboardButton(text="🌐 Веб-админка (vpn-webadmin.service)", callback_data="svc_restart_webadmin"))
        kb.row(
            InlineKeyboardButton(text="🔄 Перезапустить все", callback_data="svc_restart_all"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel_main")
        )
        return kb.as_markup()

    @dp.callback_query(F.data == "admin_services_menu")
    async def cq_services_menu(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        text = (
            "🔁 <b>Перезапуск сервисов</b>\n\n"
            "Выберите сервис для перезапуска. Требуются права systemd на сервере."
        )
        await query.message.edit_text(text, reply_markup=_services_kb())
        await query.answer()

    @dp.callback_query(F.data.in_(
        {"svc_restart_vpnbot", "svc_restart_webadmin", "svc_restart_all"}
    ))
    async def cq_restart_service(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        action = query.data
        msgs: list[str] = []
        try:
            await query.answer("⏳ Выполняю…", show_alert=False)
        except Exception:
            pass
        if action == "svc_restart_all":
            msgs.append(await _restart_service("vpn-bot.service"))
            msgs.append(await _restart_service("vpn-webadmin.service"))
        elif action == "svc_restart_vpnbot":
            msgs.append(await _restart_service("vpn-bot.service"))
        elif action == "svc_restart_webadmin":
            msgs.append(await _restart_service("vpn-webadmin.service"))
        answer = "\n".join(msgs)
        try:
            await query.message.edit_text(
                f"🔁 <b>Перезапуск сервисов</b>\n\n{answer}",
                reply_markup=_services_kb()
            )
        except Exception:
            await query.message.answer(answer, reply_markup=_services_kb())
        await query.answer()

    # --- Настройки веб‑админки ---
    @dp.callback_query(F.data == "admin_web_settings")
    async def cq_web_settings(query: CallbackQuery):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        try:
            base = (app_conf.get('connect_page_url', '') or '').strip().rstrip('/')
            secret = (app_conf.get('admin_secret_path', '') or '').strip().strip('/')
            link = f"{base}/{secret}" if base and secret else "—"
            text = (
                "⚙️ <b>Настройки веб‑админки</b>\n\n"
                f"Текущий секретный путь: <code>/{secret or '-'}</code>\n"
                f"Ссылка: {link}"
            )
        except Exception:
            text = "⚙️ <b>Настройки веб‑админки</b>"
        await query.message.edit_text(text, reply_markup=get_web_admin_settings_keyboard())
        await query.answer()

    @dp.callback_query(F.data == "admin_web_set_secret")
    async def cq_web_set_secret(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        note = (
            "\n\nПосле смены пути поправьте обратный прокси: он проксирует"
            " старый путь на <code>127.0.0.1:8181</code>, и по новому админка"
            " снаружи не откроется."
        )
        await query.message.edit_text(
            "🛡 Введите новый секретный путь (3–64 символа, латиница/цифры/._-), без слэшей." + note,
            reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_web_settings").as_markup()
        )
        await state.set_state(AdminStates.waiting_for_web_secret)
        await query.answer()

    @dp.message(AdminStates.waiting_for_web_secret)
    async def set_web_secret(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        raw = (message.text or '').strip()
        candidate = re.sub(r"[^A-Za-z0-9._-]", "", raw)
        candidate = candidate.strip("/")
        if len(candidate) < 3 or len(candidate) > 64:
            return await message.answer("❌ Неверный формат. Требуется 3–64 символа: латиница/цифры/._-")
        ok = await db_helpers.async_update_setting('admin_secret_path', candidate)
        if ok:
            try:
                await app_conf.load_settings()
            except Exception:
                pass
            base = (app_conf.get('connect_page_url', '') or '').strip().rstrip('/')
            link = f"{base}/{candidate}" if base else f"/{candidate}"
            proxy_note = (
                "\n\nНе забудьте обратный прокси: он должен отдавать"
                f" <code>/{candidate}/</code> на <code>127.0.0.1:8181</code>,"
                " иначе снаружи админка откроется только по старому пути."
            )
            await message.answer(
                f"✅ Секретный путь обновлён: <code>/{candidate}</code>\nСсылка: {link}" + proxy_note,
                reply_markup=get_admin_keyboard()
            )
        else:
            await message.answer("❌ Не удалось сохранить. Проверьте логи.")
        await state.clear()

    @dp.callback_query(F.data == "admin_web_set_password")
    async def cq_web_set_password(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await query.message.edit_text(
            "🔐 Введите новый пароль (минимум 6 символов)",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_web_settings").as_markup()
        )
        await state.set_state(AdminStates.waiting_for_web_password)
        await query.answer()

    @dp.message(AdminStates.waiting_for_web_password)
    async def set_web_password(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        pwd = (message.text or '').strip()
        if len(pwd) < 6:
            return await message.answer("❌ Пароль слишком короткий. Минимум 6 символов.")
        ok = await db_helpers.async_update_setting('admin_web_password', pwd)
        if ok:
            try:
                await app_conf.load_settings()
            except Exception:
                pass
            await message.answer("✅ Пароль веб‑админки обновлён", reply_markup=get_admin_keyboard())
        else:
            await message.answer("❌ Не удалось сохранить. Проверьте логи.")
        await state.clear()

    # Удалены пункты "Серверы", "Диагностика" и "Статистика" из админ-меню

    # Функционал "Меню управления пользователями" удален - не используется (нет кнопки в главной админ-панели)

    @dp.message(Command("cancel"), StateFilter(AdminStates))
    async def cancel_admin_action(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        current_state = await state.get_state()
        if current_state is None: return
        logger.info(f"Админ {message.from_user.id} отменил действие в состоянии {current_state}")
        await state.clear()
        await message.answer("Действие отменено. Возврат в админ-панель.", reply_markup=get_admin_keyboard())

    @dp.callback_query(F.data == "admin_ignore")
    async def cq_admin_ignore(query: CallbackQuery):
        await query.answer()

    # Обработчик admin_user_info_ удален (функция get_user_info_text не определена)

    @dp.callback_query(F.data == "admin_broadcast")
    async def cq_admin_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        
        # Получаем количество пользователей для каждого фильтра
        active_users = await db_helpers.get_active_users_for_broadcast()
        expired_users = await db_helpers.get_expired_users_for_broadcast()
        all_users = await db_helpers.get_all_users_for_broadcast()
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text=f"✅ Активным ({len(active_users)} чел.)",
            callback_data="broadcast_filter_active"
        ))
        builder.row(InlineKeyboardButton(
            text=f"❌ Истекшим ({len(expired_users)} чел.)",
            callback_data="broadcast_filter_expired"
        ))
        builder.row(InlineKeyboardButton(
            text=f"👥 Всем ({len(all_users)} чел.)",
            callback_data="broadcast_filter_all"
        ))
        builder.row(InlineKeyboardButton(
            text="🔙 Отмена",
            callback_data="admin_cancel_broadcast"
        ))
        
        await query.message.edit_text(
            "📢 <b>Отправка новости</b>\n\n"
            "Выберите категорию получателей:\n\n"
            f"✅ Активных: {len(active_users)} чел.\n"
            f"❌ Истекших: {len(expired_users)} чел.\n"
            f"👥 Всего (с UUID): {len(all_users)} чел.\n\n"
            "<blockquote>ℹ️ <b>Примечание:</b> Клиенты с пустым UUID и заблокированные, исключаются из всех категорий.</blockquote>",
            reply_markup=builder.as_markup()
        )
        await query.answer()
    
    @dp.callback_query(F.data.startswith("broadcast_filter_"))
    async def cq_broadcast_filter(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        
        filter_type = query.data.split("_")[-1]  # active, expired, all
        await state.update_data(broadcast_filter=filter_type)
        
        filter_names = {
            "active": "активным пользователям",
            "expired": "пользователям с истекшей подпиской",
            "all": "всем пользователям (с UUID)"
        }
        
        await query.message.edit_text(
            f"📢 Отправка новости <b>{filter_names[filter_type]}</b>\n\n"
            "Пришлите текст или фото/видео/GIF с подписью. Поддерживается встроенное форматирование Telegram (жирный/курсив, спойлеры, ссылки).",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_cancel_broadcast").as_markup()
        )
        await state.set_state(AdminStates.waiting_for_broadcast_message)
        await query.answer()

    @dp.callback_query(F.data == "admin_cancel_broadcast")
    async def cq_admin_cancel_broadcast(query: CallbackQuery, state: FSMContext):
        if not is_admin(query.from_user.id): return await query.answer("⛔️ Нет доступа", show_alert=True)
        await state.clear()
        await query.message.edit_text("❌ Отправка новости отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
        await query.answer()

    @dp.message(AdminStates.waiting_for_broadcast_message)
    async def process_broadcast_message(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id): return
        if message.text == "/cancel":
            await state.clear()
            await message.answer("❌ Отправка новости отменена", reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup())
            return

        # Получаем выбранный фильтр
        data = await state.get_data()
        filter_type = data.get('broadcast_filter', 'all')
        
        # Получаем пользователей в зависимости от фильтра
        if filter_type == 'active':
            users = await db_helpers.get_active_users_for_broadcast()
            filter_name = "активным пользователям"
        elif filter_type == 'expired':
            users = await db_helpers.get_expired_users_for_broadcast()
            filter_name = "пользователям с истекшей подпиской"
        else:  # all
            users = await db_helpers.get_all_users_for_broadcast()
            filter_name = "всем пользователям (с UUID)"
        
        total_users = len(users)
        if total_users == 0:
            await message.answer(f"❌ Нет пользователей для отправки новости ({filter_name})")
            await state.clear()
            return

        status_message = await message.answer(f"📢 Начинаю отправку новости {filter_name} ({total_users} чел.)...")
        success_count, error_count = 0, 0
        # Подготавливаем клавиатуру с кнопкой назад в боте для каждого получателя
        back_kb = InlineKeyboardBuilder()
        back_kb.row(InlineKeyboardButton(text="⬅️ На главную", callback_data="back_to_main"))

        for user in users:
            user_id = user[0]
            username = user[1]
            uuid = user[2]  # Проверяем UUID еще раз на всякий случай
            # Дополнительная проверка UUID (на случай если что-то изменилось)
            if not uuid or uuid.strip() == '':
                continue
            try:
                # Копируем сообщение как есть (с форматированием, цитированием и медиа)
                await message.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=back_kb.as_markup()
                )
                success_count += 1
            except Exception as e:
                logger.error(f"Ошибка отправки новости пользователю {user_id}: {e}")
                error_count += 1
            await asyncio.sleep(0.05)
        
        await status_message.edit_text(
            f"📢 Рассылка завершена!\n\n"
            f"Категория: {filter_name}\n"
            f"✅ Успешно: {success_count}\n"
            f"❌ Ошибок: {error_count}",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 В админ-панель", callback_data="admin_panel_main").as_markup()
        )
        await state.clear()
