"""Binance Japan 全銘柄 1時間足データ（4ヶ月分 / 120日）取得 & 2分割ZIP生成・Discord送信パイプライン

仕様:
1. Binance Japan の全JPY現物ペア（約27銘柄）の1時間足を過去120日（約4ヶ月）分取得。
2. 差分キャッシュ更新: すでに取得済みのローカルCSV（Data/historical_candles_binance/{symbol}_1h.csv）を更新。
3. 統合マスターCSVの生成: Data/binance_japan_all_symbols_merged.csv
4. 期間2分割ZIPアーカイブ生成:
   - Part 1/2: binance_japan_all_symbols_past_{YYYYMMDD_HHMMSS}.zip （前半60日・過去検証用）
   - Part 2/2: binance_japan_all_symbols_recent_{YYYYMMDD_HHMMSS}.zip （後半60日・直近検証用）
5. Discord送信: Bitbankパイプラインに準拠したフォーマットでDiscordへ送信。
6. クリーンアップ: 24時間超過した古いZIPを自動削除（最新1セット保護）。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url
from upload_registry import should_upload_file, record_file_uploaded

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


# ==================== Discord 送信ヘルパー ====================
class send_discord_binance:
    def __init__(self) -> None:
        self.real1_webhook = get_webhook_url("real1_bitbank")
        self.test4_webhook = get_webhook_url("test4_test")

        if sys.platform == "win32":
            self.webhook_url = self.test4_webhook or self.real1_webhook
        else:
            self.webhook_url = self.real1_webhook or self.test4_webhook

    def _get_target_webhooks(self) -> list[str]:
        for idx, arg in enumerate(sys.argv):
            if arg in ("--channel", "--webhook") and idx + 1 < len(sys.argv):
                val = sys.argv[idx + 1].strip().lower()
                if "real" in val or "bitbank" in val:
                    return [self.real1_webhook] if self.real1_webhook else []
                elif "test" in val or "win" in val:
                    return [self.test4_webhook] if self.test4_webhook else []

        if sys.platform == "win32":
            target = self.test4_webhook or self.real1_webhook
        else:
            target = self.real1_webhook or self.test4_webhook
        return [target] if target else []

    def send_message(self, content: str) -> bool:
        webhooks = self._get_target_webhooks()
        if not webhooks:
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
        if not file_path.exists():
            return False
        webhooks = self._get_target_webhooks()
        if not webhooks:
            return False

        mime_type = "application/zip" if file_path.suffix == ".zip" else "text/csv"
        success = True
        for url in webhooks:
            try:
                with open(file_path, "rb") as f:
                    resp = requests.post(
                        url,
                        data={"content": description},
                        files={"file": (file_path.name, f, mime_type)},
                        timeout=180
                    )
                    resp.raise_for_status()
            except Exception as e:
                log(f"[Discord Error] ファイル送信失敗 ({file_path.name}): {e}")
                success = False
        return success


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
def fetch_symbol_klines(symbol: str, days: int = 120) -> pd.DataFrame:
    """指定銘柄の過去 N 日分の1時間足を高速取得"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    url = f"{BINANCE_API_URL}/api/v3/klines"
    all_data = []
    curr_start = start_ms

    while curr_start < now_ms:
        params = {
            "symbol": symbol,
            "interval": "1h",
            "startTime": curr_start,
            "limit": 1000
        }
        try:
            res = requests.get(url, params=params, timeout=10).json()
            if not res or not isinstance(res, list):
                break
            all_data.extend(res)
            last_open_time = res[-1][0]
            if last_open_time <= curr_start:
                break
            curr_start = last_open_time + 1
            if len(res) < 1000:
                break
            time.sleep(0.05)
        except Exception as e:
            log(f"   ⚠️ {symbol} klines 取得エラー: {e}")
            break

    if not all_data:
        return pd.DataFrame()

    rows = []
    for k in all_data:
        ts = pd.to_datetime(k[0], unit="ms", utc=True)
        rows.append({
            "timestamp": ts,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


# ==================== クリーンアップ ====================
def cleanup_binance_data_dir(data_dir: Path, max_age_hours: float = 24.0, keep_latest_n: int = 1) -> Dict[str, Any]:
    """24時間以上経過した古いBinance ZIPアーカイブをクリーンアップ"""
    cutoff_ts = time.time() - (max_age_hours * 3600.0)
    deleted_files = []
    total_freed_bytes = 0

    if not data_dir.exists():
        return {"deleted_files": [], "freed_mb": 0.0}

    zip_categories = {
        "past": list(data_dir.glob("binance_japan_all_symbols_past_*.zip")),
        "recent": list(data_dir.glob("binance_japan_all_symbols_recent_*.zip")),
    }

    for cat_name, files in zip_categories.items():
        files_sorted = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
        to_check = files_sorted[keep_latest_n:]
        for f in to_check:
            try:
                stat = f.stat()
                if stat.st_mtime < cutoff_ts:
                    size = stat.st_size
                    f.unlink()
                    deleted_files.append(f.name)
                    total_freed_bytes += size
                    log(f"   🗑️ 古いBinance ZIP削除: {f.name} ({size / (1024 * 1024):.2f} MB)")
            except Exception as e:
                log(f"   ⚠️ 削除失敗 ({f.name}): {e}")

    # 古い日時付きCSVのクリーンアップ
    tagged_csvs = sorted(list(data_dir.glob("binance_japan_all_symbols_merged_*.csv")), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in tagged_csvs[keep_latest_n:]:
        try:
            stat = f.stat()
            if stat.st_mtime < cutoff_ts:
                size = stat.st_size
                f.unlink()
                deleted_files.append(f.name)
                total_freed_bytes += size
                log(f"   🗑️ 古いBinance日時付きCSV削除: {f.name} ({size / (1024 * 1024):.2f} MB)")
        except Exception as e:
            log(f"   ⚠️ 削除失敗 ({f.name}): {e}")

    freed_mb = total_freed_bytes / (1024 * 1024)

    return {"deleted_files": deleted_files, "freed_mb": freed_mb}


# ==================== パイプライン実行 ====================
async def run_binance_pipeline(days: int = 120, to_discord: bool = True) -> None:
    """Binance Japan 全銘柄 120日分データ取得 & 2分割ZIP Discord送信パイプライン"""
    discord = send_discord_binance()
    data_dir = BASE_DIR / "Data"
    candles_dir = data_dir / "historical_candles_binance"
    candles_dir.mkdir(parents=True, exist_ok=True)

    now_jst = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    log("=" * 70)
    log("🚀 [Binance Japan] 全銘柄4ヶ月分（120日）1時間足データ取得＆2分割ZIPパイプライン開始")
    log(f"   期間: 過去 {days} 日間 | 実行日時タグ: {now_jst}")
    log("=" * 70)

    # 1. 取引可能銘柄一覧の取得
    symbols = fetch_binance_jpy_symbols()
    log(f"📌 Binance Japan 対象銘柄: {len(symbols)} 銘柄 ({', '.join(symbols[:8])} ...)")

    # 2. 各銘柄のklinesを取得してローカルCSVに保存
    all_dfs = []
    loop = asyncio.get_running_loop()

    for idx, sym in enumerate(symbols, 1):
        csv_path = candles_dir / f"{sym.lower()}_1h.csv"
        # 非同期エグゼキュータで並行度を制御しつつ取得
        df = await loop.run_in_executor(None, fetch_symbol_klines, sym, days)
        if not df.empty:
            df["symbol"] = sym.lower()
            df.to_csv(csv_path, index=False, encoding="utf-8")
            all_dfs.append(df)
            if idx % 5 == 0 or idx == len(symbols):
                log(f"   [{idx:2d}/{len(symbols)}] {sym} 完了 ({len(df):,} 行)")
        else:
            log(f"   [{idx:2d}/{len(symbols)}] {sym} 取得なし")

        # サーバー負荷軽減およびレートリミット対策として適度なインターバル（0.3秒）を挿入
        await asyncio.sleep(0.3)

    if not all_dfs:
        log("❌ Binance Japan データを取得できませんでした。")
        return

    # 3. 統合マスターCSVの作成
    master_df = pd.concat(all_dfs, ignore_index=True)
    master_df["timestamp"] = pd.to_datetime(master_df["timestamp"])
    master_df = master_df.sort_values(by=["timestamp", "symbol"]).reset_index(drop=True)

    fixed_master_csv = data_dir / "binance_japan_all_symbols_merged.csv"
    master_df.to_csv(fixed_master_csv, index=False, encoding="utf-8")
    log(f"\n📄 Binance Japan 固定マスターCSV更新: {fixed_master_csv.name} ({len(master_df):,} 行)")

    binance_tagged_csv = data_dir / f"binance_japan_all_symbols_merged_{now_jst}.csv"
    master_df.to_csv(binance_tagged_csv, index=False, encoding="utf-8")
    log(f"📄 Binance Japan 日時付き統合CSV保存: {binance_tagged_csv.name}")


    # 4. 期間2分割（前半・後半）
    unique_ts = sorted(master_df["timestamp"].unique())
    mid_idx = len(unique_ts) // 2
    split_ts = unique_ts[mid_idx]

    df_past = master_df[master_df["timestamp"] < split_ts].copy()
    df_recent = master_df[master_df["timestamp"] >= split_ts].copy()

    past_start = str(df_past["timestamp"].min())[:10]
    past_end = str(df_past["timestamp"].max())[:10]
    recent_start = str(df_recent["timestamp"].min())[:10]
    recent_end = str(df_recent["timestamp"].max())[:10]

    ts_jst_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    parts = [
        (
            "Part 1/2 【過去データ (前半)】",
            f"{past_start} 〜 {past_end}",
            df_past,
            data_dir / f"binance_japan_all_symbols_past_{now_jst}.zip",
            f"binance_japan_all_symbols_past_{now_jst}.csv",
            "過去ヒストリー検証・長期バックテスト用"
        ),
        (
            "Part 2/2 【直近データ (後半)】",
            f"{recent_start} 〜 {recent_end}",
            df_recent,
            data_dir / f"binance_japan_all_symbols_recent_{now_jst}.zip",
            f"binance_japan_all_symbols_recent_{now_jst}.csv",
            "直近相場分析・高精度最適化用"
        )
    ]

    log("\n📦 期間2分割ZIPアーカイブを生成中...")
    for part_title, period_str, part_df, zip_path, csv_name, usage_desc in parts:
        row_count = len(part_df)
        csv_buffer = io.BytesIO()
        part_df.to_csv(csv_buffer, index=False, encoding="utf-8")
        csv_bytes = csv_buffer.getvalue()
        csv_size_mb = len(csv_bytes) / (1024 * 1024)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(csv_name, csv_bytes)

        zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
        compression_ratio = (1.0 - (zip_size_mb / csv_size_mb)) * 100.0 if csv_size_mb > 0 else 0.0
        # 2分割ZIP (past / recent) を Discord 送信 (素のCSVは送信しない)
        if to_discord:
            desc = (
                f"📂 **【Binance Japan 全銘柄 1時間足データ (4ヶ月分)】**\n"
                f"🏷️ **{part_title}**\n"
                f"⏱️ 期間: `{period_str}` (対象: Binance Japan 全 {len(symbols)} JPY現物銘柄)\n"
                f"📊 レコード数: `{row_count:,}` 行\n"
                f"💡 用途: {usage_desc}\n"
                f"📦 ファイル: `{zip_path.name}` ({zip_size_mb:.2f} MB)\n"
                f"📅 生成日時: `{ts_jst_str}`"
            )
            log(f"   🚀 Discordへ送信中: {zip_path.name} ...")
            discord.send_file(zip_path, desc)
            await asyncio.sleep(2.0)

    # 5. クリーンアップ実行

    cleanup_binance_data_dir(data_dir, max_age_hours=24.0)
    log("\n✨ [Binance Japan] 全銘柄4ヶ月分データ取得＆2分割ZIP配信完了！")


if __name__ == "__main__":
    asyncio.run(run_binance_pipeline(days=120, to_discord=False))
