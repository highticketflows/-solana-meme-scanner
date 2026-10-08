#!/usr/bin/env python3
"""
Solana Meme Coin Scanner + Telegram Alert Bot
SIGNALS ONLY. This program never trades and never touches a wallet.
Runs on GitHub Actions every ~5 minutes. Uses only Python's standard library.
"""
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- config ---
BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
JUP_KEY = (os.environ.get("JUPITER_API_KEY") or "").strip()  # optional
RPC_URLS = [u for u in [
    (os.environ.get("SOLANA_RPC_URL") or "").strip(),  # optional
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
] if u]
try:
    LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS") or 200)
except ValueError:
    LISTEN_SECONDS = 200
TZ_NAME = os.environ.get("SCANNER_TIMEZONE") or "Australia/Melbourne"

STATE_FILE = "state.json"
WSOL = "So11111111111111111111111111111111111111112"
MAX_EVAL_PER_RUN = 25      # max new tokens deep-checked per run
RESCAN_AFTER_SEC = 600     # re-check an unfinished token after 10 min
MAX_WATCHLIST = 20

DEFAULT_STATE = {
    "paused": False,
    "settings": {"min_liquidity": 5000, "max_age_min": 120},
    "watchlist": [],
    "history": [],
    "seen": {},
    "latest": [],
    "tg_offset": 0,
    "last_scan": None,
    "welcomed": False,
}

EMOJI = {"BUY": "🟢", "HOLD": "🟡", "AVOID": "🔴"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------- http ---
def http2(url, data=None, headers=None, timeout=15, retries=2):
    """Returns (json_or_None, http_code)."""
    h = {"User-Agent": "meme-scanner/1.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    safe = re.sub(r"bot\d+:[\w-]+", "bot***", url)[:100]
    code = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), r.status
        except urllib.error.HTTPError as e:
            code = e.code
            if e.code in (429, 500, 502, 503, 504) and i < retries:
                time.sleep(2 * (i + 1))
                continue
            log(f"HTTP {e.code} {safe}")
            return None, code
        except Exception as e:  # network error, bad JSON, timeout...
            code = 0
            if i < retries:
                time.sleep(1.5)
                continue
            log(f"ERROR {type(e).__name__} {safe}")
            return None, code
    return None, code


def http(*a, **k):
    return http2(*a, **k)[0]


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ state ---
def load_state():
    state = json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "settings" and isinstance(v, dict):
                state["settings"].update(v)
            else:
                state[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Could not read {STATE_FILE} ({e}); starting fresh")
    return state


def save_state(state):
    clean = {k: v for k, v in state.items() if not k.startswith("_")}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=1)


def local_time(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo(TZ_NAME)).strftime("%d %b %H:%M")
    except Exception:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M UTC")


# --------------------------------------------------------------- telegram ---
def tg(method, params=None, timeout=30):
    return http(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                data=params or {}, timeout=timeout, retries=1)


def send(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram not configured")
        return
    for i in range(0, len(text), 3900):
        tg("sendMessage", {"chat_id": CHAT_ID, "text": text[i:i + 3900],
                           "disable_web_page_preview": True})


# ------------------------------------------------------------ data sources ---
def parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def gt_new_pools(pages=3):
    """Newest Solana pools from GeckoTerminal (free, no key)."""
    out = []
    for page in range(1, pages + 1):
        j = http(f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}")
        for p in (j or {}).get("data", []) or []:
            try:
                a = p["attributes"]
                mint = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                out.append({"mint": mint,
                            "liq": fnum(a.get("reserve_in_usd")),
                            "created": parse_iso(a.get("pool_created_at"))})
            except Exception:
                continue
        time.sleep(2)  # stay under their rate limit
    return out


def ds_profiles():
    """Newest token profiles from DexScreener (free, no key)."""
    j = http("https://api.dexscreener.com/token-profiles/latest/v1")
    if not isinstance(j, list):
        return []
    return [x["tokenAddress"] for x in j
            if isinstance(x, dict) and x.get("chainId") == "solana" and x.get("tokenAddress")]


def ds_best_pair(mint):
    j = http(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
    pairs = [p for p in (j if isinstance(j, list) else []) if isinstance(p, dict)
             and p.get("chainId") == "solana"]
    base = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    pairs = base or pairs
    if not pairs:
        return None
    return max(pairs, key=lambda p: fnum((p.get("liquidity") or {}).get("usd")))


def rpc_mint_info(mint):
    """On-chain truth for mint/freeze authority. Returns None if RPC failed."""
    for url in RPC_URLS:
        j = http(url, data={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                            "params": [mint, {"encoding": "jsonParsed"}]}, retries=1)
        try:
            info = j["result"]["value"]["data"]["parsed"]["info"]
            return {"mint_auth": info.get("mintAuthority"),
                    "freeze_auth": info.get("freezeAuthority")}
        except Exception:
            continue
    return None


def rugcheck_report(mint):
    return http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=20, retries=1)


def rugcheck_lock_pct(mint, rc):
    s = http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=15, retries=1)
    if isinstance(s, dict) and s.get("lpLockedPct") is not None:
        return fnum(s["lpLockedPct"])
    vals = []
    for m in (rc or {}).get("markets") or []:
        v = (m.get("lp") or {}).get("lpLockedPct") if isinstance(m, dict) else None
        if v is not None:
            vals.append(fnum(v))
    return max(vals) if vals else None


def jup_check(mint):
    """Simulated buy then sell via Jupiter price quotes (no wallet, no trade)."""
    bases = []
    if JUP_KEY:
        bases.append(("https://api.jup.ag/swap/v1/quote", {"x-api-key": JUP_KEY}))
    bases.append(("https://lite-api.jup.ag/swap/v1/quote", {}))
    amt = 20_000_000  # 0.02 SOL
    for base, hdr in bases:
        buy, code = http2(f"{base}?inputMint={WSOL}&outputMint={mint}&amount={amt}&slippageBps=1000",
                          headers=hdr, retries=1)
        if buy and buy.get("outAmount"):
            out = int(buy["outAmount"])
            if out <= 0:
                return {"status": "no_buy_route"}
            sell, code2 = http2(f"{base}?inputMint={mint}&outputMint={WSOL}&amount={out}&slippageBps=1000",
                                headers=hdr, retries=1)
            if sell and sell.get("outAmount"):
                back = int(sell["outAmount"])
                return {"status": "ok", "loss_pct": max(0.0, (1 - back / amt) * 100)}
            if code2 in (400, 404):
                return {"status": "no_sell_route"}
            continue
        if code in (400, 404):
            return {"status": "no_buy_route"}
    return {"status": "unavailable"}


# ---------------------------------------------------------------- scoring ---
def rc_auth(rc, key):
    """(known?, value) for mintAuthority / freezeAuthority in a RugCheck report."""
    if not isinstance(rc, dict):
        return False, None
    if key in rc:
        return True, rc.get(key)
    tok = rc.get("token")
    if isinstance(tok, dict) and key in tok:
        return True, tok.get(key)
    return False, None


def score_token(liq, has_web, has_soc, auth, rc, lp_pct, jup):
    parts, flags, unver = {}, [], []

    # +20 mint authority locked, +15 freeze authority disabled
    for key, label, pts, rckey, akey in (
            ("mint", "Mint authority", 20, "mintAuthority", "mint_auth"),
            ("freeze", "Freeze authority", 15, "freezeAuthority", "freeze_auth")):
        if auth is not None:
            known, val = True, auth.get(akey)
        else:
            known, val = rc_auth(rc, rckey)
        if not known:
            parts[key] = 0
            unver.append(label)
        elif val is None:
            parts[key] = pts
        else:
            parts[key] = 0
            flags.append(f"{label} still active")

    # +20 liquidity locked and >= $10k
    if lp_pct is None:
        parts["lp"] = 0
        unver.append("Liquidity lock")
    else:
        parts["lp"] = 20 if (lp_pct >= 80 and liq >= 10000) else 0

    # +15 dev wallet clean
    if not isinstance(rc, dict):
        parts["dev"] = 0
        unver.append("Dev wallet history")
    else:
        names = [str(r.get("name", "")).lower() for r in (rc.get("risks") or [])
                 if isinstance(r, dict)]
        bad = rc.get("rugged") is True or any("creator history" in n or "rugged" in n for n in names)
        parts["dev"] = 0 if bad else 15
        if bad:
            flags.append("Dev/creator has rug history (or token already rugged)")

    # +15 honeypot pass, +10 tax under 10%
    tf = (rc or {}).get("transferFee") if isinstance(rc, dict) else None
    fee = fnum(tf.get("pct")) if isinstance(tf, dict) else 0.0
    st = (jup or {}).get("status", "unavailable")
    if st == "ok":
        parts["honeypot"] = 15
        loss = jup.get("loss_pct", 0.0)
        if loss >= 15 or fee >= 10:
            parts["tax"] = 0
            flags.append(f"High buy/sell tax or slippage (~{max(loss, fee):.0f}%)")
        else:
            parts["tax"] = 10
    else:
        parts["honeypot"] = 0
        parts["tax"] = 0
        if st == "no_sell_route":
            flags.append("Buy route exists but no SELL route (possible honeypot)")
        else:
            unver.append("Honeypot test")
            unver.append("Buy/sell tax")
        if fee >= 10:
            flags.append(f"Transfer fee {fee:.0f}%")

    # +5 website + socials
    parts["links"] = 5 if (has_web and has_soc) else 0

    return sum(parts.values()), parts, flags, unver


def tier_for(score):
    return "Safe" if score >= 75 else ("Moderate" if score >= 60 else "High Risk")


def signal_for(score, parts, flags, unver, liq, min_liq):
    if flags or score < 60:
        return "AVOID"
    if score >= 75 and parts.get("dev") == 15 and "Honeypot test" not in unver and liq >= min_liq:
        return "BUY"
    return "HOLD"


def confidence_for(score, unver):
    if not unver and score >= 85:
        return "High"
    if len(unver) <= 2 and score >= 70:
        return "Med"
    return "Low"


def evaluate(mint, min_liq, max_age=None):
    """Returns (result_or_None, reason)."""
    pair = ds_best_pair(mint)
    if not pair:
        return None, "no_pair"
    liq = fnum((pair.get("liquidity") or {}).get("usd"))
    created = fnum(pair.get("pairCreatedAt")) / 1000 or None
    age = (time.time() - created) / 60 if created else None
    if max_age is not None and age is not None and age > max_age:
        return None, "too_old"
    if liq < min_liq:
        return None, "low_liq"

    info = pair.get("info") or {}
    has_web = bool(info.get("websites"))
    has_soc = bool(info.get("socials"))

    auth = rpc_mint_info(mint)
    rc = rugcheck_report(mint)
    rc = rc if isinstance(rc, dict) else None
    lp_pct = rugcheck_lock_pct(mint, rc) if rc is not None else None
    jup = jup_check(mint)

    score, parts, flags, unver = score_token(liq, has_web, has_soc, auth, rc, lp_pct, jup)
    base = pair.get("baseToken") or {}
    res = {
        "mint": mint,
        "symbol": str(base.get("symbol") or "???").lstrip("$").upper(),
        "price": fnum(pair.get("priceUsd")),
        "liq": liq,
        "mcap": fnum(pair.get("marketCap")) or fnum(pair.get("fdv")),
        "age_min": age,
        "url": pair.get("url") or f"https://dexscreener.com/solana/{pair.get('pairAddress', mint)}",
        "score": score, "parts": parts, "flags": flags, "unverified": unver,
        "tier": tier_for(score),
        "signal": signal_for(score, parts, flags, unver, liq, min_liq),
        "confidence": confidence_for(score, unver),
        "t": time.time(),
    }
    time.sleep(0.3)
    return res, "ok"


# --------------------------------------------------------------- messages ---
def fmt_price(p):
    if p >= 1:
        return f"{p:,.4f}"
    if p >= 0.001:
        return f"{p:.6f}"
    return f"{p:.10f}".rstrip("0") or "0"


def fmt_age(a):
    if a is None:
        return "unknown"
    return f"{a:.0f} min" if a < 180 else f"{a / 60:.1f} h"


def format_alert(r):
    lines = [
        f"{EMOJI[r['signal']]} {r['signal']} SIGNAL — ${r['symbol']}",
        f"Price: ${fmt_price(r['price'])}",
        f"Liquidity: ${r['liq']:,.0f}",
        f"Market Cap: ${r['mcap']:,.0f}",
        f"Safety: {r['score']}/100 — {r['tier']}",
        f"Age: {fmt_age(r['age_min'])}",
        f"Chart: {r['url']}",
        f"Confidence: {r['confidence']}",
    ]
    if r["flags"]:
        lines.append("⚠️ Red flags: " + "; ".join(r["flags"]))
    if r["unverified"]:
        lines.append("❔ Could not check: " + ", ".join(r["unverified"]))
    lines.append(f"Mint: {r['mint']}")
    lines.append("Signal only — not financial advice. Do your own research.")
    return "\n".join(lines)


def record_history(state, r):
    now = time.time()
    state["history"].append({"t": now, "symbol": r["symbol"], "mint": r["mint"],
                             "signal": r["signal"], "score": r["score"], "liq": r["liq"]})
    state["history"] = [h for h in state["history"] if now - h["t"] <= 86400][-500:]


def push_latest(state, r):
    item = {"t": r["t"], "symbol": r["symbol"], "mint": r["mint"], "signal": r["signal"],
            "score": r["score"], "liq": r["liq"]}
    state["latest"] = ([item] + [x for x in state["latest"] if x.get("mint") != r["mint"]])[:10]


# ------------------------------------------------------------------- scan ---
def scan(state):
    st = state["settings"]
    min_liq = fnum(st.get("min_liquidity"), 5000)
    max_age = fnum(st.get("max_age_min"), 120)
    now = time.time()
    seen = state["seen"]
    for m in [m for m, v in seen.items() if now - v.get("t", 0) > 48 * 3600]:
        del seen[m]

    cands = []
    for p in gt_new_pools():
        age = (now - p["created"]) / 60 if p["created"] else None
        if age is not None and age > max_age:
            continue
        if p["liq"] < min_liq:
            continue
        if p["mint"] not in cands:
            cands.append(p["mint"])
    for m in ds_profiles():
        if m not in cands:
            cands.append(m)
    log(f"{len(cands)} candidates")

    evals = alerts = 0
    for mint in cands:
        if evals >= MAX_EVAL_PER_RUN:
            break
        sv = seen.get(mint)
        if sv and (sv.get("done") or now - sv.get("t", 0) < RESCAN_AFTER_SEC):
            continue
        evals += 1
        sv = sv or {"n": 0, "tries": 0, "alerted": False, "done": False}
        sv["t"] = now
        try:
            res, reason = evaluate(mint, min_liq, max_age)
        except Exception as e:
            log(f"evaluate failed for {mint[:6]}: {e}")
            res, reason = None, "error"
        if res is None:
            sv["tries"] += 1
            if reason == "too_old" or sv["tries"] >= 6:
                sv["done"] = True
            seen[mint] = sv
            continue
        sv["n"] += 1
        sv["signal"] = res["signal"]
        if not res["unverified"] or sv["n"] >= 3:
            sv["done"] = True
        push_latest(state, res)
        if res["signal"] == "BUY" and not sv["alerted"]:
            send(format_alert(res))
            sv["alerted"] = True
            record_history(state, res)
            alerts += 1
        seen[mint] = sv

    # watchlist: alert whenever a watched token's signal changes
    for w in state["watchlist"][:MAX_WATCHLIST]:
        try:
            res, _ = evaluate(w["mint"], 0, None)
        except Exception as e:
            log(f"watch eval failed {w.get('symbol')}: {e}")
            continue
        if not res:
            continue
        prev = w.get("signal")
        w.update({"signal": res["signal"], "score": res["score"], "price": res["price"],
                  "liq": res["liq"], "t": now})
        if prev != res["signal"]:
            send("👁 WATCHLIST UPDATE\n" + format_alert(res))
            record_history(state, res)

    state["last_scan"] = time.time()
    log(f"scan done: {evals} checked, {alerts} BUY alerts")


# --------------------------------------------------------------- commands ---
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

HELP = (
    "🤖 Meme Coin Scanner — commands\n"
    "status — latest results + watchlist\n"
    "watch SYMBOL — add to watchlist (or paste a mint address)\n"
    "unwatch SYMBOL — remove\n"
    "min_liquidity 5000 — minimum liquidity to scan\n"
    "max_age 120 — ignore tokens older than this many minutes\n"
    "pause / resume — stop / start scanning\n"
    "history — signals from the last 24h\n"
    "settings — show current settings\n"
    "Signals only. This bot never trades."
)


def resolve_token(arg):
    arg = arg.lstrip("$")
    if B58.match(arg):
        pair = ds_best_pair(arg)
        sym = str((pair or {}).get("baseToken", {}).get("symbol") or arg[:6]).upper()
        return sym, arg, None
    j = http("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(arg))
    want = arg.upper()
    pairs = [p for p in ((j or {}).get("pairs") or []) if p.get("chainId") == "solana"
             and str((p.get("baseToken") or {}).get("symbol", "")).upper() == want]
    if not pairs:
        return None, None, None
    best = max(pairs, key=lambda p: fnum((p.get("liquidity") or {}).get("usd")))
    mints = {p["baseToken"]["address"] for p in pairs}
    note = None
    if len(mints) > 1:
        note = (f"Heads up: {len(mints)} different tokens use the symbol {want}. "
                "I picked the one with the most liquidity. Send the mint address to pick a specific one.")
    return want, best["baseToken"]["address"], note


def to_number(s):
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def status_text(state):
    st = state["settings"]
    lines = [f"📡 Scanner: {'⏸ PAUSED' if state['paused'] else '✅ ON'}"]
    if state.get("last_scan"):
        lines.append(f"Last scan: {local_time(state['last_scan'])}")
    lines.append(f"Min liquidity: ${fnum(st['min_liquidity']):,.0f} | Max age: {fnum(st['max_age_min']):.0f} min")
    lines.append("\nLatest results:")
    if state["latest"]:
        for x in state["latest"][:6]:
            lines.append(f"{EMOJI[x['signal']]} ${x['symbol']} — {x['score']}/100 — "
                         f"${x['liq']:,.0f} liq — {local_time(x['t'])}")
    else:
        lines.append("Nothing checked yet.")
    lines.append(f"\nWatchlist ({len(state['watchlist'])}):")
    if state["watchlist"]:
        for w in state["watchlist"]:
            if w.get("signal"):
                lines.append(f"{EMOJI[w['signal']]} ${w['symbol']} — {w['score']}/100 — "
                             f"${fmt_price(fnum(w.get('price')))} — ${fnum(w.get('liq')):,.0f} liq")
            else:
                lines.append(f"⚪ ${w['symbol']} — not scanned yet")
    else:
        lines.append("Empty. Use: watch SYMBOL")
    return "\n".join(lines)


def history_text(state):
    now = time.time()
    rows = [h for h in state["history"] if now - h["t"] <= 86400]
    if not rows:
        return "No signals in the last 24 hours."
    lines = [f"🕒 Signals, last 24h ({len(rows)}):"]
    for h in reversed(rows[-25:]):
        lines.append(f"{local_time(h['t'])} {EMOJI[h['signal']]} ${h['symbol']} — "
                     f"{h['score']}/100 — ${h['liq']:,.0f} liq")
    return "\n".join(lines)


def handle(state, text):
    parts = text.strip().lstrip("/").split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "settings":
        sub = args[0].lower() if args else ""
        if sub in ("min_liquidity", "max_age"):
            cmd, args = sub, args[1:]
        elif sub == "telegram":
            return ("Your Telegram chat ID is stored as the GitHub secret TELEGRAM_CHAT_ID "
                    "(repo → Settings → Secrets and variables → Actions). Change it there.")
        else:
            st = state["settings"]
            return (f"⚙️ Settings\nmin_liquidity: ${fnum(st['min_liquidity']):,.0f}\n"
                    f"max_age: {fnum(st['max_age_min']):.0f} min\n"
                    f"scanning: {'paused' if state['paused'] else 'on'}")

    if cmd in ("help", "commands"):
        return HELP
    if cmd == "start":
        state["paused"] = False
        state["_scan_now"] = True
        return "✅ Scanning is ON. Running a scan now.\n\n" + HELP
    if cmd == "resume":
        state["paused"] = False
        state["_scan_now"] = True
        return "▶️ Resumed. Running a scan now."
    if cmd == "pause":
        state["paused"] = True
        return "⏸ Paused. Send 'resume' to continue."
    if cmd == "status":
        return status_text(state)
    if cmd == "history":
        return history_text(state)
    if cmd in ("min_liquidity", "max_age"):
        n = to_number(args[0]) if args else None
        if n is None or n < 0:
            return f"Usage: {cmd} 5000" if cmd == "min_liquidity" else "Usage: max_age 120"
        if cmd == "min_liquidity":
            state["settings"]["min_liquidity"] = n
            return f"✅ min_liquidity set to ${n:,.0f}"
        state["settings"]["max_age_min"] = n
        return f"✅ max_age set to {n:.0f} minutes"
    if cmd == "watch":
        if not args:
            return "Usage: watch SYMBOL   (or paste a mint address)"
        if len(state["watchlist"]) >= MAX_WATCHLIST:
            return f"Watchlist is full ({MAX_WATCHLIST}). Use unwatch first."
        sym, mint, note = resolve_token(args[0])
        if not mint:
            return f"Couldn't find a Solana token with symbol {args[0].upper()}. Try pasting the mint address."
        if any(w["mint"] == mint for w in state["watchlist"]):
            return f"${sym} is already on your watchlist."
        state["watchlist"].append({"symbol": sym, "mint": mint})
        msg = f"👁 Added ${sym} to your watchlist. I'll alert you when its signal changes."
        return msg + (("\n" + note) if note else "")
    if cmd == "unwatch":
        if not args:
            return "Usage: unwatch SYMBOL"
        key = args[0].lstrip("$")
        before = len(state["watchlist"])
        state["watchlist"] = [w for w in state["watchlist"]
                              if w["symbol"].upper() != key.upper() and w["mint"] != key]
        return (f"Removed {key.upper()} from your watchlist." if len(state["watchlist"]) < before
                else f"{key.upper()} wasn't on your watchlist.")
    return "Unknown command. Send 'help' to see what I understand."


def poll_commands(state, timeout):
    r = tg("getUpdates", {"offset": state["tg_offset"], "timeout": timeout,
                          "allowed_updates": ["message"]}, timeout=timeout + 15)
    if not r or not r.get("ok"):
        return 0
    n = 0
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT_ID:
            continue  # ignore anyone who isn't you
        text = (m.get("text") or "").strip()
        if not text:
            continue
        try:
            reply = handle(state, text)
        except Exception as e:
            log(f"command error: {e}")
            reply = "Sorry, that command hit an error. Try again."
        send(reply)
        n += 1
    return n


# ------------------------------------------------------------------- main ---
def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: Missing secrets. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "in repo Settings → Secrets and variables → Actions.")
        sys.exit(1)

    start = time.time()
    state = load_state()
    if not state.get("welcomed"):
        send("✅ Meme Coin Scanner connected.\nSignals only — I never trade.\n\n" + HELP)
        state["welcomed"] = True
    poll_commands(state, 0)          # catch up on anything sent while we were off
    save_state(state)

    def do_scan():
        try:
            scan(state)
        except Exception:
            log("scan crashed:\n" + traceback.format_exc())
        save_state(state)

    if state["paused"]:
        log("paused - skipping scan")
    else:
        do_scan()
    state.pop("_scan_now", None)

    # stay online for a while so your commands get answered within seconds
    while LISTEN_SECONDS > 0:
        remaining = LISTEN_SECONDS - (time.time() - start)
        if remaining < 6:
            break
        poll_commands(state, int(min(25, remaining - 3)))
        if state.pop("_scan_now", False) and not state["paused"]:
            do_scan()
        save_state(state)
    save_state(state)
    log("run complete")


if __name__ == "__main__":
    main()
