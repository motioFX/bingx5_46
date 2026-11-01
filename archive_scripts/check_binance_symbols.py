import requests
import datetime
from show_symbol_status import FIXED_SYMBOLS

symbols = ["BTC-USDT", "ETH-USDT"] + list(FIXED_SYMBOLS)
url = "https://api.binance.com/api/v3/klines"

print(f"Checking {len(symbols)} symbols on Binance...")
available = []
for s in symbols:
    bin_sym = s.replace("-", "")
    r = requests.get(url, params={"symbol": bin_sym, "interval": "1h", "startTime": 0, "limit": 1})
    if r.status_code == 200 and len(r.json()) > 0:
        ts = r.json()[0][0] / 1000
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        d_str = dt.strftime("%Y-%m-%d")
        print(f"  {s:12s} -> {bin_sym:10s} : OK (Oldest: {d_str})")
        available.append((s, bin_sym, d_str))
    else:
        # 代替シンボル確認（例: HYPE は Hyperliquid ネイティブ、Binanceにあるか等）
        print(f"  {s:12s} -> {bin_sym:10s} : NOT FOUND ({r.status_code})")

print(f"\n利用可能: {len(available)} / {len(symbols)} 銘柄")
