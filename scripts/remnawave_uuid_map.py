"""Проставление числового `id` учётки Remnawave в базу бота.

С версии 3.4 панель убрала у пользователя `uuid` и перешла на числовой `id`.
В базе бота учётки записаны по `uuid` (`users.remnawave_user_uuid`), и связать
их с панелью напрямую больше нечем: старое поле она не отдаёт вовсе.

**Сопоставляем по тому, что пережило обновление.** В ответе панели остались
`shortUuid` и `username`, и оба лежат у бота рядом с uuid —
`remnawave_short_uuid` и `remnawave_username`. Первым идёт `shortUuid`:
он уникален и не меняется при переименовании. `username` — запасной,
для записей, у которых короткого идентификатора не сохранилось.

Два режима:

    python scripts/remnawave_uuid_map.py dump -           # выгрузить в stdout
    python scripts/remnawave_uuid_map.py apply <файл>     # проставить в базу бота

Выгрузка ходит в панель и требует `httpx` — её запускают в контейнере
приложения, где он есть, а снимок забирают перенаправлением. Проставление
работает с базой бота на хосте и обходится стандартной библиотекой.

Выгрузка ничего не меняет. Проставление трогает только колонку
`remnawave_user_id` и никогда — существующие поля: если сопоставление
окажется неверным, откат сводится к её очистке.

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
            "Запускайте выгрузку в контейнере приложения — там они уже есть:\n"
            "  docker compose run --rm -T api python scripts/remnawave_uuid_map.py dump -\n"
            "Через `. ./.env` их не подхватить: в файле есть значения с пробелами, "
            "и оболочка спотыкается на первом же."
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
    # Импорт здесь, а не наверху: `httpx` стоит в образе приложения, а `apply`
    # запускают на хосте, где его нет и не нужно — ему хватает sqlite3.
    import httpx

    base, token = _panel()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        response = await client.get(f"{base}{USERS_PATH}", headers=headers)
        response.raise_for_status()
        return _rows(response.json())


def dump(out: Path) -> int:
    """Снимает с панели то, по чему учётку можно узнать: id, имя и shortUuid."""
    rows = asyncio.run(_fetch_users())

    pairs = []
    for row in rows:
        uid = row.get("id")
        if uid is None:
            continue
        pairs.append(
            {
                "id": str(uid),
                "username": str(row.get("username") or ""),
                "shortUuid": str(row.get("shortUuid") or ""),
            }
        )

    body = json.dumps(pairs, ensure_ascii=False, indent=2)
    # `-` означает «вывести в stdout»: так снимок забирается из контейнера
    # обычным перенаправлением, без возни с томами. Счётчики при этом уходят
    # в stderr, иначе они попали бы в сам файл.
    if str(out) == "-":
        print(body)
        report = sys.stderr
    else:
        out.write_text(body, encoding="utf-8")
        report = sys.stdout

    no_id = len(rows) - len(pairs)
    print(f"Учёток в панели: {len(rows)}", file=report)
    print(f"Снято записей:   {len(pairs)}", file=report)
    if no_id:
        print(f"Без id: {no_id} — такие сопоставить не выйдет", file=report)
    if str(out) != "-":
        print(f"Записано в {out}", file=report)
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

        # Схема бота от версии к версии разная: колонки под короткий
        # идентификатор или имя может не быть вовсе. Спрашиваем таблицу,
        # а не полагаемся на память — иначе падаем на середине работы,
        # уже успев что-то записать.
        has_short = "remnawave_short_uuid" in columns
        has_name = "remnawave_username" in columns
        if not has_short and not has_name:
            raise SystemExit(
                "В таблице users нет ни remnawave_short_uuid, ни remnawave_username — "
                "связать учётки не по чему."
            )

        by_short = 0
        by_name = 0
        for pair in pairs:
            # Сначала по короткому идентификатору: он уникален и переименование
            # клиента его не трогает. Условие на пустое значение обязательно —
            # без него одна запись с пустым полем собрала бы на себя все id.
            if has_short and pair["shortUuid"]:
                cursor = connection.execute(
                    "UPDATE users SET remnawave_user_id = ? "
                    "WHERE remnawave_short_uuid = ? AND remnawave_user_id IS NULL",
                    (pair["id"], pair["shortUuid"]),
                )
                by_short += cursor.rowcount
                if cursor.rowcount:
                    continue
            if has_name and pair["username"]:
                cursor = connection.execute(
                    "UPDATE users SET remnawave_user_id = ? "
                    "WHERE remnawave_username = ? AND remnawave_user_id IS NULL",
                    (pair["id"], pair["username"]),
                )
                by_name += cursor.rowcount
        connection.commit()

        # Сколько записей осталось без id из тех, у кого вообще есть чем
        # связаться. Условие собирается из существующих колонок: спросить
        # про отсутствующую — та же ошибка, что уронила первый заход.
        linked = [f"COALESCE({name}, '') <> ''" for name, present in (
            ("remnawave_short_uuid", has_short),
            ("remnawave_username", has_name),
        ) if present]
        # Имена колонок сюда попадают не из ввода, а из PRAGMA этой же
        # таблицы, и подставлять их параметром SQLite не даёт.
        condition = " OR ".join(linked)
        query = (
            f"SELECT COUNT(*) FROM users WHERE ({condition}) "  # noqa: S608
            "AND remnawave_user_id IS NULL"
        )
        left = connection.execute(query).fetchone()[0]
    finally:
        connection.close()

    print(f"Записей в файле:      {len(pairs)}")
    print(f"Связано по shortUuid: {by_short}")
    print(f"Связано по username:  {by_name}")
    if left:
        print(
            f"Осталось без id:      {left} — у этих записей учётки в панели нет вовсе "
            "(удалена или заведена в другой панели). Разбирать руками."
        )
    return by_short + by_name


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
