import os, re, json, time, threading, collections, requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button

KEY = "your_api_key_here"
SM = "https://www.thesuper.market/api/v1"

SM_EVERY = 2.0
POLY_EVERY = 1.0
TRADES_EVERY = 6.0
ACCT_EVERY = 20.0
DRAW_EVERY = 1.0
HISTORY = 20000
WINDOW_MIN = 1
TICK = 0.005
MARKOUT_S = 60
TRADE_WIN_S = 300

NEWS_WINDOW = True
POLY_MAP = {
    "377": ("will-mary-peltola-win-the-alaska-senate-race-in-2026", "will-dan-sullivan-win-the-alaska-senate-race-in-2026"),
}
MANUAL_PAIRS = [("377", "378")]
MAX_GROUPS = None
SHOW_DEBUG = False
RD = re.compile(r"\b(democrat(?:ic|s)?|dems?)\b(?:\s+party)?", re.I)
RR = re.compile(r"\b(republican(?:s)?|gop)\b(?:\s+party)?", re.I)
STOP = {"will", "the", "party", "win", "wins", "winner", "a", "an", "of", "in", "to", "be", "for", "by", "on", "is",
        "and", "or", "at", "as", "who", "which", "next", "election", "race"}
SEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selected_market.json")

NAN = float("nan")
READS = collections.deque()
DEBUG_SEEN = set()

def n(x): return NAN if x is None else x

def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
    return default

def to_ts(v):
    if v is None: return None
    try:
        if isinstance(v, (int, float)):
            return v / 1000 if v > 1e11 else float(v)
        s = re.sub(r"(\.\d{6})\d+", r"\1", str(v).replace("Z", "+00:00"))
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None

def debug_once(tag, obj):
    if tag not in DEBUG_SEEN:
        DEBUG_SEEN.add(tag)
        print(f"[DEBUG {tag}] {obj}", flush=True)

def sm_session():
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {KEY.strip()}"
    return s

def sm_get(s, path, **p):
    for _ in range(5):
        READS.append(time.time())
        try:
            r = s.get(SM + path, params=p, timeout=30)
        except (requests.ReadTimeout, requests.ConnectionError):
            time.sleep(1); continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30))); continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(path)

def side_info(s, mid):
    m = sm_get(s, f"/markets/{mid}")
    t = m["contexts"][0]["tournament"]
    return {"ex": m["exchanges"][0]["id"], "tid": t["id"] if t else None,
            "settle": to_ts(m.get("settlementDate"))}

def tp(side):
    return {"tournamentId": side["tid"]} if side["tid"] else {}

def sm_book(s, side):
    b = sm_get(s, f"/exchanges/{side['ex']}/orderbook", depth=10, **tp(side))
    bids, asks = b.get("bids", []), b.get("asks", [])
    lad_b = sorted(((float(x["price"]), float(x["quantity"])) for x in bids), key=lambda t: -t[0])
    lad_a = sorted(((float(x["price"]), float(x["quantity"])) for x in asks), key=lambda t: t[0])
    bid = b.get("bestBid", bids[0]["price"] if bids else None)
    ask = b.get("bestAsk", asks[0]["price"] if asks else None)
    b3 = sum(x["quantity"] for x in bids[:3]); a3 = sum(x["quantity"] for x in asks[:3])
    return {"bid": bid, "ask": ask, "lb": lad_b, "la": lad_a,
            "bsz": bids[0]["quantity"] if bids else 0, "asz": asks[0]["quantity"] if asks else 0,
            "imb": (b3 - a3) / (b3 + a3) if (b3 + a3) else 0.0}

def poly_token(slug):
    g = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=30).json()[0]
    return json.loads(g["clobTokenIds"])[json.loads(g["outcomes"]).index("Yes")]

def poly_book(ps, token):
    b = ps.get("https://clob.polymarket.com/book", params={"token_id": token}, timeout=10).json()
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    if not bids or not asks:
        return None
    bb, ba = max(bids), min(asks)
    gb = max(bids, key=lambda x: (x[1], x[0]))
    ga = max(asks, key=lambda x: (x[1], -x[0]))
    return {"bid": bb[0], "bsz": bb[1], "ask": ba[0], "asz": ba[1], "t": time.time(),
            "gb": gb[0], "gbs": gb[1], "ga": ga[0], "gas": ga[1]}

def micro(p):
    return (p["bid"] * p["asz"] + p["ask"] * p["bsz"]) / (p["bsz"] + p["asz"])

def parse_list(js, *names):
    if isinstance(js, list): return js
    if isinstance(js, dict):
        for k in ("data",) + names:
            if isinstance(js.get(k), list): return js[k]
    return []

def parse_trades(js):
    items = parse_list(js, "trades")
    if items: debug_once("TRADE", items[0])
    out = []
    for t in items:
        pr = pick(t, "price")
        ts = to_ts(pick(t, "createdAt", "executedAt", "timestamp", "time", "at", "tradedAt"))
        if pr is None or ts is None: continue
        out.append({"p": float(pr), "q": float(pick(t, "quantity", "size", "qty", "shares", default=0) or 0), "t": ts})
    return out

def market_info(m):
    exs = [{"id": str(e["id"]), "option": str(e.get("option", "")), "last": e.get("latestPrice")}
           for e in (m.get("exchanges") or [])]
    if not exs:
        return None
    ctxs = m.get("contexts") or [{}]
    t = ctxs[0].get("tournament")
    return {"id": str(m["id"]), "title": m.get("title", ""), "exs": exs, "tid": t["id"] if t else None,
            "settle": str(m.get("settlementDate") or "")[:10], "ts": to_ts(m.get("settlementDate"))}


def discover(s):
    out, cursor = [], None
    for _ in range(20):
        p = {"limit": 100, "status": "open"}
        if cursor:
            p["cursor"] = cursor
        js = sm_get(s, "/markets", **p)
        out += js.get("data", [])
        pg = js.get("pagination", {})
        cursor = pg.get("nextCursor")
        if not (pg.get("hasMore") and cursor):
            if pg.get("hasMore"):
                print("Hinweis: weitere Seiten vorhanden, aber kein nextCursor geliefert", flush=True)
            break
    return out


def party_key(title):
    d, r = bool(RD.search(title)), bool(RR.search(title))
    if d == r:
        return None, None
    t = RD.sub(" ", title) if d else RR.sub(" ", title)
    return ("D" if d else "R"), " ".join(w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP)


def toks(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOP}


def leg(info, ex, label):
    return {"ex": ex["id"], "tid": info["tid"], "mid": info["id"], "label": label, "settle": info.get("ts"), "title": info["title"], "last": ex.get("last")}


def last_sum(infos):
    v = [i["exs"][0]["last"] for i in infos]
    return sum(v) if all(x is not None for x in v) else None


def build_groups(s):
    infos = [i for i in (market_info(m) for m in discover(s)) if i]
    groups, used = [], set()

    for i in infos:
        if len(i["exs"]) >= 2:
            lg = [leg(i, e, e["option"][:10]) for e in i["exs"]]
            groups.append({"name": i["title"], "tier": "multi", "legs": lg, "titles": [i["title"]],
                           "gap": abs(sum(e["last"] or 0 for e in i["exs"]) - 1)})
            used.add(i["id"])
    single = [i for i in infos if i["id"] not in used]

    by_key, unmatched = collections.defaultdict(lambda: {"D": [], "R": []}), []
    for i in single:
        side, key = party_key(i["title"])
        if side is None:
            unmatched.append(i)
        else:
            by_key[key][side].append(i)
    leftovers = list(unmatched)
    for key, g in by_key.items():
        if len(g["D"]) == 1 and len(g["R"]) == 1:
            d, r = g["D"][0], g["R"][0]
            ls = last_sum([d, r])
            groups.append({"name": key or d["title"], "tier": "D/R", "legs": [leg(d, d["exs"][0], "D"), leg(r, r["exs"][0], "R")],
                           "titles": [d["title"], r["title"]], "gap": abs(ls - 1) if ls is not None else 9.0})
            used.update([d["id"], r["id"]])
        else:
            leftovers += g["D"] + g["R"]

    def score(a, b):
        ta, tb = toks(a["title"]), toks(b["title"])
        inter = len(ta & tb)
        if inter < 2 or (a["settle"] and b["settle"] and a["settle"] != b["settle"]):
            return None
        ls = last_sum([a, b])
        if ls is None or abs(ls - 1) > 0.2:
            return None
        jac = inter / len(ta | tb)
        return None if jac < 0.25 else jac - abs(ls - 1)

    best = {}
    for a in leftovers:
        cand = [(sc, b) for b in leftovers if b is not a for sc in [score(a, b)] if sc is not None]
        if cand:
            best[a["id"]] = max(cand, key=lambda x: x[0])[1]
    done = set()
    for a in leftovers:
        b = best.get(a["id"])
        if b and best.get(b["id"]) is a and a["id"] not in done and b["id"] not in done:
            done.update([a["id"], b["id"]])
            ls = last_sum([a, b])
            groups.append({"name": " ".join(sorted(toks(a["title"]) & toks(b["title"]))), "tier": "~",
                           "legs": [leg(a, a["exs"][0], "A"), leg(b, b["exs"][0], "B")],
                           "titles": [a["title"], b["title"]], "gap": abs(ls - 1)})

    groups.sort(key=lambda g: g["gap"])
    known = {tuple(l["mid"] for l in g["legs"]) for g in groups}
    manual = []
    for dm, rm in MANUAL_PAIRS:
        hit = next((g for g in groups if tuple(l["mid"] for l in g["legs"]) in ((dm, rm), (rm, dm))), None)
        if hit is not None:
            groups.remove(hit)
            manual.append(hit)
            continue
        try:
            d, r = market_info(sm_get(s, f"/markets/{dm}")), market_info(sm_get(s, f"/markets/{rm}"))
            manual.append({"name": party_key(d["title"])[1] or d["title"], "tier": "D/R",
                           "legs": [leg(d, d["exs"][0], "D"), leg(r, r["exs"][0], "R")], "titles": [d["title"], r["title"]], "gap": 0})
        except Exception as e:
            print("Manuelles Paar", dm, rm, "nicht ladbar:", e, flush=True)
    out = manual + groups
    if MAX_GROUPS:
        out = out[:MAX_GROUPS]
    n = collections.Counter(g["tier"] for g in out)
    print(f"{len(infos)} offene Maerkte -> {len(out)} Gruppen beobachtet: {n['D/R']} Democrat/Republican-Paare, "
          f"{n['~']} Titel-Aehnlichkeits-Paare (~), {n['multi']} Multi-Outcome-Maerkte", flush=True)
    if SHOW_DEBUG:
        for g in out:
            print(f"  [{g['tier']}] " + "  ||  ".join(t[:60] for t in g["titles"]), flush=True)
        rest = [i["title"] for i in infos if i["id"] not in used and i["id"] not in done
                and not any(i["id"] in tuple(l["mid"] for l in g["legs"]) for g in out)]
        if rest:
            print(f"{len(rest)} Maerkte ohne Partner, Beispiele:", flush=True)
            for t in rest[:15]:
                print("    ", t, flush=True)
    return out


def build_catalog(s):
    return [g for g in build_groups(s) if len(g["legs"]) == 2 and g["tier"] != "multi"]


print("Setup ...", flush=True)
s0 = sm_session()
CAT = build_catalog(s0)
if not CAT:
    raise SystemExit("Keine Paare gefunden.")
CUR = [0]
GEN = [0]
SIDE = {"D": None, "R": None}
TOK = {"D": None, "R": None}
PINFO = {"name": "", "poly": False, "label": "suche ..."}
NOPO = {"bid": NAN, "bsz": NAN, "ask": NAN, "asz": NAN, "t": 0.0, "gb": NAN, "gbs": NAN, "ga": NAN, "gas": NAN}

LOCK = threading.Lock()
POLYC = {}
GAMMA = "https://gamma-api.polymarket.com"

def _jl(x):
    if isinstance(x, str):
        try: return json.loads(x)
        except Exception: return []
    return x or []

def pm_yes(m):
    try:
        outs, toks_, pr = _jl(m.get("outcomes")), _jl(m.get("clobTokenIds")), _jl(m.get("outcomePrices"))
        i = [str(o).lower() for o in outs].index("yes")
        return toks_[i], float(pr[i])
    except Exception:
        return None

def poly_find(pair):
    d, r = pair["legs"]
    qd, qr = toks(RD.sub(" ", RR.sub(" ", d["title"]))), toks(RD.sub(" ", RR.sub(" ", r["title"])))
    qw = [w for w in re.findall(r"[a-z0-9]+", RD.sub(" ", RR.sub(" ", d["title"])).lower()) if w in (qd & qr)]
    if not qw:
        return None
    js = requests.get(GAMMA + "/public-search", params={"q": " ".join(qw), "limit_per_type": 8}, timeout=20).json()
    evs = []
    for e in js.get("events", []):
        if e.get("closed") or e.get("active") is False:
            continue
        ew = toks((e.get("title") or "") + " " + (e.get("slug") or "").replace("-", " "))
        cov = len(set(qw) & ew) / len(set(qw))
        if cov >= 0.75:
            evs.append((cov, e))
    evs.sort(key=lambda x: -x[0])
    lastD, lastR = d.get("last"), r.get("last")
    for cov, e in evs:
        ms = [(m, pm_yes(m)) for m in e.get("markets", []) if not m.get("closed") and m.get("active") is not False]
        ms = [(m, y) for m, y in ms if y]
        name = lambda m: (m.get("groupItemTitle") or m.get("question") or "?")[:26]
        if pair["tier"] == "D/R":
            pd_ = [(m, y) for m, y in ms if RD.search(m.get("question", "")) and not RR.search(m.get("question", ""))]
            pr_ = [(m, y) for m, y in ms if RR.search(m.get("question", "")) and not RD.search(m.get("question", ""))]
            if len(pd_) == 1 and len(pr_) == 1:
                (m1, y1), (m2, y2) = pd_[0], pr_[0]
                if all(l is None or abs(y - l) < 0.2 for y, l in ((y1[1], lastD), (y2[1], lastR))):
                    return {"D": y1[0], "R": y2[0], "label": f"{name(m1)} / {name(m2)}"}
        if len(ms) >= 2 and lastD is not None and lastR is not None:
            top = sorted(ms, key=lambda x: -x[1][1])[:2]
            (m1, y1), (m2, y2) = top
            straight = abs(y1[1] - lastD) + abs(y2[1] - lastR)
            swapped = abs(y2[1] - lastD) + abs(y1[1] - lastR)
            lo, hi = min(straight, swapped), max(straight, swapped)
            if lo / 2 <= 0.12 and hi - lo >= 0.05:
                if straight <= swapped:
                    return {"D": y1[0], "R": y2[0], "label": f"{name(m1)} / {name(m2)}"}
                return {"D": y2[0], "R": y1[0], "label": f"{name(m2)} / {name(m1)}"}
    return None

def resolve_poly(g, i):
    p = CAT[i]; key = p["legs"][0]["mid"]
    try:
        if key in POLYC:
            res = POLYC[key]
        else:
            if key in POLY_MAP:
                sl = POLY_MAP[key]
                res = {"D": poly_token(sl[0]), "R": poly_token(sl[1]), "label": "POLY_MAP"}
            else:
                res = poly_find(p)
            POLYC[key] = res
    except Exception as e:
        print("Polymarket-Suche fehlgeschlagen:", str(e)[:100], flush=True)
        res = None
    with LOCK:
        if g != GEN[0]:
            return
        if res:
            TOK.update(D=res["D"], R=res["R"]); PINFO.update(poly=True, label=res["label"])
        else:
            PINFO.update(poly=False, label="kein Polymarket-Markt gefunden")
        PINFO["dirty"] = True
    print(f"   Theo: {'Polymarket ' + res['label'] if res else 'keiner (nichts gefunden)'}", flush=True)

def apply_pair(i):
    p = CAT[i]
    d, r = p["legs"]
    new_side = {k: {"ex": l["ex"], "tid": l["tid"], "settle": l["settle"]} for k, l in (("D", d), ("R", r))}
    with LOCK:
        GEN[0] += 1
        SIDE.update(new_side); TOK.update(D=None, R=None)
        PINFO.update(name=p["name"], poly=False, label="suche Polymarket ...")
        PINFO["dirty"] = True
        CUR[0] = i
        g = GEN[0]
    print(f"Markt [{i + 1}/{len(CAT)}] {p['name']}", flush=True)
    threading.Thread(target=resolve_poly, args=(g, i), daemon=True).start()

apply_pair(0)
print("Setup fertig, Fenster oeffnet sich", flush=True)

S = {"sm": {"D": None, "R": None}, "sm_t": 0.0, "poly": {"D": None, "R": None},
     "trades": {"D": [], "R": []}, "pos": [], "bal": None, "newfills": []}
TPTS = {"D": [], "R": []}
TSEEN = set()

def loop_every(period, fn, label):
    def run():
        while True:
            t = time.time()
            try:
                fn()
            except Exception as e:
                print(f"{label} Fehler:", e, flush=True)
            time.sleep(max(0, period - (time.time() - t)))
    threading.Thread(target=run, daemon=True).start()

_sm_s, _ex = sm_session(), ThreadPoolExecutor(4)
def sm_job():
    g, sd = GEN[0], dict(SIDE)
    fs = {k: _ex.submit(sm_book, _sm_s, sd[k]) for k in "DR"}
    res = {k: f.result() for k, f in fs.items()}
    with LOCK:
        if g == GEN[0]:
            S["sm"].update(res); S["sm_t"] = time.time()

_ps = requests.Session()
def poly_job():
    g, tk = GEN[0], dict(TOK)
    if tk["D"] is None or tk["R"] is None:
        return
    fs = {k: _ex.submit(poly_book, _ps, tk[k]) for k in "DR"}
    res = {k: f.result() for k, f in fs.items()}
    with LOCK:
        if g == GEN[0]:
            for k, v in res.items():
                if v: S["poly"][k] = v

_tr_s = sm_session()
def trades_job():
    g, sd = GEN[0], dict(SIDE)
    for k in "DR":
        js = sm_get(_tr_s, f"/exchanges/{sd[k]['ex']}/trades", limit=50, **tp(sd[k]))
        tr = parse_trades(js)
        with LOCK:
            if g == GEN[0]: S["trades"][k] = tr

_ac_s, _seen, _first = sm_session(), set(), [True]
def acct_job():
    acc = sm_get(_ac_s, "/account")
    bal = pick(acc, "balance", "cashBalance", "cash", "availableBalance")
    pos = parse_list(sm_get(_ac_s, "/portfolio/positions"), "positions")
    if pos: debug_once("POSITION", pos[0])
    fl = parse_list(sm_get(_ac_s, "/portfolio/fills", limit=50, **tp(SIDE["D"])), "fills")
    new = []
    for f in fl:
        fid = pick(f, "id", "fillId", "tradeId") or json.dumps(f, sort_keys=True)[:200]
        if fid in _seen: continue
        _seen.add(fid)
        if not _first[0]: new.append(f)
    _first[0] = False
    with LOCK:
        S["bal"], S["pos"] = bal, pos
        S["newfills"].extend(new)

loop_every(SM_EVERY, sm_job, "SM")
loop_every(POLY_EVERY, poly_job, "Poly")
loop_every(TRADES_EVERY, trades_job, "Trades")
loop_every(ACCT_EVERY, acct_job, "Konto")

t0 = time.time()
X = collections.deque(maxlen=HISTORY)
keys = [f"{a}{b}{c}" for a in "ps" for b in "DR" for c in ("b", "a")]
H = {k: collections.deque(maxlen=HISTORY) for k in keys}
THEO = collections.deque(maxlen=HISTORY)
PEND, MARK = [], collections.deque(maxlen=100)


MAXN = 20000
XA = np.full(MAXN, np.nan)
HA = {k: np.full(MAXN, np.nan) for k in keys + ["tD", "tR", "zBD", "zSD", "zBR", "zSR"]}
THR = [TICK]
CNT = [0]

def push(xm, vals):
    i = CNT[0]
    if i >= MAXN:
        XA[:-1] = XA[1:]
        for a in HA.values():
            a[:-1] = a[1:]
        i = MAXN - 1
    XA[i] = xm
    for k, v in vals.items():
        HA[k][i] = v
    CNT[0] = i + 1

GREEN, RED, AMBER, TXT, HEADC, DIM = "#22c55e", "#ef4444", "#facc15", "#e5e7eb", "#60a5fa", "#9ca3af"
THEOC = "#facc15"

plt.style.use("dark_background")
for _k in list(plt.rcParams):
    if _k.startswith("keymap."):
        plt.rcParams[_k] = []
fig = plt.figure(figsize=(17, 9.5))
try:
    fig.canvas.manager.set_window_title("Dashboard")
except Exception:
    pass
SUP = fig.suptitle("", fontsize=10, y=0.985, x=0.70)

def set_title_text():
    p = CAT[CUR[0]]
    SUP.set_text(f"[{CUR[0] + 1}/{len(CAT)}] {p['name'][:46]}   |   Markt {p['legs'][0]['mid']}/{p['legs'][1]['mid']}   |   Theo: "
                 + (("Polymarket (" + PINFO["label"][:40] + ")") if PINFO["poly"] else ("kein Theo" if "kein" in PINFO["label"] else PINFO["label"])))
gs = fig.add_gridspec(3, 3, width_ratios=[3, 0.95, 1.25], height_ratios=[3, 3, 2.4])
axD = fig.add_subplot(gs[0, 0])
axR = fig.add_subplot(gs[1, 0], sharex=axD)
axX = fig.add_subplot(gs[2, 0], sharex=axD)
axLD = fig.add_subplot(gs[0, 1], sharey=axD)
axLR = fig.add_subplot(gs[1, 1], sharey=axR)
axT = fig.add_subplot(gs[:, 2]); axT.axis("off")

STATE = {"live": True, "theo": True, "goto": None, "typing": False, "q": ""}

bax = fig.add_axes([0.01, 0.945, 0.11, 0.035]); bax.set_in_layout(False)
btn = Button(bax, "LIVE", color="#1f3b2d", hovercolor="#2f5b44")
btn.on_clicked(lambda _: STATE.update(live=True))
tax = fig.add_axes([0.13, 0.945, 0.11, 0.035]); tax.set_in_layout(False)
tbtn = Button(tax, "THEO AN", color="#3b3414", hovercolor="#5a4f1c")
def toggle_theo(_=None):
    STATE["theo"] = not STATE["theo"]
tbtn.on_clicked(toggle_theo)

pax = fig.add_axes([0.25, 0.945, 0.035, 0.035]); pax.set_in_layout(False)
pbtn = Button(pax, "<", color="#222222", hovercolor="#444444")
nax = fig.add_axes([0.29, 0.945, 0.035, 0.035]); nax.set_in_layout(False)
nbtn = Button(nax, ">", color="#222222", hovercolor="#444444")
pbtn.on_clicked(lambda _: STATE.update(goto=(CUR[0] - 1) % len(CAT)))
nbtn.on_clicked(lambda _: STATE.update(goto=(CUR[0] + 1) % len(CAT)))

sax = fig.add_axes([0.34, 0.945, 0.20, 0.035]); sax.set_in_layout(False)
sax.set_xticks([]); sax.set_yticks([]); sax.set_facecolor("#111111")
stxt = sax.text(0.03, 0.5, "", transform=sax.transAxes, va="center", ha="left", fontsize=9, family="monospace", color=DIM)

def show_search():
    if STATE["typing"]:
        stxt.set_text("> " + STATE["q"] + "_"); stxt.set_color(TXT); sax.set_facecolor("#1d2a3a")
    else:
        stxt.set_text(STATE.get("msg") or STATE["q"] or "Suche: Klick oder /  (Name, ID, #Nr)"); stxt.set_color(DIM); sax.set_facecolor("#111111")
    fig.canvas.draw_idle()

def on_search(txt):
    q = txt.strip().lower()
    STATE["msg"] = ""
    if not q:
        return
    hit = None
    ids = [x for x in re.split(r"[/,\s]+", q) if x.isdigit()]
    if ids:
        for i, p in enumerate(CAT):
            if set(ids) & ({l["mid"] for l in p["legs"]} | {l["ex"] for l in p["legs"]}):
                hit = i; break
    if hit is None and q.startswith("#") and q[1:].isdigit() and 1 <= int(q[1:]) <= len(CAT):
        hit = int(q[1:]) - 1
    if hit is None and q.isdigit() and 1 <= int(q) <= len(CAT):
        hit = int(q) - 1
    if hit is None:
        words = q.lstrip("#").split()
        best = (0, None)
        for i, p in enumerate(CAT):
            hay = (p["name"] + " " + " ".join(p["titles"])).lower()
            k = sum(w in hay for w in words)
            if k == len(words): best = (99, i); break
            if k > best[0]: best = (k, i)
        hit = best[1]
    if hit is None:
        STATE["msg"] = "nichts gefunden: " + q[:20]
        print("Suche: nichts gefunden fuer", q, flush=True)
    else:
        STATE["goto"] = hit
        print(f"Suche '{q}' -> Paar {hit + 1}: {CAT[hit]['name'][:50]}", flush=True)

def on_click(ev):
    STATE["typing"] = ev.inaxes is sax
    if STATE["typing"]:
        STATE["q"] = ""; STATE["msg"] = ""
    show_search()
fig.canvas.mpl_connect("button_press_event", on_click)

def on_key(ev):
    k = ev.key or ""
    if STATE["typing"]:
        if k == "enter":
            STATE["typing"] = False; on_search(STATE["q"])
        elif k == "escape":
            STATE["typing"] = False; STATE["q"] = ""
        elif k == "backspace":
            STATE["q"] = STATE["q"][:-1]
        elif k == "space":
            STATE["q"] += " "
        elif len(k) == 1:
            STATE["q"] += k
        show_search()
        return
    if k == "/":
        STATE["typing"] = True; STATE["q"] = ""; STATE["msg"] = ""; show_search()
    elif k == "right":
        STATE["goto"] = (CUR[0] + 1) % len(CAT)
    elif k == "left":
        STATE["goto"] = (CUR[0] - 1) % len(CAT)
    elif k == "t":
        toggle_theo()
fig.canvas.mpl_connect("key_press_event", on_key)
show_search()

def console_loop():
    while True:
        try:
            line = input()
        except Exception:
            return
        on_search(line)
threading.Thread(target=console_loop, daemon=True).start()
print("Suche: in der Grafik auf die Suchbox klicken oder / druecken, oder hier in der Konsole tippen + Enter", flush=True)

def on_scroll(ev):
    if ev.inaxes in (axD, axR, axX):
        STATE["live"] = False
        lo, hi = axX.get_xlim(); w = hi - lo; sh = -w * 0.15 * ev.step
        axX.set_xlim(max(-w * 0.1, lo + sh), hi + sh)
fig.canvas.mpl_connect("scroll_event", on_scroll)

def on_release(ev):
    tb = getattr(fig.canvas, "toolbar", None)
    if tb is not None and str(getattr(tb, "mode", "")) != "":
        STATE["live"] = False
fig.canvas.mpl_connect("button_release_event", on_release)

def make_panel(ax, title, band_col):
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    ax.grid(alpha=0.25); ax.yaxis.tick_right(); ax.tick_params(labelbottom=False)
    pb, = ax.plot([], [], "+", color=GREEN, ms=10, mew=2, label="Bid")
    pa, = ax.plot([], [], "+", color=RED, ms=10, mew=2, label="Ask")
    pm, = ax.plot([], [], "--", color="white", lw=1.2, label="Mid")
    pc, = ax.plot([], [], "o", color="#3b82f6", ms=7, mec="white", mew=0.6, zorder=6, label="Trade")
    pt, = ax.plot([], [], "-", color=THEOC, lw=1.4, drawstyle="steps-post", label="Theo")
    zb, = ax.plot([], [], "^", color="#a3e635", ms=9, mec="white", mew=0.8, zorder=7, label="Signal kaufen")
    zs, = ax.plot([], [], "v", color="#fb923c", ms=9, mec="white", mew=0.8, zorder=7, label="Signal verkaufen")
    sp = ax.text(1.0, 1.02, "", transform=ax.transAxes, ha="right", va="bottom", fontsize=9)
    leg = ax.legend(loc="upper left", fontsize=8, framealpha=0.35)
    return {"ax": ax, "pb": pb, "pa": pa, "pm": pm, "pc": pc, "pt": pt, "zb": zb, "zs": zs, "sp": sp, "leg": leg, "band": None, "col": band_col}

NLV = 10

def make_ladder(ax, title):
    ax.set_title(title, loc="left", fontsize=9, fontweight="bold")
    ax.grid(alpha=0.15, axis="y")
    ax.tick_params(labelleft=False, left=False, labelbottom=False, bottom=False)
    ax.set_xlim(0, 1)
    ra = [Rectangle((0, 0), 0, 0, color=RED, alpha=0.85, lw=0) for _ in range(NLV)]
    rb = [Rectangle((0, 0), 0, 0, color=GREEN, alpha=0.85, lw=0) for _ in range(NLV)]
    for r in ra + rb:
        ax.add_patch(r)
    ta = [ax.text(0, 0, "", va="center", ha="left", fontsize=7.5, color=TXT) for _ in range(NLV)]
    tb = [ax.text(0, 0, "", va="center", ha="left", fontsize=7.5, color=TXT) for _ in range(NLV)]
    for t in ta + tb:
        t.set_clip_on(True); t.set_clip_box(ax.bbox)
    th = ax.axhline(0.5, color=THEOC, lw=1, ls=":")
    return {"ax": ax, "ra": ra, "rb": rb, "ta": ta, "tb": tb, "th": th}

def update_ladder(L, bids, asks, theo, show_theo):
    mx = max([q for _, q in bids + asks] or [1.0])
    h = TICK * 0.8
    for rects, txts, lv in ((L["ra"], L["ta"], asks), (L["rb"], L["tb"], bids)):
        for i in range(NLV):
            if i < len(lv):
                p, q = lv[i]
                rects[i].set_xy((0, p - h / 2)); rects[i].set_width(q); rects[i].set_height(h)
                txts[i].set_position((q + mx * 0.03, p)); txts[i].set_text(f"{q:.0f} @{p:.3f}")
            else:
                rects[i].set_width(0); txts[i].set_text("")
    L["ax"].set_xlim(0, mx * 1.75)
    L["th"].set_ydata([theo if theo == theo else 0.5] * 2); L["th"].set_visible(bool(show_theo))

LD = make_ladder(axLD, "Orderbook D")
LR = make_ladder(axLR, "Orderbook R")

PD = make_panel(axD, "", "tab:cyan")
PR = make_panel(axR, "", "tab:orange")

axX.set_title("Edge vs Polymarket (ueber 0 = handelbar)", loc="left", fontsize=11, fontweight="bold")
axX.grid(alpha=0.25); axX.yaxis.tick_right()
axX.set_xlabel("Minuten seit Start   (Mausrad: scrollen, Toolbar: Pan/Zoom, t: Theo an/aus)", fontsize=8)
axX.axhline(0, color="gray", lw=1)
EL = {
    "Dm": axX.plot([], [], color="tab:cyan", lw=2, label="Democrat Edge (Mid)")[0],
    "Dc": axX.plot([], [], color="tab:cyan", lw=1, ls="--", label="Democrat Edge (konservativ)")[0],
    "Rm": axX.plot([], [], color="tab:orange", lw=2, label="Republican Edge (Mid)")[0],
    "Rc": axX.plot([], [], color="tab:orange", lw=1, ls="--", label="Republican Edge (konservativ)")[0],
}
eleg = axX.legend(loc="upper left", fontsize=8, framealpha=0.35, ncol=2)
EINFO = axX.text(1.0, 1.02, "", transform=axX.transAxes, ha="right", va="bottom", fontsize=9)

NL, STEP = 54, 0.0186
TL = [axT.text(0.0, 1 - i * STEP, "", va="top", family="monospace", fontsize=8.2, transform=axT.transAxes)
      for i in range(NL)]

CLOCK = fig.text(0.985, 0.972, "", ha="right", va="center", fontsize=15, fontweight="bold", family="monospace")

if NEWS_WINDOW:
    try:
        import subprocess, sys, atexit
        npath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_ticker.py")
        if os.path.exists(npath):
            _news = subprocess.Popen([sys.executable, npath])
            atexit.register(_news.terminate)
        else:
            print("news_ticker.py nicht gefunden (muss im selben Ordner liegen)", flush=True)
    except Exception as e:
        print("News-Fenster konnte nicht gestartet werden:", e, flush=True)

fig.tight_layout(rect=[0, 0, 1, 0.94])
plt.ion(); plt.show(block=False)

def f3(x): return "-" if x is None or x != x else f"{x:.3f}"
def fs(x): return "-" if x is None or x != x else f"{x:+.3f}"
def sc(x): return TXT if x is None or x != x or x == 0 else (GREEN if x > 0 else RED)

def slip(arr, lag=3, last=600):
    a = np.array(arr)[-last:]
    if len(a) < lag + 8: return NAN
    d = np.abs(a[lag:] - a[:-lag])
    return float(np.nanpercentile(d, 90)) if np.isfinite(d).any() else NAN

def trade_str(tr, now):
    if not tr: return "keine Daten"
    w = [t for t in tr if now - t["t"] <= TRADE_WIN_S]
    last = max(tr, key=lambda t: t["t"])
    return f"{len(w)} | {sum(t['q'] for t in w):.0f} | {last['p']:.3f} ({now - last['t']:.0f}s)"

def pos_str(ex, pos):
    for p in pos or []:
        if str(pick(p, "exchangeId", "exchange_id")) == str(ex):
            q = pick(p, "quantity", "netQuantity", "size", "shares")
            sd = pick(p, "side", default="")
            av = pick(p, "avgPrice", "averagePrice", "avgCost", "averageCost")
            return f"{sd} {q}" + (f" @ {float(av):.3f}" if av is not None else "")
    return "keine"

def build_sections(sm, po, tD, tR, now, tr, pos, bal, A):
    bD, aD, bR, aR = sm["D"]["bid"], sm["D"]["ask"], sm["R"]["bid"], sm["R"]["ask"]
    has = tD == tD and tR == tR
    age = now - max(po["D"]["t"], po["R"]["t"])
    secs = []
    if STATE.get("reason"):
        secs.append(("SCANNER", [(STATE["reason"][:48], "", AMBER)]))
    secs.append(("THEO (POLYMARKET MICROPRICE)", [("Theo", "kein Polymarket-Markt", DIM), ("Status", PINFO["label"][:22], DIM)] if not has else [
        ("D  micro / mid", f"{tD:.4f} / {(po['D']['bid'] + po['D']['ask']) / 2:.3f}", TXT),
        ("R  micro / mid", f"{tR:.4f} / {(po['R']['bid'] + po['R']['ask']) / 2:.3f}", TXT),
        ("Summe D+R", f"{tD + tR:.3f}", AMBER if abs(tD + tR - 1) > 0.02 else TXT),
        ("Alter der Daten", f"{age:.1f}s", AMBER if age > 5 else TXT)]))
    sa_, sb_ = aD + aR, bD + bR
    thr = THR[0]
    rows, best = [], None
    for k, b_, a_, t_, pk in ((("D", bD, aD, tD, po["D"]), ("R", bR, aR, tR, po["R"])) if has else ()):
        for act, e, ec, sz, px in (("kauf", t_ - a_, pk["bid"] - a_, sm[k]["asz"], a_),
                                   ("verk", b_ - t_, b_ - pk["ask"], sm[k]["bsz"], b_)):
            rows.append((f"{k} {act} @{px:.3f} x{sz:.0f}", f"{e:+.3f} / {ec:+.3f}", GREEN if ec > 0 else AMBER if e > thr else TXT))
            if e > thr and (best is None or e > best[0]): best = (e, k, act, sz)
    rows.append(("Arb kauf beide (1-Ask)", f"{1 - sa_:+.3f}", GREEN if sa_ < 1 else TXT))
    rows.append(("Arb verk beide (Bid-1)", f"{sb_ - 1:+.3f}", GREEN if sb_ > 1 else TXT))
    rows.append(("Schwelle (Min-Edge)", f3(thr), AMBER))
    rows.append(("BESTES", f"{best[1]} {best[2]} {best[0]:+.3f} x{best[3]:.0f}" if best else ("keine Ineffizienz" if has else "kein Theo"),
                 GREEN if best else DIM))
    secs.append(("INEFFIZIENZ (EDGE THEO / KONS.)", rows))
    rows = []
    for k, b, a, t in (("D", bD, aD, tD), ("R", bR, aR, tR)):
        eb, ea = t - (b + TICK), (a - TICK) - t
        rows.append((f"{k} Spread / Edge bid,ask", f"{a - b:.3f} / {fs(eb)} {fs(ea)}", GREEN if max(eb, ea) > 0.01 else TXT))
    secs.append(("SPREAD & QUOTE-EDGE vs THEO (+1 TICK)", rows))
    rows = []
    for k, b, a, ob, oa in (("D", bD, aD, bR, aR), ("R", bR, aR, bD, aD)):
        mb, ma = 1 - oa - TICK, 1 - ob + TICK
        rows.append((f"{k} Bid max (jetzt {b:.3f})", f"{mb:.3f}  {'PLATZ' if mb > b else '-'}", GREEN if mb > b else TXT))
        rows.append((f"{k} Ask min (jetzt {a:.3f})", f"{ma:.3f}  {'PLATZ' if ma < a else '-'}", GREEN if ma < a else TXT))
    secs.append(("HEDGE-QUOTES (UEBER ANDEREN MARKT)", rows))
    secs.append(("BOOK (TOP-STUFE, IMBALANCE 3)", [
        (f"{k} Size bid/ask  Imb", f"{sm[k]['bsz']:.0f} / {sm[k]['asz']:.0f}  {sm[k]['imb']:+.2f}",
         GREEN if sm[k]['imb'] > 0.3 else RED if sm[k]['imb'] < -0.3 else TXT) for k in "DR"]))
    secs.append(("FLUSS (5 MIN)", [(f"{k} Trades | Vol | Last", trade_str(tr[k], now), TXT) for k in "DR"]))
    sl = {x: slip(A[x]) for x in ("sDb", "sDa", "sRb", "sRa")}
    vals = np.array(list(sl.values()))
    mx = np.nanmax(vals) if np.isfinite(vals).any() else NAN
    sug = max(TICK, np.ceil(mx / TICK - 1e-9) * TICK) if mx == mx else NAN
    if sug == sug: THR[0] = sug
    rng = lambda a: (np.nanmax(np.array(a)[-60:]) - np.nanmin(np.array(a)[-60:])) if len(a) > 5 else NAN
    secs.append(("VOLA / HEDGE-SLIPPAGE (3S, P90)", [
        ("D bid / ask", f"{f3(sl['sDb'])} / {f3(sl['sDa'])}", TXT),
        ("R bid / ask", f"{f3(sl['sRb'])} / {f3(sl['sRa'])}", TXT),
        ("Range 60s D / R (Bid)", f"{f3(rng(A['sDb']))} / {f3(rng(A['sRb']))}", TXT)]))
    rows = [("Balance", str(bal), TXT),
            ("Position D", pos_str(SIDE["D"]["ex"], pos), TXT),
            ("Position R", pos_str(SIDE["R"]["ex"], pos), TXT)]
    for k in "DR":
        ms = [x["m"] for x in MARK if x["mk"] == k and x["m"] is not None]
        mv = float(np.mean(ms)) if ms else NAN
        rows.append((f"Markout {MARKOUT_S}s {k} (n={len(ms)})", fs(mv), sc(mv)))
    secs.append(("KONTO / FILLS", rows))
    ds = [(SIDE[k]["settle"] - now) / 86400 for k in "DR" if SIDE[k]["settle"]]
    r = len([t for t in READS if now - t < 60])
    secs.append(("SYSTEM", [
        ("Reads / min", f"{r} / 100", AMBER if r > 90 else TXT),
        ("Settlement in", f"{min(ds):.1f} Tage" if ds else "-", TXT)]))
    return secs

def to_lines(secs):
    L = []
    for title, rows in secs:
        L.append((title, HEADC, "bold"))
        for label, val, col in rows:
            L.append((f"{label:<27}{val:>22}", col, "normal"))
        L.append(("", TXT, "normal"))
    return L

def update_panel(P, xv, b, a, theo, show_theo, bid_now, ask_now, theo_now, trades=()):
    ax = P["ax"]
    P["pc"].set_data([t[0] for t in trades], [t[1] for t in trades])
    mid = (b + a) / 2
    P["pb"].set_data(xv, b); P["pa"].set_data(xv, a); P["pm"].set_data(xv, mid)
    P["pt"].set_data(xv, theo); P["pt"].set_visible(show_theo)
    if P["band"] is not None:
        P["band"].remove()
    P["band"] = ax.fill_between(xv, b, a, color=P["col"], alpha=0.12, step="post", lw=0)
    P["sp"].set_text(f"Bid {bid_now:.3f}   Ask {ask_now:.3f}   Spread {ask_now - bid_now:.3f}   "
                     f"Mid {(bid_now + ask_now) / 2:.4f}   " + ("kein Theo" if theo_now != theo_now else f"Theo {theo_now:.4f}" if show_theo else "Theo aus"))
    parts = [b, a] + ([theo] if show_theo else [])
    v = np.concatenate(parts); v = v[~np.isnan(v)]
    if v.size:
        pad = max(0.01, (v.max() - v.min()) * 0.25)
        ax.set_ylim(v.min() - pad, v.max() + pad)

def reset_buffers():
    global t0
    XA[:] = np.nan
    for a in HA.values():
        a[:] = np.nan
    CNT[0] = 0
    for k in "DR":
        TPTS[k].clear()
    TSEEN.clear(); THEO.clear(); PEND.clear(); MARK.clear()
    t0 = time.time()
    STATE["live"] = True

def set_panel_titles():
    p = CAT[CUR[0]]
    axD.set_title(p["legs"][0]["title"][:80], loc="left", fontsize=11, fontweight="bold")
    axR.set_title(p["legs"][1]["title"][:80], loc="left", fontsize=11, fontweight="bold")
    axX.set_title(("Edge vs Polymarket" if PINFO["poly"] else "Edge (kein Theo)") + " (ueber 0 = handelbar)",
                  loc="left", fontsize=11, fontweight="bold")
    set_title_text()

def switch_to(i):
    if not STATE.pop("scan_goto", False):
        STATE["reason"] = ""
    with LOCK:
        S["sm"] = {"D": None, "R": None}; S["poly"] = {"D": None, "R": None}
        S["trades"] = {"D": [], "R": []}; S["newfills"].clear(); S["sm_t"] = 0.0
    apply_pair(i)
    reset_buffers()
    set_panel_titles()

SEL_M = [os.path.getmtime(SEL_FILE) if os.path.exists(SEL_FILE) else 0.0]
SEL_CHECK = [0.0]

def check_selection():
    if time.time() - SEL_CHECK[0] < 0.5 or not os.path.exists(SEL_FILE):
        return
    SEL_CHECK[0] = time.time()
    m = os.path.getmtime(SEL_FILE)
    if m == SEL_M[0]:
        return
    SEL_M[0] = m
    try:
        js_sel = json.load(open(SEL_FILE))
        want = {str(x) for x in js_sel.get("mids", [])}
    except Exception:
        return
    for i, p in enumerate(CAT):
        if want & {l["mid"] for l in p["legs"]} or want & {l["ex"] for l in p["legs"]}:
            STATE["reason"] = str(js_sel.get("reason", ""))
            STATE["scan_goto"] = i != CUR[0]
            STATE["goto"] = i
            return
    print("Auswahl aus dem Scanner hat kein Paar im Dashboard:", sorted(want), flush=True)

set_panel_titles()

def wait_frame():
    fig.canvas.draw_idle()
    t_ = time.time()
    while time.time() - t_ < DRAW_EVERY and STATE["goto"] is None:
        fig.canvas.start_event_loop(0.05)
        check_selection()

while plt.fignum_exists(fig.number):
    check_selection()
    if STATE["goto"] is not None:
        i_, STATE["goto"] = STATE["goto"], None
        if i_ != CUR[0]:
            switch_to(i_)
    with LOCK:
        sm = dict(S["sm"]); po = dict(S["poly"])
        tr = {k: list(v) for k, v in S["trades"].items()}
        nf = list(S["newfills"]); S["newfills"].clear()
        pos, bal = S["pos"], S["bal"]
    HAS = bool(PINFO["poly"]) and all(po.values())
    if PINFO.pop("dirty", False):
        set_panel_titles()
    if not HAS:
        po = {k: dict(NOPO, t=time.time()) for k in "DR"}
    if all(sm.values()):
        now = time.time()
        tD, tR = (micro(po["D"]), micro(po["R"])) if HAS else (NAN, NAN)
        THEO.append((now, tD, tR))
        vals = {"tD": tD, "tR": tR}
        for k, t_ in (("D", tD), ("R", tR)):
            a_, b_ = n(sm[k]["ask"]), n(sm[k]["bid"])
            vals["zB" + k] = a_ if t_ - a_ > THR[0] else NAN
            vals["zS" + k] = b_ if b_ - t_ > THR[0] else NAN
        for k in "DR":
            vals["s" + k + "b"] = n(sm[k]["bid"]); vals["s" + k + "a"] = n(sm[k]["ask"])
            vals["p" + k + "b"] = po[k]["bid"]; vals["p" + k + "a"] = po[k]["ask"]
        push((now - t0) / 60, vals)

        for f in nf:
            print("NEUER FILL:", f, flush=True)
            ex = str(pick(f, "exchangeId", "exchange_id"))
            mk = "D" if ex == str(SIDE["D"]["ex"]) else "R" if ex == str(SIDE["R"]["ex"]) else None
            pr = pick(f, "price", "fillPrice")
            if mk is None or pr is None: continue
            sd, ac = str(pick(f, "side", default="")).lower(), str(pick(f, "action", default="")).lower()
            sign = 0 if sd not in ("yes", "no") or ac not in ("buy", "sell") else (1 if (sd == "yes") == (ac == "buy") else -1)
            PEND.append({"mk": mk, "p": float(pr), "sign": sign,
                         "t": to_ts(pick(f, "createdAt", "timestamp", "time", "filledAt")) or now})
        for e in PEND[:]:
            if now - e["t"] >= MARKOUT_S:
                tgt = e["t"] + MARKOUT_S
                idx = 1 if e["mk"] == "D" else 2
                theo = next((row[idx] for row in reversed(THEO) if row[0] <= tgt), THEO[0][idx])
                MARK.append({"mk": e["mk"], "m": e["sign"] * (theo - e["p"]) if e["sign"] else None})
                PEND.remove(e)

        cnt = CNT[0]
        xs = XA[:cnt]
        xlim = (max(0, xs[-1] - WINDOW_MIN), xs[-1] + 0.05) if STATE["live"] else axX.get_xlim()
        w = xlim[1] - xlim[0]
        i0 = int(np.searchsorted(xs, xlim[0] - 0.05 * w)); i1 = int(np.searchsorted(xs, xlim[1] + 0.05 * w, side="right"))
        if i1 <= i0: i0, i1 = max(0, cnt - 1), cnt
        xv = xs[i0:i1]
        V = {k: HA[k][i0:i1] for k in HA}

        for k in "DR":
            for t in tr[k]:
                key = (k, t["t"], t["p"], t["q"])
                if key in TSEEN: continue
                TSEEN.add(key)
                if t["t"] >= t0: TPTS[k].append(((t["t"] - t0) / 60, t["p"]))
        for P_, k in ((PD, "D"), (PR, "R")):
            P_["zb"].set_data(xv, V["zB" + k]); P_["zs"].set_data(xv, V["zS" + k])
        update_panel(PD, xv, V["sDb"], V["sDa"], V["tD"], STATE["theo"] and HAS, sm["D"]["bid"], sm["D"]["ask"], tD, TPTS["D"])
        update_panel(PR, xv, V["sRb"], V["sRa"], V["tR"], STATE["theo"] and HAS, sm["R"]["bid"], sm["R"]["ask"], tR, TPTS["R"])

        update_ladder(LD, sm["D"]["lb"], sm["D"]["la"], tD, STATE["theo"] and HAS)
        update_ladder(LR, sm["R"]["lb"], sm["R"]["la"], tR, STATE["theo"] and HAS)

        def edge(side):
            pb_, pa_, sb_, sa_ = V["p" + side + "b"], V["p" + side + "a"], V["s" + side + "b"], V["s" + side + "a"]
            pm = (pb_ + pa_) / 2
            return np.maximum(pm - sa_, sb_ - pm), np.maximum(pb_ - sa_, sb_ - pa_)
        eDm, eDc = edge("D"); eRm, eRc = edge("R")
        for key, arr in (("Dm", eDm), ("Dc", eDc), ("Rm", eRm), ("Rc", eRc)):
            EL[key].set_data(xv, arr)
        EINFO.set_text("kein Theo" if not HAS else f"D Mid {eDm[-1]:+.3f} / kons. {eDc[-1]:+.3f}     R Mid {eRm[-1]:+.3f} / kons. {eRc[-1]:+.3f}")
        ev = np.concatenate([eDm, eDc, eRm, eRc, [0.0]]); ev = ev[~np.isnan(ev)]
        axX.set_ylim(ev.min() - 0.005, ev.max() + 0.005)
        axX.set_xlim(*xlim)

        A = {k: HA[k][max(0, cnt - 600):cnt] for k in HA}
        lines = to_lines(build_sections(sm, po, tD, tR, now, tr, pos, bal, A))
        for i, tx in enumerate(TL):
            if i < len(lines):
                tx.set_text(lines[i][0]); tx.set_color(lines[i][1]); tx.set_fontweight(lines[i][2])
            else:
                tx.set_text("")

        btn.label.set_text("LIVE" if STATE["live"] else "Zurueck zu LIVE")
        bax.set_facecolor("#1f3b2d" if STATE["live"] else "#5b2f2f")
        tbtn.label.set_text("THEO AN" if STATE["theo"] else "THEO AUS")
        tax.set_facecolor("#3b3414" if STATE["theo"] else "#2a2a2a")
    CLOCK.set_text(time.strftime("%H:%M:%S"))
    wait_frame()
