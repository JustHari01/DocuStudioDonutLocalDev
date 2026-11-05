from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs

from docurandom.smm_api import (
    ONE_MM_API_URL,
    SmmApiClient,
    SmmApiError,
    SmmAuthenticationError,
    clear_smm_api_key,
    load_smm_api_key,
    save_smm_api_key,
)


class _Response(BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _ApiFixture:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.forms: list[dict[str, list[str]]] = []
        self.urls: list[str] = []

    def __call__(self, request: object, *, timeout: float) -> _Response:
        del timeout
        url = str(getattr(request, "full_url"))
        form = parse_qs(getattr(request, "data").decode("utf-8"))
        self.urls.append(url)
        self.forms.append(form)
        action = form["action"][0]
        return _Response(json.dumps(self.responses[action]).encode("utf-8"))


class SmmApiTests(unittest.TestCase):
    def test_windows_secret_store_round_trip_never_writes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandom1MMSecret_") as temp:
            path = Path(temp) / "one-mm.dat"
            key = "test-key-that-must-stay-encrypted"

            save_smm_api_key(key, path)

            self.assertEqual(load_smm_api_key(path), key)
            self.assertNotIn(key.encode("utf-8"), path.read_bytes())
            clear_smm_api_key(path)
            self.assertFalse(path.exists())

    def test_services_and_balance_are_typed_and_use_v2_endpoint(self) -> None:
        fixture = _ApiFixture(
            {
                "services": [
                    {
                        "service": 17,
                        "name": "YouTube views",
                        "type": "Default",
                        "category": "YouTube",
                        "rate": "2.5",
                        "min": "200",
                        "max": "10000",
                        "refill": True,
                    }
                ],
                "balance": {"balance": "68.6868", "currency": "USD"},
            }
        )
        client = SmmApiClient("local-test-key", opener=fixture)

        services = client.services()
        balance = client.balance()

        self.assertEqual(services[0].service_id, "17")
        self.assertEqual(services[0].minimum, 200)
        self.assertTrue(services[0].refill)
        self.assertEqual(str(balance.amount), "68.6868")
        self.assertEqual(fixture.urls, [ONE_MM_API_URL, ONE_MM_API_URL])
        self.assertEqual(fixture.forms[0]["key"], ["local-test-key"])

    def test_order_add_status_multiple_and_refill(self) -> None:
        fixture = _ApiFixture(
            {
                "add": {"order": 99999},
                "status": {
                    "123": {"charge": "0.27819", "status": "Partial", "remains": "157"},
                    "456": {"error": "Incorrect order ID"},
                },
                "refill": {"refill": "1"},
            }
        )
        client = SmmApiClient("local-test-key", opener=fixture)

        order_id = client.add_order(17, "https://youtu.be/example", quantity=500)
        statuses = client.orders_status((123, 456))
        refill_id = client.create_refill(order_id)

        self.assertEqual(order_id, "99999")
        self.assertEqual(fixture.forms[0]["quantity"], ["500"])
        self.assertEqual(statuses["123"].status, "Partial")
        self.assertEqual(statuses["123"].remains, 157)
        self.assertEqual(statuses["456"].status, "Error")
        self.assertEqual(refill_id, "1")

    def test_products_and_product_order_flow(self) -> None:
        fixture = _ApiFixture(
            {
                "products": [
                    {
                        "product": 1,
                        "name": "Digital product",
                        "rate": 2.5,
                        "min": 1,
                        "max": 10,
                        "category": "Example",
                        "status": "In stock",
                        "inventory": 40,
                    }
                ],
                "add_product_order": {"order": 88},
                "product_order_status": {
                    "charge": 7.5,
                    "status": "Completed",
                    "result": "credential-id",
                },
                "result_product": {"result": ["first", "second"]},
            }
        )
        client = SmmApiClient("local-test-key", opener=fixture)

        product = client.products()[0]
        order_id = client.add_product_order(product.product_id, 2)
        status = client.product_order_status(order_id)
        result = client.product_result(order_id)

        self.assertEqual(product.inventory, 40)
        self.assertEqual(status.status, "Completed")
        self.assertEqual(result, ("first", "second"))

    def test_top_level_error_is_sanitized_and_raised(self) -> None:
        fixture = _ApiFixture({"balance": {"error": "Invalid / Missing Key"}})
        client = SmmApiClient("local-test-key", opener=fixture)

        with self.assertRaises(SmmAuthenticationError) as raised:
            client.balance()

        self.assertNotIn("local-test-key", str(raised.exception))

    def test_missing_key_stops_before_network(self) -> None:
        def should_not_open(*_args: object, **_kwargs: object) -> object:
            self.fail("network must not be called without a key")

        client = SmmApiClient(opener=should_not_open, key_loader=lambda: None)

        with self.assertRaises(SmmAuthenticationError):
            client.balance()

    def test_validates_quantity_before_spending(self) -> None:
        client = SmmApiClient("local-test-key", opener=lambda *_a, **_k: None)

        with self.assertRaises(ValueError):
            client.add_order(1, "https://example.com", quantity=0)
        with self.assertRaises(ValueError):
            client.orders_status(range(101))
        with self.assertRaises(SmmApiError):
            SmmApiClient(" ")


if __name__ == "__main__":
    unittest.main()
