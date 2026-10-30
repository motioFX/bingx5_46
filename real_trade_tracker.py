from __future__ import annotations
"""
real_trade_tracker.py
BingX USDT無期限先物 実トレード記録・スライディング10日間パフォーマンス（Clean版 / Detail版 2枚）・
月次確定レポート・足種完全統一エントリー＆エグジットチャート生成モジュール
(PROJECT_RULES.md 準拠)
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as _fm

def _configure_japanese_font():
    for _font_name in [
        'Noto Sans CJK JP', 'IPAexGothic', 'IPAGothic',
        'TakaoPGothic', 'TakaoGothic', 'VL PGothic', 'VL Gothic',
        'Hiragino Sans', 'Meiryo', 'Yu Gothic', 'Droid Sans Fallback', 'DejaVu Sans'
    ]:
        if any(f.name == _font_name for f in _fm.fontManager.ttflist):
            plt.rcParams['font.family'] = _font_name
            break
    plt.rcParams['axes.unicode_minus'] = False

_configure_japanese_font()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
TRADES_FILE = DATA_DIR / "real_trades_history.json"
TRADES_CSV = DATA_DIR / "trades_history.csv"
DAILY_PERF_CSV = DATA_DIR / "daily_performance.csv"
CHART_FILE = DATA_DIR / "real_trading_performance.png"
CHART_FILE_CLEAN = DATA_DIR / "real_trading_performance_clean.png"
CHART_FILE_DETAIL = DATA_DIR / "real_trading_performance_detail.png"
MONTHLY_STATE_FILE = DATA_DIR / "last_monthly_report_sent.json"
CHARTS_DIR = DATA_DIR / "charts"


def load_real_trades():
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def sync_trades_to_csv():
    """real_trades_history.json から trades_history.csv および daily_performance.csv を生成/同期する"""
    trades = load_real_trades()
    if not trades:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df = pd.DataFrame(trades)
        if "timestamp" in df.columns:
            dt_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df["dt_jst"] = dt_series.dt.tz_convert("Asia/Tokyo").dt.strftime("%Y-%m-%d %H:%M:%S")
            df["date_jst"] = dt_series.dt.tz_convert("Asia/Tokyo").dt.strftime("%Y-%m-%d")

        if "price" in df.columns and "qty" in df.columns:
            df["notional_usd"] = (df["price"] * df["qty"]).round(2)

        cols = ["timestamp", "dt_jst", "symbol", "action", "side", "price", "qty", "notional_usd", "pnl", "note"]
        cols = [c for c in cols if c in df.columns]
        df[cols].to_csv(TRADES_CSV, index=False, encoding="utf-8")

        # 日次パフォーマンス集計 (JST日付ベース)
        close_df = df[df["action"] == "CLOSE"].copy()
        if not close_df.empty and "date_jst" in close_df.columns:
            daily_records = []
            cum_pnl = 0.0
            for date_str, group in close_df.groupby("date_jst", sort=True):
                n_trades = len(group)
                wins = int((group["pnl"] > 0).sum())
                losses = int((group["pnl"] <= 0).sum())
                day_pnl = float(group["pnl"].sum())
                cum_pnl += day_pnl
                win_rate = (wins / n_trades * 100.0) if n_trades > 0 else 0.0
                daily_records.append({
                    "date": date_str,
                    "closed_trades": n_trades,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": round(win_rate, 1),
                    "daily_pnl_usd": round(day_pnl, 4),
                    "cumulative_pnl_usd": round(cum_pnl, 4)
                })
            if daily_records:
                pd.DataFrame(daily_records).to_csv(DAILY_PERF_CSV, index=False, encoding="utf-8")
    except Exception as e:
        print(f"[Real Trade Tracker] CSV同期エラー: {e}")


def record_real_trade(symbol: str, side: str, action: str, price: float, qty: float, pnl: float = 0.0, note: str = ""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_real_trades()

    trade_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side.upper(),
        "action": action.upper(),  # ENTRY, SCALE_IN, ADD_ENTRY, CLOSE
        "price": float(price),
        "qty": float(qty),
        "pnl": float(pnl),
        "note": note
    }
    trades.append(trade_entry)

    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

    print(f"[Real Trade Tracker] 記録完了: {symbol} {action} {side} @ ${price:.4f} (PnL: ${pnl:+.2f} USDT)")
    sync_trades_to_csv()
    plot_real_trading_performance()
    return trade_entry


def get_real_trading_summary(days: Optional[int] = None, current_balance: Optional[float] = None) -> Dict[str, Any]:
    """実トレード履歴の統計サマリーを取得（days指定時は直近days日間スライディング、未指定時は全期間）"""
    trades = load_real_trades()
    closes = [t for t in trades if t.get("action") == "CLOSE"]

    if days is not None and days > 0 and closes:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        filtered_closes = []
        for t in closes:
            ts_val = t.get("timestamp")
            if ts_val:
                try:
                    t_dt = pd.to_datetime(ts_val, utc=True)
                    if t_dt >= cutoff_dt:
                        filtered_closes.append(t)
                except Exception:
                    filtered_closes.append(t)
            else:
                filtered_closes.append(t)
        closes = filtered_closes
    pnls = [float(t.get("pnl", 0.0)) for t in closes]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    ties = [x for x in pnls if x == 0]
    total_pnl = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    wr = (len(wins) / len(closes) * 100.0) if closes else 0.0

    return {
        "total_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "ties": len(ties),
        "win_rate": round(wr, 1),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(total_pnl, 2),
        "profit_factor": round(pf, 2),
        "current_balance": round(float(current_balance), 2) if current_balance is not None else None,
    }


def plot_real_trading_performance(days: int = 10, current_balance: Optional[float] = None) -> Tuple[Optional[Path], Optional[Path]]:
    """
    直近 days 日間（デフォルト10日間スライディングウィンドウ）の累積損益パフォーマンスチャートを生成。
    Twitter/SNS共有用の「吹き出しなし (Clean版)」と、
    検証用の「吹き出しあり (Detail版: 直近15件千鳥配置)」の2種類を生成して返却する。
    戻り値: (clean_file_path, detail_file_path)
    """
    trades = load_real_trades()
    if not trades:
        return None, None

    df = pd.DataFrame(trades)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        return None, None

    df["timestamp_utc"] = df["timestamp"]
    df["dt_jst"] = df["timestamp_utc"].dt.tz_convert("Asia/Tokyo")

    now_utc = datetime.now(timezone.utc)
    if "note" in df.columns:
        df = df[~df["note"].astype(str).str.contains("test_exit", case=False, na=False)].copy()

    if days is not None and days > 0 and len(df) > 1:
        total_days_span = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).total_seconds() / 86400.0
        if total_days_span >= (days - 1):
            cutoff = now_utc - pd.Timedelta(days=days)
            df_sub = df[df["timestamp_utc"] >= cutoff].copy()
            if not df_sub.empty and len(df_sub) >= 2:
                df = df_sub.reset_index(drop=True)

    # 有効なトレード（pnl != 0）のみ抽出して累積PnLを計算
    clean_df = df[df["pnl"] != 0].copy().reset_index(drop=True)
    if clean_df.empty:
        clean_df = df[df["action"] == "CLOSE"].copy().reset_index(drop=True)
    if clean_df.empty:
        clean_df = df.copy().reset_index(drop=True)

    clean_df["cum_pnl"] = clean_df["pnl"].cumsum()
    n_close = len(clean_df)

    min_pnl = float(clean_df["cum_pnl"].min()) if not clean_df.empty else 0.0
    max_pnl = float(clean_df["cum_pnl"].max()) if not clean_df.empty else 0.0
    pnl_range = max(1.0, max_pnl - min_pnl)

    span_sec = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).total_seconds() if len(df) > 1 else 0.0
    period_title = f"Last {days} Days" if span_sec >= (days - 1) * 86400 else "All Time (Accumulating)"

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------
    # 1. 吹き出しなし (Clean版 / Twitter・SNS共有用)
    # -------------------------------------------------------------
    fig_c, ax_c = plt.subplots(figsize=(11, 5.5), dpi=150)
    fig_c.patch.set_facecolor("#131722")
    ax_c.set_facecolor("#1e222d")

    ax_c.plot(clean_df["dt_jst"], clean_df["cum_pnl"], marker='o', markersize=6, color='#29b6f6', linewidth=2.5, label="Cumulative Real PnL (USDT)", zorder=3)
    ax_c.fill_between(clean_df["dt_jst"], clean_df["cum_pnl"], 0, color='#29b6f6', alpha=0.12, zorder=2)
    ax_c.axhline(0, color="#787b86", linestyle="--", alpha=0.5, zorder=2)

    ax_c.set_ylim(min_pnl - pnl_range * 0.15, max_pnl + pnl_range * 0.20)
    ax_c.margins(x=0.06)

    ax_c.set_title(f"BingX VST/Real Trading Performance ({period_title} - Clean)", fontsize=13, color="#ffffff", pad=15, weight="bold")
    ax_c.set_xlabel("Time (JST)", fontsize=10, color="#b2b5be")
    ax_c.set_ylabel("Realized Cumulative PnL (USDT)", fontsize=10, color="#b2b5be")
    ax_c.tick_params(colors="#b2b5be", labelsize=9)
    ax_c.grid(True, linestyle=':', color="#363c4e", alpha=0.7)
    ax_c.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=9, labelcolor="#ffffff")
    plt.tight_layout()

    plt.savefig(CHART_FILE_CLEAN, dpi=150, facecolor=fig_c.get_facecolor(), edgecolor="none")
    plt.savefig(CHART_FILE, dpi=150, facecolor=fig_c.get_facecolor(), edgecolor="none")
    plt.close(fig_c)

    # -------------------------------------------------------------
    # 2. 吹き出しあり (Detail版 / 詳細検証用・千鳥配置)
    # -------------------------------------------------------------
    fig_d, ax_d = plt.subplots(figsize=(11, 5.5), dpi=150)
    fig_d.patch.set_facecolor("#131722")
    ax_d.set_facecolor("#1e222d")

    ax_d.plot(clean_df["dt_jst"], clean_df["cum_pnl"], marker='o', markersize=6, color='#29b6f6', linewidth=2.5, label="Cumulative Real PnL (USDT)", zorder=3)
    ax_d.fill_between(clean_df["dt_jst"], clean_df["cum_pnl"], 0, color='#29b6f6', alpha=0.08, zorder=2)
    ax_d.axhline(0, color="#787b86", linestyle="--", alpha=0.5, zorder=2)

    annot_start_idx = max(0, n_close - 16)
    for i in range(annot_start_idx, n_close):
        row = clean_df.iloc[i]
        color = "#26a69a" if row["pnl"] > 0 else "#ef5350"
        k = i - annot_start_idx
        offset_pattern = [
            (25, 20),
            (25, -22),
            (25, 38),
            (25, -38),
        ]
        ox, oy = offset_pattern[k % 4]
        if i >= n_close - 2:
            ox = -55
            oy = 20 if k % 2 == 0 else -20

        sym_short = str(row["symbol"]).replace("-USDT", "")
        ax_d.annotate(
            f"{sym_short}: {row['pnl']:+.2f}$",
            xy=(row["dt_jst"], row["cum_pnl"]),
            xytext=(ox, oy), textcoords="offset points",
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
            bbox=dict(boxstyle="round,pad=0.22", facecolor="#131722", edgecolor=color, alpha=0.9, lw=1.0),
            zorder=6
        )

    ax_d.set_ylim(min_pnl - pnl_range * 0.22, max_pnl + pnl_range * 0.28)
    ax_d.margins(x=0.08)

    ax_d.set_title(f"BingX VST/Real Trading Performance ({period_title} - Detailed)", fontsize=13, color="#ffffff", pad=15, weight="bold")
    ax_d.set_xlabel("Time (JST)", fontsize=10, color="#b2b5be")
    ax_d.set_ylabel("Realized Cumulative PnL (USDT)", fontsize=10, color="#b2b5be")
    ax_d.tick_params(colors="#b2b5be", labelsize=9)
    ax_d.grid(True, linestyle=':', color="#363c4e", alpha=0.7)
    ax_d.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=9, labelcolor="#ffffff")
    plt.tight_layout()

    plt.savefig(CHART_FILE_DETAIL, dpi=150, facecolor=fig_d.get_facecolor(), edgecolor="none")
    plt.close(fig_d)
    plt.close('all')

    import gc
    gc.collect()
    print(f"[Real Trade Tracker] パフォーマンスチャート更新完了:\n  ・Clean版: {CHART_FILE_CLEAN}\n  ・Detail版: {CHART_FILE_DETAIL}")
    return CHART_FILE_CLEAN, CHART_FILE_DETAIL


def get_recent_performance_summary(days: int = 10) -> dict:
    """
    直近 days 日間（デフォルト 10日間）のクローズ済みトレードを集計する。
    有効トレード数（勝敗が決まった取引）ベースで有効勝率を算出。
    """
    trades = load_real_trades()
    if not trades:
        return {
            "status": "empty",
            "message": "トレード履歴がまだ記録されていません。"
        }

    df = pd.DataFrame(trades)
    if "timestamp" not in df.columns or "action" not in df.columns or "pnl" not in df.columns:
        return {
            "status": "empty",
            "message": "トレード履歴に必要なカラムが存在しません。"
        }

    df["timestamp_utc"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp_utc"])
    df["dt_jst"] = df["timestamp_utc"].dt.tz_convert("Asia/Tokyo")

    close_df = df[df["action"] == "CLOSE"].copy()
    if close_df.empty:
        return {
            "status": "no_closed_trades",
            "message": "決済済みのトレード履歴はまだありません（ポジション保有中または新規稼働中）。"
        }

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - pd.Timedelta(days=days)

    total_span_sec = (close_df["timestamp_utc"].max() - close_df["timestamp_utc"].min()).total_seconds()
    total_span_days = total_span_sec / 86400.0
    is_capped = total_span_days >= (days - 1)

    if is_capped:
        period_df = close_df[close_df["timestamp_utc"] >= cutoff].copy()
        if period_df.empty:
            period_df = close_df.tail(10).copy()
        period_label = f"直近 {days} 日間"
    else:
        period_df = close_df.copy()
        if total_span_days < 1.0:
            hours = max(0.1, total_span_sec / 3600.0)
            period_label = f"全期間 (蓄積: 約{hours:.1f}時間)"
        else:
            period_label = f"全期間 (蓄積: 約{total_span_days:.1f}日)"

    start_jst = period_df["dt_jst"].min().strftime("%m/%d %H:%M")
    end_jst = period_df["dt_jst"].max().strftime("%m/%d %H:%M")

    wins = int((period_df["pnl"] > 0).sum())
    losses = int((period_df["pnl"] < 0).sum())
    evens = int((period_df["pnl"] == 0).sum())
    n_valid = wins + losses
    win_rate = (wins / n_valid * 100.0) if n_valid > 0 else 0.0

    total_pnl = float(period_df["pnl"].sum())
    gross_profit = float(period_df[period_df["pnl"] > 0]["pnl"].sum())
    gross_loss = abs(float(period_df[period_df["pnl"] < 0]["pnl"].sum()))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 1.0)

    avg_win = (gross_profit / wins) if wins > 0 else 0.0
    avg_loss = (gross_loss / losses) if losses > 0 else 0.0

    by_symbol = []
    for sym, grp in period_df.groupby("symbol"):
        s_pnl = float(grp["pnl"].sum())
        s_valid = grp[grp["pnl"] != 0]
        s_cnt = len(s_valid) if not s_valid.empty else len(grp)
        s_wins = int((grp["pnl"] > 0).sum())
        s_losses = int((grp["pnl"] < 0).sum())
        by_symbol.append({
            "symbol": str(sym).replace("-USDT", ""),
            "pnl": s_pnl,
            "trades": s_cnt,
            "wins": s_wins,
            "losses": s_losses
        })
    by_symbol.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "status": "ok",
        "period_label": period_label,
        "start_jst": start_jst,
        "end_jst": end_jst,
        "total_trades": n_valid,
        "raw_trades": len(period_df),
        "wins": wins,
        "losses": losses,
        "evens": evens,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "by_symbol": by_symbol
    }


def send_periodic_pnl_report_discord(days: int = 10, current_balance: Optional[float] = None) -> None:
    """
    8時間ごと定期銘柄選定時（または起動時）に呼ばれる、
    VST/実トレードPnLサマリーと累積PnLチャート（Clean版・Detail版の2枚）のDiscord送信
    """
    try:
        from bingx5_46_3logic import discord
    except Exception:
        discord = None

    summary = get_recent_performance_summary(days=days)
    bal_line = f"🏦 現在口座残高: ${current_balance:,.2f} USDT\n" if (current_balance is not None and current_balance > 0) else ""
    if summary.get("status") != "ok":
        msg = f"📊 **【BingX VST/実トレード損益レポート】**\n{bal_line}{summary.get('message', 'データなし')}"
        if discord:
            discord.print_log(msg, force_send=True)
        else:
            print(msg)
        return

    pnl_sign = "+" if summary["total_pnl"] >= 0 else ""
    pf_str = f"{summary['profit_factor']:.2f}" if summary["profit_factor"] < 900 else "INF (無敗)"

    lines = [
        f"📊 **【BingX VST/実トレード損益レポート ({summary['period_label']})】**",
        "```text",
    ]
    if current_balance is not None and current_balance > 0:
        lines.append(f"🏦 口座証拠金: ${current_balance:,.2f} USDT")
    lines.extend([
        f"⏱ 集計期間: {summary['start_jst']} 〜 {summary['end_jst']} JST",
        f"🎯 総取引数: {summary['total_trades']} 件 ({summary['wins']}勝 {summary['losses']}敗 / 勝率 {summary['win_rate']:.1f}%)",
        f"💰 確定損益: {pnl_sign}${summary['total_pnl']:.2f} USDT",
        f"📈 損益因子 (PF): {pf_str} (総利益: +${summary['gross_profit']:.2f} / 総損失: -${summary['gross_loss']:.2f})",
        f"⚖ 平均利益: +${summary['avg_win']:.2f} / 平均損失: -${summary['avg_loss']:.2f}",
        "────────────────────────────────────────",
        "📌 銘柄別損益内訳:"
    ])

    for item in summary["by_symbol"][:10]:
        s_sign = "+" if item["pnl"] >= 0 else ""
        lines.append(f"  ・{item['symbol']:<7}: {s_sign}${item['pnl']:>6.2f} ({item['wins']}勝{item['losses']}敗 / 計{item['trades']}回)")

    if len(summary["by_symbol"]) > 10:
        lines.append(f"  ... 他 {len(summary['by_symbol']) - 10} 銘柄")

    lines.append("```")
    report_text = "\n".join(lines)

    print(report_text)
    if discord:
        discord.print_log(report_text, force_send=True)

    # 累積PnLチャート画像生成 & Discord送信（Clean版 ＋ Detail版の2枚）
    try:
        clean_file, detail_file = plot_real_trading_performance(days=days, current_balance=current_balance)
        if discord:
            if clean_file and clean_file.exists():
                discord.send_file(
                    clean_file,
                    f"📈 【BingX 実運用実績】累積損益パフォーマンス (直近{days}日 - Clean版 / SNS共有用)"
                )
            if detail_file and detail_file.exists():
                discord.send_file(
                    detail_file,
                    f"🔍 【BingX 実運用実績】累積損益パフォーマンス (直近{days}日 - Detail版 / 検証用)"
                )
    except Exception as chart_err:
        print(f"[Real Trade Tracker] チャート送信エラー: {chart_err}")


def print_real_trading_table(days: int = 10, current_balance: Optional[float] = None) -> str:
    """
    コンソールログ（bot_output.log）に視認性の高いアスキーテーブル形式で実運用損益表を出力する
    """
    summary = get_recent_performance_summary(days=days)
    if summary.get("status") != "ok":
        msg = f"[実運用損益表] {summary.get('message', 'データなし')}"
        print(msg)
        return msg

    pnl_sign = "+" if summary["total_pnl"] >= 0 else ""
    pf_str = f"{summary['profit_factor']:.2f}" if summary["profit_factor"] < 900 else "INF (無敗)"
    bal_str = f"🏦 口座証拠金  : ${current_balance:,.2f} USDT" if (current_balance is not None and current_balance > 0) else ""

    lines = [
        "================================================================================",
        f"📊 【BingX 実運用トレード損益表 ({summary['period_label']} / API直接同期SSoT)】",
        "================================================================================",
    ]
    if bal_str:
        lines.append(f"  {bal_str}")
    lines.extend([
        f"  ⏱ 集計期間    : {summary['start_jst']} 〜 {summary['end_jst']} JST",
        f"  🎯 総決済回数  : {summary['total_trades']} 回 ({summary['wins']}勝 {summary['losses']}敗 / 勝率 {summary['win_rate']:.1f}%)",
        f"  💰 確定純損益  : {pnl_sign}${summary['total_pnl']:.2f} USDT (総利益: +${summary['gross_profit']:.2f} / 総損失: -${summary['gross_loss']:.2f})",
        f"  📈 損益因子(PF): {pf_str}",
        f"  ⚖ 平均損益    : 平均利益 +${summary['avg_win']:.2f} / 平均損失 -${summary['avg_loss']:.2f}",
        "--------------------------------------------------------------------------------",
        "  [銘柄別 実績内訳]"
    ])

    for item in summary["by_symbol"][:15]:
        s_sign = "+" if item["pnl"] >= 0 else ""
        lines.append(f"    ・{item['symbol']:<7}: {s_sign}${item['pnl']:>6.2f} ({item['wins']}勝 {item['losses']}敗 / 計 {item['trades']}回)")

    if len(summary["by_symbol"]) > 15:
        lines.append(f"    ... 他 {len(summary['by_symbol']) - 15} 銘柄")

    lines.append("================================================================================")
    table_text = "\n".join(lines)
    print(table_text)
    return table_text


async def sync_real_trades_from_bingx_api(mode: str = 'demo') -> int:
    """
    BingX 先物 API (/openApi/swap/v2/user/income?incomeType=REALIZED_PNL) から
    取引所で確定した実現損益（REALIZED_PNL）履歴を直接取得し、
    手動決済・外部決済・ボット決済の全レコードを real_trades_history.json と自動同期する。
    戻り値: 新規追加または更新された件数
    """
    try:
        from bingx5_46_2api import RestAPI_url, apis_bingx, _async_bingx_request, is_air
        cred_key = 'bingx_demo' if mode in ('paper', 'demo', 'testnet') else 'bingx'
        creds = apis_bingx.get(cred_key) or apis_bingx.get('bingx') or {}
        api_key = creds.get("api_key", "")
        secret_key = creds.get("secret_key", "")
        if not api_key or api_key.startswith("YOUR_") or is_air:
            print("[PnL Sync] APIキー未設定またはAIRモードのためスキップします。")
            return 0
        base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')

        res = await _async_bingx_request(
            "GET", base_url, "/openApi/swap/v2/user/income",
            api_key, secret_key,
            params={"incomeType": "REALIZED_PNL", "limit": 100},
            timeout=10
        )
        if not res or res.get("code") != 0:
            print(f"[PnL Sync] 取引所からの実現損益取得に失敗しました: {res}")
            return 0

        data = res.get("data", [])
        if not data:
            return 0

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        trades = load_real_trades()

        existing_tran_ids = {t.get("tranId") for t in trades if t.get("tranId")}

        close_records = []
        for idx, t in enumerate(trades):
            if t.get("action") == "CLOSE":
                ts = t.get("timestamp")
                try:
                    sec = pd.to_datetime(ts, utc=True).timestamp()
                except Exception:
                    sec = 0.0
                close_records.append((idx, t.get("symbol"), sec, float(t.get("pnl", 0.0))))

        new_count = 0
        updated_count = 0

        for item in reversed(data):
            sym = item.get("symbol")
            inc = float(item.get("income", 0.0))
            t_ms = item.get("time", 0)
            tran_id = item.get("tranId")
            trade_id = item.get("tradeId")
            info = item.get("info", "Close Long")

            t_sec = t_ms / 1000.0
            dt_iso = datetime.fromtimestamp(t_sec, timezone.utc).isoformat()

            if tran_id and tran_id in existing_tran_ids:
                continue

            matched_idx = None
            for idx, c_sym, c_sec, c_pnl in close_records:
                if c_sym == sym and abs(c_sec - t_sec) <= 180 and abs(c_pnl - inc) <= 0.05:
                    matched_idx = idx
                    break

            if matched_idx is not None:
                trades[matched_idx]["tranId"] = tran_id
                trades[matched_idx]["tradeId"] = trade_id
                trades[matched_idx]["pnl"] = inc
                if tran_id:
                    existing_tran_ids.add(tran_id)
                updated_count += 1
            else:
                side_detected = "SHORT" if ("short" in str(info).lower()) else "LONG"
                new_entry = {
                    "timestamp": dt_iso,
                    "symbol": sym,
                    "side": side_detected,
                    "action": "CLOSE",
                    "price": 0.0,
                    "qty": 0.0,
                    "pnl": inc,
                    "tranId": tran_id,
                    "tradeId": trade_id,
                    "note": f"取引所API直接取得決済 ({info}: 手動損切り/外部決済自動同期)"
                }
                trades.append(new_entry)
                if tran_id:
                    existing_tran_ids.add(tran_id)
                new_count += 1
                close_records.append((len(trades) - 1, sym, t_sec, inc))

        if new_count > 0 or updated_count > 0:
            with open(TRADES_FILE, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2, ensure_ascii=False)
            sync_trades_to_csv()
            try:
                plot_real_trading_performance(days=10)
            except Exception as pe:
                print(f"[PnL Sync] チャート更新エラー: {pe}")
            print(f"[PnL Sync] 取引所API直接同期完了: 新規追加={new_count}件, 更新={updated_count}件 (総履歴: {len(trades)}件)")
            return new_count + updated_count
        else:
            print(f"[PnL Sync] 取引所API同期: 全{len(data)}件の実現損益レコードは既に完全同期済みです。")
            return 0
    except Exception as e:
        print(f"[PnL Sync] 取引所API同期例外エラー: {e}")
        return 0


# =====================================================================
# 月次パフォーマンスレポート機能（月初 毎月1日 自動配信）
# =====================================================================

def get_monthly_performance_summary(target_year: int = None, target_month: int = None) -> dict:
    """指定年月（デフォルト: 前月）のJSTベース月次損益を集計する。"""
    trades = load_real_trades()
    if not trades:
        return {"status": "empty", "message": "トレード履歴がありません。"}

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(timezone(timedelta(hours=9)))
    if target_year is None or target_month is None:
        first_of_this_month = now_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_of_this_month - timedelta(days=1)
        target_year = last_month_end.year
        target_month = last_month_end.month

    target_prefix = f"{target_year:04d}-{target_month:02d}"

    df = pd.DataFrame(trades)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp_utc"])
    df["dt_jst"] = df["timestamp_utc"].dt.tz_convert("Asia/Tokyo")
    df["month_jst"] = df["dt_jst"].dt.strftime("%Y-%m")

    month_df = df[(df["month_jst"] == target_prefix) & (df["action"] == "CLOSE")].copy()
    if month_df.empty:
        return {
            "status": "empty",
            "year": target_year,
            "month": target_month,
            "month_label": f"{target_year}年{target_month:02d}月度",
            "message": f"{target_year}年{target_month:02d}月度の決済トレードはありません。"
        }

    wins = int((month_df["pnl"] > 0).sum())
    losses = int((month_df["pnl"] < 0).sum())
    evens = int((month_df["pnl"] == 0).sum())
    n_valid = wins + losses
    win_rate = (wins / n_valid * 100.0) if n_valid > 0 else 0.0

    total_pnl = float(month_df["pnl"].sum())
    gross_profit = float(month_df[month_df["pnl"] > 0]["pnl"].sum())
    gross_loss = abs(float(month_df[month_df["pnl"] < 0]["pnl"].sum()))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 1.0)

    avg_win = (gross_profit / wins) if wins > 0 else 0.0
    avg_loss = (gross_loss / losses) if losses > 0 else 0.0

    by_symbol = []
    for sym, grp in month_df.groupby("symbol"):
        s_pnl = float(grp["pnl"].sum())
        s_valid = grp[grp["pnl"] != 0]
        s_cnt = len(s_valid) if not s_valid.empty else len(grp)
        s_wins = int((grp["pnl"] > 0).sum())
        s_losses = int((grp["pnl"] < 0).sum())
        by_symbol.append({
            "symbol": str(sym).replace("-USDT", ""),
            "pnl": s_pnl,
            "trades": s_cnt,
            "wins": s_wins,
            "losses": s_losses
        })
    by_symbol.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "status": "ok",
        "year": target_year,
        "month": target_month,
        "month_label": f"{target_year}年{target_month:02d}月度",
        "total_trades": n_valid,
        "raw_trades": len(month_df),
        "wins": wins,
        "losses": losses,
        "evens": evens,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "by_symbol": by_symbol
    }


def plot_monthly_performance(target_year: int = None, target_month: int = None) -> Optional[Path]:
    """指定年月（デフォルト: 前月）の月次パフォーマンスチャート（上段: 累積PnL, 下段: 日別損益バー）を生成する。"""
    trades = load_real_trades()
    if not trades:
        return None

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(timezone(timedelta(hours=9)))
    if target_year is None or target_month is None:
        first_of_this_month = now_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_of_this_month - timedelta(days=1)
        target_year = last_month_end.year
        target_month = last_month_end.month

    target_prefix = f"{target_year:04d}-{target_month:02d}"

    df = pd.DataFrame(trades)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp_utc"])
    df["dt_jst"] = df["timestamp_utc"].dt.tz_convert("Asia/Tokyo")
    df["date_jst"] = df["dt_jst"].dt.strftime("%Y-%m-%d")
    df["month_jst"] = df["dt_jst"].dt.strftime("%Y-%m")

    m_df = df[(df["month_jst"] == target_prefix) & (df["action"] == "CLOSE") & (df["pnl"] != 0)].copy()
    if m_df.empty:
        return None

    m_df = m_df.sort_values("timestamp_utc").reset_index(drop=True)
    m_df["cum_pnl"] = m_df["pnl"].cumsum()

    daily_pnl = m_df.groupby("date_jst")["pnl"].sum().reset_index()
    daily_pnl["dt"] = pd.to_datetime(daily_pnl["date_jst"])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7.0), dpi=150, sharex=True, gridspec_kw={'height_ratios': [1.2, 1.0]})
    fig.patch.set_facecolor("#131722")
    ax1.set_facecolor("#1e222d")
    ax2.set_facecolor("#1e222d")

    ax1.plot(m_df["dt_jst"], m_df["cum_pnl"], marker='o', markersize=5, color='#29b6f6', linewidth=2.2, label='Cumulative Real PnL (USDT)')
    ax1.fill_between(m_df["dt_jst"], m_df["cum_pnl"], 0, color='#29b6f6', alpha=0.15)
    ax1.axhline(0, color="#787b86", linestyle="--", alpha=0.5)
    ax1.set_title(f"BingX Monthly Trading Performance ({target_year:04d}-{target_month:02d})", fontsize=13, color="#ffffff", pad=12, weight="bold")
    ax1.set_ylabel("Cumulative PnL (USDT)", fontsize=10, color="#b2b5be")
    ax1.tick_params(colors="#b2b5be", labelsize=9)
    ax1.grid(True, linestyle=':', color="#363c4e", alpha=0.7)
    ax1.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=9, labelcolor="#ffffff")

    colors = ['#26a69a' if p >= 0 else '#ef5350' for p in daily_pnl["pnl"]]
    bars = ax2.bar(daily_pnl["dt"], daily_pnl["pnl"], width=0.6, color=colors, alpha=0.85, zorder=3)
    ax2.axhline(0, color="#787b86", linestyle="--", alpha=0.5, zorder=2)
    ax2.set_ylabel("Daily PnL (USDT)", fontsize=10, color="#b2b5be")
    ax2.set_xlabel("Date (JST)", fontsize=10, color="#b2b5be")
    ax2.tick_params(colors="#b2b5be", labelsize=9)
    ax2.grid(True, linestyle=':', color="#363c4e", alpha=0.7)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))

    for bar, val in zip(bars, daily_pnl["pnl"]):
        y_pos = bar.get_height()
        va = 'bottom' if y_pos >= 0 else 'top'
        c = '#26a69a' if y_pos >= 0 else '#ef5350'
        ax2.annotate(f"{val:+.2f}$",
                     xy=(bar.get_x() + bar.get_width() / 2, y_pos),
                     xytext=(0, 3 if y_pos >= 0 else -10),
                     textcoords="offset points",
                     ha='center', va=va,
                     fontsize=8.0, color=c, weight='bold')

    plt.tight_layout()
    monthly_chart_path = DATA_DIR / f"monthly_performance_{target_year}{target_month:02d}.png"
    plt.savefig(monthly_chart_path, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    plt.close('all')

    import gc
    gc.collect()
    print(f"[Real Trade Tracker] 月次パフォーマンスチャート生成完了: {monthly_chart_path}")
    return monthly_chart_path


def send_monthly_pnl_report_discord(target_year: int = None, target_month: int = None) -> bool:
    """指定年月（デフォルト: 前月）の月次レポートをDiscordへ送信する。"""
    try:
        from bingx5_46_3logic import discord
    except Exception:
        discord = None

    summary = get_monthly_performance_summary(target_year=target_year, target_month=target_month)
    if summary.get("status") != "ok":
        msg = f"📅 【月次確定トレードレポート】\n{summary.get('message', 'データなし')}"
        if discord:
            discord.print_log(msg)
        else:
            print(msg)
        return False

    pnl_sign = "+" if summary["total_pnl"] >= 0 else ""
    pf_str = f"{summary['profit_factor']:.2f}" if summary["profit_factor"] < 900 else "INF (無敗)"

    lines = [
        f"📅 **【BingX 月次確定トレードレポート ({summary['month_label']})】**",
        "```text",
        f"🗓 集計対象: {summary['month_label']} 全取引確定実績",
        f"🎯 総取引数: {summary['total_trades']} 件 ({summary['wins']}勝 {summary['losses']}敗 / 勝率 {summary['win_rate']:.1f}%)",
        f"💰 月間確定損益: {pnl_sign}${summary['total_pnl']:.2f} USDT",
        f"📈 損益因子 (PF): {pf_str} (総利益: +${summary['gross_profit']:.2f} / 総損失: -${summary['gross_loss']:.2f})",
        f"⚖ 平均利益: +${summary['avg_win']:.2f} / 平均損失: -${summary['avg_loss']:.2f}",
        "────────────────────────────────────────",
        "📌 銘柄別月間損益内訳:"
    ]

    for item in summary["by_symbol"][:10]:
        s_sign = "+" if item["pnl"] >= 0 else ""
        lines.append(f"  ・{item['symbol']:<7}: {s_sign}${item['pnl']:>6.2f} ({item['wins']}勝{item['losses']}敗 / 計{item['trades']}回)")

    if len(summary["by_symbol"]) > 10:
        lines.append(f"  ... 他 {len(summary['by_symbol']) - 10} 銘柄")

    lines.append("```")
    report_text = "\n".join(lines)

    print(report_text)
    if discord:
        discord.print_log(report_text, force_send=True)

    try:
        chart_file = plot_monthly_performance(target_year=summary["year"], target_month=summary["month"])
        if chart_file and chart_file.exists() and discord:
            discord.send_file(
                chart_file,
                f"📅 【月次実績】{summary['month_label']} 確定パフォーマンスチャート ({pnl_sign}${summary['total_pnl']:.2f} USDT / 勝率 {summary['win_rate']:.1f}%)"
            )
    except Exception as chart_err:
        print(f"[Real Trade Tracker] 月次チャート送信エラー: {chart_err}")

    return True


def check_and_send_monthly_report_on_first_day() -> bool:
    """毎月1日（JST）に自動実行される月次レポート判定。"""
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(timezone(timedelta(hours=9)))

    if now_jst.day != 1:
        return False

    first_of_this_month = now_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_of_this_month - timedelta(days=1)
    target_year = last_month_end.year
    target_month = last_month_end.month
    sent_key = f"{target_year:04d}-{target_month:02d}"

    sent_data = {}
    if MONTHLY_STATE_FILE.exists():
        try:
            with open(MONTHLY_STATE_FILE, "r", encoding="utf-8") as f:
                sent_data = json.load(f)
        except Exception:
            sent_data = {}

    if sent_data.get(sent_key):
        return False

    print(f"[Real Trade Tracker] 月初検知: {sent_key} の月次レポートを配信します。")
    success = send_monthly_pnl_report_discord(target_year=target_year, target_month=target_month)
    if success:
        sent_data[sent_key] = now_jst.isoformat()
        try:
            with open(MONTHLY_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(sent_data, f, indent=2)
        except Exception as e:
            print(f"[Real Trade Tracker] 月次送信状態保存エラー: {e}")
        return True
    return False


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

        if len(w_highs) == 0 or len(w_lows) == 0:
            poc_arr[i] = closes[i]
            vah_arr[i] = closes[i]
            val_arr[i] = closes[i]
            continue

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

    has_vp = all(col in plot_df.columns for col in ['VAH', 'VAL', 'POC'])
    if has_vp:
        vah_s = plot_df['VAH'].values
        val_s = plot_df['VAL'].values
        poc_s = plot_df['POC'].values

        ax.fill_between(x_indices, val_s, vah_s, color="#ff9800", alpha=0.10, label="Value Area (VAH-VAL)", zorder=1)
        ax.plot(x_indices, vah_s, color="#ff9800", linestyle="--", linewidth=1.4, alpha=0.85, label="VAH Band (Resistance)", zorder=2)
        ax.plot(x_indices, poc_s, color="#29b6f6", linestyle="-.", linewidth=1.3, alpha=0.85, label="POC Band (Value Center)", zorder=2)
        ax.plot(x_indices, val_s, color="#ab47bc", linestyle="--", linewidth=1.4, alpha=0.85, label="VAL Band (Support/SL)", zorder=2)

    for i, row in plot_df.iterrows():
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        color = "#26a69a" if cl >= op else "#ef5350"
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.2, zorder=3)
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
    interval_label: str = "",
    side: str = "LONG",
) -> Optional[Path]:
    """新規エントリー時点のローソク足とVolume Profile(VAH/VAL/POC)履歴バンド、エントリーポイントを描画して保存する"""
    if df is None or df.empty:
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DATA_DIR / f"entry_chart_{symbol}.png"

    work_df = df.copy()
    if not all(col in work_df.columns for col in ['VAH', 'VAL', 'POC']):
        work_df = compute_volume_profile_bands(work_df, period=lookback_bars)

    plot_df = work_df.tail(lookback_bars).reset_index(drop=True)
    if len(plot_df) == 0:
        return None

    if "timestamp" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["timestamp"], utc=True)
    elif "date" in plot_df.columns:
        plot_df["dt"] = pd.to_datetime(plot_df["date"], utc=True)
    else:
        plot_df["dt"] = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(plot_df), freq="5min")

    plot_df["dt"] = plot_df["dt"].dt.tz_convert("Asia/Tokyo")

    if not interval_label and len(plot_df) >= 2:
        try:
            diff_sec = (plot_df["dt"].iloc[-1] - plot_df["dt"].iloc[-2]).total_seconds()
            diff_min = int(round(diff_sec / 60))
            if diff_min in (5, 15, 30):
                interval_label = f"{diff_min}m"
            elif diff_min == 60:
                interval_label = "1h"
            elif diff_min > 0:
                interval_label = f"{diff_min}m"
        except Exception:
            pass

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=150)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#1e222d")

    x_indices = _draw_candles_and_vp(ax, plot_df)

    is_short = (str(side).upper() == "SHORT")
    marker_color = "#ff5252" if is_short else "#00e676"
    marker_sym = "v" if is_short else "^"
    annot_tag = "▼ SHORT ENTRY" if is_short else "▲ LONG ENTRY"
    box_bg = "#b71c1c" if is_short else "#1b5e20"
    side_label_str = "SHORT" if is_short else "LONG"

    last_idx = len(plot_df) - 1
    ax.scatter([last_idx], [entry_price], color=marker_color, s=180, marker=marker_sym, edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"{side_label_str} Entry Point @ ${entry_price:.4f}")

    price_str = f"${entry_price:.6f}" if entry_price < 1 else f"${entry_price:.4f}"
    ax.annotate(
        f" {annot_tag}\n {price_str}",
        xy=(last_idx, entry_price),
        xytext=(last_idx + 1.2, entry_price),
        arrowprops=dict(
            arrowstyle="->",
            facecolor=marker_color,
            edgecolor=marker_color,
            lw=1.5,
            shrinkA=3,
            shrinkB=5
        ),
        bbox=dict(boxstyle="round,pad=0.5", facecolor=box_bg, edgecolor=marker_color, alpha=0.95),
        fontsize=9,
        color="#ffffff",
        weight="bold",
        verticalalignment="center",
        zorder=7
    )

    _setup_time_axis(ax, plot_df, x_indices, right_margin_bars=8)

    inv_str = f" [{interval_label}]" if interval_label else ""
    whale_str = f" | Whale: {whale_signal} ({whale_flow:+,.0f} USD)" if whale_signal else ""
    strat_str = f" | Strat: {strategy_name}" if strategy_name else ""
    title_price_str = f"{entry_price:.6f} USDT" if entry_price < 1 else f"{entry_price:.4f} USDT"

    ax.set_title(
        f"[NEW ENTRY] {symbol} ({side_label_str}){inv_str} - Execution & Volume Profile Bands\nEntry Price: {title_price_str}{strat_str}{whale_str}",
        fontsize=11,
        color="#ffffff",
        weight="bold",
        pad=15
    )

    ax.margins(y=0.08)
    ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=8, labelcolor="#ffffff", framealpha=0.75)
    plt.tight_layout()

    plt.savefig(out_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")

    try:
        CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
        archive_file = CHARTS_DIR / f"{now_str}_ENTRY_{symbol}.png"
        plt.savefig(archive_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")
    except Exception:
        pass

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
    interval_label: str = "",
) -> Optional[Path]:
    """決済(Exit/Close)時点のローソク足、高精度Volume Profile履歴バンド、RSI/RSIMAサブプロット、トレード軌跡を描画して保存する"""
    if df is None or df.empty:
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DATA_DIR / f"exit_chart_{symbol}.png"

    work_df = df.copy()
    # 既存の古い VAH/VAL/POC カラムは破棄（チャート表示期間に合わせて高精度再計算するため）
    for col in ['VAH', 'VAL', 'POC']:
        if col in work_df.columns:
            work_df = work_df.drop(columns=[col])

    # タイムスタンプの正規化（Asia/Tokyo）
    if "timestamp" in work_df.columns:
        work_df["dt"] = pd.to_datetime(work_df["timestamp"], utc=True).dt.tz_convert("Asia/Tokyo")
    elif "date" in work_df.columns:
        work_df["dt"] = pd.to_datetime(work_df["date"], utc=True).dt.tz_convert("Asia/Tokyo")
    else:
        work_df["dt"] = pd.date_range(end=pd.Timestamp.now(tz="Asia/Tokyo"), periods=len(work_df), freq="5min")

    # 足間隔（分）の特定
    gran_min = 5
    if len(work_df) >= 2:
        try:
            diff_sec = (work_df["dt"].iloc[-1] - work_df["dt"].iloc[-2]).total_seconds()
            diff_m = int(round(diff_sec / 60))
            if diff_m in (5, 15, 30, 60):
                gran_min = diff_m
            elif diff_m > 0:
                gran_min = diff_m
        except Exception:
            pass

    if not interval_label:
        interval_label = f"{gran_min}m" if gran_min < 60 else f"{gran_min // 60}h"

    # トレード履歴から直近のエントリーおよび決済情報を取得
    trades = load_real_trades()
    entry_trade = None
    exit_trade = None
    for t in reversed(trades):
        if t.get("symbol") == symbol:
            if not exit_trade and t.get("action") == "CLOSE":
                exit_trade = t
            elif not entry_trade and t.get("action") == "ENTRY":
                entry_trade = t
            if entry_trade and exit_trade:
                break

    if entry_trade:
        rec_px = float(entry_trade.get("price", 0))
        if rec_px > 0:
            if (entry_price is None or entry_price <= 0) or (abs(entry_price - rec_px) / max(entry_price, rec_px) > 0.15):
                entry_price = rec_px

    last_close = float(work_df["close"].iloc[-1])
    if entry_price is None or entry_price <= 0:
        entry_price = float(work_df["open"].iloc[max(0, len(work_df) - 5)])

    if last_close > 0 and (exit_price is None or exit_price <= 0 or abs(exit_price - last_close) / last_close > 0.20):
        exit_price = last_close

    # 決済時刻（Exit dt）の特定と未確定足の補完
    now_jst = pd.Timestamp.now(tz="Asia/Tokyo")
    exit_dt = now_jst
    if exit_trade:
        ex_time_val = exit_trade.get("dt_jst") or exit_trade.get("timestamp")
        if ex_time_val:
            try:
                exit_dt = pd.to_datetime(ex_time_val, utc=True).tz_convert("Asia/Tokyo")
            except Exception:
                pass

    last_candle_start = work_df["dt"].iloc[-1]
    last_candle_end = last_candle_start + pd.Timedelta(minutes=gran_min)

    # 決済時刻が最終確定足より後の場合、決済足（未確定足）を末尾に正しく追加
    if exit_dt >= last_candle_end:
        new_row = {
            "dt": last_candle_end,
            "open": float(work_df["close"].iloc[-1]),
            "high": max(float(work_df["close"].iloc[-1]), exit_price),
            "low": min(float(work_df["close"].iloc[-1]), exit_price),
            "close": exit_price,
            "volume": float(work_df["volume"].iloc[-1]) * 0.5 if "volume" in work_df.columns else 1.0,
        }
        work_df = pd.concat([work_df, pd.DataFrame([new_row])], ignore_index=True)
    elif exit_dt >= last_candle_start:
        work_df.loc[work_df.index[-1], "close"] = exit_price
        work_df.loc[work_df.index[-1], "high"] = max(float(work_df["high"].iloc[-1]), exit_price)
        work_df.loc[work_df.index[-1], "low"] = min(float(work_df["low"].iloc[-1]), exit_price)

    # RSI (期間9) および RSI-EMA (期間7) の計算（十分な母数を持つ work_df 全体から高精度計算）
    delta = work_df["close"].diff()
    gain = (delta.where(delta > 0, 0.0)).ewm(alpha=1.0 / 9.0, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / 9.0, adjust=False).mean()
    rs = gain / (loss + 1e-12)
    work_df["rsi"] = 100.0 - (100.0 / (1.0 + rs))
    work_df["rsi_ema"] = work_df["rsi"].ewm(span=7, adjust=False).mean()

    # エントリー足と表示バー数の動的決定（エントリーが見切れないように自動拡張）
    entry_dt = None
    if entry_trade:
        en_time_val = entry_trade.get("dt_jst") or entry_trade.get("timestamp")
        if en_time_val:
            try:
                entry_dt = pd.to_datetime(en_time_val, utc=True).tz_convert("Asia/Tokyo")
            except Exception:
                pass

    bars_needed = lookback_bars
    if entry_dt:
        elapsed_min = max(0, (exit_dt - entry_dt).total_seconds() / 60)
        e_bars = int(elapsed_min // gran_min) + 12
        bars_needed = max(lookback_bars, min(len(work_df), e_bars))

    plot_df = work_df.tail(bars_needed).reset_index(drop=True)

    # 画面に表示されるローソク足範囲に基づき、ボリュームプロファイルを正確に再計算（上下乖離を完全根絶）
    vp_period = min(len(plot_df), max(24, lookback_bars))
    plot_df = compute_volume_profile_bands(plot_df, period=vp_period, value_area_pct=0.70, num_bins=80)

    # エントリー足・エグジット足のインデックス厳密特定
    exit_idx = len(plot_df) - 1
    entry_idx = max(0, exit_idx - 4)
    if entry_dt:
        matched = False
        for idx, row in plot_df.iterrows():
            b_start = row["dt"]
            b_end = b_start + pd.Timedelta(minutes=gran_min)
            if b_start <= entry_dt < b_end:
                entry_idx = idx
                matched = True
                break
        if not matched:
            time_diffs = (plot_df["dt"] - entry_dt).abs()
            nearest_idx = int(time_diffs.argmin())
            if nearest_idx < exit_idx:
                entry_idx = nearest_idx

    if pnl_pct is None and entry_price > 0:
        if side.upper() == "LONG":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0

    # 戦略タイプ判定: RSIMAかVPか
    is_rsima = ("RSIMA" in exit_reason.upper() or "rsima" in symbol.lower() or "rsima" in interval_label.lower())

    # 日本語フォント設定（文字化け・警告防止）
    _configure_japanese_font()

    # 2段構成プロット (メイン: ローソク足+VP、サブ: RSI/RSIMA または Volume)
    fig, (ax_main, ax_sub) = plt.subplots(
        2, 1, figsize=(12, 7.8), dpi=150,
        gridspec_kw={"height_ratios": [3.2, 1.2], "hspace": 0.08},
        sharex=True
    )
    fig.patch.set_facecolor("#131722")
    ax_main.set_facecolor("#1e222d")
    ax_sub.set_facecolor("#1e222d")

    x_indices = _draw_candles_and_vp(ax_main, plot_df)

    entry_marker_color = "#ff5252" if side.upper() == "SHORT" else "#00e676"
    entry_marker_shape = "v" if side.upper() == "SHORT" else "^"
    ax_main.scatter([entry_idx], [entry_price], color=entry_marker_color, s=160, marker=entry_marker_shape,
                    edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"Entry ({side.upper()}) @ ${entry_price:.4f}")
    exit_marker_color = "#26a69a" if pnl >= 0 else "#ef5350"
    ax_main.scatter([exit_idx], [exit_price], color=exit_marker_color, s=160, marker="v",
                    edgecolors="#ffffff", linewidths=1.5, zorder=6, label=f"Exit @ ${exit_price:.4f}")

    line_color = "#26a69a" if pnl >= 0 else "#ef5350"
    ax_main.plot([entry_idx, exit_idx], [entry_price, exit_price], color=line_color, linestyle=":", linewidth=2.0, alpha=0.9, zorder=5)

    pnl_sign = "+" if pnl > 0 else ""
    pnl_str = f"PnL: {pnl_sign}${pnl:.2f} ({pnl_pct:+.2f}%)" if pnl_pct is not None else f"PnL: {pnl_sign}${pnl:.2f}"
    clean_exit_reason = str(exit_reason).replace("➔", "->").replace("➡", "->") if exit_reason else ""
    reason_str = f"\nReason: {clean_exit_reason}" if clean_exit_reason else ""
    bg_color = "#1b5e20" if pnl >= 0 else "#b71c1c"

    ax_main.annotate(
        f" ▼ {side} EXIT\n {pnl_str}{reason_str}",
        xy=(exit_idx, exit_price),
        xytext=(exit_idx + 1.2, exit_price),
        arrowprops=dict(arrowstyle="->", facecolor=exit_marker_color, edgecolor=exit_marker_color, lw=1.5, shrinkA=3, shrinkB=5),
        bbox=dict(boxstyle="round,pad=0.5", facecolor=bg_color, edgecolor=exit_marker_color, alpha=0.95),
        fontsize=9, color="#ffffff", weight="bold", verticalalignment="center", zorder=7
    )

    title_exit_str = f"${exit_price:.6f}" if exit_price < 1 else f"${exit_price:.4f}"
    title_entry_str = f"${entry_price:.6f}" if entry_price < 1 else f"${entry_price:.4f}"
    result_tag = "PROFIT" if pnl >= 0 else "LOSS CUT"
    inv_str = f" [{interval_label}]" if interval_label else ""
    ax_main.set_title(
        f"[{result_tag}] {symbol} ({side} CLOSE){inv_str} - Trade Execution Chart\nEntry: {title_entry_str} -> Exit: {title_exit_str} | Realized PnL: {pnl_sign}${pnl:.2f} USDT ({pnl_pct:+.2f}%)",
        fontsize=11, color="#ffffff", weight="bold", pad=12
    )
    ax_main.margins(y=0.08)
    ax_main.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=8, labelcolor="#ffffff", framealpha=0.75)

    # --- サブプロット描画 ---
    if is_rsima:
        rsi_vals = plot_df["rsi"].values
        rsi_ema_vals = plot_df["rsi_ema"].values
        ax_sub.plot(x_indices, rsi_vals, color="#ffeb3b", linewidth=1.5, label="RSI(9)", zorder=3)
        ax_sub.plot(x_indices, rsi_ema_vals, color="#00e5ff", linewidth=1.3, linestyle="--", label="RSI-EMA(7)", zorder=3)

        if side.upper() == "SHORT":
            ax_sub.axhline(60.0, color="#ef5350", linestyle=":", linewidth=1.2, alpha=0.8, label="Short Entry Threshold (60)")
            ax_sub.axhline(40.0, color="#26a69a", linestyle=":", linewidth=1.2, alpha=0.8, label="Short TP Threshold (40)")
        else:
            ax_sub.axhline(40.0, color="#26a69a", linestyle=":", linewidth=1.2, alpha=0.8, label="Long Entry Threshold (40)")
            ax_sub.axhline(60.0, color="#ef5350", linestyle=":", linewidth=1.2, alpha=0.8, label="Long TP Threshold (60)")

        ax_sub.axhline(70, color="#555555", linestyle="--", linewidth=0.8, alpha=0.6)
        ax_sub.axhline(30, color="#555555", linestyle="--", linewidth=0.8, alpha=0.6)

        ax_sub.scatter([entry_idx], [rsi_vals[entry_idx]], color=entry_marker_color, s=60, zorder=5)
        ax_sub.scatter([exit_idx], [rsi_vals[exit_idx]], color=exit_marker_color, s=60, zorder=5)

        ax_sub.set_ylim(15, 85)
        ax_sub.set_yticks([20, 40, 60, 80])
        ax_sub.set_ylabel("RSI", color="#b2b5be", fontsize=8)
        ax_sub.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=7.5, labelcolor="#ffffff", framealpha=0.75, ncol=2)
    else:
        vols = plot_df["volume"].values if "volume" in plot_df.columns else np.ones(len(plot_df))
        closes = plot_df["close"].values
        opens = plot_df["open"].values
        vol_colors = ["#26a69a" if c >= o else "#ef5350" for c, o in zip(closes, opens)]
        ax_sub.bar(x_indices, vols, color=vol_colors, alpha=0.65, width=0.6, label="Volume")
        if len(vols) >= 10:
            vol_ma = pd.Series(vols).rolling(min(20, len(vols)), min_periods=3).mean().values
            ax_sub.plot(x_indices, vol_ma, color="#ff9800", linewidth=1.2, label="Vol MA(20)")
        ax_sub.set_ylabel("Volume", color="#b2b5be", fontsize=8)
        ax_sub.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363c4e", fontsize=7.5, labelcolor="#ffffff", framealpha=0.75)

    _setup_time_axis(ax_sub, plot_df, x_indices, right_margin_bars=8)

    plt.savefig(out_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")

    try:
        CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
        archive_file = CHARTS_DIR / f"{now_str}_EXIT_{symbol}.png"
        plt.savefig(archive_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")
    except Exception:
        pass

    plt.close(fig)
    plt.close('all')
    import gc
    gc.collect()

    print(f"[Real Trade Tracker] エグジットチャート（同期VP＆サブプロット付き）生成完了: {out_file}")
    return out_file