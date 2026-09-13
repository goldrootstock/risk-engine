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
| User-Agent | `Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:130.0; risk-engine/0.1) Gecko/20100101 Firefox/130.0` | `etl.http.client_for` 기본 클라이언트 | [확인 2026-09-13: home.treasury.gov 에 6가지 UA 시험 — `python-httpx/0.28.1`·`curl/8.7.1`·설명형 `risk-engine/0.1 (+url)`·Chrome 문자열 뒤에 도구명 추가 → 전부 ReadTimeout; Chrome·Firefox 원문 → 200; **Firefox 괄호 안에 도구명** → 200]. 도구명이 들어 있어 서버 로그에서 식별 가능. 재무부 사이트 약관에 자동 수집 제한 없음 [확인 2026-09-12] | 순수 브라우저 UA: 식별 불가라 기각. 설명형 UA: 차단됨 | 재무부 WAF 규칙이 바뀌면 fetch 가 30 s × 3회 후 `RetryExhaustedError` — `network` 라이브 테스트가 잡는다 |
| EIA 페이지 길이 | 5,000 | `EiaSource.fetch` | [출처] EIA API v2 문서 최대값 [확인 2026-09-12] | 더 작게: 요청 수 증가 | 페이지 수·캐시 파일 수 |
| ECB 증분 미채택 | 매 sync 전체 zip | `EcbSource.fetch` | 파서 하나 유지(CSV). 90일 XML 존재 [확인 2026-09-13] | XML 증분: 파서 둘 유지 비용 | 연 160 MB 캐시. 1 GB 재검토 시 XML 전환 |
| 캐시 보존 | 무기한, 1 GB 도달 시 재검토 | `RawCache` | 연 ≈170 MB 실측 기반 추정(노트 03 §13) | 자동 prune: 재현 불가 구간 생김 | — |
| 검증: log 계열 jump | \|단순수익률\| > 30 % | `validate()`, `config/validation.toml` | [임의] FX 일일 30 %는 페그 붕괴 수준(2015-01 CHF: 약 20 %). 정상 변동의 10배 이상 | 10 %: 2015 CHF·2016 GBP 등 실제 사건 경고 다수 — 경고는 검토용이라 그것도 무방 | 경고 건수만. 적재는 불변 |
| 검증: yield 계열 jump | \|Δy\| > 100 bp | 같음 | [임의] 2020-03 국채 일일 변동 최대 약 30~40 bp [추정]. 100 bp는 정책 오류·데이터 오류 수준 | 50 bp: 2022 이후 단기물에서 경고 다수 | 같음 |
| 검증: 에너지 절대 jump | WTI·Brent 10 $/bbl, 정제유 0.25 $/gal, 프로판 0.15 $/gal, HH 2 $/MMBtu | 같음 | [임의] 큰 정상일의 5~10배. 2020-04-20(−55.29)·2021-02 HH(일 +10 이상)는 경고로 잡히게 | % 규칙: 0 근처·음수 기준가에서 부호 반전·발산 (노트 03 §5) | 같음 |
| 검증: 음수 종가 | absolute 계열은 warning, log 계열은 error | `validate()` | 노트 02 §11: WTI 2020-04-20 = −36.98은 실제 데이터 [확인] | 전부 error: 실제 사건 삭제 | log 계열 음수는 로그수익률 정의 불가 → 적재 거부 |
| upsert 가드 | `IS DISTINCT FROM` | `UPSERT_PRICES_SQL` | 노트 00 #3: `loaded_at`이 "값이 바뀐 시각"이어야 함 | 무조건 UPDATE: 정정 건수 추적 불가 | `rows_updated` = 벤더 정정 건수의 의미 상실 |
| 유니버스 v1 | 31 계열 (국채 11·FX 12·에너지 8) | `config/universe.csv` | 노트 02 §3-1. 공공저작물·출처표기 소스만 [확인] | 주식 포함: 무료 소스 약관 미통과 (노트 02 §2-1) | 팩터 구조(level·slope·curvature·USD·원유·크랙·가스) |
| 표본 윈도우 기본 | 2006-02-09 ~ | `config/universes.toml` | 30년물 재발행일 [확인: 재무부 페이지 주석]. ETL은 전체 이력 적재 | 1990~: 30Y 공백이 intersection을 끊음 | 관측 수·스트레스 창 포함 여부 |
