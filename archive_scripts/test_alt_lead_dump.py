import pandas as pd
from pathlib import Path

p_1h = Path(r"d:\python_bitcoin\motiobtc\bingx\bingx5_46\Data\binance_candles_1h\binance_all_crypto_1h.csv")
print("Reading 1h data (2022~2026)...")
df = pd.read_csv(p_1h, usecols=["timestamp", "symbol", "close"])
df["timestamp"] = pd.to_datetime(df["timestamp"])
df = df[df["timestamp"] >= "2022-01-01"].copy()
df.sort_values(by=["symbol", "timestamp"], inplace=True)

# 4時間リターンと24時間リターン
pivot = df.pivot(index="timestamp", columns="symbol", values="close")
ret_4h = pivot.pct_change(4) * 100
ret_24h = pivot.pct_change(24) * 100

down_ratio_4h = (ret_4h < 0).sum(axis=1) / ret_4h.notnull().sum(axis=1)
btc_ret_4h = ret_4h["BTCUSDT"]

# アルト先行下落の定義: 直近4時間でアルトの85%以上がマイナス、かつアルト平均が-3%以上下落しているが、BTCはまだ-1%未満
alt_mean_4h = ret_4h.mean(axis=1)
alt_lead_4h = (down_ratio_4h >= 0.85) & (alt_mean_4h <= -3.0) & (btc_ret_4h >= -1.0)

# その後のBTCとアルトの推移 (次4時間〜次12時間)
btc_next_12h = pivot["BTCUSDT"].pct_change(12).shift(-12) * 100
alt_next_12h = ret_4h.mean(axis=1).rolling(3).sum().shift(-12)

sub = pd.DataFrame({
    "btc_ret_4h": btc_ret_4h,
    "alt_mean_4h": alt_mean_4h,
    "down_ratio_4h": down_ratio_4h,
    "btc_next_12h": btc_next_12h,
    "alt_next_12h": alt_next_12h,
}).loc[alt_lead_4h]

print(f"\n▼ 【アルト先行下落（4時間ウィンドウ）の検出結果】")
print(f"発生回数: {len(sub)} 回")
print(f"発生時のアルト平均4h下落: {sub['alt_mean_4h'].mean():.2f}% (BTCは平均 {sub['btc_ret_4h'].mean():.2f}%)")
print(f"その後の12時間 BTC平均騰落: {sub['btc_next_12h'].mean():+.2f}% (下落確率: {(sub['btc_next_12h'] < 0).mean()*100:.1f}%)")
print(f"その後の12時間 アルト平均騰落: {sub['alt_next_12h'].mean():+.2f}% (下落確率: {(sub['alt_next_12h'] < 0).mean()*100:.1f}%)")
