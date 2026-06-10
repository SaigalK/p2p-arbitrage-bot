"""
Тести для collectors/binance_p2p.py та collectors/bybit_p2p.py
Використовуємо respx для мокання HTTP-запитів.
Запуск: pytest tests/test_collectors.py -v
"""
import pytest
import respx
import httpx

from collectors.binance_p2p import BinanceP2PCollector
from collectors.bybit_p2p import BybitP2PCollector
from config import BINANCE_P2P_URL, BYBIT_P2P_URL


# ─── Binance P2P ──────────────────────────────────────────────────────────────

BINANCE_RESPONSE_BUY = {
    "code": "000000",
    "total": 459,
    "data": [
        {
            "adv": {"price": "40.22"},
            "advertiser": {"monthFinishRate": 0.98},
        },
        {
            "adv": {"price": "40.50"},
            "advertiser": {"monthFinishRate": 0.95},
        },
        {
            "adv": {"price": "40.80"},
            "advertiser": {"monthFinishRate": 0.92},
        },
        {
            "adv": {"price": "44.67"},
            "advertiser": {"monthFinishRate": 0.85},  # нижче порогу 0.90
        },
    ],
}

BINANCE_RESPONSE_SELL = {
    "code": "000000",
    "total": 312,
    "data": [
        {
            "adv": {"price": "39.50"},
            "advertiser": {"monthFinishRate": 0.96},
        },
        {
            "adv": {"price": "39.80"},
            "advertiser": {"monthFinishRate": 0.91},
        },
    ],
}


class TestBinanceP2PCollector:

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_returns_ask_and_bid(self):
        """fetch() повертає кортеж (ask_snap, bid_snap)."""
        respx.post(BINANCE_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BINANCE_RESPONSE_BUY),
                httpx.Response(200, json=BINANCE_RESPONSE_SELL),
            ]
        )
        collector = BinanceP2PCollector()
        ask, bid = await collector.fetch("USDT", "UAH")

        assert ask is not None
        assert bid is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_ask_snapshot_has_correct_price(self):
        """ask_price = найнижча ціна серед відфільтрованих продавців."""
        respx.post(BINANCE_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BINANCE_RESPONSE_BUY),
                httpx.Response(200, json=BINANCE_RESPONSE_SELL),
            ]
        )
        collector = BinanceP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        # Фільтр: monthFinishRate >= 0.90 → [40.22, 40.50, 40.80] (44.67 відфільтровано)
        assert ask.ask_price == 40.22
        assert ask.is_valid is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_ask_sellers_count_from_total(self):
        """sellers_count береться з total, не len(data)."""
        respx.post(BINANCE_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BINANCE_RESPONSE_BUY),
                httpx.Response(200, json=BINANCE_RESPONSE_SELL),
            ]
        )
        collector = BinanceP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.sellers_count == 459  # не 4 (len(data))

    @pytest.mark.asyncio
    @respx.mock
    async def test_bid_snapshot(self):
        """bid_price = найвища ціна серед покупців (DESC)."""
        respx.post(BINANCE_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BINANCE_RESPONSE_BUY),
                httpx.Response(200, json=BINANCE_RESPONSE_SELL),
            ]
        )
        collector = BinanceP2PCollector()
        _, bid = await collector.fetch("USDT", "UAH")

        # Покупці: [39.50, 39.80] sorted DESC → 39.80 найщедріший
        assert bid.bid_price == 39.80
        assert bid.bid_prices[0] == 39.80  # DESC → перший найвищий

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_error_returns_error_snap(self):
        """При HTTP 429 snap.error встановлюється."""
        respx.post(BINANCE_P2P_URL).mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )
        collector = BinanceP2PCollector()
        ask, bid = await collector.fetch("USDT", "UAH")

        assert ask.error is not None
        assert ask.is_valid is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_error_snap(self):
        """При таймауті snap.error = 'timeout'."""
        respx.post(BINANCE_P2P_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        collector = BinanceP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.error == "timeout"
        assert ask.is_valid is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_code_returns_error_snap(self):
        """При code != 000000 snap.error встановлюється."""
        error_response = {"code": "400001", "message": "Invalid parameter"}
        respx.post(BINANCE_P2P_URL).mock(
            return_value=httpx.Response(200, json=error_response)
        )
        collector = BinanceP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.error is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_filtered_out_uses_fallback(self):
        """Якщо всі відфільтровані → використовуємо без фільтру."""
        low_rate_response = {
            "code": "000000",
            "total": 10,
            "data": [
                {
                    "adv": {"price": "40.00"},
                    "advertiser": {"monthFinishRate": 0.80},  # нижче 0.90
                },
            ],
        }
        respx.post(BINANCE_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=low_rate_response),
                httpx.Response(200, json=BINANCE_RESPONSE_SELL),
            ]
        )
        collector = BinanceP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        # Fallback: беремо без фільтру
        assert ask.ask_price == 40.00
        assert ask.is_valid is True


# ─── Bybit P2P ────────────────────────────────────────────────────────────────

BYBIT_RESPONSE_SELLERS = {
    "ret_code": 0,
    "ret_msg": "OK",
    "result": {
        "count": 534,
        "items": [
            {
                "price": "45.16",
                "isOnline": True,
                "recentExecuteRate": 95,
            },
            {
                "price": "45.30",
                "isOnline": True,
                "recentExecuteRate": 88,
            },
            {
                "price": "45.50",
                "isOnline": False,   # офлайн — відфільтровуємо
                "recentExecuteRate": 98,
            },
            {
                "price": "45.80",
                "isOnline": True,
                "recentExecuteRate": 70,   # нижче MIN_EXECUTE_RATE=85
            },
        ],
    },
}

BYBIT_RESPONSE_BUYERS = {
    "ret_code": 0,
    "ret_msg": "OK",
    "result": {
        "count": 210,
        "items": [
            {
                "price": "44.80",
                "isOnline": True,
                "recentExecuteRate": 90,
            },
            {
                "price": "44.50",
                "isOnline": True,
                "recentExecuteRate": 92,
            },
        ],
    },
}


class TestBybitP2PCollector:

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_returns_ask_and_bid(self):
        """fetch() повертає кортеж (ask_snap, bid_snap)."""
        respx.post(BYBIT_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BYBIT_RESPONSE_SELLERS),
                httpx.Response(200, json=BYBIT_RESPONSE_BUYERS),
            ]
        )
        collector = BybitP2PCollector()
        ask, bid = await collector.fetch("USDT", "UAH")

        assert ask is not None
        assert bid is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_offline_sellers_filtered(self):
        """Офлайн-продавці виключаються з результату."""
        respx.post(BYBIT_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BYBIT_RESPONSE_SELLERS),
                httpx.Response(200, json=BYBIT_RESPONSE_BUYERS),
            ]
        )
        collector = BybitP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        # 45.50 (offline) і 45.80 (низький рейтинг) виключені → мін з [45.16, 45.30]
        assert ask.ask_price == 45.16
        assert 45.50 not in ask.ask_prices

    @pytest.mark.asyncio
    @respx.mock
    async def test_sellers_count_from_count_field(self):
        """sellers_count з result.count, не len(items)."""
        respx.post(BYBIT_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BYBIT_RESPONSE_SELLERS),
                httpx.Response(200, json=BYBIT_RESPONSE_BUYERS),
            ]
        )
        collector = BybitP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.sellers_count == 534  # не 4 (len items)

    @pytest.mark.asyncio
    @respx.mock
    async def test_bid_price_is_highest_buyer(self):
        """bid_price = найвища ціна серед покупців."""
        respx.post(BYBIT_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=BYBIT_RESPONSE_SELLERS),
                httpx.Response(200, json=BYBIT_RESPONSE_BUYERS),
            ]
        )
        collector = BybitP2PCollector()
        _, bid = await collector.fetch("USDT", "UAH")

        assert bid.bid_price == 44.80  # max([44.80, 44.50])

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_code_returns_error_snap(self):
        """При ret_code != 0 snap.error встановлюється."""
        error_response = {"ret_code": 10001, "ret_msg": "params error"}
        respx.post(BYBIT_P2P_URL).mock(
            return_value=httpx.Response(200, json=error_response)
        )
        collector = BybitP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.error is not None
        assert ask.is_valid is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_error_snap(self):
        """При таймауті snap.error = 'timeout'."""
        respx.post(BYBIT_P2P_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        collector = BybitP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        assert ask.error == "timeout"

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_offline_uses_fallback(self):
        """Якщо всі офлайн → fallback без фільтру."""
        all_offline = {
            "ret_code": 0,
            "ret_msg": "OK",
            "result": {
                "count": 5,
                "items": [
                    {"price": "45.00", "isOnline": False, "recentExecuteRate": 99},
                ],
            },
        }
        respx.post(BYBIT_P2P_URL).mock(
            side_effect=[
                httpx.Response(200, json=all_offline),
                httpx.Response(200, json=BYBIT_RESPONSE_BUYERS),
            ]
        )
        collector = BybitP2PCollector()
        ask, _ = await collector.fetch("USDT", "UAH")

        # Fallback без фільтру
        assert ask.ask_price == 45.00
        assert ask.is_valid is True


# ─── PriceSnapshot properties ─────────────────────────────────────────────────

class TestPriceSnapshot:

    def test_is_valid_true_when_no_error_and_ask_price_positive(self):
        from collectors.base import PriceSnapshot
        snap = PriceSnapshot(
            platform="test", asset="USDT", fiat="UAH",
            ask_price=40.0, error=None
        )
        assert snap.is_valid is True

    def test_is_valid_false_when_error(self):
        from collectors.base import PriceSnapshot
        snap = PriceSnapshot(
            platform="test", asset="USDT", fiat="UAH",
            ask_price=40.0, error="timeout"
        )
        assert snap.is_valid is False

    def test_is_valid_false_when_ask_price_zero(self):
        from collectors.base import PriceSnapshot
        snap = PriceSnapshot(
            platform="test", asset="USDT", fiat="UAH",
            ask_price=0.0, error=None
        )
        assert snap.is_valid is False

    def test_mutable_defaults_are_independent(self):
        """Критичний тест: дефолтні списки не мають бути спільними між інстанціями."""
        from collectors.base import PriceSnapshot
        s1 = PriceSnapshot(platform="test", asset="USDT", fiat="UAH")
        s2 = PriceSnapshot(platform="test", asset="USDT", fiat="UAH")
        s1.ask_prices.append(40.0)
        assert s2.ask_prices == []  # s2 не змінюється
