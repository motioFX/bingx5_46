import sys
import os
import glob
import time
import datetime
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

sys.stdout.reconfigure(encoding='utf-8')

# ==============================================================================
# 1. 2024年〜2026年特化: 買いのみ米国時間（22:00〜02:00 JST）検証
# ==============================================================================

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def calculate_vp_fast(closes, highs, lows, volumes, period=288, num_bins=30):
    n = len(closes)
    val = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    poc = np.full(n, np.nan)
    
    for i in range(period, n):
        sub_c = closes[i-period:i]
        sub_h = highs[i-period:i]
        sub_l = lows[i-period:i]
        sub_v = volumes[i-period:i]
        
        min_p = np.min(sub_l)
        max_p = np.max(sub_h)
        if max_p <= min_p:
            continue
            
        bins = np.linspace(min_p, max_p, num_bins + 1)
        mids = (sub_c + sub_h + sub_l) / 3.0
        bin_idx = np.clip(np.digitize(mids, bins) - 1, 0, num_bins - 1)
        
        v_profile = np.zeros(num_bins)
        np.add.at(v_profile, bin_idx, sub_v)
        
        poc_bin = np.argmax(v_profile)
        poc[i] = (bins[poc_bin] + bins[poc_bin + 1]) / 2.0
        
        total_v = np.sum(v_profile)
        target_v = total_v * 0.68
        curr_v = v_profile[poc_bin]
        l_idx = poc_bin
        h_idx = poc_bin
        
        while curr_v < target_v and (l_idx > 0 or h_idx < num_bins - 1):
            next_l_v = v_profile[l_idx - 1] if l_idx > 0 else 0
            next_h_v = v_profile[h_idx + 1] if h_idx < num_bins - 1 else 0
            if next_l_v >= next_h_v and l_idx > 0:
                l_idx -= 1
                curr_v += next_l_v
            elif h_idx < num_bins - 1:
                h_idx += 1
                curr_v += next_h_v
            elif l_idx > 0:
                l_idx -= 1
                curr_v += next_l_v
            else:
                break
                
        val[i] = bins[l_idx]
        vah[i] = bins[h_idx + 1]
        
    return val, vah, poc

def resample_5m_to_1h(df_5m):
    df = df_5m.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df.set_index('timestamp', inplace=True)
    df_1h = df.resample('1h').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    return df_1h

def determine_regime_3stage(btc_1h, alt_1h_dict, target_time):
    t_end = target_time
    btc_sub = btc_1h[btc_1h['timestamp'] <= t_end]
    if len(btc_sub) < 30 * 24:
        return "NEUTRAL", {}
        
    stages = [(30, 2), (10, 2), (5, 1)]
    score = 0
    total_votes = 10
    
    for days, votes in stages:
        n_bars = days * 24
        p_start = btc_sub['close'].iloc[-n_bars]
        p_end = btc_sub['close'].iloc[-1]
        btc_perf = (p_end - p_start) / p_start
        
        if btc_perf > 0.01:
            score += votes
        elif btc_perf < -0.01:
            score -= votes
            
        alt_perfs = []
        for sym, a_df in alt_1h_dict.items():
            a_sub = a_df[a_df['timestamp'] <= t_end]
            if len(a_sub) >= n_bars:
                ap_start = a_sub['close'].iloc[-n_bars]
                ap_end = a_sub['close'].iloc[-1]
                alt_perfs.append((ap_end - ap_start) / ap_start)
                
        if alt_perfs:
            med_perf = np.median(alt_perfs)
            if med_perf > 0.01:
                score += votes
            elif med_perf < -0.01:
                score -= votes
                
    if score >= 4:
        regime = "BUY"
    elif score <= -4:
        regime = "SELL"
    else:
        regime = "NEUTRAL"
        
    return regime, {"score": score}

def precompute_all_8h_regimes(btc_1h, alt_1h_dict):
    start_dt = pd.to_datetime("2024-01-01 00:00:00", utc=True)
    end_dt = pd.to_datetime(btc_1h["timestamp"].iloc[-1], utc=True)
    
    all_dates = pd.date_range(start=start_dt.tz_convert("Asia/Tokyo").date() - pd.Timedelta(days=1),
                              end=end_dt.tz_convert("Asia/Tokyo").date() + pd.Timedelta(days=1),
                              freq='D')
    
    slot_times = []
    for d in all_dates:
        for hr in [1, 9, 17]:
            jst_dt = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hr, minute=0, second=0, tz="Asia/Tokyo")
            utc_dt = jst_dt.tz_convert("UTC")
            if start_dt <= utc_dt <= end_dt:
                slot_times.append(utc_dt)
                
    slot_times = sorted(list(set(slot_times)))
    print(f"🔍 2024年以降 ({len(slot_times)} スロット) の「8時間ごと地合い判定 (01/09/17 JST)」を計算中...")
    
    regime_results = []
    t0 = time.time()
    for st in slot_times:
        reg, details = determine_regime_3stage(btc_1h, alt_1h_dict, st)
        regime_results.append({
            "slot_time": st,
            "regime": reg,
            "score": details.get("score", 0)
        })
        
    el = time.time() - t0
    reg_counts = pd.Series([r["regime"] for r in regime_results]).value_counts().to_dict()
    print(f"✅ 2024年以降 8時間地合い判定 完了 ({len(slot_times)} スロット / {el:.1f}秒) -> 🟢BUY: {reg_counts.get('BUY', 0)}回, 🔴SELL: {reg_counts.get('SELL', 0)}回, ⚪NEUTRAL: {reg_counts.get('NEUTRAL', 0)}回")
    return regime_results

def simulate_oos_us_breakout(df_5m, df_15m, regime_lookup, us_start_hr=22, us_end_hr=2, use_retest=False):
    """
    df_5m: 5分足データ (2024年以降)
    us_start_hr, us_end_hr: 米国時間の範囲 (JST) 例: 22〜02
    use_retest: Trueならリテスト型, Falseなら通常のVAH_BREAK
    """
    ts_utc = pd.to_datetime(df_5m["timestamp"], utc=True)
    ts_jst = ts_utc.dt.tz_convert("Asia/Tokyo")
    hours = ts_jst.dt.hour.values
    
    c = df_5m["close"].values
    h = df_5m["high"].values
    l = df_5m["low"].values
    o = df_5m["open"].values
    v = df_5m["volume"].values
    n = len(df_5m)
    
    # VP (288本 = 24時間)
    val_5m, vah_5m, poc_5m = calculate_vp_fast(c, h, l, v, period=288, num_bins=30)
    
    # スロット時刻の計算
    def get_slot_time(t):
        jst = t.tz_convert("Asia/Tokyo")
        hr = jst.hour
        if hr >= 17: s_hr = 17
        elif hr >= 9: s_hr = 9
        elif hr >= 1: s_hr = 1
        else:
            jst_prev = jst - pd.Timedelta(days=1)
            return jst_prev.replace(hour=17, minute=0, second=0, microsecond=0).tz_convert("UTC")
        return jst.replace(hour=s_hr, minute=0, second=0, microsecond=0).tz_convert("UTC")
        
    slots = [get_slot_time(t) for t in ts_utc]
    regimes = [regime_lookup.get(st, "NEUTRAL") for st in slots]
    
    # 状態管理
    pos_qty = 0.0
    pos_cost = 0.0
    avg_px = 0.0
    pos_side = None
    entry_sl_px = 0.0
    target_tp = 0.0
    entry_bar_idx = -1
    entry_ts = None
    entry_strat = None
    entry_session = None
    cooldown_until = -1
    
    # リテスト追跡用
    retest_pending = False
    breakout_vah = 0.0
    retest_expire_bar = -1
    
    trades_out = []
    taker_fee = 0.0005
    timeout_bars = 48    # 4時間 (48本)
    cooldown_bars = 288  # 24時間 (288本)
    
    for i in range(289, n):
        cur_reg = regimes[i]
        hr = hours[i]
        ci = c[i]
        hi = h[i]
        li = l[i]
        oi = o[i]
        
        # 1. 決済判定 (保有時)
        if pos_qty > 0:
            is_exit = False
            exit_px = ci
            exit_type = ""
            
            if pos_side == "LONG":
                # 地合い変化BE脱出
                if cur_reg != "BUY" and (hi >= avg_px * 1.0020 or ci >= avg_px * 1.0020):
                    is_exit = True
                    exit_px = max(ci, avg_px * 1.0020) if hi >= avg_px * 1.0020 else ci
                    exit_type = "REGIME_BE_EXIT"
                # SL判定
                elif entry_sl_px > 0 and ci <= entry_sl_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL"
                elif li <= avg_px * 0.970:
                    is_exit = True
                    exit_px = avg_px * 0.970
                    exit_type = "SL_3%"
                # タイムアウト
                elif (i - entry_bar_idx) >= timeout_bars and ci < avg_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "TIMEOUT"
                # TP判定
                elif (hi >= target_tp or ci >= target_tp) and target_tp >= avg_px * 1.0040:
                    is_exit = True
                    exit_px = max(ci, target_tp) if hi >= target_tp else ci
                    exit_type = "TP"
                    
                if is_exit:
                    sell_val = pos_qty * exit_px
                    fee = sell_val * taker_fee
                    pnl = sell_val - pos_cost - fee
                    trades_out.append({
                        "entry_time": entry_ts,
                        "exit_time": ts_utc.iloc[i],
                        "side": "LONG",
                        "session": entry_session,
                        "strategy": entry_strat,
                        "pnl": float(pnl),
                        "exit_type": exit_type,
                    })
                    if exit_type.startswith("SL") or exit_type == "TIMEOUT":
                        cooldown_until = i + cooldown_bars
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_px = 0.0
                    entry_sl_px = 0.0
                    entry_bar_idx = -1
                    
            elif pos_side == "SHORT":
                if cur_reg != "SELL" and (li <= avg_px * 0.9980 or ci <= avg_px * 0.9980):
                    is_exit = True
                    exit_px = min(ci, avg_px * 0.9980) if li <= avg_px * 0.9980 else ci
                    exit_type = "REGIME_BE_EXIT"
                elif entry_sl_px > 0 and ci >= entry_sl_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL"
                elif hi >= avg_px * 1.030:
                    is_exit = True
                    exit_px = avg_px * 1.030
                    exit_type = "SL_3%"
                elif entry_strat == "val_break" and not np.isnan(poc_5m[i]) and ci > poc_5m[i] and ci > avg_px * 1.004:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL_POC"
                elif (i - entry_bar_idx) >= timeout_bars and ci > avg_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "TIMEOUT"
                elif (li <= target_tp or ci <= target_tp) and target_tp <= avg_px * 0.9960:
                    is_exit = True
                    exit_px = min(ci, target_tp) if li <= target_tp else ci
                    exit_type = "TP"
                    
                if is_exit:
                    buy_val = pos_qty * exit_px
                    fee = buy_val * taker_fee
                    pnl = pos_cost - buy_val - fee
                    trades_out.append({
                        "entry_time": entry_ts,
                        "exit_time": ts_utc.iloc[i],
                        "side": "SHORT",
                        "session": entry_session,
                        "strategy": entry_strat,
                        "pnl": float(pnl),
                        "exit_type": exit_type,
                    })
                    if exit_type.startswith("SL") or exit_type == "TIMEOUT":
                        cooldown_until = i + cooldown_bars
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_px = 0.0
                    entry_sl_px = 0.0
                    entry_bar_idx = -1
                    
            continue  # ポジション保有中は新規エントリーしない
            
        # 2. 新規エントリー判定 (ノーポジ時)
        if i <= cooldown_until:
            continue
            
        # 米国時間判定
        if us_start_hr > us_end_hr:
            is_us_time = (hr >= us_start_hr or hr < us_end_hr)
        else:
            is_us_time = (us_start_hr <= hr < us_end_hr)
            
        # (A) BUY地合いのエントリー
        if cur_reg == "BUY":
            # ① 東京時間 (09-16 JST): レンジ逆張り VAL_POC
            if 9 <= hr < 16:
                if not np.isnan(val_5m[i]) and not np.isnan(poc_5m[i]) and poc_5m[i] > val_5m[i]:
                    # VAL反発判定 (前足安値がVAL近辺、当足終値がVAL以上かつ陽線)
                    if l[i-1] <= val_5m[i-1] * 1.002 and ci > val_5m[i] and ci >= oi:
                        pos_side = "LONG"
                        entry_session = "TOKYO"
                        entry_strat = "val_poc"
                        avg_px = ci
                        pos_cost = 15.0
                        pos_qty = 15.0 / avg_px
                        fee = pos_cost * taker_fee
                        pos_cost += fee
                        entry_sl_px = min(l[i-10:i+1]) if i >= 10 else avg_px * 0.985
                        entry_sl_px = min(entry_sl_px, avg_px * 0.998)
                        target_tp = val_5m[i] + (vah_5m[i] - val_5m[i]) * 0.50
                        entry_bar_idx = i
                        entry_ts = ts_utc.iloc[i]
                        retest_pending = False
                        continue
                        
            # ② 欧州時間 (16-22 JST): 買いは完全停止（待機）
            # (ユーザー指示: 買いは米国時間のみ)
            
            # ③ 米国時間 (22-02 JST 等): 買いブレイクアウト
            if is_us_time:
                if not np.isnan(vah_5m[i]) and vah_5m[i] > 0:
                    if not use_retest:
                        # 通常の飛び乗り VAH_BREAK
                        if c[i-1] <= vah_5m[i-1] and ci > vah_5m[i]:
                            pos_side = "LONG"
                            entry_session = "US"
                            entry_strat = "vah_break"
                            avg_px = ci
                            pos_cost = 15.0
                            pos_qty = 15.0 / avg_px
                            fee = pos_cost * taker_fee
                            pos_cost += fee
                            entry_sl_px = max(avg_px * 0.970, poc_5m[i] if not np.isnan(poc_5m[i]) else avg_px * 0.985)
                            risk_dist = max(avg_px * 0.005, avg_px - entry_sl_px)
                            target_tp = avg_px + risk_dist * 2.0  # RR 1:2
                            entry_bar_idx = i
                            entry_ts = ts_utc.iloc[i]
                            continue
                    else:
                        # リテスト反発型 VAH_RETEST
                        # 初回の上抜け検知
                        if c[i-1] <= vah_5m[i-1] and ci > vah_5m[i]:
                            retest_pending = True
                            breakout_vah = vah_5m[i]
                            retest_expire_bar = i + 12  # 最大1時間(12本)待機
                        elif retest_pending:
                            if i > retest_expire_bar:
                                retest_pending = False
                            else:
                                # VAH付近まで押し目を形成し、反発陽線
                                if li <= breakout_vah * 1.003 and ci >= breakout_vah * 0.998 and ci > oi:
                                    pos_side = "LONG"
                                    entry_session = "US"
                                    entry_strat = "vah_retest"
                                    avg_px = ci
                                    pos_cost = 15.0
                                    pos_qty = 15.0 / avg_px
                                    fee = pos_cost * taker_fee
                                    pos_cost += fee
                                    entry_sl_px = breakout_vah * 0.993
                                    risk_dist = max(avg_px * 0.004, avg_px - entry_sl_px)
                                    target_tp = avg_px + risk_dist * 2.0
                                    entry_bar_idx = i
                                    entry_ts = ts_utc.iloc[i]
                                    retest_pending = False
                                    continue
                                    
        # (B) SELL地合いのエントリー
        elif cur_reg == "SELL":
            retest_pending = False
            # ① 東京時間 (09-16 JST): レンジ逆張り VAH_POC
            if 9 <= hr < 16:
                if not np.isnan(vah_5m[i]) and not np.isnan(poc_5m[i]) and vah_5m[i] > poc_5m[i]:
                    if h[i-1] >= vah_5m[i-1] * 0.998 and ci < vah_5m[i] and ci <= oi:
                        pos_side = "SHORT"
                        entry_session = "TOKYO"
                        entry_strat = "vah_poc"
                        avg_px = ci
                        pos_cost = 15.0
                        pos_qty = 15.0 / avg_px
                        fee = pos_cost * taker_fee
                        pos_cost += fee
                        entry_sl_px = max(h[i-10:i+1]) if i >= 10 else avg_px * 1.015
                        entry_sl_px = max(entry_sl_px, avg_px * 1.002)
                        target_tp = vah_5m[i] - (vah_5m[i] - val_5m[i]) * 0.50
                        entry_bar_idx = i
                        entry_ts = ts_utc.iloc[i]
                        continue
                        
            # ② 欧州時間 (16-22 JST): ショートブレイク VAL_BREAK
            elif 16 <= hr < 22:
                if not np.isnan(val_5m[i]) and val_5m[i] > 0:
                    if c[i-1] >= val_5m[i-1] and ci < val_5m[i]:
                        pos_side = "SHORT"
                        entry_session = "EUROPE"
                        entry_strat = "val_break"
                        avg_px = ci
                        pos_cost = 15.0
                        pos_qty = 15.0 / avg_px
                        fee = pos_cost * taker_fee
                        pos_cost += fee
                        entry_sl_px = min(avg_px * 1.030, poc_5m[i] if not np.isnan(poc_5m[i]) else avg_px * 1.015)
                        risk_dist = max(avg_px * 0.005, entry_sl_px - avg_px)
                        target_tp = avg_px - risk_dist * 2.0
                        entry_bar_idx = i
                        entry_ts = ts_utc.iloc[i]
                        continue
                        
    return pd.DataFrame(trades_out)

def main():
    print("="*95)
    print("🚀 【2024年〜2026年特化】買いのみ米国時間シフト検証 ($15単発固定・最大2枠)")
    print("="*95)
    
    # 1. データロード
    t0 = time.time()
    files = glob.glob("Data/binance_candles_5m_4y/*.pkl")
    btc_f = [f for f in files if "BTCUSDT" in f][0]
    alt_files = [f for f in files if "BTCUSDT" not in f]
    
    btc_5m = pd.read_pickle(btc_f)
    btc_1h = resample_5m_to_1h(btc_5m)
    
    alt_1h_dict = {}
    alt_5m_dict = {}
    for f in alt_files:
        sym = os.path.basename(f).replace("_5m.pkl", "").replace(".pkl", "")
        df_5m = pd.read_pickle(f)
        df_5m["timestamp"] = pd.to_datetime(df_5m["timestamp"], utc=True)
        # 2024年以降にフィルタ
        df_5m_2024 = df_5m[df_5m["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
        if len(df_5m_2024) > 500:
            alt_5m_dict[sym] = df_5m_2024
            alt_1h_dict[sym] = resample_5m_to_1h(df_5m)
            
    print(f"✅ 全 {len(alt_5m_dict)} 銘柄ロード完了 ({time.time() - t0:.1f}秒)")
    
    # 2. 地合い判定
    regime_results = precompute_all_8h_regimes(btc_1h, alt_1h_dict)
    regime_lookup = {r["slot_time"]: r["regime"] for r in regime_results}
    
    # 3. 複数の米国時間パターンで検証
    # パターン1: 米国コア時間 (22:00〜02:00 JST / 4時間) × 飛び乗り VAH_BREAK
    # パターン2: 米国コア時間 (22:00〜02:00 JST / 4時間) × リテスト型 VAH_RETEST
    # パターン3: 米国拡大時間 (22:00〜06:00 JST / 8時間) × リテスト型 VAH_RETEST
    # 参考: 従来の欧州ブレイク (16:00〜22:00 JST)
    
    patterns = [
        {"name": "① 米国コア (22-02 JST) × 飛び乗り", "start": 22, "end": 2, "retest": False},
        {"name": "② 米国コア (22-02 JST) × リテスト型", "start": 22, "end": 2, "retest": True},
        {"name": "③ 米国拡大 (22-06 JST) × リテスト型", "start": 22, "end": 6, "retest": True},
    ]
    
    pattern_results = {}
    
    for pat in patterns:
        print(f"\n⚡ 検証中: {pat['name']} ...")
        all_sym_trades = []
        for sym, df in alt_5m_dict.items():
            tr_df = simulate_oos_us_breakout(df, None, regime_lookup,
                                             us_start_hr=pat["start"],
                                             us_end_hr=pat["end"],
                                             use_retest=pat["retest"])
            if not tr_df.empty:
                tr_df["symbol"] = sym
                all_sym_trades.append(tr_df)
                
        if all_sym_trades:
            comb = pd.concat(all_sym_trades, ignore_index=True)
            comb = comb.sort_values("entry_time").reset_index(drop=True)
            
            # 最大2枠シミュレーション
            sim_trades = []
            active_pos = []
            for idx, r in comb.iterrows():
                e_t = r["entry_time"]
                active_pos = [p for p in active_pos if p["exit_time"] > e_t]
                if len(active_pos) < 2:
                    sim_trades.append(r)
                    active_pos.append(r)
                    
            res_df = pd.DataFrame(sim_trades)
            pattern_results[pat["name"]] = res_df
            
            # 成績表示
            n_tr = len(res_df)
            wins = res_df[res_df["pnl"] > 0]
            losses = res_df[res_df["pnl"] <= 0]
            wr = len(wins) / n_tr * 100 if n_tr > 0 else 0
            gp = wins["pnl"].sum()
            gl = abs(losses["pnl"].sum())
            pf = gp / gl if gl > 0 else np.nan
            tot_pnl = res_df["pnl"].sum()
            
            # セッション別
            tokyo_pnl = res_df[res_df["session"] == "TOKYO"]["pnl"].sum()
            europe_pnl = res_df[res_df["session"] == "EUROPE"]["pnl"].sum()
            us_pnl = res_df[res_df["session"] == "US"]["pnl"].sum()
            
            print(f"  🏆 全体: 取引: {n_tr}回 | 勝率: {wr:4.1f}% | PF: {pf:4.2f} | 純利益: {tot_pnl:+6.2f} USDT")
            print(f"     🗼東京: {tokyo_pnl:+6.2f} USDT | 🏰欧州ショート: {europe_pnl:+6.2f} USDT | 🗽米国ロング: {us_pnl:+6.2f} USDT")
            
            # 戦略別内訳
            for strat in ["val_poc", "vah_poc", "val_break", "vah_break", "vah_retest"]:
                s_df = res_df[res_df["strategy"] == strat]
                if not s_df.empty:
                    s_w = s_df[s_df["pnl"] > 0]
                    s_l = s_df[s_df["pnl"] <= 0]
                    s_pf = s_w["pnl"].sum() / abs(s_l["pnl"].sum()) if len(s_l) > 0 and abs(s_l["pnl"].sum()) > 0 else 999
                    print(f"     📌 {strat:<12} | {len(s_df):>4}回 | 勝率: {len(s_w)/len(s_df)*100:4.1f}% | PF: {s_pf:4.2f} | 損益: {s_df['pnl'].sum():+6.2f} USDT")

    # グラフ比較
    plt.figure(figsize=(13, 7))
    for name, df in pattern_results.items():
        if not df.empty:
            df["cum_pnl"] = df["pnl"].cumsum()
            plt.plot(pd.to_datetime(df["exit_time"]), df["cum_pnl"], label=f"{name} ({df['pnl'].sum():+.2f} USDT)", linewidth=2.0)
            
    plt.title("2024-2026 Shift Long Breakout to US Hours (Cumulative PnL)", fontsize=13, fontweight="bold")
    plt.ylabel("Cumulative PnL (USDT)")
    plt.xlabel("Date")
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig("Data/backtest_2024_us_breakout.png", dpi=150)
    print("\n📈 米国時間シフト比較チャート保存完了: Data/backtest_2024_us_breakout.png")

if __name__ == "__main__":
    main()
