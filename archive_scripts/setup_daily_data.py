import os
import shutil
import zipfile
import pandas as pd
from pathlib import Path

downloads_dir = Path(r"C:\Users\user\Downloads")
dest_dir = Path(r"d:\python_bitcoin\motiobtc\bingx\bingx5_46\Data\daily_candles")
dest_dir.mkdir(parents=True, exist_ok=True)

z1 = downloads_dir / "bingx_crypto_1d_part1of2_past_20240107_20250525_20261012_092929.zip"
z2 = downloads_dir / "bingx_crypto_1d_part2of2_recent_20250526_20261012_20261012_092929.zip"

print("1. Copying zip files to Data/daily_candles...")
shutil.copy2(z1, dest_dir / z1.name)
shutil.copy2(z2, dest_dir / z2.name)

print("2. Extracting CSVs...")
with zipfile.ZipFile(z1, "r") as zf:
    csv1_name = zf.namelist()[0]
    zf.extract(csv1_name, dest_dir)

with zipfile.ZipFile(z2, "r") as zf:
    csv2_name = zf.namelist()[0]
    zf.extract(csv2_name, dest_dir)

print(f"3. Reading CSV 1: {csv1_name}...")
df1 = pd.read_csv(dest_dir / csv1_name)
print(f"4. Reading CSV 2: {csv2_name}...")
df2 = pd.read_csv(dest_dir / csv2_name)

print("5. Merging datasets and removing duplicates...")
df_merged = pd.concat([df1, df2], ignore_index=True)
df_merged.drop_duplicates(subset=["timestamp", "symbol"], inplace=True)
df_merged.sort_values(by=["symbol", "timestamp"], inplace=True)

merged_csv_path = dest_dir / "bingx_all_markets_1d.csv"
print(f"6. Saving master daily CSV: {merged_csv_path}...")
df_merged.to_csv(merged_csv_path, index=False)

symbols_count = df_merged["symbol"].nunique()
min_date = df_merged["timestamp"].min()
max_date = df_merged["timestamp"].max()

print("\n=== セットアップ完了 ===")
print(f"保存先: {merged_csv_path}")
print(f"総行数: {len(df_merged):,} 行")
print(f"銘柄数: {symbols_count} 銘柄")
print(f"期間: {min_date} 〜 {max_date}")

# 20銘柄のデータ存在確認
from show_symbol_status import FIXED_SYMBOLS
present_symbols = set(df_merged["symbol"].unique())
found_fixed = [s for s in FIXED_SYMBOLS if s in present_symbols]
btc_present = "BTC-USDT" in present_symbols

print(f"\n主要20銘柄カバー状況: {len(found_fixed)} / {len(FIXED_SYMBOLS)} 銘柄存在")
print(f"BTC-USDT 存在: {btc_present}")
if len(found_fixed) < len(FIXED_SYMBOLS):
    missing = [s for s in FIXED_SYMBOLS if s not in present_symbols]
    print(f"欠落銘柄: {missing}")
