from __future__ import annotations
"""
analyze_stop_loss_optimization.py
直近30日間の全20銘柄・実5分足時系列データ（SSoT）を用いた客観データ分析スクリプト
1. 勝ちトレードの最大逆行幅（MAE: Maximum Adverse Excursion）の統計分布
2. 損切り幅感度シミュレーション（現状 vs 枠数不問VA60% vs 固定%ストップ vs ハイブリッド）
3. 時間制限エグジットの有効性検証
"""
import sys
import os
from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
CANDLES_5M_DIR = DATA_DIR / "candles_5m"

sys.path.append(str(BASE_DIR))
from bingx5_46_3logic import (
    calc_rsi_ema,
    calc_ema,
    make_market_profile_dual_va,
    resample_candles
)

TARGET_SYMBOLS = [
    "XRP-USDT", "APT-USDT", "NEAR-USDT", "TIA-USDT", "RENDER-USDT",
    "INJ-USDT", "ZEC-USDT", "HYPE-USDT", "SOL-USDT", "DOGE-USDT",
    "SUI-USDT", "AVAX-USDT", "LINK-USDT", "ARB-USDT", "UNI-USDT",
    "OP-USDT", "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT"
]


def load_candles(symbol: str) -> pd.DataFrame:
    p = CANDLES_5M_DIR / f"candles_5m_{symbol}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def simulate_strategy_with_mae(
    df: pd.DataFrame,
    symbol: str,
    strategy: str = "val_poc",  # "val_poc" or "rsima"
    sl_mode: str = "current_deadlock",  # "current_deadlock", "va_60", "fixed_1_0", "fixed_1_5", "fixed_2_0", "fixed_2_5", "fixed_3_0", "hybrid_va60_cap2", "time_4h"
    fixed_equity: float = 100.0,
    max_trades: int = 3
):
    """
    MAE（勝ちトレードの最大逆行幅）を追跡しつつ、指定された損切りルールでシミュレーションを実行する
    """
    n = len(df)
    if n < 200:
        return {"trades": [], "mae_list": [], "net_pnl": 0.0, "win_rate": 0.0, "pf": 0.0, "max_dd": 0.0}

    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    trade_size = fixed_equity / max_trades
    fee_rate = 0.0006
    maker_fee = 0.0002

    # 指標算出
    if strategy == "val_poc":
        ema200 = calc_ema(df["close"].astype(float), 200).values
        ma_filter = (c >= ema200)
        mp_res = make_market_profile_dual_va(df, period=150, num_bins=50, step=4, use_decay=True)
        vals = mp_res["val_70"]
        vahs = mp_res["vah_70"]
        pocs = mp_res["poc"]
    else:  # rsima
        rsi, rsi_ma = calc_rsi_ema(df["close"].astype(float), rsi_period=9, ma_period=7)
        rsi_val = rsi.values
        rsi_ma_val = rsi_ma.values
        mp_res = make_market_profile_dual_va(df, period=100, num_bins=50, step=4, use_decay=True)
        vals = mp_res["val_70"]
        vahs = mp_res["vah_70"]
        pocs = mp_res["poc"]

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
    winning_mae_list = []  # 勝ちトレードのMAE (%)
    all_mae_list = []      # 全トレードのMAE (%)
    equity_curve = [fixed_equity]
    current_equity = fixed_equity
    peak_equity = fixed_equity
    max_dd = 0.0

    for i in range(1, n):
        cur_c = c[i]
        cur_h = h[i]
        cur_l = l[i]

        # 0. 未約定指値チェック
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

        # 1. 保有中ポジションの監視
        if pos_qty > 0:
            min_price_in_trade = min(min_price_in_trade, cur_l)
            bars_held = i - entry_bar_idx
            val = vals[i]
            vah = vahs[i]
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * 0.60 if va_diff > 0 else (last_entry * 0.02)
            drop_dist = max(last_entry * 0.010, va_spacing)

            # --- 損切り (SL) 判定 ---
            is_sl = False
            sl_reason = ""

            if sl_mode == "current_deadlock":
                # 現状バグ: 3枠満額時のみ損切り (1〜2枠時は放置)
                sl_price = last_entry - drop_dist
                if pos_count >= max_trades and sl_price > 0 and cur_c <= sl_price:
                    is_sl = True
                    sl_reason = "DEADLOCK_SL_ONLY_AT_MAX"

            elif sl_mode == "va_60":
                # 枠数不問: 1枠目でも最終建値から VA 60% 割れで損切り
                sl_price = last_entry - drop_dist
                if sl_price > 0 and cur_c <= sl_price:
                    is_sl = True
                    sl_reason = "VA_60_STOP_LOSS"

            elif sl_mode == "fixed_1_0":
                if cur_c <= avg_price * 0.990:
                    is_sl = True
                    sl_reason = "FIXED_1_0_SL"

            elif sl_mode == "fixed_1_5":
                if cur_c <= avg_price * 0.985:
                    is_sl = True
                    sl_reason = "FIXED_1_5_SL"

            elif sl_mode == "fixed_2_0":
                if cur_c <= avg_price * 0.980:
                    is_sl = True
                    sl_reason = "FIXED_2_0_SL"

            elif sl_mode == "fixed_2_5":
                if cur_c <= avg_price * 0.975:
                    is_sl = True
                    sl_reason = "FIXED_2_5_SL"

            elif sl_mode == "fixed_3_0":
                if cur_c <= avg_price * 0.970:
                    is_sl = True
                    sl_reason = "FIXED_3_0_SL"

            elif sl_mode == "hybrid_va60_cap2":
                # VA 60% 割れ または 建値 -2.0% キャップ
                sl_price_va = last_entry - drop_dist
                sl_price_cap = avg_price * 0.980
                sl_price = max(sl_price_va, sl_price_cap)  # より浅い方で切る
                if cur_c <= sl_price:
                    is_sl = True
                    sl_reason = "HYBRID_VA60_CAP2"

            elif sl_mode == "time_4h":
                # 4時間 (48本) 経過して含み損なら撤退
                if bars_held >= 48 and cur_c < avg_price:
                    is_sl = True
                    sl_reason = "TIME_4H_STOP"
                else:
                    sl_price = last_entry - drop_dist
                    if sl_price > 0 and cur_c <= sl_price:
                        is_sl = True
                        sl_reason = "TIME_4H_VA60"

            # --- 損切りの執行 ---
            if is_sl:
                sell_val = pos_qty * cur_c
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                trades.append({
                    "pnl": pnl, "pnl_pct": (cur_c - avg_price) / avg_price,
                    "bars": bars_held, "is_win": False, "reason": sl_reason
                })
                # MAE 記録
                mae_pct = ((min_price_in_trade - avg_price) / avg_price) * 100.0
                all_mae_list.append(mae_pct)

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

            # --- 利確 (TP) 判定 ---
            is_tp = False
            if strategy == "val_poc":
                target_tp = val + va_spacing if va_spacing > 0 else pocs[i]
                if (cur_h >= target_tp or cur_c >= target_tp) and target_tp > 0:
                    exit_px = max(cur_c, target_tp) if cur_h >= target_tp else cur_c
                    if exit_px > avg_price * (1.0 + fee_rate):
                        is_tp = True
            else:  # rsima
                curr_rsi = rsi_val[i]
                if curr_rsi > 60.0 and cur_c > avg_price * 1.004:
                    exit_px = cur_c
                    is_tp = True

            if is_tp:
                sell_val = pos_qty * exit_px
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                is_win = (pnl > 0)
                mae_pct = ((min_price_in_trade - avg_price) / avg_price) * 100.0
                trades.append({
                    "pnl": pnl, "pnl_pct": (exit_px - avg_price) / avg_price,
                    "bars": bars_held, "is_win": is_win, "reason": "TP"
                })
                all_mae_list.append(mae_pct)
                if is_win:
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

        # 2. エントリー / ナンピン判定
        if pending_limit < 0:
            if pos_count == 0 and i >= cooldown_until:
                if strategy == "val_poc":
                    if not np.isnan(vals[i]) and not np.isnan(vals[i-1]):
                        val_gc = (c[i-1] <= vals[i-1]) and (cur_c > vals[i])
                        va_width = (vahs[i] - vals[i]) / vals[i] if vals[i] > 0 else 0.0
                        if val_gc and (va_width >= 0.015) and ma_filter[i]:
                            pending_limit = cur_c * 0.9998
                else:  # rsima
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

    # 集計
    total_trades = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = (n_wins / total_trades * 100.0) if total_trades > 0 else 0.0
    net_pnl = current_equity - fixed_equity
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    return {
        "symbol": symbol,
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
        "all_mae": all_mae_list
    }


def main():
    print("=" * 80)
    print("🔍 【客観データ検証】 勝ちトレード逆行幅 (MAE) ＆ 最適損切り感度シミュレーション")
    print(f"   対象銘柄: 全 20 銘柄 | データ源泉: 5分足 SSoT (直近30日間)")
    print("=" * 80)

    all_symbols_mae = []

    # 1. MAE (最大逆行幅) 調査: 勝ちトレードがどこまで逆行に耐えたかを測定
    for sym in TARGET_SYMBOLS:
        df = load_candles(sym)
        if df.empty:
            continue
        res_vp = simulate_strategy_with_mae(df, sym, strategy="val_poc", sl_mode="current_deadlock")
        res_rsi = simulate_strategy_with_mae(df, sym, strategy="rsima", sl_mode="current_deadlock")
        all_symbols_mae.extend(res_vp["winning_mae"])
        all_symbols_mae.extend(res_rsi["winning_mae"])

    print(f"\n📊 【1. 勝ちトレードの最大逆行幅 (MAE: Maximum Adverse Excursion) 統計】")
    print(f"   集計サンプル数: {len(all_symbols_mae)} 件の勝ちトレード")
    if all_symbols_mae:
        mae_arr = np.array(all_symbols_mae)
        # MAE は負の値（例: -0.5%）なので絶対値で集計
        abs_mae = np.abs(mae_arr)
        p50 = np.percentile(abs_mae, 50)
        p75 = np.percentile(abs_mae, 75)
        p85 = np.percentile(abs_mae, 85)
        p90 = np.percentile(abs_mae, 90)
        p95 = np.percentile(abs_mae, 95)
        p99 = np.percentile(abs_mae, 99)
        max_mae = np.max(abs_mae)

        print(f"   ・中央値 (50%ile)   : {p50:.2f}% (半分以上の勝ちトレードは -{p50:.2f}% 以内の逆行で反発)")
        print(f"   ・75パーセンタイル  : {p75:.2f}%")
        print(f"   ・85パーセンタイル  : {p85:.2f}%")
        print(f"   ・90パーセンタイル  : {p90:.2f}% ➔ 勝ちトレードの90%が耐えられたライン: -{p90:.2f}%")
        print(f"   ・95パーセンタイル  : {p95:.2f}% ➔ 勝ちトレードの95%が耐えられたライン: -{p95:.2f}%")
        print(f"   ・99パーセンタイル  : {p99:.2f}%")
        print(f"   ・最大逆行 (Max)    : {max_mae:.2f}%")

    # 2. 損切り感度シミュレーション（全20銘柄通算）
    sl_modes = [
        ("現状 (デッドロック)", "current_deadlock"),
        ("枠数不問 VA60%", "va_60"),
        ("固定ストップ -1.0%", "fixed_1_0"),
        ("固定ストップ -1.5%", "fixed_1_5"),
        ("固定ストップ -2.0%", "fixed_2_0"),
        ("固定ストップ -2.5%", "fixed_2_5"),
        ("固定ストップ -3.0%", "fixed_3_0"),
        ("ハイブリッド (VA60%+上限2%)", "hybrid_va60_cap2"),
        ("時間制限 (4h経過含み損撤退)", "time_4h"),
    ]

    print("\n" + "=" * 95)
    print(f"{'損切り設計パターン':<28} | {'取引数':>6} | {'勝率':>7} | {'総損益 (USDT)':>13} | {'PF':>6} | {'最大DD':>7}")
    print("-" * 95)

    comparison_results = []
    for label, mode in sl_modes:
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_gross_profit = 0.0
        total_gross_loss = 0.0
        max_dds = []

        for sym in TARGET_SYMBOLS:
            df = load_candles(sym)
            if df.empty:
                continue
            # VAL_POC と RSIMA を両方実行して合計
            r_vp = simulate_strategy_with_mae(df, sym, strategy="val_poc", sl_mode=mode)
            r_rsi = simulate_strategy_with_mae(df, sym, strategy="rsima", sl_mode=mode)

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
        avg_dd = np.mean(max_dds) if max_dds else 0.0
        max_dd_all = np.max(max_dds) if max_dds else 0.0

        pnl_str = f"{total_pnl:+.2f} USDT"
        print(f"{label:<28} | {total_trades:>6}回 | {wr:>6.1f}% | {pnl_str:>13} | {pf:>6.2f} | {max_dd_all:>6.1f}%")
        comparison_results.append({
            "label": label, "trades": total_trades, "win_rate": wr,
            "net_pnl": total_pnl, "pf": pf, "max_dd": max_dd_all
        })

    print("=" * 95)


if __name__ == "__main__":
    main()
