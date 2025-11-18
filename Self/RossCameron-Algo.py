"""
Ross Cameron Style Day Trading Algorithm
==========================================

Entry Rules (ALL must be TRUE):
1. Time Window: 07:00 AM - 10:00 AM EST
2. Pattern: Pullback after surge + First candle making new high after dip
3. MACD: Positive (not negative or crossing down)
4. Volume: No volume top detected, no excessive selling pressure
5. Position Size: Risk ≤ 10% of account balance

Exit Rules:
- Profit target: +10% from entry
- Stop-loss: -5% from entry (bracket order)
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
from datetime import datetime
import pytz
import numpy as np

# PAPER trading port
port = 7497
clientId = 2  # different from Order-LOBO.py

class TradingAlgo(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.oid = 0
        self.account_balance = None
        self.bars = []  # historical 1-min bars
        self.last_price = None
        self.position = 0  # current position size
        self.entry_order_id = None
        self.in_position = False
        self.pending_entry = False
        
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
        """Receive 1-min historical bars"""
        self.bars.append({
            'date': bar.date,
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print(f"Historical data received: {len(self.bars)} bars")

    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
        if tickType == 4:  # LAST price
            self.last_price = price

    def openOrder(self, orderId: OrderId, contract: Contract, order: Order, orderState: OrderState):
        print(f"openOrder. orderId: {orderId}, symbol: {contract.symbol}, action: {order.action}, qty: {order.totalQuantity}, status: {orderState.status}")

    def orderStatus(self, orderId: TickerId, status: str, filled: Decimal, remaining: Decimal, avgFillPrice: float, permId: TickerId, parentId: TickerId, lastFillPrice: float, clientId: TickerId, whyHeld: str, mktCapPrice: float):
        print(f"orderStatus. orderId: {orderId}, status: {status}, filled: {filled}, remaining: {remaining}, avgFillPrice: {avgFillPrice}")
        
        # Track when entry order is filled
        if orderId == self.entry_order_id:
            if status == "Filled":
                self.in_position = True
                self.pending_entry = False
                self.position = int(filled)
                print(f"✓✓✓ ENTRY FILLED: {self.position} shares @ ${avgFillPrice}")
            elif status == "Cancelled":
                self.pending_entry = False
                print("Entry order cancelled")

    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        print(f"Execution: {contract.symbol}, {execution.side}, {execution.shares} @ {execution.price}")
        
        # Track exit fills (profit or stop)
        if execution.side == "SLD" and self.in_position:
            self.position -= int(execution.shares)
            if self.position <= 0:
                self.in_position = False
                self.position = 0
                print(f"✓✓✓ POSITION CLOSED @ ${execution.price}")

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
    """Check if current time is between 07:00 AM - 10:00 AM EST"""
    est = pytz.timezone('US/Eastern')
    now_est = datetime.now(est)
    hour = now_est.hour
    minute = now_est.minute
    
    # 7:00 AM to 10:00 AM EST
    if hour == 7 or hour == 8 or hour == 9 or hour == 10:
        return True
    elif hour == 11 and minute == 0:
        return True
    return False


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
    """
    if len(bars) < 10:
        return False, "Not enough bars"
    
    # Look at recent bars (last 20)
    recent = bars[-20:] if len(bars) >= 20 else bars
    
    # Find the highest high in the period
    highs = [bar['high'] for bar in recent]
    max_high = max(highs)
    max_high_idx = len(recent) - 1 - highs[::-1].index(max_high)
    
    # Check if we had a pullback (price went down from max_high)
    if max_high_idx >= len(recent) - 2:
        return False, "No pullback detected yet (still at high)"
    
    # Check bars after the high for pullback
    pullback_detected = False
    pullback_low = max_high
    
    for i in range(max_high_idx + 1, len(recent)):
        if recent[i]['low'] < pullback_low:
            pullback_low = recent[i]['low']
            pullback_detected = True
    
    if not pullback_detected:
        return False, "No pullback after surge"
    
    # Check if the LAST bar is making a new high (breaking above previous resistance)
    last_bar = recent[-1]
    second_last_bar = recent[-2]
    
    # First candle making new high = current high > previous bar's high
    if last_bar['high'] > second_last_bar['high'] and last_bar['close'] > last_bar['open']:
        pullback_pct = ((max_high - pullback_low) / max_high) * 100
        return True, f"Pattern detected: surge to {max_high:.2f}, pullback to {pullback_low:.2f} (-{pullback_pct:.1f}%), new high at {last_bar['high']:.2f}"
    
    return False, "Waiting for first candle making new high"


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


def check_and_trade(app, contract, symbol):
    """Check conditions and place trade if all criteria met"""
    
    # Don't check if already in position or pending entry
    if app.in_position:
        return
    if app.pending_entry:
        return
    
    # Reset bars for fresh data
    app.bars = []
    
    # Get historical data
    end_time = ""
    duration = "7200 S"
    bar_size = "1 min"
    app.reqHistoricalData(4001, contract, end_time, duration, bar_size, "TRADES", 1, 1, False, [])
    time.sleep(5)
    
    if len(app.bars) < 10:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Insufficient data ({len(app.bars)} bars)")
        return
    
    # Check all conditions
    pattern_ok, pattern_msg = detect_pullback_and_new_high(app.bars)
    macd_ok, macd_msg = check_macd_positive(app.bars)
    volume_ok, volume_msg = check_volume_conditions(app.bars)
    
    timestamp = datetime.now().strftime('%H:%M:%S')
    
    if not (pattern_ok and macd_ok and volume_ok):
        print(f"[{timestamp}] Monitoring {symbol} - Pattern: {'✓' if pattern_ok else '✗'} | MACD: {'✓' if macd_ok else '✗'} | Volume: {'✓' if volume_ok else '✗'}")
        return
    
    # All conditions met!
    print(f"\n{'='*60}")
    print(f"[{timestamp}] ✓✓✓ TRADE SIGNAL - {symbol} ✓✓✓")
    print(f"{'='*60}")
    print(f"Pattern: {pattern_msg}")
    print(f"MACD: {macd_msg}")
    print(f"Volume: {volume_msg}\n")
    
    # Get current price
    app.last_price = None
    app.reqMktData(1, contract, "", False, False, [])
    time.sleep(2)
    app.cancelMktData(1)
    
    if app.last_price is None:
        print("Could not get current price. Skipping trade.")
        return
    
    entry_price = round(app.last_price, 2)
    stop_pct = 0.05
    profit_pct = 0.10
    
    stop_price = round(entry_price * (1 - stop_pct), 2)
    profit_price = round(entry_price * (1 + profit_pct), 2)
    
    qty = calculate_position_size(app.account_balance, entry_price, stop_price, risk_pct=0.10)
    notional = entry_price * qty
    risk_dollars = (entry_price - stop_price) * qty
    risk_pct_actual = (risk_dollars / app.account_balance) * 100
    
    print(f"Trade Plan:")
    print(f"  Entry: ${entry_price} | Stop: ${stop_price} (-{stop_pct*100:.0f}%) | Target: ${profit_price} (+{profit_pct*100:.0f}%)")
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
    
    app.entry_order_id = parent_id
    app.pending_entry = True
    
    # Place orders
    print(f"Placing bracket order: parent={parent_id}, profit={profit_id}, stop={stop_id}")
    app.placeOrder(parent.orderId, contract, parent)
    app.placeOrder(profit_taker.orderId, contract, profit_taker)
    app.placeOrder(stop_loss.orderId, contract, stop_loss)
    print(f"✓ Orders placed!\n")


if __name__ == "__main__":
    symbol = input("Enter stock symbol (e.g., LOBO): ").strip().upper()
    if not symbol:
        print("No symbol entered. Exiting.")
        exit(1)
    
    print(f"\n{'='*60}")
    print(f"Ross Cameron Style Trading Algorithm - {symbol}")
    print(f"Continuous Monitoring Mode")
    print(f"Press Ctrl+C to stop")
    print(f"{'='*60}\n")
    
    # Create contract
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    
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
        while True:
            # Check time window
            if not is_trading_hours():
                now_est = datetime.now(pytz.timezone('US/Eastern'))
                print(f"[{now_est.strftime('%H:%M:%S')}] Outside trading hours. Waiting...")
                time.sleep(300)  # wait 5 minutes
                continue
            
            # Check conditions and trade if signal
            check_and_trade(app, contract, symbol)
            
            # Wait 15 seconds before next check
            time.sleep(15)
            
    except KeyboardInterrupt:
        print("\n\nStopping algorithm...")
        print(f"Final position: {app.position} shares")
        print(f"In position: {app.in_position}")
        
        try:
            app.disconnect()
        except Exception:
            pass
        
        print("Disconnected. Check TWS for any open positions/orders.")
