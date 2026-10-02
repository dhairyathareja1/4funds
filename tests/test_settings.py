import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from fourfunds import cli
from fourfunds.settings import RunMode, load_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_to_dry_run_with_validated_default_settings(self):
        settings = load_settings({})

        self.assertIs(settings.mode, RunMode.DRY_RUN)
        self.assertEqual(settings.base_url, "https://mock-api.roostoo.com")
        self.assertEqual(settings.momentum_lookback_hours, 24)
        self.assertEqual(settings.trend_lookback_hours, 168)
        self.assertEqual(settings.top_k, 3)
        self.assertEqual(settings.max_asset_weight, Decimal("0.25"))

    def test_loads_strategy_and_risk_values_from_environment(self):
        settings = load_settings(
            {
                "BOT_MODE": "read_only",
                "ROOSTOO_BASE_URL": "https://api.roostoo.com/v1/",
                "STRATEGY_MOMENTUM_LOOKBACK_HOURS": "12",
                "STRATEGY_TREND_LOOKBACK_HOURS": "72",
                "STRATEGY_TOP_K": "5",
                "STRATEGY_MINIMUM_QUOTE_TURNOVER_24H": "250000.50",
                "RISK_MAX_ASSET_WEIGHT": "0.20",
                "RISK_MAX_TOTAL_EXPOSURE": "0.80",
                "RISK_MINIMUM_CASH_RESERVE": "0.20",
                "RISK_MAX_DAILY_LOSS_FRACTION": "0.04",
                "RISK_MAX_DRAWDOWN_FRACTION": "0.12",
                "RISK_MAX_QUOTE_AGE_SECONDS": "90",
                "RISK_FEE_RATE": "0.002",
            }
        )

        self.assertIs(settings.mode, RunMode.READ_ONLY)
        self.assertEqual(settings.base_url, "https://api.roostoo.com/v1")
        self.assertEqual(settings.momentum_lookback_hours, 12)
        self.assertEqual(settings.trend_lookback_hours, 72)
        self.assertEqual(settings.top_k, 5)
        self.assertEqual(
            settings.minimum_quote_turnover_24h, Decimal("250000.50")
        )
        self.assertEqual(settings.max_asset_weight, Decimal("0.20"))
        self.assertEqual(settings.max_total_exposure, Decimal("0.80"))
        self.assertEqual(settings.minimum_cash_reserve, Decimal("0.20"))
        self.assertEqual(settings.max_daily_loss_fraction, Decimal("0.04"))
        self.assertEqual(settings.max_drawdown_fraction, Decimal("0.12"))
        self.assertEqual(settings.max_quote_age_seconds, 90)
        self.assertEqual(settings.fee_rate, Decimal("0.002"))

    def test_rejects_malformed_or_inconsistent_settings(self):
        invalid_settings = [
            {"BOT_MODE": "fast"},
            {"BOT_MODE": ""},
            {"ROOSTOO_BASE_URL": "relative/path"},
            {"ROOSTOO_BASE_URL": "ftp://api.roostoo.com"},
            {"ROOSTOO_BASE_URL": "https://user:secret@api.roostoo.com"},
            {"ROOSTOO_BASE_URL": "https://api.roostoo.com/?key=secret"},
            {"ROOSTOO_BASE_URL": "https://api.roostoo.com:invalid"},
            {"STRATEGY_MOMENTUM_LOOKBACK_HOURS": "1.5"},
            {"STRATEGY_TREND_LOOKBACK_HOURS": "1"},
            {"STRATEGY_TOP_K": "0"},
            {"STRATEGY_MINIMUM_QUOTE_TURNOVER_24H": "NaN"},
            {"STRATEGY_MINIMUM_QUOTE_TURNOVER_24H": "-1"},
            {"RISK_MAX_ASSET_WEIGHT": "1.1"},
            {"RISK_MAX_DAILY_LOSS_FRACTION": "0"},
            {"RISK_FEE_RATE": "Infinity"},
            {"RISK_MAX_QUOTE_AGE_SECONDS": "0"},
            {
                "RISK_MAX_ASSET_WEIGHT": "0.80",
                "RISK_MAX_TOTAL_EXPOSURE": "0.70",
            },
            {
                "RISK_MAX_TOTAL_EXPOSURE": "0.90",
                "RISK_MINIMUM_CASH_RESERVE": "0.20",
            },
        ]
        for overrides in invalid_settings:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    load_settings(overrides)

    def test_live_mode_requires_endpoint_and_credentials(self):
        with self.assertRaisesRegex(ValueError, "explicit ROOSTOO_BASE_URL"):
            load_settings({"BOT_MODE": "live"})

        with self.assertRaisesRegex(ValueError, "API_KEY and ROOSTOO_API_SECRET"):
            load_settings(
                {"BOT_MODE": "live", "ROOSTOO_BASE_URL": "https://api.roostoo.com"}
            )

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_settings(
                {
                    "BOT_MODE": "live",
                    "ROOSTOO_BASE_URL": "http://api.roostoo.com",
                    "ROOSTOO_API_KEY": "test-key",
                    "ROOSTOO_API_SECRET": "test-secret",
                }
            )

    def test_credentials_are_excluded_from_settings_repr(self):
        settings = load_settings(
            {
                "ROOSTOO_API_KEY": "test-key",
                "ROOSTOO_API_SECRET": "test-secret",
            }
        )

        self.assertNotIn("test-key", repr(settings))
        self.assertNotIn("test-secret", repr(settings))

    def test_cli_reports_live_mode_before_refusing_live_trading(self):
        settings = load_settings(
            {
                "BOT_MODE": "live",
                "ROOSTOO_BASE_URL": "https://api.roostoo.com",
                "ROOSTOO_API_KEY": "test-key",
                "ROOSTOO_API_SECRET": "test-secret",
            }
        )
        output = io.StringIO()

        with patch.object(cli, "load_settings", return_value=settings):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    cli.main()

        self.assertEqual(output.getvalue(), "Run mode: live.\n")
