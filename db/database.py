"""
SQLite database — ініціалізація та підключення.
WAL mode вмикається при init_db() для паралельних reads під час write.
"""
import os
import aiosqlite
from config import DB_PATH
from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id          INTEGER UNIQUE NOT NULL,
    username             TEXT,
    first_name           TEXT,
    alerts_enabled       INTEGER DEFAULT 1,
    spike_threshold_pct  REAL    DEFAULT 0.5,
    monitored_asset      TEXT    DEFAULT 'USDT',
    min_limit_uah        INTEGER DEFAULT 500,
    payment_methods      TEXT    DEFAULT '[]',
    is_premium           INTEGER DEFAULT 0,
    premium_until        TEXT    DEFAULT NULL,
    created_at           TEXT    DEFAULT (datetime('now')),
    updated_at           TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    fiat            TEXT    NOT NULL,
    best_ask        REAL,
    best_bid        REAL,
    mid_price       REAL,
    sellers_count   INTEGER DEFAULT 0,
    buyers_count    INTEGER DEFAULT 0,
    raw_ask_prices  TEXT,
    raw_bid_prices  TEXT,
    error           TEXT    DEFAULT NULL,
    captured_at     TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON price_snapshots(platform, asset, captured_at);

CREATE TABLE IF NOT EXISTS market_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT    NOT NULL,
    fiat        TEXT    NOT NULL,
    period      TEXT    NOT NULL,
    min_price   REAL,
    max_price   REAL,
    avg_price   REAL,
    volatility  REAL,
    calc_at     TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    alert_type      TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    change_pct      REAL,
    price_before    REAL,
    price_after     REAL,
    message_text    TEXT,
    sent_at         TEXT    DEFAULT (datetime('now')),
    sent_at_ts      REAL    DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS api_errors (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT    NOT NULL,
    error_type   TEXT    NOT NULL,
    error_msg    TEXT,
    http_status  INTEGER,
    occurred_at  TEXT    DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    """Ініціалізувати БД: створити директорію, виконати схему."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info(f"БД ініціалізовано: {DB_PATH}")


async def get_db() -> aiosqlite.Connection:
    """Відкрити з'єднання з БД. Закривати після використання."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db
