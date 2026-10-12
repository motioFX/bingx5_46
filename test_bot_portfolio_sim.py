#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
実運用ポートフォリオ型バックテスト:
・原資: $100 (または $200)
・同時最大ポジション数: 2銘柄 (MAX_ACTIVE_POSITIONS = 2)
・レバレッジ: 10倍 (LEVERAGE_FACTOR = 10.0)
・1注文あたりの証拠金: $15 (ポジション価値 $150)
・ナンピン: 最大5回 (1銘柄あたり最大証拠金 $75、ポジション価値 $750)
・20銘柄の中から5分足でシグナルが出た順にエントリー (空き枠2スロットを取り合う)
"""
import sys
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
    mp_dict = load_data()
    symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]
    
    # 全銘柄の日時インデックスを統一
    # 各銘柄のDataFrameから、timestamp またはインデックスを揃える
    sample_df = mp_dict[symbols[0]]
    n_bars = len(sample_df)
    
    # 銘柄ごとのデータを高速アクセスのためnumpy配列化
    data = {}
    for sym in symbols:
        df = mp_dict[sym]
        vals = df["VAL"].values
        prev_vals = np.roll(vals, 1)
        prev_vals[0] = vals[0]
        data[sym] = {
            "close": df["close"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "VAL": vals,
            "prev_VAL": prev_vals,
            "VAH": df["VAH"].values,
            "POC": df["POC"].values,
        }
        
    balance = initial_balance
    equity_curve = []
    
    # ポジション管理: sym -> dict
    positions = {}
    # pending注文: sym -> dict
    pending = {}
    
    trades = []
    daily_pnls = []
    
    for i in range(1, n_bars):
        # 1. 注文の約定確認 (エントリー / ナンピン)
        for sym in list(pending.keys()):
            p_order = pending[sym]
            l = data[sym]["low"][i]
            if l <= p_order["limit_price"]:
                exec_price = p_order["limit_price"]
                # 証拠金 $15, レバレッジ10倍 => ポジション名目価値 $150
                pos_val = margin_per_stage * leverage
                qty = pos_val / exec_price
                fee = pos_val * 0.0002
                balance -= fee
                
                if sym not in positions:
                    positions[sym] = {
                        "qty": qty,
                        "cost": pos_val, # 名目価値
                        "margin": margin_per_stage,
                        "avg_price": exec_price,
                        "last_price": exec_price,
                        "count": 1
                    }
                else:
                    pos = positions[sym]
                    new_qty = pos["qty"] + qty
                    new_cost = pos["cost"] + pos_val
                    pos["qty"] = new_qty
                    pos["cost"] = new_cost
                    pos["margin"] += margin_per_stage
                    pos["avg_price"] = new_cost / new_qty
                    pos["last_price"] = exec_price
                    pos["count"] += 1
            del pending[sym]
            
        # 2. 既存ポジションのエグジット判定 (SL または 利確)
        for sym in list(positions.keys()):
            pos = positions[sym]
            c = data[sym]["close"][i]
            h = data[sym]["high"][i]
            val = data[sym]["VAL"][i]
            vah = data[sym]["VAH"][i]
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
                    
            # 3. ナンピン判定
            if pos["count"] < total_max_stages and sym not in pending:
                if (c <= pos["last_price"] * (1.0 - nanpin_drop)) or (data[sym]["low"][i] <= pos["last_price"] * (1.0 - nanpin_drop)):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        # 4. 新規エントリー判定 (空きスロットがある場合のみ)
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if active_slots >= max_active_positions:
                    break
                    
                c = data[sym]["close"][i]
                c_prev = data[sym]["close"][i-1]
                val = data[sym]["VAL"][i]
                prev_val = data[sym]["prev_VAL"][i]
                vah = data[sym]["VAH"][i]
                
                if not np.isnan(val) and not np.isnan(prev_val) and not np.isnan(vah) and val > 0:
                    va_width = (vah - val) / val
                    if (c_prev <= prev_val) and (c > val):
                        if min_va_width == 0.0 or va_width >= min_va_width:
                            pending[sym] = {"limit_price": c * 0.999}
                            active_slots += 1
                            
        # 5. 含み損益の計算
        unrealized = 0.0
        for sym, pos in positions.items():
            c = data[sym]["close"][i]
            unrealized += (pos["qty"] * c - pos["cost"])
            
        current_equity = balance + unrealized
        equity_curve.append(current_equity)
        
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
        "tp_trades": len([t for t in trades if t["type"] == "TP"])
    }

if __name__ == "__main__":
    for init_b in [100.0, 150.0, 200.0]:
        for slots in [2, 3]:
            for sl in [0.085, 0.090, 0.100]:
                res = run_realistic_bot_sim(
                    initial_balance=init_b,
                    margin_per_stage=15.0,
                    leverage=10.0,
                    max_active_positions=slots,
                    sl_pct=sl
                )
                print(f"Init: ${init_b:3.0f} | Slots: {slots} | SL: {sl*100:4.1f}% | Profit: ${res['net_profit']:+6.1f} ({res['return_pct']:+6.1f}%) | WR: {res['win_rate']:5.1f}% | Trades: {res['trades']:3d} (SL: {res['sl_trades']:2d}) | MaxDD: {res['max_dd_pct']:5.2f}%")
