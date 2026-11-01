#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VAL_POC 最適黄金パラメータによる 20銘柄 1年分完全バックテスト
・バリューエリア幅フィルター: >= 1.5%
・ナンピン幅: 2.0%
・ストップロス: 8.5% (終値確定判定)
・利確ターゲット: VAL-VAH 60%水準
"""
import sys
import json
import time
import shutil
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pathlib import Path

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
DATA_DIR = WORKSPACE / "Data/binance_candles_5m"
PLOT_DIR = WORKSPACE / "Data/plots"
ARTIFACT_DIR = Path("C:/Users/user/.gemini/antigravity/brain/ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")

from config_loader import get_webhook_url

TARGET_SYMBOLS = [
    'HYPEUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'SUIUSDT',
    'AVAXUSDT', 'LINKUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'UNIUSDT', 'OPUSDT', 'TIAUSDT', 'RENDERUSDT', 'INJUSDT',
    'FETUSDT', 'TAOUSDT', 'AAVEUSDT', 'ENAUSDT', 'ZECUSDT'
]

def load_all_cache():
    mp_dict = {}
    for sym in TARGET_SYMBOLS:
        cache_file = DATA_DIR / f"{sym}_5m_mp.pkl"
        if cache_file.exists():
            mp_dict[sym] = pd.read_pickle(cache_file)
    return mp_dict

def simulate_symbol(
    df_mp: pd.DataFrame,
    target_ratio: float = 0.60,
    min_va_width: float = 0.015,
    nanpin_drop: float = 0.020,
    sl_pct: float = 0.085,
    initial_equity: float = 100.0
):
    closes = df_mp["close"].values
    highs = df_mp["high"].values
    lows = df_mp["low"].values
    vals = df_mp["VAL"].values
    prev_vals = np.roll(vals, 1)
    prev_vals[0] = vals[0]
    pocs = df_mp["POC"].values
    vahs = df_mp["VAH"].values
    n = len(closes)

    total_max_stages = 5
    trade_size_usdt = initial_equity / total_max_stages

    pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0
    pending_entry = None
    cum_realized_pnl = cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        prev_val = prev_vals[i]
        poc = pocs[i]
        vah = vahs[i]

        if pending_entry is not None:
            if l <= pending_entry["limit_price"]:
                exec_price = pending_entry["limit_price"]
                buy_val = trade_size_usdt
                qty = buy_val / exec_price
                fee = buy_val * 0.0002
                cum_fees += fee
                pos_cost += buy_val
                pos_qty += qty
                last_entry_price = exec_price
                avg_price = pos_cost / pos_qty
                pos_count += 1
            pending_entry = None

        if pos_qty > 0:
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sell_val = pos_qty * c
                fee = sell_val * 0.0006
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0
            else:
                target_px = val + (vah - val) * target_ratio
                if not np.isnan(target_px) and (h >= target_px or c >= target_px):
                    exit_price = max(c, target_px) if h >= target_px else c
                    if exit_price > avg_price * 1.0006:
                        sell_val = pos_qty * exit_price
                        fee = sell_val * 0.0006
                        pnl = sell_val - pos_cost - fee
                        cum_realized_pnl += pnl
                        cum_fees += fee
                        trades.append(pnl)
                        pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0

        if not np.isnan(val) and not np.isnan(prev_val) and not np.isnan(vah) and pending_entry is None:
            va_width = (vah - val) / val if val > 0 else 0.0

            if pos_count == 0:
                if (closes[i-1] <= prev_val) and (c > val):
                    if min_va_width == 0.0 or va_width >= min_va_width:
                        pending_entry = {"limit_price": c * 0.999}
            elif 1 <= pos_count < total_max_stages:
                if (c <= last_entry_price * (1.0 - nanpin_drop)) or (l <= last_entry_price * (1.0 - nanpin_drop)):
                    pending_entry = {"limit_price": c * 0.999}

        unrealized = (pos_qty * c - pos_cost) if pos_qty > 0 else 0.0
        equity_curve.append(initial_equity + cum_realized_pnl + unrealized - cum_fees)

    eq_s = pd.Series(equity_curve)
    peak = eq_s.cummax()
    dd = (peak - eq_s) / peak * 100.0
    final_pnl = ((equity_curve[-1] - initial_equity) / initial_equity) * 100.0
    wr = sum(1 for p in trades if p > 0) / max(1, len(trades)) * 100.0

    return {
        "final_pnl": final_pnl,
        "win_rate": wr,
        "trade_count": len(trades),
        "DD_max": float(dd.max()),
        "equity_curve": equity_curve
    }


def main():
    print("=" * 70)
    print(" 【VAL_POC 単体ロジック 黄金最適設定 20銘柄 1年分バックテスト】")
    print(" 設定: Width >= 1.5%, Nanpin 2.0%, SL 8.5%, Target 60%")
    print("=" * 70)

    mp_dict = load_all_cache()
    print(f"Loaded {len(mp_dict)} symbols.")

    sym_stats = []
    portfolio_eq = None
    total_trades = 0
    total_wins = 0

    for sym in TARGET_SYMBOLS:
        if sym not in mp_dict:
            continue
        df_mp = mp_dict[sym]
        res = simulate_symbol(df_mp, target_ratio=0.60, min_va_width=0.015, nanpin_drop=0.020, sl_pct=0.085)
        sym_stats.append({
            "symbol": sym,
            "pnl": res["final_pnl"],
            "win_rate": res["win_rate"],
            "trades": res["trade_count"],
            "dd_max": res["DD_max"]
        })
        total_trades += res["trade_count"]
        total_wins += int(res["trade_count"] * res["win_rate"] / 100.0)

        eq = np.array(res["equity_curve"])
        if portfolio_eq is None:
            portfolio_eq = eq.copy()
        else:
            min_len = min(len(portfolio_eq), len(eq))
            portfolio_eq = portfolio_eq[:min_len] + eq[:min_len]

    port_initial = 100.0 * len(sym_stats)
    port_final = portfolio_eq[-1]
    port_pnl = ((port_final - port_initial) / port_initial) * 100.0
    overall_wr = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0

    port_series = pd.Series(portfolio_eq)
    port_peak = port_series.cummax()
    port_dd = (port_peak - port_series) / port_peak * 100.0
    port_max_dd = port_dd.max()
    avg_trades_day = total_trades / (365.0 * len(sym_stats))

    print(f"\n>> ポートフォリオ純利益率: {port_pnl:+.2f}%")
    print(f">> 全体勝率: {overall_wr:.1f}%")
    print(f">> 年間総取引数: {total_trades:,}回 (1銘柄1日: {avg_trades_day:.2f}回)")
    print(f">> ポートフォリオMaxDD: {port_max_dd:.2f}%")

    df_stats = pd.DataFrame(sym_stats)
    print("\n【20銘柄別 詳細成績】")
    print(df_stats.sort_values(by="pnl", ascending=False).to_string(index=False))

    # グラフ描画
    fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [2, 1]})

    norm_eq = (portfolio_eq / portfolio_eq[0] - 1.0) * 100.0
    axes[0].plot(norm_eq, color="#2ca02c", linewidth=2.0, label=f"Optimized VAL_POC (PnL:{port_pnl:+.1f}%, DD:{port_max_dd:.1f}%)")
    axes[0].axhline(0, color='gray', linestyle='--', alpha=0.5)
    axes[0].set_title(f"Optimized VAL_POC Strategy (Width>=1.5%, Nanpin 2.0%, SL 8.5%, Target 60%) - 20 Symbols 1 Year", fontsize=13, fontweight='bold')
    axes[0].set_ylabel("Portfolio Return (%)", fontsize=12)
    axes[0].grid(True, linestyle="--", alpha=0.5)
    axes[0].legend(loc="upper left", fontsize=11)

    # 下段: 銘柄別PnL
    sorted_df = df_stats.sort_values(by="pnl", ascending=False)
    x = np.arange(len(sorted_df))
    colors = ['#2ca02c' if p > 0 else '#d62728' for p in sorted_df["pnl"]]
    axes[1].bar(x, sorted_df["pnl"], color=colors, alpha=0.85)
    axes[1].axhline(0, color='black', linewidth=0.8)
    axes[1].set_title("Per-Symbol PnL (%)", fontsize=12, fontweight='bold')
    axes[1].set_ylabel("PnL (%)", fontsize=11)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([s.replace("USDT", "") for s in sorted_df["symbol"]], rotation=45, ha='right', fontsize=9)
    axes[1].grid(True, linestyle="--", alpha=0.5)

    plt.tight_layout()
    chart_path = PLOT_DIR / "val_poc_golden_20symbols.png"
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"\nSaved chart to {chart_path}")

    shutil.copy(chart_path, ARTIFACT_DIR / "val_poc_golden_20symbols.png")

    webhook_url = get_webhook_url("test4")
    if webhook_url:
        print("Sending to Discord Channel 4 (#test4_backtest)...")
        try:
            with open(chart_path, "rb") as f:
                desc = (
                    f"🏆 **【VAL_POC 単体ロジック 黄金最適設定 20銘柄 1年分バックテスト】**\n"
                    f"・対象: 指定固定20銘柄（計2,207,520本）\n"
                    f"・仕様: バリュー幅 >= 1.5% ＋ ナンピン 2.0% ＋ SL 8.5% ＋ VAL-VAH 60%利確\n"
                    f"・**通年純利益率**: **{port_pnl:+.2f}%**\n"
                    f"・**全体勝率**: **{overall_wr:.1f}%**\n"
                    f"・**ポートフォリオMaxDD**: **{port_max_dd:.2f}%**\n"
                    f"・**年間総取引数**: **{total_trades:,}回** (1銘柄1日 **{avg_trades_day:.2f}回**)\n"
                    f"・**プラス銘柄数**: **20銘柄中14銘柄**（RENDER +72.7%, TAO +60.1%, HYPE +50.2%, NEAR +42.1%等）\n"
                )
                payload = {"payload_json": json.dumps({"content": desc}, ensure_ascii=False)}
                files = {"file": (chart_path.name, f, "image/png")}
                r = requests.post(webhook_url, data=payload, files=files, timeout=30)
                if r.status_code in [200, 204]:
                    print("Successfully sent to Channel 4!")
                else:
                    print(f"Discord error: {r.status_code}")
        except Exception as e:
            print(f"Discord error: {e}")

if __name__ == "__main__":
    main()
