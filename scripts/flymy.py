#!/usr/bin/env python3
"""Клиент MCP FlyMyAI (Streamable HTTP, JSON-RPC). Ключ — переменная FLYMYAI_API_KEY или файл config/flymyai.env.

  python3 flymy.py tools                      # список инструментов
  python3 flymy.py call <tool> '<json-args>'  # вызов инструмента
"""
import json, os, sys, pathlib, urllib.request, urllib.error

URL = 'https://mcp-agents.flymy.ai/mcp'
ENV = pathlib.Path(__file__).resolve().parent.parent / 'config' / 'flymyai.env'


def _key():
    if os.environ.get('FLYMYAI_API_KEY'):
        return os.environ['FLYMYAI_API_KEY'].strip()
    for line in (ENV.read_text().splitlines() if ENV.exists() else []):
        if line.startswith('FLYMYAI_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"\'')
    raise SystemExit('FLYMYAI_API_KEY не найден')


class MCP:
    def __init__(self):
        self.h = {'X-API-Key': _key(), 'Content-Type': 'application/json',
                  'Accept': 'application/json, text/event-stream'}
        self.n = 0
        self.info = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                            'clientInfo': {'name': 'ai-visibility', 'version': '1'}})
        self.rpc('notifications/initialized', None, notify=True)

    def rpc(self, method, params, notify=False, timeout=600):
        body = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            body['params'] = params
        if not notify:
            self.n += 1
            body['id'] = self.n
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=self.h)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'HTTP {e.code}: {e.read().decode()[:500]}')
        sid = resp.headers.get('Mcp-Session-Id')
        if sid:
            self.h['Mcp-Session-Id'] = sid
        raw = resp.read().decode()
        if notify or not raw.strip():
            return None
        if 'text/event-stream' in (resp.headers.get('Content-Type') or ''):
            msgs = [json.loads(l[5:]) for l in raw.split('\n') if l.startswith('data:')]      # не splitlines: в текстах ответов бывают юникодные разделители строк
            d = next(m for m in msgs if m.get('id') == body['id'])
        else:
            d = json.loads(raw)
        if 'error' in d:
            raise RuntimeError(json.dumps(d['error'], ensure_ascii=False)[:800])
        return d['result']

    def call(self, tool, args=None):
        r = self.rpc('tools/call', {'name': tool, 'arguments': args or {}})
        if r.get('structuredContent') is not None:
            return r['structuredContent']
        txt = ''.join(c.get('text', '') for c in r.get('content', []))
        if r.get('isError') or txt.startswith('Error:'):
            raise RuntimeError(f'{tool}: {txt[:600]}')
        try:
            return json.loads(txt)
        except Exception:
            return txt


if __name__ == '__main__':
    m = MCP()
    if sys.argv[1] == 'tools':
        for t in m.rpc('tools/list', {})['tools']:
            print(t['name'], '—', (t.get('description') or '').split('\n')[0][:110])
    else:
        out = m.call(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
        print(json.dumps(out, ensure_ascii=False, indent=1) if not isinstance(out, str) else out)
