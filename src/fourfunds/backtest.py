from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from fourfunds.models import Candle, ExchangeRule, MarketQuote, TargetWeight
from fourfunds.settings import DEFAULT_BASE_URL, RunMode, Settings
from fourfunds.strategy import BaselineStrategy

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
INITIAL_VALUE = Decimal("10000")
ZERO = Decimal("0")
ONE = Decimal("1")
PRICE_PRECISION = 8


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
    *,
    initial_value: Decimal = INITIAL_VALUE,
    settings: Settings | None = None,
) -> BacktestResult:
    _validate_initial_value(initial_value)
    markets, timestamps = _normalize_candles(candles)
    quote_currencies = {_quote_currency(pair) for pair in markets}
    if len(quote_currencies) != 1:
        raise ValueError("All backtest pairs must use the same quote currency.")

    run_settings = settings or Settings(
        mode=RunMode.READ_ONLY,
        base_url=DEFAULT_BASE_URL,
    )
    _validate_fee_rate(run_settings.fee_rate)

    strategy = BaselineStrategy()
    positions: dict[str, Decimal] = {}
    cash = initial_value
    pending_targets: dict[str, Decimal] | None = None
    fees_paid = ZERO
    equity_values = [initial_value]
    active_trading_days: set[int] = set()
    daily_returns: list[Decimal] = []
    previous_day_value = initial_value
    current_day: int | None = None
    current_day_value = initial_value

    for index, timestamp in enumerate(timestamps):
        bars = {pair: market[index] for pair, market in markets.items()}
        if pending_targets is not None:
            open_prices = {pair: bar.open for pair, bar in bars.items()}
            cash, rebalance_fees, traded = _rebalance(
                cash,
                positions,
                pending_targets,
                open_prices,
                run_settings.fee_rate,
            )
            fees_paid += rebalance_fees
            if traded:
                active_trading_days.add(timestamp // DAY_MS)

        close_prices = {pair: bar.close for pair, bar in bars.items()}
        portfolio_value = _portfolio_value(cash, positions, close_prices)
        equity_values.append(portfolio_value)

        bar_day = (timestamp + HOUR_MS - 1) // DAY_MS
        if current_day is None:
            current_day = bar_day
        elif bar_day != current_day:
            daily_returns.append(current_day_value / previous_day_value - ONE)
            previous_day_value = current_day_value
            current_day = bar_day
        current_day_value = portfolio_value

        quotes: dict[str, MarketQuote] = {}
        rules: dict[str, ExchangeRule] = {}
        history: dict[str, tuple[Candle, ...]] = {}
        for pair, market in markets.items():
            pair_history = market[: index + 1]
            history[pair] = pair_history
            quotes[pair] = _quote_from_history(pair, pair_history)
            rules[pair] = ExchangeRule(
                pair=pair,
                can_trade=True,
                price_precision=PRICE_PRECISION,
                amount_precision=PRICE_PRECISION,
                minimum_order_value=ZERO,
            )

        targets = strategy.propose_targets(
            quotes,
            history,
            rules,
            run_settings,
        )
        pending_targets = _target_weights(targets)

    daily_returns.append(current_day_value / previous_day_value - ONE)
    final_value = equity_values[-1]
    peak_value = initial_value
    max_drawdown = ZERO
    for value in equity_values[1:]:
        peak_value = max(peak_value, value)
        max_drawdown = max(max_drawdown, (peak_value - value) / peak_value)

    return BacktestResult(
        initial_value=initial_value,
        final_value=final_value,
        total_return=final_value / initial_value - ONE,
        max_drawdown=max_drawdown,
        daily_returns=tuple(daily_returns),
        fees_paid=fees_paid,
        active_trading_days=len(active_trading_days),
    )


def _normalize_candles(
    candles: Mapping[str, Sequence[Candle]],
) -> tuple[dict[str, tuple[Candle, ...]], tuple[int, ...]]:
    if not isinstance(candles, Mapping) or not candles:
        raise ValueError("At least one pair of historical candles is required.")

    normalized: dict[str, tuple[Candle, ...]] = {}
    reference_timestamps: tuple[int, ...] | None = None
    for pair, values in candles.items():
        if (
            not isinstance(pair, str)
            or not pair
            or not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
        ):
            raise ValueError("Each market must map a pair name to candle data.")
        if any(not isinstance(candle, Candle) for candle in values):
            raise ValueError(f"Candle history for {pair} contains an invalid value.")
        ordered = tuple(sorted(values, key=lambda candle: candle.open_time_ms))
        if not ordered:
            raise ValueError(f"No candles were supplied for {pair}.")

        for index, candle in enumerate(ordered):
            _validate_candle(pair, candle)
            if index:
                previous_time = ordered[index - 1].open_time_ms
                if candle.open_time_ms != previous_time + HOUR_MS:
                    raise ValueError(
                        f"Candle history for {pair} must be contiguous."
                    )

        timestamps = tuple(candle.open_time_ms for candle in ordered)
        if reference_timestamps is not None and timestamps != reference_timestamps:
            raise ValueError("All pairs must have the same hourly candle history.")
        reference_timestamps = timestamps
        normalized[pair] = ordered

    return normalized, reference_timestamps or ()


def _validate_candle(pair: str, candle: Candle) -> None:
    if not isinstance(candle, Candle) or candle.pair != pair:
        raise ValueError(f"Candle pair does not match the mapping key {pair}.")
    if (
        not isinstance(candle.open_time_ms, int)
        or isinstance(candle.open_time_ms, bool)
        or not isinstance(candle.close_time_ms, int)
        or isinstance(candle.close_time_ms, bool)
        or candle.open_time_ms <= 0
        or candle.open_time_ms % HOUR_MS != 0
        or candle.close_time_ms != candle.open_time_ms + HOUR_MS
    ):
        raise ValueError(f"Candle timestamps for {pair} must be UTC hourly boundaries.")

    prices = (candle.open, candle.high, candle.low, candle.close)
    if any(not _is_positive_decimal(value) for value in prices):
        raise ValueError(f"Candle prices for {pair} must be finite and positive.")
    if (
        candle.high < max(candle.open, candle.close)
        or candle.low > min(candle.open, candle.close)
        or candle.high < candle.low
    ):
        raise ValueError(f"Candle OHLC values for {pair} are inconsistent.")
    if (
        not isinstance(candle.volume, Decimal)
        or not candle.volume.is_finite()
        or candle.volume < ZERO
    ):
        raise ValueError(f"Candle volume for {pair} must be finite and non-negative.")


def _quote_currency(pair: str) -> str:
    if pair.count("/") != 1:
        raise ValueError(f"Backtest pair {pair!r} must use BASE/QUOTE format.")
    base, quote = pair.split("/")
    if not base or not quote:
        raise ValueError(f"Backtest pair {pair!r} must use BASE/QUOTE format.")
    return quote


def _quote_from_history(
    pair: str,
    history: Sequence[Candle],
) -> MarketQuote:
    latest = history[-1]
    recent = history[-24:] if len(history) >= 24 else ()
    quote_turnover = sum((candle.volume * candle.close for candle in recent), ZERO)
    previous_price = history[-25].close if len(history) > 24 else latest.close
    change_24h = latest.close / previous_price - ONE
    return MarketQuote(
        pair=pair,
        server_time_ms=latest.close_time_ms,
        bid=latest.close,
        ask=latest.close,
        last=latest.close,
        change_24h=change_24h,
        quote_turnover_24h=quote_turnover,
    )


def _target_weights(targets: Sequence[TargetWeight]) -> dict[str, Decimal]:
    weights: dict[str, Decimal] = {}
    for target in targets:
        if not isinstance(target, TargetWeight):
            raise ValueError("Strategy returned an invalid target.")
        if target.pair is None:
            continue
        if target.pair in weights:
            raise ValueError(f"Strategy returned duplicate target {target.pair}.")
        if (
            not isinstance(target.weight, Decimal)
            or not target.weight.is_finite()
            or target.weight < ZERO
            or target.weight > ONE
        ):
            raise ValueError("Strategy target weights must be between zero and one.")
        weights[target.pair] = target.weight
    if sum(weights.values(), ZERO) > ONE:
        raise ValueError("Strategy target weights cannot exceed one in total.")
    return weights


def _rebalance(
    cash: Decimal,
    positions: dict[str, Decimal],
    targets: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    fee_rate: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    portfolio_value = _portfolio_value(cash, positions, prices)
    desired_values = {
        pair: portfolio_value * weight for pair, weight in targets.items()
    }
    fees_paid = ZERO
    traded = False

    for pair in sorted(set(positions) | set(desired_values)):
        current_value = positions.get(pair, ZERO) * prices[pair]
        target_value = desired_values.get(pair, ZERO)
        if current_value <= target_value:
            continue
        notional = current_value - target_value
        quantity = notional / prices[pair]
        positions[pair] = positions.get(pair, ZERO) - quantity
        cash += notional * (ONE - fee_rate)
        fees_paid += notional * fee_rate
        traded = True

    for pair, target_value in sorted(desired_values.items()):
        current_value = positions.get(pair, ZERO) * prices[pair]
        notional = min(
            max(target_value - current_value, ZERO),
            cash / (ONE + fee_rate),
        )
        if notional <= ZERO:
            continue
        positions[pair] = positions.get(pair, ZERO) + notional / prices[pair]
        cash -= notional * (ONE + fee_rate)
        fees_paid += notional * fee_rate
        traded = True

    for pair in tuple(positions):
        if positions[pair] == ZERO:
            del positions[pair]
    return cash, fees_paid, traded


def _portfolio_value(
    cash: Decimal,
    positions: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
) -> Decimal:
    return cash + sum(
        (quantity * prices[pair] for pair, quantity in positions.items()),
        ZERO,
    )


def _validate_initial_value(value: Decimal) -> None:
    if not _is_positive_decimal(value):
        raise ValueError("Initial portfolio value must be finite and positive.")


def _validate_fee_rate(value: Decimal) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < ZERO
        or value > ONE
    ):
        raise ValueError("Fee rate must be a finite fraction between zero and one.")


def _is_positive_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > ZERO
