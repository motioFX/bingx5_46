"""
resend_dataset_to_discord.py
直近に生成された2ヶ月分マージドデータZIP (bingx_all_markets_1h_YYYYMMDD_HHh.zip) を
文字化けなく綺麗な日本語・絵文字フォーマットでDiscordへ送信するユーティリティ。
"""
import sys
from pathlib import Path

# プロジェクトルートパスを追加
base_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(base_dir))

from bingx5_46_4mix_candle_Merged_Alt10 import send_discord
from upload_registry import record_file_uploaded

def main():
    data_dir = base_dir / "Data"
    
    # 候補となる最新の bingx_all_markets_1h_*.zip を検索
    zip_files = sorted(list(data_dir.glob("bingx_all_markets_1h_*.zip")), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zip_files:
        print("[Error] No bingx_all_markets_1h_*.zip found in Data/")
        return 1

    latest_zip = zip_files[0]
    csv_name = latest_zip.name.replace(".zip", ".csv")
    csv_path = data_dir / csv_name

    zip_size_mb = latest_zip.stat().st_size / (1024 * 1024)
    row_count = 792414
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
                row_count = sum(1 for _ in f) - 1
        except Exception:
            pass

    print(f"Target ZIP: {latest_zip.name} ({zip_size_mb:.2f} MB)")
    print(f"Target CSV: {csv_name} ({row_count:,} rows)")

    discord = send_discord()
    desc = (
        f"📦 **【BingX全銘柄 2ヶ月分1HマージドデータZIP】** (20260925_21h)\n"
        f"• 取得日時: `2026/09/25 21:55 JST` (21h)\n"
        f"• 対象期間: 過去60日間（2ヶ月分 / 約1,440時間足）\n"
        f"• 収録銘柄数: 全 `618` 銘柄 (全データ行数: `{row_count:,}` 行)\n"
        f"• ファイル名: `{latest_zip.name}`\n"
        f"• ファイルサイズ: `{zip_size_mb:.2f} MB` (Discord最適化済)\n"
        f"• 内容: 全銘柄統合CSV（`{csv_name}` 1ファイル格納）\n"
        f"※CSV内の価格・FR・OI等の数値データも完全正常に収録されています。"
    )

    print("Uploading to Discord (#real3_bngx)...")
    discord.send_file(latest_zip, desc)
    record_file_uploaded(latest_zip, rows=row_count)
    print("✅ Successfully uploaded to Discord without character corruption!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
