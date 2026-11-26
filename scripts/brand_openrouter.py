#!/usr/bin/env python3
"""Оценка бренда (вопрос Б) через OpenRouter — для моделей, у которых агент FlyMyAI возвращает пустой результат (Claude).
Пишет файлы того же вида, что flymy_brand.py: out/brand/<run>/<model>_<mode>_<сценарий>_<n>.json, и кладёт их в хранилище.
Без ключа OpenRouter шаг пропускается.

  python3 brand_openrouter.py --run 2026-11-19 [--model anthropic/claude-sonnet-5.5 --label claude-sonnet-5-5]
"""
import argparse, json, sys, time, pathlib
import concurrent.futures as cf
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from openrouter import api_key, call
from flymy_brand import find_rows, find_extra, build_prompt, BS, SCEN, OUR
from project import ROOT, APP

OR = BS.get('openrouter') or {}
ap = argparse.ArgumentParser()
ap.add_argument('--run', required=True)
ap.add_argument('--model', default=OR.get('model', 'anthropic/claude-sonnet-5.5'))
ap.add_argument('--label', default=OR.get('label', 'claude-sonnet-5-5'))
ap.add_argument('--runs', type=int, default=BS.get('runs', 2))
ap.add_argument('--modes', default='web,no_web')
ap.add_argument('--scenarios', default=','.join(SCEN))
a = ap.parse_args()
key = api_key()
if not key:
    print('Ключа OpenRouter нет (config/openrouter.env) — оценку Claude пропускаю.')
    sys.exit(0)
out = ROOT / 'out' / 'brand' / a.run
out.mkdir(parents=True, exist_ok=True)


def one(job):
    mode, icp, n = job
    f = out / f'{a.label}_{mode}_{icp}_{n}.json'
    if f.exists() and json.loads(f.read_text()).get('rows'):
        return f.name, 'есть', 0, None
    prompt = build_prompt(icp, mode)
    t0 = time.time()
    r = call(key, a.model, prompt, mode == 'web', max_tokens=32000)
    rows = find_rows(r['text']) or []
    cost = float((r['usage'] or {}).get('cost') or 0)
    rec = {'run': a.run, 'model': a.label, 'model_actual': r['model_actual'], 'source': 'openrouter', 'mode': mode, 'icp': icp, 'n': n,
           'status': 'completed' if rows else 'нет оценок: ' + (r['error'] or r['finish']), 'seconds': round(time.time() - t0), 'price': cost,
           'searches': len(r['annotations']), 'rows': rows, 'raw': r['text'],
           'cited': sorted({x['url_citation']['url'] for x in r['annotations'] if x.get('url_citation')}),      # что модель процитировала на деле
           'input_tokens': (r['usage'] or {}).get('prompt_tokens')}
    rec['queries'], rec['sources'] = find_extra(r['text'])
    if not rec['cited']:                             # при ответе в JSON OpenRouter цитат не отдаёт: сверять названные источники не с чем
        rec['cited'] = None
    if rec['queries']:
        rec['searches'] = len(rec['queries'])        # число поисков — по запросам, которые назвала сама модель (число цитат им не равно)
    # ответ сохраняем всегда: 20.11.2026 четыре оплаченных ответа пропали, потому что разбор не справился с новым форматом
    (f if rows else f.with_suffix('.unparsed')).write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    me = next((x for x in rows if OUR.search(str(x.get('platform', '')))), None)
    return f.name, rec['status'], cost, (len(rows), me and me.get('overall'), me and me.get('known'))


jobs = [(mode, icp, n) for mode in a.modes.split(',') for icp in a.scenarios.split(',') for n in range(1, a.runs + 1)]
total = 0
with cf.ThreadPoolExecutor(4) as pool:
    for name, st, cost, info in pool.map(one, jobs):
        total += cost
        print(f'{name:46} {st:12} ${cost:.3f} {info}', flush=True)
print(f'итого ${total:.2f}')
# готовые файлы — в хранилище проекта, откуда их берёт сводка (раньше загружались вручную)
from flymy import MCP
import uuid
m = MCP()
for f in sorted(out.glob(a.label + '_*.json')):
    m.call('apps_storage_write', {'app': APP, 'storage': 'files', 'path': f'brand/{a.run}/{f.name}', 'content': f.read_text(),
                                  'content_type': 'application/json', 'operation_key': 'brand-put-' + uuid.uuid4().hex[:10]})
    print('в хранилище:', f.name)
