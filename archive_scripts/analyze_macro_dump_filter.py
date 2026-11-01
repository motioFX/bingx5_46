import pandas as pd
import numpy as np
from pathlib import Path
from show_symbol_status import FIXED_SYMBOLS

# 日足マスターデータの読み込み
data_path = Path(__file__).resolve().parent / "Data" / "daily_candles" / "bingx_all_markets_1d.csv"
print(f"Loading {data_path}...")
df = pd.read_csv(data_path)

# 日付型変換
df["timestamp"] = pd.to_datetime(df["timestamp"])
df.sort_values(by=["symbol", "timestamp"], inplace=True)

# 前日比リターン（%）の計算: (close - prev_close) / prev_close * 100
df["prev_close"] = df.groupby("symbol")["close"].shift(1)
df["return_pct"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100.0
df["is_down"] = df["return_pct"] < 0.0

# 対象銘柄: 主要20銘柄 + BTC
target_symbols = list(FIXED_SYMBOLS) + ["BTC-USDT"]
df_targets = df[df["symbol"].isin(target_symbols)].copy()

# 日ごとの集計（ピボット）
# 各日における:
# 1. 銘柄総数 (count)
# 2. 下落銘柄数 (down_count)
# 3. 下落比率 (down_ratio = down_count / count)
# 4. 平均リターン (mean_return)
# 5. 中央値リターン (median_return)
daily_stats = df_targets.groupby("timestamp").agg(
    total_symbols=("symbol", "count"),
    down_count=("is_down", "sum"),
    mean_return=("return_pct", "mean"),
    median_return=("return_pct", "median"),
).reset_index()

daily_stats["down_ratio"] = daily_stats["down_count"] / daily_stats["total_symbols"]

# BTC単体のリターンも結合
btc_df = df[df["symbol"] == "BTC-USDT"][["timestamp", "return_pct", "close"]].rename(
    columns={"return_pct": "btc_return_pct", "close": "btc_close"}
)
daily_stats = pd.merge(daily_stats, btc_df, on="timestamp", how="left")

# 翌日の平均リターン (Next Day Return)
daily_stats["next_day_mean_return"] = daily_stats["mean_return"].shift(-1)
daily_stats["next_day_median_return"] = daily_stats["median_return"].shift(-1)
daily_stats["next_day_btc_return"] = daily_stats["btc_return_pct"].shift(-1)

# 翌3日間の累積リターン
daily_stats["next_3d_mean_return"] = daily_stats["mean_return"].rolling(3).sum().shift(-3)

print("=" * 80)
print(f"📊 【市場全体の全面安（マーケットワイド・ダンプ）日足分析】")
print(f"期間: {daily_stats['timestamp'].min().strftime('%Y-%m-%d')} 〜 {daily_stats['timestamp'].max().strftime('%Y-%m-%d')} (計 {len(daily_stats)} 日間)")
print(f"対象: 主要20銘柄 + BTC-USDT (計21銘柄)")
print("=" * 80)

# 閾値別の発生日数と翌日挙動
thresholds = [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00]

print("\n▼ 【下落比率（下落銘柄割合）別の発生状況と翌日リターン】")
print(f"{'下落比率閾値':12s} | {'発生日数':8s} | {'発生率':6s} | {'当日の平均下落率':14s} | {'翌日平均リターン':14s} | {'翌日も下落の確率'}")
print("-" * 80)

for th in thresholds:
    sub = daily_stats[daily_stats["down_ratio"] >= th]
    days = len(sub)
    pct = (days / len(daily_stats)) * 100
    day_ret = sub["mean_return"].mean()
    next_ret = sub["next_day_mean_return"].mean()
    next_down_prob = (sub["next_day_mean_return"] < 0).mean() * 100

    print(f">= {th*100:4.0f}% 下落  | {days:5d} 日 | {pct:5.1f}% | {day_ret:+12.2f}% | {next_ret:+12.2f}% | {next_down_prob:6.1f}%")

print("=" * 80)

# 複合条件の分析: 【BTC急落 ＆ アルト全面安】
print("\n▼ 【BTC急落 ＆ アルト全面安（第1層フィルター候補）の検証】")
print("-" * 80)

for btc_drop_th in [0.0, -1.0, -2.0, -3.0]:
    for alt_ratio_th in [0.75, 0.80, 0.85, 0.90]:
        cond = (daily_stats["down_ratio"] >= alt_ratio_th) & (daily_stats["btc_return_pct"] <= btc_drop_th)
        sub = daily_stats[cond]
        if len(sub) == 0:
            continue
        days = len(sub)
        day_alt = sub["mean_return"].mean()
        day_btc = sub["btc_return_pct"].mean()
        next_alt = sub["next_day_mean_return"].mean()
        next_down_rate = (sub["next_day_mean_return"] < 0).mean() * 100
        next_3d_alt = sub["next_3d_mean_return"].mean()

        print(
            f"BTC <= {btc_drop_th:+.0f}% & アルト >= {alt_ratio_th*100:.0f}%下落 | "
            f"{days:3d}日 ({days/len(daily_stats)*100:4.1f}%) | "
            f"当日アルト:{day_alt:+5.1f}% | 翌日アルト:{next_alt:+5.2f}% (翌日陰線:{next_down_rate:4.1f}%) | 翌3日累積:{next_3d_alt:+5.2f}%"
        )

print("=" * 80)

