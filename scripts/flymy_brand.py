#!/usr/bin/env python3
"""Замер «оценка бренда» (вопрос Б: оценки компаний по критериям из project.json) через агента FlyMyAI с нативным поиском модели.

  python3 flymy_brand.py --run 2026-11-19 [--models gpt-6-sol,gemini-3.8-flash] [--modes web,no_web] [--runs 2] [--scenarios a,b]
Каждый вызов — отдельный запуск агента с чистым контекстом. Ответы: out/brand/<run>/ и хранилище проекта brand/<run>/.
"""
import argparse, json, re, sys, time, uuid, pathlib
import concurrent.futures as cf
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP
from project import ROOT, APP, BRAND_AGENT as AGENT, BRAND, COMPANIES, P, path, brand_re   # агент проекта: модель и поиск переключаем перед каждой серией

BS = P['brand_score']
SCEN = {s['key']: s for s in BS['scenarios']}
OUR = brand_re()
SYSTEM = ('You are a chat assistant. Answer the user message exactly as you would in an ordinary chat%s. '
          'Do not delegate, do not create files, do not run code, do not add commentary about the task. '
          'Return your complete answer verbatim as the result.')
# Приписка к вопросу в режиме «с поиском»: без неё Claude на русском вопросе не ищет вовсе
# и оценивает по памяти. Заодно просим перечислить запросы и источники — в том же сообщении, вторым модель их уже не видит.
WEB_SUFFIX = {
 'ru': ('\n\nПеред ответом обязательно поищи в интернете свежую информацию о каждой платформе из списка.\n\n'
        'Формат ответа другой: верни один объект JSON с тремя полями. "platforms" — массив оценок, как описано выше. '
        '"queries" — все поисковые запросы, которые ты сделал, дословно. "sources" — адреса всех страниц, которые ты использовал в ответе. '
        'Без текста вокруг.'),
 'en': ('\n\nBefore answering, be sure to search the web for fresh information about each platform on the list.\n\n'
        'The answer format is different: return one JSON object with three fields. "platforms" is the array of scores described above. '
        '"queries" is every search query you ran, verbatim. "sources" is the URL of every page you used in the answer. No prose.'),
}


# Вторая половина вопроса: список компаний и критерии. Ситуацию покупателя пишет владелец в файле сценария.
ASK = {
 'en': ('Score each of these platforms for me:\n{platforms}\n\nFor each one give a score from 0.00 to 5.00 with two decimal places:\n{criteria}\n\n'
        'And for each one add:\n- known: true if you have real information about this company,\n  false if you are inferring the score from the name\n'
        '- reasoning: 1-2 sentences on what drove the score\n- concerns: the main objection or risk, if there is one\n\n'
        'Return valid JSON only — an array of objects, one per platform, each with the field "platform". No prose.'),
 'ru': ('Оцени для меня каждую из этих платформ:\n{platforms}\n\nКаждой поставь оценку от 0.00 до 5.00 с двумя знаками после точки:\n{criteria}\n\n'
        'И для каждой добавь:\n- known: true, если у тебя есть реальные сведения об этой компании,\n  false, если оценка выведена из названия\n'
        '- reasoning: 1–2 предложения о том, чем определена оценка\n- concerns: главное возражение или риск, если он есть\n\n'
        'Верни только корректный JSON — массив объектов, по одному на платформу, у каждого поле "platform". Без текста вокруг.'),
}


def build_prompt(icp, mode):
    s = SCEN[icp]
    p = path(s['file']).read_text(encoding='utf-8').strip()
    lang = 'ru' if re.search('[а-яё]', p, re.I) else 'en'
    names = [n for n in COMPANIES if n not in (s.get('exclude') or [])]
    crit = BS['criteria']                         # {ключ: {"label": подпись на странице, "ask": {"ru": …, "en": …}}}; overall обязателен
    ask = lambda v: v['ask'][lang] if isinstance(v['ask'], dict) else v['ask']
    p += '\n\n' + ASK[lang].format(platforms=', '.join(names), criteria='\n'.join('- %s: %s' % (k, ask(v)) for k, v in crit.items()))
    if mode != 'web':
        return p
    return p + WEB_SUFFIX[lang]


def find_extra(o):
    """Запросы и источники, которые модель назвала сама: ({"queries": [...], "sources": [...]}) в любой обёртке."""
    if isinstance(o, str):
        t = clean(o)
        for cand in (t, t[t.find('{'):t.rfind('}') + 1]):
            try:
                return find_extra(json.loads(cand))
            except Exception:
                pass
        return [], []
    if isinstance(o, dict):
        if isinstance(o.get('queries'), list) or isinstance(o.get('sources'), list):
            return [str(x) for x in o.get('queries') or []], [str(x) for x in o.get('sources') or []]
        for v in o.values():
            q, s = find_extra(v)
            if q or s:
                return q, s
    return [], []


def clean(text):
    """Текст ответа без меток цитирования и обёртки ```json — модели с поиском вставляют <cite> прямо в JSON."""
    t = re.sub(r'</?cite[^>]*>', '', text)
    t = re.sub(r'\(cite index="[^"]*">', '', t)
    m = re.search(r'```(?:json)?\s*(.*?)```', t, flags=re.S)
    return (m.group(1) if m else t).strip()


def find_rows(o):
    """Список оценок в любой обёртке: {"platforms": [...]}, {"answer": "[...]"}, текст с JSON внутри."""
    if isinstance(o, list) and o and all(isinstance(x, dict) for x in o) and any('platform' in x or ('name' in x and 'overall' in x) for x in o):
        for x in o:                                  # в объекте с тремя полями Claude называет поле name, а не platform
            if 'platform' not in x and 'name' in x:
                x['platform'] = x.pop('name')
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = find_rows(v)
            if r:
                return r
    if isinstance(o, str) and '[' in o:
        t = clean(o)
        for cand in (t, t[t.find('{'):t.rfind('}') + 1], t[t.find('['):t.rfind(']') + 1]):
            for variant in (cand, re.sub(r',(\s*[}\]])', r'\1', cand)):
                try:
                    r = find_rows(json.loads(variant))
                    if r:
                        return r
                except Exception:
                    pass
    return None


def one(job):
    model, mode, icp, n, run, out = job
    tag = f'{model}_{mode}_{icp}_{n}'
    if (out / f'{tag}.json').exists():
        return tag, 'есть', 0, None
    m = MCP()
    prompt = build_prompt(icp, mode)
    t0 = time.time()
    r = m.call('run_agent', {'agent_id': AGENT, 'message': prompt, 'subagent_limits': {'cap_usd': 0},
                             'operation_key': f'brand-{run}-{tag}-{uuid.uuid4().hex[:6]}'})
    ex, since = r['id'], None
    while True:
        g = m.call('get_run', {'execution_id': ex, **({'since': since} if since else {})})
        since = g.get('next_since') or since
        if g.get('poll_complete') or time.time() - t0 > 1200:
            break
        time.sleep(8)
    res = g.get('result') or {}
    v = (res.get('inline') or {}).get('value')
    if res.get('truncated'):
        v, off = '', 0
        while True:
            x = m.call('read_run_result', {'execution_id': ex, 'ref': res['ref'], 'offset': off})
            v += x.get('text', '')
            if x.get('complete'):
                break
            off = x['next_offset']
    try:
        parsed = json.loads(v) if isinstance(v, str) else v
    except Exception:
        parsed = v
    rows = find_rows(parsed) or []
    if not rows:                                     # .json не пишем — повторный запуск переснимет; сам ответ сохраняем, чтобы оплаченное не пропало
        (out / f'{tag}.unparsed').write_text(json.dumps({'execution': ex, 'raw': v}, ensure_ascii=False, indent=1))
        return tag, 'пустой ответ', 0, str(v)[:120]
    time.sleep(6)
    price = m.call('get_execution_price', {'execution_id': ex})
    rec = {'run': run, 'model': model, 'mode': mode, 'icp': icp, 'n': n, 'execution': ex, 'status': g.get('status'),
           'seconds': round(time.time() - t0), 'price': float(price.get('total_price') or 0),
           'searches': sum(1 for t in price.get('tool_calls', []) if 'web_search' in t['action_name']),
           'rows': rows, 'raw': v}
    rec['queries'], rec['sources'] = find_extra(parsed)
    body = json.dumps(rec, ensure_ascii=False, indent=1)
    (out / f'{tag}.json').write_text(body)
    m.call('apps_storage_write', {'app': APP, 'storage': 'files', 'path': f'brand/{run}/{tag}.json', 'content': body,
                                  'content_type': 'application/json', 'operation_key': 'brand-put-' + uuid.uuid4().hex[:10]})
    me = next((x for x in rows if OUR.search(str(x.get('platform', '')))), None)
    return tag, g.get('status'), rec['price'], (len(rows), me and me.get('overall'), me and me.get('known'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--models', default=','.join(BS['models']), help='Claude через агента возвращал пустые ответы — его снимает brand_openrouter.py')
    ap.add_argument('--modes', default='web,no_web')
    ap.add_argument('--scenarios', default=','.join(SCEN))
    ap.add_argument('--runs', type=int, default=BS.get('runs', 2))
    ap.add_argument('--parallel', type=int, default=4, help='запусков одной серии одновременно; у Claude при 4 ответы приходили пустыми')
    a = ap.parse_args()
    if not AGENT:
        raise SystemExit('В project.json нет ID агента оценки (flymy.brand_agent): сначала scripts/setup.py --apply')
    out = ROOT / 'out' / 'brand' / a.run
    out.mkdir(parents=True, exist_ok=True)
    m, total = MCP(), 0.0
    for model in a.models.split(','):
        for mode in a.modes.split(','):          # серия = одна модель и один режим: настройка агента общая, запуски внутри параллельны
            m.call('update_agent', {'agent_id': AGENT, 'model': model, 'web_search': 'auto' if mode == 'web' else 'off',
                                    'user_prompt': SYSTEM % (', searching the web when useful' if mode == 'web' else ', from your own knowledge only, without any web search')})
            jobs = [(model, mode, icp, n, a.run, out) for icp in a.scenarios.split(',') for n in range(1, a.runs + 1)]
            with cf.ThreadPoolExecutor(a.parallel) as pool:
                for tag, st, price, info in pool.map(one, jobs):
                    total += price
                    print(f'{tag:44} {st:10} ${price:.3f}  платформ/overall/known своего бренда: {info}', flush=True)
    print(f'итого ${total:.2f}')


if __name__ == '__main__':
    main()
