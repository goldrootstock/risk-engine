# 결정 기록 — 파라미터와 가정

모든 값의 출처를 여기 둔다. "임의 선택"이면 그렇게 쓴다. 값을 바꾸면 이 표와 `tests/test_config_pins.py`를 같은 커밋에서 바꾼다(노트 00 §3-1).

표기: [출처] = 문서·논문·규정, [벤더 관행] = 데이터 벤더·CCP 공개 문서, [임의] = 근거 없는 선택(범위와 이유 기재).

## 1. ETL (데이터 적재)

| 이름 | 값 | 쓰이는 곳 | 근거 | 검토한 대안과 기각 이유 | 바꾸면 달라지는 것 |
|---|---|---|---|---|---|
| 재수집 겹침 창 `OVERLAP_DAYS` | 14 달력일 (≈10 영업일) | `etl --incremental`, EIA·재무부 `start` 계산 | [임의] 벤더 정정은 보통 며칠 내 [추정]. 영업일 달력 없이 쓰려고 달력일로. ETL 첫 달의 `etl_runs.rows_updated` 분포로 검증 예정 | 30일: 트래픽 2배, 이득 없음 [추정]. 0일: 정정을 놓침 | 늘리면 매일 받는 행 수와 `rows_unchanged`가 비례해 증가. 정정 검출 범위 확대 |
| 재시도 횟수 `attempts` | 3 | `etl.http.fetch_with_retry` | [벤더 관행] AWS SDK·requests-urllib3 기본 재시도 3회 | 5회: 장애 시 대기 시간 31 s로 증가. 1회: 일시 오류에 취약 | 소진 시 `RetryExhaustedError`, 해당 소스 전체 series가 `failed` |
| 백오프 `backoff_seconds` | 1 s, 지수(1·2) | 같음 | [벤더 관행] exponential backoff 표준(Google API 설계 가이드) | 고정 대기: 서버 과부하 시 동시 재시도 집중 | 대기 총량. 429 처리 여유 |
| 타임아웃 `timeout_seconds` | 30 s | `httpx.Client` | [벤더 관행] httpx 기본 5 s는 EIA 2.8 MB 페이지에 부족 [확인 2026-09-13: 실측 응답 크기]. 30 s는 requests 커뮤니티 관행 | 무제한: 행 걸림. 5 s: EIA 실패 | 느린 네트워크에서 실패율 |
| 재시도 상태코드 | 429·500·502·503·504 | 같음 | [출처] RFC 9110 5xx 정의 + 429 (RFC 6585) | 모든 4xx 재시도: 404·403은 재시도해도 같음 | 잘못된 URL이 3회 재시도로 지연되는지 여부 |
| User-Agent | `risk-engine/0.1 (+https://github.com/goldrootstock/risk-engine)` (설명형) | `etl.http.client_for` 기본 클라이언트 | 도구가 무엇인지 그대로 밝힌다. 현재 소스 3개(ECB·FRED·EIA)는 이 UA 를 받는다 [확인 2026-09-13] | **시도했으나 채택하지 않음 — 재무부 WAF 우회.** home.treasury.gov 는 6종 UA 시험에서 `python-httpx/0.28.1`·`curl/8.7.1`·설명형·Chrome 문자열 뒤 도구명 추가 → ReadTimeout, Chrome·Firefox 원문과 "Firefox 괄호 안 도구명" → 200 [확인 2026-09-13]. 후자는 기술적으로 동작하고 도구명도 들어 있었지만, 공개 저장소에 WAF 우회가 남는 것은 원치 않는다는 JK 결정으로 기각. **기술적으로 가능한 것과 해도 되는 것은 다르다.** 재무부 소스 자체를 FRED 로 교체 | UA 를 브라우저 형식으로 바꾸면 재무부 직접 수집이 가능해지지만 그 선택은 하지 않는다 |
| 국채 수익률 소스 | FRED `DGS1MO…DGS30` (연준 H.15) | `sources/fred.py`, `config/universe.csv` rates 11행 | [출처] FRED 계열 페이지 "Public Domain: Citation Requested", 원천 = Board of Governors H.15 [확인 2026-09-12]. 공식 REST API, 무료 키, 프로그램 접근 명시 [확인 2026-09-13: API 문서]. 재무부 값과 겹치는 모든 날짜에서 **불일치 0건** — 1차 `fredgraph.csv` 기준, 2차 **실제 API 응답으로 sync 하여 11계열 전부 `updated=0`** (unchanged 6,280 + 9,176 + 9,179×7 + 8,240 + 8,185 = 96,134; 나머지 11행은 FRED 미공표 2026-09-11) [확인 2026-09-13] | 재무부 직접: WAF (위). 연준 DDP: 별개 서비스이며 축소 중 [확인 2026-09-12 공지] | FRED 는 H.15 를 **T+1** 로 반영 — 재무부보다 하루 늦다(2026-09-13 대조에서 재무부에만 09-11 존재). 이력은 더 길다(DGS10 1962~) |
| API 키 전달 | EIA: `X-Api-Key` **헤더** / FRED: `api_key` 쿼리 파라미터 | `sources/eia.py`, `sources/fred.py`, `etl.http` | EIA v2 는 헤더를 받는다 [확인 2026-09-13: 헤더만으로 200]. 단 헤더로 보내도 응답 `request.params.api_key` 에 키가 **에코된다** [확인 2026-09-13] → 바이트 스크럽 유지. FRED: v2 는 `Authorization: Bearer` 헤더를 쓰지만 **v2 에는 `release/observations`(릴리스 단위 벌크) 하나뿐이고 `series/observations` 가 없다** [확인 2026-09-13: `fred/v2/series/observations` 등 5개 URL 변형 404, v1 엔드포인트에 Bearer 는 400 "api_key is not set"]. 그래서 v1 쿼리 파라미터 유지. FRED 는 응답에 키를 에코하지 않는다 [확인 2026-09-13] | v2 `release/observations`(H.15 release_id 18)로 11계열을 한 요청에 받는 안: Bearer 라 키가 URL 에 안 들어가지만 응답 형식이 다르고 릴리스 전체(수백 계열)를 받는다 — 노트 04 이후 재검토 | `etl.http` 는 예외 메시지와 로그에 URL 경로만 넣고 쿼리스트링을 절대 넣지 않는다 (`test_exception_messages_never_contain_the_query_string`) |
| EIA 페이지 길이 | 5,000 | `EiaSource.fetch` | [출처] EIA API v2 문서 최대값 [확인 2026-09-12] | 더 작게: 요청 수 증가 | 페이지 수·캐시 파일 수 |
| ECB 증분 미채택 | 매 sync 전체 zip | `EcbSource.fetch` | 파서 하나 유지(CSV). 90일 XML 존재 [확인 2026-09-13] | XML 증분: 파서 둘 유지 비용 | 연 160 MB 캐시. 1 GB 재검토 시 XML 전환 |
| 캐시 보존 | 무기한, 1 GB 도달 시 재검토 | `RawCache` | 연 ≈170 MB 실측 기반 추정(노트 03 §13) | 자동 prune: 재현 불가 구간 생김 | — |
| 검증: log 계열 jump | \|단순수익률\| > 30 % | `validate()`, `config/validation.toml` | [임의] FX 일일 30 %는 페그 붕괴 수준(2015-01 CHF: 약 20 %). 정상 변동의 10배 이상 | 10 %: 2015 CHF·2016 GBP 등 실제 사건 경고 다수 — 경고는 검토용이라 그것도 무방 | 경고 건수만. 적재는 불변 |
| 검증: yield 계열 jump | \|Δy\| > 100 bp | 같음 | [임의] 2020-03 국채 일일 변동 최대 약 30~40 bp [추정]. 100 bp는 정책 오류·데이터 오류 수준 | 50 bp: 2022 이후 단기물에서 경고 다수 | 같음 |
| 검증: 에너지 절대 jump | WTI·Brent 10 $/bbl, 정제유 0.25 $/gal, 프로판 0.15 $/gal, HH 2 $/MMBtu | 같음 | [임의] 큰 정상일의 5~10배. 2020-04-20(−55.29)·2021-02 HH(일 +10 이상)는 경고로 잡히게 | % 규칙: 0 근처·음수 기준가에서 부호 반전·발산 (노트 03 §5) | 같음 |
| 검증: 음수 종가 | absolute 계열은 warning, log 계열은 error | `validate()` | 노트 02 §11: WTI 2020-04-20 = −36.98은 실제 데이터 [확인] | 전부 error: 실제 사건 삭제 | log 계열 음수는 로그수익률 정의 불가 → 적재 거부 |
| upsert 가드 | `IS DISTINCT FROM` | `UPSERT_PRICES_SQL` | 노트 00 #3: `loaded_at`이 "값이 바뀐 시각"이어야 함 | 무조건 UPDATE: 정정 건수 추적 불가 | `rows_updated` = 벤더 정정 건수의 의미 상실 |
| 유니버스 v1 | 31 계열 (국채 11·FX 12·에너지 8) | `config/universe.csv` | 노트 02 §3-1. 공공저작물·출처표기 소스만 [확인]. 국채는 2026-09-13 부터 FRED | 주식 포함: 무료 소스 약관 미통과 (노트 02 §2-1) | 팩터 구조(level·slope·curvature·USD·원유·크랙·가스) |
| 표본 윈도우 기본 | 2006-02-09 ~ | `config/universes.toml` | 30년물 재발행일 [확인: 재무부 페이지 주석]. ETL은 전체 이력 적재 | 1990~: 30Y 공백이 intersection을 끊음 | 관측 수·스트레스 창 포함 여부 |

## 2. 수익률 빌더 (노트 04)

| 이름 | 값 | 쓰이는 곳 | 근거 | 검토한 대안과 기각 이유 | 바꾸면 달라지는 것 |
|---|---|---|---|---|---|
| 달력 정렬 기본 | `intersection` | `returns.build(align=)` | 노트 02 §5 (JK 승인). 인공 값이 없어 검정 통계가 오염되지 않는다 | `ffill`: 채운 날의 변화량 0 이 Christoffersen 독립성 검정을 오염 — 옵트인으로 유지 | 관측 수(기본 집합에서 실측: 아래 walkthrough), 이틀치 변화가 하루로 합쳐지는 날의 수 |
| `ffill` 최대 간격 `max_gap` | 1 영업일 | 같음 | [임의] 하루 결측(휴일 불일치)만 메우고 그 이상은 데이터 부재로 본다 | 2 이상: 연휴를 인공 0 으로 채움 | `filled_cells` 와 0 변화량 군집 |
| FX 팩터 정의 | 1 외화 = x USD (`S_CCY = EURUSD / EURCCY`) | `returns.to_usd_per_unit` | 기준통화 USD 포트폴리오의 외화 익스포저 P&L 은 USD/CCY 로 결정된다. ECB 원계열은 EUR 기준이라 변환 필요 | ECB 원계열 그대로: JPY 포지션의 P&L 에 EUR 변동이 섞임 | 팩터 이름(통화 코드)과 포지션 수량의 의미(외화 수량) |
| 변화량 종류 | FX 로그 · 에너지 절대(USD) · 국채 bp | `returns.kind_of` | 노트 02 §11 (JK 승인): 음수 유가에서 % 발산. 금리는 bp 차분이 관행 (RiskMetrics 1996 §4 는 금리도 로그수익률을 쓰지만 0·음수 금리 시대엔 절대 변화가 표준 — ISDA SIMM 도 bp 충격) | 전부 로그: WTI 2020-04-20 정의 불가 | 표준화 잔차의 분포, 시나리오 P&L 의 물리적 타당성 |
| DV01 모델 | par 채권(반기 쿠폰) 수정 듀레이션 D = [1 − (1+y/2)^(−2T)] / y, 매일 y 로 재계산 | `pnl.modified_duration` | [출처] Fabozzi, *Bond Markets, Analysis and Strategies* 의 par 채권 듀레이션 닫힌 식; JK 검산 (노트 02 §6). CMT 는 par 수익률이라 par 채권 가정이 정확히 맞는다 | 고정 듀레이션: 금리 수준 변화에 둔감. 완전 재가격: 쿠폰·현금흐름 가정 필요, 1일 지평에 과잉 | y 수준 의존 민감도. y→0 극한 T 로 안정 |
| 컨벡시티 | 미포함 | `pnl.pnl_matrix` | [계산] 10Y·10 bp: ½·C·Δy² ≈ 0.5×80×10⁻⁶ = 4×10⁻⁵ vs D·Δy ≈ 8×10⁻⁴ → 5 % 미만. 1일 VaR 지평에서 무시 | 포함: 스트레스 100 bp 에서 ≈ 5 % 차이 → 스트레스 노트에서 재검토 | 큰 금리 충격의 손익 비대칭 |
| 만기 매핑 | 티커 접미사 파싱 (`UST_1M`→1/12, `UST_10Y`→10) | `pnl.maturity_years` | 이름 규칙이지 설정값이 아니다 | 설정 파일: 티커와 중복 | — |
| 표본 집합 | `default` 2006-02-09~ 31계열 / `from_1999` 28계열 / `rates_energy_1990` 14계열 | `config/universes.toml` | 노트 02 §3-1 (JK 승인) | — | 관측 수, 스트레스 창 포함 여부, 팩터 수 |

## 2-1. 데이터 품질 사실: DGS30 의 2002-02-19 ~ 2006-02-08 (JK 확인 요청 [A], 2026-09-13)

| 질문 | 사실 |
|---|---|
| 994행의 출처 | **FRED sync 가 inserted** 한 행이다. `prices.loaded_at` 전부 2026-09-13 08:36:25(FRED 배치), 재무부 배치(06:59)는 8,186행(1990-01-02~2026-09-11, 공백 구간 없음). `etl_runs`: fred/DGS30 `rows_inserted=4203` = 1977-02-15~1989-12-29 의 3,209행 + 공백 구간 994행. **앞선 보고의 "FRED 에만 있는 날짜는 1990년 이전" 은 DGS30 에 대해 틀렸다** — 51,001 inserted 중 994행이 2002~2006 이다 [확인 2026-09-13: loaded_at 배치·etl_runs] |
| 재무부 CSV 열 오매핑 가능성 | 없음. 재무부 적재분에 그 구간이 없고(8,186행), 30 Yr 열이 공란이었다 |
| 값의 정체 | 관측치가 아니라 **추정치**. 연준 H.15 각주: "The 30-year Treasury constant maturity series was discontinued on February 18, 2002, and reintroduced on February 9, 2006 … the U.S. Treasury published a factor for adjusting the daily nominal 20-year constant maturity in order to estimate a 30-year nominal rate" [확인 2026-09-13: federalreserve.gov/releases/h15 각주]. FRED 시리즈 노트에는 중단·재개 문장만 있고 산출 방식 설명은 없다 [확인]. DB 에서 30Y/20Y 비율은 0.96~1.07 로 날마다 달라 상수 배율이 아닌 일별 배율이다 |
| 처리 | 값은 그대로 둔다(H.15 공식 계열이고 ETL 은 받은 것을 저장). `from_1999` 집합은 30Y 를 포함하되 이 사실을 모델 문서 "데이터" 절에 명시한다. 30Y 포지션의 2002~2006 백테스트 손익은 추정 금리에 기반한다 |

## 2-2. 표본 집합 두 개 병행 (JK 결정 [B], 2026-09-13)

| 집합 | 용도 | 구성 | 관측 | 근거 |
|---|---|---|---|---|
| `from_1999` | **백테스트 정식 계열** (`tag='daily_batch'`) | 29계열: EURCNY·UST_1M 제외, 1999-01-04~ | 6,755 (첫 FHS 가능일 ≈ 2001-04) | EURCNY 는 페그·관리변동기라 팩터 정보량이 낮고 4년을 묶는다. UST_1M 은 듀레이션 0.08년으로 DV01 기여가 없다. 얻는 것: 표본 +37 %, 2000 닷컴·2001-09 포함. Kupiec 검정력은 표본 길이에 직접 걸린다 |
| `default` | 팩터 분석·검증 예시 | 31계열, 2006-02-09~ | 5,031 | 1M 의 0·음수 구간과 DV01 의 음수 y 성립을 여기서 설명 |

규칙: **모든 보고 수치에 집합 이름을 붙인다.** `risk_runs.params.universe` 에 기록되며 0006 에서 `risk_runs.universe` 컬럼으로 승격(백테스트 노트). 두 집합의 `daily_batch` 가 한 테이블에 공존하므로 조회는 항상 집합으로 거른다.

## 2-3. 초기 포트폴리오 MAIN 의 근거 (JK 지적 [C], 2026-09-13)

**임의 선택이다.** 자산군 4개에 롱·숏이 섞이고 총 규모가 1~2억 USD 수준이 되도록 손으로 정했다. 명목 균등도 리스크 기여 균등도 아니다. 구성과 그 결과(2026-09-09, `default` 집합 FHS, PV 177.3 M):

| 상품 | 수량 | 시가(USD) | 컴포넌트 ES | ES 비중 |
|---|---|---|---|---|
| WTI | 200,000 bbl | 19.5 M | 647,687 | 57.0 % |
| GASOLINE_NYH | 2,000,000 gal | 6.6 M | 426,775 | 37.6 % |
| BRENT | −100,000 bbl | −11.0 M | −220,998 | −19.5 % |
| HEATOIL_NYH | −1,000,000 gal | −4.7 M | 74,984 | 6.6 % |
| HENRYHUB | 500,000 MMBtu | 1.4 M | 10,907 | 1.0 % |
| EURUSD | 20 M EUR | 23.3 M | 30,329 | 2.7 % |
| EURJPY | 2,000 M JPY | 13.0 M | 50,927 | 4.5 % |
| EURAUD | 10 M AUD | 7.2 M | 24,072 | 2.1 % |
| EURCHF | 5 M CHF | 6.2 M | 8,949 | 0.8 % |
| EURGBP | −5 M GBP | −6.8 M | −10,574 | −0.9 % |
| EURKRW | −10,000 M KRW | −7.5 M | −17,146 | −1.5 % |
| UST_2Y | 50 M 액면 | 50.0 M | 27,931 | 2.5 % |
| UST_5Y | 30 M | 30.0 M | 38,838 | 3.4 % |
| UST_10Y | 40 M | 40.0 M | 66,055 | 5.8 % |
| UST_3M | 20 M | 20.0 M | −400 | 0.0 % |
| UST_30Y | −10 M | −10.0 M | −22,401 | −2.0 % |

읽는 법: 시가 비중은 국채 130 M(73 %) > FX 36 M > 에너지 11 M(순) 이지만, **ES 의 82 % 가 에너지 순포지션(WTI 롱 − Brent 숏 + 휘발유 롱)** 에서 나온다. 2020-04-21 의 ES 9.46 % 도 이 구성 때문이다. 이것은 문제가 아니라 사실이고, 드러난 임의값이다. 포지션은 `tests/test_config_pins.py::test_positions_main_is_pinned` 가 고정한다 — 바꾸려면 테스트와 함께 커밋해야 한다. 대안(리스크 기여 균등 포트폴리오)은 3주차 분석 모듈에서 두 번째 포트폴리오로 만들 수 있다.

## 3. FHS · Parametric · Monte Carlo (노트 05)

| 이름 | 값 | 쓰이는 곳 | 근거 | 검토한 대안과 기각 이유 | 바꾸면 달라지는 것 |
|---|---|---|---|---|---|
| EWMA λ | 0.94 | `volatility.ewma_variance`, `risk_params.toml [fhs].lambda` | [출처] RiskMetrics Technical Document (1996) §5.3 — 일별 데이터 최적 감쇠 | 0.97(월별용): 반응이 늦어 2020-03 같은 레짐 전환에서 초과 급증. GARCH: 계열별 MLE 로 대체 가능(`--vol garch`) — 기본은 EWMA(파라미터 추정 없음, 실무 표준) | σ_T 반응 속도 → VaR 의 프로시클리컬리티와 백테스트 초과 군집 |
| 잔차 풀 W | 500 영업일 | `[fhs].window_days` | Basel II 최소 250 [출처: BCBS 1996 §718(Lxxvi)]; Pritsker (2006) 의 250일 FHS 꼬리 표본 부족 지적; FRTB 내부모형·CCP 룩백(1~3년)의 중간 [임의, 250~1,000] | 250: 12.5 개 꼬리 표본으로 ES 97.5 % 추정 불안정. 1,000: 4년 전 사건이 오늘 σ 로 스케일돼 등장 | ES 의 안정성 vs 최신성. 민감도 표는 모델 문서 |
| 워밍업 W₀ | 75 관측 | `[fhs].warmup_days` | [출처] RiskMetrics TD §5.3.2: λ=0.94 에서 가중치 99 % 가 최근 74 일 (ln 0.01 / ln 0.94 = 74.4). 초기값 = 첫 75 일 표본 2 차 모멘트 | 0: 첫 구간 σ 가 임의 초기값에 지배. 250: 5 % 표본 손실 | 백테스트·풀에서 제외되는 첫 구간 길이. 기본 집합에서 첫 FHS 가능일 = 2008-06-13 (575 관측 뒤) |
| 잔차 풀 구성 | 자산별 σ 표준화 + **날짜별 공동 샘플** | `fhs.evaluate` | [출처] Barone-Adesi, Giannopoulos & Vosper (1999); Hull & White (1998) — FHS 원문의 정의 | 전체 풀 혼합·자산별 독립 추출: 자산 간 종속성 소실, 꼬리 특성 혼합(노트 05 §2-2) | 분산 효과의 크기 — 공동 vs 독립 샘플의 ES 차이가 곧 그것 |
| α (VaR / ES) | 0.99 / 0.975 | `[measures]` | [출처] Basel II VaR 99 %; FRTB(BCBS 2019) ES 97.5 % | — | 꼬리 표본 수 (500 → 5 / 12.5) |
| VaR 정의 | ⌈α·n⌉ 번째 손실 (n=500 → 5 번째로 큰 손실) | `measures.value_at_risk` | Basel 관행의 순서통계량 | 보간 분위수: 표본이 작을 때 두 값 사이 — 관행이 아님 | 이산성 |
| ES 정의 | Acerbi & Tasche (2002) 분수 가중 | `measures.expected_shortfall` | [출처] Acerbi & Tasche, "On the coherence of expected shortfall" (2002) — 이산 표본에서 일관된(coherent) 정의 | 상위 k 개 단순 평균: (1−α)n 이 정수가 아니면 편향 | n=500 에서 12.5 번째 관측의 절반 가중 |
| 스트레스 창 | 250 영업일, 현재 포지션에 ES 최대인 창 | `[measures].stressed_window_days`, `fhs.evaluate` | [출처] BCBS FRTB (2019) MAR33.5 — 12 개월 스트레스 기간 | 고정 창(2008-09~2009-08): 포트폴리오에 따라 최악이 다름 | `stressed_es` 와 창의 위치 (params 에 기록) |
| MC 경로 · seed | 10,000 · 20260913 | `[montecarlo]` | [임의] 99 % 분위수 표준오차 ≈ 2 % 수준; backfill 비용 | 100,000: 정밀도 3 배, 비용 10 배 | MC 결과의 표본 오차. seed 고정으로 재현 |
| Parametric 델타 | FX q·S, 에너지 q, 국채 −DV01 | `parametric.deltas` | 노트 04 §4 의 1 차 계수 | 완전 재가격: Parametric 의 정의에 어긋남 | Parametric 과 MC 의 차이 = 비선형성(FX 지수식) |
| 포트폴리오 가치 | FX q·S + 에너지 q·P + 국채 액면 | `fhs.portfolio_value` | 국채는 par 채권 가정이라 액면 = 시가 | — | `v_risk_headline` 의 비율 분모 |
| 초기 포트폴리오 MAIN | `config/positions_main.csv` 16 포지션, 2026-09-09 시가 ≈ 177 M USD | `positions` | [임의] 자산군 4 개에 롱·숏 혼합. 국채 롱 130 M, FX 롱·숏, 에너지 롱·숏 | — | 모든 리스크 수치 |
