
"""
Smart Strategy Optimizer - Learns from historical trades to improve entry conditions
"""
import csv
import os
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
import statistics

class SmartStrategyOptimizer:
    def __init__(self, csv_file: str):
        self.csv_file = csv_file
        self._cache_timestamp = 0
        self._cache_ttl = 300  # 5 minutes
        self._analysis_cache = None
        self._symbol_performance = {}
        self._optimal_conditions = {}
        
    def _load_trade_data(self) -> List[Dict]:
        """Load and parse trade data with proper matching"""
        if (self._analysis_cache and 
            time.time() - self._cache_timestamp < self._cache_ttl):
            return self._analysis_cache
        
        trades = []
        pending_buys = {}
        
        if not os.path.exists(self.csv_file):
            return []
        
        with open(self.csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = row['symbol']
                worker_id = row['worker_id']
                action = row['action']
                
                if action == 'BUY':
                    key = f"{symbol}_{worker_id}"
                    pending_buys[key] = {
                        'symbol': symbol,
                        'buy_time': row['time'],
                        'buy_price': float(row['price']),
                        'qty': float(row['qty']),
                        'worker_id': worker_id
                    }
                elif action.startswith('SELL'):
                    key = f"{symbol}_{worker_id}"
                    if key in pending_buys:
                        trade = pending_buys.pop(key)
                        trade.update({
                            'sell_time': row['time'],
                            'sell_price': float(row['price']),
                            'pnl_pct': float(row['pnl_pct']) if row['pnl_pct'] else 0.0,
                            'sell_action': action,
                            'note': row['note']
                        })
                        trades.append(trade)
        
        self._analysis_cache = trades
        self._cache_timestamp = time.time()
        return trades
    
    def analyze_symbol_patterns(self) -> Dict[str, Dict]:
        """Analyze patterns for each symbol"""
        trades = self._load_trade_data()
        symbol_analysis = defaultdict(lambda: {
            'trades': [],
            'win_rate': 0.0,
            'avg_pnl': 0.0,
            'volatility': 0.0,
            'best_times': [],
            'risk_score': 0.0
        })
        
        for trade in trades:
            symbol = trade['symbol']
            pnl = trade['pnl_pct']
            symbol_analysis[symbol]['trades'].append(trade)
        
        # Calculate metrics for each symbol
        for symbol, data in symbol_analysis.items():
            trades_list = data['trades']
            if not trades_list:
                continue
                
            pnls = [t['pnl_pct'] for t in trades_list]
            wins = [p for p in pnls if p > 0]
            
            data['win_rate'] = len(wins) / len(pnls) if pnls else 0
            data['avg_pnl'] = statistics.mean(pnls) if pnls else 0
            data['volatility'] = statistics.stdev(pnls) if len(pnls) > 1 else 0
            
            # Risk score (higher = riskier)
            data['risk_score'] = (
                (1 - data['win_rate']) * 0.4 +  # Win rate component
                max(0, -data['avg_pnl'] / 5) * 0.3 +  # Avg loss component
                min(1, data['volatility'] / 10) * 0.3  # Volatility component
            )
        
        self._symbol_performance = dict(symbol_analysis)
        return self._symbol_performance
    
    def get_symbol_filter(self) -> Tuple[List[str], List[str]]:
        """Get recommended symbol filters"""
        analysis = self.analyze_symbol_patterns()
        
        # Blacklist criteria
        blacklist = []
        whitelist = []
        
        for symbol, data in analysis.items():
            if len(data['trades']) < 3:  # Need minimum trades
                continue
                
            # Blacklist if consistently poor performance
            if (data['win_rate'] < 0.3 or 
                data['avg_pnl'] < -1.0 or 
                data['risk_score'] > 0.7):
                blacklist.append(symbol)
            
            # Whitelist if consistently good performance
            elif (data['win_rate'] > 0.6 and 
                  data['avg_pnl'] > 0.5 and 
                  data['risk_score'] < 0.4):
                whitelist.append(symbol)
        
        return whitelist, blacklist
    
    def get_dynamic_conditions(self, mode: str = "safe") -> Dict:
        """Generate dynamic trading conditions based on recent performance"""
        trades = self._load_trade_data()
        
        if len(trades) < 10:
            # Not enough data, return conservative defaults
            return {
                'volume_multiplier': 1.2,
                'ema_strictness': 1.0,
                'min_day_volatility': 0.6,
                'cooldown_minutes': 10
            }
        
        recent_trades = trades[-50:]  # Last 50 trades
        recent_wins = [t for t in recent_trades if t['pnl_pct'] > 0]
        recent_win_rate = len(recent_wins) / len(recent_trades)
        avg_recent_pnl = sum(t['pnl_pct'] for t in recent_trades) / len(recent_trades)
        
        # Adaptive conditions based on recent performance
        conditions = {}
        
        if recent_win_rate < 0.4:
            # Poor recent performance - tighten conditions
            conditions = {
                'volume_multiplier': 1.5,  # Require higher volume
                'ema_strictness': 1.02,   # Stricter EMA requirement
                'min_day_volatility': 0.8, # Higher volatility requirement
                'cooldown_minutes': 15    # Longer cooldown
            }
        elif recent_win_rate > 0.6:
            # Good recent performance - slightly relax
            conditions = {
                'volume_multiplier': 1.0,
                'ema_strictness': 0.998,
                'min_day_volatility': 0.4,
                'cooldown_minutes': 5
            }
        else:
            # Average performance - balanced conditions
            conditions = {
                'volume_multiplier': 1.2,
                'ema_strictness': 1.0,
                'min_day_volatility': 0.5,
                'cooldown_minutes': 8
            }
        
        return conditions
    
    def should_trade_symbol(self, symbol: str) -> Tuple[bool, str]:
        """Determine if a symbol should be traded based on historical performance"""
        whitelist, blacklist = self.get_symbol_filter()
        
        if symbol in blacklist:
            return False, f"Symbol {symbol} is blacklisted due to poor performance"
        
        if symbol in whitelist:
            return True, f"Symbol {symbol} is whitelisted for good performance"
        
        # For unknown symbols, check if we have any data
        analysis = self.analyze_symbol_patterns()
        if symbol in analysis:
            data = analysis[symbol]
            if data['risk_score'] > 0.6:
                return False, f"Symbol {symbol} has high risk score: {data['risk_score']:.2f}"
        
        return True, "Symbol approved for trading"

# Global optimizer instance
strategy_optimizer = SmartStrategyOptimizer("fast_cycle_trades.csv")
