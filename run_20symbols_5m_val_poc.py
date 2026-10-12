#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定固定20銘柄 1年分5分足データ (計2,207,520本) による
VAL_POC戦略 (出来高プロファイル反発 + 段階的ナンピン + 建値以上Quick TP + SL 8〜10%) 完全精密バックテスト
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
from concurrent.futures import ProcessPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from config_loader import get_webhook_url
from bingx5_46_3logic import logicinstance

TARGET_SYMBOLS = [
    'HYPEUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'SUIUSDT',
    'AVAXUSDT', 'LINKUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'UNIUSDT', 'OPUSDT', 'TIAUSDT', 'RENDERUSDT', 'INJUSDT',
    'FETUSDT', 'TAOUSDT', 'AAVEUSDT', 'ENAUSDT', 'ZECUSDT'
]

DATA_DIR = WORKSPACE / "Data/binance_candles_5m"
PLOT_DIR = WORKSPACE / "Data/plots"
PLOT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR = Path("C:/Users/user/.gemini/antigravity/brain/ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")


def compute_vp_for_symbol(sym: str, period: int = 150, num_bins: int = 50):
    """銘柄の5分足データを読み込み、マーケットプロファイル (VAL, POC, VAH) を算出 (キャッシュ対応)"""
    cache_file = DATA_DIR / f"{sym}_5m_mp.pkl"
    if cache_file.exists():
        df_mp = pd.read_pickle(cache_file)
        return sym, df_mp

    csv_file = DATA_DIR / f"{sym}_5m.csv"
    if not csv_file.exists():
        return None
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values(by='timestamp', inplace=True)
    df.reset_index(drop=True, inplace=True)
    if len(df) < 100:
        return None
    
    logic = logicinstance()
    df_mp = logic.make_market_profile(df, period=period, num_bins=num_bins)
    try:
        df_mp.to_pickle(cache_file)
    except Exception:
        pass
    return sym, df_mp


def simulate_val_poc_fast(
    df_mp: pd.DataFrame,
    max_nanpin: int = 4,
    nanpin_mode: str = "stage_1.5_2.5",  # "calc_add_pct", "flat_1.5", "stage_1.5_2.5", "flat_2.0"
    sl_pct: float = 0.09,
    initial_equity: float = 100.0,
    fee_rate: float = 0.0006,
    maker_fee_rate: float = 0.0002
) -> dict:
    closes = df_mp["close"].values
    highs = df_mp["high"].values if "high" in df_mp.columns else closes
    lows = df_mp["low"].values if "low" in df_mp.columns else closes
    vals = df_mp["VAL"].values
    pocs = df_mp["POC"].values
    timestamps = df_mp["timestamp"].values
    n = len(closes)

    total_max_stages = max_nanpin + 1  # 5ポジション
    trade_size_usdt = initial_equity / total_max_stages

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    last_entry_price = 0.0

    pending_entry = None
    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]
    trade_dates = []

    def get_nanpin_pct(stage_idx: int) -> float:
        if nanpin_mode == "calc_add_pct":
            return 0.004 if stage_idx <= 3 else 0.006
        elif nanpin_mode == "flat_1.5":
            return 0.015
        elif nanpin_mode == "flat_2.0":
            return 0.020
        elif nanpin_mode == "stage_1.5_2.5":
            # 1〜2回目: 1.5%, 3〜4回目: 2.5%
            return 0.015 if stage_idx <= 2 else 0.025
        return 0.015

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        prev_val = vals[i-1]
        poc = pocs[i]
        ts = timestamps[i]

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

        # 1. 決済チェック (保有ポジションがある場合)
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
                trade_dates.append(ts)
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry_price = 0.0

            # B. POC クローズ (Quick TP: 建値以上限定)
            elif not np.isnan(poc) and (h >= poc or c >= poc):
                exit_price = max(c, poc) if h >= poc else c
                if exit_price > avg_price * (1.0 + fee_rate):
                    sell_val = pos_qty * exit_price
                    fee = sell_val * fee_rate
                    pnl = sell_val - pos_cost - fee
                    cum_realized_pnl += pnl
                    cum_fees += fee
                    trades.append(pnl)
                    trade_dates.append(ts)
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_price = 0.0
                    pos_count = 0
                    last_entry_price = 0.0

        # 2. エントリー / ナンピン判定
        if not np.isnan(val) and not np.isnan(prev_val) and pending_entry is None:
            if pos_count == 0:
                # VAL ゴールデンクロス
                if (closes[i-1] <= prev_val) and (c > val):
                    pending_entry = {
                        "limit_price": c * 0.999,
                        "type": "ENTRY"
                    }
            elif 1 <= pos_count < total_max_stages:
                req_drop = get_nanpin_pct(pos_count)
                if (c <= last_entry_price * (1.0 - req_drop)) or (l <= last_entry_price * (1.0 - req_drop)):
                    pending_entry = {
                        "limit_price": c * 0.999,
                        "type": f"NANPIN_{pos_count}"
                    }

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

    return {
        "final_pnl": final_pnl,
        "win_rate": win_rate,
        "trade_count": total_trades,
        "DD_max": dd_max,
        "equity_curve": equity_curve,
        "trades": trades,
        "trade_dates": trade_dates
    }


def main():
    print("=" * 70)
    print(" 5分足固定20銘柄 1年分 VAL_POC 戦略 精密バックテスト開始")
    print(" 銘柄数: 20, データ期間: 1年 (約10.5万本/銘柄, 合計約220万本)")
    print("=" * 70)

    t0 = time.time()
    print("[1/3] マーケットプロファイル (VAL, POC, VAH) 並列計算中...")
    
    mp_cache = {}
    with ProcessPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(compute_vp_for_symbol, sym): sym for sym in TARGET_SYMBOLS}
        for fut in futures:
            res = fut.result()
            if res is not None:
                sym, df_mp = res
                mp_cache[sym] = df_mp
                print(f"  ✓ {sym:10s} (bars: {len(df_mp):6d}) 計算完了")

    print(f"VP並列計算完了 (所要時間: {time.time() - t0:.1f}s)")

    # パラメータ設定の比較
    configs = [
        {"name": "Default (Nanpin 0.4/0.6%, SL 9%)", "mode": "calc_add_pct", "sl": 0.09},
        {"name": "Nanpin 1.5% flat, SL 9%", "mode": "flat_1.5", "sl": 0.09},
        {"name": "Nanpin 1.5% flat, SL 10%", "mode": "flat_1.5", "sl": 0.10},
        {"name": "Nanpin 2.0% flat, SL 10%", "mode": "flat_2.0", "sl": 0.10},
        {"name": "Staged Nanpin 1.5/2.5%, SL 10%", "mode": "stage_1.5_2.5", "sl": 0.10},
    ]

    print("\n[2/3] 各パラメータでのシミュレーション実行中...")
    all_config_results = {}

    for cfg in configs:
        cfg_name = cfg["name"]
        mode = cfg["mode"]
        sl = cfg["sl"]
        print(f"\n--- 検証中: {cfg_name} ---")

        symbol_stats = []
        portfolio_eq = None
        total_trades = 0
        total_wins = 0

        for sym in TARGET_SYMBOLS:
            if sym not in mp_cache:
                continue
            df_mp = mp_cache[sym]
            res = simulate_val_poc_fast(df_mp, max_nanpin=4, nanpin_mode=mode, sl_pct=sl)
            symbol_stats.append({
                "symbol": sym,
                "pnl": res["final_pnl"],
                "win_rate": res["win_rate"],
                "trades": res["trade_count"],
                "dd_max": res["DD_max"]
            })
            total_trades += res["trade_count"]
            total_wins += int(res["trade_count"] * res["win_rate"] / 100.0)

            # ポートフォリオ合算曲線
            eq = np.array(res["equity_curve"])
            if portfolio_eq is None:
                portfolio_eq = eq.copy()
            else:
                min_len = min(len(portfolio_eq), len(eq))
                portfolio_eq = portfolio_eq[:min_len] + eq[:min_len]

        df_stats = pd.DataFrame(symbol_stats)
        port_initial = 100.0 * len(symbol_stats)
        port_final = portfolio_eq[-1]
        port_pnl = ((port_final - port_initial) / port_initial) * 100.0
        overall_wr = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0

        # ポートフォリオ MaxDD
        port_series = pd.Series(portfolio_eq)
        port_peak = port_series.cummax()
        port_dd = (port_peak - port_series) / port_peak * 100.0
        port_max_dd = port_dd.max()

        avg_trades_per_day = total_trades / (365.0 * len(symbol_stats))

        print(f"  >> 合計損益: {port_pnl:+.2f}%, 勝率: {overall_wr:.1f}%, 取引総数: {total_trades:,}回 (1銘柄1日: {avg_trades_per_day:.2f}回), 最大DD: {port_max_dd:.2f}%")
        all_config_results[cfg_name] = {
            "df_stats": df_stats,
            "port_pnl": port_pnl,
            "overall_wr": overall_wr,
            "total_trades": total_trades,
            "avg_trades_per_day": avg_trades_per_day,
            "port_max_dd": port_max_dd,
            "portfolio_eq": portfolio_eq
        }

    # 最良設定の選定と詳細表示
    print("\n" + "=" * 70)
    print(" 【各設定のポートフォリオ通年成績サマリー】")
    print("=" * 70)
    summary_rows = []
    for name, data in all_config_results.items():
        summary_rows.append({
            "設定名": name,
            "純利益率(%)": f"{data['port_pnl']:+.2f}%",
            "全体勝率(%)": f"{data['overall_wr']:.1f}%",
            "年間総取引数": f"{data['total_trades']:,}回",
            "1銘柄1日取引数": f"{data['avg_trades_per_day']:.2f}回",
            "ポートフォリオMaxDD": f"{data['port_max_dd']:.2f}%"
        })
    df_summary = pd.DataFrame(summary_rows)
    print(df_summary.to_string(index=False))

    # 最良設定（または代表設定）の銘柄別詳細テーブル表示
    best_cfg_name = "Nanpin 2.0% flat, SL 10%"
    if best_cfg_name in all_config_results:
        print(f"\n【{best_cfg_name} 銘柄別詳細内訳】")
        best_df = all_config_results[best_cfg_name]["df_stats"]
        print(best_df.sort_values(by="pnl", ascending=False).to_string(index=False))

    # グラフ描画
    print("\n[3/3] 比較チャート画像生成中...")
    fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=False, gridspec_kw={'height_ratios': [2, 1]})

    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
    for idx, (name, data) in enumerate(all_config_results.items()):
        eq = data["portfolio_eq"]
        normalized_eq = (eq / eq[0] - 1.0) * 100.0  # %リターン
        axes[0].plot(normalized_eq, label=f"{name} (PnL:{data['port_pnl']:+.1f}%, DD:{data['port_max_dd']:.1f}%)", color=colors[idx % len(colors)], linewidth=1.5)

    axes[0].set_title("20 Symbols 5m VAL_POC Strategy - 1 Year Performance Comparison", fontsize=14, fontweight='bold')
    axes[0].set_ylabel("Portfolio Return (%)", fontsize=12)
    axes[0].grid(True, linestyle="--", alpha=0.5)
    axes[0].legend(loc="upper left", fontsize=10)

    # 下段: 銘柄別PnL比較 (ベスト設定 vs 原本規約)
    cfg_base = all_config_results["Default (Nanpin 0.4/0.6%, SL 9%)"]["df_stats"]
    cfg_opt = all_config_results[best_cfg_name]["df_stats"]

    x = np.arange(len(TARGET_SYMBOLS))
    width = 0.35

    # 銘柄順を揃える
    merged_stats = pd.merge(cfg_base, cfg_opt, on="symbol", suffixes=('_base', '_opt'))
    axes[1].bar(x - width/2, merged_stats["pnl_base"], width, label="Default (0.4/0.6%, SL 9%)", color="#1f77b4", alpha=0.8)
    axes[1].bar(x + width/2, merged_stats["pnl_opt"], width, label="Nanpin 2.0%, SL 10%", color="#2ca02c", alpha=0.8)
    axes[1].set_title("Per-Symbol PnL Comparison (Default vs Nanpin 2.0%)", fontsize=12, fontweight='bold')
    axes[1].set_ylabel("PnL (%)", fontsize=11)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([s.replace("USDT", "") for s in merged_stats["symbol"]], rotation=45, ha='right', fontsize=9)
    axes[1].grid(True, linestyle="--", alpha=0.5)
    axes[1].legend(loc="upper left")

    plt.tight_layout()
    chart_path = PLOT_DIR / "val_poc_5m_20symbols_comparison.png"
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"✓ チャート保存完了: {chart_path}")

    # アーティファクトディレクトリへコピー
    artifact_chart = ARTIFACT_DIR / "val_poc_5m_20symbols_comparison.png"
    shutil.copy(chart_path, artifact_chart)

    # Discord 送信 (チャンネル4: #test4_backtest 厳守)
    webhook_url = get_webhook_url("test4")
    if webhook_url:
        print("\nDiscord (#test4_backtest) へチャート送信中...")
        try:
            with open(chart_path, "rb") as f:
                best_data = all_config_results[best_cfg_name]
                desc = (
                    f"📊 **【5分足 VAL_POC 戦略 20銘柄 1年分バックテスト結果】**\n"
                    f"・対象: 指定固定20銘柄（計2,207,520本）\n"
                    f"・戦略: 出来高プロファイル VAL 反発 + 段階ナンピン + 建値以上Quick TP\n"
                    f"・最良設定: **{best_cfg_name}**\n"
                    f"  - 全体勝率: **{best_data['overall_wr']:.1f}%**\n"
                    f"  - 年間総取引数: **{best_data['total_trades']:,}回** (1銘柄あたり1日 **{best_data['avg_trades_per_day']:.2f}回**)\n"
                    f"  - 通年純利益率: **{best_data['port_pnl']:+.2f}%**\n"
                    f"  - ポートフォリオMaxDD: **{best_data['port_max_dd']:.2f}%**\n"
                )
                payload = {"payload_json": json.dumps({"content": desc}, ensure_ascii=False)}
                files = {"file": (chart_path.name, f, "image/png")}
                r = requests.post(webhook_url, data=payload, files=files, timeout=30)
                if r.status_code in [200, 204]:
                    print("✓ Discord送信成功！")
                else:
                    print(f"Discord送信失敗: {r.status_code}, {r.text}")
        except Exception as e:
            print(f"Discord送信エラー: {e}")


if __name__ == "__main__":
    main()
