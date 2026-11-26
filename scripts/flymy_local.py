"""Запуск облачного скрипта (flymy_app/*.py) локально: модуль flymy подменяется вызовами MCP FlyMyAI.
Данные читаются и пишутся в то же хранилище проекта.

  python3 flymy_local.py finalize.py jobs/2026-11-19_finalize.json

Черновая сборка (dry — папка): файлы сайта ложатся в папку, в хранилище ничего не пишется, задание передаётся в памяти.
С data — папкой, устроенной как хранилище (runs/…, brand/…), — FlyMyAI не нужен совсем: так собирается выдуманный пример.
"""
import json, sys, types, uuid, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flymy import MCP
from project import ROOT, APP

MAP = {('app_storage', 'storage_read'): 'apps_storage_read', ('app_storage', 'storage_write'): 'apps_storage_write',
       ('app_storage', 'storage_list'): 'apps_storage_list', ('sites', 'site_publish'): 'publish_site', ('sites', 'site_get'): 'get_site'}


def run(script, job, dry=None, job_content=None, data=None):
    m = None if data else MCP()
    local = pathlib.Path(data) if data else None

    def tool(t, a, args, operation_key=None):
        args = dict(args)
        if t == 'app_storage':
            args['app'] = APP
        if a in ('storage_write', 'site_publish'):
            args['operation_key'] = 'local-' + uuid.uuid4().hex[:12]
        if a == 'storage_read' and job_content is not None and args['path'] == job:
            return {'content': job_content, 'next_offset': None}
        if t == 'sites' and dry:                     # черновая сборка: файлы сайта в локальную папку, без публикации
            if a == 'site_get':
                return {'live_version': 1}
            for f in args['files']:
                q = pathlib.Path(dry) / f['path']
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_text(f['content'], encoding='utf-8')
            return {'id': 'dry', 'live_version': 1, 'owner_url': 'file://' + str(pathlib.Path(dry).resolve())}
        if dry and a == 'storage_write':             # черновая сборка хранилище не меняет
            return {}
        if local:
            if a == 'storage_list':
                fs = sorted(str(q.relative_to(local)) for q in local.rglob('*') if q.is_file())
                return {'objects': [{'path': f} for f in fs if f.startswith(args['prefix'])]}
            if a == 'storage_read':
                q = local / args['path']
                if not q.exists():
                    raise RuntimeError('нет файла ' + args['path'])
                return {'content': q.read_text(encoding='utf-8'), 'next_offset': None}
        if dry and a == 'storage_read' and '/records_' in args['path']:   # кэшируем только записи ответов: задание, разбор и оценки меняются
            import hashlib
            c = pathlib.Path(dry).parent / '.cache' / hashlib.md5(('%s|%s|%s' % (APP, args['path'], args.get('offset'))).encode()).hexdigest()
            if c.exists():
                return json.loads(c.read_text(encoding='utf-8'))
            r = m.call(MAP[(t, a)], args)
            c.parent.mkdir(parents=True, exist_ok=True)
            c.write_text(json.dumps(r, ensure_ascii=False), encoding='utf-8')
            return r
        return m.call(MAP[(t, a)], args)

    shim = types.ModuleType('flymy')
    shim.tool = tool
    sys.modules['flymy'] = shim
    code = (ROOT / 'flymy_app' / script).read_text(encoding='utf-8')
    exec(compile(code, script, 'exec'), {'JOB': job, '__name__': '__cloud__'})


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)   # третий аргумент — папка для черновой сборки сайта
