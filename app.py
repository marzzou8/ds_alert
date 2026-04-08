from flask import Flask
import threading
import requests
import pandas as pd
import time
import os
import numpy as np

app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Scalping Bot running"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# === CONFIG from environment variables ===
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Trading parameters
ATR_STOP_MULT = 1.5
ATR_TARGET_MULT = 3.0
SIGNAL_COOLDOWN = 300  # seconds
MIN_CANDLES = 200

last_signal_time = 0

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def get_oanda_candles():
    url = "https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    params = {"granularity": "M1", "count": 200, "price": "M"}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    data = r.json()
    if "candles" not in data:
        return None
    rows = []
    for c in data["candles"]:
        rows.append({
            "close": float(c["mid"]["c"]),
            "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"])
        })
    return pd.DataFrame(rows)

def add_indicators(df):
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['bb_mid'] = df['close'].rolling(20).mean()
    df['bb_std'] = df['close'].rolling(20).std()
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)),
                   abs(df['low'] - df['close'].shift(1)))
    )
    df['atr'] = df['tr'].rolling(14).mean()
    return df

def get_signal(df):
    if len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_above = prev['ema9'] > prev['ema20']
    curr_above = curr['ema9'] > curr['ema20']
    bullish_cross = (not prev_above) and curr_above
    bearish_cross = prev_above and (not curr_above)
    above_200 = curr['close'] > curr['ema200']
    below_200 = curr['close'] < curr['ema200']
    oversold = curr['rsi'] < 30
    overbought = curr['rsi'] > 70
    touch_lower = curr['close'] <= curr['bb_lower']
    touch_upper = curr['close'] >= curr['bb_upper']
    if bullish_cross and above_200 and oversold and touch_lower:
        return "BUY"
    if bearish_cross and below_200 and overbought and touch_upper:
        return "SELL"
    return None

def calculate_sl_tp(df, signal):
    entry = df['close'].iloc[-1]
    atr = df['atr'].iloc[-1]
    if pd.isna(atr) or atr <= 0:
        atr = 2.0
    if signal == "BUY":
        sl = entry - (atr * ATR_STOP_MULT)
        tp = entry + (atr * ATR_TARGET_MULT)
    else:
        sl = entry + (atr * ATR_STOP_MULT)
        tp = entry - (atr * ATR_TARGET_MULT)
    return round(entry,2), round(sl,2), round(tp,2)

def run_bot():
    global last_signal_time
    send_telegram("🚀 Gold Scalping Bot Started (EMA+RSI+BB)")
    while True:
        try:
            df = get_oanda_candles()
            if df is None or len(df) < MIN_CANDLES:
                time.sleep(30)
                continue
            df = add_indicators(df)
            signal = get_signal(df)
            now = time.time()
            if signal and (now - last_signal_time) > SIGNAL_COOLDOWN:
                entry, sl, tp = calculate_sl_tp(df, signal)
                msg = f"""{signal} XAUUSD\nEntry: {entry}\nSL: {sl}\nTP: {tp}\nRSI: {df['rsi'].iloc[-1]:.1f}"""
                send_telegram(msg)
                last_signal_time = now
        except Exception as e:
            print("Bot error:", e)
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    run_bot()