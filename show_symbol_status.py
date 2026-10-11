#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BingX 主要アルト20銘柄 ステータス完全可視化ダッシュボード
(PROJECT_RULES.md 準拠: 全20銘柄の選定・除外理由の完全透明化)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 固定選定銘柄 (主要アルトコイン20銘柄)
FIXED_SYMBOLS = [
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "APT-USDT", "NEAR-USDT", "ARB-USDT",
    "UNI-USDT", "OP-USDT", "TIA-USDT", "RENDER-USDT", "INJ-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]

DATA_DIR = Path(__file__).resolve().parent / "Data"
JST = timezone(timedelta(hours=9))


def get_whale_sig_quick(symbol: str) -> str:
    """クジラセンチメント情報を取得 (存在しない場合はNEUTRAL)"""
    whale_file = DATA_DIR / "whale_sentiment.json"
    if not whale_file.exists():
        return "NEUTRAL"
    try:
        with open(whale_file, "r", encoding="utf-8") as f:
            w_data = json.load(f)
            clean = symbol.replace("-USDT", "").replace("USDT", "").upper()
            return w_data.get(clean, {}).get("signal", "NEUTRAL")
    except Exception:
        return "NEUTRAL"


def build_symbols_status_summary(
    selected_symbols: Optional[List[str]] = None,
    symbol_params_map: Optional[Dict[str, Any]] = None,
    save_json: bool = True
) -> Dict[str, Any]:
    """
    全20銘柄の選定・除外ステータスを網羅的に集計・分類する。
    """
    eligible_file = DATA_DIR / "trade_eligible_symbols.json"
    last_res_file = DATA_DIR / "last_selection_result.json"

    eligible_data = {}
    if eligible_file.exists():
        try:
            with open(eligible_file, "r", encoding="utf-8") as f:
                eligible_data = json.load(f)
        except Exception:
            pass

    # 引数が指定されていない場合は前回の選定結果JSONから復元
    if selected_symbols is None or symbol_params_map is None:
        if last_res_file.exists():
            try:
                with open(last_res_file, "r", encoding="utf-8") as f:
                    last_res = json.load(f)
                    if selected_symbols is None:
                        selected_symbols = last_res.get("selected_symbols", [])
                    if symbol_params_map is None:
                        symbol_params_map = last_res.get("symbol_params_map", {})
            except Exception:
                pass

    if selected_symbols is None:
        selected_symbols = []
    if symbol_params_map is None:
        symbol_params_map = {}

    reports = eligible_data.get("reports", {})
    evaluated_at = eligible_data.get("evaluated_at", datetime.now(timezone.utc).isoformat())

    results = []
    counts = {
        "SELECTED": 0,
        "OVER_LIMIT": 0,
        "OPT_REJECTED": 0,
        "TOP_STOP": 0,
        "BOTTOM_WATCHING": 0,
        "WHALE_SHORT": 0,
        "OTHER_REJECTED": 0,
    }

    for sym in FIXED_SYMBOLS:
        rep = reports.get(sym, {})
        is_cycle_ok = rep.get("is_eligible", False)
        cycle_res = rep.get("cycle_result", {})
        reasons = rep.get("rejection_reasons", [])

        drop_pct = cycle_res.get("drop_from_peak_pct", 0.0)
        best_top_pct = cycle_res.get("best_top_pct", 0.025)
        climax_hit = cycle_res.get("climax_hit", False)
        c_state = cycle_res.get("state_name", "NORMAL")

        whale_sig = get_whale_sig_quick(sym)

        # 最適化結果の取得
        opt_info = symbol_params_map.get(sym, {})
        pnl = opt_info.get("pnl", None)
        trade_count = opt_info.get("trade_count", 0)
        win_rate = opt_info.get("win_rate", 0.0)
        max_dd = opt_info.get("max_dd", 0.0)
        strat = opt_info.get("strategy", "").upper()
        params = opt_info.get("params", {})

        badge = ""
        category = ""
        detail_reason = ""

        # 分類判定ロジック
        if not is_cycle_ok:
            if c_state == "TOP_STOP" or any("高値" in str(r) for r in reasons):
                badge = "🔴 【高値圏警戒見送り】"
                category = "TOP_STOP"
                counts["TOP_STOP"] += 1
                detail_reason = f"168h高値から{drop_pct:+.1f}%滞在 (停止幅:{best_top_pct*100:.1f}%) 天井掴み警戒"
            elif climax_hit or c_state == "BOTTOM_WATCHING" or any("セリクラ" in str(r) for r in reasons):
                badge = "🟡 【セリクラ下落待機】"
                category = "BOTTOM_WATCHING"
                counts["BOTTOM_WATCHING"] += 1
                detail_reason = "出来高急増後、底打ち反転足（下ヒゲ陽線）の確定を待機中"
            else:
                badge = "🔴 【サイクル見送り】"
                category = "OTHER_REJECTED"
                counts["OTHER_REJECTED"] += 1
                detail_reason = ", ".join(reasons) if reasons else "168hサイクル条件不適合"
        elif whale_sig == "SHORT_ONLY":
            badge = "🔴 【大口売り優勢】"
            category = "WHALE_SHORT"
            counts["WHALE_SHORT"] += 1
            detail_reason = "クジラセンチメントが売り偏重 (SHORT_ONLY除外)"
        else:
            # サイクル合格済み
            if sym in selected_symbols:
                badge = "🟢 【トレード選定・監視中】"
                category = "SELECTED"
                counts["SELECTED"] += 1
                strat_desc = f"戦略={strat}"
                if strat in ("VAL_POC", "VP_VAL_GC"):
                    strat_desc += f" (段階ナンピン5回/POC利確)"
                elif strat:
                    strat_desc += f" (R9, M7, Ep{params.get('lEp', 40)}, Cp{params.get('lCp', 70)})"
                detail_reason = strat_desc
            elif pnl is not None and pnl > 0 and trade_count > 0:
                badge = "⚪ 【選定上限枠外】"
                category = "OVER_LIMIT"
                counts["OVER_LIMIT"] += 1
                detail_reason = f"最適化合格だが最大監視枠(上限)超過のため待機"
            else:
                badge = "🔴 【個別最適化不合格】"
                category = "OPT_REJECTED"
                counts["OPT_REJECTED"] += 1
                if pnl is not None:
                    detail_reason = f"直近バックテスト不合格 (PnL: ${pnl:+.2f}, 取引: {trade_count}回, 勝率: {win_rate:.0f}%)"
                else:
                    detail_reason = "直近データ不足またはバックテスト純利益マイナス"

        results.append({
            "symbol": sym,
            "badge": badge,
            "category": category,
            "detail_reason": detail_reason,
            "drop_pct": drop_pct,
            "pnl": pnl,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "max_dd": max_dd,
            "strategy": strat,
        })

    summary_data = {
        "evaluated_at": evaluated_at,
        "counts": counts,
        "selected_symbols": selected_symbols,
        "symbols": results,
    }

    if save_json:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        summary_file = DATA_DIR / "symbol_status_summary.json"
        try:
            with open(summary_file, "w", encoding="utf-8") as f:
                json.dump(summary_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return summary_data


def format_status_table_console(summary_data: Dict[str, Any]) -> str:
    """コンソール向けの整形テーブル文字列を作成"""
    eval_time = summary_data.get("evaluated_at", "")
    counts = summary_data.get("counts", {})
    symbols = summary_data.get("symbols", [])

    lines = []
    lines.append("=" * 115)
    lines.append(f"📊 【主要アルト20銘柄 選定・除外ステータス完全一覧ダッシュボード】 (評価: {eval_time[:19]})")
    lines.append("=" * 115)
    lines.append(
        f"{'#':2s} | {'銘柄':10s} | {'ステータス':22s} | {'1週高値乖離':10s} | {'直近PnL':12s} | {'勝率':6s} | {'取引':4s} | {'詳細理由 / 採用戦略'}"
    )
    lines.append("-" * 115)

    for idx, s in enumerate(symbols, 1):
        sym = s["symbol"]
        badge = s["badge"]
        drop = f"{s['drop_pct']:+5.1f}%"
        pnl = f"${s['pnl']:+6.2f}" if s["pnl"] is not None else "  ---  "
        wr = f"{s['win_rate']:4.0f}%" if s["pnl"] is not None and s["trade_count"] > 0 else " --- "
        tc = f"{s['trade_count']:2d}回" if s["pnl"] is not None and s["trade_count"] > 0 else "---"
        reason = s["detail_reason"]

        lines.append(
            f"{idx:2d} | {sym:10s} | {badge:22s} | {drop:>10s} | {pnl:>12s} | {wr:>6s} | {tc:>4s} | {reason}"
        )

    lines.append("-" * 115)
    total_sel = counts.get("SELECTED", 0)
    total_top = counts.get("TOP_STOP", 0)
    total_climax = counts.get("BOTTOM_WATCHING", 0)
    total_opt = counts.get("OPT_REJECTED", 0)
    total_over = counts.get("OVER_LIMIT", 0)
    total_whale = counts.get("WHALE_SHORT", 0)
    lines.append(
        f"💡 【内訳】選定・監視中: {total_sel}銘柄 | 高値圏見送り: {total_top}銘柄 | セリクラ待機: {total_climax}銘柄 | "
        f"最適化不合格: {total_opt}銘柄 | 上限枠外: {total_over}銘柄 | クジラ売り除外: {total_whale}銘柄"
    )
    lines.append("=" * 115)
    return "\n".join(lines)


def format_status_table_discord(summary_data: Dict[str, Any]) -> str:
    """
    Discord（特にスマホ表示）に最適化した整形テキスト
    ・重要銘柄（選定中・高値警戒・セリクラ）のハイライト箇条書き
    ・スマホの縦画面でも横スクロールせず収まる全20銘柄コンパクト一覧 (幅約36文字)
    """
    counts = summary_data.get("counts", {})
    symbols = summary_data.get("symbols", [])
    eval_time = summary_data.get("evaluated_at", "")[:16].replace("T", " ")

    lines = []
    lines.append(f"📊 **【主要20銘柄 選定・除外ステータス】** (`{eval_time} UTC`)")
    lines.append(
        f"💡 **内訳**: 監視: `{counts.get('SELECTED', 0)}` | 高値警戒: `{counts.get('TOP_STOP', 0)}` | "
        f"セリクラ: `{counts.get('BOTTOM_WATCHING', 0)}` | 不合格: `{counts.get('OPT_REJECTED', 0)}`"
    )

    # 1. 重要グループのハイライト (スマホで真っ先に把握したい情報)
    selected_list = [s for s in symbols if s["category"] == "SELECTED"]
    top_stop_list = [s for s in symbols if s["category"] == "TOP_STOP"]
    climax_list = [s for s in symbols if s["category"] == "BOTTOM_WATCHING"]
    whale_list = [s for s in symbols if s["category"] == "WHALE_SHORT"]

    if selected_list:
        lines.append("\n🟢 **【トレード選定・監視中】**")
        for s in selected_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            strat = s.get("strategy", "")
            pnl_str = f"+${s['pnl']:.2f}" if s.get("pnl") is not None else ""
            wr_str = f"勝率{s['win_rate']:.0f}%" if s.get("win_rate") else ""
            lines.append(f"• **{sym_clean}**: `{strat}` | {pnl_str} ({wr_str})")

    if top_stop_list:
        lines.append("\n🔴 **【高値圏警戒見送り (天井掴み防止)】**")
        for s in top_stop_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: 1週高値から `{s['drop_pct']:+.1f}%` 滞在 (停止幅2.0〜2.5%)")

    if climax_list:
        lines.append("\n🟡 **【セリクラ下落待機 (底打ち確認中)】**")
        for s in climax_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: 出来高急増後、反転足の確定待ち")

    if whale_list:
        lines.append("\n🔴 **【大口売り優勢 (Whale除外)】**")
        for s in whale_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: クジラ判定売り偏重 (SHORT_ONLY)")

    # 2. スマホの画面幅 (36文字等幅) に収まる全20銘柄コンパクト表
    lines.append("\n📱 **【全20銘柄 コンパクト一覧】**")
    lines.append("```text")
    lines.append("銘柄      | 状態       | 1週乖離| PnL")
    lines.append("------------------------------------")

    for s in symbols:
        sym = s["symbol"].replace("-USDT", "")
        cat = s["category"]
        if cat == "SELECTED":
            b_short = "🟢選定監視"
        elif cat == "TOP_STOP":
            b_short = "🔴高値警戒"
        elif cat == "BOTTOM_WATCHING":
            b_short = "🟡セリクラ"
        elif cat == "WHALE_SHORT":
            b_short = "🔴大口売り"
        elif cat == "OVER_LIMIT":
            b_short = "⚪上限枠外"
        else:
            b_short = "🔴不合格"

        drop = f"{s['drop_pct']:+5.1f}%"
        if s["pnl"] is not None and s["trade_count"] > 0:
            pnl_val = f"${s['pnl']:+5.1f}"
        else:
            pnl_val = " --- "

        lines.append(f"{sym:9s} | {b_short:8s} | {drop:>6s} | {pnl_val:>5s}")

    lines.append("```")
    return "\n".join(lines)


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))
        except Exception:
            pass


def display_and_send_symbols_status(
    selected_symbols: Optional[List[str]] = None,
    symbol_params_map: Optional[Dict[str, Any]] = None,
    send_to_discord: bool = True,
    webhook_name: str = "real3_bngx"
) -> Dict[str, Any]:
    """
    全20銘柄のステータスを集計し、コンソールに表示してDiscordにも送信する。
    """
    summary = build_symbols_status_summary(selected_symbols, symbol_params_map, save_json=True)
    table_text = format_status_table_console(summary)
    safe_print("\n" + table_text + "\n")

    if send_to_discord:
        try:
            from config_loader import get_webhook_url
            import requests

            webhook_url = get_webhook_url(webhook_name) or get_webhook_url("test4_backtest")
            if webhook_url:
                discord_text = format_status_table_discord(summary)
                res = requests.post(webhook_url, json={"content": discord_text}, timeout=10)
                res.raise_for_status()
                safe_print(f"[Discord] {webhook_name} 宛にステータス通知を正常送信しました。")
            else:
                safe_print(f"[Discord Warning] Webhook URL ({webhook_name}) が見つかりません。")
        except Exception as e:
            safe_print(f"[Discord Error] Webhook送信失敗 ({webhook_name}): {e}")

    return summary


if __name__ == "__main__":
    send_discord_flag = ("--discord" in sys.argv or "--test4" in sys.argv)
    target_wh = "test4_backtest" if ("--test4" in sys.argv or "test4" in sys.argv) else "real3_bngx"
    
    # --webhook 引数がある場合
    for idx, arg in enumerate(sys.argv):
        if arg == "--webhook" and idx + 1 < len(sys.argv):
            target_wh = sys.argv[idx + 1].strip()
            send_discord_flag = True

    print(f"[Status Tool] 20銘柄の最新選定・除外ステータスを集計中... (送信先: {target_wh if send_discord_flag else 'なし'})")
    display_and_send_symbols_status(send_to_discord=send_discord_flag, webhook_name=target_wh)

