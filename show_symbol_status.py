#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BingX 主要アルト20銘柄 ステータス完全可視化ダッシュボード
(PROJECT_RULES.md 準拠: 全20銘柄の選定・除外理由の完全透明化)
"""
from __future__ import annotations

import json
import sys
import time
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

from symbol_universe import FIXED_TRADABLE_SYMBOLS

# 固定選定銘柄 (主要アルトコイン21銘柄 / symbol_universe.py 単一正本準拠)
FIXED_SYMBOLS = list(FIXED_TRADABLE_SYMBOLS)

DATA_DIR = Path(__file__).resolve().parent / "Data"
JST = timezone(timedelta(hours=9))


def get_macro_gate_summary() -> Dict[str, Any]:
    """Data/market_state.json より3段階全体地合い情報 (14d/7d/2d) を取得"""
    state_file = DATA_DIR / "market_state.json"
    default_res = {
        "overall_regime": "NEUTRAL",
        "regime_gate": "ALLOW",
        "regime_desc": "NEUTRAL (3期間混在・慎重運用)",
        "w14": "NEUTRAL",
        "w7": "NEUTRAL",
        "w2": "NEUTRAL",
        "excluded_symbols": [],
        "exclusion_reasons": {},
        "recommended_symbols": [],
        "strength_tier": "BALANCED",
        "btc_ret14d": 0.0,
        "sym_ret14d_map": {},
        "h3b_wf90d": {},
    }
    wf_file = DATA_DIR / "h3b_wf90d_state.json"
    wf_fallback: Dict[str, Any] = {}
    if wf_file.exists():
        try:
            wf_fallback = json.loads(wf_file.read_text(encoding="utf-8"))
        except Exception:
            wf_fallback = {}
    if not state_file.exists():
        default_res["h3b_wf90d"] = wf_fallback
        return default_res
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            overall = data.get("overall_regime", "NEUTRAL")
            tier = str((data.get("regime_strength") or {}).get("tier", data.get("strength_tier", "BALANCED"))).upper()
            p_res = data.get("period_results", {})
            if any(k in p_res for k in ("14d", "7d", "2d")):
                w14 = p_res.get("14d", {}).get("state", "NEUTRAL")
                w7 = p_res.get("7d", {}).get("state", "NEUTRAL")
                w2 = p_res.get("2d", {}).get("state", "NEUTRAL")
                desc = f"総合:{overall} (14d:{w14}/7d:{w7}/2d:{w2})"
            else:
                w14 = p_res.get("30d", {}).get("state", "NEUTRAL")
                w7 = p_res.get("10d", {}).get("state", "NEUTRAL")
                w2 = p_res.get("5d", {}).get("state", "NEUTRAL")
                desc = f"総合:{overall} (30d:{w14}/10d:{w7}/5d:{w2})"
            gate = "ALLOW" if overall in ("BUY", "SELL") else "STOP"
            return {
                "overall_regime": overall,
                "strength_tier": tier,
                "regime_gate": gate,
                "regime_desc": desc,
                "w14": w14,
                "w7": w7,
                "w2": w2,
                "excluded_symbols": data.get("excluded_symbols", []),
                "exclusion_reasons": data.get("exclusion_reasons", {}),
                "recommended_symbols": data.get("recommended_symbols", []),
                "btc_ret14d": float(data.get("btc_ret14d", 0.0) or 0.0),
                "sym_ret14d_map": dict(data.get("sym_ret14d_map") or {}),
                "h3b_wf90d": dict(data.get("h3b_wf90d") or wf_fallback or {}),
                "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
    except Exception:
        default_res["h3b_wf90d"] = wf_fallback
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
        "OTHER_REJECTED": 0,
    }
    overall_reg = gate_info.get("overall_regime", "NEUTRAL")
    is_sell_mode = (overall_reg == "SELL")

    for sym in FIXED_SYMBOLS:
        clean_sym = sym.replace("-USDT", "").replace("USDT", "").upper()
        is_peak_dropped = (sym.upper() in excluded_list) or (clean_sym in excluded_list)
        peak_reasons = exclusion_reasons_map.get(sym) or exclusion_reasons_map.get(clean_sym) or []

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
            is_f7_row = bool(opt_info.get("f7_mode"))
            if is_f7_row:
                l_ok = bool(opt_info.get("f7_long_eligible_now"))
                s_ok = bool(opt_info.get("f7_short_eligible_now"))
                noexh_ok = bool(opt_info.get("noexh_ok", True))
                wf_p = bool(opt_info.get("wf90d_paused", False))
                sr14 = float(opt_info.get("sym_ret14d", (gate_info.get("sym_ret14d_map") or {}).get(sym, 0.0)) or 0.0)
                if is_peak_dropped:
                    badge = "🔴 【ノーマライズ除外】"
                    category = "PEAK_DROP"
                    counts["PEAK_DROP"] += 1
                else:
                    badge = "🟢 【F7 監視対象】"
                    category = "SELECTED"
                    counts["SELECTED"] += 1
                s_state = "稼働可" if s_ok else ("WF休止" if wf_p else ("疲れ切除外" if not noexh_ok else "待機"))
                l_state = "稼働可" if l_ok else "待機"
                detail_reason = (
                    f"F7 | L:val_poc_30m(30m/{l_state}) | S:h3b_squeeze_1h(1h/{s_state}) | "
                    f"14d={sr14*100:+.1f}% | NoExh={'OK' if noexh_ok else 'BLOCK'}"
                    f"{' [norm除外]' if is_peak_dropped else ''}"
                )
            else:
                badge = "🟢 【トレード選定・監視中】"
                category = "SELECTED"
                counts["SELECTED"] += 1
                is_short_strat = ("SHORT" in strat or strat.startswith("VAH_POC") or strat == "VAL_BREAK" or is_sell_mode)
                rng_p = opt_info.get("range_params")
                brk_p = opt_info.get("breakout_params")
                if isinstance(rng_p, dict) and isinstance(brk_p, dict) and rng_p and brk_p:
                    is_rq = bool(opt_info.get("is_range_qualified", rng_p.get("is_qualified", False)))
                    is_bq = bool(opt_info.get("is_breakout_qualified", brk_p.get("is_qualified", False)))
                    r_st = str(rng_p.get("strategy", "-")).upper()
                    r_iv = int(rng_p.get("interval", 5) or 5)
                    r_pnl = float(rng_p.get("pnl", 0.0))
                    r_cnt = int(rng_p.get("trade_count", 0))
                    r_mark = "🟢" if is_rq else "⚪"
                    b_st = str(brk_p.get("strategy", "-")).upper()
                    b_iv = int(brk_p.get("interval", 5) or 5)
                    b_pnl = float(brk_p.get("pnl", 0.0))
                    b_cnt = int(brk_p.get("trade_count", 0))
                    b_mark = "🟢" if is_bq else "⚪"
                    detail_reason = (
                        f"🗼東京:{r_mark}{r_st}({r_iv}m/${r_pnl:+.1f}/{r_cnt}回) | "
                        f"🏰欧州:{b_mark}{b_st}({b_iv}m/${b_pnl:+.1f}/{b_cnt}回)"
                    )
                else:
                    dyn_sl = abs(float(opt_info.get("dynamic_mae_stop", opt_info.get("sl_pct", 0.025))))
                    dyn_to = int(opt_info.get("dynamic_timeout_bars", max(3, 240 // max(1, int(interval_val or 5)))))
                    if strat in ("VAH_BREAK", "VAL_BREAK") or strat.startswith("VAL_POC") or strat.startswith("VAH_POC") or strat == "VP_VAL_GC":
                        mp_val = opt_info.get("mp", 200)
                        bins_val = opt_info.get("num_bins", 50)
                        va_pct = int(float(opt_info.get("value_area_pct", 0.70)) * 100)
                        tr_pct = int(float(opt_info.get("target_ratio", 0.70)) * 100)
                        ma_cond = f"{opt_info.get('ma_type', 'EMA' if 'EMA' in strat else 'NONE')}_{opt_info.get('ma_condition', 'close_below' if is_short_strat else 'close_above')}"
                        sl_label = f"直近高値確定足SL(最大{dyn_sl:.2%})" if is_short_strat else f"直近安値確定足SL(最大{dyn_sl:.2%})"
                        detail_reason = f"戦略={strat} ({interval_val}m / MP={mp_val}, Bins={bins_val}, VA={va_pct}%, TR={tr_pct}%, {ma_cond}, {sl_label}, TO={dyn_to}本)"
                    else:
                        ep_val = int(float(opt_info.get("s_ep" if is_short_strat else "l_ep", 60 if is_short_strat else 40)))
                        cp_val = int(float(opt_info.get("s_cp" if is_short_strat else "l_cp", 40 if is_short_strat else 60)))
                        sl_label = f"直近高値確定足SL(最大{dyn_sl:.2%})" if is_short_strat else f"直近安値確定足SL(最大{dyn_sl:.2%})"
                        detail_reason = f"戦略={strat or 'RSIMA'} ({interval_val}m / R9, M7, EP={ep_val}, CP={cp_val}, {sl_label}, TO={dyn_to}本)"
                if is_peak_dropped:
                    detail_reason += " [※3期間比較:前半高値警戒]"
        elif is_peak_dropped:
            if is_sell_mode:
                badge = "🔴 【前半ボトム半値超え除外】"
                category = "PEAK_DROP"
                counts["PEAK_DROP"] += 1
                if isinstance(peak_reasons, str):
                    detail_reason = peak_reasons
                elif isinstance(peak_reasons, list) and peak_reasons:
                    detail_reason = ", ".join(str(r) for r in peak_reasons)
                else:
                    detail_reason = "3期間ノーマライズ前半安値・底打ち反発除外"
            else:
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
        elif pnl is not None and pnl > 0 and trade_count >= 10:
            badge = "⚪ 【選定上限枠外】"
            category = "OVER_LIMIT"
            counts["OVER_LIMIT"] += 1
            detail_reason = f"個別最適化合格 (Net PnL:+${pnl:.2f}/{trade_count}回) だが最大監視枠超過のため待機"
        else:
            badge = "🔴 【個別最適化不合格】"
            category = "OPT_REJECTED"
            counts["OPT_REJECTED"] += 1
            rng_p = opt_info.get("range_params")
            brk_p = opt_info.get("breakout_params")
            if isinstance(rng_p, dict) and isinstance(brk_p, dict) and rng_p and brk_p:
                r_st = str(rng_p.get("strategy", "-")).upper()
                r_pnl = float(rng_p.get("pnl", 0.0))
                r_cnt = int(rng_p.get("trade_count", 0))
                b_st = str(brk_p.get("strategy", "-")).upper()
                b_pnl = float(brk_p.get("pnl", 0.0))
                b_cnt = int(brk_p.get("trade_count", 0))
                detail_reason = (
                    f"二重頑健性未達 [🗼東京:{r_st}(${r_pnl:+.1f}/{r_cnt}回) | 🏰欧州:{b_st}(${b_pnl:+.1f}/{b_cnt}回)]"
                )
            elif pnl is not None:
                detail_reason = f"個別最適化不合格 (Net PnL: ${pnl:+.2f}, 取引: {trade_count}回, 二重頑健性未達: 前半黒字かつ後半加速が必要)"
            else:
                detail_reason = "直近データ不足または二重頑健性基準(10回以上＆前半黒字＆後半加速)未達"

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

    wf_d = gate_info.get("h3b_wf90d") or {}
    wf_lbl = "PAUSED" if wf_d.get("paused") else "ACTIVE"
    btc14 = float(gate_info.get("btc_ret14d", 0.0) or 0.0) * 100.0
    lines = []
    lines.append("=" * 118)
    lines.append(f"📊 【主要アルト20銘柄 選定・除外ステータス完全一覧ダッシュボード】 (評価: {eval_time[:19]})")
    lines.append(f"🛡️  3期間全体地合いディフェンス: {g_icon} [{gate_info['regime_gate']}] {gate_info['regime_desc']}")
    lines.append(f"🏛️  F7_30mNoEU_S8bNoExh_WF90d | Tier: {gate_info.get('strength_tier', 'BALANCED')} | BTC14d: {btc14:+.1f}% | H3b WF90d: {wf_lbl} (n={wf_d.get('window_trades', 0)}, Net=${float(wf_d.get('window_net', 0.0)):+.2f}, PF={float(wf_d.get('window_pf', 0.0)):.2f})")
    lines.append("※ Net PnL は往復0.08%（Maker 0.02% + Taker 0.06%）手数料を100%控除した実質純利益です")
    lines.append("=" * 118)
    lines.append(
        f"{'#':2s} | {'銘柄':10s} | {'ステータス':24s} | {'採用足':6s} | {'Net PnL':12s} | {'勝率':6s} | {'取引':4s} | {'詳細理由 / 採用戦略'}"
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
    overall_val = gate_info.get("overall_regime", "NEUTRAL")
    peak_label = "前半ボトム反発除外" if overall_val == "SELL" else "前半ピーク割れ除外"
    lines.append(
        f"💡 【内訳】選定・監視中: {total_sel}銘柄 | {peak_label}: {total_peak}銘柄 | "
        f"最適化不合格: {total_opt}銘柄 | 上限枠外: {total_over}銘柄"
    )
    lines.append("=" * 118)
    return "\n".join(lines)


def shorten_reason_for_discord(reason: str) -> str:
    """Discord表示用に長大な理由テキストを簡潔に要約"""
    if not reason:
        return ""
    if "前半ピーク半値割れ" in reason:
        matched_wins = []
        for w in ["2d", "7d", "14d", "5d", "10d", "30d"]:
            if f"{w}前半ピーク" in reason:
                matched_wins.append(w)
        win_str = "/".join(matched_wins) if matched_wins else "3期間"
        bt_part = ""
        if "BT" in reason:
            import re
            m = re.search(r"\((?:参考BT|BT不合格):[^)]+\)", reason)
            if m:
                bt_part = f" {m.group(0)}"
        return f"{win_str}前半ピーク警戒{bt_part}"
    elif "前半ボトム半値超え" in reason:
        matched_wins = []
        for w in ["2d", "7d", "14d", "5d", "10d", "30d"]:
            if f"{w}前半ボトム" in reason:
                matched_wins.append(w)
        win_str = "/".join(matched_wins) if matched_wins else "3期間"
        bt_part = ""
        if "BT" in reason:
            import re
            m = re.search(r"\((?:参考BT|BT不合格):[^)]+\)", reason)
            if m:
                bt_part = f" {m.group(0)}"
        return f"{win_str}前半底打ち反発警戒{bt_part}"

    if len(reason) > 50:
        return reason[:47] + "..."
    return reason


def format_status_table_discord(summary_data: Dict[str, Any]) -> str:
    """
    Discord（スマホ表示）に最適化した整形テキスト（2000文字以内厳守）
    """
    counts = summary_data.get("counts", {})
    symbols = summary_data.get("symbols", [])
    eval_time = summary_data.get("evaluated_at", "")[:16].replace("T", " ")
    gate_info = get_macro_gate_summary()
    g_icon = "🟢" if gate_info["regime_gate"] == "ALLOW" else "🛑"

    wf_d = gate_info.get("h3b_wf90d") or {}
    wf_lbl = "PAUSED" if wf_d.get("paused") else "ACTIVE"
    btc14 = float(gate_info.get("btc_ret14d", 0.0) or 0.0) * 100.0
    lines = []
    lines.append(f"📊 **【主要20銘柄 選定・除外ステータス】** (`{eval_time} UTC`)")
    lines.append(f"🛡️ **3期間地合い判定**: {g_icon}`{gate_info['regime_gate']}` ({gate_info['regime_desc']})")
    lines.append(f"🏛️ **F7**: `Tier {gate_info.get('strength_tier', 'BALANCED')}` | `BTC14d {btc14:+.1f}%` | `WF90d {wf_lbl}` (n={wf_d.get('window_trades', 0)}, `${float(wf_d.get('window_net', 0.0)):+.2f}`, PF={float(wf_d.get('window_pf', 0.0)):.2f})")
    lines.append(
        f"💡 **内訳**: 監視: `{counts.get('SELECTED', 0)}` | ピーク割れ除外: `{counts.get('PEAK_DROP', 0)}` | "
        f"最適化不合格: `{counts.get('OPT_REJECTED', 0)}` | 上限枠外: `{counts.get('OVER_LIMIT', 0)}`"
    )

    selected_list = [s for s in symbols if s["category"] == "SELECTED"]
    peak_list = [s for s in symbols if s["category"] == "PEAK_DROP"]

    if selected_list:
        lines.append("\n🟢 **【トレード選定・監視中】**")
        for s in selected_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            strat = s.get("strategy", "")
            inv_s = f"{s.get('interval', 5)}m"
            pnl_str = f"+${s['pnl']:.2f}" if s.get("pnl") is not None else ""
            wr_str = f"勝率{s['win_rate']:.0f}%/{s['trade_count']}回" if s.get("trade_count") else ""
            lines.append(f"• **{sym_clean}**: `{strat}({inv_s})` | {pnl_str} ({wr_str})")

    overall_reg = gate_info.get("overall_regime", "NEUTRAL")
    is_sell = (overall_reg == "SELL")

    if peak_list:
        if is_sell:
            lines.append("\n🔴 **【前半ボトム半値超え除外 (底打ち反発警戒)】**")
        else:
            lines.append("\n🔴 **【前半ピーク半値割れ除外 (ピークアウト警戒)】**")
        for s in peak_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            s_reason = shorten_reason_for_discord(s.get("detail_reason", ""))
            lines.append(f"• **{sym_clean}**: `{s_reason}`")

    lines.append("\n📱 **【全20銘柄 コンパクト一覧】(Net PnL: 往復0.08%控除後)**")
    lines.append("```text")
    lines.append("銘柄      | 状態       | 足種  | Net PnL")
    lines.append("------------------------------------")

    for s in symbols:
        sym = s["symbol"].replace("-USDT", "")
        cat = s["category"]
        if cat == "SELECTED":
            b_short = "🟢選定監視"
        elif cat == "PEAK_DROP":
            b_short = "🔴反発除外" if is_sell else "🔴ピーク割"
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


def send_discord_text_safely(webhook_url: str, text: str, max_chunk: int = 1800) -> None:
    """Discordの2000文字上限を確実に守るため、必要に応じて行単位で分割送信"""
    import requests
    if len(text) <= max_chunk:
        res = requests.post(webhook_url, json={"content": text}, timeout=10)
        res.raise_for_status()
        return

    # 行単位で分割
    chunks = []
    current_lines = []
    current_len = 0
    in_code_block = False

    for line in text.split("\n"):
        line_len = len(line) + 1
        if "```" in line:
            in_code_block = not in_code_block

        if current_len + line_len > max_chunk:
            if in_code_block:
                current_lines.append("```")
            chunks.append("\n".join(current_lines))
            current_lines = []
            if in_code_block:
                current_lines.append("```text")
                current_len = 8
            else:
                current_len = 0

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    for c in chunks:
        if c.strip():
            res = requests.post(webhook_url, json={"content": c}, timeout=10)
            res.raise_for_status()
            time.sleep(0.5)


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

            webhook_url = get_webhook_url(webhook_name) or get_webhook_url("test4_backtest")
            if webhook_url:
                discord_text = format_status_table_discord(summary)
                send_discord_text_safely(webhook_url, discord_text)
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
