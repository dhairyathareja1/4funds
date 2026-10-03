import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests

from fourfunds.models import (
    CancellationResult,
    ExecutionResult,
    ExchangeRule,
    MarketQuote,
    OrderIntent,
    OrderType,
    PendingOrderSummary,
    WalletAsset,
    WalletSnapshot,
)
from fourfunds.settings import Settings

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10
API_PREFIX = "/v3"


class RoostooAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status_code: int | None = None,
        request_may_have_succeeded: bool = False,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.request_may_have_succeeded = request_may_have_succeeded


class RoostooClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._session = session if session is not None else requests.Session()

    def get_server_time(self) -> int:
        payload, _ = self._request("GET", "serverTime")
        return _read_server_time_ms(payload.get("ServerTime"), "serverTime")

    def get_exchange_info(self) -> dict[str, ExchangeRule]:
        payload, _ = self._request("GET", "exchangeInfo")
        trade_pairs = _read_object(
            payload.get("TradePairs"), "TradePairs", "exchangeInfo"
        )
        rules: dict[str, ExchangeRule] = {}

        for pair, raw_rule in trade_pairs.items():
            rule = _read_object(raw_rule, f"TradePairs.{pair}", "exchangeInfo")
            can_trade = rule.get("CanTrade")
            if not isinstance(can_trade, bool):
                raise _invalid_response(
                    "exchangeInfo", f"CanTrade for {pair} must be a boolean."
                )
            rules[pair] = ExchangeRule(
                pair=pair,
                can_trade=can_trade,
                price_precision=_read_integer(
                    rule.get("PricePrecision"),
                    f"PricePrecision for {pair}",
                    "exchangeInfo",
                    minimum=0,
                ),
                amount_precision=_read_integer(
                    rule.get("AmountPrecision"),
                    f"AmountPrecision for {pair}",
                    "exchangeInfo",
                    minimum=0,
                ),
                minimum_order_value=_read_decimal(
                    rule.get("MiniOrder"),
                    f"MiniOrder for {pair}",
                    "exchangeInfo",
                ),
            )

        return rules

    def get_tickers(self) -> dict[str, MarketQuote]:
        timestamp_ms = self.get_server_time()
        payload, _ = self._request(
            "GET", "ticker", params={"timestamp": str(timestamp_ms)}
        )
        server_time_ms = _read_server_time_ms(
            payload.get("ServerTime"), "ticker"
        )
        raw_tickers = _read_object(payload.get("Data"), "Data", "ticker")
        tickers: dict[str, MarketQuote] = {}

        for pair, raw_ticker in raw_tickers.items():
            ticker = _read_object(raw_ticker, f"Data.{pair}", "ticker")
            tickers[pair] = MarketQuote(
                pair=pair,
                server_time_ms=server_time_ms,
                bid=_read_decimal(ticker.get("MaxBid"), f"MaxBid for {pair}", "ticker"),
                ask=_read_decimal(ticker.get("MinAsk"), f"MinAsk for {pair}", "ticker"),
                last=_read_decimal(
                    ticker.get("LastPrice"), f"LastPrice for {pair}", "ticker"
                ),
                change_24h=_read_decimal(
                    ticker.get("Change"),
                    f"Change for {pair}",
                    "ticker",
                ),
                quote_turnover_24h=_read_decimal(
                    ticker.get("CoinTradeValue"),
                    f"CoinTradeValue for {pair}",
                    "ticker",
                ),
            )

        return tickers

    def get_balance(self) -> WalletSnapshot:
        payload, timestamp_ms = self._request("GET", "balance", signed=True)
        raw_wallet = _read_object(payload.get("Wallet"), "Wallet", "balance")
        assets: list[WalletAsset] = []

        for asset, raw_balance in raw_wallet.items():
            balance = _read_object(raw_balance, f"Wallet.{asset}", "balance")
            assets.append(
                WalletAsset(
                    asset=asset,
                    free=_read_decimal(
                        balance.get("Free"), f"Free for {asset}", "balance"
                    ),
                    locked=_read_decimal(
                        balance.get("Lock"), f"Lock for {asset}", "balance"
                    ),
                )
            )

        return WalletSnapshot(server_time_ms=timestamp_ms or 0, assets=tuple(assets))

    def get_pending_count(self) -> PendingOrderSummary:
        payload, _ = self._request(
            "GET", "pending_count", signed=True, allow_empty_pending=True
        )
        total_pending = _read_integer(
            payload.get("TotalPending"), "TotalPending", "pending_count", minimum=0
        )
        raw_pairs = _read_object(
            payload.get("OrderPairs", {}), "OrderPairs", "pending_count"
        )
        order_pairs = tuple(
            sorted(
                (
                    pair,
                    _read_integer(
                        count, f"OrderPairs.{pair}", "pending_count", minimum=0
                    ),
                )
                for pair, count in raw_pairs.items()
            )
        )
        return PendingOrderSummary(
            total_pending=total_pending, order_pairs=order_pairs
        )

    def place_order(self, intent: OrderIntent) -> ExecutionResult:
        pair = intent.pair.strip()
        if not pair:
            raise ValueError("Order pair cannot be empty.")
        if not intent.quantity.is_finite() or intent.quantity <= 0:
            raise ValueError("Order quantity must be finite and greater than zero.")

        params = {
            "pair": pair,
            "quantity": _format_decimal(intent.quantity),
            "side": intent.side.value,
            "type": intent.order_type.value,
        }
        if intent.order_type is OrderType.LIMIT:
            if (
                intent.limit_price is None
                or not intent.limit_price.is_finite()
                or intent.limit_price <= 0
            ):
                raise ValueError(
                    "Limit orders require a finite price greater than zero."
                )
            params["price"] = _format_decimal(intent.limit_price)

        payload, _ = self._request("POST", "place_order", params=params, signed=True)
        try:
            detail = _read_object(
                payload.get("OrderDetail"), "OrderDetail", "place_order"
            )
            return _parse_execution_result(detail, "place_order")
        except RoostooAPIError as exc:
            raise RoostooAPIError(
                str(exc),
                endpoint=exc.endpoint,
                status_code=exc.status_code,
                request_may_have_succeeded=True,
            ) from exc

    def query_order(self, order_id: str) -> ExecutionResult:
        order_id = order_id.strip()
        if not order_id:
            raise ValueError("Order ID cannot be empty.")

        payload, _ = self._request(
            "POST", "query_order", params={"order_id": order_id}, signed=True
        )
        matches = payload.get("OrderMatched")
        if not isinstance(matches, list) or not matches:
            raise _invalid_response(
                "query_order", "OrderMatched must contain an order."
            )
        detail = _read_object(matches[0], "OrderMatched[0]", "query_order")
        return _parse_execution_result(detail, "query_order")

    def cancel_order(self, order_id: str) -> CancellationResult:
        order_id = order_id.strip()
        if not order_id:
            raise ValueError("Order ID cannot be empty.")

        payload, _ = self._request(
            "POST", "cancel_order", params={"order_id": order_id}, signed=True
        )
        canceled = payload.get("CanceledList")
        if not isinstance(canceled, list):
            raise _invalid_response(
                "cancel_order",
                "CanceledList must be a list.",
                request_may_have_succeeded=True,
            )
        return CancellationResult(
            order_ids=tuple(str(order_id) for order_id in canceled)
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        signed: bool = False,
        allow_empty_pending: bool = False,
    ) -> tuple[dict[str, Any], int | None]:
        request_params = dict(params or {})
        timestamp_ms: int | None = None
        api_key = ""
        api_secret = ""
        if signed:
            api_key, api_secret = self._credentials(endpoint)
            timestamp_ms = self.get_server_time()
            request_params["timestamp"] = str(timestamp_ms)

        headers: dict[str, str] = {}
        data: str | None = None
        if signed:
            canonical_params = _canonical_params(request_params)
            signature = hmac.new(
                api_secret.encode("utf-8"),
                canonical_params.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["RST-API-KEY"] = api_key
            headers["MSG-SIGNATURE"] = signature
            if method == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                data = canonical_params

        url = f"{self._settings.base_url}{API_PREFIX}/{endpoint}"
        try:
            if method == "GET":
                response = self._session.get(
                    url,
                    params=(
                        _canonical_params(request_params)
                        if signed
                        else request_params or None
                    ),
                    headers=headers,
                    timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
            else:
                response = self._session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
        except requests.RequestException as exc:
            raise RoostooAPIError(
                f"Request to /{endpoint} failed ({type(exc).__name__}).",
                endpoint=endpoint,
                request_may_have_succeeded=method == "POST",
            ) from exc

        status_code = response.status_code
        if status_code < 200 or status_code >= 300:
            raise RoostooAPIError(
                f"Request to /{endpoint} returned HTTP {status_code}.",
                endpoint=endpoint,
                status_code=status_code,
                request_may_have_succeeded=method == "POST",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoostooAPIError(
                f"Request to /{endpoint} returned invalid JSON.",
                endpoint=endpoint,
                status_code=status_code,
                request_may_have_succeeded=method == "POST",
            ) from exc

        if not isinstance(payload, dict):
            raise _invalid_response(
                endpoint,
                "Response body must be a JSON object.",
                request_may_have_succeeded=method == "POST",
                status_code=status_code,
            )

        if payload.get("Success") is False:
            if allow_empty_pending and _is_empty_pending_response(payload):
                return payload, timestamp_ms
            message = payload.get("ErrMsg")
            detail = (
                message.strip()
                if isinstance(message, str) and message.strip()
                else "API returned Success=false."
            )
            raise RoostooAPIError(
                f"Request to /{endpoint} failed: {detail}",
                endpoint=endpoint,
                status_code=status_code,
            )

        return payload, timestamp_ms

    def _credentials(self, endpoint: str) -> tuple[str, str]:
        api_key = self._settings.api_key
        api_secret = self._settings.api_secret
        if not api_key or not api_secret:
            raise RoostooAPIError(
                "API credentials are required for this endpoint.", endpoint=endpoint
            )
        return api_key, api_secret


def _canonical_params(params: dict[str, str]) -> str:
    return urlencode(sorted(params.items()), safe="/")


def _is_empty_pending_response(payload: dict[str, Any]) -> bool:
    message = payload.get("ErrMsg")
    return (
        payload.get("TotalPending") == 0
        and isinstance(message, str)
        and message.strip().lower() == "no pending order under this account"
    )


def _parse_execution_result(
    detail: dict[str, Any], endpoint: str
) -> ExecutionResult:
    average_fill_price = _read_decimal(
        detail.get("FilledAverPrice", 0), "FilledAverPrice", endpoint
    )
    order_id = detail.get("OrderID")
    raw_commission = detail.get("CommissionChargeValue")
    return ExecutionResult(
        pair=_read_text(detail.get("Pair"), "Pair", endpoint),
        status=_read_text(detail.get("Status"), "Status", endpoint),
        order_id=str(order_id) if order_id is not None else None,
        filled_quantity=_read_decimal(
            detail.get("FilledQuantity", 0), "FilledQuantity", endpoint
        ),
        average_fill_price=average_fill_price if average_fill_price > 0 else None,
        commission=_read_decimal(
            raw_commission if raw_commission is not None else 0,
            "CommissionChargeValue",
            endpoint,
        ),
        commission_estimated=raw_commission is None,
    )


def _read_object(value: object, field: str, endpoint: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_response(endpoint, f"{field} must be an object.")
    return value


def _read_text(value: object, field: str, endpoint: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_response(endpoint, f"{field} must be a non-empty string.")
    return value


def _read_integer(
    value: object,
    field: str,
    endpoint: str,
    *,
    minimum: int = 1,
) -> int:
    if isinstance(value, bool):
        raise _invalid_response(endpoint, f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid_response(endpoint, f"{field} must be an integer.") from None
    if str(parsed) != str(value) or parsed < minimum:
        raise _invalid_response(endpoint, f"{field} must be at least {minimum}.")
    return parsed


def _read_server_time_ms(value: object, endpoint: str) -> int:
    timestamp_ms = _read_integer(value, "ServerTime", endpoint)
    if len(str(timestamp_ms)) != 13:
        raise _invalid_response(
            endpoint, "ServerTime must be a 13-digit millisecond timestamp."
        )
    return timestamp_ms


def _read_decimal(value: object, field: str, endpoint: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _invalid_response(endpoint, f"{field} must be a decimal number.")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _invalid_response(
            endpoint, f"{field} must be a decimal number."
        ) from None
    if not parsed.is_finite():
        raise _invalid_response(endpoint, f"{field} must be finite.")
    return parsed


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _invalid_response(
    endpoint: str,
    message: str,
    *,
    request_may_have_succeeded: bool = False,
    status_code: int | None = None,
) -> RoostooAPIError:
    return RoostooAPIError(
        f"Invalid response from /{endpoint}: {message}",
        endpoint=endpoint,
        status_code=status_code,
        request_may_have_succeeded=request_may_have_succeeded,
    )
