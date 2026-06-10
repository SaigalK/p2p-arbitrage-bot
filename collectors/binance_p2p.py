"""
Binance P2P Collector
Endpoint: POST https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search
Верифіковано: 2026-06-08

tradeType="BUY"  → показує продавців USDT (ask сторона)
tradeType="SELL" → показує покупців  USDT (bid сторона)
"""
import asyncio
import time
import httpx

from collectors.base import AbstractCollector, PriceSnapshot
from config import BINANCE_P2P_URL, HTTP_TIMEOUT_SEC, HTTP_HEADERS
from utils.logger import get_logger

logger = get_logger(__name__)

PLATFORM = "binance_p2p"
ROWS = 10          # кількість оголошень в одному запиті
MIN_FINISH_RATE = 0.90  # мінімальний рейтинг завершення угод
MIN_AMOUNT_UAH  = 8200  # мінімальна сума угоди ~$200 (фільтр дрібних оголошень)

# Тільки українські банківські методи (без Binance Pay, SWIFT, крипто)
UA_PAYMENT_METHODS = [
    "Monobank", "PrivatBank", "BankTransfer",
    "A-Bank", "Pumb", "OtpBank", "Sportbank", "IziBank",
]


class BinanceP2PCollector(AbstractCollector):

    async def fetch(
        self,
        asset: str = "USDT",
        fiat: str = "UAH",
    ) -> tuple[PriceSnapshot, PriceSnapshot]:
        """Повертає (ask_snapshot, bid_snapshot)."""
        ask_snap, bid_snap = await asyncio.gather(
            self._fetch_side(asset, fiat, trade_type="BUY"),
            self._fetch_side(asset, fiat, trade_type="SELL"),
        )
        return ask_snap, bid_snap

    async def _fetch_side(
        self,
        asset: str,
        fiat: str,
        trade_type: str,   # "BUY" = продавці, "SELL" = покупці
    ) -> PriceSnapshot:
        payload = {
            "asset": asset,
            "fiat": fiat,
            "tradeType": trade_type,
            "page": 1,
            "rows": ROWS,
            "payTypes": UA_PAYMENT_METHODS,
            "merchantCheck": False,
            "publisherType": None,
        }

        snap = PriceSnapshot(platform=PLATFORM, asset=asset, fiat=fiat)

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
                resp = await client.post(
                    BINANCE_P2P_URL,
                    json=payload,
                    headers=HTTP_HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()

            if data.get("code") != "000000":
                raise ValueError(f"Binance API error code: {data.get('code')}")

            items = data.get("data", [])
            snap.sellers_count = data.get("total", 0)  # реальна загальна кількість

            # Фільтруємо: рейтинг + мін. сума + НЕ merchant
            prices = []
            for item in items:
                adv        = item["adv"]
                advertiser = item["advertiser"]
                rate       = advertiser.get("monthFinishRate", 0)
                min_amount = float(adv.get("minSingleTransAmount", 0))
                user_type  = advertiser.get("userType", "user")   # "merchant" або "user"
                is_merchant = user_type == "merchant"

                if rate >= MIN_FINISH_RATE and min_amount >= MIN_AMOUNT_UAH and not is_merchant:
                    prices.append(float(adv["price"]))

            if not prices:
                # Fallback: тільки рейтинг + не merchant
                prices = [
                    float(item["adv"]["price"]) for item in items
                    if item["advertiser"].get("monthFinishRate", 0) >= MIN_FINISH_RATE
                    and item["advertiser"].get("userType", "user") != "merchant"
                ] or [float(item["adv"]["price"]) for item in items]

            prices.sort()  # ASC завжди

            if trade_type == "BUY":
                # Продавці: ask_prices відсортовано ASC (найдешевший перший)
                snap.ask_price = prices[0] if prices else 0.0
                snap.ask_prices = prices
            else:
                # Покупці: bid_prices відсортовано DESC (найщедріший перший)
                prices_desc = sorted(prices, reverse=True)
                snap.bid_price = prices_desc[0] if prices_desc else 0.0
                snap.bid_prices = prices_desc
                snap.buyers_count = snap.sellers_count
                snap.sellers_count = 0

            snap.timestamp = time.time()
            logger.debug(
                f"Binance {trade_type} {asset}/{fiat}: "
                f"best={prices[0] if prices else 'N/A'}, "
                f"total={data.get('total', 0)}"
            )

        except httpx.TimeoutException:
            snap.error = "timeout"
            logger.warning(f"Binance P2P timeout ({trade_type})")
        except httpx.HTTPStatusError as e:
            snap.error = f"http_{e.response.status_code}"
            logger.warning(f"Binance P2P HTTP {e.response.status_code} ({trade_type})")
        except Exception as e:
            snap.error = str(e)
            logger.error(f"Binance P2P error ({trade_type}): {e}")

        return snap
