#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSIMA戦略 (5分足) に EMA 200 トレンドフィルターを組み込んだ完全精密バックテスト:
・ベース戦略: RSIMA (RSI 9, EMA 7, GC, m < 40)
・比較:
  Case 1: RSIMA 単体 (フィルターなし、ナンピン0.5%, SL 3.0%)
  Case 2: RSIMA + EMA 200 フィルター (ナンピン0.5%, SL 3.0%)
  Case 3: RSIMA + EMA 200 フィルター + 最適設定 (ナンピン3.0%最大3回, SL 9.0%, クールダウン24h)
・実運用ポートフォリオ: 原資$100, 証拠金$7.5, レバ10倍, 最大2スロット
"""
import sys
import time
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

def calc_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(series: pd.Series, length: int = 9) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    rma_up = calc_rma(up, length)
    rma_down = calc_rma(down, length)
    rs = rma_up / rma_down.replace(0.0, np.nan)
    rsi_vals = np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))
    return pd.Series(rsi_vals, index=series.index).fillna(50.0)

def load_data():
    mp_dict = {}
    for sym in TARGET_SYMBOLS:
        cache_file = DATA_DIR / f"{sym}_5m_mp.pkl"
        if cache_file.exists():
            df = pd.read_pickle(cache_file)
            mp_dict[sym] = df
    return mp_dict

def prepare_rsima_data(mp_dict):
    symbols = [s for s in TARGET_SYMBOLS if s in mp_dict]
    data = {}
    for sym in symbols:
        df = mp_dict[sym]
        c = df["close"]
        h = df["high"].values
        l = df["low"].values
        closes = c.values
        
        rsi_s = calc_rsi(c, 9)
        lma_s = rsi_s.ewm(span=7, adjust=False).mean()
        rsi = rsi_s.values
        lma = lma_s.values
        
        # EMA 200
        ema200 = c.ewm(span=200, adjust=False).mean().values
        
        # RSIMA GC判定
        r_prev = np.roll(rsi, 1)
        r_prev[0] = rsi[0]
        m_prev = np.roll(lma, 1)
        m_prev[0] = lma[0]
        
        # GC & m < 40 & r < 70
        gc = (rsi > lma) & (r_prev <= m_prev) & (lma < 40.0) & (rsi < 70.0)
        
        data[sym] = {
            "close": closes,
            "high": h,
            "low": l,
            "rsi": rsi,
            "lma": lma,
            "ema200": ema200,
            "gc": gc
        }
    return symbols, data

def run_rsima_sim(
    symbols,
    data,
    use_ema200: bool = False,
    nanpin_drop: float = 0.005,
    max_stages: int = 5,
    sl_pct: float = 0.030,
    cooldown_bars: int = 0,
    initial_balance: float = 100.0,
    margin_usdt: float = 7.5,
    leverage: float = 10.0,
    max_active_positions: int = 2
):
    n_bars = len(data[symbols[0]]["close"])
    balance = initial_balance
    equity_curve = [initial_balance]
    positions = {}
    pending = {}
    last_sl_bar = {s: -999999 for s in symbols}
    trades = []
    
    tp_thresh = 70.0
    callback_pct = 0.015
    
    for i in range(1, n_bars):
        # 1. pending約定
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
                        "count": 1,
                        "trailing_active": False,
                        "trail_peak": 0.0
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
            c = data[sym]["close"][i]
            h = data[sym]["high"][i]
            l = data[sym]["low"][i]
            r = data[sym]["rsi"][i]
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
                
            # TP判定 (RSI > 70 でトレーリング開始)
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
                    trades.append({"symbol": sym, "pnl": pnl, "type": "TP", "count": pos["count"]})
                    del positions[sym]
                    continue
                    
            # ナンピン
            if pos["count"] < max_stages and sym not in pending:
                drop_price = pos["last_price"] * (1.0 - nanpin_drop)
                if (c <= drop_price) or (l <= drop_price):
                    pending[sym] = {"limit_price": c * 0.999}
                    
        # 3. エントリー
        active_slots = len(positions) + len([s for s in pending if s not in positions])
        if active_slots < max_active_positions:
            for sym in symbols:
                if sym in positions or sym in pending:
                    continue
                if i - last_sl_bar[sym] < cooldown_bars:
                    continue
                if use_ema200 and data[sym]["close"][i] < data[sym]["ema200"][i]:
                    # EMA 200 より下はロング禁止
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
        "tp_trades": len([t for t in trades if t["type"] == "TP"]),
        "equity_curve": equity_curve
    }

if __name__ == "__main__":
    t0 = time.time()
    mp_dict = load_data()
    symbols, data = prepare_rsima_data(mp_dict)
    print(f"Data prepared in {time.time() - t0:.2f}s")
    
    print("=" * 80)
    print(" 【RSIMA 5分足 × EMA 200 トレンドフィルター セットアップ検証】")
    print("=" * 80)
    
    # 1. RSIMA 単体 (従来設定: フィルターなし, ナンピン0.5%最大5回, SL 3.0%)
    res1 = run_rsima_sim(symbols, data, use_ema200=False, nanpin_drop=0.005, max_stages=5, sl_pct=0.030, cooldown_bars=0)
    print(f"Case 1 (RSIMA 単体・従来): 利回り:{res1['return_pct']:+6.1f}% (+${res1['profit']:5.1f}) | 最小残高:${res1['min_eq']:5.1f} | MaxDD:{res1['max_dd']:4.1f}% | 勝率:{res1['wr']:4.1f}% | 取引数:{res1['trades']} (SL:{res1['sl_trades']}回)")
    
    # 2. RSIMA + EMA 200 のみ追加 (従来設定: ナンピン0.5%最大5回, SL 3.0%)
    res2 = run_rsima_sim(symbols, data, use_ema200=True, nanpin_drop=0.005, max_stages=5, sl_pct=0.030, cooldown_bars=0)
    print(f"Case 2 (+ EMA 200 フィルター): 利回り:{res2['return_pct']:+6.1f}% (+${res2['profit']:5.1f}) | 最小残高:${res2['min_eq']:5.1f} | MaxDD:{res2['max_dd']:4.1f}% | 勝率:{res2['wr']:4.1f}% | 取引数:{res2['trades']} (SL:{res2['sl_trades']}回)")
    
    # 3. RSIMA + EMA 200 + ナンピン3.0%最大3回 + SL 9.0% + クールダウン24h
    res3 = run_rsima_sim(symbols, data, use_ema200=True, nanpin_drop=0.030, max_stages=3, sl_pct=0.090, cooldown_bars=288)
    print(f"Case 3 (+ EMA 200 + 黄金資金管理): 利回り:{res3['return_pct']:+6.1f}% (+${res3['profit']:5.1f}) | 最小残高:${res3['min_eq']:5.1f} | MaxDD:{res3['max_dd']:4.1f}% | 勝率:{res3['wr']:4.1f}% | 取引数:{res3['trades']} (SL:{res3['sl_trades']}回)")
    
    # 4. SL幅のスキャン (Case 3 をベースに SL 5%, 7%, 8.5%, 9%, 10%)
    print("\n--- RSIMA + EMA 200 における SL 幅スキャン ---")
    for sl in [0.030, 0.050, 0.070, 0.085, 0.090, 0.100]:
        res = run_rsima_sim(symbols, data, use_ema200=True, nanpin_drop=0.030, max_stages=3, sl_pct=sl, cooldown_bars=288)
        print(f"SL {sl*100:4.1f}% | 利回り:{res['return_pct']:+6.1f}% (+${res['profit']:5.1f}) | 最小残高:${res['min_eq']:5.1f} | MaxDD:{res['max_dd']:4.1f}% | 勝率:{res['wr']:4.1f}% | 取引数:{res['trades']} (SL:{res['sl_trades']}回)")
