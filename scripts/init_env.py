"""Создаёт .env из .env.example и подставляет сгенерированные секреты.

Запуск: python scripts/init_env.py  (или make env)
Существующий .env не трогает — секреты не перезатираются случайно.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
EXAMPLE_FILE = ROOT / ".env.example"

GENERATORS = {
    "SECURITY_SECRET_KEY": lambda: secrets.token_urlsafe(48),
    "SECURITY_ENCRYPTION_KEY": lambda: base64.b64encode(os.urandom(32)).decode("ascii"),
    "BOT_WEBHOOK_SECRET": lambda: secrets.token_urlsafe(32),
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
}


def main() -> int:
    if ENV_FILE.exists():
        print(".env уже существует — оставляю как есть")
        return 0
    if not EXAMPLE_FILE.exists():
        print("Не найден .env.example", file=sys.stderr)
        return 1

    lines = EXAMPLE_FILE.read_text(encoding="utf-8").splitlines()
    filled: list[str] = []
    generated: list[str] = []
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not value.strip() and key.strip() in GENERATORS:
            name = key.strip()
            filled.append(f"{name}={GENERATORS[name]()}")
            generated.append(name)
        else:
            filled.append(line)

    ENV_FILE.write_text("\n".join(filled) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(f"Создан .env, сгенерированы: {', '.join(generated)}")
    print("Заполните вручную: BOT_TOKEN, APP_BOT_USERNAME, BOT_OWNER_ID, домены, реквизиты оплаты.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
