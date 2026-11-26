# Сводка замера и публикация дашборда. Выполняется в песочнице агента FlyMyAI.
# Читает runs/<run>/records_*.json из хранилища, считает метрики, дописывает summary/index.json, публикует сайт.
import base64, json, math, re, time, flymy

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
RUN, OUR, OUR_SITE = CFG["run"], CFG["our"], CFG["our_site"]
OUR_DOMAINS, BRAND_DOMAINS = set(CFG["our_domains"]), CFG["brand_domains"]
CHANNELS = CFG["channels"]
STATE = CFG.get("state", "summary")      # папка с индексом замеров и адресом сайта
K = [0]


def tool(t, a, args):
    for wait in (0, 5, 10, 20, 30, 45, 60, 60, 60):       # платформа под нагрузкой отвечает «capacity is busy» — ждём и повторяем
        time.sleep(wait)
        K[0] += 1
        try:
            return flymy.tool(t, a, args, operation_key="fin-%s-%d-%d" % (RUN, int(time.time()), K[0]))
        except Exception as e:
            if "busy" not in repr(e):
                raise
    raise RuntimeError("платформа занята: %s %s" % (t, a))


def read(path):
    out, off = "", 0
    while off is not None:
        r = tool("app_storage", "storage_read", {"storage": "files", "path": path, "offset": off, "limit": 100000})
        out += r.get("content") or ""
        off = r.get("next_offset")
    return out


def exists_read(path, default):
    try:
        return json.loads(read(path))
    except Exception:
        return default


def write(path, obj):
    tool("app_storage", "storage_write", {"storage": "files", "path": path, "content": json.dumps(obj, ensure_ascii=False)})


def wilson(k, n):
    if not n:
        return [0, 0]
    z, p = 1.96, k / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    d = 1 + z * z / n
    return [round(100 * max(0, (c - h) / d), 1), round(100 * min(1, (c + h) / d), 1)]


def pct(k, n):
    return round(100 * k / n, 1) if n else None


# --- записи: последняя удачная по каждой паре (промпт, канал)
paths, cur = [], None
while True:
    l = tool("app_storage", "storage_list", {"storage": "files", "prefix": "runs/%s/records_" % RUN, "limit": 100, **({"cursor": cur} if cur else {})})
    objs = l.get("objects") or l.get("files") or []
    paths += [o["path"] for o in objs]
    cur = l.get("next")
    if not cur:
        break
best = {}
for path in sorted(paths):          # 00..NN, затем r1, r2 — добор перекрывает ошибки
    for r in json.loads(read(path))["records"]:
        k = (r["prompt_id"], r["channel"])
        if k not in best or r["status"] in ("ok", "no_answer") or best[k]["status"] not in ("ok", "no_answer"):
            best[k] = r
recs = list(best.values())

# --- наши источники считаем здесь, а не при съёме: список сайтов сети и публикаций пополняется
PUBS, BLOGS, LINKS = set(CFG.get("our_pubs") or []), set(CFG.get("blog_hosts") or []), set(CFG.get("our_links") or [])


def nurl(u):
    u = re.sub(r"^https?://", "", (u or "").strip().lower())
    if not u.startswith("google.com/goto"):       # нераскрытый редирект Google различается только параметром
        u = re.sub(r"[?#].*$", "", u)
    return re.sub(r"^(www|m)\.", "", u).rstrip("/")


def owner(s):
    """Чей источник: own — свой сайт, sat — другой свой сайт, pub — наша публикация на чужой площадке, link — ссылочная статья."""
    d = s["domain"]
    if d == OUR_SITE:
        return "own"
    if d in OUR_DOMAINS:
        return "sat"
    if d in BLOGS or nurl(s.get("url")) in PUBS:
        return "pub"
    if nurl(s.get("url")) in LINKS:
        return "link"
    return None


def enrich(name):
    """Дополнения от scripts/enrich.py: редиректы Google, контекст цитат, проверка наших страниц. Нет файлов — пустой словарь."""
    out, cur = {}, None
    while True:
        l = tool("app_storage", "storage_list", {"storage": "files", "prefix": "runs/%s/enrich_%s_" % (RUN, name), "limit": 100, **({"cursor": cur} if cur else {})})
        for o in (l.get("objects") or l.get("files") or []):
            out.update(json.loads(read(o["path"])))
        cur = l.get("next")
        if not cur:
            return out


REDIR, CTX, AUDIT = enrich("redirects"), enrich("contexts"), enrich("audit")
for r in recs:
    for s in (r.get("sources") or []) + (r.get("found") or []):
        t = REDIR.get(s.get("url"))
        if t:                                     # ссылка-редирект Google -> настоящий адрес
            s["url"] = t
            s["domain"] = re.sub(r"^www\.", "", re.match(r"https?://([^/?#]+)", t).group(1).lower())
    for key in ("sources", "found"):              # после раскрытия редиректов одна страница может повториться
        seen, uniq = set(), []
        for s in r.get(key) or []:
            if nurl(s.get("url")) not in seen:
                seen.add(nurl(s.get("url")))
                uniq.append(s)
        r[key] = uniq
for r in recs:
    cx = {}
    for x in CTX.get("%s|%s" % (r["prompt_id"], r["channel"])) or []:
        cx.setdefault(x["u"], x)
    for s in (r.get("sources") or []) + (r.get("found") or []):
        s["o"] = owner(s)
    for s in r.get("sources") or []:
        x = cx.get(nurl(s.get("url")))
        if s["o"] and x:
            s["x"], s["xs"] = x.get("t") or "", x.get("s") or ""
    r["sats"] = sorted({s["domain"] for s in r.get("sources") or [] if s["o"] == "sat"})
    r["our_cited"] = sorted({s["domain"] for s in r.get("sources") or [] if s["o"] in ("own", "sat")})
    r["our_found"] = sorted({s["domain"] for s in r.get("found") or [] if s["o"]})
    r["pubs"] = sorted({nurl(s["url"]) for s in r.get("sources") or [] if s["o"] == "pub"})
    r["links"] = sorted({nurl(s["url"]) for s in r.get("sources") or [] if s["o"] == "link"})

# --- упоминания брендов считаем здесь, по тексту без адресов ссылок и картинок: правила можно менять без пересъёма
PATS = {n: [re.compile(p, (re.I if n == OUR else 0) | re.M) for p in ps] for n, ps in CFG["brands"].items()}


def visible(t):
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t or "")          # картинки
    t = re.sub(r"\]\([^)]*\)", "]", t)                          # адреса ссылок, подпись остаётся
    return re.sub(r"https?://\S+", " ", t)


for r in recs:
    if r["status"] != "ok":
        continue
    t, hits = visible(r.get("text")), []
    for n, ps in PATS.items():
        pos = [m.start() for p in ps for m in [p.search(t)] if m]
        if pos:
            hits.append((min(pos), n))
    r["brands"] = [n for _, n in sorted(hits)]
    r["our_position"] = r["brands"].index(OUR) + 1 if OUR in r["brands"] else None

# --- все названные компании: разбор дешёвой моделью (scripts/flymy_entities.py), если он уже сделан для этого замера
ENT, cur = {}, None
while True:
    l = tool("app_storage", "storage_list", {"storage": "files", "prefix": "runs/%s/entities_" % RUN, "limit": 100, **({"cursor": cur} if cur else {})})
    for o in (l.get("objects") or l.get("files") or []):
        ENT.update(json.loads(read(o["path"]))["items"])
    cur = l.get("next")
    if not cur:
        break


# хвосты названий, которые не меняют компанию: общие + отраслевые из настроек (analysis.suffixes)
SUFFIX = re.compile(r"[\s.,]*(%s)$" % "|".join(sorted(set(["inc", "ltd", "llc", "app", r"\.com", r"\.io", r"\.ai"] + [re.escape(x) for x in CFG.get("suffixes") or []]), key=len, reverse=True)), re.I)


def ckey(name):
    """Ключ для склейки написаний: «Acme Business» и «Acme», «Acme Inc» и «Acme» — одна компания."""
    n = name.strip()
    for _ in range(2):
        n = SUFFIX.sub("", n).strip()
    return re.sub(r"[^a-zа-яё0-9]", "", n.lower()) or re.sub(r"\s+", "", name.lower())


def canon(name):
    for n, ps in PATS.items():          # написание модели приводим к нашему имени бренда, если это отслеживаемый бренд
        if any(p.search(name) or p.search(name + " (") for p in ps):
            return n
    return name.strip()


raw_names = {}                          # ключ -> {написание: сколько раз}; показываем самое частое
for r in recs:
    r["companies"], r["our_recommended"], r["stance"], r["quote"], r["why"] = [], None, None, "", ""
    e = ENT.get("%s|%s" % (r["prompt_id"], r["channel"]))
    if r["status"] != "ok" or e is None:
        continue
    ents = e if isinstance(e, list) else (e.get("companies") or [])
    our = None if isinstance(e, list) else e.get("our")
    seen = set()
    for c in ents:
        n = canon(c["name"])
        k = n if n in PATS else ckey(n)
        if k in seen:
            continue
        seen.add(k)
        if n not in PATS:
            raw_names.setdefault(k, {})
            raw_names[k][n] = raw_names[k].get(n, 0) + 1
        r["companies"].append({"key": k, "name": n, "rec": bool(c.get("recommended", True))})
    if our and OUR in r["brands"]:
        r["stance"], r["quote"], r["why"] = our.get("stance"), our.get("quote") or "", our.get("why") or ""
for r in recs:
    for c in r["companies"]:
        if c["key"] in raw_names:
            c["name"] = max(raw_names[c["key"]].items(), key=lambda x: (x[1], -len(x[0])))[0]
        del c["key"]
    names = [c["name"] for c in r["companies"]]
    if OUR in names:                    # место — среди всех названных компаний, а не только отслеживаемых
        r["our_position"] = names.index(OUR) + 1
        r["our_recommended"] = r["companies"][names.index(OUR)]["rec"]
    if r["stance"] is None and OUR in r.get("brands", []) and r["status"] == "ok":
        r["stance"] = "listed"
PARSED = sum(1 for r in recs if r["companies"])

# --- метрики по каналам
engines = {}
for ch in CHANNELS:
    rs = [r for r in recs if r["channel"] == ch]
    ok = [r for r in rs if r["status"] == "ok"]
    n = len(ok)
    men = [r for r in ok if OUR in r["brands"]]
    cit = [r for r in ok if OUR_SITE in r["our_cited"]]
    net = [r for r in ok if r["our_cited"]]
    sat = [r for r in ok if r["sats"]]
    pub = [r for r in ok if r["pubs"]]
    lnk = [r for r in ok if r["links"]]
    ours = [r for r in ok if r["pubs"] or r["links"] or r["our_cited"]]
    ghost = [r for r in cit if OUR not in r["brands"]]
    allm = sum(len(r["brands"]) for r in ok)
    lead = {}
    for r in ok:
        for b in r["brands"]:
            lead[b] = lead.get(b, 0) + 1
    models = {}
    for r in ok:
        models[r.get("model") or "?"] = models.get(r.get("model") or "?", 0) + 1
    lead_all = {}
    for r in ok:
        for c in r["companies"]:
            if c["rec"]:
                lead_all[c["name"]] = lead_all.get(c["name"], 0) + 1
    rec_n = sum(1 for r in ok if r["stance"] in ("top_pick", "recommended"))
    stances = {}
    for r in ok:
        if r["stance"]:
            stances[r["stance"]] = stances.get(r["stance"], 0) + 1
    engines[ch] = {
        "asked": len(rs), "answers": n, "no_answer": sum(1 for r in rs if r["status"] == "no_answer"),
        "errors": sum(1 for r in rs if r["status"] in ("error", "skipped")),
        "mentions": pct(len(men), n), "mentions_ci": wilson(len(men), n), "mentions_n": len(men),
        "citations": pct(len(cit), n), "citations_ci": wilson(len(cit), n), "citations_n": len(cit),
        "network": pct(len(net), n), "network_n": len(net), "sats": pct(len(sat), n), "sats_n": len(sat), "pubs": pct(len(pub), n), "pubs_n": len(pub), "links": pct(len(lnk), n), "links_n": len(lnk), "ours": pct(len(ours), n), "ours_n": len(ours), "ghost": pct(len(ghost), n),
        "sov": pct(len(men), allm), "brand_mentions": allm,
        "position": round(sum(r["our_position"] for r in men) / len(men), 1) if men else None,
        "searched": pct(sum(1 for r in ok if r["searched"]), n), "models": models,
        "leaderboard": sorted(lead.items(), key=lambda x: -x[1]),
        "leaderboard_all": sorted(lead_all.items(), key=lambda x: -x[1])[:60], "recommended": pct(rec_n, n), "recommended_n": rec_n, "stances": stances,
        "avg_companies": round(sum(len(r["companies"]) for r in ok) / max(1, sum(1 for r in ok if r["companies"])), 1),
        "cost": round(sum(r["cost"] for r in rs), 3),
    }

# --- источники: по сайтам и по страницам
CLS = {"own": "own", "sat": "satellite", "pub": "pub", "link": "link"}
src, pages = {}, {}
for r in recs:
    if r["status"] != "ok":
        continue
    with_us = 1 if OUR in r["brands"] else 0
    doms = {}
    for s in r["sources"]:
        doms.setdefault(s["domain"], set()).add(s["o"])
    for d, os_ in doms.items():
        e = src.setdefault(d, {"domain": d, "cited": 0, "prompts": set(), "engines": set(), "with_us": 0, "pub": 0, "o": set(), "refs": set(), "ce": {}})
        e["ce"][r["channel"]] = e["ce"].get(r["channel"], 0) + 1
        e["refs"].add((r["prompt_id"], r["channel"]))
        e["cited"] += 1
        e["prompts"].add(r["prompt_id"])
        e["engines"].add(r["channel"])
        e["with_us"] += with_us
        e["pub"] += 1 if "pub" in os_ else 0
        e["link"] = e.get("link", 0) + (1 if "link" in os_ else 0)
        e["o"] |= os_
    seen = set()
    for s in r["sources"]:
        u = nurl(s.get("url"))
        if not u or u in seen:
            continue
        seen.add(u)
        e = pages.setdefault(u, {"url": s["url"], "title": (s.get("title") or "")[:110], "domain": s["domain"], "o": s["o"], "cited": 0, "prompts": set(), "engines": set(), "with_us": 0, "refs": set(), "ce": {}, "ctx": []})
        e["ce"][r["channel"]] = e["ce"].get(r["channel"], 0) + 1
        if s["o"] and (s.get("x") or s.get("xs")):       # что движок взял с нашей страницы: фраза ответа и фрагмент страницы
            e["ctx"].append({"p": r["prompt_id"], "c": r["channel"], "t": s.get("x") or "", "s": s.get("xs") or "", "m": with_us})
        e["refs"].add((r["prompt_id"], r["channel"]))
        e["cited"] += 1
        e["prompts"].add(r["prompt_id"])
        e["engines"].add(r["channel"])
        e["with_us"] += with_us
        if len(s.get("title") or "") > len(e["title"]):
            e["title"] = s["title"][:110]


def dclass(d, os_):
    return "own" if d == OUR_SITE else "satellite" if d in OUR_DOMAINS else "pub" if d in BLOGS else "competitor" if d in BRAND_DOMAINS else "other"


sources, REFS = [], {}          # REFS: «d<место>» / «u<место>» -> ответы, где источник процитирован (для провала из вкладки «Источники»)
ranked = sorted(src.values(), key=lambda x: -x["cited"])
for i, e in enumerate(ranked):
    d = e["domain"]
    cls = dclass(d, e["o"])
    if i >= 300 and cls == "other" and not e["pub"] and not e.get("link"):      # хвост чужих сайтов не публикуем, наши — все
        continue
    REFS["d%d" % (i + 1)] = e["refs"]
    sources.append({"rank": i + 1, "domain": d, "class": cls, "brand": BRAND_DOMAINS.get(d), "cited": e["cited"], "prompts": len(e["prompts"]),
                    "engines": sorted(e["engines"]), "ce": e["ce"], "with_us": e["with_us"], "pub": e["pub"], "link": e.get("link", 0)})
urls, OURS = [], {}
for i, e in enumerate(sorted(pages.values(), key=lambda x: -x["cited"])):
    cls = CLS.get(e["o"]) or ("competitor" if e["domain"] in BRAND_DOMAINS else "other")
    if i >= 250 and cls in ("other", "competitor"):
        continue
    REFS["u%d" % (i + 1)] = e["refs"]
    urls.append({"rank": i + 1, "url": e["url"], "title": e["title"], "domain": e["domain"], "class": cls, "cited": e["cited"], "prompts": len(e["prompts"]),
                 "engines": sorted(e["engines"]), "ce": e["ce"], "with_us": e["with_us"]})
    if e["o"]:
        u = OURS.setdefault(str(i + 1), {})         # подробности по нашим страницам — отдельным файлом, чтобы не раздувать список
        cx = sorted(e["ctx"], key=lambda x: (x["m"], -len(x["t"])))          # сначала ответы без своего бренда и с фразой подлиннее
        u["ctx"] = [{"p": x["p"], "c": x["c"], "t": x["t"][:320], "s": x["s"][:220], "m": x["m"]} for x in cx[:4]]
        if nurl(e["url"]) in AUDIT:
            u["au"] = AUDIT[nurl(e["url"])]
SRC_TOTAL = {"domains": len(src), "pages": len(pages)}

# --- нашли и не сослались: сколько раз поиск движка вернул наш источник и в скольких из этих ответов на него есть ссылка
fstat = {}
for r in recs:
    if r["status"] != "ok" or not r.get("found"):
        continue
    cited = {s["domain"] for s in r["sources"] if s["o"]}
    for d, o in {(s["domain"], s["o"]) for s in r["found"] if s["o"]}:
        e = fstat.setdefault(d, {"domain": d, "class": CLS[o], "found": 0, "cited": 0, "e": {}})
        e["found"] += 1
        e["cited"] += 1 if d in cited else 0
        x = e["e"].setdefault(r["channel"], [0, 0])
        x[0] += 1
        x[1] += 1 if d in cited else 0
FOUND_STAT = sorted(fstat.values(), key=lambda x: -x["found"])[:40]
FOUND_CH = sorted({r["channel"] for r in recs if r["status"] == "ok" and r.get("found")}, key=CHANNELS.index)

# --- подзапросы (query fan-out): что движок сам искал, отвечая на вопрос
fan = {}
for r in recs:
    if r["status"] != "ok":
        continue
    for q in r.get("fanout") or []:
        k = re.sub(r"\s+", " ", str(q)).strip().lower()
        if not k:
            continue
        e = fan.setdefault(k, {"q": re.sub(r"\s+", " ", str(q)).strip(), "n": 0, "engines": set(), "prompts": set(), "us": 0, "oc": 0, "of": 0})
        e["oc"] += 1 if (r["our_cited"] or r["pubs"] or r["links"]) else 0
        e["of"] += 1 if r["our_found"] else 0
        e["n"] += 1
        e["engines"].add(r["channel"])
        e["prompts"].add(r["prompt_id"])
        e["us"] += 1 if OUR in r["brands"] else 0
fanout = [{"q": e["q"], "n": e["n"], "engines": sorted(e["engines"]), "prompts": sorted(e["prompts"]), "us": e["us"], "oc": e["oc"], "of": e["of"]}
          for e in sorted(fan.values(), key=lambda x: (-x["n"], x["q"]))]
fan_groups, group, size = [], [], 0
for f in fanout + [None]:
    b = len(json.dumps(f, ensure_ascii=False).encode()) if f else 0
    if group and (f is None or size + b > 100000):
        fan_groups.append(group)
        group, size = [], 0
    if f:
        group.append(f)
        size += b
FAN_STAT = {ch: {"answers": sum(1 for r in recs if r["channel"] == ch and r["status"] == "ok"),
                 "with": sum(1 for r in recs if r["channel"] == ch and r["status"] == "ok" and r.get("fanout")),
                 "queries": sum(len(r.get("fanout") or []) for r in recs if r["channel"] == ch and r["status"] == "ok")} for ch in CHANNELS}

# --- таблица промптов
TOPICS = [(x["key"], re.compile(x["pattern"], re.I)) for x in CFG.get("topics") or []]


def topic(t):
    """Тема вопроса по ключевым словам из настроек (topics): первая подошедшая. Считается при сводке, поэтому разрез меняется без пересъёма."""
    for k, pat in TOPICS:
        if pat.search(t):
            return k
    return CFG.get("topic_default", "other")


def form(t):
    """Вид запроса: подборка «лучшие», вопрос «как», развёрнутый вопрос или короткий запрос."""
    l = t.lower()
    if len(t) > 90:
        return "long"
    if re.search(r"лучш|порекомендуй|\bbest\b|\btop\b|leading|most reliable|highest", l):
        return "best"
    if re.search(r"^(как|how|should|can)\b", l):
        return "how"
    return "long" if "?" in t or len(t) > 60 else "short"


prompts = {}
for r in recs:
    p = prompts.setdefault(r["prompt_id"], {"id": r["prompt_id"], "text": r["prompt"], "lang": r["lang"], "tags": r.get("tags") or [], "tp": topic(r["prompt"]), "fm": form(r["prompt"]), "cells": {}})
    st = r["status"]
    p["cells"][r["channel"]] = ({"s": st} if st != "ok" else
        {"s": "ok", "m": OUR in r["brands"], "c": OUR_SITE in r["our_cited"], "n": bool(r["sats"]), "u": bool(r["pubs"]), "l": bool(r["links"]), "pos": r["our_position"],
         "f": bool(r["our_found"]), "b": r["brands"][:8], "a": [c["name"] for c in r["companies"] if c["rec"]][:14], "t": r["stance"],
         **({"w": (r.get("why") or r.get("quote") or "")[:240]} if r["stance"] in ("caution", "negative") else {})})
summary = {"run": RUN, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "prompts": len(prompts), "records": len(recs),
           "engines": engines, "cost": round(sum(r["cost"] for r in recs), 2), "parsed": PARSED, "src_total": SRC_TOTAL, "fanout": FAN_STAT, "fan_parts": len(fan_groups)}
PIDX = {pid: i for i, pid in enumerate(sorted(prompts))}
ref_groups, group, size = [], {}, 0
for k, v in REFS.items():               # компактно: «номер вопроса + буква движка», через пробел
    s = " ".join("%d%s" % (PIDX[p], chr(97 + CHANNELS.index(c))) for p, c in sorted(v))
    if group and size + len(s) > 95000:
        ref_groups.append(group)
        group, size = {}, 0
    group[k] = s
    size += len(s) + 12
ref_groups.append(group)
summary["ref_parts"] = len(ref_groups)
summary["found_stat"], summary["found_channels"] = FOUND_STAT, FOUND_CH
summary["channels"] = CHANNELS

# --- оценка бренда (промпт Б): среднее по повторам для каждой модели, режима и ICP
CRIT = list(dict.fromkeys(CFG.get("criteria") or []) | {"overall": None})
bfiles, cur = [], None
while True:
    l = tool("app_storage", "storage_list", {"storage": "files", "prefix": "brand/%s/" % RUN, "limit": 100, **({"cursor": cur} if cur else {})})
    bfiles += [o["path"] for o in (l.get("objects") or l.get("files") or [])]
    cur = l.get("next")
    if not cur:
        break


def num(v):
    try:
        return float(v)
    except Exception:
        return None


groups = {}
for path in sorted(bfiles):
    d = json.loads(read(path))
    if not d.get("rows"):
        continue
    g = groups.setdefault((d["model"], d["mode"], d["icp"]), {"n": 0, "src": d.get("source", "flymyai"), "p": {}, "searches": [], "runs": []})
    g["n"] += 1
    g["searches"].append(int(d.get("searches") or 0))
    cited = {nurl(u) for u in d.get("cited") or []}
    srcs = [str(u) for u in d.get("sources") or []][:150]
    g["runs"].append({"queries": [str(q)[:200] for q in d.get("queries") or []][:80], "sources": [{"u": u, "ok": nurl(u) in cited} for u in srcs],
                      "cited": len(cited) if d.get("cited") is not None else None})
    for x in d["rows"]:
        nm = canon(str(x.get("platform", "")))
        e = g["p"].setdefault(nm, {"s": {c: [] for c in CRIT}, "known": [], "why": [], "con": []})
        for c in CRIT:
            v = num(x.get(c))
            if v is not None:
                e["s"][c].append(v)
        e["known"].append(bool(x.get("known")))
        if nm == OUR:
            e["why"].append(str(x.get("reasoning") or "")[:400])
            e["con"].append(str(x.get("concerns") or "")[:400])
brand = []
for (model, mode, icp), g in sorted(groups.items()):
    rows = []
    for nm, e in g["p"].items():
        row = {"platform": nm, "known": round(sum(e["known"]) / max(1, len(e["known"])), 2)}
        for c in CRIT:
            row[c] = round(sum(e["s"][c]) / len(e["s"][c]), 2) if e["s"][c] else None
        if nm == OUR:
            row["why"], row["con"] = e["why"], e["con"]
        rows.append(row)
    rows.sort(key=lambda x: -(x["overall"] or 0))
    brand.append({"model": model, "mode": mode, "icp": icp, "n": g["n"], "src": g["src"], "searches": g["searches"], "runs": g["runs"], "rows": rows})
write("runs/%s/brand.json" % RUN, {"run": RUN, "brand": brand})

# --- хранилище: общий индекс замеров
index = exists_read(STATE + "/index.json", {"runs": []})
index["runs"] = sorted([x for x in index["runs"] if x["run"] != RUN] + [summary], key=lambda x: x["run"])
write(STATE + "/index.json", index)
write("runs/%s/summary.json" % RUN, {"summary": summary})

# --- сайт. Крупные файлы инструмент публикации не принимает, поэтому ответы режем на части до ~110 КБ
# и публикуем несколькими версиями подряд; страница догружает части по клику.
LIM = 110000


def js(var_expr, obj):
    return var_expr % json.dumps(obj, ensure_ascii=False)


parts, files = {}, []
for ch in CHANNELS:
    ans = [(r["prompt_id"], {"text": r.get("text", ""), "model": r.get("model"), "brands": r.get("brands", []), "companies": r.get("companies", []), "stance": r.get("stance"), "quote": r.get("quote"), "why": r.get("why"), "fanout": r.get("fanout", []),
                             "sources": r.get("sources", [])[:40], "found": sorted({s["domain"] for s in r.get("found", [])})[:60], "found_ours": sorted({s["domain"] for s in r.get("found", []) if s.get("o")}),
                             "status": r["status"], "error": r.get("error"), "asked_at": r.get("asked_at")})
           for r in recs if r["channel"] == ch]
    group, size, n = {}, 0, 0
    for pid, a in ans + [(None, None)]:
        b = len(json.dumps(a, ensure_ascii=False).encode()) if a else 0
        if group and (a is None or size + b > LIM):
            files.append({"path": "data/%s_%s_%d.js" % (RUN, ch, n), "content": "window.AV_ANS=window.AV_ANS||{};(function(k,d){window.AV_ANS[k]=Object.assign(window.AV_ANS[k]||{},d)})(%s,%s);" % (
                json.dumps(RUN + "/" + ch), json.dumps(group, ensure_ascii=False))})
            group, size, n = {}, 0, n + 1
        if a:
            group[pid] = a
            size += b
    parts[ch] = n
plist = sorted(prompts.values(), key=lambda p: p["id"])
half = (len(plist) + 1) // 2
files += [
    {"path": "data/%s.js" % RUN, "content": js("window.AV_RUN=window.AV_RUN||{};window.AV_RUN[" + json.dumps(RUN) + "]=%s;", {"parts": parts, "prompts": plist[:half], "sources": []})},
    {"path": "data/%s_p2.js" % RUN, "content": js("window.AV_RUN[" + json.dumps(RUN) + "].prompts.push.apply(window.AV_RUN[" + json.dumps(RUN) + "].prompts,%s);", plist[half:])},
    {"path": "data/%s_brand.js" % RUN, "content": js("window.AV_RUN[" + json.dumps(RUN) + "].brand=%s;", brand)},
    {"path": "data/%s_src.js" % RUN, "content": js("window.AV_RUN[" + json.dumps(RUN) + "].sources=%s;", sources)},
    {"path": "data/%s_urls.js" % RUN, "content": js("window.AV_RUN[" + json.dumps(RUN) + "].urls=%s;", urls)},
    {"path": "data/%s_ours.js" % RUN, "content": js("window.AV_RUN[" + json.dumps(RUN) + "].ours=%s;", OURS)},
]
for i, g in enumerate(fan_groups):          # подзапросов тысячи — отдельными частями, как ответы
    files.append({"path": "data/%s_fan_%d.js" % (RUN, i), "content": js("(function(r){r.fanout=(r.fanout||[]).concat(%s)})(window.AV_RUN[" + json.dumps(RUN) + "]);", g)})
for i, g in enumerate(ref_groups):
    files.append({"path": "data/%s_refs_%d.js" % (RUN, i), "content": js("(function(r){r.refs=Object.assign(r.refs||{},%s)})(window.AV_RUN[" + json.dumps(RUN) + "]);", g)})
for path, b64 in (CFG.get("static") or {}).items():       # index.html, app.js, значки, настройки страницы
    files.append({"path": path, "content": base64.b64decode(b64).decode("utf-8")})
files.append({"path": "data/index.js", "content": "window.AV_INDEX=" + json.dumps(index, ensure_ascii=False) + ";"})   # индекс последним: страница увидит замер, когда все его файлы на месте

site = exists_read(STATE + "/site.json", None)
batches, cur, size = [], [], 0
for f in files:
    b = len(f["content"].encode())
    if cur and size + b > LIM:
        batches.append(cur)
        cur, size = [], 0
    cur.append(f)
    size += b
batches.append(cur)
sid, ver, url = (site or {}).get("site_id"), None, (site or {}).get("owner_url")
for bt in batches:
    if not sid:
        if not any(f["path"] == "index.html" for f in bt):      # первая версия нового сайта обязана содержать index.html
            bt = bt + [{"path": "index.html", "content": "<!doctype html><title>…</title>"}]
        r = tool("sites", "site_publish", {"name": CFG["site_name"], "files": bt})
    else:
        if ver is None:
            g = tool("sites", "site_get", {"site_id": sid})
            ver = g.get("live_version") or (g.get("site") or {}).get("live_version")
        r = tool("sites", "site_publish", {"site_id": sid, "base_version": ver, "files": bt})
    s = r.get("site", r)
    sid, ver, url = s.get("id") or s.get("site_id") or sid, s.get("live_version") or ((ver or 0) + 1), s.get("owner_url") or url
site = {"site_id": sid, "owner_url": url, "live_version": ver}
write(STATE + "/site.json", site)
print(json.dumps({"run": RUN, "records": len(recs), "site": site, "bytes": sum(len(f["content"]) for f in files), "publishes": len(batches),
                  "engines": {c: {k: e[k] for k in ("answers", "no_answer", "errors", "mentions", "citations", "network", "sov", "position")} for c, e in engines.items()}}, ensure_ascii=False))
