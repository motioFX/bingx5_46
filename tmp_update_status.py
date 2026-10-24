# -*- coding: utf-8 -*-
with open('show_symbol_status.py', 'r', encoding='utf-8') as f:
    text = f.read()

text_norm = text.replace('\r\n', '\n')

# 1. gate の判定修正 (84行目付近)
old_gate = 'gate = "STOP" if overall == "SELL" else "ALLOW"'
new_gate = 'gate = "ALLOW" if overall in ("BUY", "SELL") else "STOP"'
assert old_gate in text_norm, "old_gate not found"
text_norm = text_norm.replace(old_gate, new_gate, 1)

# 2. build_symbols_status_summary 内の分類ロジック修正
old_cat_logic = """        if sym in selected_symbols:
            badge = "🟢 【トレード選定・監視中】"
            category = "SELECTED"
            counts["SELECTED"] += 1
            if strat in ("VAL_POC", "VP_VAL_GC"):
                mp_val = opt_info.get("mp", 200)
                bins_val = opt_info.get("num_bins", 50)
                va_pct = int(float(opt_info.get("value_area_pct", 0.70)) * 100)
                ma_cond = f"{opt_info.get('ma_type', 'EMA')}_{opt_info.get('ma_condition', 'close_above')}"
                dyn_sl = float(opt_info.get("dynamic_mae_stop", opt_info.get("sl_pct", 0.025)))
                detail_reason = f"戦略={strat} ({interval_val}m / MP={mp_val}, Bins={bins_val}, VA={va_pct}%, {ma_cond}, 動的SL={dyn_sl:.2%})"
            else:
                l_ep = int(float(opt_info.get("l_ep", 40)))
                l_cp = int(float(opt_info.get("l_cp", 60)))
                dyn_sl = float(opt_info.get("dynamic_mae_stop", opt_info.get("sl_pct", 0.025)))
                detail_reason = f"戦略={strat or 'RSIMA'} ({interval_val}m / R9, M7, EP={l_ep}, CP={l_cp}, 動的SL={dyn_sl:.2%})"
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
                detail_reason = "直近データ不足または第1段階シングル枠(10回以上&黒字)未達" """

new_cat_logic = """        overall_reg = gate_info.get("overall_regime", "NEUTRAL")
        is_sell_mode = (overall_reg == "SELL")

        if sym in selected_symbols:
            side_badge = "(SHORT)" if is_sell_mode else "(LONG)"
            badge = f"🟢 【トレード選定・監視中 {side_badge}】"
            category = "SELECTED"
            counts["SELECTED"] += 1
            if strat in ("VAL_POC", "VAH_POC", "VP_VAL_GC"):
                mp_val = opt_info.get("mp", 200)
                bins_val = opt_info.get("num_bins", 50)
                va_pct = int(float(opt_info.get("value_area_pct", 0.70)) * 100)
                ma_cond = f"{opt_info.get('ma_type', 'EMA')}_{opt_info.get('ma_condition', 'close_above')}"
                dyn_sl = float(opt_info.get("dynamic_mae_stop", opt_info.get("sl_pct", 0.025)))
                detail_reason = f"戦略={strat} ({interval_val}m / MP={mp_val}, Bins={bins_val}, VA={va_pct}%, {ma_cond}, 動的SL={dyn_sl:.2%})"
            else:
                l_ep = int(float(opt_info.get("l_ep", 40)))
                l_cp = int(float(opt_info.get("l_cp", 60)))
                dyn_sl = float(opt_info.get("dynamic_mae_stop", opt_info.get("sl_pct", 0.025)))
                detail_reason = f"戦略={strat or 'RSIMA'} ({interval_val}m / R9, M7, EP={l_ep}, CP={l_cp}, 動的SL={dyn_sl:.2%})"
            if is_peak_dropped:
                detail_reason += " [※3期間比較:前半警戒]"
        elif not is_sell_mode and whale_sig == "SHORT_ONLY":
            badge = "🔴 【大口売り優勢】"
            category = "WHALE_EXCLUDE"
            counts["WHALE_SHORT"] += 1
            detail_reason = "クジラセンチメントが売り偏重 (SHORT_ONLY除外)"
        elif is_sell_mode and whale_sig == "LONG_ONLY":
            badge = "🔴 【大口買い優勢】"
            category = "WHALE_EXCLUDE"
            counts["WHALE_SHORT"] += 1
            detail_reason = "クジラセンチメントが買い偏重 (LONG_ONLY除外)"
        elif pnl is not None and pnl > 0 and trade_count >= 10:
            badge = "⚪ 【選定上限枠外】"
            category = "OVER_LIMIT"
            counts["OVER_LIMIT"] += 1
            detail_reason = f"個別最適化合格 (PnL:+${pnl:.2f}/{trade_count}回) だが最大監視枠超過のため待機"
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
        else:
            badge = "🔴 【個別最適化不合格】"
            category = "OPT_REJECTED"
            counts["OPT_REJECTED"] += 1
            if pnl is not None:
                detail_reason = f"個別最適化不合格 (PnL: ${pnl:+.2f}, 取引: {trade_count}回 < 10回基準またはマイナス, 勝率: {win_rate:.0f}%)"
            else:
                detail_reason = "直近データ不足または第1段階シングル枠(10回以上&黒字)未達" """

assert old_cat_logic in text_norm, "old_cat_logic not found"
text_norm = text_norm.replace(old_cat_logic, new_cat_logic, 1)

# 3. Discord テキスト生成部分の修正
old_discord_text = """    if peak_list:
        lines.append("\\n🔴 **【前半ピーク半値割れ除外 (ピークアウト警戒)】**")
        for s in peak_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            s_reason = shorten_reason_for_discord(s.get("detail_reason", ""))
            lines.append(f"• **{sym_clean}**: `{s_reason}`")

    if whale_list:
        lines.append("\\n🔴 **【大口売り優勢 (Whale除外)】**")
        for s in whale_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            lines.append(f"• **{sym_clean}**: `クジラ判定売り偏重 (SHORT_ONLY)`")"""

new_discord_text = """    overall_reg = macro.get("overall_regime", "NEUTRAL")
    is_sell = (overall_reg == "SELL")

    if peak_list:
        if is_sell:
            lines.append("\\n🔴 **【前半ボトム半値超え除外 (底打ち反発警戒)】**")
        else:
            lines.append("\\n🔴 **【前半ピーク半値割れ除外 (ピークアウト警戒)】**")
        for s in peak_list:
            sym_clean = s["symbol"].replace("-USDT", "")
            s_reason = shorten_reason_for_discord(s.get("detail_reason", ""))
            lines.append(f"• **{sym_clean}**: `{s_reason}`")

    if whale_list:
        if is_sell:
            lines.append("\\n🔴 **【大口買い優勢 (Whale除外)】**")
            for s in whale_list:
                sym_clean = s["symbol"].replace("-USDT", "")
                lines.append(f"• **{sym_clean}**: `クジラ判定買い偏重 (LONG_ONLY)`")
        else:
            lines.append("\\n🔴 **【大口売り優勢 (Whale除外)】**")
            for s in whale_list:
                sym_clean = s["symbol"].replace("-USDT", "")
                lines.append(f"• **{sym_clean}**: `クジラ判定売り偏重 (SHORT_ONLY)`")"""

assert old_discord_text in text_norm, "old_discord_text not found"
text_norm = text_norm.replace(old_discord_text, new_discord_text, 1)

# CRLF 書き戻し
text_crlf = text_norm.replace('\n', '\r\n')
with open('show_symbol_status.py', 'w', encoding='utf-8') as f:
    f.write(text_crlf)
print("SUCCESS_UPDATE_SHOW_SYMBOL_STATUS")
