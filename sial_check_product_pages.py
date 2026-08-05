"""2단계 — 제품 상세 URL 자체가 구매 가능한 페이지인지 확인한다.

1단계(sial_check_ecommerce.py)는 도메인이 이커머스인지만 본다. 그래서 Shopify 를
깔아뒀지만 실제로는 팔지 않는 B2B 사이트가 A 등급으로 잡히는 오탐이 있다.
여기서는 제품 URL 을 직접 받아서 그 페이지에 "가격"과 "장바구니 담기"가
같이 있는지 본다.

  purchasable=yes     가격 + 장바구니 둘 다
             likely   장바구니만 (가격은 로그인/견적형일 수 있음)
             price_only  가격만 (구매 버튼 없음 — 카탈로그형)
             no       둘 다 없음
             unknown  요청 실패 / 404 / 홈으로 리다이렉트(페이지 소멸)

제품 URL 이 있는 3,531건(고유 URL 2,505개)만 대상이다. 나머지는 판정 불가.
도메인당 순차 요청(사이 DELAY)으로 돌고, 도메인끼리만 병렬로 처리한다.

결과는 sial_product_pages.json 에 캐시된다. 사용법: python sial_check_product_pages.py
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
CACHE = "sial_product_pages.json"

DOMAIN_WORKERS = 5      # 동시에 처리할 도메인 수
DELAY = 0.4             # 같은 도메인 내 요청 간격
TIMEOUT = 15
MAX_BYTES = 500_000

CART = re.compile(
    r"""(?ix)
    add[-_ ]?to[-_ ]?(?:cart|basket|bag) | addtocart
    | ajouter\s+au\s+panier | in\s+den\s+warenkorb | aggiungi\s+al\s+carrello
    | a[nñ]adir\s+al\s+carrito | adicionar\s+ao\s+carrinho | 장바구니
    | name\s*=\s*["']add["'] | data-add-to-cart | /cart/add
    """
)

PRICE = re.compile(
    r"""(?ix)
    "price"\s*:\s*"?\d
    | itemprop\s*=\s*["']price["']
    | og:price:amount
    | (?:€|£|\$|₩|¥)\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?
    | \d+[.,]\d{2}\s?(?:€|£|\$|EUR|USD|GBP)
    """
)

# 재고 없음 표기 — 있으면 "판매하는 페이지"라는 근거는 되지만 구매는 불가
SOLDOUT = re.compile(
    r"(?i)sold\s?out|out of stock|rupture de stock|nicht auf lager|agotado|esaurito|품절"
)

_lock = threading.Lock()
_progress = {"done": 0, "total": 0}


def fetch(session, url):
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    html = r.raw.read(MAX_BYTES, decode_content=True).decode(
        r.encoding or "utf-8", errors="ignore")
    r.close()
    return r, html


def judge(url, r, html):
    out = {"url": url, "final_url": r.url, "status": r.status_code,
           "purchasable": "unknown", "cart": False, "price": False,
           "soldout": False, "error": ""}
    if r.status_code >= 400:
        out["error"] = f"HTTP {r.status_code}"
        return out
    # 제품 페이지가 사라지면 대개 홈으로 보낸다 — 판정 불가로 둔다
    if urlparse(r.url).path.strip("/") == "" and urlparse(url).path.strip("/") != "":
        out["error"] = "redirected to home"
        return out

    out["cart"] = bool(CART.search(html))
    out["price"] = bool(PRICE.search(html))
    out["soldout"] = bool(SOLDOUT.search(html))
    if out["cart"] and out["price"]:
        out["purchasable"] = "yes"
    elif out["cart"]:
        out["purchasable"] = "likely"
    elif out["price"]:
        out["purchasable"] = "price_only"
    else:
        out["purchasable"] = "no"
    return out


def check_domain_urls(domain, urls):
    """한 도메인의 URL 들을 순차로 확인한다 (요청 사이 DELAY)."""
    s = requests.Session()
    s.headers.update({"User-Agent": E.UA, "Accept": "text/html,*/*",
                      "Accept-Language": "en;q=0.9"})
    results = {}

    allowed = E.robots_allows(s, f"https://{domain}/")
    for i, url in enumerate(urls):
        if not allowed:
            results[url] = {"url": url, "final_url": "", "status": 0,
                            "purchasable": "unknown", "cart": False, "price": False,
                            "soldout": False, "error": "robots.txt disallow"}
            continue
        try:
            r, html = fetch(s, url)
            results[url] = judge(url, r, html)
        except requests.RequestException as e:
            results[url] = {"url": url, "final_url": "", "status": 0,
                            "purchasable": "unknown", "cart": False, "price": False,
                            "soldout": False, "error": type(e).__name__}
        except Exception as e:
            results[url] = {"url": url, "final_url": "", "status": 0,
                            "purchasable": "unknown", "cart": False, "price": False,
                            "soldout": False, "error": f"{type(e).__name__}: {e}"}
        if i + 1 < len(urls):
            time.sleep(DELAY)

    with _lock:
        _progress["done"] += len(urls)
        print(f"[{_progress['done']}/{_progress['total']}] {domain} ({len(urls)}개) "
              f"{collections.Counter(v['purchasable'] for v in results.values()).most_common()}",
              flush=True)
    return results


def scan(urls_by_domain, cache):
    todo = {d: [u for u in us if u not in cache] for d, us in urls_by_domain.items()}
    todo = {d: us for d, us in todo.items() if us}
    _progress["total"] = sum(len(v) for v in todo.values())
    _progress["done"] = 0
    print(f"고유 URL {sum(len(v) for v in urls_by_domain.values())}개 중 "
          f"{_progress['total']}개 확인 (도메인 {len(todo)}개)\n", flush=True)
    if not todo:
        return cache

    with ThreadPoolExecutor(max_workers=DOMAIN_WORKERS) as ex:
        for res in ex.map(lambda kv: check_domain_urls(*kv), todo.items()):
            cache.update(res)
            if len(cache) % 200 < len(res):
                json.dump(cache, open(CACHE, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cache


PP_COLS = ["product_page_purchasable", "product_page_price", "product_page_cart",
           "product_page_soldout", "product_page_status"]


def merge(hits, cache):
    for h in hits:
        rec = cache.get(h.get("url") or "")
        h["_product_page"] = {
            "purchasable": rec["purchasable"] if rec else "unknown",
            "price": bool(rec and rec["price"]),
            "cart": bool(rec and rec["cart"]),
            "soldout": bool(rec and rec["soldout"]),
            "status": (rec["error"] or str(rec["status"])) if rec else "no product url",
        }
    return hits


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
        pp = h["_product_page"]
        row["product_page_purchasable"] = pp["purchasable"]
        row["product_page_price"] = pp["price"]
        row["product_page_cart"] = pp["cart"]
        row["product_page_soldout"] = pp["soldout"]
        row["product_page_status"] = pp["status"]
        rows.append(row)

    E.write_csv(rows, PRODUCTS_CSV)
    return rows


def summarize(rows):
    n = len(rows)
    print("\n=== 제품 상세 URL 판정 (제품 기준) ===")
    c = collections.Counter(r["product_page_purchasable"] for r in rows)
    for k in ("yes", "likely", "price_only", "no", "unknown"):
        print(f"  {k:<11} {c[k]:>6} / {n} ({c[k] / n:.1%})")

    print("\n=== 1단계(도메인) x 2단계(제품 페이지) 교차 ===")
    cross = collections.Counter((r["sold_online"], r["product_page_purchasable"]) for r in rows)
    print(f"  {'도메인':<9} {'제품페이지':<12} {'건수':>7}")
    for (a, b), v in cross.most_common(12):
        print(f"  {a:<9} {b:<12} {v:>7}")

    checked = [r for r in rows if r["product_page_purchasable"] != "unknown"]
    fp = [r for r in checked if r["sold_online"] == "yes"
          and r["product_page_purchasable"] in ("no", "price_only")]
    if checked:
        print(f"\n  1단계 yes 인데 제품 페이지엔 구매 수단 없음: {len(fp)} / {len(checked)} "
              f"({len(fp) / len(checked):.1%}) — 1단계 오탐 추정치")


def main():
    hits = json.load(open(PRODUCTS_JSON, encoding="utf-8"))
    if not hits or "_ecommerce" not in hits[0]:
        sys.exit("1단계(sial_check_ecommerce.py)를 먼저 실행할 것.")

    by_domain = collections.defaultdict(list)
    for u in sorted({h["url"] for h in hits if h.get("url")}):
        d = urlparse(u).netloc.lower()
        by_domain[d[4:] if d.startswith("www.") else d].append(u)

    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except FileNotFoundError:
        cache = {}

    started = time.time()
    cache = scan(by_domain, cache)
    print(f"\n스캔 {time.time() - started:.0f}초", flush=True)

    merge(hits, cache)
    rows = write_outputs(hits)
    summarize(rows)


if __name__ == "__main__":
    main()
