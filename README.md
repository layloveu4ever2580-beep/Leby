# Bybit Money Management Bot

A trading bot that receives webhook signals from TradingView, automatically calculates position sizes based on a target profit, and places orders on Bybit. Includes a React dashboard for monitoring trades in real time.

## Features

- Market order entry with limit order take-profit for exact TP fills
- Automatic position sizing based on target profit and live market price
- Leverage configuration per trading pair
- Auto-cancel orphaned TP limit orders when SL hits (background monitor)
- Real-time trade monitoring dashboard
- PnL tracking with Bybit position sync (imports existing positions)
- Auto-detection of closed positions during sync
- Failed order tracking with error details
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

1. TradingView Pine Script detects a setup and places a limit entry at the Fib level
2. When the limit entry fills, the script sends a webhook with ticker, action, TP, and SL
3. The bot receives the webhook and:
   - Places a **market order** to enter the position (with SL attached)
   - Places a separate **limit order** at the exact TP price (reduceOnly)
4. A background thread monitors positions every 30s — if SL hits and the position closes, it cancels the orphaned TP limit order

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

## Webhook Usage

Send a POST request to `/webhook` with the following JSON body:

```json
{
  "ticker": "BTCUSDT",
  "action": "Buy",
  "entry": 65000,
  "tp": 67000,
  "sl": 64000
}
```

- `ticker` (required): Symbol without `.P` suffix
- `action` (required): `"Buy"` or `"Sell"`
- `tp` (required): Take profit price — placed as a limit order
- `sl` (required): Stop loss price — attached to the market entry
- `entry`: Optional, used for logging. Position sizing uses live market price.

## TradingView Setup

1. Copy the Pine Script from `pinescript/4ema_fib_strategy.pine` into TradingView
2. Create an alert with condition set to **"alert() function calls only"**
3. Set the webhook URL to `https://your-server/webhook`
4. The script only sends the webhook when the limit entry actually fills, not on setup confirmation

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
