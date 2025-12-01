"""
Ross Cameron Style Day Trading Algorithm - BACKTESTING VERSION
================================================================

This script backtests the exact same strategy as RossCameron-Algo.py using historical data.
Strategy logic is imported from RossCameron-Strategy.py - any changes to strategy
parameters will automatically apply to both live trading and backtesting.

Entry Conditions (ALL must be met):
- Pullback Pattern: Stock must show surge → pullback → first candle making new high after dip
- MACD Positive: MACD line above signal line with positive histogram (12/26/9 periods)
- Volume Confirmation: No volume topping pattern, less than 4/5 red candles during pullback
- VWAP Filter: Price must be above VWAP (long trades only, calculated from 1-minute bars)
- Risk Management: Position sized to risk max 10% of account balance

Exit Conditions:
- Dynamic Exit: Candle Under Candle reversal (latest bar's low < previous bar's low)
- Stop Loss: Structural stop at pullback low price (not fixed percentage)
- Backup Profit Target: +10% limit order (cancelled if dynamic exit triggers first)
- End of Day: All positions closed at 3:25 PM EST

Data Requirements:
- 10-second bars for pattern/MACD/volume analysis
- 1-minute bars for VWAP calculation
- Uses IBKR historical data (TWS/Gateway must be running)

Usage:
1. Ensure TWS/Gateway is running on port 7497
2. Run backtest on specific date ranges
3. Analyze performance metrics (win rate, profit factor, max drawdown, etc.)

TROUBLESHOOTING:
================
If data fetch fails:

1. TWS/Gateway Connection:
   - Ensure TWS/Gateway is running on port 7497
   - Check API connection is enabled in TWS settings
   - Verify correct port number (7497 for paper, 7496 for live)

2. Symbol Issues:
   - Check symbol is valid US stock
   - Symbol must be actively traded
   - Some low-volume stocks may have limited historical data

3. Data Limitations:
   - 10-second bars: Maximum 1 trading day per request
   - 1-minute bars: Can fetch multiple days
   - For multi-day backtests, run each day separately

4. Recommended Test Setup:
   - Symbol: AAPL, TSLA, or NVDA (liquid stocks with reliable data)
   - Date: Single trading day (e.g., 2024-11-25)
   - Ensure date is a weekday when market was open

Example:
   Symbol: AAPL
   Start: 2024-11-25
   End: 2024-11-25  (same day for 10-sec bars)
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import pytz
import importlib.util
import os

# Import shared strategy logic (handle hyphen in filename)
_strategy_path = os.path.join(os.path.dirname(__file__), 'RossCameron-Strategy.py')
_spec = importlib.util.spec_from_file_location("strategy", _strategy_path)
strategy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strategy)

# Import strategy components
StrategyConfig = strategy.StrategyConfig
check_all_entry_conditions = strategy.check_all_entry_conditions
check_dynamic_exit = strategy.check_dynamic_exit
check_stop_loss_hit = strategy.check_stop_loss_hit
check_profit_target_hit = strategy.check_profit_target_hit
check_end_of_day = strategy.check_end_of_day
calculate_position_size = strategy.calculate_position_size
calculate_entry_exit_prices = strategy.calculate_entry_exit_prices
calculate_commission = strategy.calculate_commission
get_strategy_summary = strategy.get_strategy_summary

# PAPER trading port for data fetching
port = 7497
clientId = 10  # Different from live trading


class DataFetcher(EWrapper, EClient):
    """Fetch historical data from IBKR"""
    def __init__(self):
        EClient.__init__(self, self)
        self.bars = []
        self.data_ready = False
        
    def historicalData(self, reqId, bar):
        self.bars.append({
            'date': bar.date,
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume
        })
    
    def historicalDataEnd(self, reqId, start, end):
        self.data_ready = True
        print(f"Data received: {len(self.bars)} bars")
    
    def error(self, *args):
        if len(args) >= 3:
            reqId, errorCode, errorString = args[0], args[1], args[2]
            if errorCode != 2104 and errorCode != 2106 and errorCode != 2158:
                print(f"Error {errorCode}: {errorString}")


def fetch_historical_data_ibkr(symbol, start_date, end_date, bar_size="10 secs"):
    """
    Fetch historical data from IBKR
    
    Parameters:
    - symbol: Stock ticker (e.g., "AAPL")
    - start_date: Start date (YYYY-MM-DD)
    - end_date: End date (YYYY-MM-DD)
    - bar_size: "10 secs" or "1 min"
    
    Returns: pandas DataFrame with OHLCV data
    """
    print(f"\nFetching {bar_size} bars for {symbol} from {start_date} to {end_date}...")
    
    app = DataFetcher()
    app.connect("127.0.0.1", port, clientId)
    
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(2)
    
    # Create contract
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    
    # Calculate duration
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    days = (end - start).days + 1
    
    # Request data
    if bar_size == "10 secs":
        duration = f"{min(days * 86400, 86400)} S"  # Max 1 day for 10-sec bars
    else:
        duration = f"{days} D"
    
    # Format end datetime with Eastern timezone (YYYYMMDD HH:MM:SS TZ format with spaces)
    end_datetime = end.strftime("%Y%m%d 23:59:59 US/Eastern")
    app.reqHistoricalData(1, contract, end_datetime, duration, bar_size, "TRADES", 1, 1, False, [])
    
    # Wait for data
    timeout = 30
    waited = 0
    while not app.data_ready and waited < timeout:
        time.sleep(0.5)
        waited += 0.5
    
    app.disconnect()
    
    if len(app.bars) == 0:
        print(f"WARNING: No data received for {symbol}")
        return pd.DataFrame()
    
    df = pd.DataFrame(app.bars)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    
    print(f"✓ Fetched {len(df)} bars")
    return df


class BacktestEngine:
    """Backtest engine that simulates trading"""
    
    def __init__(self, initial_capital=500.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.position = None  # Current position: {'entry_price', 'stop_price', 'profit_price', 'shares', 'entry_time', 'entry_bar_idx'}
        self.trades = []  # List of completed trades
        self.equity_curve = []  # Track capital over time
        
    def check_entry_conditions(self, bars_10s, bars_1m, current_bar_idx):
        """
        Check if all entry conditions are met using shared strategy logic
        Returns: (should_enter, entry_price, stop_price, profit_price, shares)
        """
        # Need sufficient data
        if len(bars_10s) < 30 or len(bars_1m) < 10:
            return False, None, None, None, None
        
        # Get current price (use close of current bar as "current price")
        current_price = bars_10s[-1]['close']
        
        # Check all conditions using shared strategy module
        all_ok, results, pullback_low = check_all_entry_conditions(bars_10s, bars_1m, current_price)
        
        if not all_ok or pullback_low is None:
            return False, None, None, None, None
        
        # Calculate entry parameters using shared strategy module
        entry_price, stop_price, profit_price = calculate_entry_exit_prices(current_price, pullback_low)
        
        if entry_price is None:  # Invalid prices
            return False, None, None, None, None
        
        shares = calculate_position_size(self.capital, entry_price, stop_price)
        
        # Check if we have enough capital
        cost = entry_price * shares
        if cost > self.capital:
            return False, None, None, None, None
        
        return True, entry_price, stop_price, profit_price, shares
    
    def enter_position(self, entry_price, stop_price, profit_price, shares, entry_time, entry_bar_idx):
        """Enter a new position"""
        cost = entry_price * shares
        buy_commission = calculate_commission(shares, cost, is_sell=False)
        
        self.position = {
            'entry_price': entry_price,
            'stop_price': stop_price,
            'profit_price': profit_price,
            'shares': shares,
            'entry_time': entry_time,
            'entry_bar_idx': entry_bar_idx,
            'buy_commission': buy_commission
        }
        
        self.capital -= (cost + buy_commission)
        
        print(f"\n{'='*70}")
        print(f"[{entry_time}] ENTRY: {shares} shares @ ${entry_price:.2f}")
        print(f"  Stop: ${stop_price:.2f} | Target: ${profit_price:.2f}")
        print(f"  Cost: ${cost:.2f} + Commission: ${buy_commission:.2f} = ${cost + buy_commission:.2f}")
        print(f"  Remaining capital: ${self.capital:.2f}")
        print(f"{'='*70}")
    
    def exit_position(self, exit_price, exit_time, exit_reason):
        """Exit current position"""
        if self.position is None:
            return
        
        shares = self.position['shares']
        entry_price = self.position['entry_price']
        buy_commission = self.position.get('buy_commission', 0)
        
        proceeds = exit_price * shares
        sell_commission = calculate_commission(shares, proceeds, is_sell=True)
        self.capital += (proceeds - sell_commission)
        
        pnl_gross = (exit_price - entry_price) * shares
        total_commission = buy_commission + sell_commission
        pnl_net = pnl_gross - total_commission
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        
        # Record trade
        trade = {
            'entry_time': self.position['entry_time'],
            'exit_time': exit_time,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'shares': shares,
            'pnl': pnl_net,  # Net P&L after BOTH commissions
            'pnl_gross': pnl_gross,
            'commission': total_commission,
            'pnl_pct': pnl_pct,
            'exit_reason': exit_reason
        }
        self.trades.append(trade)
        
        print(f"\n{'='*70}")
        print(f"[{exit_time}] EXIT ({exit_reason}): {shares} shares @ ${exit_price:.2f}")
        print(f"  Gross P&L: ${pnl_gross:+.2f} | Commission: ${total_commission:.2f} | Net P&L: ${pnl_net:+.2f} ({pnl_pct:+.2f}%)")
        print(f"  Capital: ${self.capital:.2f}")
        print(f"{'='*70}")
        
        self.position = None
    
    def check_exit_conditions(self, bars_10s, current_bar_idx, current_time):
        """
        Check if any exit conditions are met using shared strategy logic
        Returns: (should_exit, exit_price, exit_reason)
        """
        if self.position is None:
            return False, None, None
        
        current_bar = bars_10s[-1]
        current_price = current_bar['close']
        
        # Check stop loss using shared strategy module
        if check_stop_loss_hit(current_bar, self.position['stop_price']):
            return True, self.position['stop_price'], "STOP LOSS"
        
        # Check profit target using shared strategy module
        if check_profit_target_hit(current_bar, self.position['profit_price']):
            return True, self.position['profit_price'], "PROFIT TARGET"
        
        # Check dynamic exit (Candle Under Candle) using shared strategy module
        # Only check bars since entry
        bars_since_entry = bars_10s[self.position['entry_bar_idx']:]
        if len(bars_since_entry) >= 2:
            should_exit, msg = check_dynamic_exit(bars_since_entry)
            if should_exit:
                return True, current_price, "DYNAMIC EXIT"
        
        # Check end of day using shared strategy module
        if check_end_of_day(current_time):
            return True, current_price, "END OF DAY"
        
        return False, None, None
    
    def run_backtest(self, df_10s, df_1m, symbol, start_date, end_date):
        """
        Run backtest on historical data
        
        Parameters:
        - df_10s: DataFrame with 10-second bars
        - df_1m: DataFrame with 1-minute bars
        - symbol: Stock symbol
        - start_date: Start date string
        - end_date: End date string
        """
        print(f"\n{'='*70}")
        print(f"BACKTESTING: {symbol}")
        print(f"Period: {start_date} to {end_date}")
        print(f"Initial Capital: ${self.initial_capital:.2f}")
        print(f"{'='*70}\n")
        
        # Convert to list of dicts for compatibility with existing functions
        bars_10s_list = df_10s.to_dict('records')
        bars_1m_list = df_1m.to_dict('records')
        
        # Ensure dates are datetime objects with timezone
        est = pytz.timezone('US/Eastern')
        for bar in bars_10s_list:
            if not isinstance(bar['date'], datetime):
                bar['date'] = pd.to_datetime(bar['date'])
            if bar['date'].tzinfo is None:
                bar['date'] = est.localize(bar['date'])
        
        for bar in bars_1m_list:
            if not isinstance(bar['date'], datetime):
                bar['date'] = pd.to_datetime(bar['date'])
            if bar['date'].tzinfo is None:
                bar['date'] = est.localize(bar['date'])
        
        # Simulate bar-by-bar
        lookback_bars = 360  # 1 hour of 10-sec bars for pattern detection
        lookback_1m = StrategyConfig.VWAP_LOOKBACK_BARS  # Full day of 1-min bars for VWAP
        
        for i in range(lookback_bars, len(bars_10s_list)):
            current_bar = bars_10s_list[i]
            current_time = current_bar['date']
            
            # Skip pre-market (before 9:30 AM) and after hours (after 3:30 PM)
            if current_time.hour < 9 or (current_time.hour == 9 and current_time.minute < 30):
                continue
            if current_time.hour > 15 or (current_time.hour == 15 and current_time.minute > 30):
                continue
            
            # Get recent bars for analysis
            recent_10s = bars_10s_list[i-lookback_bars:i+1]
            
            # Get 1-min bars up to current time
            recent_1m = [b for b in bars_1m_list if b['date'] <= current_time][-lookback_1m:]
            
            # Check exit conditions first
            if self.position is not None:
                should_exit, exit_price, exit_reason = self.check_exit_conditions(recent_10s, i, current_time)
                if should_exit:
                    self.exit_position(exit_price, current_time, exit_reason)
            
            # Check entry conditions (only if not in position)
            if self.position is None:
                should_enter, entry_price, stop_price, profit_price, shares = \
                    self.check_entry_conditions(recent_10s, recent_1m, i)
                
                if should_enter:
                    self.enter_position(entry_price, stop_price, profit_price, shares, current_time, i)
            
            # Track equity
            if i % 360 == 0:  # Every hour
                equity = self.capital
                if self.position is not None:
                    equity += self.position['shares'] * current_bar['close']
                self.equity_curve.append({'time': current_time, 'equity': equity})
        
        # Close any remaining position at end
        if self.position is not None:
            final_bar = bars_10s_list[-1]
            self.exit_position(final_bar['close'], final_bar['date'], "END OF BACKTEST")
        
        # Print results
        self.print_results(symbol)
    
    def print_results(self, symbol):
        """Print backtest performance metrics"""
        print(f"\n{'='*70}")
        print(f"BACKTEST RESULTS: {symbol}")
        print(f"{'='*70}\n")
        
        if len(self.trades) == 0:
            print("No trades executed.")
            return
        
        # Basic stats
        total_trades = len(self.trades)
        winning_trades = [t for t in self.trades if t['pnl'] > 0]
        losing_trades = [t for t in self.trades if t['pnl'] <= 0]
        
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
        
        total_pnl = sum([t['pnl'] for t in self.trades])
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        avg_win = sum([t['pnl'] for t in winning_trades]) / len(winning_trades) if winning_trades else 0
        avg_loss = sum([t['pnl'] for t in losing_trades]) / len(losing_trades) if losing_trades else 0
        
        profit_factor = abs(sum([t['pnl'] for t in winning_trades]) / sum([t['pnl'] for t in losing_trades])) if losing_trades and sum([t['pnl'] for t in losing_trades]) != 0 else float('inf')
        
        total_return_pct = ((self.capital - self.initial_capital) / self.initial_capital) * 100
        
        # Max drawdown
        peak = self.initial_capital
        max_dd = 0
        for trade in self.trades:
            running_capital = self.initial_capital + sum([t['pnl'] for t in self.trades[:self.trades.index(trade)+1]])
            if running_capital > peak:
                peak = running_capital
            dd = ((peak - running_capital) / peak) * 100
            if dd > max_dd:
                max_dd = dd
        
        print(f"Total Trades: {total_trades}")
        print(f"Winning Trades: {len(winning_trades)} ({len(winning_trades)/total_trades*100:.1f}%)" if total_trades > 0 else "Winning Trades: 0 (0.0%)")
        print(f"Losing Trades: {len(losing_trades)}")
        print(f"\nP&L:")
        print(f"  Total: ${total_pnl:+.2f} ({total_return_pct:+.2f}%)")
        print(f"  Average per trade: ${avg_pnl:+.2f}")
        print(f"  Average winner: ${avg_win:+.2f}")
        print(f"  Average loser: ${avg_loss:+.2f}")
        print(f"\nRisk Metrics:")
        print(f"  Profit Factor: {profit_factor:.2f}")
        print(f"  Max Drawdown: {max_dd:.2f}%")
        print(f"\nFinal Capital: ${self.capital:.2f}")
        print(f"{'='*70}\n")
        
        # Print individual trades
        print("Trade Details:")
        print(f"{'#':<4} {'Entry Time':<20} {'Exit Time':<20} {'Entry $':<10} {'Exit $':<10} {'P&L $':<10} {'P&L %':<10} {'Reason':<20}")
        print(f"{'-'*140}")
        for i, trade in enumerate(self.trades, 1):
            print(f"{i:<4} {str(trade['entry_time']):<20} {str(trade['exit_time']):<20} "
                  f"${trade['entry_price']:<9.2f} ${trade['exit_price']:<9.2f} "
                  f"${trade['pnl']:<9.2f} {trade['pnl_pct']:<9.2f}% {trade['exit_reason']:<20}")
        print(f"{'='*70}\n")


def main():
    """Main backtesting function"""
    print("="*70)
    print("ROSS CAMERON MOMENTUM STRATEGY - BACKTESTER")
    print("="*70)
    print(get_strategy_summary())
    
    # User inputs
    symbol = input("\nEnter stock symbol (e.g., LOBO, AAPL): ").strip().upper()
    if not symbol:
        print("ERROR: Symbol cannot be empty.")
        return
    
    start_date = input("Enter start date (YYYY-MM-DD): ").strip()
    end_date = input("Enter end date (YYYY-MM-DD): ").strip()
    
    # Validate date format
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        # Check if dates are reasonable
        if start_dt.year > 2025:
            confirm = input(f"WARNING: Start year is {start_dt.year}. Did you mean {start_dt.year - 1}? Continue anyway? (y/n): ")
            if confirm.lower() != 'y':
                return
        
        if end_dt < start_dt:
            print("ERROR: End date must be after start date.")
            return
            
    except ValueError:
        print("ERROR: Invalid date format. Use YYYY-MM-DD (e.g., 2024-11-25)")
        return
    
    print("\n" + "="*70)
    print("FETCHING DATA FROM IBKR...")
    print("Note: Ensure TWS/Gateway is running on port 7497")
    print("="*70)
    
    # Fetch data from IBKR
    df_10s = fetch_historical_data_ibkr(symbol, start_date, end_date, bar_size="10 secs")
    df_1m = fetch_historical_data_ibkr(symbol, start_date, end_date, bar_size="1 min")
    
    if df_10s.empty or df_1m.empty:
        print("\nERROR: Could not fetch data. Exiting.")
        return
    
    # Run backtest
    engine = BacktestEngine(initial_capital=500.0)
    engine.run_backtest(df_10s, df_1m, symbol, start_date, end_date)
    
    # Save results
    save = input("\nSave results to CSV? (y/n): ").strip().lower()
    if save == 'y':
        results_df = pd.DataFrame(engine.trades)
        filename = f"backtest_{symbol}_{start_date}_{end_date}.csv"
        results_df.to_csv(filename, index=False)
        print(f"✓ Results saved to {filename}")


if __name__ == "__main__":
    main()
