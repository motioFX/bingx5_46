#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市場全体の全面安フィルター 可視化チャート生成スクリプト
1. 歴史的大暴落 4事例のクローズアップ検証チャート (BTC + 主要アルト + フィルター区間)
2. 全期間 (2022〜2026年) のマクロ俯瞰チャート
"""
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
import shutil

plt.rcParams["font.sans-serif"] = ["Meiryo", "Yu Gothic", "Hiragino Maru Gothic Pro", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

DATA_PATH = Path(__file__).resolve().parent / "Data" / "binance_candles_1h" / "binance_all_crypto_1h.csv"
ARTIFACT_DIR = Path(r"C:\Users\user\.gemini\antigravity\brain\ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

OUT_DIR = Path(__file__).resolve().parent / "Data" / "plots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

print("Loading data...")
df_all = pd.read_csv(DATA_PATH)
df_all["timestamp"] = pd.to_datetime(df_all["timestamp"])
df_all = df_all[df_all["timestamp"] >= "2022-01-01"].copy()
df_all.sort_values(by=["symbol", "timestamp"], inplace=True)

pivot_close = df_all.pivot(index="timestamp", columns="symbol", values="close")
pivot_low = df_all.pivot(index="timestamp", columns="symbol", values="low")
pivot_open = df_all.pivot(index="timestamp", columns="symbol", values="open")

timestamps = pivot_close.index
n_bars = len(timestamps)
alt_symbols = [col for col in pivot_close.columns if col != "BTCUSDT"]

# シミュレーション実行して状態系列を取得
print("Running simulation engine...")
from simulate_dual_market_dump_filter import run_improved_simulation
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

# 4時間ローリングリターン & アルト下落比率
ret_4h = pivot_close.pct_change(4) * 100.0
alt_ret_mat = ret_4h[alt_symbols].values
valid_alt_counts = np.sum(~np.isnan(alt_ret_mat), axis=1)
down_alt_counts = np.sum(alt_ret_mat < 0.0, axis=1)
alt_down_ratios = np.where(valid_alt_counts > 0, down_alt_counts / valid_alt_counts, 0.0)

series_states = pd.Series(states, index=timestamps)
series_down_ratio = pd.Series(alt_down_ratios * 100.0, index=timestamps)

# ==============================================================================
# チャート 1: 代表的な大暴落 4事例のクローズアップ検証チャート
# ==============================================================================
print("Generating Zoom-in case study chart (4 major crashes)...")

zoom_cases = [
    {
        "title": "Case 1: 2022年5月 Lunaショック暴落 (アルト先行検知)",
        "start": "2022-05-11 00:00",
        "end": "2022-05-14 00:00",
        "alts": ["SOLUSDT", "NEARUSDT", "AVAXUSDT"],
    },
    {
        "title": "Case 2: 2024年4月中東情勢急落 (アルト先行検知)",
        "start": "2024-04-12 00:00",
        "end": "2024-04-15 00:00",
        "alts": ["SOLUSDT", "DOGEUSDT", "ARBUSDT"],
    },
    {
        "title": "Case 3: 2025年2月急落相場 (アルト先行検知 ➔ 大反発)",
        "start": "2025-02-01 12:00",
        "end": "2025-02-05 00:00",
        "alts": ["SOLUSDT", "SUIUSDT", "NEARUSDT"],
    },
    {
        "title": "Case 4: 2026年2月直近急落相場 (BTC急落連動型)",
        "start": "2026-02-04 12:00",
        "end": "2026-02-08 00:00",
        "alts": ["SOLUSDT", "DOGEUSDT", "NEARUSDT"],
    },
]

fig, axes = plt.subplots(4, 1, figsize=(16, 20), facecolor="#131722")
fig.suptitle(
    "【市場全体 全面安フィルター】 代表的大暴落 4事例の動作検証チャート\n(赤色帯 = 全銘柄ロング禁止ロック中 | 緑矢印 = 底打ち確認・ロング再開)",
    fontsize=16, fontweight="bold", color="#f0f3f6", y=0.99
)

colors = ["#2962ff", "#ff9800", "#e91e63", "#00bcd4"]

for ax_idx, case in enumerate(zoom_cases):
    ax = axes[ax_idx]
    ax.set_facecolor("#1e222d")
    
    t_start = pd.to_datetime(case["start"]).tz_localize("UTC")
    t_end = pd.to_datetime(case["end"]).tz_localize("UTC")
    
    # 対象期間スライス
    mask = (timestamps >= t_start) & (timestamps <= t_end)
    sub_ts = timestamps[mask]
    if len(sub_ts) == 0:
        continue

    # ノーマライズ価格 (開始時を100として比較)
    btc_sub = pivot_close.loc[mask, "BTCUSDT"]
    btc_norm = btc_sub / btc_sub.iloc[0] * 100.0
    
    # 軸描画
    ax.plot(sub_ts, btc_norm, label="BTCUSDT (基準)", color="#f7931a", linewidth=2.5, zorder=5)
    
    for a_idx, a_sym in enumerate(case["alts"]):
        if a_sym in pivot_close.columns:
            a_sub = pivot_close.loc[mask, a_sym]
            if not a_sub.dropna().empty and a_sub.iloc[0] > 0:
                a_norm = a_sub / a_sub.iloc[0] * 100.0
                ax.plot(sub_ts, a_norm, label=a_sym, color=colors[a_idx % len(colors)], linewidth=1.5, alpha=0.85)

    # ロック区間のハイライト (state > 0)
    sub_states = series_states.loc[mask]
    
    # 連続するロック区間を塗りつぶし
    in_lock = False
    lock_start = None
    
    for t in sub_ts:
        st = sub_states.loc[t]
        if st > 0 and not in_lock:
            in_lock = True
            lock_start = t
        elif st == 0 and in_lock:
            in_lock = False
            ax.axvspan(lock_start, t, color="#f23645", alpha=0.25, label="全面安ロック (ロング禁止)" if ax_idx==0 and lock_start==sub_ts[0] else None)
            ax.axvline(t, color="#089981", linestyle="--", linewidth=2.0)
            ax.annotate("🟢 解除\n(再開)", xy=(t, ax.get_ylim()[0]*1.02 if ax.get_ylim()[0]>0 else 80),
                        xytext=(0, 25), textcoords="offset points",
                        fontsize=9, fontweight="bold", color="#089981",
                        arrowprops=dict(arrowstyle="->", color="#089981", lw=1.5))
            
    if in_lock:
        ax.axvspan(lock_start, sub_ts[-1], color="#f23645", alpha=0.25)

    ax.set_title(case["title"], fontsize=12, fontweight="bold", color="#d1d4dc", loc="left", pad=8)
    ax.set_ylabel("相対騰落 (開始=100)", fontsize=10, color="#787b86")
    ax.tick_params(colors="#787b86")
    ax.grid(True, color="#2a2e39", linestyle="--", alpha=0.6)
    ax.legend(loc="upper right", facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="#d1d4dc", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))

plt.tight_layout(rect=[0, 0.02, 1, 0.98])
zoom_chart_path = OUT_DIR / "macro_dump_filter_zoom_cases.png"
fig.savefig(zoom_chart_path, dpi=130)
plt.close(fig)
print(f"Saved: {zoom_chart_path}")

# ==============================================================================
# チャート 2: 全期間 (2022〜2026年) のマクロ全体俯瞰チャート
# ==============================================================================
print("Generating Overall Macro View chart (2022~2026)...")

fig2, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(18, 12), sharex=True, gridspec_kw={"height_ratios": [2.5, 1.2, 1.0]}, facecolor="#131722")
fig2.suptitle("【市場全体 全面安フィルター】 4.7年間 (2022〜2026年) 全体シミュレーション俯瞰", fontsize=16, fontweight="bold", color="#f0f3f6", y=0.98)

# 1. BTC 価格推移 + ロック背景
ax1.set_facecolor("#1e222d")
btc_prices = pivot_close["BTCUSDT"]
ax1.plot(timestamps, btc_prices, color="#f7931a", linewidth=1.5, label="BTC/USDT 価格")
ax1.set_yscale("log")
ax1.set_ylabel("BTC 価格 (USDT, log)", fontsize=11, color="#787b86")
ax1.tick_params(colors="#787b86")
ax1.grid(True, color="#2a2e39", linestyle="--", alpha=0.5)

# ロック区間を塗りつぶし
lock_mask = states > 0
ax1.fill_between(timestamps, btc_prices.min(), btc_prices.max(), where=lock_mask, color="#f23645", alpha=0.25, label="全面安ロック中 (ロング禁止)")
ax1.legend(loc="upper left", facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="#d1d4dc", fontsize=10)

# 2. アルト下落比率 (Breadth)
ax2.set_facecolor("#1e222d")
ax2.plot(timestamps, series_down_ratio, color="#2962ff", linewidth=0.8, alpha=0.7, label="アルト下落比率 (直近4hマイナス銘柄割合)")
ax2.axhline(80, color="#f23645", linestyle="--", linewidth=1.5, label="全面安閾値 (80%)")
ax2.axhline(50, color="#089981", linestyle="--", linewidth=1.5, label="回復閾値 (50%)")
ax2.set_ylabel("下落比率 (%)", fontsize=11, color="#787b86")
ax2.tick_params(colors="#787b86")
ax2.grid(True, color="#2a2e39", linestyle="--", alpha=0.5)
ax2.legend(loc="upper left", facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="#d1d4dc", fontsize=9)

# 3. 状態遷移マシン
ax3.set_facecolor("#1e222d")
ax3.step(timestamps, states, color="#ff9800", linewidth=1.2, where="post", label="フィルター状態 (0:通常, 1:全面安ロック, 2:BTC底打ち待ち)")
ax3.set_yticks([0, 1, 2])
ax3.set_yticklabels(["0:通常(許可)", "1:全面安ロック", "2:アルト回復待ち"], fontsize=9)
ax3.set_ylabel("ステート", fontsize=11, color="#787b86")
ax3.tick_params(colors="#787b86")
ax3.grid(True, color="#2a2e39", linestyle="--", alpha=0.5)
ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

plt.tight_layout(rect=[0, 0.02, 1, 0.96])
overall_chart_path = OUT_DIR / "macro_dump_filter_overall_overview.png"
fig2.savefig(overall_chart_path, dpi=130)
plt.close(fig2)
print(f"Saved: {overall_chart_path}")

# アーティファクトディレクトリへコピー
shutil.copy2(zoom_chart_path, ARTIFACT_DIR / zoom_chart_path.name)
shutil.copy2(overall_chart_path, ARTIFACT_DIR / overall_chart_path.name)
print(f"Copied charts to Artifacts dir: {ARTIFACT_DIR}")

# Discord (#test4_backtest) へも送信可能かチェック
try:
    from config_loader import get_webhook_url
    import requests
    wh = get_webhook_url("test4_backtest")
    if wh:
        print("Sending charts to Discord (#test4_backtest)...")
        with open(zoom_chart_path, "rb") as f1:
            requests.post(wh, data={"content": "📊 **【市場全体 全面安フィルター】 大暴落 4事例 クローズアップ検証チャート**"}, files={"file": f1}, timeout=30)
        with open(overall_chart_path, "rb") as f2:
            requests.post(wh, data={"content": "📊 **【市場全体 全面安フィルター】 4.7年間 (2022〜2026年) 全体俯瞰チャート**"}, files={"file": f2}, timeout=30)
        print("Successfully sent charts to Discord!")
except Exception as e:
    print(f"Discord send error: {e}")
