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

# 固定選定銘柄 (主要アルトコイン20銘柄 / PROJECT_RULES.md 準拠)
FIXED_SYMBOLS = [
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "ADA-USDT", "NEAR-USDT", "APT-USDT",
    "OP-USDT", "ARB-USDT", "TIA-USDT", "RENDER-USDT", "WIF-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]

DATA_DIR = Path(__file__).resolve().parent / "Data"
JST = timezone(timedelta(hours=9))


def get_whale_sig_quick(symbol: str) -> str:
    """Data/whale_market_state.json よりクジラセンチメント情報を取得 (存在しない場合はNEUTRAL)"""
    whale_file = DATA_DIR / "whale_market_state.json"
    if not whale_file.exists():
        whale_file = DATA_DIR / "whale_sentiment.json"
    if not whale_file.exists():
        return "NEUTRAL"
    try:
        with open(whale_file, "r", encoding="utf-8") as f:
            w_data = json.load(f)
            coins_sentiment = w_data.get("coins_sentiment", w_data)
            clean = symbol.replace("-USDT", "").replace("USDT", "").upper()
            if symbol in coins_sentiment and isinstance(coins_sentiment[symbol], dict):
                return coins_sentiment[symbol].get("signal", "NEUTRAL")
            if clean in coins_sentiment and isinstance(coins_sentiment[clean], dict):
                return coins_sentiment[clean].get("signal", "NEUTRAL")
            return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def get_macro_gate_summary() -> Dict[str, Any]:
    """Data/market_state.json より3段階全体地合い情報 (30d/10d/5d) を取得"""
    state_file = DATA_DIR / "market_state.json"
    default_res = {
        "overall_regime": "NEUTRAL",
        "regime_gate": "ALLOW",
        "regime_desc": "NEUTRAL (3期間混在・慎重運用)",
        "w30": "NEUTRAL",
        "w10": "NEUTRAL",
        "w5": "NEUTRAL",
        "excluded_symbols": [],
        "exclusion_reasons": {},
        "recommended_symbols": [],
    }
    if not state_file.exists():
        return default_res
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            overall = data.get("overall_regime", "NEUTRAL")
            p_res = data.get("period_results", {})
            w30 = p_res.get("30d", {}).get("state", "NEUTRAL")
            w10 = p_res.get("10d", {}).get("state", "NEUTRAL")
            w5 = p_res.get("5d", {}).get("state", "NEUTRAL")
            gate = "STOP" if overall == "SELL" else "ALLOW"
            desc = f"総合:{overall} (30d:{w30}/10d:{w10}/5d:{w5})"
            return {
                "overall_regime": overall,
                "regime_gate": gate,
                "regime_desc": desc,
                "w30": w30,
                "w10": w10,
                "w5": w5,
                "excluded_symbols": data.get("excluded_symbols", []),
                "exclusion_reasons": data.get("exclusion_reasons", {}),
                "recommended_symbols": data.get("recommended_symbols", []),
                "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
    except Exception:
        return default_res


def build_symbols_status_summary(
    selected_symbols: Optional[List[str]] = None,
    symbol_params_map: Optional[Dict[str, Any]] = None,
    save_json: bool = True
) -> Dict[str, Any]:
    """
    全20銘柄の選定・除外ステータスを網羅的に集計・分類する (PROJECT_RULES.md 準拠)。
    """
    last_res_file = DATA_DIR / "last_selection_result.json"
    gate_info = get_macro_gate_summary()
    excluded_list = [s.upper() for s in gate_info.get("excluded_symbols", [])]
    exclusion_reasons_map = gate_info.get("exclusion_reasons", {})

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

    evaluated_at = gate_info.get("timestamp", datetime.now(timezone.utc).isoformat())

    results = []
    counts = {
        "SELECTED": 0,
        "OVER_LIMIT": 0,
        "OPT_REJECTED": 0,
        "PEAK_DROP": 0,
        "WHALE_SHORT": 0,
        "OTHER_REJECTED": 0,
    }

    for sym in FIXED_SYMBOLS:
        clean_sym = sym.replace("-USDT", "").replace("USDT", "").upper()
        is_peak_dropped = (sym.upper() in excluded_list) or (clean_sym in excluded_list)
        peak_reasons = exclusion_reasons_map.get(sym) or exclusion_reasons_map.get(clean_sym) or []

        whale_sig = get_whale_sig_quick(sym)

        # 最適化結果の取得
        opt_info = symbol_params_map.get(sym, {})
        pnl = opt_info.get("pnl", None)
        trade_count = opt_info.get("trade_count", 0)
        win_rate = opt_info.get("win_rate", 0.0)
        max_dd = opt_info.get("max_dd", 0.0)
        strat = opt_info.get("strategy", "").upper()
        interval_val = opt_info.get("interval", 5)

        badge = ""
        category = ""
        detail_reason = ""

        if sym in selected_symbols:
            badge = "🟢 【トレード選定・監視中】"
            category = "SELECTED"
            counts["SELECTED"] += 1
            if strat in ("VAL_POC", "VP_VAL_GC"):
                mp_val = opt_info.get("mp", 200)
                bins_val = opt_info.get("num_bins", 50)
                va_pct = int(float(opt_info.get("value_area_pct", 0.70)) * 100)
                ma_cond = f"{opt_info.get('ma_type', 'EMA')}_{opt_info.get('ma_condition', 'close_above')}"
                detail_reason = f"戦略={strat} ({interval_val}m / MP={mp_val}, Bins={bins_val}, VA={va_pct}%, {ma_cond})"
            else:
                l_ep = int(float(opt_info.get("l_ep", 40)))
                l_cp = int(float(opt_info.get("l_cp", 60)))
                sl_p = float(opt_info.get("sl_pct", 0.090)) * 100
                detail_reason = f"戦略={strat or 'RSIMA'} ({interval_val}m / R9, M7, EP={l_ep}, CP={l_cp}, SL={sl_p:.1f}%)"
            if is_peak_dropped:
                detail_reason += " [※3期間比較:前半高値警戒]"
        elif whale_sig == "SHORT_ONLY":
            badge = "🔴 【大口売り優勢】"
            category = "WHALE_SHORT"
            counts["WHALE_SHORT"] += 1
            detail_reason = "クジラセンチメントが売り偏重 (SHORT_ONLY除外)"
        elif pnl is not None and pnl > 0 and trade_count >= 10:
            badge = "⚪ 【選定上限枠外】"
            category = "OVER_LIMIT"
            counts["OVER_LIMIT"] += 1
            detail_reason = f"2段階最適化合格 (PnL:+${pnl:.2f}/{trade_count}回) だが最大監視枠超過のため待機"
        elif is_peak_dropped:
            badge = "🔴 【前半ピーク半値割れ除外】"
            category = "PEAK_DROP"
            counts["PEAK_DROP"] += 1
            if isinstance(peak_reasons, str):
                detail_reason = peak_reasons
            elif isinstance(peak_reasons, list) and peak_reasons:
                detail_reason = ", ".join(str(r) for r in peak_reasons)
            else:
                detail_reason = "3期間ノーマライズ前半高値・半値割れ除外"
            if pnl is not None:
                detail_reason += f" (BT不合格: ${pnl:+.2f}/{trade_count}回)"
        else:
            badge = "🔴 【個別最適化不合格】"
            category = "OPT_REJECTED"
            counts["OPT_REJECTED"] += 1
            if pnl is not None:
                detail_reason = f"2段階72通り検証不合格 (PnL: ${pnl:+.2f}, 取引: {trade_count}回 < 10回基準またはマイナス, 勝率: {win_rate:.0f}%)"
            else:
                detail_reason = "直近データ不足または第1段階シングル枠(10回以上&黒字)未達"

        results.append({
            "symbol": sym,
            "badge": badge,
            "category": category,
            "detail_reason": detail_reason,
            "pnl": pnl,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "max_dd": max_dd,
            "strategy": strat,
            "interval": interval_val,
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
    gate_info = get_macro_gate_summary()
    g_icon = "🟢" if gate_info["regime_gate"] == "ALLOW" else "🛑"

    lines = []
    lines.append("=" * 118)
    lines.append(f"📊 【主要アルト20銘柄 選定・除外ステータス完全一覧ダッシュボード】 (評価: {eval_time[:19]})")
    lines.append(f"🛡️  3期間全体地合いディフェンス: {g_icon} [{gate_info['regime_gate']}] {gate_info['regime_desc']}")
    lines.append("=" * 118)
    lines.append(
        f"{'#':2s} | {'銘柄':10s} | {'ステータス':24s} | {'採用足':6s} | {'直近PnL':12s} | {'勝率':6s} | {'取引':4s} | {'詳細理由 / 採用戦略'}"
    )
    lines.append("-" * 118)

    for idx, s in enumerate(symbols, 1):
        sym = s["symbol"]
        badge = s["badge"]
        inv_str = f"{s.get('interval', 5)}m" if s.get("pnl") is not None else "---"
        pnl = f"${s['pnl']:+6.2f}" if s["pnl"] is not None else "  ---  "
        wr = f"{s['win_rate']:4.0f}%" if s["pnl"] is not None and s["trade_count"] > 0 else " --- "
        tc = f"{s['trade_count']:2d}回" if s["pnl"] is not None and s["trade_count"] > 0 else "---"
        reason = s["detail_reason"]

        lines.append(
            f"{idx:2d} | {sym:10s} | {badge:24s} | {inv_str:>6s} | {pnl:>12s} | {wr:>6s} | {tc:>4s} | {reason}"
        )

    lines.append("-" * 118)
    total_sel = counts.get("SELECTED", 0)
    total_peak = counts.get("PEAK_DROP", 0)
    total_opt = counts.get("OPT_REJECTED", 0)
    total_over = counts.get("OVER_LIMIT", 0)
    total_whale = counts.get("WHALE_SHORT", 0)
    lines.append(
        f"💡 【内訳】選定・監視中: {total_sel}銘柄 | 前半ピーク割れ除外: {total_peak}銘柄 | "
        f"最適化不合格: {total_opt}銘柄 | 上限枠外: {total_over}銘柄 | クジラ売り除外: {total_whale}銘柄"
    )
    lines.append("=" * 118)
    return "\n".join(lines)


def format_status_table_discord(summary_data: Dict[str, Any]) -> str:
    """
    Discord（スマホ表示）に最適化した整形テキスト
    """
    counts = summary_data.get("counts", {})
    symbols = summary_data.get("symbols", [])
    eval_time = summary_data.get("evaluated_at", "")[:16].replace("T", " ")
    gate_info = get_macro_gate_summary()
    g_icon = "🟢" if gate_info["regime_gate"] == "ALLOW" else "🛑"

    lines = []
    lines.append(f"📊 **【主要20銘柄 選定・除外ステータス】** (`{eval_time} UTC`)")
    lines.append(f"🛡️ **3期間地合い判定**: {g_icon}`{gate_info['regime_gate']}` ({gate_info['regime_desc']})")
    lines.append(
        f"💡 **内訳**: 監視: `{counts.get('SELECTED', 0)}` | ピーク割れ除外: `{counts.get('PEAK_DROP', 0)}` | "
        f"最適化不合格: `{counts.get('OPT_REJECTED', 0)}` | クジラ除外: `{counts.get('WHALE_SHORT', 0)}`"
    )

    selected_list = [s for s in symbols if s["category"] == "SELECTED"]
    peak_list = [s for s in symbols if s["category"] == "PEAK_DROP"]
    whale_list = [s for s in symbols if s["category"] == "WHALE_SHORT"]

    if selected_list:
        lines.append("\n🟢 **【トレード選定・監視中】**")
        for s in selected_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            strat = s.get("strategy", "")
            inv_s = f"{s.get('interval', 5)}m"
            pnl_str = f"+${s['pnl']:.2f}" if s.get("pnl") is not None else ""
            wr_str = f"勝率{s['win_rate']:.0f}%/{s['trade_count']}回" if s.get("trade_count") else ""
            lines.append(f"• **{sym_clean}**: `{strat}({inv_s})` | {pnl_str} ({wr_str})")

    if peak_list:
        lines.append("\n🔴 **【前半ピーク半値割れ除外 (ピークアウト警戒)】**")
        for s in peak_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: `{s['detail_reason']}`")

    if whale_list:
        lines.append("\n🔴 **【大口売り優勢 (Whale除外)】**")
        for s in whale_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: クジラ判定売り偏重 (SHORT_ONLY)")

    lines.append("\n📱 **【全20銘柄 コンパクト一覧】**")
    lines.append("```text")
    lines.append("銘柄      | 状態       | 足種  | PnL")
    lines.append("------------------------------------")

    for s in symbols:
        sym = s["symbol"].replace("-USDT", "")
        cat = s["category"]
        if cat == "SELECTED":
            b_short = "🟢選定監視"
        elif cat == "PEAK_DROP":
            b_short = "🔴ピーク割"
        elif cat == "WHALE_SHORT":
            b_short = "🔴大口売り"
        elif cat == "OVER_LIMIT":
            b_short = "⚪上限枠外"
        else:
            b_short = "🔴不合格"

        inv_s = f"{s.get('interval', 5)}m" if s.get("pnl") is not None else "---"
        if s["pnl"] is not None and s["trade_count"] > 0:
            pnl_val = f"${s['pnl']:+5.1f}"
        else:
            pnl_val = " --- "

        lines.append(f"{sym:9s} | {b_short:8s} | {inv_s:>5s} | {pnl_val:>5s}")

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
    
    for idx, arg in enumerate(sys.argv):
        if arg == "--webhook" and idx + 1 < len(sys.argv):
            target_wh = sys.argv[idx + 1].strip()
            send_discord_flag = True

    print(f"[Status Tool] 20銘柄の最新選定・除外ステータスを集計中... (送信先: {target_wh if send_discord_flag else 'なし'})")
    display_and_send_symbols_status(send_to_discord=send_discord_flag, webhook_name=target_wh)
