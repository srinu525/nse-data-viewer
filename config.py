"""Central configuration for the NSE Data Viewer app.

Keeps NSE endpoints, cache/rate-limit tuning and market-hours logic out of
app.py so they can be adjusted without touching route code.
"""

NEXT_API_URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
PRE_OPEN_URL = "https://www.nseindia.com/api/market-data-pre-open?key=ALL"

# Individual endpoints used by NSEBrowserSimulator
NSE_HOME_URL = "https://www.nseindia.com"
ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"

# Request throttling (minimum seconds between NSE requests)
REQUEST_DELAY = 1.2

# Cache TTLs (seconds) per data source
TTL_QUOTE = 10
TTL_HISTORICAL = 300
TTL_LIVE_CHART = 10
TTL_INDICES = 15
TTL_PRE_OPEN = 30

# Session init sequence
INIT_HTML_URL = "https://www.nseindia.com"
INIT_INDICES_URL = "https://www.nseindia.com/api/allIndices"
INIT_SLEEP_RANGE = (0.5, 1.0)
RETRY_SLEEP_RANGE = (0.5, 1.0)
SESSION_REINIT_FAIL_SLEEP = 5

# Frontend polling behaviour (ms)
POLL_INDICES_OPEN = 15000
POLL_INDICES_CLOSED = 60000
POLL_STOCK_OPEN = 5000
POLL_STOCK_CLOSED = 60000
CHART_UPDATE_INTERVAL_OPEN = 4000
CHART_UPDATE_INTERVAL_CLOSED = 60000

# Market hours (IST, minutes from midnight) - Mon-Fri only
MARKET_OPEN_MINUTE = 9 * 60          # 09:00
MARKET_CLOSE_MINUTE = 15 * 60 + 30   # 15:30

# Stock list refresh TTL (seconds) for in-memory CSV cache
STOCKS_CACHE_TTL = 900

# Server
HOST = "0.0.0.0"
PORT = 5000
DEBUG = False
THREADED = True