import pandas as pd
import numpy as np
from inspect_drawdown_period import load_data, TARGET_SYMBOLS

mp_dict = load_data()
symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]
n_bars = len(mp_dict[symbols[0]])

# 各銘柄のEMA50 (5分足 または 上位足相当) を計算
# 5分足のEMA 288本 = 24時間 (1日) の移動平均
closes = {}
highs = {}
lows = {}
vals = {}
vahs = {}
sig_entries = {}
ema_trend = {}

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
    
    # 24時間EMA (288本)
    ema24h = pd.Series(c).ewm(span=288, adjust=False).mean().values
    
    va_w = np.where(val > 0, (vah - val) / val, 0.0)
    cross_up = (prev_c <= prev_val) & (c > val) & (va_w >= 0.015) & (~np.isnan(val))
    
    closes[sym] = c
    highs[sym] = h
    lows[sym] = l
    vals[sym] = val
    vahs[sym] = vah
    sig_entries[sym] = cross_up
    ema_trend[sym] = ema24h

def test_config(
    initial_balance: float = 100.0,
    margin_ratio: float = 0.04, # 残高の何%を1注文にするか (動的サイジング: 4%)
    fixed_margin: float = None, # 固定証拠金 (指定時は固定)
    max_stages: int = 3, # ナンピン回数
    nanpin_drop: float = 0.025,
    sl_pct: float = 0.090,
    use_trend_filter: bool = False, # EMA24h以上のみロング
    cooldown_bars: int = 144 # 12時間クールダウン
):
    balance = initial_balance
    equity_curve = [initial_balance]
    positions = {}
    pending = {}
    last_sl_bar = {s: -999999 for s in symbols}
    trades = []
    
    for i in range(1, n_bars):
        # 1. pending約定
        for sym in list(pending.keys()):
            p_order = pending[sym]
            l = lows[sym][i]
            if l <= p_order["limit_price"]:
                exec_price = p_order["limit_price"]
                margin_usdt = p_order["margin"]
                pos_val = margin_usdt * 10.0 # レバレッジ10倍
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
            
        # 2. 決済 & ナンピン
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
            target_px = val + (vah - val) * 0.60
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
                    m = pos["margin"] / pos["count"] # 初回と同額
                    pending[sym] = {"limit_price": c * 0.999, "margin": m}
                    
        # 3. エントリー
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < 2:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if i - last_sl_bar[sym] < cooldown_bars:
                    continue
                if use_trend_filter and closes[sym][i] < ema_trend[sym][i]:
                    # 下落トレンド中はロング見送り
                    continue
                if sig_entries[sym][i]:
                    # 証拠金計算
                    if fixed_margin is not None:
                        m = fixed_margin
                    else:
                        m = max(3.0, balance * margin_ratio)
                    pending[sym] = {"limit_price": closes[sym][i] * 0.999, "margin": m}
                    active_slots += 1
                    if active_slots >= 2:
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
        "profit": equity_curve[-1] - initial_balance,
        "return_pct": final_pnl,
        "min_eq": float(eq_s.min()),
        "max_dd": float(dd.max()),
        "wr": wr,
        "trades": len(trades),
        "sl_trades": len([t for t in trades if t["type"] == "SL"]),
        "equity_curve": equity_curve
    }

print("=== 【下落期間（ドローダウン）抑制の比較テスト】 ===")

# 1. 現状ベースライン (固定$5, ナンピン4回, クールダウンなし, フィルターなし)
res_base = test_config(fixed_margin=5.0, max_stages=4, cooldown_bars=0, use_trend_filter=False)
print(f"1. 現状ベースライン: 利回り: {res_base['return_pct']:+6.1f}% | 最小残高: ${res_base['min_eq']:5.1f} | MaxDD: {res_base['max_dd']:4.1f}% | SL: {res_base['sl_trades']}回")

# 2. ナンピンを最大3回に制限
res_stage3 = test_config(fixed_margin=5.0, max_stages=3, cooldown_bars=0, use_trend_filter=False)
print(f"2. ナンピン最大3回: 利回り: {res_stage3['return_pct']:+6.1f}% | 最小残高: ${res_stage3['min_eq']:5.1f} | MaxDD: {res_stage3['max_dd']:4.1f}% | SL: {res_stage3['sl_trades']}回")

# 3. 損切り後24hクールダウン追加
res_cd = test_config(fixed_margin=5.0, max_stages=3, cooldown_bars=288, use_trend_filter=False)
print(f"3. + 24hクールダウン: 利回り: {res_cd['return_pct']:+6.1f}% | 最小残高: ${res_cd['min_eq']:5.1f} | MaxDD: {res_cd['max_dd']:4.1f}% | SL: {res_cd['sl_trades']}回")

# 4. トレンドフィルター (EMA24h以上のみロング) 追加
res_trend = test_config(fixed_margin=5.0, max_stages=3, cooldown_bars=288, use_trend_filter=True)
print(f"4. + トレンドフィルター: 利回り: {res_trend['return_pct']:+6.1f}% | 最小残高: ${res_trend['min_eq']:5.1f} | MaxDD: {res_trend['max_dd']:4.1f}% | SL: {res_trend['sl_trades']}回")

# 5. ナンピン幅を 3.0% に拡大
res_np3 = test_config(fixed_margin=5.0, max_stages=3, nanpin_drop=0.030, cooldown_bars=288, use_trend_filter=True)
print(f"5. + ナンピン幅3.0%: 利回り: {res_np3['return_pct']:+6.1f}% | 最小残高: ${res_np3['min_eq']:5.1f} | MaxDD: {res_np3['max_dd']:4.1f}% | SL: {res_np3['sl_trades']}回")

# 6. 動的サイジング (残高の4%) + トレンドフィルター
res_dynamic = test_config(margin_ratio=0.04, max_stages=3, nanpin_drop=0.030, cooldown_bars=288, use_trend_filter=True)
print(f"6. + 動的ロット(4%): 利回り: {res_dynamic['return_pct']:+6.1f}% | 最小残高: ${res_dynamic['min_eq']:5.1f} | MaxDD: {res_dynamic['max_dd']:4.1f}% | SL: {res_dynamic['sl_trades']}回")
