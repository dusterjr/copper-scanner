"""
Copper weekly scanner for Kalshi's KXCOPPERW contracts, run every hour by GitHub Actions.

Each run:
  1. reads your Telegram replies and commands (Seen, Placed all, /price, /weekday, /status),
  2. scans the open week's ladder: rescaled method for bets, plain count as paper only,
     starting from COMEX copper's latest price,
  3. writes data/latest.json for the page and appends to the logs in data/log/,
  4. sends Telegram alerts: weekends only, unless weekday alerts are switched on.

Kalshi is read only: public data, no API key, no orders. You place every bet yourself.
"""
import csv
import datetime as dt
import json
import math
import os
import re
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:  # the workflow installs it; tests swap in a fake
    yf = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "log"
LATEST = DATA / "latest.json"
STATE = DATA / "state.json"
HISTORY = DATA / "copper_history.csv"

SERIES = "KXCOPPERW"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
TG_BASE = "https://api.telegram.org"
COPPER = "HG=F"
HIST_START = "2018-12-31"      # history windows start here, same as the backtest
DOWNLOAD_START = "2018-10-01"  # a few extra weeks so the first window has a daily move
ET = ZoneInfo("America/New_York")


def env(name, default=""):
    value = (os.environ.get(name) or "").strip()
    return value or default


THRESHOLD = float(env("THRESHOLD", "0.04"))                  # your rule: edge of 4c or more
WEEKDAY_THRESHOLD = float(env("WEEKDAY_THRESHOLD", "0.04"))  # bar for weekday picks
WEEKDAY_ALERTS_VAR = env("WEEKDAY_ALERTS").lower()           # GitHub variable: "true" forces weekday alerts on
DISPLAY_TZ = ZoneInfo(env("DISPLAY_TZ", "America/Chicago"))  # times in Telegram messages
_REPO = env("GITHUB_REPOSITORY", "dusterjr/copper-scanner")
PAGE_URL = env("PAGE_URL", f"https://{_REPO.split('/')[0]}.github.io/{_REPO.split('/')[-1]}/")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

session = requests.Session()
session.headers.update({"Accept": "application/json"})
tg_session = requests.Session()


class StopScan(Exception):
    """An expected pause (for example, next week's contracts aren't listed yet). Not a failure."""


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def sleep(seconds):
    time.sleep(seconds)


def iso(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if t else None


def ts(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def fmt(t):
    return t.astimezone(DISPLAY_TZ).strftime("%a %-I:%M %p %Z")


def num(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


# ---- Kalshi ---------------------------------------------------------------------------
def get(path, params=None, tries=6):
    err = ""
    for attempt in range(tries):
        try:
            r = session.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as e:
            err = str(e)
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Kalshi {path} -> {err}")
        sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Kalshi didn't respond ({err})")


def paged(path, key, params):
    items, cursor, seen = [], "", set()
    while True:
        page = get(path, {**params, **({"cursor": cursor} if cursor else {})}) or {}
        items += page.get(key) or []
        cursor = page.get("cursor") or ""
        if not cursor or cursor in seen:
            return items
        seen.add(cursor)


def week_markets(event_ticker):
    markets = paged("/markets", "markets", {"event_ticker": event_ticker, "limit": 1000})
    if not markets:
        markets = paged("/historical/markets", "markets", {"event_ticker": event_ticker, "limit": 1000})
    return markets


def week_date(ticker):
    """KXCOPPERW-26OCT0917 -> 2026-10-09, the day that week settles."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    return dt.datetime.strptime("".join(m.groups()), "%y%b%d").date() if m else None


def open_week(now):
    """The open week that settles next, with its strikes."""
    best = None
    for ev in paged("/events", "events", {"series_ticker": SERIES, "status": "open", "limit": 200}):
        markets = [m for m in week_markets(ev["event_ticker"])
                   if m.get("close_time") and ts(m["close_time"]) > now]
        if markets:
            close = min(ts(m["close_time"]) for m in markets)
            if best is None or close < best[1]:
                best = (ev["event_ticker"], close, markets)
    if best is None:
        raise StopScan("Next week's copper contracts aren't open yet.")
    return best


def last_settlement(open_ts):
    """Last week's settlement (the week whose close is this week's open): (week, price or None, note)."""
    target = open_ts.astimezone(ET).date()
    for status in ("settled", "closed"):
        events = paged("/events", "events", {"series_ticker": SERIES, "status": status, "limit": 200})
        for ev in [e for e in events if week_date(e["event_ticker"]) == target]:
            markets = week_markets(ev["event_ticker"])
            closes = [ts(m["close_time"]) for m in markets if m.get("close_time")]
            if not closes or abs((closes[0] - open_ts).total_seconds()) > 6 * 3600:
                continue
            values = [v for v in (num(m.get("expiration_value")) for m in markets) if v is not None]
            if status == "settled" and values:
                return ev["event_ticker"], values[0], "settled"
            return ev["event_ticker"], None, "not settled yet"
    return None, None, "not found"


# ---- Copper prices --------------------------------------------------------------------
def yahoo_daily(tries=3):
    if yf is None:
        raise RuntimeError("yfinance isn't installed")
    err = ""
    for attempt in range(tries):
        try:
            bars = yf.Ticker(COPPER).history(start=DOWNLOAD_START, interval="1d", auto_adjust=False)
            if len(bars):
                idx = pd.DatetimeIndex(bars.index)
                idx = idx.tz_localize(ET) if idx.tz is None else idx.tz_convert(ET)
                out = pd.DataFrame({"date": pd.to_datetime(idx.strftime("%Y-%m-%d")),
                                    "close": bars["Close"].to_numpy(float)})
                return out.dropna()
            err = "no rows returned"
        except Exception as e:
            err = str(e)
        if attempt < tries - 1:
            sleep(10 * (attempt + 1))
    raise RuntimeError(f"Yahoo daily prices unavailable ({err})")


def completed_cutoff(now):
    """Daily closes dated before this are final: a trading day ends at 5 PM New York time."""
    t = now.astimezone(ET)
    return pd.Timestamp(t.date()) + pd.Timedelta(days=1 if t.hour >= 17 else 0)


def copper_daily(now):
    """Daily closes since 2018-10: fresh from Yahoo when possible, otherwise the stored copy."""
    cutoff = completed_cutoff(now)
    stored = (pd.read_csv(HISTORY, parse_dates=["date"]) if HISTORY.exists()
              else pd.DataFrame({"date": pd.to_datetime([]), "close": []}))
    try:
        fresh = yahoo_daily()
        fresh = fresh[fresh.date < cutoff]  # completed days only
        merged = (pd.concat([stored[stored.date < fresh.date.min()], fresh])
                  .drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True))
        merged.to_csv(HISTORY, index=False, date_format="%Y-%m-%d", float_format="%.6f")
        return merged, "yahoo"
    except Exception as e:
        if len(stored) < 300:
            raise RuntimeError(f"no copper price history ({e})")
        print(f"Using the stored copper history: {e}")
        return stored.sort_values("date").reset_index(drop=True), "stored"


def comex_last_price(as_of, tries=3):
    """COMEX copper's last hourly price (Yahoo HG=F) from a bar ending by `as_of`: (price, time) or (None, None)."""
    if yf is None:
        return None, None
    err = ""
    for attempt in range(tries):
        try:
            bars = yf.Ticker(COPPER).history(period="1mo", interval="60m", auto_adjust=False)
            if len(bars):
                idx = pd.DatetimeIndex(bars.index)
                idx = idx.tz_localize(ET) if idx.tz is None else idx.tz_convert(ET)
                end = idx + pd.Timedelta(hours=1)
                close = bars["Close"].to_numpy(float)
                ok = np.asarray(end <= pd.Timestamp(as_of)) & ~np.isnan(close)
                if ok.any():
                    i = np.flatnonzero(ok)[-1]
                    return float(close[i]), end[i].to_pydatetime()
                err = "no hourly prices before now"
            else:
                err = "no rows returned"
        except Exception as e:
            err = str(e)
        if attempt < tries - 1:
            sleep(5 * (attempt + 1))
    print(f"Hourly copper price unavailable: {err}")
    return None, None


def prepare(daily, before):
    """Closes dated before `before`, with daily % changes and the 20-day daily move."""
    d = daily[daily.date < pd.Timestamp(before)].sort_values("date").reset_index(drop=True)
    d["ret"] = d.close.pct_change() * 100
    d["dm"] = d.ret.abs().rolling(20).mean()
    return d


def trading_hours(start, end):
    """COMEX copper hours between two times: Sun 6pm to Fri 5pm New York, minus the daily 5-6pm break."""
    hours = 0
    for h in pd.date_range(start, end, freq="h", inclusive="left").tz_convert(ET):
        wd, hr = h.weekday(), h.hour
        if wd == 5 or (wd == 6 and hr < 18) or (wd == 4 and hr >= 17) or hr == 17:
            continue
        hours += 1
    return hours


def samples(d, horizon):
    """Past `horizon`-day moves since 2018-12-31: rescaled (in daily-move units, trend removed) and plain (%)."""
    c, dm, dates = d.close.to_numpy(float), d.dm.to_numpy(float), d.date.to_numpy()
    i = np.arange(len(c) - horizon)
    ok = (dates[i] >= np.datetime64(HIST_START)) & ~np.isnan(dm[i])
    moves = (c[i + horizon] / c[i] - 1) * 100
    moves, dm_start = moves[ok], dm[i][ok]
    units = moves / dm_start
    return np.sort(units - units.mean()), np.sort(moves)


def p_above(sample, x):
    return 1 - np.searchsorted(sample, x, side="right") / len(sample)


# ---- Scoring --------------------------------------------------------------------------
def fee(price):
    return 0.07 * price * (1 - price)


def max_price(p_win, threshold):
    """Highest price (whole cents) that still clears the threshold after the fee."""
    c = p_win - threshold
    if c <= 0:
        return 0.0
    root = (1.07 - math.sqrt(1.07 ** 2 - 4 * 0.07 * c)) / (2 * 0.07)
    return math.floor(root * 100 + 1e-9) / 100


def evaluate(s, pcol, threshold):
    out = s.copy()
    p = out[pcol]
    out["no_ask"] = 1 - out.yes_bid
    yes_ok = out.yes_ask.between(0.0, 1.0, inclusive="neither")
    no_ok = out.yes_bid.between(0.0, 1.0, inclusive="neither")
    out["edge_yes"] = np.where(yes_ok, p - out.yes_ask - fee(out.yes_ask), -np.inf)
    out["edge_no"] = np.where(no_ok, (1 - p) - out.no_ask - fee(out.no_ask), -np.inf)
    out["side"] = np.where(out.edge_yes >= out.edge_no, "YES", "NO")
    out["edge"] = out[["edge_yes", "edge_no"]].max(axis=1)
    out["price"] = np.where(out.side == "YES", out.yes_ask, out.no_ask)
    out["p_win"] = np.where(out.side == "YES", p, 1 - p)
    out["offered"] = np.where(out.side == "YES", out.yes_ask_size, out.yes_bid_size)
    out["max_price"] = [max_price(x, threshold) for x in out.p_win]
    out["take"] = out.edge >= threshold
    return out


def pick_list(e):
    t = e[e["take"]]
    return [{"key": key_of(r.strike, r.side), "ticker": r.ticker, "strike": round(float(r.strike), 2),
             "side": r.side, "price": round(float(r.price), 4), "p_win": round(float(r.p_win), 6),
             "edge": round(float(r.edge), 6), "max_price": round(float(r.max_price), 2),
             "offered": None if pd.isna(r.offered) else float(r.offered)} for r in t.itertuples()]


def key_of(strike, side):
    return f"{float(strike):.2f}|{side}"


def weekday_alerts_on(state):
    return WEEKDAY_ALERTS_VAR == "true" or bool(state.get("weekday_alerts"))


def scan(now, state):
    week, close_ts, markets = open_week(now)
    open_ts = min(ts(m["open_time"]) for m in markets if m.get("open_time"))
    prev_week, settle_px, settle_note = last_settlement(open_ts)
    open_et = open_ts.astimezone(ET)
    reopen = (open_et + dt.timedelta(days=(6 - open_et.weekday()) % 7)).replace(
        hour=18, minute=0, second=0, microsecond=0)
    in_window = open_ts <= now < reopen
    mode = "weekend" if in_window else "weekday"
    days_left = trading_hours(max(now, open_ts), close_ts) / 23
    horizon = max(1, round(days_left))

    daily, hist_source = copper_daily(now)
    d = prepare(daily, completed_cutoff(now))
    dm_now = float(d.dm.iloc[-1])

    comex_px, comex_time = comex_last_price(now)
    comex_label = "COMEX, last price"
    if comex_px is None:
        comex_px, comex_time = float(d.close.iloc[-1]), None
        comex_label = f"COMEX, daily close {d.date.iloc[-1].date()}"
    override = state.get("price_override") or {}
    if override.get("week") == week and in_window and now < ts(override["until"]):
        spot, source = float(override["price"]), "telegram"
    else:
        spot, source = comex_px, "comex"

    units, moves = samples(d, horizon)
    rows = [{"ticker": m["ticker"], "strike": num(m["floor_strike"]),
             "yes_bid": num(m.get("yes_bid_dollars")), "yes_ask": num(m.get("yes_ask_dollars")),
             "yes_bid_size": num(m.get("yes_bid_size_fp")), "yes_ask_size": num(m.get("yes_ask_size_fp"))}
            for m in markets if m.get("strike_type") == "greater" and num(m.get("floor_strike")) is not None]
    s = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    dist = (s.strike - spot) / spot * 100
    s["p_rescaled"] = p_above(units, (dist / dm_now).to_numpy())
    s["p_plain"] = p_above(moves, dist.to_numpy())
    bar = THRESHOLD if in_window else WEEKDAY_THRESHOLD
    bets = evaluate(s, "p_rescaled", bar)
    plain = evaluate(s, "p_plain", THRESHOLD)

    def clean(v, nd):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)

    return {
        "version": 1, "status": "ok", "error": None, "series": SERIES,
        "run_utc": iso(now), "last_ok_utc": iso(now), "week": week,
        "open_utc": iso(open_ts), "close_utc": iso(close_ts), "reopen_utc": iso(reopen),
        "mode": mode, "in_window": in_window,
        "threshold": THRESHOLD, "weekday_threshold": WEEKDAY_THRESHOLD, "bar": bar,
        "weekday_alerts": weekday_alerts_on(state), "weekday_alerts_forced": WEEKDAY_ALERTS_VAR == "true",
        "spot": round(spot, 4), "spot_source": source,
        "comex": round(comex_px, 4), "comex_label": comex_label, "comex_time_utc": iso(comex_time),
        "settlement": settle_px, "settlement_week": prev_week, "settlement_note": settle_note,
        "daily_move": round(dm_now, 4), "horizon": horizon, "n_windows": int(len(units)),
        "history_source": hist_source, "history_last_date": str(d.date.iloc[-1].date()),
        "strikes": [{"ticker": r.ticker, "strike": round(float(r.strike), 2),
                     "yes_bid": clean(r.yes_bid, 4), "yes_ask": clean(r.yes_ask, 4),
                     "yes_bid_size": clean(r.yes_bid_size, 2), "yes_ask_size": clean(r.yes_ask_size, 2),
                     "p_rescaled": float(r.p_rescaled), "p_plain": float(r.p_plain)}
                    for r in s.itertuples()],
        "bets": pick_list(bets), "plain": pick_list(plain),
        "samples": {"rescaled": [round(float(v), 4) for v in units], "plain": [round(float(v), 4) for v in moves]},
        "repo": env("GITHUB_REPOSITORY", "dusterjr/copper-scanner"),
    }


# ---- Telegram ---------------------------------------------------------------------------
ACK_KEYBOARD = {"keyboard": [[{"text": "Seen"}, {"text": "Placed all"}]], "resize_keyboard": True}
HELP = ("Copper scanner commands:\n"
        "Seen: stop alerts for the bets I just sent\n"
        "Placed all: same, and record that you placed them\n"
        "placed 6.54 6.60: record specific strikes as placed\n"
        "/price 6.58: use Kalshi's copper price this weekend (/price clear to undo)\n"
        "/weekday on or /weekday off: weekday alerts (off by default)\n"
        "/status: the current bets\n"
        "I read messages once an hour, so replies can take up to an hour.")


def telegram_ready():
    return bool(TG_TOKEN and TG_CHAT)


def tg_call(method, **params):
    r = tg_session.post(f"{TG_BASE}/bot{TG_TOKEN}/{method}", json=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def tg_send(text, keyboard=None):
    if not telegram_ready():
        return False
    params = {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True}
    if keyboard == "acks":
        params["reply_markup"] = ACK_KEYBOARD
    elif keyboard == "remove":
        params["reply_markup"] = {"remove_keyboard": True}
    try:
        tg_call("sendMessage", **params)
        return True
    except Exception as e:
        print(f"Telegram message failed: {e}")
        return False


def cents(x):
    return f"{round(x * 100)}¢"


def bet_line(b):
    return (f"• Above {b['strike']:.2f}: Buy {b['side'].title()} at {cents(b['price'])} "
            f"(pay up to {cents(b['max_price'])})")


def log_ack(week, action, info, sent, now):
    path = LOG / "acks.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["message_utc", "read_utc", "week", "action", "strike", "side", "alert_price", "pay_at_most"])
        w.writerow([iso(sent), iso(now), week, action, info["strike"], info["side"], info["price"], info["max_price"]])


def ack(state, action, strikes, sent, now):
    week = state.get("current_week")
    wk = state.get("weeks", {}).get(week or "", {})
    alerted = wk.get("alerted", {})
    acked = wk.setdefault("acked", {})
    if strikes is not None:
        keys = [k for k in alerted if any(abs(alerted[k]["strike"] - x) < 1e-6 for x in strikes)]
        if not keys:
            return "I couldn't find those strikes in the bets I sent. Send /status to see them."
    else:
        keys = [k for k in alerted if acked.get(k) != "placed" and (action == "placed" or k not in acked)]
        if not keys:
            return "Nothing to mark right now: there are no alerted bets waiting."
    for k in keys:
        acked[k] = "placed" if action == "placed" or acked.get(k) == "placed" else "seen"
        log_ack(week, action, alerted[k], sent, now)
    names = ", ".join(f"{alerted[k]['strike']:.2f} {alerted[k]['side'].title()}" for k in keys)
    verb = "placed" if action == "placed" else "seen"
    return (f"Got it: marked {len(keys)} bet{'s' if len(keys) != 1 else ''} as {verb} ({names}). "
            "You'll only hear about new bets this week.")


def handle_message(text, sent, state, now, status_requests):
    low = " ".join(text.lower().split())
    window = state.get("window") or {}
    if low in ("seen", "seen ✅"):
        reply = ack(state, "seen", None, sent, now)
    elif low in ("placed all", "placed"):
        reply = ack(state, "placed", None, sent, now)
    elif low.startswith("placed "):
        strikes = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", low)]
        reply = ack(state, "placed", strikes, sent, now) if strikes else HELP
    elif low.startswith("/weekday"):
        words = low.split()[1:]
        if "on" in words:
            state["weekday_alerts"] = True
            reply = ("Weekday alerts are on. Remember the backtest found no reliable weekday edge; "
                     "send /weekday off to stop them.")
        elif "off" in words:
            state["weekday_alerts"] = False
            reply = "Weekday alerts are off. You'll only get weekend alerts."
            if WEEKDAY_ALERTS_VAR == "true":
                reply += " (They're still forced on by the WEEKDAY_ALERTS setting on GitHub.)"
        else:
            reply = (f"Weekday alerts are {'on' if weekday_alerts_on(state) else 'off'}. "
                     "Send /weekday on or /weekday off.")
    elif low.startswith("/price"):
        arg = low.split()[1] if len(low.split()) > 1 else ""
        if arg in ("clear", "off", "none"):
            state["price_override"] = None
            reply = "Cleared. Scans use COMEX's price again."
        elif not window.get("in_window"):
            reply = ("Copper is trading right now, so the scanner uses the live price. "
                     "/price only works from Friday 4 PM to Sunday 5 PM (Chicago).")
        elif num(arg) is None or not 1 <= num(arg) <= 20:
            reply = "Send it like this: /price 6.58"
        else:
            state["price_override"] = {"week": window.get("week"), "price": num(arg),
                                       "until": window.get("reopen_utc"), "set_utc": iso(now)}
            reply = (f"Using {num(arg):.2f} as copper's price for this weekend's scans, until "
                     f"{fmt(ts(window['reopen_utc']))}. Send /price clear to undo. Takes effect on the next scan.")
    elif low.startswith("/status"):
        status_requests.append(sent)
        return
    else:
        reply = HELP
    tg_send(reply)


def process_updates(state, now):
    """Read Telegram messages since the last run (only from your chat), before any alert goes out."""
    if not telegram_ready():
        return []
    try:
        updates = tg_call("getUpdates", offset=state.get("telegram_offset", 0), timeout=0,
                          allowed_updates=["message"])
    except Exception as e:
        print(f"Couldn't read Telegram messages: {e}")
        return []
    status_requests = []
    for u in updates:
        state["telegram_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(TG_CHAT):
            continue  # ignore anyone else who finds the bot
        text = (msg.get("text") or "").strip()
        if text:
            sent = dt.datetime.fromtimestamp(msg.get("date", 0), dt.timezone.utc)
            handle_message(text, sent, state, now, status_requests)
    return status_requests


DOT = "·"
RULE = f"{round(THRESHOLD * 100)}¢"


def week_label(r):
    return f"week ending {ts(r['close_utc']).astimezone(DISPLAY_TZ):%a %b %-d}"


def header(r):
    return f"Copper weekly {DOT} {week_label(r)}"


def price_line(r):
    if r["spot_source"] == "telegram":
        return f"Copper price used: {r['spot']:.2f} (your /price)."
    return (f"Copper price used: {r['spot']:.2f} (COMEX). Check it matches Kalshi; "
            "if not, send /price followed by Kalshi's number.")


def status_text(r):
    if r.get("status") != "ok":
        return f"The last scan failed ({r.get('error')}). The page shows the last good scan."
    lines = [header(r)]
    picks = r["bets"]
    label = "bets to take" if r["in_window"] else "weekday picks (paper only)"
    if picks:
        lines.append(f"{len(picks)} {label}:")
    else:
        lines.append(f"No bets right now: nothing clears {RULE}." if r["in_window"] else "No weekday picks right now.")
    lines += [bet_line(b) for b in picks]
    lines += [price_line(r), PAGE_URL]
    return "\n".join(lines)


def alerts(r, state, now):
    """Send the Friday summary and change alerts. Returns how many messages went out."""
    if not telegram_ready() or not (r["in_window"] or weekday_alerts_on(state)):
        return 0
    weeks = state.setdefault("weeks", {})
    wk = weeks.setdefault(r["week"], {"alerted": {}, "acked": {}, "summary_sent": False})
    take = {b["key"]: b for b in r["bets"]}
    acked = wk.setdefault("acked", {})

    if r["in_window"] and not wk.get("summary_sent"):
        if take:
            text = "\n".join([header(r), f"{len(take)} bet{'s' if len(take) != 1 else ''} to take (1 contract each):",
                              *[bet_line(b) for b in take.values()], price_line(r),
                              f"Window closes {fmt(ts(r['reopen_utc']))}. Tap Seen or Placed all when you've acted.",
                              PAGE_URL])
        else:
            text = "\n".join([header(r), f"No bets this weekend: nothing clears {RULE}.", price_line(r), PAGE_URL])
        if tg_send(text, "acks" if take else "remove"):
            wk["summary_sent"], wk["alerted"] = True, take
            return 1
        return 0

    alerted = wk.get("alerted", {})
    new = {k: v for k, v in take.items() if k not in alerted and k not in acked}
    dropped = {k: v for k, v in alerted.items() if k not in take and k not in acked}
    if not (new or dropped):
        wk["alerted"] = take
        return 0
    title = "Copper update" if r["in_window"] else "Copper weekday alert"
    lines = [f"{title} {DOT} {week_label(r)}"]
    if new:
        lines += ["New:", *[bet_line(b) for b in new.values()]]
    if dropped:
        lines += ["No longer worth it:", *[f"• Above {b['strike']:.2f} {b['side'].title()}" for b in dropped.values()]]
    if not r["in_window"]:
        lines.append("Weekday alerts are on (send /weekday off to stop). The backtest found no reliable weekday edge.")
    lines.append(PAGE_URL)
    if tg_send("\n".join(lines), "acks" if new else None):
        wk["alerted"] = take
        return 1
    return 0


# ---- Records ---------------------------------------------------------------------------
def append_csv(path, header_row, rows):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header_row)
        w.writerows(rows)


def log_run(r, now, alerts_sent, status, error=None):
    ok = status == "ok"
    append_csv(LOG / "runs.csv",
               ["run_utc", "week", "mode", "status", "spot", "spot_source", "comex", "settlement",
                "daily_move", "horizon", "n_bets", "n_plain", "alerts_sent", "error"],
               [[iso(now), r.get("week"), r.get("mode") if ok else "", status,
                 r.get("spot") if ok else "", r.get("spot_source") if ok else "", r.get("comex") if ok else "",
                 r.get("settlement") if ok else "", r.get("daily_move") if ok else "", r.get("horizon") if ok else "",
                 len(r.get("bets", [])) if ok else "", len(r.get("plain", [])) if ok else "", alerts_sent, error]])
    if ok:
        role = "bet" if r["in_window"] else "weekday_paper"
        rows = [[iso(now), r["mode"], "rescaled", role, b["strike"], b["side"], b["price"], b["p_win"],
                 b["edge"], b["max_price"], b["offered"], r["spot"], r["spot_source"]] for b in r["bets"]]
        rows += [[iso(now), r["mode"], "plain", "paper", b["strike"], b["side"], b["price"], b["p_win"],
                  b["edge"], b["max_price"], b["offered"], r["spot"], r["spot_source"]] for b in r["plain"]]
        if rows:
            append_csv(LOG / f"{r['week']}.csv",
                       ["run_utc", "mode", "method", "role", "strike", "side", "price", "p_win", "edge",
                        "max_price", "offered", "spot", "spot_source"], rows)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, sort_keys=False) + "\n")


def in_weekend_window_approx(now):
    t = now.astimezone(ET)
    return (t.weekday() == 4 and t.hour >= 17) or t.weekday() == 5 or (t.weekday() == 6 and t.hour < 18)


def main():
    DATA.mkdir(exist_ok=True)
    LOG.mkdir(exist_ok=True)
    now = now_utc()
    state = load_json(STATE, {})
    previous = load_json(LATEST, {})

    if telegram_ready() and not state.get("setup_done"):
        if tg_send("Setup complete. You'll get copper alerts on weekends (Friday 4 PM to Sunday 5 PM Chicago). "
                   "Send /help to see what you can reply.\n" + PAGE_URL, "remove"):
            state["setup_done"] = True

    status_requests = process_updates(state, now)

    sent = 0
    try:
        r = scan(now, state)
        failure = state.get("failure") or {}
        if failure.get("alerted"):
            tg_send("Copper scanner is working again.")
        state["failure"] = {"since": None, "alerted": False}
        state["current_week"] = r["week"]
        state["window"] = {"week": r["week"], "in_window": r["in_window"], "reopen_utc": r["reopen_utc"]}
        if (state.get("price_override") or {}).get("until") and now >= ts(state["price_override"]["until"]):
            state["price_override"] = None
        sent = alerts(r, state, now)
        wk = state.get("weeks", {}).get(r["week"], {})
        r["acked"] = wk.get("acked", {})
        r["weekday_alerts"] = weekday_alerts_on(state)
        r["price_override"] = state.get("price_override")
        for _ in status_requests:
            tg_send(status_text(r))
        save_json(LATEST, r)
        log_run(r, now, sent, "ok")
    except StopScan as e:
        out = {**previous, "status": "waiting", "error": str(e), "run_utc": iso(now)}
        save_json(LATEST, out)
        log_run(out, now, 0, "waiting", str(e))
        print(e)
    except Exception as e:
        traceback.print_exc()
        short = str(e).split("\n")[0][:160]
        out = {**previous, "status": "failed", "error": short, "run_utc": iso(now)}
        save_json(LATEST, out)
        log_run(out, now, 0, "failed", short)
        failure = state.setdefault("failure", {"since": None, "alerted": False})
        failure["since"] = failure.get("since") or iso(now)
        if (in_weekend_window_approx(now) or weekday_alerts_on(state)) and not failure.get("alerted"):
            if tg_send(f"Copper scanner: the scan failed at {fmt(now)} ({short}). The page still shows the "
                       f"last good scan. I'll message you when it's working again.\n{PAGE_URL}"):
                failure["alerted"] = True
        for _ in status_requests:
            tg_send(status_text(out))

    keep = sorted(state.get("weeks", {}), key=lambda w: week_date(w) or dt.date.min)[-6:]  # recent weeks only
    state["weeks"] = {k: state["weeks"][k] for k in keep}
    save_json(STATE, state)


if __name__ == "__main__":
    main()
