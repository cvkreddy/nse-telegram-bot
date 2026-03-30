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

SYMBOLS = {
    "NIFTY":     "26000",
    "BANKNIFTY": "26009",
    "SENSEX":    "26037"
}

OI_STRIKES_EACH_SIDE    = 5
NIFTY_STRIKE_GAP        = 50
BANKNIFTY_STRIKE_GAP    = 100
OI_CHANGE_THRESHOLD_PCT = 3     # alert if OI changed > 3%

smart    = None
oi_prev  = {}
max_oi_prev = {}


# ===== IST TIME HELPER =====
# FIX BUG 4: Render runs UTC — always use ist_now() for market hour checks
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    """Always returns current time in IST regardless of server timezone."""
    return datetime.now(IST)

def ist_str():
    return ist_now().strftime("%H:%M")

def is_market_open():
    """True only on weekdays between 9:15 AM and 3:31 PM IST."""
    now = ist_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 31


# ===== TELEGRAM =====
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            r = requests.post(url, data={
                "chat_id":    CHAT_ID,
                "text":       chunk,
                "parse_mode": "HTML"
            }, timeout=10)
            print(f"[TG] status={r.status_code} chunk_len={len(chunk)}")
    except Exception as e:
        print(f"[TG ERROR] {e}")


# ===== SMARTAPI LOGIN =====
def smart_login():
    global smart
    if smart is not None:
        return smart
    try:
        obj  = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        result = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        print(f"[LOGIN] {result}")
        smart = obj
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        smart = None
    return smart


# ===== FETCH CANDLE DATA =====
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
        print(f"[FETCH ERROR] {e}")
        return None


# ===== SUPERTREND =====
def supertrend(df, period=2, multiplier=3):
    hl2       = df['Close']
    atr       = df['Close'].rolling(period).std()
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    trend     = [True]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > upperband.iloc[i-1]:
            trend.append(True)
        elif df['Close'].iloc[i] < lowerband.iloc[i-1]:
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

    # EMA with actual values and direction arrows
    ema7_val    = round(float(last['ema7']), 1)
    ema15_val   = round(float(last['ema15']), 1)
    ema7_slope  = "↑" if last['ema7'] > prev['ema7'] else "↓" if last['ema7'] < prev['ema7'] else "→"
    ema15_slope = "↑" if last['ema15'] > prev['ema15'] else "↓" if last['ema15'] < prev['ema15'] else "→"
    cross       = "EMA7 &gt; EMA15" if last['ema7'] > last['ema15'] else "EMA7 &lt; EMA15"
    ema_txt     = f"{cross}  |  EMA7:{ema7_val}{ema7_slope}  EMA15:{ema15_val}{ema15_slope}"

    # Price vs EMA7
    price     = round(float(last['Close']))
    dist      = round(float(last['Close']) - float(last['ema7']), 2)
    price_txt = (f"{price} (+{dist}) ↑ Above EMA7" if dist > 0
                 else f"{price} ({dist}) ↓ Below EMA7")

    # SuperTrend with flip detection
    st1_bull = bool(last['st1'])
    st2_bull = bool(last['st2'])
    st1_prev = bool(prev['st1'])
    st2_prev = bool(prev['st2'])
    st1_flip = (" [FLIPPED BULL ↑]" if st1_bull and not st1_prev
                else " [FLIPPED BEAR ↓]" if not st1_bull and st1_prev else "")
    st2_flip = (" [FLIPPED BULL ↑]" if st2_bull and not st2_prev
                else " [FLIPPED BEAR ↓]" if not st2_bull and st2_prev else "")
    st_txt   = (f"ST(2,3): {'↑ BULL' if st1_bull else '↓ BEAR'}{st1_flip}  |  "
                f"ST(2,2.5): {'↑ BULL' if st2_bull else '↓ BEAR'}{st2_flip}")

    # RSI with zone
    r_prev    = round(float(prev['rsi']), 1)
    r_now     = round(float(last['rsi']), 1)
    diff      = round(r_now - r_prev, 1)
    rsi_zone  = "Overbought" if r_now >= 70 else "Oversold" if r_now <= 30 else "Neutral"
    rsi_arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
    rsi_txt   = f"{r_prev}→{r_now} {rsi_arrow} ({'+' if diff>=0 else ''}{diff})  [{rsi_zone}]"

    return {"ema": ema_txt, "price": price_txt, "st": st_txt, "rsi": rsi_txt}


# ===== CHECK SYMBOL =====
def check_symbol(name, token, tf):
    try:
        print(f"[CHECK] {name} {tf} at {ist_str()}")
        interval = "FIVE_MINUTE" if tf == "5M" else "FIFTEEN_MINUTE"
        df       = fetch_data(token, interval)
        if df is None or len(df) < 20:
            print(f"[CHECK] {name} no data")
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
            f"<b>📊 {name} ({tf})  {ist_str()}</b>\n"
            f"{'─'*30}\n"
            f"<b>EMA  :</b> {a['ema']}\n"
            f"<b>Price:</b> {a['price']}\n"
            f"<b>ST   :</b> {a['st']}\n"
            f"<b>RSI  :</b> {a['rsi']}"
        )
        send_telegram(msg)
    except Exception as e:
        print(f"[ERROR] {name}: {e}")
        send_telegram(f"❌ {name} Error: {e}")


# ===== OI HELPERS =====
def get_atm_strike(spot_price, strike_gap):
    return int(round(spot_price / strike_gap) * strike_gap)

def format_oi_number(n):
    n = abs(int(n))
    if n >= 10_000_000: return f"{n/10_000_000:.1f}Cr"
    if n >= 100_000:    return f"{n/100_000:.1f}L"
    if n >= 1_000:      return f"{n/1000:.1f}K"
    return str(n)

def get_nearest_expiry(index_name):
    MONTHS     = ["JAN","FEB","MAR","APR","MAY","JUN",
                  "JUL","AUG","SEP","OCT","NOV","DEC"]
    today      = ist_now()
    exp_day    = 2 if "BANK" in index_name else 3
    days_ahead = (exp_day - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    exp = today + timedelta(days=days_ahead)
    return f"{exp.day:02d}{MONTHS[exp.month-1]}{str(exp.year)[2:]}"

def fetch_spot_price(index_name):
    try:
        obj  = smart_login()
        tok  = SYMBOLS[index_name]
        data = obj.ltpData("NSE", index_name, tok)
        if data and data.get("data"):
            return float(data["data"]["ltp"])
    except Exception as e:
        print(f"[SPOT ERROR] {e}")
    return 0.0

def interpret_oi_change(ce_chg, pe_chg):
    if ce_chg > 0 and pe_chg > 0:   return "Both adding — strong support/resistance forming"
    if ce_chg > 0 and pe_chg < 0:   return "CE adding + PE exiting — bearish pressure ↓"
    if ce_chg < 0 and pe_chg > 0:   return "CE exiting + PE adding — bullish support ↑"
    if ce_chg < 0 and pe_chg < 0:   return "Both exiting — positions unwinding"
    return "Minimal change"

def interpret_pcr(pcr):
    if pcr > 1.2: return "Bullish tilt"
    if pcr < 0.8: return "Bearish tilt"
    return "Neutral"


# ===== FETCH OPTION CHAIN OI =====
def fetch_option_chain_oi(index_name, spot_price, strike_gap):
    obj    = smart_login()
    atm    = get_atm_strike(spot_price, strike_gap)
    expiry = get_nearest_expiry(index_name)
    strikes = [atm + i * strike_gap
               for i in range(-OI_STRIKES_EACH_SIDE, OI_STRIKES_EACH_SIDE + 1)]
    oi_data = {}
    for strike in strikes:
        oi_data[strike] = {"CE": 0, "PE": 0, "CE_ltp": 0, "PE_ltp": 0,
                           "CE_iv": 0, "PE_iv": 0,
                           "CE_vol": 0, "PE_vol": 0}
        for opt_type in ["CE", "PE"]:
            try:
                symbol_name  = f"{index_name}{expiry}{strike}{opt_type}"
                search_result = obj.searchScrip("NFO", symbol_name)
                if not search_result or not search_result.get("data"):
                    continue
                token = search_result["data"][0]["symboltoken"]
                quote = obj.ltpData("NFO", symbol_name, token)
                if quote and quote.get("data"):
                    d = quote["data"]
                    oi_data[strike][opt_type]         = int(d.get("opentinterest", 0))
                    oi_data[strike][f"{opt_type}_ltp"] = float(d.get("ltp", 0))
                    oi_data[strike][f"{opt_type}_iv"]  = float(d.get("impliedvolatility", 0))
                    oi_data[strike][f"{opt_type}_vol"] = int(d.get("volume", 0))
            except:
                pass
        time.sleep(0.1)
    return oi_data, atm


# ===== OI ANALYSIS (core) =====
def check_oi_changes(index_name, force_full=False):
    global oi_prev, max_oi_prev
    try:
        gap  = BANKNIFTY_STRIKE_GAP if "BANK" in index_name else NIFTY_STRIKE_GAP
        spot = fetch_spot_price(index_name)
        if not spot:
            print(f"[OI] No spot for {index_name}")
            return

        current_oi, atm = fetch_option_chain_oi(index_name, spot, gap)
        strikes         = sorted(current_oi.keys())
        ts              = ist_str()

        # First run — store baseline
        if index_name not in oi_prev:
            oi_prev[index_name] = current_oi
            max_ce = max(strikes, key=lambda s: current_oi[s]["CE"])
            max_pe = max(strikes, key=lambda s: current_oi[s]["PE"])
            max_oi_prev[index_name] = {"ce": max_ce, "pe": max_pe}
            print(f"[OI] {index_name} baseline stored")
            if not force_full:
                return

        prev_oi = oi_prev[index_name]

        # Compute changes
        changes = {}
        for s in strikes:
            prev_ce = prev_oi.get(s, {}).get("CE", 0)
            prev_pe = prev_oi.get(s, {}).get("PE", 0)
            cur_ce  = current_oi[s]["CE"]
            cur_pe  = current_oi[s]["PE"]
            ce_chg  = cur_ce - prev_ce
            pe_chg  = cur_pe - prev_pe
            ce_pct  = (ce_chg / prev_ce * 100) if prev_ce > 0 else 0
            pe_pct  = (pe_chg / prev_pe * 100) if prev_pe > 0 else 0
            changes[s] = {
                "ce_now": cur_ce, "pe_now": cur_pe,
                "ce_chg": ce_chg, "pe_chg": pe_chg,
                "ce_pct": ce_pct, "pe_pct": pe_pct,
                "ce_ltp": current_oi[s]["CE_ltp"],
                "pe_ltp": current_oi[s]["PE_ltp"],
                "ce_iv":  current_oi[s]["CE_iv"],
                "pe_iv":  current_oi[s]["PE_iv"],
                "ce_vol": current_oi[s]["CE_vol"],
                "pe_vol": current_oi[s]["PE_vol"],
            }

        # Aggregates
        total_ce_oi  = sum(current_oi[s]["CE"] for s in strikes)
        total_pe_oi  = sum(current_oi[s]["PE"] for s in strikes)
        total_ce_chg = sum(changes[s]["ce_chg"] for s in strikes)
        total_pe_chg = sum(changes[s]["pe_chg"] for s in strikes)
        pcr          = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

        max_ce_strike = max(strikes, key=lambda s: current_oi[s]["CE"])
        max_pe_strike = max(strikes, key=lambda s: current_oi[s]["PE"])
        max_ce_oi     = current_oi[max_ce_strike]["CE"]
        max_pe_oi     = current_oi[max_pe_strike]["PE"]

        # Any significant change?
        any_sig = force_full or any(
            abs(changes[s]["ce_pct"]) >= OI_CHANGE_THRESHOLD_PCT or
            abs(changes[s]["pe_pct"]) >= OI_CHANGE_THRESHOLD_PCT
            for s in strikes
        )

        if not any_sig:
            oi_prev[index_name] = current_oi
            return

        # Wall shift detection
        prev_max     = max_oi_prev.get(index_name, {})
        wall_alerts  = []
        if prev_max.get("ce") and prev_max["ce"] != max_ce_strike:
            wall_alerts.append(
                f"⚠️ CE WALL SHIFTED: {prev_max['ce']} → {max_ce_strike} "
                f"({'up' if max_ce_strike > prev_max['ce'] else 'down'})"
            )
        if prev_max.get("pe") and prev_max["pe"] != max_pe_strike:
            wall_alerts.append(
                f"⚠️ PE WALL SHIFTED: {prev_max['pe']} → {max_pe_strike} "
                f"({'up' if max_pe_strike > prev_max['pe'] else 'down'})"
            )

        # ── Build message ─────────────────────────────────────────────────
        pcr_txt   = interpret_pcr(pcr)
        sentiment = ("Bearish Undertone" if pcr < 0.9 else
                     "Bullish Undertone" if pcr > 1.1 else "Neutral Setup")

        lines = [
            f"<b>📊 {index_name} Options Chain: {sentiment}</b>  [{ts}]",
            "",
            f"<b>Spot:</b> ₹{spot:,.1f}   <b>ATM:</b> ₹{atm}   <b>PCR:</b> {pcr} ({pcr_txt})",
            f"Max CE (Resistance): <b>₹{max_ce_strike}</b>  OI={format_oi_number(max_ce_oi)}",
            f"Max PE (Support):    <b>₹{max_pe_strike}</b>  OI={format_oi_number(max_pe_oi)}",
            f"Total CE OI: {format_oi_number(total_ce_oi)}  ({'+' if total_ce_chg>=0 else ''}{format_oi_number(total_ce_chg)}) | "
            f"Total PE OI: {format_oi_number(total_pe_oi)}  ({'+' if total_pe_chg>=0 else ''}{format_oi_number(total_pe_chg)})",
            "",
        ]

        if wall_alerts:
            lines.extend(wall_alerts)
            lines.append("")

        # ── CE side table ────────────────────────────────────────────────
        lines.append("<b>📞 CALL SIDE — Resistance Zones</b>")
        lines.append("<code>Strike  OI(L)  OI Chg      Vol/OI   IV%   Insight</code>")

        ce_sorted = sorted(strikes, key=lambda s: changes[s]["ce_now"], reverse=True)[:4]
        ce_movers = sorted(strikes, key=lambda s: abs(changes[s]["ce_pct"]), reverse=True)[:3]
        ce_show   = sorted(set(ce_sorted[:3] + ce_movers[:2]))

        for s in sorted(ce_show):
            ch      = changes[s]
            oi_l    = ch["ce_now"] / 100000
            chg_pct = ch["ce_pct"]
            chg_l   = ch["ce_chg"] / 100000
            vol_oi  = round(ch["ce_vol"] / ch["ce_now"], 2) if ch["ce_now"] > 0 else 0
            iv      = ch["ce_iv"]
            arrow   = "↑" if ch["ce_chg"] > 0 else "↓"
            atm_tag = " [ATM]" if s == atm else ""
            max_tag = " ◀MAX" if s == max_ce_strike else ""
            if chg_pct > 50:   insight = "Massive short buildup — sellers defending aggressively"
            elif chg_pct > 20: insight = "Active shorting zone — resistance building"
            elif chg_pct > 0:  insight = "Moderate call addition"
            elif chg_pct < -20: insight = "Short covering — resistance weakening"
            else:               insight = "CE unwinding"
            lines.append(
                f"<code>{s:>7}{atm_tag:6} {oi_l:>6.1f}  "
                f"{arrow}{abs(chg_l):.1f}({chg_pct:+.0f}%)  "
                f"{vol_oi:>6.2f}  {iv:>4.1f}%</code>{max_tag}"
            )
            lines.append(f"  <i>→ {insight}</i>")

        lines.append("")

        # ── PE side table ────────────────────────────────────────────────
        lines.append("<b>📉 PUT SIDE — Support Zones</b>")
        lines.append("<code>Strike  OI(L)  OI Chg      Vol/OI   IV%   Insight</code>")

        pe_sorted = sorted(strikes, key=lambda s: changes[s]["pe_now"], reverse=True)[:4]
        pe_movers = sorted(strikes, key=lambda s: abs(changes[s]["pe_pct"]), reverse=True)[:3]
        pe_show   = sorted(set(pe_sorted[:3] + pe_movers[:2]), reverse=True)

        for s in pe_show:
            ch      = changes[s]
            oi_l    = ch["pe_now"] / 100000
            chg_pct = ch["pe_pct"]
            chg_l   = ch["pe_chg"] / 100000
            vol_oi  = round(ch["pe_vol"] / ch["pe_now"], 2) if ch["pe_now"] > 0 else 0
            iv      = ch["pe_iv"]
            arrow   = "↑" if ch["pe_chg"] > 0 else "↓"
            atm_tag = " [ATM]" if s == atm else ""
            max_tag = " ◀MAX" if s == max_pe_strike else ""
            if chg_pct > 20:    insight = "Fresh put buying — buyers protecting downside"
            elif chg_pct > 0:   insight = "Put accumulation — support building"
            elif chg_pct < -20: insight = "Short covering — prior support breaking, bearish"
            else:               insight = "PE unwinding — support eroding"
            lines.append(
                f"<code>{s:>7}{atm_tag:6} {oi_l:>6.1f}  "
                f"{arrow}{abs(chg_l):.1f}({chg_pct:+.0f}%)  "
                f"{vol_oi:>6.2f}  {iv:>4.1f}%</code>{max_tag}"
            )
            lines.append(f"  <i>→ {insight}</i>")

        lines.append("")

        # ── Full strike table ────────────────────────────────────────────
        lines.append("<b>📋 Full OI Snapshot (CE ← ATM → PE)</b>")
        lines.append("<code>Strike    CE OI   CE Chg   PE OI   PE Chg   PCR</code>")
        for s in sorted(strikes, reverse=True):
            ce_l   = changes[s]["ce_now"] / 100000
            pe_l   = changes[s]["pe_now"] / 100000
            ce_c   = changes[s]["ce_chg"] / 100000
            pe_c   = changes[s]["pe_chg"] / 100000
            s_pcr  = round(changes[s]["pe_now"] / changes[s]["ce_now"], 2) if changes[s]["ce_now"] > 0 else 0
            ca     = "↑" if ce_c > 0 else "↓" if ce_c < 0 else "→"
            pa     = "↑" if pe_c > 0 else "↓" if pe_c < 0 else "→"
            tag    = " ◀ATM" if s == atm else ""
            lines.append(
                f"<code>{s:>7}{tag:6} {ce_l:>5.1f}L {ca}{abs(ce_c):.1f}  "
                f"{pe_l:>5.1f}L {pa}{abs(pe_c):.1f}  {s_pcr:.2f}</code>"
            )

        lines.append("")

        # ── ATM interpretation ───────────────────────────────────────────
        atm_ch  = changes.get(atm, {})
        interp  = interpret_oi_change(atm_ch.get("ce_chg",0), atm_ch.get("pe_chg",0))
        lines.append(f"<b>ATM Interpretation:</b> <i>{interp}</i>")
        lines.append("")

        # ── Trader takeaway ──────────────────────────────────────────────
        lines.append("<b>🎯 Trader Takeaway</b>")
        if max_pe_strike < spot < max_ce_strike:
            rng = max_ce_strike - max_pe_strike
            lines.append(
                f"Price inside range ({max_pe_strike}–{max_ce_strike}, {rng} pts). "
                f"Break above {max_ce_strike} = bull breakout. "
                f"Break below {max_pe_strike} = bear breakdown."
            )
        elif spot >= max_ce_strike:
            lines.append(
                f"Price at/above CE wall ({max_ce_strike}). "
                f"Watch call writer covering — if CE OI drops, upside accelerates."
            )
        else:
            lines.append(
                f"Price at/below PE support ({max_pe_strike}). "
                f"If PE OI holds, bounce likely. If PE unwinds, deeper fall."
            )
        lines.append(
            f"\n<b>Key Levels:</b> Support ₹{max_pe_strike} | ATM ₹{atm} | Resistance ₹{max_ce_strike}"
        )

        send_telegram("\n".join(lines))

        # Update state
        oi_prev[index_name]     = current_oi
        max_oi_prev[index_name] = {"ce": max_ce_strike, "pe": max_pe_strike}

    except Exception as e:
        import traceback
        print(f"[OI ERROR] {index_name}: {traceback.format_exc()}")
        send_telegram(f"❌ OI Error {index_name}: {e}")


def run_oi_check():
    """5-min OI change check — only alerts if > threshold."""
    if not is_market_open():
        return
    for name in ["NIFTY", "BANKNIFTY"]:
        check_oi_changes(name, force_full=False)
        time.sleep(2)


def send_oi_summary():
    """Full OI snapshot — no threshold — used at scheduled times."""
    if ist_now().weekday() >= 5:
        return
    for name in ["NIFTY", "BANKNIFTY"]:
        check_oi_changes(name, force_full=True)
        time.sleep(3)


def send_premarket_summary():
    """9:00 AM pre-market briefing."""
    if ist_now().weekday() >= 5:
        return
    send_telegram(
        "<b>🌅 PRE-MARKET OI BRIEFING</b>\n"
        f"Time: {ist_str()} IST  |  Market opens at 9:15 AM\n"
        "Fetching overnight OI positions..."
    )
    send_oi_summary()
    send_telegram(
        "\n<b>📌 Pre-Market Checklist:</b>\n"
        "• Max CE strike = intraday resistance\n"
        "• Max PE strike = intraday support\n"
        "• PCR &gt; 1.0 = bullish bias  |  PCR &lt; 0.9 = bearish bias\n"
        "• Watch OI addition in first 30 min — confirms direction\n"
        "Good luck today! 🎯"
    )


# ===== RUN (EMA / ST / RSI checks) =====
def run():
    try:
        now = ist_now()

        # ✅ TEMPORARY: REMOVE MARKET CHECK
        # if not is_market_open():
        #     print(f"[RUN] Market closed at {ist_str()} IST — skipping")
        #     return

        print(f"[RUN] Executing at {ist_str()}")

        if now.minute % 15 == 0:
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "15M")

        elif now.minute % 5 == 0:
            for name, token in SYMBOLS.items():
                check_symbol(name, token, "5M")

        # ✅ FORCE TEST MESSAGE
        else:
            send_telegram(f"⏱ TEST RUN at {ist_str()}")

    except Exception as e:
        send_telegram(f"❌ Bot Error: {e}")


# ===== RUN BOT — FIXED STRUCTURE =====
def run_bot():
    print(f"🔥 BOT THREAD STARTED — IST: {ist_str()}")

    # ── Confirm bot is alive on Telegram ──────────────────────────────────
    send_telegram(
        f"✅ <b>NSE Bot Started</b>  [{ist_str()} IST]\n"
        f"Watching: {', '.join(SYMBOLS.keys())}\n"
        f"OI alert threshold: {OI_CHANGE_THRESHOLD_PCT}%\n"
        "Will alert during market hours (9:15–15:30 IST)"
    )

    # ── Register ALL schedules BEFORE the while loop ───────────────────────
    # EMA / ST / RSI — every 5 min
    schedule.every(5).minutes.do(run)

    # OI change detection — every 5 min (silent unless threshold hit)
    schedule.every(5).minutes.do(run_oi_check)

    # Forced full OI snapshots at specific IST times
    # schedule library uses LOCAL time on Render = UTC, so we offset:
    # 9:00 AM IST = 3:30 AM UTC
    # 9:30 AM IST = 4:00 AM UTC
    # 11:30 AM IST = 6:00 AM UTC
    # 1:00 PM IST = 7:30 AM UTC
    # 2:30 PM IST = 9:00 AM UTC
    # 3:30 PM IST = 10:00 AM UTC
    schedule.every().day.at("03:30").do(send_premarket_summary)  # 9:00 AM IST
    schedule.every().day.at("04:00").do(send_oi_summary)         # 9:30 AM IST
    schedule.every().day.at("06:00").do(send_oi_summary)         # 11:30 AM IST
    schedule.every().day.at("07:30").do(send_oi_summary)         # 1:00 PM IST
    schedule.every().day.at("09:00").do(send_oi_summary)         # 2:30 PM IST
    schedule.every().day.at("10:00").do(send_oi_summary)         # 3:30 PM IST

    # ── Single while loop — clean and correct ─────────────────────────────
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)   # check every 30 sec — efficient and not too slow
        except Exception as e:
            print(f"[THREAD ERROR] {e}")
            send_telegram(f"❌ Thread Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    Thread(target=run_bot).start()
