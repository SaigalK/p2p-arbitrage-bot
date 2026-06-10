import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "0"))

# --- Polling ---
POLLING_INTERVAL_SEC: int = int(os.getenv("POLLING_INTERVAL_SEC", "30"))
ALERT_COOLDOWN_SEC: int = int(os.getenv("ALERT_COOLDOWN_SEC", "900"))   # 15 хв
SPIKE_WINDOW_MINUTES: int = int(os.getenv("SPIKE_WINDOW_MINUTES", "10"))
HISTORY_HOURS: int = int(os.getenv("HISTORY_HOURS", "8"))

# --- Дефолти для нових юзерів ---
DEFAULT_SPIKE_THRESHOLD_PCT: float = float(os.getenv("DEFAULT_SPIKE_THRESHOLD_PCT", "0.5"))
DEFAULT_MIN_LIMIT_UAH: int = int(os.getenv("DEFAULT_MIN_LIMIT_UAH", "500"))

# --- Активи ---
SUPPORTED_ASSETS: list[str] = ["USDT", "BTC", "ETH"]
DEFAULT_ASSET: str = "USDT"
DEFAULT_FIAT: str = "UAH"

# --- API endpoints (верифіковано curl-тестами 2026-06-08) ---
BINANCE_P2P_URL: str = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
BYBIT_P2P_URL: str = "https://api2.bybit.com/fiat/otc/item/online"   # НЕ v5
NBU_URL: str = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"

# --- HTTP ---
HTTP_TIMEOUT_SEC: int = 10
HTTP_HEADERS: dict = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# --- Database ---
DB_PATH: str = os.getenv("DB_PATH", "data/dashboard.db")

# --- Монетизація ---
CRYPTOBOT_TOKEN: str = os.getenv("CRYPTOBOT_TOKEN", "")

# --- Логування ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "logs/bot.log")
