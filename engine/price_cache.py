"""
PriceCache — in-memory кеш останніх знімків по кожній платформі.
Команди /price, /market читають звідси — не роблять нові API запити.
"""
import asyncio
import time
from collectors.base import PriceSnapshot


class PriceCache:

    def __init__(self):
        # {"binance_p2p:USDT:ask": PriceSnapshot, ...}
        self._cache: dict[str, PriceSnapshot] = {}
        self._lock = asyncio.Lock()

    def _key(self, platform: str, asset: str, side: str) -> str:
        return f"{platform}:{asset}:{side}"

    async def set(self, snap: PriceSnapshot, side: str):
        """Зберегти знімок. side = "ask" або "bid"."""
        key = self._key(snap.platform, snap.asset, side)
        async with self._lock:
            self._cache[key] = snap

    async def get(self, platform: str, asset: str, side: str) -> PriceSnapshot | None:
        key = self._key(platform, asset, side)
        async with self._lock:
            return self._cache.get(key)

    async def get_all(self, asset: str) -> list[PriceSnapshot]:
        """Всі валідні ask-знімки для заданого активу."""
        async with self._lock:
            result = []
            for key, snap in self._cache.items():
                if f":{asset}:ask" in key and snap.is_valid:
                    result.append(snap)
            return result

    async def last_updated(self, asset: str) -> float:
        """Час останнього оновлення для активу."""
        snaps = await self.get_all(asset)
        if not snaps:
            return 0.0
        return max(s.timestamp for s in snaps)

    async def is_stale(self, asset: str, max_age_sec: int = 120) -> bool:
        """True якщо дані старші за max_age_sec секунд."""
        last = await self.last_updated(asset)
        return (time.time() - last) > max_age_sec


# Глобальний синглтон
price_cache = PriceCache()
