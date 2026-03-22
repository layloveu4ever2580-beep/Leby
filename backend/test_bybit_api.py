"""
Test script to verify Bybit API connectivity and data fetching.
Uses credentials from .env file.

Run: python test_bybit_api.py
"""

import os
import sys
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET,
)

passed = 0
failed = 0


def run_test(name, fn):
    global passed, failed
    try:
        result = fn()
        print(f"  PASS: {name}")
        passed += 1
        return result
    except Exception as e:
        print(f"  FAIL: {name} -> {e}")
        failed += 1
        return None


def test_server_time():
    resp = session.get_server_time()
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    ts = resp["result"]["timeSecond"]
    assert int(ts) > 0, "Invalid server timestamp"
    print(f"         Server time: {ts}")
    return resp


def test_get_tickers():
    resp = session.get_tickers(category="linear", symbol="BTCUSDT")
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    ticker_list = resp["result"]["list"]
    assert len(ticker_list) > 0, "No ticker data returned"
    last_price = float(ticker_list[0]["lastPrice"])
    assert last_price > 0, "Invalid last price"
    print(f"         BTCUSDT last price: {last_price}")
    return resp


def test_get_instruments_info():
    resp = session.get_instruments_info(category="linear", symbol="BTCUSDT")
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    instruments = resp["result"]["list"]
    assert len(instruments) > 0, "No instrument data returned"
    inst = instruments[0]
    min_qty = float(inst["lotSizeFilter"]["minOrderQty"])
    qty_step = float(inst["lotSizeFilter"]["qtyStep"])
    tick_size = float(inst["priceFilter"]["tickSize"])
    print(f"         BTCUSDT minQty={min_qty}, qtyStep={qty_step}, tickSize={tick_size}")
    return resp


def test_get_kline():
    resp = session.get_kline(category="linear", symbol="BTCUSDT", interval="60", limit=5)
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    candles = resp["result"]["list"]
    assert len(candles) > 0, "No kline data returned"
    print(f"         Got {len(candles)} candles (1h BTCUSDT)")
    return resp


def test_get_orderbook():
    resp = session.get_orderbook(category="linear", symbol="BTCUSDT")
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    bids = resp["result"]["b"]
    asks = resp["result"]["a"]
    assert len(bids) > 0, "No bids in orderbook"
    assert len(asks) > 0, "No asks in orderbook"
    print(f"         Orderbook: {len(bids)} bids, {len(asks)} asks")
    return resp


def test_wallet_balance():
    """Requires valid API key with read permissions."""
    resp = session.get_wallet_balance(accountType="UNIFIED")
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    coins = resp["result"]["list"]
    print(f"         Wallet accounts found: {len(coins)}")
    return resp


def test_get_positions():
    """Requires valid API key with read permissions."""
    resp = session.get_positions(category="linear", settleCoin="USDT")
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    positions = resp["result"]["list"]
    open_positions = [p for p in positions if float(p.get("size", 0)) > 0]
    print(f"         Open positions: {len(open_positions)}")
    return resp


def test_get_order_history():
    """Requires valid API key with read permissions."""
    resp = session.get_order_history(category="linear", limit=5)
    assert resp["retCode"] == 0, f"Unexpected retCode: {resp['retCode']}"
    orders = resp["result"]["list"]
    print(f"         Recent orders: {len(orders)}")
    return resp


if __name__ == "__main__":
    print(f"\nBybit API Test Suite")
    print(f"Testnet: {TESTNET}")
    print(f"API Key: {API_KEY[:6]}...{API_KEY[-4:]}" if len(API_KEY) > 10 else "API Key: (not set)")
    print("-" * 50)

    # Public endpoints (no auth needed)
    print("\n[Public Endpoints]")
    run_test("Server Time", test_server_time)
    run_test("Tickers (BTCUSDT)", test_get_tickers)
    run_test("Instruments Info (BTCUSDT)", test_get_instruments_info)
    run_test("Kline / Candles", test_get_kline)
    run_test("Orderbook", test_get_orderbook)

    # Authenticated endpoints
    print("\n[Authenticated Endpoints]")
    if not API_KEY or not API_SECRET:
        print("  SKIP: No API credentials set, skipping authenticated tests")
    else:
        run_test("Wallet Balance", test_wallet_balance)
        run_test("Open Positions", test_get_positions)
        run_test("Order History", test_get_order_history)

    # Summary
    print("-" * 50)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    sys.exit(1 if failed > 0 else 0)
