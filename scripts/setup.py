#!/usr/bin/env python3
"""Первичная настройка в аккаунте FlyMyAI. Повторный запуск безопасен: то, что уже создано, не создаётся заново.

  python3 scripts/setup.py           # проверить настройки и ключ, показать план приложения и оценку стоимости — ничего не создаёт и не тратит
  python3 scripts/setup.py --apply   # создать приложение (сборщик и хранилище), агента оценки бренда и страницу; ID записываются в project.json

Сама настройка бесплатна: платны только запуски (съём, разбор, оценка бренда).
"""
import argparse, json, re, sys, time, uuid, pathlib
if sys.version_info < (3, 11):
    raise SystemExit('Нужен Python 3.11 или новее')
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import project
from project import ROOT, P, APP, path

# цена одного ответа, $ (проверено 19–23.11.2026; DataForSEO через FlyMyAI без наценки)
PRICE = {'chatgpt': 0.004, 'gemini': 0.004, 'ai_mode': 0.004, 'ai_overview': 0.004, 'perplexity': 0.015, 'claude': 0.052}
PARSE = 0.0015               # разбор одного ответа (DeepSeek через FlyMyAI)
BRAND_RUN = {'flymyai': 0.65, 'openrouter': 0.8}    # один запуск оценки бренда: GPT+Gemini вместе через агента / Claude через OpenRouter


def check():
    """Ошибки в настройках, которые иначе всплывут посреди платного замера."""
    err, warn = [], []
    b = P.get('brand') or {}
    for k in ('name', 'site', 'patterns'):
        if not b.get(k):
            err.append(f'brand.{k} не заполнено')
    comps = P.get('competitors') or []
    if not comps:
        warn.append('конкурентов нет: доля голоса и место бренда будут бессмысленны')
    for c in [{'name': b.get('name'), 'patterns': b.get('patterns') or []}] + comps:
        if not c.get('patterns'):
            err.append(f'у «{c.get("name")}» нет шаблонов написания (patterns)')
        for p in c.get('patterns') or []:
            try:
                re.compile(p)
            except re.error as e:
                err.append(f'«{c.get("name")}»: шаблон {p!r} не разбирается: {e}')
    for c in comps:
        if not c.get('domain'):
            err.append(f'у конкурента «{c.get("name")}» нет домена')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9._-]*', APP or ''):
        err.append('flymy.app должно быть «логин/имя» маленькими латинскими буквами, например ivan/ai-visibility-acme')
    try:
        qs = project.questions()
        ids = [q['id'] for q in qs]
        if len(set(ids)) != len(ids):
            err.append('в вопросах повторяются id')
        langs = {q['lang'] for q in qs}
        if langs - {'ru', 'en'}:
            err.append(f'языки вопросов {sorted(langs - {"ru", "en"})}: поддерживаются только ru и en')
        for q in qs:
            if len(q['text']) > 500:
                err.append(f'вопрос {q["id"]} длиннее 500 знаков — Perplexity и Claude его не примут')
    except KeyError as e:
        err.append(f'у вопроса нет страны: язык {e} не указан в locations и у вопроса нет loc_code')
        qs = []
    except Exception as e:
        err.append(f'вопросы не читаются ({P.get("questions")}): {e}')
        qs = []
    for t in (P.get('topics') or {}).get('list') or []:
        try:
            re.compile(t['pattern'])
        except re.error as e:
            err.append(f'тема {t["key"]}: шаблон не разбирается: {e}')
    bs = P.get('brand_score') or {}
    if bs:
        if 'overall' not in (bs.get('criteria') or {}):
            err.append('brand_score.criteria: нужен критерий overall — по нему строится место и итоговая таблица')
        for s in bs.get('scenarios') or []:
            if not path(s['file']).exists():
                err.append(f'сценарий оценки {s["key"]}: нет файла {s["file"]}')
    pubs = project.publications()
    if P.get('publications') and not path(P['publications']).exists():
        warn.append(f'файла публикаций {P["publications"]} нет — свои публикации считаться не будут')
    return err, warn, qs, pubs


def cost(qs):
    n = len(qs)
    measure = n * sum(PRICE.values())
    parse = n * len(PRICE) * PARSE
    bs = P.get('brand_score') or {}
    runs, sc = bs.get('runs', 2), len(bs.get('scenarios') or [])
    brand = sc * 2 * runs * BRAND_RUN['flymyai']        # два режима: с поиском и без
    claude = sc * 2 * runs * BRAND_RUN['openrouter']
    return n, measure, parse, brand, claude


def plan_files():
    y = (ROOT / 'flymy_app' / 'flymy.yaml').read_text(encoding='utf-8').replace('__APP__', APP)
    return [{'path': 'flymy.yaml', 'content': y},
            {'path': 'agents/collector.md', 'content': (ROOT / 'flymy_app' / 'agents' / 'collector.md').read_text(encoding='utf-8')}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='создать в FlyMyAI то, чего ещё нет')
    a = ap.parse_args()

    err, warn, qs, pubs = check()
    print(f'Настройки: {project.FILE}')
    print(f'  бренд {P["brand"]["name"]} ({P["brand"]["site"]}), конкурентов {len(P.get("competitors") or [])}, '
          f'других своих сайтов {len(P.get("own_sites") or [])}, публикаций {len(pubs["urls"])}, ссылочных {len(pubs["links"])}, блогов целиком {len(pubs["blog_hosts"])}')
    for w in warn:
        print('  внимание:', w)
    if err:
        print('Исправьте настройки:')
        for e in err:
            print('  —', e)
        sys.exit(1)
    n, measure, parse, brand, claude = cost(qs)
    print(f'Вопросов {n}. Один замер ≈ ${measure:.2f} съём + ${parse:.2f} разбор; оценка бренда ≈ ${brand:.2f} (GPT и Gemini) + ${claude:.2f} (Claude через OpenRouter, по желанию).')
    print(f'Пробный замер на 5 вопросах без оценки бренда ≈ ${5 * (sum(PRICE.values()) + len(PRICE) * PARSE):.2f}.')

    from flymy import MCP, _key
    try:
        _key()
    except SystemExit:
        raise SystemExit('Нет ключа FlyMyAI: положите строку FLYMYAI_API_KEY=… в config/flymyai.env (см. README.md)')
    m = MCP()
    who = m.call('whoami')
    print(f'Аккаунт FlyMyAI: {who.get("username")}, баланс ${who.get("balance")}')
    if who.get('username') and APP.split('/')[0] != who['username']:
        raise SystemExit(f'flymy.app начинается с «{APP.split("/")[0]}», а аккаунт — «{who["username"]}». Имя приложения должно быть «{who["username"]}/…»')
    if float(who.get('balance') or 0) < measure + parse:
        print(f'  внимание: баланса не хватит на полный замер (≈ ${measure + parse:.2f})')

    apps = {x['app']: x for x in m.call('apps_list').get('apps', [])}
    have = apps.get(APP)
    collector = ((have or {}).get('agents') or {}).get('collector', {}).get('task')
    if collector:
        print(f'Приложение {APP} уже есть, сборщик {collector}')
    else:
        plan = m.call('apps_plan', {'files': plan_files()})
        print('План FlyMyAI:')
        for c in plan.get('changes') or []:
            print(f'  {c.get("action")} {c.get("kind")} {c.get("resource")}: {c.get("detail")}')
        for x in plan.get('notes') or []:
            print('  примечание:', x)
        if not a.apply:
            print('Ничего не создано. Если план верный — запустите с --apply.')
            return
        rel = m.call('apps_apply', {'files': plan_files(), 'plan_token': plan['plan_token'], 'operation_key': 'setup-' + uuid.uuid4().hex[:10]})
        print('Релиз:', rel.get('release') or rel.get('id'), rel.get('status'))
        for _ in range(40):                      # агенты создаются в фоне 1–3 минуты
            time.sleep(10)
            have = {x['app']: x for x in m.call('apps_list').get('apps', [])}.get(APP)
            collector = ((have or {}).get('agents') or {}).get('collector', {}).get('task')
            if collector and have.get('status') == 'succeeded':
                break
        if not collector:
            raise SystemExit('Сборщик не появился за 7 минут: посмотрите apps_list и запустите setup.py --apply ещё раз')
        print('Сборщик:', collector)
    if not a.apply:
        print('Дальше — с --apply (создаст агента оценки и страницу, если их нет).')
        return
    project.save_ids(collector=collector)

    agent = m.call('projects_agent', {'project_id': 'app:' + APP})
    print('Агент оценки бренда (агент проекта):', agent.get('agent'))
    project.save_ids(brand_agent=agent.get('agent'))

    site = None
    try:
        site = json.loads(m.call('apps_storage_read', {'app': APP, 'storage': 'files', 'path': 'summary/site.json'}).get('content') or 'null')
    except Exception:
        pass
    if not (site or {}).get('site_id'):
        html = ('<!doctype html><meta charset="utf-8"><title>%s</title><body style="font-family:sans-serif;padding:40px">'
                '<h1>%s</h1><p>Замеров пока нет. Страница обновится после первого замера.</p>') % (P['site']['title'], P['site']['title'])
        r = m.call('publish_site', {'name': P['site']['title'], 'files': [{'path': 'index.html', 'content': html}], 'operation_key': 'setup-site-' + uuid.uuid4().hex[:8]})
        s = r.get('site', r)
        site = {'site_id': s.get('id') or s.get('site_id'), 'owner_url': s.get('owner_url') or s.get('url'), 'live_version': s.get('live_version') or 1}
        m.call('apps_storage_write', {'app': APP, 'storage': 'files', 'path': 'summary/site.json', 'content': json.dumps(site),
                                      'content_type': 'application/json', 'operation_key': 'setup-sitejson-' + uuid.uuid4().hex[:8]})
    project.save_ids(site_id=site['site_id'], site_url=site.get('owner_url'))
    print('Страница:', site.get('owner_url'), '(закрытая: видит только владелец аккаунта, доступ даётся в FlyMyAI)')
    print('Готово. ID записаны в', project.FILE)


if __name__ == '__main__':
    main()
