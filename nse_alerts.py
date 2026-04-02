# ===== FLASK (KEEP ALIVE) =====
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running"

def run_web():
    app.run(host='0.0.0.0', port=10000)

Thread(target=run_web, daemon=True).start()


# ===== UNBUFFERED OUTPUT (so Render logs appear immediately) =====
import sys
sys.stdout.reconfigure(line_buffering=True)

# ===== IMPORTS =====
import requests
import pandas as pd
import schedule
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from SmartApi import SmartConnect
import pyotp
import os


# ===== CONFIG =====
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
API_KEY     = os.getenv("API_KEY")
CLIENT_ID   = os.getenv("CLIENT_ID")
PASSWORD    = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")

SYMBOLS = {
    "NIFTY": "26000"
}

# NSE weekly option strike step per index
STRIKE_STEP = {
    "NIFTY": 50
}

smart = None

# ===== IST TIMEZONE =====
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(IST)

def ist_str():
    return ist_now().strftime("%H:%M")


# ===== TELEGRAM =====
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, data={
            "chat_id":    CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=15)
        print(f"[TG] {r.status_code} | {msg[:60]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")


# ===== SMART LOGIN =====
def smart_login():
    global smart
    try:
        obj    = SmartConnect(api_key=API_KEY)
        totp   = pyotp.TOTP(TOTP_SECRET).now()
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(obj.generateSession, CLIENT_ID, PASSWORD, totp)
            result = future.result(timeout=20)
        print(f"[LOGIN] {result.get('message', 'ok')}")
        smart = obj
        return obj
    except FuturesTimeout:
        print("[LOGIN ERROR] Timed out after 20s")
        smart = None
        return None
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        smart = None
        return None


# ===== FETCH CANDLE DATA =====
def _do_fetch(obj, params):
    return obj.getCandleData(params)

def fetch_data(token, interval):
    try:
        obj = smart_login()
        if obj is None:
            return None

        # Use IST time — Render server runs UTC, Angel One expects IST
        fromdate = (ist_now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        todate   = ist_now().strftime("%Y-%m-%d %H:%M")
        params   = {
            "exchange":    "NSE",
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    fromdate,
            "todate":      todate
        }
        print(f"[FETCH] token={token} interval={interval}")

        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_fetch, obj, params)
            data   = future.result(timeout=25)

        status  = data.get("status") if data else False
        message = data.get("message", "?") if data else "None"
        print(f"[FETCH] status={status} msg={message} rows={len(data.get('data') or []) if data else 0}")
        if not data or not data.get("status") or not data.get("data"):
            return None

        rows        = data["data"]
        df          = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"])
        df['Close'] = pd.to_numeric(df['close'])
        df          = df.dropna()
        print(f"[FETCH] {len(df)} rows, last={df['Close'].iloc[-1]:.1f}")
        return df

    except FuturesTimeout:
        print(f"[FETCH TIMEOUT] token={token}")
        return None
    except Exception as e:
        print(f"[FETCH EXCEPTION] {e}")
        return None


# ======================================================
# ===== OI via Symbol Master + getMarketData ===========
# ======================================================
# getOptionGreeks is unreliable across Angel One accounts.
# This approach:
#   1. Downloads Angel One's public symbol master (no auth)
#   2. Gets NIFTY spot via ltpData (token 26000)
#   3. Finds ATM±5 strike tokens from master
#   4. Fetches live OI via getMarketData FULL (bulk, authenticated)
# Works 100% on server IPs — no NSE scraping.

from collections import Counter
from datetime import date

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_scrip_cache = {"date": None, "data": None}


def get_scrip_master():
    """Download & cache Angel One symbol master once per day."""
    today = date.today().isoformat()
    if _scrip_cache["date"] == today and _scrip_cache["data"]:
        return _scrip_cache["data"]
    print("[SCRIP] Downloading symbol master...", flush=True)
    try:
        r = requests.get(SCRIP_MASTER_URL, timeout=30)
        data = r.json()
        _scrip_cache["date"]  = today
        _scrip_cache["data"]  = data
        print(f"[SCRIP] Downloaded {len(data)} symbols", flush=True)
        return data
    except Exception as e:
        print(f"[SCRIP ERROR] {e}", flush=True)
        return []


def nearest_expiry_str(index_name):
    """
    NIFTY weekly expiry = Tuesday (weekday 1) since Sept 2024.
    Returns format '07Apr2026'.
    If today IS expiry day and after 15:00 IST, use next week.
    """
    from datetime import timedelta
    target     = {"NIFTY": 1}.get(index_name, 1)
    today      = date.today()
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0 and ist_now().hour >= 15:
        days_ahead = 7
    expiry = today + timedelta(days=days_ahead)
    return expiry.strftime("%d%b%Y")   # e.g. "07Apr2026"


def get_nifty_spot(obj):
    """Get live NIFTY spot price from Angel One."""
    try:
        resp = obj.ltpData("NSE", "NIFTY", "26000")
        if resp and resp.get("status"):
            ltp = float(resp["data"].get("ltp", 0))
            print(f"[SPOT] NIFTY spot = {ltp}", flush=True)
            return ltp
    except Exception as e:
        print(f"[SPOT ERROR] {e}", flush=True)
    return 0.0


def find_option_tokens(expiry_str, strikes):
    """
    Find NFO tokens for NIFTY CE/PE at given strikes & expiry.
    Symbol master stores expiry as '07APR2026' (all caps).
    Strike is stored as actual price * 100 in the master.
    Returns dict: (strike_int, 'CE'/'PE') -> token_str
    """
    master       = get_scrip_master()
    expiry_upper = expiry_str.upper()        # "07APR2026"
    strike_set   = set(strikes)
    token_map    = {}

    for row in master:
        if row.get("exch_seg")      != "NFO":      continue
        if row.get("instrumenttype")!= "OPTIDX":   continue
        if row.get("name",  "")     != "NIFTY":    continue
        if row.get("expiry","")     != expiry_upper: continue

        symbol = row.get("symbol", "")
        otype  = "CE" if symbol.endswith("CE") else \
                 "PE" if symbol.endswith("PE") else None
        if not otype:
            continue

        # Angel One stores strike * 100 as an integer string
        try:
            raw_strike = float(row.get("strike", 0))
            # If > 1_000_000 it's stored *100, otherwise direct
            strike = int(raw_strike / 100) if raw_strike > 100_000 else int(raw_strike)
        except Exception:
            continue

        if strike in strike_set:
            token_map[(strike, otype)] = str(row["token"])

    print(f"[TOKENS] Found {len(token_map)} tokens for {expiry_upper} "
          f"strikes={sorted(strike_set)}", flush=True)
    return token_map


def fetch_market_data_bulk(obj, nfo_tokens):
    """
    Bulk-fetch FULL market data for up to 50 NFO tokens.
    Returns dict: token_str -> data_dict
    """
    if not nfo_tokens:
        return {}
    try:
        params = {
            "mode":           "FULL",
            "exchangeTokens": {"NFO": list(nfo_tokens)}
        }
        # getMarketData(mode, exchangeTokens) — two positional args
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(obj.getMarketData, "FULL", {"NFO": list(nfo_tokens)})
            resp   = future.result(timeout=25)

        if resp and resp.get("status"):
            result = {}
            for item in resp.get("data", {}).get("fetched", []):
                result[str(item.get("symbolToken", ""))] = item
            print(f"[MKTDATA] Fetched {len(result)} tokens", flush=True)
            return result
        print(f"[MKTDATA] Failed: {resp.get('message') if resp else 'None'}", flush=True)
    except FuturesTimeout:
        print("[MKTDATA] Timeout", flush=True)
    except Exception as e:
        print(f"[MKTDATA ERROR] {e}", flush=True)
    return {}


def calc_max_pain(chain_map):
    try:
        strikes = sorted(chain_map.keys())
        if not strikes:
            return None
        pain = {}
        for K in strikes:
            loss = 0
            for S in strikes:
                loss += max(0, S - K) * chain_map[S]["ce_oi"]
                loss += max(0, K - S) * chain_map[S]["pe_oi"]
            pain[K] = loss
        return min(pain, key=pain.get)
    except Exception as e:
        print(f"[MAXPAIN] {e}", flush=True)
        return None


def oi_signal(pchg, side):
    if side == "CE":
        if   pchg >  40: return "🔴🔴", "HEAVY SHORT"
        elif pchg >  15: return "🔴",   "Shorts Adding"
        elif pchg >   3: return "🔺",   "Adding"
        elif pchg < -15: return "🟢",   "Covering"
        elif pchg <  -3: return "🟡",   "Exiting"
        else:            return "⚪",   "Stable"
    else:
        if   pchg >  40: return "🟢🟢", "STRONG SUPP"
        elif pchg >  15: return "🟢",   "Supp Adding"
        elif pchg >   3: return "🔺",   "Adding"
        elif pchg < -15: return "🔴",   "Supp Falling"
        elif pchg <  -3: return "🟡",   "Unwinding"
        else:            return "⚪",   "Stable"


def pcr_verdict(pcr):
    if pcr >= 1.5:  return "🟢🟢 Heavily Oversold"
    if pcr >= 1.2:  return "🟢 Bullish tilt"
    if pcr >= 0.95: return "⚪ Neutral"
    if pcr >= 0.75: return "🔴 Bearish tilt"
    return "🔴🔴 Heavily Overbought"


def analyze_oi(index_name):
    """Full OI analysis via symbol master + getMarketData."""
    obj = smart_login()
    if obj is None:
        return f"⚠️ {index_name} OI: login failed"

    step       = STRIKE_STEP[index_name]
    expiry_str = nearest_expiry_str(index_name)
    print(f"[OI] {index_name} expiry={expiry_str}", flush=True)

    # Step 1: Get spot price
    spot = get_nifty_spot(obj)
    if spot == 0:
        return f"⚠️ {index_name} OI: spot price unavailable"
    atm = round(spot / step) * step

    # Step 2: Define ATM±5 strikes
    ce_strikes = [atm + i * step for i in range(1, 6)]
    pe_strikes = [atm - i * step for i in range(0, 6)]
    all_strikes = list(set(ce_strikes + pe_strikes))

    # Step 3: Find tokens from symbol master
    token_map = find_option_tokens(expiry_str, all_strikes)

    # Retry next week's expiry if no tokens found
    if len(token_map) < 4:
        from datetime import timedelta
        target     = {"NIFTY": 1}.get(index_name, 1)
        today      = date.today()
        days       = (target - today.weekday()) % 7 + 7
        expiry_str = (today + timedelta(days=days)).strftime("%d%b%Y")
        print(f"[OI] Retrying with {expiry_str}", flush=True)
        token_map  = find_option_tokens(expiry_str, all_strikes)

    if not token_map:
        return f"⚠️ {index_name} OI: no tokens found for {expiry_str} in symbol master"

    # Step 4: Fetch live market data for all tokens
    all_tokens   = list(token_map.values())
    mkt_data     = fetch_market_data_bulk(obj, all_tokens)

    # Step 5: Build chain_map from market data
    chain_map = {}
    for (strike, otype), token in token_map.items():
        d = mkt_data.get(token, {})
        if strike not in chain_map:
            chain_map[strike] = {
                "ce_oi": 0, "ce_chg": 0, "ce_pchg": 0.0, "ce_iv": 0.0,
                "pe_oi": 0, "pe_chg": 0, "pe_pchg": 0.0, "pe_iv": 0.0,
            }
        oi   = int(float(d.get("opnInterest",       0) or 0))
        chg  = int(float(d.get("netChange",         0) or 0))
        iv   = float(d.get("impliedVolatility",     0) or 0)
        ltp  = float(d.get("ltp",                   0) or 0)
        pchg = round(chg / oi * 100, 2) if oi > 0 else 0.0

        if otype == "CE":
            chain_map[strike].update(ce_oi=oi, ce_chg=chg, ce_pchg=pchg, ce_iv=iv)
        else:
            chain_map[strike].update(pe_oi=oi, pe_chg=chg, pe_pchg=pchg, pe_iv=iv)

    # Step 6: Metrics
    tot_ce   = sum(v["ce_oi"] for v in chain_map.values())
    tot_pe   = sum(v["pe_oi"] for v in chain_map.values())
    pcr      = round(tot_pe / tot_ce, 2) if tot_ce > 0 else 0.0
    max_pain = calc_max_pain(chain_map)
    pain_str = f"{max_pain:,}" if max_pain else "N/A"
    pain_dir = ""
    if max_pain:
        diff     = round(spot - max_pain)
        pain_dir = f"Spot {abs(diff)}pts {'BELOW' if diff < 0 else 'ABOVE'} MaxPain"

    # Step 7: CE wall & PE wall
    ce_oi_map = {s: chain_map.get(s, {}).get("ce_oi", 0) for s in ce_strikes}
    pe_oi_map = {s: chain_map.get(s, {}).get("pe_oi", 0) for s in pe_strikes if s != atm}
    ce_wall   = max(ce_oi_map, key=ce_oi_map.get)
    pe_wall   = max(pe_oi_map, key=pe_oi_map.get) if pe_oi_map else atm

    sep = "\u2500" * 46

    # Step 8: Build CE rows (resistance, ATM+1 → ATM+5)
    ce_rows = []
    for s in sorted(ce_strikes, reverse=True):
        d     = chain_map.get(s, {})
        oi_l  = round(d.get("ce_oi",   0) / 100_000, 1)
        chg_l = round(d.get("ce_chg",  0) / 100_000, 1)
        pchg  = d.get("ce_pchg", 0.0)
        iv    = d.get("ce_iv",   0.0)
        em, lb = oi_signal(pchg, "CE")
        wall  = " \U0001f9f1" if s == ce_wall else ""
        ce_rows.append(
            f"{s:>7,}  {oi_l:>5.1f}L  "
            f"{('%+.1f' % chg_l)+'L':>7}  "
            f"IV{iv:>5.1f}  {em} {lb}{wall}"
        )

    # ATM row (CE side)
    d0       = chain_map.get(atm, {})
    atm_oi   = round(d0.get("ce_oi",  0) / 100_000, 1)
    atm_chg  = round(d0.get("ce_chg", 0) / 100_000, 1)
    atm_em, atm_lb = oi_signal(d0.get("ce_pchg", 0), "CE")
    atm_row  = (f"{'*'+str(atm):>7}  {atm_oi:>5.1f}L  "
                f"{('%+.1f' % atm_chg)+'L':>7}  "
                f"IV{d0.get('ce_iv',0):>5.1f}  {atm_em} {atm_lb}  <-ATM")

    # PE rows (support, ATM → ATM-5)
    pe_rows = []
    for s in sorted(pe_strikes, reverse=True):
        d     = chain_map.get(s, {})
        oi_l  = round(d.get("pe_oi",   0) / 100_000, 1)
        chg_l = round(d.get("pe_chg",  0) / 100_000, 1)
        pchg  = d.get("pe_pchg", 0.0)
        iv    = d.get("pe_iv",   0.0)
        em, lb = oi_signal(pchg, "PE")
        atm_t = "  <-ATM" if s == atm else ""
        wall  = " \U0001f9f1" if s == pe_wall else ""
        pe_rows.append(
            f"{s:>7,}  {oi_l:>5.1f}L  "
            f"{('%+.1f' % chg_l)+'L':>7}  "
            f"IV{iv:>5.1f}  {em} {lb}{atm_t}{wall}"
        )

    # Key callouts
    ce_adding  = max(ce_strikes, key=lambda x: chain_map.get(x,{}).get("ce_chg", 0))
    pe_adding  = max(pe_strikes, key=lambda x: chain_map.get(x,{}).get("pe_chg", 0))
    ce_exit_v  = min(chain_map.get(s,{}).get("ce_chg",0) for s in ce_strikes)
    pe_exit_v  = min(chain_map.get(s,{}).get("pe_chg",0) for s in pe_strikes)
    ce_exiting = min(ce_strikes, key=lambda x: chain_map.get(x,{}).get("ce_chg",0))
    pe_exiting = min(pe_strikes, key=lambda x: chain_map.get(x,{}).get("pe_chg",0))

    header = (
        f"<b>\U0001f50d NIFTY OI  {ist_str()} IST</b>\n"
        f"Spot <b>\u20b9{spot:,.1f}</b>  ATM <b>{atm:,}</b>\n"
        f"Expiry: {expiry_str}  PCR: <b>{pcr}</b>  MaxPain: <b>{pain_str}</b>\n"
        f"<i>{pain_dir}</i>\n{sep}"
    )
    ce_block = (
        "\n\U0001f534 <b>CALL SIDE \u2014 Resistance</b>\n"
        f"<code> Strike    OI    \u0394OI     IV   Signal\n{sep}\n"
    )
    for row in ce_rows:
        ce_block += row + "\n"
    ce_block += f"{sep}\n{atm_row}\n</code>"
    ce_block += (
        f"Wall: <b>{ce_wall:,}</b>  TotalCE: <b>{round(tot_ce/100_000,1)}L</b>\n"
        f"\U0001f534 Shorts adding at <b>{ce_adding:,}</b>"
    )
    if ce_exit_v < -5_000:
        ce_block += f"  \U0001f7e2 Covering at <b>{ce_exiting:,}</b>"

    pe_block = (
        "\n\n\U0001f7e2 <b>PUT SIDE \u2014 Support</b>\n"
        f"<code> Strike    OI    \u0394OI     IV   Signal\n{sep}\n"
    )
    for row in pe_rows:
        pe_block += row + "\n"
    pe_block += "</code>"
    pe_block += (
        f"Wall: <b>{pe_wall:,}</b>  TotalPE: <b>{round(tot_pe/100_000,1)}L</b>\n"
        f"\U0001f7e2 Support adding at <b>{pe_adding:,}</b>"
    )
    if pe_exit_v < -5_000:
        pe_block += f"  \U0001f534 Unwinding at <b>{pe_exiting:,}</b>"

    footer = (
        "\n\n<b>\U0001f4cc Bias</b>\n"
        f"PCR {pcr} \u2192 {pcr_verdict(pcr)}\n"
        f"Range: <b>{pe_wall:,}</b> (supp) \u2192 <b>{ce_wall:,}</b> (res)"
    )
    return header + ce_block + pe_block + footer


def check_oi_alert(name):
    try:
        print(f"[OI CHECK] {name} at {ist_str()}")
        msg = analyze_oi(name)
        if msg:
            send_telegram(msg)
    except Exception as e:
        print(f"[OI ERROR] {name}: {e}")
        send_telegram(f"❌ {name} OI Error: {e}")


# ===== SUPERTREND =====
def supertrend(df, period=2, multiplier=3):
    atr   = df['Close'].rolling(period).std()
    upper = df['Close'] + multiplier * atr
    lower = df['Close'] - multiplier * atr
    trend = [True]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > upper.iloc[i-1]:
            trend.append(True)
        elif df['Close'].iloc[i] < lower.iloc[i-1]:
            trend.append(False)
        else:
            trend.append(trend[i-1])
    return trend


# ===== ANALYZE CANDLES =====
def analyze(df):
    if df is None or len(df) < 3:
        return None
    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    e7    = round(float(last['ema7']),  1)
    e15   = round(float(last['ema15']), 1)
    e7s   = "↑" if last['ema7']  > prev['ema7']  else "↓" if last['ema7']  < prev['ema7']  else "→"
    e15s  = "↑" if last['ema15'] > prev['ema15'] else "↓" if last['ema15'] < prev['ema15'] else "→"
    cross = "EMA7 &gt; EMA15" if last['ema7'] > last['ema15'] else "EMA7 &lt; EMA15"
    ema   = f"{cross}  EMA7:{e7}{e7s}  EMA15:{e15}{e15s}"

    price = round(float(last['Close']))
    dist  = round(float(last['Close']) - float(last['ema7']), 2)
    ptxt  = f"{price} (+{dist}) ↑ Above EMA7" if dist > 0 else f"{price} ({dist}) ↓ Below EMA7"

    s1b  = bool(last['st1']); s2b = bool(last['st2'])
    s1p  = bool(prev['st1']); s2p = bool(prev['st2'])
    f1   = " [BULL FLIP ↑]" if s1b and not s1p else " [BEAR FLIP ↓]" if not s1b and s1p else ""
    f2   = " [BULL FLIP ↑]" if s2b and not s2p else " [BEAR FLIP ↓]" if not s2b and s2p else ""
    st   = f"ST(2,3):{'↑' if s1b else '↓'}{f1}  ST(2,2.5):{'↑' if s2b else '↓'}{f2}"

    rp   = round(float(prev['rsi']), 1); rn = round(float(last['rsi']), 1)
    rd   = round(rn - rp, 1)
    zone = "OB" if rn >= 70 else "OS" if rn <= 30 else "OK"
    ra   = "↑" if rd > 0 else "↓" if rd < 0 else "→"
    rsi  = f"{rp}→{rn} {ra} ({'+' if rd>=0 else ''}{rd}) [{zone}]"

    return {"ema": ema, "price": ptxt, "st": st, "rsi": rsi}


# ===== CHECK SYMBOL =====
def check_symbol(name, token, tf):
    try:
        interval = "FIVE_MINUTE" if tf == "5M" else "FIFTEEN_MINUTE"
        print(f"[CHECK] {name} {tf} interval={interval} at {ist_str()}")
        df       = fetch_data(token, interval)

        if df is None or len(df) < 20:
            cnt = len(df) if df is not None else 0
            print(f"[CHECK] {name} {tf} — insufficient data ({cnt} rows) — skipping candle alert")
            return

        df['ema7']  = EMAIndicator(df['Close'], 10).ema_indicator()
        df['ema15'] = EMAIndicator(df['Close'], 30).ema_indicator()
        df['rsi']   = RSIIndicator(df['Close'], 15).rsi()
        df['st1']   = supertrend(df, 10, 3)
        df['st2']   = supertrend(df, 3, 1.5)

        a = analyze(df)
        if a is None:
            return

        msg = (
            f"<b>📊 {name} ({tf})  {ist_str()} IST</b>\n"
            f"{'─'*28}\n"
            f"<b>EMA  :</b> {a['ema']}\n"
            f"<b>Price:</b> {a['price']}\n"
            f"<b>ST   :</b> {a['st']}\n"
            f"<b>RSI  :</b> {a['rsi']}"
        )
        send_telegram(msg)

    except Exception as e:
        print(f"[ERROR] {name} {tf}: {e}")
        send_telegram(f"❌ {name} {tf} Error: {e}")


# ===== MARKET HOURS =====
def in_market_hours():
    now  = ist_now()
    mins = now.hour * 60 + now.minute
    return now.weekday() < 5 and (9*60+15 <= mins <= 15*60+31)


# ===== RUN =====
last_run_5m  = -1
last_run_15m = -1

def run():
    global last_run_5m, last_run_15m

    now    = ist_now()
    minute = now.minute
    print(f"[RUN] {now.strftime('%H:%M:%S')} IST")

    if not in_market_hours():
        print("[RUN] Outside market hours — skipping")
        return

    bucket_15 = minute // 15
    bucket_5  = minute // 5

    if bucket_15 != last_run_15m:
        last_run_15m = bucket_15
        print("[RUN] → 15M check + OI snapshot")

        # Candle/indicator alerts
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "15M")
            time.sleep(4)

        # OI snapshots — spaced to avoid NSE rate-limiting
        for name in SYMBOLS.keys():
            check_oi_alert(name)
            time.sleep(6)

        return   # skip 5M on 15M boundary

    if bucket_5 != last_run_5m:
        last_run_5m = bucket_5
        print("[RUN] → 5M check")
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "5M")
            time.sleep(4)


# ===== MAIN =====
if __name__ == "__main__":
    print(f"BOT STARTED — IST: {ist_str()}")

    send_telegram(
        f"✅ <b>NSE Bot Started</b>  [{ist_str()} IST]\n"
        f"Watching: {', '.join(SYMBOLS.keys())}\n"
        "📊 Candle alerts (EMA/ST/RSI): every 5 min\n"
        "🔍 OI snapshot (CE/PE/PCR/MaxPain): every 15 min\n"
        "Market hours 9:15–15:30 IST"
    )

    print("[STARTUP] Testing login...")
    test = smart_login()
    if test is None:
        send_telegram("⚠️ <b>Warning:</b> Login failed — check API credentials/TOTP")
    else:
        send_telegram("🔐 Login OK — schedule starting")

    run()

    schedule.every(5).minutes.do(run)

    while True:
        try:
            schedule.run_pending()
            time.sleep(10)
        except Exception as e:
            print(f"[MAIN ERROR] {e}")
            send_telegram(f"❌ Main Loop Error: {e}")
            time.sleep(10)
