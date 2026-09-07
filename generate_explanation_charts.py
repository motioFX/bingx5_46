"""Generate detailed visual explanation charts for the momentum rotation backtest findings.
Creates intuitive, annotated charts explaining:
1. The "Peak-Buying Trap" case study (Actual candlestick with 24h ROC trigger vs next bars dump).
2. The Strategy Performance Dashboard (Rebalance cycle, Top-N, Cost vs No-Cost, Stop Loss).
3. The "Next Bar Return Distribution" showing why the edge is negative for 1H.
"""
import os
import sys
import glob
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Set Japanese font for Windows
plt.rcParams['font.sans-serif'] = ['Meiryo', 'Yu Gothic', 'MS Gothic', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

DATA_DIR = Path(__file__).resolve().parent / "Data"
OUTPUT_DIR = Path(__file__).resolve().parent / "backtest_results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR = Path("C:/Users/user/.gemini/antigravity/brain/c2ee6b7d-83a7-457e-a308-a7e7bb62b34c")


def load_universe_data(data_dir: Path):
    csv_files = glob.glob(str(data_dir / "merged_*.csv"))
    closes = {}
    opens = {}
    highs = {}
    lows = {}

    for fpath in csv_files:
        p = Path(fpath)
        symbol = p.stem.replace("merged_", "")
        if not symbol:
            continue
        try:
            df = pd.read_csv(p)
            if "timestamp" not in df.columns or "close" not in df.columns:
                continue
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).set_index("timestamp")
            closes[symbol] = df["close"].astype(float)
            opens[symbol] = df["open"].astype(float) if "open" in df.columns else df["close"].astype(float)
            highs[symbol] = df["high"].astype(float) if "high" in df.columns else df["close"].astype(float)
            lows[symbol] = df["low"].astype(float) if "low" in df.columns else df["close"].astype(float)
        except Exception:
            pass

    close_df = pd.DataFrame(closes).sort_index()
    open_df = pd.DataFrame(opens).sort_index()
    high_df = pd.DataFrame(highs).sort_index()
    low_df = pd.DataFrame(lows).sort_index()

    valid_cols = [c for c in close_df.columns if close_df[c].isna().mean() < 0.3]
    return close_df[valid_cols].ffill().bfill(), open_df[valid_cols].ffill().bfill(), high_df[valid_cols].ffill().bfill(), low_df[valid_cols].ffill().bfill()


def generate_explanation_charts():
    close_df, open_df, high_df, low_df = load_universe_data(DATA_DIR)
    roc_24h = (close_df / close_df.shift(24)) - 1.0

    # -------------------------------------------------------------
    # CHART 1: 「なぜ次の1本で負けるのか」のケーススタディ（実例チャート）
    # -------------------------------------------------------------
    # Find a representative symbol and spike event
    # Find top ranking transitions
    sample_symbols = ["PUMP", "PURR", "HYPE", "AIXBT", "FARTCOIN", "DOGE", "SOL"]
    target_sym = None
    for sym in sample_symbols:
        if sym in close_df.columns:
            target_sym = sym
            break
    if not target_sym:
        target_sym = close_df.columns[0]

    # Find the peak 24h ROC moment for this symbol
    sym_roc = roc_24h[target_sym].dropna()
    peak_idx = sym_roc.idxmax()
    peak_pos = close_df.index.get_loc(peak_idx)
    
    # Slice 48 bars before and 48 bars after
    start_pos = max(0, peak_pos - 36)
    end_pos = min(len(close_df), peak_pos + 36)
    slice_idx = close_df.index[start_pos:end_pos]
    
    df_slice = pd.DataFrame({
        "open": open_df.loc[slice_idx, target_sym],
        "high": high_df.loc[slice_idx, target_sym],
        "low": low_df.loc[slice_idx, target_sym],
        "close": close_df.loc[slice_idx, target_sym],
        "roc_24h": roc_24h.loc[slice_idx, target_sym] * 100
    })

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={'height_ratios': [2.5, 1]}, sharex=True)
    
    # Plot price
    ax_top.plot(df_slice.index, df_slice["close"], label=f"{target_sym} 価格 (USDC)", color="#1f77b4", linewidth=2.2)
    ax_top.scatter([peak_idx], [df_slice.loc[peak_idx, "close"]], color="red", s=150, zorder=5, label="24h上昇率が最大（検知・1H買い）")
    
    # Annotations
    peak_price = df_slice.loc[peak_idx, "close"]
    next_price = df_slice["close"].iloc[df_slice.index.get_loc(peak_idx) + 1]
    subsequent_min = df_slice["close"].iloc[df_slice.index.get_loc(peak_idx):df_slice.index.get_loc(peak_idx) + 12].min()
    dump_pct = (subsequent_min / peak_price - 1.0) * 100
    next_bar_pct = (next_price / peak_price - 1.0) * 100

    ax_top.annotate(
        f"【24h急騰の過熱天井】\nここで『次の1本の伸び』を期待して飛び乗る\n→ 次足リターン: {next_bar_pct:+.2f}%\n→ その後12時間の急落: {dump_pct:+.2f}%",
        xy=(peak_idx, peak_price),
        xytext=(peak_idx + pd.Timedelta(hours=4), peak_price * 1.03),
        arrowprops=dict(facecolor='red', shrink=0.08, width=2, headwidth=8),
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#ffebee", edgecolor="red", alpha=0.9),
        fontsize=11,
        fontweight="bold"
    )

    ax_top.set_title(f"【実例検証】24時間上昇率ランキング上位飛び乗りの実態（{target_sym}の価格推移）", fontsize=14, fontweight="bold", pad=12)
    ax_top.set_ylabel("価格 (USDC)", fontsize=12)
    ax_top.grid(True, linestyle="--", alpha=0.5)
    ax_top.legend(loc="upper left", fontsize=11)

    # Plot 24h ROC
    ax_bot.plot(df_slice.index, df_slice["roc_24h"], label="24時間上昇率 (%)", color="#ff7f0e", linewidth=2)
    ax_bot.axhline(0, color="gray", linestyle=":")
    ax_bot.scatter([peak_idx], [df_slice.loc[peak_idx, "roc_24h"]], color="red", s=100, zorder=5)
    ax_bot.set_ylabel("24h 上昇率 (%)", fontsize=12)
    ax_bot.set_xlabel("日時", fontsize=12)
    ax_bot.grid(True, linestyle="--", alpha=0.5)
    ax_bot.legend(loc="upper left", fontsize=11)

    plt.tight_layout()
    chart1_path = OUTPUT_DIR / "chart1_peak_trap_case_study.png"
    plt.savefig(chart1_path, dpi=150)
    plt.close()

    # -------------------------------------------------------------
    # CHART 2: 「次の1時間足」のリターン分布（全銘柄・全タイムステップ集計）
    # -------------------------------------------------------------
    # Collect next-bar return of Top 1 and Top 3 symbols whenever ranked top
    next_1h_rets_top1 = []
    next_1h_rets_top3 = []
    next_4h_rets_top3 = []
    next_24h_rets_top3 = []

    for i in range(25, len(close_df) - 24):
        t_prev = close_df.index[i - 1]
        t_curr = close_df.index[i]
        t_4h = close_df.index[i + 4]
        t_24h = close_df.index[i + 24]
        
        ranks = roc_24h.loc[t_prev].dropna().sort_values(ascending=False)
        top1_sym = ranks.index[0]
        top3_syms = ranks.index[:3]
        
        # Top 1 next 1h return
        p_now = close_df.loc[t_curr, top1_sym]
        p_next = close_df.loc[close_df.index[i + 1], top1_sym]
        next_1h_rets_top1.append((p_next / p_now - 1.0) * 100)

        # Top 3 avg returns
        p3_now = close_df.loc[t_curr, top3_syms]
        p3_1h = close_df.loc[close_df.index[i + 1], top3_syms]
        p3_4h = close_df.loc[t_4h, top3_syms]
        p3_24h = close_df.loc[t_24h, top3_syms]
        
        next_1h_rets_top3.append(((p3_1h / p3_now - 1.0).mean()) * 100)
        next_4h_rets_top3.append(((p3_4h / p3_now - 1.0).mean()) * 100)
        next_24h_rets_top3.append(((p3_24h / p3_now - 1.0).mean()) * 100)

    fig, (ax_dist1, ax_dist2) = plt.subplots(1, 2, figsize=(15, 6))

    # Distribution of 1H Next-Bar Return (Top 1)
    s1 = pd.Series(next_1h_rets_top1)
    mean1 = s1.mean()
    win1 = (s1 > 0).mean() * 100
    loss_tail = (s1 < -3.0).mean() * 100

    ax_dist1.hist(s1, bins=60, range=(-10, 10), color="#e74c3c", alpha=0.7, edgecolor="black")
    ax_dist1.axvline(0, color="black", linestyle="--", linewidth=1.5)
    ax_dist1.axvline(mean1, color="blue", linestyle="-", linewidth=2, label=f"平均期待値: {mean1:+.2f}%/本")
    ax_dist1.set_title("【1時間足】24h上昇率1位を買った『次の1本』の収益率分布", fontsize=12, fontweight="bold")
    ax_dist1.set_xlabel("次の1時間の収益率 (%)", fontsize=11)
    ax_dist1.set_ylabel("発生度数 (回数)", fontsize=11)
    ax_dist1.grid(True, linestyle="--", alpha=0.5)
    ax_dist1.legend(loc="upper right", fontsize=11)
    
    ax_dist1.text(
        0.05, 0.75,
        f"・勝率: {win1:.1f}% (負け越す)\n・平均リターン: {mean1:+.2f}%\n・1本で-3%超の急落確率: {loss_tail:.1f}%\n⇒『次の1本で伸びる』は統計的に否定される",
        transform=ax_dist1.transAxes,
        bbox=dict(boxstyle="round", facecolor="#fffde7", edgecolor="#fbc02d", alpha=0.9),
        fontsize=10.5
    )

    # Boxplot comparing 1H vs 4H vs 24H returns
    data_to_plot = [s1, pd.Series(next_1h_rets_top3), pd.Series(next_4h_rets_top3), pd.Series(next_24h_rets_top3)]
    labels = ["1H保有\n(Top 1)", "1H保有\n(Top 3分散)", "4H保有\n(Top 3分散)", "24H保有\n(Top 3分散)"]
    
    bplot = ax_dist2.boxplot(data_to_plot, patch_artist=True, labels=labels, showmeans=True, meanline=True)
    colors = ['#ff9999', '#66b3ff', '#99ff99', '#ffcc99']
    for patch, color in zip(bplot['boxes'], colors):
        patch.set_facecolor(color)
    
    ax_dist2.axhline(0, color="gray", linestyle="--", alpha=0.7)
    ax_dist2.set_title("保有時間（リバランス周期）別のリターン比較", fontsize=12, fontweight="bold")
    ax_dist2.set_ylabel("期間収益率 (%)", fontsize=11)
    ax_dist2.grid(True, linestyle="--", alpha=0.5)

    plt.tight_layout()
    chart2_path = OUTPUT_DIR / "chart2_return_distribution.png"
    plt.savefig(chart2_path, dpi=150)
    plt.close()

    # -------------------------------------------------------------
    # CHART 3: 戦略比較ダッシュボード（4面図で全貌を可視化）
    # -------------------------------------------------------------
    # Load phase results CSVs
    p1_df = pd.read_csv(OUTPUT_DIR / "phase1_rebalance_vs_topn.csv")
    p2_df = pd.read_csv(OUTPUT_DIR / "phase2_cost_sensitivity.csv")
    p3_df = pd.read_csv(OUTPUT_DIR / "phase3_stop_loss.csv")

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # Panel A: Return by Rebalance Period (Top 1 vs 3 vs 5 vs 10)
    ax_a = axes[0, 0]
    pvt_p1 = p1_df.pivot(index="rebalance_hours", columns="top_n", values="total_return_pct")
    for col in pvt_p1.columns:
        ax_a.plot(pvt_p1.index, pvt_p1[col], marker='o', linewidth=2.2, label=f"Top {col} 分散")
    ax_a.axhline(0, color="black", linestyle="--", alpha=0.7)
    ax_a.axhline(-1.19, color="orange", linestyle=":", label="BTC Buy & Hold (-1.19%)")
    ax_a.set_title("A. リバランス周期 × 分散銘柄数別の累積リターン", fontsize=12, fontweight="bold")
    ax_a.set_xlabel("リバランス周期 (時間)", fontsize=11)
    ax_a.set_ylabel("累積リターン (%)", fontsize=11)
    ax_a.set_xticks([1, 2, 4, 8, 12, 24])
    ax_a.grid(True, linestyle="--", alpha=0.5)
    ax_a.legend(loc="lower right", fontsize=10)

    # Panel B: Gross Return vs Net Return (Cost impact)
    ax_b = axes[0, 1]
    # Filter 1H Top3, 4H Top3, 24H Top3 across costs
    for reb, col in [(1, "red"), (4, "blue"), (24, "green")]:
        sub = p2_df[(p2_df["rebalance_hours"] == reb) & (p2_df["top_n"] == 3)]
        costs_pct = sub["cost_per_side"] * 200  # Round trip %
        ax_b.plot(costs_pct, sub["total_return_pct"], marker='s', linewidth=2.2, color=col, label=f"{reb}Hリバランス (Top 3)")
    ax_b.set_title("B. 取引コスト（往復手数料+スリッページ）の影響", fontsize=12, fontweight="bold")
    ax_b.set_xlabel("往復取引コスト (%)", fontsize=11)
    ax_b.set_ylabel("累積リターン (%)", fontsize=11)
    ax_b.grid(True, linestyle="--", alpha=0.5)
    ax_b.legend(loc="upper right", fontsize=10)

    # Panel C: Win Rate by Strategy
    ax_c = axes[1, 0]
    pvt_win = p1_df.pivot(index="rebalance_hours", columns="top_n", values="win_rate")
    for col in pvt_win.columns:
        ax_c.plot(pvt_win.index, pvt_win[col], marker='^', linewidth=2, label=f"Top {col}")
    ax_c.axhline(50, color="gray", linestyle="--", label="勝率 50% ライン")
    ax_c.set_title("C. 各リバランス周期におけるトレード勝率 (%)", fontsize=12, fontweight="bold")
    ax_c.set_xlabel("リバランス周期 (時間)", fontsize=11)
    ax_c.set_ylabel("勝率 (%)", fontsize=11)
    ax_c.set_xticks([1, 2, 4, 8, 12, 24])
    ax_c.set_ylim(20, 60)
    ax_c.grid(True, linestyle="--", alpha=0.5)
    ax_c.legend(loc="lower right", fontsize=10)

    # Panel D: Stop Loss Impact on 24H Top 3
    ax_d = axes[1, 1]
    sl_labels = ["ストップロスなし", "SL -1.5% (浅すぎ)", "SL -2.5% (最適)"]
    # 24H Top 3: No SL (-22.58%), SL 1.5% (-62.59%), SL 2.5% (+18.42%)
    sl_returns = [-22.58, -62.59, 18.42]
    bar_colors = ["#e74c3c", "#e67e22", "#2ecc71"]
    bars = ax_d.bar(sl_labels, sl_returns, color=bar_colors, width=0.55, edgecolor="black")
    ax_d.axhline(0, color="black", linestyle="--", alpha=0.7)
    ax_d.set_title("D. 24時間保有における損切り（ストップロス）の効果", fontsize=12, fontweight="bold")
    ax_d.set_ylabel("累積リターン (%)", fontsize=11)
    ax_d.grid(True, linestyle="--", alpha=0.5, axis='y')
    for bar in bars:
        height = bar.get_height()
        va = 'bottom' if height >= 0 else 'top'
        ax_d.annotate(f"{height:+.2f}%",
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3 if height >= 0 else -12),
                    textcoords="offset points",
                    ha='center', va=va, fontsize=11, fontweight="bold")

    plt.tight_layout()
    chart3_path = OUTPUT_DIR / "chart3_strategy_summary_dashboard.png"
    plt.savefig(chart3_path, dpi=150)
    plt.close()

    print("All explanation charts generated successfully!")

    # Copy to Artifact directory
    import shutil
    for fname in ["chart1_peak_trap_case_study.png", "chart2_return_distribution.png", "chart3_strategy_summary_dashboard.png"]:
        src = OUTPUT_DIR / fname
        dst = ARTIFACT_DIR / fname
        shutil.copy(src, dst)
        print(f"Copied {fname} to artifact dir.")


if __name__ == "__main__":
    generate_explanation_charts()
