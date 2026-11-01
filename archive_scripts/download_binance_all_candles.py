#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance 全期間（最古〜本日）ヒストリカルデータ 一括ダウンローダー
(1時間足 1h & 日足 1d)
対象: BTC, ETH, 主要20銘柄, および歴史的トップアルト
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = Path(__file__).resolve().parent / "Data"
DIR_1H = DATA_DIR / "binance_candles_1h"
DIR_1D = DATA_DIR / "binance_candles_1d"
DIR_1H.mkdir(parents=True, exist_ok=True)
DIR_1D.mkdir(parents=True, exist_ok=True)

# 取得対象シンボルリスト (Binance シンボル名)
# 1. BTC, ETH (ベンチマーク・最長期間 2017年〜)
# 2. 主要アルト20銘柄 (ボット対象)
# 3. 歴史的メジャーアルト (2017〜2018年からの地合い用: BNB, ADA, LTC, TRX, DOT)
SYMBOLS = [
    # ベンチマーク
    "BTCUSDT", "ETHUSDT",
    # ボット固定20銘柄
    "SOLUSDT", "DOGEUSDT", "XRPUSDT", "SUIUSDT", "AVAXUSDT",
    "LINKUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "UNIUSDT",
    "OPUSDT", "TIAUSDT", "RENDERUSDT", "INJUSDT", "FETUSDT",
    "TAOUSDT", "AAVEUSDT", "ENAUSDT", "ZECUSDT", "HYPEUSDT",
    # 歴史的メジャーアルト (2017〜2018年〜の暴落・バブル網羅用)
    "BNBUSDT", "ADAUSDT", "LTCUSDT", "TRXUSDT", "DOTUSDT"
]

BINANCE_API_URL = "https://api.binance.com/api/v3/klines"


def fetch_all_candles(symbol: str, interval: str = "1h") -> pd.DataFrame:
    """
    指定銘柄の最古（上場初日）から最新足まで、1000本ずつページングして全取得する。
    """
    print(f"\n▶ [{symbol}] ({interval}) 最古データから全期間取得開始...")
    all_candles = []
    start_time = 0  # 0から開始して最古の足を取得
    session = requests.Session()

    retry_count = 0
    max_retries = 5

    while True:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "limit": 1000,
        }

        try:
            resp = session.get(BINANCE_API_URL, params=params, timeout=15)
            if resp.status_code == 429:
                print(f"  [RateLimit] 429受信。10秒待機...")
                time.sleep(10)
                continue
            elif resp.status_code != 200:
                if retry_count < max_retries:
                    retry_count += 1
                    time.sleep(2)
                    continue
                else:
                    print(f"  [Error] {resp.status_code}: {resp.text}")
                    break

            data = resp.json()
            if not data or len(data) == 0:
                break

            all_candles.extend(data)
            last_open_time = data[-1][0]
            start_time = last_open_time + 1

            # 取得本数が1000未満なら最新に到達
            if len(data) < 1000:
                break

            # レートリミット保護のためわずかにウェイト
            time.sleep(0.05)

        except Exception as e:
            if retry_count < max_retries:
                retry_count += 1
                time.sleep(2)
                continue
            else:
                print(f"  [Exception] {e}")
                break

    if not all_candles:
        print(f"  [Warn] データが取得できませんでした: {symbol}")
        return pd.DataFrame()

    # DataFrame化
    # Binance klines フォーマット:
    # 0: Open time, 1: Open, 2: High, 3: Low, 4: Close, 5: Volume,
    # 6: Close time, 7: Quote asset volume, 8: Number of trades, ...
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore"]
    df = pd.DataFrame(all_candles, columns=cols)

    # タイムスタンプ・数値変換
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for num_col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[num_col] = df[num_col].astype(float)

    df["symbol"] = symbol
    # 必要なカラムのみ抽出
    df = df[["timestamp", "symbol", "open", "high", "low", "close", "volume", "quote_volume", "trades"]]
    df.drop_duplicates(subset=["timestamp"], inplace=True)
    df.sort_values(by="timestamp", inplace=True)

    min_ts = df["timestamp"].min().strftime("%Y-%m-%d %H:%M UTC")
    max_ts = df["timestamp"].max().strftime("%Y-%m-%d %H:%M UTC")
    print(f"  ✔ 完了: {len(df):,} 本 | 期間: {min_ts} 〜 {max_ts}")

    return df


def main():
    print("=" * 80)
    print("🚀 【Binance 全期間ヒストリカルデータ一括ダウンローダー】")
    print(f"対象銘柄: {len(SYMBOLS)} 銘柄")
    print(f"足種: 1時間足 (1h) ＆ 日足 (1d)")
    print(f"保存先: 1h -> {DIR_1H} | 1d -> {DIR_1D}")
    print("=" * 80)

    # 1. 日足 (1d) を全銘柄取得
    print("\n==================== 【フェーズ 1: 日足 (1d) 全期間取得】 ====================")
    all_1d_list = []
    for sym in SYMBOLS:
        df_1d = fetch_all_candles(sym, interval="1d")
        if not df_1d.empty:
            out_file = DIR_1D / f"{sym}_1d.csv"
            df_1d.to_csv(out_file, index=False)
            all_1d_list.append(df_1d)

    if all_1d_list:
        df_1d_master = pd.concat(all_1d_list, ignore_index=True)
        master_1d_path = DIR_1D / "binance_all_crypto_1d.csv"
        df_1d_master.to_csv(master_1d_path, index=False)
        print(f"\n★ [日足マスター保存完了] {master_1d_path} ({len(df_1d_master):,} 行, {df_1d_master['symbol'].nunique()} 銘柄)")

    # 2. 1時間足 (1h) を全銘柄取得
    print("\n==================== 【フェーズ 2: 1時間足 (1h) 全期間取得】 ====================")
    all_1h_list = []
    for sym in SYMBOLS:
        df_1h = fetch_all_candles(sym, interval="1h")
        if not df_1h.empty:
            out_file = DIR_1H / f"{sym}_1h.csv"
            df_1h.to_csv(out_file, index=False)
            all_1h_list.append(df_1h)

    if all_1h_list:
        df_1h_master = pd.concat(all_1h_list, ignore_index=True)
        master_1h_path = DIR_1H / "binance_all_crypto_1h.csv"
        df_1h_master.to_csv(master_1h_path, index=False)
        print(f"\n★ [1時間足マスター保存完了] {master_1h_path} ({len(df_1h_master):,} 行, {df_1h_master['symbol'].nunique()} 銘柄)")

    print("\n" + "=" * 80)
    print("🎉 全データのダウンロード ＆ マスターCSV作成が完了しました！")
    print("=" * 80)


if __name__ == "__main__":
    main()
