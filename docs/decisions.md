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
| 국채 수익률 소스 | FRED `DGS1MO…DGS30` (연준 H.15) | `sources/fred.py`, `config/universe.csv` rates 11행 | [출처] FRED 계열 페이지 "Public Domain: Citation Requested", 원천 = Board of Governors H.15 [확인 2026-09-12]. 공식 REST API, 무료 키, 프로그램 접근 명시 [확인 2026-09-13: API 문서]. 재무부 값과 겹치는 모든 날짜에서 **불일치 0건** (11계열, 9,176~9,179일 비교, 2026-09-13, `fredgraph.csv` 기준) | 재무부 직접: WAF (위). 연준 DDP: 별개 서비스이며 축소 중 [확인 2026-09-12 공지] | FRED 는 H.15 를 **T+1** 로 반영 — 재무부보다 하루 늦다(2026-09-13 대조에서 재무부에만 09-11 존재). 이력은 더 길다(DGS10 1962~) |
| API 키 전달 | EIA: `X-Api-Key` **헤더** / FRED: `api_key` 쿼리 파라미터 | `sources/eia.py`, `sources/fred.py`, `etl.http` | EIA v2 는 헤더를 받는다 [확인 2026-09-13: 헤더만으로 200]. 단 헤더로 보내도 응답 `request.params.api_key` 에 키가 **에코된다** [확인 2026-09-13] → 바이트 스크럽 유지. FRED 는 문서상 쿼리 파라미터만 [미확인: 키 없이 헤더 시험 불가] | 쿼리 파라미터만 사용: 로그·프록시·예외 메시지로 유출 | `etl.http` 는 예외 메시지와 로그에 URL 경로만 넣고 쿼리스트링을 절대 넣지 않는다 (`test_exception_messages_never_contain_the_query_string`) |
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
