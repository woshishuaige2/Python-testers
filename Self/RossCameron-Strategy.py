"""
Ross Cameron Style Trading Strategy - SHARED LOGIC
===================================================

This module contains the core strategy logic used by BOTH:
- RossCameron-Algo.py (live trading)
- RossCameron-Backtest.py (backtesting)

ANY changes to entry/exit conditions made here will automatically
apply to both live trading and backtesting.

Strategy Rules:
===============

ENTRY CONDITIONS (ALL must be met):
- Pullback Pattern: Surge → pullback → first candle making new high after dip (1-MIN BARS)
- MACD Positive: MACD line above signal line with positive histogram (12/26/9)
- Volume Confirmation: No volume topping, less than 4/5 red candles during pullback
- VWAP Filter: Price must be above session VWAP (from 9:30 AM)
- Minimum Pullback: 3% retracement required to filter noise

EXIT CONDITIONS:
- Dynamic Exit: Candle Under Candle reversal (latest bar's low < previous bar's low)
- Stop Loss: Structural stop at pullback low OR recent high (if >10% breakout), 1% buffer, minimum 2% distance
- Profit Target: +20% from entry price
- End of Day: Close all positions at 3:50 PM EST

CONFIGURATION:
==============
Modify these parameters to change strategy behavior:
"""

import numpy as np

# ==================== STRATEGY CONFIGURATION ====================

class StrategyConfig:
    """Centralized strategy parameters - modify here to change strategy"""
    
    # MACD Parameters
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    
    # Pattern Detection
    PATTERN_LOOKBACK_BARS = 30   # 30 minutes of 1-min bars for pattern detection
    MIN_BARS_FOR_PATTERN = 10    # Minimum bars needed for pattern analysis
    
    # Momentum Detection with Flexibility
    MIN_SURGE_PCT = 2.0              # Minimum 2% surge to qualify as momentum
    SURGE_LOOKBACK_MIN = 5           # Check surge starting from 5 bars back
    SURGE_LOOKBACK_MAX = 20          # Up to 20 bars back (catches extended moves)
    MIN_PULLBACK_PCT = 0.3           # Minimum 0.3% pullback from recent high
    MAX_PULLBACK_PCT = 5.0           # Maximum 5% pullback allowed
    RECENT_HIGH_LOOKBACK = 15        # Check for high in last 15 bars
    
    # Volume Analysis
    MIN_RELATIVE_VOLUME = 1.5         # Minimum 1.5x average volume for entry (Ross Cameron style)
    VOLUME_SPIKE_THRESHOLD = 2.0     # Volume must be < 2x average to avoid topping
    VOLUME_WICK_RATIO = 1.5           # Upper wick vs body ratio for topping detection
    MAX_RED_CANDLES_IN_PULLBACK = 4   # Maximum red candles allowed in last 5 bars
    VOLUME_LOOKBACK_BARS = 10         # Bars to analyze for volume
    
    # Position Sizing
    SIMULATED_ACCOUNT_SIZE = 500.0    # Simulate $500 account
    TRADE_SIZE_DOLLARS = 100.0        # $100 per trade (allows 5 trades per day)
    
    # Commission Fees (IBKR Fixed Pricing)
    COMMISSION_PER_SHARE = 0.005      # $0.005 per share
    COMMISSION_MINIMUM = 1.00         # $1 minimum per order
    SEC_FEE_PER_DOLLAR = 0.0000278    # SEC fee on sells (~$0.0278 per $1000)
    
    # Profit/Loss Targets
    PROFIT_TARGET_PCT = 0.2          # 20% profit target
    ENTRY_SPREAD_PCT = 0.002          # 0.2% spread simulation for entry
    
    # Trading Hours (EST)
    PREMARKET_START_HOUR = 5          # 5:00 AM
    PREMARKET_START_MINUTE = 0
    MARKET_OPEN_HOUR = 9              # 9:30 AM
    MARKET_OPEN_MINUTE = 30
    MARKET_CLOSE_HOUR = 15            # 3:50 PM
    MARKET_CLOSE_MINUTE = 50
    
    # Exit Timing (same as market close)
    END_OF_DAY_HOUR = 15              # 3 PM
    END_OF_DAY_MINUTE = 50            # 3:50 PM
    
    # VWAP Lookback
    VWAP_LOOKBACK_BARS = 390          # Session VWAP (from 9:30 AM market open)
    
    # Live Trading Data Fetching
    DATA_DURATION_10SEC = "3600 S"    # 1 hour of 10-second bars for exit monitoring
    DATA_DURATION_1MIN = "1 D"        # 1 day of 1-minute bars for pattern/VWAP
    BAR_SIZE_10SEC = "10 secs"
    BAR_SIZE_1MIN = "1 min"
    
    # Live Trading Account (uses real IBKR account balance, these are defaults)
    DEFAULT_ACCOUNT_BALANCE = 10000.0  # Default if API doesn't return balance
    MAX_CONCURRENT_POSITIONS = 3       # Maximum number of symbols to trade simultaneously


# ==================== INDICATOR CALCULATIONS ====================

def calculate_macd(closes, fast=None, slow=None, signal=None):
    """
    Calculate MACD indicator
    
    Parameters:
    - closes: List of closing prices
    - fast: Fast EMA period (default from config)
    - slow: Slow EMA period (default from config)
    - signal: Signal line period (default from config)
    
    Returns: (macd_line, signal_line, histogram) or (None, None, None)
    """
    if fast is None:
        fast = StrategyConfig.MACD_FAST
    if slow is None:
        slow = StrategyConfig.MACD_SLOW
    if signal is None:
        signal = StrategyConfig.MACD_SIGNAL
    
    if len(closes) < slow:
        return None, None, None
    
    closes_arr = np.array(closes)
    
    # Calculate EMAs
    ema_fast = np.zeros(len(closes))
    ema_slow = np.zeros(len(closes))
    
    ema_fast[0] = closes_arr[0]
    ema_slow[0] = closes_arr[0]
    
    alpha_fast = 2 / (fast + 1)
    alpha_slow = 2 / (slow + 1)
    
    for i in range(1, len(closes)):
        ema_fast[i] = closes_arr[i] * alpha_fast + ema_fast[i-1] * (1 - alpha_fast)
        ema_slow[i] = closes_arr[i] * alpha_slow + ema_slow[i-1] * (1 - alpha_slow)
    
    macd_line = ema_fast - ema_slow
    
    # Calculate signal line
    signal_line = np.zeros(len(macd_line))
    signal_line[0] = macd_line[0]
    alpha_signal = 2 / (signal + 1)
    
    for i in range(1, len(macd_line)):
        signal_line[i] = macd_line[i] * alpha_signal + signal_line[i-1] * (1 - alpha_signal)
    
    histogram = macd_line - signal_line
    
    return macd_line[-1], signal_line[-1], histogram[-1]


def calculate_vwap(bars):
    """
    Calculate VWAP (Volume Weighted Average Price)
    VWAP = Sum(Price * Volume) / Sum(Volume)
    Using typical price: (High + Low + Close) / 3
    
    Parameters:
    - bars: List of bar dictionaries with 'high', 'low', 'close', 'volume'
    
    Returns: VWAP value or None
    """
    if len(bars) < 2:
        return None
    
    total_pv = 0.0
    total_volume = 0.0
    
    for bar in bars:
        typical_price = (bar['high'] + bar['low'] + bar['close']) / 3
        pv = typical_price * bar['volume']
        total_pv += pv
        total_volume += bar['volume']
    
    if total_volume == 0:
        return None
    
    return total_pv / total_volume


# ==================== ENTRY CONDITIONS ====================

def check_macd_positive(bars):
    """
    Check if MACD is positive (above signal line and not crossing down)
    
    Parameters:
    - bars: List of bar dictionaries with 'close' key
    
    Returns: (bool, str) - (condition_met, message)
    """
    if len(bars) < StrategyConfig.MIN_BARS_FOR_PATTERN:
        return False, "Not enough data"
    
    closes = [bar['close'] for bar in bars]
    macd, signal, histogram = calculate_macd(closes)
    
    if macd is None:
        return False, "MACD calculation failed"
    
    # MACD must be above signal line
    if macd <= signal:
        return False, f"MACD negative: {macd:.4f} <= {signal:.4f}"
    
    # Check not crossing down (current histogram > 0)
    if histogram <= 0:
        return False, f"MACD crossing down: histogram={histogram:.4f}"
    
    return True, f"MACD positive: {macd:.4f} > {signal:.4f}, histogram={histogram:.4f}"


def detect_pullback_and_new_high(bars):
    """
    Detect pullback pattern with surge confirmation:
    1. Verify momentum: 2%+ surge in last 5-20 bars
    2. Find recent high and pullback (0.3-5%)
    3. Breakout bar making higher high + green close
    
    This catches:
    - Sharp surges (2%+ in 5-10 bars)
    - Extended moves (2%+ in 10-20 bars)
    While avoiding sideways/consolidation patterns
    
    Parameters:
    - bars: List of bar dictionaries with 'high', 'low', 'close', 'open', 'volume'
    
    Returns: (bool, str, float, float) - (pattern_found, message, pullback_low_price, recent_high_price)
    """
    if len(bars) < StrategyConfig.MIN_BARS_FOR_PATTERN:
        return False, "Not enough bars", None, None
    
    # Look at recent bars
    recent = bars[-StrategyConfig.PATTERN_LOOKBACK_BARS:] if len(bars) >= StrategyConfig.PATTERN_LOOKBACK_BARS else bars
    
    if len(recent) < 8:
        return False, "Insufficient data", None, None
    
    # STEP 1: Verify surge exists (2%+ move in last 5-20 bars)
    surge_confirmed = False
    surge_low = None
    surge_high = None
    surge_high_idx = None
    
    # Try different lookback periods (5 to 20 bars)
    for lookback in range(StrategyConfig.SURGE_LOOKBACK_MIN, min(StrategyConfig.SURGE_LOOKBACK_MAX + 1, len(recent) - 2)):
        check_start_idx = len(recent) - lookback - 2  # Don't include last 2 bars
        if check_start_idx < 0:
            continue
        
        # Find low and high in this period
        segment = recent[check_start_idx:-2]
        if len(segment) < 3:
            continue
        
        segment_low = min([bar['low'] for bar in segment])
        segment_high = max([bar['high'] for bar in segment])
        segment_high_idx_in_segment = [bar['high'] for bar in segment].index(segment_high)
        segment_high_idx = check_start_idx + segment_high_idx_in_segment
        
        # Calculate surge percentage
        surge_pct = ((segment_high - segment_low) / segment_low) * 100
        
        if surge_pct >= StrategyConfig.MIN_SURGE_PCT:
            surge_confirmed = True
            surge_low = segment_low
            surge_high = segment_high
            surge_high_idx = segment_high_idx
            break
    
    if not surge_confirmed:
        return False, f"No surge: need {StrategyConfig.MIN_SURGE_PCT}%+ move in last {StrategyConfig.SURGE_LOOKBACK_MAX} bars", None, None
    
    # STEP 2: Find recent high (in last 15 bars, excluding last 2)
    lookback_bars = min(StrategyConfig.RECENT_HIGH_LOOKBACK, len(recent) - 2)
    recent_segment = recent[-lookback_bars-2:-2]
    
    if len(recent_segment) < 3:
        return False, "Not enough bars for pattern", None, None
    
    highs = [bar['high'] for bar in recent_segment]
    recent_high = max(highs)
    recent_high_idx_in_segment = highs.index(recent_high)
    recent_high_idx = len(recent) - lookback_bars - 2 + recent_high_idx_in_segment
    
    # STEP 3: Find pullback low (after recent high, before last bar)
    if recent_high_idx >= len(recent) - 2:
        return False, "High too recent, no pullback yet", None, None
    
    bars_after_high = recent[recent_high_idx + 1:-1]
    if len(bars_after_high) == 0:
        return False, "No bars for pullback", None, None
    
    pullback_low = min([bar['low'] for bar in bars_after_high])
    
    # Calculate pullback percentage
    pullback_pct = ((recent_high - pullback_low) / recent_high) * 100
    
    # Validate pullback is within range (0.3% - 5%)
    if pullback_pct < StrategyConfig.MIN_PULLBACK_PCT:
        return False, f"No pullback: {pullback_pct:.2f}% < {StrategyConfig.MIN_PULLBACK_PCT}%", None, None
    
    if pullback_pct > StrategyConfig.MAX_PULLBACK_PCT:
        return False, f"Pullback too deep: {pullback_pct:.2f}% > {StrategyConfig.MAX_PULLBACK_PCT}%", None, None
    
    # STEP 4: Check breakout on last bar
    last_bar = recent[-1]
    second_last_bar = recent[-2]
    
    # Must make higher high and close green
    if last_bar['high'] <= second_last_bar['high']:
        return False, "No breakout - not making higher high", None, None
    
    if last_bar['close'] <= last_bar['open']:
        return False, "Breakout bar must close green", None, None
    
    # Optional: Verify we're still near the momentum (within 10% of recent high)
    distance_from_high = ((recent_high - last_bar['close']) / recent_high) * 100
    if distance_from_high > 10.0:
        return False, f"Too far from high: {distance_from_high:.1f}% below", None, None
    
    # Pattern confirmed!
    surge_pct_final = ((surge_high - surge_low) / surge_low) * 100
    message = f"Momentum: {surge_pct_final:.1f}% surge (${surge_low:.2f}→${surge_high:.2f}), pullback {pullback_pct:.1f}% to ${pullback_low:.2f}, breakout ${last_bar['high']:.2f}"
    
    return True, message, pullback_low, recent_high


def check_volume_conditions(bars):
    """
    Check volume conditions (Ross Cameron style):
    1. HIGH relative volume required (1.5x+ average) - indicates institutional interest
       - Uses average of last 2 bars vs average of previous 10 bars
    2. No volume top (high volume with topping tail/wick)
    3. No excessive selling pressure during pullback
    
    Parameters:
    - bars: List of bar dictionaries with 'high', 'low', 'close', 'open', 'volume'
    
    Returns: (bool, str) - (condition_met, message)
    """
    if len(bars) < 5:
        return False, "Not enough bars for volume analysis"
    
    recent = bars[-StrategyConfig.VOLUME_LOOKBACK_BARS:] if len(bars) >= StrategyConfig.VOLUME_LOOKBACK_BARS else bars
    
    if len(recent) < 3:  # Need at least 3 bars (1 for history + 2 for current check)
        return False, "Not enough bars for volume analysis"
    
    last_bar = recent[-1]
    second_last_bar = recent[-2]
    
    # Calculate average volume of previous 10 bars (excluding last 2)
    history_bars = recent[:-2] if len(recent) > 2 else recent[:-1]
    if len(history_bars) == 0:
        return False, "Not enough historical bars for volume analysis"
    
    avg_volume = sum([bar['volume'] for bar in history_bars]) / len(history_bars)
    
    # Calculate average of last 2 bars
    avg_last_2_bars = (last_bar['volume'] + second_last_bar['volume']) / 2
    relative_volume = avg_last_2_bars / avg_volume if avg_volume > 0 else 0
    
    # REQUIREMENT 1: High relative volume (Ross Cameron style)
    if relative_volume < StrategyConfig.MIN_RELATIVE_VOLUME:
        return False, f"Low relative volume: {relative_volume:.2f}x avg of last 2 bars (need {StrategyConfig.MIN_RELATIVE_VOLUME}x+) - no momentum"
    
    # Check for volume top: high volume + long upper wick (topping tail)
    upper_wick = last_bar['high'] - max(last_bar['open'], last_bar['close'])
    body_size = abs(last_bar['close'] - last_bar['open'])
    
    if last_bar['volume'] > avg_volume * StrategyConfig.VOLUME_SPIKE_THRESHOLD and upper_wick > body_size * StrategyConfig.VOLUME_WICK_RATIO:
        return False, f"Volume top detected: high volume ({last_bar['volume']:.0f} vs avg {avg_volume:.0f}) with topping tail"
    
    # Check selling pressure during pullback: red candles shouldn't dominate
    red_candles = sum([1 for bar in recent[-5:] if bar['close'] < bar['open']])  
    if red_candles >= StrategyConfig.MAX_RED_CANDLES_IN_PULLBACK:
        return False, f"Excessive selling pressure: {red_candles}/5 red candles"
    
    return True, f"Strong volume: {relative_volume:.2f}x avg (last 2 bars: {avg_last_2_bars:.0f} vs hist avg: {avg_volume:.0f}), no topping pattern"


def check_above_vwap(bars, current_price):
    """
    Check if current price is above VWAP
    Only take long trades when above VWAP
    
    Parameters:
    - bars: List of bar dictionaries for VWAP calculation
    - current_price: Current price to compare
    
    Returns: (bool, str) - (condition_met, message)
    """
    vwap = calculate_vwap(bars)
    
    if vwap is None:
        return False, "VWAP calculation failed"
    
    if current_price <= vwap:
        return False, f"Price below VWAP: ${current_price:.4f} <= ${vwap:.4f} (no long entry)"
    
    pct_above = ((current_price - vwap) / vwap) * 100
    return True, f"Price above VWAP: ${current_price:.4f} > ${vwap:.4f} (+{pct_above:.2f}%)"


def check_all_entry_conditions(bars_1m, current_price):
    """
    Check ALL entry conditions at once
    
    Parameters:
    - bars_1m: List of 1-minute bars for pattern/MACD/volume/VWAP
    - current_price: Current price
    
    Returns: (bool, dict, float, float) - (all_conditions_met, condition_results, pullback_low, recent_high)
    """
    # Check each condition (all using 1-min bars now)
    pattern_ok, pattern_msg, pullback_low, recent_high = detect_pullback_and_new_high(bars_1m)
    macd_ok, macd_msg = check_macd_positive(bars_1m)
    volume_ok, volume_msg = check_volume_conditions(bars_1m)
    vwap_ok, vwap_msg = check_above_vwap(bars_1m, current_price)
    
    # Compile results
    results = {
        'pattern': {'ok': pattern_ok, 'msg': pattern_msg},
        'macd': {'ok': macd_ok, 'msg': macd_msg},
        'volume': {'ok': volume_ok, 'msg': volume_msg},
        'vwap': {'ok': vwap_ok, 'msg': vwap_msg}
    }
    
    all_ok = pattern_ok and macd_ok and volume_ok and vwap_ok
    
    return all_ok, results, pullback_low, recent_high


# ==================== EXIT CONDITIONS ====================

def check_dynamic_exit(bars):
    """
    Check for Candle Under Candle exit signal
    Exit if the latest completed bar's low is below the previous bar's low
    
    Parameters:
    - bars: List of bars since entry
    
    Returns: (bool, str) - (should_exit, message)
    """
    if len(bars) < 2:
        return False, "Insufficient bar data for exit check"
    
    # Get the last two completed bars
    latest_bar = bars[-1]
    previous_bar = bars[-2]
    
    # Check if latest bar's low is below previous bar's low (reversal signal)
    if latest_bar['low'] < previous_bar['low']:
        message = f"Candle Under Candle detected: Latest low ${latest_bar['low']:.2f} < Previous low ${previous_bar['low']:.2f}"
        return True, message
    
    return False, f"No exit signal: Latest low ${latest_bar['low']:.2f} >= Previous low ${previous_bar['low']:.2f}"


def check_stop_loss_hit(current_bar, stop_price):
    """
    Check if stop loss has been hit
    
    Parameters:
    - current_bar: Current bar dictionary with 'low'
    - stop_price: Stop loss price
    
    Returns: bool - True if stop hit
    """
    return current_bar['low'] <= stop_price


def check_profit_target_hit(current_bar, profit_price):
    """
    Check if profit target has been hit
    
    Parameters:
    - current_bar: Current bar dictionary with 'high'
    - profit_price: Profit target price
    
    Returns: bool - True if profit hit
    """
    return current_bar['high'] >= profit_price


def check_end_of_day(current_time):
    """
    Check if we're near end of day (should close positions)
    
    Parameters:
    - current_time: datetime object with hour and minute
    
    Returns: bool - True if near close
    """
    if current_time.hour == StrategyConfig.END_OF_DAY_HOUR and current_time.minute >= StrategyConfig.END_OF_DAY_MINUTE:
        return True
    elif current_time.hour > StrategyConfig.END_OF_DAY_HOUR:
        return True
    return False


# ==================== POSITION SIZING ====================

def calculate_position_size(account_balance, entry_price, stop_price):
    """
    Calculate position size for $500 account simulation:
    - Use $100 per trade (allows 5 trades per day)
    - Ignore actual account balance to simulate small account
    
    Parameters:
    - account_balance: Actual account balance (ignored for simulation)
    - entry_price: Entry price per share
    - stop_price: Stop loss price
    
    Returns: int - Number of shares to trade
    """
    # Simulate $500 account with $100 per trade
    simulated_trade_size = StrategyConfig.TRADE_SIZE_DOLLARS
    
    # Calculate shares based on entry price
    shares = int(simulated_trade_size / entry_price)
    
    return max(shares, 1)  # minimum 1 share


def calculate_commission(shares, trade_value, is_sell=False):
    """
    Calculate IBKR Fixed pricing commission
    
    Parameters:
    - shares: Number of shares
    - trade_value: Total trade value (price * shares)
    - is_sell: True if selling (includes SEC fee), False if buying
    
    Returns: float - Total commission and fees
    """
    # Base commission: $0.005 per share, $1 minimum
    commission = max(shares * StrategyConfig.COMMISSION_PER_SHARE, StrategyConfig.COMMISSION_MINIMUM)
    
    # Add SEC fee on sells
    if is_sell:
        sec_fee = trade_value * StrategyConfig.SEC_FEE_PER_DOLLAR
        commission += sec_fee
    
    return commission


def calculate_entry_exit_prices(current_price, pullback_low, recent_high):
    """
    Calculate entry price, stop price, and profit target
    
    For strong breakouts (entry >10% above recent high), use recent high as stop
    to avoid excessive risk. Otherwise use pullback low.
    
    Parameters:
    - current_price: Current market price
    - pullback_low: Pullback low price (for stop loss)
    - recent_high: Recent high price (used for strong breakout stops)
    
    Returns: (entry_price, stop_price, profit_price) or (None, None, None)
    """
    # Entry price: add spread
    entry_price = round(current_price * (1 + StrategyConfig.ENTRY_SPREAD_PCT), 2)
    
    # Check if this is a strong breakout (>10% above recent high)
    breakout_pct = ((entry_price - recent_high) / recent_high) * 100 if recent_high else 0
    
    if breakout_pct > 10.0:
        # Strong breakout: use recent high as stop (tighter risk management)
        stop_price = round(recent_high * 0.99, 2)
    else:
        # Normal entry: use pullback low as stop
        stop_price = round(pullback_low * 0.99, 2)
    
    # Profit target: % gain from entry
    profit_price = round(entry_price * (1 + StrategyConfig.PROFIT_TARGET_PCT), 2)
    
    # Validate stop loss (ensure at least 2% distance)
    stop_distance_pct = (entry_price - stop_price) / entry_price
    if stop_price >= entry_price or stop_distance_pct < 0.02:
        return None, None, None
    
    return entry_price, stop_price, profit_price


# ==================== UTILITY FUNCTIONS ====================

def get_strategy_summary():
    """Return a summary of current strategy configuration"""
    return f"""
Strategy Configuration:
======================
MACD: {StrategyConfig.MACD_FAST}/{StrategyConfig.MACD_SLOW}/{StrategyConfig.MACD_SIGNAL}
Profit Target: {StrategyConfig.PROFIT_TARGET_PCT*100:.1f}%
Pattern Lookback: {StrategyConfig.PATTERN_LOOKBACK_BARS} bars
Volume Spike Threshold: {StrategyConfig.VOLUME_SPIKE_THRESHOLD}x
Max Red Candles: {StrategyConfig.MAX_RED_CANDLES_IN_PULLBACK}/5
Trade Size: ${StrategyConfig.TRADE_SIZE_DOLLARS} (of ${StrategyConfig.SIMULATED_ACCOUNT_SIZE} account)
End of Day: {StrategyConfig.END_OF_DAY_HOUR}:{StrategyConfig.END_OF_DAY_MINUTE:02d} PM
"""


if __name__ == "__main__":
    print("="*70)
    print("ROSS CAMERON TRADING STRATEGY - SHARED MODULE")
    print("="*70)
    print(get_strategy_summary())
    print("\nThis module is imported by:")
    print("  - RossCameron-Algo.py (live trading)")
    print("  - RossCameron-Backtest.py (backtesting)")
    print("\nModify StrategyConfig class to change strategy parameters.")
