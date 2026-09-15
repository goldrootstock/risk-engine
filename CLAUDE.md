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
- **Checks and validations are read-only.** A function named check / validate / verify /
  backtest / status returns findings and never mutates data, files, the database or
  parameters; state changes live in separately named commands whose diff a human reviews.
  Rationale and the list of places this can break in P1: `docs/design/00-verification-is-read-only.md`.
- **Binding decisions live in the note body.** A migration `COMMENT`, a `CHECK` constraint, a
  catalogue row, a config-file comment, a docstring or a test expectation may *restate* a
  decision, but none of them may be its only record. When one of those constrains a later
  module (the 0002 `im_floor` description fixed the IM decomposition before any margin note
  existed), the constraint is written into the owning design note first, then coded. The
  2026-09-15 audit that produced this rule and its findings: `docs/design/01-data-layer-schema.md` §10-5.
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

### Changing a test's expectation

A commit that changes a test's expected value, tolerance, threshold or seed must say in its
message **why the previous expectation was wrong** (a mis-stated theorem, a tolerance
smaller than the effect being tested, a random draw that was a known false positive). A
commit that only changes the number is forbidden: "the test failed, so I changed the test"
has the same shape as the pattern design note 00 §3 warns about, and only the recorded
reason distinguishes a correction from a fit. Retroactive record of past cases:
`docs/decisions.md` §5.

### Exit codes and pipes

Commands are designed to never report success on failure (ETL exit 2 on any skipped
series; every CLI propagates exceptions). A shell pipe defeats this: `cmd | tail` returns
tail's exit code. `make` and the pre-commit hook run bash with `-o pipefail`; do not pipe a
command whose exit code matters, and never read "exit=0" off a pipeline.

### Reporting after any change to files or the repository

Every report that ends a task which wrote files or touched git closes with the verbatim
output of `git log --oneline -3` and `git status --short`, not a summary. Gate results
quote the last line of `pytest` as printed. The author works across several windows; the
raw output is what lets them re-synchronise.

## 3. Data policy

- Raw vendor data is never committed (`data/raw/` is git-ignored). The repository holds
  code, the instrument universe (`config/universe.csv`), derived aggregates (risk numbers,
  backtest statistics, charts, model documents) and small test fixtures.
- A source is used only if its terms allow automated collection and do not forbid publishing
  derived results, and if anyone can re-download it for free. Current sources: FRED (H.15
  constant-maturity Treasury yields), ECB (euro reference rates), EIA (energy spot prices). Licence review and
  the reasoning: `docs/design/02-etl-and-data-sources.md`.
- ETL loads the **full available history** of every series. Sample windows and calendar
  alignment are parameters of the calculation, not of the load.
- Every published result cites the data sources and records the data snapshot date.
- Two sample sets coexist (`from_1999` for backtests, `default` for factor analysis): every
  reported number names its set, and queries on `risk_runs` / `backtest_results` filter on it.

## 4. Terminology

- Prose is Korean with English technical terms in the design notes; code, comments, commit
  messages and this file are English.
- Every acronym is expanded on first use in a document, with the English full form and
  Korean meaning in parentheses, e.g. FK(Foreign Key, 외래키), DDL(Data Definition
  Language, 데이터 정의 언어). Abbreviation only from the second use.
- External facts (licence terms, data availability, job postings) carry a tag:
  [확인] read at the source, [추정] indirect evidence, [미확인] not found. A [확인] tag
  states the date and how it was checked, e.g. `[확인 2026-09-13: HTTP 200, text/xml;
  .zip/.csv 404]`, so that a wrong claim can be traced to its method.

## 5. AI-assisted development

This project is built with an AI coding assistant (Claude Code) under an explicit contract:

| Who | Does |
|---|---|
| **Author (Juwon Kim)** | All core logic: ETL, return construction, FHS / ES / VaR, backtests, margin model. Reviews every design note and approves or amends it. |
| **AI assistant** | Design notes (with the open decisions listed for approval), scaffolding and infrastructure (project layout, CI, Docker, migrations, settings, test fixtures), code review of the author's modules, licence and data-availability research with sources. |
| **Both** | Review each other's work. The author's code is reviewed by the assistant; the assistant's notes and infrastructure are reviewed by the author. |

Process: design note → author's approval (recorded in the note's status line) → code.
Nothing goes into the schema or the model without an approved note.

**Mode change (2026-09-13).** From the ETL onwards the assistant implements P1-Risk end to
end (ETL, return builder, FHS/parametric/MC ES-VaR, backtests, stress, model document,
dashboard/API) and commits per module without per-step approval. The author's role becomes
review and study: every module is accompanied by `docs/decisions.md` (each parameter with
its source, rejected alternatives and sensitivity), `docs/walkthrough.md` (what the code
does, why, where it can be wrong) and interview questions with model answers. The assistant
still stops for schema changes, conflicts with an approved design decision, and data-licence
questions. Parameters follow industry references (RiskMetrics, Basel, CPMI-IOSCO, published
papers); a value without a source is labelled as arbitrary with its range. Results are never
tuned after the fact: a failing backtest is reported as failing.

The four-step pairing procedure below remains the reference for any module the author
chooses to write themselves:

1. The assistant adds the function signatures, docstrings and tests first. Bodies are left
   empty (`raise NotImplementedError`), so the tests fail.
2. The author fills in the bodies until the tests pass.
3. The assistant reviews the diff and points out Python idioms the author may not know,
   with the Java equivalent where one exists.
4. When the module is done, the author explains in their own words what it does and what it
   assumes; the assistant names what is missing from that explanation. The explanation is
   the interview answer, so it has to stand on its own. The author's professional
background is Java; the assistant explains Python idioms when they first appear so that the
author can defend every line in the repository.

The notes in `docs/design/` therefore read as a dialogue: proposals, counter-proposals and the
reasons a decision went one way. That is intentional.
