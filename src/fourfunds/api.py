# Roostoo API client interface.

from fourfunds.models import ExecutionResult, ExchangeRule, MarketQuote, OrderIntent, WalletSnapshot
from fourfunds.settings import Settings


class RoostooClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_server_time(self) -> int:
        raise NotImplementedError

    def get_exchange_rules(self) -> dict[str, ExchangeRule]:
        raise NotImplementedError

    def get_tickers(self) -> dict[str, MarketQuote]:
        raise NotImplementedError

    def get_wallet(self) -> WalletSnapshot:
        raise NotImplementedError

    def get_pending_orders(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def place_order(self, intent: OrderIntent) -> ExecutionResult:
        raise NotImplementedError

    def query_order(self, order_id: str) -> ExecutionResult:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError
