"""BingX全銘柄 1時間足データ過去遡及取得 & 小分けDiscord自動送信パイプライン
取引高上位10銘柄 & 固定銘柄（HYPE, NEAR, ZEC, ARB, UNI）のノーマライズチャート（30D / 10D / 5D）自動生成

仕様:
1. BingX USDT無期限先物 全銘柄（約920銘柄）の1時間足を直近から過去2年分（最大730日）遡及取得。
2. Discord送信サイズ制限（12MB弱）に合わせ、約30日（またはサイズ上限）ごとに小分けZIP化。
3. 直近チャンクから順次ZIP化してDiscordへ即時送信し、手元にバックテスト用データを蓄積。
4. 取引高上位10銘柄のノーマライズチャート（30D / 10D / 5D）を生成・送信。
5. 固定選定銘柄（HYPE, NEAR, ZEC, ARB, UNI + BTC）のノーマライズチャート（30D / 10D / 5D）を生成・送信。
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

from config_loader import get_webhook_url

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

REST_API_URL = {
    "bingx": "https://open-api.bingx.com",
    "bingx_demo": "https://open-api-vst.bingx.com",
}

# 固定選定銘柄リスト
FIXED_SYMBOLS = ["HYPE-USDT", "NEAR-USDT", "ZEC-USDT", "ARB-USDT", "UNI-USDT"]

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
    sym = str(symbol).strip().upper()
    if "_" in sym:
        sym = sym.replace("_", "-")
    if sym.endswith("-USDT"):
        return sym
    if sym.endswith("USDT") and "-" not in sym:
        return f"{sym[:-4]}-USDT"
    if sym.endswith("-USDC"):
        return sym
    if sym.endswith("USDC") and "-" not in sym:
        return f"{sym[:-4]}-USDC"
    if "-" not in sym:
        return f"{sym}-USDT"
    return sym


class send_discord:
    def __init__(self) -> None:
        self.real3_webhook = get_webhook_url("real3_bngx")
        self.test4_webhook = get_webhook_url("test4_backtest")
        self.webhook_url = self.real3_webhook or self.test4_webhook

    def _get_target_webhooks(self) -> list[str]:
        target = self.real3_webhook or self.test4_webhook
        return [target] if target else []

    def send_message(self, content: str) -> bool:
        webhooks = self._get_target_webhooks()
        if not webhooks:
            log("[Discord] No webhook configured. Skipping send_message.")
            return False
        success = True
        for url in webhooks:
            try:
                resp = requests.post(url, json={"content": content}, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                log(f"[Discord Error] send_message failed: {e}")
                success = False
        return success

    def send_file(self, file_path: Path, description: str = "") -> bool:
        webhooks = self._get_target_webhooks()
        if not webhooks:
            log(f"[Discord] No webhook configured. Skipping send_file for {file_path.name}.")
            return False
        if not file_path.exists():
            log(f"[Discord Error] File not found: {file_path}")
            return False
        success = True
        for url in webhooks:
            try:
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                log(f"[Discord] Sending {file_path.name} ({file_size_mb:.2f} MB) to Discord...")
                with file_path.open("rb") as fh:
                    files = {"file": (file_path.name, fh)}
                    data = {"content": description} if description else {}
                    resp = requests.post(url, data=data, files=files, timeout=60)
                    resp.raise_for_status()
                log(f"[Discord] Successfully uploaded {file_path.name}")
            except Exception as e:
                log(f"[Discord Error] Failed to send file {file_path.name}: {e}")
                success = False
        return success


def fetch_bingx_tickers(filter_non_crypto: bool = True) -> List[Dict[str, Any]]:
    """BingXからUSDT無期限先物の全銘柄ティッカーを取得し、取引高降順でソート"""
    url = f"{REST_API_URL['bingx']}/openApi/swap/v2/quote/ticker"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    raw_tickers = data.get("data", [])
                    results = []
                    for it in raw_tickers:
                        sym = normalize_symbol(it.get("symbol", ""))
                        if not sym.endswith("-USDT"):
                            continue
                        # 非暗号資産トークン（ゴールドやインデックス等）の除外
                        if filter_non_crypto:
                            base = sym.replace("-USDT", "").upper()
                            if "NCSK" in base or "2USD" in base or "GOLD" in base:
                                continue
                        vol = float(it.get("quoteVolume") or 0.0)
                        last_px = float(it.get("lastPrice") or 0.0)
                        results.append({
                            "symbol": sym,
                            "lastPrice": last_px,
                            "quoteVolume": vol,
                            "priceChangePercent": float(it.get("priceChangePercent") or 0.0),
                        })
                    results.sort(key=lambda x: x["quoteVolume"], reverse=True)
                    log(f"Fetched {len(results)} active USDT perpetual tickers from BingX")
                    return results
            time.sleep(1.0)
        except Exception as e:
            log(f"Warning: fetch_bingx_tickers attempt {attempt+1} failed: {e}")
            time.sleep(2.0)
    return []


async def fetch_symbol_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
    sem: asyncio.Semaphore,
    limit: int = 1000
) -> Tuple[str, List[Dict[str, Any]]]:
    """1銘柄の指定期間の1時間足OHLCVを取得"""
    url = f"{REST_API_URL['bingx']}/openApi/swap/v2/quote/klines"
    params = {
        "symbol": symbol,
        "interval": "1h",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(limit),
    }
    async with sem:
        for attempt in range(5):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 0:
                            items = data.get("data", [])
                            if isinstance(items, list):
                                rows = []
                                for it in items:
                                    t = int(it.get("time") or 0)
                                    rows.append({
                                        "timestamp": t,
                                        "open": float(it.get("open") or 0.0),
                                        "high": float(it.get("high") or 0.0),
                                        "low": float(it.get("low") or 0.0),
                                        "close": float(it.get("close") or 0.0),
                                        "volume": float(it.get("volume") or 0.0),
                                        "symbol": symbol,
                                    })
                                return symbol, rows
                        elif data.get("code") == 100410:
                            # レート制限検知: 指数バックオフ待機
                            await asyncio.sleep(3.0 * (attempt + 1))
                            continue
                    await asyncio.sleep(0.08)
            except Exception:
                await asyncio.sleep(1.0 * (attempt + 1))
    return symbol, []


def generate_normalized_chart(
    symbols_data: Dict[str, pd.DataFrame],
    target_symbols: List[str],
    window_name: str,
    hours: int,
    title: str,
    out_path: Path,
    benchmark_symbol: Optional[str] = "BTC-USDT"
) -> Optional[Path]:
    """ノーマライズ比較チャート（Base=1.0、パフォーマンス順凡例）を生成"""
    plot_items = []
    
    for sym in target_symbols:
        df = symbols_data.get(sym)
        if df is None or df.empty or len(df) < 2:
            continue
        sub = df.tail(hours).copy().reset_index(drop=True)
        if len(sub) < 2:
            continue
        base_px = float(sub["close"].iloc[0])
        if base_px <= 0:
            continue
        sub["norm"] = sub["close"] / base_px
        final_norm = float(sub["norm"].iloc[-1])
        plot_items.append({
            "symbol": sym,
            "df": sub,
            "final_norm": final_norm,
            "is_benchmark": (sym == benchmark_symbol)
        })

    if not plot_items:
        log(f"Warning: No valid data to plot for {title} [{window_name}]")
        return None

    # パフォーマンス順にソート (ベンチマーク以外)
    non_bm = [item for item in plot_items if not item["is_benchmark"]]
    bm = [item for item in plot_items if item["is_benchmark"]]
    non_bm.sort(key=lambda x: x["final_norm"], reverse=True)
    sorted_items = non_bm + bm

    # プロット描画
    fig, ax = plt.subplots(figsize=(11.5, 6.0), dpi=150)
    
    # カラーマップ
    colors = plt.cm.tab10(np.linspace(0, 1, len(non_bm)))
    c_idx = 0

    for item in sorted_items:
        sym = item["symbol"]
        sub = item["df"]
        fn = item["final_norm"]
        pct = (fn - 1.0) * 100.0
        sign_str = "+" if pct >= 0 else ""

        if item["is_benchmark"]:
            label = f"Ref: {sym:<8} ({fn:.2f}x | {sign_str}{pct:.1f}%)"
            ax.plot(sub["dt_jst"], sub["norm"], label=label, color="gray", linestyle="--", linewidth=1.8, alpha=0.85, zorder=2)
        else:
            c_idx += 1
            label = f"{c_idx:>2}. {sym:<10} ({fn:.2f}x | {sign_str}{pct:.1f}%)"
            color = colors[c_idx - 1]
            ax.plot(sub["dt_jst"], sub["norm"], label=label, color=color, linewidth=2.0, zorder=3)

    ax.axhline(1.0, color="black", linestyle=":", linewidth=1.2, alpha=0.7, zorder=1)
    ax.set_title(f"{title} [{window_name}] (Normalized Base = 1.0)", fontsize=13, fontweight="bold", pad=12)
    ax.set_xlabel("Date / Time (JST)", fontsize=10)
    ax.set_ylabel("Normalized Price Ratio", fontsize=10)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0., fontsize=9)
    ax.grid(True, linestyle="--", alpha=0.35)

    # X軸の日時フォーマット調整
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=JST))
    plt.xticks(rotation=20)
    plt.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    log(f"Saved normalized chart: {out_path.name}")
    return out_path


async def run_pipeline(
    total_days: int = 730,
    chunk_days: int = 30,
    test_mode: bool = False,
    skip_download: bool = False,
    skip_charts: bool = False,
    no_discord: bool = False
) -> None:
    """BingX全銘柄 過去データ遡及取得 & 小分けDiscord送信 & ノーマライズチャート生成 メインパイプライン"""
    script_dir = Path(__file__).resolve().parent
    data_dir = script_dir / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = data_dir / "historical_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    candles_dir = data_dir / "historical_candles"
    candles_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = data_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    discord = send_discord() if not no_discord else None

    # 1. 銘柄一覧の取得
    log("=================================================================")
    log(" Starting BingX All-Symbols Historical Pipeline")
    log("=================================================================")
    tickers = fetch_bingx_tickers(filter_non_crypto=True)
    if not tickers:
        log("Error: Failed to fetch tickers from BingX API. Exiting.")
        return

    all_symbols = [t["symbol"] for t in tickers]
    # 固定銘柄がリストに含まれることを保証
    for fs in FIXED_SYMBOLS + ["BTC-USDT"]:
        if fs not in all_symbols:
            all_symbols.append(fs)

    # 取引高上位10銘柄の特定 (BTCを含む上位10、またはアルト上位10+BTC)
    top10_volume_symbols = [t["symbol"] for t in tickers[:10]]
    if "BTC-USDT" not in top10_volume_symbols:
        top10_volume_symbols.insert(0, "BTC-USDT")
        top10_volume_symbols = top10_volume_symbols[:10]

    log(f"Target Universe: {len(all_symbols)} symbols")
    log(f"Top 10 Volume Symbols: {top10_volume_symbols}")
    log(f"Fixed Symbols: {FIXED_SYMBOLS}")

    # テストモード設定
    if test_mode:
        log(">>> RUNNING IN TEST MODE (First 20 symbols, 5 days only) <<<")
        all_symbols = (top10_volume_symbols + FIXED_SYMBOLS + all_symbols[:15])[:20]
        # 重複削除
        all_symbols = list(dict.fromkeys(all_symbols))
        total_days = 5
        chunk_days = 5

    # 2. 直近から過去へのチャンク分割
    now_utc = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    chunks: List[Tuple[datetime, datetime]] = []
    curr_end = now_utc

    while True:
        curr_start = curr_end - timedelta(days=chunk_days)
        chunks.append((curr_start, curr_end))
        curr_end = curr_start
        total_covered = (now_utc - curr_start).days
        if total_covered >= total_days:
            break

    chunks.reverse()  # 過去（1年前）から直近へ古い順にソート（直近データが最後に届くよう制御）
    log(f"Total Chunks to fetch: {len(chunks)} (Chunk size: ~{chunk_days} days, Target: {total_days} days, Order: OLDEST FIRST)")

    # 銘柄別データのローカルメモリ保持用（直近分はチャート生成に利用）
    latest_symbols_df: Dict[str, pd.DataFrame] = {}

    # 主要個別銘柄（個別CSVとしてZIPに含める対象）
    key_individual_symbols = set(top10_volume_symbols + FIXED_SYMBOLS + ["BTC-USDT"])

    # 3. チャンクごとの取得 & ZIP化 & Discord送信ループ (古い順から順次実行)
    if not skip_download:
        sem = asyncio.Semaphore(8)  # 同時リクエスト数 (安全マージン)
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(limit=20)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks, 1):
                start_ms = int(chunk_start.timestamp() * 1000)
                end_ms = int(chunk_end.timestamp() * 1000)
                s_tag = chunk_start.astimezone(JST).strftime("%Y%m%d")
                e_tag = chunk_end.astimezone(JST).strftime("%Y%m%d")
                chunk_label = f"Chunk {chunk_idx}/{len(chunks)} [{s_tag} -> {e_tag} JST]"

                log(f"\n--- Fetching {chunk_label} for {len(all_symbols)} symbols ---")
                t0 = time.time()

                tasks = [
                    fetch_symbol_klines(session, sym, start_ms, end_ms, sem, limit=chunk_days * 24 + 10)
                    for sym in all_symbols
                ]
                results = await asyncio.gather(*tasks)
                t1 = time.time()

                # データ集約
                valid_count = 0
                all_chunk_rows = []
                ind_dfs: Dict[str, pd.DataFrame] = {}

                for sym, rows in results:
                    if not rows:
                        continue
                    valid_count += 1
                    df_sym = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                    df_sym["dt_jst"] = pd.to_datetime(df_sym["timestamp"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(JST)
                    ind_dfs[sym] = df_sym
                    all_chunk_rows.extend(rows)

                    # 最終チャンク（直近分）のデータをチャート用・選定用に保持
                    if chunk_idx == len(chunks):
                        latest_symbols_df[sym] = df_sym

                    # ローカル累積CSVに追記保存
                    sym_csv = candles_dir / f"{sym}_1h.csv"
                    if sym_csv.exists():
                        try:
                            old_df = pd.read_csv(sym_csv)
                            comb_df = pd.concat([old_df, df_sym[["timestamp", "open", "high", "low", "close", "volume", "symbol"]]], ignore_index=True)
                            comb_df = comb_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                            comb_df.to_csv(sym_csv, index=False, encoding="utf-8")
                        except Exception:
                            df_sym[["timestamp", "open", "high", "low", "close", "volume", "symbol"]].to_csv(sym_csv, index=False, encoding="utf-8")
                    else:
                        df_sym[["timestamp", "open", "high", "low", "close", "volume", "symbol"]].to_csv(sym_csv, index=False, encoding="utf-8")

                log(f"Fetched {valid_count}/{len(all_symbols)} symbols with valid data in {t1 - t0:.1f}s")

                if not all_chunk_rows:
                    log(f"No historical data returned for {chunk_label}.")
                    continue

                # ZIPアーカイブ作成 (全銘柄統合CSV + 主要銘柄個別CSV -> 8~10MBに最適化)
                zip_filename = f"bingx_1h_all_symbols_{s_tag}_{e_tag}.zip"
                zip_path = chunks_dir / zip_filename

                log(f"Creating ZIP archive: {zip_filename}...")
                df_chunk_all = pd.DataFrame(all_chunk_rows).drop_duplicates(subset=["timestamp", "symbol"]).sort_values(by=["timestamp", "symbol"]).reset_index(drop=True)

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    # 1. 全銘柄統合CSV (全銘柄590+網羅)
                    merged_csv_str = df_chunk_all.to_csv(index=False)
                    zf.writestr(f"all_symbols_1h_{s_tag}_{e_tag}.csv", merged_csv_str)
                    
                    # 2. 主要個別銘柄CSV（検証用）
                    for sym in key_individual_symbols:
                        df_s = ind_dfs.get(sym)
                        if df_s is not None and not df_s.empty:
                            s_csv = df_s[["timestamp", "open", "high", "low", "close", "volume", "symbol"]].to_csv(index=False)
                            zf.writestr(f"individual/{sym}_1h.csv", s_csv)

                zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
                log(f"ZIP Archive created: {zip_filename} ({zip_size_mb:.2f} MB)")

                # もし個別CSV込みで12MBを超える場合は、Discord送信制限安全策として統合CSVのみのZIPを代替作成
                send_target_zip = zip_path
                if zip_size_mb > 13.0:
                    log(f"Warning: ZIP size ({zip_size_mb:.2f} MB) exceeds safe limit (~12MB). Creating compact ZIP with master CSV only...")
                    compact_zip_path = chunks_dir / f"bingx_1h_master_{s_tag}_{e_tag}.zip"
                    with zipfile.ZipFile(compact_zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                        zf.writestr(f"all_symbols_1h_{s_tag}_{e_tag}.csv", merged_csv_str)
                    send_target_zip = compact_zip_path
                    log(f"Compact ZIP created: {compact_zip_path.name} ({compact_zip_path.stat().st_size / (1024*1024):.2f} MB)")

                # Discordへ即座に送信
                if discord:
                    desc = (
                        f"📦 **【BingX 1時間足 全銘柄データ (小分け {chunk_idx}/{len(chunks)})】**\n"
                        f"• 期間: `{s_tag}` ～ `{e_tag}` ({chunk_days}日間)\n"
                        f"• 取得銘柄数: `{valid_count}` / `{len(all_symbols)}` 銘柄\n"
                        f"• 総レコード数: `{len(df_chunk_all):,}` 行\n"
                        f"• ファイルサイズ: `{send_target_zip.stat().st_size / (1024*1024):.2f} MB`"
                    )
                    discord.send_file(send_target_zip, desc)

                # API負荷軽減の短い待機
                await asyncio.sleep(4.0)
    else:
        log("Skip download flag is set. Loading existing candles from local cache for charts...")
        for sym in top10_volume_symbols + FIXED_SYMBOLS:
            f = candles_dir / f"{sym}_1h.csv"
            if f.exists():
                try:
                    d = pd.read_csv(f)
                    d["dt_jst"] = pd.to_datetime(d["timestamp"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(JST)
                    latest_symbols_df[sym] = d
                except Exception:
                    pass

    # 4. ノーマライズチャート生成 & Discord送信
    if not skip_charts and latest_symbols_df:
        log("\n=================================================================")
        log(" Generating Normalized Charts (Top 10 Volume & Fixed Symbols)")
        log("=================================================================")

        # ① 取引高上位 10 銘柄のノーマライズチャート (30D, 10D, 5D)
        log("\n[Chart 1] Generating Top 10 Volume Normalized Charts (30D, 10D, 5D)...")
        for win_label, win_hours in CHART_WINDOWS:
            chart_file = charts_dir / f"normalized_top10_volume_{win_label}.png"
            res = generate_normalized_chart(
                symbols_data=latest_symbols_df,
                target_symbols=top10_volume_symbols,
                window_name=win_label,
                hours=win_hours,
                title=f"BingX Top 10 Volume Normalized Performance",
                out_path=chart_file,
                benchmark_symbol="BTC-USDT"
            )
            if res and discord:
                discord.send_file(res, f"📊 **【BingX 取引高上位10銘柄 ノーマライズチャート [{win_label.upper()}]】**")

        # ② 銘柄選定固定のノーマライズチャート (HYPE, NEAR, ZEC, ARB, UNI + BTC)
        log("\n[Chart 2] Generating Fixed Selection Normalized Charts (30D, 10D, 5D)...")
        fixed_target_list = list(FIXED_SYMBOLS)
        if "BTC-USDT" not in fixed_target_list:
            fixed_target_list.append("BTC-USDT")

        for win_label, win_hours in CHART_WINDOWS:
            chart_file = charts_dir / f"normalized_fixed_symbols_{win_label}.png"
            res = generate_normalized_chart(
                symbols_data=latest_symbols_df,
                target_symbols=fixed_target_list,
                window_name=win_label,
                hours=win_hours,
                title=f"BingX Fixed Selection (HYPE, NEAR, ZEC, ARB, UNI) Normalized",
                out_path=chart_file,
                benchmark_symbol="BTC-USDT"
            )
            if res and discord:
                discord.send_file(res, f"🎯 **【固定選定銘柄 (HYPE, NEAR, ZEC, ARB, UNI) ノーマライズチャート [{win_label.upper()}]】**")

    log("\n=================================================================")
    log(" Historical Pipeline Completed Successfully!")
    log("=================================================================")


def main():
    parser = argparse.ArgumentParser(description="BingX All-Symbols 1H Historical Download & Normalized Charts")
    parser.add_argument("--days", type=int, default=730, help="Total days to fetch backward (default: 730 = 2 years)")
    parser.add_argument("--chunk-days", type=int, default=30, help="Days per chunk/zip (default: 30 days)")
    parser.add_argument("--test", action="store_true", help="Run test mode (20 symbols, 5 days)")
    parser.add_argument("--skip-download", action="store_true", help="Skip downloading data, only generate charts from local cache")
    parser.add_argument("--skip-charts", action="store_true", help="Skip generating charts, only download data")
    parser.add_argument("--no-discord", action="store_true", help="Do not send files/messages to Discord")
    args = parser.parse_args()

    asyncio.run(run_pipeline(
        total_days=args.days,
        chunk_days=args.chunk_days,
        test_mode=args.test,
        skip_download=args.skip_download,
        skip_charts=args.skip_charts,
        no_discord=args.no_discord
    ))


if __name__ == "__main__":
    main()
