UV ?= uv
PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
M5_SOURCE_DIR ?= data/raw

.PHONY: bootstrap check lint format test smoke seed budget data simulate forecast-validation controls api db-up db-down local-setup local-db-stop approval-demo executor simulator-worker monitor scheduler investigator episode

bootstrap:
	$(UV) sync --frozen

check: lint test smoke

lint:
	$(RUFF) check src tests
	$(RUFF) format --check src tests

format:
	$(RUFF) format src tests

test:
	$(PYTHON) -m pytest

smoke:
	$(PYTHON) -m ecommerce_agent.cli smoke

seed:
	$(PYTHON) -m ecommerce_agent.cli seed --output var/fixture

budget:
	$(PYTHON) -m ecommerce_agent.cli budget

data:
	$(PYTHON) -m ecommerce_agent.cli prepare-data --source-dir "$(M5_SOURCE_DIR)" --output var/data/m5

simulate:
	$(PYTHON) -m ecommerce_agent.cli simulate

forecast-validation:
	$(PYTHON) -m ecommerce_agent.cli forecast-validation

controls:
	$(PYTHON) -m ecommerce_agent.cli compare-controls

api:
	$(PYTHON) -m uvicorn ecommerce_agent.api:app --env-file var/runtime/api.env --host 127.0.0.1 --port 8000

local-setup:
	$(PYTHON) -m ecommerce_agent.cli local-setup

local-db-stop:
	$(PYTHON) -m ecommerce_agent.cli local-db-stop

approval-demo:
	$(PYTHON) -m ecommerce_agent.cli approval-demo

executor:
	$(PYTHON) -m ecommerce_agent.cli worker --kind executor --credential-file var/runtime/executor_0.json

simulator-worker:
	$(PYTHON) -m ecommerce_agent.cli worker --kind simulator --credential-file var/runtime/simulator_0.json

monitor:
	$(PYTHON) -m ecommerce_agent.cli monitor --credential-file var/runtime/monitor_0.json

scheduler:
	$(PYTHON) -m ecommerce_agent.cli schedule --credential-file var/runtime/scheduler_0.json

investigator:
	$(PYTHON) -m ecommerce_agent.cli investigate-worker --credential-file var/runtime/agent_0.json

episode:
	$(PYTHON) -m ecommerce_agent.cli episode --check-offline

db-up:
	docker compose --env-file .env -f infra/compose.yaml up -d --wait

db-down:
	docker compose --env-file .env -f infra/compose.yaml down
