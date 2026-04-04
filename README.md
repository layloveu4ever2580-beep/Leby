# Bybit Money Management Trading Bot

A trading bot that receives signals from TradingView webhooks, calculates position size using a money management formula, and executes trades on Bybit Perpetual Futures using **limit orders** for both entry and take profit (lower fees).

## How It Works

```
TradingView Alert → Webhook → Limit Entry + SL → (wait for fill) → TP Limit Order
```

1. **TradingView** sends a webhook alert with ticker, entry price, TP, SL, and action (buy/sell)
2. **Bot** calculates position size based on target profit and TP distance
3. **Bot** places a **limit entry order** on Bybit with stop loss attached
4. **Background monitor** detects when the entry fills, then places a **reduce-only limit order** as take profit
5. If SL hits first, the monitor cancels the orphaned TP limit order

## Project Structure

```
├── backend/
│   ├── main.py              # Flask API + webhook handler + background monitor
│   ├── leverage_config.py   # Per-coin leverage settings
│   ├── requirements.txt     # Python dependencies
│   └── .env                 # API keys (not committed)
├── frontend/
│   └── src/
│       ├── App.jsx           # React dashboard
│       └── App.css           # Styling
├── pinescript/
│   └── 4ema_fib_strategy.pine  # TradingView strategy with webhook alerts
├── Dockerfile               # Multi-stage build (frontend + backend)
├── docker-compose.yml       # Local Docker setup
└── render.yaml              # Render deployment config
```

## Setup

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Create `backend/.env`:

```env
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
BYBIT_TESTNET=false
PORT=5001
WEBHOOK_SECRET=your_webhook_secret_here
ALLOWED_ORIGINS=http://localhost:3000
```

Run: `python main.py`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard runs on `http://localhost:3000` and proxies API calls to the backend.

## Webhook JSON Format

TradingView alerts send this JSON to `POST /webhook`:

```json
{
  "ticker": "BTCUSDT",
  "action": "buy",
  "limit": 84000.50,
  "entry": 84000.50,
  "tp": 85200.00,
  "sl": 83500.00
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/test-bybit` | Test Bybit API connectivity |
| GET | `/api/settings` | Get bot settings |
| POST | `/api/settings` | Update bot settings |
| POST | `/webhook` | Receive TradingView alerts |
| GET | `/api/trades` | Get trade list |
| PATCH | `/api/trades/:id/target-profit` | Update TP (cancels old limit, places new) |
| POST | `/api/sync-trades` | Sync positions from Bybit |

## Money Management Formula

```
quantity = targetProfit / abs(entryPrice - tpPrice)
```

The bot sizes every position so that hitting TP yields exactly `$targetProfit`.

## Deployment (Render)

Push to GitHub, then deploy on Render using the included `render.yaml`. Set environment variables (`BYBIT_API_KEY`, `BYBIT_API_SECRET`, `WEBHOOK_SECRET`) in the Render dashboard.

## Key Features

- **Limit orders only** — entry and TP use limit orders for maker fees
- **Automatic TP placement** — background monitor places TP after entry fills
- **SL cleanup** — orphaned TP orders are cancelled when SL hits
- **Position sizing** — auto-calculated from target profit
- **Per-coin leverage** — configurable in `leverage_config.py`
- **React dashboard** — monitor trades, PnL, and win rate
