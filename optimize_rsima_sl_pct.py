#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSIMA戦略 損切りライン (sl_pct) 最適化グリッドサーチ
・全面安フィルターなし vs あり での損切りライン別パフォーマンス比較
期間: 2022年〜2026年 (Binance 1時間足データ、主要10銘柄)
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
ts_to_idx = {ts: idx for idx, ts in enumerate(timestamps)}

eval_symbols = [
    "SOLUSDT", "DOGEUSDT", "XRPUSDT", "NEARUSDT", "AVAXUSDT",
    "LINKUSDT", "UNIUSDT", "ARBUSDT", "OPUSDT", "APTUSDT"
]

# 1. 最適全面安フィルターの状態系列を計算
from optimize_dump_filter_thresholds import compute_filter_states

print("Computing optimal Macro Dump Filter states (Alt -3% 90% / BTC -3% / 6h lock)...")
filter_mask = compute_filter_states(
    alt_lead_down_ratio=0.90,
    alt_lead_drop_pct=-3.0,
    btc_dump_drop_pct=-3.0,
    btc_dump_down_ratio=0.70,
    alt_recovery_ratio=0.50,
    alt_rebound_pct=1.5,
    min_lock_hours=6,
    cooldown_hours=4
)
print(f"Filter lock ratio: {np.mean(filter_mask)*100:.1f}%")

def calc_rsi_series(series: pd.Series, length: int = 9) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    alpha = 1.0 / length
    rma_up = up.ewm(alpha=alpha, adjust=False).mean()
    rma_down = down.ewm(alpha=alpha, adjust=False).mean()
    rs = rma_up / rma_down.replace(0.0, np.nan)
    return np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))

# 2. 事前に各銘柄のインジケーター（RSI, lma）を事前計算
cached_sym_data = {}
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
    
    cached_sym_data[sym] = {
        "closes": c_s.values,
        "highs": h_s.values,
        "lows": l_s.values,
        "rsi": rsi,
        "lma": lma,
        "timestamps": c_s.index
    }

def run_rsima_simulation(sl_pct: float) -> pd.DataFrame:
    """指定された sl_pct で全銘柄のトレードをシミュレーション"""
    trades = []
    tp_thresh = 70.0
    callback_pct = 0.015
    maker_fee = 0.0002
    taker_fee = 0.0006

    for sym, data in cached_sym_data.items():
        c_arr = data["closes"]
        h_arr = data["highs"]
        l_arr = data["lows"]
        rsi = data["rsi"]
        lma = data["lma"]
        sym_ts = data["timestamps"]
        
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
                    entry_px = c * (1.0 - 0.001)  # 指値
                    entry_ts = cur_ts
                    trailing_active = False
                    trail_peak = 0.0
            else:
                # イグジットチェック
                # 1. 損切り (Stop Loss)
                if sl_pct > 0 and c < entry_px * (1.0 - sl_pct):
                    exit_px = c
                    pnl = (exit_px - entry_px) / entry_px * 100.0 - (maker_fee + taker_fee) * 100.0
                    trades.append({
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
                    # 2. 利確 (Trailing Take-Profit)
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
                                "symbol": sym,
                                "entry_ts": entry_ts,
                                "entry_idx": entry_idx,
                                "exit_ts": cur_ts,
                                "pnl": pnl,
                                "is_win": pnl > 0,
                                "exit_type": "TP"
                            })
                            in_pos = False

    return pd.DataFrame(trades)


def evaluate_trades(df_t: pd.DataFrame) -> dict:
    if len(df_t) == 0:
        return {"pnl": 0.0, "max_dd": 0.0, "win_rate": 0.0, "trades": 0, "pf": 0.0, "pnl_dd": 0.0}
    
    pnls = df_t["pnl"].values
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
        "trades": len(df_t),
        "pf": pf,
        "pnl_dd": pnl_dd
    }

# 3. 損切りライン探索リスト
sl_candidates = [
    0.010,  # 1.0%
    0.015,  # 1.5%
    0.020,  # 2.0%
    0.025,  # 2.5% (現行デフォルト)
    0.030,  # 3.0%
    0.035,  # 3.5%
    0.040,  # 4.0%
    0.050,  # 5.0%
    0.060,  # 6.0%
    0.080,  # 8.0%
    0.100,  # 10.0%
]

print("\n[Step 2] 損切りライン (sl_pct) グリッドサーチ実行中...")
comparison_results = []

for sl in sl_candidates:
    t0 = time.time()
    df_raw = run_rsima_simulation(sl)
    
    # フィルターなし
    perf_no_filter = evaluate_trades(df_raw)
    
    # 最適全面安フィルターあり
    entry_indices = df_raw["entry_idx"].values
    allowed_mask = ~filter_mask[entry_indices]
    df_filtered = df_raw[allowed_mask]
    perf_with_filter = evaluate_trades(df_filtered)
    
    comparison_results.append({
        "sl_pct": sl,
        "sl_label": f"{sl*100:.1f}%",
        # フィルターなし
        "no_pnl": perf_no_filter["pnl"],
        "no_dd": perf_no_filter["max_dd"],
        "no_wr": perf_no_filter["win_rate"],
        "no_pf": perf_no_filter["pf"],
        "no_trades": perf_no_filter["trades"],
        # フィルターあり
        "with_pnl": perf_with_filter["pnl"],
        "with_dd": perf_with_filter["max_dd"],
        "with_wr": perf_with_filter["win_rate"],
        "with_pf": perf_with_filter["pf"],
        "with_trades": perf_with_filter["trades"],
        "with_pnl_dd": perf_with_filter["pnl_dd"],
    })
    print(f"  ✔ sl_pct = {sl*100:4.1f}% 完了: フィルターあり PnL = {perf_with_filter['pnl']:+6.1f}%, MaxDD = {perf_with_filter['max_dd']:5.1f}%, 勝率 = {perf_with_filter['win_rate']:4.1f}%")

df_comp = pd.DataFrame(comparison_results)

print("\n" + "=" * 125)
print("📊 【RSIMA 損切りライン (sl_pct) 最適化グリッドサーチ結果比較表】 (2022〜2026年 4.7年間)")
print("=" * 125)
print(f"{'損切り幅':6s} | {'【全面安フィルターあり (最適設定)】':55s} | {'【フィルターなし (参考)】':50s}")
print(f"{'sl_pct':6s} | {'純利益 (PnL)':14s} | {'最大DD':7s} | {'勝率':6s} | {'PF':5s} | {'取引数':6s} | {'PnL/DD':7s} | {'純利益 (PnL)':14s} | {'最大DD':7s} | {'勝率':6s} | {'PF':5s}")
print("-" * 125)

for _, row in df_comp.iterrows():
    sl_l = row["sl_label"]
    # フィルターあり
    w_pnl = f"{row['with_pnl']:+11.1f}%"
    w_dd = f"{row['with_dd']:6.1f}%"
    w_wr = f"{row['with_wr']:5.1f}%"
    w_pf = f"{row['with_pf']:4.2f}"
    w_tc = f"{row['with_trades']:5d}"
    w_pdd = f"{row['with_pnl_dd']:6.2f}"
    
    # フィルターなし
    n_pnl = f"{row['no_pnl']:+11.1f}%"
    n_dd = f"{row['no_dd']:6.1f}%"
    n_wr = f"{row['no_wr']:5.1f}%"
    n_pf = f"{row['no_pf']:4.2f}"
    
    print(f"{sl_l:6s} | {w_pnl:14s} | {w_dd:7s} | {w_wr:6s} | {w_pf:5s} | {w_tc:6s} | {w_pdd:7s} | {n_pnl:14s} | {n_dd:7s} | {n_wr:6s} | {n_pf:5s}")

print("=" * 125)

# 最適損切りラインの選定 (PnL/DD 最大)
best_row = df_comp.sort_values(by="with_pnl_dd", ascending=False).iloc[0]
best_pnl_row = df_comp.sort_values(by="with_pnl", ascending=False).iloc[0]

print(f"\n🏆 【最高リスクリターン (PnL/MaxDD最大)】: 損切りライン = {best_row['sl_label']}")
print(f"   純利益: {best_row['with_pnl']:+.1f}% | 最大DD: {best_row['with_dd']:.1f}% | 勝率: {best_row['with_wr']:.1f}% | PF: {best_row['with_pf']:.2f} | PnL/DD: {best_row['with_pnl_dd']:.2f}")

print(f"\n💰 【最大純利益 (PnL最大)】: 損切りライン = {best_pnl_row['sl_label']}")
print(f"   純利益: {best_pnl_row['with_pnl']:+.1f}% | 最大DD: {best_pnl_row['with_dd']:.1f}% | 勝率: {best_pnl_row['with_wr']:.1f}% | PF: {best_pnl_row['with_pf']:.2f} | PnL/DD: {best_pnl_row['with_pnl_dd']:.2f}")
