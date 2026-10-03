import logging
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import TypeAlias

from fourfunds.api import RoostooClient
from fourfunds.data import SQLiteMarketDataStore
from fourfunds.execution import OrderExecutor
from fourfunds.execution_store import SQLiteExecutionJournal
from fourfunds.risk import RiskManager
from fourfunds.runtime import BotRunner, RetryingRoostooClient
from fourfunds.runtime_store import SQLiteCycleJournal, SQLiteRiskStateStore
from fourfunds.settings import (
    SECONDS_PER_HOUR,
    Settings,
    validate_runtime_settings,
)
from fourfunds.strategy import BaselineStrategy

logger = logging.getLogger(__name__)
MILLISECONDS_PER_SECOND = 1000
SignalHandler: TypeAlias = Callable[[int, FrameType | None], None] | int


def build_runner(settings: Settings) -> BotRunner:
    validate_runtime_settings(settings)
    raw_client = RoostooClient(settings)
    client = RetryingRoostooClient(raw_client, settings)
    execution_journal = SQLiteExecutionJournal()
    return BotRunner(
        settings=settings,
        client=client,
        market_data=SQLiteMarketDataStore("data/market.sqlite3"),
        strategy=BaselineStrategy(),
        planner=RiskManager(),
        executor=OrderExecutor(
            client,
            taker_fee_rate=settings.fee_rate,
            max_quote_age_seconds=settings.max_quote_age_seconds,
            journal=execution_journal,
        ),
        cycle_journal=SQLiteCycleJournal(),
        risk_state_store=SQLiteRiskStateStore(),
    )


def run_forever(runner: BotRunner, interval_seconds: int) -> None:
    if interval_seconds < SECONDS_PER_HOUR or interval_seconds % SECONDS_PER_HOUR:
        raise ValueError("Cycle interval must be a multiple of one hour.")
    stop_event = threading.Event()
    previous_handlers = _install_shutdown_handlers(stop_event)
    interval_ms = interval_seconds * MILLISECONDS_PER_SECOND
    try:
        while not stop_event.is_set():
            now_ms = time.time_ns() // 1_000_000
            cycle_slot_ms = now_ms // interval_ms * interval_ms
            runner.run_once(cycle_slot_ms)
            next_slot_ms = cycle_slot_ms + interval_ms
            now_ms = time.time_ns() // 1_000_000
            if next_slot_ms <= now_ms:
                next_slot_ms = (now_ms // interval_ms + 1) * interval_ms
            wait_seconds = max(0.0, (next_slot_ms - now_ms) / 1000)
            logger.info("heartbeat state=waiting next_cycle_at_ms=%d", next_slot_ms)
            if stop_event.wait(wait_seconds):
                break
    finally:
        _restore_shutdown_handlers(previous_handlers)
        logger.info("heartbeat state=stopped")


def _install_shutdown_handlers(
    stop_event: threading.Event,
) -> dict[signal.Signals, SignalHandler]:
    if threading.current_thread() is not threading.main_thread():
        return {}

    previous_handlers: dict[signal.Signals, SignalHandler] = {}

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        logger.info("Shutdown requested by signal %s.", signum)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    return previous_handlers


def _restore_shutdown_handlers(
    previous_handlers: dict[signal.Signals, SignalHandler],
) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)
