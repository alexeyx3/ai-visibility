#!/usr/bin/env python3
"""Замер видимости в облаке FlyMyAI: режет набор промптов на части и запускает агента-сборщика на каждую.

  python3 flymy_measure.py --run 2026-11-19 [--limit 5] [--chunk 25] [--parallel 3]
Вопросы — из файла, указанного в project.json (questions), или --prompts.
"""
import argparse, base64, json, sys, time, pathlib, uuid
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP
from project import ROOT, APP, COLLECTOR, BRAND, COMPANIES, OUR_DOMAINS, questions

CHANNELS = ['chatgpt', 'gemini', 'ai_mode', 'ai_overview', 'perplexity', 'claude']


LOADER = """import json, time, flymy
JOB = "%s"
def _load(path):
    out, off = "", 0
    while off is not None:
        for wait in (0, 5, 15, 30, 60):
            time.sleep(wait)
            try:
                r = flymy.tool("app_storage", "storage_read", {"storage": "files", "path": path, "offset": off, "limit": 100000}, operation_key="load-%%s-%%d-%%d" %% (path, off, int(time.time())))
                break
            except Exception as e:
                if "busy" not in repr(e) or wait == 60:
                    raise
        out += r.get("content") or ""
        off = r.get("next_offset")
    return out
exec(compile(_load("%s"), "job", "exec"))"""


def storage_write(m, path, content):
    m.call('apps_storage_write', {'app': APP, 'storage': 'files', 'path': path, 'content': content, 'content_type': 'text/plain' if path.endswith('.txt') else 'application/json',
                                  'operation_key': 'put-' + uuid.uuid4().hex[:12]})


def run_job(m, script, job_path, cfg, key, timeout=1700, limit=1200):
    """Кладёт код и задание в хранилище, агенту отдаёт только короткий загрузчик; возвращает (execution_id, статус, результат)."""
    storage_write(m, f'code/{script}.txt', (ROOT / 'flymy_app' / script).read_text(encoding='utf-8'))
    storage_write(m, job_path, json.dumps(cfg, ensure_ascii=False))
    code = LOADER % (job_path, f'code/{script}.txt')
    run = m.call('run_agent', {'agent_id': COLLECTOR, 'message': f'Run this code once with execute_code and timeout_seconds={limit}:\n```python\n' + code + '\n```',
                               'subagent_limits': {'cap_usd': 0}, 'operation_key': key})
    ex, since, t0 = run['id'], None, time.time()
    while True:
        g = m.call('get_run', {'execution_id': ex, **({'since': since} if since else {})})
        since = g.get('next_since') or since
        if g.get('poll_complete') or time.time() - t0 > timeout:
            break
        time.sleep(10)
    res = g.get('result') or {}
    v = (res.get('inline') or {}).get('value')
    if res.get('truncated'):
        v, off = '', 0
        while True:
            r = m.call('read_run_result', {'execution_id': ex, 'ref': res['ref'], 'offset': off})
            v += r.get('text', '')
            if r.get('complete'):
                break
            off = r['next_offset']
    return ex, g.get('status'), v


def storage_read(m, path):
    out, off = '', 0
    while off is not None:
        r = m.call('apps_storage_read', {'app': APP, 'storage': 'files', 'path': path, 'offset': off, 'limit': 150000})
        out += r.get('content') or ''
        off = r.get('next_offset')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--prompts', help='другой файл вопросов вместо указанного в project.json')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--offset', type=int, default=0, help='пропустить первые N промптов (досъём хвоста набора)')
    ap.add_argument('--tag', default='', help='приставка к номерам частей, чтобы досъём не перезаписал файлы основного прогона')
    ap.add_argument('--chunk', type=int, default=25, help='промптов в одном облачном запуске')
    ap.add_argument('--parallel', type=int, default=3, help='сколько облачных запусков одновременно (на аккаунт не больше трёх долгих команд)')
    ap.add_argument('--concurrency', type=int, default=8, help='одновременных вызовов внутри одного пакета (FlyMyAI: до 8)')
    ap.add_argument('--channels', default=','.join(CHANNELS))
    ap.add_argument('--only-chunks', help='номера частей через запятую — добрать упавшие')
    a = ap.parse_args()
    if not COLLECTOR:
        raise SystemExit('В project.json нет ID сборщика (flymy.collector): сначала scripts/setup.py --apply')
    prompts = (json.loads((ROOT / a.prompts).read_text(encoding='utf-8')) if a.prompts else questions())[a.offset:][:a.limit]
    channels = a.channels.split(',')
    brands = {n: {'domain': d, 'patterns': p} for n, (d, p) in COMPANIES.items()}
    chunks = [prompts[i:i + a.chunk] for i in range(0, len(prompts), a.chunk)]
    only = {int(x) for x in a.only_chunks.split(',')} if a.only_chunks else None
    m = MCP()
    b0 = float(m.call('whoami')['balance'])
    import concurrent.futures as cf

    def do(job):
        tag, part, jobs = job
        mm = MCP()                                   # сеанс MCP не делим между потоками
        cfg = {'run': a.run, 'chunk': tag, 'prompts': part, 'brands': brands, 'our': BRAND, 'jobs': jobs,
               'our_domains': sorted(OUR_DOMAINS), 'channels': channels, 'concurrency': a.concurrency,
               'max_calls': (len(jobs) if jobs else len(part) * len(channels)) * 2}
        t0, bad = time.time(), []
        try:
            ex, st, out = run_job(mm, 'collect_batch.py', f'jobs/{a.run}_{tag}.json', cfg, f'measure-{a.run}-{tag}-{uuid.uuid4().hex[:6]}')
            done = json.loads(storage_read(mm, f'runs/{a.run}/done_{tag}.json'))                  # сводка из хранилища, не из пересказа агента
            recs = [r for k in range(done['files']) for r in json.loads(storage_read(mm, f'runs/{a.run}/records_{tag}_{k:02d}.json'))['records']]
            stat = {}
            for r in recs:
                stat[r['status']] = stat.get(r['status'], 0) + 1
            bad = [(r['prompt_id'], r['channel']) for r in recs if r['status'] in ('error', 'skipped')]
            print(f'часть {tag}: {len(part)} промптов, запуск {ex} {st}, {time.time() - t0:.0f} с | {stat} | DataForSEO ${sum(r["cost"] for r in recs if not r.get("reused")):.3f}', flush=True)
        except Exception as e:                        # запуск оборвался: всё, что успело лечь в raw/, подберёт добор
            bad = [(p['id'], c) for p in part for c in channels] if not jobs else [tuple(j) for j in jobs]
            print(f'часть {tag}: не завершена за {time.time() - t0:.0f} с — {str(e)[:160]}', flush=True)
        return bad

    plan = [(a.tag + '%02d' % n, part, None) for n, part in enumerate(chunks) if not only or n in only]
    for rnd in range(1, 4):                           # основной круг и до двух кругов добора
        with cf.ThreadPoolExecutor(a.parallel) as pool:
            failed = [x for bad in pool.map(do, plan) for x in bad]
        if not failed:
            break
        print(f'добор {rnd}: {len(failed)} вызовов', flush=True)
        ids = sorted({f[0] for f in failed})
        groups = [ids[i:i + a.chunk] for i in range(0, len(ids), a.chunk)]
        plan = [(a.tag + 'r%d%02d' % (rnd, i), [p for p in prompts if p['id'] in g], [list(f) for f in failed if f[0] in g]) for i, g in enumerate(groups)]
    else:
        print(f'после добора осталось {len(failed)} несостоявшихся вызовов', flush=True)
    time.sleep(15)
    w = MCP().call('whoami')                      # новый сеанс: старый за время съёма истекает
    print('списано с баланса: $%.3f (ещё в обработке $%s)' % (b0 - float(w['balance']), w['pending_usd']))


if __name__ == '__main__':
    main()
