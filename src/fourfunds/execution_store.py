import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from fourfunds.models import (
    ExecutionResult,
    OrderIntent,
    OrderSide,
    OrderType,
    WalletAsset,
    WalletSnapshot,
)

DEFAULT_EXECUTION_DATABASE = Path("data/execution.sqlite3")


@dataclass(frozen=True)
class ExecutionRecord:
    record_id: int
    created_at_ms: int
    intent: OrderIntent
    result: ExecutionResult


class SQLiteExecutionJournal:
    def __init__(
        self, database_path: str | Path = DEFAULT_EXECUTION_DATABASE
    ) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def begin(self, intent: OrderIntent, created_at_ms: int) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO execution_records (
                    created_at_ms, pair, side, quantity, order_type,
                    limit_price, reference_price, reason, status,
                    filled_quantity, commission, commission_estimated, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REJECTED', '0', '0', 0, ?)
                """,
                (
                    created_at_ms,
                    intent.pair,
                    intent.side.value,
                    str(intent.quantity),
                    intent.order_type.value,
                    _decimal_text(intent.limit_price),
                    _decimal_text(intent.reference_price),
                    intent.reason,
                    "Intent recorded; submission has not started.",
                ),
            )
            return int(cursor.lastrowid)

    def mark_submitting(self, record_id: int) -> bool:
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE execution_records
                    SET status = 'SUBMITTING', message = ?
                    WHERE record_id = ?
                    """,
                    ("Submission started; outcome not confirmed.", record_id),
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def finish(self, record_id: int, result: ExecutionResult) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE execution_records
                SET status = ?, order_id = ?, filled_quantity = ?,
                    average_fill_price = ?, commission = ?,
                    commission_estimated = ?, wallet_snapshot = ?,
                    pending_order_count = ?, message = ?
                WHERE record_id = ?
                """,
                _result_values(result) + (record_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Execution record {record_id} does not exist.")

    def unresolved_orders(
        self, pair: str, *, exclude_record_id: int | None = None
    ) -> tuple[ExecutionRecord, ...]:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE execution_records
                SET status = 'UNKNOWN',
                    message = 'Process stopped before the order outcome was recorded.'
                WHERE pair = ? AND status = 'SUBMITTING'
                    AND (? IS NULL OR record_id != ?)
                """,
                (pair, exclude_record_id, exclude_record_id),
            )
            rows = connection.execute(
                """
                SELECT * FROM execution_records
                WHERE pair = ? AND status IN ('SUBMITTING', 'PENDING', 'UNKNOWN')
                    AND (? IS NULL OR record_id != ?)
                ORDER BY record_id
                """,
                (pair, exclude_record_id, exclude_record_id),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def history(self, *, limit: int = 100) -> tuple[ExecutionRecord, ...]:
        if limit <= 0:
            raise ValueError("History limit must be positive.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM execution_records ORDER BY record_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(_record_from_row(row) for row in reversed(rows))

    def resolve_unknown(self, pair: str, result: ExecutionResult) -> None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT record_id FROM execution_records
                WHERE pair = ? AND status IN ('SUBMITTING', 'PENDING', 'UNKNOWN')
                ORDER BY record_id DESC LIMIT 1
                """,
                (pair,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No unresolved execution exists for {pair}.")
            cursor = connection.execute(
                """
                UPDATE execution_records
                SET status = ?, order_id = ?, filled_quantity = ?,
                    average_fill_price = ?, commission = ?,
                    commission_estimated = ?, wallet_snapshot = ?,
                    pending_order_count = ?, message = ?
                WHERE record_id = ?
                """,
                _result_values(result) + (row["record_id"],),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Execution record for {pair} does not exist.")

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_records (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    limit_price TEXT,
                    reference_price TEXT,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    average_fill_price TEXT,
                    commission TEXT NOT NULL DEFAULT '0',
                    commission_estimated INTEGER NOT NULL DEFAULT 0,
                    wallet_snapshot TEXT,
                    pending_order_count INTEGER,
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_execution_per_pair
                ON execution_records(pair)
                WHERE status IN ('SUBMITTING', 'PENDING', 'UNKNOWN')
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _result_values(result: ExecutionResult) -> tuple[object, ...]:
    return (
        result.status,
        result.order_id,
        str(result.filled_quantity),
        _decimal_text(result.average_fill_price),
        str(result.commission),
        int(result.commission_estimated),
        _wallet_text(result.wallet_snapshot),
        result.pending_order_count,
        result.message,
    )


def _record_from_row(row: sqlite3.Row) -> ExecutionRecord:
    intent = OrderIntent(
        pair=row["pair"],
        side=OrderSide(row["side"]),
        quantity=Decimal(row["quantity"]),
        order_type=OrderType(row["order_type"]),
        limit_price=(
            Decimal(row["limit_price"])
            if row["limit_price"] is not None
            else None
        ),
        reason=row["reason"],
        reference_price=(
            Decimal(row["reference_price"])
            if row["reference_price"] is not None
            else None
        ),
    )
    result = ExecutionResult(
        pair=row["pair"],
        status=row["status"],
        order_id=row["order_id"],
        filled_quantity=Decimal(row["filled_quantity"]),
        average_fill_price=(
            Decimal(row["average_fill_price"])
            if row["average_fill_price"] is not None
            else None
        ),
        commission=Decimal(row["commission"]),
        commission_estimated=bool(row["commission_estimated"]),
        wallet_snapshot=_wallet_from_text(row["wallet_snapshot"]),
        pending_order_count=row["pending_order_count"],
        message=row["message"],
    )
    return ExecutionRecord(
        record_id=row["record_id"],
        created_at_ms=row["created_at_ms"],
        intent=intent,
        result=result,
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _wallet_text(wallet: WalletSnapshot | None) -> str | None:
    if wallet is None:
        return None
    return json.dumps(
        {
            "server_time_ms": wallet.server_time_ms,
            "assets": [
                {
                    "asset": asset.asset,
                    "free": str(asset.free),
                    "locked": str(asset.locked),
                }
                for asset in wallet.assets
            ],
        },
        separators=(",", ":"),
    )


def _wallet_from_text(value: str | None) -> WalletSnapshot | None:
    if value is None:
        return None
    data = json.loads(value)
    return WalletSnapshot(
        server_time_ms=int(data["server_time_ms"]),
        assets=tuple(
            WalletAsset(
                asset=item["asset"],
                free=Decimal(item["free"]),
                locked=Decimal(item["locked"]),
            )
            for item in data["assets"]
        ),
    )
