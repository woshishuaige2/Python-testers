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
import signal

from conditions import (
    AlertCondition,
    AlertConditionSet,
    MarketData,
    PriceAboveVWAPCondition,
    PriceSurgeCondition,
    VolumeSurgeCondition
)

# Import TWS integration - REQUIRED
try:
    from tws_data_fetcher import create_tws_data_app, TWSDataApp
except ImportError:
    print("[ERROR] TWS integration not available. Install ibapi: pip install ibapi")
    exit(1)


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
    
    def get_relative_volume(self) -> float:
        """Calculate relative volume (current vs average)"""
        with self.lock:
            if not self.volume_history or len(self.volume_history) < 2:
                return 1.0
            
            volumes = [vol for _, vol in self.volume_history]
            if len(volumes) < 2:
                return 1.0
            
            avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else volumes[0]
            if avg_volume == 0:
                return 1.0
            
            return self.last_volume / avg_volume if self.last_volume else 1.0
    
    def get_status_summary(self) -> Dict[str, any]:
        """Get summary of current status for display"""
        with self.lock:
            return {
                'symbol': self.symbol,
                'price': self.last_price,
                'volume': self.last_volume,
                'vwap': self.last_vwap,
                'rel_volume': self.get_relative_volume(),
                'last_update': self.last_update,
                'data_points': len(self.price_history)
            }
    
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
        self.update_count = 0  # Track number of updates received
        
        # Initialize monitors with default conditions
        self._initialize_monitors()
    
    def _initialize_monitors(self):
        """Initialize monitors with default condition set"""
        for symbol in self.symbols:
            # Create default condition set
            condition_set = AlertConditionSet(f"{symbol}_default")
            condition_set.add_condition(PriceAboveVWAPCondition())
            condition_set.add_condition(PriceSurgeCondition())  # Uses PRICE_SURGE_THRESHOLD from conditions.py
            condition_set.add_condition(VolumeSurgeCondition())  # Uses VOLUME_SURGE_THRESHOLD from conditions.py
            
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
        
        # Increment update counter
        with self.lock:
            self.update_count += 1
        
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
    
    def get_all_statuses(self) -> List[Dict]:
        """Get status summary for all monitored symbols"""
        statuses = []
        for symbol in self.symbols:
            if symbol in self.monitors:
                statuses.append(self.monitors[symbol].get_status_summary())
        return statuses
    
    def stop(self):
        """Stop the scanner"""
        self.running = False


def clear_screen():
    """Clear the console screen - simplified to avoid hanging"""
    # Just print newlines instead of using os.system which can hang
    print("\n" * 50)


def display_status_table(scanner: RealtimeAlertScanner, alert_info: str = None):
    """Display a formatted table of all monitored symbols"""
    # Don't clear screen - just add separator
    print("\n" + "="*100)
    
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print(" "*35 + "REAL-TIME ALERT SCANNER")
    print("="*100)
    print(f"Current Time: {current_time} | Updates Received: {scanner.update_count}")
    print("="*100)
    
    # Table header
    print(f"\n{'SYMBOL':<8} {'PRICE':<12} {'VOLUME':<15} {'REL VOL':<10} {'VWAP':<12} {'LAST UPDATE':<20}")
    print("-"*100)
    
    # Directly access monitors to avoid lock contention
    for symbol in scanner.symbols:
        if symbol in scanner.monitors:
            monitor = scanner.monitors[symbol]
            
            # Get data without locking for too long
            try:
                price = monitor.last_price
                volume = monitor.last_volume
                vwap = monitor.last_vwap
                last_update = monitor.last_update
                
                # Calculate relative volume safely
                rel_vol = 1.0
                if len(monitor.volume_history) >= 2:
                    volumes = [vol for _, vol in list(monitor.volume_history)]
                    if len(volumes) > 1:
                        # Average of all volumes except the latest
                        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
                        if avg_volume > 0 and volume:
                            rel_vol = volume / avg_volume
                
                if price is None or price == 0:
                    price_str = "Waiting..."
                    volume_str = "--"
                    rel_vol_str = "--"
                    vwap_str = "--"
                    update_str = "No data"
                else:
                    price_str = f"${price:,.2f}"
                    volume_str = f"{volume:,}" if volume else "0"
                    rel_vol_str = f"{rel_vol:.2f}x" if rel_vol > 0 else "1.00x"
                    vwap_str = f"${vwap:,.2f}" if vwap else "--"
                    update_str = last_update.strftime("%H:%M:%S") if last_update else "N/A"
                
                print(f"{symbol:<8} {price_str:<12} {volume_str:<15} {rel_vol_str:<10} {vwap_str:<12} {update_str:<20}")
            except Exception as e:
                print(f"{symbol:<8} ERROR: {str(e)[:50]}")
    
    print("-"*100)
    
    # Show alert info if present
    if alert_info:
        print("\n" + "!"*100)
        print("🚨 ALERT TRIGGERED 🚨")
        print("!"*100)
        print(alert_info)
        print("!"*100)
    
    print("\n[INFO] Table updates every 5 seconds | Press Ctrl+C to stop\n")


# Example usage
if __name__ == "__main__":
    # Global variable to track if we should exit
    should_exit = False
    tws_app_global = None
    
    def signal_handler(sig, frame):
        """Handle Ctrl+C gracefully"""
        global should_exit, tws_app_global
        should_exit = True
        print("\n\n" + "="*60)
        print("[INFO] Shutting down scanner...")
        if tws_app_global:
            try:
                tws_app_global.disconnect()
                print("[TWS] Disconnected")
            except:
                pass
        print("[INFO] Scanner stopped")
        print("="*60 + "\n")
        sys.exit(0)
    
    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
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
    
    # Create scanner
    scanner = RealtimeAlertScanner(symbols=symbols)
    
    # Track last alert for display
    last_alert_info = {'message': None, 'triggered': False}
    
    # Alert handler that captures alert info
    def alert_handler(symbol, timestamp, reasons, data):
        alert_msg = (
            f"Symbol: {symbol}\n"
            f"Time: {timestamp}\n"
            f"Price: ${data.price:.2f} | Volume: {data.volume:,} | VWAP: ${data.vwap:.2f}\n"
            f"Conditions: {reasons}"
        )
        last_alert_info['message'] = alert_msg
        last_alert_info['triggered'] = True
    
    scanner.on_alert(alert_handler)
    
    # Connect to TWS
    print("\n+-- TWS CONNECTION")
    print("|   Connecting to TWS/IB Gateway (paper trading - port 7497)...")
    print("|   Make sure TWS or IB Gateway is running with API enabled!")
    
    try:
        tws_app = create_tws_data_app(host="127.0.0.1", port=7497, client_id=902)
        tws_app_global = tws_app  # Store for signal handler
        
        if not tws_app:
            print("|   [ERROR] Could not connect to TWS")
            print("|   [INFO] Make sure:")
            print("|          - TWS/IB Gateway is running")
            print("|          - API is enabled in TWS settings")
            print("|          - Port 7497 is correct (paper trading)")
            print("+" + "-"*68 + "\n")
            exit(1)
        
        print("|   [OK] Connected to TWS")
    except Exception as e:
        print(f"|   [ERROR] TWS Error: {str(e)}")
        print("|   [INFO] Make sure:")
        print("|          - TWS/IB Gateway is running")
        print("|          - API is enabled in TWS settings")
        print("|          - Port 7497 is correct (paper trading)")
        print("+" + "-"*68 + "\n")
        exit(1)
    
    print("+" + "-"*68 + "\n")
    
    # Create callback for each symbol
    def create_tws_callback(scanner_obj, symbol):
        def callback(sym, price, volume, vwap, timestamp):
            """Callback receives: symbol, price, volume, vwap, timestamp"""
            # Debug: Print first few updates to confirm data is being received
            if scanner_obj.update_count < 10:
                print(f"[DEBUG {scanner_obj.update_count + 1}] {sym} | Price: ${price:.2f} | Vol: {volume:,} | VWAP: ${vwap:.2f}")
            scanner_obj.update(sym, price=price, volume=volume, vwap=vwap)
        
        return callback
    
    # Subscribe to market data for all symbols
    print("[INFO] Subscribing to live market data...")
    for symbol in symbols:
        callback = create_tws_callback(scanner, symbol)
        tws_app.subscribe_realtime_data(symbol, callback)
        print(f"[OK] Subscribed to {symbol}")
    
    print(f"\n[OK] All symbols subscribed: {', '.join(symbols)}")
    print("[INFO] Waiting for initial data (10 seconds)...")
    print("[INFO] You should see [DEBUG] messages below if data is flowing...\n")
    
    time.sleep(10)  # Wait longer for initial data
    
    print(f"\n[INFO] Updates received so far: {scanner.update_count}")
    if scanner.update_count == 0:
        print("[WARN] No data received yet. Continuing to wait...")
    print("[INFO] Starting continuous monitoring...")
    print("[INFO] Press Ctrl+C to stop\n")
    
    # Display initial table
    display_status_table(scanner)
    
    last_table_update = time.time()
    table_update_interval = 5  # Update table every 5 seconds
    
    # Keep running continuously
    while not should_exit:
        time.sleep(0.5)  # Check frequently
        
        current_time = time.time()
        
        # Update table if: 1) interval passed, or 2) alert was triggered
        if (current_time - last_table_update >= table_update_interval) or last_alert_info['triggered']:
            if should_exit:
                break
            alert_msg = last_alert_info['message'] if last_alert_info['triggered'] else None
            display_status_table(scanner, alert_msg)
            last_table_update = current_time
            
            # Reset alert flag after displaying
            if last_alert_info['triggered']:
                last_alert_info['triggered'] = False
                # Keep message for a bit longer
                time.sleep(2)
    
    # Cleanup on exit
    if tws_app:
        tws_app.disconnect()
    print("\n[INFO] Scanner terminated\n")
