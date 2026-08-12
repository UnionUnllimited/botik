"""
Отправка email.

Переехал из вырезанного `website/`: сайт удалён, а письмо с кодом
подтверждения шлёт привязка почты в боте — она осталась.

Приоритет:
  1. Resend API (если задан RESEND_API_KEY)
  2. SMTP через aiosmtplib (если заданы SMTP_HOST + SMTP_USER + SMTP_PASSWORD)

SMTP-режим формирует письмо с учётом анти-спам требований Mail.ru/Gmail:
  • Date и Message-ID заголовки (иначе фильтры ставят MISSING_DATE/MISSING_MID)
  • multipart/alternative с plain-text частью (только-HTML письма часто в спаме)
"""
import os
import re
import asyncio
import resend
from loguru import logger
from email.utils import formatdate, make_msgid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ── Resend ────────────────────────────────────────────────────────────────────

def _send_via_resend_sync(to: str, subject: str, html: str, sender: str) -> bool:
    resend.api_key = os.getenv("RESEND_API_KEY", "")
    params: resend.Emails.SendParams = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    resend.Emails.send(params)
    return True


async def _send_via_resend(to: str, subject: str, html: str, sender: str) -> bool:
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _send_via_resend_sync(to, subject, html, sender)),
            timeout=15,
        )
        logger.info(f"[EMAIL/Resend] Письмо отправлено: {to}")
        return True
    except asyncio.TimeoutError:
        logger.error(f"[EMAIL/Resend] Таймаут отправки на {to}")
        return False
    except Exception as e:
        logger.error(f"[EMAIL/Resend] Ошибка: {e}")
        return False


# ── SMTP fallback ─────────────────────────────────────────────────────────────

def _html_to_plain(html: str) -> str:
    """
    Грубое преобразование HTML → plain-text для multipart/alternative.
    Стрипает теги, схлопывает множественные переносы. Используется как
    fallback-текст для почтовых клиентов и анти-спам фильтров.
    """
    text = re.sub(r'<\s*br\s*/?\s*>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</\s*p\s*>',     '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)         # все остальные теги
    text = re.sub(r'\n[ \t]+', '\n', text)      # leading whitespace на строках
    text = re.sub(r'\n{3,}', '\n\n', text)      # схлопываем пустые строки
    return text.strip()


async def _send_via_smtp(to: str, subject: str, html: str, sender: str) -> bool:
    host     = os.getenv("SMTP_HOST", "")
    port     = int(os.getenv("SMTP_PORT", "465"))
    user     = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")

    if not host or not user or not password:
        logger.warning("[EMAIL/SMTP] SMTP не настроен (SMTP_HOST/SMTP_USER/SMTP_PASSWORD)")
        return False

    try:
        import aiosmtplib

        from_addr = sender or user

        # multipart/alternative: plain-text + HTML части. Нужна обе —
        # анти-спам фильтры Mail.ru/Gmail штрафуют только-HTML письма.
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to

        # Date + Message-ID — обязательны для прохождения спам-фильтров
        # (иначе пометки MISSING_DATE / MISSING_MID).
        msg["Date"]    = formatdate(localtime=True)
        # Domain для Message-ID берём из From-адреса. Если в From нет '@'
        # (странный кейс) — пусть email.utils сам подставит socket.getfqdn().
        msgid_domain = None
        if '@' in from_addr:
            msgid_domain = from_addr.split('@', 1)[1].strip() or None
        msg["Message-ID"] = make_msgid(domain=msgid_domain) if msgid_domain else make_msgid()

        # Plain-text часть — генерим из HTML стриппингом тегов
        # (порядок важен: text/plain первым, text/html вторым;
        #  клиенты предпочитают последнюю подходящую часть).
        plain = _html_to_plain(html)
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        # 465 → implicit TLS, 587 → STARTTLS
        use_tls = (port == 465)
        await aiosmtplib.send(
            msg,
            hostname=host,
            port=port,
            username=user,
            password=password,
            use_tls=use_tls,
            start_tls=not use_tls,
            timeout=15,
        )
        logger.info(f"[EMAIL/SMTP] Письмо отправлено: {to}")
        return True
    except ImportError:
        logger.error("[EMAIL/SMTP] aiosmtplib не установлен: pip install aiosmtplib")
        return False
    except Exception as e:
        logger.error(f"[EMAIL/SMTP] Ошибка: {e}")
        return False


# ── Основная функция ──────────────────────────────────────────────────────────

async def send_email(
    to: str,
    subject: str,
    html: str,
    smtp_from: str = "",
    **_kwargs,
) -> bool:
    """
    Отправляет письмо. Сначала пробует Resend, потом SMTP.
    """
    sender = smtp_from or os.getenv("SMTP_FROM", "")

    # Пробуем Resend
    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if resend_key:
        return await _send_via_resend(to, subject, html, sender or "onboarding@resend.dev")

    # Fallback на SMTP
    logger.info("[EMAIL] RESEND_API_KEY не задан, пробуем SMTP")
    return await _send_via_smtp(to, subject, html, sender)


# ── HTML шаблоны ──────────────────────────────────────────────────────────────

def code_email_html(code: str, project_name: str = "Сервис") -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Код входа</title>
</head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f0f0f;padding:48px 16px;">
  <tr><td align="center">
    <table width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;">

      <!-- Лого -->
      <tr><td align="center" style="padding-bottom:32px;">
        <span style="font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#555;">{project_name}</span>
      </td></tr>

      <!-- Карточка -->
      <tr><td style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:40px 40px 36px;">

        <p style="margin:0 0 8px;font-size:13px;color:#666;text-transform:uppercase;letter-spacing:.08em;">Код входа</p>

        <!-- Код -->
        <div style="margin:0 0 28px;background:#111;border:1px solid #2a2a2a;border-radius:12px;padding:24px 16px;text-align:center;">
          <span style="font-size:42px;font-weight:700;letter-spacing:.25em;color:#fff;font-family:'SF Mono','Fira Code','Courier New',monospace;">{code}</span>
        </div>

        <p style="margin:0 0 24px;font-size:14px;color:#888;line-height:1.6;">
          Введите этот код на странице входа.<br>
          Код действителен <span style="color:#ccc;font-weight:500;">15 минут</span>.
        </p>

        <hr style="border:none;border-top:1px solid #2a2a2a;margin:0 0 24px;">

        <p style="margin:0;font-size:12px;color:#444;line-height:1.5;">
          Если вы не запрашивали код — просто проигнорируйте это письмо.
        </p>

      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def subscription_activated_html(sub_url: str, expiry: str, project_name: str = "Сервис", happ_key: str = None) -> str:
    key_block = ""
    if happ_key:
        key_block = f"""
        <p style="margin:20px 0 8px;font-size:13px;color:#666;text-transform:uppercase;letter-spacing:.08em;">Ключ для вставки в Happ</p>
        <div style="background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:16px;margin-bottom:20px;">
          <code style="font-size:13px;color:#aaa;word-break:break-all;font-family:'SF Mono','Courier New',monospace;">{happ_key}</code>
        </div>
        <p style="font-size:13px;color:#666;margin-bottom:20px;">Скопируйте ключ и вставьте в Happ → «+» → «Добавить подписку»</p>"""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f0f0f;padding:48px 16px;">
  <tr><td align="center">
    <table width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;">
      <tr><td align="center" style="padding-bottom:24px;">
        <span style="font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#555;">{project_name}</span>
      </td></tr>
      <tr><td style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:36px 36px 32px;">
        <p style="margin:0 0 4px;font-size:22px;color:#fff;font-weight:700;">✅ Подписка активирована</p>
        <p style="margin:0 0 24px;font-size:14px;color:#666;">Действует до <span style="color:#ccc">{expiry}</span></p>
        {key_block}
        <a href="{sub_url}" style="display:block;text-align:center;padding:14px;background:linear-gradient(135deg,#34c759,#30d158);color:#fff;text-decoration:none;border-radius:10px;font-size:15px;font-weight:600;">
          Открыть личный кабинет
        </a>
        <hr style="border:none;border-top:1px solid #2a2a2a;margin:24px 0 16px;">
        <p style="margin:0;font-size:12px;color:#444;">Нужна помощь? Обратитесь в поддержку.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""