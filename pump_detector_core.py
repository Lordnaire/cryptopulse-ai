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
# Reads from secure GitHub Secrets / Environment variables if available, otherwise falls back
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8953327176:AAFy_DW2hDRHG-faZpUwOL0AOfcdsAUUXcs")
SAVED_CHAT_ID = os.environ.get("SAVED_CHAT_ID", "1899452216")
MAX_SCANS_ENV = os.environ.get("MAX_SCANS", None)
MAX_SCANS = int(MAX_SCANS_ENV) if MAX_SCANS_ENV else None

# File path for persistent watchlist storage across restarts
WATCHLIST_FILE = "watchlist.json"

# Alert Cooldown Settings (In minutes) to prevent spamming
ALERT_COOLDOWN_MINUTES = 120  # 2 Hours per coin minimum delay between non-milestone updates

# Minimum Drawdown Thresholds to trigger Telegram alert
MIN_ALERT_DRAWDOWN_PCT = 30.0  

# Default Watchlist including requested coins
DEFAULT_WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 
    'DOGE/USDT', 'PEPE/USDT', 'BONK/USDT', 'WIF/USDT',
    'FLOKI/USDT', 'SHIB/USDT', 'NEAR/USDT', 'RENDER/USDT',
    'AVAX/USDT', 'LINK/USDT', 'SUI/USDT',
    'COMP/USDT', 'RIF/USDT', 'ESP/USDT', 'BANK/USDT', 
    'DEXE/USDT', 'LAB/USDT', 'ALICE/USDT', 'SENT/USDT', 
    'RE/USDT', 'ONDO/USDT', 'ZEC/USDT', 'MIRA/USDT', 
    'OPEN/USDT', 'LUMIA/USDT', 'DODO/USDT', 'SYN/USDT', 'ORDI/USDT'
]

TARGET_COINS = list(DEFAULT_WATCHLIST)

# Priority exchange list for automatic failover (bypasses region/IP blocks)
EXCHANGE_PROVIDERS = ['gateio', 'binance', 'kucoin', 'okx', 'kraken', 'bybit']

# Global State Management
exchange = None              # Active CCXT Exchange instance
COIN_BRAIN_CACHE = {}        # Historical & Monthly metrics cache
LAST_ALERTED_TIER = {}       # Anti-spam milestone memory (-30%, -50%, -70%)
COOLDOWN_TRACKER = {}        # Alert cooldown timestamps
LAST_TELEGRAM_UPDATE_ID = 0  # Command cursor

def load_watchlist():
    """Loads persistent watchlist from JSON file if available."""
    global TARGET_COINS
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    TARGET_COINS = data
                    print(f"📁 Watchlist loaded from persistent memory ({len(TARGET_COINS)} coins).")
                    return
        except Exception as e:
            print(f"⚠️ Error loading persistent watchlist: {e}")
    save_watchlist()

def save_watchlist():
    """Saves active watchlist to JSON file for persistence across server restarts."""
    try:
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(TARGET_COINS, f, indent=4)
    except Exception as e:
        print(f"⚠️ Error saving persistent watchlist: {e}")

def initialize_exchange_connection():
    """
    Tests and initializes a working exchange connector.
    Automatically bypasses CloudFront / regional IP blocks by switching providers.
    """
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
    """Fetches candlestick data with failover across exchanges."""
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
    """Calculates Exponential Moving Average (EMA)."""
    return series.ewm(span=span, adjust=False).mean()

def fetch_btc_market_climate():
    """
    Market Climate Filter (BTC Trend Check):
    Evaluates Bitcoin's 24h performance to prevent buying during market crashes.
    """
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
    """
    Fetches ~1,000 daily candles (~2.7 to 3 years of price history).
    Extracts Historical ATH, ATL, 30-Day Monthly Peak, 200-day EMA, volume absorption, and recovery rate.
    """
    try:
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=1000)
        if not ohlcv:
            raise Exception("No candle data returned from exchanges")
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # 1. Historical 3-Year All-Time High (ATH)
        ath_row = df.loc[df['high'].idxmax()]
        ath_price = float(ath_row['high'])
        ath_date_str = ath_row['date'].strftime('%Y-%m-%d')

        # 2. Historical 3-Year All-Time Low (ATL)
        atl_row = df.loc[df['low'].idxmin()]
        atl_price = float(atl_row['low'])
        atl_date_str = atl_row['date'].strftime('%Y-%m-%d')
        
        # 3. Monthly High (Peak of Last 30 Days)
        last_30_days = df.tail(30)
        monthly_high = float(last_30_days['high'].max())
        monthly_avg_price = float(last_30_days['close'].mean())
        month_start_price = float(last_30_days.iloc[0]['close'])
        current_price = float(df.iloc[-1]['close'])
        
        monthly_pace_pct = ((current_price - month_start_price) / month_start_price) * 100 if month_start_price > 0 else 0.0
        atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

        # 4. 200-Day EMA Confluence Check
        df['ema_200'] = calculate_ema(df['close'], span=200)
        latest_ema200 = float(df.iloc[-1]['ema_200']) if len(df) >= 200 else 0.0
        near_200ema = False
        if latest_ema200 > 0:
            pct_diff = abs(current_price - latest_ema200) / latest_ema200 * 100
            if pct_diff <= 3.5:
                near_200ema = True

        # 5. Whale Dip Absorption Detector (24h volume vs 20d average)
        vol_20d_avg = float(df['volume'].tail(20).mean())
        latest_volume = float(df.iloc[-1]['volume'])
        volume_surge = (latest_volume / vol_20d_avg) if vol_20d_avg > 0 else 1.0
        whale_absorption = volume_surge >= 1.5
        
        # 6. Calculate Historical Recovery Probability
        df['peak_20d'] = df['high'].rolling(20, min_periods=1).max()
        df['drawdown_from_peak'] = ((df['peak_20d'] - df['close']) / df['peak_20d']) * 100
        
        major_drop_indices = df[df['drawdown_from_peak'] >= 30.0].index.tolist()
        
        drop_events = []
        if major_drop_indices:
            curr_event = [major_drop_indices[0]]
            for idx in major_drop_indices[1:]:
                if idx - curr_event[-1] <= 5: # Within 5 days
                    curr_event.append(idx)
                else:
                    drop_events.append(curr_event[0])
                    curr_event = [idx]
            drop_events.append(curr_event[0])
            
        successful_recoveries = 0
        total_events = len(drop_events)
        
        for drop_idx in drop_events:
            entry_price = df.iloc[drop_idx]['close']
            future_window = df.iloc[drop_idx:min(drop_idx + 60, len(df))]
            if len(future_window) > 1:
                max_future_price = future_window['high'].max()
                if max_future_price >= entry_price * 1.40:
                    successful_recoveries += 1
                    
        recovery_prob_pct = int((successful_recoveries / total_events) * 100) if total_events > 0 else 65
        
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
            'historical_drop_events': total_events,
            'recovery_probability_pct': max(min(recovery_prob_pct, 95), 15)
        }
        COIN_BRAIN_CACHE[symbol] = brain_data
        return brain_data
        
    except Exception as e:
        print(f"⚠️ Error processing history for {symbol}: {e}")
        fallback = {
            'symbol': symbol,
            'ath_price': 0.0,
            'ath_date': 'Unknown',
            'atl_price': 0.0,
            'atl_date': 'Unknown',
            'atl_rebound_pct': 0.0,
            'monthly_high': 0.0,
            'monthly_avg_price': 0.0,
            'monthly_pace_pct': 0.0,
            'near_200ema': False,
            'ema_200_price': 0.0,
            'whale_absorption': False,
            'volume_surge': 1.0,
            'historical_drop_events': 0,
            'recovery_probability_pct': 50
        }
        COIN_BRAIN_CACHE[symbol] = fallback
        return fallback

def auto_discover_trending_coins():
    """
    Automated Discovery Engine:
    Scans exchange market tickers for high 24h volume/gainer coins
    and automatically adds them to the active watchlist.
    """
    global exchange, TARGET_COINS
    print("🔥 Running Automated Trending Coin Discovery Scan...")
    try:
        tickers = exchange.fetch_tickers()
        trending_candidates = []
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and symbol not in TARGET_COINS:
                quote_vol = float(ticker.get('quoteVolume', 0.0) or 0.0)
                pct_change = float(ticker.get('percentage', 0.0) or 0.0)
                
                if quote_vol >= 10_000_000 and pct_change >= 8.0:
                    trending_candidates.append((symbol, quote_vol, pct_change))
                    
        trending_candidates.sort(key=lambda x: x[1], reverse=True)
        
        added_count = 0
        for item in trending_candidates[:2]:
            sym, vol, gain = item
            TARGET_COINS.append(sym)
            save_watchlist()
            fetch_3year_historical_data(sym)
            
            msg = (
                f"🔥 *AUTOMATED TRENDING DISCOVERY!*\n\n"
                f"🪙 *New Coin Added:* `{sym}`\n"
                f"📈 *24h Gain:* `+{gain:.2f}%`\n"
                f"📊 *24h Volume:* `${vol/1e6:.1f}M`\n\n"
                f"✅ 3-Year Brain Trained & Saved to Persistent Watchlist!"
            )
            send_telegram_msg(SAVED_CHAT_ID, msg)
            added_count += 1
            time.sleep(0.2)
            
        if added_count == 0:
            print("ℹ️ Discovery Scan Complete: No new qualifying trending coins found.")
            
    except Exception as e:
        print(f"⚠️ Error during trending coin discovery: {e}")

def initialize_all_brains():
    """Builds 3-year historical & monthly signatures for all watchlisted coins."""
    load_watchlist()
    print("🧠 Fetching & Training 3-Year Historical & Monthly Pullback Brains...")
    for coin in list(TARGET_COINS):
        fetch_3year_historical_data(coin)
        time.sleep(0.1)
    print("✅ All Historical & Monthly Models Initialized!\n")

def fetch_market_pullback_reason(coin_name):
    """
    Searches public news RSS feeds to identify recent catalysts.
    """
    clean_name = coin_name.split('/')[0]
    rss_url = f"https://news.google.com/rss/search?q={clean_name}+crypto+news+dump+or+drop&hl=en-US&gl=US&ceid=US:en"
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(rss_url, headers=headers, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            headlines = []
            for item in root.findall('./channel/item')[:3]:
                title = item.find('title').text if item.find('title') is not None else ""
                title = re.sub(r' - [^-]+$', '', title)
                if title:
                    headlines.append(title)
            
            if headlines:
                combined = " ".join(headlines).lower()
                if "unlock" in combined or "cliff" in combined:
                    category = "🔓 Token Unlocks & Supply Expansion"
                elif "sec" in combined or "lawsuit" in combined or "regulation" in combined:
                    category = "⚖️ Regulatory / Legal Headwinds"
                elif "hack" in combined or "exploit" in combined or "drain" in combined:
                    category = "🚨 Security Breach / Protocol Exploit"
                elif "fed" in combined or "inflation" in combined or "rate" in combined or "macro" in combined:
                    category = "📉 Macro Market Selloff & Liquidity Squeeze"
                else:
                    category = "📊 Profit-Taking / Technical Correction"
                    
                return f"{category}\n  • Headline: \"{headlines[0][:80]}...\""
    except Exception:
        pass
        
    return "📊 General Market Profit-Taking & Liquidations"

def assess_risk_rate(drawdown_pct, recovery_prob, coin):
    """Categorizes trading risk into Low, Moderate, High, or Extreme."""
    is_bluechip = any(b in coin for b in ['BTC', 'ETH', 'SOL'])
    
    if drawdown_pct >= 75.0:
        return "🔴 EXTREME RISK (Deep Fall / Severe Capitulation)"
    elif drawdown_pct >= 50.0:
        if is_bluechip or recovery_prob >= 60:
            return "🟠 HIGH RISK (Major Discount / Volatile Zone)"
        else:
            return "🔴 HIGH RISK (Low Recovery History)"
    elif drawdown_pct >= 30.0:
        if is_bluechip:
            return "🟡 MODERATE RISK (Standard Bull-Market Retest)"
        else:
            return "🟠 MODERATE RISK (Alts Retest)"
    else:
        return "🟢 LOW RISK (Minor Pullback)"

def send_telegram_msg(chat_id, text):
    """Sends Markdown formatted alert to Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def generate_pullback_report(symbol):
    """Generates deep pullback analysis for both Historical ATH/ATL & Monthly High."""
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
    
    if ath_price <= 0:
        ath_price = current_price * 1.5 if current_price > 0 else 1.0
    if monthly_high <= 0:
        monthly_high = current_price * 1.2 if current_price > 0 else 1.0
        
    ath_drawdown_pct = ((ath_price - current_price) / ath_price) * 100 if ath_price > 0 else 0.0
    monthly_drawdown_pct = ((monthly_high - current_price) / monthly_high) * 100 if monthly_high > 0 else 0.0
    atl_rebound_pct = ((current_price - atl_price) / atl_price) * 100 if atl_price > 0 else 0.0

    market_climate = fetch_btc_market_climate()
    reason = fetch_market_pullback_reason(symbol)
    risk_rate = assess_risk_rate(monthly_drawdown_pct, brain['recovery_probability_pct'], symbol)
    
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
        'volume_surge': brain['volume_surge'],
        'market_climate': market_climate,
        'reason': reason,
        'risk_rate': risk_rate,
        'recovery_prob': brain['recovery_probability_pct']
    }

def format_telegram_alert(data):
    """Formats full pullback alert report for Telegram broadcast."""
    max_drop = max(data['monthly_drawdown_pct'], data['ath_drawdown_pct'])
    
    if max_drop >= 70:
        grade = "🚨 EXTREME PULLBACK ALERT (-70%+)"
    elif max_drop >= 50:
        grade = "⚠️ MAJOR PULLBACK ALERT (-50%+)"
    else:
        grade = "📉 NOTABLE PULLBACK DETECTED (-30%+)"

    t25 = data['current_price'] * 1.25
    t50 = data['current_price'] * 1.50
    ath_gain = ((data['ath_price'] - data['current_price']) / data['current_price']) * 100 if data['current_price'] > 0 else 0

    confluences = []
    if data.get('near_200ema'):
        confluences.append(f"🧱 Sitting near 200-Day EMA Key Support (${data['ema_200_price']:.4f})")
    if data.get('whale_absorption'):
        confluences.append(f"🐋 Whale Dip Absorption ({data['volume_surge']:.1f}x Volume Surge)")

    conf_text = "\n" + "\n".join([f"  • {c}" for c in confluences]) if confluences else " Standard Dip Level"

    msg = (
        f"*{grade}*\n\n"
        f"🪙 *Asset:* `{data['symbol']}`\n"
        f"💵 *Current Price:* `${data['current_price']:.6f}`\n"
        f"🌐 *Market Climate:* `{data['market_climate']}`\n\n"
        f"🏆 *Historical ATH:* `${data['ath_price']:.6f}` (`{data['ath_date']}`)\n"
        f"🔻 *Fall from 3-Yr ATH:* `-{data['ath_drawdown_pct']:.2f}%`\n\n"
        f"🌱 *Historical ATL:* `${data['atl_price']:.6f}` (`{data['atl_date']}`)\n"
        f"📈 *Rebound from ATL:* `+{data['atl_rebound_pct']:.2f}%`\n\n"
        f"📅 *Monthly High (30d Peak):* `${data['monthly_high']:.6f}`\n"
        f"🔻 *Fall from Monthly High:* `-{data['monthly_drawdown_pct']:.2f}%`\n\n"
        f"📊 *Monthly Overview:* Avg `${data['monthly_avg_price']:.6f}` (`{data['monthly_pace_pct']:+.2f}% 30d pace`)\n\n"
        f"⚡ *Technical Confluence:*{conf_text}\n\n"
        f"🎯 *Recovery Targets & Profit Goals:*\n"
        f"  • 25% Bounce Target: `${t25:.6f}` (+25%)\n"
        f"  • 50% Bounce Target: `${t50:.6f}` (+50%)\n"
        f"  • Full ATH Rebound: `${data['ath_price']:.6f}` (`+{ath_gain:.1f}%` upside)\n\n"
        f"🔍 *Pullback Catalyst / Reason:*\n{data['reason']}\n\n"
        f"🛡️ *Risk Rate:* {data['risk_rate']}\n"
        f"📈 *Recovery Probability:* `{data['recovery_prob']}%` (Based on past cycle bounces)"
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
    max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
    current_tier = get_drawdown_tier(max_drop)
    
    if current_tier == 0:
        return False
        
    last_tier = LAST_ALERTED_TIER.get(symbol, 0)
    last_time = COOLDOWN_TRACKER.get(symbol, 0)
    now = time.time()
    
    elapsed_minutes = (now - last_time) / 60.0
    
    is_new_deeper_tier = current_tier > last_tier
    is_cooldown_expired = elapsed_minutes >= ALERT_COOLDOWN_MINUTES
    
    if is_new_deeper_tier or is_cooldown_expired:
        LAST_ALERTED_TIER[symbol] = current_tier
        COOLDOWN_TRACKER[symbol] = now
        return True
        
    return False

def handle_telegram_commands():
    """Polls Telegram for commands."""
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
                            send_telegram_msg(chat_id, f"🔄 Fetching 3-year historical & monthly data for `{symbol}`...")
                            fetch_3year_historical_data(symbol)
                            send_telegram_msg(chat_id, f"✅ `{symbol}` added & saved to persistent pullback watchlist!")
                        else:
                            send_telegram_msg(chat_id, f"ℹ️ `{symbol}` is already in your active watchlist.")
                    else:
                        send_telegram_msg(chat_id, "⚠️ Usage: `/add SUI` or `/add SUI/USDT`")
                        
                elif text.startswith("/remove") or text.startswith("/delete"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw_coin = parts[1].upper().replace("/", "")
                        symbol = f"{raw_coin}/USDT" if not raw_coin.endswith("USDT") else f"{raw_coin[:-4]}/USDT"
                        
                        if symbol in TARGET_COINS:
                            TARGET_COINS.remove(symbol)
                            save_watchlist()
                            if symbol in COIN_BRAIN_CACHE:
                                del COIN_BRAIN_CACHE[symbol]
                            if symbol in LAST_ALERTED_TIER:
                                del LAST_ALERTED_TIER[symbol]
                            send_telegram_msg(chat_id, f"🗑️ `{symbol}` deleted & removed from persistent watchlist.")
                        else:
                            send_telegram_msg(chat_id, f"⚠️ `{symbol}` not found in active watchlist.")
                    else:
                        send_telegram_msg(chat_id, "⚠️ Usage: `/delete SUI` or `/remove SUI`")
                        
                elif text.startswith("/list"):
                    items = []
                    for c in TARGET_COINS:
                        brain = COIN_BRAIN_CACHE.get(c, {})
                        ath = brain.get('ath_price', 0)
                        atl = brain.get('atl_price', 0)
                        items.append(f"• `{c}` (ATH: `${ath:.4f}` | ATL: `${atl:.4f}`)")
                    msg = f"📋 *Active Watchlist ({len(TARGET_COINS)} Coins):*\n\n" + "\n".join(items)
                    send_telegram_msg(chat_id, msg)

                elif text.startswith("/check") or text.startswith("/pullback"):
                    parts = text.split()
                    if len(parts) > 1:
                        raw = parts[1].upper().replace("/", "")
                        symbol = f"{raw}/USDT" if not raw.endswith("USDT") else f"{raw[:-4]}/USDT"
                        send_telegram_msg(chat_id, f"🔍 Generating deep pullback analysis for `{symbol}`...")
                        report = generate_pullback_report(symbol)
                        send_telegram_msg(chat_id, format_telegram_alert(report))
                    else:
                        send_telegram_msg(chat_id, "🔍 Scanning watchlist for major drawdowns...")
                        for coin in TARGET_COINS:
                            report = generate_pullback_report(coin)
                            max_drop = max(report['monthly_drawdown_pct'], report['ath_drawdown_pct'])
                            if max_drop >= MIN_ALERT_DRAWDOWN_PCT:
                                send_telegram_msg(chat_id, format_telegram_alert(report))
                                time.sleep(0.3)
                                
                elif text.startswith("/trending"):
                    send_telegram_msg(chat_id, "🔥 Manual trigger: Scanning market for top trending volume coins...")
                    auto_discover_trending_coins()
                    
    except Exception:
        pass

def run_pullback_engine():
    print("="*75)
    print("⚡ CRYPTOPULSE AI v10.1 - 24/7 CLOUD AUTOMATION PULLBACK ENGINE")
    print("="*75)
    print(f"✅ Telegram Channel Active (Chat ID: {SAVED_CHAT_ID})")
    
    initialize_exchange_connection()
    initialize_all_brains()
    
    startup_msg = (
        "🟢 *CryptoPulse Pullback Engine Live*\n"
        "🌱 ATH & ATL Tracking Enabled.\n"
        "🔥 Automated Trending Coin Discovery Active.\n"
        "📁 Persistent Watchlist Memory Loaded.\n"
        "📱 Telegram Commands: `/add SUI`, `/delete SUI`, `/list`, `/check PEPE`, `/trending`"
    )
    send_telegram_msg(SAVED_CHAT_ID, startup_msg)
    print("📲 Startup confirmation sent to Telegram.\n")
    
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
            status_str = f"DROP DETECTED (-{max_drop:.1f}%)" if max_drop >= MIN_ALERT_DRAWDOWN_PCT else "NORMAL"
            if should_alert:
                status_str += " 📲 [ALERT SENT]"
            
            results.append({
                'Coin': report['symbol'],
                'Price ($)': f"{report['current_price']:.6f}",
                'ATH ($)': f"{report['ath_price']:.4f}",
                'ATL ($)': f"{report['atl_price']:.4f}",
                'Monthly Drop': f"-{report['monthly_drawdown_pct']:.1f}%",
                'ATH Drop': f"-{report['ath_drawdown_pct']:.1f}%",
                'Status': status_str
            })
            
            if should_alert:
                send_telegram_msg(SAVED_CHAT_ID, format_telegram_alert(report))
                alerts_sent += 1
                
            time.sleep(0.12)
            
        summary_df = pd.DataFrame(results)
        print(summary_df.to_string(index=False))
        print(f"\nSummary: Scanned {len(TARGET_COINS)} coins | Telegram Alerts Sent This Scan: {alerts_sent}")
        
        # Cloud Execution check: If MAX_SCANS limit is set, exit cleanly after limit reached
        if MAX_SCANS and scan_count >= MAX_SCANS:
            print(f"🏁 Cloud scan run completed ({scan_count} cycles). Exiting cleanly.")
            break
            
        print("⏳ Sleeping 60s until next scan cycle... (Press Stop button to end)")
        scan_count += 1
        time.sleep(60)

if __name__ == "__main__":
    run_pullback_engine()
