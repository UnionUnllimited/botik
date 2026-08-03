"""Точка входа API: python -m api"""

from __future__ import annotations

import uvicorn

from core.config import settings
from core.logging import configure_logging


def main() -> None:
    configure_logging("api")
    uvicorn.run(
        "api.main:app",
        host=settings.api.host,
        port=settings.api.port,
        workers=settings.api.workers if settings.app.is_prod else 1,
        reload=not settings.app.is_prod and settings.app.debug,
        log_config=None,  # логирование уже настроено structlog
        access_log=False,  # доступ логируем своим middleware
        proxy_headers=True,
        forwarded_allow_ips="*",  # доверяем только внутренней сети docker/nginx
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
