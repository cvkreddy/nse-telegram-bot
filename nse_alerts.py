from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running"

def run_web():
    app.run(host='0.0.0.0', port=10000)

Thread(target=run_web).start()


import requests
import pandas as pd
import schedule
import time
from datetime import datetime
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

# ================== CONFIG ==================
BOT_TOKEN = "8622319954:AAFIAMBQm7jgyZZjAuaYDhnPHKElqvFjzDY"
CHAT_ID = "1592988014"

SYMBOLS = {
    "NIFTY": "%5ENSEI",
    "BANKNIFTY": "%5ENSEBANK",
    "SENSEX": "%5EBSESN"
}

last_signals = {}

# ============================================

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# ===== DATA FETCH =====
def fetch_data(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers)

    data = res.json()

    closes = data['chart']['result'][0]['indicators']['quote'][0]['close']

    df = pd.DataFrame({"Close": closes})
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
def check_symbol(name, symbol):
    global last_signals

    df = fetch_data(symbol)

    # ===== EMA =====
    df['ema7'] = EMAIndicator(df['Close'], 7).ema_indicator()
    df['ema15'] = EMAIndicator(df['Close'], 15).ema_indicator()

    # ===== RSI =====
    df['rsi'] = RSIIndicator(df['Close'], 15).rsi()
    df['rsi_ema'] = EMAIndicator(df['rsi'], 30).ema_indicator()

    # ===== ST =====
    df['st1'] = supertrend(df, 2, 3)
    df['st2'] = supertrend(df, 2, 2.5)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = round(last['Close'])

    # ===== EMA CROSS =====
    if last['ema7'] > last['ema15'] and prev['ema7'] <= prev['ema15']:
        key = f"{name}_EMA_BUY"
        if last_signals.get(key) != True:
            send_telegram(f"📈 {name} EMA BUY\nPrice: {price}")
            last_signals[key] = True

    if last['ema7'] < last['ema15'] and prev['ema7'] >= prev['ema15']:
        key = f"{name}_EMA_SELL"
        if last_signals.get(key) != True:
            send_telegram(f"📉 {name} EMA SELL\nPrice: {price}")
            last_signals[key] = True

    # ===== SUPERTREND =====
    if last['st1'] == True and prev['st1'] == False:
        send_telegram(f"🔥 {name} ST (2,3) BUY\nPrice: {price}")

    if last['st1'] == False and prev['st1'] == True:
        send_telegram(f"🔥 {name} ST (2,3) SELL\nPrice: {price}")

    if last['st2'] == True and prev['st2'] == False:
        send_telegram(f"⚡ {name} ST (2,2.5) BUY\nPrice: {price}")

    if last['st2'] == False and prev['st2'] == True:
        send_telegram(f"⚡ {name} ST (2,2.5) SELL\nPrice: {price}")

    # ===== RSI CROSS =====
    if last['rsi'] > last['rsi_ema'] and prev['rsi'] <= prev['rsi_ema']:
        send_telegram(f"📊 {name} RSI BUY\nValue: {round(last['rsi'],2)}")

    if last['rsi'] < last['rsi_ema'] and prev['rsi'] >= prev['rsi_ema']:
        send_telegram(f"📊 {name} RSI SELL\nValue: {round(last['rsi'],2)}")

# ===== MAIN LOOP =====
def run():
    now = datetime.now()

    # market hours filter
    if now.hour < 9 or now.hour > 15:
        return

    for name, symbol in SYMBOLS.items():
        check_symbol(name, symbol)

    print("Checked at:", now)

# ===== START =====
# ===== START BOT IN THREAD =====
def run_bot():
    run()
    schedule.every(5).minutes.do(run)

    while True:
        schedule.run_pending()
        time.sleep(5)

# Run bot in background
Thread(target=run_bot).start()