# 설계 노트 02 — ETL · 데이터 소스 · 유니버스 · 달력 정렬

- 상태: §8 승인 요청 6개 **승인**(2026-09-12). 이후 JK 지시로 유니버스 31계열 확대 · 공공누리 4유형 보류 · E3 재평가 · §10 마진 의존성 반영 — **확정본**
- 범위: `instruments`·`prices` 를 채우는 ETL 과 그 입력 소스. 수익률 행렬(달력 정렬 포함)은 코드상 별도 모듈이지만 정렬 옵션은 여기서 결정한다.
- 역할: 노트·리뷰·테스트 픽스처 = Claude / ETL 코드(소스 fetcher · 로더 · CLI) = JK
- 표기: [확인] = 약관 원문 열람 · [추정] = 간접 근거 · [미확인] = 원문을 못 찾음

---

## 0. 한 줄 요약

**원본 벤더 데이터는 어느 소스든 GitHub 에 올리지 않는다.** 올리는 것은 코드·유니버스 정의·파생 집계 결과·소형 픽스처뿐이다. 그 위에서, 자동 수집이 약관상 허용되고 파생 결과 공개가 금지되지 않은 소스만 쓴다. 이 조건을 [확인] 수준으로 통과한 무료 소스는 **미 재무부(금리)·ECB(FX)·EIA(에너지)** 셋이고, **주식·선물은 통과한 무료 소스가 없다.**

## 1. 데이터 정책 (저장소에 무엇을 올리나)

| 규칙 | 내용 | 이유 |
|---|---|---|
| P1 원본 미커밋 | `data/raw/` 는 `.gitignore`. 어떤 소스든 받은 파일 그대로는 커밋하지 않는다 | 소스가 공공저작물이라도 습관을 하나로. 나중에 소스를 바꿔도 정책이 안 바뀐다 |
| P2 커밋 대상 | 코드 · `config/universe.csv`(티커 목록) · 파생 집계(ES/VaR 수치, 백테스트 통계, 차트, 모델 문서) · 테스트 픽스처 | 파생 집계는 "데이터셋"이 아니라 분석 결과. 픽스처는 P3 소스이거나 합성 데이터만 |
| P3 소스 조건 | (i) 자동 다운로드가 약관에서 금지되지 않음 (ii) 파생 결과 공개가 금지되지 않음 (iii) 키 없이 또는 무료 키로 누구나 다시 받을 수 있음 | (i) 없이는 ETL 자체가 위반. (ii) 없이는 README 의 숫자가 위반. (iii) 없이는 "재현 가능"이 거짓 |
| P4 출처 기록 | 결과물마다 데이터 스냅샷 날짜 + 소스 인용문(ECB·EIA·Treasury 가 요청하는 형식) | 세 소스 모두 "출처 표기"가 조건 또는 요청 사항 |

## 2. 소스 후보 비교 (2026-09-12 약관 열람 기준)

| 소스 | 자산군 | (i) 자동 수집 | 원본 재배포 | (ii) 파생 결과 공개 | (iii) 비용·키 | 근거 | 판정 |
|---|---|---|---|---|---|---|---|
| **U.S. Treasury** — Daily Par Yield Curve (home.treasury.gov) | rates | 허용 (CSV·XML 공식 제공) | 공공저작물 | 허용 | 무료·키 없음 | [확인] 페이지에 CSV/XML 링크, 저작권 고지 없음. 미 연방정부 저작물은 17 U.S.C. §105 로 public domain | **채택** |
| **ECB** — Euro foreign exchange reference rates | fx | 허용 (CSV zip 공식 제공) | **허용, 출처 표기 조건** | 허용 | 무료·키 없음 | [확인] "distributed or reproduced … the ECB must be cited as the source" | **채택** |
| **EIA** — WTI · Brent · Henry Hub 일별 현물 | commodity | 허용 (API v2, 무료 키 / CSV) | 공공저작물 | 허용 | 무료 키 | [확인] "U.S. government publications are in the public domain". 단 제3자 제공 자료 예외 명시 → 위 3개 계열은 EIA 자체 계열 [추정] | **채택** (spot 이라 선물 롤 없음) |
| FRED (St. Louis Fed) | 위 셋의 편의 API | 허용 (API) | **금지** — "may not be … distributed … republished … without prior written permission" | 개인 이용 한정 | 무료 키 | [확인] FRED 약관. 개별 계열(DGS10)은 "Public Domain: Citation Requested" 표시 [확인] — 공공저작물이지만 FRED 경유 취득분은 FRED 약관을 탄다 | **불채택** — 같은 데이터를 원천 기관에서 직접 받는다 |
| Yahoo Finance (`yfinance`) | equity · etf · futures | **금지** — "access or collect data … using any automated means … for any purpose without our express, prior permission" | 금지 | 비상업 한정 | 무료 | [확인] Yahoo ToS. `yfinance` 자체도 "refer to Yahoo's terms for your rights to use the data" | **제외** — 수집 단계에서 이미 위반 |
| Massive (구 Polygon) 무료 플랜 | equity | 허용 | 이전·공유 금지 | 개인·비상업 한정, 파생물 조항 없음 | 무료 키 | [확인] individuals ToS: "solely for your own personal, non-commercial, and non-business purposes" | **보류** — 공개 포트폴리오가 "personal" 인지 약관이 답하지 않음 |
| Databento | equity · etf · futures(CME) | 허용 (공식 API) | [미확인] — 약관·라이선스 페이지가 열리지 않음 | [미확인] | 신규 $125 크레딧(6개월) 후 GB 당 과금. 일별 OHLCV 20종목 × 10년은 수 MB 수준 [추정] | [확인] pricing 페이지. 블로그: "historical (T+1) 데이터는 시장 라이선스 불필요" [추정 — 자사 주장] | **후보** — JK 가 Terms 원문을 열어 (ii) 를 확인한 뒤 |
| Stooq | equity · etf · futures | [미확인] | [미확인] | [미확인] | 무료·키 없음 | 약관 페이지를 검색·직접 접속 모두 실패 | **후순위 fallback** — 약관을 찾기 전엔 쓰지 않는다 |

**결론**: 금리·FX·에너지는 [확인] 소스가 있다. 주식·ETF·선물은 무료로 조건 (i)(ii) 를 [확인] 으로 통과하는 소스가 **없다.** 그래서 유니버스를 두 단계로 나눈다.

### 2-1. 주식·ETF·지수 소스 추가 조사 (2026-09-12, JK 요청)

조건은 §1 P3 와 같다: (i) 자동 수집 허용 · (ii) 파생 결과 공개 허용 · (iii) 무료 재취득 가능. "원본 재배포"는 P1 로 어차피 하지 않으므로 판정 기준에서 뺀다.

| 소스 | 제공 데이터 | (i) 자동 수집 | (ii) 파생 결과 공개 | (iii) 비용 | 근거 | 판정 |
|---|---|---|---|---|---|---|
| **Alpha Vantage** 무료 키 | 미국 주식·ETF 일별 OHLCV(조정 포함) | 허용 (API) | **조항 없음.** 단 라이선스가 "personal, non-commercial" 이고 상업 이용 판정 기준 (i) 이 "investment analysis, research, testing … activities that are **private and individual in nature**" — 공개 저장소가 "private" 인지 약관이 답하지 않음. 파생물 소유권은 "User shall retain all right … to its data, and any other Content developed by User" 로 이용자에게 | 무료 키, 일 25 요청 [추정] | [확인] ToS PDF 전문 열람 (2026-09-12) | **조건부 후보** — 수집은 명백히 허용, 공개는 해석 문제 |
| **공공데이터포털 — 금융위원회_주식시세정보 · 증권상품시세정보(ETF·ETN)** | KRX 상장 주식·ETF 일별 OHLCV, 익영업일 13시 갱신 | 허용 (OpenAPI, 무료 키) | **제한 가능성 있음.** 이용허락범위 원문: "공공누리 4유형 : 출처표시 + 상업적 이용금지 + **변경금지**" · "본 데이터는 상업적 목적 여부와 상관 없이 제3자 무단 제공 및 재배포가 엄격히 금지됩니다". **변경금지 조항은 파생물 제작 제한으로 읽힐 수 있다** — 리스크 수치도 데이터의 변형물이라는 해석이 가능 | 무료 | [확인] 두 페이지 이용허락범위 원문 (2026-09-12) | **보류** — 변경금지의 해석을 제공기관에 문의하기 전엔 쓰지 않는다 |
| **Nasdaq 지수 EOD 수준** (indexes.nasdaqomx.com) | NDX 등 지수 일별 종가, 다운로드 버튼 | 다운로드 제공, 로그인 섹션 존재(필수 여부 미확인) | 검색 스니펫상 Nasdaq Index Data Usage and Distribution Policy 가 "index level performance calculated from end of day index levels" 의 외부 배포를 GDA(Global Distribution Agreement) 없이 허용한다고 읽히나, **원문 PDF 를 열지 못했다(301→404)** | 무료 | [추정] — 스니펫만. **[추정] 상태의 문구는 경로 근거로 쓰지 않는다** | **원문 확인 후 재평가** |
| **Kenneth French Data Library** | 미국 주식 시장·산업 포트폴리오 **일별 수익률**(가격 아님), 팩터 | 허용 (정적 zip) | 저작권 표시 외 **라이선스 문구 없음.** 학술 논문에서 보편적으로 재사용 | 무료 | [확인] 페이지에 "Copyright Eugene F. Fama and Kenneth R. French" 외 조건 없음 | **보조 후보** — "equity market portfolio" 로 자산군은 채우지만 거래 가능 상품이 아니라 마진 모듈엔 부적합 |
| Cboe (VIX·SPX 이력 CSV) | 지수 이력 | 다운로드 제공 | **사전 서면 승인 + 라이선스 계약 필수.** "may not … create a derivative work … distribute … without Cboe's prior written consent" | — | [확인] /terms · /use-of-content | **제외** |
| iShares / BlackRock (NAV 이력 CSV) | ETF NAV | **금지** — "any robot, spider … automatic device … to copy this Website or the … data" | "may not distribute … for public or commercial purposes" | — | [확인] Terms and Conditions | **제외** |
| Tiingo 무료(Starter) | 미국 주식·ETF EOD | 허용 | **금지** — "publishing or otherwise making available to the public any analysis of … Tiingo Data". 무료 플랜은 **저장 자체 금지**("may not … retain Tiingo Data in any persistent or durable storage") | — | [확인] app.tiingo.com/tos | **제외** |
| Nasdaq Data Link | — | — | — | — | [미확인] 약관 페이지가 JS 로만 렌더링. 무료 EOD 주식 데이터셋(WIKI)은 2018 종료 [추정] | **보류** |
| S&P DJI 지수 페이지 · ECB 데이터포털의 STOXX 지수 · 한국은행 ECOS 의 KOSPI | 지수 수준 | — | — | — | [미확인] S&P 403, ECB 503, ECOS 는 JS. ECOS 이용조건("한국은행 작성 통계는 출처 표시로 상업 포함 자유 이용, 타 기관 작성 통계는 비상업")은 2차 출처에서만 확인 [추정] | **보류** |
| JPX · Euronext · HKEX · KRX 직접(data.krx.co.kr) | — | — | — | — | 이번에 확인하지 않음 | 미조사 |

**결론(주식)**: (i)+(ii) 를 [확인] 으로 동시에 통과하는 **무료** 소스는 없다. 가장 가까운 것은 Alpha Vantage(수집 명백 허용, 공개는 "private and individual" 해석) 하나다. 공공데이터포털 KRX 시세는 변경금지 조항 때문에 보류, Nasdaq 지수 EOD 는 정책 원문을 열기 전엔 판단 보류. 유료-저가 경로는 Databento(JK 확인 중).

### 2-2. "지수 수준"으로 우회하는 안

| 경로 | 가능 여부 | 비고 |
|---|---|---|
| 지수 제공자 직접 (Nasdaq indexes 사이트) | **원문 확인 후 재평가** | 위 표. 스니펫상 "EOD 지수 수준에서 계산한 성과"의 외부 배포 면제로 읽히나 원문 미열람 [추정] |
| 중앙은행·통계기관 경유 (FRED SP500·NIKKEI225, ECB STOXX, ECOS KOSPI) | **낮음** | 지수는 제3자 저작물이라 기관이 재배포 조건을 원저작자에 넘긴다. FRED SP500 은 "Copyrighted: Citation Required" + FRED 자체 재배포 금지 [확인 FRED 약관]. OECD 주가지수는 CC BY 4.0 이지만 **월별** [추정] |
| 학술 데이터 (French 라이브러리) | 가능하나 상품이 아님 | 시장·산업 포트폴리오 수익률. 리스크 엔진 검증엔 충분, 마진 모듈엔 부적합 |

## 3. 유니버스

### 3-1. v1 — 공공저작물·공개 라이선스만, 31계열 (1~2주차, 지금 구축)

JK 지시(2026-09-12): 10계열은 얇다. 같은 소스가 공표하는 계열로 25~30 을 채우고, 국채 곡선 전체를 넣어 level·slope·curvature 팩터 구조를 만든다.

**국채 — 11 만기** (source `ustreasury`, quote_type `yield`, USD). 아카이브 CSV 는 1990-01-02 부터 [확인].

| ticker | 원천 열 | 시작 | 비고 |
|---|---|---|---|
| UST_1M | 1 Mo | 2001-07 [추정 — 2001년 파일 안에 존재 확인, 정확한 첫 날은 ETL 첫 실행에서 기록] | |
| UST_3M · UST_6M · UST_1Y · UST_2Y · UST_3Y · UST_5Y · UST_7Y · UST_10Y | 3 Mo … 10 Yr | 1990-01-02 [확인] | 1990 파일에 9개 열 전부 값 있음 |
| UST_20Y | 20 Yr | 1993-10-01 [확인 — 재무부 페이지 주석] | 1986 말 중단 후 재개 |
| UST_30Y | 30 Yr | 1990-01-02 [확인], **2002-02-18 ~ 2006-02-09 공백** [확인 — 재무부 페이지 주석] | 30년물 발행 중단 기간 |
| 제외 | 1.5 Mo · 2 Mo(2018-10-16~) · 4 Mo(2022-10-19~) | | 이력이 짧아 intersection 시작일을 끌어올린다 |

**FX — 12 통화** (source `ecb`, quote_type `price`). ECB 는 32개 통화를 16:00 CET 경 고시 [확인]. DB 에는 ECB 원계열(1 EUR = x CCY)을 그대로 넣고 USD 크로스는 수익률 빌더가 계산한다.

| DB 원계열 | 리스크 유니버스의 USD 크로스 | 시작 |
|---|---|---|
| EURUSD | EURUSD | 1999-01-04 [추정 — 유로 출범일] |
| EURJPY · EURGBP · EURCHF · EURCAD · EURAUD · EURNZD · EURSEK · EURNOK · EURSGD · EURKRW | USDJPY · GBPUSD · USDCHF · USDCAD · AUDUSD · NZDUSD · USDSEK · USDNOK · USDSGD · USDKRW | 1999-01-04 [추정] |
| EURCNY | USDCNY | 2005-04 [추정] |
| 제외 | BRL · MXN · INR · ILS 등 2008 이후 추가분, RUB(2022-03-01 고시 중단 [확인]) | intersection 시작일 보호 |

**에너지 — 8 계열** (source `eia`, quote_type `price`, USD). EIA 현물 페이지의 기간 표기 [확인].

| ticker | EIA 계열 | 단위 | 시작 |
|---|---|---|---|
| WTI | WTI Cushing | $/bbl | 1986 |
| BRENT | Brent Europe | $/bbl | 1987 |
| GASOLINE_NYH | NY Harbor conventional gasoline regular | $/gal | 1986 |
| GASOLINE_USGC | US Gulf Coast conventional gasoline regular | $/gal | 1986 |
| HEATOIL_NYH | NY Harbor No. 2 heating oil | $/gal | 1986 |
| JET_USGC | US Gulf Coast kerosene-type jet fuel | $/gal | 1990 |
| PROPANE_MB | Mont Belvieu propane | $/gal | 1992 |
| HENRYHUB | Henry Hub natural gas spot | $/MMBtu | 1997 |
| 제외 | NYH·USGC ULSD(2006-06~), LA RBOB(2003~), LA ULSD | | 2006 이후 시작 또는 지역 중복 |

**합계 31 계열** (국채 11 · FX 12 · 에너지 8).

**적재 범위와 표본 윈도우는 다른 층이다** (JK 지시 2026-09-12, 노트 01 §1 "DB 에는 사실만" 과 일치):

- **ETL 은 각 계열의 가용 이력을 전부 적재한다** — 국채 1990-01-02, 에너지 1986, FX 1999 부터. `--since` 는 기본값 없음(= 전체). 표본 시작일은 사실이 아니라 계산 시점의 선택이므로 ETL 에 박지 않는다.
- **표본 윈도우·유니버스 부분집합은 수익률 빌더의 파라미터**다. `config/universes.toml` 에 이름 붙인 집합을 두고, 실행이 어느 집합을 썼는지 `risk_runs.params.universe` 에 기록한다.

| 집합 | 시작 | 구성 | 용도 |
|---|---|---|---|
| `default` | 2006-02-09 (30년물 재개일) | 31 전부 | 기본. 2008 · 2020-03 · 2022 포함, intersection 후 ≈ 5,000 영업일 [추정] |
| `from_1999` | 1999-01-04 (유로 출범) | 31 − {UST_1M, UST_30Y, EURCNY} = 28 | 1999 부터의 장기 백테스트. 30Y 공백·1M·CNY 제외 |
| `rates_energy_1990` | 1990-04-02 (제트유 시작) | UST 3M~10Y 8 + WTI·Brent·휘발유 2·난방유·제트유 6 = 14 | 최장 이력. FX 없음 |

"장기 이력 백테스트 vs 전체 유니버스" 비교가 실험이 되고, 그 비교 자체가 모델 문서의 내용이 된다.

**팩터 구조 (3주차 재료)**: 국채 11 만기의 Δy 에 PCA 를 걸면 level·slope·curvature 3 성분이 분산의 95% 이상을 설명하는 것이 표준 결과 [추정 — 구현 후 실측]. FX 12 는 USD 팩터 + 통화별 잔차, 에너지 8 은 원유 팩터 + 정제 마진(크랙) + 가스로 묶인다. 만기 간 고상관은 상관 붕괴 시나리오와 PCA 실험의 소재다.

**수익률 노트 03 로 넘기는 두 가지**: (1) 에너지 현물의 음수 종가(WTI 2020-04-20) — 방침은 §11. (2) FX 크로스의 부호·환산 순서.
### 3-2. v2 — 주식·ETF·선물: 경로 4개 (§2-1 조사 결과)

| 경로 | 자산 | 조건 | 권고 순위 |
|---|---|---|---|
| **E1 Databento** | SPY·QQQ·EFA·EEM · TLT·IEF · GLD · ES·GC·CL 연속 선물 | JK 가 약관에서 파생물 공개 조항 확인. $125 크레딧 안에서 일별 바 10년치 해결 [추정] | 1 — 거래 가능 상품 + 선물이라 마진 모듈까지 한 번에 |
| **E2 Alpha Vantage** | 위 ETF 동일(선물 없음) | 무료. 공개 저장소가 "private and individual" 에 해당하는지 JK 판단. 원본 미커밋(P1) 은 동일 | 2 — 즉시 가능, 해석 리스크 |
| E3 Nasdaq 지수 EOD | NDX(+OMX) 지수 수준 | **정책 원문 확인 후 재평가.** 그 전엔 순위 없음 | — |
| E4 공공데이터포털 KRX | 삼성전자 등 + KODEX ETF (KRW) | **보류** — 변경금지 조항(§2-1) | — |

소스가 무엇이든 `universe.csv` 에 행 추가 + fetcher 하나가 전부여야 한다. **E1 통과 못 하면 E2 로 간다** — 수집이 명백히 허용되고, 우리 정책 P1(원본 미커밋)이 약관의 "private" 요구와 같은 방향이다. E3·E4 는 확인 결과가 바뀌면 그때 순위를 다시 매긴다.

## 4. ETL 설계

### 4-1. 구조

```
config/universe.csv                    # instruments 의 원천. 컬럼 = source,ticker,name,asset_class,instrument_type,quote_type,currency,multiplier
src/risk_engine/data/etl/
  __init__.py
  sources/__init__.py                  # Source 프로토콜 + 레지스트리 {"ustreasury": ..., "ecb": ..., "eia": ...}
  sources/ustreasury.py                # fetch(ticker, start, end) -> DataFrame
  sources/ecb.py
  sources/eia.py
  load.py                              # upsert_instruments(universe) · upsert_prices(df)
  __main__.py                          # python -m risk_engine.data.etl sync [--since] [--tickers] [--offline]
data/raw/<source>/<ticker>.csv         # gitignore. 받은 원본 캐시 (--offline 재실행용)
```

`Source` 는 Java 의 interface 에 해당하는 `typing.Protocol` 로 정의한다 — 구현 클래스가 상속을 선언하지 않아도 시그니처만 맞으면 통과한다(구조적 타이핑).

```python
class Source(Protocol):
    name: str

    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Columns: price_date(date), close(float), adj_close(float), volume(int|NA). 결측 행은 제거해서 반환."""
```

### 4-2. 규칙

| 항목 | 결정 | 이유 |
|---|---|---|
| 멱등 | `INSERT … ON CONFLICT (instrument_id, price_date) DO UPDATE SET … WHERE prices.close IS DISTINCT FROM EXCLUDED.close OR …` | 값이 같으면 `loaded_at` 을 건드리지 않는다 — "언제 바뀌었나"가 의미를 유지 |
| 적재 범위 | **첫 실행은 전체 이력**(`--since` 기본값 없음). 증분 실행은 `last price_date − 10 영업일` 부터 다시 받는다 | 표본 시작일은 계산 파라미터지 적재 조건이 아니다(§3-1). 10일은 벤더 재공시(restatement) 흡수 — EIA·Treasury 의 통상 정정 범위 [추정] |
| 카운트 | `RETURNING (xmax = 0) AS inserted` 로 insert/update 구분 | PostgreSQL 관용구: 새 행은 xmax=0. 로그에 fetched/inserted/updated 를 남긴다 |
| 검증 (적재 전) | 필수 컬럼·타입, 날짜 중복 없음, `quote_type='price'` 면 close>0, 일간 변화 \|Δ\|>30%(price) 또는 >100bp(yield) 는 **경고만** | 거부하면 2020-04 WTI 음수 같은 실제 사건을 잃는다. 경고는 로그와 리뷰용 |
| 결측 | 소스의 빈 값('N/A', 공란, '.')은 행 제거. **채우지 않는다** | 노트 01 §1 원칙 — DB 는 받은 것만 |
| adj_close | 세 소스 모두 조정 개념이 없음 → `adj_close = close` | v2 의 ETF 소스에서만 달라진다 |
| 로그 | stdlib `logging`, (source, ticker) 당 1줄: 기간·fetched/inserted/updated·소요 | 재실행 가능한 ETL 의 증거. `etl_runs` 테이블은 필요해지면 |
| 대량 적재 | v1 은 `executemany`(계열당 수천 행). 10만 행 넘으면 `COPY` → 임시 테이블 → upsert | 지금 COPY 는 과잉 |
| 오프라인 | `--offline` 이면 `data/raw` 캐시만 사용 | 소스 장애·비행기에서도 재현 |

### 4-3. 소스별 메모

| 소스 | 엔드포인트 | 형식 특이점 |
|---|---|---|
| ustreasury | `…/TextView?type=daily_treasury_yield_curve&field_tdr_date_value=YYYY` 의 CSV 링크, 연도별 1파일 | 열 이름 "2 Yr" 등, 일부 만기(1.5 Mo, 4 Mo, 20 Yr)는 기간에 따라 공란. 고시 시각 = NY 15:30 [추정] |
| ecb | `eurofxref-hist.zip` (전체 이력 CSV 하나) | EUR 기준, 'N/A' 문자열, 고시 시각 = **CET 14:15**. TARGET 휴일 없음 |
| eia | API v2 `/petroleum/pri/spt/data/` (RWTC·RBRTE) · `/natural-gas/pri/fut/data/`(RNGWHHD), JSON, 무료 키 | 미 연방 휴일 없음. 2020-04-20 WTI −37 USD 실존 |

## 5. 달력 정렬 (수익률 빌더의 옵션 — 여기서 결정)

세 소스의 휴일이 다르다 (SIFMA 채권시장 · TARGET · 미 연방). 정렬은 DB 가 아니라 수익률 행렬을 만드는 코드에서 한다.

| 옵션 | 동작 | 장점 | 단점 | 결정 |
|---|---|---|---|---|
| `intersection` | 전 계열에 값이 있는 날짜만 남긴다 | 인공 값 없음. 검정 통계에 왜곡 없음 | 연 1~2% 날짜 손실 [추정]. 이틀치 변화가 하루 수익률로 합쳐지는 날이 생김 | **기본값** |
| `ffill` (max_gap=1) | 하루 결측은 전일 값으로 채우고 채운 행을 플래그 | 날짜 보존 | 채운 계열은 그날 수익률 0 → 변동성 과소 + **0 수익률 군집이 Christoffersen 독립성 검정을 오염** | 옵트인. 채운 행 수를 실행 로그와 `risk_runs.params` 에 기록 |
| 마스터 달력 (NYSE 등) | 외부 달력에 맞춰 커버리지 요구 | 명확한 기준 | 의존성 추가(`pandas_market_calendars`), v1 유니버스에 NYSE 상품이 없음 | v2 에서 재검토 |

**비동기 종가 문제**: ECB 14:15 CET(NY 08:15) vs Treasury NY 15:30 vs EIA 일중 평균/종가. 같은 "날짜"라도 관측 시각이 다르면 계열 간 상관이 과소 추정된다. v1 에서는 **알려진 한계로 모델 문서에 기록**하고, v2 에서 FX 를 Fed H.10(NY 정오) 로 바꿀지 검토한다 — H.10 은 공공저작물이지만 연준 DDP 가 FRED 로 이관 중이라 취득 경로를 다시 봐야 한다 [확인: DDP 축소 공지 / 미확인: 이관 후 경로].

## 6. 결정을 미룬 것 — 수익률 노트(03)에서

- **yield 계열의 P&L 매핑**: `positions.quantity × multiplier × Δprice` 는 price 계열용이다. yield 계열은 Δy(bp) 에 DV01 을 곱해야 하며, 상수만기 국채의 일별 수익률을 수익률 변화로 근사하는 표준식(r ≈ −D·Δy + y·Δt, Swinkels 2019)을 쓸지, `multiplier` 를 "1bp 당 통화 P&L" 로 정의할지 정한다. **ETL 은 영향 없음** — yield 그대로 저장.
- FX 크로스(USDJPY = EURJPY/EURUSD)의 계산 위치와 EUR 기준 → USD 기준 부호.

## 7. JK 가 작성할 코드와 Claude 가 준비할 것

| JK | Claude |
|---|---|
| `sources/ustreasury.py` · `ecb.py` · `eia.py` (fetch + 파싱) | 각 소스의 실제 응답을 잘라낸 픽스처 3개 (`tests/fixtures/etl/`, 공공저작물이라 커밋 가능, 각 20행 내외) |
| `load.py` upsert 2개 + 카운트 | `universe.csv` 초안 10행 + `instruments` upsert 테스트 |
| `__main__.py` CLI | 코드 리뷰: psycopg 파라미터 바인딩, pandas 3.0 Copy-on-Write, `Protocol`, `logging` |

Python 에서 먼저 봐 둘 것 (Java 대응): `typing.Protocol`(interface, 단 구조적) · `pandas.read_csv` 와 `DataFrame.itertuples`(ResultSet 순회) · `cursor.executemany`(JDBC batch) · `dict | dict`(3.9+, 병합) · `logging.getLogger(__name__)`(SLF4J 의 `getLogger(Class)`) · `argparse`(picocli) · `date`/`datetime` 는 다른 타입(LocalDate/LocalDateTime).

## 8. 승인 요청 (전부 승인, 2026-09-12)

1. 데이터 정책 P1~P4.
2. 유니버스 2단계: v1 = 공공저작물 **31계열**(국채 11 · FX 12 · 에너지 8, 표본 2006-02-09~)로 1~2주차 진행, v2 = 주식·ETF·선물을 §3-2 경로로 추가.
3. 주식 경로 우선순위: E1 Databento → E2 Alpha Vantage. E3 는 원문 확인 후 재평가, E4 는 보류.
4. FX 소스 = ECB (대안 H.10 은 v2 에서).
5. 달력 정렬 기본 = `intersection`, `ffill` 은 옵트인·기록.
6. 원본 캐시 `data/raw/` + `--offline` 재실행.
7. 마진 모듈 데이터 의존성: §10 권고(M2 뼈대 + M1 업그레이드) — JK 판단 대기.

## 9. 이력서 문구 — v1 상태에서 (사실 불변)

v1 에 주식이 없으므로 Gap plan §5 의 "[N] instruments across equities, rates, FX and commodities" 는 그대로 쓸 수 없다. v2 가 들어오기 전까지의 문구:

- Version A: "Built an end-to-end risk engine for [N] instruments across **rates, FX and energy commodities** (public-domain data: U.S. Treasury, ECB, EIA): …"
- Version B/C 도 자산군 열거만 같은 방식으로 교체. "multi-asset" 은 세 자산군이면 사실이다.
- Equity 서사는 P1 이 아니라 Aurora 의 Equity Options Market-Making 줄이 담당한다. P1 은 "포트폴리오 리스크·규제 백테스트·CCP 마진" 증거이지 주식 증거가 아니다.
- v2 로 주식·선물이 들어오면 그때 "equities, rates, FX and commodities" 로 되돌린다. 그 전에 그 문구를 쓰는 것은 §3-3 원칙 위반.

## 10. 마진 모듈(P1-Margin, 5~7주차)의 데이터 의존성 — M1 vs M2

v1 유니버스에는 파생상품이 없다. SPAN 2 / IRM 2 식 마진의 구성 요소별로 어떤 데이터가 필요한지 먼저 나누고, 두 경로가 각 요소를 얼마나 "진짜"로 만드는지 본다.

### 10-1. 구성 요소별 필요 데이터

| 구성 요소 | 필요한 것 | M1 실물 선물 (Databento GLBX.MDP3) | M2 합성 선물 (공공 데이터) |
|---|---|---|---|
| 코어 마진: FHS ES, 2일 MPOR(Margin Period of Risk, 마진 리스크 기간) | 선물 가격 시계열 | 실물 연속 계약(롤 처리 필요) | 현물·CMT 기반 합성 가격. **수익률은 현물과 거의 동일** |
| 안티-프로시클리컬 플로어 · 스트레스 블렌드 · 25% 버퍼 | 긴 이력(2008·2020 포함) | 있음 | 있음 (EIA 1986~, Treasury 1990~) |
| **Cross-margining (현물 vs 선물 상계)** | 현물과 선물의 **베이시스 시계열** | 실물 베이시스 (컨비니언스 일드·롤·만기 효과 포함) | **베이시스 ≈ 결정론적 캐리 → 상계율 ≈ 100%.** 상계 헤어컷을 데이터에서 추정 불가, 가정값 |
| 캘린더 스프레드 · 상품 간 스프레드 차지 | 복수 만기 가격 | 실물 만기 구조 | 만기 구조를 캐리로 합성하면 **스프레드 리스크 = 0** |
| 유동성 · 집중 add-on | 거래량·미결제약정(ADV) | ohlcv-1d 의 volume, statistics 스키마의 OI | **없음** (EIA·Treasury·ECB 모두 거래량 없음) → 파라미터 가정 |
| Legacy SPAN 16 시나리오 | 가격 스캔 범위 + 변동성 스캔 범위 | 내재변동성 없음(옵션 데이터 별도) → 역사적 변동성 프록시 | 동일 |
| Cover-2 디폴트 펀드 | 복수 청산회원 포트폴리오 | 어차피 합성 | 어차피 합성 |
| 마진 커버리지 백테스트 | 일별 마진 vs 실현 2일 손실 | 실물 | 합성 (자기 일관적이라 통과하기 쉬움 — 검증력 약함) |

### 10-2. 솔직한 판정

- **M2 로 진짜에 가깝게 되는 것**: 코어 FHS 마진, 플로어·블렌드, Cover-2, 커버리지 백테스트의 *메커니즘*. 가정을 모델 문서에 쓰면 방어 가능하다 — "합성 선물 F = S·exp((r−c)τ), c=0, τ 고정, 롤 없음".
- **M2 로는 인위적이 되는 것**: cross-margining 과 스프레드 차지. 합성 선물의 일별 수익률은 현물 수익률에 캐리 미분(하루에 수 bp)을 더한 것이라 현물-선물 상관이 0.99 를 넘는다. 상계 헤어컷은 데이터가 아니라 가정에서 나오고, 면접관은 정확히 그 지점을 묻는다 — "베이시스 리스크와 롤은 어떻게 다뤘나". 답은 "다루지 않았다"가 된다. 유동성 add-on 도 ADV 가 없어 파라미터가 된다. **CCP 퀀트의 핵심 문제(베이시스·롤·유동성)가 빠진 마진 모듈**로 읽힌다.
- **M1 로 얻는 것**: 위 세 가지가 실물이 된다. 비용은 ohlcv-1d 가 계약당 하루 1행이라 10년 × 10상품이 수 MB — $125 크레딧 안 [추정, Batch 견적으로 확인]. 대신 롤 규칙(만기 며칠 전, 거래량 기준)과 백조정을 직접 구현해야 한다 — 이건 오히려 이력서 소재다.

### 10-3. 권고: M2 를 뼈대로, M1 을 세 요소의 업그레이드로

1. **지금(5주차 전)**: 마진 엔진을 **상품 무관하게** 설계한다 — 입력은 `instruments`·`prices`·`positions` 뿐이고 `instrument_type` 으로 현물/선물을 구분한다. v1 데이터로 코어 마진·플로어·블렌드·Cover-2·커버리지 백테스트를 구현·테스트한다. 이 부분은 Databento 와 무관하게 끝난다.
2. **Databento 통과 시**: CL·NG·RB·HO·ZN·ZF·ZB(+ES·GC) 연속 계약을 v2 유니버스에 넣고 cross-margining·스프레드 차지·유동성 add-on 을 실물 데이터로 켠다. 모델 문서의 "한계" 절에서 해당 항목을 삭제한다.
3. **Databento 불통과 시**: cross-margining 은 상계율을 **외부 공표값**(CME 가 공개하는 inter-commodity spread credit 비율 [미확인 — 이용조건 확인 필요])으로 파라미터화하고, 모델 문서에 "합성 선물 — 베이시스·롤·유동성 미반영"을 알려진 취약점으로 명시한다. 이력서 셋째 bullet 에서 "spot/futures cross-margining" 문구를 빼고 "VaR/ES-based initial margin with anti-procyclicality floors and Cover-2 sizing" 까지만 쓴다.

즉 M2 는 일정을 Databento 에서 떼어 내는 데 쓰고, "진짜 마진"의 세 요소는 M1 에서만 주장한다. 두 경로를 섞되 **어느 결과가 어느 데이터에서 나왔는지** 모델 문서와 이력서가 구분하면 방어 가능하다.

**승인 (2026-09-12).** 모델 문서 "알려진 취약점" 절에 다음 두 문장을 그대로 넣는다:

> 1. 합성 선물(F = S·exp((r−c)τ), c = 0, τ 고정, 롤 없음)의 일별 수익률은 현물 수익률과의 상관이 0.99 를 넘으므로, 현물–선물 cross-margining 의 상계 헤어컷은 데이터에서 추정된 값이 아니라 가정값이다.
> 2. 유니버스 v1 의 소스(재무부·ECB·EIA)에는 거래량·미결제약정이 없으므로 유동성·집중 add-on 의 ADV 기반 파라미터는 추정이 아니라 설정값이다.

Databento(M1) 로 실물 선물이 들어오면 해당 문장을 삭제하고 실측 결과로 교체한다.

## 11. 음수 종가(WTI 2020-04-20)와 수익률 정의 — 방침 (노트 03 에서 확정)

| 정의 | 2020-04-20 전후에서 | FHS 표준화에 미치는 영향 | 백테스트에 미치는 영향 |
|---|---|---|---|
| 로그수익률 ln(P_t/P_{t-1}) | **정의 불가**(P ≤ 0). 0 근처에서도 폭주(0.5→1 = +69%) | 결측 또는 무한대 → 창 전체 오염 | 실현 P&L 계산 불가 |
| 단순수익률 ΔP/P_{t-1} | 정의되지만 18→−37 = −306%, −37→9 = −124%(부호 의미 상실) | 잔차 z ≈ −30 하나가 500일 창의 ES 97.5%(하위 12.5 개)를 지배. 시나리오 P&L = 포지션 × P_오늘 × r 이라 오늘 $80 에 −306% 를 적용하면 −$165 라는 **물리적으로 불가능한 가격** | 초과 판정은 어차피 발생. 이후 500 일 동안 VaR 이 한 잔차에 끌려다님 |
| **절대 변화 ΔP (USD/bbl)**, 변동성도 $ 단위로 EWMA/GARCH | −$55 라는 유한한 달러 충격 | z = ΔP/σ_$ 는 크지만 유한. 시나리오 = z × σ_$(오늘) 는 **가격 수준과 무관하게 달러로 정의**돼 불가능한 가격이 안 나온다. 수익률 노트에서 국채 Δy(bp) 와 같은 코드 경로 | 실현 손실 = 포지션 × ΔP_실제, 동일 매핑. 초과 1건, 창 내 영향은 σ_$ 스케일링이 흡수 |
| shifted log ln((P+c)/(P_{t-1}+c)) | c 를 골라야 함(예: 40) | 결과가 c 에 종속. 음수 금리의 shifted-lognormal 처럼 알려진 기법이지만 c 는 자의적 | c 마다 다른 백테스트 |
| 에너지만 별도 처리 | 위 중 하나를 에너지에만 | 상품별 정의가 달라도 **표준화 잔차 행렬은 단위 없는 z 라서 날짜별 공동 샘플링(상관 구조)이 유지**된다 | 상품별 P&L 을 합산할 뿐이라 영향 없음 |

**방침**: 상품 속성 `return_type ∈ {log, absolute}` 를 두고, FX(와 v2 주식)는 `log`, 국채 수익률과 에너지 현물은 `absolute`. FHS 는 z 를 공동 샘플링하고 상품별로 σ(오늘) × 오늘 단위(달러 또는 bp)로 되돌려 P&L 을 만든다. 절대 변화의 단점(원유 $100 에서의 달러 변동성이 $30 보다 크다는 수준 의존성)은 EWMA σ_$ 가 창 안에서 적응하며 부분 흡수하고, 나머지는 모델 문서 한계에 쓴다. shifted log 는 c 민감도 실험으로만 남긴다. 부수 효과: WTI 음수 구간에서 롱 포지션의 평가액이 음수가 되므로 `portfolio_value` 가 음수일 수 있다 — 결과를 NAV 비율이 아니라 통화로 저장한 결정(노트 01 §8-4)이 여기서도 맞다.
