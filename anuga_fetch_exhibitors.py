"""Anuga 2025 출품사 카탈로그 수집 → JSON / CSV.

주의 (수집 전에 반드시 읽을 것)
--------------------------------
* anuga.com 의 robots.txt 는 ClaudeBot / anthropic-ai / Claude-Web / GPTBot 등
  AI 크롤러를 `Disallow: /` 로 전면 차단하고 있다. 이 스크립트는 사용자가
  명시적으로 수집을 지시한 경우에만 사용한다.
* Anuga 는 격년제이며 현재 DB 는 2025 년(종료) 회차 데이터다 (sJ=2025).
  다음 회차는 2027-10-09 ~ 13.
* Anuga 에는 SIAL 같은 '개별 제품' 레코드가 없다. 제품 관련 정보는
  출품사에 붙은 제품군(분류 트리) / 브랜드 / 제품섹터 수준까지다.

동작
----
1) list-of-exhibitors 의 blaettern 엔드포인트를 start=0,20,40... 으로 페이징해
   출품사 목록(이름/국가/홀/부스/상세경로)을 모은다.
   - blaettern 은 `?fw_goto=aussteller&suchort=produkte` 로 init 된 PHP 세션에서만
     동작하고, 일부 오프셋은 영구적으로 빈 fallback 페이지를 돌려준다(갭).
2) 각 출품사 상세 페이지에서 주소/웹사이트/제품군/브랜드/트렌드 등을 파싱한다.

사용법:  python anuga_fetch_exhibitors.py [--limit N] [--skip-detail]
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

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
SITE = 'https://www.anuga.com'
BASE = SITE + '/anuga-exhibitors/list-of-exhibitors/'
INIT = BASE + '?fw_goto=aussteller&suchort=produkte'

STEP = 20
MAX_START = 9000
RENEW_EVERY = 40
STOP_AFTER_EMPTY = 12
DELAY = 0.3

LIST_JSON = 'anuga_exhibitors_list.json'
OUT_JSON = 'anuga_exhibitors.json'
OUT_CSV = 'anuga_exhibitors.csv'


# ---------------------------------------------------------------- 세션/공통

def new_session():
    for _ in range(4):
        try:
            s = requests.Session()
            s.headers.update({'User-Agent': UA})
            s.get(INIT, timeout=60)
            time.sleep(0.6)
            return s
        except requests.RequestException:
            time.sleep(3)
    raise RuntimeError('세션 생성 실패')


def text_of(fragment):
    t = re.sub(r'<[^>]+>', ' ', fragment or '')
    return re.sub(r'\s+', ' ', html.unescape(t)).strip()


# ---------------------------------------------------------------- 1) 목록

ITEM = re.compile(r'<div class="item[^"]*"\s*>(.*?)<!-- //item-->', re.S)
A_RE = re.compile(r'href="(/exhibitor/[^"]+)"[^>]*title="([^"]*)"', re.S)
P_RE = re.compile(r'<p>\s*(.*?)\s*</p>', re.S)
STAND_RE = re.compile(r'halle=([^&"]*)&standnr=([^&"#]*)')


def parse_list_item(chunk):
    a = A_RE.search(chunk)
    if not a:
        return None
    col1 = chunk.split('<!-- //col-->')[0]
    texts = [text_of(p) for p in P_RE.findall(col1)]
    texts = [t for t in texts if t]
    st = STAND_RE.search(chunk)
    return {
        'path': a.group(1),
        'name': html.unescape(a.group(2)).strip(),
        'country': texts[0] if texts else '',
        'hall': html.unescape(st.group(1)) if st else '',
        'stand': html.unescape(st.group(2)) if st else '',
    }


def fetch_list():
    s, used = new_session(), 0
    out, seen, start, gaps, consec = [], set(), 0, [], 0

    while start <= MAX_START:
        if used >= RENEW_EVERY:
            s, used = new_session(), 0

        chunks = []
        for _ in range(2):
            try:
                t = s.get(f'{BASE}?route=aussteller/blaettern&fw_ajax=1&start={start}',
                          timeout=90).text
                chunks = ITEM.findall(t)
                used += 1
            except requests.RequestException:
                chunks = []
            if chunks:
                break
            s, used = new_session(), 0
            time.sleep(1)

        if not chunks:
            gaps.append(start)
            consec += 1
            print(f'start={start:>6}  (빈 페이지 · 갭 {len(gaps)})            ', end='\r')
            if consec >= STOP_AFTER_EMPTY:
                print(f'\n{STOP_AFTER_EMPTY}회 연속 빈 페이지 → 목록 끝')
                break
            start += STEP
            time.sleep(DELAY)
            continue
        consec = 0

        for c in chunks:
            rec = parse_list_item(c)
            if rec and rec['path'] not in seen:
                seen.add(rec['path'])
                out.append(rec)
        print(f'start={start:>6}  누적 {len(out):>5}          ', end='\r')
        start += STEP
        time.sleep(DELAY)

    print()
    if gaps:
        print(f'빈 페이지 오프셋 {len(gaps)}개 → 최대 {len(gaps) * STEP}건 누락 가능')
    return out, gaps


# ---------------------------------------------------------------- 2) 상세

SECTION = re.compile(
    r'<b>([^<]{2,40})</b>\s*</div>\s*'
    r'<div class="(?:asdb-cap-products-list|asdb54-cap-products-list)"[^>]*>(.*?)'
    r'</div><!-- asdb-cap-products-list -->', re.S)
LOC = re.compile(r'<div class="location-info">.*?<div>(.*?)</div>', re.S)
WEB = re.compile(r'ico_link linkellipsis.*?href=\'([^\']+)\'', re.S)
PROFILE = re.compile(r'<div class="xmlcontent unternehmensportrait">(.*?)</div>\s*</div>', re.S)


def flatten_tree(node, prefix=''):
    """제품군 JSON 트리를 'A > B > C' 리스트로 편다 (리프만)."""
    paths = []
    if isinstance(node, list):
        for n in node:
            paths += flatten_tree(n, prefix)
        return paths
    if not isinstance(node, dict):
        return paths
    text = (node.get('text') or '').strip()
    cur = f'{prefix} > {text}' if prefix else text
    children = node.get('children')
    if isinstance(children, dict) and children:
        for child in children.values():
            paths += flatten_tree(child, cur)
    elif isinstance(children, list) and children:
        for child in children:
            paths += flatten_tree(child, cur)
    else:
        if cur:
            paths.append(cur)
    return paths


def parse_product_groups(body):
    m = re.search(r'(\[\{.*\}\])', body, re.S)
    if not m:
        return [], []
    raw = html.unescape(m.group(1))
    try:
        tree = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    tops = [(n.get('text') or '').strip() for n in tree if isinstance(n, dict)]
    return [t for t in tops if t], flatten_tree(tree)


def parse_detail(t):
    d = {}
    loc = LOC.search(t)
    if loc:
        parts = [text_of(x) for x in re.split(r'<br\s*/?>', loc.group(1))]
        parts = [p for p in parts if p]
        d['address'] = ', '.join(parts[:-1]) if len(parts) > 1 else ''
        d['country_detail'] = parts[-1] if parts else ''
    web = WEB.search(t)
    d['website'] = html.unescape(web.group(1)) if web else ''

    sections = {}
    for m in SECTION.finditer(t):
        sections.setdefault(m.group(1).strip(), m.group(2))

    tops, leaves = parse_product_groups(sections.get('Product groups', ''))
    d['product_groups_top'] = ' | '.join(dict.fromkeys(tops))
    d['product_groups'] = ' | '.join(dict.fromkeys(leaves))
    d['product_group_count'] = len(set(leaves))

    for key, label in [('brands', 'Brand'), ('product_sector', 'Product sector'),
                       ('trend_subjects', 'Trend subjects'),
                       ('distribution_channels', 'Distribution channels'),
                       ('target_markets', 'Target and outlet markets')]:
        d[key] = text_of(sections.get(label, ''))

    prof = PROFILE.search(t)
    d['profile'] = text_of(prof.group(1))[:4000] if prof else ''
    return d


def fetch_one(s, path, idx, delay):
    """상세 1건. CloudFront 가 /exhibitor/... 에 홈페이지를 캐싱해두는 경우가 있어
    캐시 우회용 쿼리스트링을 붙인다 (없으면 133KB 짜리 홈페이지가 돌아온다)."""
    for attempt in range(4):
        # 재시도마다 다른 쿼리스트링을 써야 한다. 같은 URL 을 반복하면 CloudFront 가
        # 캐싱해둔 홈페이지 응답을 계속 돌려받아 재시도가 무의미해진다.
        url = f'{SITE}{path}?v={idx}-{attempt}-{random.randint(1000, 999999)}'
        try:
            resp = s.get(url, timeout=60)
            if resp.status_code == 200 and 'location-info' in resp.text:
                return parse_detail(resp.text)
        except requests.RequestException:
            pass
        # 실패는 서버가 간헐적으로 홈페이지를 돌려주는 현상이라 요청률과 무관하다.
        # 길게 백오프해도 개선되지 않으므로 짧게 쉬고 바로 다시 친다.
        time.sleep(0.4)
    return {}


def fetch_details(rows, delay=1.0, workers=3, batch=0):
    """스레드 풀로 상세 수집. 워커별 세션 + 각 요청 후 delay 로 요청률을 억제한다."""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    local = threading.local()
    lock = threading.Lock()
    state = {'done': 0, 'ok': 0}
    todo = [(i, r) for i, r in enumerate(rows, 1) if not r.get('_fetched')]
    if batch:
        todo = todo[:batch]
    print(f'상세 수집 대상 {len(todo)}건 (워커 {workers}, 요청간격 {delay}s)', flush=True)

    def work(item):
        i, r = item
        if not hasattr(local, 's'):
            local.s = requests.Session()
            local.s.headers.update({'User-Agent': UA})
        d = fetch_one(local.s, r['path'], i, delay)
        r.update(d)
        r['detail_url'] = SITE + r['path']
        r['_fetched'] = bool(d)
        with lock:
            state['done'] += 1
            state['ok'] += 1 if d else 0
            if state['done'] % 25 == 0 or state['done'] == len(todo):
                print(f"상세 {state['done']}/{len(todo)}  성공 {state['ok']}", flush=True)
            if state['done'] % 100 == 0:
                # 중단되어도 --resume 으로 이어받을 수 있게 자주 저장한다
                tmp = OUT_JSON + '.tmp'
                json.dump(rows, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
                os.replace(tmp, OUT_JSON)
        time.sleep(delay)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    print()
    return rows


# ---------------------------------------------------------------- 출력

COLUMNS = ['name', 'country', 'hall', 'stand', 'address', 'website',
           'product_sector', 'product_groups_top', 'product_groups',
           'product_group_count', 'brands', 'trend_subjects',
           'distribution_channels', 'target_markets', 'profile', 'detail_url']


def write_outputs(rows):
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'저장: {OUT_JSON}')
    with open(OUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, '') for c in COLUMNS})
    print(f'저장: {OUT_CSV} ({len(rows)}행 x {len(COLUMNS)}열)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='상세 수집 건수 제한(테스트용)')
    ap.add_argument('--skip-detail', action='store_true', help='목록만 수집')
    ap.add_argument('--reuse-list', action='store_true',
                    help=f'{LIST_JSON} 가 있으면 재사용')
    ap.add_argument('--resume', action='store_true',
                    help=f'{OUT_JSON} 에서 이어받아 미수집분만 채움')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--delay', type=float, default=1.0)
    ap.add_argument('--batch', type=int, default=0,
                    help='이번 실행에서 처리할 최대 건수 (0=전부)')
    args = ap.parse_args()

    if args.resume and os.path.exists(OUT_JSON):
        rows = json.load(open(OUT_JSON, encoding='utf-8'))
        print(f'이어받기: {len(rows)}건 중 미수집 '
              f'{sum(1 for r in rows if not r.get("_fetched"))}건')
    elif args.reuse_list and os.path.exists(LIST_JSON):
        rows = json.load(open(LIST_JSON, encoding='utf-8'))
        print(f'목록 재사용: {len(rows)}건')
    else:
        rows, _ = fetch_list()
        json.dump(rows, open(LIST_JSON, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print(f'목록 {len(rows)}건 → {LIST_JSON}')

    if args.limit:
        rows = rows[:args.limit]
    if not args.skip_detail:
        rows = fetch_details(rows, delay=args.delay, workers=args.workers,
                             batch=args.batch)
    write_outputs(rows)


if __name__ == '__main__':
    main()
