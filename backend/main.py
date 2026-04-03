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
    "targetProfit": 40.0,
    "theme": "dark",
    "timezone": "UTC",
}
trades = []

# Track TP limit orders: { symbol: { "orderId": "...", "side": "Sell", "qty": "..." } }
_tp_orders = {}

# Track pending entry orders that need TP/SL set after fill
# { orderId: { "symbol": "...", "side": "Buy", "tp": 123.4, "sl": 100.0, "tp_side": "Sell", "qty": "..." } }
_pending_entries = {}


def background_monitor():
    """Single background loop that handles:
    1. Setting TP limit + SL on positions after entry limit fills
    2. Cancelling orphaned TP orders when SL hits (position closed)
    """
    while True:
        try:
            time.sleep(5)
            session = get_session()

            # ── PART 1: Check pending entries for fills ──
            if _pending_entries:
                pending_copy = dict(_pending_entries)
                for order_id, info in pending_copy.items():
                    sym = info["symbol"]
                    try:
                        # Check if entry order is still open
                        resp = bybit_call(session.get_open_orders,
                                          category="linear", symbol=sym,
                                          orderId=order_id)
                        order_list = resp.get("result", {}).get("list", [])

                        if order_list:
                            status = order_list[0].get("orderStatus", "")
                            if status in ("Cancelled", "Rejected", "Deactivated"):
                                logger.info(f"[monitor] Entry {order_id} for {sym} was {status}")
                                _pending_entries.pop(order_id, None)
                                for t in trades:
                                    if t["id"] == order_id and t["status"] == "Open":
                                        t["status"] = "Cancelled"
                            continue  # Still open, check next time

                        # Order gone from open list — check position
                        pos = bybit_call(session.get_positions,
                                         category="linear", symbol=sym)
                        pos_list = pos.get("result", {}).get("list", [])
                        position_size = 0.0
                        for p in pos_list:
                            s = float(p.get("size", 0))
                            if s > 0:
                                position_size = s
                                break

                        if position_size <= 0:
                            logger.info(f"[monitor] No position for {sym}, entry cancelled")
                            _pending_entries.pop(order_id, None)
                            for t in trades:
                                if t["id"] == order_id and t["status"] == "Open":
                                    t["status"] = "Cancelled"
                            continue

                        # Position exists — set SL and place TP limit
                        logger.info(f"[monitor] Entry filled for {sym}, size={position_size}")
                        _pending_entries.pop(order_id, None)

                        # Set SL
                        try:
                            sl_resp = bybit_call(session.set_trading_stop,
                                                 category="linear", symbol=sym,
                                                 stopLoss=str(info["sl"]), positionIdx=0)
                            logger.info(f"[monitor] SL set for {sym}: {sl_resp.get('retCode')} {sl_resp.get('retMsg')}")
                        except Exception as e:
                            logger.error(f"[monitor] SL failed for {sym}: {e}")

                        # Place TP reduce-only limit
                        tp_qty = str(position_size)
                        try:
                            tp_ord = bybit_call(session.place_order,
                                                category="linear", symbol=sym,
                                                side=info["tp_side"],
                                                orderType="Limit", qty=tp_qty,
                                                price=str(info["tp"]),
                                                reduceOnly=True, timeInForce="GTC")
                            logger.info(f"[monitor] TP limit for {sym}: {tp_ord.get('retCode')} {tp_ord.get('retMsg')}")
                            if tp_ord.get("retCode") == 0:
                                _tp_orders[sym] = {
                                    "orderId": tp_ord["result"].get("orderId", ""),
                                    "side": info["tp_side"], "qty": tp_qty
                                }
                            else:
                                # Fallback: set TP via trading stop
                                logger.warning(f"[monitor] TP limit failed, using trading stop for {sym}")
                                bybit_call(session.set_trading_stop,
                                           category="linear", symbol=sym,
                                           takeProfit=str(info["tp"]), positionIdx=0)
                        except Exception as e:
                            logger.error(f"[monitor] TP order error for {sym}: {e}")
                            try:
                                bybit_call(session.set_trading_stop,
                                           category="linear", symbol=sym,
                                           takeProfit=str(info["tp"]), positionIdx=0)
                            except Exception:
                                pass

                    except Exception as e:
                        logger.error(f"[monitor] Error checking entry {order_id}: {e}")

            # ── PART 2: Cancel orphaned TP orders (SL hit) ──
            if _tp_orders:
                try:
                    positions = bybit_call(session.get_positions,
                                           category="linear", settleCoin="USDT")
                    if positions.get("retCode") == 0:
                        position_list = positions.get("result", {}).get("list", [])
                        open_symbols = {p.get("symbol") for p in position_list
                                        if float(p.get("size", 0)) > 0}

                        for sym in [s for s in list(_tp_orders.keys()) if s not in open_symbols]:
                            tp_info = _tp_orders.pop(sym, None)
                            if tp_info:
                                try:
                                    logger.info(f"[monitor] SL hit for {sym}, cancelling TP {tp_info['orderId']}")
                                    bybit_call(session.cancel_order,
                                               category="linear", symbol=sym,
                                               orderId=tp_info["orderId"])
                                    for t in trades:
                                        if t["ticker"] == sym and t["status"] == "Open":
                                            t["status"] = "Closed"
                                except Exception as e:
                                    logger.warning(f"[monitor] Cancel TP failed for {sym}: {e}")
                except Exception as e:
                    logger.warning(f"[monitor] Orphan cleanup error: {e}")

        except Exception as e:
            logger.warning(f"[monitor] Loop error: {e}")


# Start single background monitor thread
_monitor_thread = threading.Thread(target=background_monitor, daemon=True)
_monitor_thread.start()


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

        # 1) LIMIT order to ENTER position
        logger.info(f"Placing LIMIT {side} entry: {ticker} qty={quantity} price={limit_price} sl={sl}")
        order = bybit_call(get_session().place_order,
                           category="linear", symbol=ticker, side=side,
                           orderType="Limit", qty=str(quantity),
                           price=str(limit_price),
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

        # 2) Track this entry so the background monitor sets TP/SL after fill
        entry_order_id = order["result"].get("orderId", "")
        tp_side = "Sell" if side == "Buy" else "Buy"

        # Round TP price to tick size
        tp_price = tp
        try:
            info = bybit_call(get_session().get_instruments_info, category="linear", symbol=ticker)
            tick_size = float(info["result"]["list"][0]["priceFilter"]["tickSize"])
            tp_price = round(round(tp / tick_size) * tick_size, 8)
        except Exception:
            tp_price = round(tp, 4)

        _pending_entries[entry_order_id] = {
            "symbol": ticker,
            "side": side,
            "tp": tp_price,
            "sl": sl,
            "tp_side": tp_side,
            "qty": str(quantity),
        }
        logger.info(f"Entry {entry_order_id} added to pending monitor for {ticker}")

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
        session = get_session()

        # Fetch all positions with pagination support
        all_positions = []
        cursor = ""
        while True:
            params = {"category": "linear", "settleCoin": "USDT", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            positions = bybit_call(session.get_positions, **params)

            if positions.get("retCode", -1) != 0:
                error_msg = positions.get("retMsg", "Bybit API error")
                logger.error(f"sync_trades Bybit error: {error_msg}")
                return jsonify({"error": error_msg}), 502

            position_list = positions.get("result", {}).get("list", [])
            all_positions.extend(position_list)

            cursor = positions.get("result", {}).get("nextPageCursor", "")
            if not cursor:
                break

        for pos in all_positions:
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
                created = pos.get("createdTime", "")
                try:
                    ts = int(float(created)) if created else int(time.time() * 1000)
                except (ValueError, TypeError):
                    ts = int(time.time() * 1000)

                trades.append({
                    "id": f"synced-{symbol}-{int(time.time())}",
                    "ticker": symbol,
                    "side": pos.get("side", "Buy"),
                    "entry": float(pos.get("avgPrice", 0)),
                    "tp": float(pos.get("takeProfit", 0) or 0),
                    "sl": float(pos.get("stopLoss", 0) or 0),
                    "quantity": size,
                    "leverage": int(float(pos.get("leverage", 1) or 1)),
                    "status": "Open",
                    "pnl": unrealised_pnl,
                    "timestamp": ts
                })

        open_symbols = {pos.get("symbol") for pos in all_positions if float(pos.get("size", 0)) > 0}
        for t in trades:
            if t["status"] == "Open" and t["ticker"] not in open_symbols:
                t["status"] = "Closed"

        logger.info(f"Synced {len(all_positions)} positions from Bybit")
        return jsonify({"status": "synced", "positions": len(all_positions)}), 200
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
