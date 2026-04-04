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
    """Call a Bybit API function with retry on rate limit."""
    for attempt in range(retries):
        try:
            result = fn(*args, **kwargs)
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
                logger.warning(f"Rate limited, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            raise
    return fn(*args, **kwargs)


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


def get_tick_size(symbol):
    try:
        info = bybit_call(get_session().get_instruments_info, category="linear", symbol=symbol)
        return float(info["result"]["list"][0]["priceFilter"]["tickSize"])
    except Exception:
        return 0.01


def round_price(price, tick_size):
    return round(round(price / tick_size) * tick_size, 8)


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

# Pending entries waiting for fill to place TP limit
# { orderId: { "symbol", "tp_side", "tp_price", "qty" } }
_pending_entries = {}


def _check_order_status(session, symbol, order_id):
    """Check if an order was filled, cancelled, or is still open.
    Returns: 'Filled', 'Cancelled', 'Open', or 'Unknown'
    """
    # First check open orders
    try:
        resp = bybit_call(session.get_open_orders,
                          category="linear", symbol=symbol,
                          orderId=order_id)
        order_list = resp.get("result", {}).get("list", [])
        if order_list:
            st = order_list[0].get("orderStatus", "")
            if st in ("Cancelled", "Rejected", "Deactivated"):
                return "Cancelled"
            if st == "Filled":
                return "Filled"
            # New, PartiallyFilled, etc — still active
            return "Open"
    except Exception as e:
        logger.warning(f"[monitor] get_open_orders error for {order_id}: {e}")

    # Order not in open orders — check order history for definitive status
    try:
        resp = bybit_call(session.get_order_history,
                          category="linear", symbol=symbol,
                          orderId=order_id)
        order_list = resp.get("result", {}).get("list", [])
        if order_list:
            st = order_list[0].get("orderStatus", "")
            if st == "Filled":
                return "Filled"
            if st in ("Cancelled", "Rejected", "Deactivated"):
                return "Cancelled"
            return st or "Unknown"
    except Exception as e:
        logger.warning(f"[monitor] get_order_history error for {order_id}: {e}")

    return "Unknown"


def _place_tp_limit(session, symbol, side, qty, price, retry=True):
    """Place a reduce-only limit order as TP. Returns orderId or None."""
    logger.info(f"[TP] Placing {side} reduce-only limit: {symbol} qty={qty} @ {price}")
    try:
        tp_ord = bybit_call(session.place_order,
                            category="linear", symbol=symbol,
                            side=side, orderType="Limit", qty=str(qty),
                            price=str(price), reduceOnly=True,
                            timeInForce="GTC")
        logger.info(f"[TP] Response: retCode={tp_ord.get('retCode')} retMsg={tp_ord.get('retMsg')}")
        if tp_ord.get("retCode") == 0:
            return tp_ord["result"].get("orderId", "")
        # Retry once after a short pause
        if retry:
            logger.info(f"[TP] Retrying TP limit for {symbol} in 1s...")
            time.sleep(1)
            return _place_tp_limit(session, symbol, side, qty, price, retry=False)
        # Last resort: fall back to trading stop
        logger.warning(f"[TP] Limit failed for {symbol}, falling back to trading stop")
        try:
            bybit_call(session.set_trading_stop,
                       category="linear", symbol=symbol,
                       takeProfit=str(price), positionIdx=0)
            logger.info(f"[TP] Trading stop TP set for {symbol} @ {price}")
        except Exception as e2:
            logger.error(f"[TP] Trading stop also failed for {symbol}: {e2}")
        return None
    except Exception as e:
        logger.error(f"[TP] Exception placing TP for {symbol}: {e}")
        if retry:
            time.sleep(1)
            return _place_tp_limit(session, symbol, side, qty, price, retry=False)
        return None


def background_monitor():
    """Background loop every 3s:
    1. Check pending entries — if filled, place TP reduce-only limit
    2. Cancel orphaned TP orders when SL hits (position gone)
    """
    while True:
        try:
            time.sleep(3)
            session = get_session()

            # ── Check pending entries for fills ──
            if _pending_entries:
                for order_id in list(_pending_entries.keys()):
                    info = _pending_entries.get(order_id)
                    if not info:
                        continue
                    sym = info["symbol"]

                    status = _check_order_status(session, sym, order_id)

                    if status == "Open":
                        continue  # still waiting

                    if status in ("Cancelled", "Rejected", "Deactivated"):
                        logger.info(f"[monitor] Entry {order_id} {sym} was {status}")
                        _pending_entries.pop(order_id, None)
                        for t in trades:
                            if t["id"] == order_id and t["status"] == "Open":
                                t["status"] = "Cancelled"
                        continue

                    if status == "Filled":
                        _pending_entries.pop(order_id, None)

                        # Get actual position size for TP qty
                        try:
                            pos = bybit_call(session.get_positions,
                                             category="linear", symbol=sym)
                            pos_list = pos.get("result", {}).get("list", [])
                            pos_size = 0.0
                            for p in pos_list:
                                s = float(p.get("size", 0))
                                if s > 0:
                                    pos_size = s
                                    break
                        except Exception as e:
                            logger.error(f"[monitor] Position check error for {sym}: {e}")
                            pos_size = float(info.get("qty", 0))

                        if pos_size <= 0:
                            # Position already closed (SL hit instantly)
                            logger.info(f"[monitor] Entry filled but no position for {sym} (SL hit?)")
                            for t in trades:
                                if t["id"] == order_id and t["status"] == "Open":
                                    t["status"] = "Closed"
                            continue

                        # Place TP reduce-only limit
                        tp_qty = str(pos_size)
                        tp_order_id = _place_tp_limit(
                            session, sym, info["tp_side"],
                            tp_qty, info["tp_price"]
                        )
                        if tp_order_id:
                            _tp_orders[sym] = {
                                "orderId": tp_order_id,
                                "side": info["tp_side"],
                                "qty": tp_qty,
                                "price": info["tp_price"],
                            }
                            logger.info(f"[monitor] TP limit placed for {sym}: {tp_order_id}")
                        continue

                    # status == "Unknown" — order might still be processing
                    logger.debug(f"[monitor] Entry {order_id} {sym} status unknown, will retry")

            # ── Cancel orphaned TP orders (SL hit — position gone) ──
            if _tp_orders:
                try:
                    positions = bybit_call(session.get_positions,
                                           category="linear", settleCoin="USDT")
                    if positions.get("retCode") == 0:
                        pos_list = positions.get("result", {}).get("list", [])
                        open_syms = {p.get("symbol") for p in pos_list
                                     if float(p.get("size", 0)) > 0}
                        for sym in [s for s in list(_tp_orders.keys()) if s not in open_syms]:
                            tp_info = _tp_orders.pop(sym, None)
                            if tp_info:
                                try:
                                    logger.info(f"[monitor] Position gone for {sym}, cancelling TP {tp_info['orderId']}")
                                    bybit_call(session.cancel_order,
                                               category="linear", symbol=sym,
                                               orderId=tp_info["orderId"])
                                except Exception as e:
                                    logger.warning(f"[monitor] Cancel TP failed {sym}: {e}")
                                # Mark trade as Closed
                                for t in trades:
                                    if t["ticker"] == sym and t["status"] == "Open":
                                        t["status"] = "Closed"
                except Exception as e:
                    logger.warning(f"[monitor] Orphan check error: {e}")

            # ── Check if TP limit orders themselves got filled ──
            if _tp_orders:
                for sym in list(_tp_orders.keys()):
                    tp_info = _tp_orders.get(sym)
                    if not tp_info:
                        continue
                    tp_status = _check_order_status(session, sym, tp_info["orderId"])
                    if tp_status == "Filled":
                        logger.info(f"[monitor] TP filled for {sym}")
                        _tp_orders.pop(sym, None)
                        for t in trades:
                            if t["ticker"] == sym and t["status"] == "Open":
                                t["status"] = "Closed"
                    elif tp_status in ("Cancelled", "Rejected"):
                        logger.info(f"[monitor] TP order {tp_status} for {sym}")
                        _tp_orders.pop(sym, None)

        except Exception as e:
            logger.warning(f"[monitor] Loop error: {e}")


_monitor_thread = threading.Thread(target=background_monitor, daemon=True)
_monitor_thread.start()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/test-bybit", methods=["GET"])
def test_bybit():
    results = {}
    session = get_session()
    try:
        resp = session.get_server_time()
        results["server_time"] = {"status": "ok", "retCode": resp.get("retCode")}
    except Exception as e:
        results["server_time"] = {"status": "error", "error": str(e)}
    try:
        resp = session.get_tickers(category="linear", symbol="BTCUSDT")
        price = resp["result"]["list"][0]["lastPrice"] if resp.get("retCode") == 0 else None
        results["ticker"] = {"status": "ok", "price": price}
    except Exception as e:
        results["ticker"] = {"status": "error", "error": str(e)}
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED")
        results["wallet"] = {"status": "ok", "retCode": resp.get("retCode")}
    except Exception as e:
        results["wallet"] = {"status": "error", "error": str(e)}
    try:
        resp = session.get_positions(category="linear", settleCoin="USDT")
        if resp.get("retCode") == 0:
            pos_list = resp.get("result", {}).get("list", [])
            open_pos = [p for p in pos_list if float(p.get("size", 0)) > 0]
            results["positions"] = {"status": "ok", "total": len(pos_list), "open": len(open_pos)}
    except Exception as e:
        results["positions"] = {"status": "error", "error": str(e)}
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
    if WEBHOOK_SECRET and WEBHOOK_SECRET != "your_webhook_secret_here":
        token = request.headers.get("X-Webhook-Secret", "")
        if token != WEBHOOK_SECRET:
            body = request.get_json(silent=True) or {}
            if body.get("secret") != WEBHOOK_SECRET:
                return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(silent=True)
        if data is None:
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
        # Prefer 'limit' field for explicit limit price, fall back to 'entry'
        entry = float(data.get("limit") or data.get("entry", 0))
        tp = float(data.get("tp", 0))
        sl = float(data.get("sl", 0))
        side = str(data.get("action") or data.get("side", "Buy")).capitalize()

        if not all([ticker, tp, sl]):
            return jsonify({"error": "Missing parameters (ticker, tp, sl required)"}), 400

        if entry <= 0:
            return jsonify({"error": "Missing or invalid entry/limit price"}), 400

        ticker_info = bybit_call(get_session().get_tickers, category="linear", symbol=ticker)
        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
        logger.info(f"{ticker} last_price={last_price}, entry={entry}, tp={tp}, sl={sl}, side={side}")

        price_for_calc = entry
        tp_distance = abs(price_for_calc - tp)
        if tp_distance == 0:
            return jsonify({"error": "TP distance is zero"}), 400

        target_profit = settings.get("targetProfit", 40.0)
        raw_quantity = target_profit / tp_distance
        leverage = LEVERAGE_CONFIG.get(ticker, 10)

        min_qty, qty_step = get_symbol_info(ticker)
        quantity = round_qty(raw_quantity, min_qty, qty_step)
        logger.info(f"qty={quantity} (raw={raw_quantity}, min={min_qty}, step={qty_step})")

        try:
            bybit_call(get_session().set_leverage,
                       category="linear", symbol=ticker,
                       buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            logger.info(f"set_leverage note: {e}")

        tick_size = get_tick_size(ticker)
        limit_price = round_price(entry, tick_size)
        tp_price = round_price(tp, tick_size)

        # Place LIMIT entry with SL attached (NO TP — TP will be a separate limit order)
        logger.info(f"Placing LIMIT {side}: {ticker} qty={quantity} price={limit_price} sl={sl}")
        order = bybit_call(get_session().place_order,
                           category="linear", symbol=ticker, side=side,
                           orderType="Limit", qty=str(quantity),
                           price=str(limit_price), stopLoss=str(sl),
                           timeInForce="GTC")
        logger.info(f"Entry response: {order}")

        if order.get("retCode", -1) != 0:
            error_msg = order.get("retMsg", "Unknown error")
            logger.error(f"Entry rejected: {error_msg}")
            trades.append({
                "id": "failed", "ticker": ticker, "side": side,
                "entry": limit_price, "tp": tp, "sl": sl,
                "quantity": quantity, "leverage": leverage,
                "status": "Failed", "pnl": 0.0,
                "timestamp": int(time.time() * 1000), "error": error_msg
            })
            return jsonify({"error": error_msg}), 400

        # Track entry so monitor places TP limit after fill
        entry_order_id = order["result"].get("orderId", "")
        tp_side = "Sell" if side == "Buy" else "Buy"
        _pending_entries[entry_order_id] = {
            "symbol": ticker,
            "tp_side": tp_side,
            "tp_price": tp_price,
            "qty": str(quantity),
        }
        logger.info(f"Pending TP for {ticker}: after entry {entry_order_id} fills → {tp_side} limit @ {tp_price}")

        trades.append({
            "id": entry_order_id,
            "ticker": ticker, "side": side, "entry": limit_price,
            "tp": tp, "sl": sl, "quantity": quantity,
            "leverage": leverage, "status": "Open", "pnl": 0.0,
            "entryType": "limit",
            "tpType": "limit",
            "timestamp": int(time.time() * 1000)
        })
        return jsonify({"status": "success", "order": order, "entryType": "limit", "tpType": "limit"}), 200

    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades", methods=["GET"])
def get_trades():
    return jsonify(trades), 200


@app.route("/api/trades/<trade_id>/target-profit", methods=["PATCH"])
def update_trade_tp(trade_id):
    """Update TP for an existing trade by cancelling old TP limit and placing a new one."""
    data = request.json
    new_tp = float(data.get("targetProfit", 0))
    for t in trades:
        if t["id"] == trade_id:
            sym = t["ticker"]
            session = get_session()
            tick_size = get_tick_size(sym)
            new_tp_price = round_price(new_tp, tick_size)

            # Cancel existing TP limit order if any
            existing_tp = _tp_orders.get(sym)
            if existing_tp:
                try:
                    logger.info(f"Cancelling old TP order {existing_tp['orderId']} for {sym}")
                    bybit_call(session.cancel_order,
                               category="linear", symbol=sym,
                               orderId=existing_tp["orderId"])
                except Exception as e:
                    logger.warning(f"Cancel old TP failed for {sym}: {e}")
                _tp_orders.pop(sym, None)

            # Determine TP side and qty from position
            try:
                pos = bybit_call(session.get_positions,
                                 category="linear", symbol=sym)
                pos_list = pos.get("result", {}).get("list", [])
                pos_size = 0.0
                pos_side = ""
                for p in pos_list:
                    s = float(p.get("size", 0))
                    if s > 0:
                        pos_size = s
                        pos_side = p.get("side", "")
                        break

                if pos_size <= 0:
                    return jsonify({"error": "No open position found"}), 400

                # TP side is opposite of position side
                tp_side = "Sell" if pos_side == "Buy" else "Buy"
                tp_order_id = _place_tp_limit(
                    session, sym, tp_side, str(pos_size), new_tp_price
                )
                if tp_order_id:
                    _tp_orders[sym] = {
                        "orderId": tp_order_id,
                        "side": tp_side,
                        "qty": str(pos_size),
                        "price": new_tp_price,
                    }
                t["tp"] = new_tp
                return jsonify(t), 200

            except Exception as e:
                logger.error(f"Update TP error for {sym}: {e}")
                # Fallback: try set_trading_stop
                try:
                    bybit_call(session.set_trading_stop,
                               category="linear", symbol=sym,
                               takeProfit=str(new_tp_price), positionIdx=0)
                    t["tp"] = new_tp
                    return jsonify(t), 200
                except Exception as e2:
                    return jsonify({"error": str(e2)}), 500
    return jsonify({"error": "Trade not found"}), 404


@app.route("/api/sync-trades", methods=["POST"])
def sync_trades():
    try:
        session = get_session()
        all_positions = []
        cursor = ""
        while True:
            params = {"category": "linear", "settleCoin": "USDT", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            positions = bybit_call(session.get_positions, **params)

            if positions.get("retCode", -1) != 0:
                error_msg = positions.get("retMsg", "Bybit API error")
                logger.error(f"sync error: {error_msg}")
                return jsonify({"error": error_msg}), 502

            all_positions.extend(positions.get("result", {}).get("list", []))
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

        open_symbols = {p.get("symbol") for p in all_positions if float(p.get("size", 0)) > 0}
        for t in trades:
            if t["status"] == "Open" and t["ticker"] not in open_symbols:
                t["status"] = "Closed"

        logger.info(f"Synced {len(all_positions)} positions")
        return jsonify({"status": "synced", "positions": len(all_positions)}), 200
    except Exception as e:
        logger.exception(f"sync error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Serve React frontend ──
DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    if path and os.path.exists(os.path.join(DIST_DIR, path)):
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
