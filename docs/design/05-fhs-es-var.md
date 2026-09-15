# 설계 노트 05 — FHS ES 97.5 % · VaR 99 %, Parametric, Monte Carlo

- 상태: 작성과 동시에 구현 (2026-09-13). 세 가지 임의성(§3)은 JK 지시로 선택지·근거·민감도를 명시
- 전제: 노트 01(risk_runs·risk_measures·손실 양수), 04(변화량 X, P&L 매핑), 00(§3 결과를 보고 입력을 바꾸지 않는다)
- 위치: `risk_engine.risk.{volatility,fhs,parametric,montecarlo,measures,engine,record,positions}`, `config/risk_params.toml`

---

## 0. 한 줄 요약

오늘의 포지션에 과거 변화량을 "오늘 변동성으로 다시 스케일해" 적용한 P&L 분포에서 ES 97.5 %와 VaR 99 %를 읽는다(FHS). 같은 포지션·같은 공분산 추정에 정규분포를 가정하면 Parametric, 정규분포에서 경로를 뽑아 정확한 P&L 매핑을 통과시키면 Monte Carlo 다. 세 방법이 같은 X·같은 매핑·같은 손실 정의를 쓰므로 차이는 분포 가정뿐이다. **부호 뒤집기(`loss = -pnl`)는 `measures.to_loss` 한 곳.**

```text
ReturnMatrix X ─volatility.ewma/garch─▶ σ_{i,t}  ─▶ z = x/σ (표준화 잔차)
                                                     └─▶ FHS: 시나리오 x* = z_s × σ_{i,T}  (s ∈ 잔차 풀, 날짜별 공동 샘플)
positions ─pnl.pnl_matrix(x*, levels_T)─▶ P&L (n × 상품) ─행 합─▶ 포트폴리오 P&L ─to_loss─▶ VaR·ES
```

## 1. 방법

| 방법 | 분포 | 시나리오 | 비선형 | 출처 |
|---|---|---|---|---|
| **FHS** (기본) | 경험분포, 날짜별 공동 샘플 | z_s (s = 잔차 풀 W일) × σ_T → n = W | 정확한 P&L 매핑 (FX 지수식·DV01) | Barone-Adesi, Giannopoulos & Vosper (1999); Hull & White (1998) |
| Parametric | 다변량 정규, EWMA 공분산 | 없음 (닫힌 식) | 선형 근사 δᵀx | RiskMetrics Technical Document (1996) |
| Monte Carlo | 다변량 정규, EWMA 공분산 | N 경로 (고정 seed) | 정확한 P&L 매핑 | 표준. 정규 가정은 Parametric 과 같아 둘의 차이가 곧 비선형성의 크기 |

**측정치** (전부 손실 양수, 기준통화 USD, `risk_measures`): `var`(0.99)·`es`(0.975)·`stressed_es`(0.975, §2-5)·`component_es`(scope instrument: 꼬리 시나리오에서 상품별 손실 평균 — Euler 기여, 합 = ES) — FHS·MC 에서. Parametric 은 `var`·`es` 만(선형이라 component 는 δ_i Σ δ / σ 로 별도 정의, 3주차).

## 2. FHS 세부

### 2-1. 변동성 필터

- **EWMA**: σ²_t = λ σ²_{t−1} + (1−λ) x²_{t−1}, **λ = 0.94** [출처: RiskMetrics TD 1996 §5.3, 일별 최적 λ]. 평균은 0 으로 둔다(일별 변화량의 평균은 변동성 대비 무시 가능 — RiskMetrics 같은 가정).
- **GARCH(1,1)**: σ²_t = ω + α x²_{t−1} + β σ²_{t−1}, 계열별 MLE(정규 우도), 분산 타게팅 ω = σ̄²(1−α−β) [출처: Engle & Mezrich (1996) variance targeting; Bollerslev (1986)]. 기본 필터는 EWMA 이고 GARCH 는 `method='fhs', params.vol='garch'` 로 비교 실행.

### 2-2. 표준화 잔차 풀 — 자산별 스케일, 날짜별 공동 샘플 (임의성 1)

| 선택지 | 내용 | 대가 |
|---|---|---|
| **A (채택)** 자산별 σ 로 표준화, 잔차 벡터 z_s = (x_{1,s}/σ_{1,s}, …, x_{k,s}/σ_{k,s}) 를 **날짜 s 단위로 함께** 뽑는다 | 각 자산의 꼬리 형태와 자산 간 동시 움직임(상관·꼬리 종속)이 그날 그대로 보존 | 표본 = W 일 (500). 자산 수가 늘어도 표본은 안 는다 |
| B 전체 자산 잔차를 한 풀에 섞고 자산마다 독립 추출 | 표본 k×W | **자산 간 종속성이 사라지고**(2008 의 동시 붕괴가 없어짐) 자산별 꼬리 특성(WTI 의 −55 vs FX 의 3σ)이 섞인다 — 포트폴리오 리스크 측정으로 부적합 |
| C 자산별 풀에서 자산마다 독립 추출 | 꼬리 형태 보존 | 종속성 소실 — 분산 효과 과대 |

근거: FHS 원문(BAGV 1999)이 A 다. 날짜별 공동 샘플이 "historical" 의 의미다. B·C 는 단변량 분포 추정엔 쓸 수 있어도 포트폴리오 ES 엔 못 쓴다. 민감도: A 대 C 의 ES 차이가 곧 분산 효과의 크기 — 모델 문서에 표로.

### 2-3. EWMA 초기값과 워밍업 (임의성 2)

- 초기값 σ²_0 = 첫 **W₀ = 75** 관측의 표본 분산. 이유: λ = 0.94 에서 가중치의 99 % 가 최근 74 일에 있다(1−λⁿ ≥ 0.99 → n = ln 0.01 / ln 0.94 = 74.4) [출처: RiskMetrics TD 1996 §5.3.2 "effective number of days"]. 75 일이 지나면 초기값의 영향이 1 % 미만.
- **워밍업 제외**: 처음 75 일은 잔차 풀에도, 백테스트 표본에도 넣지 않는다. 기본 집합(5,031 관측)에서 75 일은 1.5 %.
- 대안: σ²_0 = 전체 표본 분산(미래 정보 유입, 기각) / 워밍업 0 일(첫 구간 z 가 과대·과소) / 250 일(불필요하게 5 % 손실). 민감도: 워밍업 75 vs 250 의 첫 해 ES 차이 — 모델 문서.

### 2-4. 추정창과 잔차 풀 기간의 구분 (임의성 3)

- EWMA 는 창이 없다(지수 가중, 유효 기억 ≈ 74 일). 이는 **오늘의 σ_T** 를 정하는 데 쓰인다.
- 잔차 풀 **W = 500 영업일**(≈2 년)은 **어떤 z 를 뽑을 것인가** 를 정한다. 다른 개념이다. 500 의 근거: Basel II 최소 250 일(1 년) [출처: BCBS 1996/2006 §718(Lxxvi)]; Pritsker (2006) 는 250 일 FHS 의 꼬리 표본 부족을 지적; 2 년은 FRTB 내부모형의 통상 관측 기간과 SPAN 2 류 CCP 룩백(1~3 년)의 중간 [임의 — 250~1,000 범위]. 민감도: W ∈ {250, 500, 1000} 의 ES 와 Kupiec 결과 — 모델 문서 표.
- 시나리오 수 n = W (풀의 모든 날짜를 한 번씩). 부트스트랩 재추출은 하지 않는다 — 표본 그대로가 "역사적".

### 2-5. 스트레스 ES (FRTB 식)

현재 포지션으로 표본 안의 모든 250 일 창을 훑어 ES 가 최대인 창을 찾고 그 창의 잔차 풀로 ES 97.5 % 를 계산한다 [출처: BCBS FRTB(2019) MAR33.5 — 은행 포트폴리오에 가장 심각한 12 개월 스트레스 기간]. 결과 `stressed_es` 와 창의 시작·끝을 `params.stressed_window` 에 기록.

## 3. ES·VaR 정의 (`measures.py`)

- 손실 L = −P&L (**유일한 부호 반전 지점**).
- VaR_α = L 의 경험 α-분위수: 손실 오름차순 정렬 후 ⌈α·n⌉ 번째 [출처: Basel 관행 — n=500, α=0.99 → 5 번째로 큰 손실].
- ES_α = Acerbi & Tasche (2002) 의 분수 가중 정의: 상위 (1−α)n 개 손실의 평균, 경계 관측은 분수 가중. n=500, α=0.975 → 12.5 개.
- 고정 파라미터 `config/risk_params.toml`: λ 0.94 · W 500 · 워밍업 75 · α_VaR 0.99 · α_ES 0.975 · 지평 1 일 · 스트레스 창 250 · MC 경로 10,000 · seed 20260913. 전부 `tests/test_config_pins.py` 에 고정, 실행마다 파일 sha256 을 `risk_runs.params` 에.

## 4. Parametric · Monte Carlo

- 공분산 Σ_T: EWMA(λ) 공분산 [RiskMetrics]. 델타 δ: FX q·S, 에너지 q, 국채 −DV01 (노트 04 §4 의 1 차 계수). σ_p = √(δᵀΣδ). VaR = z_α σ_p, ES = φ(z_α)/(1−α) · σ_p.
- MC: x ~ N(0, Σ_T), N = 10,000 [임의 — 99 % 분위수의 표준오차가 ES 의 ~2 % 수준; 100,000 은 backfill 5,000 일에서 비용만 증가], seed 고정 → 재현 가능. P&L 은 정확 매핑.

## 5. 실행과 기록 (`engine.py`, `record.py`, `positions.py`)

- 포지션: `positions` 테이블에서 `as_of_date ≤ D` 최신 스냅샷(노트 01 §2-3). 초기 포트폴리오 `MAIN` 은 `config/positions_main.csv` 로 적재(별도 쓰기 명령).
- `run(conn, portfolio, as_of, method, universe, params)` → `RunResult`(측정치 목록, params, meta) → `record.write(conn, result)` 가 `risk_runs` + `risk_measures` 를 **한 트랜잭션**으로.
- `risk_runs.params` 에: universe 이름·sha, align·dropped·filled, λ·W·워밍업·α, stressed_window, vol 필터, seed, `risk_params.toml` sha, 최신 데이터 날짜(`data_as_of` 예약 컬럼 대신 params 로 우선).
- `backfill --from --to --tag daily_batch`: 날짜마다 실행(백테스트 재료). 5,000 일 × FHS ≈ 수 분.
- CLI: `python -m risk_engine.risk run|backfill|load-positions`.

## 6. 어디서 틀릴 수 있나

- EWMA 는 변동성 군집은 잡지만 **레짐 전환 첫날**은 늦다(2020-03). 그 지연이 곧 백테스트 초과의 원천이고, 그것을 숨기지 않는다.
- FHS 는 **풀에 없는 사건**을 만들 수 없다. W=500 이면 2008 은 2010 년 이후 풀에서 사라진다 — stressed_es 가 그 보완이다.
- 날짜별 공동 샘플은 **상관 붕괴**를 풀에 있는 만큼만 재현한다. 스트레스 모듈(노트 06)에서 상관을 명시적으로 깨뜨린다.
- 표준화 잔차의 평균이 0 이 아닐 수 있다(추세 구간). 재중심화하지 않는다 — 하면 "역사적" 이 아니게 된다. 모델 문서에 잔차 평균을 보고.

## 7. 보강 (2026-09-15) — 코드에만 있던 규칙

- **`risk_params.toml [measures].horizon_days` 는 기록용이지 계산 인자가 아니다.** `engine.run` 은 항상 1일로 평가하고(`fhs.evaluate(horizon=1)`), 파일의 `horizon_days` 값을 `risk_runs.horizon_days` 에 그대로 쓴다. 파일을 2 로 바꾸면 "2일" 로 기록된 1일 실행이 생긴다. 값 고정 테스트가 1 을 박고 있어 지금은 안전하지만, **지평을 바꾸는 유일한 경로는 `evaluate(horizon=)` 인자**다. 마진 실행(노트 09)은 이 둘을 한 곳에서 같이 정한다.
- **실행이 조용히 축소되지 않는다.** `engine.run` 은 (1) 포지션 티커가 표본 집합 밖이면 `KeyError`, (2) 수익률 행렬의 마지막 날짜가 `as_of` 를 넘으면 `ValueError` 로 멈춘다. 없는 상품을 무시하고 나머지로 계산한 VaR 이 정상으로 보이는 경로를 막기 위한 것이다(모델 문서 §5 의 "빈 포지션 VaR" 결함과 같은 계열).
- **`sigma_next` 의 GARCH 경로는 `warmup` 인자를 쓰지 않고 `min(75, n−2)` 로 재적합한다.** GARCH 는 비교 실행(`--vol garch`) 전용이고 정식 계열은 EWMA 라 결과에 영향은 없으나, GARCH 를 정식으로 올리려면 이 불일치부터 고쳐야 한다.
