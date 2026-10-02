# Baseline strategy interface.

from collections.abc import Mapping, Sequence
from typing import Protocol

from fourfunds.models import Candle, ExchangeRule, MarketQuote, TargetWeight
from fourfunds.settings import Settings


class Strategy(Protocol):
    def propose_targets(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[TargetWeight]:
        ...


class BaselineStrategy:
    def propose_targets(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[TargetWeight]:
        raise NotImplementedError
