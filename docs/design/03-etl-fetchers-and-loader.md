# 설계 노트 03 — ETL fetcher · 로더 · CLI

- 상태: 승인(2026-09-12/13) → **구현 완료 (2026-09-13)**. 31계열 전체 이력 적재 확인: ECB 83,493행 · 재무부 96,145행 · EIA 75,792행, 스킵 0. 실전에서 발견한 정정 4건은 `docs/walkthrough.md` §1
- 전제: 노트 02(소스·정책·유니버스 31계열·전체 이력 적재) 확정, 노트 00(검증은 읽기 전용) 적용, 0003 마이그레이션(`source_id`·`return_type`·어휘) 적용
- 역할: 이 노트와 픽스처·테스트 뼈대 = Claude / fetcher 3개·로더·CLI 구현 = JK / 리뷰 = Claude
- 번호: 수익률 빌더 노트는 **04** 로 밀린다

---

## 0. 한 줄 요약

fetcher 는 "소스 → 표준 DataFrame" 만 하고, 로더는 "표준 DataFrame → `prices` upsert" 만 하며, 검증은 둘 사이에서 **보고서만 반환**한다. 세 소스는 형식이 전부 달라서(연도별 CSV · 전체 이력 zip · 페이지네이션 JSON) fetcher 만 소스별이고 나머지는 공통이다.

## 1. 구조

```
config/universe.csv                       # 31행. source, ticker, source_id, … (0003)
src/risk_engine/data/etl/
  __init__.py
  contract.py                             # PriceFrame 스키마 · Source 프로토콜 · Report/Result 타입
  sources/__init__.py                     # REGISTRY: {"ecb": EcbSource(), "ustreasury": …, "eia": …}
  sources/ecb.py
  sources/ustreasury.py
  sources/eia.py
  cache.py                                # data/raw 읽기·쓰기 (덮어쓰기 없음)
  validate.py                             # validate(frame, instrument) -> Report   (읽기 전용)
  load.py                                 # upsert_instruments(universe) / upsert_prices(frame) -> LoadResult
  __main__.py                             # python -m risk_engine.data.etl {sync,status} …
tests/fixtures/etl/                       # 실제 응답 조각 3개 (있음)
tests/test_etl_*.py
```

## 2. 계약 (contract.py) — 구현됨: `src/risk_engine/data/etl/contract.py`

핵심 타입만 요약한다(원문은 모듈 docstring 참조).

```text
RawFile(source, scope, url, content: bytes, fetched_at, page=1, pages=1)   # 받은 바이트 + 출처. 비밀은 제거된 상태
Source (Protocol):
    fetch(source_ids, start, end) -> Sequence[RawFile]   # 네트워크는 여기서만. 파싱 안 함
    parse(raw: RawFile) -> DataFrame                    # 파일당 1회. long format, 파일 안의 모든 계열
PRICE_COLUMNS = source_id · price_date · close · adj_close · volume(Int64)
# parse 반환 프레임의 정렬 계약: (source_id, price_date) 오름차순 + RangeIndex(0..n-1). 벤더 파일은 전부 내림차순
Finding(level, code, detail, price_date?, value?, threshold?)              # code 는 닫힌 어휘 FindingCode
Report(source, source_id, rows, first, last, findings) .ok               # validate() 의 반환값
LoadResult(instrument_id, fetched, inserted, updated, unchanged)          # upsert 의 반환값
JumpThresholds(max_abs_return, max_abs_change_bp, max_abs_change)         # return_type 별 임계값
InstrumentSpec(instrument_id, source, source_id, ticker, quote_type, return_type)
```

- `fetch → Sequence[RawFile]` (JK #10 승인): ECB 는 파일 하나가 12 계열, 재무부는 연도 파일 하나가 11 계열이라 `Mapping[source_id, bytes]` 는 같은 바이트를 복제한다.
- `parse(raw) → long frame` (JK 제안 채택): 파일당 1회 파싱하고 `source_id` 열로 계열을 구분한다. DB 의 long format 과 모양이 같고, 파일이 다계열이라는 사실이 시그니처에 드러난다. 계열 단위 스킵은 오케스트레이터가 `source_id` 로 나눠 `validate()` 에 넘기므로 그대로 성립한다. 원안 `parse(source_id, raw)` 를 고집할 근거는 본문에 없었다 — ECB zip 을 12번 여는 비효율만 있었다.
- `validate()` 는 DB 커넥션을 받지 않는다(JK #5 조건 1). 시그니처가 원칙을 강제한다.

## 3. 소스별 fetcher

| | ECB | FRED (2026-09-13 부터; 재무부 CSV 는 §16) | EIA |
|---|---|---|---|
| 엔드포인트 | `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` — **전체 이력이 zip 하나** | `…/daily-treasury-rates.csv/{YEAR}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YEAR}&page&_format=csv` — **연도별 1파일**, 1990~현재 루프 | `https://api.eia.gov/v2/petroleum/pri/spt/data/?api_key=…&frequency=daily&data[0]=value&facets[series][]={SERIES}&sort[0][column]=period&sort[0][direction]=desc&offset=N&length=5000` (천연가스는 `/v2/natural-gas/pri/fut/data/`) — **JSON, 5,000행 페이지네이션** |
| 인증 | 없음 | 없음 | 무료 API 키 (`EIA_API_KEY` 환경변수, `.env`) |
| 한 번의 fetch 가 주는 것 | 12 source_id 전부 (열 단위) | 11 source_id 전부 (열 단위), 연도당 1요청 | source_id 하나당 1~2요청 (RWTC 는 1986~ ≈ 10,000행 → 2페이지) |
| 형식 특이점 [확인, 픽스처 기준] | 첫 열 `Date`(YYYY-MM-DD), **내림차순**, 결측 `N/A`, **모든 줄 끝에 콤마**(빈 마지막 열) | 첫 열 `Date`(**MM/DD/YYYY**), 헤더가 따옴표(`"1 Mo"`), 내림차순, 결측은 빈 문자열, 연도마다 열 구성이 다름(1990 = 9열, 2020 = 13열) | `response.data[]` 의 `period`(YYYY-MM-DD)·`value`(**문자열**)·`series`. 음수 존재 (`-36.98`) |
| parse 규칙 | 열 = source_id(ISO). `N/A`→NaN 후 행 제거. 마지막 빈 열 버림 | 열 이름의 따옴표·공백 정규화 후 source_id 로 선택. 없는 열이면 그 연도는 빈 프레임(오류 아님 — 1990 에 `1 Mo` 없음) | `value` 는 **문자열**(끝 0 탈락, `12.4`) → float. `series` 가 요청과 다르면 error. `total` 과 받은 행 수 비교. `duoarea`·`area-name` 등 facet 필드는 무시 |
| **비밀 처리** | 없음 | 없음 | 응답의 `request.params.api_key` 에 **키가 그대로 에코된다** [확인 2026-09-12]. `fetch` 는 캐시에 쓰기 전에 그 필드를 `<redacted>` 로 바꾼다. 요청 URL 도 로그에 남기지 않는다(키가 쿼리스트링에 있음) |
| 값 → 컬럼 | `close = adj_close = 값`, `volume = NA` | 동일 (단위 = %, 예: 0.93) | 동일 |
| 증분 | 전체 zip 을 매번 받는다(≈1 MB). 증분 개념 없음 | `start` 의 연도부터 현재 연도까지만 | `start` 파라미터 지원 |
| 재시도 | HTTP 5xx·타임아웃에 지수 백오프 3회. 4xx 는 즉시 실패 | 동일 | 동일 + 429 (rate limit) 는 대기 후 재시도 |
| 고시 시각 (달력 노트용) | 16:00 CET 경 | NY 15:30 [추정] | 일중 평가 |
| **sync 1회가 받는 범위** | **항상 전체 이력** (639 KB zip). XML 형식의 90일 파일(`eurofxref-hist-90d.xml`)은 존재한다 [확인 2026-09-13: HTTP 200, `text/xml`; `.zip`·`.csv` 확장자는 404]. CSV·zip 파서 하나로 유지하기 위해 채택하지 않고 매 sync마다 전체 zip을 받는다. 대가는 연 약 160 MB이며, 1 GB 재검토 시 첫 후보가 이 XML 경로다. 당일 파일(`eurofxref.zip`, 395 B)은 하루치뿐이라 결측일이 생기면 못 메운다 | **증분**: `start` 가 걸치는 연도 파일부터 현재 연도까지 (연도 파일 ≤ 15 KB). 첫 실행은 1990~ 전부 | **증분**: `start` 부터 `end` 까지 (계열당 5,000행 페이지). 첫 실행은 전체 이력 (WTI 10,245행 = 2페이지 2.8 MB, 행당 274 B [확인]) |
| **절단 없음 — 전체 upsert (결정 2026-09-13)** | 오케스트레이터는 parse 결과를 `start` 이후로 **자르지 않는다.** 유니버스 12 통화만 고른 뒤(≈ 7,000일 × 12 ≈ 84,000행; 파일 자체는 32 통화 ≈ 21만 행) 전부 validate·upsert 한다. 매 sync 가 전체 재검증이 되어 벤더 정정을 전부 잡고, `IS DISTINCT FROM` 가드 덕에 쓰기는 정정 행만 발생한다. `etl_runs.rows_unchanged` 가 매일 ≈ 84,000 인 것은 **의도**다 — 그 숫자가 곧 "전체를 다시 봤다"는 증거 | `start − 14일` 이후만 받으므로 절단 불필요 | 동일 |
| **응답 검사** (404 HTML 을 데이터로 캐시하지 않기 위해) | HTTP 2xx + 본문이 `PK` 로 시작 | HTTP 2xx + 본문이 `Date` 로 시작 | HTTP 2xx + JSON 파싱 가능 + `response.data` 존재 |

**ECB 를 첫 번째로 권고**하는 이유: 요청 1개·키 없음·페이지네이션 없음이라 fetcher 골격(fetch/parse/cache/validate/load/CLI)을 **가장 짧은 경로로 끝까지** 관통한다. 그러면서도 결측(`N/A`)·내림차순·꼬리 콤마·12 계열 동시 처리라는 실제 문제가 다 들어 있어 골격의 설계 결함이 바로 드러난다. 두 번째는 Treasury(연도 루프·열 구성 변화·yield 경로), 마지막이 EIA(키·JSON·페이지네이션·음수). 국채가 팩터 구조의 핵심이지만 "먼저 만들 것"은 엔지니어링 리스크가 낮은 쪽이다.

## 4. 캐시 (cache.py) — 노트 00 #2 · JK #1·#2·#4 반영

```text
data/raw/<source>/<scope>/<fetched_at>_<sha12>_p<n>.<ext>
data/raw/<source>/<scope>/manifest.jsonl
```

- `scope` = ECB `_all` · 재무부 `{year}` · EIA `{source_id}`. `fetched_at` 은 UTC, 콜론 없는 `YYYYMMDDTHHMMSSZ`. `sha12` = SHA-256 앞 12자리. `p<n>` 은 **항상** 붙인다 (단일 페이지 소스는 `p1`) — EIA 는 같은 시각·같은 scope 에 페이지 수만큼 파일이 생기므로 페이지 번호가 파일명에 있어야 sha 만 다른 파일이 쌓이는 것처럼 보이지 않는다. 확장자는 소스의 속성(`zip`·`csv`·`json`).
- **덮어쓰기 없음.** 직전 저장본과 SHA-256 이 같으면 파일을 쓰지 않고 manifest 에 `same_as` 항목만 추가.
- manifest 한 줄 = `{fetched_at, sha256, bytes, scope, url(쿼리스트링 제거), page, pages, path|null, same_as|null, pruned_at|null}`. **바이트 사실만** — 행 수는 parse 뒤에야 알 수 있어 넣지 않는다(넣으면 fetch 가 parse 를 불러 §2 분리를 깬다). 행 수는 `etl_runs` 에.
- **비밀은 캐시에 들어가지 않는다.** EIA `fetch` 가 키를 지운 바이트를 넘기고 `url` 은 쿼리스트링을 뺀다. gitignore 에 기대지 않는다.
- **보존: 무기한. 캐시가 1 GB 에 이르면 재검토** (근거 §13). `prune` 은 **수동 명령만** — `sync` 는 절대 지우지 않는다. `prune` 이 지운 파일마다 `pruned_at` 이 붙은 manifest 행을 추가하고 원래 fetch 행은 남긴다. 어느 기간이 오프라인 재현 불가인지 나중에 알 수 있어야 한다.
- `--offline` 은 scope 의 최신 fetch 의 전 페이지를 읽는다(`same_as` 를 따라 실제 파일로).

## 5. 검증 (validate.py) — 읽기 전용, `Report` 반환 · JK #3·#7 반영

`validate(frame, spec, thresholds, *, today=None) -> Report`. DB 커넥션 인자가 **없다**. 결측 자동 보정 없음(노트 00 #1).

| 검사 | level | 근거 |
|---|---|---|
| 필수 컬럼·dtype (`missing_column`·`bad_dtype`) | error | 계약 위반 |
| 빈 프레임 (`empty_frame`) | error | 적재할 것이 없음 |
| `price_date` 중복 (`duplicate_date`) | error | PK 충돌 전에 잡는다 |
| `price_date` 가 오늘 이후 (`future_date`) | error | 벤더 오류 |
| `close ≤ 0` 이고 `return_type='log'` (`nonpositive_price`) | error | 로그수익률 불가 (FX·v2 주식) |
| `close ≤ 0` 이고 `return_type='absolute'` (`nonpositive_price`) | **warning** | WTI 2020-04-20 = −36.98 은 실제 데이터. 기록만 |
| 일간 변화 (`jump`) — **return_type 별 분기** | warning | 아래 |

`jump` 임계값 (JK #7): 퍼센트 규칙은 0 근처·음수 기준가에서 부호가 뒤집히거나 발산하므로 absolute 계열에 적용하지 않는다.

| 계열 | 기준 | 기본값 (`config/validation.toml`) |
|---|---|---|
| `return_type='log'` (FX) | \|P_t/P_{t−1} − 1\| | 0.30 |
| `quote_type='yield'` (국채) | \|Δy\| bp (값이 % 단위라 ×100) | 100 bp |
| `return_type='absolute'`, price (에너지) | \|ΔP\| 계열 고유 단위 | WTI·Brent 10 USD/bbl · 휘발유·난방유·제트유 0.25 USD/gal · 프로판 0.15 USD/gal · Henry Hub 2.0 USD/MMBtu · 미지정 10.0 |

기본값 근거: 큰 정상일의 5~10배 — 2020-04-20(ΔP = −55.29) 과 2021-02 Henry Hub 는 warning 으로 잡히고 평일은 조용하다. 임계값 파일의 sha256 을 `etl_runs.params` 에 기록한다(노트 00 §3 의 "설정 → 해시 → 실행 기록"). 테스트 `test_wti_2020_04_20_is_warning_not_error` 가 "음수 종가는 error 가 아니라 warning" 을 고정하고, `tests/test_config_pins.py` 가 **임계값 자체**를 고정한다 — 파일의 값을 올리면 테스트가 깨지므로 변경은 반드시 두 파일이 한 커밋에 남는다(노트 00 §3-1 의 형태).

error 가 하나라도 있으면 그 series 는 **통째로 건너뛴다**(부분 적재 없음). 스킵 사유는 `etl_runs.findings` 에 남고 CLI 는 exit 2 를 돌려준다.

## 6. 로더 (load.py)

```sql
INSERT INTO prices (instrument_id, price_date, close, adj_close, volume)
VALUES (%(instrument_id)s, %(price_date)s, %(close)s, %(adj_close)s, %(volume)s)
ON CONFLICT (instrument_id, price_date) DO UPDATE
SET close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
    volume = EXCLUDED.volume, loaded_at = now()
WHERE prices.close IS DISTINCT FROM EXCLUDED.close
   OR prices.adj_close IS DISTINCT FROM EXCLUDED.adj_close
   OR prices.volume IS DISTINCT FROM EXCLUDED.volume
RETURNING (xmax = 0) AS inserted;
```

- `WHERE … IS DISTINCT FROM` — 값이 같으면 행을 건드리지 않는다(노트 00 #3). 이때 `RETURNING` 이 행을 안 돌려주므로 `unchanged = fetched − inserted − updated`.
- `xmax = 0` 이면 새 행(insert), 아니면 갱신(update) — PostgreSQL 의 MVCC(Multi-Version Concurrency Control, 다중 버전 동시성 제어) 시스템 컬럼을 이용한 관용구.
- series 하나 = 트랜잭션 하나. 실패하면 그 series 만 롤백.
- `upsert_instruments(universe.csv)` 는 `(source, ticker)` 충돌 시 name·asset_class·…·`updated_at = now()` 갱신. `instrument_id` 는 절대 바뀌지 않는다.
- v1 은 `executemany`. 계열당 최대 ≈ 10,000행이라 충분하다. `COPY` 는 10만 행 이상에서.

## 7. CLI (`python -m risk_engine.data.etl`)

```
sync   [--source ecb|ustreasury|eia] [--ticker UST_10Y …] [--since YYYY-MM-DD] [--offline] [--dry-run]
status                       # 계열별 first/last price_date, 행 수, 마지막 loaded_at (읽기 전용)
```

- `--since` 기본값 없음 = 전체 이력(노트 02 §4-2). 증분은 `status` 의 last − 10 영업일을 사람이 넣거나 `--incremental` 플래그가 계산.
- `--dry-run` = fetch + parse + validate + 캐시 쓰기까지, **DB 는 건드리지 않고** Report 와 예상 insert/update 수를 출력.
- 로그 한 줄 형식: `INFO etl: ecb/JPY 1999-01-04..2026-09-11 rows=7088 inserted=7088 updated=0 unchanged=0 findings=0 elapsed=0.4s`
- **exit code** (JK #6, `--help` 와 README 에도): `0` 전 series 적재 · `1` 설정·인프라 오류(DATABASE_URL·EIA_API_KEY 누락, DB 연결 실패, 미지원 소스) · `2` 실행은 끝났지만 스킵·실패 series 가 1건 이상. 조용한 성공 금지. `--dry-run` 도 error 발견 시 2.
- `--dry-run` 은 fetch·캐시 쓰기·parse·validate 까지 하고 DB 는 건드리지 않는다. 캐시 쓰기는 "받은 사실"의 기록이라 노트 00 과 충돌하지 않지만 디스크에 쓴다는 점은 여기 명시한다.
- `--incremental` = 소스 안 계열들의 `min(max(price_date)) − 14일` 부터. **겹침 창은 오케스트레이터가 한 번만 적용**하고 소스는 `start` 를 그대로 쓴다(첫 실전 실행에서 이중 적용 28일을 발견해 정정, 2026-09-13). 증분 단위는 소스별 §3 표.

## 8. 테스트 (Claude 가 뼈대 제공, JK 가 채움)

뼈대 상태의 테스트는 `pytest.mark.xfail(raises=NotImplementedError, strict=True)` 로 표시한다. CI 는 초록을 유지하고, JK 가 본문을 채워 테스트가 통과하는 순간 strict 때문에 **XPASS = 실패** 가 되어 마커를 떼라고 알려 준다. 마커를 떼는 것이 "구현 완료" 의 신호다. 파일: `tests/test_etl_{contract,parse,fetch,validate,cache,load}.py`.


| 테스트 | 픽스처 | 검사 |
|---|---|---|
| `test_parse_ecb` | `ecb_eurofxref-hist_2020-03_04.csv` | 42행 → 12 source_id 각각 DataFrame, `N/A` 제거, 오름차순, 꼬리 콤마 열 없음 |
| `test_parse_ustreasury` | `ustreasury_…_2020-03_04.csv` | `"10 Yr"` 선택, MM/DD/YYYY → date, 없는 열(`4 Mo`) 요청 시 빈 프레임 |
| `test_parse_eia` | `eia_rwtc_2020-04.json` | `-36.98` 이 float 로, `12.4` 문자열 처리, `series` 검증, 15행 |
| `test_eia_scrub_*` · `test_eia_fetch_paginates_and_scrubs` | `httpx.MockTransport` | `fetch` 가 넘기는 바이트에 키가 없고 `url` 에 쿼리스트링이 없다. 2페이지 → RawFile 2개 |
| `test_ecb_fetch_*` · `test_treasury_*` | `httpx.MockTransport` | 요청 URL, scope, 404 HTML·200 HTML 거부, 연도 루프(14일 겹침) |
| `test_validate_*` | 위 프레임 변형 | 중복 날짜 = error, WTI 음수 = warning(absolute), FX 음수 = error(log) |
| `test_upsert_prices` (`db`) | 임시 스키마 | 두 번 적재 시 두 번째는 unchanged, 값 하나 바꾸면 updated=1 이고 다른 행 `loaded_at` 불변 |
| `test_universe_upsert` (`db`) | `config/universe.csv` | 재적재 시 `instrument_id` 불변 |
| `test_cache_never_overwrites` | tmp_path | 같은 sha 는 파일 미생성, 다른 sha 는 새 파일 |
| `test_live_*` (`network` 마커, 기본 스킵) | 실제 엔드포인트 | 헤더 형식이 안 바뀌었는지. CI 에서는 돌리지 않는다 |

## 9. 의존성 제안

`httpx==0.28.1` 로 고정한다 [확인 2026-09-13: `pip index versions httpx` 안정판 최신 0.28.1, `--pre` 에 1.0.dev1~dev6 존재 — pre-release 는 채택하지 않는다]. import 는 `sources/*.py` 의 fetch 구현부에서만; `parse`·`validate`·`load`·`cache` 는 httpx 를 모른다. 테스트는 `httpx.MockTransport` 로 네트워크 없이 fetch 경로를 덮는다. 표준 라이브러리 `urllib` 만으로도 되지만 재시도·타임아웃을 직접 쓰게 되어 JK 의 첫 Python 네트워크 코드로는 과하다.

## 10. Python — JK 가 먼저 봐 둘 것 (Java 대응)

| Python | Java | 이 노트에서 쓰이는 곳 |
|---|---|---|
| `typing.Protocol` | interface — 단 `implements` 선언 없이 시그니처만 맞으면 됨 | `Source` |
| `@dataclass(frozen=True, slots=True)` | record | `Finding`·`Report`·`LoadResult` |
| `tuple[Finding, ...]` | `List<Finding>` 불변판 | `Report.findings` — frozen dataclass 안의 컬렉션도 불변이어야 해서 list 대신 tuple |
| `pd.read_csv(io.BytesIO(raw), na_values=["N/A"], parse_dates=["Date"])` | CSV 파서 + 타입 변환을 한 줄에 | ECB·Treasury parse |
| `df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y")` | `DateTimeFormatter.ofPattern("MM/dd/yyyy")` | Treasury |
| `pd.Int64Dtype()` / `"Int64"`(대문자) | `Integer`(nullable) vs `int` | `volume` — 소문자 `int64` 는 NA 를 못 담는다 |
| `cursor.executemany(sql, rows)` + `%(name)s` | JDBC batch + named parameter | 로더 |
| `with conn.transaction():` | try-with-resources + commit/rollback | series 단위 트랜잭션 |
| `hashlib.sha256(raw).hexdigest()` | `MessageDigest.getInstance("SHA-256")` | 캐시 manifest |
| `pathlib.Path` | `java.nio.file.Path` | 캐시 경로 |
| `logging.getLogger(__name__)` | `LoggerFactory.getLogger(Class)` | 전체 |
| pandas 3.0 Copy-on-Write | — | `df[df.x > 0]["y"] = …` 같은 체인 대입은 **오류**. `df.loc[mask, "y"] = …` 로 |

## 11. 승인 요청 — **승인됨(조건 반영, 2026-09-13)**. 조건은 §14

1. `fetch`(바이트) / `parse`(DataFrame) 분리와 `Source` 프로토콜.
2. 캐시 정책: 덮어쓰기 없음 + manifest.
3. `validate() -> Report` 읽기 전용, error 시 series 통째 스킵.
4. upsert 의 `IS DISTINCT FROM` 가드 + `xmax = 0` 카운트.
5. 구현 순서 ECB → Treasury → EIA.
6. `httpx` 의존성 추가.

## 12. `etl_runs` — ETL 실행 기록 (JK #5 승인, 0004 마이그레이션)

`db/migrations/0004_etl_runs.sql`. 한 행 = (sync 실행, series). **쓰는 주체는 sync 오케스트레이터뿐** — `validate()` 는 커넥션을 받지 않으므로 여기 쓸 수 없다(노트 00).

| 컬럼 | 타입 | `risk_runs` 와의 규약 일치 |
|---|---|---|
| `etl_run_id` | IDENTITY PK | `run_id` 와 같은 방식 |
| `started_at` · `finished_at` | TIMESTAMPTZ NOT NULL, `CHECK (finished_at >= started_at)` | `*_at` 규칙. `risk_runs` 는 현재 `created_at` 만 있다 — 엔진 노트에서 같은 쌍을 추가한다(감사 시 두 테이블을 같은 방식으로 읽기 위해) |
| `source` · `source_id` · `instrument_id`(nullable FK) | | 유니버스 밖 계열은 `instrument_id NULL` + `unknown_source_id` finding |
| `status` | `loaded` · `skipped` · `failed` · `dry_run` | CHECK 어휘 |
| `rows_fetched` · `rows_inserted` · `rows_updated` · `rows_unchanged` | INTEGER NOT NULL DEFAULT 0 | `rows_updated` = 벤더 정정 건수 (JK #4). 모델 문서 데이터 품질 절이 SQL 로 읽는다 |
| `first_date` · `last_date` | DATE | 프레임 범위 |
| `cache_sha256` | TEXT | manifest 의 키. 어느 파일에서 나왔는지 |
| `findings` | JSONB NOT NULL DEFAULT `[]` | 아래 스키마 |
| `params` | JSONB NOT NULL DEFAULT `{}` | `risk_runs.params` 와 같은 이름·용도. `{since, incremental, offline, dry_run, thresholds: {...}, thresholds_sha256}` |
| `code_version` | TEXT | `risk_runs.code_version` 과 동일 |

**`risk_runs` 와의 연결은 외래키로 하지 않는다** (JK 2026-09-13). 리스크 실행 하나가 수백 건의 적재 결과를 읽으므로 다대다다. 대신 `risk_runs.data_as_of`(그 실행이 읽은 가격 중 가장 늦은 `prices.loaded_at`)를 두어 **시간으로 조인**한다: "이 실행이 본 데이터는 어느 적재까지인가" = `etl_runs.finished_at <= risk_runs.data_as_of`. 엔진 노트에 예약, 지금 구현하지 않는다(노트 01 §4).

`findings` 스키마 (JK #5 조건 3 — 자유 형식 금지). 배열의 원소는 `Finding` dataclass 와 1:1:

```json
[
  {"level": "warning", "code": "nonpositive_price", "detail": "close <= 0 on absolute series",
   "price_date": "2020-04-20", "value": -36.98, "threshold": null},
  {"level": "warning", "code": "jump", "detail": "|dP| 55.29 > 10.0 USD/bbl",
   "price_date": "2020-04-20", "value": -55.29, "threshold": 10.0}
]
```

키는 항상 6개(`level`·`code`·`detail`·`price_date`·`value`·`threshold`), 해당 없으면 `null`. `code` 는 `contract.FindingCode` 의 닫힌 어휘. 조회 예: `SELECT * FROM etl_runs, jsonb_array_elements(findings) f WHERE f->>'code' = 'jump'`.

## 13. sync 1회가 받는 양과 캐시 성장 — 재계산 (JK 전제, 실측 2026-09-13)

| 소스 | 첫 실행 | 일별 증분 실행 | 연간 (250 영업일) |
|---|---|---|---|
| ECB | 639 KB (전체 zip) | **639 KB — 증분 없음** (매일 zip 내용이 바뀌므로 매일 새 파일) | ≈ 160 MB |
| 재무부 | 37 연도 × ≤ 15 KB ≈ 0.6 MB | 현재 연도 파일 ≤ 15 KB (1월엔 전년도 포함) | ≈ 4 MB |
| EIA | 8 계열, WTI 2.8 MB 최대, 합계 ≈ 15 MB [추정: 다른 계열은 짧다] | 계열당 14일 ≈ 10행 × 274 B + 봉투 ≈ 3 KB → 8 계열 ≈ 25 KB | ≈ 6 MB |
| **합계** | **≈ 16 MB** | **≈ 0.7 MB/일** | **≈ 170 MB/년** |

1 GB 도달 ≈ 6년. JK 가 우려한 "EIA 매번 전체 30 MB → 연 11 GB" 는 EIA 를 증분으로 두므로 발생하지 않는다. ECB 가 총량의 94 % 다. 증분 경로(90일 XML)는 존재하지만 파서를 하나로 유지하려고 채택하지 않았다(§3). 6년 뒤 1 GB 재검토 시 첫 후보가 그 XML 경로다.

겹침 창 **N = 14 일**(달력일 ≈ 10 영업일): 영업일 달력 의존성 없이 EIA·재무부의 통상 정정 범위를 덮는다 [추정 — ETL 첫 달의 `rows_updated` 분포로 검증하고 조정]. ECB 참조환율은 사실상 정정되지 않으나 전체 zip 이라 무관.

## 14. JK 조건 반영 목록 (2026-09-13)

| # | 조건 | 반영 위치 |
|---|---|---|
| 1 | 캐시 경로 `{source}/{scope}/{fetched_at_utc}_{sha12}_p{n}.{ext}`, 콜론 없는 시각 | §4, `cache.py` |
| 2 | manifest 는 바이트 사실만(행 수 제외), `same_as` | §4 |
| 3 | validate 읽기 전용, series 통째 스킵, Report 는 `etl_runs` 로, 스킵 시 exit ≠ 0, 결측 보정 없음 | §5, §7, §12 |
| 4 | 보존 무기한·1 GB 재검토, prune 수동·기록 | §4, §13 |
| 5 | `etl_runs` + 0004, validate 에 커넥션 없음, `risk_runs` 규약 일치, findings 스키마 | §12, `contract.py`, `0004_etl_runs.sql` |
| 6 | `httpx==0.28.1`, fetch 구현부에서만 import, MockTransport | §9, `pyproject.toml`, `test_etl_fetch.py` |
| 7 | jump 임계값 return_type 분기, 2020-04-20 은 warning | §5, `config/validation.toml`, `test_etl_validate.py` |
| 10 | `fetch -> Sequence[RawFile]`, `parse(raw) -> long frame` | §2, `contract.py` |
| — | 구현 순서 ECB → 재무부 → EIA, ECB 뒤 재무부 시그니처 점검 게이트 | §3 (재무부 시그니처는 이미 초안, 게이트는 ECB 완료 시 재확인) |

## 15. 공유 HTTP 헬퍼 — 재시도와 클라이언트 수명 (JK 질문 1·2, 2026-09-13)

**재시도는 소스마다 쓰지 않는다.** `etl/http.py` 의 `fetch_with_retry(client, url, *, params, policy, sleep)` 하나에 모은다. 이유: (1) 같은 루프를 세 소스에 복붙하게 되고, (2) 횟수·대기가 한 곳에 있어야 값 고정 테스트를 걸 수 있다. `httpx.HTTPTransport(retries=N)` 은 연결 실패만 재시도하고 5xx 는 재시도하지 않으므로 기본 기능으로는 요구를 못 맞춘다 [확인 2026-09-13: httpx 문서 "retries … connection failures"].

| 항목 | 값 | 고정 위치 |
|---|---|---|
| 재시도 대상 | 5xx(500·502·503·504)·429·타임아웃·전송 오류 | `RETRY_STATUSES` |
| 즉시 실패 | 그 외 4xx (`HTTPStatusError`) | |
| 시도 횟수 | 3 (첫 시도 포함) | `RetryPolicy.attempts` |
| 대기 | 1 s, 2 s (지수) — `sleep` 인자로 주입 가능해 테스트가 스케줄을 캡처 | `RetryPolicy.backoff_seconds` |
| 타임아웃 | 30 s | `RetryPolicy.timeout_seconds` |
| 소진 시 | `RetryExhaustedError` (마지막 오류를 `from` 으로) | |
| 로그 | URL 의 경로만. 쿼리스트링(EIA 키) 금지 | |

`RetryPolicy` 는 frozen — 실패를 보고 안에서 늘릴 수 없다(노트 00 §3). 값은 `tests/test_config_pins.py::test_retry_policy_is_pinned` 가 고정한다. 이 헬퍼의 본문은 보일러플레이트라 JK 의 작성 범위에서 제외한다(Claude 가 채운다).

**클라이언트 수명**: 소스는 생성자에서 `httpx.Client | None` 을 받는다. 주입된 클라이언트는 **빌려 쓰고 닫지 않는다**(소유자는 호출자 — 테스트, 또는 나중에 클라이언트 하나를 공유할 오케스트레이터). 주입이 없으면 `fetch` 호출 하나 동안만 `etl.http.client_for(None)` 이 클라이언트를 만들고 `with` 로 닫는다 — 예외가 나도 닫힌다. 재무부(연 37 요청)·EIA(16 요청)도 한 `fetch` 안에서 같은 클라이언트를 쓴다. `__init__` 에서 만들어 인스턴스에 보관하는 안은 소스 객체에 `close()` 의무가 생겨 버렸다. 계약은 `contract.Source` docstring 에 있다.

## 16. 국채 소스 교체: 재무부 CSV → FRED (JK 결정 2026-09-13)

**이유.** 첫 실전 sync 에서 home.treasury.gov 가 비브라우저 User-Agent 에 응답하지 않았다(6종 시험, `docs/decisions.md`). 브라우저 형식 문자열에 도구명을 넣으면 통과하지만, 공개 저장소에 WAF 우회를 남기지 않는다. 같은 계보(연준 H.15)를 FRED 가 공식 API 로 제공한다.

| | FRED |
|---|---|
| 엔드포인트 | `GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=…&file_type=json&limit=100000&sort_order=asc[&observation_start=…&observation_end=…]` |
| 인증 | 무료 키 `FRED_API_KEY`, **쿼리 파라미터** (헤더 방식 없음 [미확인: 키 없이 시험 불가]) → URL 은 로그·예외·manifest 어디에도 쿼리스트링 없이 기록 |
| 한 번의 fetch | 계열당 요청 1개 (limit 100,000 > 최장 계열 DGS1 16,158행) |
| 형식 | `observations[].{date, value}`; 결측은 `"."` → 행 제거 |
| 시작 | DGS1MO 2001-07-31, DGS3MO·DGS6MO 1981-09-01, DGS1·DGS3·DGS5·DGS10·DGS20 1962-01-02, DGS2 1976-06-01, DGS7 1969-07-01, DGS30 1977-02-15 [확인 2026-09-13] |
| **지연** | H.15 를 **T+1** 로 반영 — 재무부 페이지보다 하루 늦다 (2026-09-13 대조: 재무부에만 2026-09-11 존재). 일별 배치가 D 에 D−1 까지 본다 |
| 대조 | 재무부 적재분 96,145행과 날짜별 비교: **11계열 전부 불일치 0건**, 재무부에만 있는 날짜 = 최신 1일, FRED 에만 있는 날짜 = 1990 이전 이력 |

**instrument_id 보존.** `upsert_instruments` 의 충돌 키가 `(source, ticker)` 라 universe.csv 의 source 를 `fred` 로 바꾸면 새 행이 생기고 96,145행이 고아가 된다. 그래서 **데이터 마이그레이션 `0005_rates_to_fred.sql`** 이 먼저 `UPDATE instruments SET source='fred', source_id=DGS…` 로 기존 행을 바꾼다(DDL 없음, `instrument_id` 불변, `etl_runs` 이력의 `source='ustreasury'` 는 그대로). 그 뒤 universe.csv 의 `(fred, UST_10Y)` 가 기존 행과 충돌해 갱신만 일어난다. 테스트 `test_0005_moves_rates_to_fred_keeping_instrument_ids`.

**EIA 키 전달도 이 시점에 헤더로.** `X-Api-Key` 헤더를 받는다 [확인 2026-09-13]. 헤더로 보내도 응답이 키를 에코하므로 스크럽은 유지. `etl.http` 는 예외 메시지에 URL 경로만 넣는다.
