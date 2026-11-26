"""Вызов модели через OpenRouter. Ключ — переменная OPENROUTER_API_KEY или файл config/openrouter.env."""
import json, os, pathlib, time, urllib.request

API = 'https://openrouter.ai/api/v1/chat/completions'
ENV = pathlib.Path(__file__).resolve().parent.parent / 'config' / 'openrouter.env'


def api_key():
    if os.environ.get('OPENROUTER_API_KEY'):
        return os.environ['OPENROUTER_API_KEY'].strip()
    for line in (ENV.read_text().splitlines() if ENV.exists() else []):
        if line.startswith('OPENROUTER_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"\'')
    return None

def call(key, model, prompt, web, retries=3, max_tokens=16000):
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens}
    # у Perplexity поиск встроен в модель, плагин OpenRouter на ней отдаёт 404
    if web and not model.startswith('perplexity/'):
        body['plugins'] = [{'id': 'web', 'engine': 'native'}]
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    for attempt in range(retries + 1):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=900))
            ch = d['choices'][0]
            msg = ch['message']
            text = msg.get('content') or ''
            if not text.strip():
                raise RuntimeError(f"пустой ответ, finish_reason={ch.get('finish_reason')}")
            return {'text': text, 'annotations': msg.get('annotations') or [],
                    'usage': d.get('usage', {}), 'model_actual': d.get('model', model),
                    'finish': ch.get('finish_reason', ''), 'error': ''}
        except Exception as e:
            if attempt == retries:
                return {'text': '', 'annotations': [], 'usage': {}, 'model_actual': model,
                        'finish': '', 'error': f'{type(e).__name__}: {str(e)[:200]}'}
            time.sleep(8 * (attempt + 1))
