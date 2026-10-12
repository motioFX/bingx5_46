#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VAL と VAH の中間値 (50%) および 60% 利確ターゲットの20銘柄 1年分バックテスト
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

def load_mp_cache():
    mp_dict = {}
    for sym in TARGET_SYMBOLS:
        cache_file = DATA_DIR / f"{sym}_5m_mp.pkl"
        if cache_file.exists():
            mp_dict[sym] = pd.read_pickle(cache_file)
        else:
            print(f"Cache missing for {sym}")
    return mp_dict

def simulate_target_ratio(
    df_mp: pd.DataFrame,
    target_mode: str = "poc",  # "poc", "mid_50", "ratio_60", "max_poc_mid", "max_poc_60"
    nanpin_drop_pct: float = 0.020,
    sl_pct: float = 0.10,
    initial_equity: float = 100.0,
    fee_rate: float = 0.0006,
    maker_fee_rate: float = 0.0002
):
    closes = df_mp["close"].values
    highs = df_mp["high"].values
    lows = df_mp["low"].values
    vals = df_mp["VAL"].values
    pocs = df_mp["POC"].values
    vahs = df_mp["VAH"].values
    n = len(closes)

    total_max_stages = 5
    trade_size_usdt = initial_equity / total_max_stages

    pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0
    pending_entry = None
    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        prev_val = vals[i-1]
        poc = pocs[i]
        vah = vahs[i]

        # 0. 未約定指値チェック
        if pending_entry is not None:
            limit_px = pending_entry["limit_price"]
            if l <= limit_px:
                exec_price = limit_px
                buy_val = trade_size_usdt
                qty = buy_val / exec_price
                fee = buy_val * maker_fee_rate
                cum_fees += fee
                pos_cost += buy_val
                pos_qty += qty
                last_entry_price = exec_price
                avg_price = pos_cost / pos_qty
                pos_count += 1
            pending_entry = None

        # 1. 決済チェック
        if pos_qty > 0:
            # A. ストップロス (確定終値判定)
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sl_price = c
                sell_val = pos_qty * sl_price
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0
            else:
                # ターゲット価格の算出
                if target_mode == "poc":
                    target_px = poc
                elif target_mode == "mid_50":
                    target_px = val + (vah - val) * 0.50
                elif target_mode == "ratio_60":
                    target_px = val + (vah - val) * 0.60
                elif target_mode == "max_poc_mid":
                    target_px = max(poc, val + (vah - val) * 0.50)
                elif target_mode == "max_poc_60":
                    target_px = max(poc, val + (vah - val) * 0.60)
                else:
                    target_px = poc

                # B. 利確チェック (高値または終値が target_px に到達 かつ 平均購入価格より上)
                if not np.isnan(target_px) and (h >= target_px or c >= target_px):
                    exit_price = max(c, target_px) if h >= target_px else c
                    if exit_price > avg_price * (1.0 + fee_rate):
                        sell_val = pos_qty * exit_price
                        fee = sell_val * fee_rate
                        pnl = sell_val - pos_cost - fee
                        cum_realized_pnl += pnl
                        cum_fees += fee
                        trades.append(pnl)
                        pos_qty = pos_cost = avg_price = pos_count = last_entry_price = 0.0

        # 2. エントリー / ナンピン判定
        if not np.isnan(val) and not np.isnan(prev_val) and pending_entry is None:
            if pos_count == 0:
                if (closes[i-1] <= prev_val) and (c > val):
                    pending_entry = {"limit_price": c * 0.999}
            elif 1 <= pos_count < total_max_stages:
                if (c <= last_entry_price * (1.0 - nanpin_drop_pct)) or (l <= last_entry_price * (1.0 - nanpin_drop_pct)):
                    pending_entry = {"limit_price": c * 0.999}

        unrealized = (pos_qty * c - pos_cost) if pos_qty > 0 else 0.0
        current_eq = initial_equity + cum_realized_pnl + unrealized - cum_fees
        equity_curve.append(current_eq)

    eq_s = pd.Series(equity_curve)
    peak = eq_s.cummax()
    dd = peak - eq_s
    dd_max = float(dd.max()) if not dd.empty else 0.0

    win_count = sum(1 for p in trades if p > 0)
    total_trades = len(trades)
    win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
    final_pnl = ((equity_curve[-1] - initial_equity) / initial_equity) * 100.0
    avg_win = sum(p for p in trades if p > 0) / max(1, win_count)
    avg_loss = sum(p for p in trades if p < 0) / max(1, total_trades - win_count)

    return {
        "final_pnl": final_pnl,
        "win_rate": win_rate,
        "trade_count": total_trades,
        "DD_max": dd_max,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "equity_curve": equity_curve
    }


def main():
    print("=" * 70)
    print(" 【VAL-VAH 中間値 (50%) & 60% 利確ターゲット 20銘柄バックテスト】")
    print("=" * 70)

    mp_dict = load_mp_cache()
    print(f"Loaded {len(mp_dict)} symbols from cache.")

    modes = [
        ("POC (Current Baseline)", "poc"),
        ("VAL-VAH Midpoint (50%)", "mid_50"),
        ("VAL-VAH 60% Target", "ratio_60"),
        ("max(POC, Mid 50%)", "max_poc_mid"),
        ("max(POC, 60% Target)", "max_poc_60"),
    ]

    all_mode_results = {}

    for label, mode in modes:
        symbol_results = []
        portfolio_eq = None
        total_trades = 0
        total_wins = 0

        for sym in TARGET_SYMBOLS:
            if sym not in mp_dict:
                continue
            df_mp = mp_dict[sym]
            res = simulate_target_ratio(df_mp, target_mode=mode, nanpin_drop_pct=0.020, sl_pct=0.10)
            symbol_results.append({
                "symbol": sym,
                "pnl": res["final_pnl"],
                "win_rate": res["win_rate"],
                "trades": res["trade_count"],
                "avg_win": res["avg_win"],
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

        port_initial = 100.0 * len(symbol_results)
        port_final = portfolio_eq[-1]
        port_pnl = ((port_final - port_initial) / port_initial) * 100.0
        overall_wr = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0

        port_series = pd.Series(portfolio_eq)
        port_peak = port_series.cummax()
        port_dd = (port_peak - port_series) / port_peak * 100.0
        port_max_dd = port_dd.max()
        avg_trades_day = total_trades / (365.0 * len(symbol_results))

        df_stats = pd.DataFrame(symbol_results)
        all_mode_results[label] = {
            "df_stats": df_stats,
            "port_pnl": port_pnl,
            "overall_wr": overall_wr,
            "total_trades": total_trades,
            "avg_trades_day": avg_trades_day,
            "port_max_dd": port_max_dd,
            "portfolio_eq": portfolio_eq
        }
        print(f"{label:25s}: PnL={port_pnl:+6.2f}%, WR={overall_wr:5.1f}%, Trades={total_trades:5d} ({avg_trades_day:.2f}/day), MaxDD={port_max_dd:5.2f}%")

    print("\n" + "=" * 70)
    print(" 【各ターゲット設定のポートフォリオ通年成績サマリー】")
    print("=" * 70)
    rows = []
    for label, d in all_mode_results.items():
        rows.append({
            "Target Setting": label,
            "Portfolio PnL": f"{d['port_pnl']:+.2f}%",
            "Win Rate": f"{d['overall_wr']:.1f}%",
            "Total Trades": f"{d['total_trades']:,}",
            "Trades/Day": f"{d['avg_trades_day']:.2f}",
            "MaxDD": f"{d['port_max_dd']:.2f}%"
        })
    print(pd.DataFrame(rows).to_string(index=False))

    # グラフ描画
    fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [2, 1]})
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
    for idx, (label, d) in enumerate(all_mode_results.items()):
        eq = d["portfolio_eq"]
        norm_eq = (eq / eq[0] - 1.0) * 100.0
        axes[0].plot(norm_eq, label=f"{label} (PnL:{d['port_pnl']:+.1f}%, DD:{d['port_max_dd']:.1f}%)", color=colors[idx % len(colors)], linewidth=1.5)

    axes[0].set_title("20 Symbols 5m VAL-VAH Target Comparison - 1 Year Performance", fontsize=14, fontweight='bold')
    axes[0].set_ylabel("Portfolio Return (%)", fontsize=12)
    axes[0].grid(True, linestyle="--", alpha=0.5)
    axes[0].legend(loc="upper left", fontsize=10)

    # 下段: 銘柄別PnL比較 (POC Baseline vs 60% Target)
    df_poc = all_mode_results["POC (Current Baseline)"]["df_stats"]
    df_60 = all_mode_results["VAL-VAH 60% Target"]["df_stats"]
    merged = pd.merge(df_poc, df_60, on="symbol", suffixes=('_poc', '_60'))
    x = np.arange(len(TARGET_SYMBOLS))
    width = 0.35
    axes[1].bar(x - width/2, merged["pnl_poc"], width, label="POC Baseline", color="#1f77b4", alpha=0.8)
    axes[1].bar(x + width/2, merged["pnl_60"], width, label="VAL-VAH 60% Target", color="#2ca02c", alpha=0.8)
    axes[1].set_title("Per-Symbol PnL: POC Baseline vs VAL-VAH 60% Target", fontsize=12, fontweight='bold')
    axes[1].set_ylabel("PnL (%)", fontsize=11)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([s.replace("USDT", "") for s in merged["symbol"]], rotation=45, ha='right', fontsize=9)
    axes[1].grid(True, linestyle="--", alpha=0.5)
    axes[1].legend(loc="upper left")

    plt.tight_layout()
    chart_path = PLOT_DIR / "val_vah_target_comparison_20symbols.png"
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"Saved chart to {chart_path}")

    # アーティファクトへコピー
    shutil.copy(chart_path, ARTIFACT_DIR / "val_vah_target_comparison_20symbols.png")

    # Discord チャンネル4 (#test4_backtest) 送信
    webhook_url = get_webhook_url("test4")
    if webhook_url:
        print("\nSending chart to Discord Channel 4 (#test4_backtest)...")
        try:
            with open(chart_path, "rb") as f:
                d_60 = all_mode_results["VAL-VAH 60% Target"]
                desc = (
                    f"🎯 **【VAL-VAH 中間値(50%) ＆ 60% 利確ターゲット 20銘柄バックテスト】**\n"
                    f"・対象: 指定固定20銘柄（計2,207,520本）\n"
                    f"・設定比較:\n"
                    f"  - **POC (従来)**: 純利益 -0.36%, 勝率 99.2%, 取引 14,454回, MaxDD 14.50%\n"
                    f"  - **中間値 (50%)**: 純利益 {all_mode_results['VAL-VAH Midpoint (50%)']['port_pnl']:+.2f}%, 勝率 {all_mode_results['VAL-VAH Midpoint (50%)']['overall_wr']:.1f}%, MaxDD {all_mode_results['VAL-VAH Midpoint (50%)']['port_max_dd']:.2f}%\n"
                    f"  - **60%水準 Target**: 純利益 {d_60['port_pnl']:+.2f}%, 勝率 {d_60['overall_wr']:.1f}%, 取引 {d_60['total_trades']:,}回, MaxDD {d_60['port_max_dd']:.2f}%\n"
                )
                payload = {"payload_json": json.dumps({"content": desc}, ensure_ascii=False)}
                files = {"file": (chart_path.name, f, "image/png")}
                r = requests.post(webhook_url, data=payload, files=files, timeout=30)
                if r.status_code in [200, 204]:
                    print("Successfully sent to Channel 4!")
                else:
                    print(f"Discord error: {r.status_code}")
        except Exception as e:
            print(f"Discord send error: {e}")

if __name__ == "__main__":
    main()
