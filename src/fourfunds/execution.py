import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from fourfunds.api import RoostooAPIError
from fourfunds.execution_store import (
    ExecutionRecord,
    SQLiteExecutionJournal,
)
from fourfunds.models import (
    ExecutionResult,
    MarketQuote,
    OrderIntent,
    OrderSide,
    OrderType,
    PendingOrderSummary,
    WalletSnapshot,
)
from fourfunds.settings import (
    DEFAULT_FEE_RATE,
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    RunMode,
)

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")
MILLISECONDS_PER_SECOND = 1000
TERMINAL_STATUSES = {"FILLED", "REJECTED", "CANCELED"}


class ExecutionClient(Protocol):
    def get_tickers(self) -> dict[str, MarketQuote]:
        ...

    def get_balance(self) -> WalletSnapshot:
        ...

    def get_pending_count(self) -> PendingOrderSummary:
        ...

    def place_order(self, intent: OrderIntent) -> ExecutionResult:
        ...

    def query_order(self, order_id: str) -> ExecutionResult:
        ...


@dataclass(frozen=True)
class ReconciliationReport:
    checked: tuple[ExecutionRecord, ...]
    unresolved: tuple[ExecutionRecord, ...]


class Executor(Protocol):
    def reconcile_pending(self) -> ReconciliationReport:
        ...

    def execute(
        self, intents: Sequence[OrderIntent], *, mode: RunMode
    ) -> Sequence[ExecutionResult]:
        ...


class ExecutionJournal(Protocol):
    def begin(self, intent: OrderIntent, created_at_ms: int) -> int:
        ...

    def mark_submitting(self, record_id: int) -> bool:
        ...

    def finish(self, record_id: int, result: ExecutionResult) -> None:
        ...

    def unresolved_orders(
        self, pair: str, *, exclude_record_id: int | None = None
    ) -> tuple[ExecutionRecord, ...]:
        ...

    def resolve_unknown(self, pair: str, result: ExecutionResult) -> None:
        ...

    def unresolved(self) -> tuple[ExecutionRecord, ...]:
        ...


class OrderExecutor:
    def __init__(
        self,
        client: ExecutionClient,
        *,
        taker_fee_rate: Decimal = DEFAULT_FEE_RATE,
        max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        journal: ExecutionJournal | None = None,
    ) -> None:
        if (
            not isinstance(taker_fee_rate, Decimal)
            or not taker_fee_rate.is_finite()
            or taker_fee_rate < ZERO
        ):
            raise ValueError("Taker fee rate must be a finite non-negative decimal.")
        if (
            not isinstance(max_quote_age_seconds, int)
            or isinstance(max_quote_age_seconds, bool)
            or max_quote_age_seconds <= 0
        ):
            raise ValueError("Maximum quote age must be a positive integer.")
        self._client = client
        self._taker_fee_rate = taker_fee_rate
        self._max_quote_age_seconds = max_quote_age_seconds
        self._journal = journal if journal is not None else SQLiteExecutionJournal()

    def execute(
        self, intents: Sequence[OrderIntent], *, mode: RunMode
    ) -> Sequence[ExecutionResult]:
        if not isinstance(mode, RunMode):
            raise ValueError("Execution mode must be a RunMode value.")

        results: list[ExecutionResult] = []
        for intent in intents:
            record_id = self._journal.begin(intent, _now_ms())
            if mode is RunMode.DRY_RUN:
                result = self._dry_run_result(intent)
            elif mode is RunMode.READ_ONLY:
                result = _result(
                    intent.pair,
                    "REJECTED",
                    "Read-only mode does not submit orders.",
                )
            else:
                result = self._execute_live(intent, record_id)

            self._journal.finish(record_id, result)
            _log_result(result)
            results.append(result)
        return tuple(results)

    def confirm_reconciled(self, result: ExecutionResult) -> None:
        if not _valid_pair(result.pair):
            raise ValueError("Reconciliation result has an invalid trading pair.")
        unresolved = self._journal.unresolved_orders(result.pair)
        if not unresolved:
            raise ValueError(f"No unresolved execution exists for {result.pair}.")
        reconciled = _normalize_result(
            result, unresolved[-1].intent, self._taker_fee_rate
        )
        if reconciled.status not in TERMINAL_STATUSES:
            raise ValueError("Reconciliation requires a confirmed terminal status.")

        pending = self._client.get_pending_count()
        pair_pending = _pair_pending_count(pending, reconciled.pair)
        if pair_pending is None or pair_pending > 0:
            raise ValueError(
                f"Cannot reconcile {reconciled.pair} while pending orders remain."
            )
        wallet = self._client.get_balance()
        reconciled = replace(
            reconciled,
            wallet_snapshot=wallet,
            pending_order_count=pending.total_pending,
        )
        self._journal.resolve_unknown(reconciled.pair, reconciled)

    def reconcile_pending(self) -> ReconciliationReport:
        checked: list[ExecutionRecord] = []
        for record in self._journal.unresolved():
            try:
                pending = self._client.get_pending_count()
            except RoostooAPIError:
                continue
            pair_pending = _pair_pending_count(pending, record.intent.pair)
            if pair_pending is None:
                continue

            if not record.result.order_id:
                if record.result.status == "PENDING" and pair_pending == 0:
                    result = replace(
                        record.result,
                        status="UNKNOWN",
                        message=(
                            "Pending order disappeared without an order ID; "
                            "manual reconciliation is required."
                        ),
                        pending_order_count=pending.total_pending,
                    )
                    self._journal.finish(record.record_id, result)
                    checked.append(replace(record, result=result))
                continue

            try:
                queried = self._client.query_order(record.result.order_id)
            except RoostooAPIError:
                continue
            resolved = _normalize_result(
                queried, record.intent, self._taker_fee_rate
            )
            if resolved.status == "PENDING" and pair_pending == 0:
                resolved = replace(
                    resolved,
                    status="UNKNOWN",
                    message=_append_message(
                        resolved.message,
                        "Exchange reports no pending order; manual "
                        "reconciliation is required.",
                    ),
                )
            try:
                wallet = self._client.get_balance()
            except RoostooAPIError as exc:
                resolved = replace(
                    resolved,
                    message=_append_message(
                        resolved.message, f"Wallet refresh failed: {exc}"
                    ),
                )
            else:
                resolved = replace(resolved, wallet_snapshot=wallet)
            resolved = replace(
                resolved, pending_order_count=pending.total_pending
            )
            self._journal.finish(record.record_id, resolved)
            checked.append(replace(record, result=resolved))
        return ReconciliationReport(
            checked=tuple(checked),
            unresolved=self._journal.unresolved(),
        )

    def _dry_run_result(self, intent: OrderIntent) -> ExecutionResult:
        price = intent.reference_price or intent.limit_price
        commission = ZERO
        message = "Intent recorded; no order was submitted."
        if price is not None and _positive_decimal(price):
            commission = intent.quantity * price * self._taker_fee_rate
            message += " Fee is a taker-rate estimate at the intent reference price."
        else:
            message += " Taker fee estimate is unavailable without a reference price."
        return ExecutionResult(
            pair=intent.pair,
            status="DRY_RUN",
            commission=commission,
            commission_estimated=True,
            message=message,
        )

    def _execute_live(
        self, intent: OrderIntent, record_id: int
    ) -> ExecutionResult:
        validation_error = _intent_error(intent)
        if validation_error is not None:
            return _result(intent.pair, "REJECTED", validation_error)

        blocked = self._reconcile_existing(intent, record_id)
        if blocked is not None:
            return blocked

        try:
            pending = self._client.get_pending_count()
        except RoostooAPIError as exc:
            return _result(
                intent.pair,
                "REJECTED",
                f"Pending-order preflight failed; intent was not submitted: {exc}",
            )
        pending_count = _pair_pending_count(pending, intent.pair)
        if pending_count is None:
            return _result(
                intent.pair,
                "REJECTED",
                "Pending-order data is inconsistent; intent was not submitted.",
            )
        if pending_count > 0:
            return _result(
                intent.pair,
                "REJECTED",
                "An exchange order is already pending for this pair; "
                "intent was not submitted.",
                pending_order_count=pending.total_pending,
            )

        try:
            wallet_before = self._client.get_balance()
        except RoostooAPIError as exc:
            return _result(
                intent.pair,
                "REJECTED",
                f"Wallet preflight failed; intent was not submitted: {exc}",
                pending_order_count=pending.total_pending,
            )
        balance_error = _balance_error(intent, wallet_before)
        if balance_error is not None:
            return _result(
                intent.pair,
                "REJECTED",
                balance_error,
                wallet_snapshot=wallet_before,
                pending_order_count=pending.total_pending,
            )
        try:
            tickers = self._client.get_tickers()
        except RoostooAPIError as exc:
            return _result(
                intent.pair,
                "REJECTED",
                f"Ticker preflight failed; intent was not submitted: {exc}",
                wallet_snapshot=wallet_before,
                pending_order_count=pending.total_pending,
            )
        market_error = _market_data_error(
            intent,
            wallet_before,
            tickers,
            self._taker_fee_rate,
            self._max_quote_age_seconds,
        )
        if market_error is not None:
            return _result(
                intent.pair,
                "REJECTED",
                market_error,
                wallet_snapshot=wallet_before,
                pending_order_count=pending.total_pending,
            )
        if not self._journal.mark_submitting(record_id):
            return _result(
                intent.pair,
                "REJECTED",
                "Another unresolved execution owns this pair; "
                "intent was not submitted.",
                wallet_snapshot=wallet_before,
                pending_order_count=pending.total_pending,
            )

        try:
            submitted = self._client.place_order(intent)
        except RoostooAPIError as exc:
            if not exc.request_may_have_succeeded:
                return _result(
                    intent.pair,
                    "REJECTED",
                    f"Exchange rejected the order: {exc}",
                    wallet_snapshot=wallet_before,
                    pending_order_count=pending.total_pending,
                )
            return self._ambiguous_result(intent, wallet_before, str(exc))

        result = _normalize_result(submitted, intent, self._taker_fee_rate)
        if result.status in {"PENDING", "UNKNOWN"} and result.order_id:
            try:
                queried = self._client.query_order(result.order_id)
            except RoostooAPIError as exc:
                result = replace(
                    result,
                    message=_append_message(
                        result.message,
                        f"Order lookup did not complete and will be retried before "
                        f"another order: {exc}",
                    ),
                )
            else:
                result = _normalize_result(queried, intent, self._taker_fee_rate)

        return self._refresh_account_state(result)

    def _reconcile_existing(
        self, intent: OrderIntent, current_record_id: int
    ) -> ExecutionResult | None:
        records = self._journal.unresolved_orders(
            intent.pair, exclude_record_id=current_record_id
        )
        if not records:
            return None

        try:
            pending = self._client.get_pending_count()
        except RoostooAPIError as exc:
            return _result(
                intent.pair,
                "REJECTED",
                f"An earlier outcome is unresolved; pending orders could not be "
                f"checked, so this intent was not submitted: {exc}",
            )
        pair_pending = _pair_pending_count(pending, intent.pair)
        if pair_pending is None:
            return _result(
                intent.pair,
                "REJECTED",
                "An earlier outcome is unresolved and pending-order data is "
                "inconsistent; this intent was not submitted.",
            )

        for record in records:
            if not record.result.order_id:
                if record.result.status == "PENDING" and pair_pending == 0:
                    self._journal.finish(
                        record.record_id,
                        replace(
                            record.result,
                            status="UNKNOWN",
                            message=(
                                "Pending order disappeared without an order ID; "
                                "manual reconciliation is required."
                            ),
                        ),
                    )
                continue
            try:
                queried = self._client.query_order(record.result.order_id)
            except RoostooAPIError:
                continue
            resolved = _normalize_result(
                queried, record.intent, self._taker_fee_rate
            )
            try:
                wallet = self._client.get_balance()
            except RoostooAPIError as exc:
                resolved = replace(
                    resolved,
                    message=_append_message(
                        resolved.message, f"Wallet refresh failed: {exc}"
                    ),
                )
            else:
                resolved = replace(resolved, wallet_snapshot=wallet)
            self._journal.finish(record.record_id, resolved)

        try:
            pending = self._client.get_pending_count()
        except RoostooAPIError as exc:
            return _result(
                intent.pair,
                "REJECTED",
                f"Earlier order lookup completed, but pending-order status is "
                f"unknown; this intent was not submitted: {exc}",
            )
        pair_pending = _pair_pending_count(pending, intent.pair)
        unresolved = self._journal.unresolved_orders(
            intent.pair, exclude_record_id=current_record_id
        )
        if pair_pending is None or pair_pending > 0 or unresolved:
            return _result(
                intent.pair,
                "REJECTED",
                "An earlier order for this pair is still unresolved; this intent "
                "was not submitted.",
                pending_order_count=pending.total_pending,
            )
        return None

    def _ambiguous_result(
        self, intent: OrderIntent, wallet_before: WalletSnapshot, error: str
    ) -> ExecutionResult:
        result = _result(
            intent.pair,
            "UNKNOWN",
            f"Submission may have succeeded; no retry was attempted. {error}",
            wallet_snapshot=wallet_before,
        )
        return self._refresh_account_state(result)

    def _refresh_account_state(self, result: ExecutionResult) -> ExecutionResult:
        message = result.message
        wallet_snapshot: WalletSnapshot | None = None
        pending_order_count: int | None = None
        try:
            wallet_snapshot = self._client.get_balance()
        except RoostooAPIError as exc:
            message = _append_message(message, f"Wallet refresh failed: {exc}")
        try:
            pending = self._client.get_pending_count()
        except RoostooAPIError as exc:
            message = _append_message(message, f"Pending-order refresh failed: {exc}")
        else:
            pending_order_count = pending.total_pending
            pair_pending = _pair_pending_count(pending, result.pair)
            if pair_pending is not None and pair_pending > 0:
                if result.status == "UNKNOWN":
                    result = replace(result, status="PENDING")
                elif result.status == "PENDING" and not result.order_id:
                    message = _append_message(
                        message,
                        "An open order is reported for this pair; it will be "
                        "checked before any later submission.",
                    )
                message = _append_message(
                    message, f"{pair_pending} order(s) remain pending for this pair."
                )
            elif result.status == "PENDING" and not result.order_id:
                result = replace(
                    result,
                    status="UNKNOWN",
                    message=_append_message(
                        message,
                        "The exchange returned no order ID and no pending order "
                        "can be matched; manual reconciliation is required.",
                    ),
                )
                message = result.message
        return replace(
            result,
            wallet_snapshot=wallet_snapshot,
            pending_order_count=pending_order_count,
            message=message,
        )


def _normalize_result(
    result: ExecutionResult,
    intent: OrderIntent,
    taker_fee_rate: Decimal,
) -> ExecutionResult:
    status = _canonical_status(result.status)
    if result.pair != intent.pair:
        status = "UNKNOWN"
        message = _append_message(
            result.message,
            f"Exchange returned pair {result.pair!r} for intent {intent.pair!r}.",
        )
    else:
        message = result.message

    if (
        not _non_negative_decimal(result.filled_quantity)
        or not _non_negative_decimal(result.commission)
        or (
            result.average_fill_price is not None
            and not _positive_decimal(result.average_fill_price)
        )
    ):
        status = "UNKNOWN"
        message = _append_message(message, "Exchange returned invalid fill details.")
    if status == "FILLED" and result.filled_quantity <= ZERO:
        status = "UNKNOWN"
        message = _append_message(
            message,
            "Exchange marked the order filled without a positive fill quantity.",
        )

    commission = result.commission
    commission_estimated = result.commission_estimated
    if commission_estimated and result.average_fill_price is not None:
        commission = (
            result.filled_quantity * result.average_fill_price * taker_fee_rate
        )
    return replace(
        result,
        status=status,
        commission=commission,
        commission_estimated=commission_estimated,
        message=message,
    )


def _canonical_status(status: str) -> str:
    normalized = status.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"FILLED", "COMPLETE", "COMPLETED"}:
        return "FILLED"
    if normalized in {"REJECTED", "FAILED"}:
        return "REJECTED"
    if normalized in {"CANCELED", "CANCELLED", "EXPIRED"}:
        return "CANCELED"
    if normalized in {
        "PENDING",
        "OPEN",
        "NEW",
        "PARTIAL",
        "PARTIAL_FILLED",
        "PARTIALLY_FILLED",
    }:
        return "PENDING"
    return "UNKNOWN"


def _pair_pending_count(summary: PendingOrderSummary, pair: str) -> int | None:
    counts = dict(summary.order_pairs)
    if sum(counts.values()) != summary.total_pending:
        return None
    return counts.get(pair, 0)


def _balance_error(intent: OrderIntent, wallet: WalletSnapshot) -> str | None:
    if intent.side is not OrderSide.SELL:
        return None
    base, _ = intent.pair.split("/")
    free_balance = sum(
        (asset.free for asset in wallet.assets if asset.asset == base), ZERO
    )
    if free_balance < intent.quantity:
        return (
            f"Sell quantity {intent.quantity} exceeds free {base} balance "
            f"{free_balance}; intent was not submitted."
        )
    return None


def _market_data_error(
    intent: OrderIntent,
    wallet: WalletSnapshot,
    tickers: dict[str, MarketQuote],
    taker_fee_rate: Decimal,
    max_quote_age_seconds: int,
) -> str | None:
    quote = tickers.get(intent.pair)
    if quote is None or quote.pair != intent.pair:
        return "A matching ticker is required; intent was not submitted."
    if (
        not _positive_timestamp(quote.server_time_ms)
        or abs(wallet.server_time_ms - quote.server_time_ms)
        > max_quote_age_seconds * MILLISECONDS_PER_SECOND
    ):
        return "Ticker is stale relative to the wallet; intent was not submitted."
    if (
        not _positive_decimal(quote.bid)
        or not _positive_decimal(quote.ask)
        or quote.bid > quote.ask
    ):
        return "Ticker bid or ask is invalid; intent was not submitted."
    reference_price = intent.reference_price
    if reference_price is None or not _positive_decimal(reference_price):
        return "Market intent has no valid reference price; it was not submitted."

    _, currency = intent.pair.split("/")
    if intent.side is OrderSide.BUY:
        if quote.ask > reference_price:
            return (
                "Current ask is above the approved reference price; intent was "
                "not submitted."
            )
        free_quote = sum(
            (asset.free for asset in wallet.assets if asset.asset == currency),
            ZERO,
        )
        required_quote = (
            intent.quantity * quote.ask * (ONE + taker_fee_rate)
        )
        if required_quote > free_quote:
            return (
                f"Buy cost plus taker fee {required_quote} exceeds free "
                f"{currency} balance {free_quote}; intent was not submitted."
            )
    elif quote.bid < reference_price:
        return (
            "Current bid is below the approved reference price; intent was "
            "not submitted."
        )
    return None


def _intent_error(intent: OrderIntent) -> str | None:
    if not _valid_pair(intent.pair):
        return "Order pair must use distinct BASE/QUOTE currencies."
    if not isinstance(intent.side, OrderSide):
        return "Order side is invalid."
    if not _positive_decimal(intent.quantity):
        return "Order quantity must be finite and greater than zero."
    if intent.order_type is not OrderType.MARKET:
        return "The v0 executor only submits market orders."
    return None


def _valid_pair(pair: str) -> bool:
    if not isinstance(pair, str):
        return False
    parts = pair.split("/")
    return (
        len(parts) == 2
        and all(part and part == part.strip() for part in parts)
        and parts[0] != parts[1]
    )


def _result(
    pair: str,
    status: str,
    message: str,
    *,
    wallet_snapshot: WalletSnapshot | None = None,
    pending_order_count: int | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        pair=pair,
        status=status,
        wallet_snapshot=wallet_snapshot,
        pending_order_count=pending_order_count,
        message=message,
    )


def _positive_decimal(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > ZERO


def _positive_timestamp(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= ZERO


def _append_message(message: str, addition: str) -> str:
    return f"{message} {addition}".strip() if message else addition


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _log_result(result: ExecutionResult) -> None:
    logger.info(
        "Order %s %s status=%s filled=%s commission=%s estimated=%s message=%s",
        result.pair,
        result.order_id or "without-id",
        result.status,
        result.filled_quantity,
        result.commission,
        result.commission_estimated,
        result.message,
    )
