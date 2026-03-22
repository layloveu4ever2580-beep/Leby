# Bybit Money Management Bot

A trading bot that receives webhook signals, automatically calculates position sizes based on a target profit, and places orders on Bybit. Includes a React dashboard for monitoring trades in real time.

## Features

- Webhook-driven order execution with authentication
- Automatic position sizing based on target profit
- Leverage configuration per trading pair
- Real-time trade monitoring dashboard
- PnL tracking with Bybit position sync
- Light/dark theme support
- Responsive design (mobile/tablet/desktop)

## Architecture

```
├── backend/          Flask API + Bybit integration
│   ├── main.py       Webhook handler, trade API, settings
│   └── leverage_config.py
├── frontend/         React + Vite dashboard
│   └── src/App.jsx   Trading dashboard UI
├── docker-compose.yml
└── render.yaml       Render.com deployment config
```

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- Bybit API key and secret

### Backend

```bash
cd backend
cp .env.example .env
# Edit .env with your Bybit API credentials
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
  "limit": 65000,
  "tp": 67000,
  "sl": 64000
}
```

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

Configured for [Render.com](https://render.com) via `render.yaml`. Push to your repo and connect it in the Render dashboard.

## License

MIT
