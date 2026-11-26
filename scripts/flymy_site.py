#!/usr/bin/env python3
"""Сводка и сайт одной командой, локально через API FlyMyAI (без песочницы).

  python3 flymy_site.py --run 2026-11-19                     # посчитать и опубликовать
  python3 flymy_site.py --run 2026-11-19 --dry DIR           # собрать сайт в папку DIR; хранилище только читается
  python3 flymy_site.py --run 2026-11-19 --dry DIR --data D  # то же на данных из папки D вместо хранилища (пример из examples/)
"""
import argparse, base64, json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP
from project import ROOT, P, BRAND, SITE, COMPANIES, OUR_DOMAINS, publications
import flymy_measure as F
import flymy_local

ap = argparse.ArgumentParser()
ap.add_argument('--run', required=True)
ap.add_argument('--dry')
ap.add_argument('--data')
a = ap.parse_args()
if a.data and not a.dry:
    raise SystemExit('--data только вместе с --dry')
SITE_DIR = ROOT / 'flymy_app' / 'site'
PUBS = publications()
BS = P.get('brand_score') or {}
OR = BS.get('openrouter') or {}
topics = P.get('topics') or {}
page = {                                     # всё, что странице нужно знать о проекте: бренд, подписи, какие виды своих источников есть
    'brand': BRAND, 'site': SITE, 'title': P['site']['title'], 'audience': P['site']['audience'],
    'geo': P['site'].get('geo') or '', 'tracked': len(COMPANIES),
    'topics': {t['key']: t['label'] for t in topics.get('list') or []} | ({topics['default']['key']: topics['default']['label']} if topics.get('default') else {}),
    'stages': P.get('stages') or {},
    'criteria': {k: v['label'] for k, v in (BS.get('criteria') or {}).items()},
    'models': (BS.get('models') or {}) | ({OR['label']: OR.get('title', OR['label'])} if OR.get('label') else {}),
    'scenarios': [[s['key'], s['label']] for s in BS.get('scenarios') or []],
    'kinds': {'sat': len(OUR_DOMAINS) > 1, 'pub': bool(PUBS['urls'] or PUBS['blog_hosts']), 'link': bool(PUBS['links'])},
}
cfg = {'run': a.run, 'our': BRAND, 'our_site': SITE, 'our_domains': OUR_DOMAINS, 'channels': F.CHANNELS, 'state': 'summary',
       'brands': {n: p for n, (_, p) in COMPANIES.items()}, 'brand_domains': {d: n for n, (d, _) in COMPANIES.items() if n != BRAND},
       'site_name': P['site']['title'], 'our_pubs': sorted(PUBS['urls']), 'our_links': sorted(PUBS['links']), 'blog_hosts': PUBS['blog_hosts'],
       'topics': topics.get('list') or [], 'topic_default': (topics.get('default') or {}).get('key', 'other'),
       'suffixes': (P.get('analysis') or {}).get('suffixes') or [], 'criteria': list((BS.get('criteria') or {}).keys()),
       'static': {k: base64.b64encode((SITE_DIR / k).read_bytes()).decode() for k in ('index.html', 'app.js', 'icons.js')}
                 | {'data/config.js': base64.b64encode(('window.AV_CFG=' + json.dumps(page, ensure_ascii=False) + ';').encode()).decode()}}
job = f'jobs/{a.run}_finalize.json'
body = json.dumps(cfg, ensure_ascii=False)
if a.dry:                                    # черновая сборка: задание не кладём в хранилище, отдаём в памяти
    flymy_local.run('finalize.py', job, a.dry, job_content=body, data=a.data)
else:
    F.storage_write(MCP(), job, body)
    flymy_local.run('finalize.py', job)
