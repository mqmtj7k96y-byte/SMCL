"""Watch-list bot: scans the top 50 USDT pairs on Binance and sends the coins
that score "Watch" to Telegram when you send /scan (or press Run workflow).
Uses only the Python standard library."""
import json, os, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MANUAL = os.environ.get("MANUAL") == "1"
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
POOL, MAX_WATCH, WATCH_MIN = 50, 20, 6
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "BUSD", "EUR", "AEUR", "XUSD", "USD1", "PAXG", "WBTC", "WBETH"}
TZ = timezone(timedelta(hours=5))  # Tashkent


def http(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "watch-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def binance(path):
    last = None
    for b in BASES:
        try:
            return http(b + path)
        except Exception as e:  # try the next address
            last = e
    raise last


def klines(sym, iv, lim):
    rows = binance(f"/api/v3/klines?symbol={sym}&interval={iv}&limit={lim}")
    return [dict(t=r[0], o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4])) for r in rows]


def swings(K, n):
    raw = []
    for i in range(n, len(K) - n):
        hi = all(K[i]["h"] > K[i - k]["h"] and K[i]["h"] > K[i + k]["h"] for k in range(1, n + 1))
        lo = all(K[i]["l"] < K[i - k]["l"] and K[i]["l"] < K[i + k]["l"] for k in range(1, n + 1))
        if hi: raw.append({"i": i, "type": "H", "v": K[i]["h"]})
        if lo: raw.append({"i": i, "type": "L", "v": K[i]["l"]})
    s = []
    for p in raw:
        if not s or s[-1]["type"] != p["type"]:
            s.append(p)
        elif (p["type"] == "H" and p["v"] > s[-1]["v"]) or (p["type"] == "L" and p["v"] < s[-1]["v"]):
            s[-1] = p
    ph = pl = None
    for p in s:
        if p["type"] == "H":
            p["lab"] = "H" if ph is None else ("HH" if p["v"] > ph else "LH"); ph = p["v"]
        else:
            p["lab"] = "L" if pl is None else ("HL" if p["v"] > pl else "LL"); pl = p["v"]
    return s


def struct(K, n):
    s = swings(K, n)
    H = [x for x in s if x["type"] == "H"]
    L = [x for x in s if x["type"] == "L"]
    if len(H) < 2 or len(L) < 2:
        return dict(k="n/a", pts=0, H=H, L=L, h1=None, l1=None)
    h1, l1 = H[-1], L[-1]
    hh, hl = h1["lab"] == "HH", l1["lab"] == "HL"
    if hh and hl: k, pts = "Up", 2
    elif not hh and not hl: k, pts = "Down", 0
    else: k, pts = "Mixed", 1
    return dict(k=k, pts=pts, H=H, L=L, h1=h1, l1=l1)


def last_sweep(O):
    n = len(O)
    piv = []
    for i in range(5, n - 5):
        if all(O[i]["l"] < O[i - k]["l"] and O[i]["l"] < O[i + k]["l"] for k in range(1, 6)):
            piv.append(i)
    for j in range(n - 1, n - 49, -1):
        for p in reversed(piv):
            if p < j - 80: break
            if p < j - 5 and O[j]["l"] < O[p]["l"] < O[j]["c"]:
                return O[j]["t"]
    return None


def analyse(sym, vol):
    D, F, O = klines(sym, "1d", 120), klines(sym, "4h", 300), klines(sym, "1h", 500)
    sd, sf = struct(D, 3), struct(F, 4)
    px = O[-1]["c"]
    disc, pos = None, None
    if sf["h1"] and sf["l1"]:
        lo, hi = min(sf["h1"]["v"], sf["l1"]["v"]), max(sf["h1"]["v"], sf["l1"]["v"])
        pos = (px - lo) / (hi - lo) * 100
        disc = 0 <= pos <= 50  # outside the range does not count as discount
    below = sorted([x["v"] for x in sf["L"] if x["v"] < px], reverse=True)
    above = sorted([x["v"] for x in sf["H"] if x["v"] > px])
    ssl = below[0] if below else None
    bsl = above[0] if above else None
    sw = last_sweep(O)
    dist = (px / ssl - 1) * 100 if ssl else None
    score = sd["pts"] + sf["pts"] + (1 if disc else 0) + (2 if sw else 0) + (1 if dist is not None and dist <= 3 else 0)
    return dict(sym=sym, px=px, sd=sd["k"], sf=sf["k"], pos=pos, disc=disc, ssl=ssl, bsl=bsl,
                sw=sw, score=score, vol=vol)


def fmt(v):
    return f"{v:.2f}" if v >= 100 else (f"{v:.3f}" if v >= 1 else f"{v:.5f}")


def pool():
    t = binance("/api/v3/ticker/24hr")
    rows = []
    for x in t:
        s = x["symbol"]
        if not s.endswith("USDT"): continue
        base = s[:-4]
        if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")): continue
        rows.append((s, float(x["quoteVolume"])))
    rows.sort(key=lambda r: -r[1])
    return rows[:POOL]


def entry_text(i, r):
    base = r["sym"][:-4]
    if r["pos"] is None: zone = "-"
    elif r["pos"] < 0: zone = "below the 4H range"
    elif r["pos"] > 100: zone = "above the 4H range"
    else: zone = f"{r['pos']:.0f}% {'discount' if r['disc'] else 'premium'}"
    sw = datetime.fromtimestamp(r["sw"] / 1000, timezone.utc).strftime("%d %b %H:%M UTC") if r["sw"] else "none"
    ssl = f"{fmt(r['ssl'])} ({(r['ssl']/r['px']-1)*100:+.1f}%)" if r["ssl"] else "none in view"
    bsl = f"{fmt(r['bsl'])} ({(r['bsl']/r['px']-1)*100:+.1f}%)" if r["bsl"] else "none in view"
    return (f"{i}. {base}  score {r['score']}  ${fmt(r['px'])}\n"
            f"   Daily: {r['sd']} | 4H: {r['sf']} | 4H zone: {zone}\n"
            f"   Sweep: {sw}\n"
            f"   SSL (below): {ssl}   BSL (above): {bsl}\n"
            f"   https://www.tradingview.com/chart/?symbol=BINANCE:{r['sym']}")


def build_messages(results, scanned, failed):
    now = datetime.now(TZ).strftime("%d %b %Y, %H:%M")
    watch = sorted([r for r in results if r["score"] >= WATCH_MIN], key=lambda r: (-r["score"], -r["vol"]))[:MAX_WATCH]
    head = f"Watch list, {now} Tashkent\nScanned the top {scanned} USDT pairs by 24h volume. {len(watch)} coin(s) score \"Watch\"."
    if failed: head += f"\n{failed} pair(s) could not be loaded."
    foot = "Ranking only, not a buy signal. Check CHoCH, OB/FVG and net R:R yourself."
    if not watch:
        return [head + "\n\nNo coin reaches Watch today. Wait.\n\n" + foot]
    msgs, cur = [], head + "\n"
    for i, r in enumerate(watch, 1):
        block = "\n" + entry_text(i, r) + "\n"
        if len(cur) + len(block) > 3800:
            msgs.append(cur); cur = ""
        cur += block
    msgs.append(cur + "\n" + foot)
    return msgs


def run_scan():
    rows = pool()
    out, failed = [], 0

    def one(item):
        try:
            return analyse(item[0], item[1])
        except Exception as e:
            print("failed", item[0], e, file=sys.stderr)
            return None
    with ThreadPoolExecutor(6) as ex:
        for r in ex.map(one, rows):
            if r: out.append(r)
            else: failed += 1
    return build_messages(out, len(rows), failed)


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    return http(f"https://api.telegram.org/bot{TOKEN}/{method}", data)


def send(chat, text):
    tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview="true")


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN is not set")
    if MANUAL:
        if not ALLOWED: sys.exit("ALLOWED_CHAT_ID is not set")
        for m in run_scan(): send(ALLOWED, m)
        return
    upd = tg("getUpdates", timeout=0).get("result", [])
    if not upd: return
    tg("getUpdates", offset=upd[-1]["update_id"] + 1, timeout=0)  # mark as read
    want_scan = False
    for u in upd:
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip().lower()
        if not chat: continue
        if chat != ALLOWED:
            send(chat, f"Your Telegram ID is {chat}. Save it as the ALLOWED_CHAT_ID secret on GitHub to use this bot.")
        elif text.startswith("/scan"):
            want_scan = True
        elif text.startswith("/start") or text.startswith("/help"):
            send(chat, "Send /scan to get the current Watch list. Replies can take 5 to 15 minutes.")
    if want_scan:
        send(ALLOWED, "Scanning now, this takes about a minute.")
        for m in run_scan(): send(ALLOWED, m)


if __name__ == "__main__":
    main()
