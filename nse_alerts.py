# ===== FLASK (RENDER KEEP ALIVE) =====
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
import json


# ===== CONFIG =====
BOT_TOKEN    = os.getenv("BOT_TOKEN")
CHAT_ID      = os.getenv("CHAT_ID")
API_KEY      = os.getenv("API_KEY")
CLIENT_ID    = os.getenv("CLIENT_ID")
PASSWORD     = os.getenv("PASSWORD")
TOTP_SECRET  = os.getenv("TOTP_SECRET")

SYMBOLS = {
    "NIFTY":     "26000",
    "BANKNIFTY": "26009",
    "SENSEX":    "26037"
}

# OI config — how many strikes each side of ATM to watch
OI_STRIKES_EACH_SIDE = 5      # ATM ± 5 = 11 strikes total
NIFTY_STRIKE_GAP     = 50     # Nifty strikes at every 50 points
BANKNIFTY_STRIKE_GAP = 100    # BankNifty strikes at every 100 points

# OI change threshold to trigger an alert (avoids noise from tiny changes)
OI_CHANGE_THRESHOLD_PCT = 5   # alert only if OI changed by > 5%

smart = None

# ── OI STATE (stores previous OI so we can compute change) ───────────────
# Structure:  oi_prev[symbol][strike]["CE"/"PE"] = oi_value
oi_prev = {}
# Stores max OI strikes from previous check to detect shift
max_oi_prev = {}


# ===== TELEGRAM =====
def send_telegram(msg):
    try:
        url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        # Split long messages if needed (Telegram limit = 4096 chars)
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            requests.post(url, data={
                "chat_id":    CHAT_ID,
                "text":       chunk,
                "parse_mode": "HTML"
            })
    except:
        pass


# ===== SMARTAPI LOGIN =====
def smart_login():
    global smart
    if smart is not None:
        return smart
    obj  = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj.generateSession(CLIENT_ID, PASSWORD, totp)
    smart = obj
    return smart


# ===== FETCH CANDLE DATA (existing) =====
def fetch_data(symbol_token, interval):
    try:
        obj      = smart_login()
        fromdate = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        todate   = datetime.now().strftime("%Y-%m-%d %H:%M")
        data     = obj.getCandleData({
            "exchange":    "NSE",
            "symboltoken": symbol_token,
            "interval":    interval,
            "fromdate":    fromdate,
            "todate":      todate
        })
        if data is None or data.get("data") is None:
            return None
        df          = pd.DataFrame(data['data'],
                                   columns=["time","open","high","low","close","volume"])
        df['Close'] = df['close']
        return df.dropna()
    except Exception as e:
        print("FETCH ERROR:", e)
        return None


# ===== SUPERTREND (existing) =====
def supertrend(df, period=2, multiplier=3):
    hl2     = df['Close']
    atr     = df['Close'].rolling(period).std()
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    trend   = [True]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > upperband.iloc[i-1]:
            trend.append(True)
        elif df['Close'].iloc[i] < lowerband.iloc[i-1]:
            trend.append(False)
        else:
            trend.append(trend[i-1])
    return trend


# ===== ANALYZE (existing) =====
def analyze(df):
    if df is None or len(df) < 3:
        return None
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    ema   = "EMA7>EMA15" if last['ema7'] > last['ema15'] else "EMA7<EMA15"
    slope = "↑" if last['ema7'] > prev['ema7'] else "↓" if last['ema7'] < prev['ema7'] else "→"
    price = round(last['Close'])
    dist  = round(last['Close'] - last['ema7'], 2)
    price_txt = f"{price} (+{dist}) ↑" if dist > 0 else f"{price} ({dist}) ↓"
    st    = f"{'G' if last['st1'] else 'R'}/{'G' if last['st2'] else 'R'}"
    r_prev = round(prev['rsi'], 1)
    r_now  = round(last['rsi'], 1)
    diff   = round(r_now - r_prev, 1)
    rsi_txt = f"{r_prev}→{r_now} (+{diff}) ↑" if diff > 0 else f"{r_prev}→{r_now} ({diff}) ↓"
    return {"ema": f"{ema} {slope}", "price": price_txt, "st": st, "rsi": rsi_txt}


# ===== CHECK SYMBOL (existing) =====
def check_symbol(name, token, tf):
    try:
        interval = "FIVE_MINUTE" if tf == "5M" else "FIFTEEN_MINUTE"
        df       = fetch_data(token, interval)
        if df is None or len(df) < 20:
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
            f"\n📊 {name} ({tf})\n\n"
            f"EMA  : {a['ema']}\n"
            f"Price: {a['price']}\n"
            f"ST   : {a['st']}\n"
            f"RSI  : {a['rsi']}"
        )
        send_telegram(msg)
    except Exception as e:
        send_telegram(f"❌ {name} Error: {e}")


# ─────────────────────────────────────────────────────────────────────────
# ██  NEW — OI FUNCTIONS  ██
# ─────────────────────────────────────────────────────────────────────────

def get_atm_strike(spot_price, strike_gap):
    """Round spot price to nearest strike gap."""
    return round(round(spot_price / strike_gap) * strike_gap)


def fetch_option_chain_oi(index_name, spot_price, strike_gap):
    """
    Fetch OI for CE and PE across ATM ± OI_STRIKES_EACH_SIDE strikes
    using Angel One's searchScrip + getLTP calls.

    Returns dict: { strike: {"CE": oi, "PE": oi}, ... }
    """
    obj = smart_login()
    atm = get_atm_strike(spot_price, strike_gap)

    # Build list of strikes to check
    strikes = [atm + i * strike_gap
               for i in range(-OI_STRIKES_EACH_SIDE, OI_STRIKES_EACH_SIDE + 1)]

    # Get current expiry (nearest weekly for Nifty/BankNifty)
    expiry = get_nearest_expiry(index_name)

    oi_data = {}

    for strike in strikes:
        oi_data[strike] = {"CE": 0, "PE": 0}
        for opt_type in ["CE", "PE"]:
            try:
                symbol_name = f"{index_name}{expiry}{strike}{opt_type}"

                # Search for the scrip token
                search_result = obj.searchScrip("NFO", symbol_name)
                if not search_result or not search_result.get("data"):
                    continue

                token = search_result["data"][0]["symboltoken"]

                # Fetch market data (OI is in the quote response)
                quote = obj.ltpData("NFO", symbol_name, token)
                if quote and quote.get("data"):
                    oi = int(quote["data"].get("opentinterest", 0))
                    oi_data[strike][opt_type] = oi

            except Exception as e:
                # Silent — individual strike fetch errors are common
                pass

        time.sleep(0.1)   # Rate limit — Angel One allows ~3 req/sec

    return oi_data, atm


def get_nearest_expiry(index_name):
    """
    Returns nearest weekly expiry string in Angel One format e.g. '25APR'
    Nifty expires on Thursday, BankNifty on Wednesday.
    Adjust as needed if expiry day changes.
    """
    today  = datetime.now()
    # Weekly expiry day: Thursday=3 for Nifty, Wednesday=2 for BankNifty
    expiry_weekday = 2 if "BANK" in index_name else 3

    days_ahead = expiry_weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7

    expiry_date = today + timedelta(days=days_ahead)

    # Angel One format: DDMONYY  e.g. 25APR25
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    day   = str(expiry_date.day).zfill(2)
    month = months[expiry_date.month - 1]
    year  = str(expiry_date.year)[2:]
    return f"{day}{month}{year}"


def fetch_spot_price(index_name):
    """Get current spot price for the index."""
    obj   = smart_login()
    token = SYMBOLS.get(index_name)
    if not token:
        return None
    try:
        data = obj.ltpData("NSE", index_name, token)
        if data and data.get("data"):
            return float(data["data"]["ltp"])
    except:
        pass
    return None


def interpret_oi_change(ce_oi_change, pe_oi_change):
    """
    Classic OI interpretation:
    CE OI↑ + PE OI↑ → Support building (bullish at that strike)
    CE OI↑ + PE OI↓ → Bearish (short covering in PE, fresh CE shorts)
    CE OI↓ + PE OI↑ → Bullish (CE short covering, fresh PE shorts)
    CE OI↓ + PE OI↓ → Indecision / exit
    """
    if ce_oi_change > 0 and pe_oi_change > 0:
        return "Both adding — strong support/resistance forming"
    elif ce_oi_change > 0 and pe_oi_change < 0:
        return "CE adding + PE exiting — bearish pressure ↓"
    elif ce_oi_change < 0 and pe_oi_change > 0:
        return "CE exiting + PE adding — bullish support ↑"
    elif ce_oi_change < 0 and pe_oi_change < 0:
        return "Both exiting — positions unwinding"
    else:
        return "Minimal change"


def format_oi_number(n):
    """Format large OI numbers compactly: 12345678 → 12.3L"""
    if n >= 10_000_000:
        return f"{n/10_000_000:.1f}Cr"
    elif n >= 100_000:
        return f"{n/100_000:.1f}L"
    elif n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def check_oi_changes(index_name):
    """
    Core OI analysis function.
    Fetches current OI, compares with previous, sends alert only if
    meaningful change detected.
    """
    global oi_prev, max_oi_prev

    try:
        strike_gap = BANKNIFTY_STRIKE_GAP if "BANK" in index_name else NIFTY_STRIKE_GAP

        # Get current spot
        spot = fetch_spot_price(index_name)
        if not spot:
            return

        # Fetch option chain OI
        current_oi, atm = fetch_option_chain_oi(index_name, spot, strike_gap)

        now_str = datetime.now().strftime("%H:%M")

        # ── First run: just store baseline, no alerts ─────────────────────
        if index_name not in oi_prev:
            oi_prev[index_name] = current_oi
            print(f"[OI] {index_name} baseline stored at {now_str}")
            return

        prev_oi = oi_prev[index_name]

        # ── Compute OI changes per strike ─────────────────────────────────
        strikes   = sorted(current_oi.keys())
        changes   = {}
        for s in strikes:
            prev_ce = prev_oi.get(s, {}).get("CE", 0)
            prev_pe = prev_oi.get(s, {}).get("PE", 0)
            cur_ce  = current_oi[s]["CE"]
            cur_pe  = current_oi[s]["PE"]

            ce_chg  = cur_ce - prev_ce
            pe_chg  = cur_pe - prev_pe

            # Percentage change (avoid div by zero)
            ce_pct  = (ce_chg / prev_ce * 100) if prev_ce > 0 else 0
            pe_pct  = (pe_chg / prev_pe * 100) if prev_pe > 0 else 0

            changes[s] = {
                "ce_now": cur_ce, "pe_now": cur_pe,
                "ce_chg": ce_chg, "pe_chg": pe_chg,
                "ce_pct": ce_pct, "pe_pct": pe_pct,
            }

        # ── Find max OI strikes (support / resistance) ────────────────────
        max_ce_strike = max(strikes, key=lambda s: current_oi[s]["CE"])
        max_pe_strike = max(strikes, key=lambda s: current_oi[s]["PE"])
        max_ce_oi     = current_oi[max_ce_strike]["CE"]
        max_pe_oi     = current_oi[max_pe_strike]["PE"]

        # PCR  (Put-Call Ratio)
        total_pe_oi = sum(current_oi[s]["PE"] for s in strikes)
        total_ce_oi = sum(current_oi[s]["CE"] for s in strikes)
        pcr         = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

        pcr_view = ("Bullish  (PCR > 1.2)" if pcr > 1.2
                    else "Bearish  (PCR < 0.8)" if pcr < 0.8
                    else "Neutral")

        # ── Detect meaningful OI changes ──────────────────────────────────
        significant_changes = []
        for s, ch in changes.items():
            label = "ATM" if s == atm else (f"+{(s-atm)//strike_gap}" if s > atm else f"{(s-atm)//strike_gap}")
            if abs(ch["ce_pct"]) >= OI_CHANGE_THRESHOLD_PCT and ch["ce_now"] > 0:
                direction = "↑ ADDING" if ch["ce_chg"] > 0 else "↓ EXITING"
                significant_changes.append(
                    f"  CE {s} ({label})  {direction}  "
                    f"{format_oi_number(abs(ch['ce_chg']))}  "
                    f"({ch['ce_pct']:+.1f}%)  total={format_oi_number(ch['ce_now'])}"
                )
            if abs(ch["pe_pct"]) >= OI_CHANGE_THRESHOLD_PCT and ch["pe_now"] > 0:
                direction = "↑ ADDING" if ch["pe_chg"] > 0 else "↓ EXITING"
                significant_changes.append(
                    f"  PE {s} ({label})  {direction}  "
                    f"{format_oi_number(abs(ch['pe_chg']))}  "
                    f"({ch['pe_pct']:+.1f}%)  total={format_oi_number(ch['pe_now'])}"
                )

        # ── Detect if max-OI strike shifted (big player repositioning) ─────
        prev_max = max_oi_prev.get(index_name, {})
        max_shift_alerts = []
        if prev_max.get("ce") and prev_max["ce"] != max_ce_strike:
            max_shift_alerts.append(
                f"  CE wall SHIFTED  {prev_max['ce']} → {max_ce_strike}  "
                f"(resistance moved)"
            )
        if prev_max.get("pe") and prev_max["pe"] != max_pe_strike:
            max_shift_alerts.append(
                f"  PE wall SHIFTED  {prev_max['pe']} → {max_pe_strike}  "
                f"(support moved)"
            )

        # ── Build strike-by-strike OI snapshot table ─────────────────────
        # Only send full table if there are significant changes
        if not significant_changes and not max_shift_alerts:
            # Update baseline silently
            oi_prev[index_name] = current_oi
            max_oi_prev[index_name] = {"ce": max_ce_strike, "pe": max_pe_strike}
            return

        # Build detailed table for changed strikes
        table_lines = []
        for s in strikes:
            ch    = changes[s]
            label = "ATM" if s == atm else (
                    f"+{(s-atm)//strike_gap}" if s > atm
                    else f"{(s-atm)//strike_gap}")
            ce_arrow = ("↑" if ch["ce_chg"] > 0 else "↓" if ch["ce_chg"] < 0 else " ")
            pe_arrow = ("↑" if ch["pe_chg"] > 0 else "↓" if ch["pe_chg"] < 0 else " ")

            # Highlight max OI strikes
            ce_tag = " ◀MAX" if s == max_ce_strike else ""
            pe_tag = " ◀MAX" if s == max_pe_strike else ""

            table_lines.append(
                f"{str(s):>6} ({label:>4}) | "
                f"CE {format_oi_number(ch['ce_now']):>6}{ce_arrow}"
                f"({ch['ce_pct']:+5.1f}%){ce_tag} | "
                f"PE {format_oi_number(ch['pe_now']):>6}{pe_arrow}"
                f"({ch['pe_pct']:+5.1f}%){pe_tag}"
            )

        # ── Compose final Telegram message ─────────────────────────────────
        msg_parts = [
            f"<b>📊 OI ALERT — {index_name}</b>  {now_str}",
            f"Spot: <b>{spot:.0f}</b>   ATM: <b>{atm}</b>",
            "",
            f"<b>🏦 Max CE (Resistance):</b> {max_ce_strike}  OI={format_oi_number(max_ce_oi)}",
            f"<b>🛡 Max PE (Support):</b>    {max_pe_strike}  OI={format_oi_number(max_pe_oi)}",
            f"<b>PCR:</b> {pcr}  →  {pcr_view}",
            f"Total CE OI: {format_oi_number(total_ce_oi)}   Total PE OI: {format_oi_number(total_pe_oi)}",
        ]

        if max_shift_alerts:
            msg_parts.append("")
            msg_parts.append("⚠️ <b>OI WALL SHIFTED:</b>")
            msg_parts.extend(max_shift_alerts)

        if significant_changes:
            msg_parts.append("")
            msg_parts.append(f"🔄 <b>Significant OI Changes (&gt;{OI_CHANGE_THRESHOLD_PCT}%):</b>")
            msg_parts.extend(significant_changes)

        msg_parts.append("")
        msg_parts.append("<b>Strike-by-Strike OI:</b>")
        msg_parts.append("<code>Strike        CE OI          PE OI</code>")
        msg_parts.extend([f"<code>{line}</code>" for line in table_lines])

        # ── Interpretation section ─────────────────────────────────────────
        atm_ch = changes.get(atm, {})
        if atm_ch:
            interp = interpret_oi_change(atm_ch["ce_chg"], atm_ch["pe_chg"])
            msg_parts.append("")
            msg_parts.append(f"<b>ATM Interpretation:</b> {interp}")

        # Key levels for intraday
        msg_parts.append("")
        msg_parts.append(
            f"<b>Key Levels:</b>  Support ~{max_pe_strike}  |  Resistance ~{max_ce_strike}"
        )
        if max_pe_strike < spot < max_ce_strike:
            msg_parts.append("Price inside support-resistance range — range-bound likely")
        elif spot >= max_ce_strike:
            msg_parts.append("Price at/above resistance — watch for breakout or rejection")
        else:
            msg_parts.append("Price at/below support — watch for breakdown or bounce")

        send_telegram("\n".join(msg_parts))

        # ── Update stored state ────────────────────────────────────────────
        oi_prev[index_name]       = current_oi
        max_oi_prev[index_name]   = {"ce": max_ce_strike, "pe": max_pe_strike}

    except Exception as e:
        print(f"[OI ERROR] {index_name}: {e}")
        send_telegram(f"❌ OI Error {index_name}: {e}")


def run_oi_check():
    """Runs OI check for Nifty and BankNifty only (they have weekly options)."""
    now = datetime.now()
    # Only during market hours
    if now.weekday() >= 5:
        return
    if not (9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30):
        return

    for name in ["NIFTY", "BANKNIFTY"]:
        check_oi_changes(name)
        time.sleep(2)   # small gap between the two index calls


def send_oi_summary():
    """
    Sends a full OI summary once (no threshold filter) — useful at
    9:30 AM open and 1:00 PM midday to get complete picture.
    """
    global OI_CHANGE_THRESHOLD_PCT
    old_thresh = OI_CHANGE_THRESHOLD_PCT
    OI_CHANGE_THRESHOLD_PCT = 0    # force all strikes to show
    run_oi_check()
    OI_CHANGE_THRESHOLD_PCT = old_thresh


# ─────────────────────────────────────────────────────────────────────────
# ██  RUN LOOP  ██
# ─────────────────────────────────────────────────────────────────────────
def run():
    try:
        now = datetime.now()

        # 15 MIN (priority — EMA/ST/RSI)
        if now.minute % 15 == 0:
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "15M")

        # 5 MIN — EMA/ST/RSI
        elif now.minute % 5 == 0:
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "5M")

    except Exception as e:
        send_telegram(f"❌ Bot Error: {e}")


def run_bot():
    print("BOT THREAD STARTED")

    # Stagger the first runs slightly
    run()
    time.sleep(10)
    run_oi_check()

    # Every 5 minutes — EMA/RSI/ST checks
    schedule.every(5).minutes.do(run)

    # Every 5 minutes — OI change detection (only alerts on meaningful change)
    schedule.every(5).minutes.do(run_oi_check)

    # Full OI summary at open and midday
    schedule.every().day.at("09:00").do(send_oi_summary)
    schedule.every().day.at("13:00").do(send_oi_summary)
    schedule.every().day.at("15:30").do(send_oi_summary)   # closing summary

    while True:
        try:
            schedule.run_pending()
            time.sleep(5)
        except Exception as e:
            send_telegram(f"❌ Thread Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    Thread(target=run_bot).start()
