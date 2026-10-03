import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Callable, Protocol, TypeVar

from fourfunds.api import RoostooAPIError, RoostooClient
from fourfunds.data import (
    DEFAULT_MAX_DATA_AGE_MS,
    HOUR_MS,
    InsufficientMarketHistoryError,
    MarketDataError,
    MarketDataStore,
    StaleMarketDataError,
    load_historical_candles,
)
from fourfunds.execution import Executor, ReconciliationReport
from fourfunds.models import (
    Candle,
    ExecutionResult,
    ExchangeRule,
    MarketQuote,
    OrderIntent,
    PortfolioRiskState,
    PendingOrderSummary,
    WalletSnapshot,
)
from fourfunds.risk import OrderPlanner, value_portfolio
from fourfunds.settings import RunMode, Settings
from fourfunds.strategy import Strategy

logger = logging.getLogger(__name__)
STRATEGY_VERSION = "baseline-hourly-trend-v1"
DEFAULT_READ_ATTEMPTS = 3
DEFAULT_READ_BACKOFF_SECONDS = 1.0
MILLISECONDS_PER_SECOND = 1000
MILLISECONDS_PER_DAY = 24 * 60 * 60 * MILLISECONDS_PER_SECOND
_Result = TypeVar("_Result")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class RiskStateRepository(Protocol):
    def load(self) -> PortfolioRiskState | None:
        ...

    def save(self, state: PortfolioRiskState) -> None:
        ...


class CycleJournal(Protocol):
    def begin(
        self,
        cycle_id: str,
        scheduled_at_ms: int,
        started_at_ms: int,
        decision: dict[str, object],
    ) -> bool:
        ...

    def checkpoint(self, cycle_id: str, decision: dict[str, object]) -> None:
        ...

    def finish(
        self,
        cycle_id: str,
        completed_at_ms: int,
        status: str,
        decision: dict[str, object],
    ) -> None:
        ...


@dataclass(frozen=True)
class CycleOutcome:
    cycle_id: str
    status: str
    decision: dict[str, object]


class RetryingRoostooClient:
    def __init__(
        self,
        client: RoostooClient,
        settings: Settings,
        *,
        max_attempts: int = DEFAULT_READ_ATTEMPTS,
        base_backoff_seconds: float = DEFAULT_READ_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("Read retry attempts must be positive.")
        if base_backoff_seconds < 0:
            raise ValueError("Read retry backoff cannot be negative.")
        self._client = client
        self._settings = settings
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._sleep = sleep

    def get_exchange_info(self) -> dict[str, ExchangeRule]:
        return self._read("exchangeInfo", self._client.get_exchange_info)

    def get_tickers(self) -> dict[str, MarketQuote]:
        return self._read("ticker", self._client.get_tickers)

    def get_balance(self) -> WalletSnapshot:
        return self._read("balance", self._client.get_balance)

    def get_pending_count(self) -> PendingOrderSummary:
        return self._read("pending_count", self._client.get_pending_count)

    def query_order(self, order_id: str) -> ExecutionResult:
        result = self._read("query_order", lambda: self._client.query_order(order_id))
        return _redact_result(result, self._settings)

    def place_order(self, intent: OrderIntent) -> ExecutionResult:
        try:
            result = self._client.place_order(intent)
        except RoostooAPIError as exc:
            raise _redact_api_error(exc, self._settings) from None
        return _redact_result(result, self._settings)

    def _read(self, endpoint: str, operation: Callable[[], _Result]) -> _Result:
        for attempt in range(self._max_attempts):
            try:
                return operation()
            except RoostooAPIError as exc:
                if not _retryable(exc) or attempt + 1 == self._max_attempts:
                    raise _redact_api_error(exc, self._settings) from None
                logger.warning(
                    "Read from %s failed; retry %d of %d.",
                    endpoint,
                    attempt + 1,
                    self._max_attempts - 1,
                )
                self._sleep(self._base_backoff_seconds * (2**attempt))
        raise RuntimeError("Read retry loop ended without a result.")


class BotRunner:
    def __init__(
        self,
        settings: Settings,
        client: RetryingRoostooClient,
        market_data: MarketDataStore,
        strategy: Strategy,
        planner: OrderPlanner,
        executor: Executor,
        cycle_journal: CycleJournal,
        risk_state_store: RiskStateRepository,
        *,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._settings = settings
        self._client = client
        self._market_data = market_data
        self._strategy = strategy
        self._planner = planner
        self._executor = executor
        self._cycle_journal = cycle_journal
        self._risk_state_store = risk_state_store
        self._clock_ms = clock_ms

    def run_once(self, scheduled_at_ms: int | None = None) -> CycleOutcome:
        started_at_ms = self._clock_ms()
        interval_ms = self._settings.cycle_interval_seconds * MILLISECONDS_PER_SECOND
        if scheduled_at_ms is None:
            scheduled_at_ms = started_at_ms // interval_ms * interval_ms
        cycle_id = str(scheduled_at_ms)
        decision = _cycle_metadata(self._settings, cycle_id, scheduled_at_ms)
        decision["started_at_ms"] = started_at_ms

        if not self._cycle_journal.begin(
            cycle_id, scheduled_at_ms, started_at_ms, decision
        ):
            logger.info("Skipping cycle %s; its time slot is already recorded.", cycle_id)
            return CycleOutcome(cycle_id, "skipped", decision)

        logger.info(
            "heartbeat cycle=%s state=started mode=%s",
            cycle_id,
            self._settings.mode.value,
        )
        try:
            reconciliation: ReconciliationReport | None = None
            if self._settings.mode is RunMode.LIVE:
                reconciliation = self._executor.reconcile_pending()
            unresolved = reconciliation.unresolved if reconciliation else ()
            decision["reconciliation"] = {
                "checked": reconciliation is not None,
                "results": reconciliation.checked if reconciliation else (),
                "unresolved": unresolved,
            }
            self._checkpoint(cycle_id, decision)

            rules = self._client.get_exchange_info()
            quotes = self._client.get_tickers()
            wallet = self._client.get_balance()
            if not quotes:
                raise ValueError("The exchange returned an empty ticker snapshot.")
            self._market_data.record_snapshot(quotes)

            quote_time_ms = next(iter(quotes.values())).server_time_ms
            decision["inputs"] = {
                "exchange_rules": rules,
                "quotes": quotes,
                "wallet": wallet,
            }
            self._checkpoint(cycle_id, decision)
            age_ms = self._clock_ms() - quote_time_ms
            if abs(age_ms) > self._settings.max_quote_age_seconds * MILLISECONDS_PER_SECOND:
                decision["errors"] = [
                    f"Ticker snapshot is stale or from the future by {abs(age_ms)} ms."
                ]
                return self._finish(cycle_id, decision, "refused")

            candles, histories, history_errors, history_repairs = self._load_history(
                quotes, quote_time_ms
            )
            decision["inputs"]["market_history"] = histories
            decision["inputs"]["history_errors"] = history_errors
            decision["inputs"]["history_repairs"] = history_repairs
            self._checkpoint(cycle_id, decision)
            strategy_decision = self._strategy.decide(
                quotes, candles, rules, self._settings
            )
            targets = strategy_decision.targets
            decision["strategy"] = {
                "version": STRATEGY_VERSION,
                "signals": strategy_decision.signals,
                "targets": targets,
            }
            self._checkpoint(cycle_id, decision)

            currency, portfolio_value = value_portfolio(
                targets, wallet, quotes, rules, self._settings
            )
            risk_state = self._advance_risk_state(
                currency, portfolio_value, wallet.server_time_ms
            )
            plan = self._planner.plan(
                targets, wallet, quotes, rules, self._settings, risk_state
            )
            decision["risk"] = {
                "state": risk_state,
                "plan": plan,
            }
            self._checkpoint(cycle_id, decision)

            if unresolved:
                decision["execution"] = {
                    "permitted": False,
                    "reason": "Unresolved prior orders remain after reconciliation.",
                    "results": (),
                }
                return self._finish(cycle_id, decision, "blocked")

            if self._settings.mode is RunMode.READ_ONLY:
                decision["execution"] = {
                    "permitted": False,
                    "reason": "Read-only mode does not execute planned orders.",
                    "results": (),
                }
            else:
                results = self._executor.execute(plan.intents, mode=self._settings.mode)
                decision["execution"] = {
                    "permitted": True,
                    "results": results,
                }
            self._checkpoint(cycle_id, decision)
            return self._finish(cycle_id, decision, "completed")
        except Exception as exc:
            decision.setdefault("errors", []).append(
                _safe_message(exc, self._settings)
            )
            self._checkpoint(cycle_id, decision)
            logger.error(
                "heartbeat cycle=%s state=failed error=%s",
                cycle_id,
                decision["errors"][-1],
            )
            return self._finish(cycle_id, decision, "failed")

    def _load_history(
        self,
        quotes: Mapping[str, MarketQuote],
        end_time_ms: int,
    ) -> tuple[
        dict[str, tuple[Candle, ...]],
        dict[str, object],
        dict[str, str],
        dict[str, dict[str, object]],
    ]:
        window_hours = max(
            self._settings.momentum_lookback_hours + 1,
            self._settings.trend_lookback_hours,
        )
        candles: dict[str, tuple[Candle, ...]] = {}
        histories: dict[str, object] = {}
        errors: dict[str, str] = {}
        repair_candidates: list[str] = []
        repairs: dict[str, dict[str, object]] = {}
        for pair in sorted(quotes):
            try:
                history = self._market_data.get_market_history(
                    pair,
                    end_time_ms=end_time_ms,
                    window_hours=window_hours,
                    max_age_ms=DEFAULT_MAX_DATA_AGE_MS,
                )
            except StaleMarketDataError as exc:
                errors[pair] = str(exc)
                repair_candidates.append(pair)
            except MarketDataError as exc:
                errors[pair] = str(exc)
                if isinstance(exc, InsufficientMarketHistoryError):
                    repair_candidates.append(pair)
                else:
                    repairs[pair] = {
                        "repair_attempted": False,
                        "repair_succeeded": None,
                        "repair_error": None,
                    }
                continue
            else:
                candles[pair] = history.candles
                histories[pair] = history

        history_source = self._settings.market_history_csv
        for pair in repair_candidates:
            repairs[pair] = {
                "repair_attempted": False,
                "repair_succeeded": None,
                "repair_error": None,
            }

        if repair_candidates and history_source:
            for pair in repair_candidates:
                repairs[pair]["repair_attempted"] = True
            window_end_ms = end_time_ms // HOUR_MS * HOUR_MS
            window_start_ms = window_end_ms - window_hours * HOUR_MS
            try:
                source_candles = load_historical_candles(history_source)
            except Exception as exc:
                for pair in repair_candidates:
                    repairs[pair]["repair_succeeded"] = False
                    repairs[pair]["repair_error"] = _safe_message(
                        exc, self._settings
                    )
                return candles, histories, errors, repairs

            for pair in repair_candidates:
                try:
                    missing_window = tuple(
                        candle
                        for candle in source_candles
                        if candle.pair == pair
                        and window_start_ms <= candle.open_time_ms < window_end_ms
                    )
                    self._market_data.record_candles_if_missing(missing_window)
                    history = self._market_data.get_market_history(
                        pair,
                        end_time_ms=end_time_ms,
                        window_hours=window_hours,
                        max_age_ms=DEFAULT_MAX_DATA_AGE_MS,
                    )
                except Exception as exc:
                    repairs[pair]["repair_succeeded"] = False
                    repairs[pair]["repair_error"] = _safe_message(
                        exc, self._settings
                    )
                else:
                    repairs[pair]["repair_succeeded"] = True
                    candles[pair] = history.candles
                    histories[pair] = history
        return candles, histories, errors, repairs

    def _advance_risk_state(
        self, currency: str, current_value: Decimal, wallet_time_ms: int
    ) -> PortfolioRiskState:
        if wallet_time_ms <= 0 or current_value <= 0:
            raise ValueError("Wallet time and portfolio value must be positive.")
        utc_day_start_ms = wallet_time_ms // MILLISECONDS_PER_DAY * MILLISECONDS_PER_DAY
        previous = self._risk_state_store.load()
        if previous is None or previous.currency != currency:
            state = PortfolioRiskState(
                currency=currency,
                utc_day_start_ms=utc_day_start_ms,
                day_start_value=current_value,
                high_water_mark=current_value,
            )
        else:
            day_start_value = (
                previous.day_start_value
                if previous.utc_day_start_ms == utc_day_start_ms
                else current_value
            )
            state = PortfolioRiskState(
                currency=currency,
                utc_day_start_ms=utc_day_start_ms,
                day_start_value=day_start_value,
                high_water_mark=max(previous.high_water_mark, current_value),
            )
        self._risk_state_store.save(state)
        return state

    def _finish(
        self, cycle_id: str, decision: dict[str, object], status: str
    ) -> CycleOutcome:
        completed_at_ms = self._clock_ms()
        decision["completed_at_ms"] = completed_at_ms
        serialized = _jsonable(decision)
        if not isinstance(serialized, dict):
            raise TypeError("Cycle decision must serialize to an object.")
        self._cycle_journal.finish(cycle_id, completed_at_ms, status, serialized)
        logger.info(
            "heartbeat cycle=%s state=%s completed_at_ms=%d",
            cycle_id,
            status,
            completed_at_ms,
        )
        return CycleOutcome(cycle_id, status, serialized)

    def _checkpoint(self, cycle_id: str, decision: dict[str, object]) -> None:
        serialized = _jsonable(decision)
        if not isinstance(serialized, dict):
            raise TypeError("Cycle decision must serialize to an object.")
        self._cycle_journal.checkpoint(cycle_id, serialized)


def _cycle_metadata(
    settings: Settings, cycle_id: str, scheduled_at_ms: int
) -> dict[str, object]:
    configuration = {
        item.name: _jsonable(getattr(settings, item.name))
        for item in fields(settings)
        if item.name not in {"api_key", "api_secret"}
    }
    canonical_configuration = json.dumps(
        configuration, sort_keys=True, separators=(",", ":")
    )
    return {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "scheduled_at_ms": scheduled_at_ms,
        "mode": settings.mode.value,
        "strategy_version": STRATEGY_VERSION,
        "configuration": configuration,
        "configuration_fingerprint": hashlib.sha256(
            canonical_configuration.encode("utf-8")
        ).hexdigest(),
    }


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Value of type {type(value).__name__} cannot be logged.")


def _retryable(error: RoostooAPIError) -> bool:
    return (
        error.status_code is None
        or error.status_code in {408, 429}
        or error.status_code >= 500
    )


def _redact_api_error(error: RoostooAPIError, settings: Settings) -> RoostooAPIError:
    message = _redact(str(error), settings)
    return RoostooAPIError(
        message,
        endpoint=error.endpoint,
        status_code=error.status_code,
        request_may_have_succeeded=error.request_may_have_succeeded,
    )


def _redact_result(result: ExecutionResult, settings: Settings) -> ExecutionResult:
    message = _redact(result.message, settings)
    return replace(result, message=message) if message != result.message else result


def _safe_message(error: Exception, settings: Settings) -> str:
    return _redact(str(error), settings) or type(error).__name__


def _redact(message: str, settings: Settings) -> str:
    for secret in (settings.api_key, settings.api_secret):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message
