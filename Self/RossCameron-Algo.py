"""
Ross Cameron Style Day Trading Algorithm
==========================================

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
- Stop Loss: Structural stop at pullback low OR recent high (if >10% breakout), 1% buffer, minimum 2% distance
- Backup Profit Target: +20% limit order (cancelled if dynamic exit triggers first)
- End of Day: All positions closed at market close (configured in StrategyConfig)

Pre-Market Hours (configured in StrategyConfig):
- Only limit orders allowed (entry at ASK, exit at BID)
- Stop loss and profit targets monitored manually with limit orders
- At market open, automatic stop loss orders are added for pre-market positions

Regular Hours (configured in StrategyConfig):
- Full bracket orders with stop loss protection

Features:
- Multi-symbol scanning (up to 3 stocks simultaneously)
- 10-second bars for fast pattern/MACD/volume analysis
- 1-minute bars for VWAP calculation
- Real-time monitoring dashboard with clean table visualization
- Paper trading via Interactive Brokers API (port 7497)
- Smart order routing for pre-market vs regular hours
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
import importlib.util

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

# PAPER trading port
port = 7497
clientId = 3  # different from Order-LOBO.py (changed from 2 to avoid conflict)

class TradingAlgo(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.oid = 0
        self.account_balance = None
        self.bars = {}  # dictionary: symbol -> list of historical 10-sec bars
        self.bars_1min = {}  # dictionary: symbol -> list of 1-min bars for VWAP
        self.last_price = {}
        self.ask_price = {}
        self.bid_price = {}  # bid price for selling in pre-market
        self.position = {}  # current position size per symbol
        self.entry_order_id = {}  # entry order ID per symbol
        self.profit_order_id = {}  # profit taker order ID per symbol
        self.stop_order_id = {}  # stop loss order ID per symbol
        self.profit_order_active = {}  # track if profit order is still active
        self.stop_order_active = {}  # track if stop order is still active
        self.in_position = {}  # bool per symbol
        self.pending_entry = {}  # bool per symbol
        self.pending_entry_time = {}  # timestamp when pending entry was set
        self.premarket_entry = {}  # track if position entered during pre-market (initialized per symbol)
        self.entry_price = {}  # track actual entry price per symbol
        self.stop_price = {}  # track stop loss price per symbol
        self.profit_target_price = {}  # track profit target price per symbol
        self.oca_group = {}  # track OCA group per symbol (for One-Cancels-All exit orders)
        self.brackets_added_at_open = {}  # track if brackets were added at market open for pre-market positions
        self.current_symbol = None  # track which symbol is being processed
        self.current_reqid = None  # track which request ID is being processed
        self.vwap_cache = {}  # cached VWAP value per symbol
        self.vwap_last_update = {}  # timestamp of last VWAP update per symbol
        
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
            # Parse bar.date string to datetime object
            # IBKR returns format: "20231201 09:30:00" or "20231201  09:30:00" (US/Eastern)
            try:
                bar_date = datetime.strptime(bar.date.strip(), '%Y%m%d %H:%M:%S')
            except ValueError:
                try:
                    bar_date = datetime.strptime(bar.date.strip(), '%Y%m%d  %H:%M:%S')
                except ValueError:
                    # Fallback: already datetime or other format
                    bar_date = bar.date if isinstance(bar.date, datetime) else datetime.now()
            
            # Add timezone info (EST) to bar_date for proper comparison
            est = timezone(timedelta(hours=-5))
            if bar_date.tzinfo is None:
                bar_date = bar_date.replace(tzinfo=est)
            
            # reqId 4001 = 10-second bars, reqId 4002 = 1-minute bars
            if reqId == 4001:
                if self.current_symbol not in self.bars:
                    self.bars[self.current_symbol] = []
                self.bars[self.current_symbol].append({
                    'date': bar_date,
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
                    'date': bar_date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        # Data collection complete - suppress messages for cleaner display
        pass

    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
        if self.current_symbol:
            if tickType == 4:  # LAST price
                self.last_price[self.current_symbol] = price
            elif tickType == 2:  # ASK price
                self.ask_price[self.current_symbol] = price
            elif tickType == 1:  # BID price
                self.bid_price[self.current_symbol] = price

    def openOrder(self, orderId: OrderId, contract: Contract, order: Order, orderState: OrderState):
        print(f"openOrder. orderId: {orderId}, symbol: {contract.symbol}, action: {order.action}, qty: {order.totalQuantity}, status: {orderState.status}")

    def orderStatus(self, orderId: TickerId, status: str, filled: Decimal, remaining: Decimal, avgFillPrice: float, permId: TickerId, parentId: TickerId, lastFillPrice: float, clientId: TickerId, whyHeld: str, mktCapPrice: float):
        print(f"orderStatus. orderId: {orderId}, status: {status}, filled: {filled}, remaining: {remaining}, avgFillPrice: {avgFillPrice}")
        
        # Track profit/stop order status changes
        for symbol in list(self.profit_order_id.keys()):
            if orderId == self.profit_order_id.get(symbol):
                if status in ["Cancelled", "Filled", "Inactive"]:
                    self.profit_order_active[symbol] = False
                    print(f"Profit order {orderId} for {symbol} is now inactive ({status})")
                elif status in ["Submitted", "PreSubmitted"]:
                    self.profit_order_active[symbol] = True
                break
        
        for symbol in list(self.stop_order_id.keys()):
            if orderId == self.stop_order_id.get(symbol):
                if status in ["Cancelled", "Filled", "Inactive"]:
                    self.stop_order_active[symbol] = False
                    print(f"Stop order {orderId} for {symbol} is now inactive ({status})")
                elif status in ["Submitted", "PreSubmitted"]:
                    self.stop_order_active[symbol] = True
                break
        
        # Track when entry order is filled
        for symbol, entry_id in self.entry_order_id.items():
            if orderId == entry_id:
                if status == "Filled":
                    self.in_position[symbol] = True
                    self.pending_entry[symbol] = False
                    self.position[symbol] = int(filled)
                    self.entry_price[symbol] = avgFillPrice
                    print(f"✓✓✓ ENTRY FILLED ({symbol}): {self.position[symbol]} shares @ ${avgFillPrice}")
                    
                    # If filled during regular hours but was a pre-market order, immediately add stop/profit orders
                    if (is_regular_hours() and 
                        symbol in self.premarket_entry and 
                        self.premarket_entry.get(symbol, False) and
                        symbol not in self.stop_order_id):
                        
                        print(f"⚙️  Pre-market order filled in regular hours - adding stop loss for {symbol}")
                        
                        # This will be handled in main loop, but set flag to trigger it
                        # The main loop will pick this up on next iteration
                        pass
                        
                elif status == "Cancelled":
                    self.pending_entry[symbol] = False
                    # Clean up pre-market flag if order was cancelled
                    if symbol in self.premarket_entry:
                        self.premarket_entry[symbol] = False
                    if symbol in self.pending_entry_time:
                        del self.pending_entry_time[symbol]
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
                
                # Clean up all tracking dictionaries for this symbol
                if symbol in self.entry_order_id:
                    del self.entry_order_id[symbol]
                if symbol in self.profit_order_id:
                    del self.profit_order_id[symbol]
                if symbol in self.stop_order_id:
                    del self.stop_order_id[symbol]
                if symbol in self.premarket_entry:
                    self.premarket_entry[symbol] = False
                if symbol in self.entry_price:
                    del self.entry_price[symbol]
                if symbol in self.stop_price:
                    del self.stop_price[symbol]
                if symbol in self.profit_target_price:
                    del self.profit_target_price[symbol]
                
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


def is_premarket():
    """Check if current time is pre-market hours (configured in StrategyConfig)"""
    est = timezone(timedelta(hours=-5))  # EST is UTC-5
    now_est = datetime.now(est)
    hour = now_est.hour
    minute = now_est.minute
    
    # Use StrategyConfig for trading hours
    start_hour = StrategyConfig.PREMARKET_START_HOUR
    start_min = StrategyConfig.PREMARKET_START_MINUTE
    open_hour = StrategyConfig.MARKET_OPEN_HOUR
    open_min = StrategyConfig.MARKET_OPEN_MINUTE
    
    # Check if in pre-market window
    if hour > start_hour and hour < open_hour:
        return True
    elif hour == start_hour and minute >= start_min:
        return True
    elif hour == open_hour and minute < open_min:
        return True
    return False


def is_regular_hours():
    """Check if current time is regular market hours (configured in StrategyConfig)"""
    est = timezone(timedelta(hours=-5))  # EST is UTC-5
    now_est = datetime.now(est)
    hour = now_est.hour
    minute = now_est.minute
    
    # Use StrategyConfig for trading hours
    open_hour = StrategyConfig.MARKET_OPEN_HOUR
    open_min = StrategyConfig.MARKET_OPEN_MINUTE
    close_hour = StrategyConfig.MARKET_CLOSE_HOUR
    close_min = StrategyConfig.MARKET_CLOSE_MINUTE
    
    # Market open to close
    if hour > open_hour and hour < close_hour:
        return True
    elif hour == open_hour and minute >= open_min:
        return True
    elif hour == close_hour and minute <= close_min:
        return True
    return False


def is_trading_hours():
    """Check if current time is within trading hours (pre-market + regular hours from StrategyConfig)"""
    return is_premarket() or is_regular_hours()


def is_near_close():
    """Check if we're within 5 minutes of market close (wrapper for shared strategy)"""
    est = timezone(timedelta(hours=-5))
    now_est = datetime.now(est)
    return check_end_of_day(now_est)


def check_and_trade(app, contract, symbol):
    """Check conditions and place trade if all criteria met"""
    
    # Initialize symbol tracking if needed
    if symbol not in app.in_position:
        app.in_position[symbol] = False
    if symbol not in app.pending_entry:
        app.pending_entry[symbol] = False
    if symbol not in app.position:
        app.position[symbol] = 0
    if symbol not in app.premarket_entry:
        app.premarket_entry[symbol] = False
    
    # Don't check if already in position OR pending entry
    if app.in_position[symbol] or app.pending_entry[symbol]:
        # Get current price for display
        app.current_symbol = symbol
        if symbol in app.last_price:
            del app.last_price[symbol]
        app.reqMktData(1, contract, "", False, False, [])
        time.sleep(1)
        app.cancelMktData(1)
        
        current_price = app.last_price.get(symbol, 0)
        
        # Return appropriate status
        if app.pending_entry[symbol]:
            return {"symbol": symbol, "status": "PENDING ENTRY", "skip": True, "price": current_price}
        
        # Return position details for display
        entry = app.entry_price.get(symbol, 0)
        stop = app.stop_price.get(symbol, 0)
        profit = app.profit_target_price.get(symbol, 0)
        qty = app.position.get(symbol, 0)
        return {
            "symbol": symbol, 
            "status": "IN POSITION", 
            "skip": True,
            "price": current_price,
            "entry_price": entry,
            "stop_price": stop,
            "profit_price": profit,
            "quantity": qty
        }
    
    # Check for stale pending orders (over 5 minutes old) and cancel them
    if app.pending_entry[symbol]:
        if symbol in app.pending_entry_time:
            elapsed = time.time() - app.pending_entry_time[symbol]
            if elapsed > 300:  # 5 minutes
                print(f"\n[WARNING] Stale pending order for {symbol} ({elapsed:.0f}s old) - cancelling...")
                if symbol in app.entry_order_id:
                    app.cancelOrder(app.entry_order_id[symbol])
                    del app.entry_order_id[symbol]
                app.pending_entry[symbol] = False
                if symbol in app.pending_entry_time:
                    del app.pending_entry_time[symbol]
                if symbol in app.premarket_entry:
                    app.premarket_entry[symbol] = False
                if symbol in app.stop_price:
                    del app.stop_price[symbol]
                if symbol in app.profit_target_price:
                    del app.profit_target_price[symbol]
            else:
                return {"symbol": symbol, "status": "PENDING ENTRY", "skip": True}
        else:
            return {"symbol": symbol, "status": "PENDING ENTRY", "skip": True}
    
    # Set current symbol for callbacks
    app.current_symbol = symbol
    
    # Reset bars for fresh data (10-second bars only)
    if symbol in app.bars:
        app.bars[symbol] = []
    
    # Get historical data - 10 second bars for pattern/MACD/volume (fast refresh)
    end_time = ""
    duration = StrategyConfig.DATA_DURATION_10SEC
    bar_size = StrategyConfig.BAR_SIZE_10SEC
    app.reqHistoricalData(4001, contract, end_time, duration, bar_size, "TRADES", 1, 1, False, [])
    time.sleep(3)
    
    if symbol not in app.bars or len(app.bars[symbol]) < 10:
        bars_count = len(app.bars.get(symbol, []))
        return {"symbol": symbol, "status": "INSUFFICIENT DATA", "bars": bars_count, "skip": True}
    
    # Check if we need to refresh VWAP (only every 60 seconds)
    current_time = time.time()
    need_vwap_refresh = True
    
    if symbol in app.vwap_last_update:
        time_since_update = current_time - app.vwap_last_update[symbol]
        if time_since_update < 60:  # Less than 60 seconds since last update
            need_vwap_refresh = False
    
    # Get 1-minute bars for VWAP only if needed (slow refresh)
    if need_vwap_refresh:
        if symbol in app.bars_1min:
            app.bars_1min[symbol] = []
        
        duration_1min = StrategyConfig.DATA_DURATION_1MIN
        bar_size_1min = StrategyConfig.BAR_SIZE_1MIN
        app.reqHistoricalData(4002, contract, end_time, duration_1min, bar_size_1min, "TRADES", 1, 1, False, [])
        time.sleep(3)
        
        if symbol not in app.bars_1min or len(app.bars_1min[symbol]) < 10:
            bars_count = len(app.bars_1min.get(symbol, []))
            return {"symbol": symbol, "status": "INSUFFICIENT 1M DATA", "bars": bars_count, "skip": True}
        
        app.vwap_last_update[symbol] = current_time
    
    # Get current ask price first for VWAP check
    if symbol in app.ask_price:
        del app.ask_price[symbol]
    app.reqMktData(1, contract, "", False, False, [])
    time.sleep(2)
    app.cancelMktData(1)
    
    if symbol not in app.ask_price or app.ask_price[symbol] is None:
        return {"symbol": symbol, "status": "NO PRICE DATA", "skip": True}
    
    current_price = app.ask_price[symbol]
    
    # Filter 1-min bars for session VWAP (same logic as backtest)
    est = timezone(timedelta(hours=-5))
    now_est = datetime.now(est)
    market_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    
    # Validate bars have datetime objects (should be set in historicalData callback)
    if not all(isinstance(b.get('date'), datetime) for b in app.bars_1min[symbol]):
        return {"symbol": symbol, "status": "INVALID BAR DATES", "skip": True}
    
    if now_est.hour < 9 or (now_est.hour == 9 and now_est.minute < 30):
        # Pre-market: use all available bars (no filtering needed)
        filtered_bars_1m = app.bars_1min[symbol]
    else:
        # Regular hours: use ONLY bars >= 9:30 AM TODAY for session VWAP
        # Compare actual datetime objects, not just hour/minute (to exclude pre-market)
        filtered_bars_1m = [b for b in app.bars_1min[symbol] if b['date'] >= market_open]
    
    if len(filtered_bars_1m) < 10:
        return {"symbol": symbol, "status": "INSUFFICIENT SESSION DATA", "bars": len(filtered_bars_1m), "skip": True}
    
    # Check all entry conditions using shared strategy module (filtered 1-min bars only)
    all_ok, results, pullback_low_price, recent_high_price = check_all_entry_conditions(
        filtered_bars_1m, 
        current_price
    )
    
    # Extract individual results for display
    pattern_ok = results.get('pattern', {}).get('ok', False)
    pattern_msg = results.get('pattern', {}).get('msg', "")
    macd_ok = results.get('macd', {}).get('ok', False)
    macd_msg = results.get('macd', {}).get('msg', "")
    volume_ok = results.get('volume', {}).get('ok', False)
    volume_msg = results.get('volume', {}).get('msg', "")
    vwap_ok = results.get('vwap', {}).get('ok', False)
    vwap_msg = results.get('vwap', {}).get('msg', "")

    
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
    
    # Calculate entry/exit prices using shared strategy module
    entry_price, stop_price, profit_price = calculate_entry_exit_prices(
        app.ask_price[symbol], 
        pullback_low_price,
        recent_high_price
    )
    
    # Validate that we got valid prices
    if entry_price is None or stop_price is None or profit_price is None:
        print(f"Invalid entry/exit prices. Stop may be too close (<2%) or invalid. Skipping trade.")
        result["status"] = "INVALID PRICES"
        result["skip"] = True
        return result
    
    # Calculate actual risk percentage
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        print(f"Invalid stop loss: stop ${stop_price} >= entry ${entry_price}. Skipping trade.")
        result["status"] = "INVALID STOP"
        result["skip"] = True
        return result
    
    stop_pct_actual = (risk_per_share / entry_price) * 100
    
    qty = calculate_position_size(app.account_balance, entry_price, stop_price)

    
    # Validate position size
    if qty <= 0:
        print(f"Invalid position size: {qty} shares. Skipping trade.")
        result["status"] = "INVALID SIZE"
        result["skip"] = True
        return result
    
    notional = entry_price * qty
    risk_dollars = risk_per_share * qty
    risk_pct_actual = (risk_dollars / app.account_balance) * 100
    
    # Determine which stop logic was used
    breakout_pct = ((entry_price - recent_high_price) / recent_high_price) * 100 if recent_high_price else 0
    stop_type = "recent high" if breakout_pct > 10.0 else "pullback low"
    
    print(f"Trade Plan:")
    print(f"  Entry: ${entry_price} | Stop: ${stop_price} ({stop_type}, -{stop_pct_actual:.1f}%) | Target: ${profit_price} (+{StrategyConfig.PROFIT_TARGET_PCT*100:.0f}%)")
    print(f"  Quantity: {qty} shares | Notional: ${notional:.2f} | Risk: ${risk_dollars:.2f} ({risk_pct_actual:.1f}%)\n")
    
    # Check if pre-market hours
    in_premarket = is_premarket()
    
    if in_premarket:
        print(f"⚠️  PRE-MARKET MODE: Stop loss will be monitored manually with limit orders\n")
    
    # Build bracket orders
    parent = Order()
    parent.action = "BUY"
    parent.orderType = "LMT"
    parent.lmtPrice = entry_price
    parent.totalQuantity = qty
    parent.tif = "DAY"
    parent.transmit = True  # Always transmit entry order
    try:
        parent.eTradeOnly = False
        parent.firmQuoteOnly = False
    except Exception:
        pass
    
    # Assign order IDs
    parent_id = app.nextOid()
    
    # Store prices for pre-market monitoring
    app.stop_price[symbol] = stop_price
    app.profit_target_price[symbol] = profit_price
    
    if not in_premarket:
        # Regular hours: place full bracket order with stop loss
        # Use OCA (One-Cancels-All) group so IBKR automatically cancels other orders when one fills
        oca_group_name = f"OCA_{symbol}_{parent_id}"
        app.oca_group[symbol] = oca_group_name
        
        profit_taker = Order()
        profit_taker.action = "SELL"
        profit_taker.orderType = "LMT"
        profit_taker.lmtPrice = profit_price
        profit_taker.totalQuantity = qty
        profit_taker.tif = "GTC"
        profit_taker.transmit = True  # Must transmit to exchange
        profit_taker.ocaGroup = oca_group_name  # Set OCA group
        profit_taker.ocaType = 1  # Cancel all remaining orders on fill
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
        stop_loss.transmit = True  # Must transmit to exchange
        stop_loss.ocaGroup = oca_group_name  # Set OCA group
        stop_loss.ocaType = 1  # Cancel all remaining orders on fill
        try:
            stop_loss.eTradeOnly = False
            stop_loss.firmQuoteOnly = False
        except Exception:
            pass
        
        profit_id = app.nextOid()
        stop_id = app.nextOid()
        
        profit_taker.orderId = profit_id
        # Don't use parentId - let OCA group handle order relationships
        # profit_taker.parentId = parent_id  # REMOVED: causes \"parent being cancelled\" errors
        stop_loss.orderId = stop_id
        # stop_loss.parentId = parent_id     # REMOVED: OCA group handles this instead
        
        app.profit_order_id[symbol] = profit_id
        app.stop_order_id[symbol] = stop_id
        app.profit_order_active[symbol] = True  # Mark as active when placed
        app.stop_order_active[symbol] = True    # Mark as active when placed
        
        print(f"[DEBUG] OCA group '{oca_group_name}' created for {symbol} bracket orders")
        
        print(f"[DEBUG] Bracket orders for {symbol}: Profit ID={profit_id}, Stop ID={stop_id}, both marked ACTIVE")
    
    parent.orderId = parent_id
    
    # Set tracking BEFORE placing orders to prevent race conditions
    app.entry_order_id[symbol] = parent_id
    app.pending_entry[symbol] = True
    app.pending_entry_time[symbol] = time.time()  # Track when order was placed
    
    if in_premarket:
        app.premarket_entry[symbol] = True
    
    # Place orders
    if in_premarket:
        print(f"Placing entry order for {symbol}: parent={parent_id} (pre-market mode)")
        app.placeOrder(parent.orderId, contract, parent)
        print(f"✓ Entry order placed! Stop/profit will be added after 9:30 AM or monitored manually.\n")
    else:
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
        app.account_balance = StrategyConfig.DEFAULT_ACCOUNT_BALANCE
    else:
        print(f"Account balance: ${app.account_balance:.2f}\n")
    
    print("Starting continuous monitoring...\n")
    
    # Continuous monitoring loop
    try:
        scan_count = 0
        previous_results = {}
        last_display_time = 0
        while True:
            scan_count += 1
            
            # Check if near market close - close all positions AND cancel pending orders
            if is_near_close():
                est = timezone(timedelta(hours=-5))
                now_est = datetime.now(est)
                has_positions = False
                has_pending = False
                
                for symbol in symbols:
                    # Close filled positions
                    if symbol in app.in_position and app.in_position[symbol] and app.position.get(symbol, 0) > 0:
                        has_positions = True
                        print(f"\n[{now_est.strftime('%H:%M:%S')}] Near market close - closing {symbol} position...")
                        
                        # Cancel existing profit and stop orders first
                        if symbol in app.profit_order_id:
                            app.cancelOrder(app.profit_order_id[symbol])
                        if symbol in app.stop_order_id:
                            app.cancelOrder(app.stop_order_id[symbol])
                        
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
                        
                        # Clean up all tracking
                        app.in_position[symbol] = False
                        app.position[symbol] = 0
                        if symbol in app.entry_order_id:
                            del app.entry_order_id[symbol]
                        if symbol in app.profit_order_id:
                            del app.profit_order_id[symbol]
                        if symbol in app.stop_order_id:
                            del app.stop_order_id[symbol]
                        if symbol in app.premarket_entry:
                            app.premarket_entry[symbol] = False
                        if symbol in app.entry_price:
                            del app.entry_price[symbol]
                        if symbol in app.stop_price:
                            del app.stop_price[symbol]
                        if symbol in app.profit_target_price:
                            del app.profit_target_price[symbol]
                        time.sleep(2)
                    
                    # Cancel pending entry orders
                    elif symbol in app.pending_entry and app.pending_entry[symbol]:
                        has_pending = True
                        print(f"\n[{now_est.strftime('%H:%M:%S')}] Near market close - cancelling pending entry order for {symbol}...")
                        
                        if symbol in app.entry_order_id:
                            app.cancelOrder(app.entry_order_id[symbol])
                            print(f"Entry order {app.entry_order_id[symbol]} cancelled")
                        
                        # Clean up tracking
                        app.pending_entry[symbol] = False
                        if symbol in app.entry_order_id:
                            del app.entry_order_id[symbol]
                        if symbol in app.premarket_entry:
                            app.premarket_entry[symbol] = False
                        if symbol in app.stop_price:
                            del app.stop_price[symbol]
                        if symbol in app.profit_target_price:
                            del app.profit_target_price[symbol]
                        time.sleep(1)
                
                if not has_positions and not has_pending:
                    time.sleep(60)
                continue
            
            # Check time window and show current time
            est = timezone(timedelta(hours=-5))
            now_est = datetime.now(est)
            current_time_str = now_est.strftime('%H:%M:%S')
            current_time = time.time()
            
            # Check if within trading hours (5:00 AM - 3:50 PM EST)
            if not is_trading_hours():
                if current_time - last_display_time > 60:  # Update every 60 seconds when outside hours
                    os.system('cls' if os.name == 'nt' else 'clear')
                    print(f"\n[{current_time_str}] Outside trading hours (5:00 AM - 3:50 PM EST). Waiting...")
                    last_display_time = current_time
                time.sleep(60)  # check every minute
                continue
            
            # Check conditions and trade for each symbol (silently)
            results = []
            for symbol in symbols:
                result = check_and_trade(app, contracts[symbol], symbol)
                if result:
                    results.append(result)
            
            # Detect if anything changed
            results_changed = False
            current_results_hash = {}
            for r in results:
                if not r.get('skip'):
                    key = r['symbol']
                    status = "SIGNAL" if r.get('all_pass') else "WAITING"
                    pos_status = "IN_POS" if app.in_position.get(key, False) else "NO_POS"
                    pending_status = "PENDING" if app.pending_entry.get(key, False) else "NO_PENDING"
                    current_results_hash[key] = f"{status}|{pos_status}|{pending_status}|{r.get('pattern')}|{r.get('macd')}|{r.get('volume')}|{r.get('vwap')}"
                    
                    if key not in previous_results or previous_results[key] != current_results_hash[key]:
                        results_changed = True
            
            # Only update display if something changed or every 30 seconds
            if results_changed or (current_time - last_display_time > 30):
                # Get update timestamp
                est = timezone(timedelta(hours=-5))
                now_est = datetime.now(est)
                update_time_str = now_est.strftime('%H:%M:%S')
                
                # Clear screen and print header
                os.system('cls' if os.name == 'nt' else 'clear')
                print(f"{'='*70}")
                print(f"  ROSS CAMERON MOMENTUM SCANNER  |  Scan #{scan_count}  |  {update_time_str} EST")
                print(f"{'='*70}")
                print(f"  Account: ${app.account_balance:.2f} ")
                print(f"{'='*70}\n")
                
                previous_results = current_results_hash
                last_display_time = current_time
                
                # Display results table
                if results:
                    print(f"{'Symbol':<8} {'Price':<10} {'Pattern':<10} {'MACD':<8} {'Volume':<10} {'VWAP':<8} {'Status':<20}")
                    print(f"{'-'*70}")
                    
                    for r in results:
                        if r.get('skip'):
                            status = r.get('status', 'SKIPPED')
                            # If in position, show entry/stop/profit details
                            if status == "IN POSITION":
                                current = r.get('price', 0)
                                entry = r.get('entry_price', 0)
                                stop = r.get('stop_price', 0)
                                profit = r.get('profit_price', 0)
                                qty = r.get('quantity', 0)
                                
                                # Calculate P&L
                                pnl = (current - entry) * qty if current > 0 and entry > 0 else 0
                                pnl_pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else 0
                                pnl_str = f"+${pnl:.2f} (+{pnl_pct:.1f}%)" if pnl >= 0 else f"-${abs(pnl):.2f} ({pnl_pct:.1f}%)"
                                
                                print(f"{r['symbol']:<8} IN POSITION - Last:${current:.2f} {pnl_str}")
                                print(f"         Entry:${entry:.2f} | Stop:${stop:.2f} | Target:${profit:.2f} | Qty:{qty}")
                            else:
                                print(f"{r['symbol']:<8} {'-':<10} {'-':<10} {'-':<8} {'-':<10} {'-':<8} {status:<20}")
                        else:
                            price_str = f"${r.get('price', 0):.2f}"
                            status = "✓ SIGNAL!" if r.get('all_pass') else "Waiting..."
                            print(f"{r['symbol']:<8} {price_str:<10} {r.get('pattern', '-'):<10} {r.get('macd', '-'):<8} {r.get('volume', '-'):<10} {r.get('vwap', '-'):<8} {status:<20}")
                    
                    print(f"\n{'='*70}")
                    print(f"Monitoring... (updates on change, Ctrl+C to stop)")
            
            # TRANSITION: Add stop loss orders for pre-market positions when regular hours begin
            if is_regular_hours():
                for symbol in symbols:
                    if (symbol in app.in_position and app.in_position[symbol] and 
                        symbol in app.premarket_entry and app.premarket_entry.get(symbol, False) and
                        symbol not in app.stop_order_id):
                        
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        print(f"\n{'='*70}")
                        print(f"[{timestamp}] ⚙️  ADDING STOP LOSS - {symbol} (regular hours)")
                        print(f"{'='*70}")
                        
                        # Add profit taker and stop loss orders
                        stop_price = app.stop_price.get(symbol, 0)
                        profit_price = app.profit_target_price.get(symbol, 0)
                        qty = app.position[symbol]
                        
                        if stop_price > 0 and profit_price > 0 and qty > 0:
                            # Profit taker
                            profit_taker = Order()
                            profit_taker.action = "SELL"
                            profit_taker.orderType = "LMT"
                            profit_taker.lmtPrice = profit_price
                            profit_taker.totalQuantity = qty
                            profit_taker.tif = "GTC"
                            
                            # Stop loss
                            stop_loss = Order()
                            stop_loss.action = "SELL"
                            stop_loss.orderType = "STP"
                            stop_loss.auxPrice = stop_price
                            stop_loss.totalQuantity = qty
                            stop_loss.tif = "GTC"
                            
                            profit_id = app.nextOid()
                            stop_id = app.nextOid()
                            
                            profit_taker.orderId = profit_id
                            stop_loss.orderId = stop_id
                            
                            app.placeOrder(profit_id, contracts[symbol], profit_taker)
                            app.placeOrder(stop_id, contracts[symbol], stop_loss)
                            
                            app.profit_order_id[symbol] = profit_id
                            app.stop_order_id[symbol] = stop_id
                            app.profit_order_active[symbol] = True
                            app.stop_order_active[symbol] = True
                            app.premarket_entry[symbol] = False
                            
                            print(f"Stop loss @ ${stop_price} and profit target @ ${profit_price} added")
                            print(f"{'='*70}\n")
                            time.sleep(1)
            
            # Monitor active positions for dynamic exit (Candle Under Candle)
            for symbol in symbols:
                if symbol in app.in_position and app.in_position[symbol]:
                    
                    # CRITICAL: Check if this is a pre-market position that needs stop loss added NOW
                    # (handles case where pre-market order filled during regular hours)
                    if (is_regular_hours() and 
                        symbol in app.premarket_entry and 
                        app.premarket_entry.get(symbol, False) and
                        symbol not in app.stop_order_id):
                        
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        print(f"\n{'='*70}")
                        print(f"[{timestamp}] ⚙️  ADDING STOP LOSS (IMMEDIATE) - {symbol}")
                        print(f"{'='*70}")
                        
                        stop_price = app.stop_price.get(symbol, 0)
                        profit_price = app.profit_target_price.get(symbol, 0)
                        qty = app.position[symbol]
                        
                        if stop_price > 0 and profit_price > 0 and qty > 0:
                            # Profit taker
                            profit_taker = Order()
                            profit_taker.action = "SELL"
                            profit_taker.orderType = "LMT"
                            profit_taker.lmtPrice = profit_price
                            profit_taker.totalQuantity = qty
                            profit_taker.tif = "GTC"
                            
                            # Stop loss
                            stop_loss = Order()
                            stop_loss.action = "SELL"
                            stop_loss.orderType = "STP"
                            stop_loss.auxPrice = stop_price
                            stop_loss.totalQuantity = qty
                            stop_loss.tif = "GTC"
                            
                            profit_id = app.nextOid()
                            stop_id = app.nextOid()
                            
                            profit_taker.orderId = profit_id
                            stop_loss.orderId = stop_id
                            
                            app.placeOrder(profit_id, contracts[symbol], profit_taker)
                            app.placeOrder(stop_id, contracts[symbol], stop_loss)
                            
                            app.profit_order_id[symbol] = profit_id
                            app.stop_order_id[symbol] = stop_id
                            app.profit_order_active[symbol] = True
                            app.stop_order_active[symbol] = True
                            app.premarket_entry[symbol] = False
                            
                            print(f"Stop loss @ ${stop_price} and profit target @ ${profit_price} added")
                            print(f"{'='*70}\n")
                            time.sleep(1)
                    
                    # Request fresh 10-second bar data for exit monitoring
                    app.current_symbol = symbol
                    
                    # Don't clear bars - accumulate them (keep last 100 bars for better pattern detection)
                    if symbol not in app.bars:
                        app.bars[symbol] = []
                    
                    # Clear and fetch fresh data to get latest bars
                    app.bars[symbol] = []
                    
                    end_time = ""
                    duration = StrategyConfig.DATA_DURATION_10SEC
                    bar_size = StrategyConfig.BAR_SIZE_10SEC
                    app.reqHistoricalData(4001, contracts[symbol], end_time, duration, bar_size, "TRADES", 1, 1, False, [])
                    time.sleep(3)
                    
                    # Debug: Check how many bars we received
                    bar_count = len(app.bars.get(symbol, []))
                    if bar_count < 2:
                        print(f"[WARNING] {symbol}: Only {bar_count} bars received for exit monitoring. Skipping dynamic exit check.")
                        continue
                    
                    # PRE-MARKET: Monitor stop loss and profit target with limit orders
                    if is_premarket() and symbol in app.premarket_entry and app.premarket_entry.get(symbol, False):
                        # Get current bid price for selling
                        if symbol in app.bid_price:
                            del app.bid_price[symbol]
                        app.reqMktData(1, contracts[symbol], "", False, False, [])
                        time.sleep(2)
                        app.cancelMktData(1)
                        
                        if symbol in app.bid_price and app.bid_price[symbol] is not None:
                            current_bid = app.bid_price[symbol]
                            stop_price = app.stop_price.get(symbol, 0)
                            profit_price = app.profit_target_price.get(symbol, 0)
                            
                            # Check if stop loss triggered (bid at or below stop price)
                            if current_bid <= stop_price:
                                timestamp = datetime.now().strftime('%H:%M:%S')
                                print(f"\n{'='*70}")
                                print(f"[{timestamp}] 🛑 PRE-MARKET STOP LOSS - {symbol}")
                                print(f"{'='*70}")
                                print(f"Bid ${current_bid:.2f} <= Stop ${stop_price:.2f}")
                                print(f"Placing limit sell order at bid price...")
                                
                                # Place limit order at current bid to ensure fill
                                stop_order = Order()
                                stop_order.action = "SELL"
                                stop_order.orderType = "LMT"
                                stop_order.lmtPrice = current_bid
                                stop_order.totalQuantity = app.position[symbol]
                                stop_order.tif = "DAY"
                                
                                stop_id = app.nextOid()
                                stop_order.orderId = stop_id
                                app.placeOrder(stop_order.orderId, contracts[symbol], stop_order)
                                print(f"Limit sell order placed: {app.position[symbol]} shares @ ${current_bid}")
                                print(f"{'='*70}\n")
                                
                                # Clean up all tracking
                                app.in_position[symbol] = False
                                app.position[symbol] = 0
                                app.premarket_entry[symbol] = False
                                if symbol in app.entry_order_id:
                                    del app.entry_order_id[symbol]
                                if symbol in app.entry_price:
                                    del app.entry_price[symbol]
                                if symbol in app.stop_price:
                                    del app.stop_price[symbol]
                                if symbol in app.profit_target_price:
                                    del app.profit_target_price[symbol]
                                time.sleep(2)
                                continue
                            
                            # Check if profit target hit (bid at or above profit price)
                            elif current_bid >= profit_price:
                                timestamp = datetime.now().strftime('%H:%M:%S')
                                print(f"\n{'='*70}")
                                print(f"[{timestamp}] 💰 PRE-MARKET PROFIT TARGET - {symbol}")
                                print(f"{'='*70}")
                                print(f"Bid ${current_bid:.2f} >= Target ${profit_price:.2f}")
                                print(f"Placing limit sell order at bid price...")
                                
                                # Place limit order at current bid to ensure fill
                                profit_order = Order()
                                profit_order.action = "SELL"
                                profit_order.orderType = "LMT"
                                profit_order.lmtPrice = current_bid
                                profit_order.totalQuantity = app.position[symbol]
                                profit_order.tif = "DAY"
                                
                                profit_id = app.nextOid()
                                profit_order.orderId = profit_id
                                app.placeOrder(profit_order.orderId, contracts[symbol], profit_order)
                                print(f"Limit sell order placed: {app.position[symbol]} shares @ ${current_bid}")
                                print(f"{'='*70}\n")
                                
                                # Clean up all tracking
                                app.in_position[symbol] = False
                                app.position[symbol] = 0
                                app.premarket_entry[symbol] = False
                                if symbol in app.entry_order_id:
                                    del app.entry_order_id[symbol]
                                if symbol in app.entry_price:
                                    del app.entry_price[symbol]
                                if symbol in app.stop_price:
                                    del app.stop_price[symbol]
                                if symbol in app.profit_target_price:
                                    del app.profit_target_price[symbol]
                                time.sleep(2)
                                continue
                    
                    # CRITICAL: Add bracket orders at market open for pre-market positions
                    # This provides stop loss protection once regular hours begin
                    if is_regular_hours() and app.premarket_entry.get(symbol, False) and not app.brackets_added_at_open.get(symbol, False):
                        # Pre-market position without brackets - add them now!
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        print(f"\n{'='*70}")
                        print(f"[{timestamp}] 🛡️  ADDING BRACKETS AT MARKET OPEN - {symbol}")
                        print(f"{'='*70}")
                        print(f"Pre-market position detected without bracket orders")
                        print(f"Entry: ${app.entry_price.get(symbol, 0):.2f}")
                        print(f"Adding stop loss (${app.stop_price.get(symbol, 0):.2f}) and profit target (${app.profit_target_price.get(symbol, 0):.2f})...")
                        
                        # Create OCA group for brackets
                        oca_group_name = f"OCA_{symbol}_{int(time.time())}"
                        app.oca_group[symbol] = oca_group_name
                        
                        # Profit target order
                        profit_taker = Order()
                        profit_taker.action = "SELL"
                        profit_taker.orderType = "LMT"
                        profit_taker.lmtPrice = app.profit_target_price[symbol]
                        profit_taker.totalQuantity = app.position[symbol]
                        profit_taker.tif = "GTC"
                        profit_taker.transmit = True  # Must transmit to exchange
                        profit_taker.ocaGroup = oca_group_name
                        profit_taker.ocaType = 1
                        try:
                            profit_taker.eTradeOnly = False
                            profit_taker.firmQuoteOnly = False
                        except Exception:
                            pass
                        
                        # Stop loss order
                        stop_loss = Order()
                        stop_loss.action = "SELL"
                        stop_loss.orderType = "STP"
                        stop_loss.auxPrice = app.stop_price[symbol]
                        stop_loss.totalQuantity = app.position[symbol]
                        stop_loss.tif = "GTC"
                        stop_loss.transmit = True
                        stop_loss.ocaGroup = oca_group_name
                        stop_loss.ocaType = 1
                        try:
                            stop_loss.eTradeOnly = False
                            stop_loss.firmQuoteOnly = False
                        except Exception:
                            pass
                        
                        profit_id = app.nextOid()
                        stop_id = app.nextOid()
                        
                        profit_taker.orderId = profit_id
                        stop_loss.orderId = stop_id
                        
                        # Place orders
                        app.placeOrder(profit_taker.orderId, contracts[symbol], profit_taker)
                        app.placeOrder(stop_loss.orderId, contracts[symbol], stop_loss)
                        
                        # Track order IDs and mark brackets as added
                        app.profit_order_id[symbol] = profit_id
                        app.stop_order_id[symbol] = stop_id
                        app.profit_order_active[symbol] = True
                        app.stop_order_active[symbol] = True
                        app.brackets_added_at_open[symbol] = True
                        
                        print(f"✓ Bracket orders placed with OCA group: {oca_group_name}")
                        print(f"  Profit order ID: {profit_id}")
                        print(f"  Stop order ID: {stop_id}")
                        print(f"{'='*70}\n")
                        time.sleep(1)
                    
                    # Check for dynamic exit signal (Candle Under Candle) using shared strategy module
                    bars_for_check = app.bars.get(symbol, [])
                    
                    # Debug logging: Show bar data before checking
                    if len(bars_for_check) >= 2:
                        latest = bars_for_check[-1]
                        previous = bars_for_check[-2]
                        print(f"[DEBUG] {symbol} Exit Check: {len(bars_for_check)} bars | Previous low: ${previous['low']:.2f} | Latest low: ${latest['low']:.2f} | Diff: ${latest['low'] - previous['low']:.2f}")
                    
                    should_exit, exit_msg = check_dynamic_exit(bars_for_check)
                    
                    if should_exit and app.position.get(symbol, 0) > 0:
                        # CRITICAL CHECK: Only process dynamic exit if we still control the position
                        # If bracket orders already executed, skip dynamic exit
                        has_active_brackets = (app.stop_order_active.get(symbol, False) or 
                                             app.profit_order_active.get(symbol, False))
                        
                        print(f"[DEBUG] {symbol} Dynamic exit triggered! Bracket orders active: Stop={app.stop_order_active.get(symbol, False)}, Profit={app.profit_order_active.get(symbol, False)}")
                        
                        # In pre-market, no brackets exist so always allow dynamic exit
                        # In regular hours, only if brackets are still active (not filled yet)
                        if is_premarket() or has_active_brackets:
                            timestamp = datetime.now().strftime('%H:%M:%S')
                            print(f"\n{'='*70}")
                            print(f"[{timestamp}] 🔴 DYNAMIC EXIT TRIGGERED - {symbol}")
                            print(f"{'='*70}")
                            print(f"{exit_msg}")
                            
                            # In pre-market, use limit order at bid; in regular hours, use market order
                            if is_premarket():
                                print(f"Pre-market: Placing limit sell at bid...")
                                
                                # Get current bid
                                if symbol in app.bid_price:
                                    del app.bid_price[symbol]
                                app.reqMktData(1, contracts[symbol], "", False, False, [])
                                time.sleep(2)
                                app.cancelMktData(1)
                                
                                if symbol in app.bid_price and app.bid_price[symbol] is not None:
                                    exit_order = Order()
                                    exit_order.action = "SELL"
                                    exit_order.orderType = "LMT"
                                    exit_order.lmtPrice = app.bid_price[symbol]
                                    exit_order.totalQuantity = app.position[symbol]
                                    exit_order.tif = "DAY"
                                    exit_order.transmit = True  # Fully automatic execution
                                    
                                    exit_id = app.nextOid()
                                    exit_order.orderId = exit_id
                                    app.placeOrder(exit_order.orderId, contracts[symbol], exit_order)
                                    print(f"Limit sell order placed: {app.position[symbol]} shares @ ${app.bid_price[symbol]}")
                            else:
                                # Regular hours: Place dynamic exit order in SAME OCA group as brackets
                                # IBKR will automatically cancel profit/stop when this fills
                                print(f"Regular hours: Placing market exit in OCA group (auto-cancels brackets)...")
                                
                                exit_order = Order()
                                exit_order.action = "SELL"
                                exit_order.orderType = "MKT"
                                exit_order.totalQuantity = app.position[symbol]
                                exit_order.tif = "DAY"
                                exit_order.transmit = True  # Fully automatic execution
                                
                                # Join the same OCA group as profit/stop orders
                                if symbol in app.oca_group:
                                    exit_order.ocaGroup = app.oca_group[symbol]
                                    exit_order.ocaType = 1  # Cancel all remaining orders on fill
                                    print(f"  → Using OCA group: {app.oca_group[symbol]}")
                                    print(f"  → When this fills, IBKR will auto-cancel profit (${app.profit_target_price.get(symbol, 0):.2f}) and stop (${app.stop_price.get(symbol, 0):.2f})")
                                
                                exit_id = app.nextOid()
                                exit_order.orderId = exit_id
                                app.placeOrder(exit_order.orderId, contracts[symbol], exit_order)
                                print(f"Market exit order placed: {app.position[symbol]} shares @ MKT")
                            print(f"{'='*70}\n")
                        else:
                            # Bracket orders already executed - position likely already closed
                            print(f"[INFO] Dynamic exit detected for {symbol}, but bracket orders already inactive. Position likely closed by stop/profit.")
                        
                        # Update position tracking AFTER order is placed
                        app.in_position[symbol] = False
                        app.position[symbol] = 0
                        # Clean up all tracking
                        if symbol in app.entry_order_id:
                            del app.entry_order_id[symbol]
                        if symbol in app.profit_order_id:
                            del app.profit_order_id[symbol]
                        if symbol in app.stop_order_id:
                            del app.stop_order_id[symbol]
                        if symbol in app.premarket_entry:
                            app.premarket_entry[symbol] = False
                        if symbol in app.entry_price:
                            del app.entry_price[symbol]
                        if symbol in app.stop_price:
                            del app.stop_price[symbol]
                        if symbol in app.profit_target_price:
                            del app.profit_target_price[symbol]
                        time.sleep(2)
            
            # Wait 3 seconds before next check
            time.sleep(3)
            
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
