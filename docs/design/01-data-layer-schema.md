# 설계 노트 01 — 데이터층 스키마 (instruments · prices · positions · risk_runs)

- 상태: 결정 1~5 **전부 승인**(2026-09-12). `0001_init.sql`(instruments·prices·positions) · `0002_risk_runs.sql`(risk_measure_types·risk_runs·risk_measures·v_risk_headline) 코드화 완료. 단위·인덱스·어휘 강제는 §8-4~8-6
- 기준일: 2026-09-12 · 대상: P1-Risk 1주차 (Gap plan v2 §2 · §6)
- 범위: 리스크 엔진이 읽고 쓰는 최소 4개 테이블. `backtest_results` 는 Kupiec/Christoffersen 코드가 생기는 2주차에 별도 노트로.

---

## 0. 한 줄 요약

DB에는 **사실(fact)과 실행 기록(run record)만** 넣는다. 파생값(수익률·시가총액·리스크 기여)은 코드가 매번 계산한다.
예외는 `risk_runs` — 계산 비용이 크고, 대시보드 추이·백테스트 조인에 시계열로 필요하므로 결과를 저장한다.

## 1. 설계 원칙

| 원칙 | 내용 | 이유 |
|---|---|---|
| 원본 그대로 저장 | `prices` 는 벤더 값을 가공 없이 저장. 결측 보간·달력 정렬 없음 | ETL이 "무엇을 받았는가"를 보존해야 재실행·감사가 가능. 정렬은 수익률 행렬을 만드는 코드의 책임 |
| 파생값 비저장 | 수익률·NAV·팩터 노출 테이블 없음 | 파생값을 저장하면 원본 갱신 시 불일치가 생긴다. 계산은 수 ms |
| 멱등(idempotent) ETL | 모든 적재는 `INSERT … ON CONFLICT DO UPDATE` | 같은 날짜를 두 번 돌려도 결과가 같아야 "재실행 가능한 ETL"(Gap plan 데이터층 요건) |
| 자연키 vs 대리키 | 시계열·스냅샷 테이블은 복합 자연키(PK), 마스터·이벤트 테이블은 대리키(`IDENTITY`) | 시계열은 (id, date)가 곧 조회 패턴이자 유일성. 마스터는 티커가 바뀔 수 있어 안정적 FK가 필요 |
| 타입 규칙 | 측정값(가격·리스크 수치) = `DOUBLE PRECISION`, 장부값(수량·승수) = `NUMERIC` | §5 참고. Python 쪽 `Decimal` vs `float` 문제와 직결 |
| 이름 규칙 | 테이블 복수형·snake_case. `*_date` = `DATE`, `*_at` = `TIMESTAMPTZ` | 컬럼 이름만 보고 타입을 알 수 있게 |
| 손실 부호 | VaR·ES는 **손실을 양수**로 저장 (기준통화 금액) | 규제 문서·백테스트 관례. "VaR 초과" = 실현손실 > VaR 로 단순 비교 |

### 1-1. 손실 부호 규약 (승인, 2026-09-12)

| 계층 | 부호 | 근거 |
|---|---|---|
| 시나리오 P&L (엔진 내부 중간값) | P&L 부호 그대로 (이익 +, 손실 −) | 가격 변화에서 바로 나오는 값. 여기까지는 "P&L" |
| **손실 분포 → VaR·ES** | **손실 = 양수** | 뒤집는 지점은 단 한 곳: 시나리오 P&L 벡터를 손실 벡터로 바꾸는 함수 (`loss = -pnl`). 이 함수 아래로는 모두 손실 양수 |
| DB (`risk_measures.value`, 백테스트의 realized loss) | 손실 양수 | 백테스트 초과 판정이 `realized_loss > var` 한 줄 |
| API 응답 · 대시보드 · 리포트 | 손실 양수, 라벨로 의미 표시 ("VaR 99% (1d): 1.23M USD") | 규제 보고 관례. "−1.23M" 표기는 하지 않는다 |
| P&L 차트 | P&L 부호 (손실 −) | P&L 차트는 P&L 계열을 읽지 VaR 을 읽지 않는다. VaR 밴드를 P&L 차트에 겹칠 때만 `-var` 로 그린다 — 이것이 프레젠테이션에서 뒤집는 유일한 지점 |

규칙: **부호를 뒤집는 코드는 두 곳뿐** — (1) `pnl → loss` 변환 함수, (2) P&L 차트에 VaR/ES 밴드를 겹칠 때. 그 외에서 `-` 를 붙이면 버그로 본다.

## 2. 테이블 정의

### 2-1. `instruments` — 상품 마스터

```sql
CREATE TABLE instruments (
    instrument_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT          NOT NULL,   -- 'yfinance' | 'fred' | 'stooq' …
    ticker          TEXT          NOT NULL,   -- 소스 기준 심볼 ('SPY', 'GC=F', 'EURUSD=X')
    name            TEXT          NOT NULL,
    asset_class     TEXT          NOT NULL CHECK (asset_class IN ('equity', 'rates', 'fx', 'commodity')),
    instrument_type TEXT          NOT NULL CHECK (instrument_type IN ('stock', 'etf', 'future', 'fx_spot', 'index')),
    quote_type      TEXT          NOT NULL DEFAULT 'price' CHECK (quote_type IN ('price', 'yield')),
    currency        CHAR(3)       NOT NULL,   -- 호가 통화 (ISO 4217)
    multiplier      NUMERIC(18,6) NOT NULL DEFAULT 1 CHECK (multiplier > 0),
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (source, ticker)
);
```

| 컬럼 | 왜 |
|---|---|
| `instrument_id` 대리키 | 티커는 바뀌고(FB→META) 벤더마다 다르다(`GC=F` vs `GC1!`). FK가 티커에 묶이면 마스터 수정이 전 테이블로 번진다 |
| `(source, ticker)` UNIQUE | 같은 티커라도 소스가 다르면 다른 시계열. 소스 교체(예: yfinance→stooq) 시 병존 가능 |
| `asset_class` 4종 | Gap plan 의 팩터 그룹(시장·금리·FX·커머디티)과 1:1. 리스크 기여를 "자산군별"로 묶는 기준. `CHECK` 로 오타 방지 |
| `instrument_type` | **P1-Margin 을 위한 선투자.** 현물 vs 선물 cross-margining(Crypto.com JD "offsetting spot against futures")과 선물 롤 처리는 상품 유형을 알아야 한다. 지금 넣지 않으면 5주차에 마이그레이션 |
| `quote_type` | 채권 ETF(TLT·IEF)는 가격이지만, 나중에 FRED 국채 수익률을 넣으면 수익률 계산이 달라진다(가격 → 로그수익률, 수익률 → bp 차분). 수익률 행렬 빌더가 이 컬럼으로 분기 |
| `currency` | 비USD 상품의 P&L 을 기준통화로 바꿀 때 필요. 환율 자체는 `asset_class='fx'` 인 상품으로 `prices` 에 들어간다 — 별도 FX 테이블 없음 |
| `multiplier` NUMERIC | 선물 계약 승수(GC=100oz, ES=50). P&L = qty × multiplier × Δprice. 장부값이므로 정확한 십진수 |
| `is_active` | 상장폐지·롤오프 상품을 지우지 않고 유니버스에서만 제외. 과거 `risk_runs` 의 FK 무결성 유지 |

### 2-2. `prices` — 일별 종가 시계열

```sql
CREATE TABLE prices (
    instrument_id BIGINT           NOT NULL REFERENCES instruments (instrument_id),
    price_date    DATE             NOT NULL,
    close         DOUBLE PRECISION NOT NULL,   -- 미조정 종가 (포지션 평가용)
    adj_close     DOUBLE PRECISION NOT NULL,   -- 배당·분할 조정 종가 (수익률용)
    volume        BIGINT,                      -- FX 는 NULL. 마진 유동성 add-on 에서 ADV 로 사용
    loaded_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, price_date)
);
CREATE INDEX prices_price_date_idx ON prices (price_date);
```

| 컬럼·제약 | 왜 |
|---|---|
| PK `(instrument_id, price_date)` | (1) 유일성 = "상품당 하루 한 값". (2) 이 인덱스가 곧 "상품 하나의 N일 시계열" 조회 경로. (3) `ON CONFLICT (instrument_id, price_date) DO UPDATE` 의 충돌 키 |
| `price_date` 보조 인덱스 | 수익률 행렬은 "날짜 하나의 전 상품 단면"을 모아 만든다. PK 는 instrument 선행이라 이 패턴을 못 탄다 |
| `close` 와 `adj_close` 둘 다 | 포지션 평가액은 실제 거래 가격(`close`), 수익률은 배당·분할 왜곡이 없는 `adj_close`. 하나만 두면 둘 중 하나가 틀린다 |
| `DOUBLE PRECISION` | 벤더가 주는 값이 이미 float64. `NUMERIC` 은 정밀도 착시이고 Python 에서 `Decimal` 로 돌아와 매번 캐스팅해야 한다 (§5) |
| `volume` BIGINT nullable | FX·지수는 거래량이 없다. NOT NULL 로 두면 0 을 넣게 되고 ADV 계산이 오염된다 |
| OHLC 미포함 | 계획된 어떤 모듈도 고가·저가를 쓰지 않는다. 필요해지면 `ALTER TABLE ADD COLUMN` 한 줄 |
| 가격 > 0 CHECK 없음 | `quote_type='yield'` 상품은 음수 가능(JGB). 유효성 검사는 ETL 이 `quote_type` 을 알고 수행 |
| `loaded_at` | 감사 추적: 어느 값이 언제 적재·갱신됐는지. 벤더가 과거 값을 수정하는 경우(yfinance 는 흔함) 추적 근거 |

### 2-3. `positions` — 포지션 스냅샷

```sql
CREATE TABLE positions (
    portfolio_code TEXT          NOT NULL,   -- 'MAIN', 'HEDGED_A' …
    as_of_date     DATE          NOT NULL,   -- 이 날짜 종가 기준 보유량
    instrument_id  BIGINT        NOT NULL REFERENCES instruments (instrument_id),
    quantity       NUMERIC(20,6) NOT NULL CHECK (quantity <> 0),   -- 음수 = 숏
    loaded_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_code, as_of_date, instrument_id)
);
```

| 결정 | 채택 | 대안 | 이유 |
|---|---|---|---|
| 스냅샷 vs 거래 원장(trade ledger) | **스냅샷** | 체결 테이블 + 누적합 | 리스크 엔진이 필요한 건 "날짜 D 의 보유량"뿐. 원장은 체결·정정·수수료라는 별도 도메인을 끌고 오는데 타깃 공고 어디도 요구하지 않는다 |
| 스냅샷 시맨틱 | 날짜 D 의 실행은 `as_of_date <= D` 중 **가장 최근** 스냅샷 사용 | 매일 스냅샷 강제 | 포지션이 안 바뀌면 행을 복제할 이유가 없다. 실행이 실제로 쓴 날짜는 `risk_runs.positions_as_of` 에 기록해 재현성 확보 |
| `portfolio_code` TEXT (portfolios 테이블 없음) | **1주차는 코드만** | `portfolios(portfolio_id, name, base_currency)` | 지금 포트폴리오 메타는 기준통화 하나인데 전부 USD 로 고정. 메타가 둘 이상 생기면 그때 테이블로 분리 |
| `quantity` NUMERIC, 부호 있음 | | `long_qty`/`short_qty` 분리 | 숏은 헤지·선물에서 기본. 한 컬럼 부호로 표현하면 P&L 식이 `qty × Δprice` 하나 |
| `CHECK (quantity <> 0)` | | | 0 수량은 "포지션 없음"과 같은데 행이 있으면 조인 결과가 오염된다. 청산은 행 삭제 |
| `market_value`·`avg_cost` 미포함 | | | 평가액은 `prices` 에서 파생. 평균단가는 시장리스크에 불필요(마진 모듈도 미실현손익이 아니라 익스포저를 본다) |

### 2-4. `risk_runs` — 리스크 계산 실행 기록 (초안 · **§8 로 대체됨**, 기록용으로만 남김)

```sql
CREATE TABLE risk_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_code  TEXT             NOT NULL,
    as_of_date      DATE             NOT NULL,   -- 평가일 (이 날짜까지의 가격 사용)
    positions_as_of DATE             NOT NULL,   -- 실제 사용한 포지션 스냅샷 날짜
    base_currency   CHAR(3)          NOT NULL DEFAULT 'USD',
    method          TEXT             NOT NULL CHECK (method IN ('fhs', 'parametric', 'mc')),
    horizon_days    SMALLINT         NOT NULL DEFAULT 1 CHECK (horizon_days >= 1),
    window_days     INTEGER          NOT NULL CHECK (window_days > 0),   -- lookback (예: 500)
    n_scenarios     INTEGER          NOT NULL CHECK (n_scenarios > 0),   -- 실제 P&L 시나리오 수
    portfolio_value DOUBLE PRECISION NOT NULL,   -- as_of_date 평가액 (기준통화)
    var_99          DOUBLE PRECISION NOT NULL,   -- 손실 양수, 기준통화
    es_975          DOUBLE PRECISION NOT NULL,   -- 손실 양수, 기준통화
    params          JSONB            NOT NULL DEFAULT '{}'::jsonb,   -- lambda, garch spec, seed, stress window …
    details         JSONB,                                          -- 자산군·상품별 기여, 팩터 노출
    tag             TEXT             NOT NULL DEFAULT 'adhoc',       -- 'daily_batch' | 'adhoc' | 'experiment'
    code_version    TEXT,                                           -- git SHA
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX risk_runs_portfolio_date_idx ON risk_runs (portfolio_code, as_of_date);
```

| 컬럼 | 왜 |
|---|---|
| `var_99`·`es_975` 를 **고정 컬럼**으로 | 이 둘은 Gap plan 이 확정한 헤드라인 지표(FRTB ES 97.5 + VaR 99). 백테스트·대시보드가 매번 조인하므로 타입 컬럼 + NOT NULL. 다른 신뢰수준은 `details` 에 |
| `method`·`horizon_days`·`window_days` 를 타입 컬럼으로 | "모델 정체성"을 결정하는 파라미터. `WHERE method='fhs' AND window_days=500` 처럼 SQL 로 비교 가능해야 모델 간 비교표(Gap plan 검증 항목)를 뽑는다 |
| 나머지는 `params JSONB` | EWMA λ, GARCH 스펙, MC 경로 수, seed 는 방법마다 달라 컬럼으로 두면 대부분 NULL. 형태가 안정되면 컬럼으로 승격 |
| `positions_as_of` 별도 기록 | "최근 스냅샷 ≤ D" 규칙 때문에 어떤 포지션으로 계산했는지가 암묵적이 된다. 명시해야 재현 가능 (SR 11-7 문서의 "데이터" 절) |
| `portfolio_value` | ES/VaR 를 NAV 대비 % 로 바꾸는 분모. 실행 시점 값을 고정해 두어야 나중에 가격이 수정돼도 당시 보고값이 재현된다 |
| `n_scenarios` | FHS 는 결측 정렬 후 실제 시나리오 수가 `window_days` 보다 작을 수 있다. 검증 시 "몇 개 시나리오로 97.5% 를 잡았는가"가 핵심 질문 |
| `tag` | 백테스트는 "정식 일별 배치" 실행만 써야 한다. 실험 실행이 섞이면 Kupiec 결과가 오염. `WHERE tag='daily_batch'` 한 줄로 분리 |
| `code_version` | SR 11-7 변경 이력. 같은 입력에 다른 결과가 나오면 코드 버전부터 본다 |
| 실패 실행은 저장 안 함 | `status` 컬럼 없음. 실패는 로그로. 테이블에는 "결과가 존재하는 실행"만 |
| UNIQUE 없음 | 같은 (포트폴리오, 날짜, 방법)을 파라미터 바꿔 여러 번 돌리는 것이 정상. 인덱스만 |

## 3. 관계

```
instruments 1 ──< prices           (instrument_id)
instruments 1 ──< positions        (instrument_id)
positions   ~ ──  risk_runs        (portfolio_code + positions_as_of 로 논리 연결, FK 아님)
risk_runs   1 ──< risk_measures    (run_id, ON DELETE CASCADE)
risk_measure_types 1 ──< risk_measures (measure — 어휘·단위 카탈로그)
risk_runs   1 ──< backtest_results (2주차 — run_id FK 예정)
```

FK 삭제 정책은 기본값 `RESTRICT`. 상품을 지우려면 가격·포지션을 먼저 지워야 한다 — 실수로 시계열이 사라지는 것보다 삭제가 막히는 편이 낫다. 실무에서는 `is_active=false` 로 끝낸다.

## 4. 1주차에 의도적으로 뺀 것

| 항목 | 시점 | 비고 |
|---|---|---|
| `backtest_results` | 2주차 | (run_id, realized_pnl, breach 여부). Kupiec 코드와 같이 설계 |
| `portfolios` 테이블 | 메타가 필요해질 때 | 지금은 `portfolio_code` + USD 고정 |
| 스트레스 결과 테이블 | 3~4주차 | 시나리오명 → P&L 형태라 `risk_runs` 와 모양이 다름 |
| 수익률·달력 테이블 | 안 만듦 | 파생값. 달력 정렬(교집합 vs forward-fill)은 수익률 빌더의 옵션 |
| 거래 원장 | 안 만듦 | §2-3 |
| OHLC | 필요 시 | §2-2 |

## 5. 타입 결정 — Java 개발자용 메모

| PostgreSQL | psycopg 3 가 돌려주는 Python 타입 | Java 대응 | 이 프로젝트의 용도 |
|---|---|---|---|
| `DOUBLE PRECISION` | `float` (IEEE 754 배정도) | `double` | 가격·리스크 수치 → 바로 numpy `float64` |
| `NUMERIC` | `decimal.Decimal` | `BigDecimal` | 수량·승수. 연산 전 `float()` 캐스팅 필요 |
| `DATE` | `datetime.date` | `LocalDate` | 거래일 |
| `TIMESTAMPTZ` | tz-aware `datetime` | `OffsetDateTime` | 감사 시각 |
| `JSONB` | `dict` / `list` | `Map` (Jackson) | `params`·`details` |

`NUMERIC` 을 numpy 배열에 그대로 넣으면 `dtype=object` 가 되어 벡터 연산이 100배 느려지고 조용히 동작한다 — Python 은 컴파일 타임 타입 오류가 없으니 이 함정은 **테이블 설계 단계에서** 막는다. 그래서 측정값은 float, 장부값만 NUMERIC.

## 6. 스택 결정 (스키마 코드화 시 적용)

| 결정 | 채택 | 이유 |
|---|---|---|
| DB 드라이버 | `psycopg[binary]` 3.x, ORM 없음 | 이 프로젝트의 목적은 "SQL 을 쓸 줄 안다"는 증거. ORM 은 SQL 을 숨긴다. psycopg 3 는 `COPY` 로 대량 적재, 서버측 바인딩, 타입 힌트 지원 |
| 마이그레이션 | `db/migrations/NNNN_*.sql` 평문 SQL + 30줄짜리 Python 러너(`schema_migrations` 테이블에 적용 이력) | Alembic 은 SQLAlchemy 를 끌고 온다. 저장소를 여는 사람이 DDL 을 바로 읽는 편이 낫다 |
| 로컬 DB | docker-compose `postgres:17-alpine` | CI(GitHub Actions service container)와 같은 이미지 |
| 기준통화 | USD 고정 (`risk_runs.base_currency` 에 기록) | 유니버스가 미국 상장 중심 |

## 7. 승인 요청 — 결정이 필요한 항목

1. `prices` 에 OHLC 를 지금 넣을지 (기본안: 안 넣음).
2. `portfolios` 테이블을 1주차부터 둘지 (기본안: `portfolio_code` TEXT).
3. `risk_runs.var_99`·`es_975` 를 고정 컬럼으로 두는 안 vs 전부 `(measure, confidence, value)` 행으로 정규화하는 안 (기본안: 고정 컬럼).
4. 손실 = 양수 부호 규약.
5. Postgres 17 (18 은 데이터 디렉터리 구조가 바뀌어 compose 예제가 아직 흔들림).

승인되면: `db/migrations/0001_init.sql` + 러너 + `.env` 기반 `DATABASE_URL` 설정 모듈을 내가 넣고, ETL 과 수익률 행렬 빌더는 JK 가 작성 → 내가 리뷰.

---

## 8. 결정 3 재검토 — 결과 저장 구조 (2026-09-12, 승인 대기)

### 8-1. 질문에 대한 답: 고정 컬럼 방식이면 컬럼이 몇 개까지 늘어나나

| 주차 | 들어오는 지표 | 고정 컬럼으로 표현 가능? | 누적 컬럼 수 |
|---|---|---|---|
| 1 | VaR 99, ES 97.5 | 가능 | 2 |
| 3 | 스트레스 ES (FRTB SES) | 가능 | 3 |
| 3 | 컴포넌트 ES · 증분 VaR — **상품별** N개 | **불가능** (상품 수만큼 컬럼을 만들 수 없음) → JSONB 로 밀려남 | 3 + JSONB |
| 3 | 팩터 노출 · 팩터별 ES 기여 — 4 팩터 × 2 | 가능하지만 8개 | 11 + JSONB |
| 5 | 마진: IM 합계 · 코어 · 플로어 적용분 · 스트레스 블렌드 · 유동성 add-on · 집중 add-on · legacy SPAN | 가능하지만 7개 | 18 + JSONB |
| 요청 시 | "ES 99 도 같이" | **지표 × 신뢰수준마다 컬럼 추가** — 구조적으로 그렇다 | 19, 20, … |

결론: 고정 컬럼 방식은 3주차부터 이미 "컬럼 18개 + 상품별 내역은 JSONB" 라는 혼합 구조가 된다. 그리고 JSONB 로 밀린 상품별·자산군별 기여는 **SQL 로 시계열 조회가 안 된다** (`jsonb_each` 로 풀어야 하고 인덱스도 없음). 즉 실제 선택지는 "고정 컬럼 vs 정규화"가 아니라 **"고정 컬럼 + JSONB 블롭" vs "헤더 + 정규화된 지표 테이블"** 이다.

### 8-2. 두 안 비교

| 축 | A. 고정 컬럼 + `details JSONB` | B. `risk_runs` 헤더 + `risk_measures` 롱 테이블 |
|---|---|---|
| 새 지표·새 신뢰수준 | `ALTER TABLE` + 코드 수정 | 행 하나 추가. 스키마 변경 없음 |
| 상품별·자산군별 기여의 시계열 조회 | JSONB 풀기 (`jsonb_each`), 인덱스 없음 | `WHERE measure='component_es' AND scope_type='asset_class'` — 일반 SQL |
| 모델 간 비교표 (Gap plan 검증 항목) | 컬럼별 하드코딩 | `GROUP BY method, measure, confidence` |
| 헤드라인 조회 (백테스트·대시보드) | 컬럼 직접 읽기 — 가장 단순 | 조인 1회. `v_risk_headline` 뷰로 A 와 같은 모양 제공 |
| NULL 밀도 | 방법·모듈마다 안 쓰는 컬럼이 NULL | 없음 (있는 지표만 행) |
| 무결성 제약 | 컬럼별 `CHECK` | `CHECK (measure IN (...))` + `UNIQUE (run_id, measure, confidence, scope)` 로 중복 지표 차단 |
| 행 수 | 실행당 1행 | 실행당 ≈ 60행 (헤드라인 4 + 자산군 8 + 상품 20×2 + 팩터 8). 일별 배치 10년 = 15만 행 — PostgreSQL 에 무의미한 크기 |
| 타입 안전성 | 컬럼명이 곧 계약 | `measure` 문자열이 계약 — Python 쪽 `Enum` 으로 고정하고 DB `CHECK` 로 이중 방어 |
| 마진 모듈(5주차) | 별도 `margin_runs` 를 만들거나 컬럼 7개 추가 | 같은 헤더 + `measure='im', 'im_liquidity_addon', …` 재사용 |

**재제안: B.** 이 프로젝트가 보여주려는 것이 "규제 백테스트·모델 비교·기여 분석"인데, 그 조회들이 전부 "지표 × 범위 × 시간" 축을 자른다. 롱 테이블은 그 축을 SQL 로 바로 자르고, JSONB 는 못 자른다. A 의 유일한 장점(헤드라인 단순 조회)은 뷰로 회수된다.

### 8-3. B 안 DDL (초안 — 확정본은 `db/migrations/0002_risk_runs.sql`, 차이는 §8-4~8-6)

```sql
CREATE TABLE risk_runs (                       -- 실행 헤더: "무엇을 어떤 입력·설정으로 돌렸나". 결과 컬럼 없음
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_code  TEXT             NOT NULL,
    as_of_date      DATE             NOT NULL,
    positions_as_of DATE             NOT NULL,
    base_currency   CHAR(3)          NOT NULL DEFAULT 'USD',
    method          TEXT             NOT NULL CHECK (method IN ('fhs', 'parametric', 'mc')),
    horizon_days    SMALLINT         NOT NULL DEFAULT 1 CHECK (horizon_days >= 1),
    window_days     INTEGER          NOT NULL CHECK (window_days > 0),
    n_scenarios     INTEGER          NOT NULL CHECK (n_scenarios > 0),
    portfolio_value DOUBLE PRECISION NOT NULL,
    params          JSONB            NOT NULL DEFAULT '{}'::jsonb,
    tag             TEXT             NOT NULL DEFAULT 'adhoc',
    code_version    TEXT,
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX risk_runs_portfolio_date_idx ON risk_runs (portfolio_code, as_of_date);

CREATE TABLE risk_measures (                   -- 실행 결과: 지표 하나 = 행 하나
    run_id      BIGINT           NOT NULL REFERENCES risk_runs (run_id) ON DELETE CASCADE,
    measure     TEXT             NOT NULL CHECK (measure IN (
                    'var', 'es', 'stressed_es',                 -- 1~3주차
                    'component_es', 'incremental_var',          -- 3주차, scope = instrument / asset_class
                    'factor_exposure', 'factor_es',             -- 3주차, scope = factor
                    'im', 'im_core', 'im_floor', 'im_stress_blend',
                    'im_liquidity_addon', 'im_concentration_addon', 'im_span_legacy'   -- 5주차
                )),
    confidence  NUMERIC(5,4)     CHECK (confidence > 0 AND confidence < 1),   -- 0.9900 / 0.9750, 신뢰수준 없는 지표는 NULL
    scope_type  TEXT             NOT NULL DEFAULT 'portfolio'
                                 CHECK (scope_type IN ('portfolio', 'asset_class', 'instrument', 'factor')),
    scope_key   TEXT             NOT NULL DEFAULT '',   -- 'equity' | instrument_id 문자열 | 'rates' … portfolio 면 ''
    value       DOUBLE PRECISION NOT NULL,              -- 손실 양수, 기준통화 (§1-1)
    UNIQUE NULLS NOT DISTINCT (run_id, measure, confidence, scope_type, scope_key)
);
CREATE INDEX risk_measures_lookup_idx ON risk_measures (measure, scope_type, scope_key, run_id);

CREATE VIEW v_risk_headline AS                -- A 안과 같은 모양의 헤드라인 조회
SELECT r.run_id, r.portfolio_code, r.as_of_date, r.method, r.tag, r.portfolio_value,
       MAX(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)  AS var_99,
       MAX(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975) AS es_975
FROM risk_runs r
JOIN risk_measures m USING (run_id)
WHERE m.scope_type = 'portfolio'
GROUP BY r.run_id;
```

- `UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+): 기본 UNIQUE 는 NULL 을 서로 다른 값으로 취급해 `confidence IS NULL` 인 지표가 중복 삽입된다. 이 옵션이 그걸 막는다 — 17 을 고른 이유 중 하나.
- `measure` 의 `CHECK` 목록은 지표가 늘 때 `ALTER TABLE … DROP/ADD CONSTRAINT` 로 갱신한다. 컬럼 추가보다 가볍고, "허용 지표 목록"이 DDL 에 문서화된다. Python 쪽은 `enum.StrEnum` 으로 같은 목록을 들고 테스트가 두 목록의 일치를 검사한다.
- `backtest_results`(2주차)는 `run_id` 를 참조하고 헤드라인 VaR 은 뷰에서 읽는다.

## 9. 추가 질문에 대한 답

### 9-1. JPA 를 쓰던 사람에게: 왜 ORM 없이 가나

JPA 가 값을 내는 조건은 (a) 엔티티 그래프를 객체로 탐색하고, (b) 행 단위로 읽어 수정하고(dirty checking), (c) 여러 엔티티에 걸친 트랜잭션을 다루는 워크로드다. 이 프로젝트의 DB 접근은 그 셋 중 어느 것도 아니다.

| 이 프로젝트의 접근 패턴 | ORM 이 하는 일 | 평문 SQL |
|---|---|---|
| 가격 수천~수만 행 벌크 적재 (ETL) | 행마다 엔티티 생성 → `persist` → flush. N+1·메모리 | `COPY` 또는 `executemany` + `ON CONFLICT`. 한 문장 |
| 수익률 행렬 = "날짜 × 상품" 넓은 표 읽기 | 엔티티 리스트로 받아 코드에서 피벗 | `SELECT … ORDER BY date, instrument` → pandas `pivot` 한 줄. ORM 을 거쳐도 결국 같은 일을 함 |
| `risk_measures` append-only 기록 | 엔티티 매핑 필요 없음 | `INSERT` |
| 행 단위 수정·객체 탐색 | ORM 의 강점 | **이 프로젝트에 없음** |

그 외 근거:
- **이력서 신호**: 타깃 JD 의 키워드는 "SQL" 이지 "SQLAlchemy" 가 아니다. 저장소를 여는 사람이 `db/migrations/*.sql` 에서 DDL 을 바로 읽는다.
- **학습 비용**: Python 을 익히는 단계에서 SQLAlchemy 의 세션·유닛오브워크·2.0 스타일 API 는 별도 학습 항목이다. 지금 배울 것은 psycopg 하나면 된다.
- **마이그레이션**: Alembic 은 ORM 없이도 쓸 수 있지만 SQLAlchemy 를 의존성으로 끌고 온다. 우리 러너는 60줄이고 `schema_migrations` 한 테이블이다.
- **정직한 비용**: JPA 메타모델이 주던 "컬럼명 오타를 컴파일 시 잡기"가 없다. 대체 수단은 (1) CI 가 실제 PostgreSQL 에 대고 테스트를 돌리고, (2) 읽기 결과는 `psycopg.rows.class_row` 로 `dataclass` 에 매핑해(읽기 전용 엔티티에 해당) 컬럼명이 코드에 한 번만 나오게 한다.
- **다시 검토할 시점**: 동적으로 조립하는 쿼리가 수십 개가 되면 SQLAlchemy **Core**(엔티티 없는 쿼리 빌더)를 검토한다. 그 전까지는 `psycopg.sql` 로 식별자·조건을 안전하게 조립한다.

### 9-2. 고정 컬럼 vs `params JSONB` 의 기준, 그리고 조회 성능

**고정 컬럼으로 두는 조건** (하나라도 해당하면 컬럼):
1. 계획된 쿼리의 `WHERE` · `JOIN` · `GROUP BY` 에 등장한다 — `method`, `as_of_date`, `portfolio_code`, `tag`.
2. 모든 방법(FHS·Parametric·MC)에 항상 존재한다 — `horizon_days`, `window_days`, `n_scenarios`.
3. `CHECK`·FK 로 지켜야 할 값이다 — `method`, `base_currency`.
4. "이 실행과 저 실행이 같은 모델인가"를 판정하는 모델 정체성 파라미터다.

**JSONB 에 두는 조건**: 방법 하나에만 있거나(EWMA λ 는 FHS 만, 경로 수·seed 는 MC 만), 형태가 바뀌는 중이거나(GARCH 스펙), 조회 조건으로 쓰지 않는다. 컬럼으로 두면 대부분 NULL 이 되는 값들.

**"FHS 로 돌린 실행만"** 은 `WHERE method = 'fhs'` — 일반 컬럼이고 B-tree 인덱스 대상이라 JSONB 와 무관하게 빠르다. 느려질 수 있는 건 `params->>'lambda' = '0.94'` 처럼 JSONB 키를 조건으로 쓸 때인데, (1) 실행 수가 수천 건이면 순차 스캔도 밀리초 단위고, (2) 정말 필요하면 표현식 인덱스 `CREATE INDEX ON risk_runs ((params->>'lambda'))` 나 GIN 인덱스(`jsonb_path_ops`)로 해결된다. **승격 규칙**: 같은 JSONB 키가 실제 코드의 `WHERE` 절에 두 번 나타나는 순간 컬럼으로 승격하는 마이그레이션을 쓴다.

### 8-4. 단위(unit) 결정 — (a) 채택, 단 unit 은 행이 아니라 measure 의 속성 (승인 대기 → 0002 반영)

JK 지적: `value` 한 컬럼에 통화 금액·NAV 비율·민감도가 섞이면 % 와 USD 를 더하는 버그가 조용히 난다.

| | (a) unit 컬럼 + currency 컬럼 | (b) 전부 NAV 비율로 정규화 | **채택: (a′) measure → unit 카탈로그** |
|---|---|---|---|
| 파생상품 포트폴리오 | 문제 없음 | **분모가 정의되지 않는다.** 선물은 개시 시 PV≈0, 마진 계좌의 "NAV"는 담보액이지 익스포저가 아니다. 5주차 마진 모듈의 IM/NAV 는 무의미하거나 발산 | 통화 금액 저장 |
| 민감도(팩터 노출·DV01) | unit='sensitivity' | **표현 불가.** "1bp 당 USD" 는 NAV 비율이 아니다 | `currency_per_pct` · `currency_per_bp` |
| 규제·백테스트 관례 | 통화 | 변환 필요 | 실현손실(USD) > VaR(USD), CPMI-IOSCO 커버리지도 통화 |
| 비율 보고 | 별도 저장 또는 계산 | 저장값 그대로 | `value / portfolio_value` — 뷰 `v_risk_headline` 이 `var_99_frac` 제공. 정보량은 (b) 와 같고 변환 횟수만 적다 |
| 버그 방지 강도 | **행마다 unit 이 있으면 같은 measure 가 두 단위로 들어올 수 있다** — 그것이 바로 버그의 출처 | 단위가 하나라서 안전하지만 위 두 줄 때문에 성립 안 함 | **measure ⇒ unit 함수 종속을 DB 가 강제.** `risk_measure_types(measure PK, unit, description)` 에 한 번만 정의, `risk_measures.measure` 가 FK |
| currency 컬럼 | 행마다 | — | **행에 두지 않음.** 한 실행의 통화값은 전부 `risk_runs.base_currency` 하나. 행 단위 통화는 실행 안에서 통화 혼합을 허용하는 구조라 오히려 위험 |

단위 어휘: `currency`(실행 기준통화 금액) · `currency_per_pct`(팩터 +1% 당 P&L) · `currency_per_bp`(+1bp 당 P&L). NAV 비율은 **저장하지 않는다** — 필요하면 뷰나 코드에서 나눈다.

### 8-5. `risk_measures` 인덱스 설계

| 인덱스 | 예상 조회 패턴 | 비고 |
|---|---|---|
| `UNIQUE NULLS NOT DISTINCT (run_id, measure, confidence, scope_type, scope_key)` — 제약이 만드는 btree | (1) 실행 하나의 헤드라인·상세 전체 (`WHERE run_id = ?`), (2) 백테스트가 run_id 로 조인, (3) `ON DELETE CASCADE` 시 자식 탐색 | 선두가 `run_id` 라 **(run_id, measure) 별도 인덱스는 중복 — 만들지 않는다** |
| `risk_measures_series_idx (measure, scope_type, scope_key, confidence, run_id) INCLUDE (value)` | "지표 하나를 실행 축으로" — 자산군별 컴포넌트 ES 시계열, 상품 X 의 증분 VaR 추이, 방법별 비교표 | `INCLUDE (value)` 로 index-only scan: 힙 접근 없이 값까지 인덱스에서 읽는다 (PG 11+) |
| `risk_runs_portfolio_tag_date_idx (portfolio_code, tag, as_of_date)` | 백테스트·대시보드: 포트폴리오 + `tag='daily_batch'` + 날짜 범위 | tag 없이 조회해도 `portfolio_code` 접두어로 사용됨 |
| 만들지 않은 것 | `method` 단독(카디널리티 3 — 플래너가 안 씀), `params` GIN(승격 규칙 §9-2 적용 전까지 불필요) | |

규모 감각: 일별 배치 + 실험으로 연 3천 실행 × 실행당 ≈60행 = 연 20만 행. 인덱스 전부 메모리에 들어간다. 위 설계는 성능보다 **조회 의도를 DDL 에 남기는 것**이 목적이다.

### 8-6. 어휘 강제 — DB 와 애플리케이션 양쪽

| 방식 | 한 줄 트레이드오프 |
|---|---|
| `CHECK (col IN (...))` | 어휘가 DDL 에 보이고 오타를 막지만, 값 추가가 DDL 마이그레이션(제약 교체) |
| PostgreSQL `ENUM` 타입 | 저장이 작고 정렬 가능하지만, 값 삭제 불가·`ADD VALUE` 는 같은 트랜잭션에서 못 씀 — 어휘가 흔들리는 프로젝트에 부적합 |
| 카탈로그 테이블 + FK | 어휘가 **데이터**(INSERT 로 추가)이고 메타(단위·설명)를 붙일 수 있지만, 읽을 때 조인 하나 |
| 애플리케이션 `StrEnum` 만 | 바꾸기 가장 쉽지만 psql·노트북 등 다른 경로로 쓰는 값은 아무거나 들어간다 |

**결정**: `measure` → 카탈로그 FK(단위를 같이 들어야 하므로), `scope_type`·`method` → `CHECK`(구조적이고 4개·3개로 고정), Python 은 `risk_engine.data.vocab` 의 `StrEnum` 4개가 거울. `tests/test_schema_db.py::test_measure_catalogue_matches_python_vocab` 가 두 목록의 일치를 CI 에서 검사한다 — 한쪽만 고치면 CI 가 떨어진다. 한 줄 요약: **DB 제약은 "누가 써도" 막고, 앱 Enum 은 "코드에서 오타 없이" 쓰게 한다. 둘 다 둔다.**
