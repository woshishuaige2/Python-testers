from decimal import Decimal
from ibapi.client import *
from ibapi.common import TickAttrib, TickerId
from ibapi.contract import Contract, ContractDetails, ComboLeg
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.ticktype import TickType
from ibapi.wrapper import *
import time, threading
port=7497

class TestApp(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.oid = 0

    def nextValidId(self, orderId: OrderId):
        self.oid = orderId

    def nextOid(self):
        self.oid += 1
        return self.oid


    def openOrder(self, orderId: OrderId, contract: Contract, order: Order, orderState: OrderState):
        print(f"openOrder. orderId: {orderId}, contract: {contract}, order: {order}, orderState: {orderState.status}, submitter: {order.submitter}") 

    def orderStatus(self, orderId: TickerId, status: str, filled: Decimal, remaining: Decimal, avgFillPrice: float, permId: TickerId, parentId: TickerId, lastFillPrice: float, clientId: TickerId, whyHeld: str, mktCapPrice: float):
        print(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)

    # def completedOrder(self, contract: Contract, order: Order, orderState: OrderState):
    #     print(f"CompletedOrder. submitter: {order.submitter}")

    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        print(f"reqId: {reqId}, contract: {contract}, execution: {execution}, submitter: {execution.submitter}")

    def error(self, reqId: TickerId, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson=""):
        print(f"Error., Time of Error: {errorTime}, Error Code: {errorCode}, Error Message: {errorString}")
        if advancedOrderRejectJson != "":
            print(f"AdvancedOrderRejectJson: {advancedOrderRejectJson}")
        
if __name__ == "__main__":
    c= Contract()
    c.symbol = "RSM"
    c.secType = "OPT"
    c.exchange = "FORECASTX"
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = "202504"
    c.strike = -0.5
    c.right = "C"
    
    o = Order()
    o.action = "BUY"
    o.totalQuantity = 10
    o.orderType = "LMT"
    o.lmtPrice = 0.18

    app = TestApp()
    app.connect("127.0.0.1", port, 0)
    # start the socket loop in a separate thread
    threading.Thread(target=app.run, daemon=True).start()

    # wait briefly for nextValidId from the gateway/TWS
    timeout = 5.0
    waited = 0.0
    interval = 0.1
    while app.oid == 0 and waited < timeout:
        time.sleep(interval)
        waited += interval

    # get a unique order id from the app
    order_id = app.nextOid()

    # place the first order
    app.placeOrder(order_id, c, o)

    time.sleep(3)
    # modify the limit price and resend using the same order id to replace the order
    o.lmtPrice = 0.20
    app.placeOrder(order_id, c, o)