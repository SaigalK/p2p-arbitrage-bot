"""
AlertManager — cooldown між алертами, дедуплікація після рестарту.
"""
import asyncio
import time
from utils.logger import get_logger

logger = get_logger(__name__)


class AlertManager:

    def __init__(self):
        # key = "telegram_id:alert_type:asset" → unix timestamp останнього алерту
        self._last_alert: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _key(self, telegram_id: int, alert_type: str, asset: str) -> str:
        return f"{telegram_id}:{alert_type}:{asset}"

    async def restore_from_db(self, db, cooldown_sec: int = 900):
        """
        Відновити стан cooldown після рестарту.
        Без цього після рестарту надішлються дублікати всім підписникам.
        """
        from db.queries import fetch_recent_alerts
        cutoff = time.time() - cooldown_sec
        rows = await fetch_recent_alerts(db, since_ts=cutoff)
        async with self._lock:
            for row in rows:
                key = self._key(row["telegram_id"], row["alert_type"], row["asset"])
                self._last_alert[key] = max(
                    self._last_alert.get(key, 0),
                    float(row["sent_at_ts"]),
                )
        logger.info(f"AlertManager відновлено: {len(rows)} записів")

    async def should_send(
        self,
        telegram_id: int,
        alert_type: str,
        asset: str,
        cooldown_sec: int = 900,
    ) -> bool:
        key = self._key(telegram_id, alert_type, asset)
        async with self._lock:
            last = self._last_alert.get(key, 0)
            return (time.time() - last) >= cooldown_sec

    async def mark_sent(self, telegram_id: int, alert_type: str, asset: str):
        key = self._key(telegram_id, alert_type, asset)
        async with self._lock:
            self._last_alert[key] = time.time()


# Глобальний синглтон
alert_manager = AlertManager()
