# Order execution interface.

from collections.abc import Sequence
from typing import Protocol

from fourfunds.api import RoostooClient
from fourfunds.models import ExecutionResult, OrderIntent
from fourfunds.settings import RunMode


class Executor(Protocol):
    def execute(
        self, intents: Sequence[OrderIntent], *, mode: RunMode
    ) -> Sequence[ExecutionResult]:
        ...


class OrderExecutor:
    def __init__(self, client: RoostooClient) -> None:
        self._client = client

    def execute(
        self, intents: Sequence[OrderIntent], *, mode: RunMode
    ) -> Sequence[ExecutionResult]:
        raise NotImplementedError
