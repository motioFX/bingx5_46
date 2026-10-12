#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最適化バックテスト累積PnL比較チャート生成スクリプト (修正版)
optimize_rsima_sl_pct.py の正規ロジック（ts_to_idx & 確定足Close損切り）と完全一致
"""
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from pathlib import Path
from optimize_dump_filter_thresholds import compute_filter_states

DATA_PATH = Path('Data/binance_candles_1h/binance_all_crypto_1h.csv')
print(f"Loading data from {DATA_PATH}...")
df_all = pd.read_csv(DATA_PATH)
df_all['timestamp'] = pd.to_datetime(df_all['timestamp'])
df_all = df_all[df_all['timestamp'] >= '2022-01-01'].copy()
df_all.sort_values(by=['symbol', 'timestamp'], inplace=True)

pivot_close = df_all.pivot(index='timestamp', columns='symbol', values='close')
pivot_low = df_all.pivot(index='timestamp', columns='symbol', values='low')
pivot_high = df_all.pivot(index='timestamp', columns='symbol', values='high')

timestamps = pivot_close.index
n_bars = len(timestamps)
ts_to_idx = {ts: idx for idx, ts in enumerate(timestamps)}

filter_mask = compute_filter_states(
    alt_lead_down_ratio=0.90, alt_lead_drop_pct=-3.0,
    btc_dump_drop_pct=-3.0, btc_dump_down_ratio=0.70,
    alt_recovery_ratio=0.50, alt_rebound_pct=1.5,
    min_lock_hours=6, cooldown_hours=4
)

eval_symbols = ['SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'NEARUSDT', 'AVAXUSDT', 'LINKUSDT', 'UNIUSDT', 'ARBUSDT', 'OPUSDT', 'APTUSDT']

def calc_rsi_series(series: pd.Series, length: int = 9) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    alpha = 1.0 / length
    rma_up = up.ewm(alpha=alpha, adjust=False).mean()
    rma_down = down.ewm(alpha=alpha, adjust=False).mean()
    rs = rma_up / rma_down.replace(0.0, np.nan)
    return np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))

cached_sym_data = {}
for sym in eval_symbols:
    if sym not in pivot_close.columns:
        continue
    c_s = pivot_close[sym].dropna()
    if len(c_s) < 100:
        continue
    rsi = calc_rsi_series(c_s, 9)
    lma = pd.Series(rsi, index=c_s.index).ewm(span=7, adjust=False).mean().values
    cached_sym_data[sym] = {
        'closes': c_s.values,
        'highs': pivot_high[sym].loc[c_s.index].values,
        'lows': pivot_low[sym].loc[c_s.index].values,
        'rsi': rsi,
        'lma': lma,
        'timestamps': c_s.index
    }

def simulate(sl_pct: float):
    trades = []
    tp_thresh = 70.0
    callback_pct = 0.015
    maker_fee = 0.0002
    taker_fee = 0.0006

    for sym, data in cached_sym_data.items():
        c_arr = data['closes']
        h_arr = data['highs']
        l_arr = data['lows']
        rsi = data['rsi']
        lma = data['lma']
        sym_ts = data['timestamps']
        in_pos = False
        entry_idx = 0
        entry_px = 0.0
        entry_ts = None
        trailing_active = False
        trail_peak = 0.0

        for k in range(1, len(c_arr)):
            c = c_arr[k]
            h = h_arr[k]
            l = l_arr[k]
            r = rsi[k]
            r_prev = rsi[k-1]
            m = lma[k]
            m_prev = lma[k-1]
            cur_ts = sym_ts[k]

            gc = (r > m) and (r_prev <= m_prev)

            if not in_pos:
                if gc and (m < 40.0) and (r < 70.0):
                    in_pos = True
                    entry_idx = ts_to_idx[cur_ts] # 正しいタイムスタンプインデックス
                    entry_px = c * (1.0 - 0.001)  # 指値
                    entry_ts = cur_ts
                    trailing_active = False
                    trail_peak = 0.0
            else:
                # 確定足終値判定 Close によるスマート損切り
                if sl_pct > 0 and c < entry_px * (1.0 - sl_pct):
                    exit_px = c
                    pnl = (exit_px - entry_px) / entry_px * 100.0 - (maker_fee + taker_fee) * 100.0
                    trades.append({
                        'symbol': sym,
                        'entry_ts': entry_ts,
                        'entry_idx': entry_idx,
                        'exit_ts': cur_ts,
                        'pnl': pnl,
                        'exit_type': 'SL'
                    })
                    in_pos = False
                else:
                    # 建値以上での高値追従利確 (Trailing Take-Profit)
                    if not trailing_active:
                        if r > tp_thresh and c > entry_px:
                            trailing_active = True
                            trail_peak = max(c, h)
                    if trailing_active:
                        trail_peak = max(trail_peak, h)
                        trail_stop = trail_peak * (1.0 - callback_pct)
                        if c < trail_stop:
                            exit_px = c
                            pnl = (exit_px - entry_px) / entry_px * 100.0 - (maker_fee + taker_fee) * 100.0
                            trades.append({
                                'symbol': sym,
                                'entry_ts': entry_ts,
                                'entry_idx': entry_idx,
                                'exit_ts': cur_ts,
                                'pnl': pnl,
                                'exit_type': 'TP'
                            })
                            in_pos = False

    df = pd.DataFrame(trades).sort_values(by='exit_ts').reset_index(drop=True)
    return df

print("Simulating strategies with exact logic...")
df_base_raw = simulate(0.025)
df_opt_raw = simulate(0.030)

# 1. フィルターなし (sl=2.5%)
df_base = df_base_raw.copy()
df_base['cum_pnl'] = df_base['pnl'].cumsum()

# 2. フィルターあり (sl=2.5%)
mask_25 = ~filter_mask[df_base_raw['entry_idx'].values]
df_filt25 = df_base_raw[mask_25].copy().reset_index(drop=True)
df_filt25['cum_pnl'] = df_filt25['pnl'].cumsum()

# 3. 最適値: フィルターあり (sl=3.0%)
mask_30 = ~filter_mask[df_opt_raw['entry_idx'].values]
df_opt = df_opt_raw[mask_30].copy().reset_index(drop=True)
df_opt['cum_pnl'] = df_opt['pnl'].cumsum()

final_base_pnl = df_base['cum_pnl'].iloc[-1]
final_filt_pnl = df_filt25['cum_pnl'].iloc[-1]
final_opt_pnl = df_opt['cum_pnl'].iloc[-1]

print(f"Final Base PnL:  {final_base_pnl:+.1f}%")
print(f"Final Filt PnL:  {final_filt_pnl:+.1f}%")
print(f"Final Opt PnL:   {final_opt_pnl:+.1f}%")

# ドローダウン計算関数
def calc_dd(cum_series):
    peak = np.maximum.accumulate(cum_series)
    return cum_series - peak

dd_base = calc_dd(df_base['cum_pnl'].values)
dd_filt = calc_dd(df_filt25['cum_pnl'].values)
dd_opt = calc_dd(df_opt['cum_pnl'].values)

max_dd_base = abs(min(dd_base))
max_dd_filt = abs(min(dd_filt))
max_dd_opt = abs(min(dd_opt))

# グラフ描画
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={'height_ratios': [2.5, 1]})
plt.style.use('dark_background')

# 上段: 資産曲線
ax1.plot(df_base['exit_ts'], df_base['cum_pnl'], color='#ff5555', linestyle='--', linewidth=1.5, alpha=0.8, label=f'Baseline (No Filter, SL 2.5%): PnL {final_base_pnl:+.1f}% (MaxDD: {max_dd_base:.1f}%)')
ax1.plot(df_filt25['exit_ts'], df_filt25['cum_pnl'], color='#00d2ff', linestyle='-', linewidth=1.8, alpha=0.9, label=f'Macro Dump Filter (SL 2.5%): PnL {final_filt_pnl:+.1f}% (MaxDD: {max_dd_filt:.1f}%)')
ax1.plot(df_opt['exit_ts'], df_opt['cum_pnl'], color='#00ff88', linestyle='-', linewidth=2.5, label=f'🏆 Optimal (Macro Filter + SL 3.0%): PnL {final_opt_pnl:+.1f}% (MaxDD: {max_dd_opt:.1f}%)')

ax1.set_title('Macro Market Dump Filter & RSIMA Stop Loss Optimization (2022 - 2026)', fontsize=15, fontweight='bold', pad=12, color='white')
ax1.set_ylabel('Cumulative PnL (%)', fontsize=12, color='white')
ax1.grid(True, linestyle=':', alpha=0.3, color='#888888')
ax1.legend(loc='upper left', fontsize=11, framealpha=0.8)
ax1.axhline(0, color='gray', linestyle='-', linewidth=0.8, alpha=0.5)

# 下段: ドローダウン推移
ax2.fill_between(df_base['exit_ts'], dd_base, 0, color='#ff5555', alpha=0.25, label=f'Baseline MaxDD: -{max_dd_base:.1f}%')
ax2.fill_between(df_opt['exit_ts'], dd_opt, 0, color='#00ff88', alpha=0.5, label=f'Optimal MaxDD: -{max_dd_opt:.1f}% (-{max_dd_base - max_dd_opt:.1f}% Reduced)')
ax2.set_ylabel('Drawdown (%)', fontsize=11, color='white')
ax2.set_xlabel('Date', fontsize=11, color='white')
ax2.grid(True, linestyle=':', alpha=0.3, color='#888888')
ax2.legend(loc='lower left', fontsize=10, framealpha=0.8)

plt.tight_layout()
out_plot = Path('Data/plots/pnl_optimization_comparison.png')
out_plot.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(out_plot, dpi=150)
print(f"Saved corrected comparison plot to {out_plot}")
