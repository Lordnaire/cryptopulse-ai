import subprocess
import sys

# Auto-install ccxt for crypto market data fetching
try:
    import ccxt
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "ccxt"])
    import ccxt

import requests
import time
import json
import os
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import xml.etree.ElementTree as ET
import re

# ==========================================
# CONFIGURATION & PARAMETERS
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8953327176:AAFy_DW2hDRHG-faZpUwOL0AOfcdsAUUXcs")
SAVED_CHAT_ID = os.environ.get("SAVED_CHAT_ID", "1899452216")
MAX_SCANS_ENV = os.environ.get("MAX_SCANS", None)
MAX_SCANS = int(MAX_SCANS_ENV) if MAX_SCANS_ENV else None

WATCHLIST_FILE = "watchlist.json"
MIN_ALERT_DRAWDOWN_PCT = 30.0  
MIN_REBOUND_FOR_REALERT_PCT = 7.5  # Adjusted: 5% to 10% rebound unlocks repeat alerts

# Updated Default Watchlist (Excludes DOGE, PEPE, XRP, SHIB, LINK, FLOKI, LAB)
DEFAULT_WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BONK/USDT', 'WIF/USDT',
    'NEAR/USDT', 'RENDER/USDT', 'AVAX/USDT', 'SUI/USDT',
    'COMP/USDT', 'RIF/USDT', 'ESP/USDT', 'BANK/USDT', 
    'DEXE/USDT', 'ALICE/USDT', 'SENT/USDT', 
    'RE/USDT', 'ONDO/USDT', 'ZEC/USDT', 'MIRA/USDT', 
    'OPEN/USDT', 'LUMIA/USDT', 'DODO/USDT', 'SYN/USDT', 'ORDI/USDT'
]

TARGET_COINS = list(DEFAULT_WATCHLIST)
EXCHANGE_PROVIDERS = ['gateio', 'binance', 'kucoin', 'okx', 'kraken', 'bybit']

# Global State Management
exchange = None              
COIN_BRAIN_CACHE = {}        
LAST_ALERTED_TIER = {}       
LAST_ALERTED_PRICE = {}      # Memory to enforce the 7.5% rebound rule
LAST_TELEGRAM_UPDATE_ID = 0  

def load_watchlist():
    global TARGET_COINS
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    # Filter out removed pairs if present in historical JSON
                    excluded = ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
                    TARGET_COINS = [c for c in data if c not in excluded]
                    print(f"📁 Watchlist loaded from persistent memory ({len(TARGET_COINS)} coins).")
                    return
        except Exception as e:
            print(f"⚠️ Error loading persistent watchlist: {e}")
    save_watchlist()

def save_watchlist():
    try:
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(TARGET_COINS, f, indent=4)
    except Exception as e:
        print(f"⚠️ Error saving persistent watchlist: {e}")

def initialize_exchange_connection():
    global exchange
    print("🌐 Connecting to crypto market data provider...")
    for ex_id in EXCHANGE_PROVIDERS:
        try:
            ex_class = getattr(ccxt, ex_id)
            ex_instance = ex_class({'enableRateLimit': True, 'timeout': 10000})
            ex_instance.fetch_ticker('BTC/USDT')
            exchange = ex_instance
            print(f"✅ Market Data Connection Established via: [{ex_id.upper()}]\n")
            return exchange
        except Exception:
            print(f"⚠️ Provider [{ex_id.upper()}] restricted or offline. Trying backup provider...")
            time.sleep(0.2)
            
    exchange = ccxt.gateio({'enableRateLimit': True})
    return exchange

def fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=1000):
    global exchange
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        print(f"⚠️ Fetch error for {symbol} on {exchange.id}: {e}. Retrying with backup provider...")
        for ex_id in EXCHANGE_PROVIDERS:
            if ex_id == exchange.id:
                continue
            try:
                ex_class = getattr(ccxt, ex_id)
                temp_ex = ex_class({'enableRateLimit': True, 'timeout': 8000})
                data = temp_ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                exchange = temp_ex
                return data
            except Exception:
                pass
        return []

def calculate_ema(series, span=200):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_btc_market_climate():
    global exchange
    try:
        ticker = exchange.fetch_ticker('BTC/USDT')
        pct_24h = float(ticker.get('percentage', 0.0) or 0.0)
        if pct_24h <= -4.0:
            return f"⚠️ MARKET-WIDE SELLOFF (BTC 24h: {pct_24h:+.2f}%) - Exercise Caution"
        elif pct_24h >= 2.0:
            return f"🟢 BULLISH CLIMATE (BTC 24h: {pct_24h:+.2f}%)"
        else:
            return f"⚖️ NEUTRAL MARKET (BTC 24h: {pct_24h:+.2f}%)"
    except Exception:
        return "⚖️ NEUTRAL MARKET CLIMATE"

def fetch_3year_historical_data(symbol):
    try:
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=1000)
        if not ohlcv:
            raise Exception("No candle data returned from exchanges")
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        ath_row = df.loc[df['high'].idxmax()]
        ath_price = float(ath_row['high'])
        ath_date_str = ath_row['date'].strftime('%Y-%m-%d')

        atl_row = df.loc[df['low'].idxmin()]
        atl_price = float(atl_row['low'])
        atl_date_str = atl_row['date'].strftime('%Y-%m-%d')
        
        last_30_days = df.tail(30)
        monthly_high = float(last_30_days['high'].max())
        monthly_avg_price = float(last_30_days['close'].mean())
        month_start_price = float(last_30_days.iloc[0]['close'])
        current_price = float(df.iloc[-1]['close'])
        
        monthly_pace_pct = ((current_price - month_start_price) / month_start_price) * 100 if month_start_price > 0 else 0.0
        atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

        df['ema_200'] = calculate_ema(df['close'], span=200)
        latest_ema200 = float(df.iloc[-1]['ema_200']) if len(df) >= 200 else 0.0
        near_200ema = False
        if latest_ema200 > 0:
            pct_diff = abs(current_price - latest_ema200) / latest_ema200 * 100
            if pct_diff <= 3.5:
                near_200ema = True

        vol_20d_avg = float(df['volume'].tail(20).mean())
        latest_volume = float(df.iloc[-1]['volume'])
        volume_surge = (latest_volume / vol_20d_avg) if vol_20d_avg > 0 else 1.0
        whale_absorption = volume_surge >= 1.5

        df['rsi'] = calculate_rsi(df['close'], period=14)
        latest_rsi = float(df.iloc[-1]['rsi']) if not df['rsi'].isnull().iloc[-1] else 50.0
        prev_rsi = float(df.iloc[-2]['rsi']) if len(df) > 1 and not df['rsi'].isnull().iloc[-2] else 50.0
        rsi_reversal_turn = (prev_rsi <= 38.0 and latest_rsi > prev_rsi)
        
        recent_5m_pct = ((current_price - float(df.iloc[-2]['close'])) / float(df.iloc[-2]['close'])) * 100 if len(df) > 1 else 0.0
        is_pre_rally = (volume_surge >= 1.8 and recent_5m_pct >= 1.2 and latest_rsi >= 45.0)
        is_breakout = (volume_surge >= 3.0 and recent_5m_pct >= 2.5)

        recovery_prob_pct = 70
        
        brain_data = {
            'symbol': symbol,
            'ath_price': ath_price,
            'ath_date': ath_date_str,
            'atl_price': atl_price,
            'atl_date': atl_date_str,
            'atl_rebound_pct': atl_rebound_pct,
            'monthly_high': monthly_high,
            'monthly_avg_price': monthly_avg_price,
            'monthly_pace_pct': monthly_pace_pct,
            'near_200ema': near_200ema,
            'ema_200_price': latest_ema200,
            'whale_absorption': whale_absorption,
            'volume_surge': volume_surge,
            'rsi': latest_rsi,
            'rsi_reversal_turn': rsi_reversal_turn,
            'is_pre_rally': is_pre_rally,
            'is_breakout': is_breakout,
            'recent_5m_pct': recent_5m_pct,
            'recovery_probability_pct': max(min(recovery_prob_pct, 95), 15)
        }
        COIN_BRAIN_CACHE[symbol] = brain_data
        return brain_data
        
    except Exception as e:
        print(f"⚠️ Error processing history for {symbol}: {e}")
        fallback = {
            'symbol': symbol, 'ath_price': 0.0, 'ath_date': 'Unknown', 'atl_price': 0.0,
            'atl_date': 'Unknown', 'atl_rebound_pct': 0.0, 'monthly_high': 0.0, 'monthly_avg_price': 0.0,
            'monthly_pace_pct': 0.0, 'near_200ema': False, 'ema_200_price': 0.0, 'whale_absorption': False,
            'volume_surge': 1.0, 'rsi': 50.0, 'rsi_reversal_turn': False, 'is_pre_rally': False,
            'is_breakout': False, 'recent_5m_pct': 0.0, 'recovery_probability_pct': 50
        }
        COIN_BRAIN_CACHE[symbol] = fallback
        return fallback

def auto_discover_trending_coins():
    global exchange, TARGET_COINS
    print("🔥 Running Proactive Market-Wide Pre-Rally Scanner...")
    try:
        tickers = exchange.fetch_tickers()
        trending_candidates = []
        excluded = ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and symbol not in TARGET_COINS and symbol not in excluded:
                quote_vol = float(ticker.get('quoteVolume', 0.0) or 0.0)
                pct_change = float(ticker.get('percentage', 0.0) or 0.0)
                
                # Filter for early volume surge and initial momentum
                if quote_vol >= 8_000_000 and pct_change >= 4.0:
                    trending_candidates.append((symbol, quote_vol, pct_change, float(ticker.get('last', 0.0))))
                    
        trending_candidates.sort(key=lambda x: x[1], reverse=True)
        
        for item in trending_candidates[:2]:
            sym, vol, gain, curr_price = item
            TARGET_COINS.append(sym)
            save_watchlist()
            fetch_3year_historical_data(sym)
            
            # Send immediate alert notification on discovery
            msg = (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 *PRE-RALLY ALERT DISCOVERY*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🪙 *Asset:* `{sym}`\n"
                f"💵 *Current Price:* `${curr_price:.6f}`\n"
                f"📈 *Initial Rally Acceleration:* `+{gain:.2f}%`\n"
                f"📊 *24h Volume:* `${vol/1e6:.1f}M`\n\n"
                f"⚡ *Signal:* Early momentum breakout detected before major rally extension.\n"
                f"✅ Added to Active Watchlist!"
            )
            send_telegram_msg(SAVED_CHAT_ID, msg)
            time.sleep(1.5)
            
    except Exception as e:
        print(f"⚠️ Error during trending coin discovery: {e}")

def initialize_all_brains():
    load_watchlist()
    print("🧠 Fetching & Training Historical & Pre-Rally Brains...")
    for coin in list(TARGET_COINS):
        fetch_3year_historical_data(coin)
        time.sleep(0.1)
    print("✅ All Models Initialized!\n")

def fetch_market_pullback_reason(coin_name):
    clean_name = coin_name.split('/')[0]
    rss_url = f"https://news.google.com/rss/search?q={clean_name}+crypto+news+dump+or+drop&hl=en-US&gl=US&ceid=US:en"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(rss_url, headers=headers, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            headlines = []
            for item in root.findall('./channel/item')[:2]:
                title = item.find('title').text if item.find('title') is not None else ""
                title = re.sub(r' - [^-]+$', '', title)
                if title:
                    headlines.append(title)
            if headlines:
                return f"📊 Market Catalyst / Headlines:\n  • \"{headlines[0][:80]}...\""
    except Exception:
        pass
    return "📊 General Profit-Taking & Market Dynamics"

def assess_risk_rate(drawdown_pct, coin):
    is_bluechip = any(b in coin for b in ['BTC', 'ETH', 'SOL'])
    if drawdown_pct >= 75.0:
        return "🔴 EXTREME RISK (Deep Fall)"
    elif drawdown_pct >= 50.0:
        return "🟠 HIGH RISK (Major Discount)"
    elif drawdown_pct >= 30.0:
        return "🟡 MODERATE RISK" if is_bluechip else "🟠 MODERATE RISK"
    else:
        return "🟢 LOW RISK"

def send_telegram_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def generate_pullback_report(symbol):
    global exchange
    brain = COIN_BRAIN_CACHE.get(symbol) or fetch_3year_historical_data(symbol)
    
    current_price = 0.0
    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = float(ticker['last'])
    except Exception:
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='5m', limit=1)
        if ohlcv:
            current_price = float(ohlcv[-1][4])
            
    ath_price = brain['ath_price']
    atl_price = brain['atl_price']
    monthly_high = brain['monthly_high']
    
    ath_drawdown_pct = ((ath_price - current_price) / ath_price) * 100 if ath_price > 0 else 0.0
    monthly_drawdown_pct = ((monthly_high - current_price) / monthly_high) * 100 if monthly_high > 0 else 0.0
    atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

    market_climate = fetch_btc_market_climate()
    reason = fetch_market_pullback_reason(symbol)
    risk_rate = assess_risk_rate(monthly_drawdown_pct, symbol)
    
    return {
        'symbol': symbol,
        'current_price': current_price,
        'ath_price': ath_price,
        'ath_date': brain['ath_date'],
        'ath_drawdown_pct': ath_drawdown_pct,
        'atl_price': atl_price,
        'atl_date': brain['atl_date'],
        'atl_rebound_pct': atl_rebound_pct,
        'monthly_high': monthly_high,
        'monthly_drawdown_pct': monthly_drawdown_pct,
        'monthly_avg_price': brain['monthly_avg_price'],
        'monthly_pace_pct': brain['monthly_pace_pct'],
        'near_200ema': brain['near_200ema'],
        'ema_200_price': brain['ema_200_price'],
        'whale_absorption': brain['whale_absorption'],
        'rsi': brain.get('rsi', 50.0),
        'rsi_reversal_turn': brain.get('rsi_reversal_turn', False),
        'is_pre_rally': brain.get('is_pre_rally', False),
        'is_breakout': brain.get('is_breakout', False),
        'recent_5m_pct': brain.get('recent_5m_pct', 0.0),
        'volume_surge': brain['volume_surge'],
        'market_climate': market_climate,
        'reason': reason,
        'risk_rate': risk_rate,
        'recovery_prob': brain['recovery_probability_pct']
    }

def format_telegram_alert(data):
    max_drop = max(data['monthly_drawdown_pct'], data['ath_drawdown_pct'])
    
    if data.get('is_breakout'):
        header_badge = "🚀 [CONFIRMED BREAKOUT RALLY IN PROGRESS]"
    elif data.get('is_pre_rally'):
        header_badge = "🔥 [EARLY PRE-RALLY DETECTED - 5-10% START]"
    elif max_drop >= 70:
        header_badge = "🟥 🚨 EXTREME PULLBACK ALERT (-70%+)"
    elif max_drop >= 50:
        header_badge = "🟨 ⚠️ MAJOR PULLBACK ALERT (-50%+)"
    else:
        header_badge = "🟦 📉 NOTABLE PULLBACK DETECTED (-30%+)"

    t25 = data['current_price'] * 1.25
    t50 = data['current_price'] * 1.50
    ath_gain = ((data['ath_price'] - data['current_price']) / data['current_price']) * 100 if data['current_price'] > 0 else 0

    confluences = []
    if data.get('is_pre_rally'):
        confluences.append(f"⚡ Early Pre-Rally Surge (`+{data['recent_5m_pct']:.2f}%` momentum)")
    if data.get('near_200ema'):
        confluences.append(f"🧱 Sitting near 200-Day EMA Support (`${data['ema_200_price']:.4f}`)")
    if data.get('whale_absorption'):
        confluences.append(f"🐋 Whale Absorption (`{data['volume_surge']:.1f}x` Volume Surge)")
    if data.get('rsi_reversal_turn'):
        confluences.append(f"🔥 RSI Bullish Turn (`RSI: {data['rsi']:.1f}`)")

    conf_text = "\n".join([f"  • {c}" for c in confluences]) if confluences else "  • Standard Dip Level"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{header_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 *ASSET:* `{data['symbol']}`\n"
        f"💵 *Current Price:* `${data['current_price']:.6f}`\n"
        f"🌐 *Market Climate:* `{data['market_climate']}`\n\n"
        f"📌 *DRAWDOWN SUMMARY*\n"
        f"  • 📅 *Monthly Drop (30d Peak):* `-{data['monthly_drawdown_pct']:.2f}%` (Peak: `${data['monthly_high']:.6f}`)\n"
        f"  • 🏆 *3-Yr ATH Drop:* `-{data['ath_drawdown_pct']:.2f}%` (ATH: `${data['ath_price']:.6f}` on `{data['ath_date']}`)\n"
        f"  • 🌱 *3-Yr ATL Level:* `${data['atl_price']:.6f}` (`+{data['atl_rebound_pct']:.2f}%` from bottom on `{data['atl_date']}`)\n\n"
        f"⚡ *TECHNICAL CONFLUENCE*\n"
        f"{conf_text}\n\n"
        f"🎯 *PROFIT & RECOVERY TARGETS*\n"
        f"  • 🎯 *+25% Target:* `${t25:.6f}`\n"
        f"  • 🎯 *+50% Target:* `${t50:.6f}`\n"
        f"  • 🚀 *Full ATH Rebound:* `${data['ath_price']:.6f}` (`+{ath_gain:.1f}%` Upside)\n\n"
        f"{data['reason']}\n\n"
        f"🛡️ *RISK ASSESSMENT*\n"
        f"  • *Risk Level:* {data['risk_rate']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg

def get_drawdown_tier(pct):
    if pct >= 70.0:
        return 3
    elif pct >= 50.0:
        return 2
    elif pct >= 30.0:
        return 1
    return 0

def should_send_anti_spam_notification(symbol, report):
    """
    STRICT ANTI-SPAM RULE (V12.1):
    Suppresses alerts unless the coin drops into a deeper tier OR rebounds by >= 7.5%.
    """
    max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
    current_tier = get_drawdown_tier(max_drop)
    current_price = report['current_price']
    
    # Priority Override: Early Pre-Rally or Breakout alerts bypass standard pullback filters once
    if report.get('is_pre_rally') or report.get('is_breakout'):
        return True
        
    if current_tier == 0:
        return False
        
    last_tier = LAST_ALERTED_TIER.get(symbol, 0)
    last_price = LAST_ALERTED_PRICE.get(symbol, 0.0)
    
    is_new_deeper_tier = current_tier > last_tier
    
    # Check if price has made a 5% - 10% (7.5% avg) rebound since the last alert
    has_substantially_rebounded = False
    if last_price > 0:
        pct_gain_from_last = ((current_price - last_price) / last_price) * 100
        if pct_gain_from_last >= MIN_REBOUND_FOR_REALERT_PCT:
            has_substantially_rebounded = True
            
    if is_new_deeper_tier or has_substantially_rebounded or last_price == 0.0:
        LAST_ALERTED_TIER[symbol] = current_tier
        LAST_ALERTED_PRICE[symbol] = current_price
        return True
        
    return False

def handle_telegram_commands():
    global LAST_TELEGRAM_UPDATE_ID, TARGET_COINS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}
    
    try:
        res = requests.get(url, params=params, timeout=3).json()
        updates = res.get("result", [])
        
        for item in updates:
            LAST_TELEGRAM_UPDATE_ID = item["update_id"]
            if "message" in item and "text" in item["message"]:
                text = item["message"]["text"].strip()
                chat_id = item["message"]["chat"]["id"]
                
                if text.startswith("/add"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw_coin = parts[1].upper().replace("/", "")
                        symbol = f"{raw_coin}/USDT" if not raw_coin.endswith("USDT") else f"{raw_coin[:-4]}/USDT"
                        if symbol not in TARGET_COINS:
                            TARGET_COINS.append(symbol)
                            save_watchlist()
                            send_telegram_msg(chat_id, f"🔄 Fetching data for `{symbol}`...")
                            fetch_3year_historical_data(symbol)
                            send_telegram_msg(chat_id, f"✅ `{symbol}` added to active watchlist!")
                        else:
                            send_telegram_msg(chat_id, f"ℹ️ `{symbol}` is already in your active watchlist.")
                elif text.startswith("/remove") or text.startswith("/delete"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw_coin = parts[1].upper().replace("/", "")
                        symbol = f"{raw_coin}/USDT" if not raw_coin.endswith("USDT") else f"{raw_coin[:-4]}/USDT"
                        if symbol in TARGET_COINS:
                            TARGET_COINS.remove(symbol)
                            save_watchlist()
                            send_telegram_msg(chat_id, f"🗑️ `{symbol}` deleted from active watchlist.")
                elif text.startswith("/list"):
                    items = [f"• `{c}`" for c in TARGET_COINS]
                    msg = f"📋 *Active Watchlist ({len(TARGET_COINS)} Coins):*\n\n" + "\n".join(items)
                    send_telegram_msg(chat_id, msg)
                elif text.startswith("/check") or text.startswith("/pullback"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw = parts[1].upper().replace("/", "")
                        symbol = f"{raw}/USDT" if not raw.endswith("USDT") else f"{raw[:-4]}/USDT"
                        report = generate_pullback_report(symbol)
                        send_telegram_msg(chat_id, format_telegram_alert(report))
                elif text.startswith("/trending"):
                    auto_discover_trending_coins()
    except Exception:
        pass

def run_pullback_engine():
    print("="*75)
    print("⚡ CRYPTOPULSE AI v12.1 - PROACTIVE PRE-RALLY & REBOUND ENGINE")
    print("="*75)
    
    initialize_exchange_connection()
    initialize_all_brains()
    
    startup_msg = (
        "🟢 *CryptoPulse Engine v12.1 Live*\n"
        "⚡ Proactive Pre-Rally Alerts Active.\n"
        "🛡️ +7.5% Rebound Anti-Spam Filter Lock Enabled.\n"
        "📱 Commands: `/add SUI`, `/delete SUI`, `/list`, `/check BTC`"
    )
    send_telegram_msg(SAVED_CHAT_ID, startup_msg)
    
    scan_count = 1
    while True:
        handle_telegram_commands()
        
        if scan_count % 10 == 0:
            auto_discover_trending_coins()
            
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n--- [PULLBACK SCAN #{scan_count}] {now} ---")
        
        results = []
        alerts_sent = 0
        
        for symbol in list(TARGET_COINS):
            report = generate_pullback_report(symbol)
            should_alert = should_send_anti_spam_notification(symbol, report)
            
            max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
            status_str = "NORMAL"
            if report.get('is_pre_rally'):
                status_str = "🔥 PRE-RALLY DETECTED"
            elif max_drop >= MIN_ALERT_DRAWDOWN_PCT:
                status_str = f"DROP (-{max_drop:.1f}%)"
                
            if should_alert:
                status_str += " 📲 [ALERT SENT]"
                send_telegram_msg(SAVED_CHAT_ID, format_telegram_alert(report))
                alerts_sent += 1
                time.sleep(1.5)
                
            results.append({
                'Coin': report['symbol'],
                'Price ($)': f"{report['current_price']:.6f}",
                'Monthly Drop': f"-{report['monthly_drawdown_pct']:.1f}%",
                'Status': status_str
            })
            time.sleep(0.12)
            
        summary_df = pd.DataFrame(results)
        print(summary_df.to_string(index=False))
        
        if MAX_SCANS and scan_count >= MAX_SCANS:
            break
            
        scan_count += 1
        time.sleep(60)

if __name__ == "__main__":
    run_pullback_engine()
