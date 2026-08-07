"""3단계 — 제품/브랜드 사이트가 마켓플레이스 판매를 안내하는지 확인한다.

1·2단계는 자사몰만 본다. 자사몰이 없어도 Amazon·쿠팡 같은 마켓플레이스에서
팔리는 경우가 많은데, 그걸 제품명으로 검색해 확인하는 건 이 PoC 에서 하지 않는다
(제품당 검색 10,248회 + 봇 차단 + 동명 오탐).

대신 **브랜드가 자기 사이트에 걸어둔 마켓플레이스 링크**를 본다.
"Buy on Amazon", 쿠팡 배너, Alibaba 상점 링크 같은 것들이다. 오탐은 거의 없지만
누락은 많다 — 링크가 없다고 그 마켓플레이스에서 안 판다는 뜻이 아니다.
즉 이 판정은 **하한선(lower bound)** 이다.

대상은 1·2단계에서 이미 쓰던 URL 이다 (도메인 홈페이지 + 제품 상세 URL).
결과는 sial_marketplace_pages.json 에 캐시되고, sial_products.json 에 `_marketplace`
로 병합된다.

사용법:  python sial_check_marketplace.py
"""

import collections
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import truststore

truststore.inject_into_ssl()

import requests

import sial_fetch_products as S
import sial_check_ecommerce as E

PRODUCTS_JSON = "sial_products.json"
PRODUCTS_CSV = "sial_products.csv"
CACHE = "sial_marketplace_pages.json"

WORKERS = 5
TIMEOUT = 15
# 마켓플레이스 링크는 푸터에 있는 경우가 많아 본문을 넉넉히 읽어야 한다
# (500KB 로 자르면 736KB 짜리 페이지의 푸터를 통째로 놓친다)
MAX_BYTES = 2_000_000
DELAY = 0.4          # 같은 도메인 안에서의 간격

# 일시적 실패는 캐시에 남기지 않고 다음 실행에서 다시 시도한다
RETRYABLE = {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
             "ChunkedEncodingError", "SSLError", "HTTP 502", "HTTP 503", "HTTP 504"}

# 호스트 패턴 -> 마켓플레이스 이름. 링크(href)에서만 찾는다.
MARKETPLACES = {
    "amazon": r"(?:^|\.)amazon\.[a-z.]{2,6}|amzn\.(?:to|eu|asia)",
    "ebay": r"(?:^|\.)ebay\.[a-z.]{2,6}",
    "alibaba": r"(?:^|\.)alibaba\.com",
    "aliexpress": r"(?:^|\.)aliexpress\.[a-z.]{2,6}",
    "rakuten": r"(?:^|\.)rakuten\.co\.jp|item\.rakuten",
    "coupang": r"(?:^|\.)coupang\.com",
    "naver_smartstore": r"smartstore\.naver\.com|brand\.naver\.com",
    "gmarket": r"(?:^|\.)gmarket\.co\.kr",
    "11st": r"(?:^|\.)11st\.co\.kr",
    "kurly": r"(?:^|\.)kurly\.com",
    "iherb": r"(?:^|\.)iherb\.com",
    "etsy": r"(?:^|\.)etsy\.com",
    "walmart": r"(?:^|\.)walmart\.com",
    "target": r"(?:^|\.)target\.com",
    "ocado": r"(?:^|\.)ocado\.com",
    "carrefour": r"(?:^|\.)carrefour\.[a-z.]{2,6}",
    "mercadolibre": r"mercadolibre\.[a-z.]{2,6}|mercadolivre\.",
    "shopee": r"(?:^|\.)shopee\.[a-z.]{2,6}",
    "lazada": r"(?:^|\.)lazada\.[a-z.]{2,6}",
    "tmall_taobao": r"(?:^|\.)tmall\.com|(?:^|\.)taobao\.com",
    "jd": r"(?:^|\.)jd\.com",
    "weee": r"sayweee\.com",
    "yamibuy": r"(?:^|\.)yamibuy\.com",
}
COMPILED = {name: re.compile(pat, re.I) for name, pat in MARKETPLACES.items()}

HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)

_lock = threading.Lock()
_progress = {"done": 0, "total": 0}


def find_marketplaces(html, page_host):
    """페이지의 외부 링크에서 마켓플레이스 호스트를 찾는다."""
    hits = collections.defaultdict(set)
    for href in HREF.findall(html):
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        host = urlparse(href if "//" in href else "//" + href).netloc.lower()
        if not host or host == page_host:
            continue
        for name, rx in COMPILED.items():
            if rx.search(host):
                hits[name].add(href[:300])
                break
    return hits


def check_urls(host, urls):
    """한 도메인의 URL 들을 순차 확인한다."""
    s = requests.Session()
    s.headers.update({"User-Agent": E.UA, "Accept": "text/html,*/*",
                      "Accept-Language": "en;q=0.9"})
    out = {}
    allowed = E.robots_allows(s, f"https://{host}/")
    for i, url in enumerate(urls):
        rec = {"url": url, "marketplaces": {}, "error": ""}
        if not allowed:
            rec["error"] = "robots.txt disallow"
            out[url] = rec
            continue
        for attempt in range(3):        # 일시적 연결 실패가 잦아 재시도한다
            try:
                r = s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
                html = r.raw.read(MAX_BYTES, decode_content=True).decode(
                    r.encoding or "utf-8", errors="ignore")
                r.close()
                if r.status_code >= 400:
                    rec["error"] = f"HTTP {r.status_code}"
                else:
                    rec["error"] = ""
                    found = find_marketplaces(html, urlparse(r.url).netloc.lower())
                    rec["marketplaces"] = {k: sorted(v)[:3] for k, v in found.items()}
                    break
            except requests.RequestException as e:
                rec["error"] = type(e).__name__
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
            if rec["error"] not in RETRYABLE:
                break
            time.sleep(2 * (attempt + 1))
        out[url] = rec
        if i + 1 < len(urls):
            time.sleep(DELAY)

    with _lock:
        _progress["done"] += len(urls)
        found_here = sum(1 for v in out.values() if v["marketplaces"])
        print(f"[{_progress['done']}/{_progress['total']}] {host} "
              f"({len(urls)}개, 발견 {found_here})", flush=True)
    return out


def target_urls(hits):
    """1·2단계에서 쓰던 URL 을 도메인별로 모은다 (홈페이지 + 제품 상세)."""
    by_host = collections.defaultdict(set)
    for h in hits:
        links = h.get("_links") or {}
        for u in (links.get("brand_website"),
                  (links.get("exhibitor") or {}).get("website")):
            if u:
                host = E.norm_host(u)
                if host:
                    by_host[host].add(E.root_url(u))
        if h.get("url"):
            host = E.norm_host(h["url"])
            if host:
                by_host[host].add(h["url"])
    return {k: sorted(v) for k, v in by_host.items()}


def scan(by_host, cache):
    todo = {h: [u for u in us if u not in cache] for h, us in by_host.items()}
    todo = {h: us for h, us in todo.items() if us}
    _progress["total"] = sum(len(v) for v in todo.values())
    _progress["done"] = 0
    print(f"URL {sum(len(v) for v in by_host.values())}개 중 {_progress['total']}개 확인 "
          f"(도메인 {len(todo)}개)\n", flush=True)
    if not todo:
        return cache

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res in ex.map(lambda kv: check_urls(*kv), todo.items()):
            cache.update(res)
            if len(cache) % 300 < len(res):
                json.dump(cache, open(CACHE, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cache


def merge(hits, cache):
    """제품별로 브랜드/출품사/제품 페이지에서 찾은 마켓플레이스를 합친다."""
    for h in hits:
        links = h.get("_links") or {}
        found, sources, samples = {}, [], []
        candidates = [
            ("brand", E.root_url(links["brand_website"]) if links.get("brand_website") else None),
            ("exhibitor", E.root_url((links.get("exhibitor") or {}).get("website"))
                if (links.get("exhibitor") or {}).get("website") else None),
            ("product", h.get("url")),
        ]
        for label, url in candidates:
            rec = cache.get(url or "")
            if not rec or not rec["marketplaces"]:
                continue
            sources.append(label)
            for name, urls in rec["marketplaces"].items():
                found.setdefault(name, urls[0] if urls else "")
        for name, u in found.items():
            samples.append(u)
        h["_marketplace"] = {
            "marketplaces": sorted(found),
            "count": len(found),
            "sources": sources,
            "sample_urls": samples[:3],
        }
    return hits


MP_COLS = ["marketplace_count", "marketplaces", "marketplace_source", "marketplace_url"]


def write_outputs(hits):
    json.dump(hits, open(PRODUCTS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n저장: {PRODUCTS_JSON}", flush=True)

    brand_site, exh_urls = {}, {}
    for h in hits:
        b, e = h.get("brand") or {}, h.get("exhibitor") or {}
        links = h.get("_links") or {}
        if isinstance(b, dict) and b.get("id"):
            brand_site[b["id"]] = links.get("brand_website", "")
        if isinstance(e, dict) and e.get("id"):
            exh_urls[e["id"]] = links.get("exhibitor") or {}

    rows = []
    for h in hits:
        row = S.flatten(h, brand_site, exh_urls)
        ec = h.get("_ecommerce") or {}
        row["sold_online"] = ec.get("sold_online", "unknown")
        row["ecommerce_grade"] = ec.get("grade", "")
        row["ecommerce_platform"] = ec.get("platform", "")
        row["ecommerce_evidence"] = ec.get("evidence", "")
        row["ecommerce_source"] = ec.get("source", "")
        row["ecommerce_checked_url"] = ec.get("checked_url", "")
        pp = h.get("_product_page") or {}
        row["product_page_purchasable"] = pp.get("purchasable", "unknown")
        row["product_page_price"] = pp.get("price", False)
        row["product_page_cart"] = pp.get("cart", False)
        row["product_page_soldout"] = pp.get("soldout", False)
        row["product_page_status"] = pp.get("status", "")
        mp = h["_marketplace"]
        row["marketplace_count"] = mp["count"]
        row["marketplaces"] = " | ".join(mp["marketplaces"])
        row["marketplace_source"] = " | ".join(mp["sources"])
        row["marketplace_url"] = mp["sample_urls"][0] if mp["sample_urls"] else ""
        rows.append(row)

    E.write_csv(rows, PRODUCTS_CSV)
    return rows


def summarize(cache, rows):
    n = len(rows)
    withmp = [r for r in rows if r["marketplace_count"]]
    print(f"\n=== 제품 기준 ===")
    print(f"  마켓플레이스 링크 있음 {len(withmp)} / {n} ({len(withmp) / n:.1%})")

    c = collections.Counter()
    for r in withmp:
        c.update(r["marketplaces"].split(" | "))
    print("  마켓플레이스별 제품 수:")
    for name, v in c.most_common(12):
        print(f"    {name:<18}{v:>6}")

    pages = [v for v in cache.values() if v["marketplaces"]]
    print(f"\n=== 페이지 기준 ===")
    print(f"  링크 발견 페이지 {len(pages)} / {len(cache)}")

    print("\n=== 자사몰 판정과의 관계 ===")
    cross = collections.Counter((r["sold_online"], "mp" if r["marketplace_count"] else "-")
                                for r in rows)
    for (a, b), v in cross.most_common(8):
        print(f"  sold_online={a:<8} marketplace={b:<3} {v:>6}")
    only_mp = [r for r in rows if r["marketplace_count"] and r["sold_online"] in ("no", "unknown")]
    print(f"\n  자사몰은 없지만 마켓플레이스 링크는 있음: {len(only_mp)}건 "
          f"— 1·2단계만으로는 놓쳤을 제품이다")


def main():
    hits = json.load(open(PRODUCTS_JSON, encoding="utf-8"))
    if not hits or "_links" not in hits[0]:
        sys.exit("sial_fetch_products.py 를 먼저 실행할 것.")

    by_host = target_urls(hits)
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except FileNotFoundError:
        cache = {}

    # 일시적 실패로 남은 항목은 캐시에서 빼서 이번 실행에 다시 시도한다
    stale = [u for u, v in cache.items() if v.get("error") in RETRYABLE]
    for u in stale:
        del cache[u]
    if stale:
        print(f"일시적 실패 {len(stale)}건 재시도 대상으로 되돌림")

    started = time.time()
    cache = scan(by_host, cache)
    print(f"\n스캔 {time.time() - started:.0f}초", flush=True)

    merge(hits, cache)
    rows = write_outputs(hits)
    summarize(cache, rows)


if __name__ == "__main__":
    main()
