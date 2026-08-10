"""
Модуль синхронизации клиентов между базой данных и X-UI серверами.

Проверяет и исправляет несоответствия:
- Дата окончания подписки (expiry_time)
- Лимит устройств (limit_ip)
- Flow настройки (из настроек сервера)
- Статус включения клиента (enable)
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from loguru import logger

from x_ui_manager import xui_manager_instance
from db_helpers import get_db_connection_safe, get_user
from py3xui.client import Client


@dataclass
class ClientSyncStatus:
    """Статус синхронизации одного клиента"""
    uuid: str
    email: str
    telegram_id: Optional[int] = None
    
    # Проблемы
    expiry_mismatch: bool = False
    limit_ip_mismatch: bool = False
    flow_missing: bool = False
    disabled_when_active: bool = False
    
    # Значения из X-UI
    xui_expiry_ms: Optional[int] = None
    xui_limit_ip: Optional[int] = None
    xui_flow: Optional[str] = None
    xui_enable: Optional[bool] = None
    
    # Значения из БД
    db_expiry_ms: Optional[int] = None
    db_limit_ip: Optional[int] = None
    db_subscription_active: bool = False
    
    # Результат исправления
    fixed: bool = False
    fix_error: Optional[str] = None


@dataclass
class SyncResult:
    """Результат синхронизации сервера"""
    server_id: int
    server_name: str
    
    # Статистика
    total_clients_on_server: int = 0
    clients_checked: int = 0
    clients_fixed: int = 0
    clients_with_issues: int = 0
    clients_not_in_db: int = 0
    clients_to_add_count: int = 0
    clients_added_count: int = 0
    
    # Детали
    statuses: List[ClientSyncStatus] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    
    # Время выполнения
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0


async def sync_server_clients(
    server_settings: Dict,
    fix_issues: bool = True,
    check_flow: bool = True,
    check_expiry: bool = True,
    check_limit_ip: bool = True,
    check_enable: bool = True
) -> SyncResult:
    """
    Синхронизирует клиентов на указанном сервере с базой данных.
    
    Args:
        server_settings: Настройки сервера (из xui_servers)
        fix_issues: Исправлять ли найденные проблемы автоматически
        check_flow: Проверять ли наличие flow
        check_expiry: Проверять ли соответствие даты окончания
        check_limit_ip: Проверять ли соответствие лимита IP
        check_enable: Проверять ли статус включения для активных подписок
    
    Returns:
        SyncResult с детальной информацией о синхронизации
    """
    server_id = server_settings.get('id')
    server_name = server_settings.get('name', 'Unknown')
    
    result = SyncResult(
        server_id=server_id,
        server_name=server_name,
        start_time=datetime.now(timezone.utc)
    )
    
    sync_logger = logger.bind(server_id=server_id, server_name=server_name)
    sync_logger.info(f"[SYNC] Начало синхронизации сервера {server_name} (ID: {server_id})")
    
    try:
        # Получаем API клиент
        client_api = await xui_manager_instance.get_client(server_settings)
        if not client_api:
            result.errors.append("Не удалось подключиться к серверу")
            sync_logger.error("[SYNC] Не удалось получить API клиент")
            result.end_time = datetime.now(timezone.utc)
            return result
        
        # Получаем inbound
        inbound_id = server_settings.get('inbound_id')
        inbound = await xui_manager_instance._find_inbound_by_id(client_api, inbound_id)
        if not inbound:
            result.errors.append(f"Inbound {inbound_id} не найден")
            sync_logger.error(f"[SYNC] Inbound {inbound_id} не найден")
            result.end_time = datetime.now(timezone.utc)
            return result
        
        # Получаем всех клиентов с сервера
        if not hasattr(inbound, 'settings') or not inbound.settings:
            result.errors.append("Inbound не содержит settings")
            sync_logger.warning("[SYNC] Inbound не содержит settings")
            result.end_time = datetime.now(timezone.utc)
            return result
        
        if not hasattr(inbound.settings, 'clients') or not inbound.settings.clients:
            sync_logger.info("[SYNC] На сервере нет клиентов")
            result.end_time = datetime.now(timezone.utc)
            return result
        
        clients_on_server = inbound.settings.clients
        result.total_clients_on_server = len(clients_on_server)
        sync_logger.info(f"[SYNC] Найдено {result.total_clients_on_server} клиентов на сервере")
        
        # Получаем настройки по умолчанию из сервера
        default_flow = (server_settings.get('client_flow') or '').strip()

        # Hysteria2 не использует flow — отключаем проверку, чтобы sync не «чинил»
        # пустой flow до значения настроек сервера.
        _server_proto = (server_settings.get('protocol') or 'vless').strip().lower()
        if _server_proto in ('hysteria', 'hysteria2', 'hy2'):
            default_flow = ''
        
        # Загружаем данные пользователей из БД для быстрого поиска
        # Создаем словари: uuid -> user_data, email -> user_data
        db_users_by_uuid: Dict[str, Dict] = {}
        db_users_by_email: Dict[str, Dict] = {}
        
        async with get_db_connection_safe() as db:
            async with db.execute("""
                SELECT telegram_id, xui_client_uuid, xui_client_email, 
                       subscription_end_date, limit_ip, subscription_provider
                FROM users
                WHERE xui_client_uuid IS NOT NULL 
                AND xui_client_uuid != ''
                AND COALESCE(is_blocked, 0) = 0
            """) as cursor:
                async for row in cursor:
                    user_data = {
                        'telegram_id': row[0],
                        'xui_client_uuid': row[1],
                        'xui_client_email': row[2],
                        'subscription_end_date': row[3],
                        'limit_ip': row[4] or 0,
                        'subscription_provider': row[5]
                    }
                    
                    uuid_key = user_data['xui_client_uuid'].lower()
                    email_key = (user_data['xui_client_email'] or '').lower()
                    
                    db_users_by_uuid[uuid_key] = user_data
                    if email_key:
                        db_users_by_email[email_key] = user_data
        
        sync_logger.info(f"[SYNC] Загружено {len(db_users_by_uuid)} пользователей из БД")
        
        # Проверяем каждого клиента
        now_utc_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        
        for client in clients_on_server:
            try:
                result.clients_checked += 1
                
                # Получаем UUID клиента (может быть в id, auth или sub_id)
                # Для Hysteria2 в 3X-UI v2 идентификатор хранится в поле auth.
                client_uuid = None
                if hasattr(client, 'id') and client.id:
                    client_uuid = str(client.id)
                elif hasattr(client, 'auth') and getattr(client, 'auth', None):
                    client_uuid = str(client.auth)
                elif hasattr(client, 'sub_id') and client.sub_id:
                    client_uuid = str(client.sub_id)
                
                if not client_uuid:
                    sync_logger.warning(f"[SYNC] Клиент {getattr(client, 'email', 'unknown')} не имеет UUID")
                    result.clients_not_in_db += 1
                    continue
                
                # Получаем email
                client_email = getattr(client, 'email', '')
                if not client_email:
                    sync_logger.warning(f"[SYNC] Клиент {client_uuid} не имеет email")
                    continue
                
                # Ищем пользователя в БД
                user_data = None
                uuid_key = client_uuid.lower()
                email_key = client_email.lower()
                
                if uuid_key in db_users_by_uuid:
                    user_data = db_users_by_uuid[uuid_key]
                elif email_key in db_users_by_email:
                    user_data = db_users_by_email[email_key]
                
                if not user_data:
                    sync_logger.debug(f"[SYNC] Клиент {client_uuid} ({client_email}) не найден в БД")
                    result.clients_not_in_db += 1
                    continue
                
                # Создаем статус для этого клиента
                status = ClientSyncStatus(
                    uuid=client_uuid,
                    email=client_email,
                    telegram_id=user_data['telegram_id']
                )
                
                # Получаем значения из X-UI
                status.xui_expiry_ms = getattr(client, 'expiry_time', 0) or 0
                status.xui_limit_ip = getattr(client, 'limit_ip', 0) or 0
                status.xui_flow = getattr(client, 'flow', '') or ''
                status.xui_enable = getattr(client, 'enable', False)
                
                # Получаем значения из БД
                db_expiry_str = user_data.get('subscription_end_date')
                if db_expiry_str:
                    try:
                        db_expiry_dt = datetime.fromisoformat(db_expiry_str)
                        if db_expiry_dt.tzinfo is None:
                            db_expiry_dt = db_expiry_dt.replace(tzinfo=timezone.utc)
                        status.db_expiry_ms = int(db_expiry_dt.timestamp() * 1000)
                        status.db_subscription_active = status.db_expiry_ms > now_utc_ms
                    except Exception as e:
                        sync_logger.warning(f"[SYNC] Ошибка парсинга даты для {client_uuid}: {e}")
                        # Не устанавливаем db_subscription_active, если дата не распарсена
                
                status.db_limit_ip = user_data.get('limit_ip', 0) or 0
                
                # Проверяем несоответствия
                issues_found = False
                
                # 1. Проверка даты окончания
                if check_expiry and status.db_expiry_ms:
                    # Допускаем разницу до 1 минуты (60000 мс) из-за округления
                    expiry_diff = abs(status.xui_expiry_ms - status.db_expiry_ms)
                    if expiry_diff > 60000:
                        status.expiry_mismatch = True
                        issues_found = True
                        sync_logger.debug(
                            f"[SYNC] Несоответствие даты для {client_uuid}: "
                            f"X-UI={datetime.fromtimestamp(status.xui_expiry_ms/1000)}, "
                            f"БД={datetime.fromtimestamp(status.db_expiry_ms/1000)}"
                        )
                
                # 2. Проверка лимита IP
                if check_limit_ip:
                    if status.xui_limit_ip != status.db_limit_ip:
                        status.limit_ip_mismatch = True
                        issues_found = True
                        sync_logger.debug(
                            f"[SYNC] Несоответствие limit_ip для {client_uuid}: "
                            f"X-UI={status.xui_limit_ip}, БД={status.db_limit_ip}"
                        )
                
                # 3. Проверка flow
                if check_flow and default_flow:
                    if status.xui_flow != default_flow:
                        status.flow_missing = True
                        issues_found = True
                        sync_logger.debug(
                            f"[SYNC] Отсутствует flow для {client_uuid}: "
                            f"X-UI='{status.xui_flow}', должно быть='{default_flow}'"
                        )
                
                # 4. Проверка статуса включения для активных подписок
                if check_enable and status.db_subscription_active:
                    if not status.xui_enable:
                        status.disabled_when_active = True
                        issues_found = True
                        sync_logger.debug(
                            f"[SYNC] Клиент {client_uuid} отключен при активной подписке"
                        )
                
                if issues_found:
                    result.clients_with_issues += 1
                    result.statuses.append(status)
                    
                    # Исправляем проблемы, если нужно
                    if fix_issues:
                        try:
                            await _fix_client_issues(
                                client_api=client_api,
                                server_settings=server_settings,
                                client=client,
                                status=status,
                                default_flow=default_flow
                            )
                            status.fixed = True
                            result.clients_fixed += 1
                            sync_logger.info(
                                f"[SYNC] ✅ Исправлен клиент {client_uuid} ({client_email})"
                            )
                        except Exception as fix_error:
                            status.fix_error = str(fix_error)
                            result.errors.append(
                                f"Ошибка исправления {client_uuid}: {fix_error}"
                            )
                            sync_logger.error(
                                f"[SYNC] ❌ Ошибка исправления {client_uuid}: {fix_error}",
                                exc_info=True
                            )
                
            except Exception as e:
                result.errors.append(f"Ошибка обработки клиента: {e}")
                sync_logger.error(f"[SYNC] Ошибка обработки клиента: {e}", exc_info=True)
        
        # Теперь проверяем клиентов из БД, которых нет на сервере
        sync_logger.info("[SYNC] Проверка клиентов из БД, которых нет на сервере...")
        
        # Собираем множество UUID клиентов, которые есть на сервере
        existing_server_uuids: Set[str] = set()
        for client in clients_on_server:
            try:
                client_uuid = None
                if hasattr(client, 'id') and client.id:
                    client_uuid = str(client.id)
                elif hasattr(client, 'sub_id') and client.sub_id:
                    client_uuid = str(client.sub_id)
                
                if client_uuid:
                    existing_server_uuids.add(client_uuid.lower())
            except Exception:
                continue
        
        # Находим пользователей из БД, которых нет на сервере
        clients_to_add = []
        for uuid_key, user_data in db_users_by_uuid.items():
            # Пропускаем, если клиент уже есть на сервере
            if uuid_key in existing_server_uuids:
                continue
            
            # Проверяем, что у пользователя активная подписка
            db_expiry_str = user_data.get('subscription_end_date')
            if not db_expiry_str:
                continue
            
            try:
                db_expiry_dt = datetime.fromisoformat(db_expiry_str)
                if db_expiry_dt.tzinfo is None:
                    db_expiry_dt = db_expiry_dt.replace(tzinfo=timezone.utc)
                db_expiry_ms = int(db_expiry_dt.timestamp() * 1000)
                
                # Добавляем только если подписка активна
                if db_expiry_ms > now_utc_ms:
                    clients_to_add.append({
                        'telegram_id': user_data['telegram_id'],
                        'uuid': user_data['xui_client_uuid'],
                        'email': user_data['xui_client_email'],
                        'expiry_ms': db_expiry_ms,
                        'limit_ip': user_data.get('limit_ip', 0) or 0
                    })
            except Exception as e:
                sync_logger.warning(f"[SYNC] Ошибка обработки пользователя {user_data.get('telegram_id')} для добавления: {e}")
                continue
        
        # Добавляем отсутствующих клиентов на сервер батчами
        if clients_to_add:
            sync_logger.info(f"[SYNC] Найдено {len(clients_to_add)} клиентов из БД, которых нет на сервере")
            result.clients_to_add_count = len(clients_to_add)
            result.clients_added_count = 0
            
            # Преобразуем формат данных для батч-метода
            users_data_for_batch = []
            for client_data in clients_to_add:
                users_data_for_batch.append({
                    'uuid': client_data['uuid'],
                    'email': client_data['email'],
                    'expiry_timestamp_ms': client_data['expiry_ms'],
                    'telegram_id': client_data['telegram_id'],
                    'limit_ip': client_data['limit_ip']
                })
            
            # Добавляем батчами по 500 клиентов
            BATCH_SIZE = 500
            total_batches = (len(users_data_for_batch) + BATCH_SIZE - 1) // BATCH_SIZE
            
            for batch_idx in range(0, len(users_data_for_batch), BATCH_SIZE):
                batch = users_data_for_batch[batch_idx:batch_idx + BATCH_SIZE]
                batch_num = (batch_idx // BATCH_SIZE) + 1
                
                try:
                    sync_logger.info(
                        f"[SYNC] Добавление батча {batch_num}/{total_batches}: {len(batch)} клиентов"
                    )
                    
                    # Используем батч-метод для добавления
                    success_list, failed_list = await xui_manager_instance.recreate_xui_users_batch(
                        server_settings=server_settings,
                        users_data=batch
                    )
                    
                    # Обновляем счетчики
                    batch_added = len(success_list)
                    batch_failed = len(failed_list)
                    result.clients_added_count += batch_added
                    
                    sync_logger.info(
                        f"[SYNC] Батч {batch_num}/{total_batches}: добавлено {batch_added}, ошибок {batch_failed}"
                    )
                    
                    # Добавляем ошибки в список
                    for failed_item in failed_list:
                        user_data = failed_item.get('user', {})
                        error_msg = failed_item.get('error', 'unknown')
                        result.errors.append(
                            f"Не удалось добавить клиента {user_data.get('uuid', 'unknown')}: {error_msg}"
                        )
                    
                    # Обновляем прогресс (для отображения в UI)
                    # Прогресс будет обновляться через API статуса
                    
                except Exception as e:
                    sync_logger.error(
                        f"[SYNC] ❌ Ошибка при добавлении батча {batch_num}/{total_batches}: {e}",
                        exc_info=True
                    )
                    result.errors.append(f"Ошибка батча {batch_num}: {str(e)}")
                    # При ошибке батча считаем всех как неудачных
                    result.clients_added_count += 0  # Не увеличиваем счетчик
        else:
            sync_logger.info("[SYNC] Все клиенты из БД уже присутствуют на сервере")
            result.clients_to_add_count = 0
            result.clients_added_count = 0
        
        result.end_time = datetime.now(timezone.utc)
        duration = result.duration_seconds
        
        sync_logger.info(
            f"[SYNC] ✅ Завершено за {duration:.2f}с: проверено {result.clients_checked}, "
            f"проблем найдено {result.clients_with_issues}, "
            f"исправлено {result.clients_fixed}, "
            f"не в БД {result.clients_not_in_db}, "
            f"добавлено {result.clients_added_count}/{result.clients_to_add_count}"
        )
        
        if result.errors:
            sync_logger.warning(f"[SYNC] Обнаружено {len(result.errors)} ошибок:")
            for idx, error in enumerate(result.errors[:10], 1):  # Показываем первые 10 ошибок
                sync_logger.warning(f"[SYNC]   {idx}. {error}")
        
        return result
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        error_msg = f"Критическая ошибка синхронизации: {e}"
        result.errors.append(error_msg)
        sync_logger.error(f"[SYNC] ❌ {error_msg}\n{error_traceback}", exc_info=True)
        result.end_time = datetime.now(timezone.utc)
        return result


async def _fix_client_issues(
    client_api,
    server_settings: Dict,
    client: Client,
    status: ClientSyncStatus,
    default_flow: str
) -> None:
    """
    Исправляет проблемы клиента на сервере.
    Использует get_by_email для поиска клиента, затем обновляет его.
    """
    inbound_id = server_settings.get('inbound_id')
    protocol = (server_settings.get('protocol') or 'vless').strip().lower()
    client_uuid = status.uuid
    client_email = status.email
    
    try:
        # Сначала находим клиента по email (как в update_xui_user_subscription)
        client_from_xui = await client_api.client.get_by_email(client_email)
        
        if not client_from_xui:
            logger.warning(f"[SYNC] Клиент {client_uuid} ({client_email}) не найден через get_by_email. Пропускаем обновление.")
            return
        
        # Проверяем, что клиент на правильном inbound
        if client_from_xui.inbound_id != inbound_id:
            logger.warning(
                f"[SYNC] Клиент {client_uuid} найден на другом inbound "
                f"({client_from_xui.inbound_id} != {inbound_id}). Пропускаем обновление."
            )
            return
        
        # Создаем обновленный объект клиента на основе найденного клиента
        updated_client = Client.model_validate(client_from_xui.model_dump())
        
        # Исправляем дату окончания - всегда обновляем, если есть несоответствие
        if status.db_expiry_ms:
            current_expiry = getattr(updated_client, 'expiry_time', 0) or 0
            expiry_diff = abs(current_expiry - status.db_expiry_ms) if current_expiry else float('inf')
            
            # Обновляем дату, если разница больше 1 минуты (60000 мс)
            if expiry_diff > 60000:
                logger.info(
                    f"[SYNC] Обновление даты для {client_uuid}: "
                    f"текущая={datetime.fromtimestamp(current_expiry/1000) if current_expiry else 'None'}, "
                    f"новая={datetime.fromtimestamp(status.db_expiry_ms/1000)}, "
                    f"разница={expiry_diff/1000/60:.1f} минут"
                )
                updated_client.expiry_time = status.db_expiry_ms
            else:
                logger.debug(
                    f"[SYNC] Дата для {client_uuid} совпадает (разница {expiry_diff/1000:.1f} секунд)"
                )
        else:
            logger.warning(f"[SYNC] Нет db_expiry_ms для {client_uuid}, пропускаем обновление даты")
        
        # Исправляем лимит IP
        if status.limit_ip_mismatch:
            logger.info(
                f"[SYNC] Обновление limit_ip для {client_uuid}: "
                f"текущий={updated_client.limit_ip}, новый={status.db_limit_ip}"
            )
            updated_client.limit_ip = status.db_limit_ip
        
        # Исправляем flow - всегда устанавливаем из настроек сервера, если он указан
        # Это важно, так как при обновлении других полей flow может слететь
        if default_flow:
            current_flow = getattr(updated_client, 'flow', '') or ''
            if current_flow != default_flow:
                logger.info(
                    f"[SYNC] Обновление flow для {client_uuid}: "
                    f"текущий='{current_flow}', новый='{default_flow}'"
                )
                updated_client.flow = default_flow
            else:
                logger.debug(f"[SYNC] Flow для {client_uuid} уже правильный: '{current_flow}'")
        else:
            logger.debug(f"[SYNC] Flow не указан в настройках сервера для {client_uuid}, пропускаем")
        
        # Включаем клиента только если подписка явно активна
        # Не меняем статус enable для неактивных подписок или если db_subscription_active не установлен
        if status.db_subscription_active is True:
            if not updated_client.enable:
                logger.info(f"[SYNC] Включение клиента {client_uuid} (подписка активна)")
            updated_client.enable = True
        # Если db_subscription_active не True (False или None), не меняем enable - оставляем как есть
        
        # Устанавливаем правильные идентификаторы в зависимости от протокола
        if protocol == 'trojan':
            updated_client.password = client_uuid
            updated_client.sub_id = client_uuid
            await client_api.client.update(client_uuid=client_uuid, client=updated_client)
        elif protocol == 'shadowsocks':
            updated_client.sub_id = client_uuid
            await client_api.client.update(client_uuid=client_email, client=updated_client)
        elif protocol in ('hysteria', 'hysteria2', 'hy2'):
            # Для Hysteria2: auth = client_uuid (UUID из БД), flow не применим
            updated_client.auth = client_uuid
            updated_client.sub_id = client_uuid
            updated_client.flow = ''
            await client_api.client.update(client_uuid=client_uuid, client=updated_client)
        else:
            # Для VLESS/VMESS
            updated_client.id = client_uuid
            updated_client.sub_id = client_uuid
            await client_api.client.update(client_uuid=client_uuid, client=updated_client)
        
        # Проверяем, что обновление прошло успешно
        if status.db_expiry_ms and updated_client.expiry_time:
            logger.info(
                f"[SYNC] ✅ Клиент {client_uuid} успешно обновлен. "
                f"Дата окончания установлена: {datetime.fromtimestamp(updated_client.expiry_time/1000)}"
            )
        else:
            logger.debug(f"[SYNC] ✅ Клиент {client_uuid} успешно обновлен")
        
    except Exception as e:
        error_str = str(e).lower()
        logger.error(f"[SYNC] Ошибка обновления клиента {client_uuid}: {e}")
        
        # Если клиент не найден, это не критично - возможно, он был удален
        if 'not found' in error_str or '404' in error_str or 'does not exist' in error_str or 'record not found' in error_str:
            logger.warning(f"[SYNC] Клиент {client_uuid} не найден при обновлении. Возможно, был удален.")
            # Не пробрасываем ошибку дальше, так как это не критично
            return
        
        # Другие ошибки пробрасываем
        raise

