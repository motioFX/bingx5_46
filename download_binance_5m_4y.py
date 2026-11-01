#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance 過去4年分 (2021年〜現在 / 上場来全期間) 5分足 (5m) 超高速ダウンローダー
対象: 固定選定20銘柄 + BTC-USDT (計21銘柄)
データソース: Binance Vision 公開アーカイブ (月別ZIP) + Binance REST API (直近最新差分)
"""
import io
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = Path(__file__).resolve().parent / "Data"
OUT_DIR = DATA_DIR / "binance_candles_5m_4y"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 対象21銘柄
SYMBOLS: List[str] = [
    "BTCUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "SUIUSDT", "AVAXUSDT",
    "LINKUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "UNIUSDT", "OPUSDT",
    "TIAUSDT", "RENDERUSDT", "INJUSDT", "FETUSDT", "TAOUSDT", "AAVEUSDT",
    "ENAUSDT", "ZECUSDT", "HYPEUSDT"
]

# 取得対象の年月リスト (過去4年分: 2021年1月 〜 2026年11月)
START_YEAR = 2021
END_YEAR = 2026

YEAR_MONTHS: List[str] = []
for y in range(START_YEAR, END_YEAR + 1):
    for m in range(1, 13):
        if y == END_YEAR and m > 11:
            continue
        YEAR_MONTHS.append(f"{y:04d}-{m:02d}")


def download_monthly_zip(session: requests.Session, symbol: str, ym: str) -> Optional[pd.DataFrame]:
    """Binance Vision から指定年月の 5分足 ZIP を取得して DataFrame 化"""
    # 1. まず先物 (futures/um) を試す
    urls = [
        f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/5m/{symbol}-5m-{ym}.zip",
        f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/5m/{symbol}-5m-{ym}.zip"
    ]

    for url in urls:
        try:
            r = session.get(url, timeout=10)
            if r.status_code == 200 and len(r.content) > 1000:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    for filename in z.namelist():
                        if filename.endswith(".csv"):
                            with z.open(filename) as f:
                                # Binance Vision CSV はヘッダーなしの場合が多い
                                first_line = f.readline().decode("utf-8")
                                f.seek(0)
                                if "open_time" in first_line or "open" in first_line:
                                    df = pd.read_csv(f)
                                else:
                                    # 0: open_time, 1: open, 2: high, 3: low, 4: close, 5: volume, 6: close_time, 7: quote_volume, 8: count
                                    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore"]
                                    df = pd.read_csv(f, names=cols)
                                return df
        except Exception:
            pass

    return None


def fetch_recent_rest_klines(symbol: str, start_ts_ms: int) -> pd.DataFrame:
    """Binance REST API から指定タイムスタンプ以降の最新足を補完取得"""
    url_f = "https://fapi.binance.com/fapi/v1/klines"
    all_rows = []
    curr_start = start_ts_ms
    session = requests.Session()

    for _ in range(30):  # 最大30リクエスト (1500本 * 30 = 45,000本 / 約150日分)
        try:
            params = {
                "symbol": symbol,
                "interval": "5m",
                "startTime": curr_start,
                "limit": 1500
            }
            resp = session.get(url_f, params=params, timeout=10)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                break

            all_rows.extend(data)
            last_ts = data[-1][0]
            curr_start = last_ts + 1
            if len(data) < 1500:
                break
            time.sleep(0.05)
        except Exception:
            break

    if not all_rows:
        return pd.DataFrame()

    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore"]
    df = pd.DataFrame(all_rows, columns=cols)
    return df


def download_symbol_4y(symbol: str) -> Optional[Path]:
    """1銘柄の過去4年分5分足をダウンロードして保存"""
    print(f"▶ [{symbol}] 4年分データ取得開始 (対象年月: {len(YEAR_MONTHS)} ヶ月)...")
    t0 = time.time()
    session = requests.Session()
    dfs = []

    # 1. Binance Vision から月別データを並列/順次取得
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(download_monthly_zip, session, symbol, ym): ym for ym in YEAR_MONTHS}
        for future in as_completed(future_map):
            ym = future_map[future]
            try:
                res_df = future.result()
                if res_df is not None and not res_df.empty:
                    dfs.append(res_df)
            except Exception:
                pass

    if not dfs:
        # Vision にない新興銘柄等の場合、REST API で最古から一括取得
        print(f"  [{symbol}] Vision未検出のため REST API より全取得フォールバック...")
        recent_df = fetch_recent_rest_klines(symbol, 0)
        if recent_df is not None and not recent_df.empty:
            dfs.append(recent_df)

    if not dfs:
        print(f"  ❌ [{symbol}] データ取得失敗")
        return None

    # 結合・整形
    df_all = pd.concat(dfs, ignore_index=True)
    df_all["open_time"] = pd.to_numeric(df_all["open_time"], errors="coerce")
    df_all.dropna(subset=["open_time"], inplace=True)
    df_all["open_time"] = df_all["open_time"].astype(int)

    # 2. 最新足の補完 (Vision の最新月以降〜現在)
    max_ts = int(df_all["open_time"].max())
    now_ts = int(time.time() * 1000)
    if (now_ts - max_ts) > (5 * 60 * 1000 * 10):  # 10本以上未収録なら補完
        recent_df = fetch_recent_rest_klines(symbol, max_ts + 1)
        if not recent_df.empty:
            recent_df["open_time"] = pd.to_numeric(recent_df["open_time"], errors="coerce")
            recent_df.dropna(subset=["open_time"], inplace=True)
            recent_df["open_time"] = recent_df["open_time"].astype(int)
            df_all = pd.concat([df_all, recent_df], ignore_index=True)

    # フォーマット正規化
    df_all["timestamp"] = pd.to_datetime(df_all["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df_all.columns:
            df_all[col] = df_all[col].astype(float)

    # quote_volume / trades
    if "quote_volume" in df_all.columns:
        df_all["quote_volume"] = pd.to_numeric(df_all["quote_volume"], errors="coerce").fillna(0.0)
    else:
        df_all["quote_volume"] = df_all["volume"] * df_all["close"]

    df_out = df_all[["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]].copy()
    df_out.drop_duplicates(subset=["timestamp"], inplace=True)
    df_out.sort_values(by="timestamp", inplace=True)
    df_out.reset_index(drop=True, inplace=True)

    # 保存
    csv_path = OUT_DIR / f"{symbol}_5m.csv"
    df_out.to_csv(csv_path, index=False)

    # 高速読み込み用 Pickle / Feather (もし利用可能なら)
    pkl_path = OUT_DIR / f"{symbol}_5m.pkl"
    try:
        df_out.to_pickle(pkl_path)
    except Exception:
        pass

    min_d = df_out["timestamp"].iloc[0].strftime("%Y-%m-%d")
    max_d = df_out["timestamp"].iloc[-1].strftime("%Y-%m-%d")
    dur_days = (df_out["timestamp"].iloc[-1] - df_out["timestamp"].iloc[0]).days
    el_s = time.time() - t0

    print(f"  ✅ [{symbol}] 保存完了: {len(df_out):,} 本 ({dur_days} 日間: {min_d} 〜 {max_d}) | {el_s:.1f}秒 ➔ {csv_path.name}")
    return csv_path


def main():
    print("=" * 80)
    print("🚀 【Binance 過去4年分 5分足 (5m) 一括ダウンローダー】")
    print(f"対象銘柄: {len(SYMBOLS)} 銘柄 ({', '.join(SYMBOLS[:5])}...)")
    print(f"保存先: {OUT_DIR}")
    print("=" * 80)

    t_start = time.time()
    success_count = 0

    for sym in SYMBOLS:
        p = download_symbol_4y(sym)
        if p and p.exists():
            success_count += 1
        time.sleep(0.2)

    total_time = time.time() - t_start
    print("\n" + "=" * 80)
    print(f"🎉 全21銘柄のダウンロード完了！ (成功: {success_count}/{len(SYMBOLS)} 銘柄 | 所要時間: {total_time/60:.1f}分)")
    print(f"📁 保存ディレクトリ: {OUT_DIR}")
    print("=" * 80)


if __name__ == "__main__":
    main()
