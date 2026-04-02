# Bybit Money Management Bot

A trading bot that receives webhook signals from TradingView, automatically calculates position sizes based on a target profit, and places limit orders on Bybit for both entry and exit — minimizing fees by using maker orders throughout.

## Features

- **Limit order entry** at Fib price (or candle close if more favorable) — maker fees only
- **Limit order take-profit** placed automatically after entry fills — no taker fees on exit
- Smart entry price: uses `min(fib, close)` for longs, `max(fib, close)` for shorts
- Background thread monitors entry fill, then places reduce-only TP limit
- Auto-cancel orphaned TP limit orders when SL hits (background monitor)
- Automatic position sizing based on target profit and live market price
- Leverage configuration per trading pair
- 8 preset profiles in Pine Script (P1–P8) + Custom mode
- Real-time trade monitoring dashboard
- PnL tracking with Bybit position sync (imports existing positions)
- Retry with exponential backoff on Bybit rate limits
- Light/dark theme support
- Responsive design (mobile/tablet/desktop)

## Architecture

```
├── backend/                Flask API + serves React dashboard
│   ├── main.py             Webhook handler, trade API, TP cleanup, static serving
│   └── leverage_config.py  Per-symbol leverage settings
├── frontend/               React + Vite dashboard (built into backend/dist)
│   └── src/App.jsx         Trading dashboard UI
├── pinescript/              TradingView Pine Script strategy
│   └── 4ema_fib_strategy.pine
├── Dockerfile              Multi-stage: builds React, runs Flask
├── docker-compose.yml
└── render.yaml
```

## How It Works

1. TradingView Pine Script detects a setup and confirmation candle closes
2. Pine calculates the Fib entry price, TP, and SL
3. If the candle closed past the Fib level (e.g., below for longs), it uses the close price instead for a better fill
4. `alert()` fires immediately with JSON containing ticker, entry, tp, sl
5. The bot receives the webhook and:
   - Places a **limit entry order** at the signal price (with SL attached)
   - Spawns a background thread that polls the entry order status
   - Once the entry fills → places a **reduce-only limit order** at the TP price (opposite side)
6. A background cleanup thread monitors positions every 30s — if SL hits and the position closes, it cancels the orphaned TP limit order

## Fee Savings

| Order Type | Bybit Fee | Used For |
|---|---|---|
| Market (taker) | 0.055% | ❌ Not used |
| Limit (maker) | 0.02% | ✅ Entry + TP exit |

Both entry and exit use limit orders = maker fees on both sides.

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- Bybit API key and secret

### Backend

```bash
cd backend
# Edit .env with your Bybit API credentials before running
pip install -r requirements.txt
python main.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The dashboard runs at `http://localhost:3000` and the API at `http://localhost:5001`.

### Docker

```bash
docker compose up --build
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `BYBIT_API_KEY` | Bybit API key | — |
| `BYBIT_API_SECRET` | Bybit API secret | — |
| `BYBIT_TESTNET` | Use testnet | `false` |
| `PORT` | Backend port | `5001` |
| `WEBHOOK_SECRET` | Secret token for webhook auth | — |
| `ALLOWED_ORIGINS` | CORS allowed origins | `http://localhost:3000` |

## Webhook Payload

The Pine Script sends this JSON via `alert()`:

```json
{
  "ticker": "ETCUSDT",
  "action": "buy",
  "entry": 18.45,
  "tp": 19.20,
  "sl": 17.80
}
```

- `ticker` (required): Symbol without `.P` suffix
- `action` (required): `"buy"` or `"sell"`
- `entry` (required): Limit entry price (Fib level or candle close, whichever is more favorable)
- `tp` (required): Take profit price — placed as a reduce-only limit after entry fills
- `sl` (required): Stop loss price — attached to the entry limit order

## TradingView Setup

1. Copy the Pine Script from `pinescript/4ema_fib_strategy.pine` into TradingView
2. Create an alert on the strategy
3. Set condition to **"alert() function calls only"**
4. Set Message to `{{message}}`
5. Enable Webhook and set URL to `https://your-server/webhook`
6. The alert fires on confirmation candle close — no waiting for backtest fills

## Pine Script Presets

| Preset | Description | EMA Lengths | Fib Entry | Fib TP | Fib SL |
|---|---|---|---|---|---|
| Custom | Use manual settings | User-defined | User-defined | User-defined | User-defined |
| P1: Aggressive | Scalping | 306/350/500/530 | 0.746 | 1.908 | -0.315 |
| P2: Balanced | Day trading | 293/300/800/900 | 0.726 | 1.708 | -0.175 |
| P3: Conservative | Swing trading | 288/300/600/820 | 0.766 | 1.838 | -0.285 |
| P4–P8 | Empty slots | Defaults | Defaults | Defaults | Defaults |

P4–P8 use the same defaults as Custom. Edit the Pine Script to set your own values.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/test-bybit` | Diagnostic: test Bybit API connectivity |
| GET | `/api/settings` | Get bot settings |
| POST | `/api/settings` | Update bot settings |
| POST | `/webhook` | Receive trade signals |
| GET | `/api/trades` | List all trades |
| PATCH | `/api/trades/:id/target-profit` | Update trade TP |
| POST | `/api/sync-trades` | Sync positions from Bybit |

## Leverage Config

Edit `backend/leverage_config.py` to set per-symbol leverage:

```python
LEVERAGE_CONFIG = {
    "BTCUSDT": 50,
    "ETHUSDT": 50,
    "SOLUSDT": 20,
    "FARTCOINUSDT": 75,
    # Symbols not listed default to 10x
}
```

## Deployment (DigitalOcean Droplet)

```bash
# 1. SSH into your droplet
ssh root@YOUR_IP

# 2. Install Docker
apt update
apt install -y docker.io docker-compose-v2

# 3. Clone and configure
git clone https://github.com/your-repo/Leby.git
cd Leby
cat > .env << 'EOF'
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
BYBIT_TESTNET=false
WEBHOOK_SECRET=
ALLOWED_ORIGINS=same-origin
PORT=10000
EOF

# 4. Build and run
docker compose up -d --build

# 5. Set up HTTPS with Caddy
apt install -y caddy
echo 'YOUR_IP.sslip.io { reverse_proxy localhost:5001 }' > /etc/caddy/Caddyfile
systemctl restart caddy

# 6. Open firewall
ufw allow 80
ufw allow 443
ufw allow 22
ufw enable
```

To update after code changes:

```bash
cd ~/Leby
git pull
docker compose up -d --build
```

## License

MIT
