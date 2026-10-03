import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from fourfunds.models import PortfolioRiskState

DEFAULT_RUNTIME_DATABASE = Path("data/runtime.sqlite3")


@dataclass(frozen=True)
class CycleRecord:
    cycle_id: str
    scheduled_at_ms: int
    started_at_ms: int
    completed_at_ms: int | None
    status: str
    decision: dict[str, object]


class SQLiteCycleJournal:
    def __init__(self, database_path: str | Path = DEFAULT_RUNTIME_DATABASE) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def begin(
        self,
        cycle_id: str,
        scheduled_at_ms: int,
        started_at_ms: int,
        decision: dict[str, object],
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO bot_cycles (
                    cycle_id, scheduled_at_ms, started_at_ms, status, decision_json
                ) VALUES (?, ?, ?, 'RUNNING', ?)
                """,
                (
                    cycle_id,
                    scheduled_at_ms,
                    started_at_ms,
                    _json_text(decision),
                ),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        cycle_id: str,
        completed_at_ms: int,
        status: str,
        decision: dict[str, object],
    ) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE bot_cycles
                SET completed_at_ms = ?, status = ?, decision_json = ?
                WHERE cycle_id = ?
                """,
                (completed_at_ms, status, _json_text(decision), cycle_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Cycle {cycle_id} does not exist.")

    def checkpoint(self, cycle_id: str, decision: dict[str, object]) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE bot_cycles SET decision_json = ?
                WHERE cycle_id = ? AND status = 'RUNNING'
                """,
                (_json_text(decision), cycle_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Running cycle {cycle_id} does not exist.")

    def history(self, *, limit: int = 100) -> tuple[CycleRecord, ...]:
        if limit <= 0:
            raise ValueError("History limit must be positive.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM bot_cycles ORDER BY scheduled_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            CycleRecord(
                cycle_id=row["cycle_id"],
                scheduled_at_ms=int(row["scheduled_at_ms"]),
                started_at_ms=int(row["started_at_ms"]),
                completed_at_ms=(
                    int(row["completed_at_ms"])
                    if row["completed_at_ms"] is not None
                    else None
                ),
                status=row["status"],
                decision=json.loads(row["decision_json"]),
            )
            for row in reversed(rows)
        )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    scheduled_at_ms INTEGER NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER,
                    status TEXT NOT NULL,
                    decision_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


class SQLiteRiskStateStore:
    def __init__(self, database_path: str | Path = DEFAULT_RUNTIME_DATABASE) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def load(self) -> PortfolioRiskState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_risk_state WHERE state_id = 1"
            ).fetchone()
        if row is None:
            return None
        return PortfolioRiskState(
            currency=row["currency"],
            utc_day_start_ms=int(row["utc_day_start_ms"]),
            day_start_value=Decimal(row["day_start_value"]),
            high_water_mark=Decimal(row["high_water_mark"]),
            drawdown_breaker_active=bool(row["drawdown_breaker_active"]),
            daily_loss_breaker_active=bool(row["daily_loss_breaker_active"]),
            cash_cooldown_started_ms=(
                int(row["cash_cooldown_started_ms"])
                if row["cash_cooldown_started_ms"] is not None
                else None
            ),
        )

    def save(self, state: PortfolioRiskState) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO portfolio_risk_state (
                    state_id, currency, utc_day_start_ms, day_start_value,
                    high_water_mark, drawdown_breaker_active,
                    daily_loss_breaker_active, cash_cooldown_started_ms
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    currency = excluded.currency,
                    utc_day_start_ms = excluded.utc_day_start_ms,
                    day_start_value = excluded.day_start_value,
                    high_water_mark = excluded.high_water_mark,
                    drawdown_breaker_active = excluded.drawdown_breaker_active,
                    daily_loss_breaker_active = excluded.daily_loss_breaker_active,
                    cash_cooldown_started_ms = excluded.cash_cooldown_started_ms
                """,
                (
                    state.currency,
                    state.utc_day_start_ms,
                    str(state.day_start_value),
                    str(state.high_water_mark),
                    int(state.drawdown_breaker_active),
                    int(state.daily_loss_breaker_active),
                    state.cash_cooldown_started_ms,
                ),
            )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_risk_state (
                    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                    currency TEXT NOT NULL,
                    utc_day_start_ms INTEGER NOT NULL,
                    day_start_value TEXT NOT NULL,
                    high_water_mark TEXT NOT NULL,
                    drawdown_breaker_active INTEGER NOT NULL DEFAULT 0,
                    daily_loss_breaker_active INTEGER NOT NULL DEFAULT 0,
                    cash_cooldown_started_ms INTEGER
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(portfolio_risk_state)"
                )
            }
            migrations = {
                "drawdown_breaker_active": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "daily_loss_breaker_active": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "cash_cooldown_started_ms": "INTEGER",
            }
            for name, definition in migrations.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE portfolio_risk_state ADD COLUMN {name} {definition}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _json_text(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
