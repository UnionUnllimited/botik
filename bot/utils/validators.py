"""Проверка данных покупателя.

Правила переехали в `core/validators.py`: те же ФИО, телефон и адрес нужны
оформлению заказа на сайте, а бот уходит. Здесь остались реэкспорты, чтобы
не переписывать хендлеры перед их удалением.
"""

from __future__ import annotations

from core.validators import (
    clean_address,
    clean_city,
    clean_full_name,
    clean_phone,
    clean_pvz,
    format_phone,
)

__all__ = [
    "clean_address",
    "clean_city",
    "clean_full_name",
    "clean_phone",
    "clean_pvz",
    "format_phone",
]
