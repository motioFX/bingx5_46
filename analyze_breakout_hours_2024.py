import sys
import glob
sys.stdout.reconfigure(encoding='utf-8')
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

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

def main():
    print("="*80)
    print("🔍 2024年以降のアメリカ時間 vs 欧州時間 ブレイクアウト & 上昇特性の徹底調査")
    print("="*80)
    
    files = glob.glob("Data/binance_candles_5m_4y/*.pkl")
    # 除外BTC
    alt_files = [f for f in files if "BTCUSDT" not in os.path.basename(f)]
    
    # 時間帯別統計用 (0〜23時 JST)
    hours_surge_count = np.zeros(24)       # +2%以上上昇足の発生回数
    hours_surge_mag = [[] for _ in range(24)] # 上昇率
    
    # VAHブレイクアウト統計
    hours_breakout_count = np.zeros(24)    # VAH上抜け発生回数
    hours_breakout_continuation = np.zeros(24) # その後さらに+1.5%以上上昇した「本物」の回数
    hours_breakout_fakeout = np.zeros(24)      # その後レンジ内に沈んだ「ダマシ」の回数
    
    # セッション別シミュレーション (1トレード $15単発、RR 1:2)
    # セッション区分:
    # 🗼 東京: 09:00 - 16:00 JST
    # 🏰 欧州: 16:00 - 22:00 JST
    # 🗽 米国: 22:00 - 06:00 JST (NY市場オープン 22:30 JST〜)
    # 🌅 その他/朝: 06:00 - 09:00 JST
    session_trades = {"TOKYO": [], "EUROPE": [], "US": [], "EARLY_MORNING": []}
    
    for f in alt_files:
        sym = os.path.basename(f).replace(".pkl", "")
        df = pd.read_pickle(f)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        # 2024年以降にフィルタ
        df = df[df["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
        if len(df) < 500:
            continue
            
        # JST時間列の作成
        jst_time = df["timestamp"].dt.tz_convert("Asia/Tokyo")
        hours = jst_time.dt.hour.values
        
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        v = df["volume"].values
        n = len(df)
        
        # 1時間(12本)リターンの計算
        ret_1h = np.full(n, 0.0)
        ret_1h[12:] = (c[12:] - c[:-12]) / c[:-12]
        
        for i in range(12, n):
            hr = hours[i]
            # 1時間で +2%以上の上昇
            if ret_1h[i] >= 0.02:
                hours_surge_count[hr] += 1
                hours_surge_mag[hr].append(ret_1h[i])
                
        # VP計算 (288本 = 24時間ローリング)
        val, vah, poc = calculate_vp_fast(c, h, l, v, period=288, num_bins=30)
        
        # ブレイクアウトの追跡
        # 条件: 前足が VAH以下、当足の終値が VAHを上抜けた瞬間
        for i in range(289, n - 24):
            if c[i-1] <= vah[i-1] and c[i] > vah[i]:
                hr = hours[i]
                hours_breakout_count[hr] += 1
                
                # その後24本(2時間)の最高値と最安値
                sub_future_h = np.max(h[i+1:i+25])
                sub_future_l = np.min(l[i+1:i+25])
                
                # 追随度: +1.5%以上伸びたか
                if sub_future_h >= c[i] * 1.015:
                    hours_breakout_continuation[hr] += 1
                # ダマシ度: VAHの下、あるいは -1%以下に落ちたか
                if sub_future_l <= c[i] * 0.990:
                    hours_breakout_fakeout[hr] += 1
                    
                # セッション別簡易トレードシミュレーション
                # エントリー: c[i], SL: max(c[i]*0.97, c[i] - max(c[i]*0.004, c[i]-val[i])), TP: c[i] + risk*2
                entry_px = c[i]
                sl_px = max(entry_px * 0.97, val[i] if not np.isnan(val[i]) else entry_px * 0.985)
                risk_dist = max(entry_px * 0.005, entry_px - sl_px)
                tp_px = entry_px + risk_dist * 2.0
                
                # 決済シミュレーション (最大48本 = 4時間)
                pnl = 0.0
                qty = 15.0 / entry_px
                cost = 15.0
                for k in range(i+1, min(i+49, n)):
                    if h[k] >= tp_px:
                        sell_val = qty * tp_px
                        fee = (cost + sell_val) * 0.0005
                        pnl = sell_val - cost - fee
                        break
                    elif l[k] <= sl_px or (k == i+48 and c[k] < entry_px):
                        exit_p = sl_px if l[k] <= sl_px else c[k]
                        sell_val = qty * exit_p
                        fee = (cost + sell_val) * 0.0005
                        pnl = sell_val - cost - fee
                        break
                else:
                    sell_val = qty * c[min(i+48, n-1)]
                    fee = (cost + sell_val) * 0.0005
                    pnl = sell_val - cost - fee
                    
                sess = "EARLY_MORNING"
                if 9 <= hr < 16:
                    sess = "TOKYO"
                elif 16 <= hr < 22:
                    sess = "EUROPE"
                elif hr >= 22 or hr < 6:
                    sess = "US"
                    
                session_trades[sess].append(pnl)

    print("\n" + "="*80)
    print("📊 【時間帯別 (0〜23時 JST) の上昇・ブレイクアウト発生分布】")
    print("="*80)
    print(f"{'JST時間':<8} | {'急上昇(+2%以上)':<16} | {'VAHブレイク数':<14} | {'追随上昇(+1.5%高)':<16} | {'ブレイク成功率':<12}")
    print("-" * 75)
    
    success_rates = []
    for hr in range(24):
        cnt = hours_breakout_count[hr]
        cont = hours_breakout_continuation[hr]
        rate = (cont / cnt * 100) if cnt > 0 else 0
        success_rates.append(rate)
        mark = "🔥" if hr in [22, 23, 0, 1, 2, 3] else ("🏰" if 16 <= hr < 22 else ("🗼" if 9 <= hr < 16 else "🌙"))
        print(f"{hr:02d}:00 {mark:<2} | {int(hours_surge_count[hr]):>6} 回           | {int(cnt):>6} 回       | {int(cont):>6} 回          | {rate:>5.1f} %")
        
    print("\n" + "="*80)
    print("🗽 セッション別 ブレイクアウト（順張りロング）の実トレード成績 ($15単発)")
    print("="*80)
    for sess, trs in session_trades.items():
        if len(trs) == 0:
            continue
        tot_pnl = sum(trs)
        wins = [x for x in trs if x > 0]
        losses = [x for x in trs if x <= 0]
        win_rate = len(wins) / len(trs) * 100
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else np.nan
        sess_name = {
            "TOKYO": "🗼 東京時間 (09-16h JST)",
            "EUROPE": "🏰 欧州時間 (16-22h JST)",
            "US": "🗽 アメリカ時間 (22-06h JST)",
            "EARLY_MORNING": "🌅 早朝時間 (06-09h JST)"
        }[sess]
        print(f"  {sess_name:<28} | 回数: {len(trs):>5} 回 | 勝率: {win_rate:>5.1f}% | PF: {pf:>4.2f} | 純利益: {tot_pnl:>+8.2f} USDT")

    # グラフ描画
    plt.figure(figsize=(14, 8))
    
    # 1. 時間帯別 VAHブレイク数と急上昇数
    ax1 = plt.subplot(2, 1, 1)
    bar_width = 0.35
    x = np.arange(24)
    ax1.bar(x - bar_width/2, hours_surge_count, bar_width, label="Surge Bar (+2% in 1h)", color="#ff9900", alpha=0.8)
    ax1.bar(x + bar_width/2, hours_breakout_count, bar_width, label="VAH Breakout Count", color="#2980b9", alpha=0.8)
    ax1.axvspan(9, 16, color="#2ecc71", alpha=0.15, label="Tokyo (09-16 JST)")
    ax1.axvspan(16, 22, color="#9b59b6", alpha=0.15, label="Europe (16-22 JST)")
    ax1.axvspan(22, 24, color="#e74c3c", alpha=0.15, label="US Market (22-06 JST)")
    ax1.axvspan(0, 6, color="#e74c3c", alpha=0.15)
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"{h:02d}" for h in x])
    ax1.set_ylabel("Event Count")
    ax1.set_title("2024-2026 Crypto Surge & VAH Breakout Frequency by JST Hour", fontsize=13, fontweight="bold")
    ax1.legend(loc="upper left")
    ax1.grid(True, linestyle="--", alpha=0.5)
    
    # 2. ブレイクアウト成功率 (追随して伸びた確率)
    ax2 = plt.subplot(2, 1, 2)
    ax2.plot(x, success_rates, marker="o", color="#27ae60", linewidth=2.5, markersize=7, label="Breakout Continuation Rate (%)")
    ax2.axvspan(9, 16, color="#2ecc71", alpha=0.15)
    ax2.axvspan(16, 22, color="#9b59b6", alpha=0.15)
    ax2.axvspan(22, 24, color="#e74c3c", alpha=0.15)
    ax2.axvspan(0, 6, color="#e74c3c", alpha=0.15)
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"{h:02d}" for h in x])
    ax2.set_ylabel("Success Rate (%)")
    ax2.set_xlabel("Hour of Day (JST)")
    ax2.set_title("Breakout Continuation Rate by JST Hour (Did it continue surging?)", fontsize=13, fontweight="bold")
    ax2.legend(loc="upper left")
    ax2.grid(True, linestyle="--", alpha=0.5)
    
    plt.tight_layout()
    plt.savefig("Data/breakout_hours_analysis.png", dpi=150)
    print("\n📈 分析チャート保存完了: Data/breakout_hours_analysis.png")

if __name__ == "__main__":
    main()
