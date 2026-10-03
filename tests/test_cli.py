import unittest
from unittest.mock import patch

from fourfunds import cli
from fourfunds.settings import load_settings


class CLITests(unittest.TestCase):
    def test_cli_runs_the_bot_loop_at_the_configured_interval(self):
        settings = load_settings({"BOT_CYCLE_INTERVAL_SECONDS": "7200"})
        runner = object()

        with patch.object(cli, "load_settings", return_value=settings):
            with patch.object(cli, "build_runner", return_value=runner) as build_runner:
                with patch.object(cli, "run_forever") as run_forever:
                    cli.main([])

        build_runner.assert_called_once_with(settings)
        run_forever.assert_called_once_with(runner, 7200)
