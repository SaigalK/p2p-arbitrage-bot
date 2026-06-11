"""
Hot Coins Monitor
Динамічно визначає які монети зараз найактивніші.

Логіка:
1. Кожні 15 хв беремо топ монет за зміною ціни з Binance (24h)
2. Перевіряємо що монета є на Gate + OKX + MEXC
3. Повертаємо список: базові монети + гарячі монети
4. arb_monitor і ws_monitor використовують цей список

Запуск: import hot_coins → hot_coins.get_current_coins()
"""

import asyncio
import time
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ─── Налаштування ─────────────────────────────────────────────────────────────
BASE_COINS = ["SUI", "SEI", "APT", "TIA", "INJ", "ARB", "OP"]  # завжди моніторимо
MAX_TOTAL_COINS  = 20    # максимум монет одночасно
MIN_CHANGE_PCT   = 5.0   # мінімальна 24h зміна щоб потрапити в "гарячі" (%)
REFRESH_INTERVAL = 900   # оновлювати кожні 15 хв (сек)

# Стейблкоїни — виключаємо
STABLE_COINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD",
    "USDP", "PYUSD", "USDE", "USDD", "FRAX", "LUSD",
}

# ─── Кеш ──────────────────────────────────────────────────────────────────────
_cache: list[str] = list(BASE_COINS)
_last_refresh: float = 0.0
_lock = asyncio.Lock()


# ─── Перевірка наявності монети на біржах ─────────────────────────────────────
async def _check_gate(client: httpx.AsyncClient, coin: str) -> bool:
    try:
        r = await client.get(
            "https://api.gateio.ws/api/v4/spot/tickers",
            params={"currency_pair": f"{coin}_USDT"},
            timeout=4
        )
        data = r.json()
        return bool(data and float(data[0].get("last", 0)) > 0)
    except Exception:
        return False


async def _check_okx(client: httpx.AsyncClient, coin: str) -> bool:
    try:
        r = await client.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": f"{coin}-USDT"},
            timeout=4
        )
        data = r.json()
        return data.get("code") == "0" and bool(data.get("data"))
    except Exception:
        return False


async def _check_mexc(client: httpx.AsyncClient, coin: str) -> bool:
    try:
        r = await client.get(
            "https://api.mexc.com/api/v3/ticker/price",
            params={"symbol": f"{coin}USDT"},
            timeout=4
        )
        data = r.json()
        return float(data.get("price", 0)) > 0
    except Exception:
        return False


async def _coin_available_on_all(client: httpx.AsyncClient, coin: str) -> bool:
    """Перевіряє що монета є на Gate + OKX + MEXC."""
    results = await asyncio.gather(
        _check_gate(client, coin),
        _check_okx(client, coin),
        _check_mexc(client, coin),
        return_exceptions=True
    )
    return all(r is True for r in results)


# ─── Топ монети з Binance ──────────────────────────────────────────────────────
async def _fetch_top_movers(client: httpx.AsyncClient, top_n: int = 40) -> list[dict]:
    """Повертає топ монет за абсолютною зміною ціни за 24h."""
    try:
        r = await client.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=10
        )
        tickers = r.json()

        candidates = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            coin = symbol[:-4]  # прибрати "USDT"
            if coin in STABLE_COINS:
                continue
            if len(coin) > 8:  # фільтр токенів з довгими назвами
                continue

            change = abs(float(t.get("priceChangePercent", 0)))
            volume = float(t.get("quoteVolume", 0))

            if change >= MIN_CHANGE_PCT and volume >= 500_000:  # мін. обсяг $500K
                candidates.append({
                    "coin": coin,
                    "change": change,
                    "volume": volume,
                })

        # Сортуємо за зміною ціни
        candidates.sort(key=lambda x: x["change"], reverse=True)
        return candidates[:top_n]

    except Exception as e:
        logger.error(f"Binance top movers помилка: {e}")
        return []


# ─── Основна функція оновлення ────────────────────────────────────────────────
async def refresh(force: bool = False) -> list[str]:
    """
    Оновлює список гарячих монет.
    Повертає BASE_COINS + нові гарячі монети (що є на всіх 3 біржах).
    """
    global _cache, _last_refresh

    now = time.time()
    if not force and (now - _last_refresh) < REFRESH_INTERVAL:
        return _cache

    async with _lock:
        # Подвійна перевірка після acquire
        if not force and (time.time() - _last_refresh) < REFRESH_INTERVAL:
            return _cache

        logger.info("🔥 Оновлення гарячих монет...")

        async with httpx.AsyncClient() as client:
            # 1. Отримуємо топ-мувери з Binance
            movers = await _fetch_top_movers(client)

            if not movers:
                logger.warning("Не вдалось отримати топ-мувери, використовуємо BASE_COINS")
                _cache = list(BASE_COINS)
                _last_refresh = time.time()
                return _cache

            # 2. Перевіряємо наявність на всіх 3 біржах (паралельно, батчами)
            new_hot: list[str] = []
            batch_size = 10

            for i in range(0, len(movers), batch_size):
                batch = movers[i:i + batch_size]
                checks = await asyncio.gather(*[
                    _coin_available_on_all(client, m["coin"])
                    for m in batch
                ])
                for m, ok in zip(batch, checks):
                    coin = m["coin"]
                    if ok and coin not in BASE_COINS:
                        new_hot.append(coin)
                        logger.info(
                            f"  ✅ {coin}: {m['change']:+.1f}% | "
                            f"об'єм ${m['volume']:,.0f}"
                        )
                    # Зупиняємось якщо набрали достатньо
                    if len(new_hot) >= (MAX_TOTAL_COINS - len(BASE_COINS)):
                        break

                if len(new_hot) >= (MAX_TOTAL_COINS - len(BASE_COINS)):
                    break

        # 3. Збираємо фінальний список
        result = list(BASE_COINS) + new_hot
        added   = [c for c in new_hot if c not in _cache]
        removed = [c for c in _cache  if c not in result and c not in BASE_COINS]

        _cache        = result
        _last_refresh = time.time()

        logger.info(
            f"🔥 Гарячі монети оновлено: {len(result)} монет | "
            f"додано: {added} | прибрано: {removed}"
        )

        return _cache


def get_current_coins() -> list[str]:
    """Синхронний геттер поточного списку (для читання без await)."""
    return list(_cache)


def time_until_refresh() -> int:
    """Скільки секунд до наступного оновлення."""
    return max(0, int(REFRESH_INTERVAL - (time.time() - _last_refresh)))


# ─── Standalone тест ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def test():
        coins = await refresh(force=True)
        print(f"\n🔥 Поточний список ({len(coins)} монет):")
        for c in coins:
            marker = "📌 база" if c in BASE_COINS else "🔥 гарячий"
            print(f"  {c:<8} {marker}")

    asyncio.run(test())
