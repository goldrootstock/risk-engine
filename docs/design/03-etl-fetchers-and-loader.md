# 설계 노트 03 — ETL fetcher · 로더 · CLI

- 상태: 노트 **승인**(2026-09-12). §11 의 6개 항목은 채팅 요약 후 개별 승인 대기
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

## 2. 계약 (contract.py) — JK 가 구현할 인터페이스

```python
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import pandas as pd

PRICE_COLUMNS: dict[str, str] = {
    "price_date": "datetime64[ns]",  # 로더가 date 로 변환
    "close": "float64",
    "adj_close": "float64",
    "volume": "Int64",  # nullable 정수. 세 소스 모두 NA
}


class Source(Protocol):
    """소스 하나. 구현 클래스는 이 프로토콜을 상속하지 않아도 된다(구조적 타이핑)."""

    name: str  # instruments.source 와 같은 문자열

    def fetch(
        self, source_ids: list[str], start: date | None, end: date | None
    ) -> Mapping[str, bytes]:
        """원본 바이트를 source_id 별로 돌려준다. 파싱하지 않는다. 네트워크는 여기서만."""
        ...

    def parse(self, source_id: str, raw: bytes) -> pd.DataFrame:
        """원본 바이트 → PRICE_COLUMNS 를 가진 DataFrame. 결측 행 제거. 정렬은 price_date 오름차순."""
        ...


@dataclass(frozen=True, slots=True)
class Finding:
    level: str  # "error" | "warning"
    code: str  # "missing_column" | "duplicate_date" | "nonpositive_price" | "jump" …
    detail: str


@dataclass(frozen=True, slots=True)
class Report:
    """validate() 의 반환값. 데이터를 바꾸지 않는다."""

    source_id: str
    rows: int
    first: date | None
    last: date | None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)


@dataclass(frozen=True, slots=True)
class LoadResult:
    instrument_id: int
    fetched: int
    inserted: int
    updated: int
    unchanged: int
```

`fetch` 와 `parse` 를 나눈 이유: (1) 캐시는 **바이트**를 저장하므로 오프라인 재실행이 `parse` 만 다시 돈다. (2) 픽스처 테스트가 네트워크 없이 `parse` 를 검사한다. (3) 노트 00 — 네트워크 부수효과가 한 함수에만 있다.

## 3. 소스별 fetcher

| | ECB | U.S. Treasury | EIA |
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

**ECB 를 첫 번째로 권고**하는 이유: 요청 1개·키 없음·페이지네이션 없음이라 fetcher 골격(fetch/parse/cache/validate/load/CLI)을 **가장 짧은 경로로 끝까지** 관통한다. 그러면서도 결측(`N/A`)·내림차순·꼬리 콤마·12 계열 동시 처리라는 실제 문제가 다 들어 있어 골격의 설계 결함이 바로 드러난다. 두 번째는 Treasury(연도 루프·열 구성 변화·yield 경로), 마지막이 EIA(키·JSON·페이지네이션·음수). 국채가 팩터 구조의 핵심이지만 "먼저 만들 것"은 엔지니어링 리스크가 낮은 쪽이다.

## 4. 캐시 (cache.py) — 노트 00 #2

```
data/raw/<source>/<source_id>/<fetched_at YYYY-MM-DDTHHMMSSZ>.<ext>
data/raw/<source>/<source_id>/manifest.jsonl     # 한 줄 = {fetched_at, sha256, bytes, url}
```

- 새 fetch 는 **항상 새 파일**. 덮어쓰지 않는다. `--offline` 은 최신 파일을 읽는다.
- **비밀은 캐시에 들어가지 않는다.** EIA 응답은 요청 파라미터(API 키 포함)를 에코하므로 `fetch` 가 키를 지운 바이트를 넘긴다. manifest 의 `url` 도 쿼리스트링을 뺀 형태로 기록한다. 캐시 디렉터리는 gitignore 지만 그것에 기대지 않는다.
- 같은 sha256 이면 파일을 쓰지 않고 manifest 에만 기록(중복 방지).
- ECB 처럼 소스 파일 하나가 여러 source_id 를 담으면 `<source>/_all/` 아래 저장하고 parse 가 열을 고른다.
- 정리(`prune`)는 별도 명령. sync 는 지우지 않는다.

## 5. 검증 (validate.py) — 읽기 전용, `Report` 반환

| 검사 | level | 근거 |
|---|---|---|
| 필수 컬럼·dtype | error | 계약 위반 |
| `price_date` 중복 | error | PK 충돌 전에 잡는다 |
| `price_date` 가 오늘 이후 | error | 벤더 오류 |
| `quote_type = 'price'` 이고 `return_type = 'log'` 인데 close ≤ 0 | error | 로그수익률 불가 (FX 만 해당) |
| `return_type = 'absolute'` 이고 close ≤ 0 | **warning** | 정상 사건 (WTI 2020-04-20). 기록만 |
| 일간 변화: price 계열 \|단순수익률\| > 30 %, yield 계열 \|Δ\| > 100 bp | warning | 검토용. 자르지 않는다 |
| DB 의 기존 값과 다른 행 수 (재공시) | info | `LoadResult.updated` 와 대조 |

`validate()` 는 DataFrame 을 **받기만** 하고 돌려주지 않는다 — 시그니처가 원칙을 강제한다. error 가 있으면 로더는 그 series 를 **통째로 건너뛴다**(부분 적재 없음).

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

## 8. 테스트 (Claude 가 뼈대 제공, JK 가 채움)

| 테스트 | 픽스처 | 검사 |
|---|---|---|
| `test_parse_ecb` | `ecb_eurofxref-hist_2020-03_04.csv` | 42행 → 12 source_id 각각 DataFrame, `N/A` 제거, 오름차순, 꼬리 콤마 열 없음 |
| `test_parse_ustreasury` | `ustreasury_…_2020-03_04.csv` | `"10 Yr"` 선택, MM/DD/YYYY → date, 없는 열(`4 Mo`) 요청 시 빈 프레임 |
| `test_parse_eia` | `eia_rwtc_2020-04.json` | `-36.98` 이 float 로, `12.4` 문자열 처리, `series` 검증, 15행 |
| `test_eia_scrubs_api_key` | 키가 든 가짜 응답 | `fetch` 가 넘기는 바이트와 manifest 에 키 문자열이 없다 |
| `test_validate_*` | 위 프레임 변형 | 중복 날짜 = error, WTI 음수 = warning(absolute), FX 음수 = error(log) |
| `test_upsert_prices` (`db`) | 임시 스키마 | 두 번 적재 시 두 번째는 unchanged, 값 하나 바꾸면 updated=1 이고 다른 행 `loaded_at` 불변 |
| `test_universe_upsert` (`db`) | `config/universe.csv` | 재적재 시 `instrument_id` 불변 |
| `test_cache_never_overwrites` | tmp_path | 같은 sha 는 파일 미생성, 다른 sha 는 새 파일 |
| `test_live_*` (`network` 마커, 기본 스킵) | 실제 엔드포인트 | 헤더 형식이 안 바뀌었는지. CI 에서는 돌리지 않는다 |

## 9. 의존성 제안

`httpx` 를 추가한다 (타입 힌트 내장, `requests` 와 같은 동기 API, 타임아웃 기본값 있음). 표준 라이브러리 `urllib` 만으로도 되지만 재시도·타임아웃·스트리밍을 직접 쓰게 되어 JK 의 첫 Python 네트워크 코드로는 과하다.

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

## 11. 승인 요청

1. `fetch`(바이트) / `parse`(DataFrame) 분리와 `Source` 프로토콜.
2. 캐시 정책: 덮어쓰기 없음 + manifest.
3. `validate() -> Report` 읽기 전용, error 시 series 통째 스킵.
4. upsert 의 `IS DISTINCT FROM` 가드 + `xmax = 0` 카운트.
5. 구현 순서 ECB → Treasury → EIA.
6. `httpx` 의존성 추가.
