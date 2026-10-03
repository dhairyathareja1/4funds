import csv
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from fourfunds.models import Candle, CandleSource, MarketHistory, MarketQuote

HOUR_MS = 60 * 60 * 1000
DEFAULT_MAX_DATA_AGE_MS = 2 * HOUR_MS
HISTORICAL_CSV_FIELDS = (
    "pair",
    "open_time_ms",
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class MarketDataError(RuntimeError):
    pass


class InvalidMarketDataError(MarketDataError):
    pass


class DuplicateSnapshotError(MarketDataError):
    pass


class DuplicateCandleError(MarketDataError):
    pass


class InsufficientMarketHistoryError(MarketDataError):
    pass


class MissingMarketDataError(InsufficientMarketHistoryError):
    pass


class StaleMarketDataError(MarketDataError):
    pass


class MarketDataStore(Protocol):
    def record_snapshot(self, tickers: Mapping[str, MarketQuote]) -> None:
        ...

    def record_candles(self, candles: Sequence[Candle]) -> None:
        ...

    def record_candles_if_missing(self, candles: Sequence[Candle]) -> None:
        ...

    def get_market_history(
        self,
        pair: str,
        *,
        end_time_ms: int,
        window_hours: int,
        max_age_ms: int = DEFAULT_MAX_DATA_AGE_MS,
    ) -> MarketHistory:
        ...


class SQLiteMarketDataStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def record_snapshot(self, tickers: Mapping[str, MarketQuote]) -> None:
        if not tickers:
            raise InvalidMarketDataError("A market snapshot cannot be empty.")

        quotes = tuple(tickers.values())
        timestamp_ms = quotes[0].server_time_ms
        for pair, quote in tickers.items():
            if pair != pair.strip() or pair != quote.pair:
                raise InvalidMarketDataError(
                    f"Snapshot key {pair!r} does not match quote pair {quote.pair!r}."
                )
            _validate_quote(quote)
            if quote.server_time_ms != timestamp_ms:
                raise InvalidMarketDataError(
                    "All quotes in a snapshot must share one server timestamp."
                )

        try:
            with closing(self._connect()) as connection, connection:
                connection.executemany(
                    """
                    INSERT INTO market_snapshots (
                        pair, server_time_ms, bid, ask, last, change_24h,
                        quote_turnover_24h
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            quote.pair,
                            quote.server_time_ms,
                            str(quote.bid),
                            str(quote.ask),
                            str(quote.last),
                            str(quote.change_24h),
                            str(quote.quote_turnover_24h),
                        )
                        for quote in quotes
                    ],
                )
        except sqlite3.IntegrityError:
            raise DuplicateSnapshotError(
                f"A snapshot already exists for {timestamp_ms}."
            ) from None

    def record_candles(self, candles: Sequence[Candle]) -> None:
        if not candles:
            raise InvalidMarketDataError("Candle history cannot be empty.")

        for candle in candles:
            _validate_candle(candle)

        try:
            with closing(self._connect()) as connection, connection:
                connection.executemany(
                    """
                    INSERT INTO market_candles (
                        pair, open_time_ms, close_time_ms, open, high, low,
                        close, volume, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            candle.pair,
                            candle.open_time_ms,
                            candle.close_time_ms,
                            str(candle.open),
                            str(candle.high),
                            str(candle.low),
                            str(candle.close),
                            str(candle.volume),
                            candle.source.value,
                        )
                        for candle in candles
                    ],
                )
        except sqlite3.IntegrityError:
            raise DuplicateCandleError(
                "One or more candles already exist in the requested history."
            ) from None

    def record_candles_if_missing(self, candles: Sequence[Candle]) -> None:
        if not candles:
            return

        for candle in candles:
            _validate_candle(candle)

        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO market_candles (
                    pair, open_time_ms, close_time_ms, open, high, low,
                    close, volume, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        candle.pair,
                        candle.open_time_ms,
                        candle.close_time_ms,
                        str(candle.open),
                        str(candle.high),
                        str(candle.low),
                        str(candle.close),
                        str(candle.volume),
                        candle.source.value,
                    )
                    for candle in candles
                ],
            )

    def get_market_history(
        self,
        pair: str,
        *,
        end_time_ms: int,
        window_hours: int,
        max_age_ms: int = DEFAULT_MAX_DATA_AGE_MS,
    ) -> MarketHistory:
        pair = pair.strip()
        if not pair:
            raise ValueError("Pair cannot be empty.")
        if end_time_ms <= 0 or window_hours <= 0 or max_age_ms <= 0:
            raise ValueError("History time, window, and maximum age must be positive.")

        window_end_ms = end_time_ms // HOUR_MS * HOUR_MS
        window_start_ms = window_end_ms - window_hours * HOUR_MS
        if window_start_ms <= 0:
            raise ValueError("History window must start after the Unix epoch.")

        candles = self._read_candles(pair, window_start_ms, window_end_ms)
        snapshots = self._read_snapshots(pair, window_start_ms, window_end_ms)
        latest_observations = [candle.close_time_ms for candle in candles]
        latest_observations.extend(
            int(snapshot["server_time_ms"]) for snapshot in snapshots
        )
        if not latest_observations:
            raise InsufficientMarketHistoryError(
                f"No hourly history is available for {pair}."
            )

        if end_time_ms - max(latest_observations) > max_age_ms:
            raise StaleMarketDataError(f"Latest market data for {pair} is stale.")

        bars_by_hour = {candle.open_time_ms: candle for candle in candles}
        for sampled_bar in _sample_hourly_bars(pair, snapshots):
            existing = bars_by_hour.get(sampled_bar.open_time_ms)
            if existing is None or existing.source is not CandleSource.EXCHANGE_OHLC:
                bars_by_hour[sampled_bar.open_time_ms] = sampled_bar

        bars = tuple(bars_by_hour[key] for key in sorted(bars_by_hour))
        missing_hours = [
            hour_start
            for hour_start in range(window_start_ms, window_end_ms, HOUR_MS)
            if hour_start not in bars_by_hour
        ]
        if missing_hours:
            raise MissingMarketDataError(
                f"Market history for {pair} is missing "
                f"{len(missing_hours)} hourly bars."
            )

        close_series = tuple(
            (candle.close_time_ms, candle.close) for candle in bars
        )
        hourly_returns = tuple(
            (
                bars[index].close_time_ms,
                bars[index].close / bars[index - 1].close - Decimal("1"),
            )
            for index in range(1, len(bars))
        )
        return MarketHistory(
            pair=pair,
            candles=bars,
            close_series=close_series,
            hourly_returns=hourly_returns,
        )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    pair TEXT NOT NULL,
                    server_time_ms INTEGER NOT NULL,
                    bid TEXT NOT NULL,
                    ask TEXT NOT NULL,
                    last TEXT NOT NULL,
                    change_24h TEXT NOT NULL,
                    quote_turnover_24h TEXT NOT NULL,
                    PRIMARY KEY (pair, server_time_ms)
                );
                CREATE TABLE IF NOT EXISTS market_candles (
                    pair TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (pair, open_time_ms)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _read_candles(
        self, pair: str, start_time_ms: int, end_time_ms: int
    ) -> tuple[Candle, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT pair, open_time_ms, close_time_ms, open, high, low,
                       close, volume, source
                FROM market_candles
                WHERE pair = ? AND open_time_ms >= ? AND open_time_ms < ?
                ORDER BY open_time_ms
                """,
                (pair, start_time_ms, end_time_ms),
            ).fetchall()
        return tuple(_candle_from_row(row) for row in rows)

    def _read_snapshots(
        self, pair: str, start_time_ms: int, end_time_ms: int
    ) -> tuple[sqlite3.Row, ...]:
        with closing(self._connect()) as connection:
            return tuple(
                connection.execute(
                    """
                    SELECT server_time_ms, last
                    FROM market_snapshots
                    WHERE pair = ? AND server_time_ms >= ? AND server_time_ms < ?
                    ORDER BY server_time_ms
                    """,
                    (pair, start_time_ms, end_time_ms),
                ).fetchall()
            )


def load_historical_candles(csv_path: str | Path) -> tuple[Candle, ...]:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise InvalidMarketDataError("Historical CSV must include a header row.")
        missing_fields = set(HISTORICAL_CSV_FIELDS) - set(reader.fieldnames)
        if missing_fields:
            missing = ", ".join(sorted(missing_fields))
            raise InvalidMarketDataError(
                f"Historical CSV is missing required columns: {missing}."
            )

        candles: list[Candle] = []
        seen_hours: set[tuple[str, int]] = set()
        for row_number, row in enumerate(reader, start=2):
            try:
                candle = Candle(
                    pair=(row["pair"] or "").strip(),
                    open_time_ms=_parse_csv_integer(
                        row["open_time_ms"], "open_time_ms"
                    ),
                    close_time_ms=_parse_csv_integer(
                        row["close_time_ms"], "close_time_ms"
                    ),
                    open=_parse_csv_decimal(row["open"], "open"),
                    high=_parse_csv_decimal(row["high"], "high"),
                    low=_parse_csv_decimal(row["low"], "low"),
                    close=_parse_csv_decimal(row["close"], "close"),
                    volume=_parse_csv_decimal(row["volume"], "volume"),
                    source=CandleSource.EXCHANGE_OHLC,
                )
                _validate_candle(candle)
            except (KeyError, TypeError, InvalidMarketDataError) as exc:
                raise InvalidMarketDataError(
                    f"Invalid historical CSV row {row_number}: {exc}"
                ) from None

            key = (candle.pair, candle.open_time_ms)
            if key in seen_hours:
                raise DuplicateCandleError(
                    f"Historical CSV repeats {candle.pair} at {candle.open_time_ms}."
                )
            seen_hours.add(key)
            candles.append(candle)

    return tuple(sorted(candles, key=lambda item: (item.pair, item.open_time_ms)))


def _validate_quote(quote: MarketQuote) -> None:
    if not quote.pair.strip() or quote.server_time_ms <= 0:
        raise InvalidMarketDataError(
            "Quotes need a pair and positive server timestamp."
        )
    for field, value in (
        ("bid", quote.bid),
        ("ask", quote.ask),
        ("last", quote.last),
    ):
        _validate_decimal(field, value, minimum=Decimal("0"), exclusive=True)
    _validate_decimal("change_24h", quote.change_24h)
    _validate_decimal(
        "quote_turnover_24h", quote.quote_turnover_24h, minimum=Decimal("0")
    )


def _validate_candle(candle: Candle) -> None:
    if not candle.pair.strip():
        raise InvalidMarketDataError("Candles need a pair.")
    if (
        candle.open_time_ms <= 0
        or candle.open_time_ms % HOUR_MS != 0
        or candle.close_time_ms != candle.open_time_ms + HOUR_MS
    ):
        raise InvalidMarketDataError(
            "Candles must cover one UTC hour with aligned millisecond timestamps."
        )
    if not isinstance(candle.source, CandleSource):
        raise InvalidMarketDataError("Candle source is invalid.")

    for field in ("open", "high", "low", "close"):
        _validate_decimal(
            field, getattr(candle, field), minimum=Decimal("0"), exclusive=True
        )
    _validate_decimal("volume", candle.volume, minimum=Decimal("0"))
    if candle.high < max(candle.open, candle.close, candle.low):
        raise InvalidMarketDataError("Candle high is below another OHLC value.")
    if candle.low > min(candle.open, candle.close, candle.high):
        raise InvalidMarketDataError("Candle low is above another OHLC value.")


def _validate_decimal(
    field: str,
    value: Decimal,
    *,
    minimum: Decimal | None = None,
    exclusive: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidMarketDataError(f"{field} must be a finite decimal.")
    if minimum is not None and (
        value <= minimum if exclusive else value < minimum
    ):
        relation = "greater than" if exclusive else "at least"
        raise InvalidMarketDataError(f"{field} must be {relation} {minimum}.")


def _parse_csv_integer(value: str | None, field: str) -> int:
    if value is None or not value.strip().isdigit():
        raise InvalidMarketDataError(f"{field} must be a positive integer.")
    parsed = int(value.strip())
    if parsed <= 0:
        raise InvalidMarketDataError(f"{field} must be a positive integer.")
    return parsed


def _parse_csv_decimal(value: str | None, field: str) -> Decimal:
    if value is None:
        raise InvalidMarketDataError(f"{field} must be a decimal number.")
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        raise InvalidMarketDataError(f"{field} must be a decimal number.") from None
    if not parsed.is_finite():
        raise InvalidMarketDataError(f"{field} must be finite.")
    return parsed


def _sample_hourly_bars(
    pair: str, snapshots: Sequence[sqlite3.Row]
) -> tuple[Candle, ...]:
    hourly_prices: dict[int, list[tuple[int, Decimal]]] = defaultdict(list)
    for snapshot in snapshots:
        timestamp_ms = int(snapshot["server_time_ms"])
        hour_start = timestamp_ms // HOUR_MS * HOUR_MS
        hourly_prices[hour_start].append(
            (timestamp_ms, Decimal(snapshot["last"]))
        )

    bars: list[Candle] = []
    for hour_start, timestamped_prices in sorted(hourly_prices.items()):
        prices = [price for _, price in timestamped_prices]
        bars.append(
            Candle(
                pair=pair,
                open_time_ms=hour_start,
                close_time_ms=hour_start + HOUR_MS,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=Decimal("0"),
                source=CandleSource.SAMPLED_SNAPSHOT,
            )
        )
    return tuple(bars)


def _candle_from_row(row: sqlite3.Row) -> Candle:
    return Candle(
        pair=row["pair"],
        open_time_ms=int(row["open_time_ms"]),
        close_time_ms=int(row["close_time_ms"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]),
        source=CandleSource(row["source"]),
    )
