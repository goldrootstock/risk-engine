# 설계 노트 04 — 수익률 빌더와 P&L 매핑

- 상태: 작성과 동시에 구현 (2026-09-13, 승인 대기 없음 — CLAUDE.md §5 모드 변경)
- 전제: 노트 01(스키마·손실 부호), 02(§5 달력 정렬, §11 음수 종가, §6 DV01 예약), 03(ETL 완료, 31계열 적재)
- 위치: `risk_engine.risk.universe` · `risk_engine.risk.levels` · `risk_engine.risk.returns` · `risk_engine.risk.pnl`

---

## 0. 한 줄 요약

DB 의 가격·수익률 **수준(level)** 을 읽어(읽기 전용) 표본 집합에 맞춰 정렬하고, 계열 종류별로 **변화량 행렬 X**(날짜 × 팩터)을 만든 뒤, 포지션이 주어지면 변화량 한 행(또는 시나리오 여러 행)을 **USD P&L** 로 바꾼다. 부호는 P&L 그대로(이익 +). 손실로 뒤집는 곳은 ES/VaR 모듈 한 곳뿐이다(노트 01 §1-1).

```text
prices(DB) ──levels.load_levels──▶ wide levels (date × ticker)
        ──returns.build──▶ ReturnMatrix{ changes X, kind, levels, meta }
        ──pnl.pnl_matrix(X_scen, levels_today, positions)──▶ P&L (scen × instrument), USD
```

## 1. 팩터 정의 — 계열 종류별 "변화량"

| 종류 | 계열 | 변화량 x_t | 단위 | 근거 |
|---|---|---|---|---|
| `log` | FX (ECB 원계열 → USD 기준 환율) | ln(S_t / S_{t−1}) | 비율 | 로그수익률 표준. FX 는 양수 |
| `abs` | 에너지 현물 | P_t − P_{t−1} | USD/단위 | 노트 02 §11: 음수·0 근처 가격에서 % 는 발산 |
| `bp` | 국채 CMT 수익률 | (y_t − y_{t−1}) × 100 | bp | 금리 리스크 관행. 값이 % 단위라 ×100 |

**FX 팩터는 "1 외화 = x USD"** 로 정의한다. ECB 는 `1 EUR = z CCY` 로 주므로 S_CCY = EURUSD / EURCCY, S_EUR = EURUSD. 로그 변화량은 ln EURUSD − ln EURCCY 의 차분이라 ECB 원계열 두 개에서 바로 나온다. 팩터 열 이름은 통화 코드(`JPY`, `EUR`, …). 포지션 `EURJPY` 상품에 수량 q 는 **q JPY 의 롱**(USD 대비)을 뜻한다 — DB 의 `instruments.currency` 가 그 통화다. 이유: 기준통화 USD 포트폴리오에서 JPY 익스포저의 P&L 은 USD/JPY 로 결정되지 EUR/JPY 로 결정되지 않는다.

## 2. 달력 정렬 (노트 02 §5 결정의 구현)

- `align="intersection"`(기본): 포함된 모든 계열에 값이 있는 날짜만. 버린 날짜 수를 `meta.dropped_dates` 에.
- `align="ffill"`: 한 날짜 결측을 전일 값으로 채우되 `max_gap` (기본 1) 초과 구간은 버린다. 채운 셀 수 `meta.filled_cells`. 채운 날의 변화량이 0 이 되어 Christoffersen 독립성 검정을 오염시키므로 옵트인이고, 값이 실행 기록(`risk_runs.params`)에 남는다.
- 정렬은 **수준(level)** 단계에서 한다. 변화량은 정렬 후 인접 행 차분이라, intersection 에서 이틀치 변화가 하루로 합쳐지는 날이 생긴다(노트 02 §5 명시). 표준화(FHS)가 이를 일부 흡수하며 한계로 기록.

## 3. 표본 집합 (`config/universes.toml`)

`default`(2006-02-09~, 31 전부) · `from_1999` · `rates_energy_1990`. 빌더는 집합 이름을 받아 `start`·`include`/`exclude` 를 적용하고 이름과 파일 sha256 을 `meta` 에 넣는다. 끝 날짜는 인자(`end`, 기본 = 가용 최신).

## 4. P&L 매핑 (`pnl.py`)

포지션 (instrument_id, quantity q, multiplier m) 과 오늘의 수준 L_i, 변화량 x_i 에 대해 상품 i 의 P&L:

| 종류 | P&L_i | 비고 |
|---|---|---|
| `log` | q·m·S_i·(e^{x} − 1) | q = 외화 수량. 정확식(1차 근사 q·m·S·x 아님) |
| `abs` | q·m·x | q = 배럴·갤런·MMBtu 수량 |
| `bp` | −DV01_i · x | q = 액면 notional(USD), DV01 = q·m·D_mod(y, T)·10⁻⁴ |

**DV01 — 만기별 매일 재계산** (노트 02 §6 결정, JK 검산): par 채권(반기 쿠폰) 수정 듀레이션

D_mod(y, T) = [1 − (1 + y/2)^(−2T)] / y,  y = 소수(예: 0.0093), T = 만기(년)

- y → 0 극한은 T (L'Hôpital). |y| < 10⁻⁸ 이면 T 를 쓴다. 음수 y(1M 2015·2020) 도 식이 성립한다.
- 만기 T 는 티커에서 파싱(`UST_1M` → 1/12, `UST_10Y` → 10). 코드 규칙이지 설정값이 아니다.
- 컨벡시티는 **넣지 않는다** — 1일 지평에서 2차항 ½·C·Δy² 은 10Y·10 bp 이동 기준 듀레이션 항의 약 0.5 % [계산: C≈80, Δy=0.001 → 0.00004 vs D·Δy≈0.0008]. 100 bp 충격(스트레스)에서는 ≈5 % 라 스트레스 노트에서 재검토. `docs/decisions.md`.
- 부호: 수익률 상승(x > 0) 이면 롱 채권 손실 → P&L 음수. 시그니처 자체가 이를 강제한다.

시나리오 행렬 X (n × k) 에 대해 P&L 행렬 (n × k) 을 벡터화해 계산하고, 포트폴리오 P&L 은 행 합. **여기서는 부호를 뒤집지 않는다.**

## 5. 인터페이스

```text
universe.load_set(name, path=config/universes.toml) -> UniverseSet(name, start, include|exclude, sha256)
levels.load_levels(conn, tickers, start, end) -> DataFrame(date × ticker)          # 읽기 전용 SQL
returns.build(levels, specs, *, align="intersection", max_gap=1) -> ReturnMatrix
    ReturnMatrix.changes  : DataFrame(date × factor)  변화량 x
    ReturnMatrix.kind     : {factor: "log"|"abs"|"bp"}
    ReturnMatrix.levels   : DataFrame(date × factor)  정렬된 수준 (같은 인덱스; FX 는 S_CCY)
    ReturnMatrix.factor_of: {ticker: factor}
    ReturnMatrix.meta     : {align, max_gap, dropped_dates, filled_cells, n_obs, first, last, ...}
pnl.dv01(notional, y_decimal, maturity_years) -> float
pnl.pnl_matrix(changes: ndarray(n×k), levels_today: Series, positions: {ticker: qty}, specs) -> ndarray(n×k)
```

`build` 와 `pnl_matrix` 는 순수 함수(DB 없음). `load_levels` 만 DB 를 읽고 `default_transaction_read_only` 세션에서 동작한다(노트 00 §2 #4·테스트 후보 3).

## 6. 어디서 틀릴 수 있나

- **비동기 종가**: ECB 14:15 CET vs H.15 NY 종가 vs EIA. 같은 날짜의 상관이 과소 추정. 한계로 기록(노트 02 §5).
- **FRED T+1**: 국채 최신 날짜가 FX·에너지보다 하루 늦다 → intersection 이 최신일을 버린다. `meta.last` 가 그 사실을 보여 준다. 일별 배치는 D−1 기준.
- **1M 국채 0·음수 수익률**: bp 차분과 DV01 식 모두 정의되나, y≈0 에서 D_mod≈T 로 안정. 테스트로 고정.
- **FX 크로스의 두 원계열 결측 불일치**: EURUSD 와 EURJPY 중 하나만 결측이면 intersection 이 그 날짜를 버린다. 크로스는 정렬 **후** 계산한다.
- **positions.quantity 의 의미**가 종류마다 다르다(외화 수량 / 물량 / 액면). 노트 01 §2-3 의 "quantity × multiplier × Δprice" 는 price 계열에만 맞고 이 노트가 나머지를 정의한다 — 노트 01 에 상호 참조 추가.
