import sys
import os
import glob
import time
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import japanize_matplotlib

sys.stdout.reconfigure(encoding='utf-8')

# ==============================================================================
# 徹底解明: 地合いが【SELL】の時、欧州・米国時間のショートブレイクはどうなのか？
# 2024年〜2026年の全SELL地合いスロットにおいて、24時間すべての時間帯での
# VAL_BREAK (ショートブレイクアウト) の期待値・勝率・損益を徹底検証
# ==============================================================================

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

def precompute_8h_regimes(btc_1h, alt_1h_dict):
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
    regime_results = []
    t0 = time.time()
    for st in slot_times:
        reg, details = determine_regime_3stage(btc_1h, alt_1h_dict, st)
        regime_results.append({
            "slot_time": st,
            "regime": reg,
            "score": details.get("score", 0)
        })
    print(f"✅ 8時間地合い判定 完了 ({len(slot_times)} スロット / {time.time() - t0:.1f}秒)")
    return {r["slot_time"]: r["regime"] for r in regime_results}

def main():
    print("="*95)
    print("🔍 【真相究明】SELL地合いの時、欧州・米国時間のショートブレイクはどうなのか？")
    print("="*95)
    
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
        df_5m_2024 = df_5m[df_5m["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
        if len(df_5m_2024) > 500:
            alt_5m_dict[sym] = df_5m_2024
            alt_1h_dict[sym] = resample_5m_to_1h(df_5m)
            
    regime_lookup = precompute_8h_regimes(btc_1h, alt_1h_dict)
    
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

    # 1時間ごと(0〜23時 JST)の SELL地合い時 ショートブレイク統計
    # 1. 単純飛び乗り (Taker)
    # 2. 本番仕様 (過伸展防止 + 幅1.5%以上 + Maker指値 + RR 1:2)
    hourly_raw_trades = [[] for _ in range(24)]
    hourly_maker_trades = [[] for _ in range(24)]
    
    taker_fee = 0.0005
    maker_fee = 0.0002
    
    for sym, df in alt_5m_dict.items():
        ts_utc = pd.to_datetime(df["timestamp"], utc=True)
        ts_jst = ts_utc.dt.tz_convert("Asia/Tokyo")
        hours = ts_jst.dt.hour.values
        
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        o = df["open"].values
        v = df["volume"].values
        n = len(df)
        
        val, vah, poc = calculate_vp_fast(c, h, l, v, period=288, num_bins=30)
        
        for i in range(289, n-48):
            slot_t = get_slot_time(ts_utc.iloc[i])
            reg = regime_lookup.get(slot_t, "NEUTRAL")
            
            # 【重要】地合いが SELL の時だけを追跡！
            if reg != "SELL":
                continue
                
            hr = hours[i]
            ci = c[i]
            hi = h[i]
            li = l[i]
            oi = o[i]
            
            if not np.isnan(val[i]) and not np.isnan(vah[i]) and val[i] > 0:
                va_diff = vah[i] - val[i]
                va_w = va_diff / val[i] if val[i] > 0 else 0
                
                # 1. 単純飛び乗り下抜け (フィルターなし / 成行)
                if c[i-1] >= val[i-1] and ci < val[i]:
                    entry = ci
                    sl = min(entry * 1.030, poc[i] if not np.isnan(poc[i]) else entry * 1.015)
                    risk = max(entry * 0.005, sl - entry)
                    tp = entry - risk * 2.0
                    
                    pnl = 0.0
                    for k in range(i+1, i+49):
                        if l[k] <= tp:
                            pnl = 15.0 * (entry - tp) / entry - 15.0 * (taker_fee * 2)
                            break
                        elif h[k] >= sl or (k == i+48 and c[k] > entry):
                            exit_p = sl if h[k] >= sl else c[k]
                            pnl = 15.0 * (entry - exit_p) / entry - 15.0 * (taker_fee * 2)
                            break
                    else:
                        exit_p = c[i+48]
                        pnl = 15.0 * (entry - exit_p) / entry - 15.0 * (taker_fee * 2)
                    hourly_raw_trades[hr].append(pnl)
                    
                # 2. 本番仕様 (過伸展防止 not_overextended + va_w >= 0.015 + Maker指値約定)
                # 条件: 前足がVAL以上、当足がVAL下抜け、かつ下抜けすぎない (val - 0.35*va_diff以上)
                if c[i-1] >= val[i-1] and ci < val[i]:
                    not_over = (ci >= val[i] - va_diff * 0.35)
                    if not_over and (va_w >= 0.015):
                        # Maker指値: 戻りを待つ (ci * 1.0002)
                        maker_limit = ci * 1.0002
                        # 翌足で高値が指値にタッチして約定したか
                        next_h = h[i+1] if i+1 < n else 0
                        if next_h >= maker_limit:
                            entry = maker_limit
                            raw_sl = val[i] + va_diff * 0.35
                            poc_cl = poc[i] * 0.999 if (not np.isnan(poc[i]) and poc[i] > entry) else raw_sl
                            sl_dist = max(entry * 0.004, min(raw_sl, poc_cl) - entry)
                            sl = entry + sl_dist
                            tp = entry - sl_dist * 2.0
                            
                            pnl = 0.0
                            for k in range(i+2, min(i+50, n)):
                                if l[k] <= tp:
                                    # 利確 (Taker 0.05% + エントリー Maker 0.02%)
                                    pnl = 15.0 * (entry - tp) / entry - 15.0 * (taker_fee + maker_fee)
                                    break
                                elif h[k] >= sl or (k == i+49 and c[k] > entry):
                                    exit_p = sl if h[k] >= sl else c[k]
                                    pnl = 15.0 * (entry - exit_p) / entry - 15.0 * (taker_fee + maker_fee)
                                    break
                            else:
                                exit_p = c[min(i+49, n-1)]
                                pnl = 15.0 * (entry - exit_p) / entry - 15.0 * (taker_fee + maker_fee)
                            hourly_maker_trades[hr].append(pnl)

    print("\n" + "="*95)
    print("📊 【地合いSELL時】時間帯別 (0〜23時 JST) のショートブレイク成績 ($15単発・RR 1:2)")
    print("="*95)
    print(f"{'JST時間':<8} | {'成行飛び乗り':<8} | {'勝率':<6} | {'PF':<5} | {'純利益':<10} | {'本番仕様Maker':<8} | {'勝率':<6} | {'PF':<5} | {'純利益':<10}")
    print("-" * 88)
    
    raw_pnl_by_hour = []
    maker_pnl_by_hour = []
    
    for hr in range(24):
        b_trs = hourly_raw_trades[hr]
        b_cnt = len(b_trs)
        b_wins = [x for x in b_trs if x > 0]
        b_losses = [x for x in b_trs if x <= 0]
        b_wr = (len(b_wins) / b_cnt * 100) if b_cnt > 0 else 0
        b_pf = (sum(b_wins) / abs(sum(b_losses))) if b_losses and abs(sum(b_losses)) > 0 else 0
        b_tot = sum(b_trs)
        raw_pnl_by_hour.append(b_tot)
        
        m_trs = hourly_maker_trades[hr]
        m_cnt = len(m_trs)
        m_wins = [x for x in m_trs if x > 0]
        m_losses = [x for x in m_trs if x <= 0]
        m_wr = (len(m_wins) / m_cnt * 100) if m_cnt > 0 else 0
        m_pf = (sum(m_wins) / abs(sum(m_losses))) if m_losses and abs(sum(m_losses)) > 0 else 0
        m_tot = sum(m_trs)
        maker_pnl_by_hour.append(m_tot)
        
        mark = "🔥" if hr in [22, 23, 0, 1] else ("🏰" if 16 <= hr < 22 else ("🗼" if 9 <= hr < 16 else "🌙"))
        print(f"{hr:02d}:00 {mark:<2} | {b_cnt:>6}回 | {b_wr:>5.1f}% | {b_pf:>4.2f} | {b_tot:>+8.2f}$ | {m_cnt:>6}回 | {m_wr:>5.1f}% | {m_pf:>4.2f} | {m_tot:>+8.2f}$")

    # セッション別サマリー
    print("\n" + "="*95)
    print("🏆 【地合いSELL時】セッション別 ショートブレイク総合損益")
    print("="*95)
    sessions = {
        "🗼 東京 (09-16h)": range(9, 16),
        "🏰 欧州 (16-22h)": range(16, 22),
        "🗽 米国 (22-02h)": [22, 23, 0, 1],
        "🌙 深夜未明 (02-09h)": [2, 3, 4, 5, 6, 7, 8]
    }
    for s_name, hrs in sessions.items():
        b_s = sum([sum(hourly_raw_trades[h]) for h in hrs])
        m_s = sum([sum(hourly_maker_trades[h]) for h in hrs])
        b_cnt = sum([len(hourly_raw_trades[h]) for h in hrs])
        m_cnt = sum([len(hourly_maker_trades[h]) for h in hrs])
        print(f"  {s_name:<20} | 飛び乗り: {b_cnt:>5}回 / {b_s:>+8.2f} USDT | 本番仕様Maker: {m_cnt:>4}回 / {m_s:>+8.2f} USDT")

    # グラフ保存
    plt.figure(figsize=(14, 7))
    x = np.arange(24)
    plt.bar(x - 0.2, raw_pnl_by_hour, width=0.4, label="成行飛び乗りショート純利益", color="#e74c3c", alpha=0.6)
    plt.bar(x + 0.2, maker_pnl_by_hour, width=0.4, label="本番仕様Maker指値ショート純利益", color="#2980b9", alpha=0.85)
    plt.axhline(0, color="black", linestyle="--", linewidth=1.0)
    plt.axvspan(9, 16, color="#2ecc71", alpha=0.1, label="東京 (09-16)")
    plt.axvspan(16, 22, color="#9b59b6", alpha=0.1, label="欧州 (16-22)")
    plt.axvspan(22, 24, color="#e67e22", alpha=0.15, label="米国 (22-02)")
    plt.axvspan(0, 2, color="#e67e22", alpha=0.15)
    plt.xticks(x, [f"{h:02d}" for h in x])
    plt.ylabel("純利益 (USDT)")
    plt.xlabel("JST時刻")
    plt.title("【地合いSELL時】時間帯別ショートブレイク (VAL_BREAK) 純利益 (2024〜2026年)", fontsize=13, fontweight="bold")
    plt.legend(loc="upper left")
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("Data/sell_regime_breakout_hourly.png", dpi=150)
    print("\n📈 時間帯別ショート純利益チャート保存: Data/sell_regime_breakout_hourly.png")

if __name__ == "__main__":
    main()
