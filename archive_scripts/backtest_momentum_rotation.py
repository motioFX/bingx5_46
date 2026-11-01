"""Comprehensive Cross-Sectional Momentum Rotation Backtester.

Evaluates 24-hour Rate-of-Change (ROC) momentum strategies across:
- Rebalance intervals (1H, 2H, 4H, 8H, 12H, 24H)
- Portfolio sizes (Top 1, Top 3, Top 5, Top 10)
- Trading fee & slippage costs (0.0%, 0.05%, 0.10%, 0.15% per side)
- Stop loss protections (None, -1.5%, -2.5%)
- Benchmarks: BTC Buy & Hold, Equal-Weight Universe Index
"""
import os
import sys
import glob
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Any

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = Path(__file__).resolve().parent / "Data"
OUTPUT_DIR = Path(__file__).resolve().parent / "backtest_results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_universe_data(data_dir: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load all merged_*.csv files and align by timestamp.
    Returns aligned close_df, open_df, high_df, low_df.
    """
    csv_files = glob.glob(str(data_dir / "merged_*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No merged_*.csv files found in {data_dir}")

    closes = {}
    opens = {}
    highs = {}
    lows = {}

    print(f"Found {len(csv_files)} symbol CSV files in {data_dir}. Loading...")

    for fpath in csv_files:
        p = Path(fpath)
        # Extract symbol name: merged_SOL.csv -> SOL
        symbol = p.stem.replace("merged_", "")
        if not symbol:
            continue
        try:
            df = pd.read_csv(p)
            if "timestamp" not in df.columns or "close" not in df.columns:
                continue
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
            df = df.set_index("timestamp")
            
            closes[symbol] = df["close"].astype(float)
            opens[symbol] = df["open"].astype(float) if "open" in df.columns else df["close"].astype(float)
            highs[symbol] = df["high"].astype(float) if "high" in df.columns else df["close"].astype(float)
            lows[symbol] = df["low"].astype(float) if "low" in df.columns else df["close"].astype(float)
        except Exception as e:
            print(f"Warning: Failed to load {fpath}: {e}")

    close_df = pd.DataFrame(closes).sort_index()
    open_df = pd.DataFrame(opens).sort_index()
    high_df = pd.DataFrame(highs).sort_index()
    low_df = pd.DataFrame(lows).sort_index()

    # Drop symbols with too many NaNs (> 30% missing)
    valid_cols = [c for c in close_df.columns if close_df[c].isna().mean() < 0.3]
    close_df = close_df[valid_cols].ffill().bfill()
    open_df = open_df[valid_cols].ffill().bfill()
    high_df = high_df[valid_cols].ffill().bfill()
    low_df = low_df[valid_cols].ffill().bfill()

    print(f"Loaded {len(close_df.columns)} active symbols across {len(close_df)} hourly bars.")
    print(f"Data period: {close_df.index[0]} to {close_df.index[-1]} (Total {(close_df.index[-1] - close_df.index[0]).days} days)")

    return close_df, open_df, high_df, low_df


def simulate_rotation_strategy(
    close_df: pd.DataFrame,
    open_df: pd.DataFrame,
    high_df: pd.DataFrame,
    low_df: pd.DataFrame,
    lookback_hours: int = 24,
    rebalance_hours: int = 1,
    top_n: int = 1,
    cost_per_side: float = 0.001,  # 0.1% per side (0.2% round-trip)
    stop_loss_pct: float = None,   # e.g., 0.02 for 2% stop loss
    initial_capital: float = 10000.0,
) -> Dict[str, Any]:
    """Simulates cross-sectional momentum rotation."""
    timestamps = close_df.index
    n_bars = len(timestamps)
    
    # 24-hour ROC (Rate of Change): (Close_t / Close_{t-24}) - 1.0
    roc_df = (close_df / close_df.shift(lookback_hours)) - 1.0

    capital = initial_capital
    portfolio_history = [capital] * n_bars
    trade_logs = []
    
    # Track current holdings: {symbol: {'entry_price': float, 'weight': float, 'entry_bar': int}}
    current_holdings = {}
    rebalance_counter = 0

    # Start simulation after initial lookback period
    start_bar = lookback_hours + 1

    for i in range(start_bar, n_bars):
        current_time = timestamps[i]
        prev_time = timestamps[i - 1]
        
        # 1. Update valuation & check intraday stop loss on existing holdings
        bar_return_sum = 0.0
        stopped_symbols = set()
        
        if current_holdings:
            n_held = len(current_holdings)
            weight_per_held = 1.0 / n_held
            
            for sym, info in list(current_holdings.items()):
                entry_p = info["entry_price"]
                bar_open = open_df.loc[current_time, sym]
                bar_high = high_df.loc[current_time, sym]
                bar_low = low_df.loc[current_time, sym]
                bar_close = close_df.loc[current_time, sym]
                prev_close = close_df.loc[prev_time, sym]

                # Check Stop Loss
                if stop_loss_pct is not None and stop_loss_pct > 0:
                    sl_price = entry_p * (1.0 - stop_loss_pct)
                    if bar_low <= sl_price:
                        # Stopped out! Exit at SL price (or open if gapped below)
                        exit_price = min(bar_open, sl_price)
                        asset_ret = (exit_price / prev_close) - 1.0 - cost_per_side
                        bar_return_sum += asset_ret * weight_per_held
                        stopped_symbols.add(sym)
                        trade_logs.append({
                            "time": current_time,
                            "symbol": sym,
                            "type": "STOP_LOSS",
                            "entry": entry_p,
                            "exit": exit_price,
                            "ret_pct": (exit_price / entry_p - 1.0) * 100,
                            "bars_held": i - info["entry_bar"]
                        })
                        continue

                # Normal hold return
                asset_ret = (bar_close / prev_close) - 1.0
                bar_return_sum += asset_ret * weight_per_held

            # Remove stopped symbols
            for sym in stopped_symbols:
                del current_holdings[sym]

        # Apply bar return to portfolio capital
        capital *= (1.0 + bar_return_sum)
        rebalance_counter += 1

        # 2. Check if Rebalance is due
        if rebalance_counter >= rebalance_hours or not current_holdings:
            rebalance_counter = 0
            
            # Get ROC rankings at bar i-1 (available at start of bar i)
            ranks = roc_df.loc[prev_time].dropna()
            # Sort descending by 24h ROC
            sorted_ranks = ranks.sort_values(ascending=False)
            
            # Select Top N
            new_target_symbols = list(sorted_ranks.index[:top_n])
            
            # Determine turnover / changes
            old_set = set(current_holdings.keys())
            new_set = set(new_target_symbols)
            
            exited_symbols = old_set - new_set
            entered_symbols = new_set - old_set
            kept_symbols = old_set & new_set

            # Deduct trading cost for exits and entries
            turnover_weight = (len(exited_symbols) + len(entered_symbols)) / max(1, top_n)
            turnover_cost = turnover_weight * cost_per_side
            capital *= (1.0 - turnover_cost)

            # Record exit trades
            for sym in exited_symbols:
                info = current_holdings[sym]
                bar_close = close_df.loc[current_time, sym]
                trade_logs.append({
                    "time": current_time,
                    "symbol": sym,
                    "type": "REBALANCE_EXIT",
                    "entry": info["entry_price"],
                    "exit": bar_close,
                    "ret_pct": (bar_close / info["entry_price"] - 1.0) * 100,
                    "bars_held": i - info["entry_bar"]
                })

            # Update current holdings
            new_holdings = {}
            for sym in new_target_symbols:
                if sym in kept_symbols:
                    new_holdings[sym] = current_holdings[sym]
                else:
                    new_holdings[sym] = {
                        "entry_price": close_df.loc[current_time, sym],
                        "entry_bar": i
                    }
            current_holdings = new_holdings

        portfolio_history[i] = capital

    equity_series = pd.Series(portfolio_history, index=timestamps)
    
    # Calculate performance metrics
    final_capital = equity_series.iloc[-1]
    total_return_pct = (final_capital / initial_capital - 1.0) * 100.0
    
    # Max Drawdown
    cummax = equity_series.cummax()
    drawdowns = (equity_series - cummax) / cummax
    max_dd_pct = abs(drawdowns.min()) * 100.0
    
    # Hourly Sharpe (annualized)
    hourly_returns = equity_series.pct_change().fillna(0)
    sharpe = (hourly_returns.mean() / (hourly_returns.std() + 1e-9)) * np.sqrt(24 * 365) if hourly_returns.std() > 0 else 0.0

    # Trade stats
    df_trades = pd.DataFrame(trade_logs)
    n_trades = len(df_trades)
    win_rate = (df_trades["ret_pct"] > 0).mean() * 100 if n_trades > 0 else 0.0
    avg_gain = df_trades[df_trades["ret_pct"] > 0]["ret_pct"].mean() if (df_trades["ret_pct"] > 0).any() else 0.0
    avg_loss = abs(df_trades[df_trades["ret_pct"] < 0]["ret_pct"].mean()) if (df_trades["ret_pct"] < 0).any() else 1.0
    profit_factor = (avg_gain * win_rate) / (avg_loss * (100 - win_rate) + 1e-9) if (100 - win_rate) > 0 else 0.0

    return {
        "rebalance_hours": rebalance_hours,
        "top_n": top_n,
        "cost_per_side": cost_per_side,
        "stop_loss_pct": stop_loss_pct,
        "total_return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "n_trades": n_trades,
        "equity_series": equity_series
    }


def run_full_experiment():
    print("=" * 80)
    print(" 24-HOUR MOMENTUM ROTATION COMPREHENSIVE BACKTEST")
    print("=" * 80)

    close_df, open_df, high_df, low_df = load_universe_data(DATA_DIR)

    # 1. Benchmarks
    # BTC Buy & Hold
    if "BTC" in close_df.columns:
        btc_close = close_df["BTC"]
        btc_equity = 10000.0 * (btc_close / btc_close.iloc[25])
        btc_ret = (btc_equity.iloc[-1] / 10000.0 - 1.0) * 100.0
        btc_dd = abs(((btc_equity - btc_equity.cummax()) / btc_equity.cummax()).min()) * 100.0
    else:
        btc_ret = 0.0
        btc_dd = 0.0
        btc_equity = pd.Series(10000.0, index=close_df.index)

    # Equal Weight Universe Buy & Hold
    ew_daily_rets = close_df.pct_change().mean(axis=1).fillna(0)
    ew_equity = 10000.0 * (1.0 + ew_daily_rets).cumprod()
    ew_ret = (ew_equity.iloc[-1] / 10000.0 - 1.0) * 100.0
    ew_dd = abs(((ew_equity - ew_equity.cummax()) / ew_equity.cummax()).min()) * 100.0

    print(f"\n[Benchmark] BTC Buy & Hold: Return = {btc_ret:+.2f}%, MaxDD = {btc_dd:.2f}%")
    print(f"[Benchmark] Equal-Weight All Alts: Return = {ew_ret:+.2f}%, MaxDD = {ew_dd:.2f}%")

    # Grid parameters
    rebalance_options = [1, 2, 4, 8, 12, 24]
    top_n_options = [1, 3, 5, 10]
    cost_options = [0.0, 0.0005, 0.0010, 0.0015]  # 0%, 0.05%, 0.10%, 0.15% per side

    # --- Phase 1: Rebalance Interval x Top N (Standard Cost 0.1% per side, No SL) ---
    print("\n" + "=" * 80)
    print(" PHASE 1: Rebalance Interval (1H ~ 24H) x Portfolio Size (Top 1 ~ 10)")
    print(" (Standard Cost = 0.10% per side / 0.20% round-trip, No SL)")
    print("=" * 80)

    p1_results = []
    p1_equities = {}

    for reb in rebalance_options:
        for top_n in top_n_options:
            res = simulate_rotation_strategy(
                close_df=close_df,
                open_df=open_df,
                high_df=high_df,
                low_df=low_df,
                lookback_hours=24,
                rebalance_hours=reb,
                top_n=top_n,
                cost_per_side=0.001,
                stop_loss_pct=None
            )
            p1_results.append(res)
            label = f"{reb}H_Top{top_n}"
            p1_equities[label] = res["equity_series"]
            print(f"[{label:<12}] Return: {res['total_return_pct']:+7.2f}% | MaxDD: {res['max_dd_pct']:5.2f}% | Sharpe: {res['sharpe']:5.2f} | Win: {res['win_rate']:4.1f}% | Trades: {res['n_trades']:5d}")

    # --- Phase 2: Cost Sensitivity Analysis on 1H vs 4H vs 24H Top 3 ---
    print("\n" + "=" * 80)
    print(" PHASE 2: Cost Sensitivity (0.0% to 0.3% Round-Trip)")
    print("=" * 80)

    p2_results = []
    for cost in cost_options:
        for reb in [1, 4, 24]:
            for top_n in [1, 3, 5]:
                res = simulate_rotation_strategy(
                    close_df=close_df,
                    open_df=open_df,
                    high_df=high_df,
                    low_df=low_df,
                    lookback_hours=24,
                    rebalance_hours=reb,
                    top_n=top_n,
                    cost_per_side=cost,
                    stop_loss_pct=None
                )
                p2_results.append(res)
                print(f"[Cost {cost*200:.2f}% RT | {reb}H_Top{top_n:<2}] Return: {res['total_return_pct']:+7.2f}% | MaxDD: {res['max_dd_pct']:5.2f}% | Sharpe: {res['sharpe']:5.2f}")

    # --- Phase 3: Stop Loss Protection (4H & 24H Top 3 & Top 5) ---
    print("\n" + "=" * 80)
    print(" PHASE 3: Stop Loss Protection (-1.5%, -2.5% vs None)")
    print("=" * 80)

    p3_results = []
    for sl in [None, 0.015, 0.025]:
        sl_label = f"SL_{sl*100:.1f}%" if sl else "No_SL"
        for reb in [1, 4, 8, 24]:
            for top_n in [3, 5]:
                res = simulate_rotation_strategy(
                    close_df=close_df,
                    open_df=open_df,
                    high_df=high_df,
                    low_df=low_df,
                    lookback_hours=24,
                    rebalance_hours=reb,
                    top_n=top_n,
                    cost_per_side=0.001,
                    stop_loss_pct=sl
                )
                p3_results.append(res)
                print(f"[{sl_label:<8} | {reb}H_Top{top_n}] Return: {res['total_return_pct']:+7.2f}% | MaxDD: {res['max_dd_pct']:5.2f}% | Sharpe: {res['sharpe']:5.2f} | Trades: {res['n_trades']:5d}")

    # --- Plotting & Visualizing Key Findings ---
    print("\nGenerating charts...")

    fig, axes = plt.subplots(3, 1, figsize=(14, 16), sharex=True)
    
    # Subplot 1: 1H vs 4H vs 24H Top 3 (Standard Cost)
    ax1 = axes[0]
    ax1.plot(btc_equity.index, (btc_equity / 10000 - 1) * 100, label="BTC Buy & Hold", color="orange", linestyle="--", alpha=0.7)
    ax1.plot(ew_equity.index, (ew_equity / 10000 - 1) * 100, label="All Alts Equal-Weight", color="gray", linestyle=":", alpha=0.7)
    
    for reb, color in [(1, "red"), (4, "blue"), (8, "green"), (24, "purple")]:
        k = f"{reb}H_Top3"
        if k in p1_equities:
            ax1.plot(p1_equities[k].index, (p1_equities[k] / 10000 - 1) * 100, label=f"{reb}H Rebalance (Top 3)", color=color, linewidth=1.8)
            
    ax1.set_title("1. Rebalance Interval Comparison (Top 3 Alts, 0.20% Round-Trip Cost)", fontsize=13, fontweight="bold")
    ax1.set_ylabel("Cumulative Return (%)", fontsize=11)
    ax1.grid(True, linestyle="--", alpha=0.5)
    ax1.legend(loc="upper left")

    # Subplot 2: Top 1 vs Top 3 vs Top 5 vs Top 10 (4H Rebalance)
    ax2 = axes[1]
    ax2.plot(btc_equity.index, (btc_equity / 10000 - 1) * 100, label="BTC Buy & Hold", color="orange", linestyle="--", alpha=0.7)
    for top_n, color in [(1, "magenta"), (3, "blue"), (5, "teal"), (10, "darkgreen")]:
        k = f"4H_Top{top_n}"
        if k in p1_equities:
            ax2.plot(p1_equities[k].index, (p1_equities[k] / 10000 - 1) * 100, label=f"4H Rebalance - Top {top_n}", color=color, linewidth=1.8)
    ax2.set_title("2. Portfolio Concentration (Top 1 vs 3 vs 5 vs 10 at 4H Rebalance)", fontsize=13, fontweight="bold")
    ax2.set_ylabel("Cumulative Return (%)", fontsize=11)
    ax2.grid(True, linestyle="--", alpha=0.5)
    ax2.legend(loc="upper left")

    # Subplot 3: 1H Rebalance Cost Impact (0% vs 0.1% vs 0.2% vs 0.3% RT)
    ax3 = axes[2]
    cost_colors = ["green", "blue", "orange", "red"]
    for idx, cost in enumerate(cost_options):
        res = simulate_rotation_strategy(close_df, open_df, high_df, low_df, lookback_hours=24, rebalance_hours=1, top_n=3, cost_per_side=cost)
        ax3.plot(res["equity_series"].index, (res["equity_series"] / 10000 - 1) * 100, label=f"1H Top 3 - RoundTrip Cost {cost*200:.2f}%", color=cost_colors[idx], linewidth=1.8)
    ax3.set_title("3. The Trading Cost Trap: 1H Rebalance under Different Fee/Slippage Levels", fontsize=13, fontweight="bold")
    ax3.set_ylabel("Cumulative Return (%)", fontsize=11)
    ax3.grid(True, linestyle="--", alpha=0.5)
    ax3.legend(loc="upper left")

    plt.tight_layout()
    chart_path = OUTPUT_DIR / "momentum_rotation_backtest_chart.png"
    plt.savefig(chart_path, dpi=150)
    plt.close()
    print(f"Chart saved to {chart_path}")

    # Export summary CSV
    df_p1 = pd.DataFrame([{k: v for k, v in r.items() if k != 'equity_series'} for r in p1_results])
    df_p1.to_csv(OUTPUT_DIR / "phase1_rebalance_vs_topn.csv", index=False)

    df_p2 = pd.DataFrame([{k: v for k, v in r.items() if k != 'equity_series'} for r in p2_results])
    df_p2.to_csv(OUTPUT_DIR / "phase2_cost_sensitivity.csv", index=False)

    df_p3 = pd.DataFrame([{k: v for k, v in r.items() if k != 'equity_series'} for r in p3_results])
    df_p3.to_csv(OUTPUT_DIR / "phase3_stop_loss.csv", index=False)

    print("\nBacktest completed successfully!")


if __name__ == "__main__":
    run_full_experiment()
