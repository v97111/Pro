
"""
Intelligent Alerting System for Trading Bot
"""
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable
from enum import Enum

class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

@dataclass
class Alert:
    level: AlertLevel
    title: str
    message: str
    timestamp: float
    acknowledged: bool = False
    data: Optional[Dict] = None

class AlertManager:
    def __init__(self):
        self._alerts = deque(maxlen=100)
        self._handlers: List[Callable] = []
        self._thresholds = {
            'error_rate': 0.1,  # 10% error rate
            'win_rate': 0.3,    # 30% win rate
            'api_ban_keywords': ['banned', 'weight', 'rate limit'],
            'balance_drop_pct': 5.0  # 5% balance drop
        }
        self._lock = threading.Lock()
        
    def add_handler(self, handler: Callable):
        """Add alert handler (e.g., email, webhook, etc.)"""
        self._handlers.append(handler)
    
    def create_alert(self, level: AlertLevel, title: str, message: str, data: Dict = None):
        """Create a new alert"""
        alert = Alert(
            level=level,
            title=title,
            message=message,
            timestamp=time.time(),
            data=data or {}
        )
        
        with self._lock:
            self._alerts.append(alert)
        
        # Notify handlers
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as e:
                print(f"Alert handler failed: {e}")
    
    def check_trading_health(self, metrics: Dict):
        """Check trading metrics and create alerts if needed"""
        
        # Check error rate
        if metrics.get('api_calls_count', 0) > 0:
            error_rate = metrics.get('api_errors_count', 0) / metrics['api_calls_count']
            if error_rate > self._thresholds['error_rate']:
                self.create_alert(
                    AlertLevel.WARNING,
                    "High API Error Rate",
                    f"API error rate is {error_rate:.1%} (threshold: {self._thresholds['error_rate']:.1%})",
                    {"error_rate": error_rate}
                )
        
        # Check win rate
        win_rate = metrics.get('win_rate', 0)
        if metrics.get('total_trades', 0) > 10 and win_rate < self._thresholds['win_rate']:
            self.create_alert(
                AlertLevel.WARNING,
                "Low Win Rate",
                f"Win rate is {win_rate:.1%} (threshold: {self._thresholds['win_rate']:.1%})",
                {"win_rate": win_rate}
            )
    
    def check_api_response(self, response_text: str):
        """Check API response for ban indicators"""
        for keyword in self._thresholds['api_ban_keywords']:
            if keyword.lower() in response_text.lower():
                self.create_alert(
                    AlertLevel.CRITICAL,
                    "API Rate Limit/Ban Detected",
                    f"Detected '{keyword}' in API response: {response_text[:200]}",
                    {"response": response_text}
                )
                break
    
    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        """Get recent alerts"""
        with self._lock:
            alerts = list(self._alerts)[-limit:]
            return [
                {
                    'level': alert.level.value,
                    'title': alert.title,
                    'message': alert.message,
                    'timestamp': alert.timestamp,
                    'acknowledged': alert.acknowledged,
                    'data': alert.data
                }
                for alert in reversed(alerts)
            ]
    
    def acknowledge_alerts(self, alert_indices: List[int]):
        """Acknowledge specific alerts"""
        with self._lock:
            for i in alert_indices:
                if 0 <= i < len(self._alerts):
                    self._alerts[i].acknowledged = True

# Global alert manager
alert_manager = AlertManager()

# Console alert handler
def console_alert_handler(alert: Alert):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.timestamp))
    print(f"[{alert.level.value.upper()}] {timestamp} - {alert.title}: {alert.message}")

alert_manager.add_handler(console_alert_handler)
