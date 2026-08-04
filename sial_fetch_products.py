"""SIAL Paris 2026 전시 제품 카탈로그 전량 수집 → JSON / CSV.

Algolia 검색 API 는 page 기반 페이징에서 paginationLimitedTo(=2000건) 까지만
돌려주고, browse 엔드포인트는 이 public key 로 막혀 있다(403).
그래서 `_createUTCTimestamp` 숫자 범위로 재귀 이분할해 각 구간을 2000건 미만으로
줄인 뒤 구간별로 페이징한다. 이 속성은 전 레코드가 갖고 있어(9784/9784) 누락이 없다.

사용법:  python sial_fetch_products.py
"""

import csv
import json
import re
import time
from urllib.parse import urlencode

import truststore

truststore.inject_into_ssl()  # 사내망 SSL 인터셉트 대응 (requests import 보다 먼저)

import requests

GRAPHQL_URL = "https://api.comexposium-sso.com/_/graphql"
SITE = "https://www.sialparis.com"
EXHIBITION_ID = "sial"
LOCALE = "en"

PAGE_LIMIT = 2000          # Algolia paginationLimitedTo
HITS_PER_PAGE = 200        # 이 인덱스는 distinct 가 켜져 있어 hitsPerPage*distinct<=1000 제약
SPLIT_ATTR = "_createUTCTimestamp"
DELAY = 0.1

CONFIG_QUERY = (
    "query ($exhibitionId: ID!) { Exhibitions { exhibition(id: $exhibitionId) "
    "{ embedConfigTemplate { globalConfigResult } } } }"
)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Origin": SITE,
        "Referer": SITE + "/",
    })
    return s


def bootstrap(session):
    """connect2 앱과 동일하게 GraphQL 로 Algolia 자격증명/인덱스명을 받아온다."""
    r = session.post(
        GRAPHQL_URL,
        headers={
            "Content-Type": "application/json",
            "gql-exhibitionContext": json.dumps({"id": EXHIBITION_ID}),
            "gql-translationLocale": LOCALE,
        },
        json={"query": CONFIG_QUERY, "variables": {"exhibitionId": EXHIBITION_ID}},
        timeout=30,
    )
    r.raise_for_status()
    cfg = (r.json()["data"]["Exhibitions"]["exhibition"]["embedConfigTemplate"]
           ["globalConfigResult"]["algoliaConfig"])
    app_id, api_key = cfg["applicationId"], cfg["apiKey"]
    session.headers.update({"x-algolia-api-key": api_key,
                            "x-algolia-application-id": app_id})
    index = cfg["search"]["sorts"]["products"]["default"]["value"].replace("$LOCALE$", LOCALE)
    url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
    return url, index


def query(session, url, index, filters=None, page=0, hits_per_page=0):
    p = {"query": "", "page": page, "hitsPerPage": hits_per_page}
    if filters:
        p["filters"] = filters
    r = session.post(url, json={"requests": [{"indexName": index, "params": urlencode(p)}]},
                     timeout=60)
    r.raise_for_status()
    res = r.json()["results"][0]
    if "nbHits" not in res:
        raise RuntimeError(f"Algolia 응답 이상: {res}")
    return res


def range_filter(lo, hi):
    """[lo, hi) 구간. lo=None 이면 하한 없음, hi=None 이면 상한 없음."""
    parts = []
    if lo is not None:
        parts.append(f"{SPLIT_ATTR} >= {lo}")
    if hi is not None:
        parts.append(f"{SPLIT_ATTR} < {hi}")
    return " AND ".join(parts) if parts else None


def split_ranges(session, url, index, lo, hi, count, depth=0):
    """각 구간이 PAGE_LIMIT 미만이 될 때까지 이분할한 (lo, hi, count) 리스트를 만든다."""
    if count < PAGE_LIMIT:
        return [(lo, hi, count)]
    if hi is not None and lo is not None and hi - lo <= 1:
        # 같은 timestamp 에 2000건 이상 — 더 못 쪼갠다 (여기선 발생하지 않음)
        print(f"  ! 경고: ts={lo} 구간에 {count}건, 분할 불가 → 앞 {PAGE_LIMIT}건만 수집됨")
        return [(lo, hi, count)]
    mid = (lo + hi) // 2
    left = query(session, url, index, range_filter(lo, mid))["nbHits"]
    right = count - left
    print(f"  {'  ' * depth}분할 [{lo}, {mid}) = {left} / [{mid}, {hi}) = {right}")
    time.sleep(DELAY)
    return (split_ranges(session, url, index, lo, mid, left, depth + 1)
            + split_ranges(session, url, index, mid, hi, right, depth + 1))


def fetch_range(session, url, index, lo, hi):
    """한 구간을 페이징으로 전부 가져온다."""
    out = []
    filters = range_filter(lo, hi)
    page = 0
    while True:
        res = query(session, url, index, filters, page=page, hits_per_page=HITS_PER_PAGE)
        out.extend(res["hits"])
        if page + 1 >= res["nbPages"]:
            return out, res["nbHits"]
        page += 1
        time.sleep(DELAY)


HTML_TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean_html(text):
    """description/characteristics 는 HTML 이라 CSV 용으로 태그를 걷어낸다."""
    if not text:
        return ""
    t = HTML_TAG.sub(" ", str(text))
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return WS.sub(" ", t).strip()


def joined(items, key=None):
    if not items:
        return ""
    if key:
        return " | ".join(str(i.get(key, "")) for i in items if isinstance(i, dict) and i.get(key))
    return " | ".join(str(i) for i in items if i)


def flatten(h):
    """CSV 한 행으로 쓸 주요 필드만 평탄화한다."""
    exhibitor = h.get("exhibitor") or {}
    brand = h.get("brand") or {}
    prices = h.get("prices") or {}
    details = h.get("details") or {}
    stands = h.get("stands") or []
    logo = h.get("logoLink") or {}
    slug = h.get("slug") or {}
    main_ba = ((h.get("mainBusinessArea") or {}).get("label") or {}).get(LOCALE, "")

    # 스탠드 위치: hall/aisle/number 를 "7-J-446" 형태로
    locations = [
        "-".join(x for x in (st.get("hall"), st.get("aisle"), st.get("number")) if x)
        for st in stands if isinstance(st, dict)
    ]
    slug_value = slug.get("value", "") if isinstance(slug, dict) else ""

    return {
        "objectID": h.get("objectID", ""),
        "name": h.get("name", ""),
        "brand": brand.get("name", "") if isinstance(brand, dict) else "",
        "exhibitor": exhibitor.get("name", "") if isinstance(exhibitor, dict) else "",
        "manufacturer": details.get("manufacturer") or "",
        "madeIn": h.get("madeIn") or "",
        "mainBusinessArea": main_ba,
        "businessAreas": joined(h.get("businessArea"), "name"),
        "productTypes": joined(h.get("productTypes")),
        "thematics": joined(h.get("thematics")),
        "certifications": joined(h.get("certifications"), "label"),
        "stand_sectors": joined(stands, "sector"),
        "stand_locations": " | ".join(locations),
        "price_currency": prices.get("currencyCode") or "",
        "price_standard_incl_tax": prices.get("standardPriceIncludingTaxes") or "",
        "price_exhibition_incl_tax": prices.get("exhibitionPriceIncludingTaxes") or "",
        "reference": details.get("reference") or "",
        "worldPremiereLabel": h.get("worldPremiereLabel") or "",
        "description": clean_html(h.get("description")),
        "characteristics": clean_html(h.get("characteristics")),
        "product_url": h.get("url") or "",
        "catalog_url": (f"{SITE}/{LOCALE}/exhibitors-2026/product/{slug_value}"
                        if slug_value else ""),
        "image_url": logo.get("default", "") if isinstance(logo, dict) else "",
        "imageCount": h.get("imageCount", ""),
        "createdAt": h.get("createdAt") or "",
        "updatedAt": h.get("updatedAt") or "",
    }


def main():
    session = make_session()
    url, index = bootstrap(session)
    print(f"인덱스: {index}")

    total = query(session, url, index)["nbHits"]
    print(f"전체 제품 수: {total}\n분할 계산 중...")

    ranges = split_ranges(session, url, index, 1700000000, 2100000000, total)
    print(f"\n구간 {len(ranges)}개로 분할 완료\n")

    seen, hits = set(), []
    for i, (rlo, rhi, expected) in enumerate(ranges, 1):
        got, nb = fetch_range(session, url, index, rlo, rhi)
        new = [h for h in got if h.get("objectID") not in seen]
        seen.update(h.get("objectID") for h in new)
        hits.extend(new)
        print(f"[{i}/{len(ranges)}] [{rlo}, {rhi}) 예상 {nb} / 수신 {len(got)} / 신규 {len(new)} / 누적 {len(hits)}")
        time.sleep(DELAY)

    print(f"\n수집 완료: {len(hits)} / 전체 {total}")
    if len(hits) != total:
        print(f"  ! 차이 {total - len(hits)}건 (중복 objectID 또는 수집 중 인덱스 변경)")

    with open("sial_products.json", "w", encoding="utf-8") as f:
        json.dump(hits, f, ensure_ascii=False, indent=2)
    print("저장: sial_products.json")

    rows = [flatten(h) for h in hits]
    with open("sial_products.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"저장: sial_products.csv ({len(rows)}행 x {len(rows[0])}열)")


if __name__ == "__main__":
    main()
