"""
Backtest Alert Scanner
Backtests alert conditions against historical data for a specific date.

WORKFLOW:
1. User inputs symbols (up to 5) and target date
2. Scanner initializes with default conditions (Price>VWAP, Price Surge)
3. Historical OHLCV data is loaded from IBKR TWS
4. For each candle, scanner checks if ALL conditions are met
5. When all conditions trigger, an alert is recorded with timestamp and details
6. Results displayed in formatted console output and exported to JSON

Key Features:
- Processes up to 5 symbols simultaneously
- Maintains price/volume history for surge detection
- Extensible condition system for adding custom rules
- JSON export for further analysis
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import json

from conditions import (
    AlertConditionSet,
    MarketData,
    PriceAboveVWAPCondition,
    PriceSurgeCondition,
    VolumeSpike10sCondition,
    PRICE_SURGE_THRESHOLD
)

# Import TWS integration - REQUIRED
try:
    from tws_data_fetcher import create_tws_data_app, TWSDataApp
except ImportError:
    print("[ERROR] TWS integration not available. Install ibapi: pip install ibapi")
    exit(1)


@dataclass
class BacktestAlert:
    """Container for a triggered alert during backtest"""
    symbol: str
    timestamp: datetime
    price: float
    volume: int
    vwap: float
    conditions_triggered: List[str]
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            'price': f"${self.price:.2f}",
            'volume': f"{self.volume:,}",
            'vwap': f"${self.vwap:.2f}",
            'conditions': self.conditions_triggered
        }
    
    def __str__(self) -> str:
        """String representation"""
        return (
            f"[{self.timestamp.strftime('%H:%M:%S')}] {self.symbol}: "
            f"Price ${self.price:.2f} | Volume {self.volume:,} | "
            f"Conditions: {' | '.join(self.conditions_triggered)}"
        )


class BacktestSymbolData:
    """Holds OHLCV data for backtesting"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.data: List[Dict] = []  # List of {timestamp, open, high, low, close, volume, vwap}
    
    def add_candle(
        self,
        timestamp: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        vwap: float
    ):
        """Add a candle to the data"""
        self.data.append({
            'timestamp': timestamp,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'vwap': vwap,
            'intraday_ticks': []  # For intraday tick data
        })
    
    def add_intraday_tick(self, timestamp: datetime, price: float, volume: int):
        """Add intraday tick data within a candle"""
        if self.data:
            self.data[-1]['intraday_ticks'].append({
                'timestamp': timestamp,
                'price': price,
                'volume': volume
            })
    
    def get_candle_at(self, timestamp: datetime) -> Optional[Dict]:
        """Get candle containing the timestamp"""
        for candle in self.data:
            if candle['timestamp'].date() == timestamp.date():
                return candle
        return None
    
    def get_all_candles_for_date(self, date: datetime) -> List[Dict]:
        """Get all candles for a specific date"""
        target_date = date.date() if isinstance(date, datetime) else date
        return [
            c for c in self.data
            if c['timestamp'].date() == target_date
        ]


class BacktestAlertScanner:
    """
    Backtests alert conditions against historical data.
    
    Usage:
        scanner = BacktestAlertScanner(symbols=['AAPL', 'MSFT'], date='2024-01-15')
        
        # Add historical data
        scanner.add_data('AAPL', timestamp, open, high, low, close, volume, vwap)
        
        # Run backtest
        alerts = scanner.run_backtest()
    """
    
    def __init__(self, symbols: List[str], date: str, max_symbols: int = 5):
        """
        Initialize backtest scanner.
        
        Args:
            symbols: List of symbols to backtest (up to 5)
            date: Date to backtest (format: 'YYYY-MM-DD')
            max_symbols: Maximum number of symbols allowed
        """
        if len(symbols) > max_symbols:
            raise ValueError(f"Maximum {max_symbols} symbols allowed, got {len(symbols)}")
        
        self.symbols = symbols
        self.date = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        
        # Initialize data storage
        self.symbol_data: Dict[str, BacktestSymbolData] = {
            symbol: BacktestSymbolData(symbol) for symbol in symbols
        }
        
        # Alerts storage
        self.alerts: Dict[str, List[BacktestAlert]] = {symbol: [] for symbol in symbols}
        
        # Condition set per symbol (use defaults)
        self.condition_sets: Dict[str, AlertConditionSet] = {}
        self._initialize_condition_sets()
    
    def _initialize_condition_sets(self):
        """Initialize condition sets with defaults for each symbol"""
        for symbol in self.symbols:
            condition_set = AlertConditionSet(f"{symbol}_backtest")
            condition_set.add_condition(PriceAboveVWAPCondition())
            condition_set.add_condition(PriceSurgeCondition())  # Uses PRICE_SURGE_THRESHOLD from conditions.py
            condition_set.add_condition(VolumeSpike10sCondition())  # 10s volume > 5x avg of past 20 bars
            
            self.condition_sets[symbol] = condition_set
    
    def set_conditions(self, symbol: str, condition_set: AlertConditionSet):
        """Override conditions for a specific symbol"""
        if symbol not in self.symbols:
            raise ValueError(f"Symbol {symbol} not in backtest list")
        
        self.condition_sets[symbol] = condition_set
    
    def add_data(
        self,
        symbol: str,
        timestamp: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        vwap: float
    ):
        """Add OHLCV candle data"""
        if symbol not in self.symbol_data:
            raise ValueError(f"Symbol {symbol} not in backtest list")
        
        self.symbol_data[symbol].add_candle(
            timestamp, open_price, high_price, low_price, close_price, volume, vwap
        )
    
    def add_intraday_tick(
        self,
        symbol: str,
        timestamp: datetime,
        price: float,
        volume: int
    ):
        """Add intraday tick data for more granular analysis"""
        if symbol not in self.symbol_data:
            raise ValueError(f"Symbol {symbol} not in backtest list")
        
        self.symbol_data[symbol].add_intraday_tick(timestamp, price, volume)
    
    def load_data_from_tws(
        self,
        tws_app: 'TWSDataApp',
        bar_size: str = "10 secs",
        duration: str = "1 D"
    ) -> bool:
        """
        Load historical data from TWS for all symbols.
        
        Args:
            tws_app: Connected TWSDataApp instance
            bar_size: Bar size (e.g., "10 secs", "1 min", "5 mins")
            duration: Duration (e.g., "1 D", "1 W")
        
        Returns:
            True if successful, False otherwise
        """
        print(f"\n+-- LOADING DATA FROM TWS")
        print(f"|   Bar Size: {bar_size}, Duration: {duration}")
        
        # Set end time to market close on backtest date (4:00 PM)
        end_datetime = datetime.combine(
            self.date.date(),
            datetime.strptime("16:00:00", "%H:%M:%S").time()
        )
        
        success = True
        for symbol in self.symbols:
            print(f"|   Fetching {symbol}...", end=" ", flush=True)
            
            try:
                bars = tws_app.fetch_historical_bars(
                    symbol=symbol,
                    end_date=end_datetime,
                    duration=duration,
                    bar_size=bar_size,
                    what_to_show="TRADES"
                )
                
                if not bars:
                    print(f"[FAIL] No data received")
                    success = False
                    continue
                
                # Filter bars for the target date
                bars_for_date = []
                for bar in bars:
                    # Parse date string (format: "20241215 09:30:00" or "20241215" or "20241215 09:30:00 US/Eastern")
                    try:
                        date_str = bar['date']
                        
                        # Remove timezone if present (e.g., " US/Eastern")
                        if ' ' in date_str:
                            parts = date_str.split()
                            # Check if last part looks like a timezone (contains '/')
                            if len(parts) >= 3 and '/' in parts[-1]:
                                # Has timezone, remove it
                                date_str = ' '.join(parts[:-1])
                        
                        # Now parse the cleaned date string
                        if len(date_str) > 8:  # Has time component
                            bar_datetime = datetime.strptime(date_str, "%Y%m%d %H:%M:%S")
                        else:  # Date only
                            bar_datetime = datetime.strptime(date_str, "%Y%m%d")
                        
                        # Only include bars from the target date
                        if bar_datetime.date() == self.date.date():
                            bars_for_date.append((bar_datetime, bar))
                    except ValueError as e:
                        print(f"[WARN] Could not parse date: {bar['date']}")
                        continue
                
                if not bars_for_date:
                    print(f"[FAIL] No data for {self.date.strftime('%Y-%m-%d')}")
                    success = False
                    continue
                
                # Add bars to scanner
                for bar_datetime, bar in bars_for_date:
                    self.add_data(
                        symbol=symbol,
                        timestamp=bar_datetime,
                        open_price=bar['open'],
                        high_price=bar['high'],
                        low_price=bar['low'],
                        close_price=bar['close'],
                        volume=bar['volume'],
                        vwap=bar['average']  # TWS provides VWAP in 'average' field
                    )
                
                print(f"[OK] {len(bars_for_date)} bars")
                
            except Exception as e:
                print(f"[FAIL] {str(e)[:50]}")
                success = False
        
        print("+" + "-"*68)
        return success
    
    def run_backtest(self) -> Dict[str, List[BacktestAlert]]:
        """
        Run backtest for all symbols on the specified date.
        
        Returns:
            Dictionary mapping symbol -> list of BacktestAlert objects
        """
        print(f"\n{'='*70}")
        print(f"  BACKTESTING: {self.date.strftime('%Y-%m-%d')}")
        print(f"{'='*70}\n")
        
        for idx, symbol in enumerate(self.symbols, 1):
            print(f"[{idx}/{len(self.symbols)}] Processing {symbol}...", end=" ")
            candles = self.symbol_data[symbol].get_all_candles_for_date(self.date)
            
            if not candles:
                print(f"[FAIL] No data")
                continue
            
            print(f"[OK] {len(candles)} candles")
            
            # Build price and volume history for condition checking
            price_history = {}
            volume_history = {}
            
            # Sort by timestamp to ensure chronological order
            sorted_candles = sorted(candles, key=lambda x: x['timestamp'])
            
            for candle in sorted_candles:
                timestamp = candle['timestamp']
                price_history[timestamp] = candle['close']
                volume_history[timestamp] = candle['volume']
                
                # Check intraday ticks if available
                if candle.get('intraday_ticks'):
                    for tick in candle['intraday_ticks']:
                        tick_time = tick['timestamp']
                        price_history[tick_time] = tick['price']
                        volume_history[tick_time] = tick['volume']
            
            # Check conditions for each candle
            alert_count = 0
            for i, candle in enumerate(sorted_candles):
                # Build market data with history up to this point
                cutoff_time = candle['timestamp']
                historical_prices = {
                    ts: price for ts, price in price_history.items()
                    if ts <= cutoff_time
                }
                historical_volumes = {
                    ts: vol for ts, vol in volume_history.items()
                    if ts <= cutoff_time
                }
                
                market_data = MarketData(
                    symbol=symbol,
                    price=candle['close'],
                    volume=candle['volume'],
                    vwap=candle['vwap'],
                    timestamp=candle['timestamp'],
                    price_history=historical_prices,
                    volume_history=historical_volumes
                )
                
                # Check if conditions are met
                condition_set = self.condition_sets[symbol]
                if condition_set.check_all(market_data):
                    alert = BacktestAlert(
                        symbol=symbol,
                        timestamp=candle['timestamp'],
                        price=candle['close'],
                        volume=candle['volume'],
                        vwap=candle['vwap'],
                        conditions_triggered=self._extract_condition_reasons(condition_set)
                    )
                    self.alerts[symbol].append(alert)
                    alert_count += 1
                    print(f"    [ALERT] #{alert_count} at {alert.timestamp.strftime('%H:%M:%S')}")
            
            if alert_count == 0:
                print(f"    [OK] No alerts triggered")
        
        self._print_summary()
        return self.alerts
    
    def _extract_condition_reasons(self, condition_set: AlertConditionSet) -> List[str]:
        """Extract condition trigger reasons from condition set"""
        reasons = []
        for condition in condition_set.conditions:
            if condition.triggered_reason:
                reasons.append(f"{condition.name}: {condition.triggered_reason}")
        return reasons
    
    def get_alerts_for_symbol(self, symbol: str) -> List[BacktestAlert]:
        """Get all alerts for a specific symbol"""
        if symbol not in self.alerts:
            raise ValueError(f"Symbol {symbol} not in backtest list")
        return self.alerts[symbol]
    
    def _print_summary(self):
        """Print backtest summary"""
        print(f"\n{'='*70}")
        print(f"  RESULTS SUMMARY")
        print(f"{'='*70}\n")
        
        total_alerts = 0
        
        # Create summary table
        for symbol in self.symbols:
            alerts_count = len(self.alerts[symbol])
            total_alerts += alerts_count
            
            # Symbol header
            print(f"+-- {symbol} {'-' * (64 - len(symbol))}")
            
            if alerts_count == 0:
                print(f"|   No alerts triggered")
            else:
                print(f"|   {alerts_count} alert{'s' if alerts_count != 1 else ''} triggered:")
                print(f"|")
                for idx, alert in enumerate(self.alerts[symbol], 1):
                    time_str = alert.timestamp.strftime('%H:%M:%S')
                    print(f"|   [{idx}] {time_str} - Price: {alert.price:.2f} | Vol: {alert.volume:,}")
                    
                    # Show first condition reason (truncated if too long)
                    if alert.conditions_triggered:
                        reason = alert.conditions_triggered[0]
                        if len(reason) > 60:
                            reason = reason[:57] + "..."
                        print(f"|       +-- {reason}")
            
            print(f"+{'-' * 68}\n")
        
        # Overall summary
        print(f"{'-'*70}")
        print(f"  TOTAL: {total_alerts} alert{'s' if total_alerts != 1 else ''} across {len(self.symbols)} symbol{'s' if len(self.symbols) != 1 else ''}")
        print(f"{'-'*70}\n")
    
    def export_alerts_to_json(self, filename: str = None) -> str:
        """
        Export alerts to JSON file.
        
        Args:
            filename: Optional filename (default: backtest_alerts_YYYY-MM-DD.json)
            
        Returns:
            Filename of exported file
        """
        if not filename:
            filename = f"backtest_alerts_{self.date.strftime('%Y-%m-%d')}.json"
        
        export_data = {}
        for symbol in self.symbols:
            export_data[symbol] = [alert.to_dict() for alert in self.alerts[symbol]]
        
        with open(filename, 'w') as f:
            json.dump(export_data, f, indent=2)
        
        print(f"Alerts exported to {filename}")
        return filename


# Example usage
if __name__ == "__main__":
    from datetime import datetime
    import time
    
    print("\n" + "="*70)
    print(" "*20 + "BACKTEST ALERT SCANNER")
    print("="*70 + "\n")
    
    # Connect to TWS - REQUIRED
    print("+-- TWS CONNECTION")
    print("|   Connecting to TWS/IB Gateway (paper trading - port 7497)...")
    try:
        tws_app = create_tws_data_app(host="127.0.0.1", port=7497, client_id=901)
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
    
    # Get user input for symbols
    print("SYMBOLS (up to 5, comma-separated)")
    symbols_input = input("> ").strip().upper()
    symbols = [s.strip() for s in symbols_input.split(',') if s.strip()]
    
    if len(symbols) > 5:
        print(f"\n[WARN] Maximum 5 symbols allowed. Using first 5: {', '.join(symbols[:5])}")
        symbols = symbols[:5]
    
    if not symbols:
        print("\n[FAIL] No symbols provided. Exiting.")
        exit(1)
    
    print(f"[OK] Selected: {', '.join(symbols)}\n")
    
    # Get user input for date
    print("BACKTEST DATE (YYYY-MM-DD)")
    date_input = input("> ").strip()
    
    try:
        backtest_date = datetime.strptime(date_input, "%Y-%m-%d")
        print(f"[OK] Date: {backtest_date.strftime('%B %d, %Y')}\n")
    except ValueError:
        print("\n[FAIL] Invalid date format. Use YYYY-MM-DD")
        exit(1)
    
    # Create backtest scanner
    print("+-- INITIALIZING SCANNER")
    scanner = BacktestAlertScanner(symbols=symbols, date=date_input)
    print("|   [OK] Scanner initialized")
    print(f"|   [OK] Conditions: Price>VWAP, Price Surge ({PRICE_SURGE_THRESHOLD}%), Volume Spike (5x)")
    print(f"|   [INFO] Configure thresholds in conditions.py")
    print("+" + "-"*68)
    
    print("\n[!] ALERT LOGIC: ALL 3 conditions must be TRUE simultaneously:")
    print("    1. Price > VWAP")
    print(f"    2. Price surge >= {PRICE_SURGE_THRESHOLD}% in last 10 seconds")
    print(f"    3. Volume spike: current 10s > 5x average of past 20 bars\n")
    
    # Load data from TWS
    print("[INFO] Fetching historical data from IBKR TWS...\n")
    data_loaded = scanner.load_data_from_tws(
        tws_app=tws_app,
        bar_size="10 secs",  # 10-second bars for surge detection
        duration="1 D"  # 1 day of data
    )
    
    if not data_loaded:
        print("\n[ERROR] Failed to load data from TWS.")
        print("[INFO] Make sure:")
        print("       - TWS/IB Gateway is running")
        print("       - API is enabled in TWS settings")
        print("       - You have market data subscription for the symbols")
        print("       - The backtest date has available data\n")
        tws_app.disconnect()
        exit(1)
    
    # Run backtest
    alerts = scanner.run_backtest()
    
    # Cleanup
    if tws_app:
        tws_app.disconnect()
        print("[TWS] Disconnected\n")
    
    print("="*70)
    print(" "*24 + "BACKTEST COMPLETE")
    print("="*70 + "\n")
