# Market data storage interface.

from collections.abc import Sequence
from typing import Protocol

from fourfunds.models import Candle, MarketQuote


class MarketDataStore(Protocol):
    def record_snapshots(self, quotes: Sequence[MarketQuote]) -> None:
        ...

    def get_candles(
        self, pair: str, *, start_time_ms: int, end_time_ms: int
    ) -> Sequence[Candle]:
        ...
