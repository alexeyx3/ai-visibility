#!/usr/bin/env python3
"""Дополнения к замеру, которые нельзя посчитать из готовых записей. Запускать после съёма и перед сводкой (flymy_site.py):

  python3 scripts/enrich.py --run 2026-11-19

1. Редиректы Google. AI Overviews отдаёт источники как google.com/goto?url=<шифр>. Раскрываем их в настоящие адреса,
   иначе все такие ссылки неотличимы друг от друга и наши страницы среди них не находятся.
2. Контекст цитат. Из сырых ответов достаём, к какой фразе ответа привязана ссылка на наш источник,
   а у Google — ещё и фрагмент страницы, который он показал рядом со ссылкой.
3. Проверка наших процитированных страниц: есть ли на них свой бренд, где первое упоминание, каким по счёту он назван.

Результат пишется в хранилище: runs/<run>/enrich_<имя>_NN.json. Сводка подхватывает эти файлы сама.
Кэш — в out/enrich/<run>/: повторный запуск докачивает только новое.
"""
import argparse, html, json, re, sys, pathlib, urllib.request, urllib.error
import concurrent.futures as cf
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP
from project import APP, BRAND, SITE, COMPANIES, OUR_DOMAINS, publications, brand_re
import flymy_measure as F

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
ap = argparse.ArgumentParser()
ap.add_argument('--run', required=True)
a = ap.parse_args()
RUN = a.run
OUT = F.ROOT / 'out' / 'enrich' / RUN
(OUT / 'raw').mkdir(parents=True, exist_ok=True)
m = MCP()


def ls(prefix):
    paths, cur = [], None
    while True:
        l = m.call('apps_storage_list', {'app': APP, 'storage': 'files', 'prefix': prefix, 'limit': 100, **({'cursor': cur} if cur else {})})
        paths += [o['path'] for o in (l.get('objects') or l.get('files') or [])]
        cur = l.get('next')
        if not cur:
            return paths


def cached(path):
    f = OUT / 'raw' / path.replace('/', '__')
    if not f.exists():
        for attempt in range(4):                    # ответ платформы иногда обрывается на середине — повторяем
            try:
                t = F.storage_read(m, path)
                json.loads(t)
                break
            except Exception:
                if attempt == 3:
                    raise
        f.write_text(t, encoding='utf-8')
    return json.loads(f.read_text(encoding='utf-8'))


def load(name):
    f = OUT / (name + '.json')
    return json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}


def save(name, obj):
    """Локально целиком, в хранилище — частями до ~90 КБ."""
    (OUT / (name + '.json')).write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    for p in ls('runs/%s/enrich_%s_' % (RUN, name)):          # старые части перезаписываем пустыми, чтобы не осталось хвостов
        F.storage_write(m, p, '{}')
    part, size, n = {}, 0, 0
    for k, v in list(obj.items()) + [(None, None)]:
        b = len(json.dumps(v, ensure_ascii=False).encode()) + len(k or '') if k else 0
        if part and (k is None or size + b > 90000):
            F.storage_write(m, 'runs/%s/enrich_%s_%02d.json' % (RUN, name, n), json.dumps(part, ensure_ascii=False))
            part, size, n = {}, 0, n + 1
        if k:
            part[k] = v
            size += b
    print('%s: %d записей, частей %d' % (name, len(obj), n))


def nurl(u):
    u = re.sub(r'^https?://', '', (u or '').strip().lower())
    if not u.startswith('google.com/goto'):
        u = re.sub(r'[?#].*$', '', u)
    return re.sub(r'^(www|m)\.', '', u).rstrip('/')


def dom(u):
    x = re.match(r'https?://([^/?#]+)', u or '')
    return re.sub(r'^www\.', '', x.group(1).lower()) if x else ''


# ---------- записи замера
best = {}
for p in sorted(ls('runs/%s/records_' % RUN)):
    for r in cached(p)['records']:
        k = (r['prompt_id'], r['channel'])
        if k not in best or r['status'] in ('ok', 'no_answer') or best[k]['status'] not in ('ok', 'no_answer'):
            best[k] = r
recs = [r for r in best.values() if r['status'] == 'ok']
print('ответов:', len(recs))

# ---------- 1. редиректы Google


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


opener = urllib.request.build_opener(NoRedirect)


def resolve(u):
    try:
        opener.open(urllib.request.Request(u.replace('//google.com', '//www.google.com'), headers={'User-Agent': UA}), timeout=20)
    except urllib.error.HTTPError as e:
        loc = e.headers.get('Location') or ''
        if loc.startswith('http') and 'google.com/goto' not in loc:
            return loc
    except Exception:
        pass
    return None


red = load('redirects')
todo = sorted({s['url'] for r in recs for s in (r.get('sources') or []) + (r.get('found') or []) if 'google.com/goto' in s.get('url', '')} - set(red))
print('редиректов к раскрытию:', len(todo))
with cf.ThreadPoolExecutor(6) as ex:
    for u, t in zip(todo, ex.map(resolve, todo)):
        if t:
            red[u] = t
print('раскрыто всего %d, не удалось %d' % (len(red), len([u for u in todo if u not in red])))
save('redirects', red)

# ---------- чьи источники (те же правила, что в сводке)
PUBS = publications()
P_URLS, P_BLOGS, P_LINKS = set(PUBS['urls']), set(PUBS['blog_hosts']), set(PUBS.get('links') or {})


def owner(u):
    d, n = dom(u), nurl(u)
    return 'own' if d == SITE else 'sat' if d in OUR_DOMAINS else 'pub' if d in P_BLOGS or n in P_URLS else 'link' if n in P_LINKS else None


# ---------- 2. контекст цитат
def clean(t):
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', t or '')
    t = re.sub(r'\[([^\]]*)\]\((https?:[^)\s]*)\)', r'\1', t)
    return re.sub(r'\s+', ' ', re.sub(r'[*#`|]+', ' ', t)).strip()


def line_at(text, pos):
    """Строка ответа вокруг позиции: абзац или пункт списка, к которому привязана ссылка."""
    s, e = text.rfind('\n', 0, pos) + 1, text.find('\n', pos)
    return clean(text[s:e if e >= 0 else len(text)])


def contexts(ch, res):
    """[(адрес, фраза ответа, фрагмент страницы)] по всем источникам ответа."""
    out = []
    if ch in ('claude', 'perplexity'):
        secs = [s for i in res.get('items') or [] if i.get('type') == 'message' for s in (i.get('sections') or [])]
        full, off = '', []
        for s in secs:
            off.append(len(full))
            full += s.get('text') or ''
        for s, o in zip(secs, off):
            an = s.get('annotations') or []
            txt = s.get('text') or ''
            marks = list(re.finditer(r'\[(\d+)\]', txt))
            if ch == 'perplexity' and marks:           # сноски [1], [2] внутри текста: номер -> источник
                for mk in marks:
                    i = int(mk.group(1)) - 1
                    if 0 <= i < len(an):
                        out.append((an[i].get('url'), line_at(txt, mk.start()), ''))
            else:
                for x in an:
                    out.append((x.get('url'), line_at(full, o) if txt.strip() else '', ''))
    elif ch in ('chatgpt', 'gemini'):
        for i in res.get('items') or []:
            md = i.get('markdown') or ''
            for s in i.get('sources') or []:
                u = s.get('url') or ''
                p = md.find(u.split('?')[0]) if u else -1
                out.append((u, line_at(md, p) if p >= 0 else clean(md)[:400], s.get('snippet') or ''))
    else:
        for it in res.get('items') or []:
            if it.get('type') != 'ai_overview':
                continue
            for x in it.get('references') or []:
                out.append((x.get('url'), '', x.get('text') or ''))
            for el in it.get('items') or []:
                for x in el.get('references') or []:
                    out.append((x.get('url'), clean(el.get('text') or el.get('markdown')), x.get('text') or ''))
    return out


ctx = {}
packs = [p for p in ls('raw/%s/' % RUN)]
print('пакетов сырых ответов:', len(packs))
for p in packs:
    d = cached(p)
    for an in d.get('answers') or []:
        try:
            resp = an['response'].get('data', an['response'])
            res = resp['tasks'][0]['result'][0]
        except Exception:
            continue
        key, seen, rows = '%s|%s' % (an['prompt']['id'], d['channel']), set(), []
        for u, frag, snip in contexts(d['channel'], res):
            u = red.get(u, u)
            if not u or not owner(u) or not (frag or snip):
                continue
            k = (nurl(u), frag[:80])
            if k in seen:
                continue
            seen.add(k)
            rows.append({'u': nurl(u), 't': frag[:420], 's': clean(snip)[:300]})
        if rows:
            ctx[key] = rows[:12]
save('contexts', ctx)

# ---------- 3. проверка наших процитированных страниц
PATS = {n: [re.compile(p, re.I if n == BRAND else 0) for p in ps] for n, (_, ps) in COMPANIES.items()}
OUR = brand_re()
cited = {}
for r in recs:
    for s in r.get('sources') or []:
        u = red.get(s['url'], s['url'])
        if owner(u):
            cited[nurl(u)] = u
audit = load('audit')


def fetch(u):
    try:
        raw = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': UA, 'Accept-Language': 'ru,en;q=0.8'}), timeout=25).read(3_000_000)
    except Exception as e:
        return {'ok': False, 'err': (getattr(e, 'code', None) and 'HTTP %s' % e.code) or type(e).__name__}
    t = raw.decode('utf-8', 'ignore')
    title = clean(html.unescape((re.search(r'<title[^>]*>(.*?)</title>', t, re.S | re.I) or [0, ''])[1]))
    body = re.sub(r'<(script|style|noscript|svg|nav|footer|header|aside)\b.*?</\1>', ' ', t, flags=re.S | re.I)
    main = re.search(r'<(article|main)\b.*?</\1>', body, re.S | re.I)
    body = main.group(0) if main and len(main.group(0)) > 1500 else body
    heads = [clean(html.unescape(re.sub(r'<[^>]+>', ' ', h))) for h in re.findall(r'<h[1-3][^>]*>(.*?)</h[1-3]>', body, re.S | re.I)]
    text = clean(html.unescape(re.sub(r'<[^>]+>', ' ', body)))
    if len(text) < 400:
        return {'ok': False, 'err': 'пустая страница (текст подгружается скриптом)'}
    first = {}
    for n, ps in PATS.items():
        pos = [x.start() for p in ps for x in [p.search(text)] if x]
        if pos:
            first[n] = min(pos)
    order = sorted(first, key=first.get)
    me = [x.start() for x in OUR.finditer(text)]
    return {'ok': True, 'words': len(text.split()), 'n': len(me), 'first': round(100 * me[0] / len(text)) if me else None,
            'title': bool(OUR.search(title)), 'heads': len(heads), 'in_heads': sum(1 for h in heads if OUR.search(h)),
            'pos': order.index(BRAND) + 1 if BRAND in order else None, 'brands': len(order), 'before': order[:order.index(BRAND)][:6] if BRAND in order else order[:6]}


need = [n for n in cited if n not in audit or not audit[n].get('ok')]
print('страниц к проверке:', len(need), 'из', len(cited))
with cf.ThreadPoolExecutor(6) as ex:
    for n, r in zip(need, ex.map(lambda n: fetch(cited[n]), need)):
        audit[n] = r
audit = {n: audit[n] for n in cited}
print('прочитано %d, не открылось %d' % (sum(1 for v in audit.values() if v.get('ok')), sum(1 for v in audit.values() if not v.get('ok'))))
save('audit', audit)
