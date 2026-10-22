from __future__ import annotations
"""
analyze_stop_loss_optimization.py (高速キャッシュ版)
直近30日間の全20銘柄・実5分足時系列データ（SSoT）を用いた客観データ分析スクリプト
1. 勝ちトレードの最大逆行幅（MAE: Maximum Adverse Excursion）の統計分布
2. 損切り幅感度シミュレーション（現状 vs 枠数不問VA60% vs 固定%ストップ vs ハイブリッド）
3. 時間制限エグジットの有効性検証
※ Market Profile と 指標を事前計算キャッシュして超高速に評価する
"""
import sys
import os
from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
CANDLES_5M_DIR = DATA_DIR / "candles_5m"
RESULT_FILE = DATA_DIR / "sl_analysis_result.txt"

sys.path.append(str(BASE_DIR))
from bingx5_46_3logic import (
    calc_rsi_ema,
    calc_ema,
    make_market_profile_dual_va,
)

TARGET_SYMBOLS = [
    "XRP-USDT", "APT-USDT", "NEAR-USDT", "TIA-USDT", "RENDER-USDT",
    "INJ-USDT", "ZEC-USDT", "HYPE-USDT", "SOL-USDT", "DOGE-USDT",
    "SUI-USDT", "AVAX-USDT", "LINK-USDT", "ARB-USDT", "UNI-USDT",
    "OP-USDT", "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT"
]


def load_and_preprocess(symbol: str):
    p = CANDLES_5M_DIR / f"candles_5m_{symbol}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
    if len(df) < 200:
        return None

    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values

    # VAL_POC用 指標
    ema200 = calc_ema(df["close"].astype(float), 200).values
    ma_filter = (c >= ema200)
    mp_vp = make_market_profile_dual_va(df, period=150, num_bins=50, step=4, use_decay=True)

    # RSIMA用 指標
    rsi, rsi_ma = calc_rsi_ema(df["close"].astype(float), rsi_period=9, ma_period=7)
    mp_rsi = make_market_profile_dual_va(df, period=100, num_bins=50, step=4, use_decay=True)

    return {
        "symbol": symbol,
        "n": len(df),
        "c": c, "h": h, "l": l,
        "vp": {
            "ma_filter": ma_filter,
            "vals": mp_vp["val_70"],
            "vahs": mp_vp["vah_70"],
            "pocs": mp_vp["poc"]
        },
        "rsima": {
            "rsi": rsi.values,
            "rsi_ma": rsi_ma.values,
            "vals": mp_rsi["val_70"],
            "vahs": mp_rsi["vah_70"],
            "pocs": mp_rsi["poc"]
        }
    }


def simulate_cached(
    pre: dict,
    strategy: str = "val_poc",
    sl_mode: str = "current_deadlock",
    fixed_equity: float = 100.0,
    max_trades: int = 3
):
    n = pre["n"]
    c = pre["c"]
    h = pre["h"]
    l = pre["l"]
    trade_size = fixed_equity / max_trades
    fee_rate = 0.0006
    maker_fee = 0.0002

    if strategy == "val_poc":
        ma_filter = pre["vp"]["ma_filter"]
        vals = pre["vp"]["vals"]
        vahs = pre["vp"]["vahs"]
        pocs = pre["vp"]["pocs"]
    else:
        rsi_val = pre["rsima"]["rsi"]
        rsi_ma_val = pre["rsima"]["rsi_ma"]
        vals = pre["rsima"]["vals"]
        vahs = pre["rsima"]["vahs"]
        pocs = pre["rsima"]["pocs"]

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    last_entry = 0.0
    entry_bar_idx = -1
    min_price_in_trade = float("inf")
    cooldown_until = -1
    pending_limit = -1.0

    trades = []
    winning_mae_list = []
    current_equity = fixed_equity
    peak_equity = fixed_equity
    max_dd = 0.0

    for i in range(1, n):
        cur_c = c[i]
        cur_h = h[i]
        cur_l = l[i]

        if pending_limit > 0:
            if cur_l <= pending_limit:
                px = pending_limit
                qty = trade_size / px
                fee = trade_size * maker_fee
                pos_cost += trade_size + fee
                pos_qty += qty
                avg_price = pos_cost / pos_qty
                last_entry = px
                pos_count += 1
                if pos_count == 1:
                    entry_bar_idx = i
                    min_price_in_trade = px
                else:
                    min_price_in_trade = min(min_price_in_trade, px)
            pending_limit = -1.0

        if pos_qty > 0:
            min_price_in_trade = min(min_price_in_trade, cur_l)
            bars_held = i - entry_bar_idx
            val = vals[i]
            vah = vahs[i]
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * 0.60 if va_diff > 0 else (last_entry * 0.02)
            drop_dist = max(last_entry * 0.010, va_spacing)

            is_sl = False
            if sl_mode == "current_deadlock":
                sl_price = last_entry - drop_dist
                if pos_count >= max_trades and sl_price > 0 and cur_c <= sl_price:
                    is_sl = True
            elif sl_mode == "va_60":
                sl_price = last_entry - drop_dist
                if sl_price > 0 and cur_c <= sl_price:
                    is_sl = True
            elif sl_mode == "fixed_1_0":
                if cur_c <= avg_price * 0.990:
                    is_sl = True
            elif sl_mode == "fixed_1_5":
                if cur_c <= avg_price * 0.985:
                    is_sl = True
            elif sl_mode == "fixed_2_0":
                if cur_c <= avg_price * 0.980:
                    is_sl = True
            elif sl_mode == "fixed_2_5":
                if cur_c <= avg_price * 0.975:
                    is_sl = True
            elif sl_mode == "fixed_3_0":
                if cur_c <= avg_price * 0.970:
                    is_sl = True
            elif sl_mode == "hybrid_va60_cap2":
                sl_price_va = last_entry - drop_dist
                sl_price_cap = avg_price * 0.980
                sl_price = max(sl_price_va, sl_price_cap)
                if cur_c <= sl_price:
                    is_sl = True
            elif sl_mode == "time_4h":
                if bars_held >= 48 and cur_c < avg_price:
                    is_sl = True
                else:
                    sl_price = last_entry - drop_dist
                    if sl_price > 0 and cur_c <= sl_price:
                        is_sl = True

            if is_sl:
                sell_val = pos_qty * cur_c
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                trades.append(pnl)
                current_equity += pnl
                peak_equity = max(peak_equity, current_equity)
                dd = (peak_equity - current_equity) / peak_equity * 100.0
                max_dd = max(max_dd, dd)

                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry = 0.0
                entry_bar_idx = -1
                cooldown_until = i + 24
                continue

            is_tp = False
            if strategy == "val_poc":
                target_tp = val + va_spacing if va_spacing > 0 else pocs[i]
                if (cur_h >= target_tp or cur_c >= target_tp) and target_tp > 0:
                    exit_px = max(cur_c, target_tp) if cur_h >= target_tp else cur_c
                    if exit_px > avg_price * (1.0 + fee_rate):
                        is_tp = True
            else:
                if rsi_val[i] > 60.0 and cur_c > avg_price * 1.004:
                    exit_px = cur_c
                    is_tp = True

            if is_tp:
                sell_val = pos_qty * exit_px
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                trades.append(pnl)
                if pnl > 0:
                    mae_pct = ((min_price_in_trade - avg_price) / avg_price) * 100.0
                    winning_mae_list.append(mae_pct)

                current_equity += pnl
                peak_equity = max(peak_equity, current_equity)
                dd = (peak_equity - current_equity) / peak_equity * 100.0
                max_dd = max(max_dd, dd)

                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry = 0.0
                entry_bar_idx = -1
                continue

        if pending_limit < 0:
            if pos_count == 0 and i >= cooldown_until:
                if strategy == "val_poc":
                    if not np.isnan(vals[i]) and not np.isnan(vals[i-1]):
                        val_gc = (c[i-1] <= vals[i-1]) and (cur_c > vals[i])
                        va_width = (vahs[i] - vals[i]) / vals[i] if vals[i] > 0 else 0.0
                        if val_gc and (va_width >= 0.015) and ma_filter[i]:
                            pending_limit = cur_c * 0.9998
                else:
                    le = (rsi_val[i] > rsi_ma_val[i]) and (rsi_val[i-1] <= rsi_ma_val[i-1])
                    lc = (rsi_ma_val[i] < 40.0)
                    if le and lc:
                        pending_limit = cur_c * 0.9998
            elif 1 <= pos_count < max_trades:
                val = vals[i]
                vah = vahs[i]
                va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                va_spacing = va_diff * 0.60 if va_diff > 0 else (last_entry * 0.02)
                drop_dist = max(last_entry * 0.010, va_spacing)
                nanpin_px = last_entry - drop_dist
                if (cur_c <= nanpin_px or cur_l <= nanpin_px) and nanpin_px > 0:
                    pending_limit = cur_c

    total_trades = len(trades)
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = (n_wins / total_trades * 100.0) if total_trades > 0 else 0.0
    net_pnl = current_equity - fixed_equity
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    return {
        "trades_count": total_trades,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "pf": pf,
        "max_dd": max_dd,
        "winning_mae": winning_mae_list,
    }


def main():
    lines = []
    lines.append("=" * 95)
    lines.append("🔍 【客観データ検証】 勝ちトレード逆行幅 (MAE) ＆ 最適損切り感度シミュレーション")
    lines.append("   対象銘柄: 全 20 銘柄 | データ源泉: 5分足 SSoT (直近30日間)")
    lines.append("=" * 95)

    print("Pre-processing and caching 20 symbols...", flush=True)
    preprocessed_data = {}
    for sym in TARGET_SYMBOLS:
        p = load_and_preprocess(sym)
        if p:
            preprocessed_data[sym] = p

    print(f"Loaded {len(preprocessed_data)} symbols successfully.", flush=True)

    # 1. MAE 調査
    all_mae = []
    for sym, pre in preprocessed_data.items():
        r_vp = simulate_cached(pre, strategy="val_poc", sl_mode="current_deadlock")
        r_rsi = simulate_cached(pre, strategy="rsima", sl_mode="current_deadlock")
        all_mae.extend(r_vp["winning_mae"])
        all_mae.extend(r_rsi["winning_mae"])

    lines.append(f"\n📊 【1. 勝ちトレードの最大逆行幅 (MAE: Maximum Adverse Excursion) 統計】")
    lines.append(f"   集計サンプル数: {len(all_mae)} 件の勝ちトレード")
    if all_mae:
        abs_mae = np.abs(np.array(all_mae))
        p50 = np.percentile(abs_mae, 50)
        p75 = np.percentile(abs_mae, 75)
        p85 = np.percentile(abs_mae, 85)
        p90 = np.percentile(abs_mae, 90)
        p95 = np.percentile(abs_mae, 95)
        p99 = np.percentile(abs_mae, 99)
        max_mae = np.max(abs_mae)

        lines.append(f"   ・中央値 (50%ile)   : {p50:.2f}% (半数以上の勝ちトレードは -{p50:.2f}% 以内の逆行で反発)")
        lines.append(f"   ・75パーセンタイル  : {p75:.2f}% (75%の勝ちトレードは -{p75:.2f}% 以内)")
        lines.append(f"   ・85パーセンタイル  : {p85:.2f}%")
        lines.append(f"   ・90パーセンタイル  : {p90:.2f}% ➔ 勝ちトレードの90%が耐えられたライン: -{p90:.2f}%")
        lines.append(f"   ・95パーセンタイル  : {p95:.2f}% ➔ 勝ちトレードの95%が耐えられたライン: -{p95:.2f}%")
        lines.append(f"   ・99パーセンタイル  : {p99:.2f}%")
        lines.append(f"   ・最大逆行 (Max)    : {max_mae:.2f}%")

    # 2. 損切り感度シミュレーション
    sl_modes = [
        ("現状 (デッドロック・損切り無)", "current_deadlock"),
        ("枠数不問 VA60% 損切り", "va_60"),
        ("固定ストップ -1.0%", "fixed_1_0"),
        ("固定ストップ -1.5%", "fixed_1_5"),
        ("固定ストップ -2.0%", "fixed_2_0"),
        ("固定ストップ -2.5%", "fixed_2_5"),
        ("固定ストップ -3.0%", "fixed_3_0"),
        ("ハイブリッド (VA60% + 上限2.0%)", "hybrid_va60_cap2"),
        ("時間制限 (4h経過含み損撤退)", "time_4h"),
    ]

    lines.append("\n" + "=" * 95)
    lines.append(f"{'損切り設計パターン':<32} | {'取引数':>6} | {'勝率':>7} | {'総損益 (USDT)':>14} | {'PF':>6} | {'最大DD':>7}")
    lines.append("-" * 95)

    for label, mode in sl_modes:
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_gross_profit = 0.0
        total_gross_loss = 0.0
        max_dds = []

        for sym, pre in preprocessed_data.items():
            r_vp = simulate_cached(pre, strategy="val_poc", sl_mode=mode)
            r_rsi = simulate_cached(pre, strategy="rsima", sl_mode=mode)

            for r in [r_vp, r_rsi]:
                total_pnl += r["net_pnl"]
                total_trades += r["trades_count"]
                total_wins += r["wins"]
                total_losses += r["losses"]
                total_gross_profit += r["gross_profit"]
                total_gross_loss += r["gross_loss"]
                max_dds.append(r["max_dd"])

        wr = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0
        pf = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else (99.0 if total_gross_profit > 0 else 0.0)
        max_dd_all = np.max(max_dds) if max_dds else 0.0

        pnl_str = f"{total_pnl:+.2f} USDT"
        lines.append(f"{label:<32} | {total_trades:>6}回 | {wr:>6.1f}% | {pnl_str:>14} | {pf:>6.2f} | {max_dd_all:>6.1f}%")

    lines.append("=" * 95)

    full_output = "\n".join(lines)
    print(full_output)
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write(full_output)
    print(f"\nSaved analysis results to {RESULT_FILE}")


if __name__ == "__main__":
    main()
