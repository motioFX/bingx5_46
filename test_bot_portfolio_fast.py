#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
実運用ポートフォリオ型バックテスト (高速版):
・原資: $100
・同時最大ポジション数: 2銘柄 (MAX_ACTIVE_POSITIONS = 2)
・レバレッジ: 10倍 (LEVERAGE_FACTOR = 10.0)
・1注文あたりの証拠金: $15 (ポジション価値 $150)
・ナンピン: 最大5回 (1銘柄あたり最大証拠金 $75、ポジション価値 $750)
・20銘柄の中から5分足でシグナルが出た順にエントリー (空き枠2スロットを取り合う)
"""
import sys
import time
import numpy as np
import pandas as pd
from pathlib import Path

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
DATA_DIR = WORKSPACE / "Data/binance_candles_5m"

TARGET_SYMBOLS = [
    'HYPEUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT', 'SUIUSDT',
    'AVAXUSDT', 'LINKUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'UNIUSDT', 'OPUSDT', 'TIAUSDT', 'RENDERUSDT', 'INJUSDT',
    'FETUSDT', 'TAOUSDT', 'AAVEUSDT', 'ENAUSDT', 'ZECUSDT'
]

def load_data():
    mp_dict = {}
    for sym in TARGET_SYMBOLS:
        cache_file = DATA_DIR / f"{sym}_5m_mp.pkl"
        if cache_file.exists():
            df = pd.read_pickle(cache_file)
            mp_dict[sym] = df
    return mp_dict

def run_realistic_bot_sim(
    mp_dict,
    initial_balance: float = 100.0,
    margin_per_stage: float = 15.0,
    leverage: float = 10.0,
    max_active_positions: int = 2,
    target_ratio: float = 0.60,
    min_va_width: float = 0.015,
    nanpin_drop: float = 0.020,
    sl_pct: float = 0.085,
    total_max_stages: int = 5
):
    symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]
    n_bars = len(mp_dict[symbols[0]])
    
    # 銘柄ごとのデータをnumpy配列化
    closes = {}
    highs = {}
    lows = {}
    vals = {}
    vahs = {}
    sig_entries = {}
    
    for sym in symbols:
        df = mp_dict[sym]
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        val = df["VAL"].values
        vah = df["VAH"].values
        
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        prev_val = np.roll(val, 1)
        prev_val[0] = val[0]
        
        # エントリーシグナル: 前回確定足 <= prev_val かつ 現在確定足 > val
        # かつ バリュー幅 >= 1.5%
        va_w = np.where(val > 0, (vah - val) / val, 0.0)
        cross_up = (prev_c <= prev_val) & (c > val) & (va_w >= min_va_width) & (~np.isnan(val))
        
        closes[sym] = c
        highs[sym] = h
        lows[sym] = l
        vals[sym] = val
        vahs[sym] = vah
        sig_entries[sym] = cross_up
        
    balance = initial_balance
    equity_curve = [initial_balance]
    
    positions = {}
    pending = {}
    trades = []
    
    for i in range(1, n_bars):
        # 1. 注文の約定確認
        for sym in list(pending.keys()):
            p_order = pending[sym]
            l = lows[sym][i]
            if l <= p_order["limit_price"]:
                exec_price = p_order["limit_price"]
                pos_val = margin_per_stage * leverage
                qty = pos_val / exec_price
                fee = pos_val * 0.0002
                balance -= fee
                
                if sym not in positions:
                    positions[sym] = {
                        "qty": qty,
                        "cost": pos_val,
                        "margin": margin_per_stage,
                        "avg_price": exec_price,
                        "last_price": exec_price,
                        "count": 1
                    }
                else:
                    pos = positions[sym]
                    pos["qty"] += qty
                    pos["cost"] += pos_val
                    pos["margin"] += margin_per_stage
                    pos["avg_price"] = pos["cost"] / pos["qty"]
                    pos["last_price"] = exec_price
                    pos["count"] += 1
            del pending[sym]
            
        # 2. 既存ポジションのエグジット & ナンピン判定
        for sym in list(positions.keys()):
            pos = positions[sym]
            c = closes[sym][i]
            h = highs[sym][i]
            l = lows[sym][i]
            val = vals[sym][i]
            vah = vahs[sym][i]
            avg_price = pos["avg_price"]
            
            # SL判定
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sell_val = pos["qty"] * c
                fee = sell_val * 0.0006
                pnl = sell_val - pos["cost"] - fee
                balance += pnl
                trades.append({"symbol": sym, "pnl": pnl, "type": "SL", "count": pos["count"]})
                del positions[sym]
                continue
                
            # 利確判定
            target_px = val + (vah - val) * target_ratio
            if not np.isnan(target_px) and (h >= target_px or c >= target_px):
                exit_price = max(c, target_px) if h >= target_px else c
                if exit_price > avg_price * 1.0006:
                    sell_val = pos["qty"] * exit_price
                    fee = sell_val * 0.0006
                    pnl = sell_val - pos["cost"] - fee
                    balance += pnl
                    trades.append({"symbol": sym, "pnl": pnl, "type": "TP", "count": pos["count"]})
                    del positions[sym]
                    continue
                    
            # ナンピン判定
            if pos["count"] < total_max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        # 3. 新規エントリー判定
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if sig_entries[sym][i]:
                    pending[sym] = {"limit_price": closes[sym][i] * 0.999}
                    active_slots += 1
                    if active_slots >= max_active_positions:
                        break
                        
        # 含み損益計算
        unrealized = sum(positions[sym]["qty"] * closes[sym][i] - positions[sym]["cost"] for sym in positions)
        equity_curve.append(balance + unrealized)
        
    eq_s = pd.Series(equity_curve)
    peak = eq_s.cummax()
    dd = (peak - eq_s) / peak * 100.0
    final_pnl = ((equity_curve[-1] - initial_balance) / initial_balance) * 100.0
    win_trades = [t for t in trades if t["pnl"] > 0]
    wr = len(win_trades) / max(1, len(trades)) * 100.0
    
    return {
        "initial_balance": initial_balance,
        "final_equity": equity_curve[-1],
        "net_profit": equity_curve[-1] - initial_balance,
        "return_pct": final_pnl,
        "win_rate": wr,
        "trades": len(trades),
        "max_dd_pct": float(dd.max()),
        "sl_trades": len([t for t in trades if t["type"] == "SL"]),
        "tp_trades": len([t for t in trades if t["type"] == "TP"]),
        "equity_curve": equity_curve
    }

if __name__ == "__main__":
    t0 = time.time()
    mp_dict = load_data()
    print(f"Data loaded in {time.time() - t0:.2f}s")
    
    # テスト実行
    configs = [
        {"init": 100.0, "slots": 2, "sl": 0.085, "margin": 15.0},
        {"init": 150.0, "slots": 2, "sl": 0.085, "margin": 15.0},
        {"init": 200.0, "slots": 2, "sl": 0.085, "margin": 15.0},
        {"init": 150.0, "slots": 3, "sl": 0.085, "margin": 15.0},
        {"init": 200.0, "slots": 3, "sl": 0.085, "margin": 15.0},
        {"init": 100.0, "slots": 2, "sl": 0.090, "margin": 15.0},
        {"init": 150.0, "slots": 2, "sl": 0.090, "margin": 15.0},
    ]
    
    print("\n=== 実運用ボット (レバレッジ10倍, 証拠金$15) ポートフォリオ成績 ===")
    for c in configs:
        t_start = time.time()
        res = run_realistic_bot_sim(
            mp_dict,
            initial_balance=c["init"],
            margin_per_stage=c["margin"],
            leverage=10.0,
            max_active_positions=c["slots"],
            sl_pct=c["sl"]
        )
        print(f"原資: ${c['init']:3.0f} | 枠: {c['slots']} | SL: {c['sl']*100:4.1f}% | 純利益: ${res['net_profit']:+6.1f} (利回り: {res['return_pct']:+6.1f}%) | 勝率: {res['win_rate']:5.1f}% | 取引数: {res['trades']:3d} (SL: {res['sl_trades']:2d}, TP: {res['tp_trades']:3d}) | MaxDD: {res['max_dd_pct']:5.2f}% | 計算: {time.time()-t_start:.1f}s")
