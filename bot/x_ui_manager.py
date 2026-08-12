import asyncio
import json
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple, NamedTuple

# Отключаем SSL-предупреждения
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Импортируем асинхронный API
from py3xui.async_api.async_api import AsyncApi
from py3xui.client import Client as XUIClientObj, Client
from py3xui.inbound import Inbound
from loguru import logger
import uuid
import random
import asyncio
import db_helpers
from app_config import app_conf
import time

class ClientSearchResult(NamedTuple):
    """Результат поиска клиента"""
    client: Optional[XUIClientObj]
    is_api_error: bool  # True если была ошибка API (не "not found"), False если "not found" или найден


def _is_hysteria(protocol: str) -> bool:
    """
    Возвращает True для всех вариаций имени Hysteria2:
    - 'hysteria2' (как мы пишем в настройках сервера в админке)
    - 'hysteria'  (как 3X-UI отдает в inbound.protocol)
    - 'hy2'       (на всякий случай для совместимости)
    """
    return (protocol or '').strip().lower() in ('hysteria', 'hysteria2', 'hy2')

class XUIManager:
    def __init__(self):
        self.clients: Dict[int, AsyncApi] = {}  # Изменено на AsyncApi
        self.traffic_cache: Dict[str, Dict[str, Any]] = {}
        self.cache_timeout = 360  # секунды
        # (reverted) убраны локи и TTL статуса

    async def get_client(self, server_settings: Dict) -> Optional[AsyncApi]:  # Изменено на AsyncApi
        server_id = server_settings['id']
        # Используем уже созданный клиент без лишних проверок статуса.
        # При ошибке авторизации операции выполнит relogin и повтор.
        if server_id in self.clients:
            logger.debug(f"Использование существующего клиента для сервера {server_id}")
            return self.clients[server_id]

        try:
            logger.info(f"Создание X-UI клиента для сервера {server_id} ({server_settings['name']})")

            url = server_settings['url']
            if not url.startswith('http'):
                url = f"https://{url}"

            api_url = f"{url}:{server_settings['port']}"
            if server_settings.get('secret_path'):
                 api_url += f"/{server_settings['secret_path'].strip('/')}"

            logger.debug(f"API URL для {server_settings['name']}: {api_url}")

            # Используем асинхронный API
            client = AsyncApi(
                api_url, 
                server_settings['username'], 
                server_settings['password'], 
                use_tls_verify=False
            )
            
            logger.debug(f"AsyncApi клиент создан для {server_settings['name']}")
            logger.debug(f"URL: {api_url}")
            logger.debug(f"Username: {server_settings['username']}")
            logger.debug(f"Use TLS verify: {False}")

            # Нативные асинхронные вызовы с дополнительным таймаутом и повторными попытками
            max_login_retries = 2
            login_success = False
            
            for login_attempt in range(max_login_retries):
                try:
                    start_time = time.time()
                    logger.debug(f"Начинаем логин для {server_settings['name']} (попытка {login_attempt + 1}/{max_login_retries})")
                    
                    await asyncio.wait_for(client.login(), timeout=3.0) 
                    login_time = time.time() - start_time
                    logger.debug(f"Логин для {server_settings['name']} завершен за {login_time:.2f} сек")
                    login_success = True
                    break
                    
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут при подключении к {server_settings['name']} (попытка {login_attempt + 1}/{max_login_retries}, логин: 3с)")
                    if login_attempt < max_login_retries - 1:
                        await asyncio.sleep(0.1)  # Короткая задержка перед повтором
                    else:
                        logger.error(f"Все {max_login_retries} попытки логина для {server_settings['name']} завершились таймаутом")
                except Exception as e:
                    logger.warning(f"Ошибка при подключении к {server_settings['name']} (попытка {login_attempt + 1}/{max_login_retries}): {e}")
                    if login_attempt < max_login_retries - 1:
                        await asyncio.sleep(0.1)  # Короткая задержка перед повтором
                    else:
                        logger.error(f"Все {max_login_retries} попытки логина для {server_settings['name']} не удались")
            
            if not login_success:
                logger.error(f"Не удалось залогиниться на сервер {server_settings['name']} после {max_login_retries} попыток")
                return None

            logger.info(f"Подключение к {server_settings['name']} успешно.")

            self.clients[server_id] = client
            return client

        except Exception as e:
            logger.error(f"Ошибка при создании X-UI клиента для сервера {server_id} ({server_settings['name']}): {e}")
            return None

    async def _find_inbound_by_id(self, client_api: AsyncApi, inbound_id: int, caller_info: str = None) -> Optional[Inbound]:
        try:
            import traceback
            # Получаем информацию о вызывающей функции для отладки
            if caller_info is None:
                stack = traceback.extract_stack()
                if len(stack) >= 2:
                    caller_info = f"{stack[-2].name}:{stack[-2].lineno}"
                else:
                    caller_info = "unknown"
            
            logger.debug(f"[FIND_INBOUND] Запрос inbound ID={inbound_id} из {caller_info}")
            # <<< ИЗМЕНЕНИЕ: Оборачиваем блокирующий вызов
            inbound = await client_api.inbound.get_by_id(inbound_id)
            if inbound:
                logger.debug(f"[FIND_INBOUND] ✅ Inbound {inbound_id} найден, тип: {type(inbound)}, вызвано из {caller_info}")
                if not hasattr(inbound, 'settings') or not inbound.settings:
                    logger.warning(f"[FIND_INBOUND] Inbound {inbound_id} получен, но не содержит 'settings'.")
                elif not hasattr(inbound.settings, 'clients'):
                    logger.warning(f"[FIND_INBOUND] Inbound {inbound_id} получен, settings есть, но нет 'clients'.")
                else:
                    clients_count = len(inbound.settings.clients) if inbound.settings.clients else 0
                    logger.debug(f"[FIND_INBOUND] Inbound {inbound_id} валиден, содержит settings и {clients_count} клиентов, вызвано из {caller_info}")
            else:
                logger.warning(f"[FIND_INBOUND] ❌ Inbound {inbound_id} не найден (вернул None), вызвано из {caller_info}")
            return inbound
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            logger.error(f"[FIND_INBOUND] ❌ Исключение при получении inbound {inbound_id}: {error_type}: {error_msg}")
            logger.error(f"[FIND_INBOUND] Полный traceback:", exc_info=True)
            # Пытаемся один раз перелогиниться при проблемах авторизации
            msg = error_msg.lower()
            if '401' in msg or 'unauth' in msg or 'not found' in msg or '404' in msg:
                if 'not found' in msg or '404' in msg:
                    logger.error(f"[FIND_INBOUND] Inbound {inbound_id} не существует на сервере!")
                    return None
                try:
                    logger.warning(f"[FIND_INBOUND] Ошибка авторизации при получении inbound {inbound_id}. Перелогин и повтор")
                    await client_api.login()
                    inbound = await client_api.inbound.get_by_id(inbound_id)
                    if inbound:
                        logger.info(f"[FIND_INBOUND] ✅ Inbound {inbound_id} найден после relogin")
                    else:
                        logger.warning(f"[FIND_INBOUND] ❌ Inbound {inbound_id} не найден после relogin (вернул None)")
                    return inbound
                except Exception as e2:
                    logger.error(f"[FIND_INBOUND] Ошибка при повторном получении inbound {inbound_id} после логина: {e2}")
                    return None
            return None

    def _create_client_obj(self, server_settings: Dict, client_uuid: str, email: str, telegram_id: int, 
                          expiry_timestamp_ms: int, total_gb: int = 0, limit_ip: int = 0, 
                          flow_value: str = None) -> Client:
        """
        Создает объект Client в зависимости от протокола сервера.
        - VLESS/VMESS: id=uuid
        - Trojan/Shadowsocks: password=uuid
        - Hysteria2: auth=uuid (поле добавлено в py3xui.Client, см. py3xui/client/client.py)

        Протокол определяется из server_settings.get('protocol'), по умолчанию 'vless'.
        Для существующих серверов без поля 'protocol' используется значение по умолчанию 'vless'.
        """
        if flow_value is None:
            flow_value = (server_settings.get('client_flow') or '').strip()

        protocol = (server_settings.get('protocol') or 'vless').strip().lower()
        server_id = server_settings.get('id', 'N/A')
        server_name = server_settings.get('name', 'N/A')

        # Hysteria2 — auth играет роль id/password.
        # У hysteria нет flow и обычно нет totalGB/limit_ip как у XTLS, но передаём те же поля
        # для единообразия с админкой (3X-UI v2 их корректно сохранит).
        if protocol == 'hysteria2':
            logger.debug(f"[CREATE_CLIENT] Сервер {server_id} ({server_name}): протокол hysteria2, используем auth={client_uuid}")
            return Client(
                auth=client_uuid,
                email=email,
                enable=True,
                flow="",  # для hysteria flow не применим
                tg_id=str(telegram_id),
                total_gb=total_gb,
                expiry_time=expiry_timestamp_ms,
                limit_ip=limit_ip or server_settings.get('default_limit_ip', 0),
                sub_id=client_uuid,
            )

        # Для Trojan и Shadowsocks используем password вместо id
        if protocol in ('trojan', 'shadowsocks'):
            logger.debug(f"[CREATE_CLIENT] Сервер {server_id} ({server_name}): протокол {protocol}, используем password={client_uuid}")
            return Client(
                password=client_uuid,  # UUID используется как пароль
                email=email,
                enable=True,
                flow=flow_value,
                tg_id=str(telegram_id),
                total_gb=total_gb,
                expiry_time=expiry_timestamp_ms,
                limit_ip=limit_ip or server_settings.get('default_limit_ip', 0),
                sub_id=client_uuid  # sub_id всегда UUID для всех протоколов
            )
        else:
            # Для VLESS и VMESS используем id
            logger.debug(f"[CREATE_CLIENT] Сервер {server_id} ({server_name}): протокол {protocol}, используем id={client_uuid}")
            return Client(
                id=client_uuid,  # UUID используется как id
                email=email,
                enable=True,
                flow=flow_value,
                tg_id=str(telegram_id),
                total_gb=total_gb,
                expiry_time=expiry_timestamp_ms,
                limit_ip=limit_ip or server_settings.get('default_limit_ip', 0),
                sub_id=client_uuid  # sub_id всегда UUID для всех протоколов
            )

    def _get_flow_for_inbound(self, inbound: Inbound) -> str:
        """
        Определяет значение 'flow' для клиента на основе настроек inbound.
        Возвращает пустую строку, если flow не требуется.
        """
        try:
            # Проверяем, что stream_settings вообще существуют
            if not hasattr(inbound, 'stream_settings') or not inbound.stream_settings:
                return ""

            stream_settings = inbound.stream_settings
            security_type = getattr(stream_settings, 'security', None)
            network = getattr(stream_settings, 'network', None)

            # Явная проверка для XTLS
            if security_type == 'xtls' and hasattr(stream_settings, 'xtls_settings'):
                xtls_settings = getattr(stream_settings, 'xtls_settings', None)
                if xtls_settings and hasattr(xtls_settings, 'flow'):
                    if (network or '').lower() == 'tcp':
                        flow_value = xtls_settings.flow
                        logger.debug(f"Inbound ID {inbound.id} XTLS/TCP. flow='{flow_value}'")
                        return flow_value
                    else:
                        logger.debug(f"Inbound ID {inbound.id} XTLS но network='{network}'. flow не задаем.")

            # Явная проверка для Reality
            if security_type == 'reality' and hasattr(stream_settings, 'reality_settings'):
                # Для Reality flow 'xtls-rprx-vision' ТОЛЬКО при TCP
                if (network or '').lower() == 'tcp':
                    flow_value = "xtls-rprx-vision"
                    logger.debug(f"Inbound ID {inbound.id} Reality/TCP. flow='{flow_value}'")
                    return flow_value
                else:
                    logger.debug(f"Inbound ID {inbound.id} Reality но network='{network}'. flow не задаем.")

            # Для всех остальных случаев (ws, tcp без xtls/reality, etc.) flow не нужен
            logger.debug(f"Inbound ID {inbound.id} с security='{security_type}' не требует flow. Устанавливаем пустую строку.")
            return ""

        except Exception as e:
            logger.warning(f"Ошибка при определении flow для inbound ID {getattr(inbound, 'id', 'N/A')}: {e}. Возвращаем пустую строку.")
            return ""


    async def _find_client_by_email_or_uuid(self, xui_api_client: AsyncApi, inbound_id: int, identifier: str, telegram_id: Optional[int] = None) -> ClientSearchResult:
        """
        Поиск клиента по email или UUID.
        
        Оптимизация:
        1. Если identifier содержит '@' (email) - используем быстрый API метод get_by_email
        2. Если identifier это UUID - сначала пытаемся найти через email из БД (если telegram_id передан)
        3. Если не нашли через API - загружаем весь inbound и ищем в нем (fallback)
        
        Args:
            xui_api_client: API клиент X-UI
            inbound_id: ID inbound
            identifier: Email или UUID клиента
            telegram_id: Опционально - telegram_id для поиска email в БД (оптимизация для UUID)
        """
        try:
            api_error_occurred = False
            api_error_message = None
            
            # Случай 1: identifier это email (содержит '@')
            if '@' in identifier:
                logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 1: Попытка поиска через get_by_email для email={identifier}, inbound_id={inbound_id}")
                try:
                    client_obj = await xui_api_client.client.get_by_email(identifier)
                    logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 1: get_by_email вернул: {'клиент найден' if client_obj else 'клиент НЕ найден'}")
                    if client_obj:
                        if client_obj.inbound_id == inbound_id:
                            logger.info(f"[FIND_CLIENT] ✅ МЕТОД 1: Клиент найден через get_by_email: {identifier}, inbound_id={inbound_id}")
                            return ClientSearchResult(client_obj, False)
                        else:
                            logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 1: Клиент найден, но на ДРУГОМ inbound: {client_obj.inbound_id} != {inbound_id}, переходим к FALLBACK")
                            # НЕ возвращаем None - переходим к fallback
                    else:
                        # Клиент не найден через API - переходим к fallback
                        logger.warning(f"[FIND_CLIENT] ⚠️ МЕТОД 1: Клиент НЕ найден через get_by_email({identifier}), переходим к FALLBACK")
                        # НЕ возвращаем None - переходим к fallback
                except Exception as e:
                    error_str = str(e).lower()
                    # Если это явная ошибка "not found" - НЕ устанавливаем api_error (это не ошибка API, просто клиент не существует)
                    if 'not found' in error_str or '404' in error_str or 'does not exist' in error_str:
                        logger.warning(f"[FIND_CLIENT] ⚠️ МЕТОД 1: Клиент не найден (404/not found) через get_by_email({identifier}), переходим к FALLBACK")
                        # НЕ устанавливаем api_error_occurred - клиент просто не найден, это не ошибка API
                    else:
                        # Другие ошибки API (таймаут, соединение и т.д.) - это реальные ошибки
                        logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 1: Ошибка API при get_by_email({identifier}): {e}, переходим к FALLBACK")
                        api_error_occurred = True
                        api_error_message = str(e)
            
            # Случай 2: identifier это UUID, но у нас есть telegram_id - пытаемся найти email в БД
            if telegram_id and '@' not in identifier:
                logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2: Попытка поиска через email из БД для UUID={identifier}, telegram_id={telegram_id}")
                try:
                    user_data = await db_helpers.get_user(telegram_id)
                    # aiosqlite.Row поддерживает доступ по ключу через []
                    email_from_db = None
                    if user_data:
                        try:
                            email_from_db = user_data['xui_client_email']
                        except (KeyError, IndexError):
                            logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 2: Поле 'xui_client_email' отсутствует в БД для telegram_id {telegram_id}")
                    
                    if email_from_db:
                        logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2: Email из БД получен: {email_from_db}, вызываем get_by_email")
                        try:
                            client_obj = await xui_api_client.client.get_by_email(email_from_db)
                            logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2: get_by_email вернул: {'клиент найден' if client_obj else 'клиент НЕ найден'}")
                            if client_obj:
                                # Проверяем совпадение: для Trojan проверяем password, для остальных - id
                                inbound_protocol = None
                                try:
                                    inbound_temp = await self._find_inbound_by_id(xui_api_client, inbound_id, caller_info="_find_client_by_email_or_uuid_check")
                                    if inbound_temp:
                                        inbound_protocol = getattr(inbound_temp, 'protocol', '').lower() if hasattr(inbound_temp, 'protocol') else ''
                                except:
                                    pass
                                
                                # Проверяем совпадение по протоколу
                                matches = False
                                if inbound_protocol == 'trojan':
                                    password_match = getattr(client_obj, 'password', None) == identifier
                                    email_match = client_obj.email == email_from_db
                                    inbound_match = client_obj.inbound_id == inbound_id
                                    matches = (inbound_match and (password_match or email_match))
                                    logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2 TROJAN: inbound_match={inbound_match}, password_match={password_match}, email_match={email_match}, итого matches={matches}")
                                elif inbound_protocol == 'shadowsocks':
                                    inbound_match = client_obj.inbound_id == inbound_id
                                    email_match = client_obj.email == email_from_db
                                    matches = (inbound_match and email_match)
                                    logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2 SHADOWSOCKS: inbound_match={inbound_match}, email_match={email_match}, итого matches={matches}")
                                elif _is_hysteria(inbound_protocol):
                                    auth_match = getattr(client_obj, 'auth', None) == identifier
                                    email_match = client_obj.email == email_from_db
                                    inbound_match = client_obj.inbound_id == inbound_id
                                    matches = (inbound_match and (auth_match or email_match))
                                    logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2 HYSTERIA2: inbound_match={inbound_match}, auth_match={auth_match}, email_match={email_match}, итого matches={matches}")
                                else:
                                    id_match = getattr(client_obj, 'id', None) == identifier
                                    email_match = client_obj.email == email_from_db
                                    inbound_match = client_obj.inbound_id == inbound_id
                                    matches = (inbound_match and (id_match or email_match))
                                    logger.info(f"[FIND_CLIENT] 🔍 МЕТОД 2 VLESS/VMESS: inbound_match={inbound_match} (client={client_obj.inbound_id} vs target={inbound_id}), id_match={id_match} (client.id={getattr(client_obj, 'id', 'N/A')} vs UUID={identifier}), email_match={email_match}, итого matches={matches}")
                                
                                if matches:
                                    logger.info(f"[FIND_CLIENT] ✅ МЕТОД 2: Клиент найден через get_by_email (email из БД): {email_from_db}, UUID={identifier}, inbound_id={inbound_id}, protocol={inbound_protocol}")
                                    return ClientSearchResult(client_obj, False)
                                else:
                                    logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 2: Клиент найден, но НЕ совпадает: client_obj.inbound_id={client_obj.inbound_id} vs {inbound_id}, client_obj.id={getattr(client_obj, 'id', 'N/A')} vs UUID={identifier}")
                                    # НЕ возвращаем None - переходим к fallback, возможно API вернул клиента с другого inbound
                            else:
                                # Клиент не найден через API - переходим к fallback
                                logger.warning(f"[FIND_CLIENT] ⚠️ МЕТОД 2: Клиент НЕ найден через get_by_email({email_from_db}), переходим к FALLBACK")
                        except Exception as e:
                            error_str = str(e).lower()
                            # Если это явная ошибка "not found" - НЕ устанавливаем api_error (это не ошибка API, просто клиент не существует)
                            if 'not found' in error_str or '404' in error_str or 'does not exist' in error_str:
                                logger.warning(f"[FIND_CLIENT] ⚠️ МЕТОД 2: Клиент не найден (404/not found) через get_by_email({email_from_db}), переходим к FALLBACK")
                                # НЕ устанавливаем api_error_occurred - клиент просто не найден, это не ошибка API
                            else:
                                # Другие ошибки API (таймаут, соединение и т.д.) - это реальные ошибки
                                logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 2: Ошибка API при get_by_email({email_from_db}): {e}, переходим к FALLBACK")
                                api_error_occurred = True
                                api_error_message = str(e)
                    else:
                        # Нет email в БД - переходим к fallback
                        logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 2: Нет email в БД для telegram_id {telegram_id}, переходим к FALLBACK")
                except Exception as e:
                    logger.warning(f"[FIND_CLIENT] ❌ МЕТОД 2: Ошибка при получении email из БД для telegram_id {telegram_id}: {e}, переходим к FALLBACK")
            
            # Случай 3: Загружаем весь inbound и ищем в нем (fallback только при ошибках API или если нет telegram_id)
            if api_error_occurred:
                logger.warning(f"[FIND_CLIENT] Загрузка всего inbound {inbound_id} для поиска клиента {identifier} (fallback из-за ошибки API: {api_error_message})")
            else:
                logger.debug(f"[FIND_CLIENT] Загрузка всего inbound {inbound_id} для поиска клиента {identifier} (fallback - нет telegram_id или email в БД)")
            inbound = await self._find_inbound_by_id(xui_api_client, inbound_id, caller_info="_find_client_by_email_or_uuid")
            if inbound and hasattr(inbound, 'settings') and inbound.settings and \
               hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                # Определяем протокол для правильного поиска
                protocol = getattr(inbound, 'protocol', '').lower() if hasattr(inbound, 'protocol') else ''
                
                for client_data in inbound.settings.clients:
                    # Для Trojan проверяем password, для Hysteria2 — auth, для остальных — id
                    if protocol == 'trojan':
                        if client_data.email == identifier or getattr(client_data, 'password', None) == identifier:
                            logger.info(f"[FIND_CLIENT] ✅ МЕТОД 3 (FALLBACK INBOUND): Клиент найден в загруженном inbound (Trojan): {identifier}, inbound_id={inbound_id}, всего клиентов в inbound={len(inbound.settings.clients)}")
                            return ClientSearchResult(client_data, False)
                    elif protocol == 'shadowsocks':
                        # Для Shadowsocks используем email как идентификатор
                        if client_data.email == identifier:
                            logger.info(f"[FIND_CLIENT] ✅ МЕТОД 3 (FALLBACK INBOUND): Клиент найден в загруженном inbound (Shadowsocks): {identifier}, inbound_id={inbound_id}, всего клиентов в inbound={len(inbound.settings.clients)}")
                            return ClientSearchResult(client_data, False)
                    elif _is_hysteria(protocol):
                        # Для Hysteria2 проверяем auth
                        if client_data.email == identifier or getattr(client_data, 'auth', None) == identifier:
                            logger.info(f"[FIND_CLIENT] ✅ МЕТОД 3 (FALLBACK INBOUND): Клиент найден в загруженном inbound (Hysteria2): {identifier}, inbound_id={inbound_id}, всего клиентов в inbound={len(inbound.settings.clients)}")
                            return ClientSearchResult(client_data, False)
                    else:
                        # Для VLESS/VMESS проверяем id
                        if client_data.email == identifier or getattr(client_data, 'id', None) == identifier:
                            logger.info(f"[FIND_CLIENT] ✅ МЕТОД 3 (FALLBACK INBOUND): Клиент найден в загруженном inbound (VLESS/VMESS): {identifier}, inbound_id={inbound_id}, всего клиентов в inbound={len(inbound.settings.clients)}")
                            return ClientSearchResult(client_data, False)
            logger.debug(f"[FIND_CLIENT] Клиент не найден: {identifier}")
            # Если была ошибка API, возвращаем с флагом ошибки
            return ClientSearchResult(None, api_error_occurred)
        except Exception as e:
            logger.error(f"Ошибка при поиске клиента '{identifier}' в inbound {inbound_id}: {e}")
            return ClientSearchResult(None, True)  # Ошибка при поиске

    async def recreate_xui_user(self, server_settings: Dict, user_data: Dict) -> bool:
        client_api = await self.get_client(server_settings)
        if not client_api: return False

        inbound_id = server_settings['inbound_id']
        email = user_data['email']
        telegram_id = user_data.get('telegram_id')
        server_name = server_settings.get('name', 'Unknown')
        server_id = server_settings.get('id', 'N/A')
        
        # Логируем наличие telegram_id для отладки
        logger.debug(f"[RECREATE] Создание клиента {email} на сервере {server_id} ({server_name}), telegram_id в user_data: {telegram_id}")
        
        # Проверяем наличие telegram_id
        if not telegram_id:
            logger.warning(f"[RECREATE] telegram_id отсутствует в user_data для email {email} на сервере {server_name}")
            # Пытаемся получить telegram_id из email (формат: tg{telegram_id}_{random}@domain)
            try:
                if email.startswith('tg') and '@' in email:
                    tg_part = email.split('@')[0]
                    if '_' in tg_part:
                        telegram_id = int(tg_part.split('_')[0][2:])  # Извлекаем ID из "tg123456_random"
                        logger.info(f"[RECREATE] Извлечен telegram_id {telegram_id} из email {email} для сервера {server_name}")
            except (ValueError, IndexError) as e:
                logger.error(f"[RECREATE] Не удалось извлечь telegram_id из email {email}: {e}")
        
        # Проверяем, существует ли клиент по email перед созданием
        try:
            search_result = await self._find_client_by_email_or_uuid(
                client_api,
                inbound_id,
                email,
                telegram_id=telegram_id
            )
            
            if search_result.client is not None:
                # Клиент уже существует - обновляем его на актуальные данные из БД
                logger.info(f"[RECREATE] Клиент {email} уже существует на сервере {server_settings.get('name', 'Unknown')}. Обновляем на актуальные данные.")
                
                if not telegram_id:
                    logger.error(f"[RECREATE] Не удалось получить telegram_id для обновления клиента {email}")
                    return False
                
                # Создаем объект с актуальными данными из БД
                client_to_update = self._create_client_obj(
                    server_settings=server_settings,
                    client_uuid=user_data['uuid'],
                    email=user_data['email'],
                    telegram_id=telegram_id,
                    expiry_timestamp_ms=user_data['expiry_timestamp_ms'],
                    total_gb=0,
                    limit_ip=user_data.get('limit_ip', 0)
                )
                
                # Определяем протокол для правильного обновления
                protocol = (server_settings.get('protocol') or 'vless').strip().lower()
                
                # Устанавливаем обязательные поля
                client_to_update.inbound_id = inbound_id
                client_to_update.sub_id = user_data['uuid']  # ✅ КРИТИЧЕСКИ ВАЖНО: sub_id всегда = UUID из Remnawave для 3XUI
                
                # Определяем идентификатор для update в зависимости от протокола
                # Логика как в bek, но user_data['uuid'] - это UUID из Remnawave для 3XUI
                if protocol == 'trojan':
                    # Для Trojan устанавливаем password = UUID из БД
                    client_to_update.password = user_data['uuid']
                    logger.info(f"[RECREATE] Протокол {protocol}, установлен password={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                    await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_update)
                elif protocol == 'shadowsocks':
                    # Для Shadowsocks используем email как идентификатор
                    client_identifier = search_result.client.email
                    logger.info(f"[RECREATE] Протокол {protocol}, используем email={client_identifier}, sub_id={user_data['uuid']} для update")
                    await client_api.client.update(client_uuid=client_identifier, client=client_to_update)
                elif _is_hysteria(protocol):
                    # Для Hysteria2 устанавливаем auth = UUID из БД
                    client_to_update.auth = user_data['uuid']
                    logger.info(f"[RECREATE] Протокол {protocol}, установлен auth={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                    await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_update)
                else:
                    # Для VLESS/VMESS устанавливаем id = UUID из БД
                    client_to_update.id = user_data['uuid']
                    logger.info(f"[RECREATE] Протокол {protocol}, установлен id={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                    await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_update)
                logger.info(f"[RECREATE] ✅ Клиент {email} успешно обновлен (UPDATE вместо CREATE).")
                return True
        except Exception as check_error:
            # Если проверка не удалась, продолжаем попытку создания (fallback)
            logger.debug(f"[RECREATE] Не удалось проверить существование клиента {email}: {check_error}, продолжаем создание")
        
        try:
            # Проверяем наличие telegram_id перед созданием клиента
            if not telegram_id:
                logger.error(f"[RECREATE] Не удалось получить telegram_id для создания клиента {email} на сервере {server_settings.get('name', 'Unknown')}")
                return False
            
            # Используем вспомогательную функцию для создания Client в зависимости от протокола
            logger.debug(f"[RECREATE] Создание Client объекта для {email} на сервере {server_name} с telegram_id={telegram_id}")
            client_to_add = self._create_client_obj(
                server_settings=server_settings,
                client_uuid=user_data['uuid'],
                email=user_data['email'],
                telegram_id=telegram_id,
                expiry_timestamp_ms=user_data['expiry_timestamp_ms'],
                total_gb=0,
                limit_ip=user_data.get('limit_ip', 0)
            )
            # Проверяем, что tg_id действительно установлен в объекте Client
            if hasattr(client_to_add, 'tg_id'):
                logger.debug(f"[RECREATE] Client объект создан с tg_id={client_to_add.tg_id} для {email} на сервере {server_name}")
            else:
                logger.warning(f"[RECREATE] Client объект не имеет атрибута tg_id для {email} на сервере {server_name}")
        except KeyError as ke:
            logger.error(f"[RECREATE] Отсутствует обязательное поле в user_data: {ke} для email {email}")
            return False
        except Exception as e:
            logger.error(f"Ошибка подготовки данных для воссоздания: {e}", exc_info=True)
            return False

        try:
            # <<< ИЗМЕНЕНИЕ: Оборачиваем блокирующий вызов
            await client_api.client.add(inbound_id=inbound_id, clients=[client_to_add])
            return True
        except ValueError as ve:
            # ValueError из-за success=false в ответе X-UI API (py3xui бросает ValueError)
            error_msg = str(ve).lower()
            # Проверяем, может быть это дубликат email
            if "duplicate email" in error_msg or "duplicate" in error_msg:
                logger.warning(f"Дубликат {user_data['email']} на {server_settings['name']} (ValueError: {ve}). Ищем существующего клиента и обновляем.")
                
                # ИСПРАВЛЕННАЯ ЛОГИКА: вместо удаления ищем существующего клиента и обновляем его
                try:
                    search_result_retry = await self._find_client_by_email_or_uuid(
                        client_api, inbound_id, email, telegram_id=telegram_id
                    )
                    
                    if search_result_retry.client:
                        existing_client = search_result_retry.client
                        logger.info(f"[RECREATE] ✅ Найден существующий клиент {email}. Обновляем на актуальные данные из БД.")
                        
                        # Определяем протокол для правильного обновления
                        protocol = (server_settings.get('protocol') or 'vless').strip().lower()
                        
                        # Устанавливаем обязательные поля
                        client_to_add.inbound_id = inbound_id
                        client_to_add.sub_id = user_data['uuid']  # ✅ КРИТИЧЕСКИ ВАЖНО: sub_id всегда = UUID из Remnawave для 3XUI
                        
                        # Определяем идентификатор для update в зависимости от протокола
                        # Логика как в bek, но user_data['uuid'] - это UUID из Remnawave для 3XUI
                        if protocol == 'trojan':
                            # Для Trojan устанавливаем password = UUID из БД
                            client_to_add.password = user_data['uuid']
                            logger.info(f"[RECREATE] Протокол {protocol}, установлен password={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                            await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_add)
                        elif protocol == 'shadowsocks':
                            # Для Shadowsocks используем email как идентификатор
                            client_identifier = existing_client.email
                            logger.info(f"[RECREATE] Протокол {protocol}, используем email={client_identifier}, sub_id={user_data['uuid']} для update")
                            await client_api.client.update(client_uuid=client_identifier, client=client_to_add)
                        elif _is_hysteria(protocol):
                            # Для Hysteria2 устанавливаем auth = UUID из БД
                            client_to_add.auth = user_data['uuid']
                            logger.info(f"[RECREATE] Протокол {protocol}, установлен auth={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                            await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_add)
                        else:
                            # Для VLESS/VMESS устанавливаем id = UUID из БД
                            client_to_add.id = user_data['uuid']
                            logger.info(f"[RECREATE] Протокол {protocol}, установлен id={user_data['uuid']}, sub_id={user_data['uuid']}, inbound_id={inbound_id} для update")
                            await client_api.client.update(client_uuid=user_data['uuid'], client=client_to_add)
                        logger.info(f"[RECREATE] ✅ Клиент {email} успешно обновлен (UPDATE вместо CREATE).")
                        return True
                    else:
                        # Клиент "фантомный" (ошибка Duplicate есть, а найти не можем)
                        # Только в этом крайнем случае пробуем удалить по Email
                        logger.warning(f"[RECREATE] ⚠️ Фантомный дубликат {email}. Пробуем удалить по Email как крайнюю меру.")
                        try:
                            await self.delete_xui_user(server_settings, email)
                            await asyncio.sleep(1.5)  # Ждем дольше для обработки удаления
                            await client_api.client.add(inbound_id=inbound_id, clients=[client_to_add])
                            logger.info(f"[RECREATE] ✅ Фантомный дубликат {email} исправлен через удаление и пересоздание.")
                            return True
                        except Exception as delete_add_e:
                            logger.error(f"[RECREATE] ❌ Не удалось исправить фантомный дубликат {email}: {delete_add_e}")
                            # Логируем ошибку в БД
                            try:
                                await db_helpers.log_client_recreation_error(
                                    telegram_id=user_data.get('telegram_id', 0),
                                    client_uuid=user_data.get('uuid', ''),
                                    server_id=server_settings.get('id'),
                                    server_name=server_settings.get('name', 'Unknown'),
                                    error_type='phantom_duplicate_failed',
                                    error_message=f"Фантомный дубликат не исправлен: {str(delete_add_e)[:500]}"
                                )
                            except Exception:
                                pass
                            return False
                except Exception as e_retry:
                    logger.error(f"[RECREATE] ❌ Ошибка при обработке дубликата {email}: {e_retry}", exc_info=True)
                    # Логируем ошибку в БД
                    try:
                        await db_helpers.log_client_recreation_error(
                            telegram_id=user_data.get('telegram_id', 0),
                            client_uuid=user_data.get('uuid', ''),
                            server_id=server_settings.get('id'),
                            server_name=server_settings.get('name', 'Unknown'),
                            error_type='duplicate_handling_failed',
                            error_message=f"Ошибка обработки дубликата: {str(e_retry)[:500]}"
                        )
                    except Exception:
                        pass
                    return False
            else:
                # Другие ValueError (не дубликат) - пробуем relogin
                logger.warning(f"ValueError при добавлении {user_data['email']}: {ve}")
                # Не обрабатываем здесь, пусть падает дальше для обработки в общем блоке
                raise ve
        except Exception as e:
            # Если проблема с авторизацией, делаем relogin и пробуем ещё раз
            emsg = str(e).lower()
            if '401' in emsg or 'unauth' in emsg:
                try:
                    logger.warning(f"Авторизация истекла при добавлении {user_data['email']}. Перелогин и повтор")
                    await client_api.login()
                    await client_api.client.add(inbound_id=inbound_id, clients=[client_to_add])
                    return True
                except Exception as e_retry:
                    logger.error(f"Повторная попытка add после логина провалена: {e_retry}")
                    # Логируем ошибку в БД
                    try:
                        await db_helpers.log_client_recreation_error(
                            telegram_id=user_data.get('telegram_id', 0),
                            client_uuid=user_data.get('uuid', ''),
                            server_id=server_settings.get('id'),
                            server_name=server_settings.get('name', 'Unknown'),
                            error_type='recreation_failed',
                            error_message=f"Ошибка после перелогина: {str(e_retry)[:500]}"
                        )
                    except Exception:
                        pass
                    return False
            else:
                logger.error(f"Ошибка воссоздания {user_data['email']}: {e}", exc_info=True)
                # Логируем ошибку в БД для отчета
                try:
                    await db_helpers.log_client_recreation_error(
                        telegram_id=user_data.get('telegram_id', 0),
                        client_uuid=user_data.get('uuid', ''),
                        server_id=server_settings.get('id'),
                        server_name=server_settings.get('name', 'Unknown'),
                        error_type='recreation_failed',
                        error_message=str(e)[:500]  # Ограничиваем длину сообщения
                    )
                except Exception as log_err:
                    logger.warning(f"Не удалось залогировать ошибку восстановления в БД: {log_err}")
                return False

    async def recreate_xui_users_batch(self, server_settings: Dict, users_data: List[Dict[str, Any]]):
        """
        Пытается добавить список клиентов одним запросом. При неудаче выполняет поштучно с существующей логикой.
        users_data: [{'uuid','email','expiry_timestamp_ms','telegram_id','limit_ip'}]
        Возвращает (success_list, failed_list), где элементы содержат исходный user_data и, для ошибок, поле 'error'.
        """
        logger.info(f"[BATCH] Начало recreate_xui_users_batch: {len(users_data)} пользователей, сервер ID={server_settings.get('id')}")
        success_list: List[Dict[str, Any]] = []
        failed_list: List[Dict[str, Any]] = []

        client_api = await self.get_client(server_settings)
        if not client_api:
            logger.error(f"[BATCH] ❌ Не удалось получить client_api для сервера {server_settings.get('id')}")
            for u in users_data:
                failed_list.append({"user": u, "error": "no_client_api"})
            return success_list, failed_list
        logger.info(f"[BATCH] ✅ client_api получен для сервера {server_settings.get('id')}")

        inbound_id = server_settings.get('inbound_id')
        if not inbound_id:
            logger.error(f"[BATCH] ❌ inbound_id не указан в настройках сервера {server_settings.get('id')}")
            for u in users_data:
                failed_list.append({"user": u, "error": "no_inbound_id"})
            return success_list, failed_list
        
        logger.info(f"[BATCH] Проверяем существование inbound с ID={inbound_id}")
        inbound_exists = await self._find_inbound_by_id(client_api, inbound_id)
        if not inbound_exists:
            logger.error(f"[BATCH] ❌ Inbound с ID={inbound_id} не найден на сервере {server_settings.get('id')}")
            # Попробуем получить список всех inbound для диагностики
            try:
                all_inbounds = await client_api.inbound.list()
                inbound_ids = [inb.id for inb in all_inbounds] if all_inbounds else []
                logger.error(f"[BATCH] Доступные inbound IDs на сервере: {inbound_ids}")
            except Exception as diag_err:
                logger.error(f"[BATCH] Не удалось получить список inbound для диагностики: {diag_err}")
            for u in users_data:
                failed_list.append({"user": u, "error": "no_inbound"})
            return success_list, failed_list
        logger.info(f"[BATCH] ✅ Inbound найден: ID={inbound_id}")

        # Используем вспомогательную функцию для создания Client в зависимости от протокола
        client_objs: List[Client] = []
        for u in users_data:
            client_objs.append(self._create_client_obj(
                server_settings=server_settings,
                client_uuid=u['uuid'],
                email=u['email'],
                telegram_id=u['telegram_id'],
                expiry_timestamp_ms=u['expiry_timestamp_ms'],
                total_gb=0,
                limit_ip=u.get('limit_ip', 0)
            ))

        # Пытаемся отправить одним запросом
        # Если клиентов слишком много, разбиваем на пакеты по 200
        MAX_BATCH_SIZE = 1000
        if len(client_objs) > MAX_BATCH_SIZE:
            logger.info(f"[BATCH] Клиентов слишком много ({len(client_objs)}), разбиваем на пакеты по {MAX_BATCH_SIZE}")
            chunk_size = MAX_BATCH_SIZE
            for chunk_start in range(0, len(users_data), chunk_size):
                chunk_end = min(chunk_start + chunk_size, len(users_data))
                chunk_users = users_data[chunk_start:chunk_end]
                chunk_clients = client_objs[chunk_start:chunk_end]
                try:
                    logger.info(f"[BATCH] Пакет {chunk_start//chunk_size + 1}: добавляем {len(chunk_clients)} клиентов")
                    await client_api.client.add(inbound_id=inbound_id, clients=chunk_clients)
                    logger.info(f"[BATCH] ✅ Пакет {chunk_start//chunk_size + 1} успешно добавлен!")
                    success_list.extend([{"user": u} for u in chunk_users])
                except ValueError as ve:
                    error_msg = str(ve)
                    logger.warning(f"[BATCH] ⚠️ Пакет {chunk_start//chunk_size + 1} не удался (ValueError): {error_msg}")
                    
                    # Проверяем, были ли клиенты добавлены, несмотря на ValueError
                    if "not successful" in error_msg.lower():
                        logger.info(f"[BATCH] Проверяем, были ли клиенты из пакета {chunk_start//chunk_size + 1} добавлены...")
                        check_count = min(5, len(chunk_users))
                        found_count = 0
                        try:
                            inbound = await self._find_inbound_by_id(client_api, inbound_id)
                            if inbound and hasattr(inbound, 'settings') and inbound.settings:
                                if hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                                    existing_emails = {c.email.lower() for c in inbound.settings.clients if hasattr(c, 'email')}
                                    for i in range(check_count):
                                        if chunk_users[i]['email'].lower() in existing_emails:
                                            found_count += 1
                        except Exception as check_err:
                            logger.debug(f"[BATCH] Ошибка при проверке пакета: {check_err}")
                        
                        if found_count == check_count:
                            # Все проверенные клиенты найдены - считаем пакет успешным
                            logger.info(f"[BATCH] ✅ Пакет {chunk_start//chunk_size + 1}: все проверенные клиенты найдены. Считаем успешным!")
                            success_list.extend([{"user": u} for u in chunk_users])
                            continue  # Переходим к следующему пакету
                    
                    # Для этого пакета идём поштучно
                    for u in chunk_users:
                        try:
                            ok = await self.recreate_xui_user(server_settings, u)
                            if ok:
                                success_list.append({"user": u})
                            else:
                                failed_list.append({"user": u, "error": "single_add_failed"})
                        except Exception as ue:
                            failed_list.append({"user": u, "error": str(ue)})
                except Exception as chunk_err:
                    logger.error(f"[BATCH] ❌ Пакет {chunk_start//chunk_size + 1} не удался: {chunk_err}")
                    # Для этого пакета идём поштучно
                    for u in chunk_users:
                        try:
                            ok = await self.recreate_xui_user(server_settings, u)
                            if ok:
                                success_list.append({"user": u})
                            else:
                                failed_list.append({"user": u, "error": "single_add_failed"})
                        except Exception as ue:
                            failed_list.append({"user": u, "error": str(ue)})
            # Возвращаем результат после обработки всех пакетов
            logger.info(f"[BATCH] Завершено: успешно {len(success_list)}, ошибок {len(failed_list)}")
            return success_list, failed_list
        
        try:
            logger.info(f"[BATCH] Пытаемся добавить {len(client_objs)} клиентов одним запросом на сервер {server_settings.get('id')}")
            logger.info(f"[BATCH] Inbound ID: {inbound_id}, первый клиент: email={client_objs[0].email if client_objs else 'N/A'}")
            
            # Пробуем вызвать напрямую через _post, чтобы перехватить ответ
            logger.info(f"[BATCH] Вызываем client_api.client.add с {len(client_objs)} клиентами...")
            await client_api.client.add(inbound_id=inbound_id, clients=client_objs)
            # Если прошло без исключения, считаем всех успешными
            logger.info(f"[BATCH] ✅ Успешно добавлено {len(client_objs)} клиентов одним запросом!")
            success_list = [{"user": u} for u in users_data]
            return success_list, failed_list
        except ValueError as ve:
            # ValueError из-за success=false в ответе - возможно, некоторые клиенты уже существуют
            error_msg = str(ve)
            error_type = type(ve).__name__
            logger.warning(f"[BATCH] ⚠️ ValueError при batch add: {error_type}: {error_msg}")
            
            # Если это ошибка "not successful", но HTTP был 200, возможно клиенты все равно добавились
            # Проверяем несколько случайных клиентов, чтобы понять, были ли они добавлены
            if "not successful" in error_msg.lower():
                logger.info(f"[BATCH] X-UI API вернул success=false, но HTTP был 200. Проверяем, были ли клиенты добавлены...")
                # Проверяем первые 5 клиентов из batch
                check_count = min(5, len(users_data))
                found_count = 0
                try:
                    inbound = await self._find_inbound_by_id(client_api, inbound_id)
                    if inbound and hasattr(inbound, 'settings') and inbound.settings:
                        if hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                            # Создаем множество email для быстрой проверки
                            existing_emails = {c.email.lower() for c in inbound.settings.clients if hasattr(c, 'email')}
                            for i in range(check_count):
                                if users_data[i]['email'].lower() in existing_emails:
                                    found_count += 1
                except Exception as check_err:
                    logger.debug(f"[BATCH] Ошибка при проверке клиентов: {check_err}")
                
                if found_count == check_count:
                    # Все проверенные клиенты найдены - считаем batch успешным
                    logger.info(f"[BATCH] ✅ Все проверенные клиенты ({found_count}/{check_count}) найдены на сервере. Считаем batch успешным!")
                    success_list = [{"user": u} for u in users_data]
                    return success_list, failed_list
                elif found_count > 0:
                    logger.warning(f"[BATCH] Найдено только {found_count}/{check_count} клиентов. Возможно частичный успех. Переходим на поштучную обработку.")
                else:
                    logger.warning(f"[BATCH] Клиенты не найдены на сервере. Переходим на поштучную обработку.")
            # Переходим на поштучную обработку
            # (продолжаем выполнение ниже для поштучной обработки)
        except Exception as e:
            emsg = str(e).lower()
            error_str = str(e)
            error_type = type(e).__name__
            logger.error(f"[BATCH] ❌ Ошибка при batch add ({len(client_objs)} клиентов): {error_type}: {error_str}")
            logger.error(f"[BATCH] Полный traceback:", exc_info=True)
            
            # Проверяем, может быть это ошибка дубликатов - тогда некоторые клиенты уже добавлены
            if "duplicate" in emsg or "already exists" in emsg:
                logger.warning(f"[BATCH] Обнаружены дубликаты. Возможно, некоторые клиенты уже были добавлены успешно.")
                # В этом случае можем попробовать проверить, какие клиенты были добавлены
                # Но для простоты просто переходим на поштучную обработку для точности
            
            # Попытка relogin при 401
            if '401' in emsg or 'unauth' in emsg:
                try:
                    logger.warning("[BATCH] Авторизация истекла, выполняем relogin и повтор")
                    await client_api.login()
                    await client_api.client.add(inbound_id=inbound_id, clients=client_objs)
                    logger.info(f"[BATCH] ✅ Успешно добавлено {len(client_objs)} клиентов после relogin!")
                    success_list = [{"user": u} for u in users_data]
                    return success_list, failed_list
                except Exception as e2:
                    logger.error(f"[BATCH] Повторная попытка после логина провалена: {e2}")
                    # Пойдём поштучно
            # Проверяем, может ли быть проблема с размером запроса
            elif 'timeout' in emsg or 'too large' in emsg or '413' in error_str or 'payload' in emsg:
                logger.warning(f"[BATCH] Похоже на проблему с размером запроса ({len(client_objs)} клиентов). Пробуем разбить на меньшие пакеты...")
                # Пробуем разбить на пакеты по 100
                chunk_size = 100
                for chunk_start in range(0, len(users_data), chunk_size):
                    chunk_end = min(chunk_start + chunk_size, len(users_data))
                    chunk_users = users_data[chunk_start:chunk_end]
                    chunk_clients = client_objs[chunk_start:chunk_end]
                    try:
                        logger.info(f"[BATCH] Пробуем пакет {chunk_start//chunk_size + 1}: {len(chunk_clients)} клиентов")
                        await client_api.client.add(inbound_id=inbound_id, clients=chunk_clients)
                        logger.info(f"[BATCH] ✅ Пакет {chunk_start//chunk_size + 1} успешно добавлен!")
                        success_list.extend([{"user": u} for u in chunk_users])
                    except Exception as chunk_err:
                        logger.error(f"[BATCH] Пакет {chunk_start//chunk_size + 1} не удался: {chunk_err}. Переходим на поштучную обработку для этого пакета")
                        # Для этого пакета идём поштучно
                        for u in chunk_users:
                            try:
                                ok = await self.recreate_xui_user(server_settings, u)
                                if ok:
                                    success_list.append({"user": u})
                                else:
                                    failed_list.append({"user": u, "error": "single_add_failed"})
                            except Exception as ue:
                                failed_list.append({"user": u, "error": str(ue)})
                # Если хотя бы что-то добавилось пакетами, возвращаем результат
                if success_list:
                    logger.info(f"[BATCH] Частично успешно: {len(success_list)} из {len(users_data)} добавлено пакетами")
                    return success_list, failed_list
                # Если ничего не добавилось, продолжаем на поштучную обработку всех
            else:
                logger.warning(f"[BATCH] Неизвестная ошибка. Переходим на поштучную обработку всех {len(users_data)} клиентов")

        # Фоллбэк на поштучное добавление
        # При поштучной обработке, если клиент уже существует, считаем это успехом
        for u in users_data:
            try:
                ok = await self.recreate_xui_user(server_settings, u)
                if ok:
                    success_list.append({"user": u})
                else:
                    # Проверяем, может быть клиент уже существует - тогда это успех
                    # Попробуем проверить существование клиента на сервере через API
                    try:
                        inbound_id = server_settings.get('inbound_id')
                        inbound = await self._find_inbound_by_id(client_api, inbound_id)
                        if inbound and hasattr(inbound, 'settings') and inbound.settings:
                            # Проверяем клиентов в settings
                            if hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                                for c in inbound.settings.clients:
                                    if hasattr(c, 'email') and c.email.lower() == u['email'].lower():
                                        logger.info(f"[BATCH] Клиент {u['email']} уже существует на сервере, считаем успехом")
                                        success_list.append({"user": u})
                                        ok = True
                                        break
                    except Exception as check_err:
                        logger.debug(f"[BATCH] Не удалось проверить существование клиента {u['email']}: {check_err}")
                    
                    if not ok:
                        failed_list.append({"user": u, "error": "single_add_failed"})
            except ValueError as ve:
                # ValueError из-за success=false - возможно, клиент уже существует
                error_msg = str(ve).lower()
                if "duplicate email" in error_msg or "not successful" in error_msg or "duplicate" in error_msg:
                    logger.warning(f"[BATCH] Клиент {u['email']} уже существует (ValueError: {ve}). Считаем успехом.")
                    success_list.append({"user": u})
                else:
                    failed_list.append({"user": u, "error": f"ValueError: {ve}"})
            except Exception as ue:
                failed_list.append({"user": u, "error": str(ue)})

        # Логируем все ошибки из failed_list в таблицу client_recreation_errors
        for failed_item in failed_list:
            user_data = failed_item.get("user", {})
            error_msg = failed_item.get("error", "Unknown error")
            try:
                await db_helpers.log_client_recreation_error(
                    telegram_id=user_data.get('telegram_id', 0),
                    client_uuid=user_data.get('uuid', ''),
                    server_id=server_settings.get('id'),
                    server_name=server_settings.get('name', 'Unknown'),
                    error_type='batch_recreation_failed',
                    error_message=f"Ошибка при batch создании: {str(error_msg)[:400]}"
                )
            except Exception as log_err:
                logger.warning(f"Не удалось залогировать ошибку batch в БД: {log_err}")

        return success_list, failed_list

    async def create_xui_user(self, server_settings: Dict, telegram_id: int, days_valid: int, total_gb: int = 0, limit_ip: int = 0, client_uuid: Optional[str] = None, client_email: Optional[str] = None, expiry_timestamp_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        server_id = server_settings.get('id', 'N/A')
        server_name = server_settings.get('name', 'N/A')
        logger.info(f"[CREATE_XUI_USER] Начало создания пользователя {telegram_id} на сервере {server_id} ({server_name})")
        
        client_api = await self.get_client(server_settings)
        if not client_api:
            logger.error(f"[CREATE_XUI_USER] Не удалось получить API клиент для сервера {server_id} ({server_name})")
            return None

        try:
            inbound_id = server_settings['inbound_id']
            logger.debug(f"[CREATE_XUI_USER] Создаем клиента в inbound {inbound_id} на сервере {server_id}")

            # Используем переданный UUID (например, из Remnawave) или генерируем новый
            if not client_uuid:
                client_uuid = str(uuid.uuid4())
            else:
                logger.info(f"[CREATE_XUI_USER] Используется переданный UUID: {client_uuid}")
            
            # Используем переданный email или генерируем новый
            if client_email:
                email = client_email
                logger.info(f"[CREATE_XUI_USER] Используется переданный email: {email}")
            else:
                email = f"tg{telegram_id}_{''.join(random.choices('abcdef0123456789', k=6))}@{app_conf.get('email_domain', 'router.bot')}"
                logger.info(f"[CREATE_XUI_USER] Сгенерирован новый email: {email}")
            
            # Используем переданную дату истечения или рассчитываем от текущего момента
            if expiry_timestamp_ms is not None:
                logger.info(f"[CREATE_XUI_USER] Используется переданная дата истечения: {expiry_timestamp_ms} ({datetime.fromtimestamp(expiry_timestamp_ms / 1000, tz=timezone.utc)})")
            else:
                now_utc = datetime.now(timezone.utc)
                expiry_timestamp_ms = int((now_utc + timedelta(days=days_valid)).timestamp() * 1000)
                logger.info(f"[CREATE_XUI_USER] Рассчитана дата истечения от текущего момента: {expiry_timestamp_ms} ({datetime.fromtimestamp(expiry_timestamp_ms / 1000, tz=timezone.utc)})")
            
            # Используем вспомогательную функцию для создания Client в зависимости от протокола
            new_client_obj = self._create_client_obj(
                server_settings=server_settings,
                client_uuid=client_uuid,
                email=email,
                telegram_id=telegram_id,
                expiry_timestamp_ms=expiry_timestamp_ms,
                total_gb=total_gb,
                limit_ip=limit_ip
            )
            # Пытаемся сразу создать клиента (оптимизация: сначала создаем, потом обрабатываем ошибки)
            try:
                await client_api.client.add(inbound_id=inbound_id, clients=[new_client_obj])
                logger.info(f"[CREATE_XUI_USER] ✅ Клиент {email} успешно создан на сервере {server_id}")
                return {
                    "uuid": client_uuid, "email": email,
                    "expiry_timestamp_ms": expiry_timestamp_ms, "server_id": server_settings['id']
                }
            except ValueError as ve:
                # Обработка ошибок: duplicate email, несуществующий inbound и т.д.
                error_msg = str(ve).lower()
                
                if "inbound" in error_msg and ("not found" in error_msg or "404" in error_msg or "does not exist" in error_msg):
                    logger.error(f"[CREATE_XUI_USER] ❌ Inbound {inbound_id} не найден на сервере {server_id} ({server_name}): {ve}")
                    return None
                elif "duplicate email" in error_msg or "duplicate" in error_msg:
                    # Дубликат - пытаемся найти существующего клиента через быстрый API get_by_email
                    logger.warning(f"[CREATE_XUI_USER] ⚠️ Дубликат email {email} на сервере {server_id}. Ищем существующего клиента через get_by_email...")
                    try:
                        # Используем быстрый метод get_by_email вместо _find_client_by_email_or_uuid (не загружает весь inbound)
                        existing_client = await client_api.client.get_by_email(email)
                        if existing_client and existing_client.inbound_id == inbound_id:
                            # Клиент найден - возвращаем его данные
                            logger.info(f"[CREATE_XUI_USER] ✅ Клиент {email} уже существует на сервере {server_id}, возвращаем существующие данные")
                            existing_uuid = getattr(existing_client, 'id', None) or getattr(existing_client, 'password', None) or client_uuid
                            return {
                                "uuid": existing_uuid, "email": email,
                                "expiry_timestamp_ms": expiry_timestamp_ms, "server_id": server_settings['id']
                            }
                        else:
                            # Клиент найден на другом inbound или не найден - пробуем создать снова
                            logger.debug(f"[CREATE_XUI_USER] Клиент {email} найден на другом inbound ({existing_client.inbound_id if existing_client else 'N/A'}) или не найден, повторная попытка через 0.5 сек")
                            await asyncio.sleep(0.5)
                            await client_api.client.add(inbound_id=inbound_id, clients=[new_client_obj])
                            logger.info(f"[CREATE_XUI_USER] ✅ Клиент {email} создан на сервере {server_id} после повторной попытки")
                            return {
                                "uuid": client_uuid, "email": email,
                                "expiry_timestamp_ms": expiry_timestamp_ms, "server_id": server_settings['id']
                            }
                    except Exception as find_error:
                        error_str = str(find_error).lower()
                        # Если клиент не найден через get_by_email - пробуем создать снова
                        if 'not found' in error_str or '404' in error_str:
                            logger.debug(f"[CREATE_XUI_USER] Клиент {email} не найден через get_by_email, повторная попытка создания через 0.5 сек")
                            await asyncio.sleep(0.5)
                            await client_api.client.add(inbound_id=inbound_id, clients=[new_client_obj])
                            logger.info(f"[CREATE_XUI_USER] ✅ Клиент {email} создан на сервере {server_id} после повторной попытки")
                            return {
                                "uuid": client_uuid, "email": email,
                                "expiry_timestamp_ms": expiry_timestamp_ms, "server_id": server_settings['id']
                            }
                        else:
                            # Другая ошибка при поиске - логируем и пробрасываем исходную ошибку
                            logger.error(f"[CREATE_XUI_USER] ❌ Ошибка при поиске существующего клиента {email}: {find_error}")
                            raise ve
                else:
                    # Другая ошибка ValueError - логируем и пробрасываем дальше
                    logger.error(f"[CREATE_XUI_USER] ❌ Неизвестная ошибка при создании клиента {email}: {ve}")
                    raise

            logger.info(f"Клиент {email} создан на сервере {server_settings['id']}.")
            return {
                "uuid": client_uuid, "email": email,
                "expiry_timestamp_ms": expiry_timestamp_ms, "server_id": server_settings['id']
            }
        except Exception as e:
            logger.error(f"Критическая ошибка создания X-UI юзера для {telegram_id}: {e}", exc_info=True)
            return None

    async def update_xui_user_subscription(self, server_settings: Dict, client_uuid: str, new_days_valid: int, current_expiry_ms: Optional[int] = None, total_gb: int = 0, limit_ip: int = 0) -> Optional[Dict[str, Any]]:
        client_api = await self.get_client(server_settings)
        if not client_api: return None

        try:
            inbound_id = server_settings['inbound_id']
            telegram_user_id = server_settings.get('telegram_id')
            if not telegram_user_id:
                logger.error(f"Нет telegram_id для обновления подписки {client_uuid}")
                return None

            # Получаем email из БД (быстро, без запросов к API)
            user_db_data = await db_helpers.get_user(telegram_user_id)
            email_from_db = user_db_data['xui_client_email'] if user_db_data else None
            if not email_from_db:
                logger.error(f"Нет email в БД для {client_uuid}, не могу обновить.")
                return None

            # Используем UTC для всех операций с временем
            now_utc = datetime.now(timezone.utc)
            now_utc_ms = int(now_utc.timestamp() * 1000)
            
            if current_expiry_ms and current_expiry_ms > now_utc_ms:
                # Используем время из БД (в UTC)
                base_time = datetime.fromtimestamp(current_expiry_ms / 1000, tz=timezone.utc)
            else:
                # Используем текущее время UTC
                base_time = now_utc
            
            new_expiry_timestamp_ms = int((base_time + timedelta(days=new_days_valid)).timestamp() * 1000)

            # Пытаемся сразу обновить через get_by_email (быстрый метод)
            client_from_xui = None
            try:
                client_from_xui = await client_api.client.get_by_email(email_from_db)
                if client_from_xui and client_from_xui.inbound_id == inbound_id:
                    # Клиент найден, обновляем его
                    updated_client_obj = Client.model_validate(client_from_xui.model_dump())
                    updated_client_obj.expiry_time = new_expiry_timestamp_ms
                    updated_client_obj.enable = True
                    updated_client_obj.total_gb = total_gb
                    updated_client_obj.limit_ip = limit_ip or client_from_xui.limit_ip
                    updated_client_obj.inbound_id = inbound_id
                    updated_client_obj.sub_id = client_uuid
                    
                    # Восстанавливаем flow из настроек сервера (как при создании)
                    flow_value = (server_settings.get('client_flow') or '').strip()
                    updated_client_obj.flow = flow_value
                    if flow_value:
                        logger.debug(f"[UPDATE] Установлен flow='{flow_value}' для клиента {client_uuid} из настроек сервера")

                    # Определяем протокол для правильного обновления
                    protocol = (server_settings.get('protocol') or 'vless').strip().lower()
                    
                    if protocol == 'trojan':
                        updated_client_obj.password = client_uuid
                        await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)
                    elif protocol == 'shadowsocks':
                        await client_api.client.update(client_uuid=client_from_xui.email, client=updated_client_obj)
                    elif _is_hysteria(protocol):
                        updated_client_obj.auth = client_uuid
                        # Для hysteria flow не применим — сбросим, чтобы случайно не уехал
                        updated_client_obj.flow = ""
                        await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)
                    else:
                        updated_client_obj.id = client_uuid
                        await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)

                    logger.info(f"[UPDATE] ✅ Клиент {client_uuid} успешно обновлен на сервере {server_settings.get('name', 'Unknown')}")
                    return {
                        "uuid": client_uuid, "email": client_from_xui.email,
                        "expiry_timestamp_ms": new_expiry_timestamp_ms, "server_id": server_settings['id']
                    }
                elif client_from_xui is None:
                    # Клиент не найден через get_by_email - создаем его
                    logger.warning(f"Клиент {client_uuid} не найден в X-UI при попытке обновления (get_by_email вернул None). Создаем клиента через create_xui_user...")
                    # Используем create_xui_user вместо recreate_xui_user для оптимизации
                    # Передаем уже рассчитанную дату истечения (new_expiry_timestamp_ms), чтобы она совпадала с БД
                    recreate_result = await self.create_xui_user(
                        server_settings=server_settings,
                        telegram_id=telegram_user_id,
                        days_valid=new_days_valid,  # Используется только если expiry_timestamp_ms не передан
                        limit_ip=limit_ip,
                        client_uuid=client_uuid,
                        client_email=email_from_db,  # Используем email из БД
                        expiry_timestamp_ms=new_expiry_timestamp_ms  # Используем уже рассчитанную дату истечения из БД
                    )
                    recreate_success = bool(recreate_result)
                    
                    if not recreate_success:
                        await db_helpers.log_client_recreation_error(
                            telegram_id=telegram_user_id,
                            client_uuid=client_uuid,
                            server_id=server_settings.get('id'),
                            server_name=server_settings.get('name', 'Unknown'),
                            error_type='recreation_failed',
                            error_message=f"Не удалось восстановить клиента на сервере"
                        )
                    
                    return {"uuid": client_uuid, "email": email_from_db, "expiry_timestamp_ms": new_expiry_timestamp_ms, "server_id": server_settings['id']} if recreate_success else None
                else:
                    # Клиент найден, но на другом inbound - это ошибка конфигурации
                    logger.warning(f"Клиент {client_uuid} найден на другом inbound ({client_from_xui.inbound_id} != {inbound_id}). Пропускаем обновление.")
                    return None
            except Exception as update_error:
                error_str = str(update_error).lower()
                # Если клиент не найден (404/not found) - создаем его
                if 'not found' in error_str or '404' in error_str or 'does not exist' in error_str:
                    logger.warning(f"Клиент {client_uuid} не найден в X-UI при попытке обновления (исключение: {update_error}). Создаем клиента через create_xui_user...")
                    # Используем create_xui_user вместо recreate_xui_user для оптимизации
                    # Передаем уже рассчитанную дату истечения (new_expiry_timestamp_ms), чтобы она совпадала с БД
                    recreate_result = await self.create_xui_user(
                        server_settings=server_settings,
                        telegram_id=telegram_user_id,
                        days_valid=new_days_valid,  # Используется только если expiry_timestamp_ms не передан
                        limit_ip=limit_ip,
                        client_uuid=client_uuid,
                        client_email=email_from_db,  # Используем email из БД
                        expiry_timestamp_ms=new_expiry_timestamp_ms  # Используем уже рассчитанную дату истечения из БД
                    )
                    recreate_success = bool(recreate_result)
                    
                    if not recreate_success:
                        await db_helpers.log_client_recreation_error(
                            telegram_id=telegram_user_id,
                            client_uuid=client_uuid,
                            server_id=server_settings.get('id'),
                            server_name=server_settings.get('name', 'Unknown'),
                            error_type='recreation_failed',
                            error_message=f"Не удалось восстановить клиента на сервере"
                        )
                    
                    return {"uuid": client_uuid, "email": email_from_db, "expiry_timestamp_ms": new_expiry_timestamp_ms, "server_id": server_settings['id']} if recreate_success else None
                else:
                    # Другие ошибки API - логируем и возвращаем None
                    logger.error(f"Ошибка API при обновлении клиента {client_uuid} на сервере {server_settings.get('name', 'Unknown')}: {update_error}")
                    await db_helpers.log_client_recreation_error(
                        telegram_id=telegram_user_id,
                        client_uuid=client_uuid,
                        server_id=server_settings.get('id'),
                        server_name=server_settings.get('name', 'Unknown'),
                        error_type='api_error',
                        error_message=f"Ошибка API при обновлении клиента: {update_error}"
                    )
                    return None

        except Exception as e:
            logger.error(f"Критическая ошибка обновления X-UI юзера {client_uuid}: {e}", exc_info=True)
            return None

    async def update_xui_user_limit_ip(self, server_settings: Dict, client_uuid: str, limit_ip: int) -> bool:
        """
        Обновляет только лимит устройств для клиента X-UI
        """
        client_api = await self.get_client(server_settings)
        if not client_api: 
            return False

        try:
            inbound_id = server_settings['inbound_id']
            
            # Находим клиента
            search_result = await self._find_client_by_email_or_uuid(client_api, inbound_id, client_uuid)
            client_from_xui = search_result.client
            if not client_from_xui:
                logger.warning(f"Клиент {client_uuid} не найден для обновления limit_ip")
                return False

            # Создаем обновленный объект клиента
            updated_client_obj = Client.model_validate(client_from_xui.model_dump())
            updated_client_obj.limit_ip = limit_ip
            updated_client_obj.inbound_id = inbound_id
            updated_client_obj.sub_id = client_uuid  # ✅ КРИТИЧЕСКИ ВАЖНО: sub_id всегда = UUID из Remnawave для 3XUI

            # Определяем протокол для правильного обновления
            protocol = (server_settings.get('protocol') or 'vless').strip().lower()
            
            # Для разных протоколов устанавливаем правильные идентификаторы в объекте
            # Логика как в bek, но client_uuid - это UUID из Remnawave для 3XUI
            if protocol == 'trojan':
                # Для Trojan: устанавливаем password = client_uuid (UUID из Remnawave для 3XUI)
                updated_client_obj.password = client_uuid
                logger.debug(f"[UPDATE_LIMIT_IP] Протокол {protocol}, установлен password={client_uuid}, sub_id={client_uuid} для обновления")
                await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)
            elif protocol == 'shadowsocks':
                # Для Shadowsocks используем email как идентификатор
                client_identifier = client_from_xui.email
                logger.debug(f"[UPDATE_LIMIT_IP] Протокол {protocol}, используем email={client_identifier}, sub_id={client_uuid} для обновления")
                await client_api.client.update(client_uuid=client_identifier, client=updated_client_obj)
            elif _is_hysteria(protocol):
                # Для Hysteria2: устанавливаем auth = client_uuid
                updated_client_obj.auth = client_uuid
                updated_client_obj.flow = ""  # для hysteria flow не применим
                logger.debug(f"[UPDATE_LIMIT_IP] Протокол {protocol}, установлен auth={client_uuid}, sub_id={client_uuid} для обновления")
                await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)
            else:
                # Для VLESS/VMESS: устанавливаем id = client_uuid (UUID из Remnawave для 3XUI)
                updated_client_obj.id = client_uuid
                logger.debug(f"[UPDATE_LIMIT_IP] Протокол {protocol}, установлен id={client_uuid}, sub_id={client_uuid} для обновления")
                await client_api.client.update(client_uuid=client_uuid, client=updated_client_obj)
            
            logger.info(f"Лимит устройств для клиента {client_uuid} обновлен на {limit_ip}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обновления limit_ip для клиента {client_uuid}: {e}")
            return False

    async def update_xui_user_uuid(self, server_settings: Dict, old_client_uuid: str, new_client_uuid: str, user_email: str, limit_ip_from_db: Optional[int] = None) -> bool:
        """
        Обновляет UUID клиента в 3X-UI (id/password/sub_id) на новое значение.
        Сохраняет все остальные данные клиента (expiry_time, total_gb, limit_ip и т.д.)
        
        Args:
            server_settings: Настройки сервера
            old_client_uuid: Старый UUID клиента
            new_client_uuid: Новый UUID клиента
            user_email: Email клиента для поиска
            limit_ip_from_db: Лимит устройств из БД (опционально, используется если в клиенте нет или 0)
            
        Returns:
            True если успешно, False иначе
        """
        client_api = await self.get_client(server_settings)
        if not client_api:
            return False

        try:
            inbound_id = server_settings['inbound_id']
            
            # Находим клиента по email с таймаутом
            try:
                logger.debug(f"[REVOKE] Поиск клиента по email {user_email} на сервере {server_settings.get('name', 'Unknown')}")
                client_from_xui = await asyncio.wait_for(
                    client_api.client.get_by_email(user_email),
                    timeout=5.0
                )
                if not client_from_xui or client_from_xui.inbound_id != inbound_id:
                    logger.warning(f"[REVOKE] Клиент с email {user_email} не найден на сервере {server_settings.get('name', 'Unknown')} для обновления UUID")
                    return False
                logger.debug(f"[REVOKE] Клиент найден на сервере {server_settings.get('name', 'Unknown')}")
            except asyncio.TimeoutError:
                logger.error(f"[REVOKE] Таймаут поиска клиента по email {user_email} на сервере {server_settings.get('name', 'Unknown')}")
                return False
            except Exception as e:
                logger.warning(f"[REVOKE] Ошибка поиска клиента по email {user_email} на сервере {server_settings.get('name', 'Unknown')}: {e}")
                return False

            # Создаем обновленный объект клиента с новым UUID
            updated_client_obj = Client.model_validate(client_from_xui.model_dump())
            updated_client_obj.inbound_id = inbound_id
            updated_client_obj.sub_id = new_client_uuid  # Обновляем sub_id на новый UUID

            # Сохраняем limit_ip из существующего клиента или из БД (ВАЖНО: сохраняем лимит устройств)
            existing_limit_ip = getattr(client_from_xui, 'limit_ip', None) or getattr(client_from_xui, 'limitIp', None)
            if existing_limit_ip is None:
                existing_limit_ip = 0
            
            # Приоритет: значение из БД (если передано) > значение из клиента > 0
            # БД является источником истины, поэтому используем его значение если оно передано
            if limit_ip_from_db is not None:
                final_limit_ip = limit_ip_from_db
                logger.debug(f"[REVOKE] Используем limit_ip из БД: {final_limit_ip} (было в клиенте: {existing_limit_ip}) на сервере {server_settings.get('name', 'Unknown')}")
            elif existing_limit_ip is not None:
                final_limit_ip = existing_limit_ip
                logger.debug(f"[REVOKE] Используем limit_ip из клиента: {final_limit_ip} на сервере {server_settings.get('name', 'Unknown')}")
            else:
                final_limit_ip = 0
                logger.debug(f"[REVOKE] Используем limit_ip по умолчанию: 0 на сервере {server_settings.get('name', 'Unknown')}")
            
            updated_client_obj.limit_ip = final_limit_ip
            logger.info(f"[REVOKE] Установлен limit_ip: {final_limit_ip} на сервере {server_settings.get('name', 'Unknown')}")

            # Сохраняем flow из существующего клиента или из настроек сервера
            # Приоритет: существующий flow клиента > настройки сервера > пустая строка
            existing_flow = getattr(client_from_xui, 'flow', None) or ''
            existing_flow = existing_flow.strip() if isinstance(existing_flow, str) else ''
            flow_from_settings = (server_settings.get('client_flow') or '').strip()
            
            if existing_flow:
                updated_client_obj.flow = existing_flow
                logger.debug(f"[REVOKE] Сохранен существующий flow клиента: '{existing_flow}' на сервере {server_settings.get('name', 'Unknown')}")
            elif flow_from_settings:
                updated_client_obj.flow = flow_from_settings
                logger.debug(f"[REVOKE] Установлен flow из настроек сервера: '{flow_from_settings}' на сервере {server_settings.get('name', 'Unknown')}")
            else:
                updated_client_obj.flow = ''
                logger.debug(f"[REVOKE] Flow не найден, устанавливаем пустую строку на сервере {server_settings.get('name', 'Unknown')}")

            # Определяем протокол для правильного обновления
            protocol = (server_settings.get('protocol') or 'vless').strip().lower()
            
            # Для разных протоколов устанавливаем правильные идентификаторы
            try:
                if protocol == 'trojan':
                    # Для Trojan: устанавливаем password = new_client_uuid
                    updated_client_obj.password = new_client_uuid
                    logger.info(f"[REVOKE] Обновление UUID для Trojan: password={new_client_uuid}, sub_id={new_client_uuid} на сервере {server_settings.get('name', 'Unknown')}")
                    await asyncio.wait_for(
                        client_api.client.update(client_uuid=old_client_uuid, client=updated_client_obj),
                        timeout=5.0
                    )
                elif protocol == 'shadowsocks':
                    # Для Shadowsocks используем email как идентификатор для обновления
                    client_identifier = client_from_xui.email
                    logger.info(f"[REVOKE] Обновление UUID для Shadowsocks: sub_id={new_client_uuid} на сервере {server_settings.get('name', 'Unknown')}")
                    await asyncio.wait_for(
                        client_api.client.update(client_uuid=client_identifier, client=updated_client_obj),
                        timeout=5.0
                    )
                elif _is_hysteria(protocol):
                    # Для Hysteria2: устанавливаем auth = new_client_uuid
                    updated_client_obj.auth = new_client_uuid
                    updated_client_obj.flow = ""  # для hysteria flow не применим
                    logger.info(f"[REVOKE] Обновление UUID для Hysteria2: auth={new_client_uuid}, sub_id={new_client_uuid} на сервере {server_settings.get('name', 'Unknown')}")
                    await asyncio.wait_for(
                        client_api.client.update(client_uuid=old_client_uuid, client=updated_client_obj),
                        timeout=5.0
                    )
                else:
                    # Для VLESS/VMESS: устанавливаем id = new_client_uuid
                    updated_client_obj.id = new_client_uuid
                    logger.info(f"[REVOKE] Обновление UUID для VLESS/VMESS: id={new_client_uuid}, sub_id={new_client_uuid} на сервере {server_settings.get('name', 'Unknown')}")
                    await asyncio.wait_for(
                        client_api.client.update(client_uuid=old_client_uuid, client=updated_client_obj),
                        timeout=5.0
                    )
            except asyncio.TimeoutError:
                logger.error(f"[REVOKE] Таймаут обновления клиента на сервере {server_settings.get('name', 'Unknown')}")
                return False
            
            logger.info(f"[REVOKE] ✅ UUID клиента успешно обновлен с {old_client_uuid} на {new_client_uuid} на сервере {server_settings.get('name', 'Unknown')}")
            return True
            
        except Exception as e:
            logger.error(f"[REVOKE] Ошибка обновления UUID для клиента {old_client_uuid} на сервере {server_settings.get('name', 'Unknown')}: {e}")
            import traceback
            logger.debug(f"[REVOKE] Traceback: {traceback.format_exc()}")
            return False

    async def delete_xui_user(self, server_settings: Dict, client_uuid_or_email: str, telegram_id: Optional[int] = None) -> bool:
        """
        Удаляет клиента X-UI по UUID или email.
        
        Args:
            server_settings: Настройки сервера
            client_uuid_or_email: UUID или email клиента
            telegram_id: Опционально - telegram_id для оптимизации поиска (если передан UUID, сначала ищет по email из БД)
        """
        client_api = await self.get_client(server_settings)
        if not client_api: return False

        try:
            inbound_id = server_settings['inbound_id']

            # Если передан UUID (не email) и нет telegram_id, пытаемся найти email по UUID в БД
            if '@' not in client_uuid_or_email and not telegram_id:
                try:
                    # Пытаемся найти пользователя по UUID в БД для получения email
                    user_data = await db_helpers.get_user_by_uuid(client_uuid_or_email)
                    if user_data and user_data.get('xui_client_email'):
                        telegram_id = user_data.get('telegram_id')
                        logger.debug(f"[DELETE] Найден email по UUID {client_uuid_or_email} в БД, используем для оптимизации поиска")
                except Exception as e:
                    logger.debug(f"[DELETE] Не удалось найти email по UUID {client_uuid_or_email} в БД: {e}")

            search_result = await self._find_client_by_email_or_uuid(client_api, inbound_id, client_uuid_or_email, telegram_id)
            found_client = search_result.client
            if not found_client:
                 # Клиент не найден - это нормально, возможно уже удален или не существует
                 logger.debug(f"Клиент '{client_uuid_or_email}' не найден для удаления (возможно уже удален или не существует).")
                 return True # Считаем успехом, если и так нету

            # Определяем протокол для правильного удаления
            protocol = (server_settings.get('protocol') or 'vless').strip().lower()
            
            # Для всех протоколов используем client_uuid_or_email из параметра функции
            # (это уже правильный UUID или email из БД)
            if protocol == 'shadowsocks':
                # Для Shadowsocks используем email
                client_identifier = found_client.email
                logger.debug(f"[DELETE] Протокол {protocol}, используем email={client_identifier} для удаления")
            elif _is_hysteria(protocol):
                # Для Hysteria2 panel идентифицирует клиента по auth (=UUID из нашей БД).
                # Если по какой-то причине переданный идентификатор — email, попробуем взять auth из найденного объекта.
                client_identifier = client_uuid_or_email
                if '@' in str(client_identifier):
                    client_identifier = getattr(found_client, 'auth', None) or client_uuid_or_email
                logger.debug(f"[DELETE] Протокол {protocol}, используем auth/uuid={client_identifier} для удаления")
            else:
                # Для VLESS/VMESS/Trojan используем client_uuid_or_email из параметра
                client_identifier = client_uuid_or_email
                logger.debug(f"[DELETE] Протокол {protocol}, используем client_uuid_or_email={client_identifier} для удаления")
            
            # Оборачиваем блокирующий вызов
            await client_api.client.delete(inbound_id=inbound_id, client_uuid=client_identifier)
            logger.info(f"Клиент '{client_uuid_or_email}' удален.")
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления X-UI юзера '{client_uuid_or_email}': {e}")
            return False

    async def set_xui_user_enabled(self, server_settings: Dict, client_uuid_or_email: str, enabled: bool, telegram_id: Optional[int] = None) -> bool:
        """
        Включает/отключает клиента (enable flag) на X-UI. Возвращает True при успехе.
        
        Args:
            server_settings: Настройки сервера
            client_uuid_or_email: UUID или email клиента
            enabled: True для включения, False для отключения
            telegram_id: Опционально - telegram_id для оптимизации поиска (если передан UUID, сначала ищет по email из БД)
        """
        client_api = await self.get_client(server_settings)
        if not client_api:
            logger.warning(f"Не удалось получить API клиент для сервера {server_settings.get('name', 'Unknown')} при изменении enable")
            return False

        try:
            inbound_id = server_settings['inbound_id']
            search_result = await self._find_client_by_email_or_uuid(client_api, inbound_id, client_uuid_or_email, telegram_id)
            client_from_xui = search_result.client
            if not client_from_xui:
                logger.warning(f"Клиент {client_uuid_or_email} не найден для изменения enable={enabled}")
                return False

            # limit_ip из БД, flow из настроек сервера — без загрузки inbound
            limit_ip_val = 0
            if telegram_id:
                try:
                    user_data = await db_helpers.get_user(telegram_id)
                    if user_data:
                        # aiosqlite.Row не имеет .get() — конвертируем в dict
                        ud = dict(user_data) if not isinstance(user_data, dict) else user_data
                        limit_ip_val = int(ud.get('limit_ip') or 0)
                except Exception:
                    pass
            flow_val = (server_settings.get('client_flow') or '').strip()

            updated_client_obj = Client.model_validate(client_from_xui.model_dump())
            updated_client_obj.enable = enabled
            updated_client_obj.inbound_id = inbound_id
            updated_client_obj.limit_ip = limit_ip_val  # Всегда из БД — py3xui exclude_defaults=True исключает 0
            updated_client_obj.flow = flow_val
            updated_client_obj.sub_id = getattr(client_from_xui, 'sub_id', None) or getattr(client_from_xui, 'subId', None) or client_uuid_or_email

            protocol = (server_settings.get('protocol') or 'vless').strip().lower()
            if protocol == 'trojan':
                client_identifier = getattr(client_from_xui, 'password', None) or client_from_xui.email
                updated_client_obj.password = client_identifier
            elif protocol == 'shadowsocks':
                client_identifier = client_from_xui.email
            elif _is_hysteria(protocol):
                # Для Hysteria2 идентификатор клиента в URL panel — auth (UUID из БД).
                client_identifier = (
                    getattr(client_from_xui, 'auth', None)
                    or getattr(client_from_xui, 'uuid', None)
                    or client_from_xui.email
                )
                updated_client_obj.auth = str(client_identifier)
                updated_client_obj.flow = ""  # для hysteria flow не применим
            else:
                client_identifier = getattr(client_from_xui, 'uuid', None) or client_from_xui.email
                updated_client_obj.id = str(client_identifier)

            # Прямой POST с exclude_defaults=False — иначе limit_ip=0 не передаётся (py3xui использует exclude_defaults=True)
            client_dict = updated_client_obj.model_dump(by_alias=True, exclude_defaults=False)
            client_dict['limitIp'] = int(limit_ip_val)
            if _is_hysteria(protocol):
                # У hysteria-клиента нет flow/id/password — убираем мусорные поля,
                # чтобы panel не сохранил их в settings.json.
                client_dict.pop('flow', None)
                client_dict.pop('id', None)
                client_dict.pop('password', None)
                client_dict['auth'] = str(client_identifier)
            else:
                client_dict['flow'] = flow_val
            payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_dict]})}
            endpoint = f"panel/api/inbounds/updateClient/{client_identifier}"
            url = client_api.client._url(endpoint)
            await client_api.client._post(url, {"Accept": "application/json"}, payload)
            
            logger.info(f"Клиент {client_uuid_or_email} на сервере {server_settings.get('name', 'Unknown')} установлен enable={enabled}")
            return True
        except Exception as e:
            logger.error(f"Ошибка изменения enable для клиента {client_uuid_or_email}: {e}")
            return False

    async def get_server_stats(self, server_settings: Dict) -> Optional[Dict[str, Any]]:
        """
        Получает полную статистику сервера X-UI
        """
        client_api = await self.get_client(server_settings)
        if not client_api: return None

        try:
            # Получаем статус сервера
            status = await client_api.server.get_status()
            if not status:
                logger.warning(f"Не удалось получить статус сервера {server_settings.get('name', 'Unknown')}")
                return None

            # Отладочная информация
            logger.debug(f"Тип status для {server_settings.get('name', 'Unknown')}: {type(status)}")
            logger.debug(f"Status объект: {status}")

            # Конвертируем объект status в словарь, если это не словарь
            if not isinstance(status, dict):
                try:
                    # Пытаемся конвертировать объект в словарь
                    status_dict = status.__dict__ if hasattr(status, '__dict__') else {}
                    # Если есть метод to_dict, используем его
                    if hasattr(status, 'to_dict'):
                        status_dict = status.to_dict()
                    # Если есть метод dict, используем его
                    elif hasattr(status, 'dict'):
                        status_dict = status.dict()
                    else:
                        # Пытаемся получить все атрибуты
                        status_dict = {attr: getattr(status, attr) for attr in dir(status) 
                                     if not attr.startswith('_') and not callable(getattr(status, attr))}
                    
                    logger.debug(f"Конвертированный status_dict: {status_dict}")
                except Exception as e:
                    logger.warning(f"Не удалось конвертировать статус в словарь: {e}")
                    status_dict = {}
            else:
                status_dict = status

            # Получаем количество активных клиентов
            try:
                inbound_id = server_settings['inbound_id']
                inbound = await self._find_inbound_by_id(client_api, inbound_id)
                active_users = 0
                if inbound and hasattr(inbound, 'settings') and inbound.settings and \
                   hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                    active_users = len(inbound.settings.clients)
            except Exception as e:
                logger.warning(f"Не удалось получить количество активных клиентов: {e}")
                active_users = 0

            # Добавляем количество активных клиентов к статусу
            status_dict['active_users'] = active_users

            # Добавляем количество онлайн-клиентов, если доступно
            try:
                online_count = await self.get_online_clients_count(server_settings)
                if online_count is not None:
                    status_dict['online_users'] = online_count
            except Exception as e:
                logger.debug(f"Не удалось получить online_users: {e}")

            return status_dict

        except Exception as e:
            logger.error(f"Ошибка получения статистики сервера {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_active_clients_count_for_inbound(self, server_settings: Dict) -> Optional[int]:
        """
        Получает количество активных клиентов для конкретного inbound
        """
        client_api = await self.get_client(server_settings)
        if not client_api: return None

        try:
            inbound_id = server_settings['inbound_id']
            inbound = await self._find_inbound_by_id(client_api, inbound_id)
            
            if inbound and hasattr(inbound, 'settings') and inbound.settings and \
               hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                return len(inbound.settings.clients)
            return 0
        except Exception as e:
            logger.error(f"Ошибка получения количества клиентов для сервера {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_online_clients_emails(self, server_settings: Dict) -> Optional[List[str]]:
        """
        Возвращает список email онлайн-клиентов с сервера X-UI (по данным py3xui).
        """
        client_api = await self.get_client(server_settings)
        if not client_api:
            return None

        try:
            emails = await client_api.client.online()
            return emails or []
        except Exception as e:
            logger.error(f"Ошибка получения списка онлайн-клиентов для {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_online_clients_count(self, server_settings: Dict) -> Optional[int]:
        """
        Возвращает количество онлайн-клиентов (по данным py3xui).
        """
        try:
            emails = await self.get_online_clients_emails(server_settings)
            return len(emails) if emails is not None else None
        except Exception as e:
            logger.error(f"Ошибка получения количества онлайн-клиентов для {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_user_traffic_stats(self, server_settings: Dict, user_uuid: str, user_email: str = None) -> Optional[Dict[str, Any]]:
        """
        Получает статистику трафика для конкретного пользователя.
        Использует email для поиска клиента (более надежно для разных протоколов/транспортов).
        
        Args:
            server_settings: Настройки сервера
            user_uuid: UUID пользователя (используется для кэша и fallback)
            user_email: Email пользователя (приоритетный способ поиска)
        """
        # Используем email для кэша, если он есть, иначе UUID
        cache_identifier = user_email if user_email else user_uuid
        cache_key = f"{server_settings.get('id', 'unknown')}_{cache_identifier}"
        current_time = time.time()
        
        if cache_key in self.traffic_cache:
            cached_data = self.traffic_cache[cache_key]
            if current_time - cached_data.get('timestamp', 0) < self.cache_timeout:
                logger.debug(f"Используем кэшированную статистику для {cache_identifier}")
                # ВАЖНО: не кэшировать флаг enable — обновим его прямо сейчас из X-UI
                try:
                    client_api_live = await self.get_client(server_settings)
                    if client_api_live:
                        # Пытаемся найти клиента по email (приоритет) или UUID
                        identifier = user_email if user_email else user_uuid
                        inbound_id_live = server_settings.get('inbound_id')
                        if inbound_id_live:
                            inbound_live = await self._find_inbound_by_id(client_api_live, inbound_id_live)
                            if inbound_live:
                                search_result_live = await self._find_client_by_email_or_uuid(client_api_live, inbound_id_live, identifier)
                                client_live = search_result_live.client
                                if client_live:
                                    stats_cached = dict(cached_data.get('stats') or {})
                                    # Получаем актуальный enable статус
                                    enable_status = False
                                    if hasattr(client_live, 'enable'):
                                        enable_status = bool(getattr(client_live, 'enable', False))
                                    elif hasattr(client_live, 'enabled'):
                                        enable_status = bool(getattr(client_live, 'enabled', False))
                                    elif isinstance(client_live, dict):
                                        enable_status = bool(client_live.get('enable', False) or client_live.get('enabled', False))
                                    stats_cached['enable'] = enable_status
                                    logger.debug(f"Обновлен enable статус из кэша для {identifier}: {enable_status}")
                                    return stats_cached
                        else:
                            # Если нет inbound_id, пытаемся найти по email напрямую
                            if user_email:
                                try:
                                    client_live = await client_api_live.client.get_by_email(user_email)
                                    if client_live:
                                        stats_cached = dict(cached_data.get('stats') or {})
                                        # Получаем актуальный enable статус
                                        enable_status = False
                                        if hasattr(client_live, 'enable'):
                                            enable_status = bool(getattr(client_live, 'enable', False))
                                        elif hasattr(client_live, 'enabled'):
                                            enable_status = bool(getattr(client_live, 'enabled', False))
                                        elif isinstance(client_live, dict):
                                            enable_status = bool(client_live.get('enable', False) or client_live.get('enabled', False))
                                        stats_cached['enable'] = enable_status
                                        logger.debug(f"Обновлен enable статус из кэша (get_by_email) для {user_email}: {enable_status}")
                                        return stats_cached
                                except Exception:
                                    pass
                except Exception:
                    # На ошибке возвращаем кэш как есть
                    pass
                return cached_data.get('stats')
            else:
                # Удаляем устаревший кэш
                del self.traffic_cache[cache_key]
        
        # Настройки уже должны быть загружены, не перезагружаем их
        client_api = await self.get_client(server_settings)
        if not client_api: 
            logger.warning(f"Не удалось получить клиент API для сервера {server_settings.get('name', 'Unknown')}")
            return None

        try:
            client = None
            inbound_id = server_settings.get('inbound_id')
            
            # Приоритет: ищем по email, если он передан
            client_from_get_by_email = None  # Сохраняем результат первого запроса
            if user_email:
                try:
                    logger.debug(f"Поиск клиента по email: {user_email}")
                    client_obj = await client_api.client.get_by_email(user_email)
                    if client_obj:
                        client = client_obj
                        client_from_get_by_email = client_obj  # Сохраняем для повторного использования
                        # Если нашли клиента, используем его inbound_id
                        if hasattr(client_obj, 'inbound_id') and client_obj.inbound_id:
                            inbound_id = client_obj.inbound_id
                            logger.debug(f"Найден клиент по email, inbound_id={inbound_id}")
                        else:
                            # Если inbound_id не указан в настройках, пытаемся найти inbound
                            if not inbound_id:
                                logger.debug(f"inbound_id не указан, пытаемся найти inbound для клиента")
                                all_inbounds = await client_api.inbound.list()
                                if all_inbounds:
                                    for inb in all_inbounds:
                                        if hasattr(inb, 'settings') and inb.settings:
                                            if hasattr(inb.settings, 'clients') and inb.settings.clients:
                                                for c in inb.settings.clients:
                                                    if hasattr(c, 'email') and c.email == user_email:
                                                        inbound_id = inb.id
                                                        logger.debug(f"Найден inbound_id={inbound_id} для email {user_email}")
                                                        break
                                        if inbound_id:
                                            break
                except Exception as e:
                    logger.debug(f"Не удалось найти клиента по email {user_email}: {e}, пробуем по UUID")
            
            # Fallback: ищем по UUID, если не нашли по email
            if not client and inbound_id:
                inbound = await self._find_inbound_by_id(client_api, inbound_id)
                if not inbound:
                    logger.warning(f"Inbound {inbound_id} не найден на сервере {server_settings.get('name', 'Unknown')}")
                    return None

                # Ищем клиента по UUID
                identifier = user_email if user_email else user_uuid
                search_result = await self._find_client_by_email_or_uuid(client_api, inbound_id, identifier)
                client = search_result.client
                if not client:
                    logger.warning(f"Клиент {identifier} не найден в inbound {inbound_id} на сервере {server_settings.get('name', 'Unknown')}")
                    return None
            elif not client:
                logger.warning(f"Не удалось найти клиента (email={user_email}, uuid={user_uuid}) на сервере {server_settings.get('name', 'Unknown')}")
                return None

            logger.debug(f"Найден клиент {user_uuid} на сервере {server_settings.get('name', 'Unknown')}")
            logger.debug(f"Атрибуты клиента: {[attr for attr in dir(client) if not attr.startswith('_')]}")
            
            # ОПТИМИЗАЦИЯ: Если клиент уже получен через get_by_email, используем его данные напрямую
            # get_by_email использует endpoint /panel/api/inbounds/getClientTraffics/{email}
            # который возвращает ВСЕ данные: up, down, enable, expiryTime, lastOnline, allTime и т.д.
            if client_from_get_by_email:
                logger.debug(f"Используем данные из get_by_email (getClientTraffics endpoint) - все данные уже получены одним запросом")
                client = client_from_get_by_email
            
            # Получаем статистику клиента
            client_email = getattr(client, 'email', '') or user_email or ''
            
            # Получаем enable статус - проверяем разные варианты
            enable_status = False
            enable_raw_value = None
            
            # Пробуем получить enable напрямую
            if hasattr(client, 'enable'):
                enable_raw_value = getattr(client, 'enable', None)
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enable через hasattr/getattr = {enable_raw_value} (bool: {enable_status})")
            elif hasattr(client, 'enabled'):
                enable_raw_value = getattr(client, 'enabled', None)
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enabled через hasattr/getattr = {enable_raw_value} (bool: {enable_status})")
            elif isinstance(client, dict):
                enable_raw_value = client.get('enable') or client.get('enabled')
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enable через dict = {enable_raw_value} (bool: {enable_status})")
            else:
                # Пробуем получить через model_dump, если это Pydantic модель
                try:
                    if hasattr(client, 'model_dump'):
                        client_dict = client.model_dump()
                        enable_raw_value = client_dict.get('enable') or client_dict.get('enabled')
                        enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                        logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enable через model_dump = {enable_raw_value} (bool: {enable_status})")
                    elif hasattr(client, '__dict__'):
                        client_dict = client.__dict__
                        enable_raw_value = client_dict.get('enable') or client_dict.get('enabled')
                        enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                        logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enable через __dict__ = {enable_raw_value} (bool: {enable_status})")
                except Exception as e:
                    logger.warning(f"[ENABLE_STATUS] Не удалось получить enable через model_dump/__dict__: {e}")
            
            # Если все еще не получили enable, пробуем получить из inbound напрямую (только если не использовали get_by_email)
            if enable_raw_value is None and inbound_id and not client_from_get_by_email:
                try:
                    inbound_for_enable = await self._find_inbound_by_id(client_api, inbound_id)
                    if inbound_for_enable and hasattr(inbound_for_enable, 'settings') and inbound_for_enable.settings:
                        if hasattr(inbound_for_enable.settings, 'clients') and inbound_for_enable.settings.clients:
                            for c in inbound_for_enable.settings.clients:
                                if (hasattr(c, 'email') and c.email == client_email) or \
                                   (hasattr(c, 'id') and getattr(c, 'id', None) == user_uuid) or \
                                   (hasattr(c, 'password') and getattr(c, 'password', None) == user_uuid):
                                    if hasattr(c, 'enable'):
                                        enable_status = bool(getattr(c, 'enable', False))
                                        logger.info(f"[ENABLE_STATUS] Клиент {user_uuid}: enable из inbound.settings.clients = {enable_status}")
                                        break
                except Exception as e:
                    logger.debug(f"[ENABLE_STATUS] Не удалось получить enable из inbound: {e}")
            
            logger.info(f"[ENABLE_STATUS] Финальный enable статус для клиента {user_uuid}: {enable_status} (тип клиента: {type(client)})")
            
            # Создаем stats из объекта клиента (если использовали get_by_email, все данные уже там)
            # Пробуем получить last_online и all_time разными способами
            last_online_val = 0
            all_time_val = 0
            
            # Функция для безопасного получения значения
            def _get_value(obj, *attr_names, default=0):
                logger.debug(f"[_get_value] Ищем поля {attr_names} в объекте типа {type(obj)}")
                
                # Сначала пробуем через атрибуты
                for attr_name in attr_names:
                    try:
                        has_attr = hasattr(obj, attr_name)
                        logger.debug(f"[_get_value] hasattr({attr_name}): {has_attr}")
                        if has_attr:
                            val = getattr(obj, attr_name, None)
                            logger.debug(f"[_get_value] getattr({attr_name}): {val} (тип: {type(val)})")
                            if val is not None and val != 0:
                                logger.info(f"[_get_value] Найдено значение {attr_name}={val}")
                                return val
                    except Exception as e:
                        logger.debug(f"[_get_value] Ошибка при получении {attr_name}: {e}")
                
                # Пробуем через model_dump или __dict__ - ВАЖНО: проверяем ВСЕ ключи
                try:
                    client_dict = None
                    if hasattr(obj, 'model_dump'):
                        client_dict = obj.model_dump()
                        logger.info(f"[_get_value] model_dump() вернул ключи: {list(client_dict.keys())}")
                    elif hasattr(obj, '__dict__'):
                        client_dict = obj.__dict__
                        logger.info(f"[_get_value] __dict__ содержит ключи: {list(client_dict.keys())}")
                    
                    if client_dict:
                        # Проверяем все варианты имен полей
                        for attr_name in attr_names:
                            val = client_dict.get(attr_name)
                            if val is not None and val != 0:
                                logger.info(f"[_get_value] Найдено через dict {attr_name}={val}")
                                return val
                        # Также пробуем найти по частичному совпадению (на случай разных регистров)
                        client_dict_lower = {k.lower(): v for k, v in client_dict.items()}
                        for attr_name in attr_names:
                            val = client_dict_lower.get(attr_name.lower())
                            if val is not None and val != 0:
                                logger.info(f"[_get_value] Найдено через dict (lowercase) {attr_name.lower()}={val}")
                                return val
                except Exception as e:
                    logger.debug(f"[_get_value] Не удалось получить значение через model_dump/__dict__: {e}")
                
                # Если ничего не нашли - это нормально, не все серверы возвращают эти поля
                logger.debug(f"[_get_value] Не найдено ни одно из полей {attr_names} (это нормально, если сервер не возвращает эти данные), возвращаем default={default}")
                return default
            
            # Пробуем получить last_online (приоритет: lastOnline, затем last_online)
            last_online_val = _get_value(client, 'lastOnline', 'last_online', default=0)
            
            # Пробуем получить all_time (приоритет: allTime, затем all_time)
            all_time_val = _get_value(client, 'allTime', 'all_time', default=0)
            
            # ВСЕГДА делаем прямой запрос к API, если клиент был получен через get_by_email, чтобы гарантированно получить lastOnline и allTime
            # (объект из py3xui может не содержать эти поля напрямую)
            if client_from_get_by_email and user_email:
                try:
                    import httpx as _httpx
                    endpoint = f"panel/api/inbounds/getClientTraffics/{user_email}"
                    _url_builder = getattr(client_api.client, "_url", None)
                    _cookies = getattr(client_api.client, "cookies", {})
                    if callable(_url_builder):
                        raw_url = _url_builder(endpoint)
                        async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True) as _c:
                            raw_resp = await _c.post(raw_url, headers={"Accept": "application/json"})
                            if raw_resp.status_code == 200:
                                raw_json = raw_resp.json()
                                logger.info(f"[TRAFFIC_STATS][RAW] Прямой запрос к API (client_from_get_by_email) вернул: {raw_json}")
                                if raw_json.get('success') and raw_json.get('obj'):
                                    obj_data = raw_json['obj']
                                    # Всегда обновляем из прямого запроса, если значения есть
                                    raw_last_online = obj_data.get('lastOnline') or obj_data.get('last_online') or 0
                                    raw_all_time = obj_data.get('allTime') or obj_data.get('all_time') or 0
                                    if raw_last_online:
                                        last_online_val = raw_last_online
                                    if raw_all_time:
                                        all_time_val = raw_all_time
                                    logger.info(f"[TRAFFIC_STATS][RAW] Извлечено из прямого запроса: lastOnline={last_online_val}, allTime={all_time_val}")
                except Exception as raw_err:
                    logger.warning(f"[TRAFFIC_STATS][RAW] Ошибка прямого запроса: {raw_err}")
            
            logger.info(f"[TRAFFIC_STATS] Извлечено для клиента {user_uuid}: last_online={last_online_val}, all_time={all_time_val} (тип клиента: {type(client)})")
            
            stats = {
                'uuid': user_uuid,
                'email': client_email,
                'total_gb': getattr(client, 'total_gb', 0),
                'up': getattr(client, 'up', 0),
                'down': getattr(client, 'down', 0),
                'enable': enable_status,
                'expiry_time': getattr(client, 'expiry_time', 0) or getattr(client, 'expiryTime', 0),
                'limit_ip': getattr(client, 'limit_ip', 0) or getattr(client, 'limitIp', 0),
                'last_online': last_online_val,
                'all_time': all_time_val
            }
            
            # Проверяем поле total, возможно там хранится лимит трафика
            if hasattr(client, 'total') and getattr(client, 'total', 0) > 0:
                stats['total_gb'] = getattr(client, 'total', 0)
                logger.debug(f"Найден лимит трафика в поле 'total': {stats['total_gb']} GB")

            logger.debug(f"Сырые данные трафика для {cache_identifier}: up={stats['up']}, down={stats['down']}, total_gb={stats['total_gb']}, last_online={stats.get('last_online', 0)}, all_time={stats.get('all_time', 0)}")

            # Если клиент НЕ был получен через get_by_email, пробуем получить статистику через API
            # (get_by_email уже содержит все данные из getClientTraffics endpoint)
            if not client_from_get_by_email:
                try:
                    # Пробуем получить через get_by_email, если есть email
                    if user_email:
                        try:
                            logger.debug(f"Пробуем получить статистику через get_by_email для {user_email}")
                            client_by_email = await client_api.client.get_by_email(user_email)
                            if client_by_email:
                                # Обновляем статистику из объекта клиента (getClientTraffics endpoint)
                                if hasattr(client_by_email, 'up'):
                                    stats['up'] = getattr(client_by_email, 'up', stats['up'])
                                if hasattr(client_by_email, 'down'):
                                    stats['down'] = getattr(client_by_email, 'down', stats['down'])
                                if hasattr(client_by_email, 'total_gb'):
                                    stats['total_gb'] = getattr(client_by_email, 'total_gb', stats['total_gb'])
                                elif hasattr(client_by_email, 'total'):
                                    stats['total_gb'] = getattr(client_by_email, 'total', stats['total_gb'])
                                # Обновляем enable статус
                                if hasattr(client_by_email, 'enable'):
                                    stats['enable'] = bool(getattr(client_by_email, 'enable', False))
                                elif hasattr(client_by_email, 'enabled'):
                                    stats['enable'] = bool(getattr(client_by_email, 'enabled', False))
                                # Обновляем last_online и all_time - используем функцию _get_value
                                last_online_new = _get_value(client_by_email, 'lastOnline', 'last_online', default=0)
                                all_time_new = _get_value(client_by_email, 'allTime', 'all_time', default=0)
                                
                                # ВСЕГДА делаем прямой запрос к API для гарантированного получения lastOnline и allTime
                                if user_email:
                                    try:
                                        import httpx as _httpx
                                        endpoint = f"panel/api/inbounds/getClientTraffics/{user_email}"
                                        _url_builder = getattr(client_api.client, "_url", None)
                                        _cookies = getattr(client_api.client, "cookies", {})
                                        if callable(_url_builder):
                                            raw_url = _url_builder(endpoint)
                                            async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True) as _c:
                                                raw_resp = await _c.post(raw_url, headers={"Accept": "application/json"})
                                                if raw_resp.status_code == 200:
                                                    raw_json = raw_resp.json()
                                                    logger.info(f"[TRAFFIC_STATS][RAW] Прямой запрос к API вернул: {raw_json}")
                                                    if raw_json.get('success') and raw_json.get('obj'):
                                                        obj_data = raw_json['obj']
                                                        # Всегда обновляем из прямого запроса, если значения есть
                                                        raw_last_online = obj_data.get('lastOnline') or obj_data.get('last_online') or 0
                                                        raw_all_time = obj_data.get('allTime') or obj_data.get('all_time') or 0
                                                        if raw_last_online:
                                                            last_online_new = raw_last_online
                                                        if raw_all_time:
                                                            all_time_new = raw_all_time
                                                        logger.info(f"[TRAFFIC_STATS][RAW] Извлечено из прямого запроса: lastOnline={last_online_new}, allTime={all_time_new}")
                                    except Exception as raw_err:
                                        logger.warning(f"[TRAFFIC_STATS][RAW] Ошибка прямого запроса: {raw_err}")
                                
                                # Обновляем stats только если получили значения
                                if last_online_new:
                                    stats['last_online'] = last_online_new
                                    logger.info(f"[TRAFFIC_STATS] Установлен last_online={last_online_new} в stats")
                                if all_time_new:
                                    stats['all_time'] = all_time_new
                                    logger.info(f"[TRAFFIC_STATS] Установлен all_time={all_time_new} в stats")
                                
                                logger.info(f"[TRAFFIC_STATS] Обновлено из get_by_email: last_online={last_online_new}, all_time={all_time_new}")
                                
                                logger.debug(f"Обновлена статистика из get_by_email: up={stats['up']}, down={stats['down']}, enable={stats['enable']}, last_online={stats.get('last_online', 0)}, all_time={stats.get('all_time', 0)}")
                        except Exception as email_err:
                            logger.debug(f"Не удалось получить статистику через get_by_email для {user_email}: {email_err}")
                    
                        # Fallback: используем метод get_traffic_by_id для получения статистики клиента
                        client_stats = None
                        if not user_email or stats['up'] == 0 and stats['down'] == 0:
                            logger.debug(f"Пробуем получить статистику через get_traffic_by_id для {user_uuid}")
                            try:
                                client_stats = await client_api.client.get_traffic_by_id(user_uuid)
                            except Exception:
                                client_stats = None
                            
                            if client_stats:
                                logger.debug(f"Получена статистика через get_traffic_by_id для {user_uuid}: {client_stats}")
                                # Обновляем данные из API - исправляем проблему с обновлением
                                client_stat_obj = None
                                if isinstance(client_stats, list) and len(client_stats) > 0:
                                    # API возвращает список, берем первый элемент
                                    client_stat_obj = client_stats[0]
                                    logger.debug(f"Обрабатываем первый элемент из списка: {client_stat_obj}")
                                    if hasattr(client_stat_obj, 'up'):
                                        stats['up'] = client_stat_obj.up
                                        logger.debug(f"Обновлен up: {stats['up']}")
                                    if hasattr(client_stat_obj, 'down'):
                                        stats['down'] = client_stat_obj.down
                                        logger.debug(f"Обновлен down: {stats['down']}")
                                    if hasattr(client_stat_obj, 'total_gb'):
                                        stats['total_gb'] = client_stat_obj.total_gb
                                        logger.debug(f"Обновлен total_gb: {stats['total_gb']}")
                                elif hasattr(client_stats, 'up'):
                                    # API возвращает объект напрямую
                                    client_stat_obj = client_stats
                                    stats['up'] = client_stats.up
                                    logger.debug(f"Обновлен up: {stats['up']}")
                                if hasattr(client_stats, 'down'):
                                    stats['down'] = client_stats.down
                                    logger.debug(f"Обновлен down: {stats['down']}")
                                if hasattr(client_stats, 'total_gb'):
                                    stats['total_gb'] = client_stats.total_gb
                                    logger.debug(f"Обновлен total_gb: {stats['total_gb']}")
                                
                                # Обновляем enable статус, last_online и all_time из get_traffic_by_id, если доступен
                                if client_stat_obj:
                                    if hasattr(client_stat_obj, 'enable'):
                                        stats['enable'] = bool(getattr(client_stat_obj, 'enable', False))
                                    # Обновляем last_online
                                    if hasattr(client_stat_obj, 'last_online'):
                                        stats['last_online'] = getattr(client_stat_obj, 'last_online', 0)
                                    elif hasattr(client_stat_obj, 'lastOnline'):
                                        stats['last_online'] = getattr(client_stat_obj, 'lastOnline', 0)
                                    # Обновляем all_time
                                    if hasattr(client_stat_obj, 'all_time'):
                                        stats['all_time'] = getattr(client_stat_obj, 'all_time', 0)
                                    elif hasattr(client_stat_obj, 'allTime'):
                                        stats['all_time'] = getattr(client_stat_obj, 'allTime', 0)
                                    elif hasattr(client_stat_obj, 'enabled'):
                                        stats['enable'] = bool(getattr(client_stat_obj, 'enabled', False))
                                    logger.debug(f"Обновлен enable статус через get_traffic_by_id: {stats['enable']}, last_online={stats.get('last_online', 0)}, all_time={stats.get('all_time', 0)}")
                                elif hasattr(client_stats, 'enable'):
                                    stats['enable'] = bool(getattr(client_stats, 'enable', False))
                                    logger.debug(f"Обновлен enable статус через get_traffic_by_id (direct): {stats['enable']}")
                                elif hasattr(client_stats, 'enabled'):
                                    stats['enable'] = bool(getattr(client_stats, 'enabled', False))
                                    logger.debug(f"Обновлен enable статус через get_traffic_by_id (direct, enabled): {stats['enable']}")
                except Exception as e:
                    logger.debug(f"Не удалось получить статистику через API: {e}")
                
                # Попробуем альтернативный способ - получить статистику через reset_stats
                try:
                    logger.debug(f"Пробуем получить статистику через reset_stats для {user_uuid}")
                    # Сначала получаем текущую статистику, затем сбрасываем и получаем снова
                    # Это может помочь получить актуальные данные
                    inbound_stats = await client_api.inbound.reset_stats(inbound_id=inbound_id)
                    if inbound_stats:
                        logger.debug(f"Получена статистика через reset_stats для {user_uuid}: {inbound_stats}")
                        # Ищем статистику для нашего клиента
                        for client_stat in inbound_stats:
                            if hasattr(client_stat, 'id') and client_stat.id == user_uuid:
                                logger.debug(f"Найдена статистика для клиента {user_uuid}: {client_stat}")
                                if hasattr(client_stat, 'up'):
                                    stats['up'] = client_stat.up
                                if hasattr(client_stat, 'down'):
                                    stats['down'] = client_stat.down
                                if hasattr(client_stat, 'total_gb'):
                                    stats['total_gb'] = client_stat.total_gb
                                break
                except Exception as e2:
                    logger.debug(f"Не удалось получить статистику через reset_stats для {user_uuid}: {e2}")
                    
                    # Попробуем получить статистику через reset_client_stats
                    try:
                        logger.debug(f"Пробуем получить статистику через reset_client_stats для {user_uuid}")
                        client_stats = await client_api.inbound.reset_client_stats(inbound_id=inbound_id, client_uuid=user_uuid)
                        if client_stats:
                            logger.debug(f"Получена статистика через reset_client_stats для {user_uuid}: {client_stats}")
                            if hasattr(client_stats, 'up'):
                                stats['up'] = client_stats.up
                            if hasattr(client_stats, 'down'):
                                stats['down'] = client_stats.down
                            if hasattr(client_stats, 'total_gb'):
                                stats['total_gb'] = client_stats.total_gb
                    except Exception as e3:
                        logger.debug(f"Не удалось получить статистику через reset_client_stats для {user_uuid}: {e3}")

            # Вычисляем использованный трафик
            total_used = stats['up'] + stats['down']
            stats['total_used_gb'] = total_used / (1024**3) if total_used > 0 else 0
            stats['up_gb'] = stats['up'] / (1024**3) if stats['up'] > 0 else 0
            stats['down_gb'] = stats['down'] / (1024**3) if stats['down'] > 0 else 0

            # Определяем статус использования трафика
            if stats['total_gb'] > 0:
                stats['usage_percent'] = (stats['total_used_gb'] / stats['total_gb']) * 100
                stats['has_traffic_limit'] = True
            else:
                stats['usage_percent'] = 0
                stats['has_traffic_limit'] = False

            logger.debug(f"Итоговая статистика для {user_uuid}: {stats}")
            
            # Добавляем информацию о том, что статистика может быть неактуальной
            if stats['up'] == 0 and stats['down'] == 0:
                logger.info(f"Статистика трафика для {user_uuid} показывает 0. Это может означать:")
                logger.info(f"1. Пользователь еще не пользовался сервисом")
                logger.info(f"2. Статистика не обновилась в X-UI (может потребоваться время)")
                logger.info(f"3. Проблема с получением статистики из X-UI")
                logger.info(f"4. Статистика обновляется только при активном использовании сервиса")
                logger.info(f"5. Возможно, нужно перезапустить X-UI сервис для обновления статистики")
            
            # Кэшируем результат
            # Логируем финальные значения перед кэшированием
            logger.info(f"[TRAFFIC_STATS][FINAL] Финальные значения для клиента {user_uuid}: last_online={stats.get('last_online', 0)}, all_time={stats.get('all_time', 0)}")
            logger.info(f"[TRAFFIC_STATS][FINAL] Все поля stats: {list(stats.keys())}")
            
            self.traffic_cache[cache_key] = {
                'timestamp': current_time,
                'stats': stats
            }

            return stats

        except Exception as e:
            logger.error(f"Ошибка получения статистики трафика для пользователя {user_uuid} на сервере {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_basic_server_stats(self, server_settings: Dict, *, skip_online: bool = False) -> Optional[Dict[str, Any]]:
        """
        Получает базовую статистику сервера (альтернативный метод).

        skip_online=True — не дёргать get_online_clients_count внутри. Полезно
        вызывающим, у которых уже есть свежее значение онлайна (например, из
        TTL-кэша web_admin/core/xui_online_cache.py) — экономит один сетевой
        round-trip к панели на каждый рендер.
        """
        client_api = await self.get_client(server_settings)
        if not client_api: return None

        try:
            # Получаем статус сервера
            status = await client_api.server.get_status()
            if not status:
                return None

            # Отладочная информация
            logger.debug(f"Тип status для {server_settings.get('name', 'Unknown')}: {type(status)}")
            try:
                logger.debug(f"Доступные атрибуты/ключи status: {list(status.keys()) if isinstance(status, dict) else [a for a in dir(status) if not a.startswith('_')]}")
            except Exception:
                pass

            # Создаем базовый словарь с основными полями
            stats = {}

            def _get(obj, attr_name, default=None):
                if isinstance(obj, dict):
                    return obj.get(attr_name, default)
                return getattr(obj, attr_name, default)

            def _get_multi(obj, names, default=None):
                for n in names:
                    v = _get(obj, n, None)
                    if v is not None:
                        return v
                return default

            try:
                # CPU
                cpu_val = _get_multi(status, ['cpu'])
                if cpu_val is not None:
                    stats['cpu'] = cpu_val
                cpu_cores = _get_multi(status, ['cpuCores', 'cpu_cores'])
                if cpu_cores is not None:
                    stats['cpuCores'] = cpu_cores
                cpu_mhz = _get_multi(status, ['cpuSpeedMhz', 'cpu_speed_mhz'])
                if cpu_mhz is not None:
                    stats['cpuSpeedMhz'] = cpu_mhz

                # Memory
                mem_obj = _get_multi(status, ['mem'])
                if mem_obj is not None:
                    stats['mem'] = {
                        'current': _get(mem_obj, 'current', 0),
                        'total': _get(mem_obj, 'total', 0)
                    }

                # Disk
                disk_obj = _get_multi(status, ['disk'])
                if disk_obj is not None:
                    stats['disk'] = {
                        'current': _get(disk_obj, 'current', 0),
                        'total': _get(disk_obj, 'total', 0)
                    }

                # Swap
                swap_obj = _get_multi(status, ['swap'])
                if swap_obj is not None:
                    stats['swap'] = {
                        'current': _get(swap_obj, 'current', 0),
                        'total': _get(swap_obj, 'total', 0)
                    }

                # Xray
                xray_obj = _get_multi(status, ['xray'])
                if xray_obj is not None:
                    stats['xray'] = {
                        'state': _get(xray_obj, 'state', 'unknown'),
                        'version': _get(xray_obj, 'version', 'unknown'),
                        'errorMsg': _get(xray_obj, 'errorMsg', '')
                    }

                # Scalars
                uptime_val = _get_multi(status, ['uptime'])
                if uptime_val is not None:
                    stats['uptime'] = uptime_val
                loads_val = _get_multi(status, ['loads'])
                if loads_val is not None:
                    stats['loads'] = loads_val
                tcp_count = _get_multi(status, ['tcpCount', 'tcp_count'])
                if tcp_count is not None:
                    stats['tcpCount'] = tcp_count
                udp_count = _get_multi(status, ['udpCount', 'udp_count'])
                if udp_count is not None:
                    stats['udpCount'] = udp_count

                # NetIO
                netio_obj = _get_multi(status, ['netIO', 'net_io'])
                if netio_obj is not None:
                    stats['netIO'] = {
                        'up': _get(netio_obj, 'up', 0),
                        'down': _get(netio_obj, 'down', 0)
                    }

                # NetTraffic
                nettraffic_obj = _get_multi(status, ['netTraffic', 'net_traffic'])
                if nettraffic_obj is not None:
                    stats['netTraffic'] = {
                        'sent': _get(nettraffic_obj, 'sent', 0),
                        'recv': _get(nettraffic_obj, 'recv', 0)
                    }

                # Public IP
                publicip_obj = _get_multi(status, ['publicIP', 'public_ip'])
                if publicip_obj is not None:
                    stats['publicIP'] = {
                        'ipv4': _get(publicip_obj, 'ipv4', 'N/A'),
                        'ipv6': _get(publicip_obj, 'ipv6', 'N/A')
                    }

                # AppStats
                appstats_obj = _get(status, 'appStats')
                if appstats_obj is not None:
                    stats['appStats'] = {
                        'threads': _get(appstats_obj, 'threads', 0),
                        'mem': _get(appstats_obj, 'mem', 0),
                        'uptime': _get(appstats_obj, 'uptime', 0)
                    }

            except Exception as e:
                logger.warning(f"Ошибка извлечения полей статуса: {e}")

            # Получаем количество активных клиентов
            try:
                inbound_id = server_settings['inbound_id']
                inbound = await self._find_inbound_by_id(client_api, inbound_id)
                active_users = 0
                if inbound and hasattr(inbound, 'settings') and inbound.settings and \
                   hasattr(inbound.settings, 'clients') and inbound.settings.clients:
                    active_users = len(inbound.settings.clients)
                stats['active_users'] = active_users
            except Exception as e:
                logger.warning(f"Не удалось получить количество активных клиентов: {e}")
                stats['active_users'] = 0

            # Получаем количество онлайн-клиентов (по py3xui)
            if not skip_online:
                try:
                    online_count = await self.get_online_clients_count(server_settings)
                    if online_count is not None:
                        stats['online_users'] = online_count
                except Exception as e:
                    logger.debug(f"Не удалось получить online_users: {e}")

            logger.debug(f"Итоговая статистика для {server_settings.get('name', 'Unknown')}: {stats}")
            return stats

        except Exception as e:
            logger.error(f"Ошибка получения базовой статистики сервера {server_settings.get('name', 'Unknown')}: {e}")
            return None

    async def get_user_traffic_stats_multi(self, user_uuid: str, servers: List[Dict], user_email: str = None) -> Dict[str, Dict[str, Any]]:
        """
        Получает статистику трафика для пользователя по всем серверам в multi режиме
        Возвращает словарь {server_id: stats}
        
        Args:
            user_uuid: UUID пользователя
            servers: Список серверов
            user_email: Email пользователя (приоритетный способ поиска)
        """
        identifier = user_email or user_uuid
        logger.info(f"DEBUG: get_user_traffic_stats_multi вызван для {identifier} с {len(servers)} серверами")
        
        all_stats = {}
        
        # Создаем задачи для параллельного получения статистики по всем переданным серверам
        tasks = []
        logger.info(f"DEBUG: get_user_traffic_stats_multi: получено {len(servers)} серверов для обработки")
        logger.info(f"DEBUG: Список серверов: {[(s.get('id'), s.get('name'), s.get('is_location', False)) for s in servers]}")
        
        for server in servers:
            server_id = server.get('id')
            server_name = server.get('name', 'Unknown')
            is_location = server.get('is_location', False)
            logger.info(f"DEBUG: Подготовка задачи для сервера {server_name} (ID: {server_id}, тип ID: {type(server_id)}) - is_location: {is_location}")
            try:
                task = self._get_single_server_traffic(server, user_uuid, user_email)
                tasks.append((server_id, server_name, task))
                logger.info(f"DEBUG: Добавлен сервер {server_name} (ID: {server_id}) в задачи")
            except Exception as e:
                logger.error(f"DEBUG: Ошибка при подготовке задачи для сервера {server_name} (ID: {server_id}): {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
        
        logger.info(f"DEBUG: Создано {len(tasks)} задач для серверов из {len(servers)} переданных")
        
        if not tasks:
            logger.warning(f"Нет серверов для получения трафика пользователя {user_uuid}")
            return {}
        
        # Выполняем все задачи параллельно с таймаутами
        async def _execute_with_timeout(server_id, server_name, task):
            """Выполняет задачу с таймаутом 10 секунд (2 попытки логина по 3 сек + запрос трафика)"""
            try:
                logger.info(f"DEBUG: Выполняем задачу для сервера {server_name}")
                stats = await asyncio.wait_for(task, timeout=10.0)
                if stats:
                    return server_id, {
                        'server_name': server_name,
                        'stats': stats
                    }
                else:
                    return server_id, {
                        'server_name': server_name,
                        'stats': None,
                        'error': 'Не удалось получить статистику'
                    }
            except asyncio.TimeoutError:
                logger.warning(f"Таймаут при получении трафика для {user_uuid} на сервере {server_name}")
                return server_id, {
                    'server_name': server_name,
                    'stats': None,
                    'error': 'Timeout'
                }
            except Exception as e:
                logger.error(f"Ошибка получения трафика для {user_uuid} на сервере {server_name}: {e}")
                return server_id, {
                    'server_name': server_name,
                    'stats': None,
                    'error': str(e)
                }
        
        # Запускаем все задачи параллельно
        task_coros = [_execute_with_timeout(sid, sname, t) for sid, sname, t in tasks]
        results = await asyncio.gather(*task_coros, return_exceptions=True)
        
        # Обрабатываем результаты
        processed_count = 0
        exception_count = 0
        for result in results:
            if isinstance(result, Exception):
                exception_count += 1
                logger.error(f"Исключение при выполнении задачи: {result}")
                import traceback
                logger.debug(f"Traceback исключения: {traceback.format_exc()}")
                continue
            try:
                server_id, server_data = result
                all_stats[server_id] = server_data
                processed_count += 1
                logger.debug(f"DEBUG: Добавлен сервер {server_id} в all_stats")
            except Exception as e:
                logger.error(f"Ошибка обработки результата: {e}, тип результата: {type(result)}, значение: {result}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
        
        logger.info(f"DEBUG: get_user_traffic_stats_multi завершен: обработано {processed_count} серверов, исключений {exception_count}, всего в all_stats: {len(all_stats)}")
        logger.info(f"DEBUG: Ключи в all_stats: {list(all_stats.keys())}")
        return all_stats
    
    async def _get_single_server_traffic(self, server: Dict, user_uuid: str, user_email: str = None) -> Optional[Dict[str, Any]]:
        """
        Вспомогательный метод для получения трафика с одного сервера
        
        Args:
            server: Настройки сервера
            user_uuid: UUID пользователя
            user_email: Email пользователя (приоритетный способ поиска)
        """
        identifier = user_email or user_uuid
        logger.info(f"DEBUG: _get_single_server_traffic вызван для сервера {server.get('name', 'Unknown')} и пользователя {identifier}")
        
        try:
            # Таймаут 6 секунд на получение клиента (2 попытки логина по 3 сек)
            client_api = await asyncio.wait_for(self.get_client(server), timeout=6.0)
            if not client_api:
                logger.warning(f"DEBUG: Не удалось получить клиент API для сервера {server.get('name', 'Unknown')}")
                return None
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут при получении клиента API для сервера {server.get('name', 'Unknown')}")
            return None
        
        logger.info(f"DEBUG: Клиент API получен для сервера {server.get('name', 'Unknown')}")
        
        try:
            client = None
            inbound_id = server.get('inbound_id')
            
            # Приоритет: ищем по email, если он передан
            if user_email:
                try:
                    logger.debug(f"Поиск клиента по email: {user_email} на сервере {server.get('name', 'Unknown')}")
                    client_obj = await client_api.client.get_by_email(user_email)
                    if client_obj:
                        client = client_obj
                        # Если нашли клиента, используем его inbound_id
                        if hasattr(client_obj, 'inbound_id') and client_obj.inbound_id:
                            inbound_id = client_obj.inbound_id
                            logger.debug(f"Найден клиент по email, inbound_id={inbound_id}")
                except Exception as e:
                    logger.debug(f"Не удалось найти клиента по email {user_email}: {e}, пробуем по UUID")
            
            # Fallback: ищем по UUID, если не нашли по email
            if not client and inbound_id:
                inbound = await self._find_inbound_by_id(client_api, inbound_id)
                if not inbound:
                    logger.warning(f"DEBUG: Не найден inbound {inbound_id} для сервера {server.get('name', 'Unknown')}")
                    return None
                
                logger.info(f"DEBUG: Inbound {inbound_id} найден для сервера {server.get('name', 'Unknown')}")
                
                # Ищем клиента по UUID
                search_result = await self._find_client_by_email_or_uuid(client_api, inbound_id, user_uuid)
                client = search_result.client
                if not client:
                    logger.warning(f"DEBUG: Клиент {user_uuid} не найден на сервере {server.get('name', 'Unknown')}")
                    return None
            
            if not client:
                logger.warning(f"DEBUG: Клиент {identifier} не найден на сервере {server.get('name', 'Unknown')}")
                return None
            
            logger.info(f"DEBUG: Клиент {identifier} найден на сервере {server.get('name', 'Unknown')}")
            
            # Получаем статистику клиента
            client_email = getattr(client, 'email', '') or user_email or ''
            
            # Получаем enable статус - проверяем разные варианты
            enable_status = False
            enable_raw_value = None
            
            # Пробуем получить enable напрямую
            if hasattr(client, 'enable'):
                enable_raw_value = getattr(client, 'enable', None)
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enable через hasattr/getattr = {enable_raw_value} (bool: {enable_status})")
            elif hasattr(client, 'enabled'):
                enable_raw_value = getattr(client, 'enabled', None)
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enabled через hasattr/getattr = {enable_raw_value} (bool: {enable_status})")
            elif isinstance(client, dict):
                enable_raw_value = client.get('enable') or client.get('enabled')
                enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enable через dict = {enable_raw_value} (bool: {enable_status})")
            else:
                # Пробуем получить через model_dump, если это Pydantic модель
                try:
                    if hasattr(client, 'model_dump'):
                        client_dict = client.model_dump()
                        enable_raw_value = client_dict.get('enable') or client_dict.get('enabled')
                        enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                        logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enable через model_dump = {enable_raw_value} (bool: {enable_status})")
                    elif hasattr(client, '__dict__'):
                        client_dict = client.__dict__
                        enable_raw_value = client_dict.get('enable') or client_dict.get('enabled')
                        enable_status = bool(enable_raw_value) if enable_raw_value is not None else False
                        logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enable через __dict__ = {enable_raw_value} (bool: {enable_status})")
                except Exception as e:
                    logger.warning(f"[ENABLE_STATUS][MULTI] Не удалось получить enable через model_dump/__dict__: {e}")
            
            # Если все еще не получили enable, пробуем получить из inbound напрямую
            if enable_raw_value is None and inbound_id:
                try:
                    inbound_for_enable = await self._find_inbound_by_id(client_api, inbound_id)
                    if inbound_for_enable and hasattr(inbound_for_enable, 'settings') and inbound_for_enable.settings:
                        if hasattr(inbound_for_enable.settings, 'clients') and inbound_for_enable.settings.clients:
                            for c in inbound_for_enable.settings.clients:
                                if (hasattr(c, 'email') and c.email == client_email) or \
                                   (hasattr(c, 'id') and getattr(c, 'id', None) == user_uuid) or \
                                   (hasattr(c, 'password') and getattr(c, 'password', None) == user_uuid):
                                    if hasattr(c, 'enable'):
                                        enable_status = bool(getattr(c, 'enable', False))
                                        logger.info(f"[ENABLE_STATUS][MULTI] Клиент {identifier}: enable из inbound.settings.clients = {enable_status}")
                                        break
                except Exception as e:
                    logger.debug(f"[ENABLE_STATUS][MULTI] Не удалось получить enable из inbound: {e}")
            
            logger.info(f"[ENABLE_STATUS][MULTI] Финальный enable статус для клиента {identifier}: {enable_status} (тип клиента: {type(client)})")
            
            # Пробуем получить last_online и all_time разными способами
            # Функция для безопасного получения значения
            def _get_value_multi(obj, *attr_names, default=0):
                logger.debug(f"[MULTI][_get_value] Ищем поля {attr_names} в объекте типа {type(obj)}")
                
                # Сначала пробуем через атрибуты
                for attr_name in attr_names:
                    try:
                        has_attr = hasattr(obj, attr_name)
                        logger.debug(f"[MULTI][_get_value] hasattr({attr_name}): {has_attr}")
                        if has_attr:
                            val = getattr(obj, attr_name, None)
                            logger.debug(f"[MULTI][_get_value] getattr({attr_name}): {val} (тип: {type(val)})")
                            if val is not None and val != 0:
                                logger.info(f"[MULTI][_get_value] Найдено значение {attr_name}={val}")
                                return val
                    except Exception as e:
                        logger.debug(f"[MULTI][_get_value] Ошибка при получении {attr_name}: {e}")
                
                # Пробуем через model_dump или __dict__ - ВАЖНО: проверяем ВСЕ ключи
                try:
                    obj_dict = None
                    if hasattr(obj, 'model_dump'):
                        obj_dict = obj.model_dump()
                        logger.info(f"[MULTI][_get_value] model_dump() вернул ключи: {list(obj_dict.keys())}")
                    elif hasattr(obj, '__dict__'):
                        obj_dict = obj.__dict__
                        logger.info(f"[MULTI][_get_value] __dict__ содержит ключи: {list(obj_dict.keys())}")
                    
                    if obj_dict:
                        # Проверяем все варианты имен полей
                        for attr_name in attr_names:
                            val = obj_dict.get(attr_name)
                            if val is not None and val != 0:
                                logger.info(f"[MULTI][_get_value] Найдено через dict {attr_name}={val}")
                                return val
                        # Также пробуем найти по частичному совпадению (на случай разных регистров)
                        obj_dict_lower = {k.lower(): v for k, v in obj_dict.items()}
                        for attr_name in attr_names:
                            val = obj_dict_lower.get(attr_name.lower())
                            if val is not None and val != 0:
                                logger.info(f"[MULTI][_get_value] Найдено через dict (lowercase) {attr_name.lower()}={val}")
                                return val
                except Exception as e:
                    logger.debug(f"[MULTI][_get_value] Не удалось получить значение через model_dump/__dict__: {e}")
                
                # Если ничего не нашли - это нормально, не все серверы возвращают эти поля
                logger.debug(f"[MULTI][_get_value] Не найдено ни одно из полей {attr_names} (это нормально, если сервер не возвращает эти данные), возвращаем default={default}")
                return default
            
            # Пробуем получить last_online (приоритет: lastOnline, затем last_online)
            last_online_val = _get_value_multi(client, 'lastOnline', 'last_online', default=0)
            
            # Пробуем получить all_time (приоритет: allTime, затем all_time)
            all_time_val = _get_value_multi(client, 'allTime', 'all_time', default=0)
            
            logger.info(f"[MULTI][TRAFFIC_STATS] Извлечено для клиента {identifier}: last_online={last_online_val}, all_time={all_time_val} (тип клиента: {type(client)})")
            
            stats = {
                'uuid': user_uuid,
                'email': client_email,
                'total_gb': getattr(client, 'total_gb', 0),
                'up': getattr(client, 'up', 0),
                'down': getattr(client, 'down', 0),
                'enable': enable_status,
                'expiry_time': getattr(client, 'expiry_time', 0) or getattr(client, 'expiryTime', 0),
                'limit_ip': getattr(client, 'limit_ip', 0) or getattr(client, 'limitIp', 0),
                'last_online': last_online_val,
                'all_time': all_time_val
            }
            
            logger.info(f"DEBUG: Базовая статистика клиента: up={stats['up']}, down={stats['down']}, total_gb={stats['total_gb']}, last_online={stats.get('last_online', 0)}, all_time={stats.get('all_time', 0)}")
            
            # Проверяем поле total для лимита трафика
            if hasattr(client, 'total') and getattr(client, 'total', 0) > 0:
                stats['total_gb'] = getattr(client, 'total', 0)
                logger.info(f"DEBUG: Обновлен total_gb из поля total: {stats['total_gb']}")
            
            # Прямой запрос getClientTraffics для lastOnline/allTime — ВСЕГДА когда есть email клиента
            # (py3xui Client не содержит lastOnline, нужен сырой JSON)
            if client_email:
                try:
                    import httpx as _httpx
                    endpoint = f"panel/api/inbounds/getClientTraffics/{client_email}"
                    _url_builder = getattr(client_api.client, "_url", None)
                    _cookies = getattr(client_api.client, "cookies", {})
                    if callable(_url_builder):
                        raw_url = _url_builder(endpoint)
                        async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True, timeout=10.0) as _c:
                            raw_resp = await _c.get(raw_url, headers={"Accept": "application/json"})
                            if raw_resp.status_code == 200:
                                response_text = raw_resp.text.strip()
                                if response_text:
                                    try:
                                        raw_json = raw_resp.json()
                                        if raw_json.get('success') and raw_json.get('obj'):
                                            obj_data = raw_json['obj']
                                            if isinstance(obj_data, list) and obj_data:
                                                obj_data = obj_data[0]
                                            if isinstance(obj_data, dict):
                                                raw_lo = obj_data.get('lastOnline') or obj_data.get('last_online') or 0
                                                raw_at = obj_data.get('allTime') or obj_data.get('all_time') or 0
                                                if raw_lo:
                                                    stats['last_online'] = raw_lo
                                                    logger.info(f"[MULTI][TRAFFIC_STATS] last_online из getClientTraffics: {raw_lo}")
                                                if raw_at:
                                                    stats['all_time'] = raw_at
                                    except Exception as json_err:
                                        logger.debug(f"[MULTI][TRAFFIC_STATS] Ошибка парсинга getClientTraffics: {json_err}")
                except Exception as raw_err:
                    logger.debug(f"[MULTI][TRAFFIC_STATS] Ошибка прямого getClientTraffics: {raw_err}")
            
            # Попробуем получить статистику через API
            # Приоритет: используем email, если он есть
            try:
                # Сначала пытаемся получить статистику по email (более надежно)
                if user_email:
                    try:
                        logger.info(f"DEBUG: Пытаемся получить статистику через get_by_email для {user_email}")
                        client_by_email = await client_api.client.get_by_email(user_email)
                        if client_by_email:
                            # Обновляем статистику из объекта клиента
                            if hasattr(client_by_email, 'up'):
                                stats['up'] = getattr(client_by_email, 'up', stats['up'])
                            if hasattr(client_by_email, 'down'):
                                stats['down'] = getattr(client_by_email, 'down', stats['down'])
                            if hasattr(client_by_email, 'total_gb'):
                                stats['total_gb'] = getattr(client_by_email, 'total_gb', stats['total_gb'])
                            elif hasattr(client_by_email, 'total'):
                                stats['total_gb'] = getattr(client_by_email, 'total', stats['total_gb'])
                            # Обновляем enable статус
                            if hasattr(client_by_email, 'enable'):
                                stats['enable'] = bool(getattr(client_by_email, 'enable', False))
                            elif hasattr(client_by_email, 'enabled'):
                                stats['enable'] = bool(getattr(client_by_email, 'enabled', False))
                            # last_online/all_time уже получены из прямого getClientTraffics выше (Client модель их не содержит)
                            last_online_new = _get_value_multi(client_by_email, 'lastOnline', 'last_online', default=0)
                            all_time_new = _get_value_multi(client_by_email, 'allTime', 'all_time', default=0)
                            if last_online_new:
                                stats['last_online'] = last_online_new
                            if all_time_new:
                                stats['all_time'] = all_time_new
                            logger.info(f"[MULTI][TRAFFIC_STATS] Обновлено из get_by_email: last_online={last_online_new}, all_time={all_time_new}")
                    except Exception as email_err:
                        logger.debug(f"Не удалось получить статистику через get_by_email для {user_email}: {email_err}")
                
                # Fallback: используем get_traffic_by_id
                if not user_email or stats['up'] == 0 and stats['down'] == 0:
                    logger.info(f"DEBUG: Пытаемся получить статистику через get_traffic_by_id для {user_uuid}")
                    try:
                        client_stats = await client_api.client.get_traffic_by_id(user_uuid)
                        if client_stats:
                            logger.info(f"DEBUG: API вернул статистику: {client_stats}")
                            if isinstance(client_stats, list) and len(client_stats) > 0:
                                # API возвращает список, берем первый элемент
                                client_stat = client_stats[0]
                                if hasattr(client_stat, 'up'):
                                    stats['up'] = client_stat.up
                                if hasattr(client_stat, 'down'):
                                    stats['down'] = client_stat.down
                                if hasattr(client_stat, 'total_gb'):
                                    stats['total_gb'] = client_stat.total_gb
                                logger.info(f"DEBUG: Обновлена статистика из API (список): up={stats['up']}, down={stats['down']}")
                            else:
                                # API возвращает объект напрямую
                                if hasattr(client_stats, 'up'):
                                    stats['up'] = client_stats.up
                                if hasattr(client_stats, 'down'):
                                    stats['down'] = client_stats.down
                                if hasattr(client_stats, 'total_gb'):
                                    stats['total_gb'] = client_stats.total_gb
                                logger.info(f"DEBUG: Обновлена статистика из API (объект): up={stats['up']}, down={stats['down']}")
                        else:
                            logger.info(f"DEBUG: API не вернул статистику")
                    except Exception as uuid_err:
                        logger.debug(f"Не удалось получить статистику через get_traffic_by_id для {user_uuid}: {uuid_err}")
            except Exception as e:
                logger.debug(f"Не удалось получить статистику через API для {identifier} на сервере {server.get('name', 'Unknown')}: {e}")
            
            # Вычисляем использованный трафик
            total_used = stats['up'] + stats['down']
            stats['total_used_gb'] = total_used / (1024**3) if total_used > 0 else 0
            stats['up_gb'] = stats['up'] / (1024**3) if stats['up'] > 0 else 0
            stats['down_gb'] = stats['down'] / (1024**3) if stats['down'] > 0 else 0
            
            # Определяем статус использования трафика
            if stats['total_gb'] > 0:
                stats['usage_percent'] = (stats['total_used_gb'] / stats['total_gb']) * 100
                stats['has_traffic_limit'] = True
            else:
                stats['usage_percent'] = 0
                stats['has_traffic_limit'] = False
            
            logger.info(f"DEBUG: Финальная статистика: total_used_gb={stats['total_used_gb']}, usage_percent={stats['usage_percent']}")
            return stats
            
        except Exception as e:
            logger.error(f"Ошибка получения трафика для {user_uuid} на сервере {server.get('name', 'Unknown')}: {e}")
            return None


    async def restart_xray_legacy(self, server_settings: Dict) -> bool:
        """Перезапуск Xray через старые методы (< 2.6.7): server.restartXrayService и аналоги.
        Пробует сначала Python-методы клиента, затем прямой POST на server/restartXrayService.
        """
        try:
            client_api = await self.get_client(server_settings)
            if not client_api:
                return False
            # 1) py3xui объект server
            try:
                srv = getattr(client_api, 'server', None)
                for name in ('restartXrayService', 'restart_xray', 'reload_xray', 'restart'):
                    if srv and hasattr(srv, name):
                        fn = getattr(srv, name)
                        res = await fn()
                        return True if (res is None or res is True) else bool(res)
            except Exception:
                pass
            # 2) Прямой POST на server/restartXrayService
            try:
                import httpx as _httpx
                _url_builder = getattr(client_api.client, "_url", None)
                _cookies = getattr(client_api.client, "cookies", {})
                if callable(_url_builder):
                    url = _url_builder("server/restartXrayService")
                    async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True, timeout=15) as hc:
                        resp = await hc.post(url, headers={"Accept": "application/json"})
                        return resp.status_code == 200
            except Exception:
                pass
            return False
        except Exception:
            return False

    async def restart_xray_panel_api(self, server_settings: Dict) -> bool:
        """Перезапуск Xray через panel/api/server/restartXrayService (>= 2.7.0)."""
        try:
            client_api = await self.get_client(server_settings)
            if not client_api:
                return False
            try:
                import httpx as _httpx
                _url_builder = getattr(client_api.client, "_url", None)
                _cookies = getattr(client_api.client, "cookies", {})
                if callable(_url_builder):
                    url = _url_builder("panel/api/server/restartXrayService")
                    async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True, timeout=15) as hc:
                        resp = await hc.post(url, headers={"Accept": "application/json"})
                        return resp.status_code == 200
            except Exception:
                pass
            return False
        except Exception:
            return False


    async def delete_depleted_clients(self, server_settings: Dict, all_inbounds: bool = False) -> bool:
        """Удаляет клиентов с исчерпанным лимитом/сроком через API delDepletedClients.
        Если all_inbounds=True — передаём -1, иначе используем inbound_id из настроек.
        Возвращает True при успехе.
        """
        try:
            client_api = await self.get_client(server_settings)
            if not client_api:
                return False
            target_id = -1 if all_inbounds else int(server_settings.get('inbound_id') or -1)
            # 1) Пытаемся вызвать метод клиента, если существует
            try:
                if hasattr(client_api.inbound, 'del_depleted_clients'):
                    res = await client_api.inbound.del_depleted_clients(target_id)
                    return True if (res is None or res is True) else bool(res)
                if hasattr(client_api.inbound, 'delDepletedClients'):
                    res = await client_api.inbound.delDepletedClients(target_id)
                    return True if (res is None or res is True) else bool(res)
            except Exception:
                pass
            # 2) Фолбэк — прямой HTTP вызов panel/api
            try:
                import httpx as _httpx
                _url_builder = getattr(client_api.client, "_url", None)
                _cookies = getattr(client_api.client, "cookies", {})
                if callable(_url_builder):
                    endpoint = f"panel/api/inbounds/delDepletedClients/{target_id}"
                    url = _url_builder(endpoint)
                    async with _httpx.AsyncClient(cookies=_cookies, verify=False, follow_redirects=True, timeout=15) as hc:
                        resp = await hc.post(url, headers={"Accept": "application/json"})
                        return resp.status_code == 200
            except Exception:
                pass
            return False
        except Exception:
            return False

xui_manager_instance = XUIManager()

    