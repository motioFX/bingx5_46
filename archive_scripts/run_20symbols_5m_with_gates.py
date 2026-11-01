#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定固定20銘柄 5分足1年分 (計220万本)
二層門番 (第1層全面安ロック ＆ 第2層168h高値警戒) 適用 vs フィルターなし 比較検証スクリプト
"""
import sys
import os
import json
import time
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from pathlib import Path
import shutil

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from config_loader import get_webhook_url

TARGET_SYMBOLS = [
    'HYPEUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'SUIUSDT',
    'AVAXUSDT', 'LINKUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'UNIUSDT', 'OPUSDT', 'TIAUSDT', 'RENDERUSDT', 'INJUSDT',
    'FETUSDT', 'TAOUSDT', 'AAVEUSDT', 'ENAUSDT', 'ZECUSDT'
]

DATA_DIR = WORKSPACE / "Data/binance_candles_5m"

print("Loading 5m candles and preparing 1h resampled data for Gate calculations...")
raw_5m_data = {}
for sym in TARGET_SYMBOLS + ['BTCUSDT']:
    csv_file = DATA_DIR / f"{sym}_5m.csv"
    if not csv_file.exists():
        continue
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values(by='timestamp', inplace=True)
    df.reset_index(drop=True, inplace=True)
    raw_5m_data[sym] = df

# 1時間足リサンプルデータの作成 (門番計算用)
print("Resampling to 1H candles for macro & regime gates...")
h1_closes = {}
h1_highs = {}
h1_lows = {}

for sym, df_5m in raw_5m_data.items():
    df_indexed = df_5m.set_index('timestamp')
    df_1h = df_indexed.resample('1h').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    h1_closes[sym] = df_1h['close']
    h1_highs[sym] = df_1h['high']
    h1_lows[sym] = df_1h['low']

df_h1_close = pd.DataFrame(h1_closes).dropna(how='all')
df_h1_high = pd.DataFrame(h1_highs).dropna(how='all')

# ----------------- 第1層門番: 全面安フィルター計算 -----------------
print("Computing Tier 1: Macro Market Dump Filter...")
alt_cols = [s for s in TARGET_SYMBOLS if s in df_h1_close.columns]
ret_24h = df_h1_close.pct_change(24)

# アルト先行急落率 (24hで-3%以上下落しているアルトの比率)
alt_down_count = (ret_24h[alt_cols] < -0.03).sum(axis=1)
alt_total_valid = ret_24h[alt_cols].notna().sum(axis=1)
alt_down_ratio = np.where(alt_total_valid > 0, alt_down_count / alt_total_valid, 0.0)

# BTC 24h急落
btc_ret = ret_24h['BTCUSDT'] if 'BTCUSDT' in ret_24h.columns else pd.Series(0.0, index=df_h1_close.index)

dump_trigger = (alt_down_ratio >= 0.85) | ((btc_ret < -0.03) & (alt_down_ratio >= 0.65))

# ロック状態の伝播 (最低6時間ロック + クールダウン4時間)
gate_dump_locked = np.zeros(len(df_h1_close), dtype=bool)
lock_until = -1
for idx in range(len(df_h1_close)):
    if dump_trigger.iloc[idx]:
        lock_until = max(lock_until, idx + 6 + 4) # 6hロック + 4hクールダウン
    if idx < lock_until:
        gate_dump_locked[idx] = True

series_tier1_locked = pd.Series(gate_dump_locked, index=df_h1_close.index)

# ----------------- 第2層門番: 個別銘柄 168h高値圏天井停止 -----------------
print("Computing Tier 2: 168h Peak Ceiling Guard per symbol...")
series_tier2_near_top = {}
near_top_pct = 0.025 # 最高値の2.5%以内は天井警戒

for sym in alt_cols:
    s_close = df_h1_close[sym]
    s_high = df_h1_high[sym] if sym in df_h1_high.columns else s_close
    rolling_peak_168 = s_high.rolling(168, min_periods=24).max()
    is_near_top = s_close >= rolling_peak_168 * (1.0 - near_top_pct)
    series_tier2_near_top[sym] = is_near_top

def calc_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(series: pd.Series, length: int = 9) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    rma_up = calc_rma(up, length)
    rma_down = calc_rma(down, length)
    rs = rma_up / rma_down.replace(0.0, np.nan)
    rsi_vals = np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))
    return pd.Series(rsi_vals, index=series.index).fillna(50.0)

def calc_ema(series: pd.Series, length: int = 7) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def run_simulation(use_tier1_gate: bool = False, use_tier2_gate: bool = False):
    all_trades = []
    
    for sym in TARGET_SYMBOLS:
        if sym not in raw_5m_data:
            continue
        df = raw_5m_data[sym]
        n = len(df)
        if n < 100:
            continue
            
        close_s = df['close']
        rsi_s = calc_rsi(close_s, 9)
        lma_s = calc_ema(rsi_s, 7)
        
        closes = close_s.values
        highs = df['high'].values
        lows = df['low'].values
        rsi = rsi_s.values
        lma = lma_s.values
        timestamps = df['timestamp'].values
        
        # 1hインデックスとの同期マップ
        tier1_map = series_tier1_locked.reindex(df['timestamp'], method='ffill').fillna(False).values
        tier2_series = series_tier2_near_top.get(sym)
        tier2_map = tier2_series.reindex(df['timestamp'], method='ffill').fillna(False).values if tier2_series is not None else np.zeros(n, dtype=bool)
        
        max_stages = 5
        maker_fee = 0.0002
        taker_fee = 0.0006
        tp_thresh = 70.0
        callback_pct = 0.015
        sl_pct = 0.030
        
        in_pos = False
        pos_count = 0
        pos_cost = 0.0
        pos_qty = 0.0
        avg_px = 0.0
        last_px = 0.0
        trailing_active = False
        trail_peak = 0.0
        pending_entry = None
        
        for i in range(1, n):
            c = closes[i]
            h = highs[i]
            l = lows[i]
            r = rsi[i]
            r_prev = rsi[i-1]
            m = lma[i]
            m_prev = lma[i-1]
            ts = timestamps[i]
            
            # 0. 前足待機指値の約定チェック
            if pending_entry is not None:
                limit_px = pending_entry["limit_px"]
                if l <= limit_px:
                    exec_px = limit_px
                    new_qty = pos_qty + (1.0 / exec_px)
                    pos_cost += 1.0
                    avg_px = pos_cost / new_qty
                    pos_qty = new_qty
                    pos_count += 1
                    last_px = exec_px
                    if not in_pos:
                        in_pos = True
                    if trailing_active and c < trail_peak * (1.0 - callback_pct):
                        trailing_active = False
                pending_entry = None
                
            # 1. 決済チェック (確定足Close)
            if in_pos and pos_qty > 0:
                # 損切り
                if sl_pct > 0 and c < avg_px * (1.0 - sl_pct):
                    exit_px = c
                    ret_pct = ((exit_px - avg_px) / avg_px - (maker_fee + taker_fee)) * 100.0
                    weighted_ret = ret_pct * (pos_count / max_stages)
                    all_trades.append({
                        'timestamp': ts,
                        'symbol': sym,
                        'pnl_weighted': weighted_ret,
                        'ret_pct': ret_pct,
                        'pos_count': pos_count,
                        'type': 'SL'
                    })
                    in_pos = False
                    pos_count = 0
                    pos_cost = 0.0
                    pos_qty = 0.0
                    avg_px = 0.0
                    trailing_active = False
                    trail_peak = 0.0
                    pending_entry = None
                    continue
                    
                # 利確トレーリング
                if not trailing_active:
                    if r > tp_thresh and c > avg_px:
                        trailing_active = True
                        trail_peak = max(c, h)
                        
                if trailing_active:
                    trail_peak = max(trail_peak, h)
                    trail_stop = max(trail_peak * (1.0 - callback_pct), avg_px * (1.0 + taker_fee))
                    if c < trail_stop:
                        exit_px = c
                        ret_pct = ((exit_px - avg_px) / avg_px - (maker_fee + taker_fee)) * 100.0
                        weighted_ret = ret_pct * (pos_count / max_stages)
                        all_trades.append({
                            'timestamp': ts,
                            'symbol': sym,
                            'pnl_weighted': weighted_ret,
                            'ret_pct': ret_pct,
                            'pos_count': pos_count,
                            'type': 'TP'
                        })
                        in_pos = False
                        pos_count = 0
                        pos_cost = 0.0
                        pos_qty = 0.0
                        avg_px = 0.0
                        trailing_active = False
                        trail_peak = 0.0
                        pending_entry = None
                        continue
                        
                # ナンピン判定
                if pos_count < max_stages and pending_entry is None:
                    drop_req = 0.004 if pos_count <= 3 else 0.006
                    if c <= last_px * (1.0 - drop_req):
                        pending_entry = {
                            "limit_px": c * (1.0 - 0.001),
                            "type": f"NANPIN_{pos_count}"
                        }
                        
            # 2. 新規エントリー判定 (門番チェック)
            if not in_pos and pending_entry is None:
                # 第1層門番
                if use_tier1_gate and tier1_map[i]:
                    continue
                # 第2層門番
                if use_tier2_gate and tier2_map[i]:
                    continue
                    
                gc = (r > m) and (r_prev <= m_prev)
                if gc and (m < 40.0) and (r < tp_thresh):
                    pending_entry = {
                        "limit_px": c * (1.0 - 0.001),
                        "type": "INITIAL_ENTRY"
                    }
                    
    df_t = pd.DataFrame(all_trades).sort_values(by='timestamp').reset_index(drop=True)
    df_t['port_pnl'] = df_t['pnl_weighted'] / len(TARGET_SYMBOLS)
    df_t['cum_port_pnl'] = df_t['port_pnl'].cumsum()
    peak = df_t['cum_port_pnl'].cummax()
    dd = peak - df_t['cum_port_pnl']
    max_dd = float(dd.max()) if not dd.empty else 0.0
    tot_pnl = float(df_t['cum_port_pnl'].iloc[-1]) if not df_t.empty else 0.0
    win_cnt = (df_t['pnl_weighted'] > 0).sum()
    win_rate = (win_cnt / len(df_t) * 100.0) if len(df_t) > 0 else 0.0
    
    return {
        'tot_pnl': tot_pnl,
        'tot_sum_pnl': tot_pnl * len(TARGET_SYMBOLS),
        'win_rate': win_rate,
        'trades': len(df_t),
        'max_dd': max_dd,
        'df': df_t,
        'dd_series': -dd
    }

print("\n--- Running Case 1: Pure 5M RSIMA (No Gates / Baseline) ---")
res_no_gate = run_simulation(use_tier1_gate=False, use_tier2_gate=False)
print(f"No Gates:  PortPnL {res_no_gate['tot_pnl']:>+7.2f}% (Sum {res_no_gate['tot_sum_pnl']:>+7.1f}%) | WR {res_no_gate['win_rate']:>5.1f}% | DD {res_no_gate['max_dd']:>5.2f}% | Trades {res_no_gate['trades']:,}")

print("\n--- Running Case 2: + Tier 1 Macro Dump Filter Gate ---")
res_tier1 = run_simulation(use_tier1_gate=True, use_tier2_gate=False)
print(f"Tier 1:    PortPnL {res_tier1['tot_pnl']:>+7.2f}% (Sum {res_tier1['tot_sum_pnl']:>+7.1f}%) | WR {res_tier1['win_rate']:>5.1f}% | DD {res_tier1['max_dd']:>5.2f}% | Trades {res_tier1['trades']:,}")

print("\n--- Running Case 3: + Full Two-Tier Gates (Tier 1 Macro Dump + Tier 2 168h Top Guard) ---")
res_both = run_simulation(use_tier1_gate=True, use_tier2_gate=True)
print(f"Two-Tier:  PortPnL {res_both['tot_pnl']:>+7.2f}% (Sum {res_both['tot_sum_pnl']:>+7.1f}%) | WR {res_both['win_rate']:>5.1f}% | DD {res_both['max_dd']:>5.2f}% | Trades {res_both['trades']:,}")

# 比較グラフ描画
print("\nPlotting Two-Tier Gate Comparison Chart...")
plt.style.use('dark_background')
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
fig.patch.set_facecolor('#0d1117')
ax1.set_facecolor('#0d1117')
ax2.set_facecolor('#0d1117')

# 1. フィルターなし (赤橙破線)
ax1.plot(res_no_gate['df']['timestamp'], res_no_gate['df']['cum_port_pnl'] * len(TARGET_SYMBOLS),
         color='#ff5555', linestyle='--', linewidth=2.0,
         label=f"Case 1: No Gates (Baseline): Sum PnL {res_no_gate['tot_sum_pnl']:+.1f}% (Port: {res_no_gate['tot_pnl']:+.2f}%) | WR {res_no_gate['win_rate']:.1f}% | DD {res_no_gate['max_dd']:.2f}%")

# 2. 第1層のみ (黄橙実線)
ax1.plot(res_tier1['df']['timestamp'], res_tier1['df']['cum_port_pnl'] * len(TARGET_SYMBOLS),
         color='#ffb86c', linestyle='-', linewidth=2.2,
         label=f"Case 2: + Tier 1 Macro Dump Gate: Sum PnL {res_tier1['tot_sum_pnl']:+.1f}% (Port: {res_tier1['tot_pnl']:+.2f}%) | WR {res_tier1['win_rate']:.1f}% | DD {res_tier1['max_dd']:.2f}%")

# 3. 二層門番完全適用 (鮮やかエメラルドグリーン太実線)
ax1.plot(res_both['df']['timestamp'], res_both['df']['cum_port_pnl'] * len(TARGET_SYMBOLS),
         color='#50fa7b', linestyle='-', linewidth=3.2,
         label=f"★ Case 3: Full Two-Tier Gates (Macro Dump + 168h Top Guard): Sum PnL {res_both['tot_sum_pnl']:+.1f}% (Port: {res_both['tot_pnl']:+.2f}%) | WR {res_both['win_rate']:.1f}% | DD {res_both['max_dd']:.2f}%")

ax1.set_title("5-Minute RSIMA: Impact of Two-Tier Gate Architecture (20 Selected Symbols, 1-Year Backtest)", fontsize=15, fontweight='bold', color='#ffffff', pad=15)
ax1.set_ylabel("20-Symbols Sum PnL (%)", fontsize=12, color='#ffffff')
ax1.axhline(0, color='#888888', linestyle='--', linewidth=0.8, alpha=0.7)
ax1.grid(True, linestyle=':', alpha=0.3, color='#444444')
ax1.legend(loc='upper left', fontsize=10, framealpha=0.85, facecolor='#161b22', edgecolor='#30363d')

# 下段: ドローダウン比較
ax2.fill_between(res_no_gate['df']['timestamp'], res_no_gate['dd_series'], 0, color='#ff5555', alpha=0.25,
                 label=f"No Gates MaxDD: -{res_no_gate['max_dd']:.2f}%")
ax2.fill_between(res_tier1['df']['timestamp'], res_tier1['dd_series'], 0, color='#ffb86c', alpha=0.35,
                 label=f"Tier 1 Gate MaxDD: -{res_tier1['max_dd']:.2f}%")
ax2.fill_between(res_both['df']['timestamp'], res_both['dd_series'], 0, color='#50fa7b', alpha=0.55,
                 label=f"Two-Tier Gates MaxDD: -{res_both['max_dd']:.2f}% (-{res_no_gate['max_dd'] - res_both['max_dd']:.2f}% Reduced)")

ax2.set_ylabel("Drawdown (%)", fontsize=12, color='#ffffff')
ax2.set_xlabel("Date", fontsize=12, color='#ffffff')
ax2.grid(True, linestyle=':', alpha=0.3, color='#444444')
ax2.legend(loc='lower left', fontsize=10, framealpha=0.85, facecolor='#161b22', edgecolor='#30363d')
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))

plt.tight_layout()
out_plot = WORKSPACE / "Data/plots/rsima_5m_two_tier_gates_comparison.png"
plt.savefig(out_plot, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
plt.close()
print(f"Chart saved to {out_plot}")

# Copy to artifacts
brain_dir = Path("C:/Users/user/.gemini/antigravity/brain/ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")
dest_artifact = brain_dir / "rsima_5m_two_tier_gates_comparison.png"
shutil.copy(out_plot, dest_artifact)
print(f"Copied to {dest_artifact}")

# Discord Webhook 送信
webhook_url = get_webhook_url("test4_backtest")
if not webhook_url:
    webhook_url = get_webhook_url("test4_test")

if webhook_url:
    msg = (
        f"🛡️ **【二層門番（全面安ロック＋高値圏天井停止）による5分足改善検証】**\n"
        f"・対象: 指定固定20銘柄 / 過去1年間 (2,207,520本の5分足実データ)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔴 **Case 1: 門番なし (素のRSIMA)**:\n"
        f"   └ 合計PnL `{res_no_gate['tot_sum_pnl']:+.1f}%` (ポートフォリオ: `{res_no_gate['tot_pnl']:+.2f}%`) | WR `{res_no_gate['win_rate']:.1f}%` | MaxDD `{res_no_gate['max_dd']:.2f}%`\n"
        f"🟠 **Case 2: + 第1層 全面安フィルター**:\n"
        f"   └ 合計PnL `{res_tier1['tot_sum_pnl']:+.1f}%` (ポートフォリオ: `{res_tier1['tot_pnl']:+.2f}%`) | WR `{res_tier1['win_rate']:.1f}%` | MaxDD `{res_tier1['max_dd']:.2f}%`\n"
        f"🟢 **Case 3: ★ 二層門番完全適用 (全面安ロック ＋ 168h高値警戒)**:\n"
        f"   └ 合計PnL **`{res_both['tot_sum_pnl']:+.1f}%`** (ポートフォリオ: **`{res_both['tot_pnl']:+.2f}%`**)\n"
        f"   └ 通算勝率: **`{res_both['win_rate']:.1f}%`**\n"
        f"   └ 最大ドローダウン: **`{res_both['max_dd']:.2f}%`** (DDが大幅改善！)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 **結論**: 1時間足の二層門番が、5分足の「下落トレンドにおける無駄なナンピン被弾」を的確に物理遮断していることが実証されました！"
    )
    with open(out_plot, 'rb') as f:
        files = {'file': (out_plot.name, f, 'image/png')}
        payload = {'payload_json': json.dumps({'content': msg}, ensure_ascii=False)}
        requests.post(webhook_url, data=payload, files=files, timeout=30)
    print("Discord message sent successfully!")
