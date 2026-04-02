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
    "NIFTY":     "26000",
    "BANKNIFTY": "26009"
}

# NSE weekly option strike step per index
STRIKE_STEP = {
    "NIFTY":     50,
    "BANKNIFTY": 100
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

        fromdate = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        todate   = datetime.now().strftime("%Y-%m-%d %H:%M")
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
# ===== OI via Angel One getOptionGreeks (no NSE) ======
# ======================================================
# NSE direct API is blocked on server IPs (Render/AWS etc.)
# Angel One's getOptionGreeks API uses your authenticated
# session — no IP restriction, reliable live data.

from collections import Counter
import math


def nearest_expiry_str(index_name):
    """
    Calculate nearest weekly expiry in Angel One format: e.g. '03Apr2026'
    NIFTY     -> Thursday (weekday 3)
    BANKNIFTY -> Wednesday (weekday 2)
    If today IS the expiry day AND it is after 15:00 IST, skip to next week.
    """
    from datetime import date, timedelta
    target     = {"NIFTY": 3, "BANKNIFTY": 2}.get(index_name, 3)
    today      = date.today()
    days_ahead = (target - today.weekday()) % 7

    # If today is expiry day, check whether market has already closed
    if days_ahead == 0:
        now_ist = ist_now()
        # After 15:00 IST the expiry is essentially done — use next week
        if now_ist.hour >= 15:
            days_ahead = 7

    expiry = today + timedelta(days=days_ahead)
    # strftime("%b") gives "Apr" on all platforms — do NOT call .capitalize()
    # which would turn "02Apr2026" into "02apr2026" (digit as first char)
    return expiry.strftime("%d%b%Y")   # e.g. "03Apr2026"


def fetch_option_greeks(obj, index_name, expiry_str):
    """
    Call Angel One getOptionGreeks for a given index + expiry.
    Returns list of dicts (one per strike/type) or None on failure.
    """
    try:
        params = {"name": index_name, "expirydate": expiry_str}
        print(f"[OI] getOptionGreeks {index_name} expiry={expiry_str}")
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(obj.getOptionGreeks, params)
            resp   = future.result(timeout=25)
        if resp and resp.get("status") and resp.get("data"):
            print(f"[OI] Got {len(resp['data'])} rows for {index_name}")
            return resp["data"]
        print(f"[OI] Empty response: {resp.get('message','?') if resp else 'None'}")
        return None
    except FuturesTimeout:
        print(f"[OI] getOptionGreeks timeout for {index_name}")
        return None
    except Exception as e:
        print(f"[OI] getOptionGreeks error {index_name}: {e}")
        return None


def fetch_india_vix_angel(obj):
    """Fetch India VIX using Angel One LTP endpoint (token 26017 = INDIA VIX)."""
    try:
        params = {"mode": "LTP", "exchangeTokens": {"NSE": ["26017"]}}
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(obj.getMarketData, "LTP", "NSE", "26017")
            resp   = future.result(timeout=10)
        if resp and resp.get("status"):
            fetched = resp.get("data", {}).get("fetched", [])
            if fetched:
                return round(float(fetched[0].get("ltp", 0)), 2)
    except Exception as e:
        print(f"[VIX] {e}")
    return None


def calc_max_pain(chain_map):
    """Max Pain = strike where total OI writer loss is minimum."""
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
        print(f"[MAXPAIN] {e}")
        return None


def oi_signal(pchg, side):
    """Classify OI activity from % change."""
    if side == "CE":
        if   pchg >  40: return "🔴🔴", "HEAVY SHORT ⚠"
        elif pchg >  15: return "🔴",   "Shorts Adding"
        elif pchg >   3: return "🔺",   "Adding"
        elif pchg < -15: return "🟢",   "Short Covering"
        elif pchg <  -3: return "🟡",   "Exiting"
        else:            return "⚪",   "Stable"
    else:
        if   pchg >  40: return "🟢🟢", "STRONG SUPP ⚠"
        elif pchg >  15: return "🟢",   "Support Adding"
        elif pchg >   3: return "🔺",   "Adding"
        elif pchg < -15: return "🔴",   "Support Falling"
        elif pchg <  -3: return "🟡",   "Unwinding"
        else:            return "⚪",   "Stable"


def pcr_verdict(pcr):
    if pcr >= 1.5:  return "🟢🟢 Heavily Oversold"
    if pcr >= 1.2:  return "🟢 Bullish tilt"
    if pcr >= 0.95: return "⚪ Neutral"
    if pcr >= 0.75: return "🔴 Bearish tilt"
    return "🔴🔴 Heavily Overbought"


def analyze_oi(index_name):
    """
    Full OI analysis using Angel One getOptionGreeks.
    ATM±5 strikes, CE/PE walls, PCR, MaxPain.
    """
    obj = smart_login()
    if obj is None:
        return f"⚠️ {index_name} OI: login failed"

    step       = STRIKE_STEP[index_name]
    expiry_str = nearest_expiry_str(index_name)
    rows       = fetch_option_greeks(obj, index_name, expiry_str)

    # If nearest expiry fails, try next week's expiry (no .capitalize()!)
    if not rows:
        from datetime import date, timedelta
        target  = {"NIFTY": 3, "BANKNIFTY": 2}.get(index_name, 3)
        today   = date.today()
        days    = (target - today.weekday()) % 7 + 7
        expiry2 = (today + timedelta(days=days)).strftime("%d%b%Y")
        print(f"[OI] Retrying with next expiry {expiry2}")
        rows    = fetch_option_greeks(obj, index_name, expiry2)
        if rows:
            expiry_str = expiry2

    if not rows:
        return f"⚠️ {index_name} OI: no data from Angel One (expiry={expiry_str})"

    try:
        # Build chain_map: strike -> {ce_oi, ce_chg, ce_pchg, ce_iv, pe_oi, ...}
        chain_map = {}
        spot      = 0.0

        for r in rows:
            s     = int(float(r.get("strikeprice", 0)))
            otype = str(r.get("optiontype", "")).upper()
            uv    = float(r.get("underlyingValue", 0) or 0)
            if uv > 0:
                spot = uv

            if s not in chain_map:
                chain_map[s] = {
                    "ce_oi": 0, "ce_chg": 0, "ce_pchg": 0.0,
                    "ce_iv": 0.0, "ce_ltp": 0.0,
                    "pe_oi": 0, "pe_chg": 0, "pe_pchg": 0.0,
                    "pe_iv": 0.0, "pe_ltp": 0.0,
                }

            oi    = int(float(r.get("openInterest",         0) or 0))
            chg   = int(float(r.get("changeinOpenInterest", 0) or 0))
            pchg  = float(r.get("percentChangeinOpenInterest", 0) or 0)
            iv    = float(r.get("impliedVolatility",        0) or 0)
            ltp   = float(r.get("lastPrice",                0) or 0)

            if otype == "CE":
                chain_map[s].update(ce_oi=oi, ce_chg=chg,
                                    ce_pchg=pchg, ce_iv=iv, ce_ltp=ltp)
            elif otype == "PE":
                chain_map[s].update(pe_oi=oi, pe_chg=chg,
                                    pe_pchg=pchg, pe_iv=iv, pe_ltp=ltp)

        if spot == 0:
            # Fallback: estimate spot from ATM CE+PE midpoint
            if chain_map:
                mid = sorted(chain_map.keys())[len(chain_map)//2]
                spot = float(mid)

        atm = round(spot / step) * step

        # ATM ± 5 strikes
        ce_strikes = [atm + i * step for i in range(1, 6)]
        pe_strikes = [atm - i * step for i in range(0, 6)]

        # Totals for PCR
        tot_ce = sum(v["ce_oi"] for v in chain_map.values())
        tot_pe = sum(v["pe_oi"] for v in chain_map.values())
        pcr    = round(tot_pe / tot_ce, 2) if tot_ce > 0 else 0.0

        # MaxPain
        max_pain     = calc_max_pain(chain_map)
        pain_diff    = round(spot - max_pain) if max_pain else 0
        pain_str     = f"{max_pain:,}" if max_pain else "N/A"
        pain_dir     = (
            f"Spot {abs(pain_diff)}pts {'BELOW ↓' if pain_diff < 0 else 'ABOVE ↑'} MaxPain"
            if max_pain else ""
        )

        # VIX (best-effort)
        vix     = fetch_india_vix_angel(obj)
        vix_s   = str(vix) if vix else "N/A"
        vix_tag = " 🔥HIGH" if vix and vix > 20 else " ✅OK" if vix else ""

        # CE wall (highest OI among ATM+1..ATM+5)
        ce_oi_map = {s: chain_map.get(s, {}).get("ce_oi", 0) for s in ce_strikes}
        ce_wall   = max(ce_oi_map, key=ce_oi_map.get)

        pe_oi_map = {s: chain_map.get(s, {}).get("pe_oi", 0)
                     for s in pe_strikes if s != atm}
        pe_wall   = max(pe_oi_map, key=pe_oi_map.get) if pe_oi_map else atm

        # ── CE table ──
        sep     = "─" * 48
        ce_rows = []
        for s in sorted(ce_strikes, reverse=True):
            d     = chain_map.get(s, {})
            oi_l  = round(d.get("ce_oi",   0) / 100_000, 1)
            chg_l = round(d.get("ce_chg",  0) / 100_000, 1)
            pchg  = d.get("ce_pchg", 0.0)
            iv    = d.get("ce_iv",   0.0)
            em, lb = oi_signal(pchg, "CE")
            wall  = " 🧱" if s == ce_wall else ""
            ce_rows.append(
                f"{s:>7,}  {oi_l:>5.1f}L  "
                f"{('%+.1f' % chg_l)+'L':>7}  "
                f"IV{iv:>5.1f}  {em} {lb}{wall}"
            )

        # ATM boundary row (CE side)
        d0       = chain_map.get(atm, {})
        atm_oi   = round(d0.get("ce_oi",  0) / 100_000, 1)
        atm_chg  = round(d0.get("ce_chg", 0) / 100_000, 1)
        atm_iv   = d0.get("ce_iv", 0.0)
        atm_em, atm_lb = oi_signal(d0.get("ce_pchg", 0), "CE")
        atm_row  = (
            f"{'★'+str(atm):>7}  {atm_oi:>5.1f}L  "
            f"{('%+.1f' % atm_chg)+'L':>7}  "
            f"IV{atm_iv:>5.1f}  {atm_em} {atm_lb}  ←ATM"
        )

        # ── PE table ──
        pe_rows = []
        for s in sorted(pe_strikes, reverse=True):
            d     = chain_map.get(s, {})
            oi_l  = round(d.get("pe_oi",   0) / 100_000, 1)
            chg_l = round(d.get("pe_chg",  0) / 100_000, 1)
            pchg  = d.get("pe_pchg", 0.0)
            iv    = d.get("pe_iv",   0.0)
            em, lb = oi_signal(pchg, "PE")
            atm_t = "  ←ATM" if s == atm else ""
            wall  = " 🧱" if s == pe_wall else ""
            pe_rows.append(
                f"{s:>7,}  {oi_l:>5.1f}L  "
                f"{('%+.1f' % chg_l)+'L':>7}  "
                f"IV{iv:>5.1f}  {em} {lb}{atm_t}{wall}"
            )

        # Key callouts
        ce_adding  = max(ce_strikes, key=lambda x: chain_map.get(x,{}).get("ce_chg", 0))
        ce_exiting = min(ce_strikes, key=lambda x: chain_map.get(x,{}).get("ce_chg", 0))
        pe_adding  = max(pe_strikes, key=lambda x: chain_map.get(x,{}).get("pe_chg", 0))
        pe_exiting = min(pe_strikes, key=lambda x: chain_map.get(x,{}).get("pe_chg", 0))
        ce_exit_v  = chain_map.get(ce_exiting, {}).get("ce_chg", 0)
        pe_exit_v  = chain_map.get(pe_exiting, {}).get("pe_chg", 0)

        # ── Compose message ──
        header = (
            f"<b>\U0001f50d {index_name} OI  {ist_str()} IST</b>\n"
            f"Spot <b>\u20b9{spot:,.1f}</b>  ATM <b>{atm:,}</b>  VIX <b>{vix_s}{vix_tag}</b>\n"
            f"Expiry: {expiry_str}  PCR: <b>{pcr}</b>  MaxPain: <b>{pain_str}</b>\n"
            f"<i>{pain_dir}</i>\n{sep}"
        )

        ce_block = (
            "\n\U0001f534 <b>CALL SIDE \u2014 Resistance / Sellers</b>\n"
            f"<code> Strike    OI    \u0394OI     IV   Signal\n{sep}\n"
        )
        for row in ce_rows:
            ce_block += row + "\n"
        ce_block += f"{sep}\n{atm_row}\n</code>"
        ce_block += (
            f"Wall: <b>{ce_wall:,}</b>  TotalCE: <b>{round(tot_ce/100_000,1)}L</b>\n"
            f"\U0001f534 Sellers active at <b>{ce_adding:,}</b>"
        )
        if ce_exit_v < -30_000:
            ce_block += f"  \U0001f7e2 Covering at <b>{ce_exiting:,}</b>"

        pe_block = (
            "\n\n\U0001f7e2 <b>PUT SIDE \u2014 Support / Buyers</b>\n"
            f"<code> Strike    OI    \u0394OI     IV   Signal\n{sep}\n"
        )
        for row in pe_rows:
            pe_block += row + "\n"
        pe_block += "</code>"
        pe_block += (
            f"Wall: <b>{pe_wall:,}</b>  TotalPE: <b>{round(tot_pe/100_000,1)}L</b>\n"
            f"\U0001f7e2 Support at <b>{pe_adding:,}</b>"
        )
        if pe_exit_v < -30_000:
            pe_block += f"  \U0001f534 Unwinding at <b>{pe_exiting:,}</b>"

        footer = (
            "\n\n<b>\U0001f4cc Bias</b>\n"
            f"PCR {pcr} \u2192 {pcr_verdict(pcr)}\n"
            f"Range: <b>{pe_wall:,}</b> (support) \u2192 <b>{ce_wall:,}</b> (resistance)"
        )

        return header + ce_block + pe_block + footer

    except Exception as e:
        import traceback; traceback.print_exc()
        return f"⚠️ {index_name} OI parse error: {e}"


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
