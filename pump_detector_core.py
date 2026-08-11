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

# TRAINED RALLY PATTERN PARAMETERS (Trained from multiple provided images)
TRAINED_RSI_OVERSOLD = 28        # Panic/capitulation threshold
VRS_REL_VOL_SURGE = 4.0          # How much higher current volume is than local average (15 candles)
VRS_INIT_VELOCITY_PCT = 1.6     # Instant price gain threshold (1.6% - 4%) for entry candle capture

# Default Watchlist
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
LAST_ALERTED_PRICE = {}      
LAST_TELEGRAM_UPDATE_ID = 0  
HEALTH_CHECK_TIMESTAMP = time.time()

def load_watchlist():
    global TARGET_COINS
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
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
        # Fetch data with failover router (Version 12.2 Upgrade)
        ohlcv = fetch_ohlcv_with_fallback(symbol, timeframe='1d', limit=limit)
        if not ohlcv:
            raise Exception("No candle data returned from exchanges")
            
        # ... (rest of function as in v13.0)
