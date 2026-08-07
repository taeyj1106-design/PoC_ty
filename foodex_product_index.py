"""FOODEX 제품 통합 목록 만들기 → foodex_product_index.csv

출품사 페이지(`foodex_companies.json`)에 링크된 제품 번호는 3,194개인데,
제품 상세(`foodex_products.json`)가 아카이브에 남은 건 881건뿐이다.
둘을 제품 번호로 합쳐서 "이름만 아는 제품"까지 포함한 전체 목록을 만든다.

  has_detail=yes  제품 상세까지 있음 (카테고리·타겟·인증 등 채워짐)
  has_detail=no   출품사 페이지에서 이름만 확인됨

주의: 회사 레코드의 product_nos / product_names 는 ' | ' 로 이어붙인 문자열이라,
제품명 자체에 줄바꿈이 있으면 개수가 어긋난다(실측 867개사 중 2건).
그런 회사는 아카이브에서 해당 페이지만 다시 받아 쌍을 정확히 복원한다.

사용법:  python foodex_product_index.py
"""

import csv
import json

import truststore

truststore.inject_into_ssl()

import requests

import foodex_fetch_companies as C

COMPANIES = 'foodex_companies.json'
PRODUCTS = 'foodex_products.json'
SNAPSHOTS = 'foodex_company_snapshots.json'
OUT_CSV = 'foodex_product_index.csv'

COLUMNS = ['no', 'name', 'has_detail', 'company_no', 'company', 'company_website',
           'company_category', 'booth', 'attributes', 'categories', 'target_sector',
           'specialities', 'certifications', 'exhibited_products', 'image_url',
           'product_url']


def pairs_of(company, snaps, session):
    """(제품번호, 제품명) 쌍. 개수가 어긋나면 원본 페이지를 다시 파싱한다."""
    nos = [n for n in (company.get('product_nos') or '').split(' | ') if n]
    names = [n for n in (company.get('product_names') or '').split(' | ') if n]
    if len(nos) == len(names):
        return list(zip(nos, names))

    snap = next((s for s in snaps if s['no'] == company['no']), None)
    if not snap:
        return [(n, '') for n in nos]
    url = f'https://web.archive.org/web/{snap["timestamp"]}id_/{snap["original"]}'
    try:
        r = session.get(url, timeout=120)
        found = C.PRODUCT.findall(r.text)
        print(f'  재파싱: {company["name"]} → {len(found)}건')
        return [(n, C.text_of(t)) for n, t in found]
    except requests.RequestException as e:
        print(f'  재파싱 실패: {company["name"]} ({type(e).__name__})')
        return [(n, '') for n in nos]


def main():
    companies = json.load(open(COMPANIES, encoding='utf-8'))
    products = json.load(open(PRODUCTS, encoding='utf-8'))
    snaps = json.load(open(SNAPSHOTS, encoding='utf-8'))
    detail = {str(p['no']): p for p in products}

    session = requests.Session()
    session.headers.update({'User-Agent': C.UA})

    rows, seen = [], {}
    for comp in companies:
        for no, name in pairs_of(comp, snaps, session):
            d = detail.get(no, {})
            row = {
                'no': no,
                'name': d.get('name') or name,
                'has_detail': 'yes' if d else 'no',
                'company_no': comp['no'],
                'company': comp['name'],
                'company_website': comp.get('website', ''),
                'company_category': comp.get('category', ''),
                'booth': comp.get('booth', ''),
                'attributes': comp.get('attributes', ''),
                'categories': d.get('categories', ''),
                'target_sector': d.get('target_sector', ''),
                'specialities': d.get('specialities', ''),
                'certifications': d.get('certifications', ''),
                'exhibited_products': d.get('exhibited_products', ''),
                'image_url': d.get('image_url', ''),
                'product_url': f'{C.SITE}product.php?no={no}',
            }
            # 같은 제품이 여러 회사에 걸리면 상세가 있는 쪽을 남긴다
            if no not in seen or (row['has_detail'] == 'yes' and seen[no]['has_detail'] == 'no'):
                seen[no] = row
    rows = sorted(seen.values(), key=lambda r: int(r['no']))

    # 출품사 페이지에 안 걸린 제품(상세만 있는 것)도 빠뜨리지 않는다
    orphan = 0
    for no, p in detail.items():
        if no in seen:
            continue
        orphan += 1
        rows.append({
            'no': no, 'name': p.get('name', ''), 'has_detail': 'yes',
            'company_no': p.get('company_no', ''), 'company': p.get('company', ''),
            'company_website': p.get('company_url', ''), 'company_category': '',
            'booth': p.get('booth', ''), 'attributes': p.get('company_attributes', ''),
            'categories': p.get('categories', ''), 'target_sector': p.get('target_sector', ''),
            'specialities': p.get('specialities', ''),
            'certifications': p.get('certifications', ''),
            'exhibited_products': p.get('exhibited_products', ''),
            'image_url': p.get('image_url', ''),
            'product_url': p.get('product_url', ''),
        })
    rows.sort(key=lambda r: int(r['no']))

    with open(OUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    withd = sum(1 for r in rows if r['has_detail'] == 'yes')
    print(f'\n저장: {OUT_CSV} ({len(rows)}행 x {len(COLUMNS)}열)')
    print(f'  상세 있음 {withd} / 이름만 {len(rows) - withd}')
    print(f'  출품사 페이지에 안 걸린 제품 {orphan}건 (상세에서만 확인)')


if __name__ == '__main__':
    main()
