from flask import Flask
import threading
import requests
import pandas as pd
import time
import os
import numpy as np
from datetime import datetime
from collections import deque

app = Flask(__name__)

@app.route('/')
def home():
    return "P&F Gold Scalping Bot - EMA Cross Filter"

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

# === SCALPING PARAMETERS ===
BOX_SIZE = 1.0           # $1 per box (faster signals for scalping)
REVERSAL = 2             # 2-box reversal (more frequent signals)
ALERT_AT_BOX = 2         # Enter at 2nd box completion (quick entries)

# === RISK MANAGEMENT ===
POSITION_SIZE = 0.03     # Total position: 0.03 lots
TP_POINTS = 3.0          # Take profit at +3 points ($3 profit)
BE_TRIGGER_POINTS = 1.0  # Move SL to BE after +$1 profit
PARTIAL_CLOSE_SIZE = 0.01  # Close 0.01 lots at BE trigger
REMAINING_SIZE = 0.02    # Remaining 0.02 lots to run to TP
SL_POINTS = 2.0          # Stop loss at -2 points ($2 loss)

# === EMA CROSS FILTER ===
EMA_FAST = 9             # Fast EMA period
EMA_SLOW = 21            # Slow EMA period
REQUIRE_ALIGNMENT = True  # Only trade when EMAs are aligned
CROSS_CONFIRM_BARS = 1    # Require cross to be confirmed for 1 bar

# === OTHER FILTERS ===
MIN_TRADE_HOUR = 8       # London open (UTC)
MAX_TRADE_HOUR = 20      # US close
MAX_DAILY_TRADES = 15    # Scalping = more trades allowed
MIN_VOLATILITY = 0.5     # Minimum ATR in dollars
ALERT_COOLDOWN = 60      # 1 minute between signals

# === TRADE STATE ===
class ScalpingTrade:
    def __init__(self):
        self.active = False
        self.entry_price = 0.0
        self.direction = None  # 'BUY' or 'SELL'
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.be_triggered = False
        self.partial_closed = False
        self.full_size = POSITION_SIZE
        self.remaining_size = POSITION_SIZE
        self.open_time = None

trade = ScalpingTrade()

# P&F state
pf_direction = None
pf_boxes = []
last_alert_time = 0
daily_trades = 0
last_reset_day = None
performance_stats = deque(maxlen=50)

# EMA cross tracking
last_ema_cross = None  # 'GOLDEN_CROSS' (fast above slow) or 'DEATH_CROSS' (fast below slow)
cross_bars_count = 0

def send_telegram(msg):
    """Send Telegram message"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def get_oanda_candles(timeframe="M1"):
    """Get candles for specified timeframe"""
    url = "https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    
    granularity = timeframe
    count = 200 if timeframe == "M1" else 100
    
    params = {"granularity": granularity, "count": count, "price": "MBA"}
    
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
                "low": float(c["mid"]["l"]),
                "bid_c": float(c["bid"]["c"]) if "bid" in c else float(c["mid"]["c"]),
                "ask_c": float(c["ask"]["c"]) if "ask" in c else float(c["mid"]["c"]),
                "time": c["time"]
            })
        
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"Data error ({timeframe}):", e)
        return None

def calculate_ema(df, period):
    """Calculate EMA"""
    return df['close'].ewm(span=period, adjust=False).mean()

def check_ema_cross(df_m1, df_m5=None):
    """Check EMA cross condition for trend filtering"""
    global last_ema_cross, cross_bars_count
    
    # Use M5 for trend (smoother), but fallback to M1 if M5 not available
    df = df_m5 if df_m5 is not None else df_m1
    
    # Calculate EMAs
    ema_fast = calculate_ema(df, EMA_FAST)
    ema_slow = calculate_ema(df, EMA_SLOW)
    
    current_fast = ema_fast.iloc[-1]
    current_slow = ema_slow.iloc[-1]
    prev_fast = ema_fast.iloc[-2]
    prev_slow = ema_slow.iloc[-2]
    
    # Determine current cross status
    if current_fast > current_slow:
        current_status = 'BULLISH'  # Golden cross (uptrend)
    else:
        current_status = 'BEARISH'  # Death cross (downtrend)
    
    # Check if cross just happened
    cross_just_happened = False
    cross_type = None
    
    if prev_fast <= prev_slow and current_fast > current_slow:
        cross_just_happened = True
        cross_type = 'GOLDEN_CROSS'
        cross_bars_count = 0
        send_telegram(f"📈 GOLDEN CROSS detected! EMA{EMA_FAST} crossed above EMA{EMA_SLOW} | Trend: BULLISH")
    elif prev_fast >= prev_slow and current_fast < current_slow:
        cross_just_happened = True
        cross_type = 'DEATH_CROSS'
        cross_bars_count = 0
        send_telegram(f"📉 DEATH CROSS detected! EMA{EMA_FAST} crossed below EMA{EMA_SLOW} | Trend: BEARISH")
    
    # Update cross confirmation count
    if cross_just_happened:
        last_ema_cross = cross_type
        cross_bars_count = 1
    elif last_ema_cross is not None:
        cross_bars_count += 1
    
    return current_status, cross_just_happened, cross_type

def get_ema_values(df):
    """Get current EMA values for display"""
    ema_fast = calculate_ema(df, EMA_FAST)
    ema_slow = calculate_ema(df, EMA_SLOW)
    return ema_fast.iloc[-1], ema_slow.iloc[-1]

def get_atr(df, period=14):
    """Calculate ATR for volatility filter"""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean().iloc[-1]

def is_good_scalping_time():
    """Only trade during high liquidity periods"""
    now_utc = datetime.utcnow()
    hour = now_utc.hour
    minute = now_utc.minute
    
    # Best scalping hours: London session (8-16 UTC)
    if 8 <= hour <= 16:
        # Avoid first 5 minutes of major news hours
        news_hours = [8, 9, 10, 13, 14, 15]
        if hour in news_hours and minute < 5:
            return False
        return True
    
    # Allow partial US session (16-20 UTC)
    if 16 < hour <= 20:
        return True
        
    return False

def update_pf_scalping(price, current_direction, current_boxes):
    """Fast P&F update for scalping"""
    if current_direction is None or len(current_boxes) == 0:
        box_level = round(price / BOX_SIZE) * BOX_SIZE
        return ('X', [box_level], 1, False)
    
    last_box = current_boxes[-1]
    
    if current_direction == 'X':
        if price >= last_box + BOX_SIZE:
            new_boxes = current_boxes + [last_box + BOX_SIZE]
            return ('X', new_boxes, len(new_boxes), False)
        elif price <= last_box - (REVERSAL * BOX_SIZE):
            new_box_level = last_box - BOX_SIZE
            new_boxes = [new_box_level]
            return ('O', new_boxes, 1, True)
        else:
            return (current_direction, current_boxes, len(current_boxes), False)
    else:
        if price <= last_box - BOX_SIZE:
            new_boxes = current_boxes + [last_box - BOX_SIZE]
            return ('O', new_boxes, len(new_boxes), False)
        elif price >= last_box + (REVERSAL * BOX_SIZE):
            new_box_level = last_box + BOX_SIZE
            new_boxes = [new_box_level]
            return ('X', new_boxes, 1, True)
        else:
            return (current_direction, current_boxes, len(current_boxes), False)

def check_scalping_signal(df, price, direction, box_count, is_reversal, ema_status):
    """Check signal with EMA cross filter"""
    
    # === 1. EMA CROSS FILTER (MOST IMPORTANT) ===
    if REQUIRE_ALIGNMENT:
        if direction == 'BUY' and ema_status != 'BULLISH':
            return False, f"EMA not aligned for BUY (Trend: {ema_status})"
        if direction == 'SELL' and ema_status != 'BEARISH':
            return False, f"EMA not aligned for SELL (Trend: {ema_status})"
    
    # === 2. Volatility filter ===
    atr = get_atr(df)
    if atr < MIN_VOLATILITY:
        return False, f"Low volatility (ATR: {atr:.2f})"
    
    # === 3. Momentum check ===
    price_change_1m = (price - df['close'].iloc[-2]) if len(df) > 1 else 0
    if direction == 'BUY' and price_change_1m < -0.2:
        return False, "Momentum against"
    if direction == 'SELL' and price_change_1m > 0.2:
        return False, "Momentum against"
    
    # === 4. Spread check ===
    bid = df['bid_c'].iloc[-1] if 'bid_c' in df else price - 0.15
    ask = df['ask_c'].iloc[-1] if 'ask_c' in df else price + 0.15
    spread = abs(ask - bid)
    
    if spread > 0.25:
        return False, f"Wide spread ({spread:.2f})"
    
    # === 5. Quick RSI check ===
    changes = df['close'].diff().tail(14)
    gains = changes[changes > 0].sum()
    losses = abs(changes[changes < 0].sum())
    rsi = 100 - (100 / (1 + (gains / losses if losses > 0 else 10)))
    
    if direction == 'BUY' and rsi > 80:
        return False, f"Overbought (RSI {rsi:.1f})"
    if direction == 'SELL' and rsi < 20:
        return False, f"Oversold (RSI {rsi:.1f})"
    
    # === 6. Require minimum boxes ===
    if box_count < ALERT_AT_BOX:
        return False, f"Only {box_count} boxes"
    
    # === 7. Avoid trading immediately after cross (wait for confirmation) ===
    global cross_bars_count
    if cross_bars_count < CROSS_CONFIRM_BARS:
        return False, f"Waiting for cross confirmation ({cross_bars_count}/{CROSS_CONFIRM_BARS} bars)"
    
    return True, "OK"

def execute_scalping_trade(direction, entry_price, df, ema_status, ema_fast_val, ema_slow_val):
    """Execute trade with partial close strategy"""
    global trade, daily_trades
    
    # Calculate SL and TP
    if direction == 'BUY':
        sl_price = entry_price - SL_POINTS
        tp_price = entry_price + TP_POINTS
    else:
        sl_price = entry_price + SL_POINTS
        tp_price = entry_price - TP_POINTS
    
    # Set trade parameters
    trade.active = True
    trade.entry_price = entry_price
    trade.direction = direction
    trade.sl_price = sl_price
    trade.tp_price = tp_price
    trade.be_triggered = False
    trade.partial_closed = False
    trade.remaining_size = POSITION_SIZE
    trade.open_time = datetime.utcnow()
    
    # Get ATR value
    atr = get_atr(df)
    
    # Send trade execution message with EMA info
    msg = f"""⚡ SCALPING TRADE EXECUTED ⚡
{direction} XAUUSD
Entry: {entry_price:.2f}
Size: {POSITION_SIZE} lots
SL: {sl_price:.2f} (-${SL_POINTS})
TP: {tp_price:.2f} (+${TP_POINTS})

📊 MARKET FILTERS:
• EMA Trend: {ema_status} (EMA{EMA_FAST}: {ema_fast_val:.2f} | EMA{EMA_SLOW}: {ema_slow_val:.2f})
• ATR: {atr:.2f}
• Box #{box_count if 'box_count' in locals() else ALERT_AT_BOX}

💰 STRATEGY:
• Close 0.01 at +${BE_TRIGGER_POINTS}
• Move 0.02 to BE
• Target +${TP_POINTS} total

Risk: ${SL_POINTS * POSITION_SIZE:.2f}
Potential: ${TP_POINTS * POSITION_SIZE:.2f}
R:R: 1:{TP_POINTS/SL_POINTS:.1f}"""
    
    send_telegram(msg)
    daily_trades += 1

def monitor_scalping_trade(price):
    """Monitor trade with partial close at +1 point"""
    global trade, performance_stats
    
    if not trade.active:
        return
    
    # Calculate current profit
    if trade.direction == 'BUY':
        profit_points = price - trade.entry_price
    else:
        profit_points = trade.entry_price - price
    
    profit_dollars = profit_points * trade.remaining_size
    
    # === CHECK STOP LOSS ===
    if trade.direction == 'BUY' and price <= trade.sl_price:
        loss_dollars = (trade.entry_price - price) * POSITION_SIZE
        msg = f"""❌ SCALP TRADE LOST ❌
{trade.direction} XAUUSD
Entry: {trade.entry_price:.2f}
Exit: {price:.2f}
Loss: -${loss_dollars:.2f}
Time: {(datetime.utcnow() - trade.open_time).seconds // 60} min"""
        
        send_telegram(msg)
        performance_stats.append(False)
        trade.active = False
        return
        
    elif trade.direction == 'SELL' and price >= trade.sl_price:
        loss_dollars = (price - trade.entry_price) * POSITION_SIZE
        msg = f"""❌ SCALP TRADE LOST ❌
{trade.direction} XAUUSD
Entry: {trade.entry_price:.2f}
Exit: {price:.2f}
Loss: -${loss_dollars:.2f}
Time: {(datetime.utcnow() - trade.open_time).seconds // 60} min"""
        
        send_telegram(msg)
        performance_stats.append(False)
        trade.active = False
        return
    
    # === CHECK TAKE PROFIT ===
    if trade.direction == 'BUY' and price >= trade.tp_price:
        total_profit = TP_POINTS * POSITION_SIZE
        win_rate = sum(performance_stats)/len(performance_stats)*100 if performance_stats else 0
        msg = f"""✅ SCALP TP HIT! ✅
{trade.direction} XAUUSD
Total Profit: +${total_profit:.2f}
Entry: {trade.entry_price:.2f}
Exit: {price:.2f}
Time: {(datetime.utcnow() - trade.open_time).seconds // 60} min
Win Rate: {win_rate:.1f}% ({len(performance_stats)} trades)"""
        
        send_telegram(msg)
        performance_stats.append(True)
        trade.active = False
        return
        
    elif trade.direction == 'SELL' and price <= trade.tp_price:
        total_profit = TP_POINTS * POSITION_SIZE
        win_rate = sum(performance_stats)/len(performance_stats)*100 if performance_stats else 0
        msg = f"""✅ SCALP TP HIT! ✅
{trade.direction} XAUUSD
Total Profit: +${total_profit:.2f}
Entry: {trade.entry_price:.2f}
Exit: {price:.2f}
Time: {(datetime.utcnow() - trade.open_time).seconds // 60} min
Win Rate: {win_rate:.1f}% ({len(performance_stats)} trades)"""
        
        send_telegram(msg)
        performance_stats.append(True)
        trade.active = False
        return
    
    # === PARTIAL CLOSE AT +1 POINT ===
    if not trade.be_triggered and profit_points >= BE_TRIGGER_POINTS:
        trade.be_triggered = True
        
        # Close 0.01 lots at +1 point profit
        partial_profit = BE_TRIGGER_POINTS * PARTIAL_CLOSE_SIZE
        
        # Move remaining SL to break-even
        old_sl = trade.sl_price
        trade.sl_price = trade.entry_price
        trade.remaining_size = REMAINING_SIZE
        
        msg = f"""🎯 PARTIAL CLOSE & BREAKEVEN 🎯
{trade.direction} XAUUSD
✅ Closed 0.01 lots at +${BE_TRIGGER_POINTS}
💰 Secured profit: +${partial_profit:.2f}
📊 Remaining 0.02 lots SL moved to BE (${trade.entry_price:.2f})
🎯 Target: +${TP_POINTS} (${TP_POINTS * REMAINING_SIZE:.2f} more)"""
        
        send_telegram(msg)
    
    # Progress update at +2 points
    elif profit_points >= 2.0 and profit_points < 2.1 and trade.be_triggered:
        msg = f"📈 {trade.direction} at +{profit_points:.1f} pts | Running profit: ${profit_dollars:.2f} | 0.02 lots remaining"
        send_telegram(msg)

def run_bot():
    global pf_direction, pf_boxes, last_alert_time, daily_trades, last_reset_day
    global last_ema_cross, cross_bars_count
    
    send_telegram(f"""🎯 P&F GOLD SCALPING BOT v4 - EMA CROSS FILTER 🎯

📊 STRATEGY:
• EMA{EMA_FAST}/{EMA_SLOW} cross for trend direction
• Only trade with the trend
• Enter at {ALERT_AT_BOX}nd box completion
• Partial close at +${BE_TRIGGER_POINTS}
• Target +${TP_POINTS} total

⏰ Hours: {MIN_TRADE_HOUR}-{MAX_TRADE_HOUR} UTC
📈 Max trades: {MAX_DAILY_TRADES}/day
⚡ Cooldown: {ALERT_COOLDOWN} seconds

🚀 BOT ACTIVE!""")
    
    while True:
        try:
            # Reset daily counter
            today = datetime.utcnow().date()
            if last_reset_day != today:
                daily_trades = 0
                last_reset_day = today
                win_rate = sum(performance_stats)/len(performance_stats)*100 if performance_stats else 0
                trades_count = len(performance_stats)
                send_telegram(f"📅 New trading day\nPrevious day: {win_rate:.1f}% win rate ({trades_count} trades)" if trades_count > 0 else "📅 New trading day")
            
            # Get M1 and M5 data
            df_m1 = get_oanda_candles("M1")
            if df_m1 is None or len(df_m1) < 50:
                time.sleep(15)
                continue
            
            df_m5 = get_oanda_candles("M5")
            
            current_price = df_m1['close'].iloc[-1]
            
            # === CHECK EMA CROSS TREND ===
            ema_status, cross_just_happened, cross_type = check_ema_cross(df_m1, df_m5)
            ema_fast_val, ema_slow_val = get_ema_values(df_m5 if df_m5 is not None else df_m1)
            
            # Send cross alert if just happened (but not too often)
            if cross_just_happened:
                send_telegram(f"🔄 EMA CROSS CONFIRMED: {cross_type}\nTrend: {ema_status}\nEMA{EMA_FAST}: {ema_fast_val:.2f}\nEMA{EMA_SLOW}: {ema_slow_val:.2f}")
            
            # === MONITOR ACTIVE TRADE ===
            if trade.active:
                monitor_scalping_trade(current_price)
            
            # === CHECK TRADING CONDITIONS ===
            if not is_good_scalping_time():
                time.sleep(30)
                continue
            
            if daily_trades >= MAX_DAILY_TRADES:
                if daily_trades == MAX_DAILY_TRADES:
                    send_telegram(f"⏸️ Daily limit reached ({MAX_DAILY_TRADES} trades)")
                time.sleep(60)
                continue
            
            # === GENERATE SIGNAL ===
            if not trade.active and (time.time() - last_alert_time) >= ALERT_COOLDOWN:
                
                # Update P&F
                new_dir, new_boxes, box_count, is_reversal = update_pf_scalping(
                    current_price, pf_direction, pf_boxes
                )
                
                # Check for entry opportunity
                should_check = False
                entry_price = None
                direction = None
                
                if pf_direction is not None and new_dir != pf_direction:
                    if box_count >= 1:
                        should_check = True
                        entry_price = new_boxes[-1]
                        direction = 'BUY' if new_dir == 'X' else 'SELL'
                
                elif new_dir == pf_direction and len(new_boxes) > len(pf_boxes):
                    if box_count >= ALERT_AT_BOX:
                        should_check = True
                        entry_price = new_boxes[-1]
                        direction = 'BUY' if new_dir == 'X' else 'SELL'
                
                # Update P&F state
                pf_direction, pf_boxes = new_dir, new_boxes
                
                # Check signal with EMA filter
                if should_check and direction:
                    confirmed, reason = check_scalping_signal(
                        df_m1, current_price, direction, box_count, is_reversal, ema_status
                    )
                    
                    if confirmed:
                        execute_scalping_trade(direction, current_price, df_m1, ema_status, ema_fast_val, ema_slow_val)
                        last_alert_time = time.time()
                        
                        # Send quick alert
                        alert_msg = f"""🔔 {direction} SIGNAL | EMA: {ema_status}
Price: {current_price:.2f}
Box #{box_count} | {is_reversal and 'Reversal' or 'Continuation'}
EMA{EMA_FAST}: {ema_fast_val:.2f} | EMA{EMA_SLOW}: {ema_slow_val:.2f}"""
                        send_telegram(alert_msg)
                    else:
                        # Log rejection reason periodically
                        if box_count == ALERT_AT_BOX and int(time.time()) % 300 < 10:
                            print(f"Signal rejected: {reason}")
            
            # Send periodic EMA status update (every 30 minutes)
            if int(time.time()) % 1800 < 10:
                spread = abs(df_m1['ask_c'].iloc[-1] - df_m1['bid_c'].iloc[-1]) if 'ask_c' in df_m1 else 0
                atr = get_atr(df_m1)
                msg = f"📊 Market Status:\nEMA Trend: {ema_status}\nATR: {atr:.2f}\nSpread: {spread:.2f}\nActive Trade: {trade.active}\nToday: {daily_trades}/{MAX_DAILY_TRADES}"
                send_telegram(msg)
            
            # Sleep for scalping frequency
            time.sleep(10)
            
        except Exception as e:
            print(f"Bot error: {e}")
            import traceback
            traceback.print_exc()
            if "daily_trades" not in str(e) and "performance_stats" not in str(e):
                send_telegram(f"⚠️ Error: {str(e)[:100]}")
            time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    run_bot()
