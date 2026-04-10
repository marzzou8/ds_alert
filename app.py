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
BOX_SIZE = 2.0          # $2 per box
REVERSAL = 3            # 3-box reversal
SL_BOXES = 4            # stop loss = 4 boxes = $8
TP_BOXES = 6            # take profit = 6 boxes = $12
ALERT_AT_BOX = 4        # send alert when 4th box completes
PROFIT_TRIGGER = 3.0    # dollars – when price moves this much in profit, move SL to BE

last_alert_time = 0
ALERT_COOLDOWN = 300    # 5 minutes between signals

# P&F state
pf_direction = None     # 'X' or 'O'
pf_boxes = []           # list of box levels in current column

# Trade monitoring state
trade_active = False
trade_entry = 0.0
trade_direction = None   # 'BUY' or 'SELL'
trade_sl = 0.0
trade_tp = 0.0
trade_be_triggered = False   # whether we already moved SL to BE

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
    """
    Update P&F chart with new price.
    Returns (new_direction, new_boxes, alert_sent_flag)
    """
    if current_direction is None:
        # Initialize: first box
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
    else:  # current_direction == 'O'
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
    """Check active trade, send alerts, update SL to BE if profit reached."""
    global trade_active, trade_sl, trade_be_triggered, trade_entry, trade_direction, trade_tp

    if not trade_active:
        return

    profit = price - trade_entry if trade_direction == 'BUY' else trade_entry - price

    # Check if TP hit
    if trade_direction == 'BUY' and price >= trade_tp:
        msg = f"✅ TP HIT! Trade closed in profit.\nEntry: {trade_entry}\nTP: {trade_tp}\nProfit: +{price - trade_entry:.2f}"
        send_telegram(msg)
        trade_active = False
        return
    elif trade_direction == 'SELL' and price <= trade_tp:
        msg = f"✅ TP HIT! Trade closed in profit.\nEntry: {trade_entry}\nTP: {trade_tp}\nProfit: +{trade_entry - price:.2f}"
        send_telegram(msg)
        trade_active = False
        return

    # Check if SL hit (original or BE)
    if trade_direction == 'BUY' and price <= trade_sl:
        msg = f"❌ SL HIT! Trade closed.\nEntry: {trade_entry}\nSL: {trade_sl}\nLoss: {trade_entry - price:.2f}"
        send_telegram(msg)
        trade_active = False
        return
    elif trade_direction == 'SELL' and price >= trade_sl:
        msg = f"❌ SL HIT! Trade closed.\nEntry: {trade_entry}\nSL: {trade_sl}\nLoss: {price - trade_entry:.2f}"
        send_telegram(msg)
        trade_active = False
        return

    # Check if profit trigger reached and not yet moved SL to BE
    if not trade_be_triggered and profit >= PROFIT_TRIGGER:
        # Move SL to break-even
        old_sl = trade_sl
        trade_sl = trade_entry
        trade_be_triggered = True
        msg = f"🔹P&F Profit +{profit:.2f} reached. SL moved to BE ({trade_entry:.2f}).\nEntry: {trade_entry}\nOld SL: {old_sl:.2f}"
        send_telegram(msg)

def run_bot():
    global pf_direction, pf_boxes, last_alert_time
    global trade_active, trade_entry, trade_direction, trade_sl, trade_tp, trade_be_triggered

    send_telegram("🚀 P&F Gold Scalping Bot Started | Box=2, Rev=3, Alert at box 4 | Trade monitoring ON")

    while True:
        try:
            df = get_oanda_candles()
            if df is None or len(df) < 10:
                time.sleep(30)
                continue

            latest_price = df['close'].iloc[-1]

            # === MONITOR ACTIVE TRADE ===
            monitor_trade(latest_price)

            # === P&F SIGNAL GENERATION (only if no active trade) ===
            if not trade_active:
                new_dir, new_boxes, _ = update_pf(latest_price, pf_direction, pf_boxes)

                # Check if a new column started or a new box added
                if pf_direction is not None and new_dir != pf_direction:
                    # New column just started – box count = 1
                    print(f"New {new_dir} column started at price {new_boxes[0]}")
                elif pf_direction == new_dir and len(new_boxes) > len(pf_boxes):
                    new_box_count = len(new_boxes)
                    print(f"Added {new_dir} box #{new_box_count} at {new_boxes[-1]}")
                    # Send alert when we reach ALERT_AT_BOX
                    if new_box_count == ALERT_AT_BOX and (time.time() - last_alert_time) > ALERT_COOLDOWN:
                        entry_price = new_boxes[-1]  # current box level
                        if new_dir == 'X':
                            sl = entry_price - (SL_BOXES * BOX_SIZE)
                            tp = entry_price + (TP_BOXES * BOX_SIZE)
                            msg = f"""🔔 BUY XAUUSD (P&F)
Entry: {entry_price:.2f} (start of box 5)
SL: {sl:.2f} (4 boxes)
TP: {tp:.2f} (6 boxes)
R:R 1:{TP_BOXES/SL_BOXES:.1f}
Box count: {new_box_count}"""
                        else:
                            sl = entry_price + (SL_BOXES * BOX_SIZE)
                            tp = entry_price - (TP_BOXES * BOX_SIZE)
                            msg = f"""🔔 SELL XAUUSD (P&F)
Entry: {entry_price:.2f} (start of box 5)
SL: {sl:.2f} (4 boxes)
TP: {tp:.2f} (6 boxes)
R:R 1:{TP_BOXES/SL_BOXES:.1f}
Box count: {new_box_count}"""
                        send_telegram(msg)
                        last_alert_time = time.time()

                        # === ACTIVATE TRADE MONITORING ===
                        trade_active = True
                        trade_entry = entry_price
                        trade_direction = 'BUY' if new_dir == 'X' else 'SELL'
                        trade_sl = sl
                        trade_tp = tp
                        trade_be_triggered = False
                        send_telegram(f"📊 Trade monitoring ACTIVE for {trade_direction} @ {trade_entry}")

                # Update global P&F state
                pf_direction, pf_boxes = new_dir, new_boxes
            else:
                # Trade active – still need to update P&F state for future signals but don't generate new ones
                new_dir, new_boxes, _ = update_pf(latest_price, pf_direction, pf_boxes)
                pf_direction, pf_boxes = new_dir, new_boxes

        except Exception as e:
            print("Bot loop error:", e)
            send_telegram(f"⚠️ Bot error: {str(e)[:100]}")

        time.sleep(60)  # check every minute

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    run_bot()
