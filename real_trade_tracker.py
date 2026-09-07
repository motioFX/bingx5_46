from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
TRADES_FILE = DATA_DIR / "real_trades_history.json"
CHART_FILE = DATA_DIR / "real_trading_performance.png"


def load_real_trades():
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def record_real_trade(symbol: str, side: str, action: str, price: float, qty: float, pnl: float = 0.0, note: str = ""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_real_trades()
    
    trade_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side.upper(),
        "action": action.upper(),  # ENTRY or CLOSE
        "price": float(price),
        "qty": float(qty),
        "pnl": float(pnl),
        "note": note
    }
    trades.append(trade_entry)
    
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)
        
    print(f"[Real Trade Tracker] 記録完了: {symbol} {action} {side} @ ${price:.4f} (PnL: ${pnl:+.2f})")
    plot_real_trading_performance()
    return trade_entry


def plot_real_trading_performance():
    trades = load_real_trades()
    if not trades:
        return None

    df = pd.DataFrame(trades)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # 累積 PnL の計算
    df["cum_pnl"] = df["pnl"].cumsum()

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#1e222d")

    # JST変換
    if df["timestamp"].dt.tz is None:
        df["dt_jst"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert("Asia/Tokyo")
    else:
        df["dt_jst"] = df["timestamp"].dt.tz_convert("Asia/Tokyo")

    ax.plot(df["dt_jst"], df["cum_pnl"], marker='o', markersize=6, color='#29b6f6', linewidth=2.2, label="Cumulative Real PnL (USDC)", zorder=3)
    
    # 0ライン
    ax.axhline(0, color="#787b86", linestyle="--", alpha=0.5, zorder=2)

    # 決済ポイントのみ抽出して重なり・線被りを回避する配置を計算
    close_df = df[(df["action"] == "CLOSE") & (df["pnl"] != 0)].copy().reset_index(drop=True)
    n_close = len(close_df)

    for i, row in close_df.iterrows():
        color = "#26a69a" if row["pnl"] > 0 else "#ef5350"
        
        # 前後の傾き・位置関係に基づくスマートオフセット計算
        # 折れ線の直上を避け、引き出し線付きで線から離れた位置に配置
        curr_y = row["cum_pnl"]
        prev_y = close_df.loc[i - 1, "cum_pnl"] if i > 0 else 0.0
        next_y = close_df.loc[i + 1, "cum_pnl"] if i < n_close - 1 else curr_y
        
        # 傾き判定
        delta_prev = curr_y - prev_y
        delta_next = next_y - curr_y

        # デフォルトは右上に引き出し
        offset_x = 28
        offset_y = 16

        if delta_prev > 0.3 and delta_next >= 0:
            # 急上昇中: 折れ線の右側〜右下に逃がす（または交互に分散）
            if i % 2 == 0:
                offset_x = 35
                offset_y = -18
            else:
                offset_x = -55
                offset_y = 18
        elif delta_prev < -0.3 or delta_next < -0.3:
            # 急降下中: 折れ線の右側〜右上に逃がす
            if i % 2 == 0:
                offset_x = 38
                offset_y = 18
            else:
                offset_x = 38
                offset_y = -18
        else:
            # 水平または緩やかな変化: 上下に交互配置
            if i % 2 == 0:
                offset_x = 28
                offset_y = 20
            else:
                offset_x = 28
                offset_y = -22

        # 最後のポイントは右端で見切れないよう調整
        if i == n_close - 1:
            offset_x = 28
            offset_y = 16

        ax.annotate(
            f"{row['symbol']}: {row['pnl']:+.2f}$", 
            xy=(row["dt_jst"], row["cum_pnl"]),
            xytext=(offset_x, offset_y), textcoords="offset points",
            fontsize=8.5, color=color, weight='bold',
            arrowprops=dict(
                arrowstyle="->",
                color=color,
                lw=0.8,
                alpha=0.7,
                shrinkA=3,
                shrinkB=4,
                connectionstyle="arc3,rad=0.1"
            ),
            bbox=dict(boxstyle="round,pad=0.25", facecolor="#131722", edgecolor=color, alpha=0.9, lw=1.0),
            zorder=6
        )

    # 余白調整 (Y軸・X軸)
    min_pnl = df["cum_pnl"].min()
    max_pnl = df["cum_pnl"].max()
    pnl_range = max(1.5, max_pnl - min_pnl)
    ax.set_ylim(min_pnl - pnl_range * 0.22, max_pnl + pnl_range * 0.25)
    ax.margins(x=0.10)

    ax.set_title("Hyperliquid Real Trading Performance (Cumulative Realized PnL)", fontsize=12, color="#ffffff", pad=15, weight="bold")
    ax.set_xlabel("Time (JST)", fontsize=10, color="#b2b5be")
    ax.set_ylabel("Realized Cumulative PnL (USDC)", fontsize=10, color="#b2b5be")
    ax.tick_params(colors="#b2b5be", labelsize=9)
    ax.grid(True, linestyle=':', color="#363c4e", alpha=0.7)
    ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=9, labelcolor="#ffffff")
    plt.tight_layout()

    CHART_FILE.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(CHART_FILE, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    plt.close('all')
    import gc
    gc.collect()
    print(f"[Real Trade Tracker] 実運用プロットチャート更新完了: {CHART_FILE}")
    return CHART_FILE


def compute_volume_profile_bands(df: pd.DataFrame, period: int = 48, value_area_pct: float = 0.70, num_bins: int = 80) -> pd.DataFrame:
    """ローソク足データに時系列の VAH, VAL, POC (Volume Profile履歴バンド) を計算・付与する"""
    df = df.copy()
    n = len(df)
    if n == 0:
        return df

    poc_arr = np.full(n, np.nan)
    vah_arr = np.full(n, np.nan)
    val_arr = np.full(n, np.nan)

    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values if 'open' in df.columns else closes
    volumes = df['volume'].astype(float).values if 'volume' in df.columns else np.ones(n)

    min_required = min(5, period)

    for i in range(min_required, n):
        start_idx = max(0, i - period + 1)
        w_highs = highs[start_idx:i+1]
        w_lows = lows[start_idx:i+1]
        w_closes = closes[start_idx:i+1]
        w_opens = opens[start_idx:i+1]
        w_volumes = volumes[start_idx:i+1]

        p_max = w_highs.max()
        p_min = w_lows.min()

        if p_max <= p_min or np.isnan(p_max) or np.isnan(p_min):
            poc_arr[i] = closes[i]
            vah_arr[i] = closes[i]
            val_arr[i] = closes[i]
            continue

        price_bins = np.linspace(p_min, p_max, num_bins + 1)
        bin_centers = (price_bins[:-1] + price_bins[1:]) / 2

        body_lows = np.minimum(w_opens, w_closes)
        body_highs = np.maximum(w_opens, w_closes)

        w_h_2d = w_highs[:, np.newaxis]
        w_l_2d = w_lows[:, np.newaxis]
        w_bh_2d = body_highs[:, np.newaxis]
        w_bl_2d = body_lows[:, np.newaxis]
        w_v_2d = w_volumes[:, np.newaxis]

        bin_lows_2d = price_bins[:-1][np.newaxis, :]
        bin_highs_2d = price_bins[1:][np.newaxis, :]

        full_overlap = np.maximum(0.0, np.minimum(w_h_2d, bin_highs_2d) - np.maximum(w_l_2d, bin_lows_2d))
        full_range = np.maximum(w_h_2d - w_l_2d, 1e-12)
        full_ratio = full_overlap / full_range

        body_overlap = np.maximum(0.0, np.minimum(w_bh_2d, bin_highs_2d) - np.maximum(w_bl_2d, bin_lows_2d))
        body_range = np.maximum(w_bh_2d - w_bl_2d, 1e-12)
        body_ratio = body_overlap / body_range

        has_body = (body_highs - body_lows) > 1e-12
        has_body_2d = has_body[:, np.newaxis]

        eff_ratio = np.where(has_body_2d, 0.60 * body_ratio + 0.40 * full_ratio, full_ratio)
        vol_profile = (w_v_2d * eff_ratio).sum(axis=0)

        total_vol = vol_profile.sum()
        if total_vol <= 0:
            poc_arr[i] = closes[i]
            vah_arr[i] = closes[i]
            val_arr[i] = closes[i]
            continue

        poc_idx = int(np.argmax(vol_profile))
        poc_price = bin_centers[poc_idx]

        target_vol = total_vol * value_area_pct
        accum_vol = vol_profile[poc_idx]

        up_idx = poc_idx + 1
        down_idx = poc_idx - 1

        while accum_vol < target_vol and (up_idx < num_bins or down_idx >= 0):
            up_vol = vol_profile[up_idx] if up_idx < num_bins else 0.0
            down_vol = vol_profile[down_idx] if down_idx >= 0 else 0.0

            if up_vol >= down_vol and up_idx < num_bins:
                accum_vol += up_vol
                up_idx += 1
            elif down_idx >= 0:
                accum_vol += down_vol
                down_idx -= 1
            elif up_idx < num_bins:
                accum_vol += up_vol
                up_idx += 1
            else:
                break

        vah_idx = min(num_bins - 1, max(0, up_idx - 1))
        val_idx = max(0, min(num_bins - 1, down_idx + 1))

        poc_arr[i] = poc_price
        vah_arr[i] = bin_centers[vah_idx]
        val_arr[i] = bin_centers[val_idx]

    df['POC'] = pd.Series(poc_arr, index=df.index).ffill().bfill()
    df['VAH'] = pd.Series(vah_arr, index=df.index).ffill().bfill()
    df['VAL'] = pd.Series(val_arr, index=df.index).ffill().bfill()
    return df


def _draw_candles_and_vp(ax, plot_df: pd.DataFrame):
    """ローソク足と VAH/VAL/POC 履歴バンドを描画する共通描画ロジック"""
    x_indices = np.arange(len(plot_df))
    width = 0.6

    # 1. Volume Profile 履歴バンド (Value Area 塗りつぶし & ライン)
    has_vp = all(col in plot_df.columns for col in ['VAH', 'VAL', 'POC'])
    if has_vp:
        vah_s = plot_df['VAH'].values
        val_s = plot_df['VAL'].values
        poc_s = plot_df['POC'].values

        # Value Area ゾーン（半透明塗りつぶし）
        ax.fill_between(x_indices, val_s, vah_s, color="#ff9800", alpha=0.10, label="Value Area (VAH-VAL)", zorder=1)

        # 履歴ライン
        ax.plot(x_indices, vah_s, color="#ff9800", linestyle="--", linewidth=1.4, alpha=0.85, label="VAH Band (Resistance)", zorder=2)
        ax.plot(x_indices, poc_s, color="#29b6f6", linestyle="-.", linewidth=1.3, alpha=0.85, label="POC Band (Value Center)", zorder=2)
        ax.plot(x_indices, val_s, color="#ab47bc", linestyle="--", linewidth=1.4, alpha=0.85, label="VAL Band (Support/SL)", zorder=2)

    # 2. ローソク足の描画
    for i, row in plot_df.iterrows():
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        color = "#26a69a" if cl >= op else "#ef5350"

        # ヒゲ
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.2, zorder=3)
        # 実体
        body_bottom = min(op, cl)
        body_height = abs(cl - op)
        if body_height == 0:
            body_height = (hi - lo) * 0.01 if hi != lo else 0.0001
        rect = plt.Rectangle((i - width / 2, body_bottom), width, body_height,
                             facecolor=color, edgecolor=color, zorder=4)
        ax.add_patch(rect)

    return x_indices


def _setup_time_axis(ax, plot_df: pd.DataFrame, x_indices, right_margin_bars: int = 7):
    """X軸の目盛りと日時フォーマット設定（右側余白対応）"""
    n_bars = len(plot_df)
    step = max(1, n_bars // 8)
    tick_indices = list(range(0, n_bars, step))
    last_idx = n_bars - 1
    if last_idx not in tick_indices:
        tick_indices.append(last_idx)

    tick_labels = [plot_df.iloc[idx]["dt"].strftime("%m/%d %H:%M") for idx in tick_indices]
    ax.set_xticks(tick_indices)
    ax.set_xticklabels(tick_labels, rotation=20, ha="right", color="#b2b5be", fontsize=8)
    ax.tick_params(axis="y", colors="#b2b5be", labelsize=8)
    ax.grid(True, linestyle=":", color="#363c4e", alpha=0.6)
    
    # 右側に余白を設定してローソク足とラベルの重なりを完全防止
    ax.set_xlim(-0.8, n_bars - 1 + right_margin_bars)


def plot_entry_chart(
    symbol: str,
    df: pd.DataFrame,
    entry_price: float,
    vah: float = None,
    val: float = None,
    poc: float = None,
    strategy_name: str = "",
    whale_signal: str = "",
    whale_flow: float = 0.0,
    lookback_bars: int = 48,
) -> Path:
    """新規エントリー時点のローソク足とVolume Profile(VAH/VAL/POC)履歴バンド、エントリーポイントを描画して保存する"""
    if df is None or df.empty:
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DATA_DIR / f"entry_chart_{symbol}.png"

    # Volume Profile カラムがなければ計算付与
    work_df = df.copy()
    if not all(col in work_df.columns for col in ['VAH', 'VAL', 'POC']):
        work_df = compute_volume_profile_bands(work_df, period=lookback_bars)

    plot_df = work_df.tail(lookback_bars).reset_index(drop=True)
    if len(plot_df) == 0:
        return None

    # 日時処理
    if "timestamp" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["timestamp"])
    elif "date" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["date"])
    else:
        plot_df["dt"] = pd.date_range(end=pd.Timestamp.now(), periods=len(plot_df), freq="1h")

    if plot_df["dt"].dt.tz is None:
        plot_df["dt"] = plot_df["dt"].dt.tz_localize("UTC").dt.tz_convert("Asia/Tokyo")
    else:
        plot_df["dt"] = plot_df["dt"].dt.tz_convert("Asia/Tokyo")

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=150)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#1e222d")

    x_indices = _draw_candles_and_vp(ax, plot_df)

    # エントリーポイントの描画（最新バー）
    last_idx = len(plot_df) - 1
    ax.scatter([last_idx], [entry_price], color="#00e676", s=180, marker="^", edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"Entry Point @ ${entry_price:.4f}")

    # 吹き出しアノテーション（右側余白スペースに配置してローソク足を一切隠さない）
    price_str = f"${entry_price:.6f}" if entry_price < 1 else f"${entry_price:.4f}"
    ax.annotate(
        f" ▲ LONG ENTRY\n {price_str}",
        xy=(last_idx, entry_price),
        xytext=(last_idx + 1.2, entry_price),
        arrowprops=dict(
            arrowstyle="->",
            facecolor="#00e676",
            edgecolor="#00e676",
            lw=1.5,
            shrinkA=3,
            shrinkB=5
        ),
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#1b5e20", edgecolor="#00e676", alpha=0.95),
        fontsize=9,
        color="#ffffff",
        weight="bold",
        verticalalignment="center",
        zorder=7
    )

    _setup_time_axis(ax, plot_df, x_indices, right_margin_bars=8)

    # タイトル
    whale_str = f" | Whale: {whale_signal} ({whale_flow:+,.0f} USD)" if whale_signal else ""
    strat_str = f" | Strat: {strategy_name}" if strategy_name else ""
    title_price_str = f"{entry_price:.6f} USD" if entry_price < 1 else f"{entry_price:.4f} USD"
    
    ax.set_title(
        f"[NEW ENTRY] {symbol} (LONG) - Execution & Volume Profile Bands\nEntry Price: {title_price_str}{strat_str}{whale_str}",
        fontsize=11,
        color="#ffffff",
        weight="bold",
        pad=15
    )

    # Y軸マージンを少し確保
    ax.margins(y=0.08)
    ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=8, labelcolor="#ffffff", framealpha=0.75)
    plt.tight_layout()

    plt.savefig(out_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    plt.close('all')
    import gc
    gc.collect()

    print(f"[Real Trade Tracker] エントリーチャート（履歴バンド付き）生成完了: {out_file}")
    return out_file


def plot_exit_chart(
    symbol: str,
    df: pd.DataFrame,
    exit_price: float,
    entry_price: float = None,
    pnl: float = 0.0,
    pnl_pct: float = None,
    exit_reason: str = "",
    side: str = "LONG",
    whale_signal: str = "",
    lookback_bars: int = 48,
) -> Path:
    """決済(Exit/Close)時点のローソク足、Volume Profile履歴バンド、エントリー＆エグジット軌跡を描画して保存する"""
    if df is None or df.empty:
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DATA_DIR / f"exit_chart_{symbol}.png"

    # Volume Profile カラムがなければ計算付与
    work_df = df.copy()
    if not all(col in work_df.columns for col in ['VAH', 'VAL', 'POC']):
        work_df = compute_volume_profile_bands(work_df, period=lookback_bars)

    plot_df = work_df.tail(lookback_bars).reset_index(drop=True)
    if len(plot_df) == 0:
        return None

    # 日時処理
    if "timestamp" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["timestamp"])
    elif "date" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["date"])
    else:
        plot_df["dt"] = pd.date_range(end=pd.Timestamp.now(), periods=len(plot_df), freq="1h")

    if plot_df["dt"].dt.tz is None:
        plot_df["dt"] = plot_df["dt"].dt.tz_localize("UTC").dt.tz_convert("Asia/Tokyo")
    else:
        plot_df["dt"] = plot_df["dt"].dt.tz_convert("Asia/Tokyo")

    # エントリー価格と正確なエントリー日時の検索 (指定されていない場合、または正確なバー位置特定)
    trades = load_real_trades()
    entry_trade = None
    for t in reversed(trades):
        if t.get("symbol") == symbol and t.get("action") == "ENTRY":
            entry_trade = t
            break

    if (entry_price is None or entry_price <= 0) and entry_trade:
        entry_price = float(entry_trade.get("price", 0))

    if entry_price is None or entry_price <= 0:
        entry_price = float(plot_df["open"].iloc[max(0, len(plot_df) - 5)])

    # 正確なエントリーバーインデックスの特定
    exit_idx = len(plot_df) - 1
    entry_idx = max(0, exit_idx - 4)  # デフォルトフォールバック

    if entry_trade and "timestamp" in entry_trade:
        try:
            entry_dt = pd.to_datetime(entry_trade["timestamp"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.tz_localize("UTC").tz_convert("Asia/Tokyo")
            else:
                entry_dt = entry_dt.tz_convert("Asia/Tokyo")
            
            # 最も時刻が近いバーを検索
            time_diffs = (plot_df["dt"] - entry_dt).abs()
            nearest_idx = int(time_diffs.argmin())
            if nearest_idx < exit_idx:
                entry_idx = nearest_idx
        except Exception:
            pass

    # 収益率の計算
    if pnl_pct is None and entry_price > 0:
        if side.upper() == "LONG":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=150)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#1e222d")

    x_indices = _draw_candles_and_vp(ax, plot_df)

    # エントリーマーカー
    ax.scatter([entry_idx], [entry_price], color="#00e676", s=160, marker="^", edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"Entry @ ${entry_price:.4f}")
    # エグジットマーカー
    exit_marker_color = "#26a69a" if pnl >= 0 else "#ef5350"
    ax.scatter([exit_idx], [exit_price], color=exit_marker_color, s=160, marker="v", edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"Exit @ ${exit_price:.4f}")

    # トレード軌跡（破線ライン）
    line_color = "#26a69a" if pnl >= 0 else "#ef5350"
    ax.plot([entry_idx, exit_idx], [entry_price, exit_price], color=line_color, linestyle=":", linewidth=2.0, alpha=0.9, zorder=5)

    # 決済吹き出しアノテーション（右側余白スペースに配置し、過去のローソク足を一切隠さない）
    pnl_sign = "+" if pnl > 0 else ""
    pnl_str = f"PnL: {pnl_sign}${pnl:.2f} ({pnl_pct:+.2f}%)" if pnl_pct is not None else f"PnL: {pnl_sign}${pnl:.2f}"
    reason_str = f"\nReason: {exit_reason}" if exit_reason else ""
    
    bg_color = "#1b5e20" if pnl >= 0 else "#b71c1c"
    
    ax.annotate(
        f" ▼ {side} EXIT\n {pnl_str}{reason_str}",
        xy=(exit_idx, exit_price),
        xytext=(exit_idx + 1.2, exit_price),
        arrowprops=dict(
            arrowstyle="->",
            facecolor=exit_marker_color,
            edgecolor=exit_marker_color,
            lw=1.5,
            shrinkA=3,
            shrinkB=5
        ),
        bbox=dict(boxstyle="round,pad=0.5", facecolor=bg_color, edgecolor=exit_marker_color, alpha=0.95),
        fontsize=9,
        color="#ffffff",
        weight="bold",
        verticalalignment="center",
        zorder=7
    )

    _setup_time_axis(ax, plot_df, x_indices, right_margin_bars=8)

    # タイトル
    title_exit_str = f"${exit_price:.6f}" if exit_price < 1 else f"${exit_price:.4f}"
    title_entry_str = f"${entry_price:.6f}" if entry_price < 1 else f"${entry_price:.4f}"
    result_tag = "PROFIT" if pnl >= 0 else "LOSS CUT"
    
    ax.set_title(
        f"[{result_tag}] {symbol} ({side} CLOSE) - Trade Execution Chart\nEntry: {title_entry_str} -> Exit: {title_exit_str} | Realized PnL: {pnl_sign}${pnl:.2f} ({pnl_pct:+.2f}%)",
        fontsize=11,
        color="#ffffff",
        weight="bold",
        pad=15
    )

    # Y軸マージンを少し確保
    ax.margins(y=0.08)
    ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=8, labelcolor="#ffffff", framealpha=0.75)
    plt.tight_layout()

    plt.savefig(out_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    plt.close('all')
    import gc
    gc.collect()

    print(f"[Real Trade Tracker] エグジットチャート（履歴バンド付き）生成完了: {out_file}")
    return out_file


if __name__ == "__main__":
    # テスト動作確認
    print("Testing real_trade_tracker plotting...")
    sample_df = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-18 00:00", periods=48, freq="1h"),
        "open": np.linspace(0.025, 0.027, 48) + np.random.normal(0, 0.0003, 48),
        "high": np.linspace(0.0255, 0.0278, 48) + np.random.normal(0, 0.0003, 48),
        "low": np.linspace(0.0245, 0.0265, 48) + np.random.normal(0, 0.0003, 48),
        "close": np.linspace(0.0252, 0.0272, 48) + np.random.normal(0, 0.0003, 48),
        "volume": np.random.uniform(1000, 50000, 48),
    })
    
    # Entry chart test
    plot_entry_chart("STBL", sample_df, entry_price=0.0268, strategy_name="BREAKOUT", whale_signal="NEUTRAL")
    # Exit chart test
    plot_exit_chart("STBL", sample_df, exit_price=0.0254, entry_price=0.0268, pnl=-1.89, exit_reason="LONG_VP_INITIAL_BREAK")
    # PnL chart test
    plot_real_trading_performance()


