import re, json, time, threading, collections, requests

try:
    import websocket
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

KEY = "your_api_key_here"
SM = "https://www.thesuper.market/api/v1"

NAME = "Alaska Senate"
M_D, M_R = "377", "378"
POLY_D = "will-mary-peltola-win-the-alaska-senate-race-in-2026"
POLY_R = "will-dan-sullivan-win-the-alaska-senate-race-in-2026"

TICK = 0.005
ROWS = 36
REST_DEPTH = 200
POLL_NO_WS = 2.0
POLL_WS = 20.0
ORDERS_EVERY = 10.0
POLY_EVERY = 1.0

LOCK = threading.Lock()
READS = collections.deque()
DEBUG_SEEN = set()


def debug_once(tag, obj):
    if tag not in DEBUG_SEEN:
        DEBUG_SEEN.add(tag)
        print(f"[DEBUG {tag}] {str(obj)[:400]}", flush=True)


def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
    return default


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
            time.sleep(1)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(path)


def parse_list(js, *names):
    if isinstance(js, list):
        return js
    if isinstance(js, dict):
        for k in ("data",) + names:
            if isinstance(js.get(k), list):
                return js[k]
    return []


def ix(p):
    return int(round(float(p) / TICK))


def new_side():
    return {"ex": None, "tid": None, "mid": None, "bids": {}, "asks": {}, "seq": None, "t_book": 0.0,
            "vol": collections.defaultdict(float), "btr": collections.defaultdict(float),
            "atr": collections.defaultdict(float), "last": None, "seen": set(), "own": {}, "theo": None, "top": None}


ST = {"D": new_side(), "R": new_side()}
WS = {"joined": 0, "topics": 0, "conn": 0}
UI = {"manual": False}


def set_book(k, bids, asks, seq):
    def conv(levels):
        out = {}
        for x in levels or []:
            try:
                out[ix(x["price"])] = float(x.get("quantity", x.get("size", 0)))
            except (KeyError, TypeError, ValueError):
                pass
        return out
    with LOCK:
        st = ST[k]
        if seq is not None and st["seq"] is not None and seq <= st["seq"]:
            return
        st["bids"], st["asks"] = conv(bids), conv(asks)
        if seq is not None:
            st["seq"] = seq
        st["t_book"] = time.time()


def best(st):
    bb = max(st["bids"]) if st["bids"] else None
    ba = min(st["asks"]) if st["asks"] else None
    return bb, ba


def add_trade(k, price, qty, tid, classify):
    with LOCK:
        st = ST[k]
        key = tid if tid is not None else (round(float(price), 4), qty)
        if key in st["seen"]:
            return
        st["seen"].add(key)
        i = ix(price)
        st["vol"][i] += qty
        st["last"] = i
        if classify:
            bb, ba = best(st)
            if ba is not None and i >= ba:
                st["atr"][i] += qty
            elif bb is not None and i <= bb:
                st["btr"][i] += qty
            elif bb is not None and ba is not None:
                (st["atr"] if i * 2 >= bb + ba else st["btr"])[i] += qty


def fetch_book(s, k):
    st = ST[k]
    p = {"tournamentId": st["tid"]} if st["tid"] else {}
    b = sm_get(s, f"/exchanges/{st['ex']}/orderbook", depth=REST_DEPTH, **p)
    a = b.get("asOf")
    set_book(k, b.get("bids"), b.get("asks"), a.get("sequence") if isinstance(a, dict) else None)


def fetch_trades(s, k):
    st = ST[k]
    p = {"tournamentId": st["tid"]} if st["tid"] else {}
    items = parse_list(sm_get(s, f"/exchanges/{st['ex']}/trades", limit=100, **p), "trades")
    if items:
        debug_once("TRADE", items[0])
    for t in reversed(items):
        pr = pick(t, "price")
        if pr is None:
            continue
        add_trade(k, pr, float(pick(t, "quantity", "size", "qty", default=0) or 0), pick(t, "id", "tradeId"), False)


def fetch_orders(s):
    items = parse_list(sm_get(s, "/orders", status="open", limit=100), "orders")
    if items:
        debug_once("ORDER", items[0])
    own = {"D": collections.defaultdict(float), "R": collections.defaultdict(float)}
    for o in items:
        ex = str(pick(o, "exchangeId", "exchange_id"))
        k = "D" if ex == str(ST["D"]["ex"]) else "R" if ex == str(ST["R"]["ex"]) else None
        pr = pick(o, "price", "limitPrice")
        qty = pick(o, "remainingQuantity", "remaining", "leavesQuantity", "quantity", "size")
        if k is None or pr is None or qty is None:
            continue
        side, act = str(pick(o, "side", default="")).lower(), str(pick(o, "action", default="")).lower()
        pr = float(pr)
        if side == "no":
            pr, act = 1 - pr, ("sell" if act == "buy" else "buy")
        own[k][ix(pr)] += float(qty)
    with LOCK:
        for k in "DR":
            ST[k]["own"] = dict(own[k])


def poly_token(slug):
    g = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=30).json()[0]
    return json.loads(g["clobTokenIds"])[json.loads(g["outcomes"]).index("Yes")]


def poly_micro(ps, token):
    b = ps.get("https://clob.polymarket.com/book", params={"token_id": token}, timeout=10).json()
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    bb, ba = max(bids), min(asks)
    return (bb[0] * ba[1] + ba[0] * bb[1]) / (bb[1] + ba[1])


def poly_worker():
    ps, tok = requests.Session(), {}
    while True:
        t = time.time()
        try:
            if not tok:
                tok["D"], tok["R"] = poly_token(POLY_D), poly_token(POLY_R)
            for k in "DR":
                v = poly_micro(ps, tok[k])
                with LOCK:
                    ST[k]["theo"] = v
        except Exception as e:
            print("Polymarket Fehler:", str(e)[:80], flush=True)
        time.sleep(max(0, POLY_EVERY - (time.time() - t)))


def rest_worker():
    s = sm_session()
    last_book, last_orders = {"D": 0.0, "R": 0.0}, 0.0
    while True:
        now = time.time()
        for k in "DR":
            every = POLL_WS if WS["joined"] >= WS["topics"] > 0 else POLL_NO_WS
            if now - last_book[k] >= every or ST[k].get("dirty"):
                try:
                    ST[k]["dirty"] = False
                    fetch_book(s, k)
                    last_book[k] = time.time()
                except Exception as e:
                    print("Buch Fehler:", k, str(e)[:80], flush=True)
                    time.sleep(2)
        if now - last_orders >= ORDERS_EVERY:
            try:
                fetch_orders(s)
            except Exception as e:
                print("Orders Fehler (Spalte Orders bleibt leer):", str(e)[:80], flush=True)
            last_orders = time.time()
        time.sleep(0.2)


TOK = {"v": None, "exp": 0.0}
TLOCK = threading.Lock()


def get_token():
    with TLOCK:
        if not TOK["v"] or time.time() > TOK["exp"]:
            js = requests.post(SM + "/realtime/token", headers={"Authorization": f"Bearer {KEY.strip()}"}, timeout=30).json()
            TOK["v"], TOK["exp"] = js, time.time() + 9000
        return TOK["v"]


class Feed(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.topics = {}
        for k in "DR":
            st = ST[k]
            t = f"tournament:{st['tid']}:market:{st['mid']}" if st["tid"] else f"market:{st['ex']}"
            self.topics[t] = k
        self.rev, self.joined, self.refs, self.ws = {}, set(), {}, None
        WS["topics"] = len(self.topics)

    def run(self):
        backoff = 1
        while True:
            try:
                t = get_token()
                url = t["supabaseUrl"].replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + \
                    f"/realtime/v1/websocket?apikey={t['anonKey']}&vsn=1.0.0"
                self.ws = websocket.WebSocketApp(url, on_open=self.on_open, on_message=self.on_msg,
                                                 on_error=lambda w, e: print("WS Fehler:", e, flush=True),
                                                 on_close=self.on_close)
                WS["conn"] = 1
                self.ws.run_forever(ping_interval=None)
            except Exception as e:
                print("WS Verbindungsfehler:", e, flush=True)
            WS["conn"] = 0
            self.on_close(None, None, None)
            time.sleep(min(30, backoff))
            backoff = min(30, backoff * 2)

    def on_open(self, ws):
        threading.Thread(target=self.pump, args=(ws,), daemon=True).start()

    def pump(self, ws):
        tok = get_token()["token"]
        for i, topic in enumerate(self.topics):
            ref = str(i + 2)
            self.refs[ref] = topic
            try:
                ws.send(json.dumps({"topic": "realtime:" + topic, "event": "phx_join", "ref": ref, "join_ref": ref,
                                    "payload": {"config": {"broadcast": {"ack": False, "self": False}, "presence": {"key": ""},
                                                           "postgres_changes": [], "private": topic.startswith("tournament:")},
                                                "access_token": tok}}))
            except Exception:
                return
        t_open, n = time.time(), 0
        while ws is self.ws and ws.sock and ws.sock.connected:
            time.sleep(1)
            n += 1
            if n % 20 == 0:
                try:
                    ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": f"hb{n}"}))
                except Exception:
                    return
            if time.time() - t_open > 9000:
                ws.close()
                return

    def on_close(self, ws, code, msg):
        for topic in list(self.joined):
            self.joined.discard(topic)
            WS["joined"] -= 1
        self.rev.clear()

    def on_msg(self, ws, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        ev, topic = m.get("event"), str(m.get("topic", ""))[9:]
        if ev == "phx_reply" and m.get("ref") in self.refs:
            topic = self.refs[m["ref"]]
            if (m.get("payload") or {}).get("status") == "ok":
                if topic not in self.joined:
                    self.joined.add(topic)
                    WS["joined"] += 1
                ST[self.topics[topic]]["dirty"] = True
            else:
                print("Realtime-Beitritt fehlgeschlagen:", topic, m.get("payload"), flush=True)
        elif ev in ("phx_error", "phx_close") and topic in self.joined:
            self.joined.discard(topic)
            WS["joined"] -= 1
        elif ev == "broadcast" and topic in self.topics:
            pl = m.get("payload") or {}
            if pl.get("event") == "market_batch":
                self.batch(self.topics[topic], topic, pl.get("payload") or {})

    def batch(self, k, topic, b):
        st = ST[k]
        for t in b.get("trades") or []:
            if str(pick(t, "exchangeId", "exchange_id", default=st["ex"])) == str(st["ex"]) and pick(t, "price") is not None:
                add_trade(k, pick(t, "price"), float(pick(t, "quantity", "size", default=0) or 0), pick(t, "id"), True)
        for bk in b.get("books") or []:
            if str(pick(bk, "exchangeId", "exchange_id", "id")) == str(st["ex"]):
                a = bk.get("asOf")
                set_book(k, bk.get("bids"), bk.get("asks"), a.get("sequence") if isinstance(a, dict) else None)
        d = b.get("delivery") or {}
        rev, prev, last = d.get("revision"), d.get("previousRevision"), self.rev.get(topic)
        dirty = bool(b.get("resyncRequired")) or not topic.startswith("tournament:")
        if rev is not None and (last is None or rev > last):
            if last is not None and prev is not None and prev > last:
                dirty = True
            self.rev[topic] = rev
        if dirty:
            st["dirty"] = True


BG, GRID, TXT, DIM = "#000000", "#1c1c1c", "#e5e7eb", "#7a7a7a"
BIDBG, BIDBAR, ASKBG, ASKBAR = "#08203a", "#2563eb", "#2a0d0d", "#dc2626"
YEL, RED, BLUE, GRN = "#facc15", "#ef4444", "#60a5fa", "#22c55e"
COLS = [("Orders", 50), ("Vol", 58), ("Preis", 66), ("Bid", 96), ("B.Tr", 50), ("A.Tr", 50), ("Ask", 96)]
RH, HEAD = 17, 70
SIDE_W = sum(w for _, w in COLS)
GAP = 24
FONT = ("Consolas", 9)
FONTB = ("Consolas", 9, "bold")
FONTT = ("Consolas", 11, "bold")


def center_window(k, ROWS_=ROWS):
    st = ST[k]
    bb, ba = best(st)
    if bb is None or ba is None:
        return
    mid = (bb + ba) // 2
    if st["top"] is None or (not UI["manual"] and (mid > st["top"] - 5 or mid < st["top"] - ROWS_ + 6)):
        st["top"] = mid + ROWS_ // 2


def draw_side(cv, k, x0, title, color):
    with LOCK:
        st = ST[k]
        bids, asks = dict(st["bids"]), dict(st["asks"])
        vol, btr, atr, own = dict(st["vol"]), dict(st["btr"]), dict(st["atr"]), dict(st["own"])
        last, theo, top = st["last"], st["theo"], st["top"]
    bb, ba = (max(bids) if bids else None), (min(asks) if asks else None)
    if top is None:
        cv.create_text(x0 + 10, HEAD + 20, text="lade ...", fill=DIM, anchor="w", font=FONT)
        return
    xs = [x0]
    for _, w in COLS:
        xs.append(xs[-1] + w)
    X = {name: (xs[i], xs[i + 1]) for i, (name, _) in enumerate(COLS)}
    cv.create_text(x0 + 4, 12, text=title, fill=color, anchor="w", font=FONTT)
    info = (f"Bid {bb * TICK:.3f} x{bids[bb]:.0f}   Ask {ba * TICK:.3f} x{asks[ba]:.0f}   Spread {(ba - bb) * TICK:.3f}" if bb is not None and ba is not None else "")
    cv.create_text(x0 + 4, 31, text=info, fill=TXT, anchor="w", font=FONT)
    cv.create_text(x0 + 4, 46, text=(f"Last {last * TICK:.3f}   " if last is not None else "") + (f"Poly {theo:.4f}" if theo else ""),
                   fill=YEL, anchor="w", font=FONT)
    for name, (a, b) in X.items():
        cv.create_text((a + b) / 2, HEAD - 8, text=name, fill=DIM, font=FONT)
    window = range(top, top - ROWS, -1)
    mb = max([bids.get(i, 0) for i in window] + [1.0])
    ma = max([asks.get(i, 0) for i in window] + [1.0])
    mv = max([vol.get(i, 0) for i in window] + [1.0])
    for r, i in enumerate(window):
        y = HEAD + r * RH
        cv.create_rectangle(X["Bid"][0], y, X["Bid"][1], y + RH, fill=BIDBG, outline="")
        cv.create_rectangle(X["Ask"][0], y, X["Ask"][1], y + RH, fill=ASKBG, outline="")
        cv.create_line(x0, y + RH, x0 + SIDE_W, y + RH, fill=GRID)
        if i in own:
            cv.create_rectangle(*X["Orders"][:1], y + 1, X["Orders"][1], y + RH - 1, fill="#b91c1c", outline="")
            cv.create_text(X["Orders"][1] - 4, y + RH / 2, text=f"{own[i]:.0f}", fill="white", anchor="e", font=FONTB)
        if vol.get(i):
            w = (X["Vol"][1] - X["Vol"][0]) * vol[i] / mv
            cv.create_rectangle(X["Vol"][1] - w, y + 2, X["Vol"][1], y + RH - 2, fill="#1e3a5f", outline="")
            cv.create_text(X["Vol"][1] - 4, y + RH / 2, text=f"{vol[i]:.0f}", fill=TXT, anchor="e", font=FONT)
        if i == last:
            cv.create_rectangle(X["Preis"][0], y, X["Preis"][1], y + RH, fill=YEL, outline="")
            pc = "black"
        else:
            pc = GRN if i == bb else RED if i == ba else TXT
        cv.create_text((X["Preis"][0] + X["Preis"][1]) / 2, y + RH / 2, text=f"{i * TICK:.3f}", fill=pc, font=FONTB if i in (bb, ba) else FONT)
        if i in bids:
            w = (X["Bid"][1] - X["Bid"][0]) * bids[i] / mb
            cv.create_rectangle(X["Bid"][1] - w, y + 1, X["Bid"][1], y + RH - 1, fill=BIDBAR, outline="")
            cv.create_text(X["Bid"][1] - 4, y + RH / 2, text=f"{bids[i]:.0f}", fill="white", anchor="e", font=FONT)
        if i in asks:
            w = (X["Ask"][1] - X["Ask"][0]) * asks[i] / ma
            cv.create_rectangle(X["Ask"][0], y + 1, X["Ask"][0] + w, y + RH - 1, fill=ASKBAR, outline="")
            cv.create_text(X["Ask"][0] + 4, y + RH / 2, text=f"{asks[i]:.0f}", fill="white", anchor="w", font=FONT)
        if btr.get(i):
            cv.create_text(X["B.Tr"][1] - 4, y + RH / 2, text=f"{btr[i]:.0f}", fill=RED, anchor="e", font=FONT)
        if atr.get(i):
            cv.create_text(X["A.Tr"][1] - 4, y + RH / 2, text=f"{atr[i]:.0f}", fill=BLUE, anchor="e", font=FONT)
    if theo:
        fy = HEAD + (top - theo / TICK + 0.5) * RH
        if HEAD <= fy <= HEAD + ROWS * RH:
            cv.create_line(x0, fy, x0 + SIDE_W, fy, fill=YEL, width=2)
            cv.create_text(x0 + SIDE_W - 2, fy - 7, text=f"POLY {theo:.3f}", fill=YEL, anchor="e", font=FONT)


def draw_all(cv):
    cv.delete("all")
    for k in "DR":
        center_window(k)
    draw_side(cv, "D", 6, f"DEMOCRAT  ({NAME})", "#4da3ff")
    draw_side(cv, "R", 6 + SIDE_W + GAP, f"REPUBLICAN  ({NAME})", "#ff5c5c")
    reads = len([t for t in READS if time.time() - t < 60])
    mode = f"Realtime {WS['joined']}/{WS['topics']}" if HAVE_WS else "Polling (pip install websocket-client fuer Realtime)"
    cv.create_text(8, HEAD + ROWS * RH + 14, anchor="w", font=FONT, fill=DIM,
                   text=f"{time.strftime('%H:%M:%S')}   {mode}   Reads {reads}/100   Mausrad: Preisbereich verschieben   c: wieder zentrieren")


def run_gui():
    import tkinter as tk
    root = tk.Tk()
    root.title("DOM")
    W, H = 2 * SIDE_W + GAP + 12, HEAD + ROWS * RH + 30
    root.geometry(f"{W}x{H}")
    root.configure(bg=BG)
    cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0)
    cv.pack(fill="both", expand=True)

    def wheel(ev):
        step = -3 if (getattr(ev, "delta", 0) > 0 or getattr(ev, "num", 0) == 4) else 3
        UI["manual"] = True
        for k in "DR":
            if ST[k]["top"] is not None:
                ST[k]["top"] += step
    root.bind("<MouseWheel>", wheel)
    root.bind("<Button-4>", wheel)
    root.bind("<Button-5>", wheel)
    root.bind("c", lambda e: (UI.update(manual=False), [ST[k].update(top=None) for k in "DR"]))

    def tick():
        draw_all(cv)
        root.after(200, tick)

    tick()
    root.mainloop()


def side_info(s, mid):
    m = sm_get(s, f"/markets/{mid}")
    t = m["contexts"][0]["tournament"]
    return m["exchanges"][0]["id"], (t["id"] if t else None)


def main():
    s = sm_session()
    for k, mid in (("D", M_D), ("R", M_R)):
        ex, tid = side_info(s, mid)
        ST[k].update(ex=str(ex), tid=tid, mid=str(mid))
        fetch_book(s, k)
        fetch_trades(s, k)
    threading.Thread(target=poly_worker, daemon=True).start()
    threading.Thread(target=rest_worker, daemon=True).start()
    if HAVE_WS:
        Feed().start()
    else:
        print("Hinweis: 'pip install websocket-client' installieren fuer Realtime (sonst Polling alle 2 s).", flush=True)
    run_gui()


if __name__ == "__main__":
    main()
