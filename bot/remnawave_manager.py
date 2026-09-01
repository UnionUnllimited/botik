import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
from uuid import UUID

from loguru import logger
from remnawave import RemnawaveSDK
from remnawave.models import CreateUserRequestDto, UpdateUserRequestDto
from remnawave.models import CreateConfigProfileRequestDto, UpdateConfigProfileRequestDto
from remnawave.exceptions import ApiError, NotFoundError

from app_config import app_conf


def _parse_metric_counter_string(val: Optional[str]) -> float:
    """Строковые счётчики из Prometheus/Xray в метриках ноды (часто это число в строке)."""
    if val is None:
        return 0.0
    s = str(val).strip().replace(',', '').replace('\xa0', '').replace(' ', '')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _sum_traffic_stat_list(stats: Any) -> tuple[float, float]:
    """Суммирует upload/download по списку TrafficStatDto или dict."""
    up = dn = 0.0
    if not stats:
        return up, dn
    for st in stats:
        if st is None:
            continue
        if isinstance(st, dict):
            up += _parse_metric_counter_string(st.get('upload'))
            dn += _parse_metric_counter_string(st.get('download'))
        else:
            up += _parse_metric_counter_string(getattr(st, 'upload', None))
            dn += _parse_metric_counter_string(getattr(st, 'download', None))
    return up, dn


def normalize_remnawave_node_uuid(uid: Optional[str]) -> str:
    """Единый ключ для сопоставления ноды из /nodes и /bandwidth-stats/nodes/realtime."""
    if uid is None:
        return ''
    s = str(uid).strip().lower()
    if not s:
        return ''
    compact = s.replace('-', '')
    if len(compact) == 32:
        try:
            return str(UUID(hex=compact)).lower()
        except ValueError:
            return s
    try:
        return str(UUID(s)).lower()
    except ValueError:
        return s


def _realtime_bandwidth_row_as_dict(row: Any) -> Dict[str, Any]:
    """Строка realtime-stats: pydantic-модель, dict или обёртка с model_dump."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    md = getattr(row, 'model_dump', None)
    if callable(md):
        try:
            return md(by_alias=True)
        except TypeError:
            return md()
    dct = getattr(row, 'dict', None)
    if callable(dct):
        try:
            return dct(by_alias=True)
        except TypeError:
            return dct()
    return {}


def _coalesce_float(d: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k not in d:
            continue
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


class RemnawaveManager:
    """Менеджер для работы с Remnawave API"""
    
    def __init__(self):
        self._sdk: Optional[RemnawaveSDK] = None
        self._initialized = False
    
    async def _ensure_initialized(self):
        """Инициализирует SDK, если еще не инициализирован"""
        if self._initialized and self._sdk:
            return
        
        base_url = app_conf.get('remnawave_base_url')
        api_token = app_conf.get('remnawave_api_token')
        
        if not base_url or not api_token:
            logger.error("[REMNAWAVE] Не указаны base_url или api_token в настройках")
            raise ValueError("Remnawave не настроен: отсутствуют base_url или api_token")
        
        try:
            self._sdk = RemnawaveSDK(
                base_url=base_url,
                token=api_token,
                ssl_ignore=False
            )
            self._initialized = True
            logger.info(f"[REMNAWAVE] SDK инициализирован: {base_url}")
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка инициализации SDK: {e}")
            raise
    
    @property
    def sdk(self) -> Optional[RemnawaveSDK]:
        """Возвращает SDK (для доступа из других модулей)"""
        return self._sdk
    
    async def create_user(
        self,
        telegram_id: int,
        username: str,
        days_valid: int,
        total_gb: int = 0,
        description: Optional[str] = None,
        internal_squad_uuid: Optional[str] = None,
        short_uuid: Optional[str] = None,
        user_uuid: Optional[str] = None,
        base_expiry: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Создает пользователя в Remnawave
        
        Args:
            telegram_id: Telegram ID пользователя
            username: Имя пользователя (должно соответствовать паттерну ^[a-zA-Z0-9_-]+$)
            days_valid: Количество дней действия подписки
            total_gb: Лимит трафика в ГБ (0 = безлимит)
            description: Описание пользователя
            internal_squad_uuid: UUID внутреннего сквада (опционально)
            short_uuid: Короткий UUID пользователя (опционально)
            user_uuid: Полный UUID пользователя (опционально)
            base_expiry: Базовое время для расчета даты окончания (опционально, для синхронизации с X-UI)
        
        Returns:
            Dict с данными пользователя или None при ошибке
        """
        await self._ensure_initialized()
        
        try:
            # Вычисляем дату окончания подписки.
            # Используем base_expiry только если он в будущем (активная подписка).
            # Если base_expiry в прошлом — считаем от текущего момента,
            # чтобы не выдать пользователю уже истёкшую дату окончания.
            now_utc = datetime.now(timezone.utc)
            if base_expiry:
                if base_expiry.tzinfo is None:
                    base_expiry = base_expiry.replace(tzinfo=timezone.utc)
                else:
                    base_expiry = base_expiry.astimezone(timezone.utc)

                if base_expiry > now_utc:
                    # Активная подписка — продлеваем от текущего конца
                    expire_at = base_expiry + timedelta(days=days_valid)
                    logger.debug(f"[REMNAWAVE] base_expiry в будущем ({base_expiry}), продлеваем от него")
                else:
                    # Истёкшая — считаем от сейчас
                    expire_at = now_utc + timedelta(days=days_valid)
                    logger.debug(f"[REMNAWAVE] base_expiry в прошлом ({base_expiry}), считаем от now")
            else:
                expire_at = now_utc + timedelta(days=days_valid)
                logger.debug(f"[REMNAWAVE] base_expiry не передан, используем текущее время")
            
            # Переводим трафик из ГБ в байты
            traffic_limit_bytes = total_gb * 1024 * 1024 * 1024 if total_gb > 0 else None
            
            # Подготавливаем список внутренних сквадов
            active_internal_squads_list = []
            if internal_squad_uuid:
                try:
                    # Поддерживаем несколько UUID через запятую
                    squad_uuids_str = [s.strip() for s in internal_squad_uuid.split(',')]
                    for squad_uuid_str in squad_uuids_str:
                        if squad_uuid_str:  # Пропускаем пустые строки
                            squad_uuid_obj = UUID(squad_uuid_str)
                            active_internal_squads_list.append(squad_uuid_obj)
                    if active_internal_squads_list:
                        logger.info(f"[REMNAWAVE] Используется {len(active_internal_squads_list)} internal squad(s): {internal_squad_uuid}")
                except ValueError as e:
                    logger.warning(f"[REMNAWAVE] Неверный формат UUID для internal squad: {internal_squad_uuid}, ошибка: {e}")
            
            # Если не указан internal_squad_uuid, пробуем взять из настроек
            if not active_internal_squads_list:
                default_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
                if default_squad_uuid:
                    try:
                        # Поддерживаем несколько UUID через запятую
                        squad_uuids_str = [s.strip() for s in default_squad_uuid.split(',')]
                        for squad_uuid_str in squad_uuids_str:
                            if squad_uuid_str:  # Пропускаем пустые строки
                                squad_uuid_obj = UUID(squad_uuid_str)
                                active_internal_squads_list.append(squad_uuid_obj)
                        if active_internal_squads_list:
                            logger.info(f"[REMNAWAVE] Используется {len(active_internal_squads_list)} default internal squad(s) из настроек: {default_squad_uuid}")
                    except ValueError as e:
                        logger.warning(f"[REMNAWAVE] Неверный формат UUID для default internal squad: {default_squad_uuid}, ошибка: {e}")
            
            # Формируем описание
            if not description:
                description = f"Telegram ID: {telegram_id}"
            
            # Подготавливаем UUID пользователя, если передан
            user_uuid_obj = None
            if user_uuid:
                try:
                    user_uuid_obj = UUID(user_uuid)
                    logger.info(f"[REMNAWAVE] Используется переданный UUID пользователя: {user_uuid_obj}")
                except ValueError as e:
                    logger.warning(f"[REMNAWAVE] Неверный формат UUID для user_uuid: {user_uuid}, ошибка: {e}")
            
            # Создаем запрос на создание пользователя
            # vless_uuid берём из того же xui_client_uuid (user_uuid), чтобы VLESS использовал тот же UUID
            create_request = CreateUserRequestDto(
                username=username,
                expire_at=expire_at,
                traffic_limit_bytes=traffic_limit_bytes,
                description=description,
                telegram_id=telegram_id,
                active_internal_squads=active_internal_squads_list if active_internal_squads_list else None,
                short_uuid=short_uuid,  # Передаем short_uuid из БД, если указан
                uuid=user_uuid_obj,  # Передаем полный UUID пользователя (remnawave_user_uuid), если указан
                vless_uuid=user_uuid_obj  # VLESS UUID = xui_client_uuid (тот же, что и user_uuid)
            )
            
            logger.info(f"[REMNAWAVE] Создание пользователя: username={username}, days={days_valid}, traffic_gb={total_gb}, short_uuid={short_uuid}, user_uuid={user_uuid_obj}, vless_uuid={user_uuid_obj}")
            
            # Создаем пользователя
            try:
                user_response = await self._sdk.users.create_user(create_request)
            except ApiError as create_error:
                # Если ошибка связана с internal_squad, пробуем без него
                if active_internal_squads_list and create_error.code in ['A018', 'INTERNAL_SQUAD_NOT_FOUND', 'NOT_FOUND']:
                    logger.warning(f"[REMNAWAVE] Ошибка при создании с Internal Squad: {create_error.message}, пробуем без squad")
                    create_request.active_internal_squads = None
                    user_response = await self._sdk.users.create_user(create_request)
                    logger.info(f"[REMNAWAVE] Пользователь создан без Internal Squad")
                else:
                    raise
            
            # Проверяем, был ли передан short_uuid и соответствует ли он тому, что вернул Remnawave
            if short_uuid and user_response.short_uuid != short_uuid:
                logger.warning(f"[REMNAWAVE] ВНИМАНИЕ: Переданный short_uuid ({short_uuid}) не совпадает с возвращенным Remnawave ({user_response.short_uuid}). Remnawave может игнорировать переданный short_uuid.")
            elif short_uuid and user_response.short_uuid == short_uuid:
                logger.success(f"[REMNAWAVE] Short UUID успешно установлен: {short_uuid}")
            
            logger.success(f"[REMNAWAVE] Пользователь создан: UUID={user_response.uuid}, Short UUID={user_response.short_uuid}")
            
            # Возвращаем данные в формате, совместимом с X-UI
            # ВАЖНО: Если мы передали short_uuid, но Remnawave вернул другой, используем переданный
            final_short_uuid = short_uuid if short_uuid else user_response.short_uuid
            return {
                "uuid": str(user_response.uuid),
                "short_uuid": final_short_uuid,
                "username": user_response.username,
                "email": user_response.email or f"tg{telegram_id}@remnawave.local",
                "expiry_timestamp_ms": int(user_response.expire_at.timestamp() * 1000),
                "traffic_limit_bytes": user_response.traffic_limit_bytes,
                "status": user_response.status,
                "vless_uuid": getattr(user_response, 'vless_uuid', None),
                "subscription_url": user_response.subscription_url if hasattr(user_response, 'subscription_url') else None,
            }
            
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при создании пользователя: код={e.code}, сообщение={e.message}, HTTP={e.status_code}")
            if e.path:
                logger.error(f"[REMNAWAVE] Путь: {e.path}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Неожиданная ошибка при создании пользователя: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """
        Получает пользователя по Telegram ID
        
        Args:
            telegram_id: Telegram ID пользователя
        
        Returns:
            Dict с данными пользователя или None
        """
        await self._ensure_initialized()
        
        try:
            # Получаем всех пользователей и ищем по telegram_id
            # Примечание: это не оптимально, но API Remnawave не имеет прямого поиска по telegram_id
            all_users = await self._sdk.users.get_all_users()
            
            for user in all_users.users:
                if user.telegram_id == telegram_id:
                    return {
                        "uuid": str(user.uuid),
                        "short_uuid": user.short_uuid,
                        "username": user.username,
                        "email": user.email,
                        "expiry_timestamp_ms": int(user.expire_at.timestamp() * 1000) if user.expire_at else None,
                        "traffic_limit_bytes": user.traffic_limit_bytes,
                        "status": user.status,
                    }
            
            return None
            
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при получении пользователя: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении пользователя: {type(e).__name__}: {e}")
            return None
    
    async def get_user_by_uuid(self, user_uuid: str) -> Optional[Dict[str, Any]]:
        """
        Получает пользователя по UUID
        
        Args:
            user_uuid: UUID пользователя в Remnawave
        
        Returns:
            Dict с данными пользователя или None
        """
        await self._ensure_initialized()
        
        try:
            user_response = await self._sdk.users.get_user_by_uuid(user_uuid)
            
            return {
                "uuid": str(user_response.uuid),
                "short_uuid": user_response.short_uuid,
                "username": user_response.username,
                "email": user_response.email,
                "expiry_timestamp_ms": int(user_response.expire_at.timestamp() * 1000) if user_response.expire_at else None,
                "traffic_limit_bytes": user_response.traffic_limit_bytes,
                "status": user_response.status,
            }
            
        except ApiError as e:
            if e.code == "A063" or e.status_code == 404:
                logger.debug(f"[REMNAWAVE] Пользователь с UUID {user_uuid} не найден")
            else:
                logger.error(f"[REMNAWAVE] Ошибка API при получении пользователя: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении пользователя: {type(e).__name__}: {e}")
            return None
    
    async def update_user_subscription(
        self,
        user_uuid: str,
        days_to_add: int,
        traffic_limit_gb: Optional[int] = None,
        traffic_to_add_gb: Optional[int] = None,
        current_expiry: Optional[datetime] = None,
        apply_default_squad: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Продлевает подписку пользователя
        
        Args:
            user_uuid: UUID пользователя
            days_to_add: Количество дней для добавления
            traffic_limit_gb: Новый лимит трафика в ГБ (опционально, если указан, устанавливает абсолютное значение)
            traffic_to_add_gb: Количество ГБ трафика для добавления к текущему лимиту (опционально)
            current_expiry: Текущая дата окончания подписки из БД (опционально, если не указана, берется из Remnawave API)
        
        Returns:
            Обновленные данные пользователя или None
        """
        await self._ensure_initialized()
        
        try:
            # Получаем текущие данные пользователя
            current_user = await self._sdk.users.get_user_by_uuid(user_uuid)
            if not current_user:
                logger.error(f"[REMNAWAVE] Пользователь {user_uuid} не найден для обновления")
                return None
            
            # Вычисляем новую дату окончания
            # Используем current_expiry из БД, если передан, иначе берем из Remnawave API
            if current_expiry:
                # Приводим к UTC, если нужно
                if current_expiry.tzinfo is None:
                    current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                else:
                    current_expiry = current_expiry.astimezone(timezone.utc)
                
                # Используем время из БД как базовое
                if current_expiry > datetime.now(timezone.utc):
                    new_expire_at = current_expiry + timedelta(days=days_to_add)
                else:
                    new_expire_at = datetime.now(timezone.utc) + timedelta(days=days_to_add)
                logger.debug(f"[REMNAWAVE] Используем время из БД ({current_expiry}) как базовое для расчета новой даты окончания")
            else:
                # Fallback: используем время из Remnawave API
                current_expire = current_user.expire_at
                if current_expire and current_expire > datetime.now(timezone.utc):
                    new_expire_at = current_expire + timedelta(days=days_to_add)
                else:
                    new_expire_at = datetime.now(timezone.utc) + timedelta(days=days_to_add)
                logger.debug(f"[REMNAWAVE] Используем время из Remnawave API ({current_expire}) как базовое для расчета новой даты окончания")
            
            # Вычисляем новый лимит трафика
            new_traffic_limit_bytes = None
            if traffic_limit_gb is not None:
                # Если указан абсолютный лимит, используем его
                new_traffic_limit_bytes = traffic_limit_gb * 1024 * 1024 * 1024 if traffic_limit_gb > 0 else None
            elif traffic_to_add_gb is not None and traffic_to_add_gb > 0:
                # Если указано количество для добавления, добавляем к текущему лимиту
                current_limit_bytes = current_user.traffic_limit_bytes
                if current_limit_bytes and current_limit_bytes > 0:
                    # Текущий лимит в ГБ
                    current_limit_gb = current_limit_bytes / (1024 ** 3)
                    # Новый лимит = текущий + добавленный
                    new_limit_gb = current_limit_gb + traffic_to_add_gb
                    new_traffic_limit_bytes = int(new_limit_gb * 1024 * 1024 * 1024)
                    logger.info(f"[REMNAWAVE] Продление трафика: текущий лимит {current_limit_gb:.1f}GB, добавляем {traffic_to_add_gb}GB, новый лимит {new_limit_gb:.1f}GB")
                else:
                    # Если текущий лимит безлимит (0 или None), убираем безлимит и устанавливаем лимит = купленные GB
                    # Использованный трафик будет обнулен после обновления пользователя
                    new_traffic_limit_bytes = int(traffic_to_add_gb * 1024 * 1024 * 1024)
                    logger.info(f"[REMNAWAVE] Текущий лимит безлимит, убираем безлимит и устанавливаем лимит = {traffic_to_add_gb}GB (купленные GB)")
            else:
                # Сохраняем текущий лимит
                new_traffic_limit_bytes = current_user.traffic_limit_bytes
            
            # Определяем, какие internal squads использовать
            if apply_default_squad:
                # Применяем squad из настроек (для платного продления и покупки гигабайт)
                current_squads = []
                default_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
                if default_squad_uuid:
                    try:
                        # Поддерживаем несколько UUID через запятую
                        squad_uuids_str = [s.strip() for s in default_squad_uuid.split(',')]
                        for squad_uuid_str in squad_uuids_str:
                            if squad_uuid_str:  # Пропускаем пустые строки
                                squad_uuid_obj = UUID(squad_uuid_str)
                                current_squads.append(squad_uuid_obj)
                        if current_squads:
                            logger.info(f"[REMNAWAVE] Применяется {len(current_squads)} default internal squad(s) из настроек: {default_squad_uuid}")
                    except ValueError as e:
                        logger.warning(f"[REMNAWAVE] Неверный формат UUID для default internal squad: {default_squad_uuid}, ошибка: {e}")
                        # Fallback: сохраняем текущие squads
                        current_squads = [s.uuid for s in current_user.active_internal_squads] if current_user.active_internal_squads else []
                else:
                    # Если настройка не указана, сохраняем текущие squads
                    current_squads = [s.uuid for s in current_user.active_internal_squads] if current_user.active_internal_squads else []
            else:
                # Сохраняем текущие internal squads (для промо кодов, бонусов, бесплатного продления)
                current_squads = [s.uuid for s in current_user.active_internal_squads] if current_user.active_internal_squads else []
            
            # Подготавливаем данные для обновления, исключая None и невалидные значения
            # Проверяем tag на соответствие паттерну (только заглавные буквы, цифры и подчеркивания)
            tag_value = None
            if current_user.tag:
                tag_pattern = r"^[A-Z0-9_]+$"
                if re.match(tag_pattern, current_user.tag):
                    tag_value = current_user.tag
                else:
                    logger.warning(f"[REMNAWAVE] Тег '{current_user.tag}' не соответствует паттерну, будет пропущен")
            
            # Создаем словарь с данными для обновления, исключая None значения для полей с ограничениями
            update_data = {
                "uuid": current_user.uuid,
                "expire_at": new_expire_at,
            }
            
            # Добавляем только не-None значения для полей с ограничениями
            if new_traffic_limit_bytes is not None:
                update_data["traffic_limit_bytes"] = new_traffic_limit_bytes
            
            if current_squads:
                update_data["active_internal_squads"] = current_squads
            
            # Не передаем статус EXPIRED или LIMITED - они управляются Remnawave автоматически
            # Разрешаем передавать только ACTIVE или DISABLED
            if current_user.status is not None and current_user.status not in ["EXPIRED", "LIMITED"]:
                update_data["status"] = current_user.status
            elif current_user.status in ["EXPIRED", "LIMITED"]:
                logger.debug(f"[REMNAWAVE] Статус '{current_user.status}' не передается в запрос обновления (управляется Remnawave)")
            
            if current_user.description is not None:
                update_data["description"] = current_user.description
            
            if current_user.email is not None:
                update_data["email"] = current_user.email
            
            if current_user.telegram_id is not None:
                update_data["telegram_id"] = current_user.telegram_id
            
            if current_user.traffic_limit_strategy is not None:
                update_data["traffic_limit_strategy"] = current_user.traffic_limit_strategy
            
            if tag_value is not None:
                update_data["tag"] = tag_value
            
            if current_user.hwid_device_limit is not None and current_user.hwid_device_limit >= 0:
                update_data["hwid_device_limit"] = current_user.hwid_device_limit
            
            # Создаем запрос на обновление
            update_request = UpdateUserRequestDto(**update_data)
            
            logger.info(f"[REMNAWAVE] Обновление подписки: UUID={user_uuid}, +{days_to_add} дней")
            logger.debug(f"[REMNAWAVE] Данные для обновления: {update_data}")
            
            # Сохраняем информацию о том, был ли безлимит, для последующего обнуления трафика
            was_unlimited = not current_user.traffic_limit_bytes or current_user.traffic_limit_bytes == 0
            traffic_was_added = traffic_to_add_gb is not None and traffic_to_add_gb > 0
            
            updated_user = await self._sdk.users.update_user(update_request)
            
            # Если был безлимит и мы добавили трафик, нужно обнулить использованный трафик
            # Это делается после обновления лимита, чтобы не потерять изменения
            if was_unlimited and traffic_was_added:
                try:
                    # Обнуляем использованный трафик после установки лимита
                    await self._sdk.users.reset_user_traffic(user_uuid)
                    logger.info(f"[REMNAWAVE] Использованный трафик обнулен после установки лимита {traffic_to_add_gb}GB (был безлимит)")
                    # Получаем обновленного пользователя после сброса трафика
                    updated_user = await self._sdk.users.get_user_by_uuid(user_uuid)
                except Exception as e:
                    logger.warning(f"[REMNAWAVE] Не удалось обнулить использованный трафик после установки лимита: {e}")
            
            logger.success(f"[REMNAWAVE] Подписка обновлена: новый срок до {updated_user.expire_at}")
            
            return {
                "uuid": str(updated_user.uuid),
                "short_uuid": updated_user.short_uuid,
                "username": updated_user.username,
                "email": updated_user.email,
                "expiry_timestamp_ms": int(updated_user.expire_at.timestamp() * 1000),
                "traffic_limit_bytes": updated_user.traffic_limit_bytes,
                "status": updated_user.status,
            }
            
        except ApiError as e:
            # Логируем детали ошибки валидации
            error_details = ""
            if hasattr(e, 'error') and hasattr(e.error, 'errors') and e.error.errors:
                error_details = f", детали: {e.error.errors}"
            logger.error(f"[REMNAWAVE] Ошибка API при обновлении подписки: код={e.code}, сообщение={e.message}{error_details}")
            logger.debug(f"[REMNAWAVE] Полный ответ ошибки: {e.error if hasattr(e, 'error') else 'N/A'}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при обновлении подписки: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def extend_user_expire_at(
        self,
        user_uuid: str,
        expire_at: datetime,
    ) -> Dict[str, Any]:
        """Продлевает только expire_at одним PATCH без предварительного GET."""
        await self._ensure_initialized()
        if expire_at.tzinfo is None:
            expire_at = expire_at.replace(tzinfo=timezone.utc)
        else:
            expire_at = expire_at.astimezone(timezone.utc)
        try:
            update_request = UpdateUserRequestDto(
                uuid=UUID(user_uuid),
                expire_at=expire_at,
            )
            updated_user = await self._sdk.users.update_user(update_request)
            return {
                "uuid": str(updated_user.uuid),
                "expire_at": updated_user.expire_at,
            }
        except NotFoundError:
            raise
        except ApiError as e:
            logger.error(
                f"[REMNAWAVE] extend_user_expire_at {user_uuid}: "
                f"код={e.code}, сообщение={e.message}"
            )
            raise
        except Exception as e:
            logger.error(
                f"[REMNAWAVE] extend_user_expire_at {user_uuid}: "
                f"{type(e).__name__}: {e}"
            )
            raise
    
    async def get_subscription_link(self, short_uuid: str) -> Optional[str]:
        """
        Получает ссылку на подписку для пользователя
        
        Args:
            short_uuid: Short UUID пользователя
        
        Returns:
            URL подписки или None
        """
        await self._ensure_initialized()
        
        try:
            subscription_info = await self._sdk.subscriptions.get_subscription_by_short_uuid(short_uuid)
            if subscription_info.is_found and subscription_info.subscription_url:
                return subscription_info.subscription_url
            return None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при получении ссылки подписки: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении ссылки подписки: {type(e).__name__}: {e}")
            return None
    
    async def get_raw_subscription(self, short_uuid: str) -> Optional[Dict[str, Any]]:
        """
        Получает сырые данные подписки (с серверами/хостами)
        
        Args:
            short_uuid: Short UUID пользователя
        
        Returns:
            Dict с данными подписки или None
        """
        await self._ensure_initialized()
        
        try:
            raw_subscription = await self._sdk.subscriptions.get_raw_subscription(short_uuid, withDisabledHosts=False)
            
            # Преобразуем в удобный формат
            hosts = []
            for host in raw_subscription.raw_hosts:
                hosts.append({
                    "address": host.address,
                    "port": host.port,
                    "protocol": host.protocol,
                    "remark": host.remark,
                    "network": host.network,
                    "host": host.host,
                    "path": host.path,
                    "tls": host.tls,
                    "sni": host.sni,
                    "alpn": host.alpn,
                    "flow": host.flow,
                    "password": {
                        "vless": host.password.vless_password,
                        "trojan": host.password.trojan_password,
                        "ss": host.password.ss_password,
                    }
                })
            
            return {
                "user": {
                    "uuid": str(raw_subscription.user.uuid),
                    "short_uuid": raw_subscription.user.short_uuid,
                    "username": raw_subscription.user.username,
                    "vless_uuid": raw_subscription.user.vless_uuid,
                },
                "hosts": hosts,
                "subscription_url": raw_subscription.subscription_url if hasattr(raw_subscription, 'subscription_url') else None,
            }
            
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при получении сырой подписки: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении сырой подписки: {type(e).__name__}: {e}")
            return None
    
    async def delete_user(self, user_uuid: str) -> bool:
        """
        Удаляет пользователя
        
        Args:
            user_uuid: UUID пользователя
        
        Returns:
            True если успешно, False иначе
        """
        await self._ensure_initialized()
        
        try:
            await self._sdk.users.delete_user(user_uuid)
            logger.info(f"[REMNAWAVE] Пользователь {user_uuid} удален")
            return True
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при удалении пользователя: код={e.code}, сообщение={e.message}")
            return False
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при удалении пользователя: {type(e).__name__}: {e}")
            return False
    
    async def reset_user_traffic(self, user_uuid: str, apply_default_squad: bool = False) -> Optional[Dict[str, Any]]:
        """
        Сбрасывает использованный трафик пользователя
        
        Args:
            user_uuid: UUID пользователя в Remnawave
            apply_default_squad: Применить squad из настроек после сброса трафика (для платных операций)
        
        Returns:
            Обновленные данные пользователя или None при ошибке
        """
        await self._ensure_initialized()
        
        try:
            updated_user = await self._sdk.users.reset_user_traffic(user_uuid)
            logger.success(f"[REMNAWAVE] Трафик сброшен для пользователя {user_uuid}")
            
            # Если нужно применить squad из настроек (для платных операций)
            if apply_default_squad:
                try:
                    default_squad_uuid = app_conf.get('remnawave_default_internal_squad_uuid')
                    if default_squad_uuid:
                        # Получаем текущего пользователя для обновления
                        current_user = await self._sdk.users.get_user_by_uuid(user_uuid)
                        if current_user:
                            # Подготавливаем список squads из настроек
                            squad_uuids_list = []
                            try:
                                squad_uuids_str = [s.strip() for s in default_squad_uuid.split(',')]
                                for squad_uuid_str in squad_uuids_str:
                                    if squad_uuid_str:
                                        squad_uuid_obj = UUID(squad_uuid_str)
                                        squad_uuids_list.append(squad_uuid_obj)
                                if squad_uuids_list:
                                    # Обновляем пользователя с новым squad
                                    from remnawave.models import UpdateUserRequestDto
                                    update_request = UpdateUserRequestDto(
                                        uuid=current_user.uuid,
                                        active_internal_squads=squad_uuids_list,
                                        expire_at=current_user.expire_at,
                                        traffic_limit_bytes=current_user.traffic_limit_bytes,
                                        traffic_limit_strategy=current_user.traffic_limit_strategy,
                                        status=current_user.status if current_user.status not in ["EXPIRED", "LIMITED"] else None,
                                        description=current_user.description,
                                        email=current_user.email,
                                        telegram_id=current_user.telegram_id,
                                        hwid_device_limit=current_user.hwid_device_limit,
                                        tag=current_user.tag if current_user.tag and re.match(r"^[A-Z0-9_]+$", current_user.tag) else None
                                    )
                                    updated_user = await self._sdk.users.update_user(update_request)
                                    logger.info(f"[REMNAWAVE] Применен squad из настроек при сбросе трафика для пользователя {user_uuid}")
                            except ValueError as e:
                                logger.warning(f"[REMNAWAVE] Неверный формат UUID для default internal squad при сбросе трафика: {default_squad_uuid}, ошибка: {e}")
                except Exception as e:
                    logger.warning(f"[REMNAWAVE] Ошибка применения squad при сбросе трафика: {e}")
                    # Продолжаем выполнение, даже если не удалось применить squad
            
            return {
                "uuid": str(updated_user.uuid),
                "short_uuid": updated_user.short_uuid,
                "traffic_limit_bytes": updated_user.traffic_limit_bytes,
                "traffic_used_bytes": getattr(updated_user, 'traffic_used_bytes', 0),
                "last_traffic_reset_at": getattr(updated_user, 'last_traffic_reset_at', None),
            }
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при сбросе трафика: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при сбросе трафика: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def get_nodes_metrics(self) -> Optional[Dict[str, Any]]:
        """
        Метрики нод: GET /system/nodes/metrics (Prometheus / Xray).

        В актуальном API ответ — NodeMetric с inboundsStats / outboundsStats (счётчики по тегам).
        Поля cpu_usage / network_* на уровне ноды в модели отсутствуют (возвращают None).

        Дополнительно считаем сумму байт по инбаундам и аутбандам для отображения на дашборде,
        когда недоступен realtime bandwidth.
        """
        await self._ensure_initialized()

        try:
            metrics_response = await self._sdk.system.get_nodes_metrics()

            nodes_data: List[Dict[str, Any]] = []
            for node in metrics_response.nodes:
                node_uuid = str(node.node_uuid) if getattr(node, 'node_uuid', None) else None

                users_online = getattr(node, 'users_online', None)
                connected_users = getattr(node, 'connected_users', users_online)

                in_stats = getattr(node, 'inbounds_stats', None) or []
                out_stats = getattr(node, 'outbounds_stats', None) or []
                in_up, in_dn = _sum_traffic_stat_list(in_stats)
                out_up, out_dn = _sum_traffic_stat_list(out_stats)

                nodes_data.append({
                    'uuid': node_uuid,
                    'node_uuid': node_uuid,
                    'name': getattr(node, 'node_name', None) or getattr(node, 'name', None),
                    'connected_users': connected_users,
                    'users_online': users_online,
                    'cpu_usage': getattr(node, 'cpu_usage', None),
                    'memory_usage': getattr(node, 'memory_usage', None),
                    'network_upload': getattr(node, 'network_upload', None),
                    'network_download': getattr(node, 'network_download', None),
                    'uptime': getattr(node, 'uptime', None),
                    'last_seen': getattr(node, 'last_seen', None),
                    'is_online': bool(users_online) if users_online is not None else None,
                    'upload': getattr(node, 'upload', None),
                    'download': getattr(node, 'download', None),
                    'metrics_inbound_upload_bytes': in_up,
                    'metrics_inbound_download_bytes': in_dn,
                    'metrics_outbound_upload_bytes': out_up,
                    'metrics_outbound_download_bytes': out_dn,
                })
                logger.debug(
                    f'[REMNAWAVE] Метрика ноды: UUID={node_uuid}, users_online={users_online}, '
                    f'inbound Σ↑{in_up:.0f} ↓{in_dn:.0f} B'
                )

            return {'nodes': nodes_data}

        except ApiError as e:
            logger.error(f'[REMNAWAVE] Ошибка API при получении метрик нод: код={e.code}, сообщение={e.message}')
            return None
        except Exception as e:
            logger.error(f'[REMNAWAVE] Ошибка при получении метрик нод: {type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()
            return None

    async def get_nodes_realtime_speed_by_uuid(self) -> Dict[str, Dict[str, float]]:
        """Текущая скорость трафика по нодам: GET /bandwidth-stats/nodes/realtime.

        Дополняет ``get_nodes_metrics`` (там — накопительные счётчики; здесь — мгновенная скорость,
        если роут включён на панели).

        Returns:
            ``{ normalized_uuid: {"network_upload": float, "network_download": float} }``
            значения в **байтах/сек** (как ожидает веб-админка: ``* 8`` → bps).
        """
        await self._ensure_initialized()
        try:
            resp = await self._sdk.bandwidthstats.get_nodes_realtime_usage()
            if not resp:
                return {}
            items = getattr(resp, 'root', None)
            if items is None:
                try:
                    items = list(resp)
                except TypeError:
                    items = []
            out: Dict[str, Dict[str, float]] = {}
            skipped_uid = 0
            for item in items:
                d = _realtime_bandwidth_row_as_dict(item)
                uid_raw = d.get('nodeUuid') or d.get('node_uuid')
                uid = normalize_remnawave_node_uuid(uid_raw)
                if not uid:
                    skipped_uid += 1
                    continue
                up_bps = _coalesce_float(d, 'uploadSpeedBps', 'upload_speed_bps')
                dn_bps = _coalesce_float(d, 'downloadSpeedBps', 'download_speed_bps')
                total_bps = _coalesce_float(d, 'totalSpeedBps', 'total_speed_bps')
                # Некоторые сборки панели заполняют только aggregate speed
                if up_bps <= 0 and dn_bps <= 0 and total_bps > 0:
                    half = total_bps / 2.0
                    up_bps = half
                    dn_bps = half
                out[uid] = {
                    'network_upload': up_bps / 8.0,
                    'network_download': dn_bps / 8.0,
                }
            if items and not out:
                logger.warning(
                    f'[REMNAWAVE] realtime bandwidth: получено {len(items)} строк, '
                    f'но ни одна не сопоставилась по UUID (skipped_uid={skipped_uid}). '
                    f'Проверьте формат nodeUuid в ответе панели.'
                )
            elif out:
                logger.debug(f'[REMNAWAVE] realtime bandwidth: {len(out)} нод')
            return out
        except ApiError as e:
            logger.warning(f'[REMNAWAVE] realtime bandwidth ApiError: код={e.code}, {e.message}')
            return {}
        except Exception as e:
            logger.warning(f'[REMNAWAVE] realtime bandwidth: {type(e).__name__}: {e}')
            return {}
    
    async def get_online_telegram_ids(
        self,
        window_seconds: int = 180,
        page_size: int = 500,
        max_pages: int = 1000,
    ) -> set:
        """Множество telegram_id клиентов, онлайн «сейчас».

        Листает /api/users постранично и берёт тех, у кого ``onlineAt`` свежее
        ``window_seconds`` и есть ``telegram_id``. Используется фоновой задачей
        веб-админки (раз в ~10 мин) для бейджа «Онлайн» в таблице клиентов.
        """
        await self._ensure_initialized()

        def _parse_iso(v):
            if not v:
                return None
            try:
                return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return None

        online: set = set()
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(seconds=max(30, int(window_seconds)))
        start = 0
        pages = 0
        try:
            while pages < max_pages:
                # Сырой GET /users — устойчиво к изменениям SDK-модели (userTraffic и т.п.)
                resp = await self._sdk.users.client.get(
                    '/users', params={'start': start, 'size': page_size}
                )
                resp.raise_for_status()
                data = resp.json()
                body = data.get('response', data) if isinstance(data, dict) else data
                if isinstance(body, dict):
                    users = body.get('users') or []
                elif isinstance(body, list):
                    users = body
                else:
                    users = []
                if not users:
                    break
                for u in users:
                    if not isinstance(u, dict):
                        continue
                    tg = u.get('telegramId', u.get('telegram_id'))
                    if tg is None:
                        continue
                    ut = u.get('userTraffic') if isinstance(u.get('userTraffic'), dict) else {}
                    oa = _parse_iso(ut.get('onlineAt') or u.get('onlineAt'))
                    if oa is None:
                        continue
                    if oa.tzinfo is None:
                        oa = oa.replace(tzinfo=timezone.utc)
                    if oa >= threshold:
                        try:
                            online.add(int(tg))
                        except (TypeError, ValueError):
                            continue
                if len(users) < page_size:
                    break
                start += page_size
                pages += 1
            logger.info(f"[REMNAWAVE] Онлайн-опрос: {len(online)} онлайн (страниц={pages + 1})")
        except Exception as e:
            logger.warning(f"[REMNAWAVE] get_online_telegram_ids: {type(e).__name__}: {e}")
        return online

    async def health_ping(self, timeout_seconds: float = 6.0) -> bool:
        """Лёгкая проверка доступности панели Remnawave (GET /system/health).

        Возвращает True, если панель ответила 2xx за timeout. Любая ошибка/таймаут → False.
        Используется фоновой проверкой здоровья в веб-админке.
        """
        import asyncio as _aio
        try:
            await self._ensure_initialized()
            resp = await _aio.wait_for(
                self._sdk.users.client.get('/system/health'),
                timeout=float(timeout_seconds),
            )
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.debug(f"[REMNAWAVE] health_ping fail: {type(e).__name__}: {e}")
            return False

    async def get_user_card(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Карточка Remnawave-пользователя по telegram_id.

        Возвращает общую инфу: id, статус, лимит/использовано трафика, онлайн (last seen),
        срок действия. Сырой запрос — устойчив к изменениям SDK-модели.

        С панели 3.4 ручка `/users/by-telegram-id` удалена, замена — `/users/stream`
        с фильтром и курсорной постраничностью. Нам нужна первая же запись, поэтому
        курсор не листаем: у одного telegram_id учётка одна.

        Ключ учётки там же сменился: `uuid` больше не отдаётся, вместо него
        числовой `id`. Читаем оба — на случай, если панель окажется старой.
        """
        await self._ensure_initialized()
        try:
            resp = await self._sdk.users.client.get(
                '/users/stream',
                params={'telegramId': int(telegram_id), 'limit': 1},
            )
            resp.raise_for_status()
            data = resp.json()
            body = data.get('response', data) if isinstance(data, dict) else data
            if isinstance(body, dict):
                users = body.get('users') or body.get('items') or body.get('data')
            else:
                users = body
            if not users:
                return None
            u = users[0] if isinstance(users, list) else users
            ut = u.get('userTraffic') or {}
            return {
                'id': u.get('id'),
                # Прежнее имя оставлено ради вызывающих: их много, и менять
                # их все разом — отдельная работа. Значение теперь числовое.
                'uuid': u.get('id') if u.get('uuid') is None else u.get('uuid'),
                'username': u.get('username'),
                'status': u.get('status'),
                'traffic_limit_bytes': int(u.get('trafficLimitBytes') or 0),
                'used_traffic_bytes': int(ut.get('usedTrafficBytes') or u.get('usedTrafficBytes') or 0),
                'lifetime_used_traffic_bytes': int(ut.get('lifetimeUsedTrafficBytes') or 0),
                'online_at': ut.get('onlineAt') or u.get('onlineAt'),
                'expire_at': u.get('expireAt'),
                'sub_last_opened_at': u.get('subLastOpenedAt'),
            }
        except Exception as e:
            logger.warning(f"[REMNAWAVE] get_user_card({telegram_id}): {type(e).__name__}: {e}")
            return None

    async def get_user_node_usage(self, user_uuid: str, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
        """Трафик пользователя по нодам за период.

        GET /bandwidth-stats/users/{uuid} → series/topNodes (уже по ноде).
        Fallback: /legacy возвращает строки по дням — суммируем по node_uuid.
        """
        await self._ensure_initialized()
        out: List[Dict[str, Any]] = []
        try:
            resp = await self._sdk.users.client.get(
                f'/bandwidth-stats/users/{user_uuid}',
                params={
                    'start': start_iso,
                    'end': end_iso,
                    'topNodesLimit': 1000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            body = data.get('response', data) if isinstance(data, dict) else data
            if isinstance(body, dict):
                agg: Dict[str, Dict[str, Any]] = {}
                items = body.get('series') or body.get('topNodes') or body.get('top_nodes') or []
                if isinstance(items, list):
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        node_id = normalize_remnawave_node_uuid(
                            it.get('uuid') or it.get('nodeUuid') or it.get('node_uuid')
                        )
                        if not node_id:
                            continue
                        if node_id not in agg:
                            agg[node_id] = {
                                'node_uuid': node_id,
                                'node_name': it.get('name') or it.get('nodeName') or it.get('node_name') or '—',
                                'total': 0,
                            }
                        agg[node_id]['total'] += int(it.get('total') or 0)
                out = list(agg.values())
        except Exception as e:
            logger.warning(f"[REMNAWAVE] get_user_node_usage stats({user_uuid}): {type(e).__name__}: {e}")

        if out:
            out.sort(key=lambda n: n.get('total', 0), reverse=True)
            return out

        # Legacy: одна строка на ноду на каждый день — обязательно агрегируем.
        try:
            resp = await self._sdk.users.client.get(
                f'/bandwidth-stats/users/{user_uuid}/legacy',
                params={'start': start_iso, 'end': end_iso},
            )
            resp.raise_for_status()
            data = resp.json()
            body = data.get('response', data) if isinstance(data, dict) else data
            agg_legacy: Dict[str, Dict[str, Any]] = {}
            if isinstance(body, list):
                for it in body:
                    if not isinstance(it, dict):
                        continue
                    node_id = normalize_remnawave_node_uuid(
                        it.get('nodeUuid') or it.get('node_uuid')
                    )
                    if not node_id:
                        continue
                    if node_id not in agg_legacy:
                        agg_legacy[node_id] = {
                            'node_uuid': node_id,
                            'node_name': it.get('nodeName') or it.get('node_name') or '—',
                            'total': 0,
                        }
                    agg_legacy[node_id]['total'] += int(it.get('total') or 0)
            out = list(agg_legacy.values())
            out.sort(key=lambda n: n.get('total', 0), reverse=True)
        except Exception as e:
            logger.warning(f"[REMNAWAVE] get_user_node_usage legacy({user_uuid}): {type(e).__name__}: {e}")
        return out

    async def fetch_user_ips(self, user_uuid: str, timeout_seconds: float = 12.0) -> Optional[List[Dict[str, Any]]]:
        """IP пользователя по нодам через ip-control (джоба + поллинг результата).

        Возвращает [{node_name, ips:[...]}] или None (ошибка / не успели за timeout).
        """
        await self._ensure_initialized()
        import asyncio as _aio
        try:
            resp = await self._sdk.users.client.post(f'/ip-control/fetch-ips/{user_uuid}')
            resp.raise_for_status()
            data = resp.json()
            body = data.get('response', data) if isinstance(data, dict) else data
            job_id = body.get('jobId') if isinstance(body, dict) else None
            if not job_id:
                return None
            loop = _aio.get_event_loop()
            deadline = loop.time() + float(timeout_seconds)
            while loop.time() < deadline:
                await _aio.sleep(1.0)
                r2 = await self._sdk.users.client.get(f'/ip-control/fetch-ips/result/{job_id}')
                r2.raise_for_status()
                d2 = r2.json()
                b2 = d2.get('response', d2) if isinstance(d2, dict) else d2
                if isinstance(b2, dict) and b2.get('isCompleted'):
                    result = b2.get('result') or {}
                    nodes = result.get('nodes') or []

                    def _norm_ip(x):
                        if isinstance(x, dict):
                            return {
                                'ip': x.get('ip') or x.get('address') or x.get('ipAddress') or '',
                                'last_seen': (x.get('lastSeen') or x.get('last_seen')
                                              or x.get('time') or x.get('timestamp')),
                            }
                        return {'ip': str(x), 'last_seen': None}

                    out = []
                    for n in nodes:
                        if not isinstance(n, dict):
                            continue
                        raw_ips = n.get('ips') or []
                        out.append({
                            'node_name': n.get('nodeName') or '—',
                            'ips': [_norm_ip(x) for x in raw_ips if x is not None],
                        })
                    return out
            return None
        except Exception as e:
            logger.warning(f"[REMNAWAVE] fetch_user_ips({user_uuid}): {type(e).__name__}: {e}")
            return None

    async def get_system_stats(self) -> Optional[Dict[str, Any]]:
        """
        Получает общую статистику системы, включая общий онлайн
        
        Returns:
            Dict со статистикой системы или None при ошибке
            Формат: {
                "online_now": int,  # Общий онлайн пользователей
                "total_online_nodes": int,  # Количество онлайн нод
                "total_users": int,
                "cpu": {...},
                "memory": {...},
                "uptime": float,
                ...
            }
        """
        await self._ensure_initialized()
        
        try:
            stats_response = await self._sdk.system.get_stats()
            
            # SDK 2.7.0 возвращает числовые поля как float — приводим к int
            return {
                "online_now": int(stats_response.online_stats.online_now or 0),
                "total_online_nodes": int(stats_response.nodes.total_online or 0),
                "total_users": int(stats_response.users.total_users or 0),
                "cpu": {
                    "cores": stats_response.cpu.cores,
                    "physical_cores": stats_response.cpu.physical_cores,
                },
                "memory": {
                    "total": stats_response.memory.total,
                    "free": stats_response.memory.free,
                    "used": stats_response.memory.used,
                    "active": stats_response.memory.active,
                    "available": stats_response.memory.available,
                },
                "uptime": stats_response.uptime,
                "timestamp": stats_response.timestamp,
                "online_stats": {
                    "online_now": int(stats_response.online_stats.online_now or 0),
                    "last_day": int(stats_response.online_stats.last_day or 0),
                    "last_week": int(stats_response.online_stats.last_week or 0),
                    "never_online": int(stats_response.online_stats.never_online or 0),
                },
                "nodes": {
                    "total_online": int(stats_response.nodes.total_online or 0),
                    "total_bytes_lifetime": stats_response.nodes.total_bytes_lifetime,
                },
                "users": {
                    "total_users": int(stats_response.users.total_users or 0),
                    "status_counts": dict(stats_response.users.status_counts.model_dump()),
                },
            }
            
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при получении статистики системы: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении статистики системы: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _map_raw_node(self, n: dict) -> Dict[str, Any]:
        """Маппит сырую ноду из /api/nodes (2.7.x) в плоский dict для дашборда.

        CPU/RAM/сеть/uptime в 2.7.x лежат во вложенном блоке ``system`` (info+stats),
        а версии xray/node — в ``versions``. SDK-модель их не парсит, поэтому берём из сырого JSON.
        """
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _ram_str(b):
            bf = _f(b)
            if not bf or bf <= 0:
                return None
            gb = bf / (1024 ** 3)
            if gb >= 1:
                return f"{gb:.1f} GB"
            return f"{bf / (1024 ** 2):.0f} MB"

        system = n.get('system') or {}
        info = system.get('info') or {}
        stats = system.get('stats') or {}
        versions = n.get('versions') or {}

        cpus = info.get('cpus')
        load_avg = stats.get('loadAvg') or []
        iface = stats.get('interface') or {}

        # CPU % ≈ loadAvg[0] / cpus * 100
        cpu_usage = None
        if load_avg and cpus:
            la, c = _f(load_avg[0]), _f(cpus)
            if la is not None and c and c > 0:
                cpu_usage = round(min(100.0, la / c * 100.0), 1)
        # RAM %
        memory_usage = None
        mt, mu = _f(info.get('memoryTotal')), _f(stats.get('memoryUsed'))
        if mt and mt > 0 and mu is not None:
            memory_usage = round(mu / mt * 100.0, 1)

        try:
            users_online = int(n.get('usersOnline') or 0)
        except (TypeError, ValueError):
            users_online = 0

        return {
            "uuid": str(n.get('uuid') or ''),
            "name": n.get('name'),
            "address": n.get('address'),
            "port": n.get('port'),
            "is_connected": n.get('isConnected'),
            "is_disabled": n.get('isDisabled'),
            "is_connecting": n.get('isConnecting'),
            "last_status_change": n.get('lastStatusChange'),
            "last_status_message": n.get('lastStatusMessage'),
            "xray_version": versions.get('xray') or n.get('xrayVersion'),
            "node_version": versions.get('node') or n.get('nodeVersion'),
            "xray_uptime": n.get('xrayUptime'),
            "is_traffic_tracking_active": n.get('isTrafficTrackingActive'),
            "traffic_reset_day": n.get('trafficResetDay'),
            "traffic_limit_bytes": n.get('trafficLimitBytes'),
            "traffic_used_bytes": n.get('trafficUsedBytes'),
            "notify_percent": n.get('notifyPercent'),
            "users_online": users_online,
            "connected_users": users_online,
            "view_position": n.get('viewPosition'),
            "country_code": n.get('countryCode'),
            "consumption_multiplier": n.get('consumptionMultiplier'),
            "cpu_count": cpus,
            "cpu_model": info.get('cpuModel'),
            "total_ram": _ram_str(info.get('memoryTotal')),
            "created_at": n.get('createdAt'),
            "updated_at": n.get('updatedAt'),
            # системные метрики из /nodes (2.7.x): CPU%, RAM%, сеть (байт/сек), uptime (сек)
            "cpu_usage": cpu_usage,
            "memory_usage": memory_usage,
            "network_upload": _f(iface.get('txBytesPerSec')) or 0.0,
            "network_download": _f(iface.get('rxBytesPerSec')) or 0.0,
            "uptime": stats.get('uptime'),
        }

    async def get_internal_squads(self) -> Optional[List[Dict[str, Any]]]:
        """Список internal squads из Remnawave."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.internal_squads.get_internal_squads()
            squads = getattr(resp, 'internal_squads', None) or []
            return [{'uuid': str(sq.uuid), 'name': sq.name} for sq in squads]
        except Exception as e:
            logger.warning(f"[REMNAWAVE] get_internal_squads SDK: {type(e).__name__}: {e}")
            try:
                resp = await self._sdk.internal_squads.client.get('/internal-squads')
                resp.raise_for_status()
                data = resp.json()
                body = data.get('response', data) if isinstance(data, dict) else data
                raw = (body.get('internalSquads') or body.get('internal_squads') or []) if isinstance(body, dict) else []
                return [
                    {'uuid': str(s.get('uuid')), 'name': s.get('name') or '—'}
                    for s in raw if isinstance(s, dict) and s.get('uuid')
                ]
            except Exception as e2:
                logger.warning(f"[REMNAWAVE] get_internal_squads fallback: {type(e2).__name__}: {e2}")
                return None

    async def get_all_nodes(self) -> Optional[List[Dict[str, Any]]]:
        """Список нод.

        Сначала пробуем СЫРОЙ GET /nodes через настроенный клиент SDK — там есть блок
        ``system`` (CPU/RAM/сеть/uptime) и ``versions``, которых нет в SDK-модели 2.7.x.
        При любой ошибке — fallback на парсинг через SDK-модель (без системных метрик).
        """
        await self._ensure_initialized()
        try:
            resp = await self._sdk.nodes.client.get('/nodes')
            resp.raise_for_status()
            data = resp.json()
            raw_nodes = data.get('response', data) if isinstance(data, dict) else data
            if not isinstance(raw_nodes, list):
                raise ValueError("неожиданный формат ответа /nodes")
            return [self._map_raw_node(n) for n in raw_nodes if isinstance(n, dict)]
        except Exception as e:
            logger.warning(
                f"[REMNAWAVE] Сырой GET /nodes недоступен ({type(e).__name__}: {e}); "
                f"fallback на SDK-модель (без CPU/RAM/сети)"
            )
            return await self._get_all_nodes_via_sdk()

    async def _get_all_nodes_via_sdk(self) -> Optional[List[Dict[str, Any]]]:
        """
        Получает список всех нод с их информацией
        
        Returns:
            List с данными нод или None при ошибке
            Формат: [
                {
                    "uuid": str,
                    "name": str,
                    "address": str,
                    "port": int,
                    "is_connected": bool,
                    "is_disabled": bool,
                    "users_online": int,
                    ...
                },
                ...
            ]
        """
        await self._ensure_initialized()
        
        try:
            nodes_response = await self._sdk.nodes.get_all_nodes()
            
            nodes_data = []
            for node in nodes_response.root:
                nodes_data.append({
                    "uuid": str(node.uuid),
                    "name": node.name,
                    "address": node.address,
                    "port": node.port,
                    "is_connected": node.is_connected,
                    "is_disabled": node.is_disabled,
                    "is_connecting": node.is_connecting,
                    "last_status_change": node.last_status_change.isoformat() if node.last_status_change else None,
                    "last_status_message": node.last_status_message,
                    "xray_version": node.xray_version,
                    "node_version": node.node_version,
                    "xray_uptime": node.xray_uptime,
                    "is_traffic_tracking_active": node.is_traffic_tracking_active,
                    "traffic_reset_day": node.traffic_reset_day,
                    "traffic_limit_bytes": node.traffic_limit_bytes,
                    "traffic_used_bytes": node.traffic_used_bytes,
                    "notify_percent": node.notify_percent,
                    "users_online": node.users_online,
                    "view_position": node.view_position,
                    "country_code": node.country_code,
                    "consumption_multiplier": node.consumption_multiplier,
                    "cpu_count": node.cpu_count,
                    "cpu_model": node.cpu_model,
                    "total_ram": node.total_ram,
                    "created_at": node.created_at.isoformat() if node.created_at else None,
                    "updated_at": node.updated_at.isoformat() if node.updated_at else None,
                })
            
            return nodes_data
            
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API при получении списка нод: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка при получении списка нод: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def get_subscription_info(self, short_uuid: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о подписке по short_uuid через SDK.
        Возвращает dict с ключами: traffic_limit_bytes, traffic_used_bytes, days_left, is_active, user_status.
        """
        await self._ensure_initialized()
        try:
            response = await self._sdk.subscription.get_subscription_info_by_short_uuid(short_uuid)
            if not response or not response.user:
                return None
            u = response.user
            # SDK 2.7.0: traffic_*_bytes в UserSubscription возвращаются как str ("1500000000")
            def _to_int(val):
                try:
                    return int(float(val or 0))
                except (TypeError, ValueError):
                    return 0
            return {
                'trafficLimitBytes': _to_int(u.traffic_limit_bytes),
                'trafficUsedBytes': _to_int(u.traffic_used_bytes),
                'daysLeft': u.days_left,
                'isActive': u.is_active,
                'userStatus': str(u.user_status),
            }
        except ApiError as e:
            logger.error(f"[REMNAWAVE] Ошибка API get_subscription_info: код={e.code}, сообщение={e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] Ошибка get_subscription_info ({short_uuid}): {type(e).__name__}: {e}")
            return None

    # ---- Действия с нодами ---------------------------------------------------
    # Тонкие обёртки над SDK для админки. Возвращают (ok: bool, error: str|None),
    # чтобы веб-роуты могли единообразно их обрабатывать без знания SDK-моделей.

    async def restart_node(self, uuid: str, force_restart: bool = True) -> tuple[bool, Optional[str]]:
        """Перезапускает Xray на конкретной ноде.

        Идём сырым POST /nodes/{uuid}/actions/restart. Новые версии панели требуют
        в теле поле forceRestart (boolean), иначе — 400 "forceRestart Required".
        SDK (rapid_api_client) для эндпоинтов без body-параметра тело не отправлял.
        При ошибке логируем тело ответа — видно реальную причину.
        """
        await self._ensure_initialized()
        node_uuid = str(uuid).strip()
        try:
            resp = await self._sdk.nodes.client.post(
                f'/nodes/{node_uuid}/actions/restart',
                json={'forceRestart': bool(force_restart)},
            )
            if resp.status_code // 100 == 2:
                logger.info(f"[REMNAWAVE] Нода {node_uuid}: запрошен restart")
                return True, None
            body = ''
            try:
                body = (resp.text or '')[:500]
            except Exception:
                pass
            logger.error(
                f"[REMNAWAVE] restart_node({node_uuid}) HTTP {resp.status_code}: {body}"
            )
            return False, f"HTTP {resp.status_code}: {body}"
        except ApiError as e:
            logger.error(f"[REMNAWAVE] restart_node({node_uuid}) ApiError: код={e.code}, {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] restart_node({node_uuid}) error: {type(e).__name__}: {e}")
            return False, str(e)

    async def enable_node(self, uuid: str) -> tuple[bool, Optional[str]]:
        """Включает ноду (выводит из disabled)."""
        await self._ensure_initialized()
        try:
            await self._sdk.nodes.enable_node(uuid)
            logger.info(f"[REMNAWAVE] Нода {uuid}: enable")
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] enable_node({uuid}) ApiError: код={e.code}, {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] enable_node({uuid}) error: {type(e).__name__}: {e}")
            return False, str(e)

    async def disable_node(self, uuid: str) -> tuple[bool, Optional[str]]:
        """Выключает ноду (переводит в disabled)."""
        await self._ensure_initialized()
        try:
            await self._sdk.nodes.disable_node(uuid)
            logger.info(f"[REMNAWAVE] Нода {uuid}: disable")
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] disable_node({uuid}) ApiError: код={e.code}, {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] disable_node({uuid}) error: {type(e).__name__}: {e}")
            return False, str(e)

    async def get_node_by_uuid(self, uuid: str) -> Optional[Dict[str, Any]]:
        """Одна нода по UUID (для редактирования имени)."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.nodes.get_one_node(uuid)
            node = getattr(resp, 'root', resp)
            return {
                'uuid': str(getattr(node, 'uuid', uuid)),
                'name': getattr(node, 'name', None),
            }
        except ApiError as e:
            logger.error(f"[REMNAWAVE] get_node_by_uuid({uuid}) ApiError: {e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] get_node_by_uuid({uuid}) error: {type(e).__name__}: {e}")
            return None

    async def update_node_name(self, uuid: str, new_name: str) -> tuple[bool, Optional[str]]:
        """Меняет внутреннее имя ноды (PATCH /nodes)."""
        await self._ensure_initialized()
        try:
            from remnawave.models import UpdateNodeRequestDto

            await self._sdk.nodes.update_node(
                UpdateNodeRequestDto(uuid=UUID(str(uuid)), name=new_name)
            )
            logger.info(f"[REMNAWAVE] Нода {uuid}: имя -> {new_name!r}")
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] update_node_name({uuid}) ApiError: код={e.code}, {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] update_node_name({uuid}) error: {type(e).__name__}: {e}")
            return False, str(e)

    async def restart_all_nodes(self, force_restart: bool = False) -> tuple[bool, Optional[str]]:
        """Перезапускает Xray на всех нодах сразу."""
        await self._ensure_initialized()
        try:
            from remnawave.models import RestartAllNodesRequestBodyDto
            body = RestartAllNodesRequestBodyDto(force_restart=force_restart)
            await self._sdk.nodes.restart_all_nodes(body)
            logger.info(f"[REMNAWAVE] restart-all (force={force_restart})")
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] restart_all_nodes ApiError: код={e.code}, {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] restart_all_nodes error: {type(e).__name__}: {e}")
            return False, str(e)

    async def get_config_profiles_list(self) -> Optional[List[Dict[str, Any]]]:
        """Список config profiles с inbounds для UI установщика нод."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.config_profiles.get_config_profiles()
            profiles = getattr(resp, 'config_profiles', None) or []
            out = []
            for p in profiles:
                inbounds = []
                for ib in (getattr(p, 'inbounds', None) or []):
                    inbounds.append({
                        'uuid': str(getattr(ib, 'uuid', '')),
                        'tag': getattr(ib, 'tag', ''),
                        'type': getattr(ib, 'type', ''),
                    })
                out.append({
                    'uuid': str(getattr(p, 'uuid', '')),
                    'name': getattr(p, 'name', ''),
                    'inbounds': inbounds,
                })
            return out
        except ApiError as e:
            logger.error(f"[REMNAWAVE] get_config_profiles_list ApiError: {e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] get_config_profiles_list: {type(e).__name__}: {e}")
            return None

    def _config_profile_to_dict(self, p) -> Dict[str, Any]:
        def _dt(v):
            if v is None:
                return None
            if hasattr(v, 'isoformat'):
                return v.isoformat()
            return str(v)

        inbounds = []
        for ib in (getattr(p, 'inbounds', None) or []):
            inbounds.append({
                'uuid': str(getattr(ib, 'uuid', '')),
                'tag': getattr(ib, 'tag', ''),
                'type': getattr(ib, 'type', ''),
                'network': getattr(ib, 'network', None),
                'security': getattr(ib, 'security', None),
                'port': getattr(ib, 'port', None),
            })
        nodes = []
        for n in (getattr(p, 'nodes', None) or []):
            nodes.append({
                'uuid': str(getattr(n, 'uuid', '')),
                'name': getattr(n, 'name', ''),
                'country_code': getattr(n, 'country_code', None) or getattr(n, 'countryCode', None),
            })
        return {
            'uuid': str(getattr(p, 'uuid', '')),
            'name': getattr(p, 'name', ''),
            'view_position': int(getattr(p, 'view_position', 0) or 0),
            'config': getattr(p, 'config', None) or {},
            'inbounds_meta': inbounds,
            'nodes': nodes,
            'created_at': _dt(getattr(p, 'created_at', None)),
            'updated_at': _dt(getattr(p, 'updated_at', None)),
        }

    async def get_config_profiles_full(self) -> Optional[List[Dict[str, Any]]]:
        """Полный список config profiles для страницы «Профили»."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.config_profiles.get_config_profiles()
            profiles = getattr(resp, 'config_profiles', None) or []
            return [self._config_profile_to_dict(p) for p in profiles]
        except ApiError as e:
            logger.error(f"[REMNAWAVE] get_config_profiles_full ApiError: {e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] get_config_profiles_full: {type(e).__name__}: {e}")
            return None

    async def get_config_profile(self, profile_uuid: str) -> Optional[Dict[str, Any]]:
        await self._ensure_initialized()
        try:
            resp = await self._sdk.config_profiles.get_config_profile_by_uuid(str(profile_uuid))
            return self._config_profile_to_dict(resp)
        except ApiError as e:
            logger.error(f"[REMNAWAVE] get_config_profile({profile_uuid}) ApiError: {e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] get_config_profile: {type(e).__name__}: {e}")
            return None

    async def create_config_profile(self, name: str, config: dict) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        await self._ensure_initialized()
        try:
            body = CreateConfigProfileRequestDto(name=name.strip(), config=config or {})
            resp = await self._sdk.config_profiles.create_config_profile(body)
            return self._config_profile_to_dict(resp), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] create_config_profile ApiError: {e.message}")
            return None, e.message
        except Exception as e:
            logger.error(f"[REMNAWAVE] create_config_profile: {type(e).__name__}: {e}")
            return None, str(e)

    async def update_config_profile(
        self,
        profile_uuid: str,
        name: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        await self._ensure_initialized()
        try:
            body = UpdateConfigProfileRequestDto(
                uuid=UUID(str(profile_uuid)),
                name=name.strip() if name else None,
                config=config,
            )
            resp = await self._sdk.config_profiles.update_config_profile(body)
            return self._config_profile_to_dict(resp), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] update_config_profile ApiError: {e.message}")
            return None, e.message
        except Exception as e:
            logger.error(f"[REMNAWAVE] update_config_profile: {type(e).__name__}: {e}")
            return None, str(e)

    async def delete_config_profile(self, profile_uuid: str) -> tuple[bool, Optional[str]]:
        await self._ensure_initialized()
        try:
            await self._sdk.config_profiles.delete_config_profile_by_uuid(str(profile_uuid))
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] delete_config_profile ApiError: {e.message}")
            return False, e.message
        except Exception as e:
            logger.error(f"[REMNAWAVE] delete_config_profile: {type(e).__name__}: {e}")
            return False, str(e)

    async def generate_x25519_keypair(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Пара ключей Reality (privateKey / publicKey) через Remnawave API."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.system.get_x25519_key_pair()
            pairs = getattr(resp, 'key_pairs', None) or []
            if not pairs:
                return None, None, 'Пустой ответ x25519'
            pair = pairs[0]
            priv = getattr(pair, 'private_key', None) or getattr(pair, 'privateKey', None)
            pub = getattr(pair, 'public_key', None) or getattr(pair, 'publicKey', None)
            if not priv or not pub:
                return None, None, 'Некорректный ответ x25519'
            return str(priv), str(pub), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] generate_x25519_keypair ApiError: {e.message}")
            return None, None, e.message
        except Exception as e:
            logger.error(f"[REMNAWAVE] generate_x25519_keypair: {type(e).__name__}: {e}")
            return None, None, str(e)

    async def generate_node_secret_key(self) -> tuple[Optional[str], Optional[str]]:
        """GET /keygen → pubKey (SECRET_KEY для docker-compose)."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk._client.get('/keygen')
            resp.raise_for_status()
            data = resp.json()
            wrapped = data.get('response', data) if isinstance(data, dict) else data
            if isinstance(wrapped, dict):
                pub = wrapped.get('pubKey') or wrapped.get('pub_key')
            else:
                pub = getattr(wrapped, 'pub_key', None) or getattr(wrapped, 'pubKey', None)
            if not pub:
                return None, 'Пустой ответ /keygen'
            return str(pub), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] keygen ApiError: {e.message}")
            return None, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] keygen error: {type(e).__name__}: {e}")
            return None, str(e)

    async def create_node(
        self,
        *,
        name: str,
        address: str,
        port: int,
        country_code: str,
        config_profile_uuid: str,
        inbound_uuids: List[str],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """POST /nodes — регистрация ноды на панели."""
        await self._ensure_initialized()
        if not inbound_uuids:
            return None, 'Выберите хотя бы один inbound профиля'
        try:
            from remnawave.models import CreateNodeRequestDto, NodeConfigProfileRequestDto

            config_profile = NodeConfigProfileRequestDto.model_validate({
                'activeConfigProfileUuid': str(config_profile_uuid),
                'activeInbounds': [str(u) for u in inbound_uuids],
            })
            body = CreateNodeRequestDto(
                name=name,
                address=address,
                port=int(port),
                country_code=country_code or 'XX',
                config_profile=config_profile,
            )
            resp = await self._sdk.nodes.create_node(body)
            node = getattr(resp, 'root', resp)
            return {
                'uuid': str(getattr(node, 'uuid', '')),
                'name': getattr(node, 'name', name),
                'address': getattr(node, 'address', address),
                'port': getattr(node, 'port', port),
                'is_connected': getattr(node, 'is_connected', False),
            }, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] create_node ApiError: код={e.code}, {e.message}")
            return None, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] create_node error: {type(e).__name__}: {e}")
            return None, str(e)

    async def get_node_connection_status(self, uuid: str) -> Optional[Dict[str, Any]]:
        """Статус подключения ноды к панели."""
        await self._ensure_initialized()
        try:
            resp = await self._sdk.nodes.get_one_node(uuid)
            node = getattr(resp, 'root', resp)
            return {
                'uuid': str(getattr(node, 'uuid', uuid)),
                'is_connected': getattr(node, 'is_connected', False),
                'is_connecting': getattr(node, 'is_connecting', False),
                'last_status_message': getattr(node, 'last_status_message', None),
            }
        except Exception as e:
            logger.debug(f"[REMNAWAVE] get_node_connection_status({uuid}): {e}")
            return None

    # ---- Хосты (балансировка текстовых ключей) -------------------------------

    def _normalize_host_dict(self, raw: dict) -> Dict[str, Any]:
        """Нормализует сырой JSON хоста (camelCase API → единый dict для админки)."""
        inbound_raw = raw.get('inbound') or {}
        inbound: Dict[str, Any] = {}
        if isinstance(inbound_raw, dict):
            inbound = {
                'configProfileUuid': inbound_raw.get('configProfileUuid') or inbound_raw.get('config_profile_uuid'),
                'configProfileInboundUuid': inbound_raw.get('configProfileInboundUuid') or inbound_raw.get('config_profile_inbound_uuid'),
                'config_profile_uuid': inbound_raw.get('configProfileUuid') or inbound_raw.get('config_profile_uuid'),
                'config_profile_inbound_uuid': inbound_raw.get('configProfileInboundUuid') or inbound_raw.get('config_profile_inbound_uuid'),
            }
        nodes = [str(x) for x in (raw.get('nodes') or []) if x is not None]
        excl = raw.get('excludedInternalSquads') or raw.get('excluded_internal_squads') or []
        out: Dict[str, Any] = {
            'uuid': str(raw.get('uuid') or ''),
            'remark': raw.get('remark') or '',
            'address': raw.get('address') or '',
            'port': int(raw.get('port') or 0),
            'path': raw.get('path'),
            'sni': raw.get('sni'),
            'host': raw.get('host'),
            'tag': raw.get('tag'),
            'fingerprint': raw.get('fingerprint'),
            'alpn': raw.get('alpn'),
            'nodes': nodes,
            'inbound': inbound,
            'is_disabled': bool(raw.get('isDisabled', raw.get('is_disabled', False))),
            'is_hidden': bool(raw.get('isHidden', raw.get('is_hidden', False))),
            'allow_insecure': bool(raw.get('allowInsecure', raw.get('allow_insecure', False))),
            'shuffle_host': bool(raw.get('shuffleHost', raw.get('shuffle_host', False))),
            'mihomo_x25519': bool(raw.get('mihomoX25519', raw.get('mihomo_x25519', False))),
            'override_sni_from_address': bool(raw.get('overrideSniFromAddress', raw.get('override_sni_from_address', False))),
            'keep_blank_sni': bool(raw.get('keepBlankSni', raw.get('keep_blank_sni', False))),
            'server_description': raw.get('serverDescription') or raw.get('server_description'),
            'vless_route_id': raw.get('vlessRouteId') if raw.get('vlessRouteId') is not None else raw.get('vless_route_id'),
            'x_http_extra_params': raw.get('xHttpExtraParams') or raw.get('x_http_extra_params'),
            'mux_params': raw.get('muxParams') or raw.get('mux_params'),
            'sockopt_params': raw.get('sockoptParams') or raw.get('sockopt_params'),
            'excluded_internal_squads': [str(x) for x in excl],
            'excludedInternalSquads': excl,
        }
        for alias, key in (
            ('isDisabled', 'is_disabled'),
            ('isHidden', 'is_hidden'),
            ('allowInsecure', 'allow_insecure'),
            ('shuffleHost', 'shuffle_host'),
            ('mihomoX25519', 'mihomo_x25519'),
            ('overrideSniFromAddress', 'override_sni_from_address'),
            ('keepBlankSni', 'keep_blank_sni'),
            ('serverDescription', 'server_description'),
            ('xHttpExtraParams', 'x_http_extra_params'),
            ('muxParams', 'mux_params'),
            ('sockoptParams', 'sockopt_params'),
        ):
            out[alias] = out[key]
        return out

    def _host_to_dict(self, host: Any) -> Dict[str, Any]:
        if host is None:
            return {}
        if isinstance(host, dict):
            return self._normalize_host_dict(host)
        md = getattr(host, 'model_dump', None)
        if callable(md):
            try:
                return self._normalize_host_dict(md(by_alias=True, mode='json'))
            except TypeError:
                return self._normalize_host_dict(md())
        return self._normalize_host_dict({
            'uuid': getattr(host, 'uuid', ''),
            'remark': getattr(host, 'remark', ''),
            'address': getattr(host, 'address', ''),
            'port': getattr(host, 'port', 0),
            'nodes': getattr(host, 'nodes', None) or [],
            'is_disabled': getattr(host, 'is_disabled', False),
            'is_hidden': getattr(host, 'is_hidden', False),
            'inbound': getattr(host, 'inbound', None) or {},
        })

    async def get_all_hosts(self) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """Список хостов Remnawave. Сначала сырой GET /hosts (как xuiweb), затем SDK."""
        await self._ensure_initialized()
        last_err: Optional[str] = None
        try:
            resp = await self._sdk.hosts.client.get('/hosts')
            resp.raise_for_status()
            data = resp.json()
            raw_hosts = data.get('response', data) if isinstance(data, dict) else data
            if not isinstance(raw_hosts, list):
                raise ValueError(f'неожиданный формат ответа /hosts: {type(raw_hosts).__name__}')
            return [self._normalize_host_dict(h) for h in raw_hosts if isinstance(h, dict)], None
        except ApiError as e:
            last_err = f'API: {e.message}'
            logger.warning(f"[REMNAWAVE] raw GET /hosts ApiError: {e.message}")
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            logger.warning(f"[REMNAWAVE] raw GET /hosts failed ({last_err}); fallback SDK")

        try:
            resp = await self._sdk.hosts.get_all_hosts()
            items = getattr(resp, 'root', resp)
            if not isinstance(items, list):
                items = list(items) if items else []
            return [self._host_to_dict(h) for h in items], None
        except ApiError as e:
            msg = f'API: {e.message}'
            logger.error(f"[REMNAWAVE] get_all_hosts ApiError: {e.message}")
            return None, last_err or msg
        except Exception as e:
            msg = f'{type(e).__name__}: {e}'
            logger.error(f"[REMNAWAVE] get_all_hosts: {msg}")
            return None, last_err or msg

    async def get_host_by_uuid(self, host_uuid: str) -> Optional[Dict[str, Any]]:
        await self._ensure_initialized()
        uid = str(host_uuid).strip()
        try:
            resp = await self._sdk.hosts.client.get(f'/hosts/{uid}')
            resp.raise_for_status()
            data = resp.json()
            raw = data.get('response', data) if isinstance(data, dict) else data
            if isinstance(raw, dict):
                return self._normalize_host_dict(raw)
        except Exception as e:
            logger.warning(f"[REMNAWAVE] raw GET /hosts/{uid}: {type(e).__name__}: {e}")
        try:
            resp = await self._sdk.hosts.get_one_host(uid)
            return self._host_to_dict(getattr(resp, 'root', resp))
        except ApiError as e:
            logger.error(f"[REMNAWAVE] get_host_by_uuid({uid}) ApiError: {e.message}")
            return None
        except Exception as e:
            logger.error(f"[REMNAWAVE] get_host_by_uuid: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _coerce_host_enum(enum_cls, val: Any):
        if val is None:
            return None
        s = str(val).strip()
        if not s:
            return None
        try:
            return enum_cls(s)
        except (ValueError, KeyError):
            pass
        low = s.lower()
        for member in enum_cls:
            if member.value.lower() == low or member.name.lower() == low.replace('/', '_').replace(',', '_'):
                return member
        return None

    async def _create_host_api(self, body_kwargs: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        from remnawave.models import CreateHostRequestDto

        try:
            dto = CreateHostRequestDto(**body_kwargs)
        except Exception as e:
            return None, f'Некорректные данные хоста: {e}'

        payload = dto.model_dump(exclude_none=True, by_alias=True, mode='json')
        last_err: Optional[str] = None
        try:
            resp = await self._sdk.hosts.client.post('/hosts', json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data.get('response', data) if isinstance(data, dict) else data
            if isinstance(raw, dict):
                return self._normalize_host_dict(raw), None
            return self._host_to_dict(raw), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] raw POST /hosts ApiError: {e.message}")
            last_err = f'API: {e.message}'
        except Exception as e:
            err_text = str(e)
            resp = getattr(e, 'response', None)
            if resp is not None:
                try:
                    body = resp.json()
                    err_text = body.get('message') or body.get('error') or err_text
                except Exception:
                    err_text = getattr(resp, 'text', None) or err_text
            logger.warning(f"[REMNAWAVE] raw POST /hosts: {type(e).__name__}: {err_text}")
            last_err = f'API: {err_text}'

        try:
            resp = await self._sdk.hosts.create_host(dto)
            return self._host_to_dict(getattr(resp, 'root', resp)), None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] create_host SDK ApiError: {e.message}")
            return None, f'API: {e.message}'
        except Exception as e:
            logger.error(f"[REMNAWAVE] create_host: {type(e).__name__}: {e}")
            return None, last_err or str(e)

    async def update_host_fields(
        self,
        host_uuid: str,
        *,
        remark: Optional[str] = None,
        address: Optional[str] = None,
        port: Optional[int] = None,
        node_uuid: Optional[str] = None,
        is_disabled: Optional[bool] = None,
        clear_nodes: bool = False,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        await self._ensure_initialized()
        try:
            from remnawave.models import UpdateHostRequestDto

            payload: Dict[str, Any] = {'uuid': UUID(str(host_uuid))}
            if remark is not None:
                payload['remark'] = remark
            if address is not None:
                payload['address'] = address
            if port is not None:
                payload['port'] = int(port)
            if is_disabled is not None:
                payload['is_disabled'] = is_disabled
            if clear_nodes:
                payload['nodes'] = []
            elif node_uuid is not None:
                payload['nodes'] = [UUID(str(node_uuid))]

            dto = UpdateHostRequestDto(**payload)
            try:
                resp = await self._sdk.hosts.update_host(dto)
                return self._host_to_dict(getattr(resp, 'root', resp)), None
            except ApiError:
                raise
            except Exception as parse_err:
                # Апдейт на сервере применяется (HTTP 200), но SDK может не распарсить
                # ответ (UpdateHostResponseDto требует xHttpExtraParams/tag, которых нет
                # в ответе API). Это не реальная ошибка — перечитываем хост сырым GET.
                from pydantic import ValidationError as _PydValErr
                if isinstance(parse_err, _PydValErr):
                    logger.warning(
                        f"[REMNAWAVE] update_host_fields({host_uuid}): апдейт применён, "
                        f"ответ SDK не распарсился — перечитываю хост"
                    )
                    host = await self.get_host_by_uuid(host_uuid)
                    return (host or {'uuid': str(host_uuid)}), None
                raise
        except ApiError as e:
            logger.error(f"[REMNAWAVE] update_host_fields({host_uuid}) ApiError: {e.message}")
            return None, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] update_host_fields: {type(e).__name__}: {e}")
            return None, str(e)

    async def delete_host(self, host_uuid: str) -> tuple[bool, Optional[str]]:
        await self._ensure_initialized()
        try:
            await self._sdk.hosts.delete_host(str(host_uuid))
            logger.info(f"[REMNAWAVE] Host {host_uuid} deleted")
            return True, None
        except ApiError as e:
            logger.error(f"[REMNAWAVE] delete_host({host_uuid}) ApiError: {e.message}")
            return False, f"API: {e.message}"
        except Exception as e:
            logger.error(f"[REMNAWAVE] delete_host: {type(e).__name__}: {e}")
            return False, str(e)

    async def init_balancer_pool(self, host_uuid: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        from link_dedup_grouping import base_name_for_grouping, extract_trailing_index, format_member_remark

        host = await self.get_host_by_uuid(host_uuid)
        if not host:
            return None, 'Хост не найден'
        remark = host.get('remark') or ''
        if extract_trailing_index(remark) is not None:
            return host, None
        base = base_name_for_grouping(remark)
        try:
            new_remark = format_member_remark(base, 1)
        except ValueError as e:
            return None, str(e)
        return await self.update_host_fields(host_uuid, remark=new_remark)

    async def _get_node_address(self, node_uuid: str) -> Optional[str]:
        nodes = await self.get_all_nodes()
        if not nodes:
            return None
        uid = str(node_uuid).lower()
        for n in nodes:
            if str(n.get('uuid') or '').lower() == uid:
                return (n.get('address') or '').strip() or None
        return None

    async def add_balancer_pool_member(
        self,
        pool_uuid: str,
        *,
        mode: str = 'new',
        source_uuid: Optional[str] = None,
        address: Optional[str] = None,
        port: Optional[int] = None,
        node_uuid: Optional[str] = None,
        clear_nodes: bool = False,
        pool_remarks: Optional[List[str]] = None,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Добавить члена пула: новый endpoint (из шаблона пула) или дубликат существующего хоста."""
        from link_dedup_grouping import base_name_for_grouping, format_member_remark, next_pool_index

        mode = (mode or 'new').strip().lower()
        if mode not in ('new', 'duplicate', 'from_node'):
            return None, 'Неверный mode (new | duplicate | from_node)'

        pool_ref = await self.get_host_by_uuid(pool_uuid)
        if not pool_ref:
            return None, 'Хост пула не найден'

        if mode == 'duplicate':
            if not source_uuid:
                return None, 'Укажите source_uuid — хост для дублирования'
            copy_from = await self.get_host_by_uuid(source_uuid)
            if not copy_from:
                return None, 'Исходный хост не найден'
        else:
            copy_from = pool_ref

        addr = (address or '').strip()
        if mode == 'from_node':
            if not node_uuid:
                return None, 'Выберите ноду'
            if not addr:
                node_addr = await self._get_node_address(str(node_uuid))
                addr = (node_addr or '').strip()
        if mode == 'new' and not addr:
            return None, 'Укажите address'
        if mode == 'duplicate' and not addr:
            addr = (copy_from.get('address') or '').strip()
        if not addr:
            return None, 'Укажите address'

        inbound = copy_from.get('inbound') or {}
        cp_uuid = inbound.get('configProfileUuid') or inbound.get('config_profile_uuid')
        in_uuid = inbound.get('configProfileInboundUuid') or inbound.get('config_profile_inbound_uuid')
        if not cp_uuid or not in_uuid:
            return None, 'У хоста не задан inbound config profile'

        base = base_name_for_grouping(pool_ref.get('remark') or '')
        remarks = pool_remarks or [pool_ref.get('remark') or '']
        try:
            new_remark = format_member_remark(base, next_pool_index(remarks))
        except ValueError as e:
            return None, str(e)

        if mode == 'from_node':
            nodes = [UUID(str(node_uuid))]
        elif clear_nodes:
            nodes: List[UUID] = []
        elif node_uuid:
            nodes = [UUID(str(node_uuid))]
        else:
            nodes = [UUID(str(x)) for x in (copy_from.get('nodes') or [])]

        body_kwargs = self._build_create_host_kwargs(
            copy_from,
            remark=new_remark,
            address=addr,
            port=int(port or copy_from.get('port') or 443),
            nodes=nodes,
        )

        try:
            created, err = await self._create_host_api(body_kwargs)
            return created, err
        except Exception as e:
            logger.error(f"[REMNAWAVE] add_balancer_pool_member: {type(e).__name__}: {e}")
            return None, str(e)

    def _build_create_host_kwargs(
        self,
        tpl: Dict[str, Any],
        *,
        remark: str,
        address: str,
        port: int,
        nodes: List[UUID],
    ) -> Dict[str, Any]:
        from remnawave.enums import ALPN, Fingerprint
        from remnawave.models import CreateHostInboundData

        inbound = tpl.get('inbound') or {}
        cp_uuid = inbound.get('configProfileUuid') or inbound.get('config_profile_uuid')
        in_uuid = inbound.get('configProfileInboundUuid') or inbound.get('config_profile_inbound_uuid')

        body_kwargs: Dict[str, Any] = {
            'inbound': CreateHostInboundData(
                config_profile_uuid=UUID(str(cp_uuid)),
                config_profile_inbound_uuid=UUID(str(in_uuid)),
            ),
            'remark': remark,
            'address': address,
            'port': int(port),
            'path': tpl.get('path') or None,
            'sni': tpl.get('sni') or None,
            'host': tpl.get('host') or None,
            'nodes': nodes,
            'allow_insecure': bool(tpl.get('allowInsecure') or tpl.get('allow_insecure')),
            'is_disabled': bool(tpl.get('isDisabled') or tpl.get('is_disabled')),
            'is_hidden': bool(tpl.get('isHidden') or tpl.get('is_hidden')),
            'shuffle_host': bool(tpl.get('shuffleHost') or tpl.get('shuffle_host')),
            'mihomo_x25519': bool(tpl.get('mihomoX25519') or tpl.get('mihomo_x25519')),
            'override_sni_from_address': bool(
                tpl.get('overrideSniFromAddress') or tpl.get('override_sni_from_address')
            ),
            'keep_blank_sni': bool(tpl.get('keepBlankSni') or tpl.get('keep_blank_sni')),
        }
        alpn = self._coerce_host_enum(ALPN, tpl.get('alpn'))
        if alpn is not None:
            body_kwargs['alpn'] = alpn
        fp = self._coerce_host_enum(Fingerprint, tpl.get('fingerprint'))
        if fp is not None:
            body_kwargs['fingerprint'] = fp
        for key in ('server_description', 'tag'):
            val = tpl.get(key)
            if not val and key == 'server_description':
                val = tpl.get('serverDescription')
            if val:
                body_kwargs[key] = val
        vless_id = tpl.get('vlessRouteId')
        if vless_id is None:
            vless_id = tpl.get('vless_route_id')
        if vless_id is not None:
            body_kwargs['vless_route_id'] = int(vless_id)
        x_http = tpl.get('xHttpExtraParams') or tpl.get('x_http_extra_params')
        if x_http:
            body_kwargs['x_http_extra_params'] = x_http
        mux = tpl.get('muxParams') or tpl.get('mux_params')
        if mux:
            body_kwargs['mux_params'] = mux
        sockopt = tpl.get('sockoptParams') or tpl.get('sockopt_params')
        if sockopt:
            body_kwargs['sockopt_params'] = sockopt
        excl = tpl.get('excludedInternalSquads') or tpl.get('excluded_internal_squads') or []
        if excl:
            body_kwargs['excluded_internal_squads'] = [UUID(str(x)) for x in excl]
        return body_kwargs

    async def close(self):
        """Закрывает соединение с API"""
        if self._sdk and self._sdk._client:
            await self._sdk._client.aclose()
            self._initialized = False
            logger.info("[REMNAWAVE] Соединение закрыто")


# Глобальный экземпляр менеджера
remnawave_manager_instance = RemnawaveManager()

