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
from datetime import datetime, timedelta
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from SmartApi import SmartConnect
import pyotp
import os
import threading


# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")

SYMBOLS = {
    "NIFTY": "26000",
    "BANKNIFTY": "26009",
    "SENSEX": "26037"
}

smart = None


# ===== TELEGRAM =====
def send_telegram(msg):
    try:
        print("Sending Telegram:", msg)

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        response = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=5
        )

        print("TELEGRAM RESPONSE:", response.text)

    except Exception as e:
        print("TELEGRAM ERROR:", e)


# ===== SMART LOGIN =====
def smart_login():
    global smart

    if smart is not None:
        return smart

    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj.generateSession(CLIENT_ID, PASSWORD, totp)

    smart = obj
    return smart


# ===== EXCHANGE FIX =====
def get_exchange(name):
    if name == "SENSEX":
        return "BSE_INDEX"
    else:
        return "NSE_INDEX"


# ===== FETCH DATA WITH TIMEOUT =====
def fetch_data(symbol_token, interval, name):
    result = {}

    def api_call():
        try:
            obj = smart_login()

            fromdate = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
            todate = datetime.now().strftime("%Y-%m-%d %H:%M")

            exchange = get_exchange(name)

            data = obj.getCandleData({
                "exchange": exchange,
                "symboltoken": symbol_token,
                "interval": interval,
                "fromdate": fromdate,
                "todate": todate
            })

            result['data'] = data

        except Exception as e:
            result['error'] = str(e)

    t = threading.Thread(target=api_call)
    t.start()
    t.join(timeout=8)

    if t.is_alive():
        print("API TIMEOUT")
        return None

    if 'error' in result:
        print("API ERROR:", result['error'])
        return None

    if result.get('data') is None or result['data'].get('data') is None:
        return None

    df = pd.DataFrame(result['data']['data'], columns=[
        "time","open","high","low","close","volume"
    ])

    df['Close'] = df['close']
    return df.dropna()


# ===== SUPERTREND =====
def supertrend(df, period=2, multiplier=3):
    atr = df['Close'].rolling(period).std()
    upper = df['Close'] + multiplier * atr
    lower = df['Close'] - multiplier * atr

    trend = [True]

    for i in range(1, len(df)):
        if df['Close'][i] > upper[i-1]:
            trend.append(True)
        elif df['Close'][i] < lower[i-1]:
            trend.append(False)
        else:
            trend.append(trend[i-1])

    return trend


# ===== ANALYZE =====
def analyze(df):
    if df is None or len(df) < 3:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    ema = "🟢 EMA7>EMA15" if last['ema7'] > last['ema15'] else "🔴 EMA7<EMA15"
    slope = "↑" if last['ema7'] > prev['ema7'] else "↓" if last['ema7'] < prev['ema7'] else "→"

    price = round(last['Close'])
    dist = round(last['Close'] - last['ema7'], 2)
    price_txt = f"{price} (+{dist}) ↑" if dist > 0 else f"{price} ({dist}) ↓"

    st = f"{'🟢' if last['st1'] else '🔴'}/{'🟢' if last['st2'] else '🔴'}"

    r_prev = round(prev['rsi'], 1)
    r_now = round(last['rsi'], 1)
    diff = round(r_now - r_prev, 1)
    rsi_txt = f"{r_prev}→{r_now} (+{diff}) ↑" if diff > 0 else f"{r_prev}→{r_now} ({diff}) ↓"

    return {
        "ema": f"{ema} {slope}",
        "price": price_txt,
        "st": st,
        "rsi": rsi_txt
    }


# ===== CHECK SYMBOL =====
def check_symbol(name, token, tf):
    try:
        send_telegram(f"⚡ START {name} {tf}")

        interval = "FIVE_MINUTE" if tf == "5M" else "FIFTEEN_MINUTE"

        df = fetch_data(token, interval, name)

        if df is None or len(df) < 20:
            send_telegram(f"⚠️ No data {name} {tf}")
            return

        df['ema7'] = EMAIndicator(df['Close'], 7).ema_indicator()
        df['ema15'] = EMAIndicator(df['Close'], 15).ema_indicator()
        df['rsi'] = RSIIndicator(df['Close'], 15).rsi()
        df['st1'] = supertrend(df, 2, 3)
        df['st2'] = supertrend(df, 2, 2.5)

        a = analyze(df)
        if a is None:
            return

        msg = f"""
📊 {name} ({tf})

TF   | EMA Trend        | Price vs EMA7     | ST      | RSI
-----|------------------|-------------------|---------|-------------------------
{tf}  | {a['ema']:<16} | {a['price']:<17} | {a['st']:<7} | {a['rsi']}
"""

        send_telegram(msg)

    except Exception as e:
        send_telegram(f"❌ {name} Error: {e}")


# ===== RUN (FIXED TIMING) =====
last_run_5m = None
last_run_15m = None

def run():
    global last_run_5m, last_run_15m

    try:
        now = datetime.now()
        print("RUN FUNCTION CALLED:", now)

        minute = now.minute

        # 5 MIN
        if minute // 5 != (last_run_5m if last_run_5m is not None else -1):
            last_run_5m = minute // 5
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "5M")

        # 15 MIN
        if minute // 15 != (last_run_15m if last_run_15m is not None else -1):
            last_run_15m = minute // 15
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "15M")

    except Exception as e:
        send_telegram(f"❌ Bot Error: {e}")


# ===== THREAD =====
def run_bot():
    print("🔥 BOT THREAD STARTED")

    run()

    schedule.every(1).minutes.do(run)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            send_telegram(f"❌ Thread Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    Thread(target=run_bot).start()