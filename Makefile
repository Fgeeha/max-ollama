.PHONY: help install dev lint format test check build run stop logs shell clean

PROJECT_NAME = max-ollama-bot
DOCKER_IMAGE = $(PROJECT_NAME):latest
DOCKER_CONTAINER = $(PROJECT_NAME)-container

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Development

install: ## Install dependencies with uv
	uv sync

dev: ## Run bot locally
	uv run python -m bot.main

lint: ## Run linting checks
	uv run ruff check src/
	uv run mypy src/

format: ## Format code
	uv run black src/
	uv run ruff check --fix src/

test: ## Run tests with coverage
	uv run pytest --cov=src/bot --cov-report=term-missing

check: lint test ## Run all checks

##@ Docker

build: ## Build Docker image
	docker compose build

run: ## Start the bot
	docker compose up -d

stop: ## Stop the bot
	docker compose down

logs: ## Follow container logs
	docker compose logs -f bot

shell: ## Open a shell in the container
	docker compose exec bot /bin/bash

##@ Maintenance

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
