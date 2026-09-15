# 설계 노트 09 — 마진: VaR/ES 기반 초기마진 · 안티-프로시클리컬리티 · 레거시 SPAN · 커버리지 백테스트 · Cover-2

- 상태: 작성 2026-09-15. JK 승인 반영: [승인 1] 실행 식별(노트 01 §10-2 (a), 양성 선택) · [승인 2] M2 해석 (i) + 캐리 가정 명시 · [승인 3] 회원 = `portfolio_code`, 위험 성격이 다른 넷 · [승인 4] 0009 새 테이블 4개 · [승인 5] MPOR 블록 부트스트랩, 실현 2일 손실 = HPL 규칙. **§10 의 승인 항목 8개는 대기**, 구현은 단계 1 부터 진행(JK 지시: 승인을 기다리지 않는다, 설계를 되돌려야 하는 분기만 멈춘다)
- 전제: 노트 00 §3 #8(마진 파라미터는 파일에서만, 커버리지 검사는 읽기 전용), 01 §8·§10(저장·식별·미예약 테이블), 02 §10(M1/M2 데이터 의존성), 04(P&L 매핑), 05(FHS), 06 §6(블록 부트스트랩·실현 2일 손실), 07(스트레스 시나리오 — Cover-2 의 충격)
- 위치: `risk_engine.margin.{params,core,span,engine,record,coverage,default_fund,members}`, `config/margin_params.toml`, `config/positions_members.csv`, `db/migrations/0009_margin.sql`

---

## 0. 한 줄 요약

오늘의 장부에 2일 MPOR(Margin Period of Risk, 마진 리스크 기간)의 FHS(Filtered Historical Simulation, 필터링 역사적 시뮬레이션) ES(Expected Shortfall, 기대손실) 99 % 를 재고, 10년 변동성 플로어와 스트레스 기간 블렌드로 반경기순응성을 더하고, 유동성·집중 add-on 을 얹어 IM(Initial Margin, 초기마진)을 만든다. 같은 장부를 레거시 SPAN(Standard Portfolio Analysis of Risk) 16 시나리오로도 재어 세대 차이를 표로 낸다. 일별 IM 계열을 실현 2일 손실과 비교해 CPMI-IOSCO 99 % 커버리지를 검증하고, 위험 성격이 다른 청산회원 넷의 스트레스 손실에서 Cover-2 디폴트 펀드를 산정한다. **파라미터는 전부 `config/margin_params.toml` 에서만 오고, 파일 sha256 이 매 실행에 남으며, 값은 테스트로 고정된다.** 커버리지가 99 % 에 못 미치면 그대로 보고한다(노트 00 §2 #8).

```text
prepare(universe, D) ─▶ ReturnMatrix ─┐
positions(member, D) ─────────────────┼─ core.evaluate(h=2) ─▶ ES99·VaR99·stressed ES·component (2일)
margin_params.toml (sha) ─────────────┘        │
        im_core ──▶ +im_floor (10y σ 플로어) ──▶ +im_stress_blend (25 % 스트레스) ──▶ +add-ons ──▶ im
        span.evaluate ──▶ im_span_legacy (비교)
record.write ─▶ risk_runs(tag margin_batch, horizon 2) + risk_measures(im_*)
coverage.backtest(runs, realised 2일 손실) ─▶ margin_coverage_*        (읽기 전용 → 별도 record)
default_fund.cover2(members × scenarios) ─▶ default_fund_*             (읽기 전용 → 별도 record)
```

## 1. 범위와 데이터 해석 — 현물 장부를 청산 상품으로 (JK 승인 2)

노트 02 §10-4 의 (i) 를 채택한다: 입력은 `instruments`·`prices`·`positions` 그대로이고, FX 현물·에너지 현물·CMT 국채 포지션을 **청산되는 선형 상품인 것처럼** 마진한다. 합성 선물 가격 행은 만들지 않는다(노트 01 §1 위반이고 결과가 바뀌지 않는다).

**캐리 가정과 그 무게 (JK 지적, 2026-09-15).** "합성 선물 F = S·exp((r − c)τ), c = 0, τ 고정, 롤 없음" 은 편의수익·저장비용 c 를 0 으로 둔다. 이 프로젝트에서는 그 가정이 특별한 무게를 갖는다: **WTI 가 2020-04-20 에 −36.98 로 간 메커니즘이 정확히 저장비용과 콘탱고, 즉 캐리였다.** 캐리를 0 으로 두면 그날을 그날로 만든 메커니즘을 모델에서 지우는 셈이다. 일별 변화량이 현물과 같으므로 숫자는 바뀌지 않지만, 이 모델은 (1) 만기 구조·롤·베이시스 리스크를 재지 않고, (2) 현물–선물 cross-margining 의 상계율을 데이터에서 추정할 수 없으며, (3) 옵션을 F = S 로 가격하는 것도 같은 가정 위에 있다. 세 문장을 모델 문서 "알려진 취약점" 절에 그대로 넣는다(노트 02 §10-3 의 두 문장과 함께). Databento(M1) 는 [미확인] 상태이고 이 노트의 전제가 아니다.

## 2. 코어 마진 — FHS ES 99 %, 2일 MPOR (블록 부트스트랩)

| 항목 | 값 | 근거 |
|---|---|---|
| 지평 MPOR | **2 영업일** | [출처] EMIR(European Market Infrastructure Regulation, 유럽 시장인프라 규정) RTS(Regulatory Technical Standards, 규제기술표준) 153/2013 Art. 26: 비장외(non-OTC) 상품의 청산 기간 최소 2 영업일. CPMI-IOSCO PFMI(Principles for Financial Market Infrastructures, 금융시장인프라 원칙, 2012) Principle 6 KC 3 의 "closeout period" |
| 지평 확장 방식 | **h일 블록 부트스트랩** — 잔차 풀에서 연속 h 개 z 벡터를 합쳐(날짜별 공동 샘플 유지) 오늘 σ_T 로 되돌린다. √h 는 교차확인으로 병기 | [JK 승인 5] 노트 06 §6-2. 근거는 백테스트와 같다: EWMA 로 변동성 자기상관을 모형화한 모델이 √h(i.i.d.)로 지평을 늘리면 모순. h = 2 면 시나리오 n = W − 1 = 499 |
| 신뢰수준 | **99 %** (단측) | [출처] PFMI Principle 6 KC 3 "single-tailed confidence level of at least 99 percent"; EMIR RTS Art. 24(1): 비장외 99 %, 장외 99.5 % |
| 코어 측정치 | **ES 99 %** (`im_core`). VaR 99 %·ES 97.5 % 도 같은 실행에 기록 | 같은 α 에서 ES ≥ VaR 이므로 ES 99 % 는 규제 최소치(VaR 99 %)를 항상 만족한다. SPAN 2 가 ES 기반 [추정 — CME 공개 개요, 정확한 α·지평은 상품군별]. **§10 승인 항목 1** — 대안 VaR 99 %(EMIR 문언에 더 가까움). 커버리지 백테스트가 둘 다 낸다 |
| 잔차 풀 W · λ · 워밍업 | 500 · 0.94 · 75 — `risk_params.toml` 재사용, sha 기록 | 노트 05. EMIR RTS Art. 25: 룩백 최소 12개월 + 스트레스 기간 포함 → 500 일과 §3 블렌드로 충족. 마진 전용 값을 따로 두면 리스크 엔진과 마진 엔진이 "다른 모델" 이 되어 비교표의 뜻이 사라진다 |
| 스트레스 창 | 250 일, 오늘 장부에 ES 99 % 2일이 최대인 창 | 노트 05 §2-5 의 정의를 h = 2 로 확장(A1 의 `fhs.evaluate` 는 1일 고정 — 노트 06 §6-2). 마진 엔진이 자체 구현, A1 결과 불변 |
| 컴포넌트 | `component_es`@0.99, scope instrument — 코어의 Euler 분해, 합 = `im_core` | 노트 05 §1. Cover-2 의 회원별 귀속·add-on 의 상품별 근거 |

시나리오 손실 행렬 L (n × k, 손실 양수, `to_loss` 한 곳)은 코어·플로어·블렌드·SPAN 비교가 공유한다. `fhs.evaluate` 는 손실 행렬을 밖으로 내지 않으므로(노트 01 §10 보고 2) 마진 모듈이 `fhs.standardised_residuals`·`fhs.sigma_next`·`pnl.pnl_matrix`·`measures.*` 를 조합해 자체 `core.py` 를 둔다. A1 코드는 건드리지 않는다.

## 3. 안티-프로시클리컬리티 — 카탈로그가 정한 증분 구조 안에서

노트 01 §10-1: 0002 가 `im_floor`·`im_stress_blend` 를 "더해진 금액" 으로 정해 두었다. `im = im_core + im_floor + im_stress_blend + im_liquidity_addon + im_concentration_addon` 이 항등식이고 테스트가 이를 고정한다(component ES 합 = ES 와 같은 형태). EMIR RTS Art. 28(1) 의 세 APC(Anti-Procyclicality, 반경기순응성) 수단 중 **(b)·(c) 를 채택하고 (a) 를 기각**한다.

| EMIR Art. 28(1) | 내용 | 판정 |
|---|---|---|
| (a) 버퍼 | 산출 마진의 ≥ 25 % 를 버퍼로 두고 급등기에 일시 소진 허용 | **기각** — 카탈로그에 measure 가 없고(추가 = 마이그레이션), "언제 소진하고 언제 다시 쌓나" 라는 별도 정책이 필요하다. 그 정책 자체가 사후 조정의 통로가 된다(노트 00 §2 #8). **§10 승인 항목 2** |
| (b) 스트레스 가중 | 룩백의 스트레스 관측에 ≥ 25 % 가중 | **채택** → `im_stress_blend` |
| (c) 10년 플로어 | 10년 룩백 변동성으로 산출한 마진을 하한으로 | **채택** → `im_floor` |

적용 순서와 정의 (L 은 §2 의 손실 행렬 함수, ES 는 ES 99 % 2일):

| 단계 | 정의 | 증분 |
|---|---|---|
| L0 | σ_T (오늘 EWMA 예측) 로 되돌린 ES | `im_core = L0` |
| L1 | 팩터별 σ_used,i = max(σ_T,i, σ_floor,i), σ_floor,i = 최근 `floor.lookback_days`(2,500 ≈ 10 년) 관측의 **비필터 표본 2차 모멘트**의 제곱근(관측이 부족하면 워밍업 이후 전부). L1 = ES(σ_used) | `im_floor = L1 − L0 ≥ 0` (σ 가 커지면 ES 가 단조 증가하므로 음수가 될 수 없다 — 테스트) |
| L2 | ES_stress = §2 의 스트레스 창(250 일 블록, h = 2)을 σ_used 로 되돌린 ES. L2 = (1 − w)·L1 + w·ES_stress, w = `stress_blend.weight` = 0.25 | `im_stress_blend = max(0, L2 − L1) = w·max(0, ES_stress − L1)` |
| add-on | §4 | `im_liquidity_addon`, `im_concentration_addon` |
| 합 | `im = L1 + im_stress_blend + add-ons` | |

플로어를 먼저, 블렌드를 그 위에 두는 이유: EMIR (c) 는 "마진의 하한" 이고 (b) 는 "분포의 구성" 이다. 하한을 먼저 만들고 그 위에서 스트레스를 섞어야 조용한 시장에서 두 장치가 서로를 상쇄하지 않는다. 순서를 바꾸면 블렌드 증분이 플로어에 흡수되어 `im_stress_blend = 0` 이 되는 구간이 생긴다 — 모델 문서 민감도 표에 두 순서를 나란히 낸다.

## 4. Add-on — 설정값이지 추정치가 아니다

v1 소스에는 거래량·미결제약정이 없다(노트 02 §10-3 승인 문장 2). ADV(Average Daily Volume, 일평균 거래량) 기반 파라미터는 **설정값**이고 [임의] 태그를 단다. 메커니즘만 진짜다.

| add-on | 정의 | 값 [임의] | 범위와 이유 |
|---|---|---|---|
| 유동성 | 상품별 총명목 × 자산군 요율. 총명목 = \|q·m·L_i\| (FX 는 USD 환산, 에너지는 q·P, 국채는 액면) | rates 2 bp · fx 2 bp · commodity 5 bp | 청산 시 호가 스프레드의 절반 수준: G10 FX 0.5~1 bp, WTI 틱 0.01/60 ≈ 1.7 bp, 국채 1/64 ≈ 1.6 bp [추정 — 공개 호가 관행]. 2~3배 보수적으로 |
| 집중 | 상품별 총명목이 자산군 임계값을 넘는 초과분 × 요율 | 임계 rates 500 M · fx 100 M · commodity 20 M USD, 요율 0.5 % | 시장 심도에 비례하도록 국채 > FX > 에너지. MAIN 은 어느 상품도 임계 미만 → 0. CM_ENERGY 의 WTI 300,000 bbl(≈ 19 M) 도 미만. 값이 작동하는 것은 회원 장부를 키웠을 때뿐이며 그 민감도를 모델 문서에 |

## 5. 레거시 SPAN 16 시나리오 — 비교용

SPAN 은 상품별 **가격 스캔 범위**(PSR, Price Scan Range)와 변동성 스캔 범위로 만든 16 개 시나리오 중 최악 손실(스캔 리스크)을 상품별로 합치고 상품 간 스프레드 크레딧을 뺀다. 우리 장부는 선형(옵션 없음)이라 변동성 시나리오는 손익을 바꾸지 않는다 — 16 개가 사실상 7 개 가격 이동으로 퇴화한다. PLA 와 같은 종류의 정직한 한계이고 그렇게 적는다.

| 항목 | 정의 | 근거 |
|---|---|---|
| PSR_i | 잔차 풀 500 일의 **비필터** 2일 변화량 절대값의 99 % 경험 분위수 | 노트 02 §10-1 "역사적 변동성 프록시". CCP 는 스캔 범위를 주기적으로 갱신하지 매일 EWMA 로 바꾸지 않는다 — 그래서 SPAN 과 FHS 의 차이가 곧 "필터링과 공동 샘플의 가치" 가 된다 |
| 시나리오 1~14 | 가격 {0, ±⅓, ±⅔, ±1} × PSR × 변동성 {상, 하} | [출처] CME SPAN 방법론 개요의 표준 배열 [추정 — 개요 문서 기준, 거래소별 변형 있음] |
| 시나리오 15·16 | 가격 ±`extreme_multiple` × PSR, 손실의 `extreme_weight` 만 산입 | 2.0 × · 35 % [추정 — 흔히 인용되는 값; 3 × 를 쓰는 곳도 있다]. 선형 장부에서 2 × 35 % = 0.70 PSR 라 1 × 시나리오에 지배되어 **작동하지 않는다**; 3 × 면 1.05 PSR 로 5 % 초과. 민감도 표 |
| 스캔 리스크 | 상품별 16 시나리오 최악 손실 | 선형이면 = \|δ_i\| × PSR_i |
| 상품 간 스프레드 크레딧 | 부호가 반대인 두 상품 쌍에 대해 `rate × min(scan_i, scan_j)`. 쌍과 요율은 파일 `[span.spread_credits]` | [임의] CME 공표 크레딧 비율은 이용조건 [미확인]. SPAN 의 델타 비율 기반 계산을 "작은 쪽 다리" 로 단순화 — 문서화 |
| 상품 내 스프레드 차지 | 0 | 만기가 하나(합성 선물 롤 없음) |
| `im_span_legacy` | Σ 스캔 리스크 − Σ 크레딧 | 비교표: 회원별 `im_span_legacy / im` |

## 6. 커버리지 백테스트 — 읽기 전용, 결과가 나빠도 그대로

| 항목 | 정의 |
|---|---|
| 정식 계열 | `risk_runs` 의 `tag = 'margin_batch'`, `horizon_days = 2`, `method = 'fhs'`, universe `from_1999`, 회원별. 양성 선택(노트 08 §3) |
| 실현 2일 손실 | [JK 승인 5] 노트 06 §6-3: t 의 포지션·수준 고정, 정렬된 다음 두 관측일의 변화량을 종류별로 누적해 `pnl_matrix` 한 번 → `to_loss`. `hpl.horizon_pnl(rm, t, h=2, …)` — `daily_pnl` 의 확장 |
| h > 2 전이 | 영업일 h 를 기록하고 h 일 블록 IM(`im_h_block`, 같은 파라미터로 h 에서 재계산)과 비교. raw(2일 IM 그대로)도 병기 |
| breach | 실현 손실 > IM. 세 기준을 함께 기록: `breach`(정식, `im`), `breach_core`(`im_core` 만 — APC·add-on 이 산 것), `breach_span`(`im_span_legacy`) |
| 창 | 250 일 비중첩 + 오늘로 끝나는 꼬리 창(노트 06 §2 와 같은 규칙) |
| 통계 | 커버리지 = 1 − breaches/n; Kupiec POF at p = 1 − target(0.01), 양측 5 %; 부족분 (loss − im)⁺ 의 최대·평균, `/im` 배수 |
| 기준 | [출처] PFMI Principle 6 KC 3 (99 %) · KC 6 (매일 백테스트); EMIR RTS Art. 47~49 (백테스트, 최소 1 년 창) |
| 읽기 전용 | `coverage.backtest(runs, realised, cfg) -> CoverageReport` 는 커넥션도 마진 파라미터 객체도 받지 않는다(노트 00 §3 #8). `cfg` 는 검정 자체의 창·목표(`[coverage]`)만. 쓰기는 `coverage.record` 하나 |

검정력에 대한 정직한 문장(노트 02 §10-1): 합성 데이터로 마진을 만들고 같은 데이터로 커버리지를 재는 것은 **자기 일관적**이라 통과하기 쉽다. 이 검정이 잡는 것은 "필터 지연"(2020-04-20 류)과 "플로어·블렌드가 조용한 시기에 무엇을 샀나" 이지 모델의 외부 타당성이 아니다.

## 7. Cover-2 디폴트 펀드 — 회원 넷, 성격이 다르게 (JK 승인 3)

**회원 = `portfolio_code`**, `config/positions_members.csv`, 값 고정 테스트(`EXPECTED_POSITIONS_MEMBERS`). `portfolios` 테이블은 만들지 않는다(노트 01 §10-4). **임의 구성**이며 근거는 아래 표와 `decisions.md` §8.

| 회원 | 성격 | 장부 (수량 단위: 노트 04 §4) | 왜 이 구성인가 |
|---|---|---|---|
| `CM_ENERGY` | 에너지 편중 롱 | WTI 300,000 bbl · GASOLINE_NYH 3,000,000 gal · HEATOIL_NYH 1,000,000 gal · HENRYHUB 1,000,000 MMBtu · EURUSD 5 M | 2020-04·2021-02 에 가장 큰 손실. `oil_down_30`·`wti_negative_2020` 이 고른다 |
| `CM_RATES` | 금리 편중 롱 듀레이션 | UST_3M 50 M · UST_2Y 100 M · UST_5Y 80 M · UST_10Y 60 M · UST_30Y 20 M (액면) | 2022 금리 경로가 최악. `rates_2022`·`rates_up_200` 이 고른다 |
| `CM_DIVERSIFIED` | 분산 롱 | EURUSD 15 M · EURJPY 1,500 M JPY · EURGBP 8 M · EURAUD 8 M · WTI 40,000 · HENRYHUB 200,000 · UST_2Y 30 M · UST_5Y 30 M · UST_10Y 20 M | 어느 시나리오에도 1등이 아니어야 정상. 분산 효과의 대조군 |
| **`CM_HEDGED`** | **상관 의존 헤지 장부** | WTI 400,000 롱 − BRENT 400,000 숏 · GASOLINE_NYH 2,000,000 롱 − HEATOIL_NYH 2,000,000 숏 · UST_10Y 50 M 롱 − UST_5Y 100 M 숏 · EURUSD 20 M 롱 − EURGBP 15 M 숏 | **핵심.** A1 에서 상관 붕괴가 ES 를 1.74 배 키운 구조(WTI–Brent 헤지)를 그대로 가진다. 정상 시장에서 IM 이 가장 작고, 상관이 깨지는 시나리오에서 IM 대비 초과분이 가장 크다. **Cover-2 가 이 회원을 고르느냐가 결과의 해석**이다 |

`MAIN` 은 회원이 아니다(리스크 모듈의 장부이고 성격이 `CM_ENERGY` 와 `CM_HEDGED` 사이에 걸친다). 포함하려면 CLI `--members` 로 명시한다.

| 항목 | 정의 | 근거 |
|---|---|---|
| 시나리오 | `config/stress_scenarios.toml` 의 historical(원충격 누적) + hypothetical 전부. 노트 07 의 `stress.historical`·`hypothetical` 을 회원마다 호출 | 충격 크기를 코드가 정하지 않는다(노트 00 §3 #7). 파일 sha 기록 |
| 회원 초과분 | U_{m,s} = (스트레스 손실_{m,s} − IM_m)⁺, IM_m 은 같은 날 기록된 마진 실행(`im_run_id` FK) | "마진을 넘는 손실" 이 디폴트 펀드의 대상 |
| Cover-2 | DF = max_s [ 상위 2 회원의 U_{m,s} 합 ] | [출처] PFMI Principle 4 KC 4 — 두 참가자 디폴트를 극단적이지만 개연적인 조건에서; EMIR Art. 42(3) |
| 산출 | DF, 결정 시나리오, 두 회원, 회원별 U 전표. 배분은 IM 비례(정보용, `allocation = "pro_rata_im"`) [임의] | |
| 민감도 | (1) IM 을 `im_core` 로 바꿨을 때, (2) `im_span_legacy` 로 바꿨을 때의 DF — "마진 수준 민감도"(gap plan). 담보 헤어컷은 모형화하지 않는다(현금 담보 가정, 한계에 기록) | |

## 8. 저장 — 0009 (JK 승인 4, 기존 테이블·제약 불변)

마진 실행 헤더·측정치는 `risk_runs`·`risk_measures`(노트 01 §10-1). 검증 산출물 4 테이블과 COMMENT 정정 3 건:

```sql
-- 0009_margin: margin coverage backtest, Cover-2 default fund; COMMENT corrections from the
-- 2026-09-15 audit (note 01 §10-5). No existing table or constraint is changed.

COMMENT ON COLUMN risk_runs.tag IS
  'daily_batch = official 1-day series for the risk backtest; margin_batch = official 2-day MPOR series for the margin coverage backtest; adhoc / experiment / margin_adhoc otherwise. Readers select (tag, method, horizon_days) positively (note 08 §3).';
COMMENT ON COLUMN instruments.multiplier IS
  'Contract multiplier m. P&L per note 04 §4: log kind q*m*S*(exp(x)-1); abs kind q*m*x; bp kind -DV01*x with DV01 = q*m*D_mod*1e-4.';
COMMENT ON COLUMN instruments.quote_type IS
  'price = level in the quote currency; yield = percent. How the series is differenced is return_type (0003), not quote_type.';

CREATE TABLE margin_coverage_results (
    run_id           BIGINT           PRIMARY KEY REFERENCES risk_runs (run_id) ON DELETE CASCADE,
    universe         TEXT             NOT NULL,
    portfolio_code   TEXT             NOT NULL,
    as_of_date       DATE             NOT NULL,   -- t: the margin run's date
    pnl_date         DATE             NOT NULL,   -- t+2: second aligned observation after t
    h_business_days  INTEGER          NOT NULL,   -- business days t -> pnl_date (2 unless a gap)
    realised_loss    DOUBLE PRECISION NOT NULL,   -- loss positive, base currency, positions fixed at t
    im               DOUBLE PRECISION NOT NULL,   -- official IM on t (measure 'im')
    im_core          DOUBLE PRECISION NOT NULL,
    im_span_legacy   DOUBLE PRECISION,
    im_h_block       DOUBLE PRECISION,            -- IM recomputed at horizon h (NULL when h = 2)
    breach           BOOLEAN          NOT NULL,   -- official: realised_loss > (im_h_block if h > 2 else im)
    breach_raw       BOOLEAN          NOT NULL,   -- realised_loss > im (2-day IM regardless of h)
    breach_core      BOOLEAN          NOT NULL,   -- realised_loss > im_core
    breach_span      BOOLEAN,                     -- realised_loss > im_span_legacy
    shortfall        DOUBLE PRECISION NOT NULL,   -- max(0, realised_loss - im)
    attribution      JSONB            NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (pnl_date > as_of_date)
);
CREATE INDEX margin_coverage_results_series_idx ON margin_coverage_results (universe, portfolio_code, as_of_date);
COMMENT ON TABLE margin_coverage_results IS 'One row per margin run date: realised 2-day loss vs the IM recorded on that date. Read-only inputs (risk_runs, risk_measures, prices); written only by risk_engine.margin.coverage.record.';

CREATE TABLE margin_coverage_summaries (
    summary_id       BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    universe         TEXT             NOT NULL,
    portfolio_code   TEXT             NOT NULL,
    window_start     DATE             NOT NULL,
    window_end       DATE             NOT NULL,
    n_obs            INTEGER          NOT NULL,
    breaches         INTEGER          NOT NULL,
    breaches_raw     INTEGER          NOT NULL,
    breaches_core    INTEGER          NOT NULL,
    breaches_span    INTEGER,
    coverage         DOUBLE PRECISION NOT NULL,   -- 1 - breaches / n_obs
    target           DOUBLE PRECISION NOT NULL,   -- 0.99 (CPMI-IOSCO)
    kupiec_lr        DOUBLE PRECISION,
    kupiec_p         DOUBLE PRECISION,
    max_shortfall    DOUBLE PRECISION NOT NULL,
    max_shortfall_over_im DOUBLE PRECISION,
    params           JSONB            NOT NULL DEFAULT '{}'::jsonb,   -- margin_params sha256, coverage cfg
    code_version     TEXT,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX margin_coverage_summaries_series_idx ON margin_coverage_summaries (universe, portfolio_code, window_end);
COMMENT ON TABLE margin_coverage_summaries IS 'Window statistics of the margin coverage backtest. A re-run is a new row with a different params sha256, never an update.';

CREATE TABLE default_fund_runs (
    default_fund_run_id  BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    universe             TEXT             NOT NULL,
    as_of_date           DATE             NOT NULL,
    scenario_set_sha256  TEXT             NOT NULL,   -- config/stress_scenarios.toml
    margin_params_sha256 TEXT             NOT NULL,   -- config/margin_params.toml
    n_members            INTEGER          NOT NULL,
    cover                INTEGER          NOT NULL,   -- 2
    default_fund         DOUBLE PRECISION NOT NULL,   -- max over scenarios of the top-`cover` uncovered losses
    binding_scenario     TEXT             NOT NULL,
    binding_members      TEXT[]           NOT NULL,
    params               JSONB            NOT NULL DEFAULT '{}'::jsonb,
    code_version         TEXT,
    created_at           TIMESTAMPTZ      NOT NULL DEFAULT now()
);
COMMENT ON TABLE default_fund_runs IS 'One Cover-N sizing of the default fund on a date: members are portfolio_code values, shocks come only from the scenario file (sha256 recorded).';

CREATE TABLE default_fund_results (
    default_fund_run_id BIGINT           NOT NULL REFERENCES default_fund_runs (default_fund_run_id) ON DELETE CASCADE,
    scenario            TEXT             NOT NULL,
    kind                TEXT             NOT NULL CHECK (kind IN ('historical', 'hypothetical')),
    portfolio_code      TEXT             NOT NULL,   -- the clearing member
    im_run_id           BIGINT           NOT NULL REFERENCES risk_runs (run_id),   -- margin run supplying im
    stress_loss         DOUBLE PRECISION NOT NULL,   -- loss positive
    im                  DOUBLE PRECISION NOT NULL,
    uncovered           DOUBLE PRECISION NOT NULL,   -- max(0, stress_loss - im)
    UNIQUE (default_fund_run_id, scenario, portfolio_code)
);
COMMENT ON TABLE default_fund_results IS 'Per (scenario, member): stress loss, the IM recorded for that member on the date, and the uncovered excess that the default fund must absorb.';
```

## 9. 읽기 전용 강제와 값 고정

| 장치 | 형태 |
|---|---|
| 파라미터 | `MarginParams`(frozen) ← `config/margin_params.toml` 만. sha256 을 `risk_runs.params.margin_params_sha256`, `margin_coverage_summaries.params`, `default_fund_runs.margin_params_sha256` 에 |
| 값 고정 | `tests/test_config_pins.py::EXPECTED_MARGIN_PARAMS`(노트 00 §3-1 예약 자리) · `EXPECTED_POSITIONS_MEMBERS` |
| 순수 함수 | `core.evaluate`·`span.evaluate`·`coverage.backtest`·`default_fund.cover2` 는 커넥션을 받지 않는다. DB 읽기는 `engine.prepare`·`positions.load_snapshot`·`coverage.load_inputs`·`default_fund.load_inputs`, 쓰기는 `record.write`·`coverage.record`·`default_fund.record` 셋 |
| 항등식 테스트 | `im == im_core + im_floor + im_stress_blend + im_liquidity_addon + im_concentration_addon` (1e-9), 모든 증분 ≥ 0, component 합 = `im_core` |
| 사후 조정 금지 | 커버리지 결과를 보고 플로어를 바꾸는 코드 경로는 없다. 바꾸면 파일 sha 가 바뀌고 값 고정 테스트가 깨지며 새 마진 실행이 필요하다(노트 00 §3 #8) |

`config/margin_params.toml` (초안 — 값은 §10 승인 대상):

```toml
# Margin parameters (design note 09). Every value is pinned by tests/test_config_pins.py and
# the file's sha256 is written to every margin run, coverage summary and default-fund run.
# Regulatory anchors: CPMI-IOSCO PFMI (2012) Principle 6; EMIR RTS 153/2013 Art. 24-28, 42.

[core]
confidence = 0.99        # PFMI P6 KC3 / EMIR RTS Art. 24: >= 99 % single-tailed (non-OTC)
measure = "es"           # "es" | "var": core = that measure at `confidence` over the MPOR
mpor_days = 2            # EMIR RTS Art. 26: >= 2 business days for non-OTC; h-day block bootstrap

[floor]
lookback_days = 2500     # EMIR RTS Art. 28(1)(c): volatility from a 10-year lookback as the floor

[stress_blend]
weight = 0.25            # EMIR RTS Art. 28(1)(b): >= 25 % weight on stressed observations
window_days = 250        # worst 12-month window for this book at the MPOR (as note 05 §2-5)

[liquidity]              # bp of gross notional per instrument, by asset class (settings, not
rates_bp = 2.0           # estimates: the v1 sources carry no volume — note 02 §10-3)
fx_bp = 2.0
commodity_bp = 5.0

[concentration]          # charge on gross notional above the threshold (settings, see above)
rate = 0.005
[concentration.threshold_usd]
rates = 500000000
fx = 100000000
commodity = 20000000

[span]                   # legacy SPAN 16-scenario comparison (note 09 §5)
scan_confidence = 0.99   # PSR = empirical 99 % quantile of |h-day change| over the residual pool
extreme_multiple = 2.0   # scenarios 15/16: price +/- multiple x PSR ...
extreme_weight = 0.35    # ... counted at this fraction of the loss
vol_scan_pct = 0.0       # linear book: volatility scenarios do not move P&L; kept for shape
[span.spread_credits]    # inter-commodity spread credit rate on the smaller leg, opposite signs
"WTI/BRENT" = 0.80
"WTI/GASOLINE_NYH" = 0.60
"WTI/HEATOIL_NYH" = 0.60
"GASOLINE_NYH/HEATOIL_NYH" = 0.50
"UST_2Y/UST_5Y" = 0.70
"UST_5Y/UST_10Y" = 0.70
"UST_10Y/UST_30Y" = 0.60
"UST_3M/UST_2Y" = 0.40
"EURUSD/EURGBP" = 0.50

[coverage]
target = 0.99            # CPMI-IOSCO 99 % coverage
window_days = 250

[default_fund]
cover = 2                # PFMI P4 KC4 / EMIR Art. 42(3): the two largest exposures
allocation = "pro_rata_im"
```

## 10. 승인 항목

| # | 항목 | 제안 | 대안 |
|---|---|---|---|
| 1 | 코어 측정치 | ES 99 % 2일 | VaR 99 % 2일 (EMIR 문언). 두 값을 모두 기록하므로 바꿔도 재실행 불필요 — `[core].measure` 만 |
| 2 | APC 수단 | (b) 25 % 스트레스 블렌드 + (c) 10년 σ 플로어, 플로어 먼저 | (a) 25 % 버퍼 추가(카탈로그 마이그레이션 + 소진 정책 필요) / 순서 반대 |
| 3 | 플로어 정의 | 팩터별 10년 비필터 σ 로 σ_T 를 하한 | 포트폴리오 수준(10년 풀의 ES 를 하한) — 헤지 장부에서 팩터별 플로어보다 작을 수 있다 |
| 4 | 유동성·집중 값 | §4 의 [임의] 값 | 전부 0 (add-on 없음)으로 두고 메커니즘만 |
| 5 | SPAN PSR 원천 | 비필터 500 일 99 % 경험 분위수 | EWMA σ_T × z × √2 (FHS 와 같은 σ → 차이가 공동 샘플 효과만 남음) |
| 6 | SPAN 극단 시나리오 | 2 × PSR, 35 % | 3 × PSR (선형 장부에서만 작동) |
| 7 | 회원 넷의 수량 | §7 표 | 규모 조정(IM 이 비슷해지도록) — 단 성격 차이는 유지 |
| 8 | Cover-2 시나리오 집합 | historical + hypothetical 전부(상관 붕괴 제외 — 회원별 FHS 라 정의가 다름) | historical 만 |

## 11. 구현 단계 (JK 지시: 노트를 낸 뒤 승인을 기다리지 않고 단계 1 부터)

| 단계 | 내용 | 커밋 |
|---|---|---|
| 1 | `margin.params`(frozen, sha) · `margin.core`(2일 손실 행렬, ES/VaR, 스트레스 창, 플로어, 블렌드, add-on, 항등식) · `margin.span` · 순수 테스트 · `config/margin_params.toml` + 값 고정 | `feat(margin): core IM engine — FHS ES 99 % at 2-day MPOR, 10y floor, stress blend, add-ons, legacy SPAN` |
| 2 | 0009 · `margin.engine.run` · `margin.record.write`(`risk_runs` 재사용, tag `margin_batch`, horizon 2) · CLI `run|backfill|load-members` · `config/positions_members.csv` + 값 고정 · DB 테스트 | `feat(margin): margin runs recorded in risk_runs; members book; 0009` |
| 3 | `hpl.horizon_pnl` · `margin.coverage`(load_inputs 읽기 전용, backtest 순수, record) · CLI `coverage` | `feat(margin): coverage backtest against realised 2-day losses` |
| 4 | `margin.default_fund`(cover2 순수, record) · CLI `default-fund` | `feat(margin): Cover-2 default fund` |
| 5 | 실행·결과 기록: `decisions.md` §8, `walkthrough.md` 7, 모델 문서 v1.2(마진 절·한계 §7-5·변경 이력), API `/margin`·`/margin/coverage`·`/default-fund`, 대시보드 마진 구역, README | `docs:` · `feat(ci):` |

## 12. 어디서 틀릴 수 있나

- **캐리 0** (§1): 2020-04-20 을 만든 메커니즘이 모델에 없다. 숫자는 현물과 같지만 해석이 다르다.
- **자기 일관적 커버리지** (§6): 통과가 외부 타당성을 뜻하지 않는다. 필터 지연과 APC 의 효과만 잰다.
- **블록 부트스트랩 499 시나리오**: 2일 합 블록은 인접 블록이 하루를 공유해 독립이 아니다. ES 99 % 의 꼬리 표본은 4.99 개 — 1일(12.5 개, 97.5 %)보다 거칠다. 모델 문서에 표본 수를 적는다.
- **플로어의 10년**: `from_1999` 첫 10년은 2,500 관측이 안 되어 워밍업 이후 전부로 대체된다. 2009 년 이전 `im_floor` 는 "가용 이력 플로어" 다.
- **SPAN 의 퇴화**: 선형 장부라 변동성 시나리오와 극단 시나리오(2 ×, 35 %)가 작동하지 않는다. 비교표의 차이는 필터링·공동 샘플·스프레드 크레딧 단순화의 합이다.
- **회원 넷은 임의**: 어느 회원이 Cover-2 를 결정하느냐가 결과이지, 디폴트 펀드 금액 자체에는 뜻이 없다.
- **h > 2 전이**: 연휴·교집합 공백 뒤의 실현 손실은 3~11 영업일치다. 정식은 h 일 블록 IM 과 비교하고 raw 를 병기한다(노트 06 §7-2 와 같은 구조).
