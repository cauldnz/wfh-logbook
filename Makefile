# WFH Logbook — common dev tasks.
# Use `make <target>` (or `make help` to list targets).
# Inside the devcontainer, uv installs into the system Python. On the host,
# create a `.venv` with `uv venv` and uv operations use it automatically.

.PHONY: help dev test lint format typecheck migrate revision \
        docker-build docker-up docker-down export-xlsx clean

PY ?= .venv/Scripts/python
ifeq ($(OS),Windows_NT)
  PY := .venv/Scripts/python
else
  PY := .venv/bin/python
endif

# Engine: use podman compose on Chris's host (Podman in Docker-compat mode);
# override with `make docker-up COMPOSE=docker` if running with Docker Desktop.
COMPOSE ?= podman compose

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

dev: ## Run the FastAPI dev server with autoreload on port 8088.
	$(PY) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8088

test: ## Run the test suite.
	$(PY) -m pytest tests/

lint: ## Run ruff lint + format checks.
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: ## Apply ruff format fixes.
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck: ## Run mypy --strict on the app package.
	$(PY) -m mypy app/

migrate: ## Apply Alembic migrations to head.
	$(PY) -m alembic upgrade head

revision: ## Create a new Alembic revision (autogenerate). Use MSG="..." to name it.
	@if [ -z "$(MSG)" ]; then echo 'Usage: make revision MSG="describe the change"'; exit 1; fi
	$(PY) -m alembic revision --autogenerate -m "$(MSG)"

docker-build: ## Build the runtime image.
	$(COMPOSE) build app

docker-up: ## Bring up the stack (app only; pass --profile tunnel for cloudflared).
	$(COMPOSE) up -d

docker-down: ## Stop the stack.
	$(COMPOSE) down

export-xlsx: ## Export FY=<label> XLSX to OUT=<path>. Example: make export-xlsx FY=2025-26 OUT=/tmp/wfh.xlsx
	@if [ -z "$(FY)" ] || [ -z "$(OUT)" ]; then echo 'Usage: make export-xlsx FY=2025-26 OUT=/tmp/wfh.xlsx'; exit 1; fi
	$(PY) -m app.exporters --fy "$(FY)" --out "$(OUT)"

nas-status: ## Health + container status of the NAS deployment (ssh alias 'unraid').
	@echo "--- container ---"
	@ssh unraid "docker ps --filter name=wfh-logbook --format '{{.Status}}'; docker inspect wfh-logbook --format 'health={{.State.Health.Status}} restarts={{.RestartCount}}'"
	@echo "--- app health ---"
	@curl -s -m 8 http://wtrmax.local:8088/api/health
	@echo ""
	@echo "--- recent poller ---"
	@ssh unraid "docker logs wfh-logbook 2>&1 | grep -E 'poller: (ok|cycle failed|authentication)' | tail -3"

clean: ## Remove caches and build artefacts.
	@rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
