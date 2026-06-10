"""
SQL запити (CRUD) для всіх таблиць.
"""
import json
import aiosqlite
from collectors.base import PriceSnapshot
from utils.logger import get_logger

logger = get_logger(__name__)


# ─── USERS ───────────────────────────────────────────────────────────────────

async def get_or_create_user(db: aiosqlite.Connection, telegram_id: int,
                              username: str | None, first_name: str | None) -> dict:
    """INSERT OR IGNORE + SELECT. Безпечно для повторних /start."""
    await db.execute(
        """INSERT OR IGNORE INTO users (telegram_id, username, first_name)
           VALUES (?, ?, ?)""",
        (telegram_id, username, first_name),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else {}


async def get_user(db: aiosqlite.Connection, telegram_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_user_threshold(db: aiosqlite.Connection, telegram_id: int, pct: float):
    await db.execute(
        "UPDATE users SET spike_threshold_pct=?, updated_at=datetime('now') WHERE telegram_id=?",
        (pct, telegram_id),
    )
    await db.commit()


async def update_user_alerts(db: aiosqlite.Connection, telegram_id: int, enabled: bool):
    await db.execute(
        "UPDATE users SET alerts_enabled=?, updated_at=datetime('now') WHERE telegram_id=?",
        (1 if enabled else 0, telegram_id),
    )
    await db.commit()


async def get_all_alert_users(db: aiosqlite.Connection) -> list[dict]:
    """Всі юзери з увімкненими алертами."""
    async with db.execute(
        "SELECT * FROM users WHERE alerts_enabled = 1"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─── PRICE SNAPSHOTS ─────────────────────────────────────────────────────────

async def save_snapshot(db: aiosqlite.Connection, snap: PriceSnapshot, mid: float):
    await db.execute(
        """INSERT INTO price_snapshots
           (platform, asset, fiat, best_ask, best_bid, mid_price,
            sellers_count, buyers_count, raw_ask_prices, raw_bid_prices, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snap.platform, snap.asset, snap.fiat,
            snap.ask_price, snap.bid_price, mid,
            snap.sellers_count, snap.buyers_count,
            json.dumps(snap.ask_prices), json.dumps(snap.bid_prices),
            snap.error,
        ),
    )
    await db.commit()


async def get_recent_mid_prices(
    db: aiosqlite.Connection, asset: str, hours: int = 8
) -> list[tuple[float, float]]:
    """
    Повертає [(timestamp_unix, mid_price), ...] за останні N годин.
    Використовується для відновлення HistoryBuffer після рестарту.
    """
    async with db.execute(
        """SELECT strftime('%s', captured_at) as ts, mid_price
           FROM price_snapshots
           WHERE asset = ? AND mid_price IS NOT NULL AND error IS NULL
             AND captured_at >= datetime('now', ? || ' hours')
           ORDER BY captured_at ASC""",
        (asset, f"-{hours}"),
    ) as cur:
        rows = await cur.fetchall()
        return [(float(r["ts"]), r["mid_price"]) for r in rows]


# ─── SENT ALERTS ─────────────────────────────────────────────────────────────

async def save_alert(db: aiosqlite.Connection, user_id: int, alert_type: str,
                     asset: str, change_pct: float, price_before: float,
                     price_after: float, message_text: str):
    await db.execute(
        """INSERT INTO sent_alerts
           (user_id, alert_type, asset, change_pct, price_before, price_after, message_text)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, alert_type, asset, change_pct, price_before, price_after, message_text),
    )
    await db.commit()


async def fetch_recent_alerts(db: aiosqlite.Connection, since_ts: float) -> list[dict]:
    """Для відновлення cooldown стану AlertManager після рестарту."""
    async with db.execute(
        """SELECT u.telegram_id, sa.alert_type, sa.asset, sa.sent_at_ts
           FROM sent_alerts sa
           JOIN users u ON sa.user_id = u.id
           WHERE sa.sent_at_ts >= ?
           ORDER BY sa.sent_at_ts ASC""",
        (since_ts,),
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_user_last_alerts(db: aiosqlite.Connection, telegram_id: int,
                                limit: int = 10) -> list[dict]:
    async with db.execute(
        """SELECT sa.sent_at, sa.alert_type, sa.asset, sa.change_pct,
                  sa.price_before, sa.price_after
           FROM sent_alerts sa
           JOIN users u ON sa.user_id = u.id
           WHERE u.telegram_id = ?
           ORDER BY sa.sent_at DESC
           LIMIT ?""",
        (telegram_id, limit),
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─── API ERRORS ──────────────────────────────────────────────────────────────

async def save_api_error(db: aiosqlite.Connection, platform: str,
                          error_type: str, error_msg: str, http_status: int | None = None):
    await db.execute(
        "INSERT INTO api_errors (platform, error_type, error_msg, http_status) VALUES (?,?,?,?)",
        (platform, error_type, error_msg, http_status),
    )
    await db.commit()


async def count_recent_errors(db: aiosqlite.Connection, platform: str,
                               minutes: int = 5) -> int:
    """Кількість помилок платформи за останні N хвилин."""
    async with db.execute(
        """SELECT COUNT(*) as cnt FROM api_errors
           WHERE platform = ? AND occurred_at >= datetime('now', ? || ' minutes')""",
        (platform, f"-{minutes}"),
    ) as cur:
        row = await cur.fetchone()
        return row["cnt"] if row else 0


# ─── DATA RETENTION ──────────────────────────────────────────────────────────

async def cleanup_old_data(db: aiosqlite.Connection):
    """Видалити старі записи згідно з data retention policy."""
    await db.executescript("""
        DELETE FROM price_snapshots
            WHERE captured_at < datetime('now', '-7 days');
        DELETE FROM api_errors
            WHERE occurred_at < datetime('now', '-14 days');
        DELETE FROM sent_alerts
            WHERE sent_at < datetime('now', '-60 days');
        DELETE FROM market_stats
            WHERE calc_at < datetime('now', '-30 days');
    """)
    await db.commit()
    logger.info("Data retention: старі записи видалено")
