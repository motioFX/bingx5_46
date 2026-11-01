#!/usr/bin/env python3
"""Data folder cleanup utility for bingx5_46.
Organizes flat files into individual/, charts/, and historical_archives/ subdirectories.
"""
import os
import shutil
from pathlib import Path

def cleanup_data_dir(data_dir: Path) -> None:
    if not data_dir.exists():
        print(f"Data directory {data_dir} does not exist.")
        return

    individual_dir = data_dir / "individual"
    charts_dir = data_dir / "charts"
    archives_dir = data_dir / "historical_archives"

    individual_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)
    archives_dir.mkdir(parents=True, exist_ok=True)

    moved_individual = 0
    moved_charts = 0
    moved_archives = 0

    for item in list(data_dir.iterdir()):
        if not item.is_file():
            continue

        name = item.name

        # 1. 個別銘柄マージドCSV (merged_*.csv) -> individual/
        if name.startswith("merged_") and name.endswith(".csv"):
            shutil.move(str(item), str(individual_dir / name))
            moved_individual += 1

        # 2. チャート画像 (*.png) -> charts/
        elif name.endswith(".png"):
            shutil.move(str(item), str(charts_dir / name))
            moved_charts += 1

        # 3. 過去バックナンバー・Bitgetデータ -> historical_archives/
        # ※ historical_all_symbols_merged.csv / .zip は最新マスターなので直下に残す
        elif name.endswith("_Merged_All_Data_Bitget.csv"):
            shutil.move(str(item), str(archives_dir / name))
            moved_archives += 1
        elif "_all_symbols_merged." in name and not name.startswith("historical_"):
            shutil.move(str(item), str(archives_dir / name))
            moved_archives += 1
        elif name.startswith("bingx_all_markets_") or name.startswith("bingx_all_symbols_"):
            shutil.move(str(item), str(archives_dir / name))
            moved_archives += 1
        elif name.startswith("historical_selection_scores"):
            shutil.move(str(item), str(archives_dir / name))
            moved_archives += 1
        elif name in ("fetched_did_note.txt", "control_messages.json", "top10_24h_gainers_history.csv", "top10_24h_gainers_latest.csv"):
            shutil.move(str(item), str(archives_dir / name))
            moved_archives += 1

    print(f"Cleanup finished for {data_dir}:")
    print(f"  Moved {moved_individual} files to individual/")
    print(f"  Moved {moved_charts} files to charts/")
    print(f"  Moved {moved_archives} files to historical_archives/")
    
    remaining = [f.name for f in data_dir.iterdir() if f.is_file()]
    print(f"  Remaining files in {data_dir.name} ({len(remaining)}):")
    for f in sorted(remaining):
        print(f"    - {f}")

if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    # workspace root detection
    if (script_dir / "Data").exists():
        cleanup_data_dir(script_dir / "Data")
    else:
        # direct run
        cleanup_data_dir(Path("Data"))
