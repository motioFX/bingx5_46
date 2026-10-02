"""Bitbank 全銘柄 マルチ時間足（1d, 1h, 15m, 5m, 1m）データ収集＆時間分割Discord配信パイプライン

仕様:
1. Bitbank 全JPY現物ペア（約47銘柄）を対象。
2. 5つの時間足に対応:
   - 1d (日足): 過去4年分 (年別APIで超高速取得)
   - 1h (1時間足): 過去4年分 (日別API、差分キャッシュ更新)
   - 15m (15分足): 直近180日分 (日別API、差分キャッシュ更新)
   - 5m (5分足): 直近90日分 (日別API、差分キャッシュ更新)
   - 1m (1分足): 直近30日分 (日別API、差分キャッシュ更新)
3. 全銘柄網羅グリッド生成:
   - 銘柄ごとではなく、全銘柄を包含した時系列テーブルを作成。
   - 上場前などでデータが存在しない過去期間は NaN で補完。
4. 時間軸でのN分割ZIPアーカイブ生成:
   - Discord制限（25MB）内に安全に収まるよう動的サイズ判定。
5. 古い順からのDiscord順次送信:
   - 必ず Part 1（最古データ）から順次アップロードし、最後に最新データを送信。
   - 各送信間にセーフティウェイトを挿入。
6. ノーマライズ比較チャート（30d / 10d / 5d）送信（1h足データから生成、既存互換）。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import shutil
import sys
import time
import zipfile
import gc
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import requests

from config_loader import get_webhook_url, BITBANK_PUBLIC_URL
from upload_registry import should_upload_file, record_file_uploaded
from data_pipeline_utils import (
    build_full_symbol_time_grid,
    split_and_create_time_zips,
    upload_time_split_zips_to_discord,
    cleanup_expired_archives
)

# 標準出力のUTF-8設定
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

# 固定選定銘柄リスト (Bitbank指定11銘柄)
FIXED_SYMBOLS = [
    "btc_jpy", "eth_jpy", "xrp_jpy", "sol_jpy", "doge_jpy",
    "bnb_jpy", "arb_jpy", "sui_jpy", "avax_jpy", "render_jpy", "link_jpy"
]

# ノーマライズ比較チャート対象銘柄 (Bitbank主要11銘柄 ＋ Binance Japan保有のNEAR)
CHART_SYMBOLS = [
    "btc_jpy", "eth_jpy", "xrp_jpy", "sol_jpy", "doge_jpy",
    "bnb_jpy", "arb_jpy", "sui_jpy", "avax_jpy", "render_jpy", "link_jpy",
    "near_jpy"
]

# チャート期間設定 (ラベル, 時間数)
CHART_WINDOWS = [
    ("30d", 30 * 24),   # 720h
    ("10d", 10 * 24),   # 240h
    ("5d", 5 * 24),     # 120h
]

# Bitbank API の時間足マッピング
BITBANK_INTERVAL_MAP = {
    "1d": "1day",
    "1h": "1hour",
    "15m": "15min",
    "5m": "5min",
    "1m": "1min",
}


def log(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def normalize_symbol(symbol: str) -> str:
    sym = str(symbol).strip().lower()
    if "-" in sym:
        sym = sym.replace("-", "_")
    if sym.endswith("_usdt"):
        sym = sym.replace("_usdt", "_jpy")
    elif sym.endswith("usdt"):
        sym = f"{sym[:-4]}_jpy"
    elif "_" not in sym:
        sym = f"{sym}_jpy"
    if sym in ("rndr_jpy", "rndr"):
        sym = "render_jpy"
    return sym


# ==================== Bitbank API 通信 ====================
async def fetch_bitbank_tickers() -> List[Dict[str, Any]]:
    """Bitbank の全ティッカーを取得し、JPYペアを出来高（vol）順にソート"""
    url = f"{BITBANK_PUBLIC_URL}/tickers"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") == 1:
                        tickers = data.get("data", [])
                        jpy_tickers = [t for t in tickers if t.get("pair", "").endswith("_jpy")]
                        jpy_tickers.sort(key=lambda x: float(x.get("vol", 0.0)), reverse=True)
                        return jpy_tickers
    except Exception as e:
        log(f"[Bitbank API Error] fetch_bitbank_tickers failed: {e}")
    return []


async def fetch_candle_block(
    session: aiohttp.ClientSession,
    symbol: str,
    candle_type: str,
    date_or_year: str,
    sem: asyncio.Semaphore,
    max_retries: int = 3
) -> List[List[Any]]:
    """1ブロック分（1dayは年、それ以外は日付）のOHLCVを取得"""
    url = f"{BITBANK_PUBLIC_URL}/{symbol}/candlestick/{candle_type}/{date_or_year}"
    for attempt in range(max_retries):
        async with sem:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success") == 1 and "data" in data:
                            candlestick = data["data"].get("candlestick", [])
                            if candlestick and "ohlcv" in candlestick[0]:
                                return candlestick[0]["ohlcv"]
                    elif resp.status == 429:
                        await asyncio.sleep(1.0 + attempt * 1.5)
                        continue
                    elif resp.status == 404:
                        return []
            except Exception:
                await asyncio.sleep(0.5 + attempt * 0.5)
    return []


async def download_symbol_candles_by_interval(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    blocks: List[str],
    sem: asyncio.Semaphore
) -> pd.DataFrame:
    """指定銘柄・時間足の指定ブロック（日または年）リスト全件を並行取得"""
    if not blocks:
        return pd.DataFrame()

    candle_type = BITBANK_INTERVAL_MAP.get(interval, "1hour")
    tasks = [fetch_candle_block(session, symbol, candle_type, b, sem) for b in blocks]
    results = await asyncio.gather(*tasks)

    all_rows = []
    for block_rows in results:
        if block_rows:
            all_rows.extend(block_rows)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=['open', 'high', 'low', 'close', 'volume', 'timestamp'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
    df = df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    return df


def get_existing_dates_for_symbol(csv_path: Path, interval: str) -> Set[str]:
    """既存CSVからすでに完了しているブロック（日付または年）のセットを取得"""
    if not csv_path.exists():
        return set()
    try:
        df = pd.read_csv(csv_path, usecols=['timestamp'])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        today_utc = datetime.now(timezone.utc)

        if interval == "1d":
            curr_year = today_utc.strftime("%Y")
            df['year_str'] = df['timestamp'].dt.strftime("%Y")
            counts = df['year_str'].value_counts()
            completed = set(counts[counts >= 350].index)
            completed.discard(curr_year)
            return completed
        else:
            today_str = today_utc.strftime("%Y%m%d")
            df['date_str'] = df['timestamp'].dt.strftime("%Y%m%d")
            counts = df['date_str'].value_counts()
            min_count = {"1h": 24, "15m": 96, "5m": 288, "1m": 1400}.get(interval, 24)
            completed = set(counts[counts >= min_count].index)
            completed.discard(today_str)
            return completed
    except Exception:
        return set()


# ==================== ノーマライズチャート生成 ====================
def generate_normalized_charts(
    df_dict: Dict[str, pd.DataFrame],
    target_symbols: List[str],
    out_dir: Path,
    timestamp_tag: str,
    prefix: str = "bitbank"
) -> List[Path]:
    """主要銘柄の正規化比較チャート（30d / 10d / 5d）を生成"""
    created_charts = []
    out_dir.mkdir(parents=True, exist_ok=True)

    for label, hours in CHART_WINDOWS:
        plt.figure(figsize=(12, 6))
        plotted_any = False

        for sym in target_symbols:
            df = df_dict.get(sym)
            if df is None or df.empty:
                continue

            sub_df = df.tail(hours).copy()
            if len(sub_df) < max(5, hours // 4):
                continue

            base_px = sub_df["close"].iloc[0]
            if base_px <= 0 or np.isnan(base_px):
                continue

            norm_series = (sub_df["close"] / base_px) * 100.0
            last_val = norm_series.iloc[-1]
            diff_pct = last_val - 100.0
            sign = "+" if diff_pct >= 0 else ""
            sym_label = f"{sym.replace('_jpy', '').upper()} ({sign}{diff_pct:.1f}%)"

            plt.plot(sub_df["timestamp"], norm_series, label=sym_label, linewidth=1.5)
            plotted_any = True

        if plotted_any:
            plt.axhline(100.0, color="gray", linestyle="--", alpha=0.6, linewidth=1.0)
            plt.title(f"Bitbank Normalized Performance [{label.upper()}] (Base = 100%)", fontsize=13, fontweight="bold")
            plt.xlabel("Date (UTC)", fontsize=10)
            plt.ylabel("Normalized Price (%)", fontsize=10)
            plt.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=9)
            plt.grid(True, linestyle=":", alpha=0.5)
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            plt.gcf().autofmt_xdate()
            plt.tight_layout()

            chart_path = out_dir / f"{prefix}_normalized_{label}_{timestamp_tag}.png"
            plt.savefig(chart_path, dpi=120)
            plt.close()
            created_charts.append(chart_path)
        else:
            plt.close()

    return created_charts


async def create_and_send_normalized_charts(skip_upload: bool = False, timestamp_tag: Optional[str] = None) -> List[Path]:
    """主要銘柄 (Bitbank 11銘柄 ＋ Binance NEAR) のノーマライズ比較チャートを生成・Discord送信"""
    data_dir = Path(__file__).resolve().parent / "Data"
    candles_dir = data_dir / "historical_candles"
    candles_binance_dir = data_dir / "historical_candles_binance"
    plots_dir = data_dir / "plots"
    now_tag = timestamp_tag or datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    log("\n📊 ノーマライズ比較チャートを生成中 (Bitbank 11銘柄 ＋ Binance NEAR)...")
    chart_symbols = list(CHART_SYMBOLS)

    all_dfs: Dict[str, pd.DataFrame] = {}
    for sym in chart_symbols:
        p = candles_dir / f"{sym}_1h.csv"
        if not p.exists() and sym == "near_jpy":
            p = candles_binance_dir / "NEARJPY_1h.csv"
        if p.exists():
            try:
                df = pd.read_csv(p)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                all_dfs[sym] = df
            except Exception:
                pass

    charts = generate_normalized_charts(
        df_dict=all_dfs,
        target_symbols=chart_symbols,
        out_dir=plots_dir,
        timestamp_tag=now_tag,
        prefix="bitbank"
    )

    if not skip_upload and charts:
        webhook_url = get_webhook_url("real1_bitbank")
        for cp in charts:
            try:
                with open(cp, "rb") as f:
                    requests.post(
                        webhook_url,
                        data={"content": f"📊 **[Bitbank ノーマライズ比較チャート]** `{cp.name}`"},
                        files={"file": (cp.name, f, "image/png")},
                        timeout=30
                    )
                time.sleep(2.0)
            except Exception as e:
                log(f"   ⚠️ チャート送信失敗 ({cp.name}): {e}")

    return charts


# ==================== メイン実行パイプライン ====================
async def run_pipeline(
    days_1d: int = 1460,    # 4年分
    days_1h: int = 1460,    # 4年分
    days_15m: int = 180,    # 180日分
    days_5m: int = 90,      # 90日分
    days_1m: int = 30,      # 30日分
    intervals: Optional[Sequence[str]] = None,
    force: bool = False,
    force_upload: bool = False,
    skip_charts: bool = False,
    skip_upload: bool = False,
    symbols_override: Optional[List[str]] = None
) -> None:
    data_dir = Path(__file__).resolve().parent / "Data"
    candles_dir = data_dir / "historical_candles"
    candles_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = data_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    target_intervals = list(intervals) if intervals else ["1d", "1h", "15m", "5m", "1m"]

    log("=" * 70)
    log("🚀 [Bitbank] 全銘柄マルチ時間足データ収集＆時間分割Discord配信パイプライン開始")
    log(f"   対象足種: {target_intervals} | 実行日時タグ: {now_jst}")
    log("=" * 70)

    # 1. 取得対象銘柄の決定
    tickers = await fetch_bitbank_tickers()
    if symbols_override:
        target_symbols = [normalize_symbol(s) for s in symbols_override]
        log(f"📌 指定銘柄 ({len(target_symbols)} 銘柄): {', '.join(target_symbols)}")
    elif tickers:
        target_symbols = [t["pair"] for t in tickers]
        log(f"📌 Bitbank 全 {len(target_symbols)} JPY現物銘柄を対象にします")
    else:
        target_symbols = FIXED_SYMBOLS
        log(f"⚠️ ティッカー取得失敗のため固定11銘柄を使用: {', '.join(target_symbols)}")

    today_utc = datetime.now(timezone.utc).date()
    current_year = today_utc.year
    sem = asyncio.Semaphore(10)  # CPU/API保護のため同時接続数10

    # 1h足の最新データを保持しておく辞書（ノーマライズチャート用）
    cached_1h_dfs: Dict[str, pd.DataFrame] = {}

    async with aiohttp.ClientSession() as session:
        for interval in target_intervals:
            log(f"\n📂 ========== Bitbank 【{interval.upper()}足】 収集開始 ==========")

            # 日数設定
            days_map = {"1d": days_1d, "1h": days_1h, "15m": days_15m, "5m": days_5m, "1m": days_1m}
            target_days = days_map.get(interval, 30)

            # ブロック生成
            if interval == "1d":
                blocks = [str(y) for y in range(current_year - 4, current_year + 1)]
            else:
                start_d = today_utc - timedelta(days=target_days)
                blocks = []
                cur = start_d
                while cur <= today_utc:
                    blocks.append(cur.strftime("%Y%m%d"))
                    cur += timedelta(days=1)

            symbol_dfs: Dict[str, pd.DataFrame] = {}

            for sym_idx, sym in enumerate(target_symbols, 1):
                csv_path = candles_dir / f"{sym}_{interval}.csv"

                if not force and csv_path.exists():
                    existing_blocks = get_existing_dates_for_symbol(csv_path, interval)
                    blocks_to_fetch = [b for b in blocks if b not in existing_blocks]
                else:
                    blocks_to_fetch = blocks

                if blocks_to_fetch:
                    df_new = await download_symbol_candles_by_interval(session, sym, interval, blocks_to_fetch, sem)
                else:
                    df_new = pd.DataFrame()

                # 差分キャッシュ更新
                if csv_path.exists():
                    try:
                        old_df = pd.read_csv(csv_path)
                        old_df['timestamp'] = pd.to_datetime(old_df['timestamp'])
                        if not df_new.empty:
                            merged_df = pd.concat([old_df, df_new]).drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
                            merged_df.to_csv(csv_path, index=False, encoding="utf-8")
                        else:
                            merged_df = old_df
                    except Exception:
                        if not df_new.empty:
                            df_new.to_csv(csv_path, index=False, encoding="utf-8")
                            merged_df = df_new
                        else:
                            merged_df = pd.DataFrame()
                else:
                    if not df_new.empty:
                        df_new.to_csv(csv_path, index=False, encoding="utf-8")
                        merged_df = df_new
                    else:
                        merged_df = pd.DataFrame()

                if not merged_df.empty:
                    merged_df['timestamp'] = pd.to_datetime(merged_df['timestamp'])
                    symbol_dfs[sym] = merged_df
                    if interval == "1h":
                        cached_1h_dfs[sym] = merged_df

                if sym_idx % 10 == 0 or sym_idx == len(target_symbols):
                    log(f"   [{sym_idx:2d}/{len(target_symbols)}] {sym} ({interval}) 完了 (保有レコード: {len(merged_df):,} 行)")

                # API負荷抑制
                await asyncio.sleep(0.05)

            # 2. 全銘柄網羅グリッド生成 (欠損値 NaN 補完)
            log(f"\n🧩 [{interval.upper()}] 全銘柄包含マスターグリッド生成中...")
            master_df = build_full_symbol_time_grid(symbol_dfs, target_symbols)

            if interval == "1h":
                # 互換用マスターCSVの更新
                fixed_csv = data_dir / "bitbank_all_symbols_merged.csv"
                master_df.to_csv(fixed_csv, index=False, encoding="utf-8")
                legacy_csv = data_dir / "historical_all_symbols_merged.csv"
                master_df.to_csv(legacy_csv, index=False, encoding="utf-8")
                log(f"📄 [Bitbank] 1H 統合マスターCSVを更新しました: {fixed_csv.name} ({len(master_df):,} 行)")

            # 3. 時間軸での N 分割 ZIP アーカイブ生成 (Part 1 最古 〜 Part N 最新)
            parts = split_and_create_time_zips(
                df=master_df,
                exchange="bitbank",
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
                    exchange_label="Bitbank",
                    interval_label=interval,
                    interval_wait_sec=3.0,
                    force_upload=force_upload
                )

            del master_df
            del symbol_dfs
            gc.collect()

    # 5. ノーマライズ比較チャート (1H足ベース、既存互換)
    if not skip_charts:
        await create_and_send_normalized_charts(skip_upload=skip_upload, timestamp_tag=now_jst)

    # 6. 古いZIPアーカイブのクリーンアップ (24時間経過分削除)
    log("\n🧹 不要な古いZIPファイルおよびチャート画像の自動クリーンアップを実行中...")
    cleanup_expired_archives(data_dir=data_dir, patterns=("bitbank_*.zip",), max_age_hours=24.0)

    log("\n🎉 [Bitbank] 全銘柄マルチ時間足データ収集＆時間分割配信パイプラインが完了しました！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bitbank 全銘柄マルチ時間足データ収集パイプライン")
    parser.add_argument("--days-1d", type=int, default=1460, help="日足取得期間（日）")
    parser.add_argument("--days-1h", type=int, default=1460, help="1時間足取得期間（日）")
    parser.add_argument("--days-15m", type=int, default=180, help="15分足取得期間（日）")
    parser.add_argument("--days-5m", type=int, default=90, help="5分足取得期間（日）")
    parser.add_argument("--days-1m", type=int, default=30, help="1分足取得期間（日）")
    parser.add_argument("--intervals", nargs="+", default=None, help="実行する足種 (例: 1d 1h 15m 5m 1m)")
    parser.add_argument("--force", action="store_true", help="既存キャッシュを無視して全件再取得")
    parser.add_argument("--force-upload", action="store_true", help="ハッシュを無視して強制アップロード")
    parser.add_argument("--skip-charts", action="store_true", help="ノーマライズチャート生成をスキップ")
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
        skip_charts=args.skip_charts,
        skip_upload=args.skip_upload,
        symbols_override=args.symbols
    ))
