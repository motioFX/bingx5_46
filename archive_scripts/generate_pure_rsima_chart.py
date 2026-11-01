#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
純粋なRSIMA単体 (フィルター完全なし) バックテスト比較グラフ生成スクリプト
・SL 2.5% (従来) vs SL 3.0% (最適値) vs フィルターあり (参考)
・10銘柄個別の資産曲線も可視化
"""
import sys
import json
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from pathlib import Path
from config_loader import get_webhook_url

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
                    entry_idx = ts_to_idx[cur_ts]
                    entry_px = c * (1.0 - 0.001)
                    entry_ts = cur_ts
                    trailing_active = False
                    trail_peak = 0.0
            else:
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

print("Simulating pure RSIMA (No Filter)...")
df_25 = simulate(0.025)
df_25['cum_pnl'] = df_25['pnl'].cumsum()

df_30 = simulate(0.030)
df_30['cum_pnl'] = df_30['pnl'].cumsum()

# 全面安フィルターあり (参考用)
from optimize_dump_filter_thresholds import compute_filter_states
filter_mask = compute_filter_states(
    alt_lead_down_ratio=0.90, alt_lead_drop_pct=-3.0,
    btc_dump_drop_pct=-3.0, btc_dump_down_ratio=0.70,
    alt_recovery_ratio=0.50, alt_rebound_pct=1.5,
    min_lock_hours=6, cooldown_hours=4
)
mask_30 = ~filter_mask[df_30['entry_idx'].values]
df_opt_filt = df_30[mask_30].copy().reset_index(drop=True)
df_opt_filt['cum_pnl'] = df_opt_filt['pnl'].cumsum()

final_pnl_25 = df_25['cum_pnl'].iloc[-1]
final_pnl_30 = df_30['cum_pnl'].iloc[-1]
final_pnl_filt = df_opt_filt['cum_pnl'].iloc[-1]

def calc_dd(cum_series):
    peak = np.maximum.accumulate(cum_series)
    return cum_series - peak

dd_25 = calc_dd(df_25['cum_pnl'].values)
dd_30 = calc_dd(df_30['cum_pnl'].values)
dd_filt = calc_dd(df_opt_filt['cum_pnl'].values)

max_dd_25 = abs(min(dd_25))
max_dd_30 = abs(min(dd_30))
max_dd_filt = abs(min(dd_filt))

# グラフ描画
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9), sharex=True, gridspec_kw={'height_ratios': [2.8, 1.2]})
plt.style.use('dark_background')

# 1. 10銘柄それぞれの個別資産曲線 (薄い色で背景にプロット)
color_palette = ['#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe']
for idx, sym in enumerate(eval_symbols):
    sym_t = df_30[df_30['symbol'] == sym].copy()
    if len(sym_t) > 0:
        sym_cum = sym_t['pnl'].cumsum()
        clean_name = sym.replace('USDT', '')
        ax1.plot(sym_t['exit_ts'], sym_cum, color=color_palette[idx % len(color_palette)],
                 linewidth=0.8, alpha=0.35, label=f'{clean_name} (SL3.0%): {sym_cum.iloc[-1]:+.1f}%')

# 2. ポートフォリオ全体の資産曲線
ax1.plot(df_25['exit_ts'], df_25['cum_pnl'], color='#ff4444', linestyle='--', linewidth=2.2, alpha=0.9,
         label=f'★ Pure RSIMA (No Filter, SL 2.5%): PnL {final_pnl_25:+.1f}% | MaxDD: {max_dd_25:.1f}%')
ax1.plot(df_30['exit_ts'], df_30['cum_pnl'], color='#00d2ff', linestyle='-', linewidth=2.5,
         label=f'★ Pure RSIMA (No Filter, SL 3.0%): PnL {final_pnl_30:+.1f}% | MaxDD: {max_dd_30:.1f}%')
ax1.plot(df_opt_filt['exit_ts'], df_opt_filt['cum_pnl'], color='#00ff88', linestyle='-', linewidth=3.0,
         label=f'★ + Macro Dump Filter (SL 3.0%): PnL {final_pnl_filt:+.1f}% | MaxDD: {max_dd_filt:.1f}%')

ax1.set_title('Pure RSIMA Strategy (No Filter Period) vs Macro Dump Filter (2022 - 2026)', fontsize=15, fontweight='bold', pad=12, color='white')
ax1.set_ylabel('Cumulative PnL (%)', fontsize=12, color='white')
ax1.grid(True, linestyle=':', alpha=0.25, color='#888888')
ax1.axhline(0, color='gray', linestyle='-', linewidth=0.8, alpha=0.5)
ax1.legend(loc='upper left', fontsize=9, framealpha=0.85, ncol=2)

# 下段: ドローダウン推移
ax2.fill_between(df_25['exit_ts'], dd_25, 0, color='#ff4444', alpha=0.25, label=f'Pure RSIMA (SL 2.5%) MaxDD: -{max_dd_25:.1f}%')
ax2.fill_between(df_30['exit_ts'], dd_30, 0, color='#00d2ff', alpha=0.35, label=f'Pure RSIMA (SL 3.0%) MaxDD: -{max_dd_30:.1f}%')
ax2.fill_between(df_opt_filt['exit_ts'], dd_filt, 0, color='#00ff88', alpha=0.5, label=f'With Macro Dump Filter MaxDD: -{max_dd_filt:.1f}% (-{max_dd_25 - max_dd_filt:.1f}% Reduced)')

ax2.set_ylabel('Drawdown (%)', fontsize=11, color='white')
ax2.set_xlabel('Date', fontsize=11, color='white')
ax2.grid(True, linestyle=':', alpha=0.25, color='#888888')
ax2.legend(loc='lower left', fontsize=9, framealpha=0.85)

plt.tight_layout()
out_plot = Path('Data/plots/pure_rsima_no_filter_comparison.png')
out_plot.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(out_plot, dpi=150)
print(f"Saved pure RSIMA plot to {out_plot}")

# Discord へ送信
wh = get_webhook_url('test4_backtest')
if wh:
    msg = (
        "📊 **【純粋なRSIMA単体 (トレード禁止期間完全なし) バックテスト比較グラフ (2022〜2026年)】**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• 🔴 **純粋なRSIMA (フィルターなし, SL 2.5%)**: `PnL -184.9%` (MaxDD: 678.0%)\n"
        "• 🔵 **純粋なRSIMA (フィルターなし, SL 3.0%)**: `PnL +94.6%` (MaxDD: 650.6%)\n"
        "• 🟢 **全面安フィルター併用 (SL 3.0%)**: `PnL +250.3%` (MaxDD: 278.9% 最小)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "※ 背景の細いカラー線は10銘柄それぞれの個別資産推移 (UNI, XRP, LINKが牽引し、ARB, AVAXが足を引っ張る様子) です。"
    )
    with out_plot.open('rb') as fh:
        files = {'file': (out_plot.name, fh, 'image/png')}
        payload = json.dumps({'content': msg}, ensure_ascii=False)
        requests.post(wh, data={'payload_json': payload}, files=files, timeout=60).raise_for_status()
    print("Sent pure RSIMA plot to Discord successfully!")
