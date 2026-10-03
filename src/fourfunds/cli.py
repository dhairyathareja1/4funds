import argparse
import logging
from collections.abc import Sequence

from fourfunds.execution_store import SQLiteExecutionJournal
from fourfunds.main import build_runner, run_forever
from fourfunds.settings import load_settings


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fourfunds")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--once",
        action="store_true",
        help="run one cycle for the current UTC interval and exit",
    )
    operation.add_argument(
        "--report",
        action="store_true",
        help="report orders with actual fills and their UTC trading days",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.report:
        activity = SQLiteExecutionJournal().activity()
        print(f"Filled trades: {activity.filled_order_count}")
        print(f"Active trading days: {len(activity.active_trading_days)}")
        if activity.active_trading_days:
            print("UTC dates: " + ", ".join(activity.active_trading_days))
        return

    try:
        settings = load_settings()
        runner = build_runner(settings)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    if args.once:
        runner.run_once()
        return
    run_forever(runner, settings.cycle_interval_seconds)
