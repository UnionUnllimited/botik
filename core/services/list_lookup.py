"""Поиск по собранным спискам: есть ли домен или IP и откуда он взялся.

Страница списков показывала числа — «2 037 строк», «14 268 строк», — а что
внутри, видно не было. На вопрос «пойдёт ли youtube.com через туннель»
приходилось открывать файл и искать глазами, а на «почему этот домен там»
не отвечало ничто.

Два ответа на один запрос:

* **покрывается ли** — для домена это сама запись или любой родитель:
  `youtube.com` в списке ведёт через туннель и `www.youtube.com`; для IP —
  сеть из списка, в которую он попадает;
* **что похоже** — записи, где встречается введённое: полезно, когда
  точного имени не помнят.

У каждой находки — откуда она: какой источник или «свой список». Это читается
из снимков последней опубликованной сборки (`domain_lists.read_snapshots`).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ListKind, ManualList
from core.services import domain_lists

MATCH_LIMIT = 100
"""Похожих — не больше сотни: найти больше значит, что запрос слишком общий,
и листать их всё равно никто не станет."""

OWN = "свой список"

_SCHEME = re.compile(r"^[a-z]+://", re.IGNORECASE)


def normalize(query: str) -> tuple[str, str]:
    """Вид запроса и его чистое значение: ('ip', '1.2.3.4') или ('domain', 'youtube.com').

    Вставляют что придётся — ссылку целиком, с путём и портом, с «www».
    Чистим тем же духом, что и свой список: схему, путь, порт и точку в конце.
    """
    text = _SCHEME.sub("", (query or "").strip().lower())
    # Сеть с маской держит свою косую черту; у всего остального она — начало пути.
    try:
        ipaddress.ip_network(text, strict=False)
        return ListKind.PROXY_IP, text
    except ValueError:
        pass
    host = text.split("/", 1)[0].split(":", 1)[0].strip(".")
    try:
        ipaddress.ip_address(host)
        return ListKind.PROXY_IP, host
    except ValueError:
        return ListKind.PROXY_DOMAIN, host


def _lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line]


def _domain_covered_by(host: str, entries: set[str]) -> list[str]:
    """Запись и её родители: `a.b.youtube.com` покрывают `b.youtube.com`
    и `youtube.com`, если они в списке."""
    labels = host.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1) if ".".join(labels[i:]) in entries]


def _ip_covered_by(value: str, entries: list[str]) -> list[str]:
    try:
        wanted = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return []
    found = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if wanted.version == network.version and wanted.subnet_of(network):
            found.append(entry)
    return found


def _needle(kind: str, value: str) -> str:
    """По чему искать похожие. Для домена — его главная часть: по целому
    `www.youtube.com` похожих не найдётся, а по `youtube` найдутся
    `youtube-nocookie.com` и прочие родственники. Короткие части вроде
    `t` из `t.me` слишком общие — тогда ищем по имени целиком."""
    if kind != ListKind.PROXY_DOMAIN or "." not in value:
        return value
    name = value.split(".")[-2]
    return name if len(name) >= 3 else value


async def lookup(session: AsyncSession, query: str) -> dict[str, Any]:
    kind, value = normalize(query)
    if len(value) < 2:
        return {"query": query, "kind": kind, "value": value, "covered_by": [], "matches": [], "total": 0}

    entries = _lines(domain_lists.read_list(kind))
    entry_set = set(entries)
    own_row = await session.scalar(select(ManualList).where(ManualList.kind == kind))
    own = set(domain_lists.CLEANERS[kind](own_row.body if own_row else ""))

    sources, contents = domain_lists.read_snapshots()
    titles = {int(item["id"]): item.get("title") or "" for item in sources if item.get("kind") == kind}

    def origin(entry: str) -> list[str]:
        found = [titles[source_id] for source_id in titles if entry in contents.get(source_id, ())]
        if entry in own:
            found.append(OWN)
        return found

    if kind == ListKind.PROXY_IP:
        covered = _ip_covered_by(value, entries)
    else:
        covered = _domain_covered_by(value, entry_set)

    similar = [entry for entry in entries if _needle(kind, value) in entry and entry not in covered]
    return {
        "query": query,
        "kind": kind,
        "value": value,
        "covered": bool(covered),
        "covered_by": [{"entry": entry, "origin": origin(entry)} for entry in covered],
        "matches": [{"entry": entry, "origin": origin(entry)} for entry in similar[:MATCH_LIMIT]],
        "total": len(similar),
        "in_own": value in own,
        "snapshots_ready": bool(titles),
        "list_size": len(entries),
    }


async def _own_body(session: AsyncSession, kind: str) -> str:
    row = await session.scalar(select(ManualList).where(ManualList.kind == kind))
    return row.body if row is not None else ""


async def add_to_own(session: AsyncSession, kind: str, value: str, *, author: str = "") -> str:
    """Дописывает значение в свой список. Возвращает текст отказа или пусто.

    Через то же сохранение, что и поле на странице: правка ложится в историю
    и откатывается кнопкой «Вернуть», как ручная.
    """
    cleaned = domain_lists.CLEANERS[kind](value)
    if len(cleaned) != 1:
        return "Не похоже на домен." if kind == ListKind.PROXY_DOMAIN else "Не похоже на IP или сеть."
    body = await _own_body(session, kind)
    if cleaned[0] in set(domain_lists.CLEANERS[kind](body)):
        return ""
    new_body = (body.rstrip("\n") + "\n" if body.strip() else "") + cleaned[0] + "\n"
    await domain_lists.save_manual(session, kind, new_body, author=author)
    return ""


async def remove_from_own(session: AsyncSession, kind: str, value: str, *, author: str = "") -> str:
    """Убирает значение из своего списка. Строки источников так не убрать —
    они приходят извне, — поэтому и кнопка есть только у своих."""
    target = set(domain_lists.CLEANERS[kind](value))
    if not target:
        return "Нечего убирать."
    body = await _own_body(session, kind)
    kept = [line for line in body.splitlines() if not set(domain_lists.CLEANERS[kind](line)) & target]
    if len(kept) == len(body.splitlines()):
        return "Этого нет в своём списке — оно пришло из источника."
    await domain_lists.save_manual(session, kind, "\n".join(kept) + ("\n" if kept else ""), author=author)
    return ""
