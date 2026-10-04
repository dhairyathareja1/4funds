import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from fourfunds.models import (
    ExchangeRule,
    MarketQuote,
    OrderSide,
    PortfolioRiskState,
    TargetWeight,
    WalletAsset,
    WalletSnapshot,
)
from fourfunds.risk import RiskManager
from fourfunds.runtime import BotRunner
from fourfunds.runtime_store import SQLiteRiskStateStore
from fourfunds.settings import load_settings

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


class DrawdownCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "runtime.sqlite3"
        self.store = SQLiteRiskStateStore(self.database_path)
        self.settings = replace(
            load_settings({}),
            drawdown_cooldown_hours=2,
            max_daily_loss_fraction=Decimal("0.50"),
        )
        self.runner = self._runner(self.store, self.settings)
        self.start_ms = 200 * DAY_MS + HOUR_MS
        self.store.save(
            PortfolioRiskState(
                currency="USD",
                utc_day_start_ms=200 * DAY_MS,
                day_start_value=Decimal("100"),
                high_water_mark=Decimal("100"),
            )
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_drawdown_trip_starts_cooldown_when_portfolio_is_cash(self):
        tripped, transitions = self.runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms, btc="0.8")
        )

        self.assertTrue(tripped.drawdown_breaker_active)
        self.assertTrue(
            any(
                event["event"] == "breaker_trip"
                and event.get("cause") == "drawdown"
                for event in transitions
            )
        )
        self.assertIsNone(tripped.cash_cooldown_started_ms)

        cash_state, transitions = self.runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms + HOUR_MS)
        )
        self.assertEqual(cash_state.cash_cooldown_started_ms, self.start_ms + HOUR_MS)
        self.assertIn("cooldown_start", self._event_names(transitions))

    def test_cooldown_state_survives_store_restart(self):
        self.runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms, btc="0.8")
        )
        expected, _ = self.runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms + HOUR_MS)
        )

        restarted_store = SQLiteRiskStateStore(self.database_path)
        self.assertEqual(restarted_store.load(), expected)

    def test_unsellable_dust_allows_cooldown_recovery(self):
        self._save_active_cooldown()
        wallet = WalletSnapshot(
            self.start_ms + 3 * HOUR_MS,
            (
                WalletAsset("USD", Decimal("79.95"), Decimal("0")),
                WalletAsset("BTC", Decimal("0.0005"), Decimal("0")),
            ),
        )
        quote, rule = self._market_data(minimum="1", bid="100")

        state, transitions = self.runner._advance_risk_state(
            "USD", Decimal("80"), wallet, quote, rule
        )

        self.assertFalse(state.drawdown_breaker_active)
        self.assertEqual(state.high_water_mark, Decimal("80"))
        self.assertIn("drawdown_recovery", self._event_names(transitions))

    def test_sellable_and_locked_balances_prevent_cooldown_recovery(self):
        quote, rule = self._market_data(minimum="1", bid="100")
        wallets = (
            WalletSnapshot(
                self.start_ms + 3 * HOUR_MS,
                (
                    WalletAsset("USD", Decimal("78"), Decimal("0")),
                    WalletAsset("BTC", Decimal("0.02"), Decimal("0")),
                ),
            ),
            WalletSnapshot(
                self.start_ms + 3 * HOUR_MS,
                (
                    WalletAsset("USD", Decimal("79.99"), Decimal("0")),
                    WalletAsset("BTC", Decimal("0"), Decimal("0.0001")),
                ),
            ),
        )

        for wallet in wallets:
            with self.subTest(wallet=wallet):
                self._save_active_cooldown()
                state, transitions = self.runner._advance_risk_state(
                    "USD", Decimal("80"), wallet, quote, rule
                )
                self.assertTrue(state.drawdown_breaker_active)
                self.assertEqual(state.high_water_mark, Decimal("100"))
                self.assertNotIn("drawdown_recovery", self._event_names(transitions))

    def test_previous_risk_state_schema_migrates_without_losing_state(self):
        legacy_database_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_database_path) as connection:
            connection.execute(
                """
                CREATE TABLE portfolio_risk_state (
                    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                    currency TEXT NOT NULL,
                    utc_day_start_ms INTEGER NOT NULL,
                    day_start_value TEXT NOT NULL,
                    high_water_mark TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO portfolio_risk_state VALUES (1, 'USD', ?, '125.50', '150')
                """,
                (200 * DAY_MS,),
            )

        migrated_store = SQLiteRiskStateStore(legacy_database_path)

        self.assertEqual(
            migrated_store.load(),
            PortfolioRiskState(
                currency="USD",
                utc_day_start_ms=200 * DAY_MS,
                day_start_value=Decimal("125.50"),
                high_water_mark=Decimal("150"),
            ),
        )

    def test_cooldown_recovery_resets_high_water_and_allows_buys(self):
        with self.assertLogs("fourfunds.runtime", level="WARNING") as captured:
            self.runner._advance_risk_state(
                "USD", Decimal("80"), self._wallet(self.start_ms, btc="0.8")
            )
            self.runner._advance_risk_state(
                "USD", Decimal("80"), self._wallet(self.start_ms + HOUR_MS)
            )
            recovered, transitions = self.runner._advance_risk_state(
                "USD", Decimal("80"), self._wallet(self.start_ms + 3 * HOUR_MS)
            )

        self.assertFalse(recovered.drawdown_breaker_active)
        self.assertEqual(recovered.high_water_mark, Decimal("80"))
        self.assertIn("cooldown_completion", self._event_names(transitions))
        self.assertIn("drawdown_recovery", self._event_names(transitions))
        self.assertIn("buying_enabled_again", self._event_names(transitions))
        log_text = "\n".join(captured.output)
        for event_name in (
            "breaker_trip",
            "cooldown_start",
            "cooldown_completion",
            "drawdown_recovery",
            "buying_enabled_again",
        ):
            self.assertIn(f"transition={event_name}", log_text)
        plan = self._plan(
            recovered, self._wallet(self.start_ms + 3 * HOUR_MS), self.settings
        )
        self.assertTrue(any(intent.side is OrderSide.BUY for intent in plan.intents))

    def test_daily_loss_guard_still_blocks_after_drawdown_recovery(self):
        settings = replace(
            self.settings, max_daily_loss_fraction=Decimal("0.10")
        )
        runner = self._runner(self.store, settings)
        runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms, btc="0.8")
        )
        runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms + HOUR_MS)
        )
        recovered, transitions = runner._advance_risk_state(
            "USD", Decimal("80"), self._wallet(self.start_ms + 3 * HOUR_MS)
        )

        self.assertFalse(recovered.drawdown_breaker_active)
        self.assertTrue(recovered.daily_loss_breaker_active)
        self.assertNotIn("buying_enabled_again", self._event_names(transitions))
        plan = self._plan(
            recovered, self._wallet(self.start_ms + 3 * HOUR_MS), settings
        )
        self.assertFalse(any(intent.side is OrderSide.BUY for intent in plan.intents))

    def _runner(self, store, settings):
        return BotRunner(
            settings=settings,
            client=None,
            market_data=None,
            strategy=None,
            planner=RiskManager(),
            executor=None,
            cycle_journal=None,
            risk_state_store=store,
        )

    def _event_names(self, events):
        return tuple(event["event"] for event in events)

    def _save_active_cooldown(self):
        self.store.save(
            PortfolioRiskState(
                currency="USD",
                utc_day_start_ms=200 * DAY_MS,
                day_start_value=Decimal("100"),
                high_water_mark=Decimal("100"),
                drawdown_breaker_active=True,
                cash_cooldown_started_ms=self.start_ms,
            )
        )

    def _market_data(self, minimum, bid):
        pair = "BTC/USD"
        return (
            {
                pair: MarketQuote(
                    pair=pair,
                    server_time_ms=self.start_ms + 3 * HOUR_MS,
                    bid=Decimal(bid),
                    ask=Decimal(bid),
                    last=Decimal(bid),
                    change_24h=Decimal("0"),
                    quote_turnover_24h=Decimal("1000"),
                )
            },
            {
                pair: ExchangeRule(
                    pair=pair,
                    can_trade=True,
                    price_precision=2,
                    amount_precision=6,
                    minimum_order_value=Decimal(minimum),
                )
            },
        )

    def _wallet(self, timestamp, btc="0"):
        assets = [WalletAsset("USD", Decimal("80"), Decimal("0"))]
        if Decimal(btc):
            assets.append(WalletAsset("BTC", Decimal(btc), Decimal("0")))
        return WalletSnapshot(timestamp, tuple(assets))

    def _plan(self, state, wallet, settings):
        quote = MarketQuote(
            pair="BTC/USD",
            server_time_ms=wallet.server_time_ms,
            bid=Decimal("99"),
            ask=Decimal("100"),
            last=Decimal("100"),
            change_24h=Decimal("0"),
            quote_turnover_24h=Decimal("1000000"),
        )
        rule = ExchangeRule(
            pair="BTC/USD",
            can_trade=True,
            price_precision=2,
            amount_precision=6,
            minimum_order_value=Decimal("1"),
        )
        return RiskManager().plan(
            (
                TargetWeight("BTC/USD", Decimal("0.20"), "test target"),
                TargetWeight(None, Decimal("0.80"), "cash"),
            ),
            wallet,
            {"BTC/USD": quote},
            {"BTC/USD": rule},
            settings,
            state,
        )


if __name__ == "__main__":
    unittest.main()
