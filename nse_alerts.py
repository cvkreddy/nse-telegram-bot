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

# FIX 1: Correct tokens for NSE exchange (not NFO)
# These are the SPOT INDEX tokens — works for candle data
SYMBOLS = {
    "NIFTY":     "26000",    # exchange = NSE
    "BANKNIFTY": "26009"     # exchange = NSE
}

smart = None


# ===== FIX 2: IST timezone helper =====
# Render server runs UTC — always use ist_now() for time checks
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
        }, timeout=10)
        print(f"[TG] {r.status_code} | {msg[:60]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")


# ===== SMART LOGIN =====
def smart_login():
    global smart
    if smart is not None:
        return smart
    try:
        obj    = SmartConnect(api_key=API_KEY)
        totp   = pyotp.TOTP(TOTP_SECRET).now()
        result = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        print(f"[LOGIN] {result.get('message','ok')}")
        smart  = obj
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        smart = None
    return smart


# ===== FETCH DATA =====
# FIX 1 continued: exchange must be NSE for index candle data
def fetch_data(token, interval):
    try:
        obj = smart_login()
        if obj is None:
            print("[FETCH] No session")
            return None

        fromdate = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        todate   = datetime.now().strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange":    "NSE",      # ← FIXED: was "NFO"
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    fromdate,
            "todate":      todate
        }
        print(f"[FETCH] token={token} interval={interval}")

        data = obj.getCandleData(params)

        if data is None:
            print("[FETCH] Response is None")
            return None

        if not data.get("status"):
            print(f"[FETCH] API error: {data.get('message','unknown')}")
            return None

        if data.get("data") is None:
            print("[FETCH] data field is None")
            return None

        rows = data["data"]
        if len(rows) == 0:
            print("[FETCH] Empty rows")
            return None

        df          = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"])
        df['Close'] = pd.to_numeric(df['close'])
        df          = df.dropna()
        print(f"[FETCH] Got {len(df)} rows, last close={df['Close'].iloc[-1]:.1f}")
        return df

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

    # EMA with value + direction
    e7    = round(float(last['ema7']),  1)
    e15   = round(float(last['ema15']), 1)
    e7s   = "↑" if last['ema7']  > prev['ema7']  else "↓" if last['ema7']  < prev['ema7']  else "→"
    e15s  = "↑" if last['ema15'] > prev['ema15'] else "↓" if last['ema15'] < prev['ema15'] else "→"
    cross = "EMA7 &gt; EMA15" if last['ema7'] > last['ema15'] else "EMA7 &lt; EMA15"
    ema   = f"{cross}  EMA7:{e7}{e7s}  EMA15:{e15}{e15s}"

    # Price
    price = round(float(last['Close']))
    dist  = round(float(last['Close']) - float(last['ema7']), 2)
    ptxt  = f"{price} (+{dist}) ↑ Above EMA7" if dist > 0 else f"{price} ({dist}) ↓ Below EMA7"

    # SuperTrend with flip
    s1b  = bool(last['st1'])
    s2b  = bool(last['st2'])
    s1p  = bool(prev['st1'])
    s2p  = bool(prev['st2'])
    f1   = " [BULL FLIP ↑]" if s1b and not s1p else " [BEAR FLIP ↓]" if not s1b and s1p else ""
    f2   = " [BULL FLIP ↑]" if s2b and not s2p else " [BEAR FLIP ↓]" if not s2b and s2p else ""
    st   = f"ST(2,3):{'↑' if s1b else '↓'}{f1}  ST(2,2.5):{'↑' if s2b else '↓'}{f2}"

    # RSI
    rp   = round(float(prev['rsi']), 1)
    rn   = round(float(last['rsi']), 1)
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
            # Don't spam Telegram with "no data" — just log it
            print(f"[CHECK] {name} {tf} — insufficient data ({len(df) if df is not None else 0} rows)")
            return

        df['ema7']  = EMAIndicator(df['Close'], 7).ema_indicator()
        df['ema15'] = EMAIndicator(df['Close'], 15).ema_indicator()
        df['rsi']   = RSIIndicator(df['Close'], 15).rsi()
        df['st1']   = supertrend(df, 2, 3)
        df['st2']   = supertrend(df, 2, 2.5)

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


# ===== RUN — FIX 3: Rate limiting guard =====
# Angel One allows ~3 req/sec but has daily limits
# Running 2 symbols × 2 TFs = 4 calls — space them out
last_run_5m  = -1
last_run_15m = -1

def run():
    global last_run_5m, last_run_15m

    now    = ist_now()
    minute = now.minute
    print(f"[RUN] {now.strftime('%H:%M:%S')} IST")

    # Market hours check using IST
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5 or not (9*60+15 <= mins <= 15*60+31):
        print(f"[RUN] Outside market hours ({ist_str()} IST) — skipping")
        return

    bucket_15 = minute // 15
    bucket_5  = minute // 5

    if bucket_15 != last_run_15m:
        last_run_15m = bucket_15
        print(f"[RUN] 15M check")
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "15M")
            time.sleep(3)   # FIX 3: 3 sec gap between calls
        return   # don't also run 5M on a 15M minute

    if bucket_5 != last_run_5m:
        last_run_5m = bucket_5
        print(f"[RUN] 5M check")
        for name, token in SYMBOLS.items():
            check_symbol(name, token, "5M")
            time.sleep(3)   # FIX 3: 3 sec gap between calls


# ===== THREAD =====
def run_bot():
    print(f"BOT STARTED — IST: {ist_str()}")

    # Startup confirmation on Telegram
    send_telegram(
        f"✅ <b>NSE Bot Started</b>  [{ist_str()} IST]\n"
        f"Watching: {', '.join(SYMBOLS.keys())}\n"
        "Alerts during market hours 9:15–15:30 IST\n"
        "Running first check now..."
    )

    # First run immediately
    run()

    # FIX 3: every 5 min is enough — not every 1 min
    schedule.every(5).minutes.do(run)

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except Exception as e:
            print(f"[THREAD ERROR] {e}")
            send_telegram(f"❌ Thread Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    Thread(target=run_bot).start()
