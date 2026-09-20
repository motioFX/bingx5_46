"""
Gemini分析・スマホ/PCバックテスト用 直近全銘柄データ抽出 ＆ Discord自動送信ツール
(export_recent_candles.py)

BingX全銘柄（約590銘柄以上）の直近1〜2ヶ月分（デフォルト: 60日間 = 1,440時間足）を抽出し、
全銘柄統合マスターCSV ＋ 主要銘柄個別CSVを1つの最適化ZIP（8〜10MB）として生成して
Discord (#real3_bngx) へ送信します。

スマホのGeminiアプリやPCのバックテスト環境にそのまま取り込んで利用可能です。
"""

import argparse
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
import pandas as pd

from config_loader import get_webhook_url
from bingx5_46_3logic import send_discord
from upload_registry import should_upload_file, record_file_uploaded

JST = timezone(timedelta(hours=9))
FIXED_SYMBOLS = ["HYPE-USDT", "NEAR-USDT", "ZEC-USDT", "ARB-USDT", "UNI-USDT", "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT"]


def export_recent_candles(days: int = 60, send_discord_flag: bool = True, all_symbols: bool = True) -> Optional[Path]:
    base_dir = Path(__file__).resolve().parent
    candles_dir = base_dir / "Data" / "historical_candles"
    out_dir = base_dir / "Data" / "gemini_export"
    out_dir.mkdir(parents=True, exist_ok=True)

    hours_limit = days * 24

    # 対象銘柄CSVリストを決定
    if all_symbols:
        csv_files = sorted(list(candles_dir.glob("*_1h.csv")))
        target_label = "全銘柄"
    else:
        csv_files = []
        for sym in FIXED_SYMBOLS:
            clean_sym = sym.replace("-USDT", "").replace("USDT", "").upper()
            for name in [f"{sym}_1h.csv", f"{clean_sym}-USDT_1h.csv", f"{clean_sym}_1h.csv"]:
                p = candles_dir / name
                if p.exists():
                    csv_files.append(p)
                    break
        target_label = "固定銘柄"

    if not csv_files:
        print(f"[Export Error] {candles_dir} にローソク足CSVが見つかりません。")
        return None

    print(f"\n[Export] {target_label} ({len(csv_files)} 銘柄) の直近 {days} 日分 ({hours_limit} 時間足) を抽出中...")

    all_rows = []
    ind_dfs = {}
    valid_count = 0

    out_cols = ["timestamp", "datetime_jst", "symbol", "open", "high", "low", "close", "volume"]

    for idx, cand_path in enumerate(csv_files, 1):
        sym_name = cand_path.name.replace("_1h.csv", "")
        try:
            df = pd.read_csv(cand_path)
            if df.empty or "close" not in df.columns:
                continue

            # timestamp を ISO/JST文字列に変換
            if "timestamp" in df.columns:
                if pd.to_numeric(df["timestamp"], errors="coerce").notna().all():
                    df["datetime_jst"] = pd.to_datetime(df["timestamp"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M")
                else:
                    df["datetime_jst"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d %H:%M")
            else:
                continue

            if "symbol" not in df.columns:
                df["symbol"] = sym_name

            # 直近N本（60日 = 1440本）を抽出
            sub_df = df.tail(hours_limit).copy().reset_index(drop=True)
            if len(sub_df) < 5:
                continue

            valid_count += 1
            avail = [c for c in out_cols if c in sub_df.columns]
            export_sub = sub_df[avail]

            # 主要銘柄または個別抽出用
            ind_dfs[sym_name] = export_sub
            all_rows.extend(export_sub.to_dict(orient="records"))

            if idx % 100 == 0 or idx == len(csv_files):
                print(f"  ... 処理中: {idx}/{len(csv_files)} 銘柄完了 (有効: {valid_count})")

        except Exception as e:
            pass

    if not all_rows:
        print("[Export Error] 有効なデータが抽出できませんでした。")
        return None

    df_all = pd.DataFrame(all_rows).drop_duplicates(subset=["timestamp", "symbol"]).sort_values(by=["timestamp", "symbol"]).reset_index(drop=True)
    print(f"\n✅ 全データ統合完了: {valid_count} 銘柄, 総レコード数: {len(df_all):,} 行")

    # 実際の日付範囲（JST）から開始日と終了日を正確に取得
    first_dt = df_all["datetime_jst"].iloc[0]
    last_dt = df_all["datetime_jst"].iloc[-1]
    start_tag = pd.to_datetime(first_dt).strftime("%Y%m%d")
    end_tag = pd.to_datetime(last_dt).strftime("%Y%m%d")
    start_fmt = pd.to_datetime(first_dt).strftime("%Y/%m/%d %H:%M")
    end_fmt = pd.to_datetime(last_dt).strftime("%Y/%m/%d %H:%M")

    # 取得時刻（何時取得か）のタグを生成（英数字表記: 例 "15h", "17h"）
    now_jst = datetime.now(JST)
    acq_tag = f"{now_jst.strftime('%H')}h"
    acq_fmt = now_jst.strftime("%Y/%m/%d %H:%M JST")

    # ZIPファイル名設定（何日から何日までのデータか、何時取得かを明確に命名: 'master' は含めない）
    mode_prefix = "all_symbols" if all_symbols else "fixed5"
    zip_name = f"bingx_{mode_prefix}_1h_{start_tag}_to_{end_tag}_{acq_tag}.zip"
    zip_path = out_dir / zip_name

    print(f"📦 ZIPアーカイブ作成中: {zip_path.name} (期間: {start_tag} ～ {end_tag}, 取得: {acq_tag}) ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 全銘柄統合CSV 1ファイルのみをZIPに格納（個別CSVは含めず軽量化）
        all_csv_name = f"bingx_{mode_prefix}_1h_{start_tag}_to_{end_tag}_{acq_tag}.csv"
        all_csv_str = df_all.to_csv(index=False)
        zf.writestr(all_csv_name, all_csv_str)

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"✅ ZIP作成完了: {zip_path.name} ({zip_size_mb:.2f} MB)")

    send_target = zip_path

    if send_discord_flag:
        discord = send_discord()
        if should_upload_file(send_target):
            desc = (
                f"📊 **【バックテスト用】BingX {target_label} 1時間足データ ({start_tag} ～ {end_tag} / {acq_tag})**\n"
                f"• 対象期間: `{start_fmt}` ～ `{end_fmt} JST` ({days}日間 / {hours_limit}本)\n"
                f"• 取得日時: `{acq_fmt}` ({acq_tag})\n"
                f"• ファイル名: `{send_target.name}`\n"
                f"• 収録銘柄数: 全 `{valid_count}` 銘柄 (総行数: `{len(df_all):,}` 行)\n"
                f"• ファイルサイズ: `{send_target.stat().st_size / (1024*1024):.2f} MB`\n"
                f"• 内容: 全銘柄統合CSV（`{all_csv_name}` 1ファイルのみ格納）\n"
                f"※スマホのGeminiやPCのバックテスト環境にそのまま添付・利用可能です。"
            )
            print(f"📤 Discord (#real3_bngx) へアップロード中: {send_target.name} ...")
            sent = discord.send_file(send_target, desc)
            if sent:
                record_file_uploaded(send_target, rows=len(df_all))
                print(f"🚀 Discord送信完了 ＆ レジストリ記録完了: {send_target.name}")
        else:
            print(f"📦 [アップロード不要] {send_target.name} はすでにDiscord送信完了済み（データ変更なし）のため送信をスキップしました。")

    return send_target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export recent candles for backtesting and Gemini analysis")
    parser.add_argument("--days", type=int, default=60, help="Number of days to export (default: 60 = 2 months)")
    parser.add_argument("--fixed-only", action="store_true", help="Export fixed symbols only (default: all symbols)")
    parser.add_argument("--no-send", action="store_true", help="Do not send to Discord")
    args = parser.parse_args()

    export_recent_candles(
        days=args.days,
        send_discord_flag=(not args.no_send),
        all_symbols=(not args.fixed_only)
    )
