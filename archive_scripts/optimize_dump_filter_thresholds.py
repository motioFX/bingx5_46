#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市場全体 全面安フィルター パラメータ閾値 最適化グリッドサーチ
ボットの主力戦略 (RSIMA) の実トレード損益・MaxDD・勝率に基づいて最適値を算出
期間: 2022年〜2026年 (Binance 1時間足データ)
"""
import sys
import time
import numpy as np
import pandas as pd
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_PATH = Path(__file__).resolve().parent / "Data" / "binance_candles_1h" / "binance_all_crypto_1h.csv"
print(f"Loading data from {DATA_PATH}...")
df_all = pd.read_csv(DATA_PATH)
df_all["timestamp"] = pd.to_datetime(df_all["timestamp"])
df_all = df_all[df_all["timestamp"] >= "2022-01-01"].copy()
df_all.sort_values(by=["symbol", "timestamp"], inplace=True)

pivot_close = df_all.pivot(index="timestamp", columns="symbol", values="close")
pivot_low = df_all.pivot(index="timestamp", columns="symbol", values="low")
pivot_high = df_all.pivot(index="timestamp", columns="symbol", values="high")
pivot_open = df_all.pivot(index="timestamp", columns="symbol", values="open")

timestamps = pivot_close.index
n_bars = len(timestamps)
alt_symbols = [col for col in pivot_close.columns if col != "BTCUSDT"]

# ==============================================================================
# 1. 主要アルトにおける RSIMA 戦略の全トレード（ベースライン）を生成
# ==============================================================================
print("\n[Step 1] 主要アルトの RSIMA トレード履歴を抽出中...")

# 代表的な主要10銘柄
eval_symbols = [
    "SOLUSDT", "DOGEUSDT", "XRPUSDT", "NEARUSDT", "AVAXUSDT",
    "LINKUSDT", "UNIUSDT", "ARBUSDT", "OPUSDT", "APTUSDT"
]

all_trades = []

def calc_rsi_series(series: pd.Series, length: int = 9) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    alpha = 1.0 / length
    rma_up = up.ewm(alpha=alpha, adjust=False).mean()
    rma_down = down.ewm(alpha=alpha, adjust=False).mean()
    rs = rma_up / rma_down.replace(0.0, np.nan)
    return np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))

ts_to_idx = {ts: idx for idx, ts in enumerate(timestamps)}

for sym in eval_symbols:
    if sym not in pivot_close.columns:
        continue
    c_s = pivot_close[sym].dropna()
    if len(c_s) < 100:
        continue
    h_s = pivot_high[sym].loc[c_s.index]
    l_s = pivot_low[sym].loc[c_s.index]
    
    rsi = calc_rsi_series(c_s, 9)
    lma = pd.Series(rsi, index=c_s.index).ewm(span=7, adjust=False).mean().values
    
    c_arr = c_s.values
    h_arr = h_s.values
    l_arr = l_s.values
    sym_ts = c_s.index
    
    in_pos = False
    entry_idx = 0
    entry_px = 0.0
    entry_ts = None
    sl_pct = 0.025
    tp_thresh = 70.0
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
        
        # エントリーチェック
        gc = (r > m) and (r_prev <= m_prev)
        if not in_pos:
            if gc and (m < 40.0) and (r < 70.0):
                # 翌足で約定想定
                in_pos = True
                entry_idx = ts_to_idx[cur_ts]
                entry_px = c * (1.0 - 0.001)  # maker指値
                entry_ts = cur_ts
                trailing_active = False
                trail_peak = 0.0
        else:
            # イグジットチェック
            # 1. 損切り
            if c < entry_px * (1.0 - sl_pct):
                exit_px = c
                pnl = (exit_px - entry_px) / entry_px * 100.0 - 0.08  # 手数料引き
                all_trades.append({
                    "symbol": sym,
                    "entry_ts": entry_ts,
                    "entry_idx": entry_idx,
                    "exit_ts": cur_ts,
                    "pnl": pnl,
                    "is_win": pnl > 0,
                    "exit_type": "SL"
                })
                in_pos = False
            else:
                # 2. 利確 (Trailing TP)
                if not trailing_active:
                    if r > tp_thresh and c > entry_px:
                        trailing_active = True
                        trail_peak = max(c, h)
                if trailing_active:
                    trail_peak = max(trail_peak, h)
                    trail_stop = trail_peak * (1.0 - 0.015)
                    if c < trail_stop:
                        exit_px = c
                        pnl = (exit_px - entry_px) / entry_px * 100.0 - 0.08
                        all_trades.append({
                            "symbol": sym,
                            "entry_ts": entry_ts,
                            "entry_idx": entry_idx,
                            "exit_ts": cur_ts,
                            "pnl": pnl,
                            "is_win": pnl > 0,
                            "exit_type": "TP"
                        })
                        in_pos = False

df_trades = pd.DataFrame(all_trades)
print(f"✔ ベースライン総トレード数: {len(df_trades):,} 回 (勝率: {df_trades['is_win'].mean()*100:.1f}%)")

def evaluate_portfolio(trades_df: pd.DataFrame) -> dict:
    """トレード群からPnL, MaxDD, WinRate, PF を算出"""
    if len(trades_df) == 0:
        return {"pnl": 0.0, "max_dd": 0.0, "win_rate": 0.0, "trades": 0, "pf": 0.0, "pnl_dd_ratio": 0.0}
    
    pnls = trades_df["pnl"].values
    cum_pnl = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum_pnl)
    dd = peak - cum_pnl
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0
    
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    pf = (float(np.sum(wins)) / abs(float(np.sum(losses)))) if len(losses) > 0 and np.sum(losses) != 0 else 99.0
    
    total_pnl = float(cum_pnl[-1])
    pnl_dd = (total_pnl / max_dd) if max_dd > 0 else 0.0
    
    return {
        "pnl": total_pnl,
        "max_dd": max_dd,
        "win_rate": float(np.mean(pnls > 0) * 100.0),
        "trades": len(trades_df),
        "pf": pf,
        "pnl_dd_ratio": pnl_dd
    }

base_perf = evaluate_portfolio(df_trades)
print(f"■ ベースライン (フィルターなし): 純利益 = {base_perf['pnl']:+.1f}% | 最大DD = {base_perf['max_dd']:.1f}% | 勝率 = {base_perf['win_rate']:.1f}% | PF = {base_perf['pf']:.2f} | PnL/DD = {base_perf['pnl_dd_ratio']:.2f}")

# ==============================================================================
# 2. 状態系列生成エンジンの高速化実装
# ==============================================================================
print("\n[Step 2] 全面安フィルター グリッドサーチ エンジン準備...")

ret_4h = pivot_close.pct_change(4) * 100.0
btc_ret_4h = ret_4h["BTCUSDT"].values

alt_ret_mat = ret_4h[alt_symbols].values
valid_alt_counts = np.sum(~np.isnan(alt_ret_mat), axis=1)
down_alt_counts = np.sum(alt_ret_mat < 0.0, axis=1)
alt_down_ratios = np.where(valid_alt_counts > 0, down_alt_counts / valid_alt_counts, 0.0)
alt_mean_rets = np.nanmean(alt_ret_mat, axis=1)

btc_close_arr = pivot_close["BTCUSDT"].values
btc_low_arr = pivot_low["BTCUSDT"].values
btc_open_arr = pivot_open["BTCUSDT"].values
alt_close_mat = pivot_close[alt_symbols].values
alt_low_mat = pivot_low[alt_symbols].values

def compute_filter_states(
    alt_lead_down_ratio: float,
    alt_lead_drop_pct: float,
    btc_dump_drop_pct: float,
    btc_dump_down_ratio: float,
    alt_recovery_ratio: float,
    alt_rebound_pct: float,
    min_lock_hours: int,
    cooldown_hours: int = 4
) -> np.ndarray:
    states = np.zeros(n_bars, dtype=bool)
    current_state = 0
    current_dump_start = 0
    btc_lowest_in_dump = float("inf")
    btc_lowest_bar_idx = 0
    alt_lowest_in_dump = None
    last_unlock_idx = -999

    for i in range(4, n_bars):
        c_state = current_state
        if c_state == 0:
            if (i - last_unlock_idx) >= cooldown_hours:
                is_alt_lead = (alt_down_ratios[i] >= alt_lead_down_ratio) and (alt_mean_rets[i] <= alt_lead_drop_pct)
                is_btc_lead = (btc_ret_4h[i] <= btc_dump_drop_pct) and (alt_down_ratios[i] >= btc_dump_down_ratio)
                if is_alt_lead or is_btc_lead:
                    c_state = 1
                    current_dump_start = i
                    btc_lowest_in_dump = btc_low_arr[i]
                    btc_lowest_bar_idx = i
                    alt_lowest_in_dump = alt_low_mat[i].copy()
        elif c_state in (1, 2):
            lock_duration = i - current_dump_start
            if btc_low_arr[i] < btc_lowest_in_dump:
                btc_lowest_in_dump = btc_low_arr[i]
                btc_lowest_bar_idx = i
                c_state = 1
            if alt_lowest_in_dump is not None:
                alt_lowest_in_dump = np.fmin(alt_lowest_in_dump, alt_low_mat[i])

            if lock_duration >= min_lock_hours:
                if c_state == 1:
                    hours_since_lowest = i - btc_lowest_bar_idx
                    btc_rebound = (btc_close_arr[i] - btc_lowest_in_dump) / btc_lowest_in_dump * 100.0 if btc_lowest_in_dump > 0 else 0
                    is_btc_green = btc_close_arr[i] > btc_open_arr[i]
                    if (hours_since_lowest >= 3) and (is_btc_green or btc_rebound >= 0.8):
                        c_state = 2

                if c_state == 2:
                    if alt_lowest_in_dump is not None:
                        valid_alts = ~np.isnan(alt_lowest_in_dump) & (alt_lowest_in_dump > 0)
                        rebound_pcts = (alt_close_mat[i][valid_alts] - alt_lowest_in_dump[valid_alts]) / alt_lowest_in_dump[valid_alts] * 100.0
                        rebounded_count = np.sum(rebound_pcts >= alt_rebound_pct)
                        rebound_ratio = (rebounded_count / len(rebound_pcts)) if len(rebound_pcts) > 0 else 0.0
                        if rebound_ratio >= alt_recovery_ratio:
                            c_state = 0
                            last_unlock_idx = i

        current_state = c_state
        states[i] = (current_state > 0)

    return states

# ==============================================================================
# 3. グリッドサーチ実行
# ==============================================================================
print("\n[Step 3] パラメータ探索グリッドサーチ実行中...")

# パラメータグリッド
alt_drop_list = [-3.0, -4.0, -5.0, -6.0]         # アルト下落率閾値
alt_ratio_list = [0.80, 0.85, 0.90]              # アルト下落比率
btc_drop_list = [-3.0, -4.0, -5.0]               # BTC下落率閾値
min_lock_list = [4, 6, 12]                       # 最低ロック時間
cooldown_list = [4, 8]                           # 再ロック禁止クールダウン

total_combos = len(alt_drop_list) * len(alt_ratio_list) * len(btc_drop_list) * len(min_lock_list) * len(cooldown_list)
print(f"総探索パターン数: {total_combos} 通り")

results = []
t0 = time.time()

trade_entry_indices = df_trades["entry_idx"].values

for a_drop in alt_drop_list:
    for a_ratio in alt_ratio_list:
        for b_drop in btc_drop_list:
            for m_lock in min_lock_list:
                for c_down in cooldown_list:
                    # 状態マスク取得
                    lock_mask = compute_filter_states(
                        alt_lead_down_ratio=a_ratio,
                        alt_lead_drop_pct=a_drop,
                        btc_dump_drop_pct=b_drop,
                        btc_dump_down_ratio=0.70,
                        alt_recovery_ratio=0.50,
                        alt_rebound_pct=1.5,
                        min_lock_hours=m_lock,
                        cooldown_hours=c_down
                    )
                    
                    # ロック中のエントリーを除外
                    allowed_mask = ~lock_mask[trade_entry_indices]
                    filtered_trades = df_trades[allowed_mask]
                    
                    perf = evaluate_portfolio(filtered_trades)
                    lock_time_pct = np.mean(lock_mask) * 100.0
                    
                    results.append({
                        "alt_drop": a_drop,
                        "alt_ratio": a_ratio,
                        "btc_drop": b_drop,
                        "min_lock": m_lock,
                        "cooldown": c_down,
                        "lock_pct": lock_time_pct,
                        "pnl": perf["pnl"],
                        "max_dd": perf["max_dd"],
                        "win_rate": perf["win_rate"],
                        "trades": perf["trades"],
                        "pf": perf["pf"],
                        "pnl_dd": perf["pnl_dd_ratio"],
                        "dd_reduced": base_perf["max_dd"] - perf["max_dd"],
                        "pnl_diff": perf["pnl"] - base_perf["pnl"]
                    })

elapsed = time.time() - t0
print(f"✔ 探索完了 ({elapsed:.1f} 秒)")

df_res = pd.DataFrame(results)

# 評価基準: PnL / MaxDD 比率（リスクリターン効率）の降順でソート
df_res.sort_values(by="pnl_dd", ascending=False, inplace=True)

print("\n" + "=" * 115)
print("🏆 【全面安フィルター 最適パラメータ ランキング TOP 10 (PnL/MaxDD 改善度順)】")
print("=" * 115)
print(f"{'順位':4s} | {'アルト閾値':11s} | {'BTC閾値':7s} | {'最短/CD':8s} | {'停止率':6s} | {'純利益 (PnL)':13s} | {'最大DD':8s} | {'DD削減':8s} | {'勝率':6s} | {'PF':5s} | {'PnL/DD'}")
print("-" * 115)

# ベースライン行
print(f"{'BASE':4s} | {'フィルターなし':11s} | {'---':7s} | {'---':8s} | {' 0.0%':6s} | {base_perf['pnl']:+11.1f}% | {base_perf['max_dd']:7.1f}% | {' --- ':8s} | {base_perf['win_rate']:5.1f}% | {base_perf['pf']:4.2f} | {base_perf['pnl_dd_ratio']:5.2f}")
print("-" * 115)

for rank, (_, row) in enumerate(df_res.head(10).iterrows(), 1):
    alt_str = f"{row['alt_drop']:.0f}%, {int(row['alt_ratio']*100)}%"
    btc_str = f"{row['btc_drop']:.0f}%"
    time_str = f"{int(row['min_lock'])}h/{int(row['cooldown'])}h"
    lock_str = f"{row['lock_pct']:4.1f}%"
    pnl_str = f"{row['pnl']:+11.1f}% ({row['pnl_diff']:+5.1f}%)"
    dd_str = f"{row['max_dd']:7.1f}%"
    dd_red_str = f"-{row['dd_reduced']:4.1f}%" if row['dd_reduced'] > 0 else f"+{-row['dd_reduced']:4.1f}%"
    wr_str = f"{row['win_rate']:5.1f}%"
    pf_str = f"{row['pf']:4.2f}"
    pnl_dd_str = f"{row['pnl_dd']:5.2f}"
    
    print(f"#{rank:2d}  | {alt_str:11s} | {btc_str:7s} | {time_str:8s} | {lock_str:6s} | {pnl_str:13s} | {dd_str:8s} | {dd_red_str:8s} | {wr_str:6s} | {pf_str:5s} | {pnl_dd_str}")

print("=" * 115)
