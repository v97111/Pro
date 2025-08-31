# -*- coding: utf-8 -*-
"""
Fast-cycle Binance bot + Flask dashboard (multi-worker)

SAFE mode (realistic + conservative):
- EMA50 strict, volume >= 1.2x avg10
- Pattern: previous closed bar dips >= 0.6% and next closed bar closes above previous HIGH

FAST mode (easier):
- EMA >= 99.2% of EMA50
- Volume >= 0.85x avg10 OR last volume is top-3 among last 10 closed bars
- Pattern: bounce-only (last close > previous close)

Exits for both:
- Hard TP +1.25% (to net >= ~1% after fees), then trailing arms at +1.6% with 0.4% giveback
- Stop-loss -1.5%, Max trade time 45m

Dashboard:
- Mobile friendly, worker cards, live unrealized PnL, time-in-trade, debug (toggle/copy/export), trade history
"""
import os, time, csv, math, threading, sys, asyncio, websocket, json
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from typing import Dict, List, Optional
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv
from binance.client import Client
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import io
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal, ROUND_DOWN, getcontext

# Set high precision for Decimal calculations
getcontext().prec = 28

# ---- Concurrency / caches ----
LOG_LOCK = threading.Lock()
PRICES_LOCK = threading.Lock()
CANDLES_CACHE_LOCK = threading.Lock()

# Console output capture
class ConsoleCapture:
    def __init__(self, max_lines=1000):
        self.max_lines = max_lines
        self.lines = deque(maxlen=max_lines)
        self.lock = threading.Lock()
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.capturing = True

    def write(self, data):
        # Write to original stdout first
        self.original_stdout.write(data)
        self.original_stdout.flush()

        # Only capture if enabled and data is meaningful
        if self.capturing and data and data.strip() and not data.isspace():
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with self.lock:
                self.lines.append(f"[{timestamp}] {data.strip()}")

    def flush(self):
        self.original_stdout.flush()

    def get_lines(self, limit=None):
        with self.lock:
            lines_list = list(self.lines)
            if limit:
                return lines_list[-limit:]
            return lines_list

    def disable_capture(self):
        self.capturing = False

    def enable_capture(self):
        self.capturing = True

# Global console capture
console_capture = ConsoleCapture()

_PRICE_CACHE = {"ts": 0.0, "prices": {}}  # global last-fetched prices
PRICE_CACHE_TTL = 2  # seconds
CANDLES_CACHE_TTL = 30  # seconds

# Rate Limiting
class RateLimiter:
    def __init__(self, requests_per_minute=600):
        self.requests_per_minute = requests_per_minute
        self.requests = deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.time()
            # Remove requests older than 1 minute
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()

            if len(self.requests) >= self.requests_per_minute:
                sleep_time = 60 - (now - self.requests[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    return self.acquire()

            self.requests.append(now)

# WebSocket Price Feed
class WebSocketPriceFeed:
    def __init__(self, symbols):
        self.symbols = [s.lower() for s in symbols]
        self.prices = {}
        self.ws = None
        self.running = False
        self._lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

    def get_price(self, symbol):
        with self._lock:
            return self.prices.get(symbol.upper())

    def _run_forever(self):
        while self.running:
            try:
                streams = [f"{symbol}@ticker" for symbol in self.symbols]
                stream_url = f"wss://stream.binance.com:9443/ws/{'/'.join(streams)}"

                def on_message(ws, message):
                    try:
                        data = json.loads(message)
                        if 'data' in data:
                            data = data['data']
                        if 's' in data and 'c' in data:
                            symbol = data['s']
                            price = float(data['c'])
                            with self._lock:
                                self.prices[symbol] = price
                    except Exception:
                        pass

                def on_error(ws, error):
                    pass

                def on_close(ws, close_status_code, close_msg):
                    if self.running:
                        time.sleep(5)  # Reconnect after 5 seconds

                import websocket
                websocket.enableTrace(False)
                self.ws = websocket.WebSocketApp(
                    stream_url,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                self.ws.run_forever()

            except Exception:
                if self.running:
                    time.sleep(10)  # Wait before reconnecting

# ------------------ Watchlist ------------------
WATCHLIST: List[str] = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","TRXUSDT","LINKUSDT",
    "DOTUSDT","MATICUSDT","TONUSDT","OPUSDT","ARBUSDT","SUIUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","NEARUSDT",
    "APTUSDT","FILUSDT","HBARUSDT","ICPUSDT","GALAUSDT","CFXUSDT","FETUSDT","RNDRUSDT","INJUSDT","FTMUSDT",
    "THETAUSDT","MANAUSDT","SANDUSDT","AXSUSDT","FLOWUSDT","KAVAUSDT","ROSEUSDT","C98USDT","GMTUSDT","ANKRUSDT",
    "CHZUSDT","CRVUSDT","DYDXUSDT","ENSUSDT","LRCUSDT","ONEUSDT","QTUMUSDT","STGUSDT","WAVESUSDT","ZILUSDT",
    "MINAUSDT","PEPEUSDT","JOEUSDT","HIGHUSDT","IDEXUSDT","ILVUSDT","MAGICUSDT","LINAUSDT","OCEANUSDT","IMXUSDT",
    "RLCUSDT","GLMRUSDT","CELOUSDT","COTIUSDT","ACHUSDT","API3USDT","ALGOUSDT","BADGERUSDT","BANDUSDT","BATUSDT",
    "BICOUSDT","BLZUSDT","COMPUSDT","CTKUSDT","DASHUSDT","DENTUSDT","DODOUSDT","ELFUSDT","ENJUSDT","EOSUSDT",
    "ETCUSDT","FLMUSDT","FXSUSDT","GRTUSDT","HOTUSDT","ICXUSDT","IOSTUSDT","IOTAUSDT","KLAYUSDT","KNCUSDT",
    "MASKUSDT","MKRUSDT","MTLUSDT","NKNUSDT","OGNUSDT","OMGUSDT","PHAUSDT","PYRUSDT","REIUSDT","RENUSDT",
    "SKLUSDT","SPELLUSDT","STMXUSDT","STORJUSDT","TLMUSDT","UMAUSDT","UNIUSDT","VETUSDT","XLMUSDT","XTZUSDT",
    "YFIUSDT","ZRXUSDT",
    # Additional 100 high-potential coins with strong daily movement potential
    "WIFUSDT","JUPUSDT","BOMEUSDT","WLDUSDT","NOTUSDT","FLOKIUSDT","BONKUSDT","1000SATSUSDT","ORDIUSDT","RAYUSDT",
    "PENDLEUSDT","ETHFIUSDT","ENAUSDT","WUSDT","AIUSDT","ALTUSDT","TNSR USDT","OMNIUSDT","SAGA USDT","TAOUSDT",
    "ARKMUSDT","PIXELUSDT","STRKUSDT","ACEUSDT","NFPUSDT","AIUSDT","XAIUSDT","MANTAUSDT","PYTHUSDT","DYMUSDT",
    "PORTALUSDT","PONDUSDT","BRETTUSDT","TURBOUSDT","MEUSDT","REZUSDT","BBUSDT","NOTUSDT","IOUSDT","ZKUSDT",
    "LISTAUSDT","ZROUSDT","GUSDT","BANANAUSDT","RENDERUSDT","TAOUSDT","OMNIUSDT","WIFUSDT","JUPUSDT","BOMEUSDT",
    "SEIUSDT","TIAUSDT","STXUSDT","ARKMUSDT","BEAMUSDT","PIVXUSDT","VIBEUSDT","CAKEUSDT","PUNDIXUSDT","TLMUSDT",
    "JASMYUSDT","DARUSDT","UNFIUSDT","RADUSDT","TRUUSDT","TWTUSDT","DEXEUSDT","POLYXUSDT","MDTUSDT","STPTUSDT",
    "GASUSDT","POWRUSDT","SLPUSDT","FISUSDT","REEFUSDT","CLVUSDT","YFIIUSDT","EASYUSDT","AUDIOUSDT","CTKUSDT",
    "BCHUSDT","SUSHIUSDT","SNXUSDT","BALUSDT","ZENUSDT","SKLUSDT","RUNEUSDT","SRMUSDT","LPTUSDT","ALPHAUSDT",
    "DEGOUSDT","CVPUSDT","EPXUSDT","LOKAUSDT","OOKIUSDT","PROSUSDT","VOXELUSDT","HIGHUSDT","CVXUSDT","STGUSDT"
]

# ------------------ Config ------------------
INTERVAL = Client.KLINE_INTERVAL_1MINUTE
KLIMIT   = 120

# Exits (guarantee >= ~1% TP first, then trail) - Now tunable
TAKE_PROFIT_MIN_PCT   = 0.0100  # +1.00% hard TP
TRAIL_ARM_PCT         = 0.0160  # arm trailing only if >= +1.6%
TRAIL_GIVEBACK_PCT    = 0.0040  # 0.4% giveback; worst trailing ~+1.2%
STOP_LOSS_PCT         = 0.0100  # -1.0%
MAX_TRADE_MINUTES     = 45

# Entry filters - Now tunable
MIN_DAY_VOLATILITY_PCT = 0.5     # 24h range >= 0.5%
COOLDOWN_MINUTES        = 8

# Global tunable parameters
TUNABLE_PARAMS = {
    'take_profit_pct': TAKE_PROFIT_MIN_PCT * 100,
    'trail_arm_pct': TRAIL_ARM_PCT * 100,
    'trail_giveback_pct': TRAIL_GIVEBACK_PCT * 100,
    'stop_loss_pct': STOP_LOSS_PCT * 100,
    'min_day_volatility_pct': MIN_DAY_VOLATILITY_PCT,
    'cooldown_minutes': COOLDOWN_MINUTES,
    'max_trade_minutes': MAX_TRADE_MINUTES,
    'ema_relax': 99.0,  # EMA strictness as percentage of EMA50
    'vol_mult': 0.85,   # Volume multiplier
    'reversal_threshold_pct': 0.2,  # Reversal threshold for TP opportunity mode
    'buying_pattern': 1  # Add missing buying pattern parameter
}

# Loop timing
POLL_SECONDS_IDLE   = 1    # 1 second when not in position
POLL_SECONDS_ACTIVE = 1    # 1 second for price checking in position

# Logging / debug
LOG_FILE             = os.getenv("LOG_FILE", "fast_cycle_trades.csv")
RECENT_TRADES_LIMIT  = 500
DEBUG_BUFFER         = 2000

# ------------------ Cache Management ------------------
class ThreadSafeCacheManager:
    def __init__(self, ttl=30):
        self._cache = {}
        self._lock = threading.Lock()
        self.ttl = ttl

    def get(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] <= self.ttl:
                return entry["data"]
            return None

    def set(self, key, data):
        with self._lock:
            self._cache[key] = {"ts": time.time(), "data": data}

    def invalidate(self, key=None):
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()

# Enhanced features disabled for stability
ENHANCED_FEATURES = False

# ------------------ Trade Analysis ------------------
class TradeAnalyzer:
    def __init__(self, csv_file):
        self.csv_file = csv_file
        self._analysis_cache = {}
        self._cache_timestamp = 0
        self.cache_ttl = 300  # 5 minutes

    def invalidate_cache(self):
        """Force cache refresh on next access"""
        self._analysis_cache = {}
        self._cache_timestamp = 0
        print("[TradeAnalyzer] Cache invalidated - will reload on next access")

    def _get_trades_data(self):
        """Load and parse all trade data with improved matching logic"""
        current_time = time.time()
        if current_time - self._cache_timestamp < self.cache_ttl and self._analysis_cache:
            return self._analysis_cache

        trades = []
        all_orders = []  # Store all orders chronologically

        try:
            if not os.path.exists(self.csv_file):
                print(f"[TradeAnalyzer] CSV file not found: {self.csv_file}")
                self._analysis_cache = []
                self._cache_timestamp = current_time
                return self._analysis_cache

            with open(self.csv_file, 'r', encoding='utf-8') as f:
                import csv as csv_module
                from datetime import datetime

                # Skip header and manually parse CSV to avoid issues
                lines = f.readlines()
                if len(lines) < 2:
                    print(f"[TradeAnalyzer] CSV file has insufficient data: {len(lines)} lines")
                    self._analysis_cache = []
                    self._cache_timestamp = current_time
                    return []

                # Skip header line
                data_lines = lines[1:]
                row_count = 0

                # First pass: collect all orders
                for line in data_lines:
                    row_count += 1
                    try:
                        # Manual CSV parsing to handle any formatting issues
                        parts = line.strip().split(',')
                        if len(parts) < 8:  # Need at least 8 columns
                            continue

                        time_str = parts[0].strip()
                        symbol = parts[1].strip()
                        action = parts[2].strip()
                        price_str = parts[3].strip()
                        qty_str = parts[4].strip()
                        pnl_str = parts[5].strip()
                        note = parts[6].strip()
                        worker_id = parts[7].strip()

                        if not symbol or not worker_id or not action or not time_str:
                            continue

                        # Parse timestamp for chronological matching
                        try:
                            timestamp = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                        except:
                            continue

                        order = {
                            'symbol': symbol,
                            'action': action,
                            'time': time_str,
                            'timestamp': timestamp,
                            'price': float(price_str) if price_str else 0.0,
                            'qty': float(qty_str) if qty_str else 0.0,
                            'worker_id': worker_id,
                            'pnl_pct': float(pnl_str) if pnl_str and pnl_str != '' else 0.0,
                            'note': note
                        }
                        all_orders.append(order)

                    except (ValueError, TypeError, KeyError) as e:
                        print(f"[TradeAnalyzer] Error parsing row {row_count}: {e}")
                        continue

                # Sort orders by timestamp
                all_orders.sort(key=lambda x: x['timestamp'])

                # Second pass: match BUYs with SELLs chronologically by worker+symbol
                pending_buys = {}  # Key: "symbol_worker_id" -> buy_order

                buy_count = 0
                sell_count = 0

                for order in all_orders:
                    if order['action'] == 'BUY':
                        buy_count += 1
                        # Create unique key for this worker+symbol combination
                        key = f"{order['symbol']}_{order['worker_id']}"
                        pending_buys[key] = order
                        if buy_count <= 3:  # Log first few for debugging
                            print(f"[TradeAnalyzer] BUY order: {order['symbol']} W{order['worker_id']} @ {order['price']}")

                    elif order['action'].startswith('SELL'):
                        sell_count += 1
                        # Look for matching BUY order with same worker+symbol
                        key = f"{order['symbol']}_{order['worker_id']}"
                        matching_buy = pending_buys.pop(key, None)

                        if sell_count <= 3:  # Log first few for debugging
                            print(f"[TradeAnalyzer] SELL order: {order['symbol']} W{order['worker_id']} @ {order['price']} PnL: {order['pnl_pct']}")
                            print(f"[TradeAnalyzer] Looking for key: {key}, Found match: {'Yes' if matching_buy else 'No'}")

                        if matching_buy:
                            # Create completed trade
                            trade = {
                                'symbol': matching_buy['symbol'],
                                'buy_time': matching_buy['time'],
                                'buy_price': matching_buy['price'],
                                'qty': matching_buy['qty'],
                                'worker_id': matching_buy['worker_id'],
                                'sell_time': order['time'],
                                'sell_price': order['price'],
                                'pnl_pct': order['pnl_pct'],
                                'sell_action': order['action'],
                                'note': order['note'],
                                'duration_minutes': self._calculate_trade_duration(
                                    matching_buy['time'], order['time']
                                )
                            }
                            trades.append(trade)
                            print(f"[TradeAnalyzer] Matched trade: {order['symbol']} W{order['worker_id']} PnL: {order['pnl_pct']:+.2f}%")
                        else:
                            print(f"[TradeAnalyzer] No matching BUY found for SELL: {order['symbol']} W{order['worker_id']}")

                print(f"[TradeAnalyzer] Processed {row_count} CSV rows")
                print(f"[TradeAnalyzer] Found {buy_count} BUY orders, {sell_count} SELL orders")
                print(f"[TradeAnalyzer] Found {len(trades)} complete trades")
                print(f"[TradeAnalyzer] {len(pending_buys)} unmatched BUY orders remaining")

                if len(pending_buys) > 0:
                    print(f"[TradeAnalyzer] Sample unmatched BUYs:")
                    for i, (key, buy) in enumerate(list(pending_buys.items())[:5]):
                        print(f"  {i+1}. {key}: {buy['symbol']} W{buy['worker_id']} @ {buy['price']}")

                if len(trades) == 0 and len(all_orders) > 0:
                    print(f"[TradeAnalyzer] Sample orders from CSV:")
                    for i, order in enumerate(all_orders[:10]):
                        print(f"  {i+1}. {order['action']} {order['symbol']} W{order['worker_id']} PnL:{order['pnl_pct']}")

                if len(trades) > 0:
                    print(f"[TradeAnalyzer] Sample trades:")
                    for i, trade in enumerate(trades[:3]):
                        print(f"  {i+1}. {trade['symbol']}: {trade['pnl_pct']:+.2f}%")

            self._analysis_cache = trades
            self._cache_timestamp = current_time
            return self._analysis_cache

        except Exception as e:
            print(f"[TradeAnalyzer] Error loading trades data: {e}")
            import traceback
            traceback.print_exc()
            self._analysis_cache = []
            self._cache_timestamp = current_time
            return []

    def _calculate_trade_duration(self, buy_time, sell_time):
        """Calculate trade duration in minutes"""
        try:
            from datetime import datetime
            buy_dt = datetime.fromisoformat(buy_time.replace('Z', '+00:00'))
            sell_dt = datetime.fromisoformat(sell_time.replace('Z', '+00:00'))
            return (sell_dt - buy_dt).total_seconds() / 60
        except:
            return 0

    def get_comprehensive_analysis(self):
        """Get comprehensive trading analysis"""
        try:
            trades = self._get_trades_data()

            if not trades:
                return {
                    "total_trades": 0,
                    "completed_trades": 0,
                    "win_rate": 0.0,
                    "avg_pnl": 0.0,
                    "total_pnl": 0.0,
                    "avg_duration": 0.0,
                    "symbol_stats": {},
                    "recent_trades": [],
                    "performance_by_time": {},
                    "risk_metrics": {
                        "volatility": 0.0,
                        "max_drawdown": 0.0,
                        "sharpe_ratio": 0.0,
                        "best_trade": 0.0,
                        "worst_trade": 0.0
                    },
                    "recommendations": []
                }

            # Basic metrics
            total_trades = len(trades)
            winners = [t for t in trades if t.get('pnl_pct', 0) > 0]
            losers = [t for t in trades if t.get('pnl_pct', 0) <= 0]

            win_rate = (len(winners) / total_trades * 100) if total_trades > 0 else 0.0
            pnl_values = [t.get('pnl_pct', 0) for t in trades]
            avg_pnl = sum(pnl_values) / total_trades if total_trades > 0 else 0.0
            total_pnl = sum(pnl_values)

            # Duration analysis
            durations = [t.get('duration_minutes', 0) for t in trades if t.get('duration_minutes', 0) > 0]
            avg_duration = sum(durations) / len(durations) if durations else 0.0

            # Symbol performance
            symbol_stats = self.analyze_symbol_performance()

            # Performance by time of day
            time_performance = self._analyze_time_performance(trades)

            # Risk metrics
            risk_metrics = self._calculate_risk_metrics(trades)

            # Recommendations
            recommendations = self._generate_recommendations(trades, win_rate, total_pnl, symbol_stats)

            return {
                "total_trades": total_trades,
                "completed_trades": total_trades,
                "win_rate": round(win_rate, 2),
                "avg_pnl": round(avg_pnl, 4),
                "total_pnl": round(total_pnl, 4),
                "avg_duration": round(avg_duration, 1),
                "symbol_stats": symbol_stats,
                "recent_trades": trades[-20:] if trades else [],
                "performance_by_time": time_performance,
                "risk_metrics": risk_metrics,
                "recommendations": recommendations
            }

        except Exception as e:
            print(f"[TradeAnalyzer] Error in comprehensive analysis: {e}")
            return {
                "error": f"Analysis failed: {str(e)}",
                "total_trades": 0,
                "completed_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
                "avg_duration": 0.0,
                "symbol_stats": {},
                "recent_trades": [],
                "performance_by_time": {},
                "risk_metrics": {},
                "recommendations": []
            }

    def _analyze_time_performance(self, trades):
        """Analyze performance by time of day"""
        time_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0, 'wins': 0})

        for trade in trades:
            try:
                from datetime import datetime
                trade_time = datetime.fromisoformat(trade['buy_time'].replace('Z', '+00:00'))
                hour = trade_time.hour
                time_bucket = f"{hour:02d}:00"

                time_stats[time_bucket]['trades'] += 1
                time_stats[time_bucket]['pnl'] += trade['pnl_pct']
                if trade['pnl_pct'] > 0:
                    time_stats[time_bucket]['wins'] += 1
            except:
                continue

        # Calculate win rates
        for time_bucket, stats in time_stats.items():
            if stats['trades'] > 0:
                stats['win_rate'] = (stats['wins'] / stats['trades']) * 100
                stats['avg_pnl'] = stats['pnl'] / stats['trades']

        return dict(time_stats)

    def _calculate_risk_metrics(self, trades):
        """Calculate risk and volatility metrics"""
        if not trades:
            return {}

        pnls = [t['pnl_pct'] for t in trades]

        # Calculate volatility (standard deviation)
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((x - mean_pnl) ** 2 for x in pnls) / len(pnls)
        volatility = variance ** 0.5

        # Max drawdown
        cumulative_pnl = []
        running_total = 0
        for pnl in pnls:
            running_total += pnl
            cumulative_pnl.append(running_total)

        peak = cumulative_pnl[0]
        max_drawdown = 0
        for value in cumulative_pnl:
            if value > peak:
                peak = value
            drawdown = peak - value
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Sharpe ratio approximation (using daily returns)
        sharpe_ratio = (mean_pnl / volatility) if volatility > 0 else 0

        return {
            "volatility": round(volatility, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "best_trade": round(max(pnls), 4),
            "worst_trade": round(min(pnls), 4)
        }

    def _generate_recommendations(self, trades, win_rate, total_pnl, symbol_stats):
        """Generate intelligent recommendations"""
        recommendations = []

        if win_rate < 45:
            recommendations.append({
                "type": "strategy",
                "priority": "high",
                "message": f"Win rate is low at {win_rate:.1f}%. Consider stricter entry conditions.",
                "action": "Increase volume requirements or switch to more conservative parameters"
            })

        if total_pnl < 0:
            recommendations.append({
                "type": "risk_management",
                "priority": "high",
                "message": f"Overall PnL is negative at {total_pnl:.2f}%. Risk management needs improvement.",
                "action": "Reduce position sizes and tighten stop losses"
            })

        # Symbol-specific recommendations
        poor_performers = [s for s, stats in symbol_stats.items()
                          if stats['total_trades'] >= 3 and stats['avg_pnl'] < -0.5]
        if poor_performers:
            recommendations.append({
                "type": "symbol_filter",
                "priority": "medium",
                "message": f"Poor performing symbols detected: {', '.join(poor_performers[:3])}",
                "action": "Consider removing these symbols from watchlist"
            })

        # Duration recommendations
        long_trades = [t for t in trades if t.get('duration_minutes', 0) > 30]
        if len(long_trades) > len(trades) * 0.3:
            recommendations.append({
                "type": "timing",
                "priority": "medium",
                "message": "Many trades are held longer than 30 minutes",
                "action": "Consider tighter exit conditions or shorter max trade time"
            })

        return recommendations

    def analyze_symbol_performance(self):
        """Analyze performance by symbol"""
        trades = self._get_trades_data()
        symbol_stats = defaultdict(lambda: {
            'total_trades': 0, 'wins': 0, 'losses': 0,
            'total_pnl': 0.0, 'avg_pnl': 0.0, 'win_rate': 0.0,
            'best_trade': 0.0, 'worst_trade': 0.0,
            'avg_duration': 0.0
        })

        for trade in trades:
            symbol = trade['symbol']
            pnl = trade['pnl_pct']
            duration = trade.get('duration_minutes', 0)
            stats = symbol_stats[symbol]

            stats['total_trades'] += 1
            stats['total_pnl'] += pnl
            stats['avg_duration'] += duration

            if pnl > 0:
                stats['wins'] += 1
            else:
                stats['losses'] += 1

            if pnl > stats['best_trade']:
                stats['best_trade'] = pnl
            if pnl < stats['worst_trade']:
                stats['worst_trade'] = pnl

        # Calculate derived metrics
        for symbol, stats in symbol_stats.items():
            if stats['total_trades'] > 0:
                stats['avg_pnl'] = stats['total_pnl'] / stats['total_trades']
                stats['win_rate'] = stats['wins'] / stats['total_trades'] * 100
                stats['avg_duration'] = stats['avg_duration'] / stats['total_trades']

        return dict(symbol_stats)

    def get_optimal_conditions(self):
        """Generate recommendations for better buying conditions"""
        trades = self._get_trades_data()
        if len(trades) < 10:
            return {"status": "insufficient_data", "message": "Need at least 10 completed trades for analysis"}

        return self.get_comprehensive_analysis()

# ------------------ Helpers ------------------
def ema(values, period):
    if len(values) < period or period <= 0: return None
    k = 2.0/(period+1.0); e = values[0]
    for v in values[1:]: e = v*k + e*(1-k)
    return e

def round_to(value, step):
    if step == 0: return value
    return math.floor(value/step)*step

def now_utc(): return datetime.now(timezone.utc)

def log_row(row):
    newfile = not os.path.isfile(LOG_FILE)
    with LOG_LOCK:
        with open(LOG_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["time","symbol","action","price","qty","pnl_pct","note","worker_id"])
            w.writerow(row)

def read_csv_tail(path, n=RECENT_TRADES_LIMIT):
    if not os.path.isfile(path):
        return []

    try:
        trades = []
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return []

            # Split into lines and filter out empty lines
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            if len(lines) < 2:  # Need at least header + 1 data row
                return []

            # Parse CSV manually to avoid DictReader issues
            header = lines[0].split(',')

            for i, line in enumerate(lines[1:], 1):
                try:
                    values = line.split(',')
                    if len(values) != len(header):
                        continue

                    # Create row dict
                    row = dict(zip(header, values))

                    # Clean and validate the row data
                    trade = {
                        'time': (row.get('time', '') or '').strip(),
                        'symbol': (row.get('symbol', '') or '').strip(),
                        'action': (row.get('action', '') or '').strip(),
                        'price': (row.get('price', '') or '').strip(),
                        'qty': (row.get('qty', '') or '').strip(),
                        'pnl_pct': (row.get('pnl_pct', '') or '').strip(),
                        'note': (row.get('note', '') or '').strip(),
                        'worker_id': (row.get('worker_id', '') or '').strip()
                    }

                    # Only require essential fields - allow empty pnl_pct for BUY orders
                    if trade['time'] and trade['symbol'] and trade['action'] and trade['price']:
                        trades.append(trade)

                except Exception:
                    continue
        # Return last n trades, newest first
        return trades[-n:][::-1] if trades else []

    except Exception as e:
        print(f"[ERROR] Failed to read CSV {path}: {e}")
        return []

# ------------------ Binance ops ------------------
def build_client():
    load_dotenv()
    key = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_API_SECRET", "")
    testnet_mode = os.getenv("BINANCE_TESTNET", "true").lower()

    print(f"[DEBUG] Environment check:")
    print(f"[DEBUG] - API Key present: {'Yes' if key else 'No'}")
    print(f"[DEBUG] - Secret present: {'Yes' if sec else 'No'}")
    print(f"[DEBUG] - Testnet mode: {testnet_mode}")
    print(f"[DEBUG] - Environment: {'DEPLOYMENT' if os.getenv('REPL_DEPLOYMENT') else 'DEVELOPMENT'}")

    if not key or not sec:
        print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET"); sys.exit(1)

    # Create client with proper testnet configuration
    if testnet_mode in ("1","true","yes","y"):
        client = Client(key, sec, testnet=True)
        print("[INFO] Using BINANCE TESTNET")
    else:
        client = Client(key, sec)
        print("[INFO] Using BINANCE LIVE")

    return client

def get_symbol_filters(client, symbol):
    info = client.get_symbol_info(symbol)
    if not info or info.get("status") != "TRADING":
        raise RuntimeError(f"{symbol} not tradable")
    tick, lot, min_notional = 0.0, 0.0, 0.0
    for f in info["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE":
            lot = float(f["stepSize"])
        elif f["filterType"] == "MARKET_LOT_SIZE":
            # Use MARKET_LOT_SIZE for market orders if available
            lot = float(f["stepSize"])
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            min_notional = float(f.get("minNotional", f.get("notional", 0.0)))

    # Fallback if lot size is 0
    if lot == 0.0:
        lot = 0.00001  # Default small lot size

    print(f"[FILTERS] {symbol}: tick={tick}, lot={lot}, min_notional={min_notional}")
    return tick, lot, min_notional

def get_price(client, symbol): return float(client.get_symbol_ticker(symbol=symbol)["price"])

def get_24h_stats(client, symbol):
    t = client.get_ticker(symbol=symbol)
    last = float(t["lastPrice"]); high = float(t["highPrice"]); low = float(t["lowPrice"])
    move_pct = ((high-low)/low*100.0) if low>0 else 0.0
    return last, move_pct

def get_klines(client, symbol, interval=INTERVAL, limit=KLIMIT):
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return [{
        "open_time": int(k[0]),
        "open":  float(k[1]),
        "high":  float(k[2]),
        "low":   float(k[3]),
        "close": float(k[4]),
        "volume":float(k[5]),
    } for k in raw]

def filter_valid_symbols(client, watchlist):
    info = client.get_exchange_info()
    valid = {s["symbol"] for s in info["symbols"] if s.get("status") == "TRADING"}
    return [s for s in watchlist if s in valid], len(watchlist)

def get_all_prices(client) -> Dict[str, float]:
    return {t["symbol"]: float(t["price"]) for t in client.get_all_tickers()}

# Global rate limiter and WebSocket feed
_RATE_LIMITER = RateLimiter(600)  # 600 requests per minute
_WS_FEED = None

def get_all_prices_cached(client) -> Dict[str, float]:
    global _WS_FEED
    now = time.time()

    # Try WebSocket first if available
    if _WS_FEED:
        ws_prices = {}
        for symbol in WATCHLIST:
            price = _WS_FEED.get_price(symbol)
            if price:
                ws_prices[symbol] = price

        if len(ws_prices) > len(WATCHLIST) * 0.8:  # If we have 80%+ prices from WS
            with PRICES_LOCK:
                _PRICE_CACHE["ts"] = now
                _PRICE_CACHE["prices"] = ws_prices
            return ws_prices

    # Fallback to REST API with rate limiting
    with PRICES_LOCK:
        if now - _PRICE_CACHE["ts"] <= PRICE_CACHE_TTL and _PRICE_CACHE["prices"]:
            return _PRICE_CACHE["prices"]

    _RATE_LIMITER.acquire()
    prices = get_all_prices(client)
    with PRICES_LOCK:
        _PRICE_CACHE["ts"] = now
        _PRICE_CACHE["prices"] = prices
    return prices

def get_price_cached(client, symbol: str) -> float:
    global _WS_FEED

    # Try WebSocket first
    if _WS_FEED:
        price = _WS_feed.get_price(symbol)
        if price:
            return price

    # Fallback to cached REST prices
    max_retries = 3
    for attempt in range(max_retries):
        try:
            prices = get_all_prices_cached(client)
            p = prices.get(symbol)
            if p is not None:
                return p

            # Last resort: direct API call with rate limiting
            _RATE_LIMITER.acquire()
            return get_price(client, symbol)
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Failed to get price for {symbol} after {max_retries} attempts: {e}")
            time.sleep(min(2 ** attempt, 5))  # Exponential backoff

def get_net_usdt_value(client) -> float:
    acct = client.get_account(); prices = get_all_prices_cached(client); total = 0.0
    for b in acct["balances"]:
        asset = b["asset"]; amt = float(b["free"]) + float(b["locked"])
        if amt == 0.0: continue
        if asset == "USDT": total += amt
        else:
            pair = asset + "USDT"; p = prices.get(pair)
            if p: total += amt*p
    return total

# ------------------ Signal Policy ----------------
def make_policy():
    policy = {
        "ema_relax": TUNABLE_PARAMS['ema_relax'] / 100.0,
        "vol_mult": TUNABLE_PARAMS['vol_mult'],
        "min_day_vol": TUNABLE_PARAMS['min_day_volatility_pct'],
        "pattern": "bounce_strong"
    }

    return policy

def evaluate_buy_checks(client, symbol, cache, policy):
    start_time = time.time()

    try:
        # Enhanced features disabled for stability

        # 24h volatility with rate limiting
        _RATE_LIMITER.acquire()
        _, day_move = get_24h_stats(client, symbol)
        day_ok = day_move >= policy["min_day_vol"]

        # klines (cached with TTL)
        candles = cache.get(symbol)
        if candles is None:
            _RATE_LIMITER.acquire()
            candles = get_klines(client, symbol)
            cache.set(symbol, candles)

    except Exception as e:
        return {"ok": False, "reason": f"data_fetch_error: {e}", "day_ok": False,
                "ema_ok": False, "vol_ok": False, "pattern_ok": False}
    if len(candles) < 60:
        return {"ok": False, "reason": "few_candles", "day_ok": day_ok,
                "ema_ok": False, "vol_ok": False, "pattern_ok": False}

    closes = [c["close"] for c in candles]
    vols   = [c["volume"] for c in candles]

    # EMA50 trend
    ema50  = ema(closes[-60:], 50)
    ema_ok = False
    if ema50 is not None:
        ema_ok = (closes[-1] >= ema50 * policy["ema_relax"]) and (ema(closes[-61:-1], 50) is None or ema50 >= ema(closes[-61:-1], 50))

    # Volume
    vol_ok = False
    if len(vols) >= 11:
        last_closed_vol = vols[-2]  # last CLOSED bar
        avg10 = sum(vols[-12:-2]) / 10.0
        # Fast mode: soft threshold OR top-3 rank
        cond_soft = (avg10 > 0) and (last_closed_vol >= policy["vol_mult"] * avg10)
        block = vols[-12:-2]
        top3 = sorted(block, reverse=True)[:3] if block else []
        cond_rank = bool(block) and (last_closed_vol >= (top3[-1] if len(top3)==3 else (top3[-1] if top3 else 0)))
        vol_ok = bool(cond_soft or cond_rank)

    # Pattern - fast mode bounce (more permissive)
    pattern_ok = False
    if len(candles) >= 2:
        last_closed = candles[-2]
        # Fast mode: just need a green body (bullish candle)
        pattern_ok = last_closed["close"] > last_closed["open"]

    # Reason
    if not day_ok:      reason = "low_24h_move"
    elif not ema_ok:    reason = "below_ema50"
    elif not vol_ok:    reason = "no_vol_spike"
    elif not pattern_ok:reason = "no_pullback_recovery"
    else:               reason = "ok"

    return {"ok": (reason == "ok"), "reason": reason,
            "day_ok": day_ok, "ema_ok": ema_ok, "vol_ok": vol_ok, "pattern_ok": pattern_ok}

# ------------------ Orders ----------------------
def market_buy_by_quote(client, symbol, quote_usdt):
    max_retries = 3

    # Balance validation - check only free USDT (fees are deducted from received amount)
    for balance_check in range(3):
        try:
            account = client.get_account()
            usdt_balance = 0.0
            for balance in account['balances']:
                if balance['asset'] == 'USDT':
                    usdt_balance = float(balance['free'])  # Only free USDT
                    break

            # Direct comparison - no buffer needed as fees come from received amount
            if usdt_balance < float(quote_usdt):
                raise RuntimeError(f"Insufficient USDT balance. Available: {usdt_balance:.2f}, Required: {quote_usdt}")

            print(f"[BUY] Balance check OK: {usdt_balance:.2f} free USDT available")
            break

        except Exception as balance_error:
            if "Insufficient" in str(balance_error):
                raise balance_error
            if balance_check == 2:
                print(f"[BUY] Warning - Could not verify balance after 3 attempts: {balance_error}")
                break
            time.sleep(1)

    for attempt in range(max_retries):
        try:
            price = get_price_cached(client, symbol)
            tick, lot, min_notional = get_symbol_filters(client, symbol)
            min_req = max(10.0, min_notional)  # No buffer needed
            spend = max(float(quote_usdt), min_req)

            print(f"[BUY] Attempting to buy {symbol} with {spend:.2f} USDT at ~{price:.6f}")
            print(f"[FILTERS] {symbol}: tick={tick}, lot={lot}, min_notional={min_notional}")

            try:
                # Try quoteOrderQty first (preferred for market orders)
                order = client.create_order(symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=round(spend, 2))
                print(f"[BUY] Success using quoteOrderQty method")
            except Exception as quote_error:
                print(f"[BUY] QuoteOrderQty failed for {symbol}, using quantity method: {quote_error}")

                # INTEGER-BASED LOT CALCULATION (same as sell method)
                raw_qty = spend / price

                if lot > 0:
                    # Calculate lot units using integer division to avoid floating-point errors
                    lot_units = int(raw_qty / lot)

                    # Calculate exact quantity by multiplying back
                    qty = lot_units * lot

                    # Determine precision from lot size for proper formatting
                    if lot >= 1:
                        precision = 0
                    elif lot >= 0.1:
                        precision = 1
                    elif lot >= 0.01:
                        precision = 2
                    elif lot >= 0.001:
                        precision = 3
                    elif lot >= 0.0001:
                        precision = 4
                    elif lot >= 0.00001:
                        precision = 5
                    else:
                        precision = 8

                    print(f"[BUY_INTEGER] Raw qty: {raw_qty:.8f}, Lot: {lot}")
                    print(f"[BUY_INTEGER] Lot units: {lot_units}, Final qty: {qty:.8f}")

                    # Verification - should be exactly zero remainder
                    remainder_check = qty % lot
                    print(f"[BUY_VERIFY] Remainder check: {remainder_check:.20f}")

                else:
                    qty = raw_qty
                    precision = 8  # Default precision

                if qty <= 0:
                    raise RuntimeError(f"Quantity rounds to 0. Raw: {raw_qty:.8f}, Lot: {lot}, Calculated: {qty}")

                # Verify minimum notional
                trade_value = price * qty
                if trade_value < min_notional:
                    raise RuntimeError(f"Trade value {trade_value:.2f} below minimum {min_notional:.2f}")

                # Format quantity as string with proper precision (never send as float)
                if precision == 0:
                    qty_str = str(int(qty))
                else:
                    qty_str = f"{qty:.{precision}f}".rstrip('0').rstrip('.')
                    if not qty_str or qty_str == '':
                        qty_str = f"{qty:.{precision}f}"

                print(f"[BUY] Using quantity string: {qty_str} (value: {trade_value:.2f} USDT)")
                order = client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty_str)
                print(f"[BUY] Success using integer-based quantity method")
            break

        except Exception as e:
            error_msg = str(e).lower()
            if "insufficient balance" in error_msg:
                raise RuntimeError(f"Insufficient balance confirmed by exchange: {e}")
            elif "lot_size" in error_msg:
                print(f"[WARN] LOT_SIZE error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    # Try with slightly different quantity calculation
                    time.sleep(1)
                    continue
            elif "precision" in error_msg:
                print(f"[WARN] Precision error on attempt {attempt + 1}: {e}")

            if attempt == max_retries - 1:
                raise RuntimeError(f"Failed to execute buy order for {symbol} after {max_retries} attempts: {e}")
            print(f"[WARN] Buy attempt {attempt + 1} failed for {symbol}: {e}")
            time.sleep(min(2 ** attempt, 3))

    # Process the order response
    fills = order.get("fills", [])
    if fills:
        spent_total = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        got_qty = sum(float(f["qty"]) for f in fills)
        avg_price = spent_total / got_qty if got_qty > 0 else price
        qty = got_qty
        print(f"[BUY] Success: Got {qty:.8f} {symbol.replace('USDT', '')} for {spent_total:.2f} USDT (avg price: {avg_price:.6f})")
    else:
        qty = spend / price
        avg_price = price
        print(f"[BUY] Fallback calculation: {qty:.8f} {symbol.replace('USDT', '')} at {avg_price:.6f}")

    return avg_price, qty

def market_sell_qty(client, symbol, qty):
    """
    Sell available quantity using improved lot size calculation to handle Binance filter requirements.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Use the worker's tracked quantity instead of account balance to avoid conflicts
            available_qty = qty

            # Verify we have some balance (safety check)
            asset = symbol.replace('USDT', '')
            account = client.get_account()
            account_balance = 0.0

            for balance in account['balances']:
                if balance['asset'] == asset:
                    account_balance = float(balance['free'])
                    break

            if account_balance <= 0:
                raise RuntimeError(f"No {asset} balance available to sell")

            # Use the minimum of tracked quantity and account balance for safety
            available_qty = min(available_qty, account_balance)

            if available_qty <= 0:
                raise RuntimeError(f"No {asset} quantity available to sell")

            # Get symbol filters with retry
            tick, lot, min_notional = get_symbol_filters(client, symbol)

            # Get current market lot size (may differ from stored filters)
            try:
                symbol_info = client.get_symbol_info(symbol)
                market_lot_size = None
                for filter_item in symbol_info.get('filters', []):
                    if filter_item.get('filterType') == 'MARKET_LOT_SIZE':
                        market_lot_size = float(filter_item.get('stepSize', lot))
                        break
                
                # Use market lot size if available, otherwise use regular lot size
                effective_lot = market_lot_size if market_lot_size else lot
                print(f"[LOT_CHECK] Symbol: {symbol}, Regular lot: {lot}, Market lot: {market_lot_size}, Using: {effective_lot}")
            except Exception as filter_error:
                print(f"[LOT_CHECK] Could not get market lot size, using regular: {filter_error}")
                effective_lot = lot

            # Enhanced LOT_SIZE calculation using Decimal for precision
            if effective_lot > 0:
                from decimal import Decimal, ROUND_DOWN
                
                # Convert to Decimal for precise calculations
                available_decimal = Decimal(str(available_qty))
                lot_decimal = Decimal(str(effective_lot))
                
                # Calculate lot units using floor division
                lot_units = int(available_decimal / lot_decimal)
                actual_qty_decimal = lot_units * lot_decimal
                actual_qty = float(actual_qty_decimal)

                # Determine precision from lot size with better logic
                if effective_lot >= 1:
                    precision = 0
                elif effective_lot >= 0.1:
                    precision = 1
                elif effective_lot >= 0.01:
                    precision = 2
                elif effective_lot >= 0.001:
                    precision = 3
                elif effective_lot >= 0.0001:
                    precision = 4
                elif effective_lot >= 0.00001:
                    precision = 5
                elif effective_lot >= 0.000001:
                    precision = 6
                elif effective_lot >= 0.0000001:
                    precision = 7
                else:
                    precision = 8

                print(f"[DECIMAL_LOT] Available: {available_qty:.8f}, Effective lot: {effective_lot}")
                print(f"[DECIMAL_LOT] Lot units: {lot_units}, Sellable qty: {actual_qty:.8f}")
                print(f"[DECIMAL_LOT] Decimal check: {actual_qty_decimal}")

            else:
                actual_qty = available_qty
                precision = 8

            if actual_qty <= 0:
                raise RuntimeError(f"Quantity rounds to 0 after lot size adjustment. Available: {available_qty:.8f}, Lot: {effective_lot}")

            # Check minimum notional value
            price = get_price_cached(client, symbol)
            trade_value = price * actual_qty

            if trade_value < min_notional:
                raise RuntimeError(f"Trade value {trade_value:.2f} below minimum {min_notional:.2f}. Cannot proceed with sale.")

            # Format quantity as string with proper precision
            if precision == 0:
                qty_str = str(int(actual_qty))
            else:
                qty_str = f"{actual_qty:.{precision}f}".rstrip('0').rstrip('.')
                if not qty_str or qty_str == '.':
                    qty_str = f"{actual_qty:.{precision}f}"

            print(f"[SELL] Executing order: {qty_str} {asset} (value: {trade_value:.2f} USDT)")

            # Execute the order
            order = client.create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty_str
            )

            print(f"[SELL] Order executed successfully with quantity: {qty_str}")
            break

        except Exception as e:
            error_msg = str(e).lower()

            if "lot_size" in error_msg and "code=-1013" in str(e):
                print(f"[ERROR] LOT_SIZE filter failure on attempt {attempt + 1}: {e}")
                
                if attempt == max_retries - 1:
                    # Final attempt: try with reduced quantity
                    try:
                        print(f"[FINAL_ATTEMPT] Trying with 99% of calculated quantity...")
                        reduced_qty = actual_qty * 0.99
                        if effective_lot > 0:
                            reduced_units = int(reduced_qty / effective_lot)
                            final_qty = reduced_units * effective_lot
                        else:
                            final_qty = reduced_qty

                        if final_qty > 0:
                            if precision == 0:
                                final_qty_str = str(int(final_qty))
                            else:
                                final_qty_str = f"{final_qty:.{precision}f}".rstrip('0').rstrip('.')
                                if not final_qty_str or final_qty_str == '.':
                                    final_qty_str = f"{final_qty:.{precision}f}"

                            print(f"[FINAL_ATTEMPT] Trying with {final_qty_str} {asset}")
                            order = client.create_order(
                                symbol=symbol,
                                side="SELL",
                                type="MARKET",
                                quantity=final_qty_str
                            )
                            print(f"[FINAL_ATTEMPT] Success with reduced quantity: {final_qty_str}")
                            break
                        else:
                            print(f"[FINAL_ATTEMPT] Reduced quantity is 0, cannot proceed")
                            # Mark position as stuck and continue
                            print(f"[WARNING] Position stuck due to LOT_SIZE constraints, will retry later")
                            return price, 0  # Return 0 quantity to indicate failure
                    except Exception as final_error:
                        print(f"[FINAL_ATTEMPT] Failed: {final_error}")
                        
                        # Emergency mode: Try selling with minimum lot size
                        try:
                            print(f"[EMERGENCY] Attempting emergency sell with minimum lot size...")
                            emergency_qty = effective_lot * 100  # Try 100 lot units minimum
                            if emergency_qty <= available_qty:
                                if precision == 0:
                                    emergency_qty_str = str(int(emergency_qty))
                                else:
                                    emergency_qty_str = f"{emergency_qty:.{precision}f}".rstrip('0').rstrip('.')
                                
                                print(f"[EMERGENCY] Trying emergency sell: {emergency_qty_str} {asset}")
                                order = client.create_order(
                                    symbol=symbol,
                                    side="SELL",
                                    type="MARKET",
                                    quantity=emergency_qty_str
                                )
                                print(f"[EMERGENCY] Emergency sell successful!")
                                return price, emergency_qty
                            else:
                                print(f"[EMERGENCY] Emergency quantity too large")
                        except Exception as emergency_error:
                            print(f"[EMERGENCY] Emergency sell failed: {emergency_error}")
                        
                        # Mark position as stuck
                        print(f"[WARNING] Position stuck due to LOT_SIZE constraints")
                        return price, 0

            elif "insufficient" in error_msg:
                print(f"[ERROR] Insufficient balance: {e}")
                break
            else:
                print(f"[ERROR] Attempt {attempt + 1} failed: {e}")

            if attempt == max_retries - 1:
                print(f"[ERROR] All sell attempts failed for {symbol}, marking position as stuck")
                return price, 0

            time.sleep(min(2 ** attempt, 3))

    # Process the order response
    fills = order.get("fills", [])
    if fills:
        earned = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        sold = sum(float(f["qty"]) for f in fills)
        avg_price = earned / sold if sold > 0 else price
        final_qty = sold
        print(f"[SELL] Success: Sold {final_qty:.8f} {asset} for {earned:.2f} USDT (avg price: {avg_price:.6f})")
    else:
        # Fallback calculation if no fills data
        avg_price = price
        final_qty = actual_qty
        print(f"[SELL] Fallback calculation: {final_qty:.8f} {asset} at {avg_price:.6f}")

    return avg_price, final_qty

# ------------------ Multi-Worker ----------------
class WorkerState:
    def __init__(self, wid: int, quote: float):
        self.id = wid; self.quote = quote
        self.status = "scanning"
        self.symbol = None; self.last_pnl = None
        self.note = "Scanning watchlist…"; self.updated = now_utc().isoformat()
        # live-trade context for UI
        self.entry_price = None
        self.qty = None
        self.started = None  # datetime

class CircuitBreaker:
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"  # closed, open, half_open
        self._lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        with self._lock:
            if self.state == "open":
                if time.time() - self.last_failure_time > self.timeout:
                    self.state = "half_open"
                else:
                    raise RuntimeError("Circuit breaker is OPEN")

            try:
                result = func(*args, **kwargs)
                if self.state == "half_open":
                    self.state = "closed"
                    self.failure_count = 0
                return result
            except Exception as e:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold:
                    self.state = "open"
                raise e

class FastCycleBot:
    def __init__(self):
        print("=== INITIALIZING BOT CORE ===")
        print("Connecting to Binance client...")
        self._client = build_client()
        print("✓ Binance client connected to TESTNET")

        print("Starting WebSocket price feed...")
        self._ws_feed = WebSocketPriceFeed(WATCHLIST)
        self._ws_feed.start()
        print("✓ WebSocket price feed started")

        watchlist_full = WATCHLIST
        valid_symbols, total_symbols = filter_valid_symbols(self._client, watchlist_full)
        self.watchlist = valid_symbols
        self._watchlist_total = total_symbols
        self._watchlist_count = len(self.watchlist)
        print(f"✓ Watchlist filtered: {self._watchlist_count} valid symbols (from {total_symbols})")

        # Worker tracking
        self._workers: Dict[int, threading.Thread] = {}
        self._worker_state: Dict[int, WorkerState] = {}
        self._stop_flags: Dict[int, threading.Event] = {}
        self._lock = threading.RLock()  # Use RLock for nested locking
        self._active_symbols = set()
        self._candles_cache = ThreadSafeCacheManager(CANDLES_CACHE_TTL)
        self._last_sell_time = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))
        self._running = False
        self._max_workers = 10  # Limit concurrent workers to prevent resource exhaustion
        self.start_net_usdt = None
        self.current_net_usdt = None

        self._metrics_thread = None
        self._metrics_stop = threading.Event()

        self.debug_enabled = True
        self._debug_events = deque(maxlen=DEBUG_BUFFER)

        # Enhanced error handling and monitoring
        self._api_circuit_breaker = CircuitBreaker(failure_threshold=3, timeout=30)
        self.trade_analyzer = TradeAnalyzer(LOG_FILE)
        self._error_counts = defaultdict(int)
        self._last_error_time = defaultdict(float)
        self._performance_metrics = {
            'total_trades': 0,
            'successful_trades': 0,
            'failed_trades': 0,
            'avg_trade_duration': 0,
            'api_errors': 0,
            'uptime_start': time.time(),
            'total_buy_orders': 0,
            'total_sell_orders': 0,
            'total_profit_usd': 0.0
        }

        # Clear any existing debug events for fresh start
        self._debug_events.clear()
        print("✓ Worker management initialized")

        # Enable console capture for dashboard
        global console_capture
        console_capture.enable_capture()
        sys.stdout = console_capture
        sys.stderr = console_capture

        # Portfolio tracking
        try:
            self.start_net_usdt = get_net_usdt_value(self._client)
            self.current_net_usdt = self.start_net_usdt
            print(f"✓ Initial balance: {self.current_net_usdt:,.2f} USDT")
        except Exception as e:
            print(f"⚠ Failed to get account balance: {e}")
            self.start_net_usdt = self.current_net_usdt = None

        print("=== BOT CORE INITIALIZED SUCCESSFULLY ===")
        print("Dashboard is ready to accept requests")


    # ---- lifecycle ----
    def start_core(self):
        if self._running:
            print("[INFO] Core already running")
            return

        print("[INFO] Starting bot core...")
        try:
            # Test client connection
            try:
                account_info = self._client.get_account()
                print("[INFO] Successfully connected to Binance API")
            except Exception as e:
                print(f"[ERROR] Failed to connect to Binance API: {e}")
                self._running = False
                return

            # Get initial balance with retries
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    self.start_net_usdt = get_net_usdt_value(self._client)
                    print(f"[INFO] Initial balance: {self.start_net_usdt:.2f} USDT")
                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"[WARN] Could not fetch start net value after {max_retries} attempts: {e}")
                        self.start_net_usdt = None
                    else:
                        print(f"[WARN] Balance fetch attempt {attempt + 1} failed: {e}")
                        time.sleep(2)
            self.current_net_usdt = self.start_net_usdt

            self._metrics_stop.clear()
            self._metrics_thread = threading.Thread(target=self._refresh_metrics_loop, daemon=True)
            self._metrics_thread.start()
            self._performance_metrics['uptime_start'] = time.time()
            self._running = True
            print("[INFO] Trading bot core started successfully")

        except Exception as e:
            print(f"[ERROR] Failed to start core: {e}")
            import traceback
            traceback.print_exc()
            self._running = False

    def stop_core(self):
        # Signal all workers to stop
        with self._lock:
            stop_flags_copy = dict(self._stop_flags)
        for wid, ev in stop_flags_copy.items():
            ev.set()

        # Wait for threads to finish
        workers_copy = dict(self._workers)
        for wid, thread in workers_copy.items():
            if thread.is_alive():
                thread.join(timeout=5.0)

        # Clean up state safely
        with self._lock:
            self._workers.clear()
            self._stop_flags.clear()
            self._worker_state.clear()
            self._active_symbols.clear()

        self._running = False
        self._metrics_stop.set()
        if self._ws_feed: self._ws_feed.stop()

    def _refresh_metrics_loop(self):
        consecutive_failures = 0
        last_balance_time = 0
        while not self._metrics_stop.is_set():
            try:
                current_time = time.time()
                # Always try to fetch balance if we have a client, regardless of running state
                if self._client and (current_time - last_balance_time) >= 30:  # More frequent updates (30s)
                    try:
                        new_balance = get_net_usdt_value(self._client)
                        self.current_net_usdt = new_balance
                        last_balance_time = current_time
                        consecutive_failures = 0
                        print(f"[METRICS] Balance updated: {new_balance:.2f} USDT")
                    except Exception as e:
                        consecutive_failures += 1
                        error_msg = str(e)
                        if "Invalid API-key" in error_msg or "permissions" in error_msg:
                            print(f"[WARN] API permission error - balance fetch failed: {error_msg}")
                        else:
                            print(f"[WARN] Balance fetch failed (attempt {consecutive_failures}): {e}")

                        if consecutive_failures > 5:
                            print("[WARN] Too many balance fetch failures, pausing updates")
                            time.sleep(120)  # Wait 2 minutes before retrying
                            consecutive_failures = 0
            except Exception:
                pass
            time.sleep(5)  # More frequent checks for better responsiveness

    def _eligible_symbol(self, symbol: str) -> bool:
        """Check if symbol is eligible for trading (not on cooldown)"""
        if symbol not in self._last_sell_time:
            return True

        cooldown_end = self._last_sell_time[symbol] + timedelta(minutes=TUNABLE_PARAMS['cooldown_minutes'])
        return now_utc() >= cooldown_end

    def _debug_push(self, symbol, wid, flags):
        if not self.debug_enabled or not flags: return
        try:
            # Only store important events to save memory
            reason = flags.get("reason", "unknown")
            if reason in ["ok", "data_fetch_error"] or (flags.get("day_ok") and flags.get("ema_ok")):
                event = {
                    "time": now_utc().isoformat(),
                    "symbol": symbol, "worker_id": wid, "reason": reason,
                    "day_ok": flags.get("day_ok", False), "ema_ok": flags.get("ema_ok", False),
                    "vol_ok": flags.get("vol_ok", False), "pattern_ok": flags.get("pattern_ok", False),
                }
                self._debug_events.append(event)
        except Exception:
            pass

    # ---- workers ----
    def add_worker(self, quote_amount: float) -> int:
        if not self._running:
            print("[WARN] Bot core not running, attempting to start...")
            self.start_core()
            if not self._running:
                raise RuntimeError("Failed to start bot core.")

        # Enhanced balance validation with minimum quote amount enforcement
        try:
            account = self._client.get_account()
            usdt_balance = 0.0
            for balance in account['balances']:
                if balance['asset'] == 'USDT':
                    usdt_balance = float(balance['free'])  # Only free USDT, not locked
                    break

            # Enforce minimum viable quote amount (Binance minimum notional is usually 5-10 USDT)
            min_quote = 10.0
            if quote_amount < min_quote:
                quote_amount = min_quote
                print(f"[Bot] Quote amount adjusted to minimum: {quote_amount:.2f} USDT")

            # Calculate current total allocation from existing workers (excluding those in position)
            active_allocation = 0.0
            scanning_allocation = 0.0
            
            for worker in self._worker_state.values():
                if worker.status in ["in_position", "buying", "selling", "closing"]:
                    # These workers have already spent their allocation
                    continue
                elif worker.status in ["scanning", "stopped", "error", "paused"]:
                    # These workers still have their allocation available
                    scanning_allocation += worker.quote
                    
            # Total that would be needed if all scanning workers plus new worker tried to buy
            total_potential_allocation = scanning_allocation + quote_amount

            # Check if new worker would exceed available balance
            if total_potential_allocation > usdt_balance:
                raise RuntimeError(f"Cannot create worker: Total potential allocation would be {total_potential_allocation:.2f} USDT but only {usdt_balance:.2f} USDT available. Currently scanning: {scanning_allocation:.2f} USDT")

            # Individual worker amount check with buffer
            if usdt_balance < quote_amount + 2.0:  # Add 2 USDT buffer for fees
                raise RuntimeError(f"Insufficient free USDT balance. Available: {usdt_balance:.2f} USDT, Required: {quote_amount + 2.0:.2f} USDT (including fee buffer)")

            print(f"[Bot] Balance check OK: {usdt_balance:.2f} free USDT available, {scanning_allocation:.2f} allocated to scanning workers, {quote_amount:.2f} requested")

        except Exception as e:
            if "Insufficient" in str(e) or "Cannot create worker" in str(e):
                raise e
            else:
                raise RuntimeError(f"Cannot verify USDT balance: {e}")

        with self._lock:
            # Check worker limit
            if len(self._workers) >= self._max_workers:
                raise RuntimeError(f"Maximum workers ({self._max_workers}) reached. Stop some workers first.")

            # Generate unique worker ID with time-based suffix to prevent race conditions
            wid = 1
            while wid in self._workers or wid in self._worker_state:
                wid += 1

            # Additional check: wait if a worker with this ID was recently stopped
            max_wait_attempts = 10
            for attempt in range(max_wait_attempts):
                if wid not in self._workers and wid not in self._worker_state and wid not in self._stop_flags:
                    break
                print(f"[Bot] Worker ID {wid} still cleaning up, waiting... (attempt {attempt + 1})")
                time.sleep(0.2)
                wid += 1  # Try next ID if current one is still being cleaned up
            state = WorkerState(wid, float(quote_amount))
            self._worker_state[wid] = state
            stop_ev = threading.Event(); self._stop_flags[wid] = stop_ev
            t = threading.Thread(target=self._worker_loop, args=(wid, stop_ev), daemon=True)
            self._workers[wid] = t; t.start()
            print(f"[Bot] Worker {wid} added with ${quote_amount} quote.")
        return wid

    def stop_worker(self, wid: int):
        ev = self._stop_flags.get(wid)
        st = self._worker_state.get(wid)

        if not ev or not st:
            print(f"[Bot] Worker {wid} not found for stopping.")
            return

        # If in position, show closing status and wait for natural exit
        if st.status in ["in_position", "closing"] and st.symbol and st.qty and st.entry_price:
            print(f"[Bot] Worker {wid} in position, setting closing status...")
            self._update_state(wid, status="closing", note=f"Closing position on {st.symbol}...")
            # Signal stop but keep worker alive until position closes naturally
            ev.set()
            print(f"[Bot] Worker {wid} will close position naturally and then stop.")

        else:
            # Signal stop and remove card data structures immediately if not in position
            ev.set()
            # Mark for cleanup to prevent immediate reuse
            self._update_state(wid, status="stopping", note="Cleaning up...")

            # Clean up in a separate thread to avoid blocking and ensure proper cleanup
            def cleanup_worker():
                time.sleep(0.5)  # Small delay to ensure worker loop sees the stop signal
                with self._lock:
                    self._workers.pop(wid, None)
                    self._stop_flags.pop(wid, None)
                    self._worker_state.pop(wid, None)
                print(f"[Bot] Worker {wid} cleaned up successfully.")

            cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
            cleanup_thread.start()
            print(f"[Bot] Worker {wid} stop signal sent, cleanup in progress.")


    def _update_state(self, wid: int, **kwargs):
        st = self._worker_state.get(wid)
        if not st: return
        for k, v in kwargs.items(): setattr(st, k, v)
        st.updated = now_utc().isoformat()

    def _worker_loop(self, wid: int, stop_ev: threading.Event):
        st = self._worker_state[wid]; client = self._client
        import random

        print(f"[Worker-{wid}] Started with ${st.quote}")

        def get_randomized_watchlist():
            """Get a new randomized watchlist excluding cooldown symbols"""
            available_symbols = []
            for symbol in self.watchlist:
                if self._eligible_symbol(symbol):
                    available_symbols.append(symbol)
            random.shuffle(available_symbols)
            return available_symbols

        while not stop_ev.is_set():
            current_state = self._worker_state.get(wid)
            if not current_state:
                print(f"[Worker-{wid}] State removed, exiting loop")
                break

            # Get fresh randomized watchlist for scanning
            worker_watchlist = get_randomized_watchlist()
            if not worker_watchlist:
                print(f"[Worker-{wid}] No eligible symbols, waiting...")
                time.sleep(5)
                continue

            print(f"[Worker-{wid}] Starting scan cycle with {len(worker_watchlist)} eligible symbols")

            found_buy_signal = False

            # Scan through the randomized list
            for symbol in worker_watchlist:
                # Check if worker should stop (if scanning, stop immediately)
                if stop_ev.is_set():
                    print(f"[Worker-{wid}] Stop requested during scanning, exiting...")
                    with self._lock:
                        self._workers.pop(wid, None)
                        self._stop_flags.pop(wid, None)
                        self._worker_state.pop(wid, None)
                    return

                # Check if symbol is still eligible (might have been traded recently)
                if not self._eligible_symbol(symbol):
                    continue

                # Try to reserve this symbol for scanning
                symbol_reserved = False
                with self._lock:
                    if symbol not in self._active_symbols:
                        self._active_symbols.add(symbol)
                        symbol_reserved = True

                if not symbol_reserved:
                    continue  # Skip if another worker is already using this symbol

                try:
                    # Update status to show which symbol we're scanning
                    self._update_state(wid, status="scanning", symbol=symbol, note=f"Scanning {symbol}")

                    # Evaluate buy conditions
                    policy = make_policy()
                    flags = evaluate_buy_checks(client, symbol, self._candles_cache, policy)

                    if flags["ok"]:
                        print(f"[Worker-{wid}] 🎯 BUY SIGNAL for {symbol}")
                        found_buy_signal = True

                        # Execute buy order
                        self._update_state(wid, status="buying", symbol=symbol, note=f"Buying {symbol}")
                        try:
                            entry, qty = market_buy_by_quote(client, symbol, st.quote)
                            start = now_utc()
                            log_row([start.isoformat(), symbol, "BUY", f"{entry:.8f}", f"{qty:.8f}", "", f"Worker {wid} buy signal", wid])
                            print(f"[BUY] W{wid} {symbol} @ {entry:.6f} | Qty: {qty:.4f}")
                            self._performance_metrics['total_buy_orders'] += 1
                        except Exception as buy_error:
                            error_msg = str(buy_error)
                            print(f"[ERROR] W{wid} Buy failed for {symbol}: {buy_error}")
                            
                            # If insufficient balance, pause this worker to prevent spam
                            if "Insufficient" in error_msg:
                                print(f"[WORKER-{wid}] Insufficient balance detected, pausing for 60 seconds...")
                                self._update_state(wid, status="paused", note=f"Insufficient balance, pausing...")
                                with self._lock:
                                    self._active_symbols.discard(symbol)
                                time.sleep(60)  # Pause for 1 minute
                                break  # Exit scanning loop
                            else:
                                self._update_state(wid, status="error", note=f"Buy failed: {buy_error}")
                                with self._lock:
                                    self._active_symbols.discard(symbol)
                                break  # Exit scanning loop to get new watchlist

                        # Store trade context
                        st.symbol = symbol
                        st.entry_price = entry
                        st.qty = qty
                        st.started = start

                        # Calculate exit levels
                        hard_tp = entry * (1 + TUNABLE_PARAMS['take_profit_pct'] / 100.0)
                        trail_arm = entry * (1 + TUNABLE_PARAMS['trail_arm_pct'] / 100.0)
                        peak = entry
                        trailing = False

                        # Enter position monitoring
                        self._update_state(wid, status="in_position", note=f"In position {symbol}")
                        position_exit_reason = None

                        # Position monitoring loop
                        while position_exit_reason is None:
                            try:
                                price = get_price_cached(client, symbol)
                                ts = now_utc()
                                if price > peak:
                                    peak = price
                            except Exception as price_error:
                                print(f"[Worker-{wid}] Price fetch error for {symbol}: {price_error}")
                                time.sleep(2)
                                continue

                            # Check if worker stop requested while in position
                            if stop_ev.is_set():
                                self._update_state(wid, status="closing", note=f"Closing {symbol} on stop request...")

                            # Immediate take-profit (simplified logic)
                            if price >= hard_tp and not trailing:
                                self._update_state(wid, status="selling", note=f"Take profit hit")
                                exitp, sold = market_sell_qty(client, symbol, qty)
                                pnl = (exitp/entry - 1)*100.0
                                profit_usd = (exitp - entry) * sold
                                log_row([ts.isoformat(), symbol, "SELL_TP", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", f"Worker {wid} take-profit", wid])
                                print(f"[SELL] W{wid} {symbol} TP @ {exitp:.6f} | P&L: {pnl:+.2f}%")
                                self._last_sell_time[symbol] = now_utc()
                                self._update_state(wid, last_pnl=pnl)
                                self._performance_metrics['total_sell_orders'] += 1
                                self._performance_metrics['total_profit_usd'] += profit_usd
                                position_exit_reason = "take_profit"
                                break

                            # Trail arming
                            elif not trailing and price >= trail_arm:
                                trailing = True
                                self._update_state(wid, note=f"Trailing armed")

                            # Stop-loss
                            stop_loss_threshold = entry * (1.0 - TUNABLE_PARAMS['stop_loss_pct'] / 100.0)
                            if price <= stop_loss_threshold:
                                self._update_state(wid, status="selling", note=f"Stop-loss")
                                exitp, sold = market_sell_qty(client, symbol, qty)
                                pnl = (exitp/entry - 1)*100.0
                                profit_usd = (exitp - entry) * sold
                                log_row([ts.isoformat(), symbol, "SELL_SL", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", f"Worker {wid} stop-loss", wid])
                                print(f"[SELL] W{wid} {symbol} SL @ {exitp:.6f} | P&L: {pnl:+.2f}%")
                                self._last_sell_time[symbol] = now_utc()
                                self._update_state(wid, last_pnl=pnl)
                                self._performance_metrics['total_sell_orders'] += 1
                                self._performance_metrics['total_profit_usd'] += profit_usd
                                position_exit_reason = "stop_loss"
                                break

                            # Trailing stop
                            elif trailing and price <= peak * (1.0 - TUNABLE_PARAMS['trail_giveback_pct'] / 100.0):
                                self._update_state(wid, status="selling", note=f"Trailing stop")
                                exitp, sold = market_sell_qty(client, symbol, qty)
                                pnl = (exitp/entry - 1)*100.0
                                profit_usd = (exitp - entry) * sold
                                log_row([ts.isoformat(), symbol, "SELL_TRAIL", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", f"Worker {wid} trailing stop", wid])
                                print(f"[SELL] W{wid} {symbol} TRAIL @ {exitp:.6f} | P&L: {pnl:+.2f}%")
                                self._last_sell_time[symbol] = now_utc()
                                self._update_state(wid, last_pnl=pnl)
                                self._performance_metrics['total_sell_orders'] += 1
                                self._performance_metrics['total_profit_usd'] += profit_usd
                                position_exit_reason = "trailing_stop"
                                break

                            time.sleep(POLL_SECONDS_ACTIVE)

                        # Clear trade context after position exit
                        st.entry_price = None
                        st.qty = None
                        st.started = None
                        st.symbol = None

                        # If stop was requested, clean up and exit
                        if stop_ev.is_set():
                            print(f"[Worker-{wid}] Position closed ({position_exit_reason}), stopping worker")
                            with self._lock:
                                self._active_symbols.discard(symbol)
                                self._workers.pop(wid, None)
                                self._stop_flags.pop(wid, None)
                                self._worker_state.pop(wid, None)
                            return

                        # Release symbol and break from scanning loop to get new watchlist
                        with self._lock:
                            self._active_symbols.discard(symbol)
                        print(f"[Worker-{wid}] Trade completed, getting new watchlist...")
                        break

                    else:
                        # No buy signal, continue scanning
                        self._debug_push(symbol, wid, flags)

                except Exception as e:
                    print(f"[Worker-{wid}] Error analyzing {symbol}: {e}")
                    self._update_state(wid, status="error", note=f"Analysis error: {e}")

                finally:
                    # Always release symbol reservation after scanning
                    if symbol_reserved:
                        with self._lock:
                            # Only release if we're not in a position with this symbol
                            if st.symbol != symbol or st.status not in ["in_position", "buying", "selling", "closing"]:
                                self._active_symbols.discard(symbol)

                # 1.5 second pause between symbol scans as requested
                time.sleep(1.5)

            # If we finished scanning the entire list without finding a buy signal
            if not found_buy_signal and not stop_ev.is_set():
                print(f"[Worker-{wid}] Completed scan cycle, no signals found. Getting new watchlist...")
                self._update_state(wid, status="scanning", symbol=None, note="Completed cycle, getting new list...")
                time.sleep(2)  # Brief pause before next cycle

        print(f"[Worker-{wid}] Stopped")
        self._update_state(wid, status="stopped", note="Stopped")


    # ---- status for UI ----
    def dashboard_state(self):
        start_val = self.start_net_usdt; cur_val = self.current_net_usdt
        profit_usd = profit_pct = None
        if start_val is not None and cur_val is not None:
            profit_usd = cur_val - start_val
            profit_pct = (profit_usd / start_val * 100.0) if start_val > 0 else None

        workers = []
        for wid, st in self._worker_state.items():
            # defaults
            unreal_pct = unreal_usd = None
            cur_price = tp_price = trail_arm_price = sl_price = None
            started_iso = st.started.isoformat() if st.started else None

            # compute live metrics if in position or closing
            if st.status in ["in_position", "closing", "selling"] and st.symbol and st.entry_price and st.qty:
                try:
                    cur_price = get_price_cached(self._client, st.symbol)
                    unreal_pct = (cur_price / st.entry_price - 1.0) * 100.0
                    unreal_usd = (cur_price - st.entry_price) * st.qty
                    tp_price = st.entry_price * (1.0 + TAKE_PROFIT_MIN_PCT)
                    trail_arm_price = st.entry_price * (1.0 + TRAIL_ARM_PCT)
                    sl_price = st.entry_price * (1.0 - STOP_LOSS_PCT)
                except Exception:
                    pass

            workers.append({
                "id": st.id, "quote": st.quote, "status": st.status, "symbol": st.symbol,
                "last_pnl": st.last_pnl, "note": st.note, "updated": st.updated,
                # live ctx
                "entry_price": st.entry_price, "qty": st.qty, "started": started_iso,
                "cur_price": cur_price, "unreal_pct": unreal_pct, "unreal_usd": unreal_usd,
                "tp_price": tp_price, "trail_arm_price": trail_arm_price, "sl_price": sl_price
            })
        workers.sort(key=lambda x: x["id"])

        return {
            "running": self._running,
            "watchlist_count": self._watchlist_count, "watchlist_total": self._watchlist_total,
            "start_net_usdt": start_val, "current_net_usdt": cur_val,
            "profit_usd": profit_usd, "profit_pct": profit_pct,
            "trade_profit_usd": self._performance_metrics['total_profit_usd'],
            "total_buys": self._performance_metrics['total_buy_orders'],
            "total_sells": self._performance_metrics['total_sell_orders'],
            "workers": workers, "debug_enabled": self.debug_enabled,
            "tunable_params": {
                'take_profit_pct': TUNABLE_PARAMS['take_profit_pct'],
                'trail_arm_pct': TUNABLE_PARAMS['trail_arm_pct'],
                'trail_giveback_pct': TUNABLE_PARAMS['trail_giveback_pct'],
                'stop_loss_pct': TUNABLE_PARAMS['stop_loss_pct'],
                'min_day_volatility_pct': TUNABLE_PARAMS['min_day_volatility_pct'],
                'volume_multiplier': TUNABLE_PARAMS['vol_mult'],
                'ema_strictness': TUNABLE_PARAMS['ema_relax'],
                'buying_pattern': TUNABLE_PARAMS.get('buying_pattern', 1),
                'cooldown_minutes': TUNABLE_PARAMS['cooldown_minutes'],
                'reversal_threshold_pct': TUNABLE_PARAMS['reversal_threshold_pct']
            }
        }

# ------------------ Flask ------------------
app = Flask(__name__, template_folder="templates")
bot = None # Initialize bot to None

# Helper function to get or create bot instance
def get_bot_instance():
    global bot
    if bot is None:
        print("[WARN] Bot instance accessed before initialization. Creating...")
        try:
            bot = FastCycleBot()
        except Exception as e:
            print(f"[ERROR] Failed to initialize bot instance: {e}")
            raise
    return bot

# ---- API Routes ----

@app.route("/")
def dashboard():
    try:
        bot_instance = get_bot_instance()
        recent = read_csv_tail(LOG_FILE, RECENT_TRADES_LIMIT)
        state = bot_instance.dashboard_state()
        return render_template("dashboard.html",
                             state=state,
                             recent_trades=recent,
                             watchlist_list=bot_instance.watchlist,
                             tp_trigger_pct=(TAKE_PROFIT_MIN_PCT*100),
                             trail_arm=(TRAIL_ARM_PCT*100),
                             trail_pct=(TRAIL_GIVEBACK_PCT*100),
                             sl_pct=(STOP_LOSS_PCT*100),
                             time_limit=MAX_TRADE_MINUTES,
                             min_day_vol=MIN_DAY_VOLATILITY_PCT,
                             recent_limit=RECENT_TRADES_LIMIT)
    except Exception as e:
        print(f"[Dashboard] Error rendering: {e}")
        return "Error loading dashboard.", 500

@app.get("/api/status")
def api_status():
    try:
        bot_instance = get_bot_instance()
        status = bot_instance.dashboard_state()
        return jsonify(status)
    except Exception as e:
        print(f"[API] Status ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.get("/api/trades")
def api_trades():
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"rows": [], "message": "No trade data file found"})

        trades = read_csv_tail(LOG_FILE, RECENT_TRADES_LIMIT)
        return jsonify({"rows": trades})
    except Exception as e:
        print(f"[API] Trades API failed: {e}")
        return jsonify({"rows": [], "error": str(e)}), 500

# ---- Debug API ----
@app.get("/api/debug")
def api_debug():
    try:
        bot_instance = get_bot_instance()
        events = list(bot_instance._debug_events)[-500:]  # Show more recent events
        counts = defaultdict(int)
        for e in events:
            if e and "reason" in e:
                counts[e["reason"]] += 1
        return jsonify({
            "enabled": bot_instance.debug_enabled,
            "events": events[::-1] if events else [],
            "counts": dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)) if counts else {},
            "total_events": len(bot_instance._debug_events)
        })
    except Exception as e:
        print(f"[API] Debug ERROR: {e}")
        return jsonify({
            "enabled": False,
            "events": [],
            "counts": {},
            "error": str(e)
        }), 500

@app.post("/api/debug/toggle")
def api_debug_toggle():
    try:
        bot_instance = get_bot_instance()
        data = request.get_json(force=True, silent=True) or {}
        bot_instance.debug_enabled = bool(data.get("enabled", True))
        return jsonify({"ok": True, "enabled": bot_instance.debug_enabled})
    except Exception as e:
        print(f"[API] Debug toggle ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/debug/export")
def api_debug_export():
    try:
        bot_instance = get_bot_instance()
        format_type = request.args.get("format", "csv").lower()
        if format_type == "csv":
            events = list(bot_instance._debug_events)
            csv_data = "Time,Worker,Symbol,Reason,Day_OK,EMA_OK,Vol_OK,Pattern_OK\n"
            for e in events:
                csv_data += f"{e.get('time','')},{e.get('worker_id','')},{e.get('symbol','')},{e.get('reason','')},{e.get('day_ok', False)},{e.get('ema_ok', False)},{e.get('vol_ok', False)},{e.get('pattern_ok', False)}\n"
            return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=debug_events.csv"})
        return jsonify({"error": "Unsupported format"})
    except Exception as e:
        print(f"[API] Debug export ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# ---- Analysis API ----
@app.route("/analysis")
def analysis_page():
    try:
        get_bot_instance() # Ensure bot is initialized
        return render_template("analysis.html")
    except Exception as e:
        print(f"[Analysis Page] Error: {e}")
        return "Error loading analysis page.", 500

@app.post("/api/analysis/refresh")
def api_analysis_refresh():
    try:
        bot_instance = get_bot_instance()
        bot_instance.trade_analyzer.invalidate_cache()
        return jsonify({"status": "cache_cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/analysis/performance")
def api_analysis_performance():
    try:
        bot_instance = get_bot_instance()

        # Check if CSV file exists and has data
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                print(f"[API] CSV file has {len(lines)} lines")
        else:
            print(f"[API] CSV file not found: {LOG_FILE}")

        analysis = bot_instance.trade_analyzer.get_comprehensive_analysis()

        print(f"[API] Analysis result - Total trades: {analysis.get('total_trades', 0)}")

        # Ensure all required fields are present and properly formatted
        if 'error' not in analysis:
            # Make sure numeric fields are properly formatted
            for key in ['win_rate', 'avg_pnl', 'total_pnl', 'avg_duration']:
                if key in analysis and analysis[key] is not None:
                    analysis[key] = float(analysis[key])
                else:
                    analysis[key] = 0.0

            # Ensure risk_metrics exists
            if 'risk_metrics' not in analysis or not analysis['risk_metrics']:
                analysis['risk_metrics'] = {
                    "volatility": 0.0,
                    "max_drawdown": 0.0,
                    "sharpe_ratio": 0.0,
                    "best_trade": 0.0,
                    "worst_trade": 0.0
                }

        return jsonify(analysis)
    except Exception as e:
        print(f"[API] Analysis performance ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "total_pnl": 0.0,
            "avg_duration": 0.0,
            "symbol_stats": {},
            "recent_trades": [],
            "performance_by_time": {},
            "risk_metrics": {
                "volatility": 0.0,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0
            },
            "recommendations": []
        })

# ---- Core/Workers ----
@app.post("/api/start-core")
def api_start_core():
    try:
        bot_instance = get_bot_instance()
        if bot_instance._running:
            return jsonify({"status": "already_running"})
        else:
            bot_instance.start_core()
            if bot_instance._running:
                return jsonify({"status": "started"})
            else:
                return jsonify({"error": "Failed to start bot core"}), 500
    except Exception as e:
        print(f"[API] Start-core ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.post("/api/stop-core")
def api_stop_core():
    try:
        print("[API] Stop-core requested")
        bot_instance = get_bot_instance()
        bot_instance.stop_core()
        print("[API] Bot core stopped")
        return jsonify({"status": "stopped"})
    except Exception as e:
        print(f"[API] Stop-core ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.post("/api/add-worker")
def api_add_worker():
    try:
        bot_instance = get_bot_instance()
        data = request.get_json(force=True, silent=True) or {}
        quote = float(data.get('quote', 20.0))
        wid = bot_instance.add_worker(quote)
        return jsonify({"worker_id": wid, "status": "added"})
    except Exception as e:
        print(f"[API] Add-worker ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.post("/api/stop-worker")
def api_stop_worker():
    try:
        bot_instance = get_bot_instance()
        data = request.get_json(force=True, silent=True) or {}
        worker_id = int(data.get("worker_id"))
        force = data.get("force", False)

        if force:
            # Force cleanup - immediately remove worker state
            print(f"[API] Force cleanup requested for worker {worker_id}")
            with bot_instance._lock:
                st = bot_instance._worker_state.get(worker_id)
                if st and st.symbol:
                    bot_instance._active_symbols.discard(st.symbol)
                bot_instance._workers.pop(worker_id, None)
                bot_instance._stop_flags.pop(worker_id, None)
                bot_instance._worker_state.pop(worker_id, None)
            return jsonify({"worker_id": worker_id, "status": "force_cleaned"})
        else:
            bot_instance.stop_worker(worker_id)
            return jsonify({"worker_id": worker_id, "status": "stop_signal_sent"})
    except Exception as e:
        print(f"[API] Stop-worker ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.get("/api/params")
def api_params():
    try:
        return jsonify({
            'take_profit_pct': TUNABLE_PARAMS['take_profit_pct'],
            'trail_arm_pct': TUNABLE_PARAMS['trail_arm_pct'],
            'trail_giveback_pct': TUNABLE_PARAMS['trail_giveback_pct'],
            'stop_loss_pct': TUNABLE_PARAMS['stop_loss_pct'],
            'min_day_volatility_pct': TUNABLE_PARAMS['min_day_volatility_pct'],
            'volume_multiplier': TUNABLE_PARAMS['vol_mult'],
            'ema_strictness': TUNABLE_PARAMS['ema_relax'],
            'buying_pattern': TUNABLE_PARAMS['buying_pattern'],
            'cooldown_minutes': TUNABLE_PARAMS['cooldown_minutes'],
            'reversal_threshold_pct': TUNABLE_PARAMS['reversal_threshold_pct']
        })
    except Exception as e:
        print(f"[API] Get params ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.post("/api/update-params")
def update_params():
    try:
        data = request.get_json() or {}

        # Update tunable parameters
        if 'take_profit_pct' in data:
            TUNABLE_PARAMS['take_profit_pct'] = float(data['take_profit_pct'])
        if 'trail_arm_pct' in data:
            TUNABLE_PARAMS['trail_arm_pct'] = float(data['trail_arm_pct'])
        if 'trail_giveback_pct' in data:
            TUNABLE_PARAMS['trail_giveback_pct'] = float(data['trail_giveback_pct'])
        if 'stop_loss_pct' in data:
            TUNABLE_PARAMS['stop_loss_pct'] = float(data['stop_loss_pct'])
        if 'min_day_volatility_pct' in data:
            TUNABLE_PARAMS['min_day_volatility_pct'] = float(data['min_day_volatility_pct'])
        if 'volume_multiplier' in data:
            TUNABLE_PARAMS['vol_mult'] = float(data['volume_multiplier'])
        if 'ema_strictness' in data:
            TUNABLE_PARAMS['ema_relax'] = float(data['ema_strictness'])
        if 'buying_pattern' in data:
            TUNABLE_PARAMS['buying_pattern'] = int(data['buying_pattern'])
        if 'cooldown_minutes' in data:
            TUNABLE_PARAMS['cooldown_minutes'] = int(data['cooldown_minutes'])
        if 'reversal_threshold_pct' in data:
            TUNABLE_PARAMS['reversal_threshold_pct'] = float(data['reversal_threshold_pct'])

        print(f"[API] Parameters updated: {TUNABLE_PARAMS}")
        return jsonify({"status": "success", "params": TUNABLE_PARAMS})
    except Exception as e:
        print(f"[API] Update params ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.get("/api/console")
def api_console():
    try:
        limit = request.args.get('limit', 100, type=int)

        # Get real console output from capture system
        global console_capture
        lines = console_capture.get_lines(limit)

        if not lines:
            lines = ["Bot initialized - Console output will appear here"]

        return jsonify({"lines": lines, "total": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e), "lines": []}), 500

@app.get("/api/console/stream")
def api_console_stream():
    def generate():
        last_count = 0
        while True:
            try:
                global console_capture
                current_lines = console_capture.get_lines()
                current_count = len(current_lines)

                if current_count > last_count:
                    new_lines = current_lines[last_count:]
                    for line in new_lines:
                        yield f"data: {json.dumps({'line': line})}\n\n"
                    last_count = current_count

                time.sleep(1)  # Check every second for real-time updates
            except Exception:
                break

    return Response(generate(), mimetype='text/event-stream',
                   headers={'Cache-Control': 'no-cache'})

@app.post("/api/console/clear")
def api_console_clear():
    try:
        global console_capture
        with console_capture.lock:
            console_capture.lines.clear()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/reconnect-api")
def api_reconnect():
    try:
        global bot
        if bot is None:
            return jsonify({"error": "Bot not initialized"}), 400

        print("[API] Reconnecting to Binance API...")

        # Test current connection first
        try:
            test_response = bot._client.get_account()
            return jsonify({
                "status": "already_connected",
                "message": "API connection is already working",
                "balance": f"{get_net_usdt_value(bot._client):.2f} USDT"
            })
        except Exception as conn_error:
            print(f"[API] Current connection failed: {conn_error}")

        # Reinitialize the client
        old_client = bot._client
        bot._client = build_client()

        # Test new connection
        try:
            account_info = bot._client.get_account()
            new_balance = get_net_usdt_value(bot._client)
            bot.current_net_usdt = new_balance

            # Force immediate balance refresh in the metrics loop
            if hasattr(bot, '_last_balance_time'):
                bot._last_balance_time = 0  # Reset to force immediate update

            print(f"[API] Successfully reconnected to Binance API")
            print(f"[API] New balance: {new_balance:.2f} USDT")
            print(f"[API] Dashboard balance will refresh within 30 seconds")

            return jsonify({
                "status": "reconnected",
                "message": "Successfully reconnected to Binance API",
                "balance": f"{new_balance:.2f} USDT",
                "server_ip": get_server_ip(),
                "force_refresh": True
            })

        except Exception as new_conn_error:
            # Rollback to old client if new one fails
            bot._client = old_client
            print(f"[API] Reconnection failed, rolled back: {new_conn_error}")

            return jsonify({
                "error": f"Reconnection failed: {new_conn_error}",
                "suggestion": "Make sure you've whitelisted the server IP in Binance"
            }), 400

    except Exception as e:
        print(f"[API] Reconnect ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.get("/api/server-info")
def api_server_info():
    try:
        server_ip = get_server_ip()

        # Parse the IP and hostname from the formatted string
        ip_part = server_ip.split(" (")[0] if " (" in server_ip else server_ip
        hostname_part = ""
        if " (hostname: " in server_ip:
            hostname_part = server_ip.split(" (hostname: ")[1].rstrip(")")
        elif " (detected)" in server_ip:
            hostname_part = "Auto-detected"

        # Try to determine region based on environment
        region = "Unknown"
        if os.getenv('REPL_DEPLOYMENT'):
            region = "Replit Deployment (USA)"
        elif os.getenv('REPL_SLUG'):
            region = "Replit Development (USA)"

        return jsonify({
            "ip": ip_part,
            "hostname": hostname_part,
            "region": region,
            "environment": "DEPLOYMENT" if os.getenv('REPL_DEPLOYMENT') else "DEVELOPMENT"
        })
    except Exception as e:
        print(f"[API] Server info ERROR: {e}")
        return jsonify({
            "ip": "Unable to fetch",
            "hostname": "Unknown",
            "region": "Unknown",
            "environment": "Unknown",
            "error": str(e)
        }), 500

def get_server_ip():
    """Get the server's external IP address for Binance whitelisting"""
    try:
        # First try to get external IP using HTTP services
        services = [
            "https://api.ipify.org",
            "https://ipecho.net/plain",
            "https://icanhazip.com",
            "https://ident.me"
        ]

        for service in services:
            try:
                response = requests.get(service, timeout=10)
                if response.status_code == 200:
                    external_ip = response.text.strip()
                    # Validate it's a proper IP address
                    import socket
                    socket.inet_aton(external_ip)
                    return f"{external_ip} (external)"
            except Exception:
                continue

        # Fallback: Get internal IP with warning
        import socket
        hostname = socket.gethostname()

        try:
            # Try socket method for internal IP
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                internal_ip = s.getsockname()[0]
            return f"{internal_ip} (⚠️ INTERNAL - Use external IP for Binance)"
        except Exception:
            return "Unable to determine IP address"

    except Exception as e:
        return f"Error getting IP: {str(e)}"

if __name__ == "__main__":
    print("=== STARTING TRADEPRO BOT ===")

    # Output IP address
    server_ip = get_server_ip()
    print(f"🌐 Server IP Address: {server_ip}")

    print("Initializing bot core...")

    try:
        # Initialize bot without auto-starting (will be started via API or dashboard)
        bot = FastCycleBot()
        print("✅ Bot core initialized (ready for startup via dashboard)")

        # Enable console capture for real-time dashboard display
        print("🔧 Console capture enabled - all output will appear in dashboard")

        # Disable Flask's request logging for performance
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        # Start Flask server directly on port 5000
        port = int(os.getenv("PORT", "5000"))

        print(f"🌐 Starting dashboard on port {port}...")
        print(f"🔗 Dashboard URL: https://{os.getenv('REPL_SLUG', 'workspace')}-{os.getenv('REPL_OWNER', 'username')}.replit.app")

        try:
            app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
        except OSError as e:
            if "Address already in use" in str(e):
                print(f"⚠️ Port {port} is in use, trying port {port + 1}...")
                app.run(host="0.0.0.0", port=port + 1, debug=False, threaded=True)
            else:
                raise e

    except Exception as e:
        print(f"CRITICAL ERROR: Failed to start Flask server or initialize bot: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)