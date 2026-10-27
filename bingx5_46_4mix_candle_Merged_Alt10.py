from __future__ import annotations
"""
bingx5_46_4mix_candle_Merged_Alt10.py
BingX USDT無期限先物 20銘柄 + BTC ノーマライズ比較 ＆ 3段階全体地合い判定スクリプト (PROJECT_RULES.md 準拠)

- 5分足 SSoT ストア (candle_5m_store) より高速に 1H足 / 日足 を動的生成
- 30d, 10d, 5d ノーマライズ比較チャート（先月高安 PMH/PML 点線入り）を出力・Discord送信
- 前半高値ピークアウト監査・レンジ半値維持補完ルールによる崩壊銘柄除外
- 3段階全体地合い判定（BTC 2票 / 計22票制、11票以上過半数）により BULL / NEUTRAL / BEAR を算出
- 結果を Data/market_state.json に永続化
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as md
import numpy as np
import pandas as pd
import requests

from config_loader import get_webhook_url
from candle_5m_store import (
    sync_all_target_symbols_5m,
    sync_symbol_5m_candles,
    load_5m_candles,
    resample_from_5m,
    get_resampled_candles,
    normalize_symbol_name,
    get_clean_symbol,
)
from monthly_key_levels import get_or_update_monthly_key_levels, compute_bounce_metrics
from bingx5_46_3logic import (
    evaluate_market_regime_3window,
    check_normalized_peak_timing,
    check_normalized_trough_timing,
    check_normalized_timing_by_regime,
    check_bottom_reversal_recovery,
    check_peak_breakdown_trend,
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

JST = timezone(timedelta(hours=9))


async def fetch_bingx_ohlcv(symbol: str, inst_type: str = "SWAP", interval: str = "5m", start_time: int = 0, end_time: int = 0, mode: str = "demo"):
    """5分足 SSoT からローソク足を辞書リスト形式で取得（後方互換用）"""
    from candle_5m_store import load_5m_candles, sync_symbol_5m_candles
    df = sync_symbol_5m_candles(symbol, target_days=3)
    if df.empty:
        df = load_5m_candles(symbol)
    if df.empty:
        return []
    return df.to_dict(orient="records")

def is_forex_symbol(s: str) -> bool:
    clean = str(s).upper().replace("-", "").replace("_", "")
    forex_pairs = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"}
    return any(clean.startswith(fp) or clean == fp for fp in forex_pairs)

# 固定選定銘柄 (主要アルトコイン20銘柄 + BTC-USDT 計22票制 / PROJECT_RULES.md 準拠)
FIXED_TARGET_SYMBOLS: List[str] = [
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "APT-USDT", "NEAR-USDT", "ARB-USDT",
    "UNI-USDT", "OP-USDT", "TIA-USDT", "RENDER-USDT", "INJ-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]
REGIME_SYMBOLS: List[str] = ["BTC-USDT"]
ALL_SYMBOLS: List[str] = ["BTC-USDT"] + [s for s in FIXED_TARGET_SYMBOLS if s != "BTC-USDT"]


class send_discord:
    hourly_only: bool = True

    def __init__(self, hourly_only: bool = True) -> None:
        self.hourly_only = hourly_only
        self.real3_webhook = get_webhook_url("real3_bngx")
        self.test4_webhook = get_webhook_url("test4_backtest")
        self.webhook_url = self.real3_webhook or self.test4_webhook

    def _get_target_webhooks(self, text: str = "") -> list[str]:
        target = self.real3_webhook or self.test4_webhook
        return [target] if target else []

    def send_message(self, content: str, force_send: bool = True) -> None:
        if getattr(self, "hourly_only", True) and not force_send:
            return
        webhooks = self._get_target_webhooks(content)
        for url in webhooks:
            try:
                requests.post(url, json={"content": content}, timeout=10).raise_for_status()
            except requests.RequestException:
                pass

    def send_file(self, file_path: Path, description: str = "", force_send: bool = True) -> None:
        if getattr(self, "hourly_only", True) and not force_send:
            return
        webhooks = self._get_target_webhooks(description)
        for url in webhooks:
            try:
                if not file_path.exists():
                    continue
                with file_path.open("rb") as fh:
                    files = {"file": (file_path.name, fh)}
                    payload_json = json.dumps({"content": description}, ensure_ascii=False)
                    data = {"payload_json": payload_json}
                    requests.post(url, data=data, files=files, timeout=30).raise_for_status()
            except requests.RequestException as e:
                print(f"[Discord Error] Failed to send file {file_path.name}: {e}")


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def generate_normalized_charts(
    all_dfs: List[pd.DataFrame],
    out_dir: Path,
    window_name: str,
    hours_limit: int,
    excluded_symbols: Optional[Set[str]] = None,
) -> Tuple[Optional[Path], float, str, float]:
    """指定期間 (30d / 10d / 5d) の指定全20銘柄の正規化比較チャートを作成し、地合いを判定"""
    if not all_dfs:
        return None, 1.0, "NEUTRAL", 0.5

    plot_items = []
    for df in all_dfs:
        if df.empty or "close" not in df.columns or "symbol" not in df.columns:
            continue
        sym = df["symbol"].iloc[0]
        clean_sym = normalize_symbol_name(sym)
        df_sliced = df.tail(hours_limit).reset_index(drop=True)
        if len(df_sliced) < 2:
            continue

        close_series = df_sliced["close"].astype(float)
        base_price = close_series.iloc[0]
        if base_price <= 0:
            continue

        norm_series = close_series / base_price
        final_norm = float(norm_series.iloc[-1])
        ts_series = pd.to_datetime(df_sliced["timestamp"], utc=True)
        plot_items.append({
            "symbol": clean_sym,
            "ticker": get_clean_symbol(clean_sym),
            "ts": ts_series,
            "norm": norm_series,
            "final_norm": final_norm,
            "base_price": base_price,
            "current_price": float(close_series.iloc[-1])
        })

    if not plot_items:
        return None, 1.0, "NEUTRAL", 0.5

    # 期間パフォーマンス（final_norm）の降順にソート
    plot_items.sort(key=lambda x: x["final_norm"], reverse=True)

    norm_performances = [item["final_norm"] for item in plot_items]
    mean_norm = float(np.mean(norm_performances)) if norm_performances else 1.0
    pos_count = sum(1 for fn in norm_performances if fn >= 1.0)
    pos_ratio = (pos_count / len(norm_performances)) if norm_performances else 0.5
    market_state = "LONG" if mean_norm >= 1.0 else "SHORT"

    fig, ax = plt.subplots(figsize=(12.0, 7.0))

    # プロット & 凡例
    for rank, item in enumerate(plot_items, 1):
        sym = item["symbol"]
        ticker = item["ticker"]
        fn = item["final_norm"]
        pct_change = (fn - 1.0) * 100.0
        sign_str = "+" if pct_change >= 0 else ""
        ex_tag = " [EX]" if excluded_symbols and (sym in excluded_symbols or ticker in excluded_symbols) else ""
        label = f"{rank:>2}. {ticker:<7} ({fn:.2f}x | {sign_str}{pct_change:.1f}%){ex_tag}"
        ax.plot(item["ts"], item["norm"], label=label, linewidth=1.8)

    ax.axhline(1.0, color="black", linestyle="--", linewidth=1.2, alpha=0.6, zorder=1, label="--- Base Line (1.00x)")

    # 先月高安ライン (PMH / PML) の点線描画 (PROJECT_RULES.md 準拠)
    try:
        data_dir = out_dir if out_dir.name == "Data" else out_dir / "Data"
        all_syms = [item["symbol"] for item in plot_items]
        if "BTC-USDT" not in all_syms:
            all_syms.append("BTC-USDT")
        m_levels = get_or_update_monthly_key_levels(all_syms, data_dir)

        # 1. BTC-USDT の先月高安ライン (市場全体のサポート/レジスタンス基準線 / 太い点線)
        btc_item = next((item for item in plot_items if item["ticker"] == "BTC"), None)
        if btc_item is not None:
            btc_base_px = float(btc_item["base_price"])
            btc_curr_px = float(btc_item["current_price"])
            btc_metrics = compute_bounce_metrics("BTC-USDT", btc_base_px, btc_curr_px, m_levels)
            if btc_metrics["pmh_norm"] > 0 and btc_metrics["pml_norm"] > 0:
                ax.axhline(btc_metrics["pmh_norm"], color="darkorange", linestyle=":", linewidth=2.2, alpha=0.85,
                           label=f"--- BTC Prev-M High ({btc_metrics['pmh_norm']:.2f}x)", zorder=3)
                ax.axhline(btc_metrics["pml_norm"], color="deepskyblue", linestyle=":", linewidth=2.2, alpha=0.85,
                           label=f"--- BTC Prev-M Low ({btc_metrics['pml_norm']:.2f}x / Bounce {btc_metrics['bounce_from_pml_pct']:+.1f}%)", zorder=3)

        # 2. アルト平均の先月高安ライン (市場全体のサポート/レジスタンス基準線 / 破線)
        all_pml_norms = []
        all_pmh_norms = []
        for item in plot_items:
            if item["ticker"] == "BTC":
                continue
            b_px = float(item["base_price"])
            c_px = float(item["current_price"])
            m_met = compute_bounce_metrics(item["symbol"], b_px, c_px, m_levels)
            if m_met["pmh_norm"] > 0:
                all_pmh_norms.append(m_met["pmh_norm"])
            if m_met["pml_norm"] > 0:
                all_pml_norms.append(m_met["pml_norm"])

        if all_pmh_norms and all_pml_norms:
            avg_pmh = float(np.mean(all_pmh_norms))
            avg_pml = float(np.mean(all_pml_norms))
            ax.axhline(avg_pmh, color="crimson", linestyle="--", linewidth=1.4, alpha=0.6,
                       label=f"--- Alt Avg Prev-M High ({avg_pmh:.2f}x)", zorder=2)
            ax.axhline(avg_pml, color="limegreen", linestyle="--", linewidth=1.4, alpha=0.6,
                       label=f"--- Alt Avg Prev-M Low ({avg_pml:.2f}x)", zorder=2)
    except Exception as m_err:
        log(f"[Normalized Chart] 先月高安ライン描画エラー: {m_err}")

    ax.set_title(f"BingX Top 20 Symbols Normalized Performance [{window_name}] (Regime: {market_state})", fontsize=12, pad=12)
    ax.set_xlabel("Time (JST)", fontsize=10)
    ax.set_ylabel("Normalized Price (Base = 1.0)", fontsize=10)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0., fontsize=8, labelspacing=0.25)
    ax.grid(True, linestyle=':', alpha=0.4)
    plt.tight_layout()

    out_file = out_dir / f"normalized_chart_{window_name.lower()}.png"
    plt.savefig(out_file, dpi=150, bbox_inches="tight")
    plt.close()
    return out_file, mean_norm, market_state, pos_ratio


async def fetch_bingx_ticker_summary() -> List[Dict[str, Any]]:
    """BingX API から全ティッカーの最新スナップショットを取得"""
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", [])
    except Exception as e:
        log(f"[Ticker Fetch Error] {e}")
    return []


async def main() -> None:
    parser = argparse.ArgumentParser(description="BingX 20 Symbols Merged & 3-Window Regime Screening")
    parser.add_argument("--no-chart-send", action="store_true", help="Discordチャート送信をスキップ")
    parser.add_argument("--skip-zip", action="store_true", help="ZIP圧縮をスキップ")
    args = parser.parse_args()

    out_dir = Path(__file__).resolve().parent / "Data"
    out_dir.mkdir(parents=True, exist_ok=True)

    log("==================================================")
    log("  BingX Top 20 Alts & BTC SSoT Sync & Regime Evaluation")
    log("==================================================")

    # 1. 5分足 SSoT ストアの一括同期
    log(f"[SSoT Sync] 全 {len(ALL_SYMBOLS)} 銘柄の 5分足時系列ストアを同期中...")
    sync_all_target_symbols_5m(ALL_SYMBOLS, target_days=65)

    # 2. 5分足ストアから 1H足 および 日足(1D) を動的生成して Data/ に保存
    all_dfs = []
    symbol_1h_dict = {}

    for sym in ALL_SYMBOLS:
        df_5m = load_5m_candles(sym)
        if df_5m.empty:
            continue
        df_1h = resample_from_5m(df_5m, 60)
        df_1d = resample_from_5m(df_5m, 1440)
        df_1h["symbol"] = sym
        df_1d["symbol"] = sym

        # 個別CSV保存
        clean_sym = normalize_symbol_name(sym)
        ticker = get_clean_symbol(clean_sym)
        df_1h.to_csv(out_dir / f"merged_{clean_sym}.csv", index=False)
        df_1h.to_csv(out_dir / f"merged_{ticker}.csv", index=False)
        df_1d.to_csv(out_dir / f"daily_{clean_sym}.csv", index=False)
        df_1d.to_csv(out_dir / f"daily_{ticker}.csv", index=False)

        all_dfs.append(df_1h)
        symbol_1h_dict[clean_sym] = df_1h

    log(f"[Resample Done] 生成完了: {len(all_dfs)} 銘柄の 1H足 & 日足データ")

    # 3. 3段階全体地合い判定 (30d / 10d / 5d 22票制) を先行判定
    regime_eval_res = evaluate_market_regime_3window(symbol_1h_dict)
    overall_reg = regime_eval_res.get("overall_regime", "NEUTRAL")
    period_res = regime_eval_res.get("period_results", {})
    recommended_syms = list(regime_eval_res.get("recommended_symbols", []))

    # 4. 地合い連動ノーマライズ前半高安監査 (BUY: ピークアウト除外 / SELL: 底打ち反発除外 / NEUTRAL: 停止)
    #    および【PROJECT_RULES 第2項準拠】買い時底打ち反転銘柄・売り時天井崩壊銘柄の候補追加
    excluded_symbols = set()
    exclusion_reasons = {}

    for d in all_dfs:
        sym_name = d["symbol"].iloc[0]
        ticker = get_clean_symbol(sym_name)
        if ticker == "BTC":
            continue

        if overall_reg == "BUY":
            is_bad, bad_reasons = check_normalized_peak_timing(d)
            if is_bad:
                reason_str = ", ".join(bad_reasons)
                print(f"   [Normalized Peak Drop] {ticker}: {reason_str} -> 前半高値下落型のため買い除外")
                excluded_symbols.add(sym_name)
                excluded_symbols.add(ticker)
                exclusion_reasons[ticker] = reason_str
            else:
                # 【PROJECT_RULES 第2項準拠】買い時: 前半ボトムかつレンジ半値以上へ買い戻された銘柄（セリクラ底打ち反転・V字回復型）を買い候補に追加採用
                is_recovery, rec_reasons = check_bottom_reversal_recovery(d)
                if is_recovery:
                    reason_str = ", ".join(rec_reasons)
                    print(f"   [Bottom Reversal Recovery] {ticker}: {reason_str} -> セリクラ底打ち反転型のため買い候補に追加採用")
                    clean_sym = normalize_symbol_name(sym_name)
                    if clean_sym not in recommended_syms and ticker not in [get_clean_symbol(s) for s in recommended_syms]:
                        recommended_syms.append(clean_sym)
        elif overall_reg == "SELL":
            is_bad, bad_reasons = check_normalized_trough_timing(d)
            if is_bad:
                reason_str = ", ".join(bad_reasons)
                print(f"   [Normalized Trough Rebound] {ticker}: {reason_str} -> 前半安値反発型のため売り除外")
                excluded_symbols.add(sym_name)
                excluded_symbols.add(ticker)
                exclusion_reasons[ticker] = reason_str
            else:
                # 【PROJECT_RULES 第2項準拠】売り時: 前半ピークかつレンジ半値以下へ崩落した銘柄（天井崩壊トレンド型）を売り候補に追加採用（温存）
                is_breakdown, bd_reasons = check_peak_breakdown_trend(d)
                if is_breakdown:
                    reason_str = ", ".join(bd_reasons)
                    print(f"   [Peak Breakdown Trend] {ticker}: {reason_str} -> 天井崩壊トレンド型のため売り候補に追加採用")
                    clean_sym = normalize_symbol_name(sym_name)
                    if clean_sym not in recommended_syms and ticker not in [get_clean_symbol(s) for s in recommended_syms]:
                        recommended_syms.append(clean_sym)

    recommended_syms = sorted(list(set(recommended_syms)))

    # 5. 30d, 10d, 5d ノーマライズ比較チャート生成 & Discord送信
    windows = [
        ("30d", 30 * 24),
        ("10d", 10 * 24),
        ("5d", 5 * 24),
    ]

    discord = send_discord()
    window_states = []
    ts_jst_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    for win_label, win_hours in windows:
        chart_file, mean_norm, market_state, pos_ratio = generate_normalized_charts(
            all_dfs, out_dir, win_label, win_hours, excluded_symbols=excluded_symbols
        )
        state_icon = "🟢" if market_state == "LONG" else "🔴"
        log(f"   └ [{win_label} Window] Mean Norm: {mean_norm:.4f} (Pos Ratio: {pos_ratio:.1%}) -> Market State: {market_state}")
        window_states.append((win_label, market_state, mean_norm, pos_ratio))

        if not args.no_chart_send and chart_file and chart_file.exists():
            discord.send_file(chart_file, f"BingX Normalized Performance [{win_label}] All 20 Symbols ({market_state})")

    # 6. 地合い判定結果サマリー＆3態勢アクション Discord送信
    if not args.no_chart_send:
        state_label_map = {"BUY": "買い", "SELL": "売り", "NEUTRAL": "中立"}
        regime_summary_lines = [
            "📊 **【新・全体地合い判定サマリー（30日 / 10日 / 5日）】**",
            "※判定基準: アルト20銘柄(各1票) + BTC-USDT(2票) = 計22票 / 11票以上で判定"
        ]
        for win_label in ["30d", "10d", "5d"]:
            p_info = period_res.get(win_label, {})
            p_state = p_info.get("state", "NEUTRAL")
            b_votes = p_info.get("buy_votes", 0)
            s_votes = p_info.get("sell_votes", 0)
            tot_v = p_info.get("total_votes", 22)
            s_label = state_label_map.get(p_state, p_state)
            icon = "🟢" if p_state == "BUY" else ("🔴" if p_state == "SELL" else "⚪")
            win_label_jp = {"30d": "30日分", "10d": "10日分", "5d": "5日分"}.get(win_label, win_label)
            regime_summary_lines.append(
                f"・**{win_label_jp} ({win_label})**: {icon} **{s_label}**（買い: `{b_votes}/{tot_v}`票, 売り: `{s_votes}/{tot_v}`票）"
            )

        overall_icon = "🟢" if overall_reg == "BUY" else ("🔴" if overall_reg == "SELL" else "⚪")
        overall_label = state_label_map.get(overall_reg, overall_reg)
        reg_reason = regime_eval_res.get("regime_reason", "")
        reason_suffix = f" ({reg_reason})" if reg_reason else " (3期間合致判定)"
        regime_summary_lines.append(f"\n👉 **総合地合い判定**: {overall_icon} **{overall_label}**{reason_suffix}")

        if overall_reg == "BUY":
            regime_summary_lines.append(f"🎯 **買いトレード推奨銘柄 ({len(recommended_syms)}銘柄)**: `{', '.join(recommended_syms) if recommended_syms else 'なし'}`")
            regime_summary_lines.append("⚡ **アクション**: 買い推奨銘柄の個別最適化（ロング144通り: 単発厳選 ➔ 最大3枠ナンピン）を実行し、ロングトレードを開始します。")
        elif overall_reg == "SELL":
            regime_summary_lines.append(f"🎯 **売りトレード推奨銘柄 ({len(recommended_syms)}銘柄)**: `{', '.join(recommended_syms) if recommended_syms else 'なし'}`")
            regime_summary_lines.append("⚡ **アクション**: 総合地合い売り（BEAR）態勢です。売り推奨銘柄の個別最適化（ショート144通り: 単発1本固定/ナンピン完全なし）を実行し、ショートトレードを開始します。")
        else:
            regime_summary_lines.append("🛡️ **アクション**: 総合地合い中立（NEUTRAL）です。新規エントリーは完全停止（ノーポジ待機）し、既存ポジションがある場合は建値早期撤退（+0.20%）のみ実行します。")

        regime_eval_msg = "\n".join(regime_summary_lines)
        discord.send_message(regime_eval_msg)
        print(f"\n{regime_eval_msg}\n")

    # 6. market_state.json の保存
    final_state = "long_only" if overall_reg == "BUY" else ("short_only" if overall_reg == "SELL" else "neutral")
    state_file = out_dir / "market_state.json"
    state_data = {
        "market_state": final_state,
        "overall_regime": overall_reg,
        "regime_reason": regime_eval_res.get("regime_reason", ""),
        "is_30d_peak_breakdown": regime_eval_res.get("is_30d_peak_breakdown", False),
        "is_30d_bottom_reversal": regime_eval_res.get("is_30d_bottom_reversal", False),
        "is_buy_regime": (overall_reg == "BUY"),
        "is_sell_regime": (overall_reg == "SELL"),
        "period_results": period_res,
        "recommended_symbols": recommended_syms,
        "excluded_symbols": list(excluded_symbols),
        "exclusion_reasons": exclusion_reasons,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2, ensure_ascii=False)
    log(f"Saved market state ({final_state} | overall={overall_reg}) to {state_file}")

    log("==================================================")
    log("  BingX Screening & Multi-Window Analysis Completed!")
    log("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
