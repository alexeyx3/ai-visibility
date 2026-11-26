"""Настройки проекта: всё, что относится к бренду, конкурентам, вопросам и аккаунту FlyMyAI, лежит в project.json.

Остальные скрипты берут их отсюда. Путь к файлу можно задать переменной AV_PROJECT (по умолчанию project.json в корне).
"""
import json, os, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILE = pathlib.Path(os.environ.get('AV_PROJECT') or ROOT / 'project.json')
if not FILE.is_absolute():
    FILE = ROOT / FILE


def load():
    if not FILE.exists():
        raise SystemExit(f'Нет файла настроек {FILE}. Скопируйте project.example.json в project.json и заполните (см. README.md).')
    return json.loads(FILE.read_text(encoding='utf-8'))


def save_ids(**ids):
    """Дописывает ID из FlyMyAI в блок flymy, не трогая остальное."""
    p = load()
    p.setdefault('flymy', {}).update(ids)
    FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


P = load()
BRAND = P['brand']['name']
SITE = P['brand']['site']
APP = P['flymy']['app']
COLLECTOR = P['flymy'].get('collector') or ''
BRAND_AGENT = P['flymy'].get('brand_agent') or ''
# имя → (домен, шаблоны). Свой бренд — первым; его шаблоны ищутся без учёта регистра, конкурентов — с учётом
COMPANIES = {BRAND: (SITE, P['brand']['patterns'])}
COMPANIES.update({c['name']: (c['domain'], c['patterns']) for c in P.get('competitors') or []})
OUR_DOMAINS = sorted({SITE, *(P.get('own_sites') or [])})


def path(rel):
    q = pathlib.Path(rel)
    return q if q.is_absolute() else ROOT / q


def norm(url):
    """Адрес без протокола, www/m, параметров и хвостового слэша — так его сверяем с источниками."""
    u = re.sub(r'^https?://', '', url.strip().lower())
    u = re.sub(r'[?#].*$', '', u)
    u = re.sub(r'^(www|m)\.', '', u)
    return u.rstrip('/')


def publications():
    """Свои публикации на чужих площадках. Файл — по строке на адрес:
         https://… — наша публикация;  link https://… — ссылочная (купленная) статья;  blog хост — весь блог на этом хосте наш."""
    out = {'urls': {}, 'links': {}, 'blog_hosts': []}
    f = P.get('publications')
    if not f or not path(f).exists():
        return out
    for line in path(f).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        kind, _, rest = line.partition(' ')
        if kind == 'link' and rest.strip():
            out['links'][norm(rest)] = ''
        elif kind == 'blog' and rest.strip():
            out['blog_hosts'].append(norm(rest))
        elif line.startswith('http'):
            out['urls'][norm(line)] = ''
    out['blog_hosts'] = sorted(set(out['blog_hosts']))
    return out


def questions():
    """Вопросы покупателей; страна берётся из вопроса (loc_code) или по языку из locations."""
    qs = json.loads(path(P['questions']).read_text(encoding='utf-8'))
    loc = P.get('locations') or {}
    for q in qs:
        if not q.get('loc_code'):
            q['loc_code'] = loc[q['lang']]
    return qs


def brand_re():
    """Шаблоны своего бренда одной регуляркой (без учёта регистра)."""
    return re.compile('|'.join('(?:%s)' % p for p in P['brand']['patterns']), re.I)
