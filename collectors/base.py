from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import time


@dataclass
class PriceSnapshot:
    """
    Знімок цін з однієї платформи в один момент часу.

    ask_price  — найнижча ціна серед продавців (ти купуєш у них)
    bid_price  — найвища ціна серед покупців  (ти продаєш їм)
    ask_prices — список топ-N цін продавців, відсортований ASC
    bid_prices — список топ-N цін покупців,  відсортований DESC
    """
    platform: str
    asset: str                                         # "USDT", "TON"
    fiat: str                                          # "UAH"
    ask_price: float = 0.0
    bid_price: float = 0.0
    ask_prices: list[float] = field(default_factory=list)
    bid_prices: list[float] = field(default_factory=list)
    sellers_count: int = 0   # реальна кількість оголошень на ринку (з API total/count)
    buyers_count: int = 0
    timestamp: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and self.ask_price > 0


class AbstractCollector(ABC):
    """Базовий клас для всіх збирачів даних."""

    @abstractmethod
    async def fetch(self, asset: str, fiat: str) -> tuple[PriceSnapshot, PriceSnapshot]:
        """
        Повертає два знімки: (ask_snapshot, bid_snapshot).
        ask_snapshot — ціни продавців (tradeType=BUY з точки зору покупця).
        bid_snapshot — ціни покупців  (tradeType=SELL з точки зору покупця).
        """
        ...
