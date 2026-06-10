"""
Bybit P2P Collector
Endpoint: POST https://api2.bybit.com/fiat/otc/item/online
Верифіковано: 2026-06-08

ВАЖЛИВО:
- Використовуємо api2.bybit.com (НЕ api.bybit.com/v5 — вимагає API ключ)
- "side" передається як РЯДОК: "0" = продавці (ти купуєш), "1" = покупці (ти продаєш)
- Фільтруємо isOnline=true, щоб не брати офлайн-трейдерів
"""
import asyncio
import time
import httpx

from collectors.base import AbstractCollector, PriceSnapshot
from config import BYBIT_P2P_URL, HTTP_TIMEOUT_SEC, HTTP_HEADERS
from utils.logger import get_logger

logger = get_logger(__name__)

PLATFORM = "bybit_p2p"
SIZE = "10"
MIN_EXECUTE_RATE = 85   # мінімальний % виконання угод
MIN_AMOUNT_UAH   = 8200 # мінімальна сума угоди ~$200 (фільтр дрібних оголошень)


class BybitP2PCollector(AbstractCollector):

    async def fetch(
        self,
        asset: str = "USDT",
        fiat: str = "UAH",
    ) -> tuple[PriceSnapshot, PriceSnapshot]:
        """Повертає (ask_snapshot, bid_snapshot)."""
        ask_snap, bid_snap = await asyncio.gather(
            self._fetch_side(asset, fiat, side="0"),   # продавці
            self._fetch_side(asset, fiat, side="1"),   # покупці
        )
        return ask_snap, bid_snap

    async def _fetch_side(
        self,
        asset: str,
        fiat: str,
        side: str,   # "0" = продавці (BUY), "1" = покупці (SELL)
    ) -> PriceSnapshot:
        payload = {
            "tokenId": asset,
            "currencyId": fiat,
            "side": side,          # РЯДОК, не число
            "size": SIZE,
            "page": "1",
            "payment": [],
        }

        snap = PriceSnapshot(platform=PLATFORM, asset=asset, fiat=fiat)

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
                resp = await client.post(
                    BYBIT_P2P_URL,
                    json=payload,
                    headers=HTTP_HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()

            if data.get("ret_code") != 0:
                raise ValueError(f"Bybit API error: {data.get('ret_msg')}")

            result = data.get("result", {})
            items = result.get("items", [])
            total_count = result.get("count", 0)

            # Фільтруємо: онлайн + рейтинг + мінімальна сума (~$200)
            filtered = [
                item for item in items
                if item.get("isOnline", False)
                and item.get("recentExecuteRate", 0) >= MIN_EXECUTE_RATE
                and float(item.get("minAmount", 0)) >= MIN_AMOUNT_UAH
            ]
            if not filtered:
                # Якщо нічого — тільки онлайн + рейтинг (без фільтру суми)
                filtered = [
                    item for item in items
                    if item.get("isOnline", False)
                    and item.get("recentExecuteRate", 0) >= MIN_EXECUTE_RATE
                ] or items

            prices = sorted([float(item["price"]) for item in filtered])

            if side == "0":
                # Продавці — ask сторона, ASC
                snap.ask_price = prices[0] if prices else 0.0
                snap.ask_prices = prices
                snap.sellers_count = total_count
            else:
                # Покупці — bid сторона, DESC
                prices_desc = sorted(prices, reverse=True)
                snap.bid_price = prices_desc[0] if prices_desc else 0.0
                snap.bid_prices = prices_desc
                snap.buyers_count = total_count

            snap.timestamp = time.time()
            logger.debug(
                f"Bybit side={side} {asset}/{fiat}: "
                f"best={prices[0] if prices else 'N/A'}, "
                f"total={total_count}"
            )

        except httpx.TimeoutException:
            snap.error = "timeout"
            logger.warning(f"Bybit P2P timeout (side={side})")
        except httpx.HTTPStatusError as e:
            snap.error = f"http_{e.response.status_code}"
            logger.warning(f"Bybit P2P HTTP {e.response.status_code} (side={side})")
        except Exception as e:
            snap.error = str(e)
            logger.error(f"Bybit P2P error (side={side}): {e}")

        return snap
