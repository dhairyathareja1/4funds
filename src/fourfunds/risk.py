# Portfolio valuation and risk-checked order planning.

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
from typing import Protocol

from fourfunds.models import (
    ExchangeRule,
    MarketQuote,
    OrderIntent,
    OrderPlan,
    OrderRejection,
    OrderSide,
    OrderType,
    PortfolioRiskState,
    TargetWeight,
    WalletSnapshot,
)
from fourfunds.settings import Settings

ZERO = Decimal("0")
ONE = Decimal("1")
DAY_MS = 24 * 60 * 60 * 1000
MILLISECONDS_PER_SECOND = 1000
MAX_EXCHANGE_PRECISION = 18
PREFERRED_NUMERAIRE_CURRENCIES = ("USD", "USDT", "USDC")


class OrderPlanner(Protocol):
    def plan(
        self,
        targets: Sequence[TargetWeight],
        wallet: WalletSnapshot,
        quotes: Mapping[str, MarketQuote],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
        risk_state: PortfolioRiskState | None = None,
    ) -> OrderPlan:
        ...


class RiskManager:
    def plan(
        self,
        targets: Sequence[TargetWeight],
        wallet: WalletSnapshot,
        quotes: Mapping[str, MarketQuote],
        rules: Mapping[str, ExchangeRule],
        settings: Settings,
        risk_state: PortfolioRiskState | None = None,
    ) -> OrderPlan:
        return plan_orders(targets, wallet, quotes, rules, settings, risk_state)


@dataclass(frozen=True)
class _Portfolio:
    currency: str
    value: Decimal
    balances: Mapping[str, Decimal]
    free_balances: Mapping[str, Decimal]
    asset_values: Mapping[str, Decimal]


@dataclass(frozen=True)
class _TradeMarket:
    rule: ExchangeRule
    quote: MarketQuote
    base_currency: str


def plan_orders(
    targets: Sequence[TargetWeight],
    wallet: WalletSnapshot,
    tickers: Mapping[str, MarketQuote],
    exchange_rules: Mapping[str, ExchangeRule],
    settings: Settings,
    risk_state: PortfolioRiskState | None = None,
) -> OrderPlan:
    settings_error = _settings_error(settings)
    if settings_error is not None:
        return _empty_plan(settings_error)
    if not targets:
        return _empty_plan("No target weights were supplied; no orders were planned.")

    portfolio, portfolio_error = _build_portfolio(
        targets, wallet, tickers, exchange_rules, settings
    )
    if portfolio_error is not None:
        return _empty_plan(portfolio_error)
    if portfolio is None:
        return _empty_plan("Portfolio valuation failed.")

    risk_error, circuit_breaker_reason = _check_risk_state(
        risk_state, wallet.server_time_ms, portfolio, settings
    )
    if risk_error is not None:
        return _empty_plan(risk_error, portfolio.value)

    rejections: list[OrderRejection] = []
    targets_by_pair, cash_weight, target_error = _parse_targets(
        targets, portfolio.currency, rejections
    )
    if target_error is not None:
        rejections.append(OrderRejection(None, target_error))
        return _finish_plan((), rejections, portfolio.value)

    targets_by_base, pair_rejections = _targets_by_base(targets_by_pair)
    rejections.extend(pair_rejections)
    if pair_rejections:
        return _finish_plan((), rejections, portfolio.value)
    effective_weights, target_notes, exposure_cap = _effective_weights(
        targets_by_base, cash_weight, settings
    )
    if circuit_breaker_reason is not None:
        rejections.append(OrderRejection(None, circuit_breaker_reason))
        effective_weights = {}
        target_notes = defaultdict(list)

    pair_by_base = {
        base: target.pair
        for base, target in targets_by_base.items()
        if target.pair is not None
    }
    positions = set(pair_by_base)
    positions.update(
        asset
        for asset, balance in portfolio.balances.items()
        if asset != portfolio.currency and balance > ZERO
    )

    sell_intents: list[OrderIntent] = []
    buy_candidates: list[tuple[str, _TradeMarket, Decimal, str]] = []
    for base in sorted(positions):
        pair = pair_by_base.get(base, f"{base}/{portfolio.currency}")
        market = _trade_market(
            pair, wallet, tickers, exchange_rules, settings
        )
        if isinstance(market, str):
            rejections.append(OrderRejection(pair, market))
            continue
        if market.base_currency != base:
            rejections.append(
                OrderRejection(pair, f"Pair does not represent wallet asset {base}.")
            )
            continue

        target = targets_by_base.get(base)
        target_weight = effective_weights.get(base, ZERO)
        target_value = portfolio.value * target_weight
        target_quantity = target_value / market.quote.last
        current_quantity = portfolio.balances.get(base, ZERO)
        difference = target_quantity - current_quantity
        target_reason = (
            target.reason if target is not None else "Target weight is zero."
        )
        target_reason = _append_notes(target_reason, target_notes.get(base, ()))
        if circuit_breaker_reason is not None:
            target_reason = _append_notes(target_reason, (circuit_breaker_reason,))

        if difference < ZERO:
            sell_intent, sell_rejections = _plan_sell(
                pair,
                market,
                -difference,
                portfolio.free_balances.get(base, ZERO),
                settings,
                target_reason,
            )
            if sell_intent is not None:
                sell_intents.append(sell_intent)
            rejections.extend(sell_rejections)
        elif difference > ZERO and circuit_breaker_reason is None:
            buy_candidates.append((base, market, difference, target_reason))

    if circuit_breaker_reason is not None:
        return _finish_plan(sell_intents, rejections, portfolio.value)

    current_exposure = sum(
        (
            value
            for asset, value in portfolio.asset_values.items()
            if asset != portfolio.currency
        ),
        ZERO,
    )
    total_exposure_budget = max(
        ZERO, portfolio.value * exposure_cap - current_exposure
    )
    cash_reserve = max(settings.minimum_cash_reserve, cash_weight or ZERO)
    current_cash = portfolio.balances.get(portfolio.currency, ZERO)
    free_cash = portfolio.free_balances.get(portfolio.currency, ZERO)
    spendable_cash = min(
        free_cash,
        max(ZERO, current_cash - portfolio.value * cash_reserve),
    )

    buy_intents: list[OrderIntent] = []
    for base, market, requested_quantity, target_reason in sorted(
        buy_candidates, key=lambda candidate: candidate[1].rule.pair
    ):
        intent, order_rejections, spend, exposure_spend = _plan_buy(
            base,
            market,
            requested_quantity,
            portfolio,
            settings,
            target_reason,
            total_exposure_budget,
            spendable_cash,
        )
        if intent is not None:
            buy_intents.append(intent)
            spendable_cash -= spend
            total_exposure_budget -= exposure_spend
        rejections.extend(order_rejections)

    return _finish_plan(
        (*sell_intents, *buy_intents), rejections, portfolio.value
    )


def _settings_error(settings: Settings) -> str | None:
    fractions = (
        ("RISK_MAX_ASSET_WEIGHT", settings.max_asset_weight, False),
        ("RISK_MAX_TOTAL_EXPOSURE", settings.max_total_exposure, False),
        ("RISK_MINIMUM_CASH_RESERVE", settings.minimum_cash_reserve, True),
        ("RISK_MAX_DAILY_LOSS_FRACTION", settings.max_daily_loss_fraction, False),
        ("RISK_MAX_DRAWDOWN_FRACTION", settings.max_drawdown_fraction, False),
        ("RISK_FEE_RATE", settings.fee_rate, True),
    )
    for name, value, allow_zero in fractions:
        if not isinstance(value, Decimal) or not value.is_finite():
            return f"{name} must be a finite decimal."
        if value < ZERO or value > ONE or (not allow_zero and value == ZERO):
            return f"{name} must be within its configured fraction range."
    if settings.max_asset_weight > settings.max_total_exposure:
        return "Per-asset exposure cannot exceed total exposure."
    if settings.max_total_exposure + settings.minimum_cash_reserve > ONE:
        return "Total exposure plus cash reserve cannot exceed 100%."
    if (
        not isinstance(settings.max_quote_age_seconds, int)
        or isinstance(settings.max_quote_age_seconds, bool)
        or settings.max_quote_age_seconds <= 0
    ):
        return "Maximum quote age must be a positive integer."
    return None


def _build_portfolio(
    targets: Sequence[TargetWeight],
    wallet: WalletSnapshot,
    tickers: Mapping[str, MarketQuote],
    rules: Mapping[str, ExchangeRule],
    settings: Settings,
) -> tuple[_Portfolio | None, str | None]:
    if not _is_positive_timestamp(wallet.server_time_ms):
        return None, "Wallet snapshot has an invalid server timestamp."

    balances: dict[str, Decimal] = {}
    free_balances: dict[str, Decimal] = {}
    for item in wallet.assets:
        if (
            not isinstance(item.asset, str)
            or not item.asset
            or item.asset != item.asset.strip()
            or "/" in item.asset
            or item.asset in balances
        ):
            return (
                None,
                f"Wallet contains an invalid or duplicate asset {item.asset!r}.",
            )
        if not _is_non_negative_decimal(item.free) or not _is_non_negative_decimal(
            item.locked
        ):
            return None, f"Wallet balance for {item.asset} is invalid."
        free_balances[item.asset] = item.free
        balances[item.asset] = item.free + item.locked

    currency, currency_error = _choose_numeraire(
        targets, balances, tickers, rules
    )
    if currency_error is not None:
        return None, currency_error
    if currency is None:
        return None, "Could not select a portfolio valuation currency."

    asset_values: dict[str, Decimal] = {}
    for asset, balance in sorted(balances.items()):
        if balance == ZERO:
            asset_values[asset] = ZERO
            continue
        if asset == currency:
            asset_values[asset] = balance
            continue
        pair = f"{asset}/{currency}"
        quote = tickers.get(pair)
        if (
            quote is None
            or quote.pair != pair
            or not _quote_is_fresh(quote, wallet.server_time_ms, settings)
            or not _is_positive_decimal(quote.last)
        ):
            return (
                None,
                f"Wallet asset {asset} cannot be marked with a fresh {pair} ticker; "
                "no orders were planned.",
            )
        asset_values[asset] = balance * quote.last

    portfolio_value = sum(asset_values.values(), ZERO)
    if portfolio_value <= ZERO or not portfolio_value.is_finite():
        return None, "Portfolio value must be positive and finite."
    return (
        _Portfolio(
            currency=currency,
            value=portfolio_value,
            balances=balances,
            free_balances=free_balances,
            asset_values=asset_values,
        ),
        None,
    )


def _choose_numeraire(
    targets: Sequence[TargetWeight],
    balances: Mapping[str, Decimal],
    tickers: Mapping[str, MarketQuote],
    rules: Mapping[str, ExchangeRule],
) -> tuple[str | None, str | None]:
    target_currencies: set[str] = set()
    for target in targets:
        if target.pair is None:
            continue
        parts = _split_pair(target.pair)
        if parts is not None:
            target_currencies.add(parts[1])
    if len(target_currencies) > 1:
        return (
            None,
            "Target pairs use multiple quote currencies; no orders were planned.",
        )
    if target_currencies:
        return next(iter(target_currencies)), None

    held_preferred = next(
        (
            currency
            for currency in PREFERRED_NUMERAIRE_CURRENCIES
            if balances.get(currency, ZERO) > ZERO
        ),
        None,
    )
    if held_preferred is not None:
        return held_preferred, None

    quote_currencies: set[str] = set()
    for pair in set(tickers) | set(rules):
        parts = _split_pair(pair)
        if parts is not None:
            quote_currencies.add(parts[1])
    held_quote_currencies = {
        currency
        for currency in quote_currencies
        if balances.get(currency, ZERO) > ZERO
    }
    preferred = next(
        (
            currency
            for currency in PREFERRED_NUMERAIRE_CURRENCIES
            if currency in held_quote_currencies
        ),
        None,
    )
    if preferred is not None:
        return preferred, None
    if len(held_quote_currencies) == 1:
        return next(iter(held_quote_currencies)), None
    if len(held_quote_currencies) > 1:
        return (
            None,
            "Wallet holds multiple quote currencies without a preferred numeraire.",
        )
    if len(quote_currencies) == 1:
        return next(iter(quote_currencies)), None
    preferred = next(
        (
            currency
            for currency in PREFERRED_NUMERAIRE_CURRENCIES
            if currency in quote_currencies
        ),
        None,
    )
    if preferred is not None:
        return preferred, None
    return (
        None,
        "Exchange pairs do not identify one portfolio valuation currency.",
    )


def _check_risk_state(
    state: PortfolioRiskState | None,
    wallet_time_ms: int,
    portfolio: _Portfolio,
    settings: Settings,
) -> tuple[str | None, str | None]:
    if state is None:
        return (
            "Daily-start value and high-water mark are required to evaluate "
            "loss and drawdown limits.",
            None,
        )
    if state.currency != portfolio.currency:
        return "Risk-state currency does not match the portfolio currency.", None
    current_day_start = wallet_time_ms // DAY_MS * DAY_MS
    if state.utc_day_start_ms != current_day_start:
        return "Risk state is not initialized for the wallet snapshot's UTC day.", None
    if (
        not _is_positive_decimal(state.day_start_value)
        or not _is_positive_decimal(state.high_water_mark)
        or state.high_water_mark < state.day_start_value
    ):
        return "Daily-start value or high-water mark is invalid.", None

    daily_loss = (state.day_start_value - portfolio.value) / state.day_start_value
    drawdown = (state.high_water_mark - portfolio.value) / state.high_water_mark
    triggers: list[str] = []
    if daily_loss >= settings.max_daily_loss_fraction:
        triggers.append(
            f"daily loss {daily_loss} reached limit "
            f"{settings.max_daily_loss_fraction}"
        )
    if drawdown >= settings.max_drawdown_fraction:
        triggers.append(
            f"drawdown {drawdown} reached limit {settings.max_drawdown_fraction}"
        )
    if not triggers:
        return None, None
    return (
        None,
        "Circuit breaker active: "
        + "; ".join(triggers)
        + ". New buys are disabled.",
    )


def _parse_targets(
    targets: Sequence[TargetWeight],
    currency: str,
    rejections: list[OrderRejection],
) -> tuple[dict[str, TargetWeight], Decimal | None, str | None]:
    cash_targets = [target for target in targets if target.pair is None]
    if len(cash_targets) > 1:
        return {}, None, "Only one cash target may be supplied."
    cash_weight: Decimal | None = None
    if cash_targets:
        cash_weight = cash_targets[0].weight
        if not _is_fraction(cash_weight):
            return {}, None, "Cash target weight must be between zero and one."

    targets_by_pair: dict[str, TargetWeight] = {}
    duplicate_pairs: set[str] = set()
    for target in targets:
        if target.pair is None:
            continue
        if not _is_fraction(target.weight):
            reason = "Target weight must be between zero and one."
            rejections.append(OrderRejection(target.pair, reason))
            return (
                {},
                cash_weight,
                f"Invalid target for {target.pair}; no orders were planned.",
            )
        parts = _split_pair(target.pair)
        if parts is None:
            rejections.append(OrderRejection(target.pair, "Target pair is invalid."))
            return {}, cash_weight, "An invalid target pair was supplied."
        if parts[1] != currency:
            reason = f"Target quote currency must be {currency}."
            rejections.append(
                OrderRejection(target.pair, reason)
            )
            return {}, cash_weight, f"Invalid target for {target.pair}."
        if target.pair in targets_by_pair:
            duplicate_pairs.add(target.pair)
        else:
            targets_by_pair[target.pair] = target

    for pair in sorted(duplicate_pairs):
        targets_by_pair.pop(pair, None)
        rejections.append(OrderRejection(pair, "Duplicate target pair was rejected."))
    if duplicate_pairs:
        return (
            {},
            cash_weight,
            "Duplicate target pairs were supplied; no orders were planned.",
        )
    return targets_by_pair, cash_weight, None


def _targets_by_base(
    targets_by_pair: Mapping[str, TargetWeight],
) -> tuple[dict[str, TargetWeight], list[OrderRejection]]:
    targets_by_base: dict[str, TargetWeight] = {}
    duplicates: set[str] = set()
    rejections: list[OrderRejection] = []
    for pair, target in sorted(targets_by_pair.items()):
        parts = _split_pair(pair)
        if parts is None:
            continue
        base = parts[0]
        if base in targets_by_base:
            duplicates.add(base)
        else:
            targets_by_base[base] = target
    for base in sorted(duplicates):
        targets_by_base.pop(base, None)
        rejections.append(
            OrderRejection(
                None,
                f"Multiple target pairs represent wallet asset {base}.",
            )
        )
    return targets_by_base, rejections


def _effective_weights(
    targets_by_base: Mapping[str, TargetWeight],
    cash_weight: Decimal | None,
    settings: Settings,
) -> tuple[dict[str, Decimal], dict[str, list[str]], Decimal]:
    weights: dict[str, Decimal] = {}
    notes: dict[str, list[str]] = defaultdict(list)
    for base, target in sorted(targets_by_base.items()):
        weight = target.weight
        if weight > settings.max_asset_weight:
            weight = settings.max_asset_weight
            notes[base].append(
                f"per-asset cap reduced target weight to {weight}"
            )
        weights[base] = weight

    exposure_cap = min(
        settings.max_total_exposure,
        ONE - settings.minimum_cash_reserve,
    )
    if cash_weight is not None:
        exposure_cap = min(exposure_cap, ONE - cash_weight)
    total_weight = sum(weights.values(), ZERO)
    if total_weight > exposure_cap and total_weight > ZERO:
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            scale = exposure_cap / total_weight
            for base, weight in weights.items():
                weights[base] = weight * scale
                notes[base].append(
                    f"total exposure cap reduced target weight to {weights[base]}"
                )
    return weights, notes, exposure_cap


def _trade_market(
    pair: str,
    wallet: WalletSnapshot,
    tickers: Mapping[str, MarketQuote],
    rules: Mapping[str, ExchangeRule],
    settings: Settings,
) -> _TradeMarket | str:
    parts = _split_pair(pair)
    if parts is None:
        return "Pair must use BASE/QUOTE format."
    rule = rules.get(pair)
    if rule is None or rule.pair != pair:
        return "Exchange rule is missing or mismatched."
    if rule.can_trade is not True:
        return "Exchange marks the pair as non-tradable."
    if (
        not isinstance(rule.price_precision, int)
        or isinstance(rule.price_precision, bool)
        or not 0 <= rule.price_precision <= MAX_EXCHANGE_PRECISION
        or not isinstance(rule.amount_precision, int)
        or isinstance(rule.amount_precision, bool)
        or not 0 <= rule.amount_precision <= MAX_EXCHANGE_PRECISION
    ):
        return "Exchange precision is invalid or unsupported."
    if not _is_non_negative_decimal(rule.minimum_order_value):
        return "Exchange minimum order value is invalid."

    quote = tickers.get(pair)
    if quote is None or quote.pair != pair:
        return "Ticker is missing or mismatched."
    if not _quote_is_fresh(quote, wallet.server_time_ms, settings):
        return "Ticker is stale relative to the wallet snapshot."
    if not all(
        _is_positive_decimal(value) for value in (quote.bid, quote.ask, quote.last)
    ):
        return "Ticker bid, ask, and last prices must be positive and finite."
    if quote.bid > quote.ask:
        return "Ticker bid is above its ask."
    return _TradeMarket(rule, quote, parts[0])


def _plan_sell(
    pair: str,
    market: _TradeMarket,
    requested_quantity: Decimal,
    free_quantity: Decimal,
    settings: Settings,
    target_reason: str,
) -> tuple[OrderIntent | None, list[OrderRejection]]:
    rejections: list[OrderRejection] = []
    available_quantity = min(requested_quantity, free_quantity)
    if available_quantity < requested_quantity:
        rejections.append(
            OrderRejection(
                pair,
                "Locked balance prevents selling the full target reduction.",
            )
        )
    estimated_price = _rounded_price(
        market.quote.bid, market.rule.price_precision, ROUND_DOWN
    )
    quantity = _rounded_quantity(available_quantity, market.rule.amount_precision)
    if estimated_price is None or estimated_price <= ZERO:
        rejections.append(
            OrderRejection(pair, "Sell reference price rounds to zero or is invalid.")
        )
        return None, rejections
    if quantity is None or quantity <= ZERO:
        rejections.append(
            OrderRejection(pair, "Sell quantity rounds to zero or is invalid.")
        )
        return None, rejections
    if quantity < available_quantity:
        rejections.append(
            OrderRejection(
                pair,
                "Sell quantity was rounded down to exchange amount precision.",
            )
        )
    notional = quantity * estimated_price
    if notional < market.rule.minimum_order_value:
        rejections.append(
            OrderRejection(
                pair,
                f"Sell notional {notional} is below the exchange minimum "
                f"{market.rule.minimum_order_value}.",
            )
        )
        return None, rejections
    fee = notional * settings.fee_rate
    return (
        OrderIntent(
            pair=pair,
            side=OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reason=_append_notes(
                f"Reduce holdings to the target. Estimated taker fee: {fee}.",
                (target_reason,),
            ),
            reference_price=estimated_price,
        ),
        rejections,
    )


def _plan_buy(
    base: str,
    market: _TradeMarket,
    requested_quantity: Decimal,
    portfolio: _Portfolio,
    settings: Settings,
    target_reason: str,
    total_exposure_budget: Decimal,
    spendable_cash: Decimal,
) -> tuple[OrderIntent | None, list[OrderRejection], Decimal, Decimal]:
    pair = market.rule.pair
    rejections: list[OrderRejection] = []
    estimated_price = _rounded_price(
        market.quote.ask, market.rule.price_precision, ROUND_UP
    )
    if estimated_price is None or estimated_price <= ZERO:
        return (
            None,
            [OrderRejection(pair, "Buy reference price is invalid.")],
            ZERO,
            ZERO,
        )

    current_asset_value = portfolio.asset_values.get(base, ZERO)
    remaining_asset_capacity = max(
        ZERO,
        portfolio.value * settings.max_asset_weight - current_asset_value,
    )
    remaining_total_capacity = max(ZERO, total_exposure_budget)
    cash_capacity_quantity = spendable_cash / (
        estimated_price * (ONE + settings.fee_rate)
    )
    exposure_price = max(estimated_price, market.quote.last)
    exposure_unit_cost = exposure_price * (ONE + settings.fee_rate)
    asset_capacity_quantity = remaining_asset_capacity / exposure_unit_cost
    total_capacity_quantity = remaining_total_capacity / exposure_unit_cost
    allowed_quantity = min(
        requested_quantity,
        cash_capacity_quantity,
        asset_capacity_quantity,
        total_capacity_quantity,
    )
    quantity = _rounded_quantity(allowed_quantity, market.rule.amount_precision)
    if quantity is None or quantity <= ZERO:
        rejections.append(
            OrderRejection(
                pair,
                "Buy rejected by available cash, cash reserve, exposure limits, "
                "or amount precision.",
            )
        )
        return None, rejections, ZERO, ZERO

    if quantity < requested_quantity:
        rejections.append(
            OrderRejection(
                pair,
                f"Buy quantity reduced from {requested_quantity} to {quantity} "
                "by cash, reserve, exposure, or exchange precision limits.",
            )
        )
    notional = quantity * estimated_price
    if notional < market.rule.minimum_order_value:
        rejections.append(
            OrderRejection(
                pair,
                f"Buy notional {notional} is below the exchange minimum "
                f"{market.rule.minimum_order_value}.",
            )
        )
        return None, rejections, ZERO, ZERO
    fee = notional * settings.fee_rate
    total_spend = notional + fee
    exposure_spend = quantity * exposure_unit_cost
    if total_spend > spendable_cash:
        rejections.append(
            OrderRejection(pair, "Buy cost plus fee allowance exceeds free cash.")
        )
        return None, rejections, ZERO, ZERO

    return (
        OrderIntent(
            pair=pair,
            side=OrderSide.BUY,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reason=_append_notes(
                f"Move toward the target weight. Estimated taker fee: {fee}.",
                (target_reason,),
            ),
            reference_price=estimated_price,
        ),
        rejections,
        total_spend,
        exposure_spend,
    )


def _rounded_price(
    value: Decimal, precision: int, rounding: str
) -> Decimal | None:
    return _quantize(value, precision, rounding)


def _rounded_quantity(value: Decimal, precision: int) -> Decimal | None:
    return _quantize(value, precision, ROUND_DOWN)


def _quantize(value: Decimal, precision: int, rounding: str) -> Decimal | None:
    try:
        quantum = ONE.scaleb(-precision)
        return value.quantize(quantum, rounding=rounding)
    except (InvalidOperation, ValueError):
        return None


def _quote_is_fresh(
    quote: MarketQuote, wallet_time_ms: int, settings: Settings
) -> bool:
    if not _is_positive_timestamp(quote.server_time_ms):
        return False
    age_ms = abs(wallet_time_ms - quote.server_time_ms)
    return age_ms <= settings.max_quote_age_seconds * MILLISECONDS_PER_SECOND


def _split_pair(pair: str) -> tuple[str, str] | None:
    if not isinstance(pair, str):
        return None
    parts = pair.split("/")
    if (
        len(parts) != 2
        or not parts[0].strip()
        or not parts[1].strip()
        or parts[0].strip() == parts[1].strip()
    ):
        return None
    return parts[0].strip(), parts[1].strip()


def _is_positive_timestamp(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_positive_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > ZERO


def _is_non_negative_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= ZERO


def _is_fraction(value: Decimal) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and ZERO <= value <= ONE
    )


def _append_notes(reason: str, notes: Sequence[str]) -> str:
    additions = [note for note in notes if note]
    if not additions:
        return reason
    return f"{reason} {'; '.join(additions)}"


def _finish_plan(
    intents: Sequence[OrderIntent],
    rejections: Sequence[OrderRejection],
    portfolio_value: Decimal | None,
) -> OrderPlan:
    ordered_rejections = tuple(
        sorted(rejections, key=lambda item: (str(item.pair or ""), item.reason))
    )
    return OrderPlan(tuple(intents), ordered_rejections, portfolio_value)


def _empty_plan(reason: str, portfolio_value: Decimal | None = None) -> OrderPlan:
    return OrderPlan((), (OrderRejection(None, reason),), portfolio_value)
