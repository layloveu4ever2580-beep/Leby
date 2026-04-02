import os
import time
import logging
import threading
from flask import Flask, request, jsonify, send_from_directory
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from flask_cors import CORS
from leverage_config import LEVERAGE_CONFIG

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS: allow same-origin (no restriction needed) or explicit origins
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
if _raw_origins in ("same-origin", "*", ""):
    CORS(app)
else:
    CORS(app, origins=_raw_origins.split(","))

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
PORT = int(os.getenv("PORT", 5001))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

_session = None


def get_session():
    global _session
    if _session is None:
        _session = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
    return _session


def bybit_call(fn, *args, retries=3, **kwargs):
    """Call a Bybit API function with retry on rate limit (ErrCode 403 / 10006)."""
    for attempt in range(retries):
        try:
            result = fn(*args, **kwargs)
            # pybit raises exceptions for HTTP errors, but some rate limits
            # come back as retCode != 0 in the JSON response
            ret_code = result.get("retCode", 0) if isinstance(result, dict) else 0
            if ret_code in (10006, 403):
                wait = 2 ** attempt + 1
                logger.warning(f"Rate limited (retCode={ret_code}), retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            return result
        except Exception as e:
            err_str = str(e)
            if "rate limit" in err_str.lower() or "403" in err_str or "10006" in err_str:
                wait = 2 ** attempt + 1
                logger.warning(f"Rate limited (exception), retry {attempt+1}/{retries} in {wait}s: {err_str[:100]}")
                time.sleep(wait)
                continue
            raise
    # Final attempt — let it raise naturally
    return fn(*args, **kwargs)


# Cache symbol info to avoid repeated API calls
_symbol_cache = {}


def get_symbol_info(symbol):
    if symbol in _symbol_cache:
        return _symbol_cache[symbol]
    try:
        info = bybit_call(get_session().get_instruments_info, category="linear", symbol=symbol)
        instrument = info["result"]["list"][0]
        lot_filter = instrument["lotSizeFilter"]
        min_qty = float(lot_filter["minOrderQty"])
        qty_step = float(lot_filter["qtyStep"])
        _symbol_cache[symbol] = (min_qty, qty_step)
        return min_qty, qty_step
    except Exception:
        return 0.001, 0.001


def round_qty(qty, min_qty, qty_step):
    if qty < min_qty:
        qty = min_qty
    steps = int(qty / qty_step)
    return round(steps * qty_step, 8)


settings = {
    "targetProfit": 30.0,
    "theme": "dark",
    "timezone": "UTC",
}
trades = []

# Track TP limit orders: { symbol: { "orderId": "...", "side": "Sell", "qty": "..." } }
_tp_orders = {}


def cleanup_orphaned_tp_orders():
    """Background task: cancel TP limit orders for positions that no longer exist (SL hit)."""
    while True:
        try:
            time.sleep(30)
            if not _tp_orders:
                continue

            session = get_session()
            positions = bybit_call(session.get_positions, category="linear", settleCoin="USDT")
            if positions.get("retCode") != 0:
                continue

            position_list = positions.get("result", {}).get("list", [])
            open_symbols = {p.get("symbol") for p in position_list if float(p.get("size", 0)) > 0}

            # Find TP orders for closed positions
            symbols_to_cancel = [sym for sym in list(_tp_orders.keys()) if sym not in open_symbols]

            for sym in symbols_to_cancel:
                tp_info = _tp_orders.pop(sym, None)
                if tp_info:
                    try:
                        logger.info(f"SL hit detected for {sym}, cancelling TP limit order {tp_info['orderId']}")
                        bybit_call(session.cancel_order,
                                   category="linear", symbol=sym,
                                   orderId=tp_info["orderId"])
                        logger.info(f"TP order cancelled for {sym}")

                        # Update trade status
                        for t in trades:
                            if t["ticker"] == sym and t["status"] == "Open":
                                t["status"] = "Closed"
                    except Exception as e:
                        logger.warning(f"Failed to cancel TP order for {sym}: {e}")

        except Exception as e:
            logger.warning(f"TP cleanup error: {e}")


# Start background cleanup thread
_cleanup_thread = threading.Thread(target=cleanup_orphaned_tp_orders, daemon=True)
_cleanup_thread.start()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/test-bybit", methods=["GET"])
def test_bybit():
    """Diagnostic endpoint: test Bybit API connectivity from this server."""
    results = {}
    session = get_session()

    # Test 1: Server time (public, no auth)
    try:
        resp = session.get_server_time()
        results["server_time"] = {"status": "ok", "retCode": resp.get("retCode"), "data": resp.get("result")}
    except Exception as e:
        results["server_time"] = {"status": "error", "error": str(e)}

    # Test 2: Ticker price (public, no auth)
    try:
        resp = session.get_tickers(category="linear", symbol="BTCUSDT")
        price = resp["result"]["list"][0]["lastPrice"] if resp.get("retCode") == 0 else None
        results["ticker"] = {"status": "ok", "retCode": resp.get("retCode"), "retMsg": resp.get("retMsg"), "price": price}
    except Exception as e:
        results["ticker"] = {"status": "error", "error": str(e)}

    # Test 3: Wallet balance (authenticated)
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED")
        results["wallet"] = {"status": "ok", "retCode": resp.get("retCode"), "retMsg": resp.get("retMsg")}
        if resp.get("retCode") == 0:
            coins = resp.get("result", {}).get("list", [])
            results["wallet"]["accounts"] = len(coins)
    except Exception as e:
        results["wallet"] = {"status": "error", "error": str(e)}

    # Test 4: Positions (authenticated)
    try:
        resp = session.get_positions(category="linear", settleCoin="USDT")
        results["positions"] = {"status": "ok", "retCode": resp.get("retCode"), "retMsg": resp.get("retMsg")}
        if resp.get("retCode") == 0:
            pos_list = resp.get("result", {}).get("list", [])
            open_pos = [p for p in pos_list if float(p.get("size", 0)) > 0]
            results["positions"]["total"] = len(pos_list)
            results["positions"]["open"] = len(open_pos)
    except Exception as e:
        results["positions"] = {"status": "error", "error": str(e)}

    # Test 5: API key info
    results["config"] = {
        "api_key_prefix": BYBIT_API_KEY[:6] + "..." if len(BYBIT_API_KEY) > 6 else "(not set)",
        "testnet": BYBIT_TESTNET,
    }

    return jsonify(results), 200


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(settings), 200


@app.route("/api/settings", methods=["POST"])
def update_settings():
    global settings
    data = request.json
    if "targetProfit" in data:
        settings["targetProfit"] = float(data["targetProfit"])
    if "theme" in data:
        settings["theme"] = data["theme"]
    if "timezone" in data:
        settings["timezone"] = data["timezone"]
    return jsonify(settings), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    # Authenticate: check header OR JSON body field (TradingView can't send headers)
    if WEBHOOK_SECRET and WEBHOOK_SECRET != "your_webhook_secret_here":
        token = request.headers.get("X-Webhook-Secret", "")
        if token != WEBHOOK_SECRET:
            body = request.get_json(silent=True) or {}
            if body.get("secret") != WEBHOOK_SECRET:
                return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(silent=True)
        if data is None:
            # Try to parse raw text — "Order fills only" may send the comment as plain text
            raw = request.get_data(as_text=True).strip()
            logger.info(f"Raw webhook body: {raw[:500]}")
            if raw.startswith("{"):
                import json
                try:
                    data = json.loads(raw)
                except Exception:
                    pass
            if data is None:
                return jsonify({"status": "ignored", "reason": "not a trade signal"}), 200

        logger.info(f"Webhook received: {data}")

        if "ticker" not in data or "tp" not in data:
            logger.info(f"Ignoring non-trade alert: {data}")
            return jsonify({"status": "ignored", "reason": "not a trade signal"}), 200

        ticker = data.get("ticker")
        entry = float(data.get("limit") or data.get("entry", 0))
        tp = float(data.get("tp", 0))
        sl = float(data.get("sl", 0))
        side = str(data.get("action") or data.get("side", "Buy")).capitalize()

        if not all([ticker, tp, sl]):
            return jsonify({"error": "Missing parameters (ticker, tp, sl required)"}), 400

        # Fetch market price with retry
        ticker_info = bybit_call(get_session().get_tickers, category="linear", symbol=ticker)
        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
        logger.info(f"{ticker} last_price={last_price}, entry={entry}, tp={tp}, sl={sl}, side={side}")

        price_for_calc = last_price if last_price > 0 else entry
        tp_distance = abs(price_for_calc - tp)
        if tp_distance == 0:
            return jsonify({"error": "TP distance is zero, cannot calculate quantity"}), 400

        target_profit = settings.get("targetProfit", 100.0)
        raw_quantity = target_profit / tp_distance
        leverage = LEVERAGE_CONFIG.get(ticker, 10)

        min_qty, qty_step = get_symbol_info(ticker)
        quantity = round_qty(raw_quantity, min_qty, qty_step)
        logger.info(f"Calculated qty={quantity} (raw={raw_quantity}, min={min_qty}, step={qty_step})")

        # Set leverage (ignore errors if already set)
        try:
            bybit_call(get_session().set_leverage,
                       category="linear", symbol=ticker,
                       buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            logger.info(f"set_leverage note: {e}")

        # Round entry price to tick size
        limit_price = entry
        try:
            info = bybit_call(get_session().get_instruments_info, category="linear", symbol=ticker)
            tick_size = float(info["result"]["list"][0]["priceFilter"]["tickSize"])
            limit_price = round(round(limit_price / tick_size) * tick_size, 8)
        except Exception:
            limit_price = round(limit_price, 4)

        # 1) LIMIT order to ENTER position with SL attached
        logger.info(f"Placing LIMIT {side} entry: {ticker} qty={quantity} price={limit_price} sl={sl}")
        order = bybit_call(get_session().place_order,
                           category="linear", symbol=ticker, side=side,
                           orderType="Limit", qty=str(quantity),
                           price=str(limit_price), stopLoss=str(sl),
                           timeInForce="GTC")
        logger.info(f"Limit entry response: {order}")

        if order.get("retCode", -1) != 0:
            error_msg = order.get("retMsg", "Unknown Bybit error")
            logger.error(f"Bybit limit entry rejected: {error_msg}")
            trades.append({
                "id": "failed", "ticker": ticker, "side": side,
                "entry": limit_price, "tp": tp, "sl": sl,
                "quantity": quantity, "leverage": leverage,
                "status": "Failed", "pnl": 0.0,
                "timestamp": int(time.time() * 1000), "error": error_msg
            })
            return jsonify({"error": error_msg}), 400

        # 2) Monitor entry fill in background, then place TP limit order
        #    Can't place reduce-only TP until position exists (entry limit filled).
        entry_order_id = order["result"].get("orderId", "")
        tp_side = "Sell" if side == "Buy" else "Buy"

        def _place_tp_after_fill(order_id, sym, tp_s, qty, take_profit):
            """Poll entry order until filled, then place reduce-only TP limit."""
            session = get_session()
            poll_interval = 3  # seconds

            while True:
                time.sleep(poll_interval)
                try:
                    resp = bybit_call(session.get_open_orders,
                                      category="linear", symbol=sym,
                                      orderId=order_id)
                    order_list = resp.get("result", {}).get("list", [])

                    if not order_list:
                        # Order no longer open — filled or cancelled
                        # Check if position actually exists
                        pos = bybit_call(session.get_positions,
                                         category="linear", symbol=sym)
                        pos_list = pos.get("result", {}).get("list", [])
                        has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)

                        if has_position:
                            logger.info(f"Entry filled for {sym}, placing TP limit {tp_s} @ {take_profit}")
                            tp_ord = bybit_call(session.place_order,
                                                category="linear", symbol=sym, side=tp_s,
                                                orderType="Limit", qty=str(qty),
                                                price=str(take_profit), reduceOnly=True,
                                                timeInForce="GTC")
                            logger.info(f"TP limit response: {tp_ord}")
                            if tp_ord.get("retCode") == 0:
                                _tp_orders[sym] = {
                                    "orderId": tp_ord["result"].get("orderId", ""),
                                    "side": tp_s, "qty": str(qty)
                                }
                            else:
                                # Fallback to trading stop if limit fails
                                logger.warning(f"TP limit failed: {tp_ord.get('retMsg')}, using trading stop")
                                bybit_call(session.set_trading_stop,
                                           category="linear", symbol=sym,
                                           takeProfit=str(take_profit), positionIdx=0)
                        else:
                            logger.info(f"Entry order {order_id} for {sym} was cancelled/rejected, no TP needed")
                            for t in trades:
                                if t["id"] == order_id and t["status"] == "Open":
                                    t["status"] = "Cancelled"
                        return

                    status = order_list[0].get("orderStatus", "")
                    if status in ("Cancelled", "Rejected", "Deactivated"):
                        logger.info(f"Entry {order_id} for {sym} status={status}, no TP needed")
                        for t in trades:
                            if t["id"] == order_id and t["status"] == "Open":
                                t["status"] = "Cancelled"
                        return

                except Exception as e:
                    logger.warning(f"Error polling entry order {order_id}: {e}")

        threading.Thread(
            target=_place_tp_after_fill,
            args=(entry_order_id, ticker, tp_side, quantity, tp),
            daemon=True
        ).start()

        trade_record = {
            "id": order["result"].get("orderId", "unknown"),
            "ticker": ticker, "side": side, "entry": limit_price,
            "tp": tp, "sl": sl, "quantity": quantity,
            "leverage": leverage, "status": "Open", "pnl": 0.0,
            "entryType": "limit",
            "timestamp": int(time.time() * 1000)
        }
        trades.append(trade_record)
        return jsonify({"status": "success", "order": order, "entryType": "limit"}), 200

    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades", methods=["GET"])
def get_trades():
    return jsonify(trades), 200


@app.route("/api/trades/<trade_id>/target-profit", methods=["PATCH"])
def update_trade_tp(trade_id):
    data = request.json
    new_tp = float(data.get("targetProfit", 0))
    for t in trades:
        if t["id"] == trade_id:
            try:
                bybit_call(get_session().set_trading_stop,
                           category="linear", symbol=t["ticker"],
                           takeProfit=str(new_tp), positionIdx=0)
                t["tp"] = new_tp
                return jsonify(t), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Trade not found"}), 404


@app.route("/api/sync-trades", methods=["POST"])
def sync_trades():
    try:
        positions = bybit_call(get_session().get_positions,
                               category="linear", settleCoin="USDT")

        if positions.get("retCode", -1) != 0:
            error_msg = positions.get("retMsg", "Bybit API error")
            logger.error(f"sync_trades Bybit error: {error_msg}")
            return jsonify({"error": error_msg}), 502

        position_list = positions.get("result", {}).get("list", [])

        for pos in position_list:
            size = float(pos.get("size", 0))
            if size == 0:
                continue

            symbol = pos.get("symbol", "")
            unrealised_pnl = float(pos.get("unrealisedPnl", 0))

            matched = False
            for t in trades:
                if t["ticker"] == symbol and t["status"] == "Open":
                    t["pnl"] = unrealised_pnl
                    matched = True

            if not matched:
                trades.append({
                    "id": f"synced-{symbol}-{int(time.time())}",
                    "ticker": symbol,
                    "side": pos.get("side", "Buy"),
                    "entry": float(pos.get("avgPrice", 0)),
                    "tp": float(pos.get("takeProfit", 0)),
                    "sl": float(pos.get("stopLoss", 0)),
                    "quantity": size,
                    "leverage": int(float(pos.get("leverage", 1))),
                    "status": "Open",
                    "pnl": unrealised_pnl,
                    "timestamp": int(float(pos.get("createdTime", time.time() * 1000)))
                })

        open_symbols = {pos.get("symbol") for pos in position_list if float(pos.get("size", 0)) > 0}
        for t in trades:
            if t["status"] == "Open" and t["ticker"] not in open_symbols:
                t["status"] = "Closed"

        logger.info(f"Synced {len(position_list)} positions from Bybit")
        return jsonify({"status": "synced", "positions": len(position_list)}), 200
    except Exception as e:
        logger.exception(f"sync_trades error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Serve React frontend ─────────────────────────────────────────────────────
DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    if path and os.path.exists(os.path.join(DIST_DIR, path)):
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
