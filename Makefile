# Local gate — identical to the CI job. Run before every commit: `make check`.
# bash with -o pipefail: a failing command inside a pipe fails the target (a plain shell
# pipe reports the exit code of the LAST command and hid a crashed backfill on 2026-09-13).
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
# Nothing here modifies files; `make fmt` is the only write-mode target and is explicit.

.PHONY: check lint typecheck test fmt db migrate api dashboard

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

api:            ## read-only FastAPI on :8000 (design note 08)
	$(PY)/uvicorn risk_engine.app.api:app --port 8000

dashboard:      ## read-only Streamlit dashboard on :8501
	$(PY)/streamlit run src/risk_engine/app/dashboard.py
