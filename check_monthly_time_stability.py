import sys
import os
import glob
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

# ==============================================================================
# 月別・時間帯別 パフォーマンス安定性検証スクリプト
# 「時間最適化を毎回やると過学習になるか？ 月単位でも一貫した時間帯の傾向があるか？」
# ==============================================================================

files = glob.glob("Data/binance_candles_5m_4y/*.pkl")
if not files:
    print("No pkl files found!")
    sys.exit(1)

print(f"Loading {len(files)} symbols from 4Y dataset...")

# 代表的な上位銘柄 (SOL, DOGE, AVAX, LINK, NEAR 等) で 2024年を検証
target_files = files[:10]

monthly_records = []

for f in target_files:
    sym = os.path.basename(f).replace("_5m.pkl", "")
    df = pd.read_pickle(f)
    if 'timestamp' not in df.columns:
        continue
    df['dt'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert('Asia/Tokyo')
    # 2024年以降
    df = df[df['dt'] >= '2024-01-01'].copy().reset_index(drop=True)
    if len(df) < 288 * 30:
        continue

    # 地合い (BTC の 200EMA または 簡易地合い)
    c = df['close'].astype(float).values
    h = df['high'].astype(float).values
    l = df['low'].astype(float).values
    v = df['volume'].astype(float).values
    hours = df['dt'].dt.hour.values
    months = df['dt'].dt.strftime('%Y-%m').values
    n = len(c)

    # 簡易VP (288本 = 24時間)
    period = 288
    # 24時間ごとのローリング高安で代用 (高速化)
    s_h = pd.Series(h).rolling(period).max().values
    s_l = pd.Series(l).rolling(period).min().values
    vah = s_l + (s_h - s_l) * 0.70
    val = s_l + (s_h - s_l) * 0.30
    poc = s_l + (s_h - s_l) * 0.50

    # 地合い (EMA200)
    ema200 = pd.Series(c).ewm(span=200).mean().values

    # 各トレードのシミュレーション (ロングブレイク & ショートブレイク)
    # BUY地合い: c > ema200
    # SELL地合い: c < ema200

    # 1. ロングブレイク
    for i in range(period + 1, n - 24):
        hr = hours[i]
        mo = months[i]
        is_buy_regime = (c[i] > ema200[i])
        
        # VAH 上抜けブレイク
        if is_buy_regime and (c[i-1] <= vah[i-1]) and (c[i] > vah[i]) and (c[i] <= vah[i] * 1.01):
            entry_px = c[i]
            # 最大24本 (2時間) 保有、SL = entry * 0.98, TP = entry * 1.015
            pnl = 0.0
            for k in range(1, 25):
                cur_h = h[i+k]
                cur_l = l[i+k]
                if cur_l <= entry_px * 0.98: # SL -2%
                    pnl = -0.02 - 0.0008
                    break
                elif cur_h >= entry_px * 1.015: # TP +1.5%
                    pnl = 0.015 - 0.0008
                    break
            else:
                pnl = (c[i+24] - entry_px) / entry_px - 0.0008
            
            monthly_records.append({
                'month': mo,
                'symbol': sym,
                'side': 'BUY_BREAKOUT',
                'hour': hr,
                'pnl': pnl * 15.0 # $15 size
            })

        # 2. ショートブレイク
        is_sell_regime = (c[i] < ema200[i])
        if is_sell_regime and (c[i-1] >= val[i-1]) and (c[i] < val[i]) and (c[i] >= val[i] * 0.99):
            entry_px = c[i]
            pnl = 0.0
            for k in range(1, 25):
                cur_h = h[i+k]
                cur_l = l[i+k]
                if cur_h >= entry_px * 1.02: # SL -2%
                    pnl = -0.02 - 0.0008
                    break
                elif cur_l <= entry_px * 0.985: # TP +1.5%
                    pnl = 0.015 - 0.0008
                    break
            else:
                pnl = (entry_px - c[i+24]) / entry_px - 0.0008

            monthly_records.append({
                'month': mo,
                'symbol': sym,
                'side': 'SELL_BREAKOUT',
                'hour': hr,
                'pnl': pnl * 15.0
            })

df_rec = pd.DataFrame(monthly_records)
print(f"Total simulated trades: {len(df_rec)}")

# 時間帯のカテゴリ分け
def get_window(hr):
    if 4 <= hr < 8:
        return '1_早朝 (04-08h)'
    elif 9 <= hr < 16:
        return '2_東京日中 (09-16h)'
    elif 16 <= hr < 22:
        return '3_欧州 (16-22h)'
    elif hr >= 22 or hr < 4:
        return '4_米国深夜 (22-04h)'
    return 'Other'

df_rec['window'] = df_rec['hour'].apply(get_window)

print("\n" + "="*80)
print("【検証1: BUY地合い ロングブレイク 月別・時間帯別 損益 (USDT)】")
print("="*80)
df_buy = df_rec[df_rec['side'] == 'BUY_BREAKOUT']
pvt_buy = df_buy.pivot_table(index='month', columns='window', values='pnl', aggfunc=['sum', 'count'])
# sum のみ見やすく表示
pvt_buy_sum = df_buy.pivot_table(index='month', columns='window', values='pnl', aggfunc='sum').fillna(0.0)
print(pvt_buy_sum.round(2).to_string())

print("\n" + "="*80)
print("【検証2: SELL地合い ショートブレイク 月別・時間帯別 損益 (USDT)】")
print("="*80)
df_sell = df_rec[df_rec['side'] == 'SELL_BREAKOUT']
pvt_sell_sum = df_sell.pivot_table(index='month', columns='window', values='pnl', aggfunc='sum').fillna(0.0)
print(pvt_sell_sum.round(2).to_string())
