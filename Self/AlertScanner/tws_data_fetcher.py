"""
TWS Data Fetcher for Alert Scanner
Fetches both historical and real-time data from IBKR TWS API.
"""
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import TickerId, TickAttrib, BarData
from ibapi.ticktype import TickTypeEnum
from datetime import datetime, timedelta
from typing import List, Dict, Callable, Optional
from collections import deque
import threading
import time


def tick_type_str(tickType):
    """Return a human-friendly string for tickType across ibapi versions."""
    try:
        if hasattr(TickTypeEnum, 'toStr'):
            return TickTypeEnum.toStr(tickType)
        if hasattr(TickTypeEnum, 'to_str'):
            return TickTypeEnum.to_str(tickType)
        if hasattr(tickType, 'name'):
            return tickType.name
        try:
            return TickTypeEnum(tickType).name
        except Exception:
            return str(tickType)
    except Exception:
        return str(tickType)


class TWSDataApp(EClient, EWrapper):
    """
    TWS Application for fetching historical and real-time market data.
    Enhanced for alert scanner with VWAP calculation.
    """
    
    def __init__(self):
        EClient.__init__(self, self)
        self.next_order_id = None
        self.req_id_counter = 2000
        self.connected = False
        self.lock = threading.Lock()
        
        # Historical data storage
        self.historical_data = {}  # reqId -> list of bars
        self.historical_complete = {}  # reqId -> bool
        
        # Real-time data storage
        self.realtime_callbacks = {}  # reqId -> (symbol, callback)
        self.realtime_data = {}  # symbol -> {price, bid, ask, last_size, bid_size, ask_size, volume, vwap}
        self.contracts = {}  # symbol -> Contract
        
    def nextValidId(self, orderId: int):
        """Called when connection is established"""
        self.next_order_id = orderId
        self.connected = True
        print(f"[TWS] Connected. Next valid order ID: {orderId}")
        
    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson=""):
        """Error handler"""
        # Suppress common info messages
        if errorCode in [2104, 2106, 2158]:  # Market data farm connection messages
            return
        if errorCode == 10167:  # Displaying delayed market data
            print(f"[TWS] Using delayed market data (live subscription may be needed)")
            return
        print(f"[TWS Error] ReqId: {reqId}, Code: {errorCode}, Msg: {errorString}")
        
    def historicalData(self, reqId: int, bar: BarData):
        """Receive historical bar data"""
        with self.lock:
            if reqId not in self.historical_data:
                self.historical_data[reqId] = []
            self.historical_data[reqId].append({
                'date': bar.date,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'average': bar.average,  # VWAP
                'barCount': bar.barCount
            })
    
    def historicalDataEnd(self, reqId: int, start: str, end: str):
        """Called when historical data is complete"""
        with self.lock:
            self.historical_complete[reqId] = True
        print(f"[TWS] Historical data complete for reqId {reqId} ({start} to {end})")
    
    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib: TickAttrib):
        """Handle price ticks"""
        with self.lock:
            if reqId not in self.realtime_callbacks:
                return
            symbol, callback = self.realtime_callbacks[reqId]
            
            if symbol not in self.realtime_data:
                self.realtime_data[symbol] = {
                    'price': 0.0, 'bid': 0.0, 'ask': 0.0,
                    'last_size': 0, 'bid_size': 0, 'ask_size': 0,
                    'volume': 0, 'vwap': 0.0
                }
            
        tt = tick_type_str(tickType)
        
        if tt == 'LAST':
            with self.lock:
                self.realtime_data[symbol]['price'] = price
        elif tt == 'BID':
            with self.lock:
                self.realtime_data[symbol]['bid'] = price
        elif tt == 'ASK':
            with self.lock:
                self.realtime_data[symbol]['ask'] = price
        elif tt == 'OPEN':
            pass  # Can store if needed
    
    def tickSize(self, reqId: TickerId, tickType: int, size: int):
        """Handle size ticks"""
        with self.lock:
            if reqId not in self.realtime_callbacks:
                return
            symbol, callback = self.realtime_callbacks[reqId]
            
            if symbol not in self.realtime_data:
                self.realtime_data[symbol] = {
                    'price': 0.0, 'bid': 0.0, 'ask': 0.0,
                    'last_size': 0, 'bid_size': 0, 'ask_size': 0,
                    'volume': 0, 'vwap': 0.0
                }
        
        tt = tick_type_str(tickType)
        
        if tt == 'LAST_SIZE':
            with self.lock:
                self.realtime_data[symbol]['last_size'] = size
        elif tt == 'BID_SIZE':
            with self.lock:
                self.realtime_data[symbol]['bid_size'] = size
        elif tt == 'ASK_SIZE':
            with self.lock:
                self.realtime_data[symbol]['ask_size'] = size
        elif tt == 'VOLUME':
            with self.lock:
                old_volume = self.realtime_data[symbol]['volume']
                self.realtime_data[symbol]['volume'] = size
                
                # Trigger callback when we have price and volume update
                price = self.realtime_data[symbol]['price']
                if price > 0:
                    # Calculate simple VWAP approximation
                    # (in production, you'd track cumulative price*volume)
                    vwap = self.realtime_data[symbol].get('vwap', price)
                    if vwap == 0:
                        vwap = price
                    else:
                        # Weighted update: 90% old VWAP, 10% new price
                        vwap = vwap * 0.9 + price * 0.1
                    
                    self.realtime_data[symbol]['vwap'] = vwap
                    
                    # Call callback with updated data
                    callback(symbol, price, size, vwap, datetime.now())
    
    def get_next_req_id(self):
        """Get next request ID"""
        with self.lock:
            req_id = self.req_id_counter
            self.req_id_counter += 1
        return req_id
    
    def fetch_historical_bars(
        self,
        symbol: str,
        end_date: datetime,
        duration: str = "1 D",
        bar_size: str = "10 secs",
        what_to_show: str = "TRADES"
    ) -> List[Dict]:
        """
        Fetch historical bar data from TWS.
        
        Args:
            symbol: Stock symbol
            end_date: End date for historical data
            duration: Duration string (e.g., "1 D", "1 W", "1 M")
            bar_size: Bar size (e.g., "1 min", "5 mins", "10 secs")
            what_to_show: Data type ("TRADES", "MIDPOINT", "BID", "ASK")
        
        Returns:
            List of bar dictionaries with keys: date, open, high, low, close, volume, average (VWAP)
        """
        # Create contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        
        # Get request ID
        req_id = self.get_next_req_id()
        
        # Initialize storage
        with self.lock:
            self.historical_data[req_id] = []
            self.historical_complete[req_id] = False
        
        # Format end date
        end_date_str = end_date.strftime("%Y%m%d %H:%M:%S")
        
        # Request historical data
        self.reqHistoricalData(
            reqId=req_id,
            contract=contract,
            endDateTime=end_date_str,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=1,  # Only regular trading hours
            formatDate=1,  # Date format as string
            keepUpToDate=False,
            chartOptions=[]
        )
        
        # Wait for data to complete
        timeout = 30.0
        waited = 0.0
        interval = 0.1
        while waited < timeout:
            with self.lock:
                if self.historical_complete.get(req_id, False):
                    break
            time.sleep(interval)
            waited += interval
        
        # Get the data
        with self.lock:
            bars = self.historical_data.get(req_id, [])
            # Clean up
            if req_id in self.historical_data:
                del self.historical_data[req_id]
            if req_id in self.historical_complete:
                del self.historical_complete[req_id]
        
        return bars
    
    def subscribe_realtime_data(self, symbol: str, callback: Callable):
        """
        Subscribe to real-time market data.
        
        Args:
            symbol: Stock symbol
            callback: Function with signature (symbol, price, volume, vwap, timestamp)
        """
        # Create contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        
        with self.lock:
            self.contracts[symbol] = contract
        
        # Get request ID
        req_id = self.get_next_req_id()
        
        with self.lock:
            self.realtime_callbacks[req_id] = (symbol, callback)
            self.realtime_data[symbol] = {
                'price': 0.0, 'bid': 0.0, 'ask': 0.0,
                'last_size': 0, 'bid_size': 0, 'ask_size': 0,
                'volume': 0, 'vwap': 0.0
            }
        
        # Request market data
        # Use market data type 1 for live data (requires subscription)
        # Use market data type 3 for delayed data (free)
        self.reqMarketDataType(1)  # Live data
        self.reqMktData(
            reqId=req_id,
            contract=contract,
            genericTickList="",
            snapshot=False,
            regulatorySnapshot=False,
            mktDataOptions=[]
        )
        
        print(f"[TWS] Subscribed to real-time data for {symbol} (reqId: {req_id})")
    
    def unsubscribe_realtime_data(self, symbol: str):
        """Unsubscribe from real-time market data"""
        # Find reqId for this symbol
        req_id_to_cancel = None
        with self.lock:
            for req_id, (sym, _) in self.realtime_callbacks.items():
                if sym == symbol:
                    req_id_to_cancel = req_id
                    break
        
        if req_id_to_cancel:
            self.cancelMktData(req_id_to_cancel)
            with self.lock:
                if req_id_to_cancel in self.realtime_callbacks:
                    del self.realtime_callbacks[req_id_to_cancel]
                if symbol in self.realtime_data:
                    del self.realtime_data[symbol]
            print(f"[TWS] Unsubscribed from {symbol}")


def create_tws_data_app(host="127.0.0.1", port=7497, client_id=0) -> Optional[TWSDataApp]:
    """
    Create and connect a TWS data application.
    
    Args:
        host: TWS/IB Gateway host (default: localhost)
        port: TWS port - 7497 for paper trading, 7496 for live trading
        client_id: Unique client ID
    
    Returns:
        Connected TWSDataApp instance or None if connection failed
    """
    app = TWSDataApp()
    
    try:
        app.connect(host, port, client_id)
    except Exception as e:
        print(f"[TWS] Connection error: {e}")
        return None
    
    # Start the socket loop in a background thread
    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()
    
    # Wait for connection
    timeout = 10.0
    waited = 0.0
    interval = 0.1
    while not app.connected and waited < timeout:
        time.sleep(interval)
        waited += interval
    
    if not app.connected:
        print("[TWS] Failed to connect to TWS/IB Gateway")
        print("[TWS] Make sure TWS or IB Gateway is running with:")
        print("      - API enabled in settings")
        print("      - Socket port matches (7497 for paper, 7496 for live)")
        print("      - 'Read-Only API' unchecked")
        return None
    
    return app


# Test code
if __name__ == "__main__":
    print("\n" + "="*70)
    print(" "*20 + "TWS DATA FETCHER TEST")
    print("="*70 + "\n")
    
    # Connect to TWS (paper trading)
    print("[TEST] Connecting to TWS paper trading account (port 7497)...")
    app = create_tws_data_app(host="127.0.0.1", port=7497, client_id=900)
    
    if not app:
        print("\n[FAIL] Could not connect to TWS. Exiting.")
        exit(1)
    
    print("[OK] Connected!\n")
    
    # Test 1: Fetch historical data
    print("+-- TEST 1: Historical Data")
    print("|   Fetching 1 day of 10-second bars for AAPL...")
    
    end_date = datetime.now()
    bars = app.fetch_historical_bars(
        symbol="AAPL",
        end_date=end_date,
        duration="1 D",
        bar_size="10 secs"
    )
    
    if bars:
        print(f"|   [OK] Received {len(bars)} bars")
        print("|   Sample (first 3 bars):")
        for i, bar in enumerate(bars[:3], 1):
            print(f"|     [{i}] {bar['date']} - Close: ${bar['close']:.2f}, Vol: {bar['volume']}, VWAP: ${bar['average']:.2f}")
    else:
        print("|   [WARN] No historical data received")
    print("+" + "-"*68 + "\n")
    
    # Test 2: Real-time data
    print("+-- TEST 2: Real-Time Data")
    print("|   Subscribing to AAPL real-time data for 10 seconds...")
    
    tick_count = [0]
    
    def test_callback(symbol, price, volume, vwap, timestamp):
        tick_count[0] += 1
        if tick_count[0] <= 5:  # Print first 5 ticks
            time_str = timestamp.strftime("%H:%M:%S.%f")[:-3]
            print(f"|   [{time_str}] {symbol}: ${price:.2f} | Vol: {volume:,} | VWAP: ${vwap:.2f}")
    
    app.subscribe_realtime_data("AAPL", test_callback)
    
    time.sleep(10)
    
    print(f"|   [OK] Received {tick_count[0]} ticks in 10 seconds")
    print("+" + "-"*68 + "\n")
    
    # Cleanup
    app.disconnect()
    print("="*70)
    print(" "*24 + "TEST COMPLETE")
    print("="*70 + "\n")
