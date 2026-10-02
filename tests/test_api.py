import hashlib
import hmac
import unittest
from decimal import Decimal

import requests

from fourfunds.api import RoostooAPIError, RoostooClient
from fourfunds.models import OrderIntent, OrderSide
from fourfunds.settings import load_settings


class StubResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class StubSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._next_response()

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._next_response()

    def _next_response(self):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_settings(*, credentials=True):
    values = {}
    if credentials:
        values.update(
            {
                "ROOSTOO_API_KEY": "test-api-key",
                "ROOSTOO_API_SECRET": "test-api-secret",
            }
        )
    return load_settings(values)


class RoostooClientTests(unittest.TestCase):
    def test_parses_exchange_rules_and_tickers(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [
                StubResponse(
                    {
                        "IsRunning": True,
                        "TradePairs": {
                            "BTC/USD": {
                                "CanTrade": True,
                                "PricePrecision": 2,
                                "AmountPrecision": 6,
                                "MiniOrder": 1.0,
                            }
                        },
                    }
                ),
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {
                        "Success": True,
                        "ServerTime": timestamp_ms,
                        "Data": {
                            "BTC/USD": {
                                "MaxBid": 62000.25,
                                "MinAsk": 62001.5,
                                "LastPrice": 62000.5,
                                "Change": 0.025,
                                "CoinTradeValue": 9000000.75,
                            }
                        },
                    }
                ),
            ]
        )
        client = RoostooClient(make_settings(), session=session)

        rules = client.get_exchange_info()
        tickers = client.get_tickers()

        self.assertTrue(rules["BTC/USD"].can_trade)
        self.assertEqual(rules["BTC/USD"].minimum_order_value, Decimal("1.0"))
        self.assertEqual(tickers["BTC/USD"].bid, Decimal("62000.25"))
        self.assertEqual(tickers["BTC/USD"].server_time_ms, timestamp_ms)
        self.assertEqual(
            session.get_calls[2][1]["params"], {"timestamp": str(timestamp_ms)}
        )

    def test_parses_wallet_and_documented_empty_pending_response(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {
                        "Success": True,
                        "Wallet": {
                            "BTC": {"Free": 0.5, "Lock": 0.1},
                            "USD": {"Free": 1000, "Lock": 0},
                        },
                    }
                ),
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {
                        "Success": False,
                        "ErrMsg": "no pending order under this account",
                        "TotalPending": 0,
                        "OrderPairs": {},
                    }
                ),
            ]
        )
        client = RoostooClient(make_settings(), session=session)

        wallet = client.get_balance()
        pending = client.get_pending_count()

        self.assertEqual(wallet.server_time_ms, timestamp_ms)
        self.assertEqual(wallet.assets[0].free, Decimal("0.5"))
        self.assertEqual(pending.total_pending, 0)
        self.assertEqual(pending.order_pairs, ())
        balance_request = session.get_calls[1][1]
        self.assertEqual(balance_request["params"], f"timestamp={timestamp_ms}")
        self.assertEqual(balance_request["headers"]["RST-API-KEY"], "test-api-key")
        self.assertEqual(
            balance_request["headers"]["MSG-SIGNATURE"],
            hmac.new(
                b"test-api-secret",
                f"timestamp={timestamp_ms}".encode(),
                hashlib.sha256,
            ).hexdigest(),
        )

    def test_signs_sorted_form_body_and_parses_market_order(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {
                        "Success": True,
                        "OrderDetail": {
                            "Pair": "BNB/USD",
                            "OrderID": 81,
                            "Status": "FILLED",
                            "FilledQuantity": 2000,
                            "FilledAverPrice": 12.5,
                            "CommissionChargeValue": 0.3,
                        },
                    }
                ),
            ]
        )
        client = RoostooClient(make_settings(), session=session)

        result = client.place_order(
            OrderIntent(
                pair="BNB/USD",
                side=OrderSide.BUY,
                quantity=Decimal("2000"),
            )
        )

        body = (
            "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
        )
        signature = hmac.new(
            b"test-api-secret",
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        url, request = session.post_calls[0]

        self.assertEqual(url, "https://mock-api.roostoo.com/v3/place_order")
        self.assertEqual(request["data"], body)
        self.assertEqual(request["headers"]["MSG-SIGNATURE"], signature)
        self.assertEqual(
            request["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.order_id, "81")
        self.assertEqual(result.average_fill_price, Decimal("12.5"))

    def test_query_and_cancel_orders_return_structured_results(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {
                        "Success": True,
                        "OrderMatched": [
                            {
                                "Pair": "BTC/USD",
                                "OrderID": 81,
                                "Status": "PENDING",
                                "FilledQuantity": 0,
                                "FilledAverPrice": 0,
                                "CommissionChargeValue": 0,
                            }
                        ],
                    }
                ),
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse({"Success": True, "CanceledList": [81]}),
            ]
        )
        client = RoostooClient(make_settings(), session=session)

        queried = client.query_order("81")
        canceled = client.cancel_order("81")

        self.assertEqual(queried.status, "PENDING")
        self.assertIsNone(queried.average_fill_price)
        self.assertEqual(canceled.order_ids, ("81",))

    def test_success_false_is_an_api_error(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [
                StubResponse({"ServerTime": timestamp_ms}),
                StubResponse(
                    {"Success": False, "ErrMsg": "insufficient balance"}
                ),
            ]
        )
        client = RoostooClient(make_settings(), session=session)

        with self.assertRaisesRegex(RoostooAPIError, "insufficient balance") as caught:
            client.place_order(
                OrderIntent(
                    pair="BNB/USD",
                    side=OrderSide.BUY,
                    quantity=Decimal("1"),
                )
            )

        self.assertFalse(caught.exception.request_may_have_succeeded)
        self.assertNotIn("test-api-secret", str(caught.exception))
        self.assertEqual(len(session.post_calls), 1)

    def test_post_timeout_is_reported_as_ambiguous_without_retry(self):
        timestamp_ms = 1_580_774_512_000
        session = StubSession(
            [StubResponse({"ServerTime": timestamp_ms}), requests.Timeout()]
        )
        client = RoostooClient(make_settings(), session=session)

        with self.assertRaises(RoostooAPIError) as caught:
            client.place_order(
                OrderIntent(
                    pair="BNB/USD",
                    side=OrderSide.BUY,
                    quantity=Decimal("1"),
                )
            )

        self.assertTrue(caught.exception.request_may_have_succeeded)
        self.assertEqual(len(session.post_calls), 1)

    def test_requires_credentials_before_making_signed_request(self):
        session = StubSession([])
        client = RoostooClient(make_settings(credentials=False), session=session)

        with self.assertRaisesRegex(RoostooAPIError, "credentials are required"):
            client.get_balance()

        self.assertEqual(session.get_calls, [])

    def test_rejects_second_based_server_timestamps(self):
        session = StubSession([StubResponse({"ServerTime": 1_580_774_512})])
        client = RoostooClient(make_settings(), session=session)

        with self.assertRaisesRegex(RoostooAPIError, "13-digit millisecond"):
            client.get_server_time()

    def test_reports_http_status_errors(self):
        session = StubSession([StubResponse({}, status_code=503)])
        client = RoostooClient(make_settings(), session=session)

        with self.assertRaisesRegex(RoostooAPIError, "HTTP 503") as caught:
            client.get_exchange_info()

        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
