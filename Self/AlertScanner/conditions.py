"""
Alert Conditions Module
Defines base condition class and specific alert conditions for the scanner.
New conditions can be easily added by extending the AlertCondition class.

CENTRALIZED CONFIGURATION:
- PRICE_SURGE_THRESHOLD: Percentage change to trigger price surge alert
- VOLUME_SURGE_THRESHOLD: Volume multiplier to trigger volume surge alert
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any
from datetime import datetime, timedelta


# =============================================================================
# CENTRALIZED ALERT CONFIGURATION
# Configure these values to adjust alert sensitivity across all scanners
# =============================================================================

PRICE_SURGE_THRESHOLD = 3.0  # Percentage (e.g., 3.0 = 3% price increase)
VOLUME_SURGE_THRESHOLD = 5.0  # Multiplier (e.g., 5.0 = 5x volume increase)


@dataclass
class MarketData:
    """Container for current market data"""
    symbol: str
    price: float
    volume: int
    vwap: float
    timestamp: datetime
    price_history: Dict[str, float] = None  # timestamp -> price
    volume_history: Dict[str, int] = None  # timestamp -> volume


class AlertCondition(ABC):
    """Base class for all alert conditions. Extend this to add new conditions."""
    
    def __init__(self, name: str):
        self.name = name
        self.triggered_reason = ""
    
    @abstractmethod
    def check(self, data: MarketData) -> bool:
        """
        Check if condition is met.
        
        Args:
            data: MarketData object with current market data
            
        Returns:
            bool: True if condition is triggered, False otherwise
        """
        pass
    
    def get_trigger_reason(self) -> str:
        """Return the reason why condition was triggered"""
        return self.triggered_reason


class PriceAboveVWAPCondition(AlertCondition):
    """Condition: Price is above VWAP"""
    
    def __init__(self):
        super().__init__("Price Above VWAP")
    
    def check(self, data: MarketData) -> bool:
        if data.price > data.vwap:
            self.triggered_reason = f"Price ${data.price:.2f} > VWAP ${data.vwap:.2f}"
            return True
        self.triggered_reason = ""
        return False


class PriceSurgeCondition(AlertCondition):
    """Condition: Huge surge in price in the last 10 seconds"""
    
    def __init__(self, surge_threshold: float = None):
        """
        Args:
            surge_threshold: Percentage increase threshold (uses PRICE_SURGE_THRESHOLD if None)
        """
        super().__init__("Price Surge (Last 10s)")
        self.surge_threshold = surge_threshold if surge_threshold is not None else PRICE_SURGE_THRESHOLD
        self.lookback_seconds = 10
    
    def check(self, data: MarketData) -> bool:
        if not data.price_history or len(data.price_history) < 2:
            self.triggered_reason = ""
            return False
        
        # Get prices from last 10 seconds
        cutoff_time = data.timestamp - timedelta(seconds=self.lookback_seconds)
        recent_prices = {
            ts: price for ts, price in data.price_history.items()
            if ts >= cutoff_time
        }
        
        if len(recent_prices) < 2:
            self.triggered_reason = ""
            return False
        
        # Find lowest price in the window
        min_price = min(recent_prices.values())
        
        # Calculate percentage change
        if min_price == 0:
            self.triggered_reason = ""
            return False
        
        pct_change = ((data.price - min_price) / min_price) * 100
        
        if pct_change >= self.surge_threshold:
            self.triggered_reason = (
                f"Price surged {pct_change:.2f}% in last 10s "
                f"(${min_price:.2f} -> ${data.price:.2f})"
            )
            return True
        
        self.triggered_reason = ""
        return False


class VolumeSurgeCondition(AlertCondition):
    """Condition: Huge surge in volume during the last 10 seconds"""
    
    def __init__(self, surge_threshold: float = None):
        """
        Args:
            surge_threshold: Volume multiplier threshold (uses VOLUME_SURGE_THRESHOLD if None)
        """
        super().__init__("Volume Surge (Last 10s)")
        self.surge_threshold = surge_threshold if surge_threshold is not None else VOLUME_SURGE_THRESHOLD
        self.lookback_seconds = 10
    
    def check(self, data: MarketData) -> bool:
        if not data.volume_history or len(data.volume_history) < 2:
            self.triggered_reason = ""
            return False
        
        # Get volumes from last 10 seconds
        cutoff_time = data.timestamp - timedelta(seconds=self.lookback_seconds)
        recent_volumes = {
            ts: vol for ts, vol in data.volume_history.items()
            if ts >= cutoff_time
        }
        
        if len(recent_volumes) < 2:
            self.triggered_reason = ""
            return False
        
        # Get average volume before this spike
        all_volumes = list(data.volume_history.values())
        if len(all_volumes) >= 3:
            avg_volume = sum(all_volumes[:-1]) / (len(all_volumes) - 1)
        else:
            avg_volume = min(recent_volumes.values())
        
        if avg_volume == 0:
            self.triggered_reason = ""
            return False
        
        current_volume = data.volume
        multiplier = current_volume / avg_volume if avg_volume > 0 else 0
        
        if multiplier >= self.surge_threshold:
            self.triggered_reason = (
                f"Volume surged {multiplier:.2f}x in last 10s "
                f"(Avg: {avg_volume:.0f} -> Current: {current_volume:.0f})"
            )
            return True
        
        self.triggered_reason = ""
        return False


class AlertConditionSet:
    """Container for multiple conditions with AND logic"""
    
    def __init__(self, name: str):
        self.name = name
        self.conditions: list[AlertCondition] = []
        self.triggered_reasons: list[str] = []
    
    def add_condition(self, condition: AlertCondition) -> 'AlertConditionSet':
        """Add a condition to the set. Returns self for chaining."""
        self.conditions.append(condition)
        return self
    
    def check_all(self, data: MarketData) -> bool:
        """
        Check if ALL conditions are met.
        
        Args:
            data: MarketData object
            
        Returns:
            bool: True only if all conditions are triggered
        """
        self.triggered_reasons = []
        
        for condition in self.conditions:
            if condition.check(data):
                self.triggered_reasons.append(condition.get_trigger_reason())
            else:
                return False
        
        return True
    
    def get_trigger_summary(self) -> str:
        """Get summary of all triggered conditions"""
        return " | ".join(self.triggered_reasons)
