#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市場全体の全面安フィルター (デュアル・トリガー ＆ 着実な底打ち解除)
改善版シミュレーションスクリプト (2022年〜2026年 Binance 1時間足データ)
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_PATH = Path(__file__).resolve().parent / "Data" / "binance_candles_1h" / "binance_all_crypto_1h.csv"
print(f"Loading {DATA_PATH}...")
df_all = pd.read_csv(DATA_PATH)
df_all["timestamp"] = pd.to_datetime(df_all["timestamp"])
df_all = df_all[df_all["timestamp"] >= "2022-01-01"].copy()
df_all.sort_values(by=["symbol", "timestamp"], inplace=True)

pivot_close = df_all.pivot(index="timestamp", columns="symbol", values="close")
pivot_low = df_all.pivot(index="timestamp", columns="symbol", values="low")
pivot_high = df_all.pivot(index="timestamp", columns="symbol", values="high")
pivot_open = df_all.pivot(index="timestamp", columns="symbol", values="open")

timestamps = pivot_close.index
n_bars = len(timestamps)
alt_symbols = [col for col in pivot_close.columns if col != "BTCUSDT"]


def run_improved_simulation(
    alt_lead_down_ratio: float = 0.80,   # アルト先行: 下落比率 (80%)
    alt_lead_drop_pct: float = -2.5,     # アルト先行: アルト平均下落率 (-2.5%)
    btc_dump_drop_pct: float = -2.5,     # BTC主導: BTC下落率 (-2.5%)
    btc_dump_down_ratio: float = 0.70,   # BTC主導: アルト下落比率 (70%)
    alt_recovery_ratio: float = 0.50,    # アルト回復率: 底値から反発した銘柄の割合 (50%)
    alt_rebound_pct: float = 1.5,        # アルト回復: 底値からの反発幅 (+1.5%)
    min_lock_hours: int = 4,             # 最低ロック時間 (4時間: 早漏解除防止)
    btc_stable_hours: int = 3,           # BTC底打ち確認期間 (3時間連続で安値未更新)
):
    states = np.zeros(n_bars, dtype=int)
    
    # 4時間ローリングリターン (%)
    ret_4h = pivot_close.pct_change(4) * 100.0
    btc_ret_4h = ret_4h["BTCUSDT"].values
    
    alt_ret_mat = ret_4h[alt_symbols].values
    valid_alt_counts = np.sum(~np.isnan(alt_ret_mat), axis=1)
    down_alt_counts = np.sum(alt_ret_mat < 0.0, axis=1)
    alt_down_ratios = np.where(valid_alt_counts > 0, down_alt_counts / valid_alt_counts, 0.0)
    alt_mean_rets = np.nanmean(alt_ret_mat, axis=1)

    btc_close_arr = pivot_close["BTCUSDT"].values
    btc_low_arr = pivot_low["BTCUSDT"].values
    btc_open_arr = pivot_open["BTCUSDT"].values
    alt_close_mat = pivot_close[alt_symbols].values
    alt_low_mat = pivot_low[alt_symbols].values

    current_state = 0
    dump_events = []
    
    current_dump_start = None
    current_dump_type = None
    btc_lowest_in_dump = float("inf")
    btc_lowest_bar_idx = 0
    alt_lowest_in_dump = None

    for i in range(4, n_bars):
        c_state = current_state
        
        # 1. 突入判定 (NORMAL -> DUMP_LOCKED)
        if c_state == 0:
            is_alt_lead = (alt_down_ratios[i] >= alt_lead_down_ratio) and (alt_mean_rets[i] <= alt_lead_drop_pct)
            is_btc_lead = (btc_ret_4h[i] <= btc_dump_drop_pct) and (alt_down_ratios[i] >= btc_dump_down_ratio)
            
            if is_alt_lead or is_btc_lead:
                c_state = 1
                current_dump_start = i
                current_dump_type = "ALT_LEAD" if (is_alt_lead and not is_btc_lead) else ("BTC_LEAD" if (is_btc_lead and not is_alt_lead) else "BOTH")
                btc_lowest_in_dump = btc_low_arr[i]
                btc_lowest_bar_idx = i
                alt_lowest_in_dump = alt_low_mat[i].copy()
        
        # 2. ロック中の処理 (DUMP_LOCKED: 1 または BTC_STOPPED: 2)
        elif c_state in (1, 2):
            lock_duration = i - current_dump_start

            # 最安値更新チェック
            if btc_low_arr[i] < btc_lowest_in_dump:
                btc_lowest_in_dump = btc_low_arr[i]
                btc_lowest_bar_idx = i
                # 新安値更新時は必ず下落中(state=1)に戻る
                c_state = 1
            
            if alt_lowest_in_dump is not None:
                alt_lowest_in_dump = np.fmin(alt_lowest_in_dump, alt_low_mat[i])

            # 最低ロック時間 (min_lock_hours) を経過するまでは解除判定に進まない
            if lock_duration >= min_lock_hours:
                # 状態 1 -> 状態 2 (BTC下落停止・底打ち確認)
                if c_state == 1:
                    # BTCが直近 btc_stable_hours 時間で最安値を更新していないこと
                    hours_since_lowest = i - btc_lowest_bar_idx
                    btc_rebound = (btc_close_arr[i] - btc_lowest_in_dump) / btc_lowest_in_dump * 100.0 if btc_lowest_in_dump > 0 else 0
                    is_btc_green = btc_close_arr[i] > btc_open_arr[i]

                    if hours_since_lowest >= btc_stable_hours and (is_btc_green or btc_rebound >= 0.8):
                        c_state = 2

                # 状態 2 -> 状態 0 (アルト過半数回復・ロック解除)
                if c_state == 2:
                    if alt_lowest_in_dump is not None:
                        valid_alts = ~np.isnan(alt_lowest_in_dump) & (alt_lowest_in_dump > 0)
                        rebound_pcts = (alt_close_mat[i][valid_alts] - alt_lowest_in_dump[valid_alts]) / alt_lowest_in_dump[valid_alts] * 100.0
                        rebounded_count = np.sum(rebound_pcts >= alt_rebound_pct)
                        rebound_ratio = rebounded_count / len(rebound_pcts) if len(rebound_pcts) > 0 else 0.0

                        # 解除条件: アルトのN%以上が底値から反発
                        if rebound_ratio >= alt_recovery_ratio:
                            c_state = 0
                            
                            start_idx = current_dump_start
                            btc_total_drop = (btc_lowest_in_dump - btc_close_arr[start_idx]) / btc_close_arr[start_idx] * 100.0
                            
                            # ロック期間中のアルト最大下落幅 (回避できたダメージ)
                            alt_drops = (alt_lowest_in_dump[valid_alts] - alt_close_mat[start_idx][valid_alts]) / alt_close_mat[start_idx][valid_alts] * 100.0
                            alt_max_drop = np.nanmean(alt_drops)

                            # 解除後 12時間および24時間のリターン (リバウンドを捉えられたか)
                            end_12h = min(n_bars - 1, i + 12)
                            post_12h_alt = np.nanmean((alt_close_mat[end_12h] - alt_close_mat[i]) / alt_close_mat[i] * 100.0)
                            end_24h = min(n_bars - 1, i + 24)
                            post_24h_alt = np.nanmean((alt_close_mat[end_24h] - alt_close_mat[i]) / alt_close_mat[i] * 100.0)

                            dump_events.append({
                                "start_ts": timestamps[start_idx],
                                "end_ts": timestamps[i],
                                "duration_hours": lock_duration,
                                "trigger_type": current_dump_type,
                                "btc_drop": btc_total_drop,
                                "alt_drop": alt_max_drop,
                                "post_12h_alt": post_12h_alt,
                                "post_24h_alt": post_24h_alt,
                            })
                            current_dump_start = None
                            alt_lowest_in_dump = None

        current_state = c_state
        states[i] = current_state

    return states, pd.DataFrame(dump_events)


print("\n================================================================================")
print("🔍 【市場全体 全面安フィルター（着実な底打ち解除版） シミュレーション結果】")
print("================================================================================")

states, df_events = run_improved_simulation(
    alt_lead_down_ratio=0.80,
    alt_lead_drop_pct=-2.5,
    btc_dump_drop_pct=-2.5,
    btc_dump_down_ratio=0.70,
    alt_recovery_ratio=0.50,
    alt_rebound_pct=1.5,
    min_lock_hours=4,
    btc_stable_hours=3
)

total_hours = len(states)
locked_hours = np.sum(states > 0)
lock_pct = (locked_hours / total_hours) * 100.0

print(f"■ 全観測時間: {total_hours:,} 時間 (約4.7年間 / 2022〜2026年)")
print(f"■ 全面安ロック発動回数: {len(df_events):,} 回 (年間 約100回 / 月8〜9回)")
print(f"■ ロック総時間: {locked_hours:,} 時間 (全体の {lock_pct:.1f}% 停止)")
print(f"■ 1回あたりの平均ロック継続時間: {df_events['duration_hours'].mean():.1f} 時間 (中央値: {df_events['duration_hours'].median():.0f} 時間)")

# トリガー内訳
type_counts = df_events["trigger_type"].value_counts()
print(f"\n▼ 【発動トリガー内訳】")
for t_name, cnt in type_counts.items():
    print(f"  • {t_name:10s}: {cnt:3d} 回 ({cnt/len(df_events)*100:4.1f}%)")

print(f"\n▼ 【ロック中の回避実績 ＆ 解除後のリターン】")
print(f"■ ロック中のアルト平均最大追加下落 (回避したドローダウン): 平均 {df_events['alt_drop'].mean():.2f}% (BTC平均 {df_events['btc_drop'].mean():.2f}%)")
print(f"■ ロック解除後12時間のアルト平均リターン: 平均 {df_events['post_12h_alt'].mean():+.2f}% (勝率: {(df_events['post_12h_alt'] > 0).mean()*100:.1f}%)")
print(f"■ ロック解除後24時間のアルト平均リターン: 平均 {df_events['post_24h_alt'].mean():+.2f}% (勝率: {(df_events['post_24h_alt'] > 0).mean()*100:.1f}%)")

print("\n▼ 【歴史的大暴落におけるフィルター発動ログ（主要ワースト10）】")
print(f"{'発動日時 (UTC)':16s} | {'解除日時 (UTC)':16s} | {'停止時間':8s} | {'トリガー':10s} | {'回避アルト下落':14s} | {'解除後12h':10s} | {'解除後24h'}")
print("-" * 100)

sample_events = df_events.sort_values(by="alt_drop").head(10)
for _, ev in sample_events.iterrows():
    s_ts = ev["start_ts"].strftime("%Y-%m-%d %H:%M")
    e_ts = ev["end_ts"].strftime("%Y-%m-%d %H:%M")
    dur = f"{int(ev['duration_hours'])}時間"
    ttype = ev["trigger_type"]
    a_drop = f"{ev['alt_drop']:+.2f}%"
    p12 = f"{ev['post_12h_alt']:+.2f}%"
    p24 = f"{ev['post_24h_alt']:+.2f}%"
    print(f"{s_ts:16s} | {e_ts:16s} | {dur:>8s} | {ttype:10s} | {a_drop:>14s} | {p12:>10s} | {p24:>10s}")

print("=" * 100)
