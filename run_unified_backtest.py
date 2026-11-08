#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_unified_backtest.py
================================================================================
🚀 bingx5_46 公式統一長期バックテストランナー (SSoT: Single Source of Truth)
================================================================================
本番ボット（bingx5_46_3logic.py）のシミュレーションエンジンを直接 import して実行する
公式長期検証ランナー。実運用コードとのロジック乖離を物理的に 100% 根絶します。

【準拠仕様】
  1. セッション分離: 東京（09-16h JST）レンジ逆張り / 欧州（16-22h JST）ブレイクアウト
  2. 厳格成行利確: 高値/安値がTP到達時に目標TP価格そのもので約定 (過大計上排除)
  3. ブレイクアウト直前滞在率 70% ゲート & W = int(period * 0.25) [15〜40本]
  4. 実効手数料（Maker 0.02% / Taker 0.05%）& 動的MAEストップ & 動的タイムアウト

使用方法:
  python run_unified_backtest.py --days 30 --symbols SUI-USDT,BTC-USDT
  python run_unified_backtest.py --days 65 --all --export-csv
  python run_unified_backtest.py --days 30 --side short --export-csv
"""

import os
import sys
import argparse
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd

# bingx5_46_3logic から本番と同一の関数を直接インポート (SSoT)
from bingx5_46_3logic import (
    run_interval_comparison,
    _run_interval_comparison_short,
    make_market_profile_dual_va,
    calc_rsi_ema,
    build_entry_precision_filter,
    simulate_val_poc_fast,
    simulate_vah_poc_fast,
    simulate_vah_break_fast,
    simulate_val_break_fast
)

CANDLE_DIR = Path("Data/candles_5m")
OUTPUT_DIR = Path("backtest_data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT",
    "SUI-USDT", "TAO-USDT", "ENA-USDT", "APT-USDT", "NEAR-USDT",
    "LINK-USDT", "AVAX-USDT", "AAVE-USDT", "RENDER-USDT", "TAO-USDT",
    "SEI-USDT", "OP-USDT", "ARB-USDT", "FET-USDT", "TIA-USDT", "PEPE-USDT"
]

def load_candle_data(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """ローカルSSoT時系列ストアから指定期間の5分足データを取得"""
    fpath = CANDLE_DIR / f"candles_5m_{symbol}.csv"
    if not fpath.exists():
        return None
    df = pd.read_csv(fpath)
    if df.empty or len(df) < 500:
        return None
    
    # 1日あたり288本 (24h * 12)
    req_bars = int(days * 288)
    if len(df) < req_bars:
        df_sub = df.copy().reset_index(drop=True)
    else:
        df_sub = df.iloc[-req_bars:].copy().reset_index(drop=True)
    
    return df_sub


def extract_detailed_trades(
    symbol: str,
    side: str,
    df_chart: pd.DataFrame,
    initial_equity: float = 100.0,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.0006
) -> List[Dict[str, Any]]:
    """チャート用DataFrameから個別トレード明細を抽出"""
    trades = []
    if df_chart is None or df_chart.empty:
        return trades

    exec_buys = df_chart.get('exec_buy_price', pd.Series(np.nan, index=df_chart.index)).values
    exec_sells = df_chart.get('exec_sell_price', pd.Series(np.nan, index=df_chart.index)).values
    timestamps = df_chart['timestamp'].values if 'timestamp' in df_chart.columns else np.arange(len(df_chart))
    
    side_lower = side.lower()
    in_pos = False
    entry_idx = -1
    entry_px = 0.0
    entry_ts = ""

    for i in range(len(df_chart)):
        if side_lower == "long":
            # ロング: exec_buy でエントリー、exec_sell でエグジット
            if not np.isnan(exec_buys[i]) and not in_pos:
                in_pos = True
                entry_idx = i
                entry_px = float(exec_buys[i])
                entry_ts = str(timestamps[i])
            elif not np.isnan(exec_sells[i]) and in_pos:
                exit_px = float(exec_sells[i])
                exit_ts = str(timestamps[i])
                qty = initial_equity / entry_px
                cost = initial_equity + initial_equity * maker_fee
                revenue = qty * exit_px
                fee_exit = revenue * taker_fee
                pnl = revenue - cost - fee_exit
                ret_pct = (pnl / initial_equity) * 100.0
                bars_held = i - entry_idx
                trades.append({
                    "symbol": symbol,
                    "side": "LONG",
                    "entry_time": entry_ts,
                    "exit_time": exit_ts,
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "bars_held": bars_held,
                    "pnl_usdt": round(pnl, 4),
                    "return_pct": round(ret_pct, 2),
                    "is_win": bool(pnl > 0)
                })
                in_pos = False
        else:
            # ショート: exec_sell でエントリー、exec_buy でエグジット
            if not np.isnan(exec_sells[i]) and not in_pos:
                in_pos = True
                entry_idx = i
                entry_px = float(exec_sells[i])
                entry_ts = str(timestamps[i])
            elif not np.isnan(exec_buys[i]) and in_pos:
                exit_px = float(exec_buys[i])
                exit_ts = str(timestamps[i])
                qty = initial_equity / entry_px
                cost = initial_equity
                fee_entry = cost * maker_fee
                revenue = qty * exit_px
                fee_exit = revenue * taker_fee
                pnl = cost - revenue - fee_exit - fee_entry
                ret_pct = (pnl / initial_equity) * 100.0
                bars_held = i - entry_idx
                trades.append({
                    "symbol": symbol,
                    "side": "SHORT",
                    "entry_time": entry_ts,
                    "exit_time": exit_ts,
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "bars_held": bars_held,
                    "pnl_usdt": round(pnl, 4),
                    "return_pct": round(ret_pct, 2),
                    "is_win": bool(pnl > 0)
                })
                in_pos = False

    return trades


def run_backtest_for_symbol(
    symbol: str,
    days: int = 30,
    side_mode: str = "both"
) -> Dict[str, Any]:
    """1銘柄に対する公式バックテスト実行"""
    df_sub = load_candle_data(symbol, days)
    if df_sub is None:
        return {"symbol": symbol, "error": "Insufficient data"}

    res: Dict[str, Any] = {"symbol": symbol, "bars": len(df_sub), "trades": []}

    # 1. ロング検証
    if side_mode in ("long", "both"):
        res_l_dict, strat_l, inv_l, mp_l, sl_l = run_interval_comparison(
            df_sub, lot=1.0, data_equity=100.0, symbol=symbol
        )
        best_l = res_l_dict.get("_best_params_", {})
        chart_l = best_l.get("chart_df")
        trades_l = extract_detailed_trades(symbol, "long", chart_l)
        res["long"] = {
            "strategy": best_l.get("strategy", strat_l),
            "interval": best_l.get("interval", inv_l),
            "mp_period": best_l.get("mp", mp_l),
            "pnl": best_l.get("pnl", 0.0),
            "trade_count": best_l.get("trade_count", 0),
            "win_rate": best_l.get("win_rate", 0.0),
            "dd_max": best_l.get("DD_max", 0.0),
            "is_qualified": best_l.get("is_qualified", False),
            "range_params": best_l.get("range_params", {}),
            "breakout_params": best_l.get("breakout_params", {})
        }
        res["trades"].extend(trades_l)

    # 2. ショート検証
    if side_mode in ("short", "both"):
        res_s_dict, strat_s, inv_s, mp_s, sl_s = _run_interval_comparison_short(
            df_sub, lot=1.0, data_equity=100.0, symbol=symbol
        )
        best_s = res_s_dict.get("_best_params_", {})
        chart_s = best_s.get("chart_df")
        trades_s = extract_detailed_trades(symbol, "short", chart_s)
        res["short"] = {
            "strategy": best_s.get("strategy", strat_s),
            "interval": best_s.get("interval", inv_s),
            "mp_period": best_s.get("mp", mp_s),
            "pnl": best_s.get("pnl", 0.0),
            "trade_count": best_s.get("trade_count", 0),
            "win_rate": best_s.get("win_rate", 0.0),
            "dd_max": best_s.get("DD_max", 0.0),
            "is_qualified": best_s.get("is_qualified", False),
            "range_params": best_s.get("range_params", {}),
            "breakout_params": best_s.get("breakout_params", {})
        }
        res["trades"].extend(trades_s)

    return res


def main():
    parser = argparse.ArgumentParser(description="bingx5_46 公式統一長期バックテストランナー")
    parser.add_argument("--days", type=int, default=30, help="バックテスト評価期間 (日換算, デフォルト: 30)")
    parser.add_argument("--symbols", type=str, default=None, help="対象銘柄 (カンマ区切り, 例: SUI-USDT,BTC-USDT)")
    parser.add_argument("--all", action="store_true", help="固定20銘柄 + BTC 全銘柄を評価")
    parser.add_argument("--side", type=str, default="both", choices=["long", "short", "both"], help="トレード方向 (long/short/both)")
    parser.add_argument("--export-csv", action="store_true", help="取引履歴明細をCSV出力")
    args = parser.parse_args()

    symbols = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.all:
        symbols = DEFAULT_SYMBOLS
    else:
        # デフォルトは主要3銘柄
        symbols = ["SUI-USDT", "BTC-USDT", "ETH-USDT"]

    print("=" * 80)
    print("🚀 bingx5_46 公式統一長期バックテストランナー (SSoT)")
    print(f"⏱ 評価期間: 直近 {args.days} 日間 | 対象銘柄数: {len(symbols)} 銘柄 | 方向: {args.side.upper()}")
    print("=" * 80)

    all_summary = []
    all_trades = []

    for sym in symbols:
        print(f"\n▶ 銘柄検証開始: {sym} ...")
        res = run_backtest_for_symbol(sym, days=args.days, side_mode=args.side)
        if "error" in res:
            print(f"  ❌ スキップ: {res['error']}")
            continue

        l_info = res.get("long", {})
        s_info = res.get("short", {})
        all_trades.extend(res.get("trades", []))

        all_summary.append({
            "銘柄": sym,
            "L_戦略": f"{l_info.get('strategy', '-')}({l_info.get('interval', '-')}m)",
            "L_純利益": f"{l_info.get('pnl', 0.0):+.2f}",
            "L_回数": l_info.get('trade_count', 0),
            "L_勝率": f"{l_info.get('win_rate', 0.0):.1f}%",
            "L_合否": "🟢合格" if l_info.get('is_qualified') else "🔴未達",
            "S_戦略": f"{s_info.get('strategy', '-')}({s_info.get('interval', '-')}m)",
            "S_純利益": f"{s_info.get('pnl', 0.0):+.2f}",
            "S_回数": s_info.get('trade_count', 0),
            "S_勝率": f"{s_info.get('win_rate', 0.0):.1f}%",
            "S_合否": "🟢合格" if s_info.get('is_qualified') else "🔴未達",
        })

    # 結果表示
    print("\n" + "=" * 80)
    print("📊 公式統一バックテスト結果サマリー")
    print("=" * 80)
    if all_summary:
        df_sum = pd.DataFrame(all_summary)
        print(df_sum.to_string(index=False))

    # トレード集計
    if all_trades:
        df_tr = pd.DataFrame(all_trades)
        total_pnl = df_tr['pnl_usdt'].sum()
        total_cnt = len(df_tr)
        wins = df_tr[df_tr['is_win']]
        losses = df_tr[~df_tr['is_win']]
        win_cnt = len(wins)
        win_rate = (win_cnt / total_cnt * 100.0) if total_cnt > 0 else 0.0
        gross_profit = wins['pnl_usdt'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl_usdt'].sum()) if not losses.empty else 0.0
        pf = (gross_profit / gross_loss) if gross_loss > 0 else 99.99
        expected_payoff = total_pnl / total_cnt if total_cnt > 0 else 0.0

        print("\n" + "-" * 80)
        print("📈 全体トレード統計 (実約定・セッション限定・手数料後)")
        print("-" * 80)
        print(f"  総取引回数: {total_cnt} 回 ({win_cnt}勝 {len(losses)}敗 / 勝率: {win_rate:.1f}%)")
        print(f"  合計純利益: {total_pnl:+.2f} USDT (総利益: +{gross_profit:.2f} / 総損失: -{gross_loss:.2f})")
        print(f"  プロフィットファクター (PF): {pf:.2f}")
        print(f"  1取引あたり期待利益 (Expected Payoff): {expected_payoff:+.4f} USDT/trade")

        if args.export_csv:
            ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = OUTPUT_DIR / f"unified_trades_{args.days}d_{ts_str}.csv"
            df_tr.to_csv(csv_path, index=False, encoding="utf-8")
            print(f"\n💾 取引履歴明細を出力しました: {csv_path}")

    print("\n✅ 公式統一バックテストが正常に完了しました。")

if __name__ == "__main__":
    main()
