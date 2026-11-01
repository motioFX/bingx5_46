import pandas as pd
import numpy as np
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

def analyze_losses():
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
        
    initial_balance = 100.0
    margin_per_stage = 5.0
    leverage = 10.0
    max_active_positions = 2
    target_ratio = 0.60
    nanpin_drop = 0.025
    sl_pct = 0.090
    total_max_stages = 4
    
    balance = initial_balance
    equity_curve = [initial_balance]
    
    positions = {}
    pending = {}
    trade_log = []
    
    for i in range(1, n_bars):
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
                        "count": 1,
                        "entry_bar": i
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
            
        for sym in list(positions.keys()):
            pos = positions[sym]
            c = closes[sym][i]
            h = highs[sym][i]
            l = lows[sym][i]
            val = vals[sym][i]
            vah = vahs[sym][i]
            avg_price = pos["avg_price"]
            
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sell_val = pos["qty"] * c
                fee = sell_val * 0.0006
                pnl = sell_val - pos["cost"] - fee
                balance += pnl
                trade_log.append({
                    "bar": i,
                    "day": i // 288,
                    "symbol": sym,
                    "type": "SL",
                    "pnl": pnl,
                    "count": pos["count"],
                    "balance_after": balance
                })
                del positions[sym]
                continue
                
            target_px = val + (vah - val) * target_ratio
            if not np.isnan(target_px) and (h >= target_px or c >= target_px):
                exit_price = max(c, target_px) if h >= target_px else c
                if exit_price > avg_price * 1.0006:
                    sell_val = pos["qty"] * exit_price
                    fee = sell_val * 0.0006
                    pnl = sell_val - pos["cost"] - fee
                    balance += pnl
                    trade_log.append({
                        "bar": i,
                        "day": i // 288,
                        "symbol": sym,
                        "type": "TP",
                        "pnl": pnl,
                        "count": pos["count"],
                        "balance_after": balance
                    })
                    del positions[sym]
                    continue
                    
            if pos["count"] < total_max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
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
                        
        unrealized = sum(positions[sym]["qty"] * closes[sym][i] - positions[sym]["cost"] for sym in positions)
        equity_curve.append(balance + unrealized)
        
    df_trades = pd.DataFrame(trade_log)
    eq_s = pd.Series(equity_curve)
    
    # 銘柄別のSL回数とSL損失額
    sl_trades = df_trades[df_trades["type"] == "SL"]
    print("=== 銘柄別 損切り (SL) 内訳 ===")
    sym_sl = sl_trades.groupby("symbol").agg(
        sl_count=("pnl", "count"),
        total_loss=("pnl", "sum"),
        avg_loss=("pnl", "mean")
    ).sort_values(by="total_loss")
    print(sym_sl)
    
    # 資産が最も落ち込んだ時期（Min Equity周辺）
    min_idx = eq_s.idxmin()
    min_val = eq_s[min_idx]
    print(f"\n【最大下落の底】: Bar {min_idx} (Day {min_idx // 288}) | 残高: ${min_val:.2f}")
    
    # その前後のトレード
    window_trades = df_trades[(df_trades["bar"] >= min_idx - 288*10) & (df_trades["bar"] <= min_idx + 288*2)]
    print("\n【最大下落期前後のトレード履歴 (10日分)】")
    print(window_trades.tail(15).to_string(index=False))

if __name__ == "__main__":
    analyze_losses()
