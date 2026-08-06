# PoC_ty — 식품 전시회 카탈로그 수집

SIAL / Anuga / ISM / FOODEX 출품 카탈로그를 브라우저 없이 `requests` 로 수집하는 PoC.
전시회마다 백엔드가 달라서 접근 방식도 다르다.

| 전시회 | 스크립트 | 수집 단위 | 방식 |
| --- | --- | --- | --- |
| SIAL Paris 2026 | [`sial_fetch_products.py`](sial_fetch_products.py) | 제품 10,248 | Algolia 검색 API |
| Anuga 2025 | [`anuga_fetch_exhibitors.py`](anuga_fetch_exhibitors.py) | 출품사 8,265 | HTML 파싱 (Koelnmesse ASDB) |
| ISM Cologne | [`ism_fetch_exhibitors.py`](ism_fetch_exhibitors.py) | 출품사 1,614 | HTML 파싱 (사이트맵 경유) |
| FOODEX JAPAN 2025 | [`foodex_fetch_products.py`](foodex_fetch_products.py) | 제품 881 | Web Archive 스냅샷 |

**개별 제품 레코드가 있는 건 SIAL 과 FOODEX 뿐이다.** Anuga·ISM 은 같은 ASDB
시스템이라 제품군(분류 트리)·브랜드 수준까지만 있다.

## SIAL 파이프라인

세 단계로 나뉜다. 앞 단계 산출물을 뒤 단계가 읽는다.

```
sial_fetch_products.py        Algolia 에서 제품/브랜드/출품사 3개 인덱스 전량 수집
  → sial_products.json/csv       + 홈페이지·SNS 링크 조인

sial_check_ecommerce.py       고유 도메인 1,397개의 홈페이지를 1회씩 받아
  → sial_ecommerce_domains.json  이커머스 플랫폼 지문 / 장바구니 / 가격 흔적 판정

sial_check_product_pages.py   제품 상세 URL 2,505개를 직접 받아
  → sial_product_pages.json      그 페이지가 실제 구매 가능한지 판정
```

### 1) API key 는 하드코딩되어 있지 않다

카탈로그 페이지는 Comexposium `connect2` Vue 앱이고, 부팅 시 GraphQL 로 설정을
받아온다. 그 GraphQL 한 번이면 키·인덱스 이름을 전부 얻는다.

```
www.sialparis.com/en/exhibitors-2026
  └ connect2.prod.comexposium-webservices.com/connect2Loader.js
      └ js/cxpmc2.*.js  →  POST https://api.comexposium-sso.com/_/graphql
          query { Exhibitions { exhibition(id:"sial") {
                    embedConfigTemplate { globalConfigResult } } } }
          → globalConfigResult.algoliaConfig.{applicationId, apiKey}
```

받아오는 키는 검색 전용 public search key다. 전체 흐름을 단계별로 뜯어본 노트북이
[`sial_algolia_request.ipynb`](sial_algolia_request.ipynb) 에 있다.

### 2) 2000건 페이징 상한 우회

Algolia 는 `page` 기반 페이징에서 `paginationLimitedTo`(=2000) 까지만 돌려주고
`browse` 는 이 public key 로 막혀 있다(403). 그래서 `_createUTCTimestamp` 숫자
범위를 재귀 이분할해 각 구간을 2000건 미만으로 줄인 뒤 구간별로 페이징한다.
이 속성은 전 레코드가 갖고 있어 누락이 없다 (10,248/10,248 확인).

### 3) 링크는 다른 인덱스에서 조인

제품 인덱스에는 홈페이지/SNS 가 없다. `brands` 의 `website`, `exhibitors` 의
`urls.*` 를 같이 받아 `brand.id` / `exhibitor.id` 로 붙인다.

| 컬럼 | 채움률 (제품 10,248건 기준) |
| --- | --- |
| `homepage` (brand → exhibitor → product URL 순 대체) | 95.4% |
| `exhibitor_website` | 91.2% |
| `exhibitor_linkedin` / `instagram` / `facebook` | 57.3% / 56.1% / 50.4% |
| `brand_website` | 49.1% |

### 4) 온라인 판매 여부 판정

**1단계 (도메인)** — 플랫폼 지문(`cdn.shopify.com`, `woocommerce`, PrestaShop…)
또는 장바구니+가격이 있으면 A, 하나만 있으면 B, 없으면 C.

| `sold_online` | 도메인 1,397 | 제품 10,248 |
| --- | --- | --- |
| yes | 441 (31.6%) | 3,077 (30.0%) |
| likely | 54 | 556 |
| no | 731 (52.3%) | 5,221 (50.9%) |
| unknown | 171 | 1,394 |

플랫폼은 WooCommerce 328 / Shopify 58 / PrestaShop 24 순. 식품 중소기업은
워드프레스 기반이 주류다.

**2단계 (제품 페이지)** — 제품 상세 URL 을 직접 받아 가격+장바구니 확인.
URL 이 있는 3,151건 기준:

| `product_page_purchasable` | 건수 | 비율 |
| --- | --- | --- |
| yes (가격+장바구니) | 606 | 19.2% |
| likely (장바구니만) | 561 | 17.8% |
| price_only (가격만) | 160 | 5.1% |
| no | 1,824 | 57.9% |

## 산출물

| 파일 | 크기 | 내용 |
| --- | --- | --- |
| `sial_products.csv` | 10,248행 × 47열 | 기본 26 + 링크 10 + 이커머스 6 + 제품페이지 5 |
| `sial_products.json` | 84MB | 원본 hit + `_links` / `_ecommerce` / `_product_page` |
| `sial_ecommerce_domains.json` | 0.3MB | 도메인 판정 캐시 |
| `sial_product_pages.json` | 0.9MB | 제품 URL 판정 캐시 |
| `anuga_exhibitors.csv` | 8,265행 × 16열 | |
| `ism_exhibitors.csv` | 1,684행 × 15열 | 1,614행만 내용 있음 (아래 참고) |
| `foodex_products.csv` | 881행 × 16열 | |

수집 결과물은 저장소에 커밋한다 (다른 PC 에서 이어받기 위함). 단
`sial_products.json` 만 GitHub 권장 한도(50MB)를 넘어 제외한다 — 재생성 15분.
판정 캐시 2개는 커밋되어 있어 이커머스 스캔 53분은 재실행할 필요가 없다.

## 실행

```bash
pip install requests pandas truststore
python sial_fetch_products.py          # 15분
python sial_check_ecommerce.py         # 25분 (캐시 있으면 즉시)
python sial_check_product_pages.py     # 28분 (캐시 있으면 즉시)
```

`LOCALE = "en"` 을 `"fr"` 로 바꾸면 프랑스어 인덱스를 쓴다.

## 주의사항

**수집 예의**

- Anuga / ISM 의 robots.txt 는 ClaudeBot·GPTBot 등 AI 크롤러를 `Disallow: /` 로
  전면 차단한다. 사용자가 명시적으로 지시한 경우에만 사용한다.
- ISM·Anuga 상세 페이지는 요청률에 민감하다. 초당 1건을 넘기면 원본이 실제
  페이지 대신 홈페이지를 돌려준다 (Anuga 실측: 4워커 → 성공률 2%). 기본 간격을
  함부로 올리지 말 것.
- 이커머스 판정은 외부 사이트 1,289개를 건드린다. robots.txt 를 확인하고,
  도메인당 홈페이지 1회 + 제품 URL 은 0.4초 간격 순차 요청으로 제한한다.
- 대상 사이트의 이용약관 확인은 사용자 책임이다.

**데이터 해석**

- `sold_online` 과 `product_page_purchasable` 은 답하는 질문이 다르다. 전자는
  "이 회사가 온라인몰을 운영하는가", 후자는 "이 제품을 실제로 살 수 있는가"다.
- 몰만 깔아두고 팔지는 않는 사이트가 있다. 도메인이 `yes` 인데 제품 페이지엔
  구매 수단이 없는 경우가 **8.7%** (274/3,151).
- 제품 URL 이 있는 건 34.5% 뿐이라, 나머지는 도메인 판정으로 갈음해야 한다.
  브랜드 홈페이지가 없으면 출품사 홈페이지로 대체되므로 유통사가 출품한
  제품은 특히 어긋난다.
- **마켓플레이스(Amazon·쿠팡 등) 판매는 잡지 못한다.** 자사몰이 없어도 거기서
  팔릴 수 있는데 제품당 검색이 필요하고 동명 오탐이 크다. 현재 데이터에 있는
  마켓플레이스 직링크는 11건(전부 Alibaba)뿐이다.
- FOODEX 는 전수가 아니라 **표본**이다. 아카이브에 남은 881건으로 전체 13,000
  여 건 중 약 7% — 점유율 분석에 쓰면 편향된다.
- Anuga 데이터는 2025년(종료) 회차다. 다음 회차는 2027-10-09~13.
- ISM 은 사이트맵 1,684건 중 **1,614건(95.8%) 수집, 70건은 상세 페이지가 없다.**
  원본이 404 대신 출품사 목록 페이지를 200 으로 돌려주는 탓에 죽은 경로와
  일시적 폴백이 구분되지 않는다. CSV 에 그 70행이 빈 행으로 남아 있다.
  재시도 간격을 짧게 잡으면(0.4초 4연타) 그 자체가 초당 1건을 넘겨 폴백을
  유발하고 성공률이 0% 로 떨어진다 — 백오프를 2·4·6초로 두는 이유다.

**죽은 패싯** — 캡처된 요청의 `made_in` 과 `specs.made_in_france.value` 는 이
인덱스의 `attributesForFaceting` 에 없어서 응답에 나타나지 않는다 (Algolia 가
조용히 무시). 원산지 패싯의 실제 속성명은 `madeIn` 이다.
