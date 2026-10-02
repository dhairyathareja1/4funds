# Portfolio risk and order planning.

from collections.abc import Mapping, Sequence
from typing import Protocol

from fourfunds.models import (
    ExchangeRule,
    MarketQuote,
    OrderIntent,
    TargetWeight,
    WalletSnapshot,
)
from fourfunds.settings import Settings


class OrderPlanner(Protocol):
    def plan(
        self,
        targets: Sequence[TargetWeight],
        wallet: WalletSnapshot,
        quotes: Mapping[str, MarketQuote],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[OrderIntent]:
        ...


class RiskManager:
    def plan(
        self,
        targets: Sequence[TargetWeight],
        wallet: WalletSnapshot,
        quotes: Mapping[str, MarketQuote],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[OrderIntent]:
        raise NotImplementedError
