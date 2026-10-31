import glob
import os
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from bingx5_46_3logic import (
    make_market_profile_dual_va,
    build_entry_precision_filter,
    calc_rsi_ema,
    calc_ema
)

def run_session_backtest(df_5m: pd.DataFrame, symbol: str, side_mode: str = "long", days: int = 30):
    """
    直近 days 日間のデータで、
    [A] 現行24時間モデル
    [B] 時間帯ロール制 + 米国時間トレーリングストップモデル
    を比較バックテストする
    """
    bars_needed = days * 288
    if len(df_5m) < bars_needed:
        df = df_5m.copy().reset_index(drop=True)
    else:
        df = df_5m.tail(bars_needed).copy().reset_index(drop=True)
    
    n = len(df)
    c = df['close'].astype(float).values
    h = df['high'].astype(float).values
    l = df['low'].astype(float).values
    
    # タイムスタンプからJST時間を算出
    # timestamp形式: "2026-10-31 12:00:00" or ISO
    ts = pd.to_datetime(df['timestamp'])
    if ts.dt.tz is None:
        # UTCと仮定してJST(+9)に変換
        hours_jst = (ts + pd.Timedelta(hours=9)).dt.hour.values
    else:
        hours_jst = ts.dt.tz_convert('Asia/Tokyo').dt.hour.values

    # MP計算 (5m足、period=150, num_bins=50, va=70%)
    mp_res = make_market_profile_dual_va(df, period=150, num_bins=50, step=6, use_decay=True)
    val = mp_res['val_70']
    vah = mp_res['vah_70']
    poc = mp_res['poc']
    
    # エントリーフィルター
    filter_vp = build_entry_precision_filter(df, side=side_mode, is_vp=True, is_breakout=False)
    filter_brk = build_entry_precision_filter(df, side=side_mode, is_vp=True, is_breakout=True)

    def simulate(mode: str):
        # mode: '24h' or 'session_ts'
        equity = 100.0
        pos = 0 # 0: none, 1: long, -1: short
        entry_px = 0.0
        peak_px = 0.0 # トレール用
        trades = []
        pnl_curve = []
        
        target_ratio = 0.60
        sl_pct = 0.025
        trail_act_pct = 0.006 # +0.6%でトレール発動
        trail_dist_pct = 0.004 # ピークから0.4%押しで決済
        
        cooldown = 0
        
        for i in range(1, n):
            curr_c = c[i]
            prev_c = c[i-1]
            curr_h = h[i]
            curr_l = l[i]
            curr_val = val[i]
            curr_vah = vah[i]
            prev_vah = vah[i-1]
            prev_val = val[i-1]
            curr_poc = poc[i]
            hr = hours_jst[i]
            
            # 時間帯区分 (JST)
            # 東京: 9〜16
            # 欧州: 16〜22
            # 米国: 22〜翌9
            is_tokyo = (9 <= hr < 16)
            is_london = (16 <= hr < 22)
            is_ny = not (is_tokyo or is_london)
            
            # 決済判定
            if pos != 0:
                if pos == 1:
                    # LONG保有中
                    if curr_h > peak_px:
                        peak_px = curr_h
                    
                    unrealized_pct = (curr_c - entry_px) / entry_px
                    
                    # 1. SL (動的SL -2.5%)
                    if curr_c <= entry_px * (1 - sl_pct):
                        pnl = (entry_px * (1 - sl_pct) - entry_px) / entry_px * 15.0 - 0.03
                        equity += pnl
                        trades.append({'pnl': pnl, 'type': 'SL', 'reason': 'SL'})
                        pos = 0
                        cooldown = 12
                        continue
                    
                    # 2. 米国時間トレーリングストップ (モード有効時)
                    if mode == 'session_ts' and is_ny:
                        # ピークが建値+0.6%以上到達していればトレール発動
                        if peak_px >= entry_px * (1 + trail_act_pct):
                            trail_stop = peak_px * (1 - trail_dist_pct)
                            # 最低でも建値微益確保
                            trail_stop = max(trail_stop, entry_px * 1.002)
                            if curr_c <= trail_stop:
                                exit_px = trail_stop
                                pnl = (exit_px - entry_px) / entry_px * 15.0 - 0.03
                                equity += pnl
                                trades.append({'pnl': pnl, 'type': 'TRAIL', 'reason': 'NY_TRAIL_TP'})
                                pos = 0
                                continue
                    
                    # 3. 通常利確 (VAH到達または上方拡張)
                    tp_px = curr_vah + (curr_vah - curr_val) * target_ratio if trade_strat == 'brk' else curr_val + (curr_vah - curr_val) * target_ratio
                    if curr_c >= tp_px and curr_c >= entry_px * 1.004:
                        pnl = (curr_c - entry_px) / entry_px * 15.0 - 0.03
                        equity += pnl
                        trades.append({'pnl': pnl, 'type': 'TP', 'reason': 'REGULAR_TP'})
                        pos = 0
                        continue
                        
                elif pos == -1:
                    # SHORT保有中
                    if curr_l < peak_px or peak_px == 0.0:
                        peak_px = curr_l
                        
                    # 1. SL (動的SL +2.5%)
                    if curr_c >= entry_px * (1 + sl_pct):
                        pnl = (entry_px - entry_px * (1 + sl_pct)) / entry_px * 15.0 - 0.03
                        equity += pnl
                        trades.append({'pnl': pnl, 'type': 'SL', 'reason': 'SL'})
                        pos = 0
                        cooldown = 12
                        continue
                    
                    # 2. 米国時間トレーリングストップ (モード有効時)
                    if mode == 'session_ts' and is_ny:
                        if peak_px <= entry_px * (1 - trail_act_pct):
                            trail_stop = peak_px * (1 + trail_dist_pct)
                            trail_stop = min(trail_stop, entry_px * 0.998)
                            if curr_c >= trail_stop:
                                exit_px = trail_stop
                                pnl = (entry_px - exit_px) / entry_px * 15.0 - 0.03
                                equity += pnl
                                trades.append({'pnl': pnl, 'type': 'TRAIL', 'reason': 'NY_TRAIL_TP'})
                                pos = 0
                                continue
                                
                    # 3. 通常利確
                    tp_px = curr_val - (curr_vah - curr_val) * target_ratio if trade_strat == 'brk' else curr_vah - (curr_vah - curr_val) * target_ratio
                    if curr_c <= tp_px and curr_c <= entry_px * 0.996:
                        pnl = (entry_px - curr_c) / entry_px * 15.0 - 0.03
                        equity += pnl
                        trades.append({'pnl': pnl, 'type': 'TP', 'reason': 'REGULAR_TP'})
                        pos = 0
                        continue
            
            # クールダウン
            if cooldown > 0:
                cooldown -= 1
                continue
                
            # 新規エントリー判定
            if pos == 0:
                # [B] 時間帯ロール制: 米国時間は新規禁止
                if mode == 'session_ts' and is_ny:
                    continue
                
                # LONGエントリー判定
                if side_mode == 'long':
                    # 1. レンジ逆張り (VAL_POC)
                    val_gc = (prev_c <= prev_val) and (curr_c > curr_val)
                    allow_vp = (mode == '24h') or (mode == 'session_ts' and is_tokyo)
                    if allow_vp and val_gc and filter_vp[i]:
                        pos = 1
                        entry_px = curr_c
                        peak_px = curr_c
                        trade_strat = 'vp'
                        continue
                        
                    # 2. ブレイクアウト (VAH_BREAK)
                    vah_brk = (prev_c <= prev_vah) and (curr_c > curr_vah) and (curr_c <= curr_vah + (curr_vah - curr_val) * 0.35)
                    allow_brk = (mode == '24h') or (mode == 'session_ts' and is_london)
                    if allow_brk and vah_brk and filter_brk[i]:
                        pos = 1
                        entry_px = curr_c
                        peak_px = curr_c
                        trade_strat = 'brk'
                        continue

                # SHORTエントリー判定
                elif side_mode == 'short':
                    # 1. レンジ逆張り (VAH_POC)
                    vah_dc = (prev_c >= prev_vah) and (curr_c < curr_vah)
                    allow_vp = (mode == '24h') or (mode == 'session_ts' and is_tokyo)
                    if allow_vp and vah_dc and filter_vp[i]:
                        pos = -1
                        entry_px = curr_c
                        peak_px = curr_c
                        trade_strat = 'vp'
                        continue
                        
                    # 2. ブレイクアウト (VAL_BREAK)
                    val_brk = (prev_c >= prev_val) and (curr_c < curr_val) and (curr_c >= curr_val - (curr_vah - curr_val) * 0.35)
                    allow_brk = (mode == '24h') or (mode == 'session_ts' and is_london)
                    if allow_brk and val_brk and filter_brk[i]:
                        pos = -1
                        entry_px = curr_c
                        peak_px = curr_c
                        trade_strat = 'brk'
                        continue

        total_pnl = sum(t['pnl'] for t in trades)
        trade_cnt = len(trades)
        wins = [t for t in trades if t['pnl'] > 0]
        win_rate = (len(wins) / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
        trail_cnt = len([t for t in trades if t['type'] == 'TRAIL'])
        sl_cnt = len([t for t in trades if t['type'] == 'SL'])
        tp_cnt = len([t for t in trades if t['type'] == 'TP'])
        
        return {
            'pnl': total_pnl,
            'trades': trade_cnt,
            'win_rate': win_rate,
            'trail_cnt': trail_cnt,
            'sl_cnt': sl_cnt,
            'tp_cnt': tp_cnt
        }

    res_24h = simulate('24h')
    res_session = simulate('session_ts')
    
    return res_24h, res_session

if __name__ == '__main__':
    # 主要銘柄で直近30日の比較を実行
    symbols = ['BTC-USDT', 'SOL-USDT', 'DOGE-USDT', 'APT-USDT', 'ARB-USDT', 'AVAX-USDT', 'NEAR-USDT', 'SUI-USDT']
    
    print("=" * 80)
    print("【直近30日間 実データバックテスト比較】")
    print(" [A] 現行 24時間フル稼働モデル")
    print(" [B] 時間帯ロール制 + 米国時間トレーリングストップモデル")
    print("     (東京: レンジ逆張り / 欧州: ブレイク / 米国: 新規停止＆トレール決済)")
    print("=" * 80)
    
    for side in ['long', 'short']:
        print(f"\n▼ 方向: {side.upper()} ----------------------------------------------------")
        print(f"{'銘柄':<12} | {'24h 損益':<10} {'回数':<6} {'勝率':<6} | {'時間帯+TS 損益':<14} {'回数':<6} {'勝率':<6} {'トレール':<6} | {'損益差分':<8}")
        print("-" * 80)
        
        sum_pnl_a = 0.0
        sum_pnl_b = 0.0
        
        for sym in symbols:
            fpath = f"Data/candles_5m/candles_5m_{sym}.csv"
            if not os.path.exists(fpath):
                continue
            df = pd.read_csv(fpath)
            res_a, res_b = run_session_backtest(df, sym, side_mode=side, days=30)
            
            diff = res_b['pnl'] - res_a['pnl']
            diff_str = f"{diff:+.2f}"
            sum_pnl_a += res_a['pnl']
            sum_pnl_b += res_b['pnl']
            
            print(f"{sym:<12} | ${res_a['pnl']:+6.2f}    {res_a['trades']:<6} {res_a['win_rate']:5.1f}% | ${res_b['pnl']:+6.2f}        {res_b['trades']:<6} {res_b['win_rate']:5.1f}% {res_b['trail_cnt']:<6} | {diff_str:<8}")
            
        print("-" * 80)
        print(f"合計損益    | [24h] ${sum_pnl_a:+.2f} USDT  ➔  [時間帯+TS] ${sum_pnl_b:+.2f} USDT (差分: {sum_pnl_b - sum_pnl_a:+.2f} USDT)")
