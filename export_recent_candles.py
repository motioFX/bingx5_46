"""
Gemini分析・スマホバックテスト用 直近データ抽出 ＆ Discord送信ツール
(export_recent_candles.py)

固定5銘柄 (HYPE, NEAR, ZEC, ARB, UNI) + BTC の直近1ヶ月分 / 2ヶ月分OHLCVデータを
軽量CSVおよびZIPとして抽出し、スマホのGeminiにそのまま添付できる形式で
Discord (#real3_bngx) へ送信します。
"""

import argparse
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

from config_loader import get_webhook_url
from bingx5_46_3logic import send_discord

JST = timezone(timedelta(hours=9))
FIXED_SYMBOLS = ["HYPE-USDT", "NEAR-USDT", "ZEC-USDT", "ARB-USDT", "UNI-USDT", "BTC-USDT"]


def export_recent_candles(days: int = 60, send_discord_flag: bool = True) -> Path:
    base_dir = Path(__file__).resolve().parent
    candles_dir = base_dir / "Data" / "historical_candles"
    out_dir = base_dir / "Data" / "gemini_export"
    out_dir.mkdir(parents=True, exist_ok=True)

    hours_limit = days * 24
    exported_files = []

    print(f"\n[Export] 固定銘柄の直近 {days} 日分 ({hours_limit} 時間足) を抽出中...")

    for sym in FIXED_SYMBOLS:
        clean_sym = sym.replace("-USDT", "").replace("USDT", "").upper()
        # 候補ファイル探索
        cand_file = None
        for name in [f"{sym}_1h.csv", f"{clean_sym}-USDT_1h.csv", f"{clean_sym}_1h.csv"]:
            p = candles_dir / name
            if p.exists():
                cand_file = p
                break

        if not cand_file:
            print(f"  ⚠️ {sym}: ローカル蓄積CSVが見つかりません。スキップします。")
            continue

        try:
            df = pd.read_csv(cand_file)
            if df.empty or "close" not in df.columns:
                continue

            # timestamp を ISO/日時文字列形式に整形
            if pd.to_numeric(df["timestamp"], errors="coerce").notna().all():
                df["datetime_jst"] = pd.to_datetime(df["timestamp"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M")
            else:
                df["datetime_jst"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d %H:%M")

            # 直近N本を抽出
            sub_df = df.tail(hours_limit).copy().reset_index(drop=True)
            
            # Gemini用に分かりやすい列構成に整形
            out_cols = ["datetime_jst", "open", "high", "low", "close", "volume"]
            avail = [c for c in out_cols if c in sub_df.columns]
            export_sub = sub_df[avail]

            out_csv_name = f"{sym}_recent_{days}d.csv"
            out_csv_path = out_dir / out_csv_name
            export_sub.to_csv(out_csv_path, index=False, encoding="utf-8")
            file_kb = out_csv_path.stat().st_size / 1024
            print(f"  ✅ {sym:9s}: {len(export_sub)} 行抽出完了 ({file_kb:.1f} KB) -> {out_csv_name}")
            exported_files.append(out_csv_path)

        except Exception as e:
            print(f"  ❌ {sym} 抽出エラー: {e}")

    if not exported_files:
        print("[Export] 有効なデータが抽出できませんでした。")
        return out_dir

    # コンパクトなZIPアーカイブにまとめる (スマホで1タップ保存可能)
    zip_name = f"bingx_fixed5_recent_{days}days_for_gemini.zip"
    zip_path = out_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in exported_files:
            zf.write(f, arcname=f.name)

    zip_kb = zip_path.stat().st_size / 1024
    print(f"\n📦 Gemini用ZIPアーカイブ作成完了: {zip_path.name} ({zip_kb:.1f} KB)")

    if send_discord_flag:
        discord = send_discord()
        desc = (
            f"📱 **【スマホGemini分析用】固定5銘柄 直近{days}日間 (約{days//30}ヶ月分) OHLCVデータ**\n"
            f"• 期間: 直近 `{days}` 日間 ({hours_limit}本)\n"
            f"• 収録銘柄: `HYPE`, `NEAR`, `ZEC`, `ARB`, `UNI` (+ `BTC`)\n"
            f"• ファイルサイズ: `{zip_kb:.1f} KB` (軽量・Gemini直渡し最適化)\n"
            f"※スマホのDiscordでダウンロードして、そのままGeminiに添付してバックテスト・分析できます。"
        )
        discord.send_file(zip_path, desc)
        print("🚀 Discord (#real3_bngx) へ送信完了しました。")

    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export recent candles for Gemini backtesting")
    parser.add_argument("--days", type=int, default=60, help="Number of days to export (default: 60 = 2 months)")
    parser.add_argument("--no-send", action="store_true", help="Do not send to Discord")
    args = parser.parse_args()

    export_recent_candles(days=args.days, send_discord_flag=(not args.no_send))
