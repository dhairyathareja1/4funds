# Shared data contracts between the bot's modules.

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class ExchangeRule:
    pair: str
    can_trade: bool
    price_precision: int
    amount_precision: int
    minimum_order_value: Decimal


@dataclass(frozen=True)
class MarketQuote:
    pair: str
    server_time_ms: int
    bid: Decimal
    ask: Decimal
    last: Decimal
    change_24h: Decimal
    quote_turnover_24h: Decimal


class CandleSource(str, Enum):
    EXCHANGE_OHLC = "exchange_ohlc"
    SAMPLED_SNAPSHOT = "sampled_snapshot"


@dataclass(frozen=True)
class Candle:
    pair: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: CandleSource = CandleSource.EXCHANGE_OHLC


@dataclass(frozen=True)
class MarketHistory:
    pair: str
    candles: tuple[Candle, ...]
    close_series: tuple[tuple[int, Decimal], ...]
    hourly_returns: tuple[tuple[int, Decimal], ...]


@dataclass(frozen=True)
class WalletAsset:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class WalletSnapshot:
    server_time_ms: int
    assets: tuple[WalletAsset, ...]


@dataclass(frozen=True)
class PendingOrderSummary:
    total_pending: int
    order_pairs: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CancellationResult:
    order_ids: tuple[str, ...]


@dataclass(frozen=True)
class TargetWeight:
    pair: str | None
    weight: Decimal
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    pair: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    pair: str
    status: str
    order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    commission: Decimal = Decimal("0")
    message: str = ""
