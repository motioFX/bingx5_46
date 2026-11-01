#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSIMA戦略: ナンピン4回 (合計5ポジション) vs ワンポジ バックテスト詳細比較 ＆ グラフ生成スクリプト
・純粋RSIMA (フィルターなし) ナンピン5枠 (SL 2.5% vs SL 3.0%)
・全面安フィルター併用 ナンピン5枠 (SL 3.0%)
・純粋ワンポジ vs ナンピン5枠の挙動比較
・10銘柄個別パフォーマンス内訳
"""
import sys
import os
import json
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from pathlib import Path

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from config_loader import get_webhook_url

DATA_PATH = WORKSPACE / 'Data/binance_candles_1h/binance_all_crypto_1h.csv'
print(f"Loading data from {DATA_PATH}...")
df_all = pd.read_csv(DATA_PATH)
df_all['timestamp'] = pd.to_datetime(df_all['timestamp'])
df_all = df_all[df_all['timestamp'] >= '2022-01-01'].copy()
df_all.sort_values(by=['symbol', 'timestamp'], inplace=True)

pivot_close = df_all.pivot(index='timestamp', columns='symbol', values='close')
pivot_low = df_all.pivot(index='timestamp', columns='symbol', values='low')
pivot_high = df_all.pivot(index='timestamp', columns='symbol', values='high')

timestamps = pivot_close.index
eval_symbols = ['SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'NEARUSDT', 'AVAXUSDT', 'LINKUSDT', 'UNIUSDT', 'ARBUSDT', 'OPUSDT', 'APTUSDT']

# 全面安フィルター (第1層門番) 計算
print("Calculating Macro Market Dump Filter...")
ret_24h = pivot_close.pct_change(24)
dump_threshold = -0.05
dump_symbols_count = (ret_24h < dump_threshold).sum(axis=1)
total_active = pivot_close.notna().sum(axis=1)
dump_ratio = np.where(total_active > 0, dump_symbols_count / total_active, 0.0)
gate_dump_active = pd.Series(dump_ratio >= 0.35, index=pivot_close.index)

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

def calc_add_pct(pos_count: int) -> float:
    # 段階ナンピン幅: 1〜3回目: 0.4%, 4〜5回目: 0.6%
    if pos_count <= 3:
        return 0.004
    return 0.006

def run_simulation(max_trades: int = 5, sl_pct: float = 0.030, use_macro_filter: bool = False):
    """
    max_trades: 1 ならワンポジ、5 なら初期1+ナンピン4
    """
    initial_equity = 100.0
    trade_size_usdt = initial_equity / max_trades
    maker_fee = 0.0002
    taker_fee = 0.0006
    tp_thresh = 70.0
    callback_pct = 0.015

    all_trades = []
    sym_equity_curves = {}
    sym_stats = {}

    for sym, data in cached_sym_data.items():
        c_arr = data['closes']
        h_arr = data['highs']
        l_arr = data['lows']
        rsi = data['rsi']
        lma = data['lma']
        sym_ts = data['timestamps']
        n = len(c_arr)

        pos_qty = 0.0
        pos_cost = 0.0
        avg_price = 0.0
        pos_count = 0
        pending_entry = None
        trailing_tp_active = False
        trail_peak = 0.0

        cum_realized_pnl = 0.0
        cum_fees = 0.0
        trades = []
        sym_curve = []

        for i in range(1, n):
            c = c_arr[i]
            h = h_arr[i]
            l = l_arr[i]
            r = rsi[i]
            r_prev = rsi[i-1]
            ma_val = lma[i]
            ma_prev = lma[i-1]
            ts = sym_ts[i]

            gc = (r > ma_val) and (r_prev <= ma_prev)

            # 0. 前足の待機指値の約定判定
            if pending_entry is not None:
                limit_px = pending_entry["limit_price"]
                if l <= limit_px:
                    exec_price = limit_px
                    buy_val = trade_size_usdt
                    qty = buy_val / exec_price
                    fee = buy_val * maker_fee
                    cum_fees += fee
                    pos_cost += buy_val
                    pos_qty += qty
                    avg_price = pos_cost / pos_qty
                    pos_count += 1
                pending_entry = None

            # 1. 決済チェック
            if pos_qty > 0:
                # 損切り (確定足終値で avg_price から sl_pct 割れ)
                if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                    sl_price = c
                    sell_val = pos_qty * sl_price
                    fee = sell_val * taker_fee
                    pnl = sell_val - pos_cost - fee
                    cum_realized_pnl += pnl
                    cum_fees += fee
                    trade_info = {
                        'timestamp': ts,
                        'symbol': sym,
                        'pnl': pnl,
                        'pnl_pct': (sl_price - avg_price) / avg_price * 100.0,
                        'pos_count': pos_count,
                        'type': 'STOP_LOSS'
                    }
                    trades.append(trade_info)
                    all_trades.append(trade_info)
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_price = 0.0
                    pos_count = 0
                    trailing_tp_active = False
                    trail_peak = 0.0
                else:
                    # 利確トレーリング
                    if not trailing_tp_active:
                        if r > tp_thresh and c > avg_price:
                            trailing_tp_active = True
                            trail_peak = max(c, h)

                    if trailing_tp_active:
                        trail_peak = max(trail_peak, h)
                        trail_stop = max(trail_peak * (1.0 - callback_pct), avg_price * (1.0 + taker_fee))
                        if c < trail_stop:
                            sell_val = pos_qty * c
                            fee = sell_val * taker_fee
                            pnl = sell_val - pos_cost - fee
                            cum_realized_pnl += pnl
                            cum_fees += fee
                            trade_info = {
                                'timestamp': ts,
                                'symbol': sym,
                                'pnl': pnl,
                                'pnl_pct': (c - avg_price) / avg_price * 100.0,
                                'pos_count': pos_count,
                                'type': 'TAKE_PROFIT'
                            }
                            trades.append(trade_info)
                            all_trades.append(trade_info)
                            pos_qty = 0.0
                            pos_cost = 0.0
                            avg_price = 0.0
                            pos_count = 0
                            trailing_tp_active = False
                            trail_peak = 0.0

            # 2. エントリー / ナンピン判定
            if pos_count < max_trades and pending_entry is None:
                can_enter = False
                entry_type = None
                
                # 門番判定 (新規エントリー時のみブロック)
                gate_blocked = False
                if use_macro_filter and ts in gate_dump_active.index:
                    gate_blocked = bool(gate_dump_active.loc[ts])

                if pos_count == 0:
                    if not gate_blocked and gc and (ma_val < 40.0) and (r < tp_thresh):
                        can_enter = True
                        entry_type = "ENTRY_RSIMA_GC"
                else:
                    # ナンピン判定 (保有中は門番関係なく平均建値から段階ナンピン)
                    add_pct = calc_add_pct(pos_count)
                    if c <= avg_price * (1.0 - add_pct):
                        can_enter = True
                        entry_type = f"NANPIN_RSIMA_{pos_count}"

                if can_enter:
                    limit_price = c * (1.0 - 0.001)
                    pending_entry = {
                        "limit_price": limit_price,
                        "pos_count": pos_count,
                        "entry_type": entry_type
                    }

            unrealized = (pos_qty * c - pos_cost) if pos_qty > 0 else 0.0
            current_eq = initial_equity + cum_realized_pnl + unrealized - cum_fees
            sym_curve.append(current_eq - initial_equity)

        sym_equity_curves[sym] = pd.Series(sym_curve, index=sym_ts[1:])
        trade_cnt = len(trades)
        win_cnt = sum(1 for t in trades if t['pnl'] > 0)
        win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
        final_pnl = float(sym_curve[-1]) if sym_curve else 0.0
        sym_stats[sym] = {
            'pnl': final_pnl,
            'trades': trade_cnt,
            'win_rate': win_rate
        }

    # 10銘柄統合資産曲線の作成 (日時同期)
    df_combined_curves = pd.DataFrame(sym_equity_curves).fillna(method='ffill').fillna(0.0)
    total_curve = df_combined_curves.sum(axis=1) # 合計PnL (USDT / %)
    
    peak = total_curve.cummax()
    dd = peak - total_curve
    max_dd = float(dd.max()) if not dd.empty else 0.0

    df_t = pd.DataFrame(all_trades)
    tot_trades = len(df_t)
    tot_wins = (df_t['pnl'] > 0).sum() if not df_t.empty else 0
    tot_win_rate = (tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
    final_tot_pnl = float(total_curve.iloc[-1]) if not total_curve.empty else 0.0

    # ナンピン回数分布
    nanpin_counts = df_t['pos_count'].value_counts().to_dict() if not df_t.empty else {}

    return {
        'total_pnl': final_tot_pnl,
        'win_rate': tot_win_rate,
        'trade_count': tot_trades,
        'max_dd': max_dd,
        'nanpin_counts': nanpin_counts,
        'total_curve': total_curve,
        'drawdown_curve': -dd,
        'sym_curves': df_combined_curves,
        'sym_stats': sym_stats
    }

print("\n--- Running Scenario 1: Pure RSIMA (Single Position, SL 3.0%, No Filter) ---")
res_single_sl30 = run_simulation(max_trades=1, sl_pct=0.030, use_macro_filter=False)
print(f"Single Pos SL 3.0%: PnL={res_single_sl30['total_pnl']:+.1f}%, WinRate={res_single_sl30['win_rate']:.1f}%, Trades={res_single_sl30['trade_count']}, MaxDD={res_single_sl30['max_dd']:.1f}%")

print("\n--- Running Scenario 2: Pure RSIMA (Nanpin 4 / Total 5 Pos, SL 2.5%, No Filter) ---")
res_nanpin_sl25 = run_simulation(max_trades=5, sl_pct=0.025, use_macro_filter=False)
print(f"Nanpin Pos SL 2.5%: PnL={res_nanpin_sl25['total_pnl']:+.1f}%, WinRate={res_nanpin_sl25['win_rate']:.1f}%, Trades={res_nanpin_sl25['trade_count']}, MaxDD={res_nanpin_sl25['max_dd']:.1f}%")
print(f"  Nanpin distribution: {res_nanpin_sl25['nanpin_counts']}")

print("\n--- Running Scenario 3: Pure RSIMA (Nanpin 4 / Total 5 Pos, SL 3.0%, No Filter) ---")
res_nanpin_sl30 = run_simulation(max_trades=5, sl_pct=0.030, use_macro_filter=False)
print(f"Nanpin Pos SL 3.0%: PnL={res_nanpin_sl30['total_pnl']:+.1f}%, WinRate={res_nanpin_sl30['win_rate']:.1f}%, Trades={res_nanpin_sl30['trade_count']}, MaxDD={res_nanpin_sl30['max_dd']:.1f}%")
print(f"  Nanpin distribution: {res_nanpin_sl30['nanpin_counts']}")

print("\n--- Running Scenario 4: With Macro Dump Filter (Nanpin 4 / Total 5 Pos, SL 3.0%) ---")
res_nanpin_gate_sl30 = run_simulation(max_trades=5, sl_pct=0.030, use_macro_filter=True)
print(f"Gate+Nanpin SL 3.0%: PnL={res_nanpin_gate_sl30['total_pnl']:+.1f}%, WinRate={res_nanpin_gate_sl30['win_rate']:.1f}%, Trades={res_nanpin_gate_sl30['trade_count']}, MaxDD={res_nanpin_gate_sl30['max_dd']:.1f}%")
print(f"  Nanpin distribution: {res_nanpin_gate_sl30['nanpin_counts']}")

print("\n--- Running Scenario 5: With Macro Dump Filter (Single Position, SL 3.0%) ---")
res_single_gate_sl30 = run_simulation(max_trades=1, sl_pct=0.030, use_macro_filter=True)
print(f"Gate+Single SL 3.0%: PnL={res_single_gate_sl30['total_pnl']:+.1f}%, WinRate={res_single_gate_sl30['win_rate']:.1f}%, Trades={res_single_gate_sl30['trade_count']}, MaxDD={res_single_gate_sl30['max_dd']:.1f}%")

# ==================== グラフ描画 ====================
print("\nGenerating comprehensive comparison chart...")
plt.style.use('dark_background')
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
fig.patch.set_facecolor('#0d1117')
ax1.set_facecolor('#0d1117')
ax2.set_facecolor('#0d1117')

# 背景に個別10銘柄の資産曲線 (Nanpin SL3.0% 純粋版)
colors = ['#ff79c6', '#8be9fd', '#50fa7b', '#ffb86c', '#bd93f9', '#ff5555', '#f1fa8c', '#6272a4', '#a1efe4', '#e6db74']
for idx, sym in enumerate(eval_symbols):
    if sym in res_nanpin_sl30['sym_curves'].columns:
        pnl_val = res_nanpin_sl30['sym_stats'][sym]['pnl']
        wr_val = res_nanpin_sl30['sym_stats'][sym]['win_rate']
        ax1.plot(
            res_nanpin_sl30['sym_curves'].index,
            res_nanpin_sl30['sym_curves'][sym],
            color=colors[idx % len(colors)],
            alpha=0.25,
            linewidth=1.2,
            label=f"{sym.replace('USDT','')}: {pnl_val:+.1f}% (WR {wr_val:.0f}%)"
        )

# メインライン
# ① 純粋RSIMA ナンピン5枠 SL 2.5% (赤破線)
ax1.plot(
    res_nanpin_sl25['total_curve'].index,
    res_nanpin_sl25['total_curve'],
    color='#ff4444',
    linestyle='--',
    linewidth=2.0,
    label=f"★ Pure RSIMA [Nanpin 5枠] (SL 2.5% NoFilter): PnL {res_nanpin_sl25['total_pnl']:+.1f}% | WR {res_nanpin_sl25['win_rate']:.1f}% | DD {res_nanpin_sl25['max_dd']:.1f}%"
)

# ② 純粋RSIMA ナンピン5枠 SL 3.0% (橙実線)
ax1.plot(
    res_nanpin_sl30['total_curve'].index,
    res_nanpin_sl30['total_curve'],
    color='#ff9900',
    linestyle='-',
    linewidth=2.5,
    label=f"★ Pure RSIMA [Nanpin 5枠] (SL 3.0% NoFilter): PnL {res_nanpin_sl30['total_pnl']:+.1f}% | WR {res_nanpin_sl30['win_rate']:.1f}% | DD {res_nanpin_sl30['max_dd']:.1f}%"
)

# ③ 全面安門番 + ナンピン5枠 SL 3.0% (緑太線)
ax1.plot(
    res_nanpin_gate_sl30['total_curve'].index,
    res_nanpin_gate_sl30['total_curve'],
    color='#00ff88',
    linestyle='-',
    linewidth=3.0,
    label=f"★ + Macro Dump Gate [Nanpin 5枠] (SL 3.0%): PnL {res_nanpin_gate_sl30['total_pnl']:+.1f}% | WR {res_nanpin_gate_sl30['win_rate']:.1f}% | DD {res_nanpin_gate_sl30['max_dd']:.1f}%"
)

# ④ 参考: 純粋RSIMA ワンポジ SL 3.0% (水色破線)
ax1.plot(
    res_single_sl30['total_curve'].index,
    res_single_sl30['total_curve'],
    color='#00ccff',
    linestyle=':',
    linewidth=2.0,
    label=f"Ref: Pure RSIMA [Single Pos] (SL 3.0% NoFilter): PnL {res_single_sl30['total_pnl']:+.1f}% | WR {res_single_sl30['win_rate']:.1f}% | DD {res_single_sl30['max_dd']:.1f}%"
)

# ⑤ 参考: 門番 + ワンポジ SL 3.0% (マゼンタ破線)
ax1.plot(
    res_single_gate_sl30['total_curve'].index,
    res_single_gate_sl30['total_curve'],
    color='#e066ff',
    linestyle='-.',
    linewidth=2.0,
    label=f"Ref: + Macro Dump Gate [Single Pos] (SL 3.0%): PnL {res_single_gate_sl30['total_pnl']:+.1f}% | WR {res_single_gate_sl30['win_rate']:.1f}% | DD {res_single_gate_sl30['max_dd']:.1f}%"
)

ax1.set_title("RSIMA Strategy: Nanpin 4-stage (Total 5 Positions) vs Single Position Backtest (2022-2026, 10 Symbols)", fontsize=15, fontweight='bold', color='#ffffff', pad=15)
ax1.set_ylabel("Cumulative PnL (%)", fontsize=12, color='#ffffff')
ax1.axhline(0, color='#888888', linestyle='--', linewidth=0.8, alpha=0.7)
ax1.grid(True, linestyle=':', alpha=0.3, color='#444444')
ax1.legend(loc='upper left', fontsize=9.5, framealpha=0.85, facecolor='#161b22', edgecolor='#30363d')

# 下段: ドローダウン比較
ax2.fill_between(res_nanpin_sl25['drawdown_curve'].index, res_nanpin_sl25['drawdown_curve'], 0, color='#ff4444', alpha=0.2, label=f"Nanpin SL2.5% MaxDD: -{res_nanpin_sl25['max_dd']:.1f}%")
ax2.fill_between(res_nanpin_sl30['drawdown_curve'].index, res_nanpin_sl30['drawdown_curve'], 0, color='#ff9900', alpha=0.3, label=f"Nanpin SL3.0% MaxDD: -{res_nanpin_sl30['max_dd']:.1f}%")
ax2.fill_between(res_nanpin_gate_sl30['drawdown_curve'].index, res_nanpin_gate_sl30['drawdown_curve'], 0, color='#00ff88', alpha=0.45, label=f"Gate + Nanpin SL3.0% MaxDD: -{res_nanpin_gate_sl30['max_dd']:.1f}%")
ax2.set_ylabel("Drawdown (%)", fontsize=12, color='#ffffff')
ax2.set_xlabel("Date", fontsize=12, color='#ffffff')
ax2.grid(True, linestyle=':', alpha=0.3, color='#444444')
ax2.legend(loc='lower left', fontsize=9.5, framealpha=0.85, facecolor='#161b22', edgecolor='#30363d')
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax2.xaxis.set_major_locator(mdates.YearLocator())

plt.tight_layout()
OUTPUT_PLOT = WORKSPACE / "Data/plots/nanpin5_rsima_comparison.png"
OUTPUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUTPUT_PLOT, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
plt.close()
print(f"Chart successfully saved to {OUTPUT_PLOT}")

# Copy to brain artifact directory
brain_dir = Path("C:/Users/user/.gemini/antigravity/brain/ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")
dest_artifact = brain_dir / "nanpin5_rsima_comparison.png"
import shutil
shutil.copy(OUTPUT_PLOT, dest_artifact)
print(f"Copied to artifact dir: {dest_artifact}")

# Discord Webhook 送信
webhook_url = get_webhook_url("test4_test")
if not webhook_url:
    webhook_url = get_webhook_url("test4_backtest")

if webhook_url:
    print(f"Sending chart to Discord Webhook...")
    caption = (
        f"📊 **【RSIMA ナンピン4回・合計5ポジション バックテスト検証】**\n"
        f"・期間: 2022年〜2026年 (約4.7年間) / 主要10銘柄\n"
        f"・仕様: 初回1枠 + ナンピン最大4回 (-0.4% / -0.6%) ＝ 合計5枠保有\n\n"
        f"【結果サマリー】\n"
        f"1️⃣ 🔴 **純粋ナンピン5枠 (SL 2.5% フィルターなし)**: PnL `{res_nanpin_sl25['total_pnl']:+.1f}%` | 勝率 `{res_nanpin_sl25['win_rate']:.1f}%` | 最大DD `{res_nanpin_sl25['max_dd']:.1f}%`\n"
        f"2️⃣ 🟠 **純粋ナンピン5枠 (SL 3.0% フィルターなし)**: PnL `{res_nanpin_sl30['total_pnl']:+.1f}%` | 勝率 `{res_nanpin_sl30['win_rate']:.1f}%` | 最大DD `{res_nanpin_sl30['max_dd']:.1f}%`\n"
        f"3️⃣ 🟢 **全面安フィルター併用 ナンピン5枠 (SL 3.0%)**: PnL `{res_nanpin_gate_sl30['total_pnl']:+.1f}%` | 勝率 `{res_nanpin_gate_sl30['win_rate']:.1f}%` | 最大DD `{res_nanpin_gate_sl30['max_dd']:.1f}%`\n"
        f"4️⃣ 🔵 **純粋ワンポジ (SL 3.0% フィルターなし)**: PnL `{res_single_sl30['total_pnl']:+.1f}%` | 勝率 `{res_single_sl30['win_rate']:.1f}%` | 最大DD `{res_single_sl30['max_dd']:.1f}%`\n"
        f"5️⃣ 🟣 **全面安フィルター併用 ワンポジ (SL 3.0%)**: PnL `{res_single_gate_sl30['total_pnl']:+.1f}%` | 勝率 `{res_single_gate_sl30['win_rate']:.1f}%` | 最大DD `{res_single_gate_sl30['max_dd']:.1f}%`"
    )
    with open(OUTPUT_PLOT, 'rb') as f:
        files = {'file': (OUTPUT_PLOT.name, f, 'image/png')}
        payload = {'payload_json': json.dumps({'content': caption}, ensure_ascii=False)}
        resp = requests.post(webhook_url, data=payload, files=files, timeout=30)
        if resp.status_code in (200, 204):
            print("Discord notification sent successfully!")
        else:
            print(f"Failed to send to Discord: {resp.status_code}, {resp.text}")
