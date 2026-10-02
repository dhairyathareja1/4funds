import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fourfunds.data import (
    HOUR_MS,
    DuplicateCandleError,
    DuplicateSnapshotError,
    InsufficientMarketHistoryError,
    InvalidMarketDataError,
    MissingMarketDataError,
    SQLiteMarketDataStore,
    StaleMarketDataError,
    load_historical_candles,
)
from fourfunds.models import Candle, CandleSource, MarketQuote

FIRST_HOUR = (1_700_000_000_000 // HOUR_MS + 2) * HOUR_MS


def quote(timestamp_ms, price):
    value = Decimal(str(price))
    return MarketQuote(
        pair="BTC/USD",
        server_time_ms=timestamp_ms,
        bid=value - Decimal("1"),
        ask=value + Decimal("1"),
        last=value,
        change_24h=Decimal("0.01"),
        quote_turnover_24h=Decimal("500000"),
    )


def candle(open_time_ms, open_price, high, low, close):
    return Candle(
        pair="BTC/USD",
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + HOUR_MS,
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
    )


class SQLiteMarketDataStoreTests(unittest.TestCase):
    def test_persists_snapshots_and_builds_sampled_hourly_features(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "market.sqlite3"
            store = SQLiteMarketDataStore(database_path)
            quotes = [
                quote(FIRST_HOUR + 5 * 60_000, 100),
                quote(FIRST_HOUR + 20 * 60_000, 105),
                quote(FIRST_HOUR + 30 * 60_000, 100),
                quote(FIRST_HOUR + HOUR_MS + 5 * 60_000, 110),
                quote(FIRST_HOUR + HOUR_MS + 20 * 60_000, 108),
                quote(FIRST_HOUR + HOUR_MS + 30 * 60_000, 110),
                quote(FIRST_HOUR + 2 * HOUR_MS + 10 * 60_000, 121),
            ]
            for item in quotes:
                store.record_snapshot({item.pair: item})

            reopened_store = SQLiteMarketDataStore(database_path)
            history = reopened_store.get_market_history(
                "BTC/USD",
                end_time_ms=FIRST_HOUR + 3 * HOUR_MS,
                window_hours=3,
            )

        self.assertEqual(len(history.candles), 3)
        self.assertEqual(history.candles[0].high, Decimal("105"))
        self.assertEqual(history.candles[0].low, Decimal("100"))
        self.assertEqual(history.candles[0].close, Decimal("100"))
        self.assertTrue(
            all(bar.source is CandleSource.SAMPLED_SNAPSHOT for bar in history.candles)
        )
        self.assertEqual(history.candles[0].volume, Decimal("0"))
        self.assertEqual(
            [value for _, value in history.close_series],
            [Decimal("100"), Decimal("110"), Decimal("121")],
        )
        self.assertEqual(
            [value for _, value in history.hourly_returns],
            [Decimal("0.1"), Decimal("0.1")],
        )

    def test_loads_and_persists_documented_historical_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "history.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(
                    [
                        "pair",
                        "open_time_ms",
                        "close_time_ms",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    ]
                )
                writer.writerow(
                    [
                        "BTC/USD",
                        FIRST_HOUR,
                        FIRST_HOUR + HOUR_MS,
                        "100",
                        "105",
                        "98",
                        "100",
                        "1000",
                    ]
                )
                writer.writerow(
                    [
                        "BTC/USD",
                        FIRST_HOUR + HOUR_MS,
                        FIRST_HOUR + 2 * HOUR_MS,
                        "100",
                        "110",
                        "99",
                        "110",
                        "1200",
                    ]
                )

            candles = load_historical_candles(csv_path)
            store = SQLiteMarketDataStore(Path(directory) / "market.sqlite3")
            store.record_candles(candles)
            history = store.get_market_history(
                "BTC/USD",
                end_time_ms=FIRST_HOUR + 2 * HOUR_MS,
                window_hours=2,
            )

        self.assertEqual(len(candles), 2)
        self.assertTrue(
            all(item.source is CandleSource.EXCHANGE_OHLC for item in history.candles)
        )
        self.assertEqual(history.candles[-1].close, Decimal("110"))
        self.assertEqual(history.hourly_returns[0][1], Decimal("0.1"))

    def test_exchange_candles_override_sampled_bars_and_partial_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketDataStore(Path(directory) / "market.sqlite3")
            store.record_candles(
                [
                    candle(FIRST_HOUR, 100, 105, 98, 100),
                    candle(FIRST_HOUR + HOUR_MS, 100, 110, 99, 110),
                ]
            )
            store.record_snapshot(
                {
                    "BTC/USD": quote(
                        FIRST_HOUR + HOUR_MS + 20 * 60_000, 120
                    )
                }
            )
            store.record_snapshot(
                {"BTC/USD": quote(FIRST_HOUR + 2 * HOUR_MS + 10 * 60_000, 130)}
            )

            history = store.get_market_history(
                "BTC/USD",
                end_time_ms=FIRST_HOUR + 2 * HOUR_MS + 30 * 60_000,
                window_hours=2,
            )

        self.assertEqual(len(history.candles), 2)
        self.assertTrue(
            all(item.source is CandleSource.EXCHANGE_OHLC for item in history.candles)
        )
        self.assertEqual(history.candles[-1].close, Decimal("110"))

    def test_rejects_duplicate_snapshots_and_candles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketDataStore(Path(directory) / "market.sqlite3")
            snapshot = {"BTC/USD": quote(FIRST_HOUR + 1, 100)}
            history = [candle(FIRST_HOUR, 100, 101, 99, 100)]

            store.record_snapshot(snapshot)
            store.record_candles(history)
            with self.assertRaises(DuplicateSnapshotError):
                store.record_snapshot(snapshot)
            with self.assertRaises(DuplicateCandleError):
                store.record_candles(history)

    def test_reports_insufficient_stale_and_missing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketDataStore(Path(directory) / "market.sqlite3")
            end_time_ms = FIRST_HOUR + 3 * HOUR_MS

            with self.assertRaises(InsufficientMarketHistoryError):
                store.get_market_history(
                    "BTC/USD", end_time_ms=end_time_ms, window_hours=3
                )

            store.record_snapshot(
                {"BTC/USD": quote(FIRST_HOUR + 2 * HOUR_MS + 5 * 60_000, 121)}
            )
            with self.assertRaises(StaleMarketDataError):
                store.get_market_history(
                    "BTC/USD",
                    end_time_ms=end_time_ms,
                    window_hours=3,
                    max_age_ms=60_000,
                )

            with self.assertRaises(MissingMarketDataError):
                store.get_market_history(
                    "BTC/USD",
                    end_time_ms=end_time_ms,
                    window_hours=3,
                    max_age_ms=HOUR_MS,
                )

    def test_rejects_invalid_historical_csv_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "invalid.csv"
            csv_path.write_text(
                "pair,open_time_ms,close_time_ms,open,high,low,close,volume\n"
                f"BTC/USD,{FIRST_HOUR},{FIRST_HOUR + 60_000},100,105,98,102,10\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(InvalidMarketDataError, "one UTC hour"):
                load_historical_candles(csv_path)


if __name__ == "__main__":
    unittest.main()
