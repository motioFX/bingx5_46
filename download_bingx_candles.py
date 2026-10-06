"""BingX 全銘柄（FX除くクリプト・株・コモディティ）マルチ時間足データ収集＆時間分割Discord配信パイプライン

仕様:
1. BingX USDT無期限先物・スワップのFX（NCFX / FOREX）を除く全銘柄（約1,100+銘柄）を対象。
2. 5つの時間足に対応:
   - 1d (日足): 過去4年分 (差分キャッシュ更新)
   - 1h (1時間足): 過去4年分 (差分キャッシュ更新)
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

BINGX_API_URL = "https://open-api.bingx.com"


def log(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


CATEGORY_METADATA = {
    "crypto": {
        "label": "🪙 クリプト",
        "file_tag": "crypto",
    },
    "indices_commodities": {
        "label": "🥇 指数＆コモディティ",
        "file_tag": "indices_commodities",
    },
    "forex": {
        "label": "💱 FX",
        "file_tag": "forex",
    },
    "stocks": {
        "label": "📈 株式",
        "file_tag": "stocks",
    },
}


def get_symbol_category(symbol: str) -> str:
    """銘柄コードからカテゴリーを判定 (crypto, indices_commodities, forex, stocks)"""
    sym = symbol.strip().upper()
    if sym.startswith("NCFX") or "FOREX" in sym:
        return "forex"
    elif sym.startswith("NCSK"):
        return "stocks"
    elif sym.startswith("NCCO") or sym.startswith("NCSI") or sym.startswith("NCID") or sym.startswith("NCIN"):
        return "indices_commodities"
    else:
        return "crypto"


def fetch_bingx_symbols_by_category() -> Dict[str, List[str]]:
    """BingX の全1,200+銘柄を4大カテゴリー別に分類して取得"""
    url = f"{BINGX_API_URL}/openApi/swap/v2/quote/contracts"
    categorized: Dict[str, List[str]] = {
        "crypto": [],
        "indices_commodities": [],
        "forex": [],
        "stocks": [],
    }
    try:
        resp = requests.get(url, timeout=12).json()
        contracts = resp.get("data", [])
        for c in contracts:
            sym = c.get("symbol", "")
            if not sym:
                continue
            cat = get_symbol_category(sym)
            if cat in categorized:
                categorized[cat].append(sym)
        for cat in categorized:
            categorized[cat].sort()
        return categorized
    except Exception as e:
        log(f"[BingX API Error] 銘柄一覧取得失敗: {e}")

    return {
        "crypto": [
            "BTC-USDT", "ETH-USDT", "SOL-USDT", "NEAR-USDT", "ARB-USDT",
            "DOGE-USDT", "BNB-USDT", "XRP-USDT", "SUI-USDT", "AVAX-USDT", "LINK-USDT"
        ],
        "indices_commodities": ["NCCOGOLD2USD-USDT", "NCSINASDAQ1002USD-USDT", "NCSISP5002USD-USDT"],
        "forex": ["NCFXUSD2JPY-USDT", "NCFXEUR2USD-USDT"],
        "stocks": ["NCSKTSLA2USD-USDT", "NCSKNVDA2USD-USDT", "NCSKMSFT2USD-USDT"],
    }


def fetch_symbol_klines_bingx(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000
) -> List[Dict[str, Any]]:
    """BingX の klines を安全に取得（無限ループ防止ガード付き）"""
    url = f"{BINGX_API_URL}/openApi/swap/v2/quote/klines"
    all_data = []
    curr_start = start_ms

    interval_ms_map = {
        "1m": 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    int_ms = interval_ms_map.get(interval, 60 * 1000)

    while curr_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": str(curr_start),
            "endTime": str(end_ms),
            "limit": str(limit),
        }
        res_items = None
        for attempt in range(4):
            try:
                resp = requests.get(url, params=params, timeout=12).json()
                code = resp.get("code")
                if code == 0:
                    data = resp.get("data", [])
                    if isinstance(data, list):
                        res_items = data
                        break
                elif code == 100410:
                    time.sleep(3.0 * (attempt + 1))
                    continue
                else:
                    # 無効シンボルや上場前等の通常エラーはリトライせずスキップ
                    break
            except Exception:
                time.sleep(1.0 * (attempt + 1))

        if not res_items:
            break

        all_data.extend(res_items)
        last_t = int(res_items[-1].get("time", 0))

        if len(res_items) < limit or last_t >= end_ms - int_ms:
            break

        curr_start = max(curr_start + (limit * int_ms), last_t + int_ms)
        time.sleep(0.10)

    return all_data


def download_symbol_candles_bingx(
    symbol: str,
    interval: str,
    days: int,
    candles_dir: Path,
    force: bool = False,
    incremental_hours: int = 8
) -> pd.DataFrame:
    """差分キャッシュを考慮して指定銘柄・時間足のデータを取得・保存"""
    safe_sym_name = symbol.replace("-", "_").replace("/", "_")
    csv_path = candles_dir / f"{safe_sym_name}_{interval}.csv"
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
                start_ms = max(target_start_ms, latest_ms + 1)
                if incremental_hours > 0:
                    start_ms = max(start_ms, now_ms - (incremental_hours + 1) * 3600 * 1000)
        except Exception:
            old_df = pd.DataFrame()

    new_rows = []
    if start_ms < now_ms:
        raw_klines = fetch_symbol_klines_bingx(symbol, interval, start_ms, now_ms)
        for k in raw_klines:
            try:
                t_val = int(k.get("time", 0))
                new_rows.append({
                    "timestamp": pd.to_datetime(t_val, unit="ms"),
                    "open": float(k.get("open", 0.0)),
                    "high": float(k.get("high", 0.0)),
                    "low": float(k.get("low", 0.0)),
                    "close": float(k.get("close", 0.0)),
                    "volume": float(k.get("volume", 0.0)),
                    "symbol": symbol
                })
            except Exception:
                continue

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
    days_1d: int = 1460,    # 4年分
    days_1h: int = 1460,    # 4年分
    days_15m: int = 180,    # 180日分
    days_5m: int = 90,      # 90日分
    days_1m: int = 30,      # 30日分
    intervals: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    force: bool = False,
    force_upload: bool = False,
    skip_upload: bool = False,
    symbols_override: Optional[List[str]] = None,
    incremental_hours: int = 8
) -> None:
    data_dir = Path(__file__).resolve().parent / "Data"
    candles_dir = data_dir / "historical_candles_bingx"
    candles_dir.mkdir(parents=True, exist_ok=True)

    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    target_intervals = list(intervals) if intervals else ["1d", "1h", "15m", "5m"]

    default_cat_order = ["crypto", "indices_commodities", "forex", "stocks"]
    target_categories = [c.lower() for c in categories] if categories else default_cat_order

    log("=" * 75)
    log("🚀 [BingX] 4大カテゴリー別マルチ時間足データ収集＆時間分割配信パイプライン開始")
    log(f"   対象カテゴリー: {target_categories}")
    log(f"   対象足種: {target_intervals} | 実行日時タグ: {now_jst}")
    log("=" * 75)

    if symbols_override:
        # 指定銘柄のみをカスタムカテゴリーとして実行
        all_categories = {"custom": [s.strip().upper() for s in symbols_override]}
        run_cats = ["custom"]
    else:
        all_categories = fetch_bingx_symbols_by_category()
        run_cats = [c for c in target_categories if c in all_categories and all_categories[c]]

    days_map = {"1d": days_1d, "1h": days_1h, "15m": days_15m, "5m": days_5m, "1m": days_1m}

    for cat_idx, cat in enumerate(run_cats, 1):
        target_symbols = all_categories[cat]
        cat_meta = CATEGORY_METADATA.get(cat, {"label": cat, "file_tag": cat})
        cat_label = cat_meta["label"]
        cat_tag = cat_meta["file_tag"]

        log("\n" + "=" * 75)
        log(f"🏷️ [{cat_idx}/{len(run_cats)}] BingX カテゴリー: 【{cat_label}】 収集開始 ({len(target_symbols)} 銘柄)")
        log("=" * 75)

        for interval in target_intervals:
            log(f"\n📂 ========== BingX [{cat_label}] 【{interval.upper()}足】 収集開始 ==========")
            target_days = days_map.get(interval, 30)

            symbol_dfs: Dict[str, pd.DataFrame] = {}

            for sym_idx, sym in enumerate(target_symbols, 1):
                df = download_symbol_candles_bingx(
                    symbol=sym,
                    interval=interval,
                    days=target_days,
                    candles_dir=candles_dir,
                    force=force,
                    incremental_hours=incremental_hours
                )
                if not df.empty:
                    symbol_dfs[sym] = df

                if sym_idx % 20 == 0 or sym_idx == len(target_symbols):
                    log(f"   [{sym_idx:4d}/{len(target_symbols)}] {sym} ({interval}) 完了 (保有レコード: {len(df):,} 行)")

                time.sleep(0.30)

            # 2. カテゴリー別 全銘柄網羅グリッド生成 (欠損値 NaN 補完)
            gc.collect()
            time.sleep(1.0)
            log(f"\n🧩 [{cat_label}] [{interval.upper()}] 全銘柄包含マスターグリッド生成中...")
            master_df = build_full_symbol_time_grid(symbol_dfs, target_symbols)

            if interval == "1h":
                cat_csv = data_dir / f"bingx_{cat_tag}_all_symbols_merged.csv"
                master_df.to_csv(cat_csv, index=False, encoding="utf-8")
                log(f"📄 [BingX] [{cat_label}] 1H 統合マスターCSVを更新しました: {cat_csv.name} ({len(master_df):,} 行)")

            # 3. カテゴリー別 ZIP アーカイブ生成 (8時間つけ足し時は最新1本スライス、全件時はN分割)
            exchange_slug = f"bingx_{cat_tag}"
            if incremental_hours > 0 and not force:
                parts = create_recent_slice_zip(
                    df=master_df,
                    exchange=exchange_slug,
                    interval=interval,
                    out_dir=data_dir,
                    timestamp_tag=now_jst,
                    hours=incremental_hours
                )
            else:
                parts = split_and_create_time_zips(
                    df=master_df,
                    exchange=exchange_slug,
                    interval=interval,
                    out_dir=data_dir,
                    timestamp_tag=now_jst,
                    max_part_rows=400_000,
                    min_parts=2
                )

            # 4. 古い順からの Discord 順次アップロード (全データ送信先を Bitbank チャンネルへ集約)
            if not skip_upload:
                upload_time_split_zips_to_discord(
                    parts=parts,
                    webhook_name="real1_bitbank",
                    exchange_label=f"BingX [{cat_label}]",
                    interval_label=interval,
                    interval_wait_sec=3.0,
                    force_upload=force_upload
                )

            del master_df
            del symbol_dfs
            gc.collect()
            time.sleep(1.5)

        # カテゴリー完了後のクールダウン
        log(f"\n✅ BingX カテゴリー: 【{cat_label}】 全足種の収集・配信が完了しました。")
        gc.collect()
        await asyncio.sleep(2.0)

    # 5. クリーンアップ
    log("\n🧹 不要な古いZIPファイルの自動クリーンアップを実行中...")
    cleanup_expired_archives(data_dir=data_dir, patterns=("bingx_*.zip",), max_age_hours=24.0)

    log("\n🎉 [BingX] 全4大カテゴリー・マルチ時間足データ収集＆時間分割配信パイプラインがすべて完了しました！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BingX 4大カテゴリー別 全銘柄マルチ時間足データ収集パイプライン")
    parser.add_argument("--days-1d", type=int, default=1460, help="日足取得期間（日）")
    parser.add_argument("--days-1h", type=int, default=1460, help="1時間足取得期間（日）")
    parser.add_argument("--days-15m", type=int, default=180, help="15分足取得期間（日）")
    parser.add_argument("--days-5m", type=int, default=90, help="5分足取得期間（日）")
    parser.add_argument("--days-1m", type=int, default=30, help="1分足取得期間（日）")
    parser.add_argument("--intervals", nargs="+", default=["1d", "1h", "15m", "5m"], help="実行する足種 (デフォルト: 1d 1h 15m 5m)")
    parser.add_argument("--categories", nargs="+", default=["crypto", "indices_commodities", "forex", "stocks"], help="対象カテゴリー (crypto, indices_commodities, forex, stocks)")
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
        categories=args.categories,
        force=args.force,
        force_upload=args.force_upload,
        skip_upload=args.skip_upload,
        symbols_override=args.symbols
    ))
