# Historical strategy replay boundary.

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from fourfunds.models import Candle


@dataclass(frozen=True)
class BacktestResult:
    initial_value: Decimal
    final_value: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    daily_returns: tuple[Decimal, ...]
    fees_paid: Decimal
    active_trading_days: int


def run_backtest(
    candles: Mapping[str, Sequence[Candle]],
) -> BacktestResult:
    raise NotImplementedError
