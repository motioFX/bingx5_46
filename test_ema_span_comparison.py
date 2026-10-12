import pandas as pd
import numpy as np
from inspect_drawdown_period import load_data, TARGET_SYMBOLS

mp_dict = load_data()
symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]
n_bars = len(mp_dict[symbols[0]])

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
    
    va_w = np.where(val > 0, (vah - val) / val, 0.0)
    cross_up = (prev_c <= prev_val) & (c > val) & (va_w >= 0.015) & (~np.isnan(val))
    
    closes[sym] = c
    highs[sym] = h
    lows[sym] = l
    vals[sym] = val
    vahs[sym] = vah
    sig_entries[sym] = cross_up

def run_ema_test(span_bars: int):
    # span_bars: EMA期間 (例: 200, 288)
    ema_dict = {}
    if span_bars > 0:
        for sym in symbols:
            ema_dict[sym] = pd.Series(closes[sym]).ewm(span=span_bars, adjust=False).mean().values
        
    initial_balance = 100.0
    margin_usdt = 7.5 # 原資$100, 証拠金$7.5
    leverage = 10.0
    max_active_positions = 2
    target_ratio = 0.60
    nanpin_drop = 0.030
    sl_pct = 0.090
    max_stages = 3
    cooldown_bars = 288 # 24hクールダウン
    
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
            l = lows[sym][i]
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
            c = closes[sym][i]
            h = highs[sym][i]
            l = lows[sym][i]
            val = vals[sym][i]
            vah = vahs[sym][i]
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
                    
            # ナンピン
            if pos["count"] < max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        # 新規エントリー
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if i - last_sl_bar[sym] < cooldown_bars:
                    continue
                if span_bars > 0 and closes[sym][i] < ema_dict[sym][i]:
                    continue
                if sig_entries[sym][i]:
                    pending[sym] = {"limit_price": closes[sym][i] * 0.999}
                    active_slots += 1
                    if active_slots >= max_active_positions:
                        break
                        
        unrealized = sum(positions[sym]["qty"] * closes[sym][i] - positions[sym]["cost"] for sym in positions)
        equity_curve.append(balance + unrealized)
        
    eq_s = pd.Series(equity_curve)
    peak = eq_s.cummax()
    dd = (peak - eq_s) / peak * 100.0
    final_pnl = ((equity_curve[-1] - initial_balance) / initial_balance) * 100.0
    win_trades = [t for t in trades if t["pnl"] > 0]
    wr = len(win_trades) / max(1, len(trades)) * 100.0
    
    return {
        "span": span_bars,
        "hours": span_bars / 12.0 if span_bars > 0 else 0,
        "profit": equity_curve[-1] - initial_balance,
        "return_pct": final_pnl,
        "min_eq": float(eq_s.min()),
        "max_dd": float(dd.max()),
        "wr": wr,
        "trades": len(trades),
        "sl_trades": len([t for t in trades if t["type"] == "SL"]),
        "equity_curve": equity_curve
    }

print("=== 【EMA期間スキャン: 200 vs 288 vs その他】 ===")
spans = [0, 100, 144, 200, 240, 288, 360, 576]
results = []
for sp in spans:
    res = run_ema_test(sp)
    results.append(res)
    label = f"なし (0本)" if sp == 0 else f"EMA {sp:3d} ({res['hours']:4.1f}h)"
    print(f"{label:18s} | 利回り: {res['return_pct']:+6.1f}% (利益: ${res['profit']:+5.1f}) | 最小残高: ${res['min_eq']:5.1f} | MaxDD: {res['max_dd']:4.1f}% | 勝率: {res['wr']:4.1f}% | 取引数: {res['trades']} (SL: {res['sl_trades']}回)")
