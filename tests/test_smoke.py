"""Smoke tests for the NSE viewer app.

NSE HTTP access is fully mocked (fetch_nse_data / load_stocks) so the suite
runs offline and deterministically.
"""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as viewer
import config


def fake_quote(symbol):
    return {
        "equityResponse": [
            {
                "metaData": {
                    "symbol": symbol,
                    "companyName": "Test Co Ltd",
                    "previousClose": "100",
                    "basePrice": "100",
                    "change": "5",
                    "pChange": "5.00",
                    "open": "102",
                    "dayHigh": "110",
                    "dayLow": "99",
                },
                "orderBook": {"lastPrice": "105"},
                "priceInfo": {
                    "priceBand": "80-120",
                    "yearHigh": "150",
                    "yearLow": "90",
                    "cmDailyVolatility": "2.1",
                    "cmAnnualVolatility": "12.5",
                },
                "tradeInfo": {"totalTradedVolume": "1234567", "totalTradedValue": "42.5"},
                "secInfo": {
                    "basicIndustry": "IT",
                    "pdSectorPe": "28.5",
                    "pdSectorInd": "IT - Software",
                    "securityvar": "3.5",
                    "varMargin": "4.0",
                    "extremelossMargin": "1.5",
                    "applicableMargin": "5.5",
                    "deliveryTotradedQuantity": "72.3",
                    "marketLot": "1",
                },
                "lastUpdateTime": "24-Sep-2026 15:00:00",
            }
        ]
    }


def fake_historical():
    return [
        {
            "mtimestamp": "24-Sep-2026",
            "chOpeningPrice": 102,
            "chTradeHighPrice": 110,
            "chTradeLowPrice": 99,
            "chClosingPrice": 105,
            "chLastTradedPrice": 105,
            "chPreviousClsPrice": 100,
            "chTotTradedQty": 1000000,
            "chTotTradedVal": 100.5,
            "ch52WeekHighPrice": 150,
            "ch52WeekLowPrice": 90,
            "chTotalTrades": 5000,
            "vwap": 103.2,
        }
    ]


def fake_live():
    base = 1700000000000
    return {
        "grapthData": [
            [base, 100.0, "PO"],
            [base + 60000, 102.0, "PO"],
            [base + 120000, 103.0, "NM"],
            [base + 180000, 105.0, "NM"],
        ],
        "closePrice": 105.0,
    }


def fake_indices():
    return {
        "data": [
            {"indexSymbol": "NIFTY 50", "last": 25000, "previousClose": 24900},
            {"indexSymbol": "NIFTY BANK", "last": 53100, "previousClose": 53000},
        ]
    }


def stub_fetcher(url, **kwargs):
    if url == config.PRE_OPEN_URL:
        return {
            "data": [
                {"metadata": {"symbol": "TCS"}, "detail": {"preOpenMarket": {"totalTradedVolume": 100, "totalBuyQuantity": 60, "totalSellQuantity": 40, "Change": 2.5, "perChange": 1.2}}}
            ]
        }
    if "chart-databyindex-dynamic" in url or url.endswith("type=symbol"):
        return fake_live()
    if url == config.ALL_INDICES_URL:
        return fake_indices()
    if "GetQuoteApi" in url and "getSymbolData" in url:
        return fake_quote(url.split("symbol=")[1].split("&")[0].upper())
    if "GetQuoteApi" in url and "getHistoricalTradeData" in url:
        return fake_historical()
    return None


class NseAppTest(unittest.TestCase):
    def setUp(self):
        viewer.app.config["TESTING"] = True
        viewer.app.config["WTF_CSRF_ENABLED"] = False
        self.client = viewer.app.test_client()

        fetcher_patch = patch.object(viewer.nse_fetcher, "fetch_nse_data", side_effect=stub_fetcher)
        fetcher_patch.start()
        self.addCleanup(fetcher_patch.stop)

        stocks_patch = patch.object(
            viewer,
            "load_stocks",
            return_value=[
                {"symbol": "RELIANCE", "name": "Reliance Industries", "series": "EQ"},
                {"symbol": "TCS", "name": "Tata Consultancy Services", "series": "EQ"},
                {"symbol": "INFY", "name": "Infosys Limited", "series": "EQ"},
                {"symbol": "RELBANK", "name": "Reliance Bank", "series": "EQ"},
            ],
        )
        stocks_patch.start()
        self.addCleanup(stocks_patch.stop)

    def test_index_page_render(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("{{", body, "Unrendered Jinja markers on index page")

    def test_index_with_symbol(self):
        response = self.client.get("/?symbol=TCS")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("TCS", body)
        self.assertNotIn("{{", body, "Unrendered Jinja markers on symbol page")
        self.assertIn("/static/app.js", body)
        self.assertIn("/static/site.css", body)

    def test_invalid_symbol_is_safe(self):
        response = self.client.get("/?symbol=TCS;%20DROP")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("{{", body)
        self.assertNotIn("DROP", body)

    def test_get_stock_data_ok(self):
        response = self.client.get("/get_stock_data?symbol=TCS")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["info"]["symbol"], "TCS")
        self.assertEqual(data["priceInfo"]["lastPrice"], 105)
        self.assertEqual(data["priceInfo"]["upperCP"], 120)
        self.assertIn("preOpenMarket", data)
        self.assertIn("max-age=", response.headers.get("Cache-Control", ""))

    def test_get_stock_data_invalid(self):
        response = self.client.get("/get_stock_data?symbol=BAD!SYM")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_get_historical_ok(self):
        response = self.client.get(
            "/get_historical?symbol=TCS&from_date=2026-09-01&to_date=2026-09-24"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["date"], "2026-09-24")
        self.assertIn("max-age=", response.headers.get("Cache-Control", ""))

    def test_get_historical_bad_dates(self):
        response = self.client.get(
            "/get_historical?symbol=TCS&from_date=zzz&to_date=2026-09-24"
        )
        self.assertEqual(response.status_code, 400)

    def test_get_live_data(self):
        response = self.client.get("/get_live_data?symbol=TCS")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["pre_open"]["prices"]), 2)
        self.assertEqual(len(data["normal_market"]["prices"]), 2)
        self.assertEqual(data["close_price"], 105.0)

    def test_get_live_data_invalid(self):
        response = self.client.get("/get_live_data?symbol=abc def")
        self.assertEqual(response.status_code, 400)

    def test_get_indices_data(self):
        response = self.client.get("/get_indices_data")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["symbol"], "NIFTY 50")
        self.assertAlmostEqual(data[0]["change"], 100)

    def test_search_exact_match_first(self):
        response = self.client.get("/search_stocks?query=reliance")
        data = response.get_json()
        self.assertEqual(data[0]["symbol"], "RELIANCE")

    def test_search_shortest_symbol_wins_ties(self):
        response = self.client.get("/search_stocks?query=rel")
        data = response.get_json()
        self.assertEqual(data[0]["symbol"], "RELBANK")
        self.assertIn("RELIANCE", [s["symbol"] for s in data])

    def test_search_empty_query(self):
        self.assertEqual(self.client.get("/search_stocks").get_json(), [])


if __name__ == "__main__":
    unittest.main()