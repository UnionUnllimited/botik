"""
Конфигурация модуля устройств
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# База данных устройств — рядом с модулем
DEVICES_DB_PATH = os.path.join(BASE_DIR, 'devices.db')
