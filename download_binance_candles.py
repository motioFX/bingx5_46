"""Binance Japan 全銘柄 マルチ時間足（1d, 1h, 15m, 5m, 1m）データ収集＆時間分割Discord配信パイプライン

仕様:
1. Binance Japan の全JPY現物ペア（約27銘柄）を対象。
2. 5つの時間足に対応:
   - 1d (日足): 過去4年分 (設立以降全期間、差分キャッシュ更新)
   - 1h (1時間足): 過去4年分 (設立以降全期間、差分キャッシュ更新)
   - 15m (15分足): 直近180日分 (差分キャッシュ更新)
   - 5m (5分足): 直近90日分 (差分キャッシュ更新)
   - 1m (1分足): 直近30日分 (差分キャッシュ更新)
3. 全銘柄網羅グリッド生成:
   - 銘柄ごとではなく、全銘柄を包含した時系列テーブルを作成。
   - 上場前などでデータが存在しない過去期間は NaN で補完。
4. 時間軸でのN分割ZIPアーカイブ生成:
   - Discord制限（25MB）内に安全に収まるよう動的サイズ判定。
5. 古い順からのDiscord順次送信:
   - 必ず Part 1（最古データ）から順次アップロードし、最後に最新データを送信。
   - 各送信間にセーフティウェイトを挿入。
6. クリーンアップ: 24時間超過した古いZIPを自動削除。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import zipfile
import gc
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url
from upload_registry import should_upload_file, record_file_uploaded
from data_pipeline_utils import (
    build_full_symbol_time_grid,
    split_and_create_time_zips,
    create_recent_slice_zip,
    upload_time_split_zips_to_discord,
    cleanup_expired_archives
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

BINANCE_API_URL = "https://api.binance.com"


def log(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


# ==================== Binance API 銘柄取得 ====================
def fetch_binance_jpy_symbols() -> List[str]:
    """Binance Japan で取引可能な全現物 JPY ペアを取得"""
    url = f"{BINANCE_API_URL}/api/v3/exchangeInfo"
    try:
        resp = requests.get(url, timeout=10).json()
        symbols = resp.get("symbols", [])
        jpy_symbols = []
        for s in symbols:
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", False):
                if s.get("quoteAsset") == "JPY":
                    jpy_symbols.append(s.get("symbol"))
        if jpy_symbols:
            return sorted(jpy_symbols)
    except Exception as e:
        log(f"[Binance API Error] 銘柄一覧取得失敗: {e}")

    # フォールバック銘柄リスト
    return [
        "ADAJPY", "APTJPY", "BCHJPY", "BNBJPY", "BTCJPY", "DOGEJPY", "ETHJPY",
        "FETJPY", "GIGGLEJPY", "IOTXJPY", "LINKJPY", "LPTJPY", "LTCJPY", "MEMEJPY",
        "NEARJPY", "PEPEJPY", "POLJPY", "SEIJPY", "SHIBJPY", "SOLJPY", "SUIJPY",
        "TAOJPY", "TRBJPY", "TRUMPJPY", "TRXJPY", "XLMJPY", "XRPJPY"
    ]


# ==================== klines 取得 ====================
def fetch_symbol_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000
) -> List[List[Any]]:
    """指定銘柄・時間足・期間の klines を取得"""
    url = f"{BINANCE_API_URL}/api/v3/klines"
    all_data = []
    curr_start = start_ms

    while curr_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": curr_start,
            "endTime": end_ms,
            "limit": limit
        }
        success = False
        for attempt in range(4):
            try:
                res = requests.get(url, params=params, timeout=12).json()
                if isinstance(res, dict) and "code" in res:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if not res or not isinstance(res, list):
                    success = True
                    break

                all_data.extend(res)
                last_open_time = res[-1][0]
                if last_open_time <= curr_start:
                    success = True
                    break
                curr_start = last_open_time + 1
                success = True
                if len(res) < limit:
                    break
                time.sleep(0.04)
                break
            except Exception:
                time.sleep(1.0 * (attempt + 1))

        if not success or (isinstance(res, list) and len(res) < limit):
            break

    return all_data


def download_symbol_candles_binance(
    symbol: str,
    interval: str,
    days: int,
    candles_dir: Path,
    force: bool = False,
    incremental_hours: int = 8
) -> pd.DataFrame:
    """差分キャッシュを考慮して指定銘柄・時間足のデータを取得・保存"""
    csv_path = candles_dir / f"{symbol}_{interval}.csv"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    start_ms = target_start_ms
    old_df = pd.DataFrame()

    if not force and csv_path.exists():
        try:
            old_df = pd.read_csv(csv_path)
            old_df["timestamp"] = pd.to_datetime(old_df["timestamp"])
            if not old_df.empty:
                max_ts = old_df["timestamp"].max()
                latest_ms = int(max_ts.replace(tzinfo=timezone.utc).timestamp() * 1000)
                # 最新データから直近までを取得
                start_ms = max(target_start_ms, latest_ms + 1)
                if incremental_hours > 0:
                    start_ms = max(start_ms, now_ms - (incremental_hours + 1) * 3600 * 1000)
        except Exception:
            old_df = pd.DataFrame()

    new_rows = []
    if start_ms < now_ms:
        raw_klines = fetch_symbol_klines(symbol, interval, start_ms, now_ms)
        for k in raw_klines:
            new_rows.append({
                "timestamp": pd.to_datetime(k[0], unit="ms"),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "symbol": symbol
            })

    df_new = pd.DataFrame(new_rows)

    if not old_df.empty and not df_new.empty:
        merged_df = pd.concat([old_df, df_new]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    elif not df_new.empty:
        merged_df = df_new.sort_values("timestamp").reset_index(drop=True)
    elif not old_df.empty:
        merged_df = old_df
    else:
        merged_df = pd.DataFrame()

    if not merged_df.empty:
        merged_df.to_csv(csv_path, index=False, encoding="utf-8")

    return merged_df


# ==================== メイン実行パイプライン ====================
async def run_pipeline(
    days_1d: int = 1460,    # 4年分 (設立以降)
    days_1h: int = 1460,    # 4年分 (設立以降)
    days_15m: int = 180,    # 180日分
    days_5m: int = 90,      # 90日分
    days_1m: int = 30,      # 30日分
    intervals: Optional[Sequence[str]] = None,
    force: bool = False,
    force_upload: bool = False,
    skip_upload: bool = False,
    symbols_override: Optional[List[str]] = None,
    incremental_hours: int = 8
) -> None:
    data_dir = Path(__file__).resolve().parent / "Data"
    candles_dir = data_dir / "historical_candles_binance"
    candles_dir.mkdir(parents=True, exist_ok=True)

    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    target_intervals = list(intervals) if intervals else ["1d", "1h", "15m", "5m", "1m"]

    log("=" * 70)
    log("🚀 [Binance Japan] 全銘柄マルチ時間足データ収集＆時間分割Discord配信パイプライン開始")
    log(f"   対象足種: {target_intervals} | 実行日時タグ: {now_jst}")
    log("=" * 70)

    # 1. 銘柄一覧
    if symbols_override:
        target_symbols = [s.strip().upper() for s in symbols_override]
        log(f"📌 指定銘柄 ({len(target_symbols)} 銘柄): {', '.join(target_symbols)}")
    else:
        target_symbols = fetch_binance_jpy_symbols()
        log(f"📌 Binance Japan 対象銘柄: {len(target_symbols)} 銘柄 ({', '.join(target_symbols[:8])} ...)")

    days_map = {"1d": days_1d, "1h": days_1h, "15m": days_15m, "5m": days_5m, "1m": days_1m}

    for interval in target_intervals:
        log(f"\n📂 ========== Binance Japan 【{interval.upper()}足】 収集開始 ==========")
        target_days = days_map.get(interval, 30)

        symbol_dfs: Dict[str, pd.DataFrame] = {}

        for sym_idx, sym in enumerate(target_symbols, 1):
            df = download_symbol_candles_binance(
                symbol=sym,
                interval=interval,
                days=target_days,
                candles_dir=candles_dir,
                force=force,
                incremental_hours=incremental_hours
            )
            if not df.empty:
                symbol_dfs[sym] = df

            if sym_idx % 5 == 0 or sym_idx == len(target_symbols):
                log(f"   [{sym_idx:2d}/{len(target_symbols)}] {sym} ({interval}) 完了 (保有レコード: {len(df):,} 行)")

            time.sleep(0.04)

        # 2. 全銘柄網羅グリッド生成 (欠損値 NaN 補完)
        log(f"\n🧩 [{interval.upper()}] 全銘柄包含マスターグリッド生成中...")
        master_df = build_full_symbol_time_grid(symbol_dfs, target_symbols)

        if interval == "1h":
            fixed_csv = data_dir / "binance_japan_all_symbols_merged.csv"
            master_df.to_csv(fixed_csv, index=False, encoding="utf-8")
            log(f"📄 [Binance Japan] 1H 統合マスターCSVを更新しました: {fixed_csv.name} ({len(master_df):,} 行)")

        # 3. ZIP アーカイブ生成 (8時間つけ足し時は最新1本スライス、全件時はN分割)
        if incremental_hours > 0 and not force:
            parts = create_recent_slice_zip(
                df=master_df,
                exchange="binance_japan",
                interval=interval,
                out_dir=data_dir,
                timestamp_tag=now_jst,
                hours=incremental_hours
            )
        else:
            parts = split_and_create_time_zips(
                df=master_df,
                exchange="binance_japan",
                interval=interval,
                out_dir=data_dir,
                timestamp_tag=now_jst,
                max_part_rows=400_000,
                min_parts=2
            )

        # 4. 古い順からの Discord 順次アップロード
        if not skip_upload:
            upload_time_split_zips_to_discord(
                parts=parts,
                webhook_name="real1_bitbank",
                exchange_label="Binance Japan",
                interval_label=interval,
                interval_wait_sec=3.0,
                force_upload=force_upload
            )

        del master_df
        del symbol_dfs
        gc.collect()

    # 5. クリーンアップ
    log("\n🧹 不要な古いZIPファイルの自動クリーンアップを実行中...")
    cleanup_expired_archives(data_dir=data_dir, patterns=("binance_japan_*.zip",), max_age_hours=24.0)

    log("\n🎉 [Binance Japan] 全銘柄マルチ時間足データ収集＆時間分割配信パイプラインが完了しました！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance Japan 全銘柄マルチ時間足データ収集パイプライン")
    parser.add_argument("--days-1d", type=int, default=1460, help="日足取得期間（日）")
    parser.add_argument("--days-1h", type=int, default=1460, help="1時間足取得期間（日）")
    parser.add_argument("--days-15m", type=int, default=180, help="15分足取得期間（日）")
    parser.add_argument("--days-5m", type=int, default=90, help="5分足取得期間（日）")
    parser.add_argument("--days-1m", type=int, default=30, help="1分足取得期間（日）")
    parser.add_argument("--intervals", nargs="+", default=None, help="実行する足種 (例: 1d 1h 15m 5m 1m)")
    parser.add_argument("--force", action="store_true", help="既存キャッシュを無視して全件再取得")
    parser.add_argument("--force-upload", action="store_true", help="ハッシュを無視して強制アップロード")
    parser.add_argument("--skip-upload", action="store_true", help="Discordアップロードをスキップ")
    parser.add_argument("--symbols", nargs="+", default=None, help="対象銘柄の絞り込み")

    args = parser.parse_args()

    asyncio.run(run_pipeline(
        days_1d=args.days_1d,
        days_1h=args.days_1h,
        days_15m=args.days_15m,
        days_5m=args.days_5m,
        days_1m=args.days_1m,
        intervals=args.intervals,
        force=args.force,
        force_upload=args.force_upload,
        skip_upload=args.skip_upload,
        symbols_override=args.symbols
    ))
