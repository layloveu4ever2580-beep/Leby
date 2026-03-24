import os
import time
import logging
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
    CORS(app)  # allow all when served from same origin
else:
    CORS(app, origins=_raw_origins.split(","))

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
PORT = int(os.getenv("PORT", 5001))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

_session = None


def get_session():
    """Lazy-initialize the Bybit HTTP session."""
    global _session
    if _session is None:
        _session = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
    return _session

settings = {
    "targetProfit": 100.0,
    "theme": "dark",
    "timezone": "UTC"
}
trades = []


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


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


def get_symbol_info(symbol):
    """Get tick size and lot size constraints for a symbol."""
    try:
        info = get_session().get_instruments_info(category="linear", symbol=symbol)
        instrument = info["result"]["list"][0]
        lot_filter = instrument["lotSizeFilter"]
        min_qty = float(lot_filter["minOrderQty"])
        qty_step = float(lot_filter["qtyStep"])
        return min_qty, qty_step
    except Exception:
        return 0.001, 0.001


def round_qty(qty, min_qty, qty_step):
    """Round quantity to valid lot size."""
    if qty < min_qty:
        qty = min_qty
    # Round down to nearest qty_step
    steps = int(qty / qty_step)
    return round(steps * qty_step, 8)


@app.route("/webhook", methods=["POST"])
def webhook():
    # Authenticate: check header OR JSON body field (TradingView can't send headers)
    if WEBHOOK_SECRET and WEBHOOK_SECRET != "your_webhook_secret_here":
        token = request.headers.get("X-Webhook-Secret", "")
        if token != WEBHOOK_SECRET:
            # Also check if secret is embedded in the JSON body
            body = request.get_json(silent=True) or {}
            if body.get("secret") != WEBHOOK_SECRET:
                return jsonify({"error": "Unauthorized"}), 401

    try:
        # Handle non-JSON alerts (setup/confirmation text alerts from Pine Script)
        data = request.get_json(silent=True)
        if data is None:
            raw = request.get_data(as_text=True)
            logger.info(f"Non-JSON webhook received (ignoring): {raw[:200]}")
            return jsonify({"status": "ignored", "reason": "not a trade signal"}), 200

        logger.info(f"Webhook received: {data}")

        # Skip non-trade alerts (text alerts like "Bullish Setup Detected...")
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

        # Fetch current market price — use it for position sizing
        ticker_info = get_session().get_tickers(category="linear", symbol=ticker)
        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
        logger.info(f"{ticker} last_price={last_price}, entry={entry}, tp={tp}, sl={sl}, side={side}")

        # Use market price for quantity calc (more accurate than alert's limit price)
        price_for_calc = last_price if last_price > 0 else entry
        tp_distance = abs(price_for_calc - tp)
        if tp_distance == 0:
            return jsonify({"error": "TP distance is zero, cannot calculate quantity"}), 400

        target_profit = settings.get("targetProfit", 100.0)
        raw_quantity = target_profit / tp_distance
        leverage = LEVERAGE_CONFIG.get(ticker, 10)

        # Round quantity to valid lot size
        min_qty, qty_step = get_symbol_info(ticker)
        quantity = round_qty(raw_quantity, min_qty, qty_step)
        logger.info(f"Calculated qty={quantity} (raw={raw_quantity}, min={min_qty}, step={qty_step})")

        # Set leverage (ignore errors if already set)
        try:
            get_session().set_leverage(
                category="linear",
                symbol=ticker,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage)
            )
        except Exception as e:
            logger.info(f"set_leverage note: {e}")

        # Place market order directly — no price validation gate
        logger.info(f"Placing MARKET {side} order: {ticker} qty={quantity} tp={tp} sl={sl}")
        order = get_session().place_order(
            category="linear",
            symbol=ticker,
            side=side,
            orderType="Market",
            qty=str(quantity),
            takeProfit=str(tp),
            stopLoss=str(sl)
        )
        logger.info(f"Order response: {order}")

        if order.get("retCode", -1) != 0:
            error_msg = order.get("retMsg", "Unknown Bybit error")
            logger.error(f"Bybit order rejected: {error_msg}")
            # Record as failed trade
            trades.append({
                "id": "failed",
                "ticker": ticker,
                "side": side,
                "entry": last_price,
                "tp": tp,
                "sl": sl,
                "quantity": quantity,
                "leverage": leverage,
                "status": "Failed",
                "pnl": 0.0,
                "timestamp": int(time.time() * 1000),
                "error": error_msg
            })
            return jsonify({"error": error_msg}), 400

        trade_record = {
            "id": order["result"].get("orderId", "unknown"),
            "ticker": ticker,
            "side": side,
            "entry": last_price,
            "tp": tp,
            "sl": sl,
            "quantity": quantity,
            "leverage": leverage,
            "status": "Open",
            "pnl": 0.0,
            "timestamp": int(time.time() * 1000)
        }
        trades.append(trade_record)

        return jsonify({"status": "success", "order": order}), 200
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades", methods=["GET"])
def get_trades():
    return jsonify(trades), 200


@app.route("/api/trades/<trade_id>/target-profit", methods=["PATCH"])
def update_trade_tp(trade_id):
    """Update TP/SL on the position (not the filled order)."""
    data = request.json
    new_tp = float(data.get("targetProfit", 0))
    for t in trades:
        if t["id"] == trade_id:
            try:
                get_session().set_trading_stop(
                    category="linear",
                    symbol=t["ticker"],
                    takeProfit=str(new_tp),
                    positionIdx=0
                )
                t["tp"] = new_tp
                return jsonify(t), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Trade not found"}), 404


@app.route("/api/sync-trades", methods=["POST"])
def sync_trades():
    """Sync open positions from Bybit."""
    try:
        session = get_session()
        positions = session.get_positions(category="linear", settleCoin="USDT")

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

            # Update existing trades with PnL
            matched = False
            for t in trades:
                if t["ticker"] == symbol and t["status"] == "Open":
                    t["pnl"] = unrealised_pnl
                    matched = True

            # If position exists on Bybit but not in local trades, add it
            if not matched:
                side_str = pos.get("side", "Buy")
                trades.append({
                    "id": f"synced-{symbol}-{int(time.time())}",
                    "ticker": symbol,
                    "side": side_str,
                    "entry": float(pos.get("avgPrice", 0)),
                    "tp": float(pos.get("takeProfit", 0)),
                    "sl": float(pos.get("stopLoss", 0)),
                    "quantity": size,
                    "leverage": int(float(pos.get("leverage", 1))),
                    "status": "Open",
                    "pnl": unrealised_pnl,
                    "timestamp": int(float(pos.get("createdTime", time.time() * 1000)))
                })

        # Mark local trades as closed if no matching open position on Bybit
        open_symbols = {pos.get("symbol") for pos in position_list if float(pos.get("size", 0)) > 0}
        for t in trades:
            if t["status"] == "Open" and t["ticker"] not in open_symbols:
                t["status"] = "Closed"

        logger.info(f"Synced {len(position_list)} positions from Bybit")
        return jsonify({"status": "synced", "positions": len(position_list)}), 200
    except Exception as e:
        logger.exception(f"sync_trades error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Serve React frontend (must be last route) ────────────────────────────────
DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    """Serve React build. Falls back to index.html for client-side routing."""
    if path and os.path.exists(os.path.join(DIST_DIR, path)):
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
