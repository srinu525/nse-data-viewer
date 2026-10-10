import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as viewer


class ApiResilienceTest(unittest.TestCase):
    def test_fetch_nse_data_ignores_non_json_success_responses(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                raise ValueError("not json")

        old_get = viewer.nse_fetcher.session.get
        viewer.nse_fetcher.session.get = lambda *args, **kwargs: FakeResponse()
        try:
            self.assertIsNone(viewer.nse_fetcher.fetch_nse_data("https://example.com/quote"))
        finally:
            viewer.nse_fetcher.session.get = old_get

    def test_next_api_quote_accepts_dict_payloads(self):
        payload = {
            "equityResponse": {
                "metaData": {"symbol": "RELIANCE"},
                "orderBook": {"lastPrice": 123},
            }
        }
        old_fetch = viewer.nse_fetcher.fetch_nse_data
        viewer.nse_fetcher.fetch_nse_data = lambda *args, **kwargs: payload
        try:
            self.assertEqual(
                viewer.next_api_quote("reliance")["metaData"]["symbol"],
                "RELIANCE",
            )
        finally:
            viewer.nse_fetcher.fetch_nse_data = old_fetch


if __name__ == '__main__':
    unittest.main()
