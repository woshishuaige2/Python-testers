"""
Backtest Alert Scanner
Backtests alert conditions against historical data for a specific date.

WORKFLOW:
1. User inputs symbols (up to 5) and target date
2. Scanner initializes with default conditions (Price>VWAP, Price Surge, Volume Surge)
3. Historical OHLCV data is loaded (from data provider or simulated)
4. For each candle, scanner checks if ALL conditions are met
5. When all conditions trigger, an alert is recorded with timestamp and details
6. Results displayed in formatted console output and exported to JSON
7. Optional: Check TWS connectivity for future live integration

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
    VolumeSurgeCondition
)

# Try to import TWS integration for connectivity check
try:
    from tws_data_fetcher import create_tws_data_app, TWSDataApp
    HAS_TWS = True
except ImportError:
    HAS_TWS = False
    print("[WARN] TWS integration not available. Install ibapi: pip install ibapi")


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
            condition_set.add_condition(PriceSurgeCondition(surge_threshold=2))
            condition_set.add_condition(VolumeSurgeCondition(surge_threshold=3.0))
            
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
                    # Parse date string (format: "20241215 09:30:00" or "20241215")
                    try:
                        if len(bar['date']) > 8:  # Has time component
                            bar_datetime = datetime.strptime(bar['date'], "%Y%m%d %H:%M:%S")
                        else:  # Date only
                            bar_datetime = datetime.strptime(bar['date'], "%Y%m%d")
                        
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
    from datetime import datetime, timedelta
    import time
    
    print("\n" + "="*70)
    print(" "*20 + "BACKTEST ALERT SCANNER")
    print("="*70 + "\n")
    
    # Connect to TWS
    tws_app = None
    use_tws = False
    
    if HAS_TWS:
        print("+-- TWS CONNECTION")
        print("|   Connecting to TWS/IB Gateway (paper trading - port 7497)...")
        try:
            tws_app = create_tws_data_app(host="127.0.0.1", port=7497, client_id=901)
            if tws_app:
                print("|   [OK] Connected to TWS")
                print("|   [OK] Will fetch historical data from IBKR")
                use_tws = True
            else:
                print("|   [WARN] Could not connect to TWS")
                print("|   [INFO] Will use simulated data instead")
        except Exception as e:
            print(f"|   [WARN] TWS Error: {str(e)[:40]}...")
            print("|   [INFO] Will use simulated data instead")
        print("+" + "-"*68 + "\n")
    else:
        print("+-- TWS NOT AVAILABLE")
        print("|   [INFO] TWS integration not installed")
        print("|   [INFO] Install with: pip install ibapi")
        print("|   [INFO] Will use simulated data")
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
    print("|   [OK] Default conditions: Price>VWAP, Price Surge (0.5%), Volume Surge (2x)")
    print("+" + "-"*68)
    
    print("\n[!] ALERT LOGIC: ALL 3 conditions must be TRUE simultaneously:")
    print("    1. Price > VWAP")
    print("    2. Price surge >= 0.5% in last 10 seconds")
    print("    3. Volume surge >= 2x in last 10 seconds\n")
    
    # Load data from TWS or simulate
    if use_tws and tws_app:
        print("[INFO] Fetching historical data from IBKR TWS...\n")
        data_loaded = scanner.load_data_from_tws(
            tws_app=tws_app,
            bar_size="10 secs",  # 10-second bars for surge detection
            duration="1 D"  # 1 day of data
        )
        
        if not data_loaded:
            print("\n[WARN] Failed to load data from TWS. Exiting.")
            print("[INFO] Make sure:")
            print("       - TWS/IB Gateway is running")
            print("       - API is enabled in TWS settings")
            print("       - You have market data subscription for the symbols")
            print("       - The backtest date has available data\n")
            tws_app.disconnect()
            exit(1)
    else:
        print("\n[!] NOTE: Using simulated 10-second candle data with random surges.")
        print("    ~5% of candles will have price surges (0.5-2%) and volume spikes (2-5x).")
        print("    For real data: Connect to TWS with live market data subscription.\n")
        
        # Progress indicator
        print("\n+-- LOADING SIMULATED DATA")
        for symbol in symbols:
            print(f"|   Loading {symbol}...", end=" ")
            
            # Use realistic base prices (as of Dec 2025)
            base_price = {
                'AAPL': 195, 'MSFT': 425, 'GOOGL': 175, 
                'TSLA': 350, 'AMZN': 210, 'NVDA': 140,
                'META': 585, 'NFLX': 850, 'AMD': 145,
                'YCBD': 1.10, 'THH': 2.50, 'TLRY': 11.50,  # Recent prices
                'SPY': 590, 'QQQ': 515, 'IWM': 220  # ETFs
            }.get(symbol, 50)  # Default to $50 for unknown symbols
            vwap_base = base_price * 0.995
            
            # Simulate intraday data with 10-SECOND candles (more realistic for surge detection)
            candle_count = 0
            import random
            random.seed(hash(symbol) % 1000)  # Different but reproducible seed per symbol
            
            # Start at 9:30 AM market open
            current_time = datetime.combine(backtest_date.date(), __import__('datetime').time(9, 30))
            market_close = datetime.combine(backtest_date.date(), __import__('datetime').time(16, 0))
            
            # Track intraday trend (slight upward/downward bias throughout the day)
            daily_trend = random.uniform(-0.05, 0.15)  # -5% to +15% overall daily movement
            
            while current_time < market_close:
                # Calculate time progression through the day (0.0 to 1.0)
                seconds_since_open = (current_time - datetime.combine(backtest_date.date(), __import__('datetime').time(9, 30))).total_seconds()
                day_progress = seconds_since_open / (6.5 * 3600)  # 6.5 hour trading day
                
                # Base price drifts gradually with daily trend, oscillates around base
                trend_component = base_price * daily_trend * day_progress
                oscillation = base_price * random.uniform(-0.02, 0.02)  # ±2% random oscillation
                price = base_price + trend_component + oscillation
                
                # Normal volume: stay within realistic range
                base_volume = 1000000 if base_price > 100 else 500000 if base_price > 10 else 100000
                volume = base_volume * random.uniform(0.5, 1.5)
                
                # Occasionally create REAL surge conditions (5% of time)
                if random.random() < 0.05:
                    # Price surge: 0.5% to 2% spike above current level
                    surge_pct = random.uniform(0.005, 0.02)
                    price = price * (1 + surge_pct)
                    # Volume spike: 2x to 5x normal volume
                    volume = volume * random.uniform(2.0, 5.0)
                
                # Keep price within reasonable bounds (±20% of base)
                price = max(base_price * 0.8, min(price, base_price * 1.2))
                
                # Calculate VWAP (stays close to base price)
                vwap = base_price * (0.995 + random.uniform(-0.01, 0.01))
                
                scanner.add_data(
                    symbol,
                    current_time,
                    open_price=price * 0.999,
                    high_price=price * 1.002,
                    low_price=price * 0.998,
                    close_price=price,
                    volume=int(volume),
                    vwap=vwap
                )
                
                candle_count += 1
                current_time += timedelta(seconds=10)
            
            print(f"[OK] {candle_count} candles")
        
        print("+" + "-"*68)
    
    # Run backtest
    alerts = scanner.run_backtest()
    
    # Cleanup
    if tws_app:
        tws_app.disconnect()
        print("[TWS] Disconnected\n")
    
    print("="*70)
    print(" "*24 + "BACKTEST COMPLETE")
    print("="*70 + "\n")
