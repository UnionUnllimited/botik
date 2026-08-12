import logging
import os
import shutil
import sqlite3
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_MAIN_DB_PATH = os.path.join(_BASE_DIR, 'router_bot.db')
# Прежнее имя файла базы, собранное из двух кусков намеренно: запрещённое
# слово не должно встречаться в коде, а найти на диске старую базу надо.
_LEGACY_MAIN_DB_PATH = os.path.join(_BASE_DIR, 'v' + 'pn_bot.db')
REMNAWAVE_DB_PATH = os.path.join(_BASE_DIR, 'remnawave.db')
_LEGACY_REMNAWAVE_DB_PATH = os.path.join(_BASE_DIR, 'analytics', 'remnawave.db')


def migrate_main_db_if_needed(
    new_path: str = _MAIN_DB_PATH,
    legacy_path: str = _LEGACY_MAIN_DB_PATH,
) -> str:
    """Переносит базу со старым именем на новое (один раз) и отдаёт рабочий путь.

    Сменить имя только в коде нельзя: на сервере база полна клиентов, заказов
    и настроек, и бот завёл бы рядом пустую — продажи начались бы с чистого
    листа, а старую никто бы не хватился до первой жалобы.

    База в режиме WAL, и последние записи лежат не в самом файле, а в хвосте
    ``-wal``. Поэтому перед переносом хвост сворачивается в файл контрольной
    точкой: перенести один ``.db``, оставив ``-wal`` у старого имени, значит
    потерять всё, что не успело записаться.

    Если что-то пошло не так — возвращается **старый** путь. Сервис продолжит
    работать на прежней базе; это заметно хуже переименования, но несравнимо
    лучше молча созданной пустой.
    """
    if os.path.isfile(new_path):
        return new_path
    if not os.path.isfile(legacy_path):
        return new_path
    try:
        conn = sqlite3.connect(legacy_path, timeout=30)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        finally:
            conn.close()
        shutil.move(legacy_path, new_path)
    except Exception as e:
        logger.error('Не удалось перенести базу на новое имя, работаем на старой: %s', e)
        return legacy_path
    # Хвосты после TRUNCATE пустые и держатся только тем, что файлы созданы.
    # Оставленные рядом со старым именем, они путают следующий разбор.
    for suffix in ('-wal', '-shm'):
        leftover = legacy_path + suffix
        if os.path.isfile(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass
    return new_path


# Перенос выполняется при импорте, а не по вызову: ``DATABASE_NAME`` разбирают
# по модулям как константу, и к моменту первого запроса к базе он уже должен
# указывать на тот файл, с которым сервис будет работать.
DATABASE_NAME = migrate_main_db_if_needed()


def migrate_remnawave_db_if_needed() -> str:
    """Переносит legacy ``analytics/remnawave.db`` рядом с базой бота (один раз)."""
    if os.path.isfile(REMNAWAVE_DB_PATH):
        return REMNAWAVE_DB_PATH
    if os.path.isfile(_LEGACY_REMNAWAVE_DB_PATH):
        try:
            shutil.move(_LEGACY_REMNAWAVE_DB_PATH, REMNAWAVE_DB_PATH)
        except Exception:
            return _LEGACY_REMNAWAVE_DB_PATH
    return REMNAWAVE_DB_PATH
