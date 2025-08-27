
"""
Monitoring and Performance Tracking for Fast-Cycle Bot
"""
import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Optional
import json

@dataclass
class PerformanceMetrics:
    uptime_seconds: float = 0
    total_trades: int = 0
    successful_trades: int = 0
    failed_trades: int = 0
    win_rate: float = 0.0
    avg_trade_duration: float = 0.0
    total_pnl: float = 0.0
    api_calls_count: int = 0
    api_errors_count: int = 0
    memory_usage_mb: float = 0.0
    active_workers: int = 0
    symbols_scanned_per_minute: float = 0.0

class SystemMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._metrics = PerformanceMetrics()
        self._api_call_times = deque(maxlen=1000)
        self._trade_durations = deque(maxlen=100)
        self._error_log = deque(maxlen=500)
        self._alerts = deque(maxlen=50)
        self._start_time = time.time()
        
    def record_api_call(self, duration: float, success: bool):
        with self._lock:
            self._metrics.api_calls_count += 1
            self._api_call_times.append(duration)
            if not success:
                self._metrics.api_errors_count += 1
                if self._metrics.api_errors_count > 10:
                    self._add_alert("HIGH_API_ERRORS", f"API error rate: {self._get_error_rate():.1%}")
    
    def record_trade(self, duration_seconds: float, pnl_pct: float, success: bool):
        with self._lock:
            self._metrics.total_trades += 1
            if success:
                self._metrics.successful_trades += 1
            else:
                self._metrics.failed_trades += 1
            
            self._trade_durations.append(duration_seconds)
            self._metrics.total_pnl += pnl_pct
            
            # Update averages
            if self._metrics.total_trades > 0:
                self._metrics.win_rate = self._metrics.successful_trades / self._metrics.total_trades
            if self._trade_durations:
                self._metrics.avg_trade_duration = sum(self._trade_durations) / len(self._trade_durations)
    
    def record_error(self, error_type: str, message: str):
        with self._lock:
            self._error_log.append({
                'timestamp': time.time(),
                'type': error_type,
                'message': message
            })
    
    def _add_alert(self, alert_type: str, message: str):
        self._alerts.append({
            'timestamp': time.time(),
            'type': alert_type,
            'message': message
        })
    
    def _get_error_rate(self) -> float:
        if self._metrics.api_calls_count == 0:
            return 0.0
        return self._metrics.api_errors_count / self._metrics.api_calls_count
    
    def get_health_score(self) -> float:
        """Return a health score from 0-100"""
        score = 100.0
        
        # Penalize high error rates
        error_rate = self._get_error_rate()
        if error_rate > 0.1:  # 10% error rate
            score -= min(50, error_rate * 200)
        
        # Penalize low win rate
        if self._metrics.win_rate < 0.4:
            score -= (0.4 - self._metrics.win_rate) * 100
        
        # Penalize if no recent trades
        if self._metrics.total_trades == 0:
            score -= 30
        
        return max(0, score)
    
    def get_metrics(self) -> Dict:
        with self._lock:
            self._metrics.uptime_seconds = time.time() - self._start_time
            return {
                'metrics': self._metrics.__dict__,
                'health_score': self.get_health_score(),
                'recent_errors': list(self._error_log)[-10:],
                'alerts': list(self._alerts)[-5:],
                'avg_api_response_time': sum(self._api_call_times) / len(self._api_call_times) if self._api_call_times else 0
            }

# Global monitor instance
system_monitor = SystemMonitor()
