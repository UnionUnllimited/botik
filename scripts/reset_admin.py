"""Создание учётки администратора или сброс её пароля.

    python -m scripts.reset_admin                  # логин owner, роль owner
    python -m scripts.reset_admin --login ivan     # другой сотрудник
    python -m scripts.reset_admin --reset-2fa      # заодно отвязать приложение TOTP

Пароль печатается один раз — в базе хранится только хеш Argon2, восстановить
прежний невозможно. Все активные сессии сотрудника завершаются.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import select

from core.config import settings
from core.db import dispose_engine, session_scope
from core.enums import AdminRole
from core.logging import configure_logging, get_logger
from core.models import AdminUser
from core.security import hash_password

log = get_logger("reset_admin")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Создать администратора или сбросить пароль")
    parser.add_argument("--login", default="owner", help="логин (по умолчанию owner)")
    parser.add_argument(
        "--role",
        default=AdminRole.OWNER.value,
        choices=[role.value for role in AdminRole],
        help="роль при создании новой учётки",
    )
    parser.add_argument("--reset-2fa", action="store_true", help="сбросить второй фактор")
    args = parser.parse_args()

    configure_logging("reset_admin")
    login = args.login.strip().lower()
    password = secrets.token_urlsafe(12)

    async with session_scope() as session:
        admin = await session.scalar(select(AdminUser).where(AdminUser.login == login))
        created = admin is None

        if admin is None:
            admin = AdminUser(
                login=login,
                password_hash=hash_password(password),
                full_name="Владелец" if args.role == AdminRole.OWNER.value else login,
                tg_id=settings.bot.owner_id or None,
                role=AdminRole(args.role),
                is_active=True,
            )
            session.add(admin)
        else:
            admin.password_hash = hash_password(password)
            admin.is_active = True

        # Снимаем блокировку по неудачным попыткам — иначе войти не получится.
        admin.failed_attempts = 0
        admin.locked_until = None

        if args.reset_2fa:
            admin.totp_enabled = False
            admin.totp_secret_enc = None

        await session.flush()
        admin_id = admin.id
        totp_enabled = admin.totp_enabled

    # Сессии живут в Redis и переживают смену пароля — гасим их явно.
    from api.admin.auth import destroy_sessions_of

    killed = await destroy_sessions_of(admin_id)
    await dispose_engine()

    action = "создана" if created else "обновлена"
    print(f"Учётная запись {action}: {login}")
    print(f"Пароль: {password}")
    print("Сохраните его — второй раз он не покажется.")
    if killed:
        print(f"Завершено активных сессий: {killed}")
    if totp_enabled:
        print("Второй фактор уже подключён — понадобится код из приложения.")
    else:
        print("При первом входе система попросит подключить второй фактор.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
