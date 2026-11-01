import sys
import pandas as pd
import numpy as np
from test_rsima_ema200 import load_data, calc_rsi, TARGET_SYMBOLS

mp_dict = load_data()
symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]

def test_rsima_variations(rsi_len=9, lma_len=7, rsi_max=40.0, tp_target="trailing"):
    # データ準備
    data = {}
    for sym in symbols:
        df = mp_dict[sym]
        c = df["close"]
        h = df["high"].values
        l = df["low"].values
        closes = c.values
        
        rsi_s = calc_rsi(c, rsi_len)
        lma_s = rsi_s.ewm(span=lma_len, adjust=False).mean()
        rsi = rsi_s.values
        lma = lma_s.values
        ema200 = c.ewm(span=200, adjust=False).mean().values
        
        r_prev = np.roll(rsi, 1)
        r_prev[0] = rsi[0]
        m_prev = np.roll(lma, 1)
        m_prev[0] = lma[0]
        
        gc = (rsi > lma) & (r_prev <= m_prev) & (lma < rsi_max)
        data[sym] = {"close": closes, "high": h, "low": l, "rsi": rsi, "lma": lma, "ema200": ema200, "gc": gc}
        
    n_bars = len(data[symbols[0]]["close"])
    initial_balance = 100.0
    margin_usdt = 7.5
    leverage = 10.0
    max_active_positions = 2
    sl_pct = 0.090
    nanpin_drop = 0.030
    max_stages = 3
    cooldown_bars = 288
    
    balance = initial_balance
    equity_curve = [initial_balance]
    positions = {}
    pending = {}
    last_sl_bar = {s: -999999 for s in symbols}
    trades = []
    
    tp_thresh = 65.0
    callback_pct = 0.015
    
    for i in range(1, n_bars):
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
                        "qty": qty, "cost": pos_val, "margin": margin_usdt,
                        "avg_price": exec_price, "last_price": exec_price, "count": 1,
                        "trailing_active": False, "trail_peak": 0.0
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
            
        for sym in list(positions.keys()):
            pos = positions[sym]
            c = data[sym]["close"][i]
            h = data[sym]["high"][i]
            l = data[sym]["low"][i]
            r = data[sym]["rsi"][i]
            avg_price = pos["avg_price"]
            
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sell_val = pos["qty"] * c
                fee = sell_val * 0.0006
                pnl = sell_val - pos["cost"] - fee
                balance += pnl
                trades.append({"symbol": sym, "pnl": pnl, "type": "SL"})
                last_sl_bar[sym] = i
                del positions[sym]
                continue
                
            if not pos["trailing_active"]:
                if r > tp_thresh and c > avg_price:
                    pos["trailing_active"] = True
                    pos["trail_peak"] = max(c, h)
            if pos["trailing_active"]:
                pos["trail_peak"] = max(pos["trail_peak"], h)
                trail_stop = max(pos["trail_peak"] * (1.0 - callback_pct), avg_price * 1.0006)
                if c < trail_stop:
                    sell_val = pos["qty"] * c
                    fee = sell_val * 0.0006
                    pnl = sell_val - pos["cost"] - fee
                    balance += pnl
                    trades.append({"symbol": sym, "pnl": pnl, "type": "TP"})
                    del positions[sym]
                    continue
                    
            if pos["count"] < max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if i - last_sl_bar[sym] < cooldown_bars:
                    continue
                if data[sym]["close"][i] < data[sym]["ema200"][i]:
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
    sl_count = len([t for t in trades if t["type"] == "SL"])
    return {
        "pnl": final_pnl, "min_eq": float(eq_s.min()), "dd": float(dd.max()),
        "wr": wr, "trades": len(trades), "sl": sl_count
    }

print("=== 【RSIMA パラメータ微調整 × EMA 200】 ===")
configs = [
    (9, 7, 40.0),
    (9, 7, 35.0),
    (9, 7, 30.0),
    (14, 9, 40.0),
    (14, 9, 35.0),
    (14, 9, 30.0),
    (14, 14, 35.0),
]
for r_len, m_len, r_max in configs:
    res = test_rsima_variations(rsi_len=r_len, lma_len=m_len, rsi_max=r_max)
    print(f"RSI({r_len:2d}), MA({m_len:2d}), MaxRSI {r_max:2.0f} | 利回り: {res['pnl']:+6.1f}% | 最小残高: ${res['min_eq']:5.1f} | MaxDD: {res['dd']:4.1f}% | 勝率: {res['wr']:4.1f}% | 取引: {res['trades']} (SL: {res['sl']}回)")
