#!/usr/bin/env python
# coding: utf-8
"""
BTC Macro Regime & Sensitivity Analysis
BTCのマクロレジーム判定をアルトコイン選定・ロング許可に連動させた場合の効果検証
およびパラメータ感度分析
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from test_regime_filter_backtest import RegimeFilterSimulator, run_strategy_backtest

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def run_full_investigation():
    btc_file = Path("Data/individual/merged_BTC-USDT.csv")
    if not btc_file.exists():
        print("BTC file not found")
        return

    btc_df = pd.read_csv(btc_file)
    
    # 1. パラメータ感度分析
    print("================================================================================")
    print("【1. パラメータ感度分析（BTC & アルトコイン全体）】")
    print("================================================================================")
    
    symbols = [
        "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT", "AVAX-USDT",
        "LINK-USDT", "NEAR-USDT", "UNI-USDT", "ENA-USDT", "ZEC-USDT"
    ]
    
    param_configs = [
        {"name": "① デフォルト (48h/24h停滞/出来高0.8/下落-10%/セリクラ2.0倍)", "lookback": 48, "stag": 24, "decay": 0.80, "drop": -0.10, "surge": 2.0, "wick": 0.35},
        {"name": "② 慎重型 (48h/36h停滞/出来高0.75/下落-12%/セリクラ2.5倍)", "lookback": 48, "stag": 36, "decay": 0.75, "drop": -0.12, "surge": 2.5, "wick": 0.40},
        {"name": "③ 積極型 (36h/18h停滞/出来高0.85/下落-8%/セリクラ1.8倍)", "lookback": 36, "stag": 18, "decay": 0.85, "drop": -0.08, "surge": 1.8, "wick": 0.30},
        {"name": "④ トレンド重視 (60h/36h停滞/出来高0.70/下落-15%/セリクラ2.0倍)", "lookback": 60, "stag": 36, "decay": 0.70, "drop": -0.15, "surge": 2.0, "wick": 0.35},
    ]

    for p_cfg in param_configs:
        sim = RegimeFilterSimulator(
            lookback_high=p_cfg["lookback"],
            stagnation_bars=p_cfg["stag"],
            vol_decay_ratio=p_cfg["decay"],
            alt_drop_pct=p_cfg["drop"],
            vol_surge_mult=p_cfg["surge"],
            lower_wick_ratio=p_cfg["wick"],
        )
        
        raw_pnls, fil_pnls = [], []
        raw_dds, fil_dds = [], []
        raw_pfs, fil_pfs = [], []
        
        for sym in symbols:
            p = Path(f"Data/individual/merged_{sym}.csv")
            if not p.exists(): continue
            df = pd.read_csv(p)
            for strat in ["rsima", "trend_pullback"]:
                res_raw = run_strategy_backtest(df, strategy_name=strat, use_regime_filter=False)
                # カスタムシミュレータを適用
                df_fil = df.copy()
                _ = sim.compute_regime(df_fil, is_btc=False)
                
                # 手動バックテスト
                res_fil = run_strategy_backtest(df, strategy_name=strat, use_regime_filter=True)
                
                raw_pnls.append(res_raw["total_pnl_pct"])
                fil_pnls.append(res_fil["total_pnl_pct"])
                raw_dds.append(res_raw["max_dd_pct"])
                fil_dds.append(res_fil["max_dd_pct"])
                raw_pfs.append(res_raw["profit_factor"])
                fil_pfs.append(res_fil["profit_factor"])
                
        print(f"設定: {p_cfg['name']}")
        print(f"  PnL: {np.mean(raw_pnls):+6.2f}% -> {np.mean(fil_pnls):+6.2f}% (差: {np.mean(fil_pnls) - np.mean(raw_pnls):+6.2f}%)")
        print(f"  MaxDD: {np.mean(raw_dds):5.2f}% -> {np.mean(fil_dds):5.2f}% (削減: {np.mean(raw_dds) - np.mean(fil_dds):+5.2f}%)")
        print(f"  PF:    {np.mean(raw_pfs):5.2f} -> {np.mean(fil_pfs):5.2f}")
        print("-" * 75)

    # 2. BTCマクロ連動フィルターの検証
    print("\n================================================================================")
    print("【2. BTCマクロ連動型レジームフィルター（Macro BTC Regime Gate）検証】")
    print("  仕様: BTCが高値圏・ジリ下げ中は、全アルトコインのロングも一斉停止する")
    print("================================================================================")
    
    sim_btc = RegimeFilterSimulator(lookback_high=48, stagnation_bars=24, vol_decay_ratio=0.80, btc_drop_pct=-0.05)
    btc_allowed = sim_btc.compute_regime(btc_df, is_btc=True)
    btc_map = dict(zip(btc_df["timestamp"], btc_allowed))
    
    macro_pnls = []
    macro_dds = []
    macro_pfs = []
    macro_trades = []

    for sym in symbols:
        p = Path(f"Data/individual/merged_{sym}.csv")
        if not p.exists(): continue
        df = pd.read_csv(p)
        df["btc_gate"] = df["timestamp"].map(btc_map).fillna(True)
        
        for strat in ["rsima", "trend_pullback"]:
            # BTCマクロ門番を適用したバックテスト
            # 独自に実行
            df_m = df.copy()
            close = df_m["close"]
            if strat == "rsima":
                from test_regime_filter_backtest import calc_rsi, calc_ema
                rsi = calc_rsi(close, 9)
                rsi_ma = calc_ema(rsi, 7)
                long_sig = (rsi > rsi_ma) & (rsi.shift(1) <= rsi_ma.shift(1)) & (rsi < 45)
                exit_sig = rsi > 65
            else:
                from test_regime_filter_backtest import calc_ema
                ema12 = calc_ema(close, 12)
                ema26 = calc_ema(close, 26)
                long_sig = (ema12 > ema26) & (close > ema12) & (close.shift(1) <= ema12.shift(1))
                exit_sig = close < ema26
                
            # エントリーは BTC gate が True の時のみ
            effective_entry = long_sig & df_m["btc_gate"]
            # BTC gate が False になった瞬間にポジション早期決済
            early_exit = (~df_m["btc_gate"]) & (df_m["btc_gate"].shift(1) == True)
            effective_exit = exit_sig | early_exit
            
            # シミュレーション
            trades = []
            in_pos = False
            entry_p = 0.0
            for i in range(1, len(df_m)):
                c = df_m["close"].iloc[i]
                l = df_m["low"].iloc[i]
                if in_pos:
                    sl_p = entry_p * (1.0 - 0.03)
                    if l <= sl_p:
                        trades.append((sl_p - entry_p) / entry_p - 0.0008)
                        in_pos = False
                    elif effective_exit.iloc[i] or c >= entry_p * 1.06:
                        ep = entry_p * 1.06 if c >= entry_p * 1.06 else c
                        trades.append((ep - entry_p) / entry_p - 0.0008)
                        in_pos = False
                else:
                    if effective_entry.iloc[i]:
                        in_pos = True
                        entry_p = c
                        
            if trades:
                cum_eq = np.cumprod(1.0 + np.array(trades))
                pk = np.maximum.accumulate(cum_eq)
                dd = (cum_eq - pk) / pk
                max_dd = abs(np.min(dd)) * 100.0
                tot_pnl = (cum_eq[-1] - 1.0) * 100.0
                wins = [t for t in trades if t > 0]
                losses = [abs(t) for t in trades if t <= 0]
                pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else 99.0
            else:
                max_dd, tot_pnl, pf = 0.0, 0.0, 0.0
                
            macro_pnls.append(tot_pnl)
            macro_dds.append(max_dd)
            macro_pfs.append(pf)
            macro_trades.append(len(trades))
            
    print(f"■ BTCマクロ門番連動 平均損益 (PnL):     {np.mean(macro_pnls):+6.2f}%")
    print(f"■ BTCマクロ門番連動 平均最大DD (MaxDD): {np.mean(macro_dds):6.2f}%")
    print(f"■ BTCマクロ門番連動 平均PF:             {np.mean(macro_pfs):6.2f}")
    print(f"■ BTCマクロ門番連動 平均トレード回数:   {np.mean(macro_trades):5.1f}回")
    print("================================================================================")


if __name__ == "__main__":
    run_full_investigation()
