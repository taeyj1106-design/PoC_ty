"""SIAL 제품에 연결된 도메인이 온라인 판매(이커머스)를 하는지 판정한다 — 1단계(도메인 단위).

제품 10,248건이 가리키는 고유 도메인은 1,289개뿐이라, 도메인당 홈페이지 1회만
받아서 이커머스 플랫폼 지문 / 장바구니 / 가격(Offer) 흔적을 찾는다.

  A = 플랫폼 지문 확인 (Shopify, WooCommerce, ...) 또는 장바구니+가격 둘 다  -> sold_online=yes
  B = 장바구니 또는 가격 흔적 하나만                                          -> sold_online=likely
  C = 흔적 없음 (B2B/브로슈어형 사이트)                                        -> sold_online=no
      요청 실패 / robots.txt 금지 / 링크 없음                                  -> sold_online=unknown

판정 대상은 제품이 아니라 "그 제품에 붙은 사이트"다. 브랜드 홈페이지가 없으면
출품사 홈페이지로 대체되므로, 유통사가 출품한 제품은 어긋날 수 있다.
제품 자체의 구매 가능 여부는 2단계(제품 상세 URL 확인)에서 본다.

결과는 sial_ecommerce_domains.json 에 캐시되어 재실행 시 이미 본 도메인은 건너뛴다.
sial_products.json 에는 `_ecommerce` 키로 병합하고 CSV 도 다시 쓴다.

사용법:  python sial_check_ecommerce.py
"""

import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import truststore

truststore.inject_into_ssl()

import requests

import sial_fetch_products as S

PRODUCTS_JSON = "sial_products.json"
PRODUCTS_CSV = "sial_products.csv"
CACHE = "sial_ecommerce_domains.json"

WORKERS = 5             # 동시 요청 수 (도메인당 1회뿐이라 낮게 유지)
TIMEOUT = 15
MAX_BYTES = 400_000     # 본문은 앞부분만 봐도 지문이 잡힌다
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 플랫폼 지문 — 발견되면 그 자체로 A 등급
PLATFORMS = {
    "shopify": ("cdn.shopify.com", "myshopify.com", "shopify.theme", "/cdn/shop/"),
    "woocommerce": ("woocommerce", "wp-content/plugins/woocommerce", "wc-ajax"),
    "magento": ("magento", "mage/cookies", "/static/version"),
    "prestashop": ("prestashop",),
    "bigcommerce": ("bigcommerce", "stencil-utils"),
    "wix_stores": ("wixstores", "wix-stores", "ecom-platform"),
    "ecwid": ("ecwid",),
    "squarespace_commerce": ("squarespace-commerce", "sqs-add-to-cart"),
    "shopware": ("shopware",),
    "opencart": ("index.php?route=checkout", "opencart"),
    "cafe24": ("cafe24", "echosting"),
    "demandware": ("demandware", "sfcc"),
    "shoplazza": ("shoplazza",),
    "jimdo_shop": ("jimdo-shop", "jimdostore"),
}

# 장바구니 흔적 (다국어)
CART = re.compile(
    r"""(?ix)
    (?:href|action)\s*=\s*["'][^"']*/(?:cart|panier|warenkorb|carrito|carrello|
        winkelwagen|koszyk|kosik|sepet|corbeille|checkout|basket)\b
    | add[-_ ]?to[-_ ]?cart | addtocart | ajouter\s+au\s+panier | in\s+den\s+warenkorb
    | a[nñ]adir\s+al\s+carrito | aggiungi\s+al\s+carrello | 장바구니 | 구매하기
    """
)

# 가격/판매 흔적
OFFER = re.compile(
    r"""(?ix)
    "@type"\s*:\s*"Offer" | itemprop\s*=\s*["']price | og:price:amount
    | "priceCurrency" | class\s*=\s*["'][^"']*\bprice\b
    """
)

_print_lock = threading.Lock()


def norm_host(u):
    try:
        h = (urlparse(u).netloc or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def root_url(u):
    p = urlparse(u)
    scheme = p.scheme if p.scheme in ("http", "https") else "https"
    return urlunparse((scheme, p.netloc, "/", "", "", ""))


def collect_domains(hits):
    """도메인 -> 대표 URL. 브랜드 > 출품사 > 제품 URL 순으로 대표를 정한다."""
    domains = {}
    for h in hits:
        links = h.get("_links") or {}
        for u in (links.get("brand_website"),
                  (links.get("exhibitor") or {}).get("website"),
                  h.get("url")):
            if not u:
                continue
            d = norm_host(u)
            if d and d not in domains:
                domains[d] = root_url(u)
    return domains


def robots_allows(session, base):
    """robots.txt 가 루트 수집을 막으면 건너뛴다. 못 받으면 허용으로 본다."""
    rp = RobotFileParser()
    try:
        r = session.get(base + "robots.txt", timeout=TIMEOUT)
        if r.status_code >= 400:
            return True
        rp.parse(r.text.splitlines())
    except requests.RequestException:
        return True
    try:
        return rp.can_fetch(UA, base)
    except Exception:
        return True


def grade(html):
    platform = next((name for name, sigs in PLATFORMS.items()
                     if any(s in html for s in sigs)), "")
    cart = bool(CART.search(html))
    offer = bool(OFFER.search(html))

    if platform or (cart and offer):
        g, sold = "A", "yes"
    elif cart or offer:
        g, sold = "B", "likely"
    else:
        g, sold = "C", "no"

    evidence = [x for x in (f"platform:{platform}" if platform else "",
                            "cart" if cart else "",
                            "offer" if offer else "") if x]
    return {"grade": g, "sold_online": sold, "platform": platform,
            "evidence": "+".join(evidence)}


def check_domain(domain, url):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*",
                      "Accept-Language": "en;q=0.9"})
    base = root_url(url)
    out = {"domain": domain, "checked_url": base, "grade": "", "sold_online": "unknown",
           "platform": "", "evidence": "", "error": ""}

    if not robots_allows(s, base):
        out["error"] = "robots.txt disallow"
        return out
    try:
        r = s.get(base, timeout=TIMEOUT, allow_redirects=True, stream=True)
        html = r.raw.read(MAX_BYTES, decode_content=True).decode(
            r.encoding or "utf-8", errors="ignore")
        r.close()
        out["checked_url"] = r.url
        if r.status_code >= 400:
            out["error"] = f"HTTP {r.status_code}"
            return out
        out.update(grade(html))
    except requests.RequestException as e:
        out["error"] = type(e).__name__
    except Exception as e:                      # 인코딩/파싱 등 예상 못한 실패
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def scan(domains, cache):
    todo = [(d, u) for d, u in domains.items() if d not in cache]
    print(f"도메인 {len(domains)}개 중 {len(cache)}개는 캐시됨 → {len(todo)}개 확인")
    if not todo:
        return cache

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res in ex.map(lambda a: check_domain(*a), todo):
            cache[res["domain"]] = res
            done += 1
            with _print_lock:
                mark = res["grade"] or ("!" if res["error"] else "?")
                print(f"[{done}/{len(todo)}] {mark} {res['domain']} "
                      f"{res['platform'] or res['evidence'] or res['error']}")
            if done % 50 == 0:
                json.dump(cache, open(CACHE, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cache


def merge(hits, cache):
    """제품마다 대표 링크의 판정을 붙인다. 링크 우선순위는 collect_domains 와 동일."""
    for h in hits:
        links = h.get("_links") or {}
        rec, src = None, ""
        for label, u in (("brand", links.get("brand_website")),
                         ("exhibitor", (links.get("exhibitor") or {}).get("website")),
                         ("product", h.get("url"))):
            if u and norm_host(u) in cache:
                rec, src = cache[norm_host(u)], label
                break
        if rec:
            h["_ecommerce"] = {"sold_online": rec["sold_online"], "grade": rec["grade"],
                               "platform": rec["platform"], "evidence": rec["evidence"],
                               "source": src, "checked_url": rec["checked_url"],
                               "error": rec["error"]}
        else:
            h["_ecommerce"] = {"sold_online": "unknown", "grade": "", "platform": "",
                               "evidence": "", "source": "", "checked_url": "",
                               "error": "no link"}
    return hits


EC_COLS = ["sold_online", "ecommerce_grade", "ecommerce_platform",
           "ecommerce_evidence", "ecommerce_source", "ecommerce_checked_url"]


def write_outputs(hits):
    json.dump(hits, open(PRODUCTS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n저장: {PRODUCTS_JSON}")

    # CSV 는 sial_fetch_products.flatten 을 그대로 쓰고 이커머스 컬럼만 덧붙인다
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
        ec = h["_ecommerce"]
        row["sold_online"] = ec["sold_online"]
        row["ecommerce_grade"] = ec["grade"]
        row["ecommerce_platform"] = ec["platform"]
        row["ecommerce_evidence"] = ec["evidence"]
        row["ecommerce_source"] = ec["source"]
        row["ecommerce_checked_url"] = ec["checked_url"]
        rows.append(row)

    write_csv(rows, PRODUCTS_CSV)
    return rows


def write_csv(rows, path):
    """Excel 등이 파일을 잡고 있으면(PermissionError) 옆 이름으로 떨군다."""
    for target in (path, path.replace(".csv", f".{int(time.time())}.csv")):
        try:
            with open(target, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        except PermissionError:
            print(f"  ! {target} 가 잠겨 있다 (다른 프로그램에서 열림) — 다른 이름으로 저장한다")
            continue
        print(f"저장: {target} ({len(rows)}행 x {len(rows[0])}열)")
        return target
    raise PermissionError(path)


def summarize(cache, rows):
    import collections
    print("\n=== 도메인 기준 ===")
    c = collections.Counter(r["sold_online"] for r in cache.values())
    n = len(cache)
    for k in ("yes", "likely", "no", "unknown"):
        print(f"  {k:<8} {c[k]:>5} / {n} ({c[k] / n:.1%})")
    plat = collections.Counter(r["platform"] for r in cache.values() if r["platform"])
    print("  플랫폼:", dict(plat.most_common(8)))
    err = collections.Counter(r["error"] for r in cache.values() if r["error"])
    print("  실패:", dict(err.most_common(6)))

    print("\n=== 제품 기준 ===")
    c = collections.Counter(r["sold_online"] for r in rows)
    n = len(rows)
    for k in ("yes", "likely", "no", "unknown"):
        print(f"  {k:<8} {c[k]:>6} / {n} ({c[k] / n:.1%})")


def main():
    hits = json.load(open(PRODUCTS_JSON, encoding="utf-8"))
    if not hits or "_links" not in hits[0]:
        sys.exit(f"{PRODUCTS_JSON} 에 _links 가 없다. sial_fetch_products.py 를 먼저 실행할 것.")

    domains = collect_domains(hits)
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except FileNotFoundError:
        cache = {}

    started = time.time()
    cache = scan(domains, cache)
    print(f"\n스캔 {time.time() - started:.0f}초")

    merge(hits, cache)
    rows = write_outputs(hits)
    summarize(cache, rows)


if __name__ == "__main__":
    main()
