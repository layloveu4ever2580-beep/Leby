import os
import json

# Use /app/data if it exists (Docker volume), otherwise same dir as this file
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)
_CONFIG_FILE = os.path.join(_DATA_DIR, "leverage_config.json")

# Default config — used when no JSON file exists yet
_DEFAULT_CONFIG = {
    "BTCUSDT": 50,
    "ETHUSDT": 50,
    "SOLUSDT": 20,
    "XRPUSDT": 20,
    "ADAUSDT": 20,
    "JASMYUSDT": 50,
    "APEUSDT": 50,
    "DEXEUSDT": 20,
    "FARTCOINUSDT": 75,
    "YFIUSDT": 25,
    "ETCUSDT": 50,
    "ZROUSDT": 25,
    "PENGUUSDT": 75,
    "ILVUSDT": 20,
    "MAGICUSDT": 25,
    "DOGEUSDT": 75,
    "DEEPUSDT": 25,
    "ICXUSDT": 25,
    "COMPUSDT": 25,
    "VIRTUALUSDT": 50,
}


def _load_config():
    """Load config from JSON file, falling back to defaults."""
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return dict(_DEFAULT_CONFIG)


def save_leverage_config(config):
    """Persist the leverage config to disk as JSON."""
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# Module-level dict used by the rest of the app
LEVERAGE_CONFIG = _load_config()
