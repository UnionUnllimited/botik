"""Визитёр переживает недоступный сервер и знает, куда идти.

22 сентября 2026 путь от нашей машины до frps пропал целиком — не шёл даже
ping, в обе стороны, при рабочем интернете с обеих сторон. Роутеры при этом
на сервер заходили: режут не сервер, а конкретное направление. Похоже
на фильтрацию по адресу у одного из хостеров.

Панель роутера, показания, SSH и автоактивация в эти часы не работали.
У клиентов при этом всё было в порядке: доступ идёт с роутера прямо
на узлы, туннель в нём не участвует. Слепнем мы, а не они.

Схема после починки описана в `deploy/frp/README.md`. Здесь — два свойства
кода, без которых она не держится.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = (ROOT / "worker" / "tasks" / "frpc_config.py").read_text(encoding="utf-8")
README = (ROOT / "deploy" / "frp" / "README.md").read_text(encoding="utf-8")


def test_the_visitor_does_not_give_up_on_a_silent_server():
    """По умолчанию frpc завершается при первой неудаче входа, и контейнер
    поднимает его заново. Повтор через полную перезагрузку процесса теряет
    все поднятые визитёры и упирается в задержку перезапуска докера."""
    assert '"loginFailExit = false"' in RENDER


def test_the_server_address_reaches_the_visitor():
    """Сравнивается весь файл, а не список устройств: смена `FRP_SERVER_HOST`
    обязана доехать так же, как появление нового роутера. Пока сравнение шло
    по устройствам, смена адреса осталась бы только в `.env`."""
    head = RENDER.index("def write_if_changed")
    body = RENDER[head : RENDER.index("return True", head)]
    assert "read_bytes()" in body, "сравнивать надо содержимое файла целиком"


def test_an_error_without_text_cannot_happen():
    """`{"error": ""}` в журнале сообщает, что не вышло, и ничего — о том,
    почему. Ровно такая запись стоила лишнего часа: у httpx.ReadError
    `str()` пустой, и обрыв через мост выглядел как молчание.
    """
    from core.errors import describe

    class SilentError(Exception):
        pass

    assert describe(SilentError()) == "SilentError"
    assert describe(SilentError("  ")) == "SilentError"
    assert describe(ValueError("так и так")) == "ValueError: так и так"


def test_nobody_logs_a_bare_exception_text():
    """Одно место, забывшее имя класса, — это один будущий час поисков."""
    import re

    bare = []
    for tree in ("core", "api", "worker"):
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            body = path.read_text(encoding="utf-8")
            if re.search(r"log\.\w+\([^)]*?error=str\(exc\)", body, re.S):
                bare.append(str(path.relative_to(ROOT)))
    assert bare == [], f"в журнал уходит текст без имени ошибки: {bare}"
