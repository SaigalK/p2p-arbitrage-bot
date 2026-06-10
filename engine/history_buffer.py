"""
HistoryBuffer — in-memory кільцевий буфер mid_price за останні 8 годин.
Заповнюється кожні 30 сек із scheduler loop.
Читається функціями trend(), volatility(), spike_detected().
"""
from collections import deque
import asyncio
import time

from config import HISTORY_HOURS, POLLING_INTERVAL_SEC
from utils.logger import get_logger

logger = get_logger(__name__)

# 8 год × 3600 сек / 30 сек = 960 точок
MAX_POINTS = (HISTORY_HOURS * 3600) // POLLING_INTERVAL_SEC


class HistoryBuffer:

    def __init__(self):
        # Окремий буфер для кожного активу: {"USDT": deque, ...}
        self._data: dict[str, deque] = {}
        self._lock = asyncio.Lock()

    async def append(self, asset: str, mid: float, ts: float | None = None):
        """Додати нову точку. Викликається після кожного циклу поллінгу."""
        async with self._lock:
            if asset not in self._data:
                self._data[asset] = deque(maxlen=MAX_POINTS)
            self._data[asset].append((ts or time.time(), mid))

    async def get(self, asset: str) -> list[tuple[float, float]]:
        """Повернути копію буфера [(timestamp, mid_price), ...] від старих до нових."""
        async with self._lock:
            return list(self._data.get(asset, []))

    async def has_enough_data(self, asset: str, minutes: int) -> bool:
        """Перевірити чи є хоча б одна точка старша за N хвилин."""
        data = await self.get(asset)
        if len(data) < 2:
            return False
        oldest_ts = data[0][0]
        return (time.time() - oldest_ts) >= (minutes * 60)

    async def restore_from_db(self, db, asset: str, hours: int = 8):
        """
        Завантажити historical дані з SQLite після рестарту бота.
        Без цього після рестарту треба чекати N хвилин перш ніж працюватиме trend().
        """
        from db.queries import get_recent_mid_prices
        rows = await get_recent_mid_prices(db, asset=asset, hours=hours)
        if not rows:
            return
        async with self._lock:
            if asset not in self._data:
                self._data[asset] = deque(maxlen=MAX_POINTS)
            for ts, mid in rows:
                self._data[asset].append((ts, mid))
        logger.info(f"HistoryBuffer відновлено: {len(rows)} точок для {asset}")


# Глобальний синглтон — імпортується в main.py і engine
history_buffer = HistoryBuffer()
