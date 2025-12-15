"""
AlertScanner Package
Real-time and backtesting alert scanner for stock market conditions.

Modules:
    conditions.py - Base condition classes and built-in conditions
    realtime_scanner.py - Real-time monitoring and alert triggering
    backtest_scanner.py - Backtesting historical data against conditions
"""

from .conditions import (
    AlertCondition,
    AlertConditionSet,
    MarketData,
    PriceAboveVWAPCondition,
    PriceSurgeCondition,
    VolumeSurgeCondition
)
from .realtime_scanner import RealtimeAlertScanner, RealtimeSymbolMonitor
from .backtest_scanner import BacktestAlertScanner, BacktestAlert

__all__ = [
    'AlertCondition',
    'AlertConditionSet',
    'MarketData',
    'PriceAboveVWAPCondition',
    'PriceSurgeCondition',
    'VolumeSurgeCondition',
    'RealtimeAlertScanner',
    'RealtimeSymbolMonitor',
    'BacktestAlertScanner',
    'BacktestAlert'
]
