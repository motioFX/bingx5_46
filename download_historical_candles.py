"""Bitbank 全銘柄 1時間足データ（1年分 / 365日）遡及取得 & 日時付き統合CSV生成・Discord送信パイプライン

仕様:
1. Bitbank 全JPY現物ペア（約47銘柄）の1時間足を過去365日分遡及取得。
2. 差分キャッシュ更新: すでに取得済みのローカルCSV（Data/historical_candles/{symbol}_1h.csv）がある場合、完了済みの日付をスキップして高速化。
3. 日時付き統合CSVの生成:
   ファイル名: bitbank_all_symbols_1h_{YYYYMMDD_HHMMSS}.csv
   保存先: Data/bitbank_all_symbols_1h_{YYYYMMDD_HHMMSS}.csv
4. Discord送信:
   小分けではなく、全データが入った1個のファイル（日時付きCSV）を送信。
   ※ Discordのファイルサイズ上限（10MB/25MB）を超える場合は、安全のため同名の日時付きZIP（約4〜5MB）に自動圧縮して送信。
5. ノーマライズ比較チャート（30D / 10D / 5D）も併せて送信。
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

# 固定選定銘柄リスト (Bitbank指定11銘柄: BTC, ETH, XRP, SOL, DOGE, BNB, ARB, SUI, AVAX, RNDR/RENDER, LINK)
FIXED_SYMBOLS = [
    "btc_jpy", "eth_jpy", "xrp_jpy", "sol_jpy", "doge_jpy",
    "bnb_jpy", "arb_jpy", "sui_jpy", "avax_jpy", "render_jpy", "link_jpy"
]

# チャート期間設定 (ラベル, 時間数)
CHART_WINDOWS = [
    ("30d", 30 * 24),   # 720h
    ("10d", 10 * 24),   # 240h
    ("5d", 5 * 24),     # 120h
]


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


class send_discord:
    def __init__(self) -> None:
        self.real1_webhook = get_webhook_url("real1_bitbank")
        self.test4_webhook = get_webhook_url("test4_backtest")
        self.webhook_url = self.real1_webhook or self.test4_webhook

    def _get_target_webhooks(self) -> list[str]:
        target = self.real1_webhook or self.test4_webhook
        return [target] if target else []

    def send_message(self, content: str) -> bool:
        webhooks = self._get_target_webhooks()
        if not webhooks:
            log("[Discord] Webhookが設定されていません。送信をスキップします。")
            return False
        success = True
        for url in webhooks:
            try:
                resp = requests.post(url, json={"content": content}, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                log(f"[Discord Error] メッセージ送信失敗: {e}")
                success = False
        return success

    def send_file(self, file_path: Path, description: str = "") -> bool:
        """ファイルを Discord に送信。サイズが大きすぎる場合は ZIP 圧縮して自動フォールバック"""
        if not file_path.exists():
            log(f"[Discord Error] ファイルが見つかりません: {file_path}")
            return False
        webhooks = self._get_target_webhooks()
        if not webhooks:
            log("[Discord] Webhookが設定されていません。送信をスキップします。")
            return False

        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        target_upload_path = file_path
        mime_type = "text/csv" if target_upload_path.suffix == ".csv" else (
            "image/png" if target_upload_path.suffix == ".png" else "application/zip"
        )

        # 10MBを超えるCSVの場合、Discord制限（通常10MB〜25MB）に配慮してZIP圧縮版を用意
        created_temp_zip = None
        if target_upload_path.suffix == ".csv" and file_size_mb > 10.0:
            log(f"   ℹ️ CSVサイズが {file_size_mb:.2f} MB のため、Discord上限対策として同名ZIPを作成します...")
            zip_path = target_upload_path.with_suffix(".zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(target_upload_path, arcname=target_upload_path.name)
            zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
            log(f"   ZIP圧縮完了: {zip_path.name} ({zip_size_mb:.2f} MB)")
            target_upload_path = zip_path
            mime_type = "application/zip"
            description = f"{description}\n*(※Discordファイル容量制限対策のためZIP圧縮形式で送信しています)*"

        success = True
        for url in webhooks:
            try:
                with open(target_upload_path, "rb") as f:
                    resp = requests.post(
                        url,
                        data={"content": description},
                        files={"file": (target_upload_path.name, f, mime_type)},
                        timeout=180
                    )
                    resp.raise_for_status()
            except Exception as e:
                log(f"[Discord Error] ファイル送信失敗 ({target_upload_path.name}): {e}")
                success = False

        return success


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


async def fetch_candle_day(
    session: aiohttp.ClientSession,
    symbol: str,
    date_str: str,
    sem: asyncio.Semaphore,
    max_retries: int = 3
) -> List[List[Any]]:
    """1日分の1時間足OHLCVを取得"""
    url = f"{BITBANK_PUBLIC_URL}/{symbol}/candlestick/1hour/{date_str}"
    for attempt in range(max_retries):
        async with sem:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                        # まだ上場していない過去日などはスキップ
                        return []
            except Exception:
                await asyncio.sleep(0.5 + attempt * 0.5)
    return []


async def download_symbol_candles(
    session: aiohttp.ClientSession,
    symbol: str,
    dates: List[str],
    sem: asyncio.Semaphore
) -> pd.DataFrame:
    """指定銘柄の指定日付リスト全日の1時間足を並行取得"""
    if not dates:
        return pd.DataFrame()
    tasks = [fetch_candle_day(session, symbol, d, sem) for d in dates]
    results = await asyncio.gather(*tasks)

    all_rows = []
    for day_rows in results:
        if day_rows:
            all_rows.extend(day_rows)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=['open', 'high', 'low', 'close', 'volume', 'timestamp'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
    df = df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    return df


def get_existing_dates_for_symbol(csv_path: Path) -> Set[str]:
    """既存CSVからすでに完了している日付（UTC）のセットを取得"""
    if not csv_path.exists():
        return set()
    try:
        df = pd.read_csv(csv_path, usecols=['timestamp'])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        df['date_str'] = df['timestamp'].dt.strftime("%Y%m%d")
        counts = df['date_str'].value_counts()
        completed = set(counts[counts >= 24].index)
        completed.discard(today_str)  # 今日は再取得対象
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
    """価格を100%基準に正規化した比較チャートを生成（ファイル名に日時タグ付与）"""
    created_charts = []
    out_dir.mkdir(parents=True, exist_ok=True)

    for label, hours in CHART_WINDOWS:
        plt.figure(figsize=(12, 6))
        plotted_any = False

        for sym in target_symbols:
            if sym not in df_dict or df_dict[sym].empty:
                continue
            df = df_dict[sym].copy()
            if len(df) < 5:
                continue
            df = df.tail(hours)
            base_price = df["close"].iloc[0]
            if base_price <= 0:
                continue
            norm_series = (df["close"] / base_price - 1.0) * 100.0
            plt.plot(df["timestamp"], norm_series, label=sym.upper(), linewidth=1.5)
            plotted_any = True

        if not plotted_any:
            plt.close()
            continue

        plt.title(f"Bitbank Top Assets Normalized Return ({label.upper()})", fontsize=14, fontweight="bold")
        plt.xlabel("Date (JST)", fontsize=10)
        plt.ylabel("Return (%)", fontsize=10)
        plt.grid(True, linestyle="--", alpha=0.5)
        plt.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=9)
        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=JST))
        plt.tight_layout()

        # 日時タグ付きファイル名
        out_path = out_dir / f"{prefix}_normalized_{label}_{timestamp_tag}.png"
        plt.savefig(out_path, dpi=120)
        plt.close()
        created_charts.append(out_path)

    return created_charts


# ==================== メイン実行パイプライン ====================
async def run_pipeline(
    days: int = 365,
    top_n: int = 0,
    force: bool = False,
    force_upload: bool = False,
    skip_charts: bool = False,
    skip_upload: bool = False,
    symbols_override: Optional[List[str]] = None
) -> None:
    discord = send_discord()
    data_dir = Path(__file__).resolve().parent / "Data"
    candles_dir = data_dir / "historical_candles"
    candles_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = data_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    # 実行日時タグ（JST: YYYYMMDD_HHMMSS）
    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    log("=" * 70)
    log("🚀 [Bitbank 5.46] 全銘柄1年分（365日）1時間足データ取得＆統合CSV送信パイプライン開始")
    log(f"   期間: 過去 {days} 日間 | 実行日時タグ: {now_jst}")
    log("=" * 70)

    # 1. 取得対象銘柄の決定
    tickers = await fetch_bitbank_tickers()
    if symbols_override:
        target_symbols = [normalize_symbol(s) for s in symbols_override]
        log(f"📌 指定銘柄 ({len(target_symbols)} 銘柄): {', '.join(target_symbols)}")
    elif top_n > 0 and tickers:
        top_pairs = [t["pair"] for t in tickers[:top_n]]
        for f_sym in FIXED_SYMBOLS:
            if f_sym not in top_pairs:
                top_pairs.append(f_sym)
        target_symbols = top_pairs
        log(f"📌 出来高上位＋固定 ({len(target_symbols)} 銘柄): {', '.join(target_symbols)}")
    elif tickers:
        target_symbols = [t["pair"] for t in tickers]
        log(f"📌 Bitbank 全 {len(target_symbols)} JPY現物銘柄を対象にします")
    else:
        target_symbols = FIXED_SYMBOLS
        log(f"⚠️ ティッカー取得失敗のため固定5銘柄を使用: {', '.join(target_symbols)}")

    # 2. 日付リストの生成 (古い順)
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days)
    all_dates = []
    curr = start_date
    while curr <= today:
        all_dates.append(curr.strftime("%Y%m%d"))
        curr += timedelta(days=1)

    log(f"📅 取得対象期間: {all_dates[0]} 〜 {all_dates[-1]} ({len(all_dates)} 日分)")

    sem = asyncio.Semaphore(12)  # 同時接続数12
    total_downloaded_days = 0

    async with aiohttp.ClientSession() as session:
        all_symbol_dfs: Dict[str, pd.DataFrame] = {}

        log("\n📥 各銘柄の1時間足データを並行取得・差分更新中...")
        for sym_idx, sym in enumerate(target_symbols, 1):
            csv_path = candles_dir / f"{sym}_1h.csv"

            # 差分判定: force でない場合はすでに取得済みの日付を除外
            if not force and csv_path.exists():
                existing_dates = get_existing_dates_for_symbol(csv_path)
                dates_to_fetch = [d for d in all_dates if d not in existing_dates]
            else:
                dates_to_fetch = all_dates

            if dates_to_fetch:
                df_new = await download_symbol_candles(session, sym, dates_to_fetch, sem)
                total_downloaded_days += len(dates_to_fetch)
            else:
                df_new = pd.DataFrame()

            # 既存CSVとマージして保存
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
                all_symbol_dfs[sym] = merged_df

            if sym_idx % 5 == 0 or sym_idx == len(target_symbols):
                log(f"   [{sym_idx:2d}/{len(target_symbols)}] {sym} 完了 (取得済レコード数: {len(merged_df):,} 行)")

    log(f"\n✅ 全 {len(target_symbols)} 銘柄のローカルCSV保存が完了しました (新規DL日次ブロック: {total_downloaded_days:,} 件)")

    # 3. 日時付き統合CSVファイルの作成 (Data/bitbank_all_symbols_1h_{YYYYMMDD_HHMMSS}.csv)
    merged_rows = []
    for sym in target_symbols:
        csv_path = candles_dir / f"{sym}_1h.csv"
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path)
                df["symbol"] = sym
                merged_rows.append(df)
            except Exception:
                pass

    if not merged_rows:
        log("❌ データが存在しないため処理を終了します。")
        return

    master_df = pd.concat(merged_rows, ignore_index=True)
    # タイムスタンプ順にソート
    master_df['timestamp'] = pd.to_datetime(master_df['timestamp'])
    master_df = master_df.sort_values(by=['timestamp', 'symbol']).reset_index(drop=True)

    # ① 日時付き統合CSV（被らないように保存）
    dated_csv_name = f"bitbank_all_symbols_1h_{now_jst}.csv"
    dated_csv_path = data_dir / dated_csv_name
    master_df.to_csv(dated_csv_path, index=False, encoding="utf-8")
    dated_csv_size_mb = dated_csv_path.stat().st_size / (1024 * 1024)
    log(f"\n📄 日時付き統合CSVを保存しました: {dated_csv_name} ({len(master_df):,} 行 / {dated_csv_size_mb:.2f} MB)")

    # ② 固定名マスターCSVも最新化
    fixed_master_csv = data_dir / "historical_all_symbols_merged.csv"
    master_df.to_csv(fixed_master_csv, index=False, encoding="utf-8")
    log(f"📄 固定マスターCSVも更新しました: {fixed_master_csv.name}")

    # 4. Discord へ全データ入りの1個のファイルを送信
    if not skip_upload:
        log("\n📤 Discord へ全銘柄統合データを送信中...")
        desc = (
            f"📊 **[Bitbank 5.46 全銘柄1年分データ]**\n"
            f"• 銘柄数: `{len(target_symbols)}` 銘柄 (JPY現物全銘柄)\n"
            f"• 期間: 過去 `{days}` 日分 (〜{all_dates[-1]})\n"
            f"• 総レコード数: `{len(master_df):,}` 行 ({dated_csv_size_mb:.2f} MB)\n"
            f"• ファイル名: `{dated_csv_name}`"
        )
        send_ok = discord.send_file(dated_csv_path, description=desc)
        if send_ok:
            record_file_uploaded(dated_csv_path)
            log(f"   ✅ Discord 送信完了: {dated_csv_name}")
        else:
            log(f"   ⚠️ Discord 送信に失敗しました。")

    # 5. ノーマライズ比較チャート生成 & 送信
    if not skip_charts:
        log("\n📊 ノーマライズ比較チャートを生成中...")
        chart_symbols = list(FIXED_SYMBOLS)


        chart_dfs = {}
        for sym in chart_symbols:
            csv_path = candles_dir / f"{sym}_1h.csv"
            if csv_path.exists():
                try:
                    df = pd.read_csv(csv_path)
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    chart_dfs[sym] = df
                except Exception:
                    pass

        chart_paths = generate_normalized_charts(chart_dfs, chart_symbols, plots_dir, timestamp_tag=now_jst)
        for cp in chart_paths:
            if not skip_upload:
                desc = f"📈 **[Bitbank 主要銘柄 リターン比較]** `{cp.name}`"
                if discord.send_file(cp, description=desc):
                    record_file_uploaded(cp)
                    log(f"   ✅ チャート Discord 送信完了: {cp.name}")
            else:
                log(f"   チャート生成完了: {cp.name}")

    log("\n🎉 [Bitbank 5.46] 全銘柄1年分データ取得＆統合CSV保存・送信パイプラインが完了しました！")


def main():
    parser = argparse.ArgumentParser(description="Bitbank 全銘柄1年分1時間足データ取得＆日時付き統合CSV送信")
    parser.add_argument("--days", type=int, default=365, help="取得日数 (デフォルト: 365日)")
    parser.add_argument("--top-n", type=int, default=0, help="出来高上位取得数 (0 = 全JPY銘柄47ペア)")
    parser.add_argument("--symbols", type=str, default="", help="カンマ区切り銘柄指定 (例: btc_jpy,xrp_jpy)")
    parser.add_argument("--force", action="store_true", help="既存キャッシュを無視して全日を再取得")
    parser.add_argument("--force-upload", action="store_true", help="レジストリ判定を無視して強制Discordアップロード")
    parser.add_argument("--skip-charts", action="store_true", help="チャート生成をスキップ")
    parser.add_argument("--skip-upload", action="store_true", help="Discord送信をスキップ")
    args = parser.parse_args()

    symbols_override = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None

    asyncio.run(
        run_pipeline(
            days=args.days,
            top_n=args.top_n,
            force=args.force,
            force_upload=args.force_upload,
            skip_charts=args.skip_charts,
            skip_upload=args.skip_upload,
            symbols_override=symbols_override
        )
    )


if __name__ == "__main__":
    main()
