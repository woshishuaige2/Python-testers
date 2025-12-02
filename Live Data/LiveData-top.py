from decimal import Decimal
import threading
import time
from ibapi.client import *
from ibapi.common import TickAttrib, TickerId
from ibapi.wrapper import *
from ibapi.ticktype import TickTypeEnum


port = 7497


def tick_type_str(tickType):
    """Return a human-friendly string for tickType across ibapi versions."""
    try:
        # older ibapi had TickTypeEnum.toStr
        if hasattr(TickTypeEnum, 'toStr'):
            return TickTypeEnum.toStr(tickType)
        # some versions may have to_str
        if hasattr(TickTypeEnum, 'to_str'):
            return TickTypeEnum.to_str(tickType)
        # if tickType is an Enum instance
        if hasattr(tickType, 'name'):
            return tickType.name
        # try constructing the enum from int
        try:
            return TickTypeEnum(tickType).name
        except Exception:
            return str(tickType)
    except Exception:
        return str(tickType)

class TestApp(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        # Map reqId -> symbol and store latest market values for a clean printout
        self.reqid_symbol = {}
        self.market_data = {}

    def nextValidId(self, orderId: OrderId):
        contract = Contract()
        contract.symbol = 'TSLA'
        contract.secType = 'STK'
        contract.exchange = 'SMART'
        contract.currency = 'USD'
        self.reqContractDetails(1, contract)

        # If live data is not available, request delayed data.
        self.reqMarketDataType(3)

        self.reqMktData(
            reqId=orderId,
            contract=contract,
            genericTickList="",
            snapshot=False,
            regulatorySnapshot=False,
            mktDataOptions= []
        )
        # remember symbol for this request id and initialize market state
        self.reqid_symbol[orderId] = contract.symbol
        self.market_data[orderId] = {
            'last_price': None,
            'ask': None,
            'bid': None,
            'volume': None,
            'vwap': None,
        }
        
    def tickOptionComputation(self, reqId: TickerId, tickType: TickType, tickAttrib: int, impliedVol: float, delta: float, optPrice: float, pvDividend: float, gamma: float, vega: float, theta: float, undPrice: float):
        print(f"tickOptionComputation. reqId: {reqId}, tickType: {tick_type_str(tickType)}, tickAttrib: {tickAttrib}, ImpVol: {impliedVol}, delta: {delta}, optPrice: {optPrice}, pvDividend: {pvDividend}, gamma: {gamma}, vega: {vega}, theta: {theta}, undPrice: {undPrice}")
    
    def tickPrice(self, reqId: TickerId, tickType: TickerId, price: float, attrib: TickAttrib):
        # Update market_data for nicer single-line output
        tt = tick_type_str(tickType)
        md = self.market_data.get(reqId)
        if md is not None:
            if tt == 'LAST':
                md['last_price'] = price
            elif tt == 'ASK':
                md['ask'] = price
            elif tt == 'BID':
                md['bid'] = price
        # print a concise row showing the important fields
        self.print_market_row(reqId)

    def tickReqParams(self, tickerId: TickerId, minTick: float, bboExchange: str, snapshotPermissions: TickerId):
        print(tickerId, minTick, bboExchange, snapshotPermissions)

    def rerouteMktDataReq(self, reqId: TickerId, conId: TickerId, exchange: str):
        print("Reroute CFD data:", conId, exchange)

    def tickSize(self, reqId: TickerId, tickType: TickType, size: Decimal):
        tt = tick_type_str(tickType)
        md = self.market_data.get(reqId)
        if md is not None:
            # some versions send volume via tickSize as 'VOLUME' or 'LAST_SIZE'
            if tt in ('VOLUME', 'LAST_SIZE'):
                try:
                    md['volume'] = int(size)
                except Exception:
                    md['volume'] = size
        self.print_market_row(reqId)

    def tickGeneric(self, reqId: TickerId, tickType: TickType, value: float):
        tt = tick_type_str(tickType)
        md = self.market_data.get(reqId)
        if md is not None:
            # VWAP is often sent as a generic tick
            if tt == 'VWAP':
                md['vwap'] = float(value)
        self.print_market_row(reqId)

    def tickString(self, reqId: TickerId, tickType: TickType, value: str):
        print("tickString: ", reqId, tick_type_str(tickType), value)
        
    def tickNews(self, tickerId: int, timeStamp: int, providerCode: str, articleId: str, headline: str, extraData: str):
        print("tickNews",tickerId, timeStamp, providerCode, articleId, headline, extraData)

    def tickSnapshotEnd(self, reqId: int):
        print(f"tickSnapshotEnd. reqId:{reqId}")

    def error(self, reqId, *args):
        """Flexible error handler compatible with multiple ibapi versions.

        Minimally accepts either (reqId, errorCode, errorString)
        or (reqId, errorTime, errorCode, errorString, advancedOrderRejectJson).
        """
        errorTime = None
        errorCode = None
        errorString = ''
        advancedOrderRejectJson = ''

        # Map common argument shapes without changing other code
        if len(args) == 2:
            errorCode, errorString = args
        elif len(args) == 3:
            errorCode, errorString, advancedOrderRejectJson = args
        elif len(args) == 4:
            errorTime, errorCode, errorString, advancedOrderRejectJson = args
        else:
            # Best-effort fallback
            try:
                if args:
                    errorCode = args[0]
                    errorString = args[1] if len(args) > 1 else ''
            except Exception:
                errorString = ' '.join(map(str, args))

        print(f"Error. Time of Error: {errorTime}, Error Code: {errorCode}, Error Message: {errorString}")
        if advancedOrderRejectJson:
            print(f"AdvancedOrderRejectJson: {advancedOrderRejectJson}")
    
    def print_market_row(self, reqId: int):
        """Print a compact, human-friendly market-data row for the given reqId."""
        symbol = self.reqid_symbol.get(reqId, f"req{reqId}")
        md = self.market_data.get(reqId, {})
        def fmt(v):
            return str(v) if v is not None else 'N/A'

        last = fmt(md.get('last_price'))
        bid = fmt(md.get('bid'))
        ask = fmt(md.get('ask'))
        vol = fmt(md.get('volume'))
        vwap = fmt(md.get('vwap'))

        print(f"{symbol} | Last: {last} | Bid: {bid} | Ask: {ask} | Vol: {vol} | VWAP: {vwap}")
        

def main():
    app = TestApp()
    app.connect("127.0.0.1", port, 0)

    # Run the ibapi message loop in a background thread so the main thread
    # can catch KeyboardInterrupt (Ctrl+C) and call disconnect().
    def run_loop():
        try:
            app.run()
        except Exception as e:
            print("App run loop exited with:", e)

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    try:
        # Keep the main thread alive while background thread runs.
        while t.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("KeyboardInterrupt received — disconnecting from IB...")
        try:
            app.disconnect()
        except Exception:
            pass
        # give run loop a moment to finish
        t.join(timeout=2)
    except Exception as e:
        print(f"Unhandled exception: {e}")
        try:
            app.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    main()