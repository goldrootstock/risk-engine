# risk-engine — project conventions

This file is read by people and by the AI assistant used on this project. The first part
is the project's own rules; the last section describes how AI assistance is used.

## 1. What this project is

Portfolio Risk & CCP Margin Engine in Python / PostgreSQL. Three modules, one repository:

| Module | Scope | Verification standard |
|---|---|---|
| **Risk** (`risk_engine.risk`, `risk_engine.backtest`) | Filtered historical simulation (EWMA/GARCH scaling), Expected Shortfall 97.5% (FRTB-style, stressed window) and VaR 99%, component / incremental attribution, factor exposure, stress scenarios | Kupiec POF, Christoffersen independence, Basel traffic light, PLA-style Spearman/KS test; results recorded per run in `risk_measures` and reported in an SR 11-7-style model document |
| **Margin** (`risk_engine.margin`) | VaR/ES-based initial margin in the style of SPAN 2 / IRM 2 (2-day MPOR, anti-procyclicality floors, stress blend, add-ons), legacy SPAN 16-scenario comparison, Cover-2 default fund | Margin coverage backtest against realised 2-day losses (CPMI-IOSCO 99% coverage), breach count and size |
| **Engineering** (`risk_engine.data`, API, dashboard) | PostgreSQL schema and migrations, re-runnable ETL, FastAPI endpoints, Streamlit dashboard, Docker Compose | CI green on every push: ruff, mypy, pytest against a real PostgreSQL; core-function coverage ≥ 70% |

Design decisions live in `docs/design/NN-*.md`, one note per topic, written before the code
and kept as the record of *why*. The notes are part of the deliverable.

## 2. Coding rules

- Python 3.12, `src/` layout, type hints on every public function, Google-style docstrings.
  `ruff check`, `ruff format --check`, `mypy` and `pytest` must pass locally before a commit.
- Tests: `pytest`; tests marked `db` need `DATABASE_URL` and run in a throwaway schema each.
  Coverage gate 70% on core modules once they exist.
- Database: PostgreSQL 17, psycopg 3, **no ORM**. Schema changes are plain-SQL migrations
  `db/migrations/NNNN_name.sql`, one transaction each, recorded in `schema_migrations`.
  Design intent is written into the DDL with `COMMENT ON`.
- Vocabularies (`measure`, `scope_type`, `method`) are enforced twice: DB constraints /
  catalogue table, and `risk_engine.data.vocab` StrEnums. A test asserts they match.
- `risk_runs.params` is JSONB for method-specific settings. Promotion rule: a key that
  appears in a `WHERE` clause twice becomes a column.
- Sign convention: VaR, ES, margin and realised losses are **positive numbers** in the run's
  base currency. The sign is flipped in exactly two places: the `pnl -> loss` conversion and
  the VaR/ES band drawn on a P&L chart. Details: `docs/design/01-data-layer-schema.md` §1-1.
- Units: every result measure has one unit fixed in `risk_measure_types`; fractions of NAV
  are derived, never stored.
- Commits: Conventional Commits with scopes `data`, `etl`, `risk`, `backtest`, `margin`,
  `ci`, `docs`. One commit = one decision unit.

### Local gate before every commit

- `make check` runs exactly what CI runs: `ruff check`, `ruff format --check`, `mypy`, `pytest`.
  It never modifies files. `make fmt` is the only write-mode command and is run on purpose,
  with the diff reviewed before committing.
- Optional check-only hook: `git config core.hooksPath .githooks` makes `pre-commit` run
  `make check` and block the commit on failure. It does not auto-fix anything; bypass with
  `git commit --no-verify` only when you know why.
- `ruff format` also formats fenced Python blocks in Markdown (design notes, README). This is
  intended: documented code must be real, formatted code. Blocks that are deliberate fragments
  are fenced as `text`, not `python`.
- `ruff` and `mypy` are pinned to exact versions in `pyproject.toml` so the local gate and CI
  cannot drift; bump them deliberately in their own commit.

## 3. Data policy

- Raw vendor data is never committed (`data/raw/` is git-ignored). The repository holds
  code, the instrument universe (`config/universe.csv`), derived aggregates (risk numbers,
  backtest statistics, charts, model documents) and small test fixtures.
- A source is used only if its terms allow automated collection and do not forbid publishing
  derived results, and if anyone can re-download it for free. Current sources: U.S. Treasury
  (par yield curve), ECB (euro reference rates), EIA (energy spot prices). Licence review and
  the reasoning: `docs/design/02-etl-and-data-sources.md`.
- ETL loads the **full available history** of every series. Sample windows and calendar
  alignment are parameters of the calculation, not of the load.
- Every published result cites the data sources and records the data snapshot date.

## 4. Terminology

- Prose is Korean with English technical terms in the design notes; code, comments, commit
  messages and this file are English.
- Every acronym is expanded on first use in a document, with the English full form and
  Korean meaning in parentheses, e.g. FK(Foreign Key, 외래키), DDL(Data Definition
  Language, 데이터 정의 언어). Abbreviation only from the second use.
- External facts (licence terms, data availability, job postings) carry a tag:
  [확인] read at the source, [추정] indirect evidence, [미확인] not found.

## 5. AI-assisted development

This project is built with an AI coding assistant (Claude Code) under an explicit contract:

| Who | Does |
|---|---|
| **Author (Juwon Kim)** | All core logic: ETL, return construction, FHS / ES / VaR, backtests, margin model. Reviews every design note and approves or amends it. |
| **AI assistant** | Design notes (with the open decisions listed for approval), scaffolding and infrastructure (project layout, CI, Docker, migrations, settings, test fixtures), code review of the author's modules, licence and data-availability research with sources. |
| **Both** | Review each other's work. The author's code is reviewed by the assistant; the assistant's notes and infrastructure are reviewed by the author. |

Process: design note → author's approval (recorded in the note's status line) → code.
Nothing goes into the schema or the model without an approved note. The author's professional
background is Java; the assistant explains Python idioms when they first appear so that the
author can defend every line in the repository.

The notes in `docs/design/` therefore read as a dialogue: proposals, counter-proposals and the
reasons a decision went one way. That is intentional.
