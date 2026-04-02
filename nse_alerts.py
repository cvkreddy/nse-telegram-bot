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

        if not data or not data.get("status") or not data.get("data"):
            print(f"[FETCH] Bad response")
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


# ======================================
# ===== NSE OPTIONS CHAIN / OI =========
# ======================================

NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


def fetch_nse_options(symbol):
    """Fetch NSE options chain. Retries once on failure."""
    for attempt in range(2):
        try:
            s = requests.Session()
            # Must hit homepage first to get valid session cookies
            s.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=15)
            time.sleep(1)
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            r   = s.get(url, headers=NSE_HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"[NSE] HTTP {r.status_code} for {symbol} attempt {attempt+1}")
            time.sleep(3)
        except Exception as e:
            print(f"[NSE ERROR] {symbol} attempt {attempt+1}: {e}")
            time.sleep(3)
    return None


def fetch_india_vix():
    """Fetch India VIX value from NSE allIndices."""
    try:
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=12)
        time.sleep(0.5)
        r = s.get("https://www.nseindia.com/api/allIndices",
                  headers=NSE_HEADERS, timeout=12)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                if item.get("index") == "INDIA VIX":
                    return round(float(item["last"]), 2)
    except Exception as e:
        print(f"[VIX ERROR] {e}")
    return None


def calc_max_pain(chain_map):
    """
    Max Pain = strike where total OI writer loss is minimum.
    chain_map: dict of strike -> {ce_oi, pe_oi}
    """
    try:
        strikes = sorted(chain_map.keys())
        pain    = {}
        for K in strikes:
            loss = 0
            for S in strikes:
                loss += max(0, S - K) * chain_map[S]["ce_oi"]
                loss += max(0, K - S) * chain_map[S]["pe_oi"]
            pain[K] = loss
        return min(pain, key=pain.get)
    except Exception as e:
        print(f"[MAXPAIN ERROR] {e}")
        return None


def oi_signal(oi_chg, pchg, side):
    """
    Classify participant activity from OI change.
    CE above ATM: OI adding = sellers (shorts) building resistance
    PE below ATM: OI adding = buyers/writers building support
    Returns (emoji, label)
    """
    if side == "CE":
        if   pchg >  40:  return "🔴🔴", "HEAVY SHORT ⚠"
        elif pchg >  15:  return "🔴",   "Shorts Adding"
        elif pchg >   3:  return "🔺",   "Adding"
        elif pchg < -15:  return "🟢",   "Short Covering"
        elif pchg <  -3:  return "🟡",   "Exiting"
        else:             return "⚪",   "Stable"
    else:  # PE
        if   pchg >  40:  return "🟢🟢", "STRONG SUPPORT ⚠"
        elif pchg >  15:  return "🟢",   "Support Adding"
        elif pchg >   3:  return "🔺",   "Adding"
        elif pchg < -15:  return "🔴",   "Support Falling"
        elif pchg <  -3:  return "🟡",   "Unwinding"
        else:             return "⚪",   "Stable"


def pcr_verdict(pcr):
    if pcr >= 1.5:  return "🟢🟢 Heavily Oversold — reversal likely"
    if pcr >= 1.2:  return "🟢 Bullish tilt"
    if pcr >= 0.95: return "⚪ Neutral"
    if pcr >= 0.75: return "🔴 Bearish tilt"
    return "🔴🔴 Heavily Overbought — fall risk"


def analyze_oi(index_name):
    """
    Full OI analysis: ATM±5 strikes, CE/PE walls, PCR, MaxPain, VIX.
    Returns formatted Telegram HTML string.
    """
    print(f"[OI] Analyzing {index_name}")
    raw = fetch_nse_options(index_name)
    if raw is None:
        return f"⚠️ {index_name} OI: NSE fetch failed"

    try:
        records  = raw.get("records", {})
        filtered = raw.get("filtered", {})
        spot     = float(records.get("underlyingValue", 0))
        expiry   = records.get("expiryDates", ["?"])[0]   # nearest expiry
        step     = STRIKE_STEP[index_name]
        atm      = round(spot / step) * step

        # Strikes to show: CE = ATM+1 to ATM+5 (above), PE = ATM to ATM-5
        ce_strikes = [atm + i * step for i in range(1, 6)]
        pe_strikes = [atm - i * step for i in range(0, 6)]
        all_watch  = set(ce_strikes + pe_strikes)

        # Parse options chain into lookup dict
        chain_map = {}
        for row in records.get("data", []):
            if row.get("expiryDate") != expiry:
                continue
            s  = int(row.get("strikePrice", 0))
            ce = row.get("CE", {}) or {}
            pe = row.get("PE", {}) or {}
            chain_map[s] = {
                "ce_oi":   int(ce.get("openInterest",             0)),
                "ce_chg":  int(ce.get("changeinOpenInterest",     0)),
                "ce_pchg": float(ce.get("pchangeinOpenInterest",  0)),
                "ce_iv":   float(ce.get("impliedVolatility",      0)),
                "ce_ltp":  float(ce.get("lastPrice",              0)),
                "pe_oi":   int(pe.get("openInterest",             0)),
                "pe_chg":  int(pe.get("changeinOpenInterest",     0)),
                "pe_pchg": float(pe.get("pchangeinOpenInterest",  0)),
                "pe_iv":   float(pe.get("impliedVolatility",      0)),
                "pe_ltp":  float(pe.get("lastPrice",              0)),
            }

        # Totals and derived metrics
        tot_ce = int(filtered.get("CE", {}).get("totOI", 0))
        tot_pe = int(filtered.get("PE", {}).get("totOI", 0))
        pcr    = round(tot_pe / tot_ce, 2) if tot_ce > 0 else 0

        max_pain  = calc_max_pain(chain_map)
        pain_diff = round(spot - max_pain) if max_pain else 0
        pain_tag  = (f"Spot {abs(pain_diff)}pts "
                     f"{'BELOW ↓' if pain_diff < 0 else 'ABOVE ↑'} MaxPain")

        vix     = fetch_india_vix()
        vix_str = str(vix) if vix else "N/A"
        vix_tag = (" 🔥HIGH" if vix and vix > 20
                   else " ✅LOW" if vix and vix < 14 else "")

        # ── CE table: highest strike first (descending) ──
        ce_oi_vals = {s: chain_map.get(s, {}).get("ce_oi", 0) for s in ce_strikes}
        ce_wall    = max(ce_oi_vals, key=ce_oi_vals.get)

        ce_rows = []
        for s in sorted(ce_strikes, reverse=True):
            d     = chain_map.get(s, {})
            oi_l  = round(d.get("ce_oi",   0) / 100_000, 1)
            chg_l = round(d.get("ce_chg",  0) / 100_000, 1)
            pchg  = d.get("ce_pchg", 0)
            iv    = d.get("ce_iv",   0)
            emoji, label = oi_signal(d.get("ce_chg", 0), pchg, "CE")
            wall_tag = " 🧱" if s == ce_wall else ""
            ce_rows.append(
                f"{s:>7,}  {oi_l:>5.1f}L  "
                f"{('%+.1f' % chg_l) + 'L':>8}  "
                f"IV{iv:>5.1f}  {emoji} {label}{wall_tag}"
            )

        # ATM row shown at bottom of CE block
        atm_d      = chain_map.get(atm, {})
        atm_ce_oi  = round(atm_d.get("ce_oi",   0) / 100_000, 1)
        atm_ce_chg = round(atm_d.get("ce_chg",  0) / 100_000, 1)
        atm_pchg   = atm_d.get("ce_pchg", 0)
        atm_iv     = atm_d.get("ce_iv", 0)
        atm_e, atm_l = oi_signal(atm_d.get("ce_chg", 0), atm_pchg, "CE")
        atm_ce_row   = (
            f"{'★'+str(atm):>7}  {atm_ce_oi:>5.1f}L  "
            f"{('%+.1f' % atm_ce_chg) + 'L':>8}  "
            f"IV{atm_iv:>5.1f}  {atm_e} {atm_l}  ←ATM"
        )

        # ── PE table: highest (ATM) first, then lower ──
        pe_oi_vals  = {s: chain_map.get(s, {}).get("pe_oi", 0)
                       for s in pe_strikes if s != atm}
        pe_wall     = max(pe_oi_vals, key=pe_oi_vals.get) if pe_oi_vals else atm

        pe_rows = []
        for s in sorted(pe_strikes, reverse=True):
            d     = chain_map.get(s, {})
            oi_l  = round(d.get("pe_oi",   0) / 100_000, 1)
            chg_l = round(d.get("pe_chg",  0) / 100_000, 1)
            pchg  = d.get("pe_pchg", 0)
            iv    = d.get("pe_iv",   0)
            emoji, label = oi_signal(d.get("pe_chg", 0), pchg, "PE")
            atm_tag  = "  ←ATM" if s == atm else ""
            wall_tag = " 🧱"    if s == pe_wall else ""
            pe_rows.append(
                f"{s:>7,}  {oi_l:>5.1f}L  "
                f"{('%+.1f' % chg_l) + 'L':>8}  "
                f"IV{iv:>5.1f}  {emoji} {label}{atm_tag}{wall_tag}"
            )

        # Key strike callouts for summary
        ce_adding  = max(ce_strikes,
                         key=lambda x: chain_map.get(x, {}).get("ce_chg", 0))
        ce_exiting = min(ce_strikes,
                         key=lambda x: chain_map.get(x, {}).get("ce_chg", 0))
        pe_adding  = max(pe_strikes,
                         key=lambda x: chain_map.get(x, {}).get("pe_chg", 0))
        pe_exiting = min(pe_strikes,
                         key=lambda x: chain_map.get(x, {}).get("pe_chg", 0))

        ce_exit_val = chain_map.get(ce_exiting, {}).get("ce_chg", 0)
        pe_exit_val = chain_map.get(pe_exiting, {}).get("pe_chg", 0)

        # ── Compose message ──
        sep = "─" * 50

        header = (
            f"<b>🔍 {index_name} OI Snapshot  {ist_str()} IST</b>\n"
            f"Spot <b>₹{spot:,.1f}</b>  ATM <b>{atm:,}</b>  "
            f"VIX <b>{vix_str}{vix_tag}</b>\n"
            f"Expiry: {expiry}  PCR: <b>{pcr}</b>  MaxPain: <b>{max_pain:,}</b>\n"
            f"<i>{pain_tag}</i>\n"
            f"{sep}"
        )

        ce_block = (
            f"\n🔴 <b>CALL SIDE — Sellers cap upside here</b>\n"
            f"<code>"
            f" Strike     OI     ΔOI      IV    Signal\n"
            f"{sep}\n"
        )
        for row in ce_rows:
            ce_block += row + "\n"
        ce_block += f"{sep}\n"
        ce_block += atm_ce_row + "\n"
        ce_block += "</code>"

        ce_summary = (
            f"CE Wall (max OI): <b>{ce_wall:,}</b>  "
            f"Total: <b>{round(tot_ce/100_000,1)}L</b>\n"
            f"🔴 Sellers most active at <b>{ce_adding:,}</b>"
        )
        if ce_exit_val < -30_000:
            ce_summary += f"\n🟢 Covering (shorts exiting) at <b>{ce_exiting:,}</b>"

        pe_block = (
            f"\n\n🟢 <b>PUT SIDE — Buyers defend support here</b>\n"
            f"<code>"
            f" Strike     OI     ΔOI      IV    Signal\n"
            f"{sep}\n"
        )
        for row in pe_rows:
            pe_block += row + "\n"
        pe_block += "</code>"

        pe_summary = (
            f"PE Wall (max OI): <b>{pe_wall:,}</b>  "
            f"Total: <b>{round(tot_pe/100_000,1)}L</b>\n"
            f"🟢 Support strongest at <b>{pe_adding:,}</b>"
        )
        if pe_exit_val < -30_000:
            pe_summary += f"\n🔴 Unwinding (support leaving) at <b>{pe_exiting:,}</b>"

        footer = (
            f"\n\n<b>📌 Market Bias</b>\n"
            f"PCR {pcr} → {pcr_verdict(pcr)}\n"
            f"CE Wall: <b>{ce_wall:,}</b>  ←resistance\n"
            f"PE Wall: <b>{pe_wall:,}</b>  ←support\n"
            f"Range: <b>{pe_wall:,} – {ce_wall:,}</b>"
        )

        return header + ce_block + ce_summary + pe_block + pe_summary + footer

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"⚠️ {index_name} OI error: {e}"


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
        print(f"[CHECK] {name} {tf} at {ist_str()}")
        interval = "FIVE_MINUTE" if tf == "5M" else "FIFTEEN_MINUTE"
        df       = fetch_data(token, interval)

        if df is None or len(df) < 20:
            print(f"[CHECK] {name} {tf} — insufficient data")
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
