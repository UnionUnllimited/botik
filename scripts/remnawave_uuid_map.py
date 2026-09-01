"""Выгрузка пар «uuid учётки → числовой id» из панели Remnawave.

**Запускать до обновления панели на 3.4.** С этой версии `uuid` у пользователя
удалён: панель его не отдаёт и по нему не ищет. В базе бота учётки записаны
именно по `uuid` (`users.remnawave_user_uuid`), и если обновиться, не сняв
соответствие, связать клиента с его учёткой будет нечем — останется разбор
по именам руками.

Два режима, оба безопасные:

    python scripts/remnawave_uuid_map.py dump             # выгрузить в файл
    python scripts/remnawave_uuid_map.py apply <файл>     # проставить в базу бота

Выгрузка ничего не меняет. Проставление трогает только новую колонку
`remnawave_user_id` и никогда — существующие поля: если сопоставление
окажется неверным, откат сводится к очистке одной колонки.

Адрес и токен панели берутся из окружения основного приложения
(`REMNAWAVE_BASE_URL`, `REMNAWAVE_TOKEN`) — тех же, которыми оно ходит
в панель само.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx

BOT_DB = Path(__file__).resolve().parents[1] / "bot" / "router_bot.db"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "remnawave-uuid-map.json"
USERS_PATH = "/api/users"
TIMEOUT_SEC = 30


def _panel() -> tuple[str, str]:
    base = (os.getenv("REMNAWAVE_BASE_URL") or "").strip().rstrip("/")
    token = (os.getenv("REMNAWAVE_TOKEN") or "").strip()
    if not base or not token:
        raise SystemExit(
            "Нужны REMNAWAVE_BASE_URL и REMNAWAVE_TOKEN в окружении.\n"
            "На боевом сервере: cd /opt/router-shop && set -a && . ./.env && set +a"
        )
    return base, token


def _rows(payload: object) -> list[dict]:
    """Список учёток из ответа. Панель заворачивает его по-разному в разных
    версиях: то в `response`, то в `response.users`, то отдаёт голым массивом."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("response", "data", "users", "items"):
            inner = payload.get(key)
            if inner is not None:
                return _rows(inner)
    return []


async def _fetch_users() -> list[dict]:
    base, token = _panel()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        response = await client.get(f"{base}{USERS_PATH}", headers=headers)
        response.raise_for_status()
        return _rows(response.json())


def dump(out: Path) -> int:
    """Скачивает учётки и раскладывает пары в файл.

    Запись синхронная и вынесена из корутины намеренно: писать на диск внутри
    асинхронной функции — значит держать цикл событий на время записи, а
    выигрыша тут никакого, файл один.
    """
    rows = asyncio.run(_fetch_users())

    pairs = []
    for row in rows:
        uuid = str(row.get("uuid") or "").strip()
        uid = row.get("id")
        if not uuid or uid is None:
            continue
        pairs.append(
            {
                "uuid": uuid,
                "id": str(uid),
                # Имя кладём рядом не для сопоставления, а для проверки глазами:
                # если что-то пойдёт не так, по нему видно, чья это учётка.
                "username": str(row.get("username") or ""),
            }
        )

    out.write_text(json.dumps(pairs, ensure_ascii=False, indent=2), encoding="utf-8")
    without_id = len(rows) - len(pairs)
    print(f"Учёток в панели: {len(rows)}")
    print(f"Пар uuid → id:   {len(pairs)}")
    if without_id:
        print(f"Без одного из полей: {without_id} — их сопоставить не выйдет, проверьте вручную")
    print(f"Записано в {out}")
    return len(pairs)


def apply(source: Path) -> int:
    if not source.exists():
        raise SystemExit(f"Файл выгрузки не найден: {source}")
    if not BOT_DB.exists():
        raise SystemExit(f"База бота не найдена: {BOT_DB}")

    pairs = json.loads(source.read_text(encoding="utf-8"))
    connection = sqlite3.connect(BOT_DB)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "remnawave_user_id" not in columns:
            raise SystemExit(
                "В таблице users нет колонки remnawave_user_id.\n"
                "Она заводится при старте бота — обновите код и перезапустите router-bot."
            )

        updated = 0
        for pair in pairs:
            cursor = connection.execute(
                "UPDATE users SET remnawave_user_id = ? WHERE remnawave_user_uuid = ?",
                (pair["id"], pair["uuid"]),
            )
            updated += cursor.rowcount
        connection.commit()
    finally:
        connection.close()

    print(f"Пар в файле:      {len(pairs)}")
    print(f"Обновлено записей: {updated}")
    if updated < len(pairs):
        print(
            "Разница — это учётки панели, которых нет в базе бота: "
            "заведённые вручную или оставшиеся от удалённых клиентов. Это нормально."
        )
    return updated


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("dump", "apply"):
        print(__doc__)
        raise SystemExit(1)
    if argv[0] == "dump":
        out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
        dump(out)
    else:
        source = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
        apply(source)


if __name__ == "__main__":
    main()
