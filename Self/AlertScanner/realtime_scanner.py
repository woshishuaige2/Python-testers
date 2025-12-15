"""
Real-Time Alert Scanner with IBKR TWS Integration
Monitors multiple symbols in real-time and triggers alerts when conditions are met.
Integrates with Interactive Brokers TWS API for live market data.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable
from collections import deque
import threading
import time
import sys

from conditions import (
    AlertCondition,
    AlertConditionSet,
    MarketData,
    PriceAboveVWAPCondition,
    PriceSurgeCondition,
    VolumeSurgeCondition
)

# Try to import TWS integration
try:
    from tws_data_fetcher import create_tws_data_app, TWSDataApp
    HAS_TWS = True
except ImportError:
    HAS_TWS = False
    print("[WARN] TWS integration not available. Install ibapi: pip install ibapi")


class RealtimeSymbolMonitor:
    """Monitors a single symbol in real-time"""
    
    def __init__(
        self,
        symbol: str,
        condition_set: AlertConditionSet,
        history_window_seconds: int = 60,
        max_history_size: int = 1000
    ):
        self.symbol = symbol
        self.condition_set = condition_set
        self.history_window_seconds = history_window_seconds
        self.max_history_size = max_history_size
        
        # Data tracking
        self.price_history = deque(maxlen=max_history_size)
        self.volume_history = deque(maxlen=max_history_size)
        self.last_price = None
        self.last_volume = None
        self.last_vwap = None
        self.last_update = None
        self.lock = threading.Lock()
        
        # Alert tracking
        self.last_alert_time = None
        self.alert_cooldown_seconds = 5  # Prevent duplicate alerts
    
    def update_market_data(self, price: float, volume: int, vwap: float):
        """Update market data for this symbol"""
        with self.lock:
            timestamp = datetime.now()
            
            self.price_history.append((timestamp, price))
            self.volume_history.append((timestamp, volume))
            
            self.last_price = price
            self.last_volume = volume
            self.last_vwap = vwap
            self.last_update = timestamp
    
    def get_market_data(self) -> Optional[MarketData]:
        """Get current market data as MarketData object"""
        with self.lock:
            if self.last_price is None:
                return None
            
            # Convert history deques to dicts
            price_dict = {ts: price for ts, price in self.price_history}
            volume_dict = {ts: vol for ts, vol in self.volume_history}
            
            return MarketData(
                symbol=self.symbol,
                price=self.last_price,
                volume=self.last_volume,
                vwap=self.last_vwap,
                timestamp=self.last_update,
                price_history=price_dict,
                volume_history=volume_dict
            )
    
    def check_conditions(self) -> Dict[str, any]:
        """
        Check if alert conditions are met.
        
        Returns:
            Dict with 'triggered' (bool) and 'reasons' (str) keys
        """
        data = self.get_market_data()
        if data is None:
            return {'triggered': False, 'reasons': ''}
        
        # Check if alert should be triggered
        if self.condition_set.check_all(data):
            # Check cooldown
            if (self.last_alert_time is None or
                (datetime.now() - self.last_alert_time).total_seconds() > self.alert_cooldown_seconds):
                self.last_alert_time = datetime.now()
                return {
                    'triggered': True,
                    'reasons': self.condition_set.get_trigger_summary(),
                    'data': data
                }
        
        return {'triggered': False, 'reasons': ''}


class RealtimeAlertScanner:
    """
    Main real-time alert scanner for multiple symbols.
    
    Usage:
        scanner = RealtimeAlertScanner(symbols=['AAPL', 'MSFT'])
        scanner.on_alert(my_alert_handler)
        
        # Simulate market data feed
        scanner.update('AAPL', price=150.25, volume=1000000, vwap=149.50)
    """
    
    def __init__(self, symbols: List[str], max_symbols: int = 5):
        """
        Initialize scanner.
        
        Args:
            symbols: List of symbols to monitor (up to 5)
            max_symbols: Maximum number of symbols allowed
        """
        if len(symbols) > max_symbols:
            raise ValueError(f"Maximum {max_symbols} symbols allowed, got {len(symbols)}")
        
        self.symbols = symbols
        self.monitors: Dict[str, RealtimeSymbolMonitor] = {}
        self.alert_callbacks: List[Callable] = []
        self.running = False
        self.lock = threading.Lock()
        
        # Initialize monitors with default conditions
        self._initialize_monitors()
    
    def _initialize_monitors(self):
        """Initialize monitors with default condition set"""
        for symbol in self.symbols:
            # Create default condition set
            condition_set = AlertConditionSet(f"{symbol}_default")
            condition_set.add_condition(PriceAboveVWAPCondition())
            condition_set.add_condition(PriceSurgeCondition(surge_threshold=0.5))
            condition_set.add_condition(VolumeSurgeCondition(surge_threshold=2.0))
            
            self.monitors[symbol] = RealtimeSymbolMonitor(symbol, condition_set)
    
    def set_conditions(self, symbol: str, condition_set: AlertConditionSet):
        """Override conditions for a specific symbol"""
        if symbol not in self.monitors:
            raise ValueError(f"Symbol {symbol} not in monitored list")
        
        self.monitors[symbol].condition_set = condition_set
    
    def on_alert(self, callback: Callable):
        """Register callback for alerts. Callback receives (symbol, timestamp, reasons)"""
        self.alert_callbacks.append(callback)
    
    def update(self, symbol: str, price: float, volume: int, vwap: float):
        """
        Update market data for a symbol and check conditions.
        
        Args:
            symbol: Stock symbol
            price: Current price
            volume: Current volume
            vwap: Volume-weighted average price
        """
        if symbol not in self.monitors:
            raise ValueError(f"Symbol {symbol} not in monitored list")
        
        monitor = self.monitors[symbol]
        monitor.update_market_data(price, volume, vwap)
        
        # Check conditions and trigger alerts
        result = monitor.check_conditions()
        if result['triggered']:
            self._trigger_alert(symbol, result['data'], result['reasons'])
    
    def _trigger_alert(self, symbol: str, data: MarketData, reasons: str):
        """Trigger an alert"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Console log message
        alert_message = (
            f"[{timestamp}] ALERT: {symbol}\n"
            f"  Price: ${data.price:.2f} | Volume: {data.volume:,}\n"
            f"  Conditions: {reasons}"
        )
        print(alert_message)
        
        # Call registered callbacks
        for callback in self.alert_callbacks:
            try:
                callback(symbol, timestamp, reasons, data)
            except Exception as e:
                print(f"Error in alert callback: {e}")
    
    def get_monitor(self, symbol: str) -> Optional[RealtimeSymbolMonitor]:
        """Get monitor for a specific symbol"""
        return self.monitors.get(symbol)
    
    def get_monitored_symbols(self) -> List[str]:
        """Get list of monitored symbols"""
        return list(self.monitors.keys())
    
    def stop(self):
        """Stop the scanner"""
        self.running = False


# Example usage
if __name__ == "__main__":
    print("\n" + "="*60)
    print("REAL-TIME ALERT SCANNER")
    print("="*60)
    
    # Get user input for symbols
    print("\nEnter up to 5 symbols separated by commas (e.g., AAPL,MSFT,GOOGL):")
    symbols_input = input("> ").strip().upper()
    symbols = [s.strip() for s in symbols_input.split(',')]
    
    if len(symbols) > 5:
        print("❌ Maximum 5 symbols allowed. Using first 5.")
        symbols = symbols[:5]
    
    if not symbols or symbols[0] == '':
        print("❌ No symbols provided. Exiting.")
        exit(1)
    
    print(f"✓ Symbols: {', '.join(symbols)}")
    
    # Choose between simulation and TWS
    if HAS_TWS:
        print("\nOptions:")
        print("  1. Connect to TWS (live market data)")
        print("  2. Simulation mode (demo data)")
        choice = input("Select (1 or 2): ").strip()
    else:
        choice = "2"
        print("\n⚠️  TWS integration not available. Using simulation mode.")
    
    # Create scanner
    scanner = RealtimeAlertScanner(symbols=symbols)
    
    # Optional: Add custom callback
    def my_alert_handler(symbol, timestamp, reasons, data):
        print(f"\n>>> CUSTOM HANDLER for {symbol} at {timestamp}")
        print(f"    Reasons: {reasons}\n")
    
    scanner.on_alert(my_alert_handler)
    
    if choice == "1" and HAS_TWS:
        # Connect to TWS
        print("\nConnecting to TWS...")
        print("Make sure TWS or IB Gateway is running with API enabled!")
        print("Using paper trading port 7497\n")
        
        tws_app = create_tws_data_app(host="127.0.0.1", port=7497, client_id=902)
        
        if tws_app is None:
            print("❌ Failed to connect to TWS. Exiting.")
            exit(1)
        
        print("✓ Connected to TWS\n")
        
        # Create callback for each symbol
        def create_tws_callback(scanner_obj, symbol):
            def callback(sym, price, volume, vwap, timestamp):
                """Callback receives: symbol, price, volume, vwap, timestamp"""
                scanner_obj.update(sym, price=price, volume=volume, vwap=vwap)
            
            return callback
        
        # Subscribe to market data for all symbols
        for symbol in symbols:
            callback = create_tws_callback(scanner, symbol)
            tws_app.subscribe_realtime_data(symbol, callback)
        
        print(f"Subscribed to live market data for: {', '.join(symbols)}")
        print("Listening for alerts (Ctrl+C to stop)...\n")
        
        try:
            # Keep running
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nStopping scanner...")
            tws_app.disconnect()
            print("Disconnected from TWS")
    
    else:
        # Simulation mode
        import random
        
        print("Starting real-time scanner in SIMULATION mode...\n")
        print("Simulating price and volume movements...")
        print("Listening for alerts (Ctrl+C to stop)...\n")
        
        iteration = 0
        try:
            while True:
                iteration += 1
                
                # Simulate price and volume movements
                for symbol in scanner.get_monitored_symbols():
                    base_price = {
                        'AAPL': 150, 'MSFT': 330, 'GOOGL': 140,
                        'TSLA': 250, 'AMZN': 170, 'NVDA': 875
                    }.get(symbol, 100)
                    vwap = base_price * (0.98 + random.uniform(0, 0.03))
                    
                    # Occasionally create surge conditions (30% chance)
                    if random.random() < 0.3:
                        price = base_price * (1.01 + random.uniform(0, 0.02))
                        volume = random.randint(2000000, 5000000)
                    else:
                        price = base_price * (0.99 + random.uniform(0, 0.01))
                        volume = random.randint(500000, 1000000)
                    
                    scanner.update(symbol, price=price, volume=volume, vwap=vwap)
                
                time.sleep(1)
        
        except KeyboardInterrupt:
            print("\n\nScanner stopped by user")
            print(f"Ran for {iteration} iterations")
