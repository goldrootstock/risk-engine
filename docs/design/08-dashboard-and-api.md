# 08 — 대시보드와 API (읽기 전용 서빙 계층)

- 상태: 구현 (2026-09-13, mode change 이후 — 저자 승인 없이 구현, 사후 리뷰 대상)
- 선행 노트: 00 (검증은 읽기 전용), 01 (스키마), 05·06·07 (계산 결과의 형태)
- 범위: P1-Risk MVP 의 마지막 조각. FastAPI(파이썬 웹 프레임워크) 로 결과를 JSON 으로 내고,
  Streamlit(파이썬 데이터 앱 프레임워크) 로 같은 결과를 화면에 그린다. **계산은 하지 않는다.**

## 1. 한 문장 원칙

서빙 계층은 DB 에 이미 기록된 결과를 읽어서 보여 줄 뿐이고, 어떤 경로로도 쓰지 못한다.
계산과 기록은 CLI(`python -m risk_engine.risk|backtest`) 만 한다.

이유는 노트 00 §3 과 같다. 대시보드에 "다시 계산" 버튼이 생기는 순간 파라미터를 바꿔 가며
결과를 고르는 경로가 열린다. API 에 POST 가 생기면 어떤 결과가 어떤 코드·설정으로 나왔는지의
추적(`risk_runs.params`, `code_version`)이 끊긴다. 그래서 두 가지를 구조로 막는다.

| 장치 | 내용 |
|---|---|
| 연결 옵션 | API 와 대시보드가 여는 모든 연결은 `default_transaction_read_only = on` 으로 연다. 코드에 INSERT 가 섞여 들어와도 PostgreSQL 이 `ReadOnlySqlTransaction` 으로 거부한다. 테스트가 이를 실제로 확인한다 (`tests/test_app_api.py::test_connection_is_read_only`). |
| HTTP 메서드 | GET 만 정의한다. 라우터에 POST/PUT/DELETE 가 없다는 것을 테스트가 라우트 목록으로 확인한다. |

## 2. 공용 질의 모듈 `risk_engine.app.queries`

API 와 대시보드가 **같은 함수** 를 부른다. 두 화면의 숫자가 서로 다르면 원인이 SQL 두 벌인
경우가 가장 흔하므로 SQL 은 한 벌만 둔다. 함수는 psycopg 연결을 받아 JSON 으로 바로 직렬화되는
값(문자열 날짜, float, dict, list)을 돌려준다. 판다스는 대시보드 쪽에서만 만든다.

| 함수 | 읽는 테이블 | 돌려주는 것 |
|---|---|---|
| `catalog` | `risk_runs` | (universe, portfolio, tag, method) 조합별 건수와 기간 — 화면 선택기의 원천 |
| `headline_series` | `risk_runs` ⋈ `risk_measures` | 날짜별 VaR 99 / ES 97.5 / stressed ES / portfolio_value |
| `latest_run` | 같음 + `component_es` | 기준일 이하 최신 run 의 헤더·포트폴리오 측정값·종목별 component ES |
| `backtest_latest` | `backtest_summaries`, `backtest_results` | 최신 배치(같은 `created_at`)의 창 표 + 전체 합계(정식·raw·√h) |
| `backtest_days` | `backtest_results` | 일별 손익·VaR·초과 여부(정식·raw·√h)·h·귀속 |
| `stress_latest` | `stress_runs`, `stress_results` | 기준일 이하 최신 스트레스 실행의 헤더와 시나리오별 손실 |

"최신 배치" 정의: 백테스트 기록은 append-only 이므로(노트 06) 같은 universe·portfolio 에 창 표가
여러 벌 쌓인다. 한 번의 `record.write` 는 한 트랜잭션이고 PostgreSQL 의 `now()` 는 트랜잭션
시작 시각이므로 같은 배치의 행은 `created_at` 이 정확히 같다. 최대 `created_at` 을 가진 행들이
최신 배치다. 이 정의는 쿼리에 주석으로 남긴다.

## 3. API

`uvicorn risk_engine.app.api:app`. 모든 엔드포인트는 GET, 인증 없음(로컬·데모 용도, §6).

| 경로 | 질의 | 비고 |
|---|---|---|
| `GET /health` | `SELECT 1` | DB 연결 확인 |
| `GET /catalog` | `catalog` | |
| `GET /es`, `GET /var` | `latest_run` | `universe`, `portfolio`, `method`(기본 fhs), `as_of`(기본: 최신). 값·신뢰수준·run_id·기준일·params sha 를 돌려준다 |
| `GET /runs/latest` | `latest_run` | 위 둘의 원천: 측정값 전체와 component ES |
| `GET /series` | `headline_series` | `start`, `end` 선택 |
| `GET /backtest` | `backtest_latest` | |
| `GET /backtest/days` | `backtest_days` | `exceptions_only=true` 로 초과일만 |
| `GET /stress` | `stress_latest` | |
| `GET /margin` | `margin_latest` | v1.2 (노트 09): `tag`(기본 margin_batch)·`horizon_days`(기본 2) 양성 선택. IM 분해·add-on |
| `GET /margin/coverage`, `GET /margin/coverage/days` | `coverage_latest`, `coverage_days` | 최신 배치(같은 `created_at`), 세 잣대의 breach |
| `GET /default-fund` | `default_fund_latest` | universe 기준 최신 Cover-N 실행과 (시나리오 × 회원) 전표 |

**실행 계열의 양성 선택 (2026-09-15, JK 승인 1).** `latest_run`·`headline_series`·`backtest.runner`
는 실행을 `(universe, portfolio, tag, method, horizon_days)` 로 **명시적으로 고른다**. `horizon_days`
기본값은 1 이고 배제 조건(`!= 2`)이 아니라 선택 조건이라, 다음 모듈이 또 다른 지평의 실행을
`risk_runs` 에 넣어도 1일 독자는 깨지지 않는다. `/catalog` 도 `horizon_days` 를 돌려준다.
필터가 아니라 **테스트가 안전장치다**: `tests/test_app_api.py::test_readers_ignore_margin_shaped_runs`
와 `tests/test_backtest_db.py` 가 마진 모양(2일, `margin_batch`)의 실행을 한 건 넣고 세 독자가
그것을 무시함을 단언한다. 마진 계열은 `tag='margin_batch', horizon_days=2` 로 물어야 보인다.

응답에 `run_id`·`params_sha256`·`code_version` 을 항상 실어 보낸다. 숫자만 나가면 어느 설정의
숫자인지 알 수 없다(노트 00 §3 의 추적 가능성).

연결은 요청마다 연다(`Depends(get_conn)`). 커넥션 풀은 데모 트래픽에 과하고, 테스트에서
의존성 주입으로 던져 버릴 스키마의 연결을 넣기 쉽다. 404 는 "그 조건의 run 이 없다" 한 가지다.

## 4. 대시보드

`streamlit run src/risk_engine/app/dashboard.py`. 사이드바에서 universe·portfolio·tag 를 고른다
(`catalog` 에서). 본문 네 구역:

1. **헤드라인** — 최신 run 의 ES 97.5, VaR 99, stressed ES, 포트폴리오 가치와 ES/VaR 비율.
   종목별 component ES 막대.
2. **시계열** — ES·VaR 선과 백테스트 초과일 표시. 그 아래 손익 차트: HPL 점과 −VaR 띠.
   **부호를 뒤집는 두 곳 중 두 번째** 가 여기다(CLAUDE.md §2 sign convention: `pnl → loss` 변환과
   P&L 차트의 VaR 띠). 코드에 그 문장을 주석으로 둔다.
3. **백테스트** — 최신 배치의 창 표(신호등·Kupiec·Christoffersen·PLA)와 전체 합계를
   정식·raw·√h 로 나란히. 어느 것이 정식인지 `params.horizon_method` 로 표시한다.
4. **스트레스** — 최신 스트레스 실행의 시나리오별 손실을 ES 배수로, 종류(historical /
   hypothetical / correlation / vol_lag) 별 색.

차트는 Altair(Streamlit 이 이미 의존하는 선언형 차트 라이브러리) 로 그린다. 추가 의존성 없음.
질의는 `st.cache_data(ttl=60)` 로 60 초 캐시 — 기록은 CLI 가 하므로 화면이 1 분 늦어도 문제가 없다.

## 5. 테스트

| 테스트 | 무엇을 보장하나 |
|---|---|
| `test_app_api.py` (db) | 던져 버릴 스키마에 run·measure·backtest·stress 행을 직접 넣고 FastAPI `TestClient` 로 모든 GET 을 호출해 값이 왕복하는지, 최신 배치 선택이 맞는지 |
| `test_connection_is_read_only` (db) | `get_conn` 이 연 연결로 INSERT 를 시도하면 `ReadOnlySqlTransaction` |
| `test_only_get_routes` | 라우트 메서드 집합이 `{GET}` 뿐 |
| `test_dashboard_smoke` (db) | `streamlit.testing.v1.AppTest` 로 대시보드를 같은 스키마에 대해 한 번 렌더링, 예외 없음 |

의존성은 `pip install -e ".[dev,app]"`. CI 도 `app` 을 설치한다(대시보드 스모크 포함).

## 6. 일부러 뺀 것

- 인증·HTTPS·CORS: 로컬 데모. 공개 배포 시의 첫 작업이며 이 노트의 범위 밖.
- 결과 재계산·파라미터 입력 UI: §1.
- 캐시 서버·풀: 트래픽이 없다. 필요해지면 `psycopg_pool` 한 줄.
- 마진(P1-Margin) 화면: 테이블이 아직 없다. `im_*` 측정값이 기록되면 `latest_run` 이 그대로 보여 준다.
  **보강(2026-09-15)**: 단, `latest_run` 은 `horizon_days` 를 거르지 않으므로 마진 실행이 같은
  universe·portfolio·method 로 들어오면 `tag=None` 기본에서 2일 MPOR 실행이 1일 리스크 실행을
  이길 수 있다. 노트 01 §10-2 의 (a) — 마진 `tag='margin_batch'` + A1 독자 세 곳에
  `horizon_days = 1` 추가 — 가 A2 의 첫 코드 변경이고, `/margin` 엔드포인트와 대시보드 마진
  구역은 마진 노트(09)에서.
