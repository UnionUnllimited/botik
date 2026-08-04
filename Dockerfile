# Один образ на все процессы: bot, api, worker, миграции.
# Различаются только командой запуска в docker-compose.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

FROM base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Каталог для конфига visitor-туннелей: docker переносит владельца из образа
# в пустой том, иначе воркер не сможет туда писать от пользователя app.
RUN mkdir -p /frpc && chown app:app /frpc

WORKDIR /app
COPY --chown=app:app . .

USER app

# Команду задаёт docker-compose: python -m bot | python -m api | python -m worker
CMD ["python", "-m", "api"]
