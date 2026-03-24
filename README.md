# Bybit Money Management Bot

A trading bot that receives webhook signals, automatically calculates position sizes based on a target profit, and places orders on Bybit. Includes a React dashboard for monitoring trades in real time.

## Features

- Webhook-driven market order execution with authentication
- Automatic position sizing based on target profit and live market price
- Leverage configuration per trading pair
- Real-time trade monitoring dashboard
- PnL tracking with Bybit position sync (imports existing positions on sync)
- Auto-detection of closed positions during sync
- Failed order tracking with error details
- Light/dark theme support
- Responsive design (mobile/tablet/desktop)

## Architecture

```
├── backend/          Flask API + serves React dashboard
│   ├── main.py       Webhook handler, trade API, settings, static serving
│   └── leverage_config.py
├── frontend/         React + Vite dashboard (built into backend/dist)
│   └── src/App.jsx   Trading dashboard UI
├── Dockerfile        Multi-stage: builds React, runs Flask
├── docker-compose.yml
└── render.yaml       Render.com deployment (single service)
```

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
| `VITE_API_URL` | Backend URL for frontend | `http://localhost:5001` |

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

The bot places a **market order** immediately on receiving the alert. The `entry` (or `limit`) field is optional — position sizing uses the live market price for accuracy. Only `ticker`, `tp`, and `sl` are required.

Include the `X-Webhook-Secret` header if `WEBHOOK_SECRET` is configured.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/settings` | Get bot settings |
| POST | `/api/settings` | Update bot settings |
| POST | `/webhook` | Receive trade signals |
| GET | `/api/trades` | List all trades |
| PATCH | `/api/trades/:id/target-profit` | Update trade TP |
| POST | `/api/sync-trades` | Sync positions from Bybit |

## Deployment

Deployed as a **single service** on [Render.com](https://render.com) via `render.yaml`.
Flask serves both the API and the React dashboard from one URL — no separate frontend service needed.

Push to your repo, connect it in the Render dashboard, and set your environment variables.

## License

MIT
