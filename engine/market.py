"""
Market Engine — всі ринкові розрахунки.
"""
import statistics
import time
from collectors.base import PriceSnapshot


def mid_price(snapshots: list[PriceSnapshot]) -> float:
    """Середньоринкова ціна — медіана ask цін топ-5 по всіх платформах."""
    all_asks = []
    for s in snapshots:
        if s.is_valid:
            all_asks.extend(s.ask_prices[:5])
    return round(statistics.median(all_asks), 4) if all_asks else 0.0


def spread_pct(ask: float, bid: float) -> float:
    """% різниця між ціною купівлі і продажу."""
    if ask <= 0:
        return 0.0
    return round(((bid - ask) / ask) * 100, 2)


def trend(
    current_mid: float,
    history: list[tuple[float, float]],   # [(timestamp, mid_price), ...]
    minutes: int = 60,
) -> tuple[float, str]:
    """
    Розраховує тренд за останні N хвилин.
    Повертає (% зміна, "up"/"down"/"flat").
    Якщо даних недостатньо — повертає (0.0, "flat").
    """
    cutoff = time.time() - (minutes * 60)
    past = [price for ts, price in history if ts >= cutoff]

    if not past:
        return 0.0, "flat"

    oldest = past[0]
    if oldest == 0:
        return 0.0, "flat"

    change = ((current_mid - oldest) / oldest) * 100

    if change > 0.1:
        direction = "up"
    elif change < -0.1:
        direction = "down"
    else:
        direction = "flat"

    return round(change, 2), direction


def volatility(
    history: list[tuple[float, float]],
    minutes: int = 60,
) -> float:
    """Різниця між макс. і мін. ціною за останні N хвилин у %."""
    cutoff = time.time() - (minutes * 60)
    prices = [price for ts, price in history if ts >= cutoff]

    if len(prices) < 2:
        return 0.0

    low = min(prices)
    return round(((max(prices) - low) / low) * 100, 2) if low > 0 else 0.0


def advice_price(
    snapshots: list[PriceSnapshot],
    side: str = "sell",
    offset_pct: float = 0.1,
) -> float:
    """
    Рекомендована ціна для виставлення оголошення.

    side="sell": ти продаєш USDT → конкуренти = продавці (ask_prices).
                 Стань на offset% дешевше за найдешевшого продавця.

    side="buy":  ти купуєш USDT (виставляєш оголошення покупця) →
                 конкуренти = покупці (bid_prices).
                 Стань на offset% щедріше за найкращого покупця.
    """
    if side == "sell":
        all_asks = [p for s in snapshots if s.ask_price > 0 for p in s.ask_prices[:3]]
        if not all_asks:
            return 0.0
        best = min(all_asks)
        return round(best - best * offset_pct / 100, 2)

    else:  # "buy"
        all_bids = [p for s in snapshots if s.bid_price > 0 for p in s.bid_prices[:3]]
        if not all_bids:
            return 0.0
        best = max(all_bids)
        return round(best + best * offset_pct / 100, 2)


def spike_detected(
    current_mid: float,
    history: list[tuple[float, float]],
    threshold_pct: float = 0.5,
    window_minutes: int = 10,
) -> tuple[bool, float]:
    """
    Визначає різку зміну ціни за останні N хвилин.
    Повертає (True/False, % зміни).
    """
    cutoff = time.time() - (window_minutes * 60)
    past = [(ts, p) for ts, p in history if ts >= cutoff]

    if not past:
        return False, 0.0

    oldest_price = past[0][1]
    if oldest_price == 0:
        return False, 0.0

    change = abs((current_mid - oldest_price) / oldest_price) * 100
    direction = "up" if current_mid > oldest_price else "down"

    return change >= threshold_pct, round(change, 2)


def sparkline(history: list[tuple[float, float]], minutes: int = 60) -> str:
    """ASCII sparkline для відображення тренду в /trend."""
    BLOCKS = "▁▂▃▄▅▆▇█"
    cutoff = time.time() - (minutes * 60)
    prices = [p for ts, p in history if ts >= cutoff]

    if len(prices) < 2:
        return "─ (недостатньо даних)"

    # Беремо 8 рівномірних точок
    step = max(1, len(prices) // 8)
    sampled = prices[::step][-8:]

    low, high = min(sampled), max(sampled)
    if high == low:
        return "─" * len(sampled)

    chars = [BLOCKS[int((p - low) / (high - low) * 7)] for p in sampled]
    return "".join(chars)
