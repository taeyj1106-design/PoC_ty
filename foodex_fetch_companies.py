"""FOODEX JAPAN 2025 출품사(회사) 카탈로그 수집 (Web Archive 경유) → JSON / CSV.

배경
----
제품 페이지(`foodex_fetch_products.py`)는 아카이브에 881건밖에 안 남아 전체의
약 7% 표본이다. 반면 회사 페이지(`company.php`)는 **1,599건**이 남아 있어
출품사 단위로는 훨씬 온전한 데이터를 만들 수 있다.
(제품 881건에 등장하는 회사는 462개뿐이었다.)

회사 페이지에서 얻는 것
  회사명 / 홈페이지 / 카테고리 / 부스 / 속성(국내·수출가능 등) / 소개문 /
  출품 품목 / 그 회사가 출품한 제품 목록(product.php 링크 + 이름)

한계
  * 2025 년 3 월(FOODEX JAPAN 2025) 스냅샷이다. 2026 년 데이터가 아니다.
  * 아카이브에 잡힌 회사만 존재한다. 원본은 전시 종료 후 401 로 잠겨 있다.
  * 제품 목록은 회사 페이지에 노출된 것만이라 그 회사의 전체 출품작이 아닐 수 있다.

사용법:  python foodex_fetch_companies.py [--limit N] [--resume]
"""

import argparse
import csv
import html
import json
import os
import re
import time

import truststore

truststore.inject_into_ssl()  # 사내망 SSL 인터셉트 대응

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
CDX = 'https://web.archive.org/cdx/search/cdx'
TARGET = 'jma-tradeshow.com/foodex/webguide_en/company.php*'
SITE = 'https://www.jma-tradeshow.com/foodex/webguide_en/'

INDEX_JSON = 'foodex_company_snapshots.json'
OUT_JSON = 'foodex_companies.json'
OUT_CSV = 'foodex_companies.csv'
DELAY = 1.0           # 아카이브에 부담 주지 않도록 (0.6 에서 실측상 실패 구간 발생)


def text_of(fragment):
    t = re.sub(r'<br\s*/?>', ' | ', fragment or '')
    t = re.sub(r'<[^>]+>', ' ', t)
    return re.sub(r'\s+', ' ', html.unescape(t)).strip()


# ------------------------------------------------------------ 1) 스냅샷 목록

def list_snapshots(session):
    """CDX 로 아카이브된 company.php 스냅샷 목록을 얻는다 (URL 당 최신 1개)."""
    # CDX 는 연속 호출하면 503 을 준다. 길게 쉬며 재시도한다.
    for attempt in range(4):
        r = session.get(CDX, params={
            'url': TARGET, 'output': 'json', 'limit': '80000',
            'fl': 'original,timestamp,statuscode', 'filter': 'statuscode:200',
        }, timeout=300)
        if r.status_code == 200:
            break
        print(f'  CDX {r.status_code} — {10 * (attempt + 1)}초 후 재시도')
        time.sleep(10 * (attempt + 1))
    r.raise_for_status()
    rows = r.json()[1:]
    best = {}
    for original, ts, _ in rows:
        m = re.search(r'no=(\d+)', original)
        if not m:
            continue
        no = int(m.group(1))
        # 같은 회사가 여러 번 아카이브됐으면 가장 나중 스냅샷을 쓴다
        if no not in best or ts > best[no][0]:
            best[no] = (ts, original)
    out = [{'no': no, 'timestamp': ts, 'original': orig}
           for no, (ts, orig) in sorted(best.items())]
    print(f'아카이브 스냅샷 {len(out)}건 (no 범위 {out[0]["no"]}~{out[-1]["no"]})')
    return out


# ------------------------------------------------------------ 2) 파싱

H1 = re.compile(r'<h1>(.*?)</h1>', re.S)
WEBSITE = re.compile(r'<a href=[\'"](https?://[^\'"]+)[\'"][^>]*class=[\'"]txt[\'"]', re.S)
DOTS = re.compile(r'<ul class="list_dots">(.*?)</ul>', re.S)
ATTRS = re.compile(r'<ul class="list_attribute">(.*?)</ul>', re.S)
LI = re.compile(r'<li[^>]*>(.*?)</li>', re.S)
INTRO = re.compile(r'<p class=[\'"]intro[^\'"]*[\'"]>(.*?)</p>', re.S)
TABLE_ROW = re.compile(r'<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>', re.S)
IMG = re.compile(r'<figure>\s*<img src=[\'"]([^\'"]+)[\'"]', re.S)
PRODUCT = re.compile(r'<a href="product\.php\?no=(\d+)">\s*<div>\s*<h3>(.*?)</h3>', re.S)


def parse_company(t, no):
    m = H1.search(t)
    name = text_of(m.group(1)) if m else ''
    if not name:
        return None

    category, booth = '', ''
    md = DOTS.search(t)
    if md:
        for d in [text_of(x) for x in LI.findall(md.group(1))]:
            if d.lower().startswith('booth'):
                booth = d.replace('Booth number', '').strip()
            elif d:
                category = d          # 부스가 아닌 항목이 카테고리(파빌리온)다

    ma = ATTRS.search(t)
    attrs = [text_of(x) for x in LI.findall(ma.group(1))] if ma else []

    tables = {text_of(k): text_of(v) for k, v in TABLE_ROW.findall(t)}
    products = PRODUCT.findall(t)
    w = WEBSITE.search(t)
    img = IMG.search(t)

    return {
        'no': no,
        'name': name,
        'website': w.group(1) if w else '',
        'category': category,
        'booth': booth,
        'attributes': ' | '.join(attrs),
        'intro': text_of(INTRO.search(t).group(1)) if INTRO.search(t) else '',
        'exhibited_products': tables.get('Exhibited products', ''),
        'product_count': len(products),
        'product_nos': ' | '.join(n for n, _ in products),
        'product_names': ' | '.join(text_of(x) for _, x in products),
        'image_url': (SITE + img.group(1).replace('../', '')) if img else '',
        'company_url': f'{SITE}company.php?no={no}',
    }


# ------------------------------------------------------------ 3) 수집

def fetch(session, snap):
    """id_ 플래그를 붙여 아카이브의 원본 HTML(툴바 주입 없이)을 받는다."""
    url = f'https://web.archive.org/web/{snap["timestamp"]}id_/{snap["original"]}'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=120)
            # 원본이 소프트 404 를 200 으로 응답해 아카이브에도 그대로 남았다.
            # 재시도해도 달라지지 않으니 바로 포기한다.
            if '404 Page Not Found' in r.text:
                return None
            if r.status_code == 200 and 'tit_headline' in r.text:
                d = parse_company(r.text, snap['no'])
                if d:
                    d['archived_at'] = snap['timestamp']
                return d
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return None


COLUMNS = ['no', 'name', 'website', 'category', 'booth', 'attributes', 'intro',
           'exhibited_products', 'product_count', 'product_nos', 'product_names',
           'image_url', 'company_url', 'archived_at']


def write_outputs(rows):
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'저장: {OUT_JSON}')
    ok = [r for r in rows if r.get('name')]
    with open(OUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in ok:
            w.writerow({c: r.get(c, '') for c in COLUMNS})
    print(f'저장: {OUT_CSV} ({len(ok)}행 x {len(COLUMNS)}열, '
          f'파싱 실패 {len(rows) - len(ok)}건 제외)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='수집 건수 제한(테스트용)')
    ap.add_argument('--resume', action='store_true',
                    help=f'{OUT_JSON} 에 있는 no 는 건너뛴다')
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({'User-Agent': UA})

    if os.path.exists(INDEX_JSON):
        snaps = json.load(open(INDEX_JSON, encoding='utf-8'))
        print(f'스냅샷 목록 재사용: {len(snaps)}건')
    else:
        snaps = list_snapshots(session)
        json.dump(snaps, open(INDEX_JSON, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

    rows, done = [], set()
    if args.resume and os.path.exists(OUT_JSON):
        rows = json.load(open(OUT_JSON, encoding='utf-8'))
        done = {r['no'] for r in rows}
        print(f'이어받기: 이미 {len(done)}건')

    todo = [s for s in snaps if s['no'] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f'수집 대상 {len(todo)}건', flush=True)

    ok = 0
    for i, snap in enumerate(todo, 1):
        d = fetch(session, snap)
        if d:
            rows.append(d)
            ok += 1
        if i % 25 == 0 or i == len(todo):
            print(f'  {i}/{len(todo)}  성공 {ok}', flush=True)
        if i % 200 == 0:
            tmp = OUT_JSON + '.tmp'
            json.dump(rows, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(tmp, OUT_JSON)
        time.sleep(DELAY)

    rows.sort(key=lambda r: r['no'])
    print(f'\n수집 {ok}/{len(todo)}')
    write_outputs(rows)


if __name__ == '__main__':
    main()
