import sys, os, glob
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

# ==============================================================================
# 時間帯最適化 ウォークフォワード検証
# 過去N ヶ月で最良だった時間帯ウィンドウを選び、翌月(未知データ)に適用した場合の損益を
# 「時間帯フィルターなし(全時間)」「各ウィンドウの平均(ランダム選択相当)」と比較する。
# 集計単位: (A) 全銘柄合算  (B) 銘柄ごと (ボットの個別最適化と同じ粒度)
# ==============================================================================

files = sorted(glob.glob("Data/binance_candles_5m_4y/*.pkl"))
WINDOWS = ['DAWN', 'DAY', 'EU', 'NIGHT']

def get_window(hr):
    if 4 <= hr < 8: return 'DAWN'
    if 9 <= hr < 16: return 'DAY'
    if 16 <= hr < 22: return 'EU'
    if hr >= 22 or hr < 4: return 'NIGHT'
    return 'OTHER'

recs = []
for f in files:
    sym = os.path.basename(f).replace("_5m.pkl", "")
    df = pd.read_pickle(f)
    if 'timestamp' not in df.columns:
        continue
    df['dt'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert('Asia/Tokyo')
    df = df[df['dt'] >= '2024-01-01'].reset_index(drop=True)
    if len(df) < 288 * 60:
        continue
    c = df['close'].astype(float).values; h = df['high'].astype(float).values; l = df['low'].astype(float).values
    hours = df['dt'].dt.hour.values; months = df['dt'].dt.strftime('%Y-%m').values
    n = len(c); P = 288
    s_h = pd.Series(h).rolling(P).max().values; s_l = pd.Series(l).rolling(P).min().values
    vah = s_l + (s_h - s_l) * 0.70; val = s_l + (s_h - s_l) * 0.30
    ema = pd.Series(c).ewm(span=200).mean().values
    for i in range(P + 1, n - 24):
        w = get_window(hours[i])
        if w == 'OTHER':
            continue
        if c[i] > ema[i] and c[i-1] <= vah[i-1] and c[i] > vah[i] and c[i] <= vah[i] * 1.01:
            e = c[i]; pnl = None
            for k in range(1, 25):
                if l[i+k] <= e * 0.98: pnl = -0.0208; break
                if h[i+k] >= e * 1.015: pnl = 0.0142; break
            if pnl is None: pnl = (c[i+24] - e) / e - 0.0008
            recs.append((months[i], sym, 'LONG', w, pnl * 15.0))
        if c[i] < ema[i] and c[i-1] >= val[i-1] and c[i] < val[i] and c[i] >= val[i] * 0.99:
            e = c[i]; pnl = None
            for k in range(1, 25):
                if h[i+k] >= e * 1.02: pnl = -0.0208; break
                if l[i+k] <= e * 0.985: pnl = 0.0142; break
            if pnl is None: pnl = (e - c[i+24]) / e - 0.0008
            recs.append((months[i], sym, 'SHORT', w, pnl * 15.0))

R = pd.DataFrame(recs, columns=['month', 'symbol', 'side', 'window', 'pnl'])
R = R[R['month'] < '2026-11']
months_all = sorted(R['month'].unique())
print(f"トレード数: {len(R)}  期間: {months_all[0]}〜{months_all[-1]}  銘柄数: {R['symbol'].nunique()}")

def walk_forward(grp_keys, lookback):
    """grp_keys: 集計単位 (['side'] or ['side','symbol'])"""
    out = []
    piv = R.groupby(grp_keys + ['month', 'window'])['pnl'].sum().unstack('window').reindex(columns=WINDOWS).fillna(0.0)
    for key, sub in piv.groupby(level=list(range(len(grp_keys)))):
        sub = sub.droplevel(list(range(len(grp_keys)))).reindex(months_all).fillna(0.0)
        for idx in range(lookback, len(months_all)):
            m = months_all[idx]
            past = sub.iloc[idx - lookback: idx].sum()
            best = past.idxmax()
            nxt = sub.iloc[idx]
            out.append({
                'side': key[0] if isinstance(key, tuple) else key,
                'month': m,
                'picked': nxt[best],          # 過去最良ウィンドウを翌月に適用
                'all': nxt.sum(),             # 時間帯フィルターなし
                'avg_one': nxt.mean(),        # ウィンドウを1つランダムに選んだ場合の期待値
                'best_hindsight': nxt.max(),  # 後知恵の上限
                'hit': int(nxt[best] == nxt.max()),
            })
    return pd.DataFrame(out)

for label, keys in [('(A) 全銘柄合算で時間帯を選ぶ', ['side']), ('(B) 銘柄ごとに時間帯を選ぶ (ボット方式)', ['side', 'symbol'])]:
    print("\n" + "=" * 90)
    print(label)
    print("=" * 90)
    for lb in [1, 3, 6]:
        W = walk_forward(keys, lb)
        for side in ['LONG', 'SHORT']:
            s = W[W['side'] == side]
            print(f" lookback={lb}ヶ月 {side:5s} | 最適化選択: {s['picked'].sum():8.2f} | ランダム1枠: {s['avg_one'].sum():8.2f} | "
                  f"フィルタなし: {s['all'].sum():8.2f} | 後知恵上限: {s['best_hindsight'].sum():8.2f} | 的中率: {s['hit'].mean()*100:5.1f}% (偶然=25%)")

# 月ごとのウィンドウ順位の持続性 (先月の順位と今月の順位の相関)
print("\n" + "=" * 90)
print("ウィンドウ順位の月間持続性 (先月と今月のスピアマン相関の平均 / 0=無相関, 1=完全持続)")
print("=" * 90)
for keys, label in [(['side'], '全銘柄合算'), (['side', 'symbol'], '銘柄ごと')]:
    piv = R.groupby(keys + ['month', 'window'])['pnl'].sum().unstack('window').reindex(columns=WINDOWS).fillna(0.0)
    for side in ['LONG', 'SHORT']:
        cors = []
        sub_all = piv.xs(side, level=0)
        groups = [sub_all] if len(keys) == 1 else [g.droplevel(0) for _, g in sub_all.groupby(level=0)]
        for g in groups:
            g = g.reindex(months_all).fillna(0.0)
            for a, b in zip(g.index[:-1], g.index[1:]):
                x, y = g.loc[a].rank(), g.loc[b].rank()
                if x.std() > 0 and y.std() > 0:
                    cors.append(np.corrcoef(x, y)[0, 1])
        print(f" {label:8s} {side:5s}: 平均相関 {np.mean(cors):+.3f} (n={len(cors)})")
