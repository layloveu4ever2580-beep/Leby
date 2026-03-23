import os
import time
from flask import Flask, request, jsonify, send_from_directory
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from flask_cors import CORS
from leverage_config import LEVERAGE_CONFIG

load_dotenv()

app = Flask(__name__)

# CORS: restrict to frontend origin in production
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
CORS(app, origins=ALLOWED_ORIGINS)

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
    # Authenticate webhook
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Webhook-Secret", "")
        if token != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.json
        ticker = data.get("ticker")
        entry = float(data.get("limit") or data.get("entry", 0))
        tp = float(data.get("tp", 0))
        sl = float(data.get("sl", 0))
        side = str(data.get("action") or data.get("side", "Buy")).capitalize()

        if not all([ticker, entry, tp, sl]):
            return jsonify({"error": "Missing parameters"}), 400

        target_profit = settings.get("targetProfit", 100.0)
        raw_quantity = target_profit / abs(entry - tp)
        leverage = LEVERAGE_CONFIG.get(ticker, 10)

        # Round quantity to valid lot size
        min_qty, qty_step = get_symbol_info(ticker)
        quantity = round_qty(raw_quantity, min_qty, qty_step)

        try:
            get_session().set_leverage(
                category="linear",
                symbol=ticker,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage)
            )
        except Exception:
            pass

        # Check if market price is between SL and TP
        # Buy:  sl < last_price < tp  (price below TP, above SL)
        # Sell: tp < last_price < sl  (price above TP, below SL)
        ticker_info = get_session().get_tickers(category="linear", symbol=ticker)
        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])

        if side == "Buy":
            if not (sl < last_price < tp):
                return jsonify({"error": f"Price validation failed: last={last_price}, sl={sl}, tp={tp}"}), 400
        else:  # Sell / Short
            if not (tp < last_price < sl):
                return jsonify({"error": f"Price validation failed: last={last_price}, tp={tp}, sl={sl}"}), 400

        order = get_session().place_order(
            category="linear",
            symbol=ticker,
            side=side,
            orderType="Market",
            qty=str(quantity),
            takeProfit=str(tp),
            stopLoss=str(sl)
        )

        trade_record = {
            "id": order["result"]["orderId"] if "result" in order and "orderId" in order["result"] else "unknown",
            "ticker": ticker,
            "side": side,
            "entry": entry,
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
        positions = get_session().get_positions(category="linear", settleCoin="USDT")
        position_list = positions.get("result", {}).get("list", [])

        # Build a set of known trade tickers for matching
        known_ids = {t["id"] for t in trades}

        for pos in position_list:
            size = float(pos.get("size", 0))
            if size == 0:
                continue

            # Update existing trades with PnL
            symbol = pos.get("symbol", "")
            unrealised_pnl = float(pos.get("unrealisedPnl", 0))

            for t in trades:
                if t["ticker"] == symbol and t["status"] == "Open":
                    t["pnl"] = unrealised_pnl

        return jsonify({"status": "synced", "positions": len(position_list)}), 200
    except Exception as e:
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
