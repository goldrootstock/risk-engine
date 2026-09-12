# Local gate — identical to the CI job. Run before every commit: `make check`.
# Nothing here modifies files; `make fmt` is the only write-mode target and is explicit.

.PHONY: check lint typecheck test fmt db migrate

PY ?= .venv/bin

check: lint typecheck test

lint:
	$(PY)/ruff check .
	$(PY)/ruff format --check .

typecheck:
	$(PY)/mypy

test:
	$(PY)/pytest -q

fmt:            ## write-mode formatter — review the diff before committing
	$(PY)/ruff format .

db:             ## start local PostgreSQL
	docker compose up -d

migrate:
	$(PY)/python -m risk_engine.data.migrate
