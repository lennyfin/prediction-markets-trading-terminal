import os, re, json, time, math, threading, collections

import arb_scanner as A
import poly_gap_scanner as P

TICK = 0.005
FLOW_S = 600
MIN_TRADES = 2
MIN_CAP = 0.005
EDGE = 0.005
PRICE_LO, PRICE_HI = 0.10, 0.90
VOLA_MOVE = 0.005
POLY_MAX_SPREAD = 0.05
MAX_PLAUS = 0.25
SEL_FILE = P.SEL_FILE

ROWS = P.ROWS
TRP = collections.defaultdict(collections.deque)
PHIST = collections.defaultdict(lambda: collections.deque(maxlen=200))
PARTNER = {}
SORT = ["status"]
LINEMAP = []
ALERT = set()
START = time.time()

_prev_batch = A.Shard.batch


def _batch(self, topic, b):
    now = time.time()
    mine = self.topics.get(topic, [])
    for t in b.get("trades") or []:
        ex = str(t.get("exchangeId") or t.get("exchange_id") or (mine[0] if len(mine) == 1 else ""))
        if ex in mine:
            try:
                TRP[ex].append((now, float(t.get("quantity") or t.get("size") or 0), float(t.get("price"))))
            except (TypeError, ValueError):
                pass
    _prev_batch(self, topic, b)


A.Shard.batch = _batch


def flow(ex, mid, now):
    dq = TRP[ex]
    while dq and now - dq[0][0] > FLOW_S:
        dq.popleft()
    nb = sum(1 for _, _, p in dq if p >= mid)
    ns = len(dq) - nb
    return len(dq), sum(q for _, q, _ in dq), nb, ns


def poly_mid(r):
    if not r or not r.get("tok"):
        return None
    with P.PLOCK:
        p = P.POLY.get(r["tok"])
        p = dict(p) if p else None
    if not p or p["bid"] is None or p["ask"] is None:
        return None
    return (p["bid"] + p["ask"]) / 2, p["ask"] - p["bid"], p["t"]


def theo_for(r, now):
    a = poly_mid(r)
    if a is None:
        return None
    pm, psp, pt = a
    pr = PARTNER.get(r["ex"])
    b = poly_mid(pr) if pr else None
    th = pm
    if b is not None and 0.9 <= pm + b[0] <= 1.1:
        th = pm / (pm + b[0])
    age = 0.0 if time.time() - P.PSTAT["ws"] < 15 else now - pt
    return th, psp, age, pm


def evaluate(r, now):
    ex = r["ex"]
    with A.LOCK:
        b, sage = A.book_state(ex, now)
        b = dict(b) if b else None
    if b is None or b["bid"] is None or b["ask"] is None:
        return None
    bid, ask = b["bid"], b["ask"]
    mid = (bid + ask) / 2
    ticks = round((ask - bid) / TICK)
    n, vol, nb, ns = flow(ex, mid, now)
    th_ = theo_for(r, now)
    x = {"bid": bid, "ask": ask, "bsz": b["bsz"], "asz": b["asz"], "ticks": ticks, "n": n, "vol": vol, "nb": nb, "ns": ns,
         "sage": sage, "rest_age": now - b["t_rest"], "th": None, "psp": None, "page": None, "pd30": None,
         "jb": None, "ja": None, "cap": 0.0, "status": "", "score": 0.0, "tag": "dim"}
    if th_ is not None:
        th, psp, page, pm = th_
        x.update(th=th, psp=psp, page=page)
        h = PHIST[ex]
        if not h or now - h[-1][0] >= 1.0:
            h.append((now, pm))
        old = next((v for t, v in reversed(h) if t <= now - 30), None)
        x["pd30"] = None if old is None else pm - old
    return x


def classify(x, limit):
    if x["sage"] is None or x["sage"] > limit:
        x.update(status="alt", tag="dim"); return
    bid, ask = x["bid"], x["ask"]
    if bid > ask + 1e-9:
        x.update(status="CROSSED", tag="g", score=1e6 + (bid - ask)); return
    mid = (bid + ask) / 2
    if x["ticks"] < 3:
        x.update(status="eng", tag="dim"); return
    if not (PRICE_LO <= mid <= PRICE_HI):
        x.update(status="Preis extrem", tag="dim"); return
    jb, ja = bid + TICK, ask - TICK
    th = x["th"]
    if th is None:
        x.update(jb=jb, ja=ja, cap=ja - jb)
        if x["n"] >= MIN_TRADES and ja - jb >= MIN_CAP - 1e-9:
            x.update(status="ohne Theo", tag="y", score=(ja - jb) * x["n"] * 0.3)
        else:
            x.update(status="ohne Theo, kein Fluss" if x["n"] < MIN_TRADES else "eng", tag="dim")
        return
    if x["psp"] > P.POLY_MAX_SPREAD:
        x.update(status="Poly duenn", tag="dim"); return
    if x["page"] is not None and x["page"] > 6:
        x.update(status="Poly alt", tag="dim"); return
    if abs(th - mid) > MAX_PLAUS:
        x.update(status="CHECK Zuordnung", tag="dim"); return
    jb, ja = min(jb, th - EDGE), max(ja, th + EDGE)
    jb, ja = math.floor(jb / TICK + 1e-9) * TICK, math.ceil(ja / TICK - 1e-9) * TICK
    bid_ok, ask_ok = jb >= bid - 1e-9, ja <= ask + 1e-9
    x.update(jb=jb if bid_ok else None, ja=ja if ask_ok else None)
    calm = x["pd30"] is None or abs(x["pd30"]) < VOLA_MOVE
    flow_ok = x["n"] >= MIN_TRADES
    two_sided = x["nb"] >= 1 and x["ns"] >= 1
    cap = (ja - jb) if (bid_ok and ask_ok) else 0.0
    x["cap"] = cap
    if bid_ok and ask_ok and cap >= MIN_CAP - 1e-9:
        if not calm:
            x.update(status="VOLA", tag="y", score=cap * x["n"] * 0.3)
        elif not flow_ok:
            x.update(status="kein Fluss", tag="y", score=cap * 0.5)
        elif not two_sided:
            x.update(status="MM einseitiger Fluss", tag="y", score=cap * x["n"] * 0.6)
        else:
            x.update(status="MM", tag="g", score=cap * x["n"])
    elif bid_ok != ask_ok:
        x.update(status="nur BID" if bid_ok else "nur ASK", tag="y", score=(TICK * x["n"]) * 0.3 if flow_ok else 0.0)
    else:
        x.update(status="kein Platz", tag="dim")


def render():
    now = time.time()
    limit = A.MAX_AGE_S if A.WSTAT["joined"] else max(A.MAX_AGE_S, 1.3 * len(A.EXINFO) * 60.0 / A.READ_BUDGET)
    data = []
    for i, r in enumerate(ROWS):
        x = evaluate(r, now)
        if x is not None:
            classify(x, limit)
        data.append((i, r, x))
    ok = [d for d in data if d[2]]
    prio = {"g": 0, "y": 1, "dim": 2}
    key = {"status": lambda d: (prio[d[2]["tag"]], -d[2]["score"], -d[2]["ticks"]),
           "spread": lambda d: (-d[2]["ticks"],), "trades": lambda d: (-d[2]["n"],)}[SORT[0]]
    ok.sort(key=key)
    rest = [d for d in data if not d[2]]
    LINEMAP[:] = [None, None] + [d[0] for d in ok + rest]
    n_mm = sum(1 for _, _, x in ok if x["status"] in ("MM", "CROSSED"))
    n_one = sum(1 for _, _, x in ok if x["status"] in ("nur BID", "nur ASK"))
    n_th = sum(1 for _, r, x in ok if x["th"] is not None)
    out = [(f"MM SCANNER 2  {time.strftime('%H:%M:%S')}   Buecher {len(ROWS)}   MM-Kandidaten {n_mm}   einseitig {n_one}   mit Polymarket {n_th}   "
            f"Sortierung {SORT[0]} (1=Status 2=Spread 3=Trades)   Fluss seit {(now - START) / 60:.0f} min gemessen (Fenster {FLOW_S // 60} min)   "
            f"Poly-WS {'an' if time.time() - P.PSTAT['ws'] < 15 else 'AUS'}   Reads {A.reads_last_min()}/100", "w"),
           (f"{'ID':<8}{'MARKT':<38}{'BID':>6}{'ASK':>6}{'SPR':>5}{'THEO':>7}{'PD30':>6}{'QBID':>7}{'QASK':>7}{'CAPT':>6}{'TRD':>5}{'K/V':>6}{'VOL':>7}{'SCORE':>7}  {'STATUS':<22}", "dim")]
    for _, r, x in ok:
        fb = lambda v: "      -" if v is None else f"{v:>7.3f}"
        pd = "     -" if x["pd30"] is None else f"{x['pd30'] * 100:>+5.1f}"
        kv = f"{x['nb']}/{x['ns']}"
        line = (f"{r['mid']:<8}{r['title'][:36]:<38}{x['bid']:>6.3f}{x['ask']:>6.3f}{x['ticks']:>4}t{fb(x['th'])}{pd}{fb(x['jb'])}{fb(x['ja'])}"
                f"{x['cap']:>6.3f}{x['n']:>5}{kv:>6}{x['vol']:>7.0f}{x['score']:>7.2f}  {x['status']:<22}")
        out.append((line, x["tag"]))
        if x["tag"] == "g" and x["status"] == "MM" and r["ex"] not in ALERT:
            ALERT.add(r["ex"])
            print(f"[{time.strftime('%H:%M:%S')}] MM {r['title'][:45]}  quote {x['jb']:.3f} / {x['ja']:.3f}  capture {x['cap']:.3f}  trades {x['n']}", flush=True)
        elif x["status"] != "MM":
            ALERT.discard(r["ex"])
    out.append((f"{len(rest)} Buecher ohne Daten (laden noch).", "dim"))
    out.append(("", "w"))
    out.append(("GRUEN MM = beide Seiten quoten lohnt: Spread >= 3 Ticks, Polymarket-Theo traegt beide Quotes (Bid <= Theo - Polster, Ask >= Theo + Polster), Fluss mit Kaeufen UND Verkaeufen, ruhig, Preis 0.10 bis 0.90.", "dim"))
    out.append(("GELB = nur BID/nur ASK (Theo liegt ausserhalb des Spreads: Penny Jump nur auf der Signalseite), VOLA (Polymarket bewegt sich), kein/einseitiger Fluss, oder ohne Theo (kein Schutz).  CROSSED = Bid ueber Ask.", "dim"))
    out.append(("QBID/QASK = Penny-Jump-Ziele (Bid+1 Tick, Ask-1 Tick, am Theo gedeckelt).  CAPT = Gewinn pro Runde.  TRD = Trades im Fenster, K/V = Kauf-/Verkaufs-Trades (grobe Schaetzung ueber den Preis zum Mid).  SCORE = CAPT x Trades.", "dim"))
    out.append(("THEO = Polymarket (normiert D/(D+R)). Ohne Zuordnung steht - und es gibt kein Gruen.  PD30 = Polymarket-Bewegung 30 s in Prozentpunkten.", "dim"))
    return out


def run_gui():
    import tkinter as tk
    from tkinter import font as tkfont
    root = tk.Tk()
    root.title("MM Scanner 2")
    root.geometry("1450x780")
    root.configure(bg="black")
    f = tkfont.Font(family="Consolas", size=10)
    fb = tkfont.Font(family="Consolas", size=10, weight="bold")
    txt = tk.Text(root, bg="black", fg="white", font=f, bd=0, highlightthickness=0, wrap="none", cursor="arrow")
    txt.pack(fill="both", expand=True, padx=8, pady=6)
    txt.tag_config("w", foreground="white")
    txt.tag_config("g", foreground="#22c55e", font=fb)
    txt.tag_config("dim", foreground="#7a7a7a")
    txt.tag_config("y", foreground="#facc15")

    def on_dbl(ev):
        try:
            row = int(txt.index(f"@{ev.x},{ev.y}").split(".")[0]) - 1
            i = LINEMAP[row] if 0 <= row < len(LINEMAP) else None
            if i is not None:
                r = ROWS[i]
                with open(SEL_FILE, "w") as fh:
                    json.dump({"mids": [r["mid"]], "exs": [r["ex"]], "name": r["title"], "t": time.time()}, fh)
                print("Im Dashboard oeffnen:", r["title"], flush=True)
        except Exception as e:
            print("Auswahl fehlgeschlagen:", e, flush=True)
    txt.bind("<Double-Button-1>", on_dbl)
    root.bind("<Key>", lambda ev: SORT.__setitem__(0, {"1": "status", "2": "spread", "3": "trades"}.get(ev.char, SORT[0])))

    def tick():
        lines = render()
        top = txt.yview()[0]
        txt.config(state="normal")
        txt.delete("1.0", "end")
        for text, tag in lines:
            txt.insert("end", text + "\n", tag)
        txt.config(state="disabled")
        txt.yview_moveto(top)
        root.after(250, tick)
    tick()
    root.mainloop()


def run_console():
    col = {"w": "\033[97m", "g": "\033[92m", "y": "\033[93m", "dim": "\033[90m"}
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        for text, tag in render():
            print(col[tag] + text + "\033[0m")
        time.sleep(0.5)


def main():
    print("Suche Maerkte ...", flush=True)
    s = A.sm_session()
    _disc, cache = A.discover, {}
    A.discover = lambda ss: cache.setdefault("d", _disc(ss))
    A.leg = lambda info, ex, label: {"ex": ex["id"], "tid": info["tid"], "mid": info["id"], "label": label,
                                     "title": info["title"], "last": ex.get("last")}
    A.SHOW_DEBUG = False
    groups = [g for g in A.build_groups(s) if g["tier"] != "multi" and len(g["legs"]) == 2]
    seen = set()
    for gi, g in enumerate(groups):
        for side, l in enumerate(g["legs"]):
            ROWS.append({"ex": l["ex"], "mid": l["mid"], "tid": l["tid"], "title": l["title"], "gi": gi, "side": side,
                         "gname": g["name"], "tok": None, "label": ""})
            seen.add(l["ex"])
    for m in A.discover(s):
        i = A.market_info(m)
        if not i:
            continue
        for e in i["exs"]:
            if e["id"] in seen:
                continue
            nm = i["title"] if len(i["exs"]) == 1 else f"{i['title']} [{e['option']}]"
            ROWS.append({"ex": e["id"], "mid": i["id"], "tid": i["tid"], "title": nm, "gi": None, "side": 0, "gname": nm, "tok": None, "label": ""})
    for r in ROWS:
        A.EXINFO[r["ex"]] = {"tid": r["tid"], "mid": r["mid"]}
    byk = {(r["gi"], r["side"]): r for r in ROWS if r["gi"] is not None}
    for r in ROWS:
        if r["gi"] is not None:
            PARTNER[r["ex"]] = byk.get((r["gi"], 1 - r["side"]))
    print(f"{len(ROWS)} Buecher, davon {len(groups) * 2} in Paaren (Polymarket-Zuordnung moeglich)", flush=True)
    if A.HAVE_WS:
        A.start_realtime([{"legs": [{"ex": r["ex"], "tid": r["tid"], "mid": r["mid"]}]} for r in ROWS])
    else:
        print("Hinweis: 'pip install websocket-client' fuer Echtzeit statt Polling.", flush=True)
    for i, r in enumerate(ROWS):
        A.Q.push(r["ex"], 10 + i)
    for _ in range(3):
        threading.Thread(target=A.rest_worker, daemon=True).start()
    threading.Thread(target=P.poly_poll, daemon=True).start()

    def matcher():
        P.match_worker(groups)
        for r in ROWS:
            if r.get("tok") and r["side"] == 0:
                print(f"  {r['title'][:45]:<46}-> Polymarket: {r['label']}", flush=True)
        P.start_poly_ws()
    threading.Thread(target=matcher, daemon=True).start()
    try:
        run_gui()
    except ImportError:
        run_console()


if __name__ == "__main__":
    main()
