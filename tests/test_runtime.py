import csv
import inspect
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from fourfunds.data import HOUR_MS, SQLiteMarketDataStore
from fourfunds.models import (
    Candle,
    ExchangeRule,
    MarketQuote,
    WalletAsset,
    WalletSnapshot,
)
from fourfunds.risk import RiskManager
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


def _candles(pair=PAIR):
    closes = (Decimal("100"), Decimal("102"), Decimal("104"))
    return tuple(
        Candle(
            pair=pair,
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


def _write_history(path, candles=None):
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
        for candle in _candles() if candles is None else candles:
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


def _quote(pair=PAIR, timestamp_ms=END_TIME_MS):
    return MarketQuote(
        pair=pair,
        server_time_ms=timestamp_ms,
        bid=Decimal("103"),
        ask=Decimal("105"),
        last=Decimal("104"),
        change_24h=Decimal("0.04"),
        quote_turnover_24h=Decimal("500000"),
    )


class _TestClient:
    def __init__(self, quotes):
        self._quotes = quotes

    def get_exchange_info(self):
        return {
            pair: ExchangeRule(
                pair=pair,
                can_trade=True,
                price_precision=2,
                amount_precision=4,
                minimum_order_value=Decimal("1"),
            )
            for pair in self._quotes
        }

    def get_tickers(self):
        return self._quotes

    def get_balance(self):
        return WalletSnapshot(
            server_time_ms=max(quote.server_time_ms for quote in self._quotes.values()),
            assets=(WalletAsset("USD", Decimal("10000"), Decimal("0")),),
        )


class _TestCycleJournal:
    def begin(self, cycle_id, scheduled_at_ms, started_at_ms, decision):
        return True

    def checkpoint(self, cycle_id, decision):
        pass

    def finish(self, cycle_id, completed_at_ms, status, decision):
        self.status = status
        self.decision = decision


class _TestRiskStateStore:
    def load(self):
        return None

    def save(self, state):
        pass


def _runner(store, csv_path=None, quotes=None, clock_ms=None):
    quotes = quotes or {PAIR: _quote()}
    env = {
        "BOT_MODE": "read_only",
        "STRATEGY_MOMENTUM_LOOKBACK_HOURS": "2",
        "STRATEGY_TREND_LOOKBACK_HOURS": "3",
        "STRATEGY_MINIMUM_QUOTE_TURNOVER_24H": "0",
    }
    if csv_path is not None:
        env["MARKET_HISTORY_CSV"] = str(csv_path)
    settings = load_settings(env)
    return BotRunner(
        settings=settings,
        client=_TestClient(quotes),
        market_data=store,
        strategy=BaselineStrategy(),
        planner=RiskManager(),
        executor=None,
        cycle_journal=_TestCycleJournal(),
        risk_state_store=_TestRiskStateStore(),
        clock_ms=clock_ms or (lambda: max(q.server_time_ms for q in quotes.values())),
    ), settings


class RuntimeHistoryBackfillTests(unittest.TestCase):
    def test_backfills_empty_store_before_strategy_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            _write_history(csv_path)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            runner, _ = _runner(store, csv_path)

            candles, histories, errors, repairs = runner._load_history(
                {PAIR: _quote()}, END_TIME_MS
            )

            self.assertEqual(len(candles[PAIR]), 3)
            self.assertIn(PAIR, histories)
            self.assertIn(PAIR, errors)
            self.assertTrue(repairs[PAIR]["repair_attempted"])
            self.assertTrue(repairs[PAIR]["repair_succeeded"])

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

            candles, histories, errors, repairs = runner._load_history(
                {PAIR: _quote()}, END_TIME_MS
            )

            self.assertEqual(len(candles[PAIR]), 3)
            self.assertIn(PAIR, histories)
            self.assertEqual(errors, {})
            self.assertEqual(repairs, {})

    def test_stale_pair_does_not_refuse_cycle_with_healthy_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            quote_time_ms = END_TIME_MS + 10 * 60 * 1000
            quotes = {
                PAIR: _quote(PAIR, quote_time_ms),
                "ETH/USD": _quote("ETH/USD", quote_time_ms),
            }
            store.record_candles(_candles(PAIR))
            store.record_candles((_candles("ETH/USD")[0],))
            runner, _ = _runner(
                store,
                quotes=quotes,
                clock_ms=lambda: quote_time_ms,
            )

            outcome = runner.run_once(quote_time_ms)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                outcome.decision["strategy"]["targets"][0]["pair"], PAIR
            )
            self.assertEqual(
                set(outcome.decision["inputs"]["market_history"]), {PAIR}
            )
            self.assertIn("ETH/USD", outcome.decision["inputs"]["history_errors"])
            self.assertFalse(
                outcome.decision["inputs"]["history_repairs"]["ETH/USD"][
                    "repair_attempted"
                ]
            )

    def test_gappy_pair_is_repaired_and_original_error_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            history = _candles()
            _write_history(csv_path, history)
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            store.record_candles((history[0], history[2]))
            runner, _ = _runner(store, csv_path)

            candles, histories, errors, repairs = runner._load_history(
                {PAIR: _quote()}, END_TIME_MS
            )

            self.assertEqual(len(candles[PAIR]), 3)
            self.assertIn(PAIR, histories)
            self.assertIn("missing 1 hourly bars", errors[PAIR])
            self.assertTrue(repairs[PAIR]["repair_attempted"])
            self.assertTrue(repairs[PAIR]["repair_succeeded"])
            self.assertIsNone(repairs[PAIR]["repair_error"])

    def test_unrepairable_pair_is_excluded_while_healthy_pair_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "history.csv"
            _write_history(csv_path, _candles(PAIR))
            store = SQLiteMarketDataStore(root / "market.sqlite3")
            quote_time_ms = END_TIME_MS + 10 * 60 * 1000
            quotes = {
                PAIR: _quote(PAIR, quote_time_ms),
                "ETH/USD": _quote("ETH/USD", quote_time_ms),
            }
            store.record_candles(_candles(PAIR))
            eth_history = _candles("ETH/USD")
            store.record_candles((eth_history[0], eth_history[2]))
            runner, _ = _runner(
                store,
                csv_path,
                quotes=quotes,
                clock_ms=lambda: quote_time_ms,
            )

            outcome = runner.run_once(quote_time_ms)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                set(outcome.decision["inputs"]["market_history"]), {PAIR}
            )
            self.assertIn("ETH/USD", outcome.decision["inputs"]["history_errors"])
            repair = outcome.decision["inputs"]["history_repairs"]["ETH/USD"]
            self.assertTrue(repair["repair_attempted"])
            self.assertFalse(repair["repair_succeeded"])
            self.assertIn("missing 1 hourly bars", repair["repair_error"])

    def test_stale_ticker_snapshot_still_refuses_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketDataStore(Path(directory) / "market.sqlite3")
            quotes = {PAIR: _quote()}
            runner, _ = _runner(
                store,
                quotes=quotes,
                clock_ms=lambda: END_TIME_MS + 10 * 60 * 1000,
            )

            outcome = runner.run_once(END_TIME_MS)

            self.assertEqual(outcome.status, "refused")
            self.assertIn("Ticker snapshot is stale", outcome.decision["errors"][0])
            self.assertNotIn("strategy", outcome.decision)
