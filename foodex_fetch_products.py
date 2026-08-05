"""FOODEX JAPAN 2025 제품 카탈로그 수집 (Web Archive 경유) → JSON / CSV.

배경
----
FOODEX 의 온라인 가이드북(www.jma-tradeshow.com/foodex/webguide_en/)은 전시 종료 후
HTTP 인증(401) 으로 잠겨 있어 원본 사이트에서는 수집할 수 없다.
전시 기간 중 공개돼 있던 시점의 스냅샷이 Internet Archive 에 남아 있어 그것을 쓴다.

한계 (반드시 인지할 것)
  * 2025 년 3 월(FOODEX JAPAN 2025) 스냅샷이다. 2026 년 데이터가 아니다.
  * 아카이브에 잡힌 제품만 존재한다. product.php?no= 는 21~13,286 범위인데
    200 응답으로 아카이브된 것은 881 건뿐 → 전체의 약 7%.
  * 즉 '전수'가 아니라 '표본'이다. 집계·점유율 분석에 쓰면 편향된다.

사용법:  python foodex_fetch_products.py [--limit N] [--resume]
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
TARGET = 'jma-tradeshow.com/foodex/webguide_en/product.php*'
SITE = 'https://www.jma-tradeshow.com/foodex/webguide_en/'

INDEX_JSON = 'foodex_snapshots.json'
OUT_JSON = 'foodex_products.json'
OUT_CSV = 'foodex_products.csv'
DELAY = 0.6           # 아카이브에 부담 주지 않도록


def text_of(fragment):
    t = re.sub(r'<br\s*/?>', ' | ', fragment or '')
    t = re.sub(r'<[^>]+>', ' ', t)
    return re.sub(r'\s+', ' ', html.unescape(t)).strip()


# ------------------------------------------------------------ 1) 스냅샷 목록

def list_snapshots(session):
    """CDX 로 아카이브된 product.php 스냅샷 목록을 얻는다 (URL 당 최신 1개)."""
    r = session.get(CDX, params={
        'url': TARGET, 'output': 'json', 'limit': '50000',
        'fl': 'original,timestamp,statuscode', 'filter': 'statuscode:200',
    }, timeout=300)
    r.raise_for_status()
    rows = r.json()[1:]
    best = {}
    for original, ts, _ in rows:
        m = re.search(r'no=(\d+)', original)
        if not m:
            continue
        no = int(m.group(1))
        # 같은 제품이 여러 번 아카이브됐으면 가장 나중 스냅샷을 쓴다
        if no not in best or ts > best[no][0]:
            best[no] = (ts, original)
    out = [{'no': no, 'timestamp': ts, 'original': orig}
           for no, (ts, orig) in sorted(best.items())]
    print(f'아카이브 스냅샷 {len(out)}건 (no 범위 {out[0]["no"]}~{out[-1]["no"]})')
    return out


# ------------------------------------------------------------ 2) 파싱

H1 = re.compile(r'<h1 class="tit_emphasis[^"]*">(.*?)</h1>', re.S)
LABELS = re.compile(r'<ul class="list_label[^"]*">(.*?)</ul>', re.S)
LI = re.compile(r'<li[^>]*>(.*?)</li>', re.S)
FEATURES = re.compile(r'<ul class="list_feature">(.*?)</ul>', re.S)
FEAT_ITEM = re.compile(r'<li>\s*<em>(.*?)</em>\s*<p>(.*?)</p>\s*</li>', re.S)
TABLE_ROW = re.compile(r'<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>', re.S)
COMPANY = re.compile(r'<div id="company_detail">(.*?)</div>', re.S)
COMPANY_NAME = re.compile(r'<h2><a href="company\.php\?no=(\d+)"[^>]*>(.*?)</a></h2>', re.S)
COMPANY_URL = re.compile(r'<h2>.*?</h2>\s*<a href="(https?://[^"]+)"', re.S)
ATTRS = re.compile(r'<ul class="list_attribute[^"]*">(.*?)</ul>', re.S)
DOTS = re.compile(r'<ul class="list_dots[^"]*">(.*?)</ul>', re.S)
IMG = re.compile(r'<figure>\s*<img src="([^"]+)"', re.S)


def parse_product(t, no):
    name = text_of(H1.search(t).group(1)) if H1.search(t) else ''
    if not name:
        return None

    m = LABELS.search(t)
    categories = [text_of(x) for x in LI.findall(m.group(1))] if m else []

    features = {}
    mf = FEATURES.search(t)
    if mf:
        for label, val in FEAT_ITEM.findall(mf.group(1)):
            features[text_of(label)] = text_of(val)

    # 표(인증/수입상 등)는 제품 영역과 출품사 영역 양쪽에 있다
    tables = {text_of(k): text_of(v) for k, v in TABLE_ROW.findall(t)}

    comp = COMPANY.search(t)
    cseg = comp.group(1) if comp else ''
    cm = COMPANY_NAME.search(t)
    company_no, company = (cm.group(1), text_of(cm.group(2))) if cm else ('', '')
    cu = COMPANY_URL.search(cseg) if cseg else None

    attrs, pavilion, booth, show = [], '', '', ''
    ma = ATTRS.search(cseg)
    if ma:
        attrs = [text_of(x) for x in LI.findall(ma.group(1))]
    md = DOTS.search(cseg)
    if md:
        dots = [text_of(x) for x in LI.findall(md.group(1))]
        for d in dots:
            if d.lower().startswith('booth'):
                booth = d.replace('Booth number', '').strip()
            elif 'FOODEX' in d:
                show = d
            elif d:
                pavilion = d

    img = IMG.search(t)
    return {
        'no': no,
        'name': name,
        'categories': ' | '.join(categories),
        'target_sector': features.get('Target Sector', ''),
        'specialities': features.get('Specialities,Sales point', ''),
        'certifications': tables.get('Requisite documents/ certification', ''),
        'about_importers': tables.get('About Importers', ''),
        'company': company,
        'company_no': company_no,
        'company_url': cu.group(1) if cu else '',
        'company_attributes': ' | '.join(attrs),
        'pavilion': pavilion,
        'booth': booth,
        'show': show,
        'exhibited_products': tables.get('Exhibited products', ''),
        'image_url': (SITE + img.group(1).replace('../', '')) if img else '',
        'product_url': f'{SITE}product.php?no={no}',
    }


# ------------------------------------------------------------ 3) 수집

def fetch(session, snap):
    """id_ 플래그를 붙여 아카이브의 원본 HTML(툴바 주입 없이)을 받는다."""
    url = f'https://web.archive.org/web/{snap["timestamp"]}id_/{snap["original"]}'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 200 and 'tit_emphasis' in r.text:
                return parse_product(r.text, snap['no'])
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return None


COLUMNS = ['no', 'name', 'categories', 'target_sector', 'specialities',
           'certifications', 'about_importers', 'company', 'company_url',
           'company_attributes', 'pavilion', 'booth', 'exhibited_products',
           'show', 'image_url', 'product_url']


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
    print(f'저장: {OUT_CSV} ({len(ok)}행 x {len(COLUMNS)}열)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--delay', type=float, default=DELAY)
    args = ap.parse_args()

    s = requests.Session()
    s.headers.update({'User-Agent': UA})

    if os.path.exists(INDEX_JSON):
        snaps = json.load(open(INDEX_JSON, encoding='utf-8'))
        print(f'스냅샷 목록 재사용: {len(snaps)}건')
    else:
        snaps = list_snapshots(s)
        json.dump(snaps, open(INDEX_JSON, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    done = {}
    if args.resume and os.path.exists(OUT_JSON):
        for r in json.load(open(OUT_JSON, encoding='utf-8')):
            done[r['no']] = r
        print(f'이어받기: 이미 {len(done)}건')

    todo = [x for x in snaps if x['no'] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f'수집 대상 {len(todo)}건 (간격 {args.delay}s)')

    for i, snap in enumerate(todo, 1):
        rec = fetch(s, snap)
        if rec:
            rec['archived_at'] = snap['timestamp']
            done[snap['no']] = rec
        if i % 25 == 0 or i == len(todo):
            print(f'  {i}/{len(todo)}  수집 {len(done)}', flush=True)
            tmp = OUT_JSON + '.tmp'
            json.dump(sorted(done.values(), key=lambda r: r['no']),
                      open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(tmp, OUT_JSON)
        time.sleep(args.delay)

    write_outputs(sorted(done.values(), key=lambda r: r['no']))


if __name__ == '__main__':
    main()
