from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

BASE_DIR = Path(r"d:\python_bitcoin\motiobtc\bitbank\bitbank5_46")
sys.path.insert(0, str(BASE_DIR))

# 日本語フォント設定
matplotlib.rcParams["font.sans-serif"] = [
    "Meiryo", "Yu Gothic", "MS Gothic", "Noto Sans CJK JP", "IPAGothic", "DejaVu Sans", "sans-serif"
]
matplotlib.rcParams["axes.unicode_minus"] = False

JST = timezone(timedelta(hours=9))


def fetch_bitbank_candles(pair: str, candle_type: str, years: list[int]) -> pd.DataFrame:
    """BitbankパブリックAPIから複数年のローソク足を取得して結合"""
    records = []
    for y in years:
        url = f"https://public.bitbank.cc/{pair}/candlestick/{candle_type}/{y}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", {}).get("candlestick", [{}])[0].get("ohlcv", [])
                for row in data:
                    # [open, high, low, close, volume, timestamp]
                    records.append({
                        "open": float(row[0]),
                        "high": float(row[1]),
                        "low": float(row[2]),
                        "close": float(row[3]),
                        "volume": float(row[4]),
                        "timestamp": pd.to_datetime(row[5], unit="ms", utc=True).tz_convert(JST)
                    })
        except Exception as e:
            print(f"Error fetching {pair} {candle_type} {y}: {e}")

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def plot_btc_weekly_position(save_path: Path):
    """BTC/JPY の長期保有ポジション (週足 & 直近) を可視化"""
    print("Fetching BTC weekly candles...")
    df_week = fetch_bitbank_candles("btc_jpy", "1week", [2024, 2025, 2026])
    if df_week.empty:
        print("Failed to fetch BTC weekly data.")
        return

    # 最新ticker
    t_resp = requests.get("https://public.bitbank.cc/btc_jpy/ticker", timeout=5).json()
    last_price = float(t_resp.get("data", {}).get("last", 0.0))

    entry_price = 12794387.0
    btc_qty = 0.1434
    eval_val = btc_qty * last_price
    cost_val = btc_qty * entry_price
    pnl_val = eval_val - cost_val
    pnl_pct = (pnl_val / cost_val) * 100.0

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9), gridspec_kw={"height_ratios": [3.0, 1.0]}, sharex=True)
    plt.subplots_adjust(hspace=0.08)

    times = df_week["timestamp"]
    closes = df_week["close"].values
    highs = df_week["high"].values
    lows = df_week["low"].values
    volumes = df_week["volume"].values

    # 移動平均線
    ema12 = pd.Series(closes).ewm(span=12).mean()
    ema26 = pd.Series(closes).ewm(span=26).mean()

    # 1. 上段: 週足チャート
    ax1.plot(times, closes, label="BTC/JPY 週足終値", color="#ffffff", linewidth=1.5, alpha=0.9)
    ax1.plot(times, ema12, label="EMA 12週 (短期トレンド)", color="#00e5ff", linewidth=1.2, linestyle="--", alpha=0.7)
    ax1.plot(times, ema26, label="EMA 26週 (中期トレンド)", color="#ff9100", linewidth=1.2, linestyle="--", alpha=0.7)

    # 高値・安値のヒゲ幅を塗る
    ax1.fill_between(times, lows, highs, color="#37474f", alpha=0.3)

    # 平均建値ライン (12,794,387円)
    ax1.axhline(entry_price, color="#ffd600", linestyle="-", linewidth=2.0, label=f"平均建値 (確定簿価): {entry_price:,.0f} 円")
    # 現在価格ライン
    ax1.axhline(last_price, color="#00e676", linestyle=":", linewidth=1.8, label=f"現在価格: {last_price:,.0f} 円 ({pnl_pct:+.2f}%)")

    # 含み益/含み損のゾーン表示
    min_x, max_x = times.iloc[0], times.iloc[-1]
    if last_price >= entry_price:
        ax1.axhspan(entry_price, last_price, color="#00e676", alpha=0.12, label="含み益ゾーン")
    else:
        ax1.axhspan(last_price, entry_price, color="#ff1744", alpha=0.12, label="含み損ゾーン")

    # 現在のポジション情報ボックス
    status_text = (
        f"[BTC 長期保有ポジション状況]\n"
        f"  • 保有数量 : {btc_qty:.4f} BTC\n"
        f"  • 平均建値 : {entry_price:,.0f} 円 (1,300万弱)\n"
        f"  • 現在価格 : {last_price:,.0f} 円\n"
        f"  • 評価額   : {eval_val:,.0f} 円\n"
        f"  • 投資元本 : {cost_val:,.0f} 円\n"
        f"  • 含み損益 : {pnl_val:+,.0f} 円 ({pnl_pct:+.2f}%)\n"
        f"  • 運用方針 : ボット売買から隔離・完全長期ホールド"
    )
    box_props = dict(boxstyle="round,pad=0.8", facecolor="#1e293b", edgecolor="#38bdf8", alpha=0.9)
    ax1.text(0.02, 0.95, status_text, transform=ax1.transAxes, fontsize=10.5,
             verticalalignment="top", fontfamily="sans-serif", bbox=box_props, color="#f8fafc", zorder=10)

    # 軸・タイトルの装飾
    title_str = f"【BTC/JPY 週足】長期保有ポジション状況確認チャート | 現在含み損益: {pnl_val:+,.0f} 円 ({pnl_pct:+.2f}%)"
    ax1.set_title(title_str, fontsize=13, fontweight="bold", color="#f8fafc", pad=12)
    ax1.set_ylabel("Price (JPY)", fontsize=11, color="#cbd5e1")
    ax1.grid(True, linestyle=":", alpha=0.4, color="#64748b")
    ax1.legend(loc="lower right", fontsize=9, facecolor="#1e293b", edgecolor="#475569", labelcolor="#f8fafc")
    ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))

    # 2. 下段: 出来高
    ax2.bar(times, volumes, width=5, color="#0288d1", alpha=0.6, label="Weekly Volume")
    ax2.set_ylabel("Volume (BTC)", fontsize=10, color="#cbd5e1")
    ax2.set_xlabel("Date (JST)", fontsize=11, color="#cbd5e1")
    ax2.grid(True, linestyle=":", alpha=0.4, color="#64748b")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    # 背景色のダークテーマ化
    fig.patch.set_facecolor("#0f172a")
    ax1.set_facecolor("#0f172a")
    ax2.set_facecolor("#0f172a")
    ax1.tick_params(colors="#cbd5e1")
    ax2.tick_params(colors="#cbd5e1")

    plt.tight_layout()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=160, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print(f"Saved BTC weekly chart to: {save_path}")


def plot_render_orders_and_position(save_path: Path):
    """RENDER/JPY の日足チャート & 現在配置中の5本指値注文を可視化"""
    print("Fetching RENDER daily candles...")
    df_day = fetch_bitbank_candles("render_jpy", "1day", [2025, 2026])
    if df_day.empty:
        print("Failed to fetch RENDER daily data.")
        return

    # 直近180日分に絞る
    if len(df_day) > 180:
        df_day = df_day.iloc[-180:].reset_index(drop=True)

    t_resp = requests.get("https://public.bitbank.cc/render_jpy/ticker", timeout=5).json()
    last_price = float(t_resp.get("data", {}).get("last", 0.0))

    # 配置中の5本買い指値
    orders = [
        {"price": 280.0, "qty": 1000.0, "val": 280000.0},
        {"price": 270.0, "qty": 1000.0, "val": 270000.0},
        {"price": 260.0, "qty": 1000.0, "val": 260000.0},
        {"price": 250.0, "qty": 1000.0, "val": 250000.0},
        {"price": 240.0, "qty": 1000.0, "val": 240000.0},
    ]
    total_qty = sum(o["qty"] for o in orders)
    total_val = sum(o["val"] for o in orders)
    avg_expected_price = total_val / total_qty  # 260.0円

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9), gridspec_kw={"height_ratios": [3.0, 1.0]}, sharex=True)
    plt.subplots_adjust(hspace=0.08)

    times = df_day["timestamp"]
    closes = df_day["close"].values
    highs = df_day["high"].values
    lows = df_day["low"].values
    volumes = df_day["volume"].values

    # 移動平均線
    sma25 = pd.Series(closes).rolling(25).mean()
    sma75 = pd.Series(closes).rolling(75).mean()

    # 1. 上段: 日足チャート
    ax1.plot(times, closes, label="RENDER/JPY 日足終値", color="#ffffff", linewidth=1.5, alpha=0.9)
    ax1.plot(times, sma25, label="25日移動平均線", color="#ffca28", linewidth=1.2, linestyle="--", alpha=0.7)
    ax1.plot(times, sma75, label="75日移動平均線", color="#ec407a", linewidth=1.2, linestyle="--", alpha=0.7)
    ax1.fill_between(times, lows, highs, color="#37474f", alpha=0.3)

    # 現在価格ライン
    ax1.axhline(last_price, color="#00e5ff", linestyle="-", linewidth=2.0, label=f"現在価格: {last_price:,.1f} 円")

    # 指値買いゾーンのハイライト
    ax1.axhspan(240.0, 280.0, color="#10b981", alpha=0.15, label="指値配置ゾーン (240〜280円)")

    # 指値注文ライン (各1000 RENDER)
    colors = ["#34d399", "#10b981", "#059669", "#047857", "#065f46"]
    for i, o in enumerate(orders):
        p = o["price"]
        ax1.axhline(p, color=colors[i], linestyle="--", linewidth=1.2, alpha=0.9)
        # ラベル
        ax1.text(times.iloc[-1] + timedelta(days=2), p, f" 買い指値: {p:.0f}円 (1,000 RENDER)",
                 color=colors[i], verticalalignment="center", fontsize=8.5, fontweight="bold")

    # 全約定時の平均建値 (260円)
    ax1.axhline(avg_expected_price, color="#fbbf24", linestyle="-.", linewidth=1.8,
                label=f"全約定時 平均建値: {avg_expected_price:.1f} 円 (5,000 RENDER)")

    # 情報サマリーボックス
    order_info_text = (
        f"[RENDER 12月末向け長期ポジション構築状況]\n"
        f"  • 現在価格     : {last_price:,.1f} 円\n"
        f"  • 発注中指値   : 280, 270, 260, 250, 240 円 (各1,000 RENDER)\n"
        f"  • 目標総数量   : {total_qty:,.0f} RENDER (5分割エントリー)\n"
        f"  • 拘束JPY総額  : {total_val:,.0f} JPY (約130万円)\n"
        f"  • 全約定時建値 : {avg_expected_price:,.1f} 円 (現値比 -15.1%の押し目)\n"
        f"  • 最低指値     : 240 円 (現値比 -21.6%の底値サポート)\n"
        f"  • 運用目標     : 12月末までのスイング〜中長期ホールド"
    )
    box_props = dict(boxstyle="round,pad=0.8", facecolor="#1e293b", edgecolor="#10b981", alpha=0.9)
    ax1.text(0.02, 0.95, order_info_text, transform=ax1.transAxes, fontsize=10.5,
             verticalalignment="top", fontfamily="sans-serif", bbox=box_props, color="#f8fafc", zorder=10)

    title_str = f"【RENDER/JPY 日足】長期ポジション構築・指値発注チェックチャート (現在値: {last_price:.1f}円)"
    ax1.set_title(title_str, fontsize=13, fontweight="bold", color="#f8fafc", pad=12)
    ax1.set_ylabel("Price (JPY)", fontsize=11, color="#cbd5e1")
    ax1.grid(True, linestyle=":", alpha=0.4, color="#64748b")
    ax1.legend(loc="upper right", fontsize=9, facecolor="#1e293b", edgecolor="#475569", labelcolor="#f8fafc")

    # 2. 下段: 出来高
    ax2.bar(times, volumes, width=0.8, color="#00bcd4", alpha=0.6, label="Daily Volume")
    ax2.set_ylabel("Volume", fontsize=10, color="#cbd5e1")
    ax2.set_xlabel("Date (JST)", fontsize=11, color="#cbd5e1")
    ax2.grid(True, linestyle=":", alpha=0.4, color="#64748b")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))

    # 背景色のダークテーマ化
    fig.patch.set_facecolor("#0f172a")
    ax1.set_facecolor("#0f172a")
    ax2.set_facecolor("#0f172a")
    ax1.tick_params(colors="#cbd5e1")
    ax2.tick_params(colors="#cbd5e1")

    plt.tight_layout()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=160, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print(f"Saved RENDER chart to: {save_path}")


def main():
    plots_dir = BASE_DIR / "Data" / "plots"
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    btc_chart_path = plots_dir / f"btc_long_term_weekly_{ts}.png"
    render_chart_path = plots_dir / f"render_position_check_{ts}.png"

    plot_btc_weekly_position(btc_chart_path)
    plot_render_orders_and_position(render_chart_path)


if __name__ == "__main__":
    main()
