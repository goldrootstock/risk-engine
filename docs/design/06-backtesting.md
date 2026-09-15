# 설계 노트 06 — 백테스트: Kupiec · Christoffersen · Basel traffic light · PLA

- 상태: 작성과 동시에 구현 (2026-09-13). JK 지시 [D]: 2020-03~04 에 위반이 몰려도 파라미터를 건드리지 않는다. 위반마다 자산 귀속을 남긴다. **§6 보강 (2026-09-15): 0008 지평 보정의 본문 반영, 마진 MPOR 과 실현 2일 손실의 정의**
- 전제: 노트 00 §3 (백테스트는 읽기 전용, 파라미터를 인자로만), 01 §8 (`backtest_results` 예약, `risk_runs.data_as_of` 예약), 05 (`daily_batch` 실행 계열)
- 위치: `risk_engine.backtest.{hpl,statistics,runner,record}`, `db/migrations/0006_backtests.sql`, `config/backtest_params.toml`

---

## 0. 한 줄 요약

날짜 t 의 `daily_batch` 실행(VaR·ES, 포지션, 표본 집합)과 t→t+1 의 **실제 팩터 변화**로 가상 손익 HPL(Hypothetical P&L, 가상 손익)을 만들어 VaR 초과를 판정하고, 초과 수(Kupiec)·초과의 군집(Christoffersen)·Basel 존·PLA 를 계산한다. 백테스트는 `risk_runs`·`risk_measures`·`prices` 를 **읽기만** 하고 `backtest_results`·`backtest_summaries` 에만 쓴다. 파라미터는 `BacktestConfig`(frozen) 로만 들어오고 함수 안에서 재추정하지 않는다.

```text
risk_runs(t, daily_batch, universe) ─┐
prices → ReturnMatrix(집합) ─ changes[t+1] ─┼─ pnl_matrix(levels_t, positions_t) ─▶ HPL_t (signed, USD) · 상품별 귀속
                                             └─ deltas_t · changes[t+1] ─▶ RTPL_t (선형)
                     loss_t = −HPL_t  vs  VaR_99,t  ─▶ exception_t
       250일 창: Kupiec POF · Christoffersen IND/CC · traffic light · PLA(Spearman·KS)
```

## 1. 손익 정의

| 이름 | 정의 | 왜 |
|---|---|---|
| **HPL** (hypothetical) | t 의 포지션·수준으로 t+1 의 실제 변화량을 `pnl_matrix` 에 통과시킨 P&L. 포지션 불변, 수수료·캐리 없음 | Basel 이 VaR 백테스트에 요구하는 것이 "포지션 고정" 손익 [출처: BCBS 1996 Supervisory framework for backtesting §II]. 우리 포지션은 스냅샷이라 실제 손익과 같다 |
| **RTPL** (risk-theoretical) | 같은 변화량을 **선형 델타** 로 통과시킨 P&L (노트 05 §4 의 δ) | FRTB PLA 는 리스크 모델의 손익(RTPL)과 실제 손익(HPL)을 비교한다 [출처: BCBS 2019 MAR32.4-32.14]. 우리 FHS 는 정확 매핑을 쓰므로 RTPL 을 정확식으로 잡으면 HPL 과 같아져 검정이 무의미하다. 선형 델타를 RTPL 로 두면 PLA 가 **매핑의 비선형성(FX 지수식)이 얼마나 되는가** 를 잰다 — 정직한 대용이며 문서에 명시 |
| 초과 | loss_t = −HPL_t > VaR_99,t | 손실 양수 규약. VaR 는 t 의 `risk_measures.var@0.99` |
| 귀속 | 상품별 손실 (n 상품 벡터), 초과일에 상위 기여 저장 | JK [D]: 군집이 나왔을 때 "왜 몰렸는가" |

t+1 은 **정렬된 다음 관측일**(교집합 표본). 휴일 건너뛰기는 변화량이 이틀치인 것과 같은 한계(노트 04 §6).

## 2. 검정 (전부 250 일 창, 창 = 마지막 250 관측, 롤링은 옵션)

| 검정 | 통계량 | 기준 | 출처 |
|---|---|---|---|
| Kupiec POF(proportion of failures) | LR_uc = −2 ln[(1−p)^{n−x} p^x / ((1−x/n)^{n−x} (x/n)^x)] ~ χ²(1), p = 0.01 | 유의수준 5 % → 기각 p-value < 0.05 | Kupiec (1995) |
| Christoffersen 독립성 | LR_ind: 초과 여부의 1차 마르코프 전이 (π₀₁ vs π₁₁) ~ χ²(1); 조건부 커버리지 LR_cc = LR_uc + LR_ind ~ χ²(2) | 5 % | Christoffersen (1998) |
| Basel traffic light | 250 일 초과 수: 0~4 녹색 · 5~9 황색 · ≥10 적색 | 존 | BCBS (1996) §III |
| PLA (FRTB 식) | Spearman ρ(HPL, RTPL) 과 KS 통계량(두 분포) | Spearman: > 0.80 녹색, 0.70~0.80 황색, < 0.70 적색; KS: < 0.09 녹색, 0.09~0.12 황색, > 0.12 적색 | BCBS (2019) MAR32.11-32.13 |

ES 자체의 백테스트(Acerbi–Székely 2014)는 MVP 범위 밖 — 노트에 예약.

## 3. 저장 (0006)

- `backtest_results` — 날짜 t 한 행: `run_id`(FK risk_runs), `universe`, `portfolio_code`, `as_of_date`(t), `pnl_date`(t+1), `hpl`, `rtpl`, `var_99`, `es_975`, `exception`, `attribution` JSONB(`{"ticker": loss, …}` 상위 5 + 합), `created_at`. UNIQUE (run_id).
- `backtest_summaries` — 창 한 행: `universe`, `portfolio_code`, `window_start`, `window_end`, `n_obs`, `exceptions`, `expected`, `kupiec_lr`, `kupiec_p`, `christoffersen_lr`, `christoffersen_p`, `cc_lr`, `cc_p`, `traffic_light`, `pla_spearman`, `pla_ks`, `pla_zone`, `params` JSONB(`backtest_params.toml` sha256 포함), `code_version`, `created_at`.
- `risk_runs.universe TEXT NOT NULL DEFAULT 'default'` 승격 (params 에서 채움) + 인덱스 `(universe, portfolio_code, tag, as_of_date)`. JK [B]: 집합 식별자가 두 테이블에 있어야 한다. 노트 01 §9-2 승격 규칙 적용.
- `risk_runs.data_as_of`(예약)는 `params.data_last_date` 로 대체 유지 — 승격 조건 미충족.

## 4. 읽기 전용 강제

`runner.backtest(runs, hpl, cfg) -> BacktestReport` 는 커넥션을 받지 않는다. 데이터 적재(`runner.load_inputs`)는 `default_transaction_read_only = on` 세션에서 동작하는 것을 테스트가 확인하고, 쓰기는 `record.write` 하나다. 결과가 나쁘면 `cfg` 를 바꿔 다시 돌리는 코드는 없다 — 새 실행은 새 `backtest_summaries` 행이고 `params.sha256` 이 다르다(노트 00 §3).

## 5. 어디서 틀릴 수 있나

- 2020-03~04: EWMA 가 레짐 전환에 하루 늦어 초과가 몰릴 것이다. 그대로 보고한다. Christoffersen 이 그것을 잡아야 정상이다.
- HPL 은 포지션 고정 가정이라 실제 헤지·리밸런싱 손익과 다르다. 우리 장부는 스냅샷이라 같다.
- t+1 이 휴일 다음날이면 이틀치 손익을 하루 VaR 과 비교한다. 초과가 과대될 수 있다 — 창 통계에 그 날 수를 함께 보고.
- PLA 의 RTPL 정의가 FRTB 의 "리스크 모델 손익" 과 다르다(대용). 문서에 명시.

## 6. 노트 본문에 빠져 있던 것 — 지평 보정(0008)과 A2 로의 연결 (2026-09-15 보강)

### 6-1. 0008 은 이 노트에 반영되지 않았다

2026-09-13 의 JK 결정(정식 초과 = h일 블록 부트스트랩 VaR, raw·√h 병기)은 `decisions.md` §4, 모델 문서 §7-2, 0008 마이그레이션, `runner.backtest(horizon_var=)` 에 있고, 이 노트 §1~§4 는 결정 전 상태다. 요약: t→t+1 전이가 h ≥ 2 영업일이면 `fhs.evaluate(horizon=h)` 가 잔차 풀에서 연속 h 개 벡터를 합쳐(날짜별 공동 샘플 유지, σ_T 는 지평 안에서 고정) h일 VaR 을 만들고 그것과 비교한다. 근거는 초과 건수가 아니라 내부 정합성이다 — EWMA 로 변동성 자기상관을 모형화한 모델이 √h(i.i.d.)로 지평을 늘리는 것은 모순.

0008 이 더한 열(§3 의 표에 없던 것): `backtest_results.h_business_days`(전이의 영업일 수, 기본 1) · `var_h_block`(h일 블록 VaR, **h = 1 이면 NULL** 이고 `var_99` 가 그 값) · `var_h_sqrt`(`var_99 × √h`) · `exception_raw`(−hpl > `var_99`) · `exception_sqrt`(−hpl > `var_h_sqrt`); `backtest_summaries.exceptions_raw` · `exceptions_sqrt`. `exception` 의 뜻은 COMMENT 대로 "정식 = 지평 일치 VaR 대비" 다. `config/backtest_params.toml [horizon].method` 의 어휘는 **`block` · `sqrt` · `raw`** 셋뿐이고, `block` 인데 h ≥ 2 전이에 블록 VaR 이 없으면 `backtest()` 는 `ValueError` 로 멈춘다(조용히 raw 로 내려가지 않는다). 귀속(`attribution`)은 정식 초과일 **또는 raw 초과일** 에 저장된다 — 보정으로 초과가 사라진 날도 무엇이 컸는지 남기기 위해서다.

### 6-2. 같은 결정이 마진 MPOR 에 적용된다 (A2, 추론)

CLAUDE.md §1 의 마진 모듈은 2일 MPOR(Margin Period of Risk, 마진 리스크 기간)이다. 위 결정은 백테스트 지평에 대해 내려졌지 MPOR 에 대해 명시되지 않았으나, 근거(EWMA 와 √h 의 모순)는 지평의 용도와 무관하므로 **MPOR 2일 = `fhs.evaluate(horizon=2)` 의 블록 부트스트랩** 으로 읽는다. √2 스케일은 교차확인으로 병기한다. 마진 노트(09)가 이를 확정 문장으로 받는다.

코드 상 알아 둘 것 두 가지(둘 다 `fhs.evaluate` 의 docstring 에만 있다): (1) h = 2 면 시나리오 수가 W − 1 = 499 로 준다 — `risk_runs.n_scenarios` 에 그대로 기록된다. (2) **스트레스 창(`stressed_es`)은 항상 1일로 평가된다.** 스트레스 블렌드를 MPOR 로 하려면 2일 스트레스 ES 가 필요하고, 그것은 A2 가 `fhs` 에 추가해야 하는 확장이다(기존 1일 결과는 바뀌지 않는다).

### 6-3. 커버리지 백테스트의 "실현 2일 손실" 은 HPL 의 정의를 그대로 따른다 (유추)

CLAUDE.md §1 의 "realised 2-day losses" 는 어디에도 정의돼 있지 않다. §1 의 HPL 정의와 같은 규칙을 쓴다: t 의 포지션·수준을 고정하고 **정렬된 다음 두 관측일**(t+1, t+2)의 변화량을 종류별로 누적(log·abs·bp 모두 합)해 `pnl_matrix` 에 한 번 통과시킨 P&L 의 손실(`to_loss`). 휴일·교집합 공백으로 영업일 h > 2 인 전이는 §3 처럼 h 를 기록하고 h일 블록 IM 과 비교한다. 이 함수(`hpl.horizon_pnl` 가칭)는 `daily_pnl` 의 확장이지 대체가 아니다 — 1일 백테스트 결과는 바뀌지 않는다.

### 6-4. DDL·코드에만 있던 규칙 (2026-09-15 전수 조사, 노트 01 §10-5)

- `backtest_results` 의 키는 `run_id` (PRIMARY KEY) 이고 `CHECK (pnl_date > as_of_date)` 가 있다. **일별 행은 upsert** 다(같은 run_id 로 다시 쓰면 값이 갱신되고 `created_at` 이 지금으로 바뀐다). **창 요약은 append-only** 다(노트 00 §3: 재실행 = 새 행). 노트 08 §2 의 "append-only" 는 창 요약을 가리킨다.
- 어휘가 두 개다: `traffic_light IN ('green','yellow','red')` 는 Basel(1996) 용어, `pla_zone IN ('green','amber','red')` 는 FRTB(MAR32) 용어. 같은 색을 두 단어로 쓰는 것은 각 규정의 원어를 그대로 따른 결과이며, 합치지 않는다.
- `load_inputs` 는 **마지막 실행 하나만** t+1 없음을 허용한다. 그 밖에 정렬된 다음 관측일이 없는 실행 날짜가 하나라도 있으면 `ValueError` — 데이터 구멍이 조용히 빠지지 않게(커밋 76db322 "loud failures for dropped run dates").
