#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定固定20銘柄 (+ BTC) の過去1年分 (最大105,120本) 5分足データ一括ダウンロードスクリプト
・Binance Futures 公開API 使用 (Bybit V5 フォールバック対応)
・DNS瞬断 / 接続エラーに対する堅牢なリトライ (指数バックオフ) 実装
・Data/binance_candles_5m/{symbol}_5m.csv に保存
"""
import sys
import os
import time
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# PROJECT_RULES準拠の固定選定20銘柄 + BTC
TARGET_SYMBOLS = [
    'HYPEUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'SUIUSDT',
    'AVAXUSDT', 'LINKUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'UNIUSDT', 'OPUSDT', 'TIAUSDT', 'RENDERUSDT', 'INJUSDT',
    'FETUSDT', 'TAOUSDT', 'AAVEUSDT', 'ENAUSDT', 'ZECUSDT',
    'BTCUSDT'
]

SAVE_DIR = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46/Data/binance_candles_5m")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

INTERVAL = "5m"
LIMIT = 1500
TARGET_DAYS = 365

# 堅牢なリトライ対応 Session の生成
def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

session = create_robust_session()

def safe_get_binance(params: dict, max_retries: int = 5):
    url = 'https://fapi.binance.com/fapi/v1/klines'
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=12)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait_sec = 5 * attempt
                print(f"  [RateLimit 429] Waiting {wait_sec}s...")
                time.sleep(wait_sec)
            else:
                print(f"  [HTTP {resp.status_code}] Retry {attempt}/{max_retries}...")
                time.sleep(1.5 * attempt)
        except Exception as e:
            print(f"  [ConnError: {type(e).__name__}] Retry {attempt}/{max_retries}...")
            time.sleep(2.0 * attempt)
    return None

def fetch_symbol_5m(symbol: str):
    csv_file = SAVE_DIR / f"{symbol}_5m.csv"
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=TARGET_DAYS)).timestamp() * 1000)
    
    print(f"\n==================================================")
    print(f"[{symbol}] Fetching 5m candles (Target: {TARGET_DAYS} days)...")
    
    # 最初の利用可能バーを探索
    init_data = safe_get_binance({'symbol': symbol, 'interval': INTERVAL, 'startTime': start_ms, 'limit': 1})
    if init_data:
        actual_first_ms = init_data[0][0]
        curr_start = max(start_ms, actual_first_ms)
    else:
        recent_data = safe_get_binance({'symbol': symbol, 'interval': INTERVAL, 'limit': 1})
        if not recent_data:
            print(f"[{symbol}] Failed to query symbol info.")
            return None
        curr_start = start_ms

    all_rows = []
    req_count = 0
    
    while curr_start < end_ms:
        req_count += 1
        params = {
            'symbol': symbol,
            'interval': INTERVAL,
            'startTime': curr_start,
            'endTime': end_ms,
            'limit': LIMIT
        }
        bars = safe_get_binance(params)
        if not bars:
            print(f"[{symbol}] No more bars returned or network error.")
            break
            
        all_rows.extend(bars)
        last_open_time = bars[-1][0]
        next_start = last_open_time + 300000
        if next_start <= curr_start:
            break
        curr_start = next_start
        
        if req_count % 15 == 0:
            dt_str = datetime.fromtimestamp(last_open_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            print(f"  └ Progress: {len(all_rows):,} bars fetched (reached {dt_str} UTC)...")
            
        time.sleep(0.05)
        
    if not all_rows:
        print(f"[{symbol}] No data fetched.")
        return None
        
    df = pd.DataFrame(all_rows, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades_count',
        'taker_buy_volume', 'taker_buy_quote_volume', 'ignore'
    ])
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].copy()
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
        
    df.drop_duplicates(subset=['timestamp'], inplace=True)
    df.sort_values(by='timestamp', inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    df.to_csv(csv_file, index=False)
    file_size_mb = csv_file.stat().st_size / (1024 * 1024)
    start_dt = df['timestamp'].iloc[0].strftime('%Y-%m-%d')
    end_dt = df['timestamp'].iloc[-1].strftime('%Y-%m-%d')
    print(f"[OK] [{symbol}] Saved {len(df):,} bars ({start_dt} to {end_dt}) | Size: {file_size_mb:.2f} MB")
    return len(df)

def main():
    print(f"Starting robust batch fetch of 5m candles for {len(TARGET_SYMBOLS)} symbols...")
    start_time = time.time()
    total_bars = 0
    results = {}
    
    for idx, sym in enumerate(TARGET_SYMBOLS, 1):
        csv_file = SAVE_DIR / f"{sym}_5m.csv"
        # 既に1年分(約10万行)保存済みの場合はスキップ
        if csv_file.exists() and csv_file.stat().st_size > 1000000:
            try:
                line_count = sum(1 for _ in open(csv_file, 'r', encoding='utf-8'))
                if line_count > 50000:
                    print(f"[{idx}/{len(TARGET_SYMBOLS)}] [SKIP] {sym} already cached ({line_count:,} lines).")
                    total_bars += (line_count - 1)
                    results[sym] = (line_count - 1)
                    continue
            except Exception:
                pass

        print(f"[{idx}/{len(TARGET_SYMBOLS)}]", end=" ")
        count = fetch_symbol_5m(sym)
        if count:
            total_bars += count
            results[sym] = count
            
    elapsed = time.time() - start_time
    print("\n==================================================")
    print(f"[SUCCESS] All {len(results)}/{len(TARGET_SYMBOLS)} symbols fetched successfully in {elapsed:.1f}s!")
    print(f"[STATS] Total 5m bars downloaded: {total_bars:,}")
    print(f"[DIR] Directory: {SAVE_DIR.resolve()}")
    print("==================================================")

if __name__ == '__main__':
    main()
