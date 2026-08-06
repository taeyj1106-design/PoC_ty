"""ISM Cologne 출품사 카탈로그 수집 → JSON / CSV.

주의 (수집 전에 반드시 읽을 것)
--------------------------------
* ism-cologne.com 의 robots.txt 는 ClaudeBot / anthropic-ai / Claude-Web / GPTBot 등
  AI 크롤러를 `Disallow: /` 로 전면 차단하고 있다. 사용자가 명시적으로 지시한
  경우에만 사용한다.
* ISM 은 Anuga 와 같은 Koelnmesse ASDB 시스템이라 '개별 제품' 레코드가 없다.
  제품 관련 정보는 출품사에 붙은 제품군(분류 트리) / 브랜드 / 트렌드 수준까지다.
* 상세 페이지는 요청률에 민감하다. 초당 1건을 넘기면 원본이 실제 페이지 대신
  홈페이지를 돌려주기 시작한다 (Anuga 에서 실측: 4워커 → 성공률 2%).
  기본값(단일 세션 · 1.0s 간격)을 함부로 올리지 말 것.

Anuga 수집기와 다른 점
  출품사 목록을 blaettern 페이징 대신 asdb-sitemap.xml 에서 통째로 받는다.
  세션 관리·오프셋 갭 문제가 없고 한 번의 요청으로 전체 목록이 나온다.

사용법:  python ism_fetch_exhibitors.py [--resume] [--limit N] [--batch N]
"""

import argparse
import csv
import html
import json
import os
import random
import re
import time

import truststore

truststore.inject_into_ssl()  # 사내망 SSL 인터셉트 대응

import requests

# Anuga 수집기의 상세 파서를 그대로 재사용한다 (동일한 ASDB 마크업)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    'anuga', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'anuga_fetch_exhibitors.py'))
_anuga = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_anuga)

parse_detail = _anuga.parse_detail
text_of = _anuga.text_of

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
SITE = 'https://www.ism-cologne.com'
SITEMAP = SITE + '/asdb-sitemap.xml'

LIST_JSON = 'ism_exhibitors_list.json'
OUT_JSON = 'ism_exhibitors.json'
OUT_CSV = 'ism_exhibitors.csv'
DELAY = 1.0


def fetch_list(session):
    """asdb-sitemap.xml 에서 출품사 URL 전체를 받는다."""
    r = session.get(SITEMAP, timeout=120)
    r.raise_for_status()
    locs = re.findall(r'<loc>(.*?)</loc>', r.text)
    paths = []
    seen = set()
    for u in locs:
        m = re.match(r'https?://[^/]+(/exhibitor/[^/\s]+)/?$', u.strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            paths.append({'path': m.group(1).rstrip('/') + '/'})
    print(f'sitemap 출품사 {len(paths)}건 (전체 URL {len(locs)})')
    return paths


def fetch_one(session, path, idx):
    """상세 1건. 원본이 간헐적으로 출품사 목록 페이지를 돌려주므로 캐시버스터를 바꿔 재시도.

    재시도 간격을 넉넉히 둔다. 0.4s 로 몰아치면 그 자체가 초당 1건을 넘겨
    폴백을 다시 유발하고, 한번 빠지면 계속 실패하는 악순환이 된다
    (실측: 이 상태로 513건을 돌리면 성공률 0%).
    """
    for attempt in range(4):
        url = f'{SITE}{path}?v={idx}-{attempt}-{random.randint(1000, 999999)}'
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 200 and 'location-info' in r.text:
                return parse_ism(r.text)
            if r.status_code == 404:
                return {}
        except requests.RequestException:
            pass
        if attempt < 3:
            time.sleep(2.0 * (attempt + 1))     # 2s, 4s, 6s
    return {}


NAME_RE = re.compile(r'<div class="headline-title">.*?<strong><span>(.*?)</span>', re.S)
HALL_RE = re.compile(r'halle=([^&"]*)&standnr=([^&"#]*)')


def parse_ism(t):
    """ASDB 공통 필드 + ISM 에서 따로 뽑아야 하는 회사명/홀/부스.

    sitemap 에는 URL 밖에 없어서 Anuga(목록에서 얻음)와 달리 상세에서 뽑는다.
    """
    d = parse_detail(t)
    m = NAME_RE.search(t)
    d['name'] = text_of(m.group(1)) if m else ''
    h = HALL_RE.search(t)
    d['hall'] = html.unescape(h.group(1)) if h else ''
    d['stand'] = html.unescape(h.group(2)) if h else ''
    return d


def fetch_details(rows, delay=DELAY, batch=0):
    s = requests.Session()
    s.headers.update({'User-Agent': UA})
    todo = [(i, r) for i, r in enumerate(rows, 1) if not r.get('_fetched')]
    if batch:
        todo = todo[:batch]
    print(f'상세 수집 대상 {len(todo)}건 (단일 세션, 간격 {delay}s)', flush=True)

    done = ok = 0
    for i, r in todo:
        d = fetch_one(s, r['path'], i)
        r.update(d)
        r['detail_url'] = SITE + r['path']
        r['_fetched'] = bool(d)
        done += 1
        ok += 1 if d else 0
        # 죽은 경로는 재시도로 건당 20초가 걸린다. 진행이 멈춘 건지 느린 건지
        # 구분되도록 로그는 10건마다 찍고, 저장만 25건마다 한다.
        if done % 10 == 0 or done == len(todo):
            print(f'  {done}/{len(todo)}  성공 {ok}', flush=True)
        if done % 25 == 0 or done == len(todo):
            tmp = OUT_JSON + '.tmp'
            json.dump(rows, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(tmp, OUT_JSON)
        time.sleep(delay)
    return rows


COLUMNS = ['name', 'country', 'address', 'website', 'hall', 'stand',
           'product_groups_top', 'product_groups', 'product_group_count',
           'brands', 'trend_subjects', 'distribution_channels',
           'target_markets', 'profile', 'detail_url']


def write_outputs(rows):
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'저장: {OUT_JSON}')
    # 상세 페이지가 없는 항목은 CSV 에서 뺀다 (전 컬럼이 빈 행이 되므로).
    # JSON 에는 남겨야 --resume 이 미수집분을 다시 시도할 수 있다.
    got = [r for r in rows if r.get('_fetched')]
    with open(OUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in got:
            row = {c: r.get(c, '') for c in COLUMNS}
            row['country'] = row['country'] or r.get('country_detail', '')
            w.writerow(row)
    print(f'저장: {OUT_CSV} ({len(got)}행 x {len(COLUMNS)}열, '
          f'상세 없는 {len(rows) - len(got)}건 제외)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--batch', type=int, default=0)
    ap.add_argument('--delay', type=float, default=DELAY)
    args = ap.parse_args()

    s = requests.Session()
    s.headers.update({'User-Agent': UA})

    if args.resume and os.path.exists(OUT_JSON):
        rows = json.load(open(OUT_JSON, encoding='utf-8'))
        print(f'이어받기: {len(rows)}건 중 미수집 '
              f'{sum(1 for r in rows if not r.get("_fetched"))}건')
    elif os.path.exists(LIST_JSON):
        rows = json.load(open(LIST_JSON, encoding='utf-8'))
        print(f'목록 재사용: {len(rows)}건')
    else:
        rows = fetch_list(s)
        json.dump(rows, open(LIST_JSON, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

    if args.limit:
        rows = rows[:args.limit]
    rows = fetch_details(rows, delay=args.delay, batch=args.batch)
    write_outputs(rows)


if __name__ == '__main__':
    main()
