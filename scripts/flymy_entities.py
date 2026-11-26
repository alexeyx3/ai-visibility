#!/usr/bin/env python3
"""Разбор ответов дешёвой моделью FlyMyAI: все компании, которые назвал движок, по порядку, с пометкой «рекомендует».

  python3 flymy_entities.py --run 2026-11-19 [--model flymyai/mimo-v2_6-flash]
Читает записи замера из хранилища проекта, результат кладёт туда же (runs/<run>/entities_NN.json) и в out/entities/<run>.json.
Повторный запуск разбирает только новые ответы.
"""
import argparse, hashlib, json, re, sys, time, uuid, pathlib, urllib.request
import concurrent.futures as cf
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP, _key
from project import APP, ROOT, BRAND, SITE, P, brand_re

A = P['analysis']
SYS = ('You extract companies from an AI assistant answer. Return JSON only: {"companies":[{"name":"...","recommended":true|false}]}. '
       'List every company, platform, product or service brand that the answer names as an option for the user '
       '(' + A['company_kinds'] + '), '
       'in order of first appearance, each once, name exactly as written in the answer. "recommended": true if the answer presents it as a suitable option, '
       'false if it is mentioned only as unavailable, unsuitable or as a warning. '
       'Do not list: ' + A['not_companies'] + ', regulators, laws, countries, currencies, media, review sites or sources. Never add a company that is not written in the answer. '
       'If none, return {"companies":[]}.')
OUR = brand_re()
STANCES = ['top_pick', 'recommended', 'listed', 'caution', 'negative']
SYS_STANCE = ('You judge how an AI assistant answer treats the company %s (%s). Return JSON only: ' % (BRAND, SITE) +
              '{"stance":"top_pick|recommended|listed|caution|negative","quote":"...","why":"..."}. '
              'top_pick: named the best or first choice for the user. recommended: presented as a suitable option with a positive description. '
              'listed: only named or described neutrally, without advice to choose it. '
              'caution: named with reservations - little known, unverified, check before using, limited information, promotional sources. '
              'negative: the answer advises against it or says it does not fit. '
              '"quote": the sentence of the answer that best shows this, copied verbatim, at most 300 characters. "why": one short sentence in Russian.')


def chat(model, system, text, max_tokens=1500):
    body = {'model': model.split('/')[1], 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': text[:14000]}],
            'max_tokens': max_tokens, 'temperature': 0, 'response_format': {'type': 'json_object'}}
    req = urllib.request.Request(f'https://api.flymy.ai/api/v1/{model}/openai/v1/chat/completions', data=json.dumps(body).encode(),
                                 headers={'X-API-KEY': _key(), 'Authorization': 'Bearer ' + _key(), 'Content-Type': 'application/json'})
    for attempt in range(4):
        try:
            if attempt:                              # пустой ответ = лимит ушёл на рассуждение (оно входит в выходные токены): повторяем с запасом
                body['max_tokens'] = max_tokens * (2 if attempt == 1 else 4)
                req.data = json.dumps(body).encode()
            d = json.load(urllib.request.urlopen(req, timeout=180))
            return json.loads(d['choices'][0]['message']['content']), d.get('usage', {})
        except Exception as e:
            err = repr(e)[:200]
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(err)


def norm(s):
    return re.sub(r'[^a-zа-яё0-9]', '', s.lower())


def ask(model, text):
    """Компании из ответа. Всё, чего нет в тексте ответа буква в букву, отбрасываем: модель не должна дописывать от себя."""
    d, u = chat(model, SYS, text)
    t, out = norm(text), []
    for c in d.get('companies') or []:
        name = str(c.get('name') or '').strip()[:80]
        if name and norm(name) and norm(name) in t:
            out.append({'name': name, 'recommended': bool(c.get('recommended', True))})
    return out, u


def stance(model, text):
    d, u = chat(model, SYS_STANCE, text, 400)
    st = d.get('stance') if d.get('stance') in STANCES else 'listed'
    q = str(d.get('quote') or '')[:320]
    return {'stance': st, 'quote': q if norm(q)[:40] in norm(text) else '', 'why': str(d.get('why') or '')[:200]}, u


def ls(m, prefix):
    out, cur = [], None
    while True:
        l = m.call('apps_storage_list', {'app': APP, 'storage': 'files', 'prefix': prefix, 'limit': 100, **({'cursor': cur} if cur else {})})
        out += [o['path'] for o in l.get('objects', [])]
        cur = l.get('next')
        if not cur:
            return out


def read(m, path):
    out, off = '', 0
    while off is not None:
        r = m.call('apps_storage_read', {'app': APP, 'storage': 'files', 'path': path, 'offset': off, 'limit': 150000})
        out += r.get('content') or ''
        off = r.get('next_offset')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--model', default='flymyai/deepseek-v4_1-flash', help='MiMo V2.6 Flash дешевле, но на сверке 20.11 дописывал компании, которых нет в ответе')
    ap.add_argument('--threads', type=int, default=8)
    a = ap.parse_args()
    m = MCP()
    cache_f = ROOT / 'out' / 'entities' / f'{a.run}.json'
    cache_f.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    best = {}
    for path in sorted(ls(m, f'runs/{a.run}/records_')):          # тот же порядок слияния, что в сводке
        for r in json.loads(read(m, path))['records']:
            k = (r['prompt_id'], r['channel'])
            if k not in best or r['status'] in ('ok', 'no_answer') or best[k]['status'] not in ('ok', 'no_answer'):
                best[k] = r
    todo = []
    for (pid, ch), r in best.items():
        if r['status'] != 'ok' or not r.get('text'):
            continue
        h = hashlib.sha1(r['text'].encode()).hexdigest()[:12]      # ответ пересняли — разбираем заново
        key = f'{pid}|{ch}'
        if cache.get(key, {}).get('h') != h or cache[key].get('model') != a.model:
            todo.append((key, h, r['text']))
    print(f'ответов {len(best)}, к разбору {len(todo)}', flush=True)
    tin = tout = fail = 0

    def one(job):
        key, h, text = job
        try:
            comp, u = ask(a.model, text)
            val = {'h': h, 'model': a.model, 'companies': comp}
            if OUR.search(re.sub(r'https?://\S+|\]\([^)]*\)', ' ', text)):     # тон — только там, где свой бренд назван в тексте
                val['our'], u2 = stance(a.model, text)
                u = {k: u.get(k, 0) + u2.get(k, 0) for k in ('prompt_tokens', 'completion_tokens')}
            return key, val, u
        except Exception as e:
            return key, None, {'err': str(e)}
    with cf.ThreadPoolExecutor(a.threads) as pool:
        for i, (key, val, u) in enumerate(pool.map(one, todo), 1):
            if val:
                cache[key] = val
                tin += u.get('prompt_tokens', 0)
                tout += u.get('completion_tokens', 0)
            else:
                fail += 1
            if i % 100 == 0:
                cache_f.write_text(json.dumps(cache, ensure_ascii=False))
                print(f'  {i}/{len(todo)}', flush=True)
    cache_f.write_text(json.dumps(cache, ensure_ascii=False))
    items = {k: {'companies': v['companies'], 'our': v.get('our')} for k, v in cache.items() if tuple(k.split('|')) in best}
    part, size, n = {}, 0, 0
    for k, v in list(items.items()) + [(None, None)]:
        b = len(json.dumps(v, ensure_ascii=False).encode()) + len(k or '') if k else 0
        if part and (k is None or size + b > 200000):
            m.call('apps_storage_write', {'app': APP, 'storage': 'files', 'path': f'runs/{a.run}/entities_{n:02d}.json', 'content_type': 'application/json',
                                          'content': json.dumps({'run': a.run, 'model': a.model, 'items': part}, ensure_ascii=False), 'operation_key': 'ent-' + uuid.uuid4().hex[:10]})
            part, size, n = {}, 0, n + 1
        if k:
            part[k] = v
            size += b
    print(f'разобрано {len(todo) - fail}, ошибок {fail}, токенов {tin} + {tout}, файлов в хранилище {n}')


if __name__ == '__main__':
    main()
