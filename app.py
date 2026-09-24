import csv
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from curl_cffi import requests as curl_requests
from flask import Flask, render_template, jsonify, request

import config

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("nse-viewer")

# Cache-Control: allow browsers to cache data endpoints briefly (reduces hits)
CACHE_TTL_HEADER = {
    "/get_stock_data": config.TTL_QUOTE,
    "/get_historical": config.TTL_HISTORICAL,
    "/get_live_data": config.TTL_LIVE_CHART,
    "/get_indices_data": config.TTL_INDICES,
    "/search_stocks": 3600,
}


@app.after_request
def add_cache_headers(response):
    for prefix, ttl in CACHE_TTL_HEADER.items():
        if request.path == prefix:
            if 200 <= response.status_code < 400:
                response.headers["Cache-Control"] = f"public, max-age={ttl}"
                response.headers["Vary"] = "Accept-Encoding"
            break
    else:
        if request.path != "/":
            response.headers["Cache-Control"] = "no-store"
    return response


SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,20}$")


def is_valid_symbol(symbol):
    return bool(symbol) and bool(SYMBOL_RE.match(symbol.upper()))


class NSEBrowserSimulator:
    def __init__(self):
        self.session = None
        self.http_lock = Lock()          # serialises actual HTTP calls on the shared session
        self.cache = {}
        self.cache_lock = Lock()
        self.last_successful_request = 0
        self._initialize_session()

    def _new_session(self):
        session = curl_requests.Session(impersonate="chrome")
        session.headers.update({
            "User-Agent": random.choice(self.user_agents),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://www.nseindia.com/",
            "Origin": "https://www.nseindia.com",
        })
        session.cookies.clear()
        return session

    def _initialize_session(self):
        """Initialize session with browser impersonation and warm up cookies"""
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ]

        if self.session is None:
            self.session = self._new_session()

        if self._warm_pass():
            return True
        # Retry with a fresh session
        self.session = self._new_session()
        return self._warm_pass()

    def _warm_pass(self):
        init_sequence = [
            (config.INIT_HTML_URL, "Homepage"),
            (config.INIT_INDICES_URL, "Market Data"),
        ]
        for url, desc in init_sequence:
            try:
                self.session.get(url, timeout=20)
                time.sleep(random.uniform(*config.INIT_SLEEP_RANGE))
            except Exception as e:
                logger.warning("Initialization failed at %s: %s", desc, e)
                return False
        return True

    def fetch_nse_data(self, url, max_retries=3, cache_ttl=30):
        """Fetch data from NSE with retries, caching and rate limiting.

        Cache lookup and the rate-limit slot reservation are guarded by the
        same lock so concurrent requests cannot race past the throttler. The
        actual HTTP call is additionally serialized so the shared session is
        never used by two threads at once.
        """
        with self.cache_lock:
            if url in self.cache and (time.time() - self.cache[url]["timestamp"]) < cache_ttl:
                return self.cache[url]["data"]

            time_since_last = time.time() - self.last_successful_request
            if time_since_last < config.REQUEST_DELAY:
                time.sleep(config.REQUEST_DELAY - time_since_last)
            self.last_successful_request = time.time()

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(random.uniform(*config.RETRY_SLEEP_RANGE))

                with self.http_lock:
                    self.session.headers["User-Agent"] = random.choice(self.user_agents)
                    response = self.session.get(url, timeout=20)
                    data = response.json() if response.status_code == 200 else None

                if response.status_code == 200:
                    with self.cache_lock:
                        self.cache[url] = {"data": data, "timestamp": time.time()}
                    return data

                elif response.status_code == 403:
                    logger.warning("403 Forbidden - Reinitializing session")
                    if not self._initialize_session():
                        time.sleep(config.SESSION_REINIT_FAIL_SLEEP)
                        continue

            except (curl_requests.RequestsError, ConnectionResetError) as e:
                logger.warning("Attempt %d failed: %s", attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep((attempt + 1) * 2)  # Exponential backoff
                continue

        return None


nse_fetcher = NSEBrowserSimulator()


def get_pre_open_market(symbol):
    """Fetch pre-open market data for a symbol from a cached full-market snapshot.

    NSE's pre-open endpoint only returns meaningful data with key=ALL, so the
    full payload is cached for a short window and reused for all lookups.
    """
    data = nse_fetcher.fetch_nse_data(config.PRE_OPEN_URL, cache_ttl=config.TTL_PRE_OPEN)
    if not data or not isinstance(data, dict):
        return None
    target = symbol.upper()
    for record in data.get("data", []):
        meta = record.get("metadata", {})
        if str(meta.get("symbol", "")).upper() == target:
            detail = record.get("detail", {})
            return detail.get("preOpenMarket") or None
    return None


def next_api_quote(symbol):
    """Fetch equity quote via NSE NextApi (bypasses the 403 on /api/quote-equity)."""
    from urllib.parse import urlencode
    url = config.NEXT_API_URL + "?" + urlencode(dict(
        functionName="getSymbolData", marketType="N", series="EQ", symbol=symbol.upper()
    ))
    data = nse_fetcher.fetch_nse_data(url, cache_ttl=config.TTL_QUOTE)
    if not data or not isinstance(data, dict):
        return None
    resp = data.get("equityResponse") or []
    return resp[0] if resp else None


def next_api_historical(symbol, from_date, to_date):
    """Fetch historical trade data via NSE NextApi (bypasses 503 on /api/historical)."""
    from urllib.parse import urlencode
    url = config.NEXT_API_URL + "?" + urlencode(dict(
        functionName="getHistoricalTradeData", symbol=symbol.upper(),
        series="EQ", fromDate=from_date, toDate=to_date
    ))
    data = nse_fetcher.fetch_nse_data(url, cache_ttl=config.TTL_HISTORICAL)
    return data if isinstance(data, list) else None


def map_next_quote_to_legacy(q):
    """Map NSE NextApi quote response to the legacy /api/quote-equity schema."""
    meta = q.get("metaData", {})
    ob = q.get("orderBook", {})
    ti = q.get("tradeInfo", {})
    pi = q.get("priceInfo", {})
    si = q.get("secInfo", {})

    last_price = float(ob.get("lastPrice") or 0)
    prev_close = float(meta.get("previousClose") or meta.get("basePrice") or 0)
    change = float(meta.get("change") or (last_price - prev_close))
    p_change = float(meta.get("pChange") or 0)

    band_low = band_high = None
    if pi.get("priceBand"):
        try:
            lo, hi = pi["priceBand"].split("-")
            band_low, band_high = float(lo), float(hi)
        except (ValueError, TypeError):
            pass
    if band_high is None:
        band_high = round(prev_close * 1.2, 2)
        band_low = round(prev_close * 0.8, 2)

    return {
        "info": {
            "symbol": meta.get("symbol"),
            "companyName": meta.get("companyName"),
            "industry": si.get("basicIndustry") or si.get("industryInfo"),
        },
        "priceInfo": {
            "lastPrice": last_price,
            "change": change,
            "pChange": p_change,
            "open": float(meta.get("open") or 0),
            "previousClose": prev_close,
            "intraDayHighLow": {
                "max": float(meta.get("dayHigh") or 0),
                "min": float(meta.get("dayLow") or 0),
            },
            "weekHighLow": {
                "max": float(pi.get("yearHigh") or 0),
                "min": float(pi.get("yearLow") or 0),
            },
            "upperCP": band_high,
            "lowerCP": band_low,
        },
        "metadata": {
            "pdSectorPe": si.get("pdSectorPe"),
            "pdSectorInd": (si.get("pdSectorInd") or "").strip() or None,
        },
        "marketDeptOrderBook": {
            "tradeInfo": {
                "totalTradedVolume": ti.get("totalTradedVolume"),
                "totalTradedValue": ti.get("totalTradedValue"),
                "cmDailyVolatility": pi.get("cmDailyVolatility"),
                "cmAnnualVolatility": pi.get("cmAnnualVolatility"),
            },
            "valueAtRisk": {
                "securityVar": si.get("securityvar"),
                "varMargin": si.get("varMargin"),
                "extremeLossMargin": si.get("extremelossMargin"),
                "applicableMargin": si.get("applicableMargin"),
            },
        },
        "securityWiseDP": {
            "deliveryToTradedQuantity": si.get("deliveryTotradedQuantity")
        },
        "lastUpdateTime": q.get("lastUpdateTime"),
    }


def convert_timestamp(timestamp_ms):
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S"), dt.strftime("%Y-%m-%d")


def filter_leading_zeros(data_points):
    filtered = []
    found_non_zero = False
    for point in data_points:
        if not found_non_zero and point["price"] > 0:
            found_non_zero = True
        if found_non_zero:
            filtered.append(point)
    return filtered


def _default_dates():
    to_date = datetime.now()
    from_date = to_date - timedelta(days=15)
    return (
        from_date.strftime("%Y-%m-%d"),
        to_date.strftime("%Y-%m-%d"),
    )


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        symbol = request.form.get("symbol", "").strip().upper()
        active_tab = request.form.get("active_tab", "charts")
    else:
        symbol = (request.args.get("symbol") or "").strip().upper()
        active_tab = request.args.get("active_tab", "charts")

    default_from, default_to = _default_dates()

    if not is_valid_symbol(symbol):
        return render_template(
            "index.html",
            symbol="",
            active_tab="charts",
            default_from=default_from,
            default_to=default_to,
        )

    # Page shell renders instantly; quote / charts / historical load via
    # /get_stock_data, /get_live_data and /get_historical (client-side).
    return render_template(
        "index.html",
        symbol=symbol,
        from_date=default_from,
        to_date=default_to,
        stocks=[],
        dynamic_chart=None,
        active_tab=active_tab,
        default_from=default_from,
        default_to=default_to,
    )


@app.route("/get_historical")
def get_historical():
    symbol = request.args.get("symbol", "").strip().upper()
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol."}), 400

    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    historical_data = next_api_historical(
        symbol, fd.strftime("%d-%m-%Y"), td.strftime("%d-%m-%Y")
    )

    stocks = []
    if historical_data:
        for entry in historical_data:
            try:
                date_obj = datetime.strptime(entry["mtimestamp"], "%d-%b-%Y")
                stocks.append(
                    {
                        "date": date_obj.strftime("%Y-%m-%d"),
                        "open": entry.get("chOpeningPrice"),
                        "high": entry.get("chTradeHighPrice"),
                        "low": entry.get("chTradeLowPrice"),
                        "close": entry.get("chClosingPrice"),
                        "last_traded": entry.get("chLastTradedPrice"),
                        "previous_close": entry.get("chPreviousClsPrice"),
                        "volume": entry.get("chTotTradedQty"),
                        "value": entry.get("chTotTradedVal"),
                        "52w_high": entry.get("ch52WeekHighPrice"),
                        "52w_low": entry.get("ch52WeekLowPrice"),
                        "total_trades": entry.get("chTotalTrades"),
                        "vwap": entry.get("vwap"),
                    }
                )
            except Exception as e:
                logger.warning("Error processing historical data: %s", e)
                continue

    return jsonify(stocks)


@app.route("/get_live_data")
def get_live_data():
    symbol = request.args.get("symbol", "").strip().upper()

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol."}), 400

    dynamic_data = nse_fetcher.fetch_nse_data(
        f"https://www.nseindia.com/api/chart-databyindex-dynamic?index={symbol}EQN&type=symbol",
        cache_ttl=config.TTL_LIVE_CHART,
    )

    if not dynamic_data:
        return jsonify({"error": "Failed to fetch live data"}), 500

    pre_open_data = []
    normal_market_data = []

    if "grapthData" in dynamic_data and isinstance(dynamic_data["grapthData"], list):
        for item in dynamic_data["grapthData"]:
            if isinstance(item, list) and len(item) >= 3:
                timestamp, price, market_phase = item[0], item[1], item[2]
                time_str, date_str = convert_timestamp(timestamp)
                if time_str and date_str:
                    point = {
                        "timestamp": timestamp,
                        "price": float(price),
                        "time": time_str,
                        "date": date_str,
                        "market_phase": market_phase,
                    }

                    if market_phase == "PO":
                        pre_open_data.append(point)
                    elif market_phase == "NM":
                        normal_market_data.append(point)

    filtered_pre_open = filter_leading_zeros(pre_open_data)

    return jsonify(
        {
            "pre_open": {
                "times": [point["time"] for point in filtered_pre_open],
                "prices": [point["price"] for point in filtered_pre_open],
                "all_data": filtered_pre_open,
            },
            "normal_market": {
                "times": [point["time"] for point in normal_market_data],
                "prices": [point["price"] for point in normal_market_data],
                "all_data": normal_market_data,
            },
            "close_price": (
                float(dynamic_data.get("closePrice", 0))
                if dynamic_data.get("closePrice")
                else None
            ),
        }
    )


@app.route("/get_indices_data")
def get_indices_data():
    try:
        data = nse_fetcher.fetch_nse_data(config.ALL_INDICES_URL, cache_ttl=config.TTL_INDICES)
        if not data:
            return jsonify({"error": "Failed to fetch indices data"}), 500

        indices = []
        for item in data.get("data", []):
            if item.get("indexSymbol"):
                previous_close = float(item.get("previousClose", 0))
                current_value = float(item.get("last", 0))
                change = current_value - previous_close

                if previous_close != 0:
                    change_percent = (change / previous_close) * 100
                else:
                    change_percent = 0

                indices.append(
                    {
                        "symbol": item.get("indexSymbol"),
                        "last": current_value,
                        "change": change,
                        "change_percent": change_percent,
                    }
                )
        return jsonify(indices)
    except Exception as e:
        logger.exception("Error fetching indices")
        return jsonify({"error": str(e)}), 500


@app.route('/get_stock_data')
def get_stock_data():
    symbol = request.args.get('symbol', 'RELIANCE').strip().upper()

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol."}), 400

    try:
        quote_response = next_api_quote(symbol)

        if not quote_response:
            return jsonify({"error": "Failed to fetch quote data"}), 500

        legacy = map_next_quote_to_legacy(quote_response)
        try:
            pre_open = get_pre_open_market(symbol)
            if pre_open:
                legacy["preOpenMarket"] = pre_open
        except Exception as e:
            logger.warning("Error fetching pre-open data: %s", e)

        return jsonify(legacy)

    except Exception as e:
        logger.exception("Error fetching stock data")
        return jsonify({"error": str(e)}), 500


_stocks_cache = None
_stocks_cache_time = 0


def load_stocks(force_reload=False):
    global _stocks_cache, _stocks_cache_time
    now = time.time()
    if _stocks_cache is not None and not force_reload and (now - _stocks_cache_time) < config.STOCKS_CACHE_TTL:
        return _stocks_cache

    stocks = []
    try:
        with open('stocks.csv', mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            for row in reader:
                row.setdefault('sector', '')
                stocks.append(row)
    except FileNotFoundError:
        logger.error("Error: stocks.csv file not found")
    except Exception as e:
        logger.error("Error loading stocks.csv: %s", e)

    _stocks_cache = stocks
    _stocks_cache_time = now
    return stocks


@app.route('/search_stocks')
def search_stocks():
    query = request.args.get('query', '').strip().lower()
    stocks = load_stocks()

    if not query:
        return jsonify([])

    results = []
    for s in stocks:
        symbol = s.get('symbol', '').lower()
        name = s.get('name', '').lower()
        sector = s.get('sector', '').lower()

        if not (symbol or name):
            continue

        if symbol == query:
            rank = 0
        elif symbol.startswith(query):
            rank = 1
        elif name.startswith(query) or sector.startswith(query):
            rank = 2
        elif query in symbol:
            rank = 3
        elif query in name or query in sector:
            rank = 4
        else:
            continue

        results.append((rank, symbol, name, s))

    # Sort by relevance, then symbol length (shortest first), then alphabetically
    results.sort(key=lambda r: (r[0], len(r[1]), r[1]))

    return jsonify([r[3] for r in results[:10]])


if __name__ == "__main__":
    try:
        from waitress import serve
        serve(app, host=config.HOST, port=config.PORT, threads=4)
    except ImportError:
        app.run(
            host=config.HOST,
            port=config.PORT,
            debug=config.DEBUG,
            threaded=config.THREADED,
        )