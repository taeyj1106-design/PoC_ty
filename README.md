# PoC_ty — SIAL 카탈로그 Algolia 검색 재현

[SIAL Paris](https://www.sialparis.com) 전시회 출품 상품 카탈로그의 검색 요청을
브라우저 없이 `requests` 로 재현하는 PoC 노트북.

## 무엇을 하는가

카탈로그 페이지는 Comexposium `connect2` Vue 앱이고, Algolia 검색 키를
하드코딩하지 않는다. 앱 부팅 시 GraphQL 로 설정을 받아오는 구조라서,
그 GraphQL 한 번만 호출하면 키·인덱스 이름을 전부 얻을 수 있다.

```
www.sialparis.com/en/exhibitors-2026
  └ connect2.prod.comexposium-webservices.com/connect2Loader.js
      └ js/cxpmc2.*.js  →  POST https://api.comexposium-sso.com/_/graphql
          query { Exhibitions { exhibition(id:"sial") {
                    embedConfigTemplate { globalConfigResult } } } }
          → globalConfigResult.algoliaConfig.{applicationId, apiKey}
```

따라서 이 노트북에는 **키가 하드코딩되어 있지 않다.** 실행 시점에 받아온다.
(받아오는 키는 검색 전용 public search key)

## 파일

| 파일 | 설명 |
| --- | --- |
| [`sial_algolia_request.ipynb`](sial_algolia_request.ipynb) | 전체 흐름 (설정 조회 → 검색 → 페이징 수집 → 저장) |

## 실행

```bash
pip install requests pandas
jupyter lab sial_algolia_request.ipynb
```

위에서부터 순서대로 실행하면 된다. `LOCALE = "en"` 을 `"fr"` 로 바꾸면
프랑스어 인덱스(`catalog.prod.sial.products.fr`)를 쓴다.

## 노트북 구성

1. **상수 + 세션** — `Origin` / `Referer` 를 실제 사이트로 맞춘 `requests.Session`
2. **API key 자동 확보** — `fetch_algolia_config()` 로 `applicationId` / `apiKey` 획득
3. **인덱스 이름** — `algoliaConfig.search.sorts` 의 `$LOCALE$` 치환
4. **요청 빌더** — Algolia multi-queries 규격의 `params` 쿼리스트링 생성
5. **실행** — 캡처된 요청과 동일 조건(`query=""`, `page=0`)으로 검증
6. **전체 수집** — `fetch_all()`, `fetch_by_facet()`
7. **저장** — `sial_products.json` / `sial_products.csv` (기본 주석 처리, gitignore 대상)

## 주의사항

- **페이징 상한**: Algolia `page` 기반 페이징은 `paginationLimitedTo`(이 인덱스는 약 2000건)
  까지만 돌려준다. `nbHits` 가 그보다 크면 `fetch_by_facet()` 으로 패싯을 쪼개 수집해야 한다.
  패싯 값이 비어 있는 상품은 이 방식으로 잡히지 않으므로, 마지막에 `nbHits` 와
  수집량을 비교해 누락분을 확인할 것.
- **죽은 패싯**: 캡처된 요청의 `made_in` 과 `specs.made_in_france.value` 는 이 인덱스의
  `attributesForFaceting` 에 없어서 응답에 아예 나타나지 않는다 (Algolia 가 조용히 무시).
  원산지 패싯의 실제 속성명은 `madeIn` 이다.
- **DSN 호스트**: `-dsn` → `-1` → `-2` → `-3` 순으로 리트라이한다.
- 대량 수집 시 `delay` 를 낮추지 말 것. 대상 사이트의 이용약관 확인은 사용자 책임이다.
