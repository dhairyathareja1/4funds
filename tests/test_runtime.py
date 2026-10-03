import csv
import inspect
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from fourfunds.data import HOUR_MS, SQLiteMarketDataStore
from fourfunds.models import Candle, ExchangeRule, MarketQuote
from fourfunds.runtime import BotRunner
from fourfunds.settings import load_settings
from fourfunds.strategy import BaselineStrategy

FIRST_HOUR = (1_700_000_000_000 // HOUR_MS + 2) * HOUR_MS
PAIR = "BTC/USD"
END_TIME_MS = FIRST_HOUR + 3 * HOUR_MS


class RuntimeTests(unittest.TestCase):
    def test_default_clock_reports_current_epoch_milliseconds(self):
        default_clock = inspect.signature(BotRunner.__init__).parameters[
            "clock_ms"
        ].default

        before_ms = time.time_ns() // 1_000_000
        reading_ms = default_clock()
        after_ms = time.time_ns() // 1_000_000

        self.assertGreaterEqual(reading_ms, before_ms)
        self.assertLessEqual(reading_ms, after_ms)


def _candles():
    closes = (Decimal("100"), Decimal("102"), Decimal("104"))
    return tuple(
        Candle(
            pair=PAIR,
            open_time_ms=FIRST_HOUR + index * HOUR_MS,
            close_time_ms=FIRST_HOUR + (index + 1) * HOUR_MS,
            open=close - Decimal("1"),
            high=close,
            low=close - Decimal("1"),
            close=close,
            volume=Decimal("1000"),
        )
        for index, close in enumerate(closes)
    )


def _write_history(path):
    with path.open("w", newline="", encoding="utf-8") as csv_file:
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
        for candle in _candles():
            writer.writerow(
                [
                    candle.pair,
                    candle.open_time_ms,
                    candle.close_time_ms,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                ]
            )


def _quote():
    return MarketQuote(
        pair=PAIR,
        server_time_ms=END_TIME_MS,
        bid=Decimal("103"),
        ask=Decimal("105"),
        last=Decimal("104"),
        change_24h=Decimal("0.04"),
        quote_turnover_24h=Decimal("500000"),
    )


def _runner(store, csv_path):
    settings = load_settings(
        {
            "MARKET_HISTORY_CSV": str(csv_path),
            "STRATEGY_MOMENTUM_LOOKBACK_HOURS": "2",
            "STRATEGY_TREND_LOOKBACK_HOURS": "3",
            "STRATEGY_MINIMUM_QUOTE_TURNOVER_24H": "0",
        }
    )
    return BotRunner(
        settings=settings,
        client=None,
        market_data=store,
        strategy=BaselineStrategy(),
        planner=None,
        executor=None,
        cycle_journal=None,
        risk_state_store=None,
    ), settings


class RuntimeHistoryBackfillTests(unittest.TestCase):
    def test_backfills_empty_store_before_strategy_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            _write_history(csv_path)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            runner, _ = _runner(store, csv_path)

            candles, histories, errors, stale = runner._load_history(
                {PAIR: _quote()}, END_TIME_MS
            )

            self.assertEqual(len(candles[PAIR]), 3)
            self.assertIn(PAIR, histories)
            self.assertEqual(errors, {})
            self.assertEqual(stale, ())

    def test_backfill_is_idempotent_when_history_is_loaded_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            _write_history(csv_path)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            runner, _ = _runner(store, csv_path)
            quotes = {PAIR: _quote()}

            first = runner._load_history(quotes, END_TIME_MS)
            store.record_candles_if_missing(_candles())
            second = runner._load_history(quotes, END_TIME_MS)

            self.assertEqual(first[0], second[0])
            self.assertEqual(len(second[0][PAIR]), 3)

    def test_backfilled_history_can_produce_non_cash_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            _write_history(csv_path)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            runner, settings = _runner(store, csv_path)
            quote = _quote()
            candles, _, _, _ = runner._load_history({PAIR: quote}, END_TIME_MS)

            decision = BaselineStrategy().decide(
                {PAIR: quote},
                candles,
                {
                    PAIR: ExchangeRule(
                        pair=PAIR,
                        can_trade=True,
                        price_precision=2,
                        amount_precision=4,
                        minimum_order_value=Decimal("1"),
                    )
                },
                settings,
            )

            self.assertEqual(decision.targets[0].pair, PAIR)
            self.assertGreater(decision.targets[0].weight, Decimal("0"))

    def test_skips_configured_source_when_required_history_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_csv = root / "not-present.csv"
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            store.record_candles(_candles())
            runner, _ = _runner(store, missing_csv)

            candles, histories, errors, stale = runner._load_history(
                {PAIR: _quote()}, END_TIME_MS
            )

            self.assertEqual(len(candles[PAIR]), 3)
            self.assertIn(PAIR, histories)
            self.assertEqual(errors, {})
            self.assertEqual(stale, ())
