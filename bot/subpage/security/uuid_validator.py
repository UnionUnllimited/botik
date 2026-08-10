"""
Валидация UUID для защиты от брутфорса
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# UUID v4 формат: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
# где x - hex цифра, y - один из 8, 9, A, B
UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def is_valid_uuid(uuid: str) -> bool:
    """Проверка валидности UUID формата"""
    if not uuid or not isinstance(uuid, str):
        return False
    
    uuid = uuid.strip()
    
    # Проверяем длину (UUID должен быть 36 символов с дефисами)
    if len(uuid) != 36:
        return False
    
    # Проверяем формат через regex
    if not UUID_PATTERN.match(uuid):
        return False
    
    return True


def validate_uuid(uuid: str) -> tuple[bool, Optional[str]]:
    """
    Валидация UUID с возвратом результата и сообщения об ошибке
    
    Returns:
        (is_valid, error_message)
    """
    if not uuid:
        return False, "UUID не предоставлен"
    
    if not isinstance(uuid, str):
        return False, "UUID должен быть строкой"
    
    uuid = uuid.strip()
    
    if len(uuid) != 36:
        return False, f"Неверная длина UUID: ожидается 36 символов, получено {len(uuid)}"
    
    if not UUID_PATTERN.match(uuid):
        return False, "Неверный формат UUID: должен быть UUID v4"
    
    return True, None

