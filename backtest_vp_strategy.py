"""ボリュームプロファイルトレーリング (VPトレーリング) 2段階最適化バックテストスクリプト

仕様:
1. 第1段階 (基礎パラメータ最適化):
   Envelope戻り戦略 と RSIMA戦略 のグリッドサーチを行い、各銘柄の基礎ベストPnLパラメータを特定。
2. 第2段階 (ボリュームプロファイルトレーリング適用):
   特定されたベスト設定に対して、VPトレーリング (含み損時VAL-α%損切り、POC/VAH上抜け切り上げ、大相場コールバック追従) を適用。
3. 結果比較:
   基礎PnL vs VPトレーリングPnL、勝率、最大DD、トレード数を比較表示し、資産推移チャートを出力。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from rich.console import Console
from rich.table import Table

# クロスプラットフォーム日本語フォントフォールバック設定
matplotlib.rcParams["font.sans-serif"] = [
    "Meiryo", "Yu Gothic", "MS Gothic", "Noto Sans CJK JP", "IPAGothic", "DejaVu Sans", "sans-serif"
]
matplotlib.rcParams["axes.unicode_minus"] = False

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bitbank5_46_3logic import (
    simulate_envelope_strategy,
    simulate_rsima_strategy,
    optimize_symbol_strategy,
)
from real_trade_tracker import compute_volume_profile_bands

JST = timezone(timedelta(hours=9))
console = Console()


def load_symbol_data(symbol: str, days: int = 120) -> pd.DataFrame:
    """指定銘柄のヒストリカルCSVを読み込み、指定期間分を返す"""
    csv_path = BASE_DIR / "Data" / "historical_candles" / f"{symbol}_1h.csv"
    if not csv_path.exists():
        # 代替パス
        csv_path = BASE_DIR / "Data" / f"{symbol}_1h.csv"
    if not csv_path.exists():
        return pd.DataFrame()

    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    if days > 0 and len(df) > 0:
        latest_dt = df["timestamp"].max()
        cutoff_dt = latest_dt - timedelta(days=days)
        df = df[df["timestamp"] >= cutoff_dt].copy().reset_index(drop=True)

    return df


def plot_backtest_result(df: pd.DataFrame, res: Dict[str, Any], symbol: str, save_path: Path):
    """ローソク足、VPバンド、売買ポイント、および資産推移を描画"""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [2.5, 1.2]}, sharex=True)
    plt.subplots_adjust(hspace=0.08)

    timestamps = pd.to_datetime(df["timestamp"])
    closes = df["close"].values
    highs = df["high"].values if "high" in df.columns else closes
    lows = df["low"].values if "low" in df.columns else closes

    # 1. 上段: ローソク足 & VPバンド
    ax1.plot(timestamps, closes, label=f"{symbol} Close", color="#78909c", linewidth=1.0, alpha=0.7)

    if "VAH" in df.columns and "VAL" in df.columns and "POC" in df.columns:
        ax1.fill_between(timestamps, df["VAL"], df["VAH"], color="#ff9800", alpha=0.10, label="Value Area (VAH-VAL)")
        ax1.plot(timestamps, df["VAH"], color="#ff9800", linestyle="--", linewidth=1.2, alpha=0.85, label="VAH")
        ax1.plot(timestamps, df["POC"], color="#29b6f6", linestyle="-.", linewidth=1.2, alpha=0.85, label="POC")
        ax1.plot(timestamps, df["VAL"], color="#ab47bc", linestyle="--", linewidth=1.2, alpha=0.85, label="VAL")

    # 売買ポイントのプロット
    exec_history = res.get("exec_history", [])
    for trade in exec_history:
        t_ts = pd.to_datetime(trade["timestamp"])
        t_px = trade["price"]
        t_type = trade["type"]

        if t_type == "BUY":
            ax1.scatter(t_ts, t_px, color="#00e676", marker="^", s=80, zorder=5, edgecolors="#1b5e20")
        elif "STOP_LOSS" in t_type:
            ax1.scatter(t_ts, t_px, color="#ff1744", marker="x", s=80, zorder=5, linewidths=2)
        else:  # TP / SELL
            ax1.scatter(t_ts, t_px, color="#ffd600", marker="v", s=80, zorder=5, edgecolors="#f57f17")

    strat_name = res.get("strategy", "").upper()
    pnl = res.get("final_pnl", 0.0)
    win_rate = res.get("win_rate", 0.0)
    dd_max = res.get("DD_max", 0.0)
    trades_n = res.get("trade_count", 0)

    ax1.set_title(
        f"【{symbol.upper()}】 VPトレーリング バックテスト結果 | 戦略: {strat_name} | "
        f"PnL: {pnl:+.2f} USDT ({pnl:+.1f}%) | 勝率: {win_rate:.1f}% | 最大DD: {dd_max:.2f} | トレード: {trades_n}回",
        fontsize=12, fontweight="bold"
    )
    ax1.set_ylabel("Price (JPY)", fontsize=10)
    ax1.grid(True, linestyle=":", alpha=0.5)
    ax1.legend(loc="upper left", fontsize=8)

    # 2. 下段: 資産推移 (Equity Curve)
    equity_curve = res.get("equity_curve", [])
    if len(equity_curve) == len(timestamps):
        eq_times = timestamps
        eq_vals = equity_curve
    else:
        # 長さが異なる場合は末尾に合わせてスライス
        eq_times = timestamps[-len(equity_curve):]
        eq_vals = equity_curve

    ax2.plot(eq_times, eq_vals, label="Equity Curve", color="#00e5ff", linewidth=1.5)
    ax2.axhline(100.0, color="#ffffff", linestyle="--", linewidth=0.8, alpha=0.6)
    ax2.fill_between(eq_times, 100.0, eq_vals, where=(np.array(eq_vals) >= 100.0), color="#00e5ff", alpha=0.15)
    ax2.fill_between(eq_times, 100.0, eq_vals, where=(np.array(eq_vals) < 100.0), color="#ff1744", alpha=0.15)

    ax2.set_ylabel("Equity", fontsize=10)
    ax2.set_xlabel("Date (JST)", fontsize=10)
    ax2.grid(True, linestyle=":", alpha=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150)
    plt.close()


def run_vp_backtest(symbols: List[str], days: int = 120, force_strategy: Optional[str] = None):
    """複数銘柄のVPバックテストを一括実行し、比較表とチャートを出力"""
    console.print(f"\n[bold cyan]================================================================[/bold cyan]")
    console.print(f"[bold cyan]  🚀 ボリュームプロファイルトレーリング (VPトレーリング) 2段階バックテスト[/bold cyan]")
    console.print(f"[bold cyan]     対象期間: 過去 {days} 日分 | 対象銘柄数: {len(symbols)} 銘柄[/bold cyan]")
    console.print(f"[bold cyan]================================================================[/bold cyan]\n")

    summary_table = Table(title="💎 【2段階最適化 バックテスト比較結果】 (初期資金 $100基準)", show_lines=True)
    summary_table.add_column("銘柄", style="bold yellow", justify="center")
    summary_table.add_column("採用戦略", style="bold cyan", justify="center")
    summary_table.add_column("最適パラメータ", style="white", justify="left")
    summary_table.add_column("① 基礎 PnL\n(トレーリング無)", justify="right")
    summary_table.add_column("② VPトレーリング PnL\n(最新ロジック)", style="bold green", justify="right")
    summary_table.add_column("PnL 改善額", justify="right")
    summary_table.add_column("勝率 (%)", justify="right")
    summary_table.add_column("最大DD", justify="right")
    summary_table.add_column("トレード数", justify="center")

    plots_dir = BASE_DIR / "Data" / "plots"
    ts_tag = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    total_base_pnl = 0.0
    total_vp_pnl = 0.0

    for sym in symbols:
        df = load_symbol_data(sym, days=days)
        if df.empty or len(df) < 50:
            console.print(f"[yellow]⚠️ {sym}: 有効なデータが不足しているためスキップします。[/yellow]")
            continue

        # VPバンド付与
        df = compute_volume_profile_bands(df, period=48)

        # 2段階最適化実行
        best_strat, best_params, vp_res, top10 = optimize_symbol_strategy(
            df, symbol=sym, max_trades=1, initial_equity=100.0, force_strategy=force_strategy
        )

        base_pnl = vp_res.get("base_pnl", 0.0)
        vp_pnl = vp_res.get("final_pnl", 0.0)
        pnl_diff = vp_pnl - base_pnl
        win_rate = vp_res.get("win_rate", 0.0)
        dd_max = vp_res.get("DD_max", 0.0)
        trades_n = vp_res.get("trade_count", 0)

        total_base_pnl += base_pnl
        total_vp_pnl += vp_pnl

        # パラメータ要約文字列
        if best_strat == "envelope":
            p_str = f"L={best_params.get('length')} P={best_params.get('lower_pct')}%"
        else:
            p_str = f"R={best_params.get('rsi_len')} M={best_params.get('lma_len')} Ep={best_params.get('lEp')} Cp={best_params.get('lCp')}"

        diff_sign = "+" if pnl_diff >= 0 else ""
        diff_style = "bold green" if pnl_diff > 0 else ("bold red" if pnl_diff < 0 else "white")

        summary_table.add_row(
            sym.upper(),
            best_strat.upper(),
            p_str,
            f"{base_pnl:+.2f}",
            f"{vp_pnl:+.2f}",
            f"[{diff_style}]{diff_sign}{pnl_diff:.2f}[/{diff_style}]",
            f"{win_rate:.1f}%",
            f"{dd_max:.2f}",
            f"{trades_n} 回"
        )

        # チャート保存
        chart_path = plots_dir / f"backtest_vp_{sym}_{ts_tag}.png"
        plot_backtest_result(df, vp_res, sym, chart_path)

    console.print(summary_table)

    total_diff = total_vp_pnl - total_base_pnl
    t_diff_sign = "+" if total_diff >= 0 else ""
    t_diff_style = "bold green" if total_diff > 0 else "bold red"

    win_symbols_base = sum(1 for sym in symbols if True) # placeholder
    console.print(f"\n[bold]📊 【ポートフォリオ全体 合計損益】[/bold]")
    console.print(f"  • 対象銘柄数                 : [bold cyan]{len(symbols)} 銘柄[/bold cyan]")
    console.print(f"  • ① 基礎ロジック合計PnL      : [white]{total_base_pnl:+.2f} USDT[/white]")
    console.print(f"  • ② VPトレーリング合計PnL    : [bold green]{total_vp_pnl:+.2f} USDT[/bold green]")
    console.print(f"  • 💎 トレーリング改善効果    : [{t_diff_style}]{t_diff_sign}{total_diff:.2f} USDT[/{t_diff_style}]")
    console.print(f"\n📈 チャート画像を [cyan]Data/plots/[/cyan] に保存しました。\n")


def main():
    parser = argparse.ArgumentParser(description="ボリュームプロファイルトレーリング (VPトレーリング) バックテスト")
    parser.add_argument("--symbols", type=str, default="btc_jpy,eth_jpy,xrp_jpy,sol_jpy,doge_jpy", help="対象銘柄 (カンマ区切り)")
    parser.add_argument("--all", action="store_true", help="Data/historical_candles 内の全銘柄を対象にする")
    parser.add_argument("--days", type=int, default=120, help="テスト期間日数 (デフォルト: 120日 / 約4ヶ月)")
    parser.add_argument("--strategy", type=str, default=None, choices=["envelope", "rsima"], help="戦略固定 (未指定時は最良を自動選択)")
    args = parser.parse_args()

    if args.all:
        candles_dir = BASE_DIR / "Data" / "historical_candles"
        csv_files = list(candles_dir.glob("*_1h.csv"))
        symbols = sorted([f.name.replace("_1h.csv", "") for f in csv_files])
    else:
        symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]

    run_vp_backtest(symbols=symbols, days=args.days, force_strategy=args.strategy)


if __name__ == "__main__":
    main()

