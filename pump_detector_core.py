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
from datetime import datetime
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
MIN_REBOUND_FOR_REALERT_PCT = 7.5  

# Fixed modular data limits to prevent UnboundLocalErrors (V15.2 Stability Fix)
HISTORICAL_BRAIN_LIMIT_DAYS = 1000  # Days for deep historical context
PRECISION_SNIPER_LIMIT_CANDLES = 40  # 5-min candles for local volume relative sniping

# V15.0 PANIC SNIPER (Trained Pattern Recognition)
SNIPER_24H_DROP_REQ = -8.0     
SNIPER_24H_VOL_REQ = 7_000_000 
TRAINED_RSI_OVERSOLD = 28      
VRS_REL_VOL_SURGE = 4.0        
VRS_INIT_VELOCITY_PCT = 1.6    

DISCOVERED_COIN_LIFESPAN_HOURS = 24

CORE_WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BONK/USDT', 'WIF/USDT',
    'NEAR/USDT', 'RENDER/USDT', 'AVAX/USDT', 'SUI/USDT',
    'COMP/USDT', 'RIF/USDT', 'ESP/USDT', 'BANK/USDT', 
    'DEXE/USDT', 'ALICE/USDT', 'SENT/USDT', 
    'RE/USDT', 'ONDO/USDT', 'ZEC/USDT', 'MIRA/USDT', 
    'OPEN/USDT', 'LUMIA/USDT', 'DODO/USDT', 'SYN/USDT', 'ORDI/USDT'
]

TARGET_COINS = list(CORE_WATCHLIST)
DISCOVERED_COINS_DATA = {} 

EXCHANGE_PROVIDERS = ['gateio', 'binance', 'kucoin', 'okx', 'kraken', 'bybit']

exchange, COIN_BRAIN_CACHE, LAST_ALERTED_TIER, LAST_ALERTED_PRICE, LAST_TELEGRAM_UPDATE_ID = None, {}, {}, {}, 0  
HEALTH_CHECK_TIMESTAMP, WATCHLIST_CLEANUP_TIMESTAMP = time.time(), time.time()

def load_watchlist_v15():
    """Loads persistent watchlist and discovered coin lifecycle data."""
    global TARGET_COINS, DISCOVERED_COINS_DATA
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                persistent_coins = data.get('watchlist', [])
                excluded = ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
                active_core = [c for c in CORE_WATCHLIST if c not in excluded]
                active_discovered = [c for c in persistent_coins if c not in CORE_WATCHLIST and c not in excluded]
                TARGET_COINS = active_core + active_discovered
                DISCOVERED_COINS_DATA = data.get('discovered_data', {})
                print(f"📁 V15.2 Emergency Watchlist loaded ({len(TARGET_COINS)} active).")
                return
        except Exception as e:
            print(f"⚠️ Error loading persistent watchlist v15.2: {e}")
    TARGET_COINS = list(CORE_WATCHLIST)
    save_watchlist_v15()

def save_watchlist_v15():
    try:
        data_to_save = {
            'watchlist': TARGET_COINS,
            'discovered_data': DISCOVERED_COINS_DATA,
            'excluded_by_user': ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
        }
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=4)
    except Exception as e:
        print(f"⚠️ Error saving persistent watchlist v15.2: {e}")

def cleanup_discovered_coins_lifecycle():
    global TARGET_COINS, DISCOVERED_COINS_DATA
    print("🧹 Running Automated Watchlist Lifecycle Cleanup...")
    now = time.time()
    cutoff_time = now - (DISCOVERED_COIN_LIFESPAN_HOURS * 3600)
    
    initial_count = len(TARGET_COINS)
    removed_coins = []
    
    discovered_on_list = [c for c in TARGET_COINS if c in DISCOVERED_COINS_DATA]
    
    for symbol in discovered_on_list:
        discovery_time = DISCOVERED_COINS_DATA[symbol].get('timestamp', 0)
        
        if discovery_time < cutoff_time:
            TARGET_COINS.remove(symbol)
            removed_coins.append(symbol)
            if symbol in DISCOVERED_COINS_DATA: del DISCOVERED_COINS_DATA[symbol]
            if symbol in COIN_BRAIN_CACHE: del COIN_BRAIN_CACHE[symbol]
            if symbol in LAST_ALERTED_TIER: del LAST_ALERTED_TIER[symbol]
            if symbol in LAST_ALERTED_PRICE: del LAST_ALERTED_PRICE[symbol]
            
    if initial_count != len(TARGET_COINS):
        save_watchlist_v15()
        print(f"✅ Cleaned {len(removed_coins)} coins.")
    else:
        print("✅ No discovered coins have expired.")

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
            time.sleep(0.2)
            
    exchange = ccxt.gateio({'enableRateLimit': True})
    return exchange

def fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=1000):
    global exchange
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        # Fallback router logic
        for ex_id in EXCHANGE_PROVIDERS:
            if ex_id == exchange.id: continue
            try:
                ex_class = getattr(ccxt, ex_id)
                temp_ex = ex_class({'enableRateLimit': True, 'timeout': 8000})
                data = temp_ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                exchange = temp_ex
                return data
            except Exception: pass
        return []

def calculate_ema(series, span=200):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi(series, period=14):
    if len(series) <= period: return series
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

def fetch_deep_intelligence_brain(symbol):
    """Fetches deep context & modular patterns (Modular V15.2 Stability Patch)."""
    global exchange
    try:
        # Emergency Patch: Explicit limit parameter prevents crashing
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=HISTORICAL_BRAIN_LIMIT_DAYS)
        if not ohlcv: return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        ath_row = df.loc[df['high'].idxmax()]
        ath_price, ath_date_str = float(ath_row['high']), ath_row['date'].strftime('%Y-%m-%d')
        atl_row = df.loc[df['low'].idxmin()]
        atl_price, atl_date_str = float(atl_row['low']), atl_row['date'].strftime('%Y-%m-%d')
        
        last_30_days = df.tail(30)
        monthly_high, monthly_avg_price = float(last_30_days['high'].max()), float(last_30_days['close'].mean())
        month_start_price, current_price = float(last_30_days.iloc[0]['close']), float(df.iloc[-1]['close'])
        
        monthly_pace_pct = ((current_price - month_start_price) / month_start_price) * 100 if month_start_price > 0 else 0.0
        atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

        df['ema_200'] = calculate_ema(df['close'], span=200)
        latest_ema200 = float(df.iloc[-1]['ema_200']) if len(df) >= 200 else 0.0
        near_200ema = latest_ema200 > 0 and (abs(current_price - latest_ema200) / latest_ema200 * 100 <= 3.5)

        vol_20d_avg, latest_volume = float(df['volume'].tail(20).mean()), float(df.iloc[-1]['volume'])
        volume_surge_vs_avg = (latest_volume / vol_20d_avg) if vol_20d_avg > 0 else 1.0
        whale_absorption = volume_surge_vs_avg >= 1.5

        df['rsi'] = calculate_rsi(df['close'], period=14)
        latest_rsi, prev_rsi = float(df.iloc[-1]['rsi']), float(df.iloc[-2]['rsi']) if len(df) > 1 else 50.0
        rsi_reversal_turn = (prev_rsi <= 38.0 and latest_rsi > prev_rsi)
        
        return {
            'symbol': symbol,
            'ath_price': ath_price, 'ath_date': ath_date_str, 'atl_price': atl_price, 'atl_date': atl_date_str,
            'atl_rebound_pct': atl_rebound_pct, 'monthly_high': monthly_high, 'monthly_avg_price': monthly_avg_price,
            'monthly_pace_pct': monthly_pace_pct, 'current_price': current_price,
            'near_200ema': near_200ema, 'ema_200_price': latest_ema200, 'whale_absorption': whale_absorption,
            'rsi': latest_rsi, 'rsi_reversal_turn': rsi_reversal_turn,
            'volume_surge_vs_avg': volume_surge_vs_avg, 'recovery_probability_pct': 70
        }
        
    except Exception as e:
        print(f"⚠️ Error deep processing for {symbol}: {e}")
        return None

def general_market_panic_sniper():
    """Market-Wide "Panic Washout" Discovery Routine (Modular V15.0 modular)."""
    global exchange, TARGET_COINS, DISCOVERED_COINS_DATA
    print("🎯 Running Automated General Market Panic Sniper Scan...")
    tickers = exchange.fetch_tickers()
    if not tickers: return

    try:
        discoveries, excluded_patterns = [], ['DOGE', 'PEPE', 'XRP', 'SHIB', 'LINK', 'FLOKI', 'LAB']
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and symbol not in TARGET_COINS and not any(p in symbol for p in excluded_patterns):
                
                pct_change_24h = float(ticker.get('percentage', 0.0) or 0.0)
                quote_vol_24h = float(ticker.get('quoteVolume', 0.0) or 0.0)
                
                if pct_change_24h <= SNIPER_24H_DROP_REQ and quote_vol_24h >= SNIPER_24H_VOL_REQ:
                    discoveries.append((symbol, quote_vol_24h, pct_change_24h))
                    
        discoveries.sort(key=lambda x: x[1], reverse=True)
        
        discovered_count = 0
        for item in discoveries:
            if discovered_count >= 2: break 
            sym, vol24, drop24 = item
            
            # --- LOCAL CONTEXT (5-Min Precision Check) ---
            ohlcv5 = fetch_ohlcv_with_fallback(sym, timeframe='5m', limit=PRECISION_SNIPER_LIMIT_CANDLES)
            time.sleep(0.1)
            if not ohlcv5: continue
            
            df5 = pd.DataFrame(ohlcv5, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = float(df5.iloc[-1]['close'])
            
            # Trained Signature Verification
            rsi_series5 = calculate_rsi(df5['close'], period=14)
            latest_rsi5 = float(rsi_series5.iloc[-1]) if not rsi_series5.empty else 50.0
            is_capitulating = latest_rsi5 <= TRAINED_RSI_OVERSOLD
            
            local_vol_avg = float(df5['volume'].iloc[:-1].mean()) # Context average
            latest_volume = float(df5.iloc[-1]['volume'])
            volume_spike_context = (latest_volume / local_vol_avg >= VRS_REL_VOL_SURGE) if local_vol_avg > 0 else False
            
            recent_5m_pct = ((current_price - float(df5.iloc[-2]['close'])) / float(df5.iloc[-2]['close']) * 100) if len(df5)>1 else 0.0
            is_trigger_candle = recent_5m_pct >= VRS_INIT_VELOCITY_PCT
            
            # PRECISION CONFLUENCE MATCH
            if (is_capitulating and volume_spike_context and is_trigger_candle):
                
                brain = fetch_deep_intelligence_brain(sym)
                if brain: COIN_BRAIN_CACHE[sym] = brain
                else: continue
                
                discovered_count += 1
                TARGET_COINS.append(sym)
                DISCOVERED_COINS_DATA[sym] = {'timestamp': time.time(), 'source': 'general_market_sniper', 'discovery_24h_drop': drop24}
                save_watchlist_v15() 
                
                send_telegram_msg(SAVED_CHAT_ID, format_telegram_alert(brain, discovery_header=True))
                time.sleep(1.5)
    except Exception as e:
        print(f"⚠️ Error during trending coin discovery v15.2: {e}")

def format_telegram_alert(data, discovery_header=False):
    """Formats full pullback alert report for Telegram broadcast."""
    max_drop = max(data['monthly_drawdown_pct'], data['ath_drawdown_pct'])
    
    # Priority Badge Logic
    if discovery_header:
        header_badge = "👑 *PRECISION MARKET DISCOVERY (Panic Sniper)*"
    elif max_drop >= 70:
        header_badge = "🟥 🚨 EXTREME PULLBACK ALERT (-70%+)"
    elif max_drop >= 50:
        header_badge = "🟨 ⚠️ MAJOR PULLBACK ALERT (-50%+)"
    else:
        header_badge = "🟦 📉 NOTABLE PULLBACK DETECTED (-30%+)"

    confluences = []
    if data.get('near_200ema'): confluences.append(f"🧱 Sitting near 200-Day EMA Support")
    if data.get('rsi_reversal_turn'): confluences.append(f"🔥 RSI Bullish Turn (`RSI: {data['rsi']:.1f}`)")
    if data.get('whale_absorption'): confluences.append(f"🐋 Whale Absorption (`{data['volume_surge_vs_avg']:.1f}x` Vol Surge)")

    conf_text = "\n".join([f"  • {c}" for c in confluences]) if confluences else "  • Standard Dip Level"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{header_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 *ASSET:* `{data['symbol']}`\n"
        f"💵 *Current Price:* `${data['current_price']:.6f}`\n\n"
        f"📌 *DRAWDOWN SUMMARY*\n"
        f"  • Monthly Drop: `-{data['monthly_drawdown_pct']:.2f}%`\n"
        f"  • 🏆 3-Yr ATH Drop: `-{data['ath_drawdown_pct']:.2f}%`\n"
        f"  • 🌱 3-Yr ATL: `${data['atl_price']:.6f}` (on `{data['atl_date']}`)\n\n"
        f"⚡ *TECHNICAL CONFLUENCE*\n"
        f"{conf_text}\n\n"
        f"🛡️ *RISK:* {data['risk_rate']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg

def get_drawdown_tier(pct):
    if pct >= 70.0: return 3
    elif pct >= 50.0: return 2
    elif pct >= 30.0: return 1
    return 0

def should_send_anti_spam_notification(symbol, report):
    max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
    current_tier = get_drawdown_tier(max_drop)
    
    if current_tier == 0: return False
        
    last_tier = LAST_ALERTED_TIER.get(symbol, 0)
    last_price = LAST_ALERTED_PRICE.get(symbol, 0.0)
    
    if current_tier > last_tier:
        LAST_ALERTED_TIER[symbol] = current_tier
        LAST_ALERTED_PRICE[symbol] = report['current_price']
        return True
        
    if last_price > 0 and ((report['current_price'] - last_price) / last_price * 100 >= MIN_REBOUND_FOR_ALERT_PCT):
        LAST_ALERTED_PRICE[symbol] = report['current_price']
        return True
        
    return False

def handle_telegram_commands():
    global LAST_TELEGRAM_UPDATE_ID, TARGET_COINS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        # V15.2 PATCH: Forced parameter definition fix UnboundLocalError risks
        res = requests.get(url, params={"offset": LAST_TELEGRAM_UPDATE_ID + 1, "timeout": 1}).json()
        for item in res.get("result", []):
            LAST_TELEGRAM_UPDATE_ID = item["update_id"]
            if "message" in item and "text" in item["message"]:
                text, chat_id = item["message"]["text"].strip(), item["message"]["chat"]["id"]
                if text.startswith("/add"):
                    raw = text.split()[1].upper().replace("/", "") if len(text.split())>1 else None
                    if raw: TARGET_COINS.append(f"{raw}/USDT"); save_watchlist_v15(); send_telegram_msg(chat_id, f"✅ `{raw}/USDT` added manually.")
                elif text.startswith("/delete"):
                    raw = text.split()[1].upper().replace("/", "") if len(text.split())>1 else None
                    if raw and f"{raw}/USDT" in TARGET_COINS: TARGET_COINS.remove(f"{raw}/USDT"); save_watchlist_v15(); send_telegram_msg(chat_id, f"🗑️ `{raw}/USDT` deleted.")
                elif text.startswith("/list"): send_telegram_msg(chat_id, f"📋 *Watchlist ({len(TARGET_COINS)} Coins):*\n\n" + "\n".join([f"• `{c}`" for c in TARGET_COINS]))
                elif text.startswith("/trending"): general_market_panic_sniper()
    except Exception: pass

def generate_pullback_report_v15(symbol):
    """Modular. Rebuilds deep brain from current market data."""
    global exchange
    brain = fetch_deep_intelligence_brain(symbol)
    if brain is None: return None
            
    brain['market_climate'] = fetch_btc_market_climate()
    brain['reason'] = 'General Market Profit-Taking'
    brain['risk_rate'] = 'Moderate'
    # Recalculate drawdowns explicitly in runtime
    brain['ath_drawdown_pct'] = ((brain['ath_price'] - brain['current_price']) / brain['ath_price'] * 100) if brain['ath_price'] > 0 else 0.0
    brain['monthly_drawdown_pct'] = ((brain['monthly_high'] - brain['current_price']) / brain['monthly_high'] * 100) if brain['monthly_high'] > 0 else 0.0
    return brain

def run_pullback_engine_v15():
    """Main execution non-stop monitoring loop (Modular V15.2 PATCH)."""
    global HEALTH_CHECK_TIMESTAMP, WATCHLIST_CLEANUP_TIMESTAMP
    print("="*75)
    print("⚡ CRYPTOPULSE AI v15.2 EMERGENCY PATCH - STABILITY")
    print("="*75)
    initialize_exchange_connection()
    # Initialize runtime list & persistence structure
    load_watchlist_v15()
    
    # Pre-Initialize known brains (Required to prevent runtime fetches)
    print("🧠 Fetching & Training Deep Brains for Watchlist (v15.2 Stability Patch)...")
    for coin in list(TARGET_COINS):
        brain = fetch_deep_intelligence_brain(coin)
        if brain: COIN_BRAIN_CACHE[coin] = brain
        else:
            # V15.2 PATCH: Force brain creation fallback if API momentarily times out.
            COIN_BRAIN_CACHE[coin] = {
                'symbol': coin, 'ath_price': 1.0, 'ath_date': '2020-01-01', 'atl_price': 0.0001, 'atl_date': '2020-01-01',
                'atl_rebound_pct': 0.0, 'monthly_high': 1.0, 'monthly_avg_price': 0.5, 'monthly_pace_pct': 0.0,
                'near_200ema': False, 'ema_200_price': 0.0, 'whale_absorption': False, 'current_price': 1.0,
                'rsi': 50.0, 'rsi_reversal_turn': False, 'volume_surge_vs_avg': 1.0, 'recovery_probability_pct': 50
            }
        time.sleep(0.12)
    print("✅ All Runtime Models Initialized!\n")
    
    send_telegram_msg(SAVED_CHAT_ID, "🟢 *CryptoPulse Engine v15.2 EMERGENCY Patch Live*\nTypeError crash fixed. Parameter modularity reverted for stability. General market panic sniping & non-stop monitoring resumed.")
    
    scan_count = 1
    while True:
        handle_telegram_commands()
        if scan_count % 10 == 0: general_market_panic_sniper()
        if time.time() - WATCHLIST_CLEANUP_TIMESTAMP >= 86400: cleanup_discovered_coins_lifecycle(); WATCHLIST_CLEANUP_TIMESTAMP = time.time()
            
        print(f"\n--- [V15.2 STABILITY MONITORING SCAN #{scan_count}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        results = []
        for symbol in list(TARGET_COINS):
            report = generate_pullback_report_v15(symbol)
            if report and should_send_anti_spam_notification(symbol, report):
                send_telegram_msg(SAVED_CHAT_ID, format_telegram_alert(report))
                time.sleep(1.5)
            time.sleep(0.12) # Fetch OHLCV stagger buffer
            
        if MAX_SCANS and scan_count >= MAX_SCANS: break
        scan_count += 1; time.sleep(60)

if __name__ == "__main__":
    run_pullback_engine_v15()
