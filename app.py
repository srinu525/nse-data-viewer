import csv
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from curl_cffi import requests as curl_requests
from flask import Flask, render_template, jsonify, request

import config

app = Flask(__name__)


@app.template_filter("inr")
def inr_filter(value):
    """Format a number for display: grouping with 2 decimals, dash for blanks."""
    if value is None:
        return "—"
    return f"{value:,.2f}"


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
    "/get_peers": config.TTL_PEERS,
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
    """Fetch historical trade data via NSE NextApi (bypasses 503 on /api/historical).

    NextApi's getHistoricalTradeData silently clips any window to the most recent
    ~100 calendar days, so longer ranges are fetched one calendar month at a time
    and merged in chronological order.
    """
    from urllib.parse import urlencode

    fd = datetime.strptime(from_date, "%d-%m-%Y").date()
    td = datetime.strptime(to_date, "%d-%m-%Y").date()

    merged = {}
    month_start = fd.replace(day=1)
    while month_start <= td:
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        window_to = min(month_end, td)
        url = config.NEXT_API_URL + "?" + urlencode(dict(
            functionName="getHistoricalTradeData", symbol=symbol.upper(),
            series="EQ", fromDate=month_start.strftime("%d-%m-%Y"),
            toDate=window_to.strftime("%d-%m-%Y"),
        ))
        data = nse_fetcher.fetch_nse_data(url, cache_ttl=config.TTL_HISTORICAL)
        if isinstance(data, list):
            for entry in data:
                merged[entry.get("mtimestamp")] = entry
        month_start = month_end + timedelta(days=1)

    return sorted(merged.values(), key=lambda e: datetime.strptime(e["mtimestamp"], "%d-%b-%Y"), reverse=True) or None


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


# NSE timestamps are epoch ms whose UTC rendering is already IST wall-clock
# time (a 09:15 IST print arrives as 09:15 UTC). Format in UTC on purpose -
# adding the +05:30 offset would push every label five and a half hours ahead.
UTC = timezone.utc


def convert_timestamp(timestamp_ms):
    """Convert an NSE epoch-millisecond timestamp to market (IST) clock/date."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
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


def _live_series(data_points):
    """Serialize a price series for charting.

    ``timestamps`` (epoch seconds, IST-aligned) lets the client plot a real
    time axis instead of evenly spaced string labels, so trading gaps render
    at their true width. ``times`` stays for display/tooltips.
    """
    return {
        "timestamps": [point["timestamp"] / 1000.0 for point in data_points],
        "times": [point["time"] for point in data_points],
        "prices": [point["price"] for point in data_points],
        "all_data": data_points,
    }


def _default_dates():
    to_date = datetime.now()
    from_date = to_date - timedelta(days=15)
    return (
        from_date.strftime("%Y-%m-%d"),
        to_date.strftime("%Y-%m-%d"),
    )


def _chart_axis_config():
    """Session windows (minutes from midnight, IST) for the client charts.

    Each intraday chart keeps a fixed, full-width x axis so the plotted line
    grows within a constant frame instead of the frame rescaling on every poll.
    """
    return {
        "pre_open_open_minute": config.PRE_OPEN_START_MINUTE,
        "pre_open_close_minute": config.PRE_OPEN_END_MINUTE,
        "market_open_minute": config.MARKET_OPEN_MINUTE,
        "market_close_minute": config.MARKET_CLOSE_MINUTE,
    }


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
            **_chart_axis_config(),
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
        **_chart_axis_config(),
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
            "pre_open": _live_series(filtered_pre_open),
            "normal_market": _live_series(normal_market_data),
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


_peers_cache = None
_peers_cache_time = 0


def load_peers(force_reload=False):
    """Load symbol -> peer symbol mapping from peers.csv (comma-separated)."""
    global _peers_cache, _peers_cache_time
    now = time.time()
    if _peers_cache is not None and not force_reload and (now - _peers_cache_time) < config.STOCKS_CACHE_TTL:
        return _peers_cache

    peers_map = {}
    try:
        with open('peers.csv', mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            for row in reader:
                symbol = (row.get('symbol') or '').strip().upper()
                raw = row.get('peers') or ''
                peers = [p.strip().upper() for p in raw.split(',') if p.strip()]
                if symbol and peers:
                    peers_map[symbol] = peers
    except FileNotFoundError:
        logger.warning("peers.csv file not found - peers tab will show sector info only")
    except Exception as e:
        logger.error("Error loading peers.csv: %s", e)

    _peers_cache = peers_map
    _peers_cache_time = now
    return peers_map


@app.route('/get_peers')
def get_peers():
    symbol = request.args.get('symbol', '').strip().upper()

    if not is_valid_symbol(symbol):
        return jsonify({"error": "Invalid symbol."}), 400

    peers_map = load_peers()
    known = {s.get('symbol', '').upper() for s in load_stocks()}
    peer_symbols = [
        p for p in peers_map.get(symbol, [])
        if p != symbol and p in known
    ]

    sector = {}
    try:
        quote_response = next_api_quote(symbol)
        if quote_response:
            legacy = map_next_quote_to_legacy(quote_response)
            sector = {
                "industry": legacy["info"].get("industry"),
                "sectorIndex": legacy["metadata"].get("pdSectorInd"),
                "sectorPe": legacy["metadata"].get("pdSectorPe"),
            }
    except Exception as e:
        logger.warning("Error fetching sector data for peers: %s", e)

    peers = []
    if peer_symbols:
        for peer in peer_symbols[:config.MAX_PEERS]:
            try:
                pq = next_api_quote(peer)
                if not pq:
                    continue
                legacy = map_next_quote_to_legacy(pq)
                peers.append({
                    "symbol": legacy["info"].get("symbol") or peer,
                    "name": legacy["info"].get("companyName"),
                    "lastPrice": legacy["priceInfo"].get("lastPrice"),
                    "change": legacy["priceInfo"].get("change"),
                    "pChange": legacy["priceInfo"].get("pChange"),
                    "pe": legacy["metadata"].get("pdSectorPe"),
                    "industry": legacy["info"].get("industry"),
                })
            except Exception as e:
                logger.warning("Error fetching peer data for %s: %s", peer, e)

    if peers:
        message = None
    elif symbol not in peers_map:
        message = (
            "No peers are configured for %s. Add a row to peers.csv "
            "(e.g. %s,\"PEER1,PEER2\") to enable peer comparison." % (symbol, symbol)
        )
    else:
        message = "Peer data could not be loaded for %s. Please try again." % symbol

    return jsonify({
        "symbol": symbol,
        "sector": sector,
        "peers": peers,
        "message": message,
    })


@app.route('/brokerage-calculator', methods=['GET', 'POST'])
def brokerage_calculator():
    result = None
    error = None
    form = {
        'buy_price': request.form.get('buy_price', ''),
        'sell_price': request.form.get('sell_price', ''),
        'quantity': request.form.get('quantity', ''),
        'lots': request.form.get('lots', '1'),
        'broker': request.form.get('broker', 'zerodha'),
        'segment': request.form.get('segment', 'equity_intraday'),
        'strike_price': request.form.get('strike_price', ''),
        'premium': request.form.get('premium', ''),
    }
    if request.method == 'POST':
        try:
            buy_price = float(form['buy_price']) if form['buy_price'] else 0
            sell_price = float(form['sell_price']) if form['sell_price'] else 0
            quantity = int(float(form['quantity'])) if form['quantity'] else 0
            broker = form['broker']
            segment = form['segment']

            buy_val = buy_price * quantity
            sell_val = sell_price * quantity
            turnover = buy_val + sell_val

            # ---- Brokerage ----
            # Zerodha/Dhan : 0 delivery, min(0.03% per order, Rs20)
            # Upstox       : 0 delivery, min(0.05% per order, Rs20)
            # Groww/Angel  : 0 delivery, Rs20 flat per order (Rs40 round trip)
            if segment == 'equity_delivery':
                brokerage = 0.0
            elif segment in ('equity_intraday', 'fo_futures'):
                if broker in ('groww', 'angel'):
                    brokerage = 40.0
                elif broker == 'upstox':
                    brokerage = min(0.0005 * buy_val, 20.0) + min(0.0005 * sell_val, 20.0)
                else:  # zerodha, dhan
                    brokerage = min(0.0003 * buy_val, 20.0) + min(0.0003 * sell_val, 20.0)
            elif segment == 'fo_options':
                premium = float(form['premium']) if form['premium'] else buy_price
                premium_turnover = premium * quantity
                if broker in ('groww', 'angel'):
                    brokerage = 40.0
                elif broker == 'upstox':
                    brokerage = min(0.0005 * premium_turnover, 20.0) * 2
                else:  # zerodha, dhan
                    brokerage = min(0.0003 * premium_turnover, 20.0) * 2
            else:
                brokerage = 0.0
            brokerage = round(brokerage, 2)

            # Groww charge computation helpers
            import math
            def fl(x): return int(x * 100) / 100.0          # floor to paisa
            def cl(x): return math.ceil(x * 100) / 100.0    # ceil to paisa

            # ---- STT: 0.025% on sell_val, floored to paisa ----
            if segment == 'equity_delivery':
                stt = fl(0.001 * (buy_val + sell_val))
            elif segment == 'equity_intraday':
                stt = fl(0.00025 * sell_val)
            elif segment == 'fo_futures':
                stt = fl(0.000125 * sell_val)
            elif segment == 'fo_options':
                stt = fl(0.001 * sell_val)
            else:
                stt = 0.0

            # ---- Exchange Transaction Charges ----
            # Groww: 0.003412% of turnover
            # All others (Zerodha/Upstox/Dhan/Angel): NSE standard 0.00297%
            if segment in ('equity_delivery', 'equity_intraday'):
                exc = fl(0.00003412 * turnover) if broker == 'groww' else fl(0.0000297 * turnover)
            elif segment == 'fo_futures':
                exc = fl(0.00000173 * turnover)
            elif segment == 'fo_options':
                exc = fl(0.0003503 * (buy_val + sell_val) / 2)
            else:
                exc = 0.0

            # ---- SEBI Charges: ₹10 per crore, floored ----
            sebi = fl(0.000001 * turnover)

            # ---- Stamp Duty: 0.003% on buy_val, ceiled to paisa ----
            if segment == 'equity_delivery':
                stamp = cl(0.00015 * buy_val)
            elif segment == 'equity_intraday':
                stamp = cl(0.00003 * buy_val)
            elif segment == 'fo_futures':
                stamp = cl(0.00002 * buy_val)
            elif segment == 'fo_options':
                stamp = cl(0.00003 * buy_val)
            else:
                stamp = 0.0

            # ---- DP Charges: delivery sell side only ----
            # Angel One: ₹20 + 18% GST = ₹23.60
            # Upstox   : ₹18.5 + 18% GST = ₹21.83
            # Others   : ₹13.5 + 18% GST = ₹15.93 (CDSL)
            if segment == 'equity_delivery':
                if broker == 'angel':  dp = round(20 * 1.18, 2)
                elif broker == 'upstox': dp = round(18.5 * 1.18, 2)
                else: dp = round(13.5 * 1.18, 2)
            else:
                dp = 0.0

            # ---- GST: 18% on (brokerage + exchange + SEBI), rounded ----
            gst = round(0.18 * (brokerage + exc + sebi), 2)

            total_charges = round(brokerage + stt + exc + sebi + stamp + dp + gst, 2)
            gross_pnl = round(sell_val - buy_val, 2)
            net_profit = round(gross_pnl - total_charges, 2)
            profit_pct = round((net_profit / buy_val * 100), 2) if buy_val > 0 else 0

            result = {
                'buy_value': round(buy_val, 2),
                'sell_value': round(sell_val, 2),
                'turnover': round(turnover, 2),
                'gross_pnl': gross_pnl,
                'brokerage': brokerage,
                'stt': stt,
                'exchange_charges': exc,
                'sebi_charges': sebi,
                'stamp_duty': stamp,
                'dp_charges': dp,
                'gst': gst,
                'total_charges': total_charges,
                'net_profit': net_profit,
                'profit_percent': profit_pct,
            }
        except Exception as e:
            error = str(e)
    return render_template('brokerage_calculator.html', form=form, result=result, error=error)


TARGET_DEFAULTS = {
    'capital': '10000',
    'target_pct': '1',
    'start_date': '',
    'days': '100',
}
MAX_TARGET_DAYS = 1000
TARGET_MILESTONES = (30, 100, 365, 1000)


def compute_target_plan(capital, target_pct, start_date, days, daily_pnl=None,
                        holidays=None, weekends_as_holidays=True):
    """Build the day-by-day plan from the 'Target' sheet of DreamProject.xlsx.

    Target side: the balance compounds at ``target_pct`` per day
    (profit = investment x pct, closing = investment + profit, and the next
    day invests the previous closing balance).

    Achievement side: you type each day's profit/loss (column H of the sheet).
    Net P/L adds every entry to a running total and Difference compares that
    with the accumulated target. 'Today Target Amount' adapts to the gap: the
    day's target profit minus the last difference (zero before the first
    entry), so it shows what today must earn to get back on track. Rows
    without an entry show blank Net P/L / Difference, as in the sheet.

    Holidays (Saturdays and Sundays by default, plus any dates in
    ``holidays``) do not trade: no compounding, no target and no P/L row
    values - the next trading day continues from the same balance.
    """
    entered = list(daily_pnl or [])
    holiday_set = {
        h.isoformat() if hasattr(h, 'isoformat') else str(h)
        for h in (holidays or [])
    }
    rate = target_pct / 100.0

    rows = []
    target_invest = capital
    net_target = 0.0
    net_pnl = 0.0
    last_diff = 0.0
    last_entry_index = None

    for i in range(days):
        row_date = start_date + timedelta(days=i)
        weekend = row_date.weekday() >= 5
        is_holiday = (weekend and weekends_as_holidays) or row_date.isoformat() in holiday_set

        if is_holiday:
            rows.append({
                "day": i + 1,
                "date": row_date.strftime("%d/%m/%Y"),
                "iso": row_date.isoformat(),
                "t_invest": target_invest,
                "t_pct": target_pct,
                "t_pct_text": f"{target_pct:g}%",
                "t_profit": None,
                "t_close": target_invest,
                "today_target": None,
                "pnl": None,
                "net_pnl": None,
                "t_net": net_target,
                "diff": None,
                "holiday": True,
                "weekend": weekend,
            })
            continue

        target_profit = target_invest * rate
        target_close = target_invest + target_profit
        net_target += target_profit

        today_target = target_profit - last_diff
        pnl = entered[i] if i < len(entered) else None
        row_net = None
        diff = None
        if pnl is not None:
            net_pnl += pnl
            row_net = net_pnl
            diff = row_net - net_target
            last_diff = diff
            last_entry_index = i

        rows.append({
            "day": i + 1,
            "date": row_date.strftime("%d/%m/%Y"),
            "iso": row_date.isoformat(),
            "t_invest": target_invest,
            "t_pct": target_pct,
            "t_pct_text": f"{target_pct:g}%",
            "t_profit": target_profit,
            "t_close": target_close,
            "today_target": today_target,
            "pnl": pnl,
            "net_pnl": row_net,
            "t_net": net_target,
            "diff": diff,
            "holiday": False,
            "weekend": weekend,
        })
        target_invest = target_close

    tradable = [r for r in rows if not r["holiday"]]
    first_missing = next(
        (i for i, r in enumerate(rows) if not r["holiday"] and r["pnl"] is None), None)
    if first_missing is not None:
        req_i = first_missing
    elif tradable:
        req_i = rows.index(tradable[-1])
    else:
        req_i = days - 1

    summary = {
        "days": days,
        "trading_days": len(tradable),
        "holiday_count": days - len(tradable),
        "target_close": rows[-1]["t_close"],
        "target_profit_total": net_target,
        "next_day": req_i + 1,
        "next_today_target": rows[req_i]["today_target"],
        "all_tracked": first_missing is None,
        "net_pnl": net_pnl if last_entry_index is not None else None,
        "entry_day": None if last_entry_index is None else last_entry_index + 1,
        "entry_date": None if last_entry_index is None else rows[last_entry_index]["date"],
        "net_target_at_entry": None if last_entry_index is None else rows[last_entry_index]["t_net"],
        "difference": None if last_entry_index is None else rows[last_entry_index]["diff"],
        "milestones": [
            {
                "day": n,
                "balance": capital * (1 + rate) ** n,
                "profit": capital * ((1 + rate) ** n - 1),
            }
            for n in TARGET_MILESTONES
        ],
    }
    return rows, summary


TARGET_DB_PATH = os.environ.get('NSE_TARGET_DB') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'target.db')


def _target_conn():
    conn = sqlite3.connect(TARGET_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_target_db():
    with _target_conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS target_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            capital REAL NOT NULL,
            target_pct REAL NOT NULL,
            start_date TEXT NOT NULL,
            days INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS target_entries (
            day INTEGER PRIMARY KEY,
            pnl REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS target_holidays (
            date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')


def load_target_state():
    """Return the saved settings row (as None if never saved) and {day: pnl}."""
    init_target_db()
    with _target_conn() as conn:
        row = conn.execute(
            'SELECT capital, target_pct, start_date, days FROM target_settings WHERE id = 1'
        ).fetchone()
        entries = {
            int(r['day']): float(r['pnl'])
            for r in conn.execute('SELECT day, pnl FROM target_entries')
        }
    return (dict(row) if row else None), entries


def load_holidays():
    """Saved extra holidays as ISO dates (weekends are always holidays)."""
    init_target_db()
    with _target_conn() as conn:
        return [r['date'] for r in
                conn.execute('SELECT date FROM target_holidays ORDER BY date')]


def save_holidays(dates):
    init_target_db()
    with _target_conn() as conn:
        conn.execute('DELETE FROM target_holidays')
        conn.executemany(
            'INSERT OR IGNORE INTO target_holidays (date) VALUES (?)',
            [(d,) for d in dates],
        )


def save_target_state(values, pnls):
    """Persist settings plus the day-indexed P/L entries (None clears a day)."""
    init_target_db()
    with _target_conn() as conn:
        conn.execute(
            '''INSERT INTO target_settings (id, capital, target_pct, start_date, days, updated_at)
               VALUES (1, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET capital=excluded.capital,
               target_pct=excluded.target_pct, start_date=excluded.start_date,
               days=excluded.days, updated_at=datetime('now')''',
            (values['capital'], values['target_pct'],
             values['start_date'].isoformat(), values['days']),
        )
        conn.execute('DELETE FROM target_entries')
        conn.executemany(
            'INSERT INTO target_entries (day, pnl) VALUES (?, ?)',
            [(i + 1, p) for i, p in enumerate(pnls) if p is not None],
        )


def _parse_target_settings(raw):
    """Validate capital / % / days / start date. Returns (form, values, error)."""
    form = {}
    for k in TARGET_DEFAULTS:
        v = raw.get(k)
        form[k] = '' if v is None else str(v).strip()
    error = None
    try:
        capital = float(form['capital'])
        if capital <= 0:
            error = 'Starting capital must be greater than 0.'
    except ValueError:
        error = 'Starting capital must be a number.'

    target_pct = days = start_date = None
    if error is None:
        try:
            target_pct = float(form['target_pct'])
            if not 0 <= target_pct <= 100:
                error = 'Target % must be between 0 and 100.'
        except ValueError:
            error = 'Target % must be a number.'

    if error is None:
        try:
            days = int(form['days'])
            if not 1 <= days <= MAX_TARGET_DAYS:
                error = f'Days must be between 1 and {MAX_TARGET_DAYS}.'
        except ValueError:
            error = 'Days must be a whole number.'

    if error is None:
        try:
            start_date = datetime.strptime(form['start_date'], '%Y-%m-%d').date()
        except ValueError:
            error = 'Start date must be a valid date.'

    if error is not None:
        return form, None, error
    return form, {'capital': capital, 'target_pct': target_pct,
                  'days': days, 'start_date': start_date}, None


def _parse_pnl_list(raw_items):
    """Accept a list of daily P/L values (numbers, blanks or None)."""
    pnls = []
    for raw in raw_items or []:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            pnls.append(None)
            continue
        try:
            pnls.append(float(raw))
        except (TypeError, ValueError):
            return None, 'Profit/loss must be a number.'
    return pnls, None


def _parse_holidays(raw_items):
    """Accept a list of holiday dates (ISO format)."""
    dates = []
    for raw in raw_items or []:
        s = str(raw).strip()
        if not s:
            continue
        try:
            datetime.strptime(s, '%Y-%m-%d')
        except ValueError:
            return None, 'Holiday dates must be in YYYY-MM-DD format.'
        dates.append(s)
    return dates, None


@app.route('/target', methods=['GET', 'POST'])
def target():
    saved, saved_entries = load_target_state()
    defaults = dict(TARGET_DEFAULTS, start_date=datetime.now().date().isoformat())
    if saved:
        defaults.update({
            'capital': f"{saved['capital']:g}",
            'target_pct': f"{saved['target_pct']:g}",
            'start_date': saved['start_date'],
            'days': str(saved['days']),
        })

    form = defaults
    rows = summary = None
    pnl_values = []
    holidays = load_holidays()
    error = None

    if request.method == 'POST':
        form, values, error = _parse_target_settings(request.form)
        if error is None:
            pnls, error = _parse_pnl_list(request.form.getlist('pnl'))
        if error is None:
            save_target_state(values, pnls[:values['days']])
            rows, summary = compute_target_plan(
                values['capital'], values['target_pct'],
                values['start_date'], values['days'], pnls[:values['days']],
                holidays=holidays,
            )
            pnl_values = [r.strip() for r in request.form.getlist('pnl')[:values['days']]]
            pnl_values += [''] * (values['days'] - len(pnl_values))
    else:
        days = int(defaults['days'])
        start = datetime.strptime(defaults['start_date'], '%Y-%m-%d').date()
        pnls = [saved_entries.get(d) for d in range(1, days + 1)]
        rows, summary = compute_target_plan(
            float(defaults['capital']), float(defaults['target_pct']),
            start, days, pnls, holidays=holidays,
        )
        pnl_values = [format(p, '.10g') if p is not None else '' for p in pnls]

    return render_template(
        'target.html',
        form=form,
        rows=rows,
        summary=summary,
        pnl_values=pnl_values,
        holidays=holidays,
        error=error,
    )


@app.route('/target/save', methods=['POST'])
def target_save():
    """Debounced save from the page's live editor (JSON body)."""
    data = request.get_json(silent=True) or {}
    _, values, error = _parse_target_settings(data)
    pnls = []
    if error is None:
        pnls, error = _parse_pnl_list(data.get('pnls'))
    hol = None
    if error is None and 'holidays' in data:
        hol, error = _parse_holidays(data.get('holidays'))
    if error is not None:
        return jsonify(error=error), 400
    save_target_state(values, pnls[:values['days']])
    if hol is not None:
        save_holidays(hol)
    return jsonify(ok=True)


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
