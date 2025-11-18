from decimal import Decimal
from ibapi.client import *
from ibapi.common import TickAttrib, TickerId
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.wrapper import *
import time, threading

# PAPER trading port
port = 7497
# change clientId if you already use clientId 0 elsewhere
clientId = 1

class TestApp(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.oid = 0
        self.last_price = None  # will hold the last traded price

    def nextValidId(self, orderId: OrderId):
        self.oid = orderId

    def nextOid(self):
        self.oid += 1
        return self.oid

    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib):
        # tickType 4 = LAST price
        if tickType == 4:
            self.last_price = price
            print(f"Last traded price for reqId {reqId}: {price}")

    def openOrder(self, orderId: OrderId, contract: Contract, order: Order, orderState: OrderState):
        print(f"openOrder. orderId: {orderId}, contract: {contract.symbol}, action: {order.action}, qty: {order.totalQuantity}, orderState: {orderState.status}")

    def orderStatus(self, orderId: TickerId, status: str, filled: Decimal, remaining: Decimal, avgFillPrice: float, permId: TickerId, parentId: TickerId, lastFillPrice: float, clientId: TickerId, whyHeld: str, mktCapPrice: float):
        print(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)

    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        print(f"reqId: {reqId}, contract: {contract}, execution: {execution}, submitter: {execution.submitter}")

    def error(self, *args):
        # IB Python API can call error with different signatures depending on version:
        #  - error(reqId, errorCode, errorString)
        #  - error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)
        try:
            if len(args) == 3:
                reqId, errorCode, errorString = args
                print(f"Error. ReqId: {reqId}, Error Code: {errorCode}, Error Message: {errorString}")
            elif len(args) >= 4:
                reqId, errorTime, errorCode, errorString = args[:4]
                adv = args[4] if len(args) > 4 else ""
                print(f"Error., Time of Error: {errorTime}, Error Code: {errorCode}, Error Message: {errorString}")
                if adv:
                    print(f"AdvancedOrderRejectJson: {adv}")
            else:
                print("Error: unexpected error() signature", args)
        except Exception as e:
            print("Exception in error handler:", e, "args:", args)


if __name__ == "__main__":
    # Stock contract for LOBO
    c = Contract()
    c.symbol = "LOBO"
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"

    app = TestApp()
    app.connect("127.0.0.1", port, clientId)
    threading.Thread(target=app.run, daemon=True).start()

    # wait briefly for nextValidId from the gateway/TWS
    timeout = 5.0
    waited = 0.0
    interval = 0.1
    while app.oid == 0 and waited < timeout:
        time.sleep(interval)
        waited += interval

    if app.oid == 0:
        print("Did not receive nextValidId from TWS/gateway. Check connection and API settings.")
        try:
            app.disconnect()
        except Exception:
            pass
        exit(1)

    # --- ENTRY LOGIC ---
    # We use a LIMIT order to enter at or below a specific price (lmtPrice).
    # This gives you control over entry price (no slippage beyond your limit).
    # The allocation (in USD) determines how much capital to deploy per trade.
    # Quantity is computed as: floor(allocation / lmtPrice).
    
    allocation = 500.0  # dollars per trade
    
    # Request market data to get the last traded price
    print(f"Requesting market data for {c.symbol}...")
    app.reqMktData(1, c, "", False, False, [])
    
    # wait for last price (up to 10 seconds)
    timeout_mkt = 10.0
    waited_mkt = 0.0
    interval_mkt = 0.2
    while app.last_price is None and waited_mkt < timeout_mkt:
        time.sleep(interval_mkt)
        waited_mkt += interval_mkt
    
    # cancel market data subscription
    app.cancelMktData(1)
    
    if app.last_price is None:
        print("Could not retrieve last traded price. Exiting.")
        try:
            app.disconnect()
        except Exception:
            pass
        exit(1)
    
    # Use last traded price as entry limit
    entry_limit_price = round(app.last_price, 2)
    print(f"Using last traded price as entry: {entry_limit_price}")
    
    # compute quantity based on allocation (floor to whole shares, minimum 1)
    try:
        qty = int(allocation // float(entry_limit_price))
    except Exception:
        qty = 1
    if qty < 1:
        qty = 1

    # --- EXIT LOGIC ---
    # We use a BRACKET order: parent entry + profit target (limit sell) + stop-loss (stop sell).
    # - Profit target: sell at a higher price (e.g., +10% above entry)
    # - Stop-loss: sell at a lower price (e.g., -5% below entry) to limit downside
    # Only ONE of the exit orders will execute; when one fills, the other is canceled (OCA behavior).
    
    profit_pct = 0.10   # 10% profit target
    stop_pct = 0.05     # 5% stop-loss
    
    profit_price = round(entry_limit_price * (1 + profit_pct), 2)
    stop_price = round(entry_limit_price * (1 - stop_pct), 2)

    # Parent order (entry)
    parent = Order()
    parent.action = "BUY"
    parent.orderType = "LMT"
    parent.lmtPrice = entry_limit_price
    parent.totalQuantity = qty
    parent.tif = "DAY"
    parent.transmit = False  # don't send until all child orders are attached
    # Explicitly disable attributes that some IB backends reject
    try:
        parent.eTradeOnly = False
        parent.firmQuoteOnly = False
    except Exception:
        pass

    # Profit target (child order 1)
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

    # Stop-loss (child order 2)
    stop_loss = Order()
    stop_loss.action = "SELL"
    stop_loss.orderType = "STP"
    stop_loss.auxPrice = stop_price  # stop trigger price
    stop_loss.totalQuantity = qty
    stop_loss.tif = "GTC"
    stop_loss.transmit = True  # send all three orders when this is placed
    try:
        stop_loss.eTradeOnly = False
        stop_loss.firmQuoteOnly = False
    except Exception:
        pass

    # assign sequential order ids for bracket: parent, profit_taker, stop_loss
    parent_id = app.nextOid()
    profit_id = app.nextOid()
    stop_id = app.nextOid()
    
    parent.orderId = parent_id
    profit_taker.orderId = profit_id
    profit_taker.parentId = parent_id
    stop_loss.orderId = stop_id
    stop_loss.parentId = parent_id
    
    notional = entry_limit_price * qty
    print(f"Bracket order preview:")
    print(f"  Entry (parent): id={parent_id}, {c.symbol}, qty={qty}, limit={entry_limit_price}, notional={notional:.2f}")
    print(f"  Profit target:  id={profit_id}, SELL @ {profit_price} (+{profit_pct*100:.1f}%)")
    print(f"  Stop-loss:      id={stop_id}, SELL @ {stop_price} (-{stop_pct*100:.1f}%)")
    
    # interactive confirmation before placing live order (type y to confirm)
    confirm = input("Place this bracket order? (y/N): ")
    if confirm.strip().lower() == 'y':
        print(f"Placing bracket order: parent={parent_id}, profit={profit_id}, stop={stop_id}")
        app.placeOrder(parent.orderId, c, parent)
        app.placeOrder(profit_taker.orderId, c, profit_taker)
        app.placeOrder(stop_loss.orderId, c, stop_loss)
    else:
        print("Order not placed (confirmation declined).")

    # keep the script alive briefly to receive callbacks
    time.sleep(5)
    try:
        app.disconnect()
    except Exception:
        pass
