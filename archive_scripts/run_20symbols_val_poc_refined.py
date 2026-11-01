#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VAL_POC ボリュームプロファイル単体ロジックの徹底洗練バックテスト (20銘柄 1年分)
・バリューエリア幅フィルター (VA Width >= 1.2%, 1.5%, 1.8%)
・利確ターゲット比率 (60%, 70%)
・Discord チャンネル4 (#test4_backtest) 送信
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

def simulate_refined(
    df_mp: pd.DataFrame,
    target_ratio: float = 0.60,
    min_va_width: float = 0.015,
    nanpin_drop: float = 0.020,
    sl_pct: float = 0.10,
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
    print(" 【VAL_POC ボリュームプロファイル単体ロジック洗練 20銘柄バックテスト】")
    print("=" * 70)

    mp_dict = load_all_cache()
    print(f"Loaded {len(mp_dict)} symbols.")

    configs = [
        ("Base: Ratio 0.60, Width 0.0%", 0.60, 0.0),
        ("Refined 1: Ratio 0.60, Width >= 1.2%", 0.60, 0.012),
        ("Refined 2: Ratio 0.60, Width >= 1.5%", 0.60, 0.015),
        ("Refined 3: Ratio 0.70, Width >= 1.2%", 0.70, 0.012),
        ("Refined 4: Ratio 0.70, Width >= 1.5%", 0.70, 0.015),
        ("Refined 5: Ratio 0.70, Width >= 1.8%", 0.70, 0.018),
    ]

    all_res = {}

    for name, ratio, width in configs:
        sym_stats = []
        portfolio_eq = None
        total_trades = 0
        total_wins = 0

        for sym in TARGET_SYMBOLS:
            if sym not in mp_dict:
                continue
            df_mp = mp_dict[sym]
            res = simulate_refined(df_mp, target_ratio=ratio, min_va_width=width, nanpin_drop=0.020, sl_pct=0.10)
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

        df_stats = pd.DataFrame(sym_stats)
        all_res[name] = {
            "df_stats": df_stats,
            "port_pnl": port_pnl,
            "overall_wr": overall_wr,
            "total_trades": total_trades,
            "avg_trades_day": avg_trades_day,
            "port_max_dd": port_max_dd,
            "portfolio_eq": portfolio_eq
        }
        print(f"{name:38s}: PnL={port_pnl:+6.2f}%, WR={overall_wr:5.1f}%, DD={port_max_dd:5.2f}%, Trades={total_trades:5d} ({avg_trades_day:.2f}/day)")

    print("\n" + "=" * 70)
    print(" 【各設定のポートフォリオ通年成績サマリー】")
    print("=" * 70)
    summary_rows = []
    for name, d in all_res.items():
        summary_rows.append({
            "Setting": name,
            "Portfolio PnL": f"{d['port_pnl']:+.2f}%",
            "Win Rate": f"{d['overall_wr']:.1f}%",
            "Total Trades": f"{d['total_trades']:,}",
            "Trades/Day": f"{d['avg_trades_day']:.2f}",
            "MaxDD": f"{d['port_max_dd']:.2f}%"
        })
    print(pd.DataFrame(summary_rows).to_string(index=False))

    # 最良設定の銘柄別内訳表示
    best_name = max(all_res.keys(), key=lambda k: all_res[k]["port_pnl"])
    print(f"\n【最良設定 ({best_name}) 銘柄別詳細】")
    best_df = all_res[best_name]["df_stats"]
    print(best_df.sort_values(by="pnl", ascending=False).to_string(index=False))

    # グラフ描画
    fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [2, 1]})
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
    for idx, (name, d) in enumerate(all_res.items()):
        eq = d["portfolio_eq"]
        norm_eq = (eq / eq[0] - 1.0) * 100.0
        axes[0].plot(norm_eq, label=f"{name} (PnL:{d['port_pnl']:+.1f}%, DD:{d['port_max_dd']:.1f}%)", color=colors[idx % len(colors)], linewidth=1.5)

    axes[0].set_title("20 Symbols 5m VAL_POC Refinement - 1 Year Performance Comparison", fontsize=14, fontweight='bold')
    axes[0].set_ylabel("Portfolio Return (%)", fontsize=12)
    axes[0].grid(True, linestyle="--", alpha=0.5)
    axes[0].legend(loc="upper left", fontsize=10)

    # 下段: 銘柄別PnL比較 (Base vs Best)
    df_base = all_res["Base: Ratio 0.60, Width 0.0%"]["df_stats"]
    merged = pd.merge(df_base, best_df, on="symbol", suffixes=('_base', '_best'))
    x = np.arange(len(TARGET_SYMBOLS))
    width = 0.35
    axes[1].bar(x - width/2, merged["pnl_base"], width, label="Base (Ratio 0.60, Width 0.0%)", color="#1f77b4", alpha=0.8)
    axes[1].bar(x + width/2, merged["pnl_best"], width, label=f"Best ({best_name})", color="#2ca02c", alpha=0.8)
    axes[1].set_title(f"Per-Symbol PnL: Base vs Best ({best_name})", fontsize=12, fontweight='bold')
    axes[1].set_ylabel("PnL (%)", fontsize=11)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([s.replace("USDT", "") for s in merged["symbol"]], rotation=45, ha='right', fontsize=9)
    axes[1].grid(True, linestyle="--", alpha=0.5)
    axes[1].legend(loc="upper left")

    plt.tight_layout()
    chart_path = PLOT_DIR / "val_poc_refined_20symbols.png"
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"\nSaved chart to {chart_path}")

    shutil.copy(chart_path, ARTIFACT_DIR / "val_poc_refined_20symbols.png")

    webhook_url = get_webhook_url("test4")
    if webhook_url:
        print("Sending to Discord Channel 4 (#test4_backtest)...")
        try:
            with open(chart_path, "rb") as f:
                d_best = all_res[best_name]
                desc = (
                    f"🔥 **【VAL_POC ボリュームプロファイル単体ロジック洗練 20銘柄バックテスト】**\n"
                    f"・対象: 指定固定20銘柄（計2,207,520本）\n"
                    f"・最良設定: **{best_name}**\n"
                    f"  - 純利益率: **{d_best['port_pnl']:+.2f}%**\n"
                    f"  - 全体勝率: **{d_best['overall_wr']:.1f}%**\n"
                    f"  - 年間総取引数: **{d_best['total_trades']:,}回** (1銘柄1日 **{d_best['avg_trades_day']:.2f}回**)\n"
                    f"  - ポートフォリオMaxDD: **{d_best['port_max_dd']:.2f}%**\n"
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
