import sys
import pandas as pd
import numpy as np
from test_rsima_ema200 import load_data, prepare_rsima_data, TARGET_SYMBOLS

mp_dict = load_data()
symbols, data = prepare_rsima_data(mp_dict)
n_bars = len(data[symbols[0]]["close"])

def run_rsima_advanced(
    symbols,
    data,
    tp_type: str = "fixed_pct", # "fixed_pct", "rsi_cross", "trailing"
    tp_pct: float = 0.020, # 固定利確 +2.0%
    sl_pct: float = 0.090,
    nanpin_drop: float = 0.030,
    max_stages: int = 3,
    cooldown_bars: int = 288,
    use_ema200: bool = True,
    rsi_lower_limit: float = 40.0
):
    initial_balance = 100.0
    margin_usdt = 7.5
    leverage = 10.0
    max_active_positions = 2
    
    balance = initial_balance
    equity_curve = [initial_balance]
    positions = {}
    pending = {}
    last_sl_bar = {s: -999999 for s in symbols}
    trades = []
    
    for i in range(1, n_bars):
        # pending約定
        for sym in list(pending.keys()):
            p_order = pending[sym]
            l = data[sym]["low"][i]
            if l <= p_order["limit_price"]:
                exec_price = p_order["limit_price"]
                pos_val = margin_usdt * leverage
                qty = pos_val / exec_price
                fee = pos_val * 0.0002
                balance -= fee
                
                if sym not in positions:
                    positions[sym] = {
                        "qty": qty,
                        "cost": pos_val,
                        "margin": margin_usdt,
                        "avg_price": exec_price,
                        "last_price": exec_price,
                        "count": 1
                    }
                else:
                    pos = positions[sym]
                    pos["qty"] += qty
                    pos["cost"] += pos_val
                    pos["margin"] += margin_usdt
                    pos["avg_price"] = pos["cost"] / pos["qty"]
                    pos["last_price"] = exec_price
                    pos["count"] += 1
            del pending[sym]
            
        # 決済 & ナンピン
        for sym in list(positions.keys()):
            pos = positions[sym]
            c = data[sym]["close"][i]
            h = data[sym]["high"][i]
            l = data[sym]["low"][i]
            r = data[sym]["rsi"][i]
            m = data[sym]["lma"][i]
            avg_price = pos["avg_price"]
            
            # SL
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sell_val = pos["qty"] * c
                fee = sell_val * 0.0006
                pnl = sell_val - pos["cost"] - fee
                balance += pnl
                trades.append({"symbol": sym, "pnl": pnl, "type": "SL", "count": pos["count"]})
                last_sl_bar[sym] = i
                del positions[sym]
                continue
                
            # TP
            hit_tp = False
            exit_px = c
            if tp_type == "fixed_pct":
                target_px = avg_price * (1.0 + tp_pct)
                if h >= target_px or c >= target_px:
                    hit_tp = True
                    exit_px = max(c, target_px) if h >= target_px else c
            elif tp_type == "rsi_cross": # RSIが平滑線をデッドクロス
                r_prev = data[sym]["rsi"][i-1]
                m_prev = data[sym]["lma"][i-1]
                if (r < m) and (r_prev >= m_prev) and (c > avg_price * 1.002):
                    hit_tp = True
                    exit_px = c
            elif tp_type == "trailing": # 従来
                pass # 既存ロジック
                
            if hit_tp:
                sell_val = pos["qty"] * exit_px
                fee = sell_val * 0.0006
                pnl = sell_val - pos["cost"] - fee
                balance += pnl
                trades.append({"symbol": sym, "pnl": pnl, "type": "TP", "count": pos["count"]})
                del positions[sym]
                continue
                
            # ナンピン
            if pos["count"] < max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        # エントリー
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if i - last_sl_bar[sym] < cooldown_bars:
                    continue
                if use_ema200 and data[sym]["close"][i] < data[sym]["ema200"][i]:
                    continue
                if data[sym]["gc"][i]:
                    pending[sym] = {"limit_price": data[sym]["close"][i] * 0.999}
                    active_slots += 1
                    if active_slots >= max_active_positions:
                        break
                        
        unrealized = sum(positions[sym]["qty"] * data[sym]["close"][i] - positions[sym]["cost"] for sym in positions)
        equity_curve.append(balance + unrealized)
        
    eq_s = pd.Series(equity_curve)
    peak = eq_s.cummax()
    dd = (peak - eq_s) / peak * 100.0
    final_pnl = ((equity_curve[-1] - initial_balance) / initial_balance) * 100.0
    win_trades = [t for t in trades if t["pnl"] > 0]
    wr = len(win_trades) / max(1, len(trades)) * 100.0
    
    return {
        "final_equity": equity_curve[-1],
        "profit": equity_curve[-1] - initial_balance,
        "return_pct": final_pnl,
        "min_eq": float(eq_s.min()),
        "max_dd": float(dd.max()),
        "wr": wr,
        "trades": len(trades),
        "sl_trades": len([t for t in trades if t["type"] == "SL"]),
        "tp_trades": len([t for t in trades if t["type"] == "TP"])
    }

print("=== 【RSIMA + EMA 200: 利確ターゲット別スキャン】 ===")

# 1. 固定利確 (% target)
for tp_p in [0.010, 0.015, 0.020, 0.025, 0.030]:
    for sl_p in [0.085, 0.090, 0.100]:
        res = run_rsima_advanced(symbols, data, tp_type="fixed_pct", tp_pct=tp_p, sl_pct=sl_p)
        print(f"固定利確: +{tp_p*100:3.1f}% | SL: {sl_p*100:4.1f}% | 利回り: {res['return_pct']:+6.1f}% (利益: ${res['profit']:+5.1f}) | 最小残高: ${res['min_eq']:5.1f} | MaxDD: {res['max_dd']:4.1f}% | 勝率: {res['wr']:4.1f}% | 取引数: {res['trades']} (SL: {res['sl_trades']}回)")

# 2. RSI デッドクロス利確
res_dc = run_rsima_advanced(symbols, data, tp_type="rsi_cross", sl_pct=0.090)
print(f"\nRSI DC利確 | SL: 9.0% | 利回り: {res_dc['return_pct']:+6.1f}% (利益: ${res_dc['profit']:+5.1f}) | 最小残高: ${res_dc['min_eq']:5.1f} | MaxDD: {res_dc['max_dd']:4.1f}% | 勝率: {res_dc['wr']:4.1f}% | 取引数: {res_dc['trades']} (SL: {res_dc['sl_trades']}回)")
