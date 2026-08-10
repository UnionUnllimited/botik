import json
from typing import Any, Type, TypeVar
from loguru import logger
import db_helpers

T = TypeVar('T')

class SettingsManager:
    """
    Менеджер настроек, который загружает конфигурацию из базы данных в кэш.
    """
    def __init__(self):
        self._settings_cache = {}
        logger.info("Менеджер настроек инициализирован.")

    async def load_settings(self):
        """Загружает или перезагружает все настройки из базы данных."""
        logger.info("Загрузка/перезагрузка настроек из базы данных...")
        try:
            settings_from_db = await db_helpers.load_all_settings()
            self._settings_cache = settings_from_db
            logger.success(f"Успешно загружено {len(self._settings_cache)} настроек.")
        except Exception as e:
            logger.error(f"Не удалось загрузить настройки из БД: {e}")
            # В случае ошибки не очищаем старый кэш, чтобы бот мог продолжить работу
            # на старых настройках, если они были загружены ранее.

    def get(self, key: str, default: T = None) -> T:
        """
        Получает значение настройки из кэша.
        Выполняет приведение типов на основе типа значения по умолчанию.
        """
        value_str = self._settings_cache.get(key)

        # Если значение — кортеж (например, (текст, описание)), берем только текст
        if isinstance(value_str, tuple):
            value_str = value_str[0]

        if value_str is None:
            # Предупреждаем только если кэш не пустой (настройки уже загружены)
            if self._settings_cache:
                logger.warning(f"Настройка '{key}' не найдена в кэше. Используется значение по умолчанию: {default}")
            return default

        if default is None:
            return value_str

        target_type = type(default)

        try:
            if target_type == bool:
                return value_str.lower() in ('true', '1', 't', 'y', 'yes')
            if target_type == int:
                return int(value_str)
            if target_type == float:
                return float(value_str)
            if target_type in (list, dict):
                # Для сложных типов, таких как списки серверов, ожидаем JSON-строку
                return json.loads(value_str)
            return value_str # Для str и других типов
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            logger.error(f"Ошибка приведения типа для ключа '{key}' (значение: '{value_str}') к типу {target_type}. "
                         f"Ошибка: {e}. Используется значение по умолчанию: {default}")
            return default

# Создаем единый экземпляр менеджера настроек для всего приложения
app_conf = SettingsManager() 