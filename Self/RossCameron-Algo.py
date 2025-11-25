"""
Ross Cameron Style Day Trading Algorithm
==========================================

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

Features:
- Multi-symbol scanning (up to 3 stocks simultaneously)
- 10-second bars for fast pattern/MACD/volume analysis
- 1-minute bars for VWAP calculation
- Real-time monitoring dashboard with clean table visualization
- Paper trading via Interactive Brokers API (port 7497)
- Bracket orders with limit entry at ASK + 0.2% buffer for better fills
"""

from decimal import Decimal
from ibapi.client import *
from ibapi.common import TickAttrib, TickerId
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.wrapper import *
import time
import threading
from datetime import datetime, timezone, timedelta
import numpy as np
import os

# PAPER trading port
port = 7497
clientId = 2  # different from Order-LOBO.py

class TradingAlgo(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.oid = 0
        self.account_balance = None
        self.bars = {}  # dictionary: symbol -> list of historical 10-sec bars
        self.bars_1min = {}  # dictionary: symbol -> list of 1-min bars for VWAP
        self.last_price = {}
        self.ask_price = {}
        self.position = {}  # current position size per symbol
        self.entry_order_id = {}  # entry order ID per symbol
        self.profit_order_id = {}  # profit taker order ID per symbol
        self.stop_order_id = {}  # stop loss order ID per symbol
        self.in_position = {}  # bool per symbol
        self.pending_entry = {}  # bool per symbol
        self.current_symbol = None  # track which symbol is being processed
        self.current_reqid = None  # track which request ID is being processed
        
    def nextValidId(self, orderId: OrderId):
        self.oid = orderId

    def nextOid(self):
        self.oid += 1
        return self.oid

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        if tag == "TotalCashValue":
            self.account_balance = float(value)
            print(f"Account balance: ${self.account_balance:.2f}")

    def accountSummaryEnd(self, reqId: int):
        pass

    def historicalData(self, reqId: int, bar):
        """Receive historical bars - 10-sec for patterns/MACD/volume, 1-min for VWAP"""
        if self.current_symbol:
            # reqId 4001 = 10-second bars, reqId 4002 = 1-minute bars
            if reqId == 4001:
                if self.current_symbol not in self.bars:
                    self.bars[self.current_symbol] = []
                self.bars[self.current_symbol].append({
                    'date': bar.date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                })
            elif reqId == 4002:
                if self.current_symbol not in self.bars_1min:
                    self.bars_1min[self.current_symbol] = []
                self.bars_1min[self.current_symbol].append({
                    'date': bar.date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        if self.current_symbol:
            if reqId == 4001 and self.current_symbol in self.bars:
                print(f"10-sec data received for {self.current_symbol}: {len(self.bars[self.current_symbol])} bars")
            elif reqId == 4002 and self.current_symbol in self.bars_1min:
                print(f"1-min data received for {self.current_symbol}: {len(self.bars_1min[self.current_symbol])} bars")

    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
        if self.current_symbol:
            if tickType == 4:  # LAST price
                self.last_price[self.current_symbol] = price
            elif tickType == 2:  # ASK price
                self.ask_price[self.current_symbol] = price

    def openOrder(self, orderId: OrderId, contract: Contract, order: Order, orderState: OrderState):
        print(f"openOrder. orderId: {orderId}, symbol: {contract.symbol}, action: {order.action}, qty: {order.totalQuantity}, status: {orderState.status}")

    def orderStatus(self, orderId: TickerId, status: str, filled: Decimal, remaining: Decimal, avgFillPrice: float, permId: TickerId, parentId: TickerId, lastFillPrice: float, clientId: TickerId, whyHeld: str, mktCapPrice: float):
        print(f"orderStatus. orderId: {orderId}, status: {status}, filled: {filled}, remaining: {remaining}, avgFillPrice: {avgFillPrice}")
        
        # Track when entry order is filled
        for symbol, entry_id in self.entry_order_id.items():
            if orderId == entry_id:
                if status == "Filled":
                    self.in_position[symbol] = True
                    self.pending_entry[symbol] = False
                    self.position[symbol] = int(filled)
                    print(f"✓✓✓ ENTRY FILLED ({symbol}): {self.position[symbol]} shares @ ${avgFillPrice}")
                elif status == "Cancelled":
                    self.pending_entry[symbol] = False
                    print(f"Entry order cancelled ({symbol})")
                break

    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        print(f"Execution: {contract.symbol}, {execution.side}, {execution.shares} @ {execution.price}")
        
        # Track exit fills (profit or stop)
        symbol = contract.symbol
        if execution.side == "SLD" and symbol in self.in_position and self.in_position[symbol]:
            self.position[symbol] = self.position.get(symbol, 0) - int(execution.shares)
            if self.position[symbol] <= 0:
                self.in_position[symbol] = False
                self.position[symbol] = 0
                print(f"✓✓✓ POSITION CLOSED ({symbol}) @ ${execution.price}")

    def error(self, *args):
        try:
            if len(args) == 3:
                reqId, errorCode, errorString = args
                print(f"Error. ReqId: {reqId}, Code: {errorCode}, Msg: {errorString}")
            elif len(args) >= 4:
                reqId, errorTime, errorCode, errorString = args[:4]
                print(f"Error. Code: {errorCode}, Msg: {errorString}")
        except Exception as e:
            print("Error handler exception:", e, args)


def is_trading_hours():
    """Check if current time is between 06:30 AM - 03:30 PM EST"""
    est = timezone(timedelta(hours=-5))  # EST is UTC-5
    now_est = datetime.now(est)
    hour = now_est.hour
    minute = now_est.minute
    
    # 6:30 AM to 3:30 PM EST
    if hour == 6 and minute >= 30:
        return True
    elif hour >= 7 and hour <= 14:
        return True
    elif hour == 15 and minute <= 30:
        return True
    return False


def is_near_close():
    """Check if we're within 5 minutes of market close (3:30 PM EST) - DISABLED FOR TESTING"""
    return False  # Disabled for testing


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """Calculate MACD indicator"""
    if len(closes) < slow:
        return None, None, None
    
    closes_arr = np.array(closes)
    
    # Calculate EMAs
    ema_fast = np.zeros(len(closes))
    ema_slow = np.zeros(len(closes))
    
    # Simple start values
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


def check_macd_positive(bars):
    """Check if MACD is positive (above signal line and not crossing down)"""
    if len(bars) < 30:
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
    Detect:
    1. Initial surge (price went up significantly)
    2. Pullback/dip occurred
    3. First candle making new high after the dip
    
    Returns: (success, message, pullback_low_price)
    """
    if len(bars) < 10:
        return False, "Not enough bars", None
    
    # Look at recent bars (last 20)
    recent = bars[-20:] if len(bars) >= 20 else bars
    
    # Find the highest high in the period
    highs = [bar['high'] for bar in recent]
    max_high = max(highs)
    max_high_idx = len(recent) - 1 - highs[::-1].index(max_high)
    
    # Check if we had a pullback (price went down from max_high)
    if max_high_idx >= len(recent) - 2:
        return False, "No pullback detected yet (still at high)", None
    
    # Check bars after the high for pullback
    pullback_detected = False
    pullback_low = max_high
    
    for i in range(max_high_idx + 1, len(recent)):
        if recent[i]['low'] < pullback_low:
            pullback_low = recent[i]['low']
            pullback_detected = True
    
    if not pullback_detected:
        return False, "No pullback after surge", None
    
    # Check if the LAST bar is making a new high (breaking above previous resistance)
    last_bar = recent[-1]
    second_last_bar = recent[-2]
    
    # First candle making new high = current high > previous bar's high
    if last_bar['high'] > second_last_bar['high'] and last_bar['close'] > last_bar['open']:
        pullback_pct = ((max_high - pullback_low) / max_high) * 100
        message = f"Pattern detected: surge to {max_high:.2f}, pullback to {pullback_low:.2f} (-{pullback_pct:.1f}%), new high at {last_bar['high']:.2f}"
        return True, message, pullback_low  # Return the pullback low price as stop loss
    
    return False, "Waiting for first candle making new high", None


def check_volume_conditions(bars):
    """
    Check:
    1. No volume top (high volume with topping tail/wick)
    2. No excessive selling pressure during pullback
    """
    if len(bars) < 5:
        return False, "Not enough bars for volume analysis"
    
    recent = bars[-10:] if len(bars) >= 10 else bars
    last_bar = recent[-1]
    
    # Calculate average volume
    avg_volume = sum([bar['volume'] for bar in recent[:-1]]) / len(recent[:-1])
    
    # Check for volume top: high volume + long upper wick (topping tail)
    upper_wick = last_bar['high'] - max(last_bar['open'], last_bar['close'])
    body_size = abs(last_bar['close'] - last_bar['open'])
    
    if last_bar['volume'] > avg_volume * 2 and upper_wick > body_size * 1.5:
        return False, f"Volume top detected: high volume ({last_bar['volume']:.0f} vs avg {avg_volume:.0f}) with topping tail"
    
    # Check selling pressure during pullback: red candles shouldn't dominate
    red_candles = sum([1 for bar in recent[-5:] if bar['close'] < bar['open']])  
    if red_candles >= 4:
        return False, f"Excessive selling pressure: {red_candles}/5 red candles"
    
    return True, f"Volume OK: current={last_bar['volume']:.0f}, avg={avg_volume:.0f}, no topping pattern"


def calculate_vwap(bars):
    """
    Calculate VWAP (Volume Weighted Average Price)
    VWAP = Sum(Price * Volume) / Sum(Volume)
    Using typical price: (High + Low + Close) / 3
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
    
    vwap = total_pv / total_volume
    return vwap


def check_above_vwap(bars, current_price):
    """
    Check if current price is above VWAP
    Only take long trades when above VWAP
    """
    vwap = calculate_vwap(bars)
    
    if vwap is None:
        return False, "VWAP calculation failed"
    
    if current_price <= vwap:
        return False, f"Price below VWAP: ${current_price:.4f} <= ${vwap:.4f} (no long entry)"
    
    pct_above = ((current_price - vwap) / vwap) * 100
    return True, f"Price above VWAP: ${current_price:.4f} > ${vwap:.4f} (+{pct_above:.2f}%)"
def calculate_position_size(account_balance, entry_price, stop_price, risk_pct=0.10):
    """
    Calculate position size based on risk management:
    - Risk ≤ 10% of account balance
    """
    if account_balance is None or account_balance <= 0:
        return 0
    
    max_risk_dollars = account_balance * risk_pct
    risk_per_share = abs(entry_price - stop_price)
    
    if risk_per_share <= 0:
        return 0
    
    shares = int(max_risk_dollars / risk_per_share)
    
    # Also cap by total allocation (50% of account max)
    max_shares_by_allocation = int((account_balance * 0.5) / entry_price)
    shares = min(shares, max_shares_by_allocation)
    
    return max(shares, 1)  # minimum 1 share


def check_dynamic_exit(app, symbol):
    """
    Check for Candle Under Candle exit signal
    Exit if the latest completed bar's low is below the previous bar's low
    
    Returns: (should_exit, message)
    """
    if symbol not in app.bars or len(app.bars[symbol]) < 2:
        return False, "Insufficient bar data for exit check"
    
    # Get the last two completed bars
    bars = app.bars[symbol]
    latest_bar = bars[-1]
    previous_bar = bars[-2]
    
    # Check if latest bar's low is below previous bar's low (reversal signal)
    if latest_bar['low'] < previous_bar['low']:
        message = f"Candle Under Candle detected: Latest low ${latest_bar['low']:.2f} < Previous low ${previous_bar['low']:.2f}"
        return True, message
    
    return False, f"No exit signal: Latest low ${latest_bar['low']:.2f} >= Previous low ${previous_bar['low']:.2f}"


def check_and_trade(app, contract, symbol):
    """Check conditions and place trade if all criteria met"""
    
    # Initialize symbol tracking if needed
    if symbol not in app.in_position:
        app.in_position[symbol] = False
    if symbol not in app.pending_entry:
        app.pending_entry[symbol] = False
    if symbol not in app.position:
        app.position[symbol] = 0
    
    # Don't check if already in position or pending entry
    if app.in_position[symbol]:
        return {"symbol": symbol, "status": "IN POSITION", "skip": True}
    if app.pending_entry[symbol]:
        return {"symbol": symbol, "status": "PENDING ENTRY", "skip": True}
    
    # Set current symbol for callbacks
    app.current_symbol = symbol
    
    # Reset bars for fresh data
    if symbol in app.bars:
        app.bars[symbol] = []
    if symbol in app.bars_1min:
        app.bars_1min[symbol] = []
    
    # Get historical data - 10 second bars for pattern/MACD/volume
    end_time = ""
    duration = "3600 S"  # 1 hour of data
    bar_size = "10 secs"
    app.reqHistoricalData(4001, contract, end_time, duration, bar_size, "TRADES", 1, 1, False, [])
    time.sleep(3)
    
    # Get historical data - 1 minute bars for VWAP
    duration_1min = "1 D"  # 1 day of data for VWAP
    bar_size_1min = "1 min"
    app.reqHistoricalData(4002, contract, end_time, duration_1min, bar_size_1min, "TRADES", 1, 1, False, [])
    time.sleep(3)
    
    if symbol not in app.bars or len(app.bars[symbol]) < 10:
        bars_count = len(app.bars.get(symbol, []))
        return {"symbol": symbol, "status": "INSUFFICIENT DATA", "bars": bars_count, "skip": True}
    
    if symbol not in app.bars_1min or len(app.bars_1min[symbol]) < 10:
        bars_count = len(app.bars_1min.get(symbol, []))
        return {"symbol": symbol, "status": "INSUFFICIENT 1M DATA", "bars": bars_count, "skip": True}
    
    # Get current ask price first for VWAP check
    if symbol in app.ask_price:
        del app.ask_price[symbol]
    app.reqMktData(1, contract, "", False, False, [])
    time.sleep(2)
    app.cancelMktData(1)
    
    if symbol not in app.ask_price or app.ask_price[symbol] is None:
        return {"symbol": symbol, "status": "NO PRICE DATA", "skip": True}
    
    current_price = app.ask_price[symbol]
    
    # Check all conditions
    pattern_ok, pattern_msg, pullback_low_price = detect_pullback_and_new_high(app.bars[symbol])
    macd_ok, macd_msg = check_macd_positive(app.bars[symbol])
    volume_ok, volume_msg = check_volume_conditions(app.bars[symbol])
    vwap_ok, vwap_msg = check_above_vwap(app.bars_1min[symbol], current_price)
    
    # Return status for display
    result = {
        "symbol": symbol,
        "price": current_price,
        "pattern": "✓" if pattern_ok else "✗",
        "macd": "✓" if macd_ok else "✗",
        "volume": "✓" if volume_ok else "✗",
        "vwap": "✓" if vwap_ok else "✗",
        "all_pass": pattern_ok and macd_ok and volume_ok and vwap_ok,
        "skip": False
    }
    
    if not result["all_pass"]:
        return result
    
    # Verify we have pullback low price for stop loss
    if pullback_low_price is None:
        result["status"] = "NO PULLBACK LOW"
        result["skip"] = True
        return result
    
    # All conditions met - place trade!
    timestamp = datetime.now().strftime('%H:%M:%S')
    
    print(f"\n{'='*70}")
    print(f"[{timestamp}] ✓✓✓ TRADE SIGNAL - {symbol} ✓✓✓")
    print(f"{'='*70}")
    print(f"Pattern: {pattern_msg}")
    print(f"MACD: {macd_msg}")
    print(f"Volume: {volume_msg}")
    print(f"VWAP: {vwap_msg}\n")
    
    # Get current price (use ASK for buying)
    if symbol in app.ask_price:
        del app.ask_price[symbol]
    app.reqMktData(1, contract, "", False, False, [])
    time.sleep(2)
    app.cancelMktData(1)
    
    if symbol not in app.ask_price or app.ask_price[symbol] is None:
        print(f"Could not get current ask price for {symbol}. Skipping trade.")
        result["status"] = "PRICE ERROR"
        result["skip"] = True
        return result
    
    # Use ask price + small buffer for limit order to ensure fill
    entry_price = round(app.ask_price[symbol] * 1.002, 2)
    
    # Use pullback low as stop loss
    stop_price = round(pullback_low_price, 2)
    
    # Profit target: +10% from entry
    profit_pct = 0.10
    profit_price = round(entry_price * (1 + profit_pct), 2)
    
    # Calculate actual risk percentage
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        print(f"Invalid stop loss: stop ${stop_price} >= entry ${entry_price}. Skipping trade.")
        result["status"] = "INVALID STOP"
        result["skip"] = True
        return result
    
    stop_pct_actual = (risk_per_share / entry_price) * 100
    
    qty = calculate_position_size(app.account_balance, entry_price, stop_price, risk_pct=0.10)
    notional = entry_price * qty
    risk_dollars = risk_per_share * qty
    risk_pct_actual = (risk_dollars / app.account_balance) * 100
    
    print(f"Trade Plan:")
    print(f"  Entry: ${entry_price} | Stop: ${stop_price} (pullback low, -{stop_pct_actual:.1f}%) | Target: ${profit_price} (+{profit_pct*100:.0f}%)")
    print(f"  Quantity: {qty} shares | Notional: ${notional:.2f} | Risk: ${risk_dollars:.2f} ({risk_pct_actual:.1f}%)\n")
    
    # Build bracket orders
    parent = Order()
    parent.action = "BUY"
    parent.orderType = "LMT"
    parent.lmtPrice = entry_price
    parent.totalQuantity = qty
    parent.tif = "DAY"
    parent.transmit = False
    try:
        parent.eTradeOnly = False
        parent.firmQuoteOnly = False
    except Exception:
        pass
    
    profit_taker = Order()
    profit_taker.action = "SELL"
    profit_taker.orderType = "LMT"
    profit_taker.lmtPrice = profit_price
    profit_taker.totalQuantity = qty
    profit_taker.tif = "GTC"
    profit_taker.transmit = False
    try:
        profit_taker.eTradeOnly = False
        profit_taker.firmQuoteOnly = False
    except Exception:
        pass
    
    stop_loss = Order()
    stop_loss.action = "SELL"
    stop_loss.orderType = "STP"
    stop_loss.auxPrice = stop_price
    stop_loss.totalQuantity = qty
    stop_loss.tif = "GTC"
    stop_loss.transmit = True
    try:
        stop_loss.eTradeOnly = False
        stop_loss.firmQuoteOnly = False
    except Exception:
        pass
    
    # Assign order IDs
    parent_id = app.nextOid()
    profit_id = app.nextOid()
    stop_id = app.nextOid()
    
    parent.orderId = parent_id
    profit_taker.orderId = profit_id
    profit_taker.parentId = parent_id
    stop_loss.orderId = stop_id
    stop_loss.parentId = parent_id
    
    app.entry_order_id[symbol] = parent_id
    app.profit_order_id[symbol] = profit_id
    app.stop_order_id[symbol] = stop_id
    app.pending_entry[symbol] = True
    
    # Place orders
    print(f"Placing bracket order for {symbol}: parent={parent_id}, profit={profit_id}, stop={stop_id}")
    app.placeOrder(parent.orderId, contract, parent)
    app.placeOrder(profit_taker.orderId, contract, profit_taker)
    app.placeOrder(stop_loss.orderId, contract, stop_loss)
    print(f"✓ Orders placed!\n")
    
    result["status"] = "ORDER PLACED"
    return result


if __name__ == "__main__":
    symbols_input = input("Enter up to 3 stock symbols separated by commas (e.g., LOBO,AAPL,TSLA): ").strip().upper()
    if not symbols_input:
        print("No symbols entered. Exiting.")
        exit(1)
    
    symbols = [s.strip() for s in symbols_input.split(',')][:3]  # Max 3 symbols
    if len(symbols) == 0:
        print("No valid symbols entered. Exiting.")
        exit(1)
    
    print(f"\n{'='*60}")
    print(f"Ross Cameron Style Trading Algorithm")
    print(f"Scanning: {', '.join(symbols)}")
    print(f"Continuous Monitoring Mode")
    print(f"Press Ctrl+C to stop")
    print(f"{'='*60}\n")
    
    # Create contracts for all symbols
    contracts = {}
    for symbol in symbols:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contracts[symbol] = contract
    
    # Connect to TWS
    app = TradingAlgo()
    app.connect("127.0.0.1", port, clientId)
    threading.Thread(target=app.run, daemon=True).start()
    time.sleep(1)
    
    # Wait for connection
    timeout = 5.0
    waited = 0.0
    while app.oid == 0 and waited < timeout:
        time.sleep(0.1)
        waited += 0.1
    
    if app.oid == 0:
        print("Failed to connect to TWS/Gateway. Exiting.")
        exit(1)
    
    print("Connected to TWS/Gateway\n")
    
    # Get initial account balance
    print("Fetching account balance...")
    app.reqAccountSummary(9001, "All", "TotalCashValue")
    time.sleep(2)
    app.cancelAccountSummary(9001)
    
    if app.account_balance is None:
        print("Warning: Could not retrieve account balance. Using default risk management.")
        app.account_balance = 10000.0
    else:
        print(f"Account balance: ${app.account_balance:.2f}\n")
    
    print("Starting continuous monitoring...\n")
    
    # Continuous monitoring loop
    try:
        scan_count = 0
        while True:
            scan_count += 1
            
            # Check if near market close - close all positions
            if is_near_close():
                est = timezone(timedelta(hours=-5))
                now_est = datetime.now(est)
                has_positions = False
                for symbol in symbols:
                    if symbol in app.in_position and app.in_position[symbol]:
                        has_positions = True
                        print(f"\n[{now_est.strftime('%H:%M:%S')}] Near market close - closing {symbol} position...")
                        
                        # Place market order to close position
                        close_order = Order()
                        close_order.action = "SELL"
                        close_order.orderType = "MKT"
                        close_order.totalQuantity = app.position[symbol]
                        close_order.tif = "DAY"
                        
                        close_id = app.nextOid()
                        close_order.orderId = close_id
                        app.placeOrder(close_order.orderId, contracts[symbol], close_order)
                        print(f"Market close order placed for {symbol}: {app.position[symbol]} shares @ MKT\n")
                        
                        app.in_position[symbol] = False
                        app.position[symbol] = 0
                        time.sleep(2)
                
                if not has_positions:
                    time.sleep(60)
                continue
            
            # Check time window and show current time
            est = timezone(timedelta(hours=-5))
            now_est = datetime.now(est)
            current_time_str = now_est.strftime('%H:%M:%S')
            
            # Trading hours check DISABLED FOR TESTING
            
            # Clear screen and print header
            os.system('cls' if os.name == 'nt' else 'clear')
            print(f"{'='*70}")
            print(f"  ROSS CAMERON MOMENTUM SCANNER  |  Scan #{scan_count}  |  {current_time_str} EST")
            print(f"{'='*70}")
            print(f"  Account: ${app.account_balance:.2f}  |  Symbols: {', '.join(symbols)}")
            print(f"{'='*70}\n")
            
            # Check conditions and trade for each symbol
            results = []
            for symbol in symbols:
                result = check_and_trade(app, contracts[symbol], symbol)
                if result:
                    results.append(result)
            
            # Monitor active positions for dynamic exit (Candle Under Candle)
            for symbol in symbols:
                if symbol in app.in_position and app.in_position[symbol]:
                    # Request fresh 10-second bar data for exit monitoring
                    app.current_symbol = symbol
                    if symbol in app.bars:
                        app.bars[symbol] = []
                    
                    end_time = ""
                    duration = "3600 S"
                    bar_size = "10 secs"
                    app.reqHistoricalData(4001, contracts[symbol], end_time, duration, bar_size, "TRADES", 1, 1, False, [])
                    time.sleep(3)
                    
                    # Check for exit signal
                    should_exit, exit_msg = check_dynamic_exit(app, symbol)
                    
                    if should_exit:
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        print(f"\n{'='*70}")
                        print(f"[{timestamp}] 🔴 DYNAMIC EXIT TRIGGERED - {symbol}")
                        print(f"{'='*70}")
                        print(f"{exit_msg}")
                        print(f"Cancelling profit taker and placing market sell order...")
                        
                        # Cancel the profit taker order
                        if symbol in app.profit_order_id:
                            app.cancelOrder(app.profit_order_id[symbol], "")
                            print(f"Profit taker order {app.profit_order_id[symbol]} cancelled")
                        
                        # Place market order to exit
                        exit_order = Order()
                        exit_order.action = "SELL"
                        exit_order.orderType = "MKT"
                        exit_order.totalQuantity = app.position[symbol]
                        exit_order.tif = "DAY"
                        
                        exit_id = app.nextOid()
                        exit_order.orderId = exit_id
                        app.placeOrder(exit_order.orderId, contracts[symbol], exit_order)
                        print(f"Market exit order placed: {app.position[symbol]} shares @ MKT")
                        print(f"{'='*70}\n")
                        
                        # Update position tracking
                        app.in_position[symbol] = False
                        app.position[symbol] = 0
                        time.sleep(2)
            
            # Display results table
            if results:
                print(f"{'Symbol':<8} {'Price':<10} {'Pattern':<10} {'MACD':<8} {'Volume':<10} {'VWAP':<8} {'Status':<20}")
                print(f"{'-'*70}")
                
                for r in results:
                    if r.get('skip'):
                        status = r.get('status', 'SKIPPED')
                        print(f"{r['symbol']:<8} {'-':<10} {'-':<10} {'-':<8} {'-':<10} {'-':<8} {status:<20}")
                    else:
                        price_str = f"${r.get('price', 0):.2f}"
                        status = "✓ SIGNAL!" if r.get('all_pass') else "Waiting..."
                        print(f"{r['symbol']:<8} {price_str:<10} {r.get('pattern', '-'):<10} {r.get('macd', '-'):<8} {r.get('volume', '-'):<10} {r.get('vwap', '-'):<8} {status:<20}")
                
                print(f"\n{'='*70}")
            
            # Wait 5 seconds before next check
            print(f"\nNext scan in 5 seconds... (Ctrl+C to stop)")
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\n\nStopping algorithm...")
        for symbol in symbols:
            pos = app.position.get(symbol, 0)
            in_pos = app.in_position.get(symbol, False)
            print(f"{symbol}: Position={pos} shares, In position={in_pos}")
        
        try:
            app.disconnect()
        except Exception:
            pass
        
        print("Disconnected. Check TWS for any open positions/orders.")
