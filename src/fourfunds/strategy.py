# Deterministic hourly trend strategy.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from typing import Protocol

from fourfunds.models import Candle, ExchangeRule, MarketQuote, TargetWeight
from fourfunds.settings import Settings

HOUR_MS = 60 * 60 * 1000
ZERO = Decimal("0")
ONE = Decimal("1")
PERCENT_SCALE = Decimal("100")


class Strategy(Protocol):
    def decide(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> StrategyDecision:
        ...

    def propose_targets(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[TargetWeight]:
        ...


@dataclass(frozen=True)
class StrategySignal:
    pair: str
    qualifies: bool
    momentum: Decimal | None
    current_price: Decimal | None
    trend_average: Decimal | None
    rank: int | None
    reason: str


@dataclass(frozen=True)
class StrategyDecision:
    signals: tuple[StrategySignal, ...]
    targets: tuple[TargetWeight, ...]


class BaselineStrategy:
    def propose_targets(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> Sequence[TargetWeight]:
        return self.decide(quotes, candles, rules, settings).targets

    def decide(
        self,
        quotes: Mapping[str, MarketQuote],
        candles: Mapping[str, Sequence[Candle]],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
    ) -> StrategyDecision:
        _validate_settings(settings)
        pairs = sorted(set(quotes) | set(candles) | set(rules))
        signals: list[_Signal] = []
        rejections: dict[str, str] = {}

        for pair in pairs:
            result = _evaluate_pair(pair, quotes, candles, rules, settings)
            if isinstance(result, str):
                rejections[pair] = result
            else:
                signals.append(result)

        if not signals:
            return StrategyDecision(
                signals=tuple(
                    StrategySignal(
                        pair=pair,
                        qualifies=False,
                        momentum=None,
                        current_price=None,
                        trend_average=None,
                        rank=None,
                        reason=reason,
                    )
                    for pair, reason in sorted(rejections.items())
                ),
                targets=(
                    TargetWeight(
                        pair=None,
                        weight=ONE,
                        reason=_cash_reason(rejections),
                    ),
                ),
            )

        ranked_signals = sorted(
            signals, key=lambda signal: (-signal.momentum, signal.pair)
        )
        selected = ranked_signals[: settings.top_k]
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            weight = ONE / Decimal(len(selected))
        targets = tuple(
            TargetWeight(
                pair=signal.pair,
                weight=weight,
                reason=_target_reason(
                    signal, rank, len(ranked_signals), settings
                ),
            )
            for rank, signal in enumerate(selected, start=1)
        )
        decision_signals = tuple(
            StrategySignal(
                pair=signal.pair,
                qualifies=True,
                momentum=signal.momentum,
                current_price=signal.current_price,
                trend_average=signal.trend_average,
                rank=rank,
                reason=_target_reason(
                    signal, rank, len(ranked_signals), settings
                ),
            )
            for rank, signal in enumerate(ranked_signals, start=1)
        )
        decision_signals += tuple(
            StrategySignal(
                pair=pair,
                qualifies=False,
                momentum=None,
                current_price=None,
                trend_average=None,
                rank=None,
                reason=reason,
            )
            for pair, reason in sorted(rejections.items())
        )
        return StrategyDecision(signals=decision_signals, targets=targets)


@dataclass(frozen=True)
class _Signal:
    pair: str
    momentum: Decimal
    current_price: Decimal
    trend_average: Decimal


def _evaluate_pair(
    pair: str,
    quotes: Mapping[str, MarketQuote],
    candles: Mapping[str, Sequence[Candle]],
    rules: Mapping[str, ExchangeRule],
    settings: Settings,
) -> _Signal | str:
    rule = rules.get(pair)
    if rule is None or rule.pair != pair:
        return "missing or mismatched exchange rule"
    if not rule.can_trade:
        return "exchange marks the pair as non-tradable"

    quote = quotes.get(pair)
    if quote is None or quote.pair != pair or quote.server_time_ms <= 0:
        return "missing or invalid current quote"
    if not _is_positive_decimal(quote.last):
        return "current quote price is not positive and finite"
    turnover = quote.quote_turnover_24h
    if not isinstance(turnover, Decimal) or not turnover.is_finite():
        return "24-hour quote turnover is not finite"
    if turnover < settings.minimum_quote_turnover_24h:
        return (
            f"24-hour quote turnover {turnover} is below the minimum "
            f"{settings.minimum_quote_turnover_24h}"
        )

    pair_candles = candles.get(pair)
    if not pair_candles:
        return "no hourly candles are available"
    required_bars = max(
        settings.momentum_lookback_hours + 1,
        settings.trend_lookback_hours,
    )
    recent_candles = _recent_hourly_candles(pair, pair_candles, required_bars)
    if isinstance(recent_candles, str):
        return recent_candles
    if quote.server_time_ms < recent_candles[-1].close_time_ms:
        return "current quote predates the latest hourly candle"

    closes = tuple(candle.close for candle in recent_candles)
    current_price = quote.last
    momentum_base = closes[-settings.momentum_lookback_hours - 1]
    momentum = current_price / momentum_base - ONE
    trend_closes = closes[-settings.trend_lookback_hours :]
    trend_average = sum(trend_closes, ZERO) / Decimal(len(trend_closes))

    if momentum <= ZERO:
        momentum_percent = momentum * PERCENT_SCALE
        return (
            f"{settings.momentum_lookback_hours}-hour momentum "
            f"({momentum_percent}%) is not positive"
        )
    if current_price <= trend_average:
        return (
            f"current price {current_price} is not above the "
            f"{settings.trend_lookback_hours}-hour close average {trend_average}"
        )
    return _Signal(pair, momentum, current_price, trend_average)


def _recent_hourly_candles(
    pair: str, candles: Sequence[Candle], required_bars: int
) -> tuple[Candle, ...] | str:
    if len(candles) < required_bars:
        return (
            f"insufficient hourly history: {len(candles)} bars available, "
            f"{required_bars} contiguous bars required"
        )

    ordered = sorted(candles, key=lambda candle: candle.open_time_ms)
    recent = tuple(ordered[-required_bars:])
    for candle in recent:
        if (
            candle.pair != pair
            or candle.open_time_ms <= 0
            or candle.open_time_ms % HOUR_MS != 0
            or candle.close_time_ms != candle.open_time_ms + HOUR_MS
            or not _is_positive_decimal(candle.close)
        ):
            return "hourly history contains an invalid candle"

    for previous, current in zip(recent, recent[1:]):
        if current.open_time_ms - previous.open_time_ms != HOUR_MS:
            return "hourly history is not contiguous"
    return recent


def _is_positive_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > ZERO


def _validate_settings(settings: Settings) -> None:
    for name, value, minimum in (
        ("Momentum lookback", settings.momentum_lookback_hours, 1),
        ("Trend lookback", settings.trend_lookback_hours, 2),
        ("Top-k", settings.top_k, 1),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{name} must be an integer of at least {minimum}.")

    minimum_turnover = settings.minimum_quote_turnover_24h
    if (
        not isinstance(minimum_turnover, Decimal)
        or not minimum_turnover.is_finite()
        or minimum_turnover < ZERO
    ):
        raise ValueError(
            "Minimum quote turnover must be a finite non-negative decimal."
        )


def _cash_reason(rejections: Mapping[str, str]) -> str:
    if not rejections:
        return "No market pairs were supplied; hold 100% cash."
    details = "; ".join(
        f"{pair}: {reason}" for pair, reason in sorted(rejections.items())
    )
    return f"No pair qualified; hold 100% cash. {details}."


def _target_reason(
    signal: _Signal,
    rank: int,
    qualifying_count: int,
    settings: Settings,
) -> str:
    momentum_percent = signal.momentum * PERCENT_SCALE
    return (
        f"{settings.momentum_lookback_hours}-hour momentum is "
        f"{momentum_percent}% and current price {signal.current_price} is above "
        f"the {settings.trend_lookback_hours}-hour close average "
        f"{signal.trend_average}; ranked {rank} of {qualifying_count}."
    )
