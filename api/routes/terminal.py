"""Живой терминал роутера в браузере оператора.

Разовые команды из карточки (`fleet_api`) закрывают почти всё, но не всё:
`top`, `logread -f`, интерактивный `vi` и любая программа, которая ждёт ввода,
через «отправил — получил вывод» не работают. Здесь настоящая сессия
с псевдотерминалом: байты из браузера уходят в SSH, байты из SSH — обратно.

Вход тот же, что у веб-панели: админка бота просит разовый билет, браузер
меняет его на короткую сессию, и уже она открывает вебсокет.

**Запрет опасных команд здесь не действует, и иначе быть не может.**
В разовой команде строку видно целиком, и `sysupgrade` отсекается до запуска.
В живом терминале в сокет приходят отдельные нажатия, вперемешку с управляющими
последовательностями редактора; собирать из них «строку команды» — гадание,
которое всё равно обходится в две секунды. Поэтому защита тут другая:
короткая сессия, разовый билет и запись открытия и закрытия в журнал
устройства — видно, кто и когда заходил.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import asyncssh
import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from core.config import settings
from core.models import Device
from core.services import router_shell, terminal_ticket
from core.services import routers as routers_service

router = APIRouter(include_in_schema=False)
log = structlog.get_logger("api.terminal")

IDLE_LIMIT_SEC = 15 * 60
"""Столько терминал живёт без единого нажатия. Забытая вкладка — открытый root
на устройстве клиента, и висеть до перезапуска сервиса она не должна."""

EXPIRED = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Терминал роутера</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,'Segoe UI',Roboto,sans-serif">
<div style="max-width:460px;padding:26px;background:#171a21;border:1px solid #2a2f3a;border-radius:12px">
<h1 style="font-size:19px;margin:0 0 10px">Терминал закрыт</h1>
<p style="color:#99a0ae;margin:0">Сессия истекла. Вернитесь в карточку роутера
в админке и нажмите «Открыть терминал» ещё раз.</p>
</div></body></html>"""


def _page(mac: str) -> str:
    """Страница терминала. Всё своё: xterm лежит у нас в статике.

    Ставить его с чужого CDN нельзя — админку открывают из сетей, где половина
    таких адресов не отвечает, и терминал превратился бы в чёрный экран без
    единого объяснения.
    """
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Терминал · {mac}</title>
<link rel="stylesheet" href="/static/vendor/xterm.css">
<style>
  html,body {{ margin:0; height:100%; background:#0f1115; }}
  #head {{ font:13px/1.4 -apple-system,'Segoe UI',Roboto,sans-serif; color:#99a0ae;
          padding:8px 12px; border-bottom:1px solid #2a2f3a; }}
  #head b {{ color:#e6e8ee; }}
  #term {{ height:calc(100% - 34px); }}
</style></head>
<body>
<div id="head">Терминал роутера <b>{mac}</b> · <span id="state">подключаемся…</span></div>
<div id="term"></div>
<script src="/static/vendor/xterm.js"></script>
<script src="/static/vendor/xterm-addon-fit.js"></script>
<script>
(function () {{
  const state = document.getElementById('state');

  // Если библиотеки нет, молчать нельзя. Без этой проверки страница застывала
  // на «подключаемся…» навсегда: `new Terminal` падал первой же строкой, до
  // открытия сокета дело не доходило, и понять, что не хватает файла в
  // /static/vendor/, было неоткуда — ни на экране, ни в шапке.
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {{
    state.textContent = 'библиотека терминала не загрузилась';
    document.getElementById('term').innerHTML =
      '<div style="color:#e0736d;font:13px/1.7 monospace;padding:14px">'
      + 'Не загрузился <b>/static/vendor/xterm.js</b> — терминал не запустится.<br>'
      + 'Файлы xterm лежат в образе приложения. Если их там нет, пересоберите его: '
      + '<b>docker compose up -d --build api</b>.'
      + '</div>';
    return;
  }}

  const term = new Terminal({{ fontSize: 13, fontFamily: 'monospace', cursorBlink: true }});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('term'));
  fit.fit();

  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(scheme + '://' + location.host + '/terminal/ws');
  socket.binaryType = 'arraybuffer';

  socket.onopen = function () {{
    state.textContent = 'на связи';
    sendSize();
  }};
  socket.onmessage = function (event) {{
    term.write(new Uint8Array(event.data));
  }};
  socket.onclose = function () {{
    state.textContent = 'соединение закрыто';
    term.write('\\r\\n\\x1b[33mСоединение закрыто.\\x1b[0m\\r\\n');
  }};
  socket.onerror = function () {{ state.textContent = 'ошибка соединения'; }};

  term.onData(function (data) {{
    if (socket.readyState === WebSocket.OPEN) {{ socket.send(data); }}
  }});

  // Размер окна уходит отдельным сообщением: без него `top` и `vi` рисуют
  // по 80x24 и разъезжаются на первом же переносе строки.
  function sendSize() {{
    if (socket.readyState !== WebSocket.OPEN) {{ return; }}
    socket.send(JSON.stringify({{ resize: [term.cols, term.rows] }}));
  }}
  window.addEventListener('resize', function () {{ fit.fit(); sendSize(); }});
  term.focus();
}})();
</script>
</body></html>"""


@router.get("/terminal/open")
async def open_terminal(ticket: str = "") -> Response:
    """Обмен разового билета на сессию терминала. Сюда ведёт кнопка в админке."""
    redeemed = await terminal_ticket.redeem(ticket)
    if redeemed is None:
        return HTMLResponse(EXPIRED, status_code=409)

    cookie_value, target = redeemed
    response = HTMLResponse(_page(target.mac))
    response.set_cookie(
        terminal_ticket.COOKIE,
        cookie_value,
        max_age=terminal_ticket.SESSION_TTL_SEC,
        httponly=True,
        secure=settings.app.is_prod,
        samesite="lax",
        path="/terminal",
    )
    return response


async def _pump_to_browser(process, socket: WebSocket) -> None:
    """Байты из роутера — в браузер, пока хоть одна сторона жива."""
    while True:
        chunk = await process.stdout.read(4096)
        if not chunk:
            return
        await socket.send_bytes(chunk)


@router.websocket("/terminal/ws")
async def terminal_ws(
    socket: WebSocket, session: AsyncSession = Depends(get_session)
) -> None:
    """Живая сессия: вебсокет с одной стороны, псевдотерминал SSH с другой."""
    target = await terminal_ticket.load(socket.cookies.get(terminal_ticket.COOKIE))
    if target is None:
        # 1008 — «policy violation»: браузер покажет это в консоли, а страница
        # напишет «соединение закрыто». Пускать без сессии нельзя.
        await socket.close(code=1008)
        return

    device = await session.get(Device, target.device_id)
    if device is None:
        await socket.close(code=1008)
        return

    await socket.accept()

    try:
        connection = await router_shell.connect(device)
    except router_shell.ShellError as exc:
        await socket.send_bytes(f"\r\n\x1b[31m{exc}\x1b[0m\r\n".encode())
        await socket.close()
        return

    routers_service.add_event(
        session,
        device_id=device.id,
        mac=device.mac,
        level="warning",
        message="Открыт терминал",
    )
    await session.commit()

    log.info("terminal.session_started", device_id=device.id, mac=device.mac)
    try:
        process = await connection.create_process(
            term_type="xterm-256color", term_size=(80, 24), encoding=None
        )
        reader = asyncio.create_task(_pump_to_browser(process, socket))
        try:
            while True:
                message = await asyncio.wait_for(socket.receive(), timeout=IDLE_LIMIT_SEC)
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                text = message.get("text")
                if text is not None:
                    # Размер окна приходит текстом, всё остальное — нажатия.
                    if text.startswith('{"resize"'):
                        cols, rows = json.loads(text)["resize"]
                        process.change_terminal_size(int(cols), int(rows))
                        continue
                    data = text.encode()
                if data:
                    process.stdin.write(data)
        finally:
            reader.cancel()
    except TimeoutError:
        await socket.send_bytes("\r\n\x1b[33mТерминал закрыт по бездействию.\x1b[0m\r\n".encode())
    except (WebSocketDisconnect, asyncssh.Error, OSError) as exc:
        log.info("terminal.session_broken", device_id=device.id, error=str(exc))
    finally:
        connection.close()
        log.info("terminal.session_finished", device_id=device.id, mac=device.mac)
        # Закрытие уже закрытого сокета — обычное дело: браузер мог уйти
        # первым, и падать на этом в `finally` незачем.
        with contextlib.suppress(RuntimeError):
            await socket.close()
