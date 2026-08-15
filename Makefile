# Единая точка входа для операций проекта.
# Зависимости ставятся только через uv — не pip и не poetry.

.DEFAULT_GOAL := help

PROJECT_NAME := max-ollama-bot
DOCKER_IMAGE := $(PROJECT_NAME):latest

.PHONY: help install run lint format test check migrate migration \
        build up-local down-local logs shell \
        clean

help: ## Показать список целей
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Разработка ---------------------------------------------------------------

install: ## Установить зависимости (uv sync)
	uv sync

run: ## Запустить бота локально
	uv run python -m bot.main

lint: ## Проверить код (ruff + mypy)
	uv run ruff check src/
	uv run mypy src/

format: ## Отформатировать код (black + ruff --fix)
	uv run black src/
	uv run ruff check --fix src/

test: ## Прогнать тесты с покрытием
	uv run pytest --cov=src/bot --cov-report=term-missing

check: lint test ## Полная проверка перед коммитом

# --- База данных ----------------------------------------------------------------

migrate: ## Применить миграции
	uv run alembic upgrade head

migration: ## Создать ревизию: make migration m="описание"
	@test -n "$(m)" || { echo "Укажите описание: make migration m=\"добавил users\""; exit 1; }
	uv run alembic revision --autogenerate -m "$(m)"

# --- Docker -----------------------------------------------------------------------

build: ## Собрать Docker-образ
	docker compose build

up-local: ## Поднять бота в Docker
	docker compose up -d

down-local: ## Остановить бота
	docker compose down

logs: ## Логи контейнера (follow)
	docker compose logs -f bot

shell: ## Shell внутри контейнера
	docker compose exec bot /bin/bash

# --- Прочее -------------------------------------------------------------------------

clean: ## Удалить кэши и временные артефакты
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
