"""
НБУ Collector — офіційний курс НБУ як fallback.
Оновлюється раз на день. Використовується тільки якщо обидва P2P API недоступні.
"""
import time
import httpx

from config import NBU_URL, HTTP_TIMEOUT_SEC
from utils.logger import get_logger

logger = get_logger(__name__)

_cache: dict = {"rate": 0.0, "fetched_at": 0.0}
CACHE_TTL_SEC = 3600   # 1 година


async def get_usd_uah_rate() -> float:
    """
    Повертає офіційний курс USD/UAH від НБУ.
    Кешується на 1 годину — НБУ оновлює раз на день.
    """
    now = time.time()
    if _cache["rate"] > 0 and (now - _cache["fetched_at"]) < CACHE_TTL_SEC:
        return _cache["rate"]

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            resp = await client.get(NBU_URL)
            resp.raise_for_status()
            data = resp.json()

        rate = float(data[0]["rate"])
        _cache["rate"] = rate
        _cache["fetched_at"] = now
        logger.info(f"НБУ курс оновлено: {rate} UAH/USD")
        return rate

    except Exception as e:
        logger.error(f"НБУ API error: {e}")
        return _cache["rate"]   # повернути старе значення якщо є
