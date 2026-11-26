# Сбор и разбор ответов пакетами: по каждому каналу все промпты части уходят одним flymy.batch (параллельно).
# Сырые ответы -> raw/<run>/pack_<chunk>_<channel>.json, разобранные записи -> runs/<run>/records_<chunk>.json
import base64, json, re, time, threading, flymy
from concurrent.futures import ThreadPoolExecutor

K0 = [0]


def _read(path):
    out, off = "", 0
    while off is not None:
        for wait in (0, 5, 15, 30, 60):       # при многих одновременных запусках платформа отвечает «capacity is busy»
            time.sleep(wait)
            K0[0] += 1
            try:
                r = flymy.tool("app_storage", "storage_read", {"storage": "files", "path": path, "offset": off, "limit": 100000}, operation_key="cfg-%s-%d-%d" % (path, int(time.time()), K0[0]))
                break
            except Exception as e:
                if "busy" not in repr(e) or wait == 60:
                    raise
        out += r.get("content") or ""
        off = r.get("next_offset")
    return out


CFG = json.loads(_read(JOB))        # JOB задаёт загрузчик: путь к заданию в хранилище
RUN, CHUNK = CFG["run"], CFG["chunk"]
BRANDS = {n: [re.compile(p, re.I if n == CFG["our"] else 0) for p in b["patterns"]] for n, b in CFG["brands"].items()}
BRAND_DOMAINS = {b["domain"]: n for n, b in CFG["brands"].items()}
OUR_DOMAINS = set(CFG["our_domains"])
CH = {
    "chatgpt": lambda t, g: ("dataforseo_ai_llm_scraper", {"platform": "chat_gpt", "keyword": t, "force_web_search": True, **g}),
    "gemini": lambda t, g: ("dataforseo_ai_llm_scraper", {"platform": "gemini", "keyword": t, **g}),
    "ai_mode": lambda t, g: ("dataforseo_serp_google_ai_mode_live", {"keyword": t, **g}),
    "ai_overview": lambda t, g: ("dataforseo_serp_google_organic_live", {"keyword": t, "depth": 10, "load_async_ai_overview": True, **g}),
    "perplexity": lambda t, g: ("dataforseo_ai_llm_response", {"platform": "perplexity", "model_name": "sonar-pro", "user_prompt": t}),
    "claude": lambda t, g: ("dataforseo_ai_llm_response", {"platform": "claude", "model_name": "claude-sonnet-5-5", "user_prompt": t, "web_search": True}),
}
def dom(u):
    m = re.match(r"https?://([^/?#]+)", u or "")
    return re.sub(r"^www\.", "", m.group(1).lower()) if m else ""


def links(items, url_key="url"):
    out, seen = [], set()
    for s in items or []:
        u = re.sub(r"[?&]utm_source=(chatgpt\.com|openai)", "", s.get(url_key) or "")
        if not u or u in seen:
            continue
        seen.add(u)
        d = dom(u)
        if d == "google.com" and s.get("source"):      # ссылка-редирект Google: настоящий сайт только в названии
            d = "google-redirect:" + s["source"]
        out.append({"url": u, "domain": d, "title": (s.get("title") or s.get("source_name") or "")[:200]})
    return out


def extract(ch, res):
    r = {"text": "", "sources": [], "found": [], "fanout": [], "model": None, "answered": True}
    if ch in ("chatgpt", "gemini"):
        its = res.get("items") or []
        r["text"] = res.get("markdown") or "\n".join(i.get("markdown") or "" for i in its)
        r["sources"] = links(res.get("sources") or [s for i in its for s in (i.get("sources") or [])])
        r["found"] = links(res.get("search_results"))
        r["fanout"] = res.get("fan_out_queries") or []
        r["model"] = res.get("model")
    elif ch in ("ai_mode", "ai_overview"):
        aio = [i for i in res.get("items") or [] if i.get("type") == "ai_overview"]
        r["model"] = "google_" + ch
        if not aio:
            r["answered"] = False                       # Google не показал ИИ-ответ — это результат, не ошибка
        else:
            a = aio[0]
            r["text"] = a.get("markdown") or "\n".join(s.get("markdown") or s.get("text") or "" for s in (a.get("items") or []))
            r["sources"] = links((a.get("references") or []) + [x for s in (a.get("items") or []) for x in (s.get("references") or [])])
        if ch == "ai_overview":
            r["found"] = links([i for i in res.get("items") or [] if i.get("type") == "organic"])
    else:
        secs = [s for i in res.get("items") or [] if i.get("type") == "message" for s in (i.get("sections") or [])]
        r["text"] = "".join(s.get("text") or "" for s in secs)
        r["sources"] = links([a for s in secs for a in (s.get("annotations") or [])])
        r["found"] = links(res.get("search_results"))
        r["fanout"] = res.get("fan_out_queries") or []
        r["model"] = res.get("model_name")
    return r


def analyze(r):
    """Упоминания брендов — поиском по тексту ответа; цитирования — по доменам источников."""
    text, hits = r["text"], []
    for name, pats in BRANDS.items():
        pos = [m.start() for p in pats for m in [p.search(text)] if m]
        if pos:
            hits.append((min(pos), name))
    hits.sort()
    r["brands"] = [n for _, n in hits]                                    # в порядке первого появления
    r["our_position"] = r["brands"].index(CFG["our"]) + 1 if CFG["our"] in r["brands"] else None
    doms = {s["domain"] for s in r["sources"]}
    r["our_cited"] = sorted(d for d in doms if d in OUR_DOMAINS)
    r["brands_cited"] = sorted({BRAND_DOMAINS[d] for d in doms if d in BRAND_DOMAINS})
    r["our_found"] = sorted({s["domain"] for s in r["found"] if s["domain"] in OUR_DOMAINS})
    r["searched"] = bool(r["fanout"] or r["sources"] or r["found"])
    return r




def put(path, obj):
    for wait in (0, 5, 10, 20, 30, 45, 60):               # «capacity is busy» — ждём и повторяем
        time.sleep(wait)
        K0[0] += 1
        try:
            flymy.tool("app_storage", "storage_write", {"storage": "files", "path": path, "content": json.dumps(obj, ensure_ascii=False)},
                       operation_key="put-%s-%d-%d" % (path, int(time.time()), K0[0]))
            return
        except Exception as e:
            if "busy" not in repr(e):
                raise
    raise RuntimeError("платформа занята: запись " + path)


def wire(o):
    """Размер записи в том виде, в каком уходит вызов: строка внутри JSON, где кириллица и кавычки экранируются (русская буква — 6 байт)."""
    return len(json.dumps(json.dumps(o, ensure_ascii=False)))


def put_list(prefix, head, key, items, limit=170000):
    """Список пишется несколькими файлами <prefix>_NN.json. Платформа не принимает вызов больше 262 144 байт, считая с экранированием;
    23.11.2026 части с русскими вопросами в этот лимит не попали, и оплаченные ответы пропали. Если запись всё же не прошла — делим пополам."""
    files = [0]

    def flush(part):
        try:
            put("%s_%02d.json" % (prefix, files[0]), {**head, key: part})
            files[0] += 1
        except Exception as e:
            if "busy" in repr(e):
                raise
            if len(part) < 2:
                # Одна запись, и её не приняли. 23.11.2026 платформа отклоняла записи, где в тексте ответа была подписанная ссылка
                # («egress policy rejected a signed storage link»): пробуем без параметров в адресах, затем — заглушку под добор.
                t = re.sub(r'(https?://[^\s"\\?]+)\?[^\s"\\)]*', r"\1", json.dumps(part[0], ensure_ascii=False))
                try:
                    put("%s_%02d.json" % (prefix, files[0]), {**head, key: [json.loads(t)]})
                except Exception as e2:
                    if "busy" in repr(e2) or key != "records":
                        raise
                    r0 = part[0]
                    put("%s_%02d.json" % (prefix, files[0]), {**head, key: [{k: r0.get(k) for k in ("run", "prompt_id", "prompt", "lang", "loc_code", "tags", "channel", "attempts", "cost")}
                                                                             | {"status": "error", "error": "запись отклонена платформой: " + repr(e2)[:120]}]})
                files[0] += 1
                return
            h = len(part) // 2
            flush(part[:h])
            flush(part[h:])

    part, size = [], 0
    for it in items + [None]:
        b = wire(it) if it is not None else 0
        if part and (it is None or size + b > limit):
            flush(part)
            part, size = [], 0
        if it is not None:
            part.append(it)
            size += b
    return files[0]


def good(x):
    d = (x or {}).get("data", x) if isinstance(x, dict) else None
    task = ((d or {}).get("tasks") or [{}])[0]
    return d if task.get("status_code") == 20000 and task.get("result") else None


def why(x):
    try:
        return "провайдер: %s" % x.get("data", x)["tasks"][0].get("status_message")
    except Exception:
        return "ошибка: " + str(x)[:160]


T0, recs, log = time.time(), [], {}
for ch in CFG["channels"]:
    t0 = time.time()
    only = {tuple(j) for j in CFG["jobs"]} if CFG.get("jobs") else None       # добор: только упавшие пары [промпт, канал]
    P = [p for p in CFG["prompts"] if not only or (p["id"], ch) in only]
    if not P:
        continue
    reqs = [CH[ch](p["text"], {"location_code": p["loc_code"], "language_code": p["lang"]}) for p in P]
    action = reqs[0][0]
    got, cost, tries, last = [None] * len(P), [0.0] * len(P), [0] * len(P), [None] * len(P)
    for attempt in (1, 2, 3):
        todo = [i for i in range(len(P)) if got[i] is None]
        if not todo:
            break
        if attempt > 1:
            time.sleep(8 * attempt)
        try:
            r = flymy.batch("dataforseo", action, [reqs[i][1] for i in todo], concurrency=CFG["concurrency"], keep_results=True)
            res = r.get("results") or []
        except Exception as e:
            res, last_err = [], repr(e)[:200]
            for i in todo:
                last[i] = "ошибка пакета: " + last_err
        for i, x in zip(todo, res):
            tries[i] = attempt
            try:
                cost[i] += float(((x or {}).get("data", x) or {}).get("cost") or 0)
            except Exception:
                pass
            d = good(x)
            if d:
                got[i] = d
            else:
                last[i] = why(x)
    pack = []
    for i, p in enumerate(P):
        rec = {"run": RUN, "prompt_id": p["id"], "prompt": p["text"], "lang": p["lang"], "loc_code": p["loc_code"], "tags": p.get("tags", []),
               "channel": ch, "status": "error", "attempts": tries[i], "cost": round(cost[i], 6)}
        if got[i]:
            try:
                res0 = got[i]["tasks"][0]["result"][0]
                rec.update(analyze(extract(ch, res0)))
                rec["status"] = "ok" if rec["answered"] else "no_answer"
                rec["asked_at"] = res0.get("datetime")
                pack.append({"prompt": p, "request": reqs[i][1], "response": got[i]})
            except Exception as e:
                rec["error"] = "разбор: " + repr(e)[:200]
        else:
            rec["error"] = last[i] or "нет ответа"
        recs.append(rec)
    try:
        put_list("raw/%s/pack_%s_%s" % (RUN, CHUNK, ch), {"run": RUN, "chunk": CHUNK, "channel": ch}, "answers", pack)
    except Exception as e:
        log["raw_" + ch] = repr(e)[:200]
    log[ch] = {"sec": round(time.time() - t0), "ok": sum(1 for x in got if x), "of": len(P)}
nfiles = put_list("runs/%s/records_%s" % (RUN, CHUNK), {"run": RUN, "chunk": CHUNK}, "records", recs)
put("runs/%s/done_%s.json" % (RUN, CHUNK), {"run": RUN, "chunk": CHUNK, "files": nfiles, "records": len(recs)})
st = {}
for r in recs:
    st[r["status"]] = st.get(r["status"], 0) + 1
print(json.dumps({"run": RUN, "chunk": CHUNK, "sec": round(time.time() - T0), "status": st, "dataforseo_usd": round(sum(r["cost"] for r in recs), 4), "log": log}, ensure_ascii=False))
