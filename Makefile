.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE      := docker compose
COMPOSE_DEV  := docker compose -f docker-compose.yml -f docker-compose.dev.yml
# На сервере Python не нужен — он требуется только для локальных тестов и линтера.
PY           := $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)

.PHONY: help
help: ## Список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- окружение -------------------------------------------------------------
.PHONY: env
env: ## Создать .env из шаблона и сгенерировать ключи
	@bash deploy/init-env.sh

.PHONY: secrets
secrets: ## Показать свежие ключи (для ручной вставки в .env)
	@printf 'SECURITY_SECRET_KEY=%s\n' "$$(head -c 36 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"
	@printf 'SECURITY_ENCRYPTION_KEY=%s\n' "$$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
	@printf 'BOT_WEBHOOK_SECRET=%s\n' "$$(head -c 24 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"

# --- прод ------------------------------------------------------------------
.PHONY: build
build: ## Собрать образ
	$(COMPOSE) build

.PHONY: up
up: ## Поднять весь стек
	$(COMPOSE) up -d

.PHONY: down
down: ## Остановить стек
	$(COMPOSE) down

.PHONY: restart
restart: ## Перезапустить приложения (без БД)
	$(COMPOSE) restart api bot worker

.PHONY: ps
ps: ## Статус контейнеров
	$(COMPOSE) ps

.PHONY: logs
logs: ## Логи всех сервисов (make logs s=bot — конкретного)
	$(COMPOSE) logs -f --tail=200 $(s)

.PHONY: deploy
deploy: ## Обновить код на сервере: сборка, миграции, перезапуск
	git pull --ff-only
	$(COMPOSE) build
	$(COMPOSE) run --rm migrate
	$(COMPOSE) up -d
	$(COMPOSE) ps

# --- разработка ------------------------------------------------------------
.PHONY: dev
dev: ## Локальный стек (бот в polling, порты наружу)
	$(COMPOSE_DEV) up -d --build

.PHONY: dev-logs
dev-logs: ## Логи локального стека
	$(COMPOSE_DEV) logs -f --tail=200 $(s)

.PHONY: dev-down
dev-down: ## Остановить локальный стек
	$(COMPOSE_DEV) down

# --- база ------------------------------------------------------------------
.PHONY: migrate
migrate: ## Накатить миграции
	$(COMPOSE) run --rm migrate

.PHONY: migration
migration: ## Создать миграцию: make migration m="add table"
	$(COMPOSE) run --rm --entrypoint alembic api revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Откатить последнюю миграцию
	$(COMPOSE) run --rm --entrypoint alembic api downgrade -1

.PHONY: psql
psql: ## Консоль psql
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-router_shop} -d $${POSTGRES_DB:-router_shop}

.PHONY: backup
backup: ## Резервная копия БД
	./deploy/backup.sh

# --- качество кода ---------------------------------------------------------
.PHONY: test
test: ## Прогнать тесты
	$(PY) -m pytest -q

.PHONY: cov
cov: ## Тесты с покрытием
	$(PY) -m pytest --cov --cov-report=term-missing

.PHONY: lint
lint: ## Проверка стиля и типов
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy core api bot worker

.PHONY: fmt
fmt: ## Форматирование
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .
