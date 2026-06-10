"""
Тести для engine/market.py
Запуск: pytest tests/test_market.py -v
"""
import time
import pytest

from collectors.base import PriceSnapshot
from engine.market import (
    mid_price,
    spread_pct,
    trend,
    volatility,
    advice_price,
    spike_detected,
    sparkline,
)


# ─── Фікстури ─────────────────────────────────────────────────────────────────

def make_snap(
    platform="test",
    asset="USDT",
    fiat="UAH",
    ask_prices=None,
    bid_prices=None,
    error=None,
) -> PriceSnapshot:
    ask_prices = ask_prices or []
    bid_prices = bid_prices or []
    snap = PriceSnapshot(
        platform=platform,
        asset=asset,
        fiat=fiat,
        ask_prices=sorted(ask_prices),
        bid_prices=sorted(bid_prices, reverse=True),
        error=error,
    )
    if ask_prices:
        snap.ask_price = sorted(ask_prices)[0]
    if bid_prices:
        snap.bid_price = sorted(bid_prices, reverse=True)[0]
    return snap


def make_history(prices: list[float], interval_sec: int = 30) -> list[tuple[float, float]]:
    """Генерує [(timestamp, price), ...] від минулого до зараз."""
    now = time.time()
    n = len(prices)
    return [
        (now - (n - i) * interval_sec, p)
        for i, p in enumerate(prices)
    ]


# ─── mid_price ────────────────────────────────────────────────────────────────

class TestMidPrice:

    def test_single_snapshot_returns_median_of_asks(self):
        snap = make_snap(ask_prices=[40.0, 40.5, 41.0, 41.5, 42.0])
        result = mid_price([snap])
        assert result == 41.0  # медіана 5 цін

    def test_two_snapshots_uses_all_top5_asks(self):
        s1 = make_snap(ask_prices=[40.0, 40.2, 40.4, 40.6, 40.8])
        s2 = make_snap(ask_prices=[41.0, 41.2, 41.4, 41.6, 41.8])
        result = mid_price([s1, s2])
        # 10 значень: [40.0, 40.2, 40.4, 40.6, 40.8, 41.0, 41.2, 41.4, 41.6, 41.8]
        # медіана = (40.8 + 41.0) / 2 = 40.9
        assert result == 40.9

    def test_invalid_snapshot_ignored(self):
        valid = make_snap(ask_prices=[41.0, 42.0, 43.0])
        invalid = make_snap(error="timeout")
        result = mid_price([valid, invalid])
        assert result == 42.0  # медіана [41, 42, 43] = 42

    def test_empty_list_returns_zero(self):
        assert mid_price([]) == 0.0

    def test_only_invalid_snapshots_returns_zero(self):
        snap = make_snap(error="http_429")
        assert mid_price([snap]) == 0.0

    def test_uses_only_top_5_per_snapshot(self):
        # 8 цін, але top-5 = [40.0, 40.1, 40.2, 40.3, 40.4]
        snap = make_snap(ask_prices=[40.0, 40.1, 40.2, 40.3, 40.4, 50.0, 51.0, 52.0])
        result = mid_price([snap])
        # медіана [40.0, 40.1, 40.2, 40.3, 40.4] = 40.2
        assert result == 40.2


# ─── spread_pct ───────────────────────────────────────────────────────────────

class TestSpreadPct:

    def test_normal_spread(self):
        # ask=40, bid=42 → spread=(42-40)/40*100=5%
        assert spread_pct(40.0, 42.0) == 5.0

    def test_zero_ask_returns_zero(self):
        assert spread_pct(0.0, 42.0) == 0.0

    def test_negative_spread(self):
        # bid < ask → від'ємний спред
        result = spread_pct(42.0, 40.0)
        assert result < 0


# ─── trend ────────────────────────────────────────────────────────────────────

class TestTrend:

    def test_uptrend(self):
        history = make_history([40.0, 40.1, 40.2, 40.3, 40.5])
        pct, direction = trend(40.5, history, minutes=60)
        assert direction == "up"
        assert pct > 0

    def test_downtrend(self):
        history = make_history([42.0, 41.8, 41.5, 41.2, 41.0])
        pct, direction = trend(41.0, history, minutes=60)
        assert direction == "down"
        assert pct < 0

    def test_flat_within_threshold(self):
        # Зміна 0.05% < 0.1% порогу → flat
        base = 40.0
        history = make_history([base] * 5)
        slightly_up = base * 1.0005  # +0.05%
        pct, direction = trend(slightly_up, history, minutes=60)
        assert direction == "flat"

    def test_empty_history_returns_flat(self):
        pct, direction = trend(40.0, [], minutes=60)
        assert pct == 0.0
        assert direction == "flat"

    def test_no_data_in_window(self):
        # Всі точки старші за window
        old_ts = time.time() - 120 * 60  # 2 год тому
        history = [(old_ts, 40.0), (old_ts + 1, 40.5)]
        pct, direction = trend(41.0, history, minutes=30)
        # Точки поза вікном → flat
        assert pct == 0.0
        assert direction == "flat"

    def test_percentage_calculation(self):
        # 40 → 41 = +2.5%
        history = make_history([40.0] * 10)
        pct, direction = trend(41.0, history, minutes=60)
        assert direction == "up"
        assert abs(pct - 2.5) < 0.01


# ─── volatility ───────────────────────────────────────────────────────────────

class TestVolatility:

    def test_stable_market(self):
        history = make_history([40.0] * 20)
        result = volatility(history, minutes=60)
        assert result == 0.0

    def test_volatile_market(self):
        # min=40, max=42 → (42-40)/40*100 = 5%
        prices = [40.0, 41.0, 42.0, 41.5, 40.5]
        history = make_history(prices)
        result = volatility(history, minutes=60)
        assert result == 5.0

    def test_insufficient_data(self):
        history = make_history([40.0])
        result = volatility(history, minutes=60)
        assert result == 0.0

    def test_only_recent_window_used(self):
        now = time.time()
        # Старе значення 2 год тому
        old = (now - 7200, 30.0)
        # Нові значення в межах 60 хв
        recent = [(now - 600 + i * 60, 40.0 + i * 0.1) for i in range(5)]
        history = [old] + recent
        result = volatility(history, minutes=60)
        # 30.0 не повинен потрапити у вікно → vol тільки на [40.0-40.4]
        expected = round(((40.4 - 40.0) / 40.0) * 100, 2)
        assert result == expected


# ─── advice_price ─────────────────────────────────────────────────────────────

class TestAdvicePrice:

    def test_sell_side_undercuts_cheapest(self):
        # Конкуренти продають за 40.00, 40.50, 41.00
        snap = make_snap(ask_prices=[40.0, 40.5, 41.0])
        result = advice_price([snap], side="sell", offset_pct=0.1)
        # min=40.0 - 0.1% = 39.96
        assert result == round(40.0 - 40.0 * 0.1 / 100, 2)
        assert result < 40.0

    def test_buy_side_outbids_best_buyer(self):
        snap = make_snap(bid_prices=[39.0, 38.5, 38.0])
        result = advice_price([snap], side="buy", offset_pct=0.1)
        # max=39.0 + 0.1% = 39.039
        expected = round(39.0 + 39.0 * 0.1 / 100, 2)
        assert result == expected
        assert result > 39.0

    def test_sell_no_data_returns_zero(self):
        snap = make_snap()  # порожній снапшот
        result = advice_price([snap], side="sell")
        assert result == 0.0

    def test_buy_no_data_returns_zero(self):
        snap = make_snap()
        result = advice_price([snap], side="buy")
        assert result == 0.0

    def test_uses_only_top3_prices(self):
        # ask_prices=[40, 41, 42, 100, 200] → top-3=[40,41,42] → min=40
        snap = make_snap(ask_prices=[40.0, 41.0, 42.0, 100.0, 200.0])
        result = advice_price([snap], side="sell", offset_pct=0.0)
        assert result == 40.0  # min(top-3) без зміщення

    def test_multiple_snapshots_sell(self):
        s1 = make_snap(ask_prices=[41.0, 42.0, 43.0])
        s2 = make_snap(ask_prices=[39.0, 40.0, 41.0])
        result = advice_price([s1, s2], side="sell", offset_pct=0.0)
        # min з усіх top-3 = 39.0
        assert result == 39.0


# ─── spike_detected ───────────────────────────────────────────────────────────

class TestSpikeDetected:

    def test_spike_up_detected(self):
        # base=40.0, current=40.3 → +0.75% > 0.5%
        history = make_history([40.0] * 5, interval_sec=120)  # кожні 2 хв
        detected, pct = spike_detected(40.3, history, threshold_pct=0.5, window_minutes=10)
        assert detected is True
        assert pct > 0.5

    def test_spike_down_detected(self):
        history = make_history([40.0] * 5, interval_sec=120)
        detected, pct = spike_detected(39.7, history, threshold_pct=0.5, window_minutes=10)
        assert detected is True
        assert pct > 0.5

    def test_no_spike_within_threshold(self):
        # Зміна 0.2% < 0.5%
        history = make_history([40.0] * 5, interval_sec=120)
        detected, pct = spike_detected(40.08, history, threshold_pct=0.5, window_minutes=10)
        assert detected is False

    def test_empty_history(self):
        detected, pct = spike_detected(40.0, [], threshold_pct=0.5, window_minutes=10)
        assert detected is False
        assert pct == 0.0

    def test_no_data_in_window(self):
        # Всі точки старші за window_minutes
        old_ts = time.time() - 20 * 60
        history = [(old_ts, 38.0)]
        detected, pct = spike_detected(40.0, history, threshold_pct=0.5, window_minutes=10)
        assert detected is False


# ─── sparkline ────────────────────────────────────────────────────────────────

class TestSparkline:

    def test_returns_string(self):
        history = make_history([40.0 + i * 0.1 for i in range(20)])
        result = sparkline(history, minutes=60)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ascending_prices_end_with_high_block(self):
        prices = [40.0, 40.1, 40.2, 40.3, 40.4, 40.5, 40.6, 40.7]
        history = make_history(prices)
        result = sparkline(history, minutes=60)
        # Останній символ має бути найвищим блоком
        BLOCKS = "▁▂▃▄▅▆▇█"
        assert result[-1] == BLOCKS[-1]

    def test_flat_prices_returns_dashes(self):
        history = make_history([40.0] * 10)
        result = sparkline(history, minutes=60)
        assert all(c == "─" for c in result)

    def test_insufficient_data(self):
        history = make_history([40.0])
        result = sparkline(history, minutes=60)
        assert "недостатньо" in result
