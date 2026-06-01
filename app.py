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
    return "P&F Gold Scalping Bot running"

@app.route('/health')
def health():
    return "OK", 200

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# === CONFIG ===
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# P&F parameters
BOX_SIZE = 2.0
REVERSAL = 3
SL_BOXES = 4
TP_BOXES = 6
ALERT_AT_BOX = 4
PROFIT_TRIGGER = 3.0

last_alert_time = 0
ALERT_COOLDOWN = 300

# P&F state
pf_direction = None
pf_boxes = []

# Trade monitoring state
trade_active = False
trade_entry = 0.0
trade_direction = None
trade_sl = 0.0
trade_tp = 0.0
trade_be_triggered = False

# === EMA helper ===
def compute_ema(df, period=50):
    return df['close'].ewm(span=period, adjust=False).mean()

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
    try:
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
    except Exception as e:
        print("Data error:", e)
        return None

def update_pf(price, current_direction, current_boxes):
    if current_direction is None:
        box_level = round(price / BOX_SIZE) * BOX_SIZE
        return ('X', [box_level], False)

    last_box = current_boxes[-1]
    if current_direction == 'X':
        if price >= last_box + BOX_SIZE:
            new_boxes = current_boxes + [last_box + BOX_SIZE]
            return ('X', new_boxes, False)
        elif price <= last_box - (REVERSAL * BOX_SIZE):
            new_box_level = last_box - BOX_SIZE
            new_boxes = [new_box_level]
            return ('O', new_boxes, False)
        else:
            return (current_direction, current_boxes, False)
    else:  # 'O'
        if price <= last_box - BOX_SIZE:
            new_boxes = current_boxes + [last_box - BOX_SIZE]
            return ('O', new_boxes, False)
        elif price >= last_box + (REVERSAL * BOX_SIZE):
            new_box_level = last_box + BOX_SIZE
            new_boxes = [new_box_level]
            return ('X', new_boxes, False)
        else:
            return (current_direction, current_boxes, False)

def monitor_trade(price):
    global trade_active, trade_sl, trade_be_triggered, trade_entry, trade_direction, trade_tp

    if not trade_active:
        return

    profit = price - trade_entry if trade_direction == 'BUY' else trade_entry - price

    if trade_direction == 'BUY' and price >= trade_tp:
        send_telegram(f"✅ TP HIT! Entry {trade_entry}, TP {trade_tp}, Profit +{price - trade_entry:.2f}")
        trade_active = False
        return
    elif trade_direction == 'SELL' and price <= trade_tp:
        send_telegram(f"✅ TP HIT! Entry {trade_entry}, TP {trade_tp}, Profit +{trade_entry - price:.2f}")
        trade_active = False
        return

    if trade_direction == 'BUY' and price <= trade_sl:
        send_telegram(f"❌ SL HIT! Entry {trade_entry}, SL {trade_sl}, Loss {trade_entry - price:.2f}")
        trade_active = False
        return
    elif trade_direction == 'SELL' and price >= trade_sl:
        send_telegram(f"❌ SL HIT! Entry {trade_entry}, SL {trade_sl}, Loss {price - trade_entry:.2f}")
        trade_active = False
        return

    if not trade_be_triggered and profit >= PROFIT_TRIGGER:
        old_sl = trade_sl
        trade_sl = trade_entry
        trade_be_triggered = True
        send_telegram(f"🔹 Profit +{profit:.2f} reached. SL moved to BE {trade_entry:.2f}. Old SL {old_sl:.2f}")

def run_bot():
    global pf_direction, pf_boxes, last_alert_time
    global trade_active, trade_entry, trade_direction, trade_sl, trade_tp, trade_be_triggered

    send_telegram("🚀 Bot Started | EMA filter ON")

    while True:
        try:
            df = get_oanda_candles()
            if df is None or len(df) < 10:
                time.sleep(30)
                continue

            latest_price = df['close'].iloc[-1]

            # Compute EMAs
            df['ema_fast'] = compute_ema(df, period=20)
            df['ema_slow'] = compute_ema(df, period=50)
            ema_fast = df['ema_fast'].iloc[-1]
            ema_slow = df['ema_slow'].iloc[-1]

            monitor_trade(latest_price)

            if not trade_active:
                new_dir, new_boxes, _ = update_pf(latest_price, pf_direction, pf_boxes)

                if pf_direction is not None and new_dir != pf_direction:
                    print(f"New {new_dir} column started at {new_boxes[0]}")
                elif pf_direction == new_dir and len(new_boxes) > len(pf_boxes):
                    new_box_count = len(new_boxes)
                    print(f"Added {new_dir} box #{new_box_count} at {new_boxes[-1]}")

                    # EMA filter
                    trend_ok = (new_dir == 'X' and ema_fast > ema_slow) or (new_dir == 'O' and ema_fast < ema_slow)

                    if new_box_count == ALERT_AT_BOX and (time.time() - last_alert_time) > ALERT_COOLDOWN and trend_ok:
                        entry_price = new_boxes[-1]
                        if new_dir == 'X':
                            sl = entry_price - (SL_BOXES * BOX_SIZE)
                            tp = entry_price + (TP_BOXES * BOX_SIZE)
                            msg = f"🔔 BUY XAUUSD (P&F+EMA)\nEntry {entry_price:.2f}, SL {sl:.2f}, TP {tp:.2f}, Trend Bullish"
                        else:
                            sl = entry_price + (SL_BOXES * BOX_SIZE)
                            tp = entry_price - (TP_BOXES * BOX_SIZE)
                            msg = f"🔔 SELL XAUUSD (P&F+EMA)\nEntry {entry_price:.2f}, SL {sl:.2f}, TP {tp:.2f}, Trend Bearish"
                        send_telegram(msg)
                        last_alert_time = time.time()

                        trade_active = True
                        trade_entry = entry_price
                        trade_direction = 'BUY' if new_dir == 'X' else 'SELL'
                        trade_sl = sl
                        trade_tp = tp
                        trade_be_triggered = False
                        send_telegram(f"📊 Monitoring {trade_direction} @ {trade_entry}")

                pf_direction, pf_boxes = new_dir, new_boxes
            else:
                new_dir, new_boxes, _ = update_pf(latest_price, pf_direction, pf_boxes)
                pf_direction, pf_boxes = new_dir, new_boxes

        except Exception as e:
            print("Bot loop error:", e)
            send_telegram(f"⚠️ Bot error: {str(e)[:100]}")

        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    run_bot()
