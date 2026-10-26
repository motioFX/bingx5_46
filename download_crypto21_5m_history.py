"""BingX クリプト主要21銘柄 5分足ヒストリカルデータ収集＆2ヶ月分蓄積パイプライン

対象銘柄 (計21銘柄):
  HYPE-USDT, SOL-USDT, DOGE-USDT, XRP-USDT, SUI-USDT, AVAX-USDT, LINK-USDT,
  APT-USDT, NEAR-USDT, ARB-USDT, UNI-USDT, OP-USDT, TIA-USDT, RENDER-USDT,
  INJ-USDT, FET-USDT, TAO-USDT, AAVE-USDT, ENA-USDT, ZEC-USDT, BTC-USDT

仕様:
1. 5分足（5m）の銘柄ヒストリカルデータを収集・差分アペンド蓄積。
2. 初回取得時はBingX API上限（約44日分、約13,000本/銘柄）を安全に全件取得。
3. 既存キャッシュがある場合は過去データを保持したまま最新差分を追記し、2ヶ月分（60日分以上）のヒストリーを永続蓄積。
4. キャッシュファイル（Data/historical_candles_bingx/{symbol}_5m.csv）は75日（約2.5ヶ月分）を上限として安全にトリミング。
5. 全21銘柄を包含する時系列マスターグリッド（欠損NaN補完）を構築。
6. マスターCSV（Data/bingx_crypto21_5m_merged.csv）を出力。
7. 時間軸2分割（Part 1 過去30日 / Part 2 直近30日）ZIPアーカイブを生成。
8. Discord（real1_bitbank / Bitbankチャンネル集約）へ古い順から順次アップロード。
9. 24時間経過した古いZIPアーカイブを自動削除。
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    upload_time_split_zips_to_discord,
    cleanup_expired_archives
)
from download_bingx_candles import (
    download_symbol_candles_bingx,
    CRYPTO_21_SYMBOLS,
    BINGX_API_MAX_DAYS
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


def log(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


async def run_crypto21_5m_pipeline(
    days: int = 60,               # デフォルト2ヶ月分（60日分）
    retention_days: int = 75,     # キャッシュ安全トリミング上限（75日分）
    force: bool = False,
    force_upload: bool = False,
    skip_upload: bool = False,
    incremental_hours: int = 8,
    webhook_name: str = "real1_bitbank"
) -> Tuple[Optional[pd.DataFrame], List[Tuple[str, Path, str, int, int]]]:
    """クリプト主要21銘柄 5分足データセット収集＆2ヶ月分蓄積パイプライン"""
    start_time = time.time()
    data_dir = BASE_DIR / "Data"
    candles_dir = data_dir / "historical_candles_bingx"
    candles_dir.mkdir(parents=True, exist_ok=True)

    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    symbols = list(CRYPTO_21_SYMBOLS)

    log("=" * 80)
    log("🚀 【BingX クリプト主要21銘柄 5分足（5m）データセット収集＆2ヶ月分蓄積パイプライン開始】")
    log(f"   対象銘柄数: {len(symbols)} 銘柄")
    log(f"   蓄積保持目標: {days} 日分 (2ヶ月分) | キャッシュ保護枠: {retention_days} 日分")
    log(f"   銘柄一覧: {', '.join(symbols)}")
    log("=" * 80)

    # 1. 各銘柄の5分足データを取得・蓄積（差分アペンド）
    symbol_dfs: Dict[str, pd.DataFrame] = {}

    for idx, sym in enumerate(symbols, 1):
        df = download_symbol_candles_bingx(
            symbol=sym,
            interval="5m",
            days=days,
            candles_dir=candles_dir,
            force=force,
            incremental_hours=incremental_hours,
            retention_days=retention_days
        )
        if not df.empty:
            symbol_dfs[sym] = df
            min_ts = df["timestamp"].min()
            max_ts = df["timestamp"].max()
            period_days = (max_ts - min_ts).total_seconds() / 86400.0
            log(f"   [{idx:2d}/{len(symbols)}] {sym:12s} 完了: {len(df):,} 行 (期間: {min_ts.strftime('%Y-%m-%d')} 〜 {max_ts.strftime('%Y-%m-%d')} / {period_days:.1f}日分蓄積)")
        else:
            log(f"   [{idx:2d}/{len(symbols)}] {sym:12s} ⚠️ データなし")

        # サーバー負荷対策のウェイト
        await asyncio.sleep(0.25)

    if not symbol_dfs:
        log("❌ データが取得できませんでした。処理を中断します。")
        return None, []

    # 2. 直近 N 日分（デフォルト60日分）のスライスを作成して全銘柄網羅グリッドを生成
    gc.collect()
    log("\n🧩 【全21銘柄包含マスターグリッド生成中】 (タイムスタンプ統合 & 欠損NaN補完)...")

    # 全体の中で直近days日分の開始タイムスタンプを計算 (tz-naive に統一)
    now_naive = pd.Timestamp.utcnow().tz_localize(None)
    grid_start_ts = now_naive - pd.Timedelta(days=days)

    filtered_dfs: Dict[str, pd.DataFrame] = {}
    for sym, df in symbol_dfs.items():
        if not df.empty:
            cdf = df.copy()
            cdf["timestamp"] = pd.to_datetime(cdf["timestamp"]).dt.tz_localize(None)
            sub_df = cdf[cdf["timestamp"] >= grid_start_ts].copy()
            filtered_dfs[sym] = sub_df if not sub_df.empty else cdf
        else:
            filtered_dfs[sym] = pd.DataFrame()

    master_df = build_full_symbol_time_grid(filtered_dfs, symbols)

    if master_df.empty:
        log("❌ マスターグリッドの生成に失敗しました。")
        return None, []

    # 3. マスターCSVの保存
    master_csv = data_dir / "bingx_crypto21_5m_merged.csv"
    master_df.to_csv(master_csv, index=False, encoding="utf-8")
    file_size_mb = master_csv.stat().st_size / (1024 * 1024)
    min_grid_ts = master_df["timestamp"].min()
    max_grid_ts = master_df["timestamp"].max()
    log(f"📄 【マスターCSV更新完了】: {master_csv.name} ({len(master_df):,} 行 / {file_size_mb:.2f} MB)")
    log(f"   マスター期間: {pd.to_datetime(min_grid_ts).strftime('%Y-%m-%d %H:%M')} 〜 {pd.to_datetime(max_grid_ts).strftime('%Y-%m-%d %H:%M')}")

    # 4. 全期間一括 1本（単一ZIPアーカイブ）生成 (容量約5MB / Discord制限25MB内に安全収容)
    p_start_str = pd.to_datetime(min_grid_ts).strftime("%Y%m%d")
    p_end_str = pd.to_datetime(max_grid_ts).strftime("%Y%m%d")
    period_label = f"{pd.to_datetime(min_grid_ts).strftime('%Y-%m-%d')} 〜 {pd.to_datetime(max_grid_ts).strftime('%Y-%m-%d')}"

    zip_filename = f"bingx_crypto21_5m_history_{p_start_str}_{p_end_str}_{now_jst}.zip"
    inner_csv_name = f"bingx_crypto21_5m_history_{p_start_str}_{p_end_str}_{now_jst}.csv"
    zip_path = data_dir / zip_filename

    log(f"📦 [BINGX_CRYPTO21 | 5m] 全期間一括ZIPアーカイブ作成中 (全 {len(master_df):,} 行 / 全 {len(symbols)} 銘柄)...")
    csv_buf = master_df.to_csv(index=False).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr(inner_csv_name, csv_buf)
    del csv_buf
    gc.collect()

    file_size_mb = zip_path.stat().st_size / (1024 * 1024)
    log(f"   ✓ 一括アーカイブ作成完了: {zip_path.name} ({len(master_df):,} 行 / {file_size_mb:.2f} MB / 期間: {period_label})")

    parts: List[Tuple[str, Path, str, int, int]] = [
        ("全期間一括 (2ヶ月分ヒストリー)", zip_path, period_label, len(master_df), len(symbols))
    ]

    # 5. Discord への一括アップロード
    if not skip_upload:
        log(f"\n📤 【Discord配信開始】 送信先 Webhook: {webhook_name} (Bitbankチャンネル集約)...")
        upload_time_split_zips_to_discord(
            parts=parts,
            webhook_name=webhook_name,
            exchange_label="BingX [🪙 クリプト主要21銘柄]",
            interval_label="5m",
            interval_wait_sec=3.0,
            force_upload=force_upload
        )
    else:
        log("⏩ [Discord] --skip-upload が指定されているため送信をスキップしました。")

    # 6. 不要な古いZIPのクリーンアップ (24時間超過分を安全削除)
    log("\n🧹 不要な古いZIPファイルの自動クリーンアップを実行中...")
    cleanup_expired_archives(data_dir=data_dir, patterns=("bingx_crypto21_*.zip",), max_age_hours=24.0)

    elapsed_sec = time.time() - start_time
    log("=" * 80)
    log(f"🎉 【BingX クリプト主要21銘柄 5分足パイプライン完了】 (所要時間: {elapsed_sec:.1f} 秒)")
    log(f"   生成ファイル: {master_csv.name}, {[p[1].name for p in parts]}")
    log("=" * 80 + "\n")

    return master_df, parts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BingX クリプト主要21銘柄 5分足データセット収集＆2ヶ月分蓄積パイプライン")
    parser.add_argument("--days", type=int, default=60, help="蓄積・対象期間（日、デフォルト60日＝2ヶ月分）")
    parser.add_argument("--retention-days", type=int, default=75, help="キャッシュ安全トリミング保持期間（日、デフォルト75日）")
    parser.add_argument("--incremental-hours", type=int, default=8, help="直近N時間の差分追記（デフォルト8時間、0で全期間チェック）")
    parser.add_argument("--force", action="store_true", help="既存キャッシュを無視してAPI上限から再取得")
    parser.add_argument("--force-upload", action="store_true", help="ハッシュを無視して強制アップロード")
    parser.add_argument("--skip-upload", action="store_true", help="Discordアップロードをスキップ")
    parser.add_argument("--webhook", type=str, default="real1_bitbank", help="送信先Webhook名 (デフォルト: real1_bitbank)")

    args = parser.parse_args()

    asyncio.run(run_crypto21_5m_pipeline(
        days=args.days,
        retention_days=args.retention_days,
        force=args.force,
        force_upload=args.force_upload,
        skip_upload=args.skip_upload,
        incremental_hours=args.incremental_hours,
        webhook_name=args.webhook
    ))
