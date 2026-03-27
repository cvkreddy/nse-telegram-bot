# ===== FLASK (RENDER KEEP ALIVE) =====
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running"

def run_web():
    app.run(host='0.0.0.0', port=10000)

Thread(target=run_web).start()


# ===== IMPORTS =====
import requests
import pandas as pd
import schedule
import time
from datetime import datetime
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

from SmartApi import SmartConnect
import pyotp
import os


# ===== CONFIG =====
BOT_TOKEN = "8622319954:AAFIAMBQm7jgyZZjAuaYDhnPHKElqvFjzDY"
CHAT_ID = "1592988014"

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")


SYMBOLS = {
    "NIFTY": "26000",
    "BANKNIFTY": "26009",
    "SENSEX": "26037"
}

last_signals = {}
smart = None


# ===== TELEGRAM =====
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})


# ===== SMARTAPI LOGIN =====

def smart_login():
    global smart

    if smart is not None:
        return smart

    print("TOTP_SECRET VALUE:", TOTP_SECRET)

    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()

    print("Generated TOTP:", totp)

    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)

    smart = obj
    return smart


def fetch_data(symbol_token):
    global smart
    obj = smart_login()

    try:
        fromdate = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        todate = datetime.now().strftime("%Y-%m-%d %H:%M")

        historic = obj.getCandleData({
            "exchange": "NSE",
            "symboltoken": symbol_token,
            "interval": "FIVE_MINUTE",
            "fromdate": fromdate,
            "todate": todate
        })

        data = historic['data']

    except Exception as e:
        print("Re-login:", e)
        smart = None
        obj = smart_login()

        # ✅ USE SAME DYNAMIC DATES AGAIN
        fromdate = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        todate = datetime.now().strftime("%Y-%m-%d %H:%M")

        historic = obj.getCandleData({
            "exchange": "NSE",
            "symboltoken": symbol_token,
            "interval": "FIVE_MINUTE",
            "fromdate": fromdate,
            "todate": todate
        })

        data = historic['data']

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume"
    ])

    df['Close'] = df['close']
    df = df.dropna()

    return df

# ===== SUPERTREND =====
def supertrend(df, period=2, multiplier=3):
    hl2 = df['Close']
    atr = df['Close'].rolling(period).std()

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    trend = [True]

    for i in range(1, len(df)):
        if df['Close'][i] > upperband[i-1]:
            trend.append(True)
        elif df['Close'][i] < lowerband[i-1]:
            trend.append(False)
        else:
            trend.append(trend[i-1])

    return trend


# ===== MAIN LOGIC =====
def check_symbol(name, token):
    send_telegram(f"TEST: {name} running")   # ✅ correct place


    global last_signals

    df = fetch_data(token)

    # EMA
    df['ema7'] = EMAIndicator(df['Close'], 7).ema_indicator()
    df['ema15'] = EMAIndicator(df['Close'], 15).ema_indicator()

    # RSI
    df['rsi'] = RSIIndicator(df['Close'], 15).rsi()
    df['rsi_ema'] = EMAIndicator(df['rsi'], 30).ema_indicator()

    # Supertrend
    df['st1'] = supertrend(df, 2, 3)
    df['st2'] = supertrend(df, 2, 2.5)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = round(last['Close'])

    # ===== EMA =====
    if last['ema7'] > last['ema15'] and prev['ema7'] <= prev['ema15']:
        send_telegram(f"📈 {name} EMA BUY\nPrice: {price}")

    if last['ema7'] < last['ema15'] and prev['ema7'] >= prev['ema15']:
        send_telegram(f"📉 {name} EMA SELL\nPrice: {price}")

    # ===== ST =====
    if last['st1'] and not prev['st1']:
        send_telegram(f"🔥 {name} ST (2,3) BUY\nPrice: {price}")

    if not last['st1'] and prev['st1']:
        send_telegram(f"🔥 {name} ST (2,3) SELL\nPrice: {price}")

    if last['st2'] and not prev['st2']:
        send_telegram(f"⚡ {name} ST (2,2.5) BUY\nPrice: {price}")

    if not last['st2'] and prev['st2']:
        send_telegram(f"⚡ {name} ST (2,2.5) SELL\nPrice: {price}")

    # ===== RSI =====
    if last['rsi'] > last['rsi_ema'] and prev['rsi'] <= prev['rsi_ema']:
        send_telegram(f"📊 {name} RSI BUY\nValue: {round(last['rsi'],2)}")

    if last['rsi'] < last['rsi_ema'] and prev['rsi'] >= prev['rsi_ema']:
        send_telegram(f"📊 {name} RSI SELL\nValue: {round(last['rsi'],2)}")


def run():
    try:
        print("RUN FUNCTION CALLED")
        send_telegram("🚀 RUNNING NOW")

        now = datetime.now()

        for name, token in SYMBOLS.items():
            check_symbol(name, token)

        print("Checked:", now)

    except Exception as e:
        print("ERROR IN RUN:", e)
        send_telegram(f"❌ Bot Error: {e}")



# ===== THREAD =====
def run_bot():
    print("🔥 BOT THREAD STARTED")

    run()   # ✅ ADD THIS LINE (important)

    schedule.every(5).minutes.do(run)

    while True:
        print("Checking schedule...")
        schedule.run_pending()
        time.sleep(5)
    
    
# START THREAD
if __name__ == "__main__":
    Thread(target=run_bot).start()



