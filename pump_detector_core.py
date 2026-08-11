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
MIN_REBOUND_FOR_REALERT_PCT = 7.5  

# V15.0 PANIC SNIPER & TRAINED RALLY PATTERN (Trained from provided images)
SNIPER_24H_DROP_REQ = -8.0     # Broad filter: Coin must be deeply red
SNIPER_24H_VOL_REQ = 7_000_000 # Broad filter: Coin must have active volume
TRAINED_RSI_OVERSOLD = 28       # Panic/capitulation precision threshold
VRS_REL_VOL_SURGE = 4.0          # How much higher local vol is vs local avg
VRS_INIT_VELOCITY_PCT = 1.6     # Instant price gain on entry candle capture

# Watchlist Lifecycle Settings
DISCOVERED_COIN_LIFESPAN_HOURS = 24

# Core Default Watchlist (Excludes DOGE, PEPE, XRP, SHIB, LINK, FLOKI, LAB)
# These are your requested coins that are NEVER auto-deleted.
CORE_WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BONK/USDT', 'WIF/USDT',
    'NEAR/USDT', 'RENDER/USDT', 'AVAX/USDT', 'SUI/USDT',
    'COMP/USDT', 'RIF/USDT', 'ESP/USDT', 'BANK/USDT', 
    'DEXE/USDT', 'ALICE/USDT', 'SENT/USDT', 
    'RE/USDT', 'ONDO/USDT', 'ZEC/USDT', 'MIRA/USDT', 
    'OPEN/USDT', 'LUMIA/USDT', 'DODO/USDT', 'SYN/USDT', 'ORDI/USDT'
]

# TARGET_COINS is the dynamic runtime watchlist (Core + Discovered)
TARGET_COINS = list(CORE_WATCHLIST)
DISCOVERED_COINS_DATA = {} # Tracking discovered coin timestamps

EXCHANGE_PROVIDERS = ['gateio', 'binance', 'kucoin', 'okx', 'kraken', 'bybit']

# Global State Management
exchange = None              
COIN_BRAIN_CACHE = {}        
LAST_ALERTED_TIER = {}       
LAST_ALERTED_PRICE = {}      
LAST_TELEGRAM_UPDATE_ID = 0  
HEALTH_CHECK_TIMESTAMP = time.time()
WATCHLIST_CLEANUP_TIMESTAMP = time.time()

def load_watchlist_v15():
    """Loads persistent watchlist and discovered coin lifecycle data."""
    global TARGET_COINS, DISCOVERED_COINS_DATA
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                # Parse Core List
                persistent_coins = data.get('watchlist', [])
                excluded = ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
                # Ensure Core coins are present and removed coins are absent
                active_core = [c for c in CORE_WATCHLIST if c not in excluded]
                active_discovered = [c for c in persistent_coins if c not in CORE_WATCHLIST and c not in excluded]
                TARGET_COINS = active_core + active_discovered
                
                # Parse Discovered Data Lifecycle
                DISCOVERED_COINS_DATA = data.get('discovered_data', {})
                print(f"📁 V15 Watchlist loaded ({len(TARGET_COINS)} active, {len(active_discovered)} discovered).")
                return
        except Exception as e:
            print(f"⚠️ Error loading persistent watchlist v15: {e}")
    # Fallback to defaults
    TARGET_COINS = list(CORE_WATCHLIST)
    save_watchlist_v15()

def save_watchlist_v15():
    """Saves active watchlist and discovery metadata to file."""
    try:
        data_to_save = {
            'watchlist': TARGET_COINS,
            'discovered_data': DISCOVERED_COINS_DATA,
            'excluded_by_user': ['DOGE/USDT', 'PEPE/USDT', 'XRP/USDT', 'SHIB/USDT', 'LINK/USDT', 'FLOKI/USDT', 'LAB/USDT']
        }
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=4)
    except Exception as e:
        print(f"⚠️ Error saving persistent watchlist v15: {e}")

def cleanup_discovered_coins_lifecycle():
    """Automatically removes expired discovered coins from the watchlist."""
    global TARGET_COINS, DISCOVERED_COINS_DATA
    print("🧹 Running Automated Watchlist Lifecycle Cleanup...")
    now = time.time()
    cutoff_time = now - (DISCOVERED_COIN_LIFESPAN_HOURS * 3600)
    
    initial_count = len(TARGET_COINS)
    removed_coins = []
    
    # We only clean TARGET_COINS that are in DISCOVERED_COINS_DATA
    discovered_on_list = [c for c in TARGET_COINS if c in DISCOVERED_COINS_DATA]
    
    for symbol in discovered_on_list:
        discovery_time = DISCOVERED_COINS_DATA[symbol].get('timestamp', 0)
        # 1. Has the lifespan expired?
        lifespan_expired = discovery_time < cutoff_time
        # 2. Is the coin currently quiet? (Has it not alerted recently?)
        # last_price = LAST_ALERTED_PRICE.get(symbol, 0.0)
        # last_tier = LAST_ALERTED_TIER.get(symbol, 0)
        # is_quiet = (last_tier <= 1 and last_price == 0.0) # Tier 1 is just -30%
        
        if lifespan_expired:
            TARGET_COINS.remove(symbol)
            removed_coins.append(symbol)
            if symbol in DISCOVERED_COINS_DATA: del DISCOVERED_COINS_DATA[symbol]
            if symbol in COIN_BRAIN_CACHE: del COIN_BRAIN_CACHE[symbol]
            if symbol in LAST_ALERTED_TIER: del LAST_ALERTED_TIER[symbol]
            if symbol in LAST_ALERTED_PRICE: del LAST_ALERTED_PRICE[symbol]
            
    final_count = len(TARGET_COINS)
    if initial_count != final_count:
        save_watchlist_v15()
        removed_str = ", ".join(removed_coins[:3]) + ("..." if len(removed_coins)>3 else "")
        msg = f"🧹 *Active Lifecycle Management:* Removed `{initial_count - final_count}` expired discovery assets ({removed_str}) to maintain peak engine velocity."
        send_telegram_msg(SAVED_CHAT_ID, msg)
        print(f"✅ Cleaned {len(removed_coins)} coins. New watchlist size: {final_count}")
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

def fetch_deep_intelligence_brain(symbol):
    """
    Fetches deep historical context & multi-timeframetrained patterns.
    (This is the main 'Brain' logic from previous versions, now modularized.)
    """
    try:
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=1000)
        if not ohlcv: return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        ath_row = df.loc[df['high'].idxmax()]
        ath_price, ath_date_str = float(ath_row['high']), ath_row['date'].strftime('%Y-%m-%d')
        atl_row = df.loc[df['low'].idxmin()]
        atl_price, atl_date_str = float(atl_row['low']), atl_row['date'].strftime('%Y-%m-%d')
        
        last_30_days = df.tail(30)
        monthly_high = float(last_30_days['high'].max())
        monthly_avg_price = float(last_30_days['close'].mean())
        month_start_price = float(last_30_days.iloc[0]['close'])
        current_price = float(df.iloc[-1]['close'])
        
        monthly_pace_pct = ((current_price - month_start_price) / month_start_price) * 100 if month_start_price > 0 else 0.0
        atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

        df['ema_200'] = calculate_ema(df['close'], span=200)
        latest_ema200 = float(df.iloc[-1]['ema_200']) if len(df) >= 200 else 0.0
        near_200ema = latest_ema200 > 0 and (abs(current_price - latest_ema200) / latest_ema200 * 100 <= 3.5)

        vol_20d_avg = float(df['volume'].tail(20).mean())
        latest_volume = float(df.iloc[-1]['volume'])
        volume_surge_vs_avg = (latest_volume / vol_20d_avg) if vol_20d_avg > 0 else 1.0
        whale_absorption = volume_surge_vs_avg >= 1.5

        df['rsi'] = calculate_rsi(df['close'], period=14)
        latest_rsi = float(df.iloc[-1]['rsi']) if not df['rsi'].isnull().iloc[-1] else 50.0
        prev_rsi = float(df.iloc[-2]['rsi']) if len(df) > 1 and not df['rsi'].isnull().iloc[-2] else 50.0
        rsi_reversal_turn = (prev_rsi <= 38.0 and latest_rsi > prev_rsi)
        
        # --- VRS SNIPER (TRAINED FOOTPRINT MATCHING) ---
        # 1. RSI Capitulation
        is_capitulating = latest_rsi <= TRAINED_RSI_OVERSOLD
        # 2. Local Volume Surge vs Context (15 Candles)
        local_vol_series = df['volume'].tail(15)
        local_vol_avg = float(local_vol_series.iloc[:-1].mean()) # Average of previous 14 candles
        volume_surge_relative = (latest_volume / local_vol_avg) if local_vol_avg > 0 else 1.0
        volume_spike_context = volume_surge_relative >= VRS_REL_VOL_SURGE
        
        # 3. Trigger Candle Velocity (Caught 1.6% - 4.0% early into pump)
        recent_5m_pct = ((current_price - float(df.iloc[-2]['close'])) / float(df.iloc[-2]['close'])) * 100 if len(df) > 1 else 0.0
        is_trigger_candle = recent_5m_pct >= VRS_INIT_VELOCITY_PCT
        
        # Confluence of the Trained Signature
        is_trained_pattern_match = (is_capitulating and volume_spike_context and is_trigger_candle)
        is_breakout = (volume_surge_vs_avg >= 3.0 and recent_5m_pct >= 2.5)

        return {
            'symbol': symbol,
            'ath_price': ath_price, 'ath_date': ath_date_str, 'atl_price': atl_price, 'atl_date': atl_date_str,
            'atl_rebound_pct': atl_rebound_pct, 'monthly_high': monthly_high, 'monthly_avg_price': monthly_avg_price,
            'monthly_pace_pct': monthly_pace_pct, 'current_price': current_price,
            'near_200ema': near_200ema, 'ema_200_price': latest_ema200, 'whale_absorption': whale_absorption,
            'rsi': latest_rsi, 'rsi_reversal_turn': rsi_reversal_turn,
            'is_trained_pattern_match': is_trained_pattern_match, # PRECISION V15 Trigger
            'is_breakout': is_breakout, 'volume_surge_vs_avg': volume_surge_vs_avg,
            'recent_5m_pct': recent_5m_pct, 'recovery_probability_pct': 70
        }
        
    except Exception as e:
        print(f"⚠️ Error deep processing for {symbol}: {e}")
        return None

def general_market_panic_sniper():
    """
    Market-Wide "Panic Washout" Discovery Routine (V15.0).
    Scans ALL available USDT pairs for capitulation confluences.
    """
    global exchange, TARGET_COINS, DISCOVERED_COINS_DATA
    print("🎯 Running Automated General Market Panic Sniper Scan...")
    try:
        # Redundant failover router data fetch
        tickers = None
        for ex_id in EXCHANGE_PROVIDERS:
            try:
                ex_class = getattr(ccxt, ex_id)
                temp_ex = ex_class({'enableRateLimit': True, 'timeout': 9000})
                tickers = temp_ex.fetch_tickers()
                if tickers: break
            except: continue
        if not tickers: return

        discoveries = []
        excluded_patterns = ['DOGE', 'PEPE', 'XRP', 'SHIB', 'LINK', 'FLOKI', 'LAB']
        
        # 1. BROAD MARKET SNIPING (24h DATA Filter)
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and symbol not in TARGET_COINS and not any(p in symbol for p in excluded_patterns):
                
                quote_vol_24h = float(ticker.get('quoteVolume', 0.0) or 0.0)
                pct_change_24h = float(ticker.get('percentage', 0.0) or 0.0)
                
                # Broad Panic Qualifier: Deep red 24h drop + high 24h volume activity
                is_broad_panic = (pct_change_24h <= SNIPER_24H_DROP_REQ and quote_vol_24h >= SNIPER_24H_VOL_REQ)
                
                if is_broad_panic:
                    discoveries.append((symbol, quote_vol_24h, pct_change_24h))
                    
        # Sort by 24h volume to prioritize the strongest washout candidates
        discoveries.sort(key=lambda x: x[1], reverse=True)
        
        discovered_count = 0
        
        # 2. DEEP PRECISION BRAIN TRAINING (Fetch OHLCV Confluence)
        for item in discoveries:
            if discovered_count >= 2: break # Max 2 discoveries per trending routine
            sym, vol24, drop24 = item
            
            # Fetch the deep intelligence brain for confluence verification
            print(f"🔎 SNIPER: Broad Qualifier `{sym}` ({drop24:.1f}% Drop). Verifying Trained Pattern Confluence...")
            brain = fetch_deep_intelligence_brain(sym)
            time.sleep(0.1)
            
            if brain and brain.get('is_trained_pattern_match'):
                
                # Confirmed precision match from general market!
                discovered_count += 1
                
                # Initialize runtime state & persistence metadata
                COIN_BRAIN_CACHE[sym] = brain
                TARGET_COINS.append(sym)
                
                DISCOVERED_COINS_DATA[sym] = {
                    'timestamp': time.time(),
                    'source': 'general_market_sniper',
                    'discovery_24h_drop': drop24
                }
                
                # Save the new persistence structure
                save_watchlist_v15()
                
                # Dispatch the High Priority Telegram Discovery Alert
                msg = (
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👑 *PRECISION MARKET DISCOVERY*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🪙 *Asset identified:* `{sym}`\n"
                    f"🌐 *Context:* Market capitulation (`{drop24:.1f}%` 24h Washout)\n"
                    f"💵 *Current Price:* `${brain['current_price']:.6f}`\n\n"
                    f"⚡ *Signal:* General market panic sniper has confirmed the *Trained Rally Signature* confluence.\n\n"
                    f"✅ Brain trained, added to Active Watchlist & Lifecycle Monitor!"
                )
                send_telegram_msg(SAVED_CHAT_ID, msg)
                time.sleep(1.5)
                
        if discovered_count == 0:
            print("ℹ️ Sniper scan complete. No general market panic confluences identified this routine.")
            
    except Exception as e:
        print(f"⚠️ Error during trending coin discovery v15: {e}")

def format_telegram_alert(data):
    """Formats full pullback alert report for Telegram broadcast."""
    max_drop = max(data['monthly_drawdown_pct'], data['ath_drawdown_pct'])
    
    # Priority Badge (PRECISION Trained Signature Match from broad market or watchlist)
    if data.get('is_trained_pattern_match'):
        header_badge = "👑 [TRAINED PRE-RALLY SIGNATURE DETECTED]"
    elif data.get('is_breakout'):
        header_badge = "🚀 [CONFIRMED BREAKOUT RALLY]"
    elif max_drop >= 70:
        header_badge = "🟥 🚨 EXTREME PULLBACK ALERT (-70%+)"
    elif max_drop >= 50:
        header_badge = "🟨 ⚠️ MAJOR PULLBACK ALERT (-50%+)"
    else:
        header_badge = "🟦 📉 NOTABLE PULLBACK DETECTED (-30%+)"

    confluences = []
    if data.get('is_trained_pattern_match'):
        # Precision Trained Confluence details
        confluences.append(f"👑 Trained Signature Match (`RSI: {data['rsi']:.1f}` Oversold)")
        if data.get('whale_absorption'): confluences.append(f"🐋 Whale Absorption (`{data['volume_surge_vs_avg']:.1f}x` Vol Surge)")
    elif data.get('near_200ema'):
        confluences.append(f"🧱 Sitting near 200-Day EMA Support")
    elif data.get('rsi_reversal_turn'):
        confluences.append(f"🔥 RSI Bullish Turn")

    conf_text = "\n".join([f"  • {c}" for c in confluences]) if confluences else "  • Standard Dip Level"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{header_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 *ASSET:* `{data['symbol']}`\n"
        f"💵 *Current Price:* `${data['current_price']:.6f}`\n"
        f"🌐 *Market Climate:* `{data['market_climate']}`\n\n"
        f"📌 *DRAWDOWN SUMMARY*\n"
        f"  • 📅 *Monthly Drop:* `-{data['monthly_drawdown_pct']:.2f}%` (Peak: `${data['monthly_high']:.6f}`)\n"
        f"  • 🏆 *3-Yr ATH Drop:* `-{data['ath_drawdown_pct']:.2f}%`\n"
        f"  • 🌱 *3-Yr ATL:* `${data['atl_price']:.6f}` (on `{data['atl_date']}`)\n\n"
        f"⚡ *TECHNICAL CONFLUENCE*\n"
        f"{conf_text}\n\n"
        f"{data['reason']}\n\n"
        f"🛡️ *RISK ASSESSMENT*\n"
        f"  • *Risk Level:* {data['risk_rate']}\n"
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
    current_price = report['current_price']
    
    # Priority Alerts always bypass standard anti-spam logic once
    if report.get('is_trained_pattern_match') or report.get('is_breakout'):
        return True
        
    if current_tier == 0: return False
        
    last_tier = LAST_ALERTED_TIER.get(symbol, 0)
    last_price = LAST_ALERTED_PRICE.get(symbol, 0.0)
    
    is_new_deeper_tier = current_tier > last_tier
    
    # Check if price has rebounded substantially since last non-V pullback alert
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
    """Polls Telegram for commands (V15.0 modular command router)."""
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
                            save_watchlist_v15() # Persistence save
                            send_telegram_msg(chat_id, f"✅ `{symbol}` added manually.")
                elif text.startswith("/delete") or text.startswith("/remove"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw_coin = parts[1].upper().replace("/", "")
                        symbol = f"{raw_coin}/USDT" if not raw_coin.endswith("USDT") else f"{raw_coin[:-4]}/USDT"
                        if symbol in TARGET_COINS:
                            if symbol in CORE_WATCHLIST:
                                send_telegram_msg(chat_id, f"⚠️ Note: `{symbol}` is a Core Watchlist item. Removing it manually will prevent deep monitoring.")
                            TARGET_COINS.remove(symbol)
                            if symbol in DISCOVERED_COINS_DATA: del DISCOVERED_COINS_DATA[symbol]
                            save_watchlist_v15() # Persistence save
                            send_telegram_msg(chat_id, f"🗑️ `{symbol}` deleted.")
                elif text.startswith("/list"):
                    items = []
                    for c in TARGET_COINS:
                        is_core = c in CORE_WATCHLIST
                        is_discovered = c in DISCOVERED_COINS_DATA
                        marker = "🌱" if is_discovered else "💎" if is_core else "•"
                        items.append(f"{marker} `{c}`")
                    send_telegram_msg(chat_id, f"📋 *V15 Active Watchlist ({len(TARGET_COINS)} Coins):*\n\n" + "\n".join(items))
                elif text.startswith("/check"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw = parts[1].upper().replace("/", "")
                        symbol = f"{raw}/USDT"
                        report = generate_pullback_report_v15(symbol)
                        send_telegram_msg(chat_id, format_telegram_alert(report))
                elif text.startswith("/trending") or text.startswith("/sniper"):
                    send_telegram_msg(chat_id, "🔥 Manual trigger: Running General Market Panic Sniper...")
                    general_market_panic_sniper()
    except Exception: pass

def generate_pullback_report_v15(symbol):
    """
    Modular modular pullback report generator.
    Main non-stop monitoring loop logic.
    """
    global exchange
    brain = COIN_BRAIN_CACHE.get(symbol) or fetch_deep_intelligence_brain(symbol)
    
    if brain is None:
        return { 'symbol': symbol, 'ath_price': 0.0, 'ath_drawdown_pct': 0.0, 'monthly_drawdown_pct': 0.0,
                'market_climate': '⚖️ Market Neutral', 'reason': 'General Market profit-taking', 'risk_rate': 'Moderate' }
            
    ath_price = brain['ath_price']
    current_price = brain['current_price']
    
    ath_drawdown_pct = ((ath_price - current_price) / ath_price) * 100 if ath_price > 0 else 0.0
    monthly_drawdown_pct = ((brain['monthly_high'] - current_price) / brain['monthly_high']) * 100 if brain['monthly_high'] > 0 else 0.0
    
    market_climate = fetch_btc_market_climate()
    reason = fetch_market_pullback_reason(symbol)
    risk_rate = assess_risk_rate(monthly_drawdown_pct, symbol)
    
    return {
        'symbol': symbol, 'current_price': current_price, 'ath_price': ath_price, 'ath_date': brain['ath_date'],
        'ath_drawdown_pct': ath_drawdown_pct, 'atl_price': brain['atl_price'], 'atl_date': brain['atl_date'],
        'monthly_high': brain['monthly_high'], 'monthly_drawdown_pct': monthly_drawdown_pct,
        'near_200ema': brain['near_200ema'], 'ema_200_price': brain['ema_200_price'],
        'whale_absorption': brain['whale_absorption'], 'rsi': brain.get('rsi', 50.0),
        'rsi_reversal_turn': brain.get('rsi_reversal_turn', False),
        'is_trained_pattern_match': brain.get('is_trained_pattern_match', False), # V15 precision
        'is_breakout': brain.get('is_breakout', False), 'volume_surge_vs_avg': brain['volume_surge_vs_avg'],
        'recent_5m_pct': brain.get('recent_5m_pct', 0.0), 'market_climate': market_climate,
        'reason': reason, 'risk_rate': risk_rate
    }

def run_pullback_engine_v15():
    """Main execution non-stop monitoring loop (Modular V15.0)."""
    global HEALTH_CHECK_TIMESTAMP, WATCHLIST_CLEANUP_TIMESTAMP
    print("="*75)
    print("⚡ CRYPTOPULSE AI v15.0 - UNIFIED PANIC SNIPER ENGINE")
    print("="*75)
    
    initialize_exchange_connection()
    # Initialize runtime list & persistence structure
    load_watchlist_v15()
    
    # Modular deep training for existing coins
    print("🧠 Fetching & Training Deep Brains for Watchlist (v15)...")
    for coin in list(TARGET_COINS):
        brain = fetch_deep_intelligence_brain(coin)
        if brain: COIN_BRAIN_CACHE[coin] = brain
        time.sleep(0.1)
    print("✅ All Runtime Models Trained!\n")
    
    startup_msg = (
        "🟢 *CryptoPulse Engine v15.0 Live*\n"
        "Unified Panic Washout Snipping active across general market.\n"
        "Trained pattern recognition matched to example images enabled.\n"
        "🌱 Discovered Lifecycle Monitoring Active (24h lifespan)."
    )
    send_telegram_msg(SAVED_CHAT_ID, startup_msg)
    
    scan_count = 1
    while True:
        handle_telegram_commands()
        
        # Routine: Market Panic Sniper Scan (Runs automatically every 10 scan cycles, ~10 mins)
        if scan_count % 10 == 0: general_market_panic_sniper()
        
        # Routine: Watchlist Lifecycle Cleanup (Runs every 24 hours)
        if time.time() - WATCHLIST_CLEANUP_TIMESTAMP >= 86400:
            cleanup_discovered_coins_lifecycle()
            WATCHLIST_CLEANUP_TIMESTAMP = time.time()
            
        # Routine: Daily System Health Check Message
        if time.time() - HEALTH_CHECK_TIMESTAMP >= 86400:
            active_count = len(TARGET_COINS)
            discovered_count = len([c for c in TARGET_COINS if c in DISCOVERED_COINS_DATA])
            send_telegram_msg(SAVED_CHAT_ID, f"🟢 CryptoPulse v15.0 System Health: Scanned {scan_count} cycles. Unified monitoring active for `{active_count}` assets (`{discovered_count}` discovered). Non-stop operational.")
            HEALTH_CHECK_TIMESTAMP = time.time()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n--- [V15 PULLBACK MONITORING SCAN #{scan_count}] {now} ---")
        
        results, alerts_sent = [], 0
        
        for symbol in list(TARGET_COINS):
            report = generate_pullback_report_v15(symbol)
            should_alert = should_send_anti_spam_notification(symbol, report)
            
            max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
            status_str = "NORMAL"
            
            # Runtime Status display prioritizes precision signals
            if report.get('is_trained_pattern_match'): status_str = "👑 PRECISION MATCH"
            elif report.get('is_breakout'): status_str = "🔥 BREAKOUT"
            elif max_drop >= MIN_ALERT_DRAWDOWN_PCT: status_str = f"DROP (-{max_drop:.1f}%)"
                
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
            time.sleep(0.1) # Fetch OHLCV stagger
            
        # Summary log output to console
        summary_df = pd.DataFrame(results)
        print(summary_df.to_string(index=False))
        
        # Non-stop loop termination logic for GitHub Actions
        if MAX_SCANS and scan_count >= MAX_SCANS: break
            
        scan_count += 1
        time.sleep(60) # 60s monitor cycle time

if __name__ == "__main__":
    run_pullback_engine_v15()
