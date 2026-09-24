import csv
import os
import requests

EQUITY_LIST_URLS = [
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "stocks.csv")


def fetch_csv():
    last_error = None
    for url in EQUITY_LIST_URLS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as e:
            last_error = e
            print(f"Retrying with fallback after error from {url}: {e}")
    raise last_error


def download_nse_stocks():
    try:
        text = fetch_csv()
        reader = csv.DictReader(text.splitlines())

        # Normalise header keys (' SERIES' / 'SERIES' / 'series' -> lowercase strip)
        field_map = {}
        for key in (reader.fieldnames or []):
            if key is None:
                continue
            norm = key.strip().lower()
            field_map.setdefault(norm, key)

        sym_col = field_map.get("symbol")
        name_col = field_map.get("name of company")
        series_col = field_map.get("series")
        if not (sym_col and series_col):
            raise ValueError("Unexpected CSV columns: %r" % (reader.fieldnames,))

        seen = set()
        stocks = []
        for row in reader:
            series = (row.get(series_col) or "").strip()
            if series != "EQ":
                continue
            symbol = (row.get(sym_col) or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            stocks.append({
                "symbol": symbol,
                "name": (row.get(name_col) or "").strip() if name_col else "",
                "series": series,
            })

        stocks.sort(key=lambda s: s["symbol"])
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["symbol", "name", "series"])
            for stock in stocks:
                writer.writerow([stock["symbol"], stock["name"], stock["series"]])

        print(f"Updated {len(stocks)} stocks -> {OUTPUT_CSV}")
        return True

    except Exception as e:
        print(f"Error: {e}")
        return False


if __name__ == "__main__":
    if not download_nse_stocks():
        print("Failed to update stocks.csv")