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
# FIX: Always create a fresh session — don't cache a stale/hung object
def smart_login():
    global smart
    try:
        obj    = SmartConnect(api_key=API_KEY)
        totp   = pyotp.TOTP(TOTP_SECRET).now()
        # FIX: Wrap in executor with 20s timeout so it never hangs forever
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


# ===== FETCH DATA =====
def _do_fetch(obj, params):
    """Inner fetch — run inside executor so we can timeout it."""
    return obj.getCandleData(params)

def fetch_data(token, interval):
    try:
        # FIX: Always re-login — session tokens expire; caching causes silent failures
        obj = smart_login()
        if obj is None:
            print("[FETCH] Login failed — skipping")
            return None

        fromdate = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        todate   = datetime.now().strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange":    "NSE",
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    fromdate,
            "todate":      todate
        }
        print(f"[FETCH] token={token} interval={interval}")

        # FIX: 25s timeout on the actual API call — prevents infinite hang
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_fetch, obj, params)
            data   = future.result(timeout=25)

        if data is None:
            print("[FETCH] Response is None")
            return None
        if not data.get("status"):
            print(f"[FETCH] API error: {data.get('message', 'unknown')}")
            return None
        if not data.get("data"):
            print("[FETCH] Empty data field")
            return None

        rows = data["data"]
        if len(rows) == 0:
            print("[FETCH] Zero rows returned")
            return None

        df          = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"])
        df['Close'] = pd.to_numeric(df['close'])
        df          = df.dropna()
        print(f"[FETCH] Got {len(df)} rows, last close={df['Close'].iloc[-1]:.1f}")
        return df

    except FuturesTimeout:
        print(f"[FETCH ERROR] getCandleData timed out after 25s — token={token}")
        return None
    except Exception as e:
        print(f"[FETCH EXCEPTION] {e}")
        return None


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


# ===== ANALYZE =====
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

        df = fetch_data(token, interval)

        if df is None or len(df) < 20:
            print(f"[CHECK] {name} {tf} — insufficient data ({len(df) if df is not None else 0} rows)")
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


# ===== MARKET HOURS CHECK =====
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
    print(f"[RUN] {now.strftime('%H:%M:%S')} IST")   # FIX: heartbeat every call

    if not in_market_hours():
        print(f"[RUN] Outside market hours — skipping")
        return

    bucket_15 = minute // 15
    bucket_5  = minute // 5

    if bucket_15 != last_run_15m:
        last_run_15m = bucket_15
        print("[RUN] → 15M check")
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "15M")
            time.sleep(4)
        return

    if bucket_5 != last_run_5m:
        last_run_5m = bucket_5
        print("[RUN] → 5M check")
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "5M")
            time.sleep(4)


# ===== MAIN =====
# FIX: Run everything in the MAIN thread — no extra bot thread needed.
# Flask already runs in its own daemon thread.
# The main thread owns the schedule loop, so nothing can kill it silently.
if __name__ == "__main__":
    print(f"BOT STARTED — IST: {ist_str()}")

    send_telegram(
        f"✅ <b>NSE Bot Started</b>  [{ist_str()} IST]\n"
        f"Watching: {', '.join(SYMBOLS.keys())}\n"
        "Alerts during market hours 9:15–15:30 IST\n"
        "Running first check now..."
    )

    # Warm up login once at startup
    print("[STARTUP] Testing login...")
    test = smart_login()
    if test is None:
        send_telegram("⚠️ <b>Warning:</b> Login failed at startup — check API credentials/TOTP")
    else:
        send_telegram("🔐 Login OK — schedule starting")

    # First immediate run
    run()

    # FIX: Schedule in main thread — runs every 5 min
    schedule.every(5).minutes.do(run)

    # FIX: Main thread loop — tighter sleep so schedule fires on time
    while True:
        try:
            schedule.run_pending()
            time.sleep(10)   # check every 10s — won't miss a 5-min window
        except Exception as e:
            print(f"[MAIN ERROR] {e}")
            send_telegram(f"❌ Main Loop Error: {e}")
            time.sleep(10)
