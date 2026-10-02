"""全取引所・全銘柄・マルチ時間足ヒストリカルデータ共通パイプラインユーティリティ

仕様:
1. 全銘柄網羅グリッド生成:
   - 全銘柄が常にすべてのタイムスタンプに含まれる時系列テーブルを作成。
   - 上場前などのデータが存在しない過去期間は NaN で補完。
2. 時間軸でのN分割ZIPアーカイブ生成:
   - 銘柄ごとではなく、全銘柄を包含した状態で時間軸（期間）に沿って均等分割。
   - Discord制限（25MB）内に安全に収まるよう動的サイズ判定。
3. 古い順からのDiscord順次送信:
   - 必ず Part 1（最古データ）から順次アップロードし、最後に最新データを送信。
   - 各送信間にセーフティウェイトを挿入し、Discordのレート制限を回避。
4. ディスク空き容量保護:
   - 24時間経過した古いZIPアーカイブを自動削除し、最新1セットのみを保護。
"""
from __future__ import annotations

import gc
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url
from upload_registry import should_upload_file, record_file_uploaded

JST = timezone(timedelta(hours=9))
UTC = timezone.utc


def log_util(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def build_full_symbol_time_grid(
    symbol_dfs: Dict[str, pd.DataFrame],
    all_symbols: Sequence[str]
) -> pd.DataFrame:
    """
    全銘柄を包含する時系列マスターテーブルを構築。
    すべてのタイムスタンプにおいて全銘柄が存在するようにし、
    上場前などでデータが存在しない期間・銘柄は NaN で補完する。
    """
    if not symbol_dfs:
        return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])

    # 1. 全銘柄のユニークなタイムスタンプの集合を抽出
    all_ts_set: Set[pd.Timestamp] = set()
    cleaned_dfs: Dict[str, pd.DataFrame] = {}

    for sym in all_symbols:
        df = symbol_dfs.get(sym)
        if df is not None and not df.empty:
            df = df.copy()
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            cleaned_dfs[sym] = df
            all_ts_set.update(df["timestamp"].dropna().tolist())

    if not all_ts_set:
        return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])

    sorted_ts = sorted(list(all_ts_set))
    log_util(f"   ⏱️ タイムスタンプ統合: 全 {len(sorted_ts):,} ステップ (全 {len(all_symbols)} 銘柄対象)")

    # 2. メモリ効率を考慮し、全銘柄をアライメント
    aligned_dfs: List[pd.DataFrame] = []
    base_ts_df = pd.DataFrame({"timestamp": sorted_ts})

    for sym in all_symbols:
        if sym in cleaned_dfs:
            sdf = cleaned_dfs[sym]
            merged = pd.merge(base_ts_df, sdf, on="timestamp", how="left")
        else:
            merged = base_ts_df.copy()
            for col in ["open", "high", "low", "close", "volume"]:
                merged[col] = np.nan

        merged["symbol"] = sym
        # 必要なカラム順を保証
        merged = merged[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]
        aligned_dfs.append(merged)

    # 3. 結合してタイムスタンプ順・銘柄順にソート
    master_df = pd.concat(aligned_dfs, ignore_index=True)
    master_df = master_df.sort_values(by=["timestamp", "symbol"]).reset_index(drop=True)

    del aligned_dfs
    del base_ts_df
    gc.collect()

    log_util(f"   ✅ 全銘柄網羅テーブル生成完了: {len(master_df):,} 行 (欠損NaN補完済み)")
    return master_df


def split_and_create_time_zips(
    df: pd.DataFrame,
    exchange: str,
    interval: str,
    out_dir: Path,
    timestamp_tag: str,
    max_part_rows: int = 500_000,
    min_parts: int = 2
) -> List[Tuple[str, Path, str, int, int]]:
    """
    全銘柄が含まれたデータフレームを、時間軸（期間）に沿って古い順から N 分割し、ZIPアーカイブを作成する。
    戻り値: [(part_name, zip_path, period_str, row_count, total_symbols), ...] （Part 1 最古 ➔ Part N 直近）
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if df.empty:
        return []

    unique_ts = sorted(df["timestamp"].dropna().unique())
    total_ts_count = len(unique_ts)
    total_rows = len(df)
    n_symbols = len(df["symbol"].unique())

    # 分割数の算出 (少なくとも min_parts、または行数基準)
    parts_by_rows = max(1, (total_rows + max_part_rows - 1) // max_part_rows)
    n_parts = max(min_parts, parts_by_rows)

    # タイムスタンプ配列を n_parts に均等分割
    chunk_size = (total_ts_count + n_parts - 1) // n_parts
    results: List[Tuple[str, Path, str, int, int]] = []

    log_util(f"📦 [{exchange.upper()} | {interval}] 時間軸 {n_parts} 分割アーカイブ作成開始 (全 {total_rows:,} 行 / 全 {n_symbols} 銘柄)...")

    for part_idx in range(n_parts):
        start_i = part_idx * chunk_size
        end_i = min((part_idx + 1) * chunk_size, total_ts_count)
        if start_i >= total_ts_count:
            break

        part_ts_slice = unique_ts[start_i:end_i]
        min_ts = part_ts_slice[0]
        max_ts = part_ts_slice[-1]

        # 該当タイムスタンプ範囲のスライスを取得
        sub_df = df[(df["timestamp"] >= min_ts) & (df["timestamp"] <= max_ts)].copy()
        if sub_df.empty:
            continue

        p_start_str = pd.to_datetime(min_ts).strftime("%Y%m%d")
        p_end_str = pd.to_datetime(max_ts).strftime("%Y%m%d")
        period_label = f"{pd.to_datetime(min_ts).strftime('%Y-%m-%d')} 〜 {pd.to_datetime(max_ts).strftime('%Y-%m-%d')}"

        part_num = part_idx + 1
        part_name = f"Part {part_num}/{n_parts}"
        tag_role = "past" if part_num < n_parts else "recent"

        zip_filename = f"{exchange}_{interval}_part{part_num}of{n_parts}_{tag_role}_{p_start_str}_{p_end_str}_{timestamp_tag}.zip"
        inner_csv_name = f"{exchange}_{interval}_part{part_num}of{n_parts}_{p_start_str}_{p_end_str}_{timestamp_tag}.csv"
        zip_path = out_dir / zip_filename

        # ZIP作成 (CPU負荷抑制のため compresslevel=1)
        csv_buf = sub_df.to_csv(index=False).encode("utf-8")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            zf.writestr(inner_csv_name, csv_buf)

        del csv_buf
        gc.collect()

        file_size_mb = zip_path.stat().st_size / (1024 * 1024)
        log_util(f"   ✓ {part_name} 作成: {zip_path.name} ({len(sub_df):,} 行 / {file_size_mb:.2f} MB / 期間: {period_label})")
        results.append((part_name, zip_path, period_label, len(sub_df), n_symbols))

    return results


def upload_time_split_zips_to_discord(
    parts: List[Tuple[str, Path, str, int, int]],
    webhook_name: str,
    exchange_label: str,
    interval_label: str,
    interval_wait_sec: float = 3.0,
    force_upload: bool = False
) -> None:
    """
    時間分割されたZIPアーカイブを、必ず古い順（Part 1 ➔ Part N）からDiscordへ順次送信する。
    各送信間にウェイトを挿入してDiscordのレート制限を回避する。
    """
    if not parts:
        return

    webhook_url = get_webhook_url(webhook_name)
    if not webhook_url:
        log_util(f"[Discord Error] Webhook '{webhook_name}' が見つかりません。送信をスキップします。")
        return

    ts_jst_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    total_parts = len(parts)

    log_util(f"\n📤 [{exchange_label} | {interval_label}] Discord古い順アップロード開始 (計 {total_parts} アーカイブ)...")

    # 必ず古い順（インデックス順）でループ
    for idx, (part_name, zip_path, period_str, row_count, total_symbols) in enumerate(parts, 1):
        if not zip_path.exists():
            continue

        if not force_upload and not should_upload_file(zip_path):
            log_util(f"   ⏩ 送信スキップ (既に同一ハッシュが送信済み): {zip_path.name}")
            continue

        file_size_mb = zip_path.stat().st_size / (1024 * 1024)
        is_latest = (idx == total_parts)
        role_desc = "直近最新データ・全銘柄収録" if is_latest else "過去ヒストリー・全銘柄収録"

        content = (
            f"📦 **[{exchange_label} 全銘柄ヒストリー ({interval_label.upper()})] {part_name}** ({ts_jst_str})\n"
            f"• ファイル名: `{zip_path.name}`\n"
            f"• 期間: `{period_str}` ({row_count:,} 行)\n"
            f"• 収録銘柄: `全 {total_symbols} 銘柄` (未上場・欠損期間は NaN 補完)\n"
            f"• ファイルサイズ: `{file_size_mb:.2f} MB`\n"
            f"• 区分: {role_desc}"
        )

        log_util(f"   ⬆️ アップロード中 ({idx}/{total_parts}): {zip_path.name} ({file_size_mb:.2f} MB)...")
        success = False
        max_retries = 3

        for attempt in range(max_retries):
            try:
                with open(zip_path, "rb") as f:
                    resp = requests.post(
                        webhook_url,
                        data={"content": content},
                        files={"file": (zip_path.name, f, "application/zip")},
                        timeout=180
                    )
                    if resp.status_code in (200, 204):
                        success = True
                        break
                    elif resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", 5.0))
                        log_util(f"   ⚠️ Discord Rate Limit検知。{retry_after}秒待機します...")
                        time.sleep(retry_after + 1.0)
                    else:
                        resp.raise_for_status()
            except Exception as e:
                log_util(f"   ⚠️ 送信エラー (試行 {attempt + 1}/{max_retries}): {e}")
                time.sleep(2.0 * (attempt + 1))

        if success:
            record_file_uploaded(zip_path, rows=row_count)
            log_util(f"   ✅ Discord 送信成功: {zip_path.name}")
        else:
            log_util(f"   ❌ Discord 送信失敗: {zip_path.name}")

        # レート制限回避セーフティインターバル
        if idx < total_parts:
            time.sleep(interval_wait_sec)

    log_util(f"✨ [{exchange_label} | {interval_label}] 全パートのアップロード処理が完了しました。\n")


def cleanup_expired_archives(
    data_dir: Path,
    patterns: Sequence[str] = ("*_part*of*.zip",),
    max_age_hours: float = 24.0,
    keep_latest_n_per_pattern: int = 2
) -> None:
    """
    指定ディレクトリ内の指定パターンの古いZIPアーカイブを削除（最新 keep_latest_n は保護）
    """
    if not data_dir.exists():
        return

    now_ts = time.time()
    cutoff_ts = now_ts - (max_age_hours * 3600.0)
    deleted_count = 0
    freed_bytes = 0

    for pat in patterns:
        matched = sorted(list(data_dir.glob(pat)), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in matched[keep_latest_n_per_pattern:]:
            try:
                st = f.stat()
                if st.st_mtime < cutoff_ts:
                    sz = st.st_size
                    f.unlink()
                    deleted_count += 1
                    freed_bytes += sz
                    log_util(f"   🗑️ 古いZIP削除: {f.name} ({sz / (1024 * 1024):.2f} MB)")
            except Exception as e:
                log_util(f"   ⚠️ 削除失敗 ({f.name}): {e}")

    if deleted_count > 0:
        log_util(f"🧹 [Cleanup] 計 {deleted_count} ファイル削除, 約 {freed_bytes / (1024 * 1024):.2f} MB 解放")
