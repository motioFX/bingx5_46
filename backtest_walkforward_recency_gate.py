"""本番完全準拠ウォークフォワード検証 (個別最適化 108/126通り + Recency Gate + セッション別デュアル保持)
- 方式A (本番準拠): 過去30日(8,640本)で最適化 & Recency Gate判定 ➔ 次の7日(2,016本)にOut-of-Sample適用
- 方式B (中期検証): 過去90日(25,920本)で最適化 & Recency Gate判定 ➔ 次の30日(8,640本)にOut-of-Sample適用
- 全20銘柄・2023〜2026年 (403,330本) で検証
"""
import sys, glob, os, time
import numpy as np, pandas as pd
import numba as nb
sys.stdout.reconfigure(encoding="utf-8")
import bingx5_46_3logic as L
from scratch.verify_numba_vp import make_mp_fast

L.time.sleep = lambda x: None

# ==============================================================================
# Numba JIT シミュレータ群 (bingx5_46_3logic.py の simulate_*_fast と 100% 同一ロジック)
# 戻り値:
#   stats: (final_pnl, pnl_1st, pnl_2nd, pnl_7d, trades_7d, trade_cnt, win_cnt, max_dd)
#   trades_out: shape (max_possible, 3) -> [entry_bar, exit_bar, pnl]
# ==============================================================================

@nb.njit(cache=True)
def sim_val_poc_nb(closes, highs, lows, vals, vahs, pocs, ma_filter, has_filter,
                   trade_size, sl_pct, target_ratio, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    fee_rate = 0.0006
    maker_fee = 0.0002

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    entry_sl_px = 0.0
    pos_count = 0
    last_entry = 0.0
    entry_bar_idx = -1
    cooldown_until = -1
    pending_limit = -1.0

    cum_pnl = 0.0
    pnl_first_half = 0.0
    pnl_second_half = 0.0
    pnl_recent_7d = 0.0
    trades_recent_7d = 0
    trade_cnt = 0
    win_cnt = 0
    peak_eq = trade_size
    max_dd = 0.0

    w_len = max(15, min(40, int(n * 0.05))) if n > 0 else 20
    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        vah = vahs[i]

        if pending_limit > 0.0:
            if l <= pending_limit:
                px = pending_limit
                qty = trade_size / px
                fee = trade_size * maker_fee
                pos_cost += trade_size + fee
                pos_qty += qty
                avg_price = pos_cost / pos_qty
                last_entry = px
                pos_count += 1
                if pos_count == 1:
                    entry_bar_idx = i
                    s0 = max(0, i - 10)
                    rec_l = lows[s0]
                    for k in range(s0 + 1, i + 1):
                        if lows[k] < rec_l:
                            rec_l = lows[k]
                    c1 = rec_l if rec_l < avg_price * 0.9980 else avg_price * 0.9980
                    c2 = avg_price * (1.0 - sl_pct)
                    entry_sl_px = c1 if c1 > c2 else c2
            pending_limit = -1.0

        if pos_qty > 0.0:
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * target_ratio if va_diff > 0.0 else (last_entry * 0.030)

            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c < entry_sl_px:
                is_sl = True
                sl_exec_px = c
            elif l <= avg_price * 0.970:
                is_sl = True
                sl_exec_px = avg_price * 0.970
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_price:
                is_sl = True
                sl_exec_px = c

            if is_sl:
                sell_val = pos_qty * sl_exec_px
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                if i < half_idx:
                    pnl_first_half += pnl
                else:
                    pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl
                    trades_recent_7d += 1
                entries[trade_cnt] = entry_bar_idx
                exits[trade_cnt] = i
                pnls[trade_cnt] = pnl
                trade_cnt += 1
                if pnl > 0.0:
                    win_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0
                pos_count = 0; last_entry = 0.0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                target_tp = val + va_spacing if va_spacing > 0.0 else pocs[i]
                if (h >= target_tp or c >= target_tp) and target_tp > 0.0:
                    exit_px = (c if c > target_tp else target_tp) if h >= target_tp else c
                    if exit_px >= avg_price * 1.0040:
                        sell_val = pos_qty * exit_px
                        fee = sell_val * fee_rate
                        pnl = sell_val - pos_cost - fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        entries[trade_cnt] = entry_bar_idx
                        exits[trade_cnt] = i
                        pnls[trade_cnt] = pnl
                        trade_cnt += 1
                        if pnl > 0.0:
                            win_cnt += 1
                        pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0
                        pos_count = 0; last_entry = 0.0; entry_bar_idx = -1

        if pending_limit < 0.0 and pos_count == 0 and i >= cooldown_until:
            if not np.isnan(val) and not np.isnan(vals[i - 1]):
                val_crossover = (closes[i - 1] <= vals[i - 1]) and (c > val)
                is_lower = c <= (val + (vah - val) * 0.50) if vah > val else True
                if val_crossover and is_lower:
                    va_width = (vah - val) / val if val > 0.0 else 0.0
                    if va_width >= 0.015 and (not has_filter or ma_filter[i]):
                        w_start = max(0, i - w_len)
                        in_cnt = 0
                        tot_w = i - w_start + 1
                        low_b = val * 0.998
                        high_b = vah * 1.002
                        for k in range(w_start, i + 1):
                            if low_b <= closes[k] <= high_b:
                                in_cnt += 1
                        if (in_cnt / tot_w) >= 0.70:
                            pending_limit = c * 0.9998

        cur_eq = trade_size + cum_pnl + (pos_qty * c - pos_cost if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq:
            peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd:
            max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trade_cnt, win_cnt, max_dd,
            entries[:trade_cnt], exits[:trade_cnt], pnls[:trade_cnt])


@nb.njit(cache=True)
def sim_vah_poc_nb(closes, highs, lows, vals, vahs, pocs, ma_filter, has_filter,
                   trade_size, sl_pct, target_ratio, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    fee_rate = 0.0006
    maker_fee = 0.0002

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    entry_sl_px = 0.0
    pos_count = 0
    entry_bar_idx = -1
    cooldown_until = -1
    pending_limit = -1.0

    cum_pnl = 0.0
    pnl_first_half = 0.0
    pnl_second_half = 0.0
    pnl_recent_7d = 0.0
    trades_recent_7d = 0
    trade_cnt = 0
    win_cnt = 0
    peak_eq = trade_size
    max_dd = 0.0

    w_len = max(15, min(40, int(n * 0.05))) if n > 0 else 20
    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        vah = vahs[i]

        if pending_limit > 0.0:
            if h >= pending_limit:
                px = pending_limit
                qty = trade_size / px
                pos_cost = trade_size
                pos_qty = qty
                avg_price = px
                pos_count = 1
                entry_bar_idx = i
                s0 = max(0, i - 10)
                rec_h = highs[s0]
                for k in range(s0 + 1, i + 1):
                    if highs[k] > rec_h:
                        rec_h = highs[k]
                c1 = rec_h if rec_h > avg_price * 1.0020 else avg_price * 1.0020
                c2 = avg_price * (1.0 + sl_pct)
                entry_sl_px = c1 if c1 < c2 else c2
            pending_limit = -1.0

        if pos_qty > 0.0:
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * target_ratio if va_diff > 0.0 else (avg_price * 0.030)

            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c > entry_sl_px:
                is_sl = True
                sl_exec_px = c
            elif h >= avg_price * 1.030:
                is_sl = True
                sl_exec_px = avg_price * 1.030
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_price:
                is_sl = True
                sl_exec_px = c

            if is_sl:
                buy_val = pos_qty * sl_exec_px
                fee = buy_val * fee_rate
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx:
                    pnl_first_half += pnl
                else:
                    pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl
                    trades_recent_7d += 1
                entries[trade_cnt] = entry_bar_idx
                exits[trade_cnt] = i
                pnls[trade_cnt] = pnl
                trade_cnt += 1
                if pnl > 0.0:
                    win_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0
                pos_count = 0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                target_tp = vah - va_spacing if va_spacing > 0.0 else pocs[i]
                if (l <= target_tp or c <= target_tp) and target_tp > 0.0:
                    exit_px = (c if c < target_tp else target_tp) if l <= target_tp else c
                    if exit_px <= avg_price * 0.9960:
                        buy_val = pos_qty * exit_px
                        fee = buy_val * fee_rate
                        entry_fee = pos_cost * maker_fee
                        pnl = pos_cost - buy_val - fee - entry_fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        entries[trade_cnt] = entry_bar_idx
                        exits[trade_cnt] = i
                        pnls[trade_cnt] = pnl
                        trade_cnt += 1
                        if pnl > 0.0:
                            win_cnt += 1
                        pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0
                        pos_count = 0; entry_bar_idx = -1

        if pending_limit < 0.0 and pos_count == 0 and i >= cooldown_until:
            if not np.isnan(vah) and not np.isnan(vahs[i - 1]):
                vah_crossunder = (closes[i - 1] >= vahs[i - 1]) and (c < vah)
                is_upper = c >= (val + (vah - val) * 0.50) if vah > val else True
                if vah_crossunder and is_upper:
                    va_width = (vah - val) / val if val > 0.0 else 0.0
                    if va_width >= 0.015 and (not has_filter or ma_filter[i]):
                        w_start = max(0, i - w_len)
                        in_cnt = 0
                        tot_w = i - w_start + 1
                        low_b = val * 0.998
                        high_b = vah * 1.002
                        for k in range(w_start, i + 1):
                            if low_b <= closes[k] <= high_b:
                                in_cnt += 1
                        if (in_cnt / tot_w) >= 0.70:
                            pending_limit = c * 1.0002

        cur_eq = trade_size + cum_pnl + (pos_cost - pos_qty * c if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq:
            peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd:
            max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trade_cnt, win_cnt, max_dd,
            entries[:trade_cnt], exits[:trade_cnt], pnls[:trade_cnt])


@nb.njit(cache=True)
def sim_vah_break_nb(closes, highs, lows, vals, vahs, pocs, ma_filter, has_filter,
                     trade_size, sl_pct, target_ratio, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    fee_rate = 0.0006
    maker_fee = 0.0002

    pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
    pos_count = 0; entry_bar_idx = -1; cooldown_until = -1; pending_limit = -1.0
    cum_pnl = 0.0; pnl_first_half = 0.0; pnl_second_half = 0.0; pnl_recent_7d = 0.0
    trades_recent_7d = 0; trade_cnt = 0; win_cnt = 0
    peak_eq = trade_size; max_dd = 0.0

    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(1, n):
        c = closes[i]; h = highs[i]; l = lows[i]
        val = vals[i]; vah = vahs[i]; poc = pocs[i]

        if pending_limit > 0.0:
            if l <= pending_limit:
                px = pending_limit
                qty = trade_size / px
                fee = trade_size * maker_fee
                pos_cost = trade_size + fee
                pos_qty = qty
                avg_price = pos_cost / pos_qty
                pos_count = 1
                entry_bar_idx = i
                va_diff_e = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                raw_sl = vah - va_diff_e * 0.35 if va_diff_e > 0.0 else avg_price * 0.985
                poc_floor = poc * 1.001 if (not np.isnan(poc) and poc > 0.0 and poc < avg_price) else raw_sl
                calc_sl = raw_sl if raw_sl > poc_floor else poc_floor
                d1 = avg_price * 0.004
                d2 = avg_price - calc_sl
                sl_dist = d1 if d1 > d2 else d2
                entry_sl_px = avg_price - sl_dist
                target_tp = avg_price + sl_dist * 2.0
            pending_limit = -1.0

        if pos_qty > 0.0:
            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c <= entry_sl_px:
                is_sl = True
                sl_exec_px = c if c < entry_sl_px else entry_sl_px
            elif sl_pct > 0.0 and c <= avg_price * (1.0 - sl_pct):
                is_sl = True
                sl_exec_px = c
            elif not np.isnan(poc) and poc > 0.0 and c < poc and c < avg_price * 0.9960:
                is_sl = True
                sl_exec_px = c
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_price:
                is_sl = True
                sl_exec_px = c

            if is_sl:
                sell_val = pos_qty * sl_exec_px
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trade_cnt] = entry_bar_idx; exits[trade_cnt] = i; pnls[trade_cnt] = pnl
                trade_cnt += 1
                if pnl > 0.0: win_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
                pos_count = 0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                if (h >= target_tp or c >= target_tp) and target_tp > 0.0:
                    exit_px = (c if c > target_tp else target_tp) if h >= target_tp else c
                    if exit_px >= avg_price * 1.0040:
                        sell_val = pos_qty * exit_px
                        fee = sell_val * fee_rate
                        pnl = sell_val - pos_cost - fee
                        cum_pnl += pnl
                        if i < half_idx: pnl_first_half += pnl
                        else: pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl; trades_recent_7d += 1
                        entries[trade_cnt] = entry_bar_idx; exits[trade_cnt] = i; pnls[trade_cnt] = pnl
                        trade_cnt += 1
                        if pnl > 0.0: win_cnt += 1
                        pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
                        pos_count = 0; entry_bar_idx = -1

        if pending_limit < 0.0 and pos_count == 0 and i >= cooldown_until:
            if not np.isnan(vah) and not np.isnan(vahs[i - 1]) and not np.isnan(val):
                va_diff_e = vah - val if vah > val else 0.0
                vah_breakout = (closes[i - 1] <= vahs[i - 1]) and (c > vah)
                not_overextended = (c <= vah + va_diff_e * 0.35) if va_diff_e > 0.0 else True
                va_width = va_diff_e / val if val > 0.0 else 0.0
                if vah_breakout and not_overextended and (va_width >= 0.015) and (not has_filter or ma_filter[i]):
                    pending_limit = c * 0.9998

        cur_eq = trade_size + cum_pnl + (pos_qty * c - pos_cost if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq: peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd: max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trade_cnt, win_cnt, max_dd,
            entries[:trade_cnt], exits[:trade_cnt], pnls[:trade_cnt])


@nb.njit(cache=True)
def sim_val_break_nb(closes, highs, lows, vals, vahs, pocs, ma_filter, has_filter,
                     trade_size, sl_pct, target_ratio, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    fee_rate = 0.0006
    maker_fee = 0.0002

    pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
    pos_count = 0; entry_bar_idx = -1; cooldown_until = -1; pending_limit = -1.0
    cum_pnl = 0.0; pnl_first_half = 0.0; pnl_second_half = 0.0; pnl_recent_7d = 0.0
    trades_recent_7d = 0; trade_cnt = 0; win_cnt = 0
    peak_eq = trade_size; max_dd = 0.0

    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(1, n):
        c = closes[i]; h = highs[i]; l = lows[i]
        val = vals[i]; vah = vahs[i]; poc = pocs[i]

        if pending_limit > 0.0:
            if h >= pending_limit:
                px = pending_limit
                qty = trade_size / px
                pos_cost = trade_size
                pos_qty = qty
                avg_price = px
                pos_count = 1
                entry_bar_idx = i
                va_diff_e = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                raw_sl = val + va_diff_e * 0.35 if va_diff_e > 0.0 else avg_price * 1.015
                poc_ceiling = poc * 0.999 if (not np.isnan(poc) and poc > 0.0 and poc > avg_price) else raw_sl
                calc_sl = raw_sl if raw_sl < poc_ceiling else poc_ceiling
                d1 = avg_price * 0.004
                d2 = calc_sl - avg_price
                sl_dist = d1 if d1 > d2 else d2
                entry_sl_px = avg_price + sl_dist
                t1 = avg_price * 0.50
                t2 = avg_price - sl_dist * 2.0
                target_tp = t1 if t1 > t2 else t2
            pending_limit = -1.0

        if pos_qty > 0.0:
            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c >= entry_sl_px:
                is_sl = True
                sl_exec_px = c if c > entry_sl_px else entry_sl_px
            elif not np.isnan(poc) and poc > 0.0 and c > poc and c > avg_price * 1.0040:
                is_sl = True
                sl_exec_px = c
            elif h >= avg_price * 1.030:
                is_sl = True
                sl_exec_px = avg_price * 1.030
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_price:
                is_sl = True
                sl_exec_px = c

            if is_sl:
                buy_val = pos_qty * sl_exec_px
                fee = buy_val * fee_rate
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trade_cnt] = entry_bar_idx; exits[trade_cnt] = i; pnls[trade_cnt] = pnl
                trade_cnt += 1
                if pnl > 0.0: win_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
                pos_count = 0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                if (l <= target_tp or c <= target_tp) and target_tp > 0.0:
                    exit_px = (c if c < target_tp else target_tp) if l <= target_tp else c
                    if exit_px <= avg_price * 0.9960:
                        buy_val = pos_qty * exit_px
                        fee = buy_val * fee_rate
                        entry_fee = pos_cost * maker_fee
                        pnl = pos_cost - buy_val - fee - entry_fee
                        cum_pnl += pnl
                        if i < half_idx: pnl_first_half += pnl
                        else: pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl; trades_recent_7d += 1
                        entries[trade_cnt] = entry_bar_idx; exits[trade_cnt] = i; pnls[trade_cnt] = pnl
                        trade_cnt += 1
                        if pnl > 0.0: win_cnt += 1
                        pos_qty = 0.0; pos_cost = 0.0; avg_price = 0.0; entry_sl_px = 0.0; target_tp = 0.0
                        pos_count = 0; entry_bar_idx = -1

        if pending_limit < 0.0 and pos_count == 0 and i >= cooldown_until:
            if not np.isnan(val) and not np.isnan(vals[i - 1]) and not np.isnan(vah):
                va_diff_e = vah - val if vah > val else 0.0
                val_breakdown = (closes[i - 1] >= vals[i - 1]) and (c < val)
                not_overextended = (c >= val - va_diff_e * 0.35) if va_diff_e > 0.0 else True
                va_width = va_diff_e / val if val > 0.0 else 0.0
                if val_breakdown and not_overextended and (va_width >= 0.015) and (not has_filter or ma_filter[i]):
                    pending_limit = c * 1.0002

        cur_eq = trade_size + cum_pnl + (pos_cost - pos_qty * c if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq: peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd: max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trade_cnt, win_cnt, max_dd,
            entries[:trade_cnt], exits[:trade_cnt], pnls[:trade_cnt])


@nb.njit(cache=True)
def sim_rsima_long_nb(closes, highs, lows, rsi, rsi_ma, ma_filter, has_filter,
                      trade_size, ep, cp, sl_pct, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    maker_fee = 0.0002; taker_fee = 0.0006

    pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0
    pos_cnt = 0; entry_bar_idx = -1; pending_px = -1.0; cooldown_until = -1
    cum_pnl = 0.0; pnl_first_half = 0.0; pnl_second_half = 0.0; pnl_recent_7d = 0.0
    trades_recent_7d = 0; trades_cnt = 0; wins_cnt = 0
    peak_eq = trade_size; max_dd = 0.0

    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(2, n):
        c = closes[i]; l = lows[i]
        if pending_px > 0.0:
            if l <= pending_px:
                px = pending_px
                qty = trade_size / px
                pos_cost = trade_size + trade_size * maker_fee
                pos_qty = qty
                avg_px = pos_cost / pos_qty
                pos_cnt = 1
                entry_bar_idx = i
                s0 = max(0, i - 10)
                rec_l = lows[s0]
                for k in range(s0 + 1, i + 1):
                    if lows[k] < rec_l: rec_l = lows[k]
                c1 = rec_l if rec_l < avg_px * 0.9980 else avg_px * 0.9980
                c2 = avg_px * (1.0 - sl_pct)
                entry_sl_px = c1 if c1 > c2 else c2
            pending_px = -1.0

        if pos_qty > 0.0:
            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c < entry_sl_px:
                is_sl = True; sl_exec_px = c
            elif l <= avg_px * 0.970:
                is_sl = True; sl_exec_px = avg_px * 0.970
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_px:
                is_sl = True; sl_exec_px = c

            if is_sl:
                sell_val = pos_qty * sl_exec_px
                fee = sell_val * taker_fee
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx: pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trades_cnt] = entry_bar_idx; exits[trades_cnt] = i; pnls[trades_cnt] = pnl
                trades_cnt += 1
                if pnl > 0.0: wins_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0; pos_cnt = 0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            elif rsi[i] > cp and c >= avg_px * 1.004:
                sell_val = pos_qty * c
                fee = sell_val * taker_fee
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx: pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trades_cnt] = entry_bar_idx; exits[trades_cnt] = i; pnls[trades_cnt] = pnl
                trades_cnt += 1
                if pnl > 0.0: wins_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0; pos_cnt = 0; entry_bar_idx = -1
        elif pending_px < 0.0 and i >= cooldown_until:
            gc = (rsi[i] > rsi_ma[i]) and (rsi[i - 1] <= rsi_ma[i - 1])
            if gc and ((rsi_ma[i] < ep) or (rsi_ma[i - 1] < ep)) and (not has_filter or ma_filter[i]):
                pending_px = c * 0.9998

        cur_eq = trade_size + cum_pnl + (pos_qty * c - pos_cost if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq: peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd: max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trades_cnt, wins_cnt, max_dd,
            entries[:trades_cnt], exits[:trades_cnt], pnls[:trades_cnt])


@nb.njit(cache=True)
def sim_rsima_short_nb(closes, highs, lows, rsi, rsi_ma, ma_filter, has_filter,
                       trade_size, s_ep, s_cp, sl_pct, cooldown_bars, timeout_bars):
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    maker_fee = 0.0002; taker_fee = 0.0006

    pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0
    pos_cnt = 0; entry_bar_idx = -1; pending_px = -1.0; cooldown_until = -1
    cum_pnl = 0.0; pnl_first_half = 0.0; pnl_second_half = 0.0; pnl_recent_7d = 0.0
    trades_recent_7d = 0; trades_cnt = 0; wins_cnt = 0
    peak_eq = trade_size; max_dd = 0.0

    entries = np.empty(n // 2 + 2, dtype=np.int64)
    exits = np.empty(n // 2 + 2, dtype=np.int64)
    pnls = np.empty(n // 2 + 2, dtype=np.float64)

    for i in range(2, n):
        c = closes[i]; h = highs[i]
        if pending_px > 0.0:
            if h >= pending_px:
                px = pending_px
                qty = trade_size / px
                pos_cost = trade_size
                pos_qty = qty
                avg_px = px
                pos_cnt = 1
                entry_bar_idx = i
                s0 = max(0, i - 10)
                rec_h = highs[s0]
                for k in range(s0 + 1, i + 1):
                    if highs[k] > rec_h: rec_h = highs[k]
                c1 = rec_h if rec_h > avg_px * 1.0020 else avg_px * 1.0020
                c2 = avg_px * (1.0 + sl_pct)
                entry_sl_px = c1 if c1 < c2 else c2
            pending_px = -1.0

        if pos_qty > 0.0:
            is_sl = False
            sl_exec_px = c
            if entry_sl_px > 0.0 and c > entry_sl_px:
                is_sl = True; sl_exec_px = c
            elif h >= avg_px * 1.030:
                is_sl = True; sl_exec_px = avg_px * 1.030
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_px:
                is_sl = True; sl_exec_px = c

            if is_sl:
                buy_val = pos_qty * sl_exec_px
                fee = buy_val * taker_fee
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx: pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trades_cnt] = entry_bar_idx; exits[trades_cnt] = i; pnls[trades_cnt] = pnl
                trades_cnt += 1
                if pnl > 0.0: wins_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0; pos_cnt = 0; entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            elif rsi[i] < s_cp and c <= avg_px * 0.996:
                buy_val = pos_qty * c
                fee = buy_val * taker_fee
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx: pnl_first_half += pnl
                else: pnl_second_half += pnl
                if i >= recent_7d_idx: pnl_recent_7d += pnl; trades_recent_7d += 1
                entries[trades_cnt] = entry_bar_idx; exits[trades_cnt] = i; pnls[trades_cnt] = pnl
                trades_cnt += 1
                if pnl > 0.0: wins_cnt += 1
                pos_qty = 0.0; pos_cost = 0.0; avg_px = 0.0; entry_sl_px = 0.0; pos_cnt = 0; entry_bar_idx = -1
        elif pending_px < 0.0 and i >= cooldown_until:
            dc = (rsi[i] < rsi_ma[i]) and (rsi[i - 1] >= rsi_ma[i - 1]) and (rsi_ma[i] <= rsi_ma[i - 1])
            if dc and ((rsi_ma[i] > s_ep) or (rsi_ma[i - 1] > s_ep)) and (not has_filter or ma_filter[i]):
                pending_px = c * 1.0002

        cur_eq = trade_size + cum_pnl + (pos_cost - pos_qty * c if pos_qty > 0.0 else 0.0)
        if cur_eq > peak_eq: peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd: max_dd = cur_dd

    return (cum_pnl, pnl_first_half, pnl_second_half, pnl_recent_7d,
            trades_recent_7d, trades_cnt, wins_cnt, max_dd,
            entries[:trades_cnt], exits[:trades_cnt], pnls[:trades_cnt])


def score_candidate(pnl, p1st, p2nd, p7d, c7d, cnt, min_trades=10):
    if cnt >= min_trades and p1st > 0.0 and p2nd > p1st and p7d > 0.0 and c7d >= 1:
        return 1000.0 + p2nd * 1.0 + p7d * 1.0 + pnl * 0.2, 1  # Tier 1
    elif cnt >= min_trades and p1st > 0.0 and p2nd > p1st:
        return 500.0 + p2nd + pnl * 0.1, 2  # Tier 2
    elif cnt >= 5 and pnl > 0.0:
        return pnl, 0
    else:
        return -1000.0 + pnl, 0


def run_walkforward_for_symbol(sym, df_5m, reg_code_5m):
    """1銘柄分の全パラメータ系列を事前構築し、方式A(30d->7d)と方式B(90d->30d)のOOSトレードを抽出する"""
    t0 = time.time()
    SIZE = 15.0
    MP_LIST = [100, 150, 200, 250, 300, 350, 400, 450, 500]
    TR_LIST = [0.50, 0.60, 0.70, 0.80]
    BINS = 50

    # 5m arrays
    c5 = df_5m["close"].astype(float).values
    h5 = df_5m["high"].astype(float).values
    l5 = df_5m["low"].astype(float).values
    dt5 = df_5m["dt"].values
    n5 = len(c5)

    flt5_vp_l = L.build_entry_precision_filter(df_5m, side="long", is_vp=True, is_breakout=False)
    flt5_vb_l = L.build_entry_precision_filter(df_5m, side="long", is_vp=True, is_breakout=True)
    flt5_rs_l = L.build_entry_precision_filter(df_5m, side="long", is_vp=False, is_breakout=False)
    flt5_vp_s = L.build_entry_precision_filter(df_5m, side="short", is_vp=True, is_breakout=False)
    flt5_vb_s = L.build_entry_precision_filter(df_5m, side="short", is_vp=True, is_breakout=True)
    flt5_rs_s = L.build_entry_precision_filter(df_5m, side="short", is_vp=False, is_breakout=False)

    rsi5_s, rsima5_s = L.calc_rsi_ema(df_5m["close"].astype(float), rsi_period=9, ma_period=7)
    rsi5 = rsi5_s.values; rsima5 = rsima5_s.values

    mp5_cache = {}
    for mp in MP_LIST:
        res = make_mp_fast(df_5m, period=mp, num_bins=BINS, step=8, use_decay=True)
        mp5_cache[mp] = (res["val_70"], res["vah_70"], res["poc"])

    # 15m arrays
    df_15m = L.resample_candles(df_5m, 15).reset_index(drop=True)
    df_15m["dt"] = pd.to_datetime(df_15m["timestamp"], utc=True)
    c15 = df_15m["close"].astype(float).values
    h15 = df_15m["high"].astype(float).values
    l15 = df_15m["low"].astype(float).values
    dt15 = df_15m["dt"].values
    reg_code_15m = pd.Series(reg_code_5m, index=df_5m["dt"]).reindex(df_15m["dt"], method="ffill").values

    flt15_vp_l = L.build_entry_precision_filter(df_15m, side="long", is_vp=True, is_breakout=False)
    flt15_rs_l = L.build_entry_precision_filter(df_15m, side="long", is_vp=False, is_breakout=False)
    flt15_vp_s = L.build_entry_precision_filter(df_15m, side="short", is_vp=True, is_breakout=False)
    flt15_rs_s = L.build_entry_precision_filter(df_15m, side="short", is_vp=False, is_breakout=False)

    rsi15_s, rsima15_s = L.calc_rsi_ema(df_15m["close"].astype(float), rsi_period=9, ma_period=7)
    rsi15 = rsi15_s.values; rsima15 = rsima15_s.values

    mp15_cache = {}
    for mp in MP_LIST:
        res = make_mp_fast(df_15m, period=mp, num_bins=BINS, step=3, use_decay=True)
        mp15_cache[mp] = (res["val_70"], res["vah_70"], res["poc"])

    oos_trades = []  # (scheme, sym, side, role, strat, inv, tier, edt, xdt, regime_at_entry, pnl)

    for scheme, lb_5m, fw_5m, min_tr in [("30d_to_7d", 8640, 2016, 10), ("90d_to_30d", 25920, 8640, 15)]:
        lb_15m = lb_5m // 3
        fw_15m = fw_5m // 3
        start_5m = lb_5m + 600
        step_idx = start_5m
        while step_idx + fw_5m <= n5:
            i0_5 = step_idx - lb_5m
            i1_5 = step_idx
            i2_5 = step_idx + fw_5m

            # 対応する 15m インデックス
            t_split = dt5[i1_5]
            i1_15 = int(np.searchsorted(dt15, t_split))
            i0_15 = max(0, i1_15 - lb_15m)
            i2_15 = min(len(c15), i1_15 + fw_15m)

            # ---- 1. LONG 最適化 (Lookback) ----
            best_rng_l_score = -1e18; best_rng_l_tier = 0; best_rng_l_cfg = None
            best_brk_l_score = -1e18; best_brk_l_tier = 0; best_brk_l_cfg = None

            # 5m VAL_POC & VAH_BREAK
            for mp in MP_LIST:
                v, vh, p = mp5_cache[mp]
                for tr in TR_LIST:
                    r = sim_val_poc_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                       v[i0_5:i1_5], vh[i0_5:i1_5], p[i0_5:i1_5],
                                       flt5_vp_l[i0_5:i1_5], True, SIZE, 0.025, tr, 288, 48)
                    sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                    if sc > best_rng_l_score:
                        best_rng_l_score = sc; best_rng_l_tier = tr_lvl
                        best_rng_l_cfg = ("VAL_POC", 5, mp, tr, 0.0, 0.0)

                    rb = sim_vah_break_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                          v[i0_5:i1_5], vh[i0_5:i1_5], p[i0_5:i1_5],
                                          flt5_vb_l[i0_5:i1_5], True, SIZE, 0.025, tr, 288, 48)
                    scb, trb_lvl = score_candidate(rb[0], rb[1], rb[2], rb[3], rb[4], rb[5], min_tr)
                    if scb > best_brk_l_score:
                        best_brk_l_score = scb; best_brk_l_tier = trb_lvl
                        best_brk_l_cfg = ("VAH_BREAK", 5, mp, tr, 0.0, 0.0)

            # 15m VAL_POC
            if i1_15 - i0_15 >= 200:
                for mp in MP_LIST:
                    v, vh, p = mp15_cache[mp]
                    for tr in TR_LIST:
                        r = sim_val_poc_nb(c15[i0_15:i1_15], h15[i0_15:i1_15], l15[i0_15:i1_15],
                                           v[i0_15:i1_15], vh[i0_15:i1_15], p[i0_15:i1_15],
                                           flt15_vp_l[i0_15:i1_15], True, SIZE, 0.025, tr, 96, 16)
                        sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                        if sc > best_rng_l_score:
                            best_rng_l_score = sc; best_rng_l_tier = tr_lvl
                            best_rng_l_cfg = ("VAL_POC", 15, mp, tr, 0.0, 0.0)

            # 5m & 15m RSIMA (Long)
            for ep in (30.0, 35.0, 40.0):
                for cp in (60.0, 65.0, 70.0):
                    r = sim_rsima_long_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                          rsi5[i0_5:i1_5], rsima5[i0_5:i1_5],
                                          flt5_rs_l[i0_5:i1_5], True, SIZE, ep, cp, 0.025, 288, 48)
                    sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                    if sc > best_rng_l_score:
                        best_rng_l_score = sc; best_rng_l_tier = tr_lvl
                        best_rng_l_cfg = ("RSIMA", 5, 100, 0.70, ep, cp)
                    if i1_15 - i0_15 >= 200:
                        r15 = sim_rsima_long_nb(c15[i0_15:i1_15], h15[i0_15:i1_15], l15[i0_15:i1_15],
                                                rsi15[i0_15:i1_15], rsima15[i0_15:i1_15],
                                                flt15_rs_l[i0_15:i1_15], True, SIZE, ep, cp, 0.025, 96, 16)
                        sc15, tr15_lvl = score_candidate(r15[0], r15[1], r15[2], r15[3], r15[4], r15[5], min_tr)
                        if sc15 > best_rng_l_score:
                            best_rng_l_score = sc15; best_rng_l_tier = tr15_lvl
                            best_rng_l_cfg = ("RSIMA", 15, 100, 0.70, ep, cp)

            # ---- 2. SHORT 最適化 (Lookback) ----
            best_rng_s_score = -1e18; best_rng_s_tier = 0; best_rng_s_cfg = None
            best_brk_s_score = -1e18; best_brk_s_tier = 0; best_brk_s_cfg = None

            for mp in MP_LIST:
                v, vh, p = mp5_cache[mp]
                for tr in TR_LIST:
                    r = sim_vah_poc_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                       v[i0_5:i1_5], vh[i0_5:i1_5], p[i0_5:i1_5],
                                       flt5_vp_s[i0_5:i1_5], True, SIZE, 0.025, tr, 288, 48)
                    sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                    if sc > best_rng_s_score:
                        best_rng_s_score = sc; best_rng_s_tier = tr_lvl
                        best_rng_s_cfg = ("VAH_POC", 5, mp, tr, 0.0, 0.0)

                    rb = sim_val_break_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                          v[i0_5:i1_5], vh[i0_5:i1_5], p[i0_5:i1_5],
                                          flt5_vb_s[i0_5:i1_5], True, SIZE, 0.025, tr, 288, 48)
                    scb, trb_lvl = score_candidate(rb[0], rb[1], rb[2], rb[3], rb[4], rb[5], min_tr)
                    if scb > best_brk_s_score:
                        best_brk_s_score = scb; best_brk_s_tier = trb_lvl
                        best_brk_s_cfg = ("VAL_BREAK", 5, mp, tr, 0.0, 0.0)

            if i1_15 - i0_15 >= 200:
                for mp in MP_LIST:
                    v, vh, p = mp15_cache[mp]
                    for tr in TR_LIST:
                        r = sim_vah_poc_nb(c15[i0_15:i1_15], h15[i0_15:i1_15], l15[i0_15:i1_15],
                                           v[i0_15:i1_15], vh[i0_15:i1_15], p[i0_15:i1_15],
                                           flt15_vp_s[i0_15:i1_15], True, SIZE, 0.025, tr, 96, 16)
                        sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                        if sc > best_rng_s_score:
                            best_rng_s_score = sc; best_rng_s_tier = tr_lvl
                            best_rng_s_cfg = ("VAH_POC", 15, mp, tr, 0.0, 0.0)

            for ep in (60.0, 65.0, 70.0):
                for cp in (30.0, 35.0, 40.0):
                    r = sim_rsima_short_nb(c5[i0_5:i1_5], h5[i0_5:i1_5], l5[i0_5:i1_5],
                                           rsi5[i0_5:i1_5], rsima5[i0_5:i1_5],
                                           flt5_rs_s[i0_5:i1_5], True, SIZE, ep, cp, 0.025, 288, 48)
                    sc, tr_lvl = score_candidate(r[0], r[1], r[2], r[3], r[4], r[5], min_tr)
                    if sc > best_rng_s_score:
                        best_rng_s_score = sc; best_rng_s_tier = tr_lvl
                        best_rng_s_cfg = ("RSIMA_SHORT", 5, 100, 0.70, ep, cp)
                    if i1_15 - i0_15 >= 200:
                        r15 = sim_rsima_short_nb(c15[i0_15:i1_15], h15[i0_15:i1_15], l15[i0_15:i1_15],
                                                 rsi15[i0_15:i1_15], rsima15[i0_15:i1_15],
                                                 flt15_rs_s[i0_15:i1_15], True, SIZE, ep, cp, 0.025, 96, 16)
                        sc15, tr15_lvl = score_candidate(r15[0], r15[1], r15[2], r15[3], r15[4], r15[5], min_tr)
                        if sc15 > best_rng_s_score:
                            best_rng_s_score = sc15; best_rng_s_tier = tr15_lvl
                            best_rng_s_cfg = ("RSIMA_SHORT", 15, 100, 0.70, ep, cp)

            # ---- 3. Out-of-Sample Forward 適用 ([i1 : i2]) ----
            for side, role, tier, cfg in [
                ("long", "range", best_rng_l_tier, best_rng_l_cfg),
                ("long", "breakout", best_brk_l_tier, best_brk_l_cfg),
                ("short", "range", best_rng_s_tier, best_rng_s_cfg),
                ("short", "breakout", best_brk_s_tier, best_brk_s_cfg),
            ]:
                if cfg is None:
                    continue
                st_name, inv, mp, tr, ep, cp = cfg
                if inv == 5:
                    s0, s1 = i1_5, i2_5
                    cc, hh, ll, dts, rcodes = c5[s0:s1], h5[s0:s1], l5[s0:s1], dt5[s0:s1], reg_code_5m[s0:s1]
                    cd_b, to_b = 288, 48
                    if st_name == "VAL_POC":
                        v, vh, p = mp5_cache[mp]
                        out = sim_val_poc_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt5_vp_l[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "VAH_BREAK":
                        v, vh, p = mp5_cache[mp]
                        out = sim_vah_break_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt5_vb_l[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "VAH_POC":
                        v, vh, p = mp5_cache[mp]
                        out = sim_vah_poc_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt5_vp_s[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "VAL_BREAK":
                        v, vh, p = mp5_cache[mp]
                        out = sim_val_break_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt5_vb_s[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "RSIMA":
                        out = sim_rsima_long_nb(cc, hh, ll, rsi5[s0:s1], rsima5[s0:s1], flt5_rs_l[s0:s1], True, SIZE, ep, cp, 0.025, cd_b, to_b)
                    else:
                        out = sim_rsima_short_nb(cc, hh, ll, rsi5[s0:s1], rsima5[s0:s1], flt5_rs_s[s0:s1], True, SIZE, ep, cp, 0.025, cd_b, to_b)
                else:
                    s0, s1 = i1_15, i2_15
                    if s1 - s0 < 10:
                        continue
                    cc, hh, ll, dts, rcodes = c15[s0:s1], h15[s0:s1], l15[s0:s1], dt15[s0:s1], reg_code_15m[s0:s1]
                    cd_b, to_b = 96, 16
                    if st_name == "VAL_POC":
                        v, vh, p = mp15_cache[mp]
                        out = sim_val_poc_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt15_vp_l[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "VAH_POC":
                        v, vh, p = mp15_cache[mp]
                        out = sim_vah_poc_nb(cc, hh, ll, v[s0:s1], vh[s0:s1], p[s0:s1], flt15_vp_s[s0:s1], True, SIZE, 0.025, tr, cd_b, to_b)
                    elif st_name == "RSIMA":
                        out = sim_rsima_long_nb(cc, hh, ll, rsi15[s0:s1], rsima15[s0:s1], flt15_rs_l[s0:s1], True, SIZE, ep, cp, 0.025, cd_b, to_b)
                    else:
                        out = sim_rsima_short_nb(cc, hh, ll, rsi15[s0:s1], rsima15[s0:s1], flt15_rs_s[s0:s1], True, SIZE, ep, cp, 0.025, cd_b, to_b)

                ent_idx, ex_idx, t_pnls = out[8], out[9], out[10]
                for k in range(len(t_pnls)):
                    eb = ent_idx[k]
                    xb = ex_idx[k]
                    oos_trades.append((scheme, sym, side, role, st_name, inv, tier, dts[eb], dts[xb], rcodes[eb], float(t_pnls[k])))

            step_idx += fw_5m

    print(f"  [WF] {sym} 完了 ({time.time()-t0:.1f}s, OOS trades={len(oos_trades)})", flush=True)
    return oos_trades


def stat(g):
    if len(g) == 0:
        return pd.Series({"n": 0, "win%": 0.0, "sum$": 0.0, "avg$": 0.0, "PF": np.nan})
    w = g.pnl[g.pnl > 0].sum(); ls = -g.pnl[g.pnl <= 0].sum()
    return pd.Series({"n": int(len(g)), "win%": round((g.pnl > 0).mean() * 100, 1), "sum$": round(g.pnl.sum(), 2),
                      "avg$": round(g.pnl.mean(), 4), "PF": round(w / ls, 2) if ls > 0 else np.nan})


def apply_portfolio_max_pos(df_trades, max_pos=2):
    """時系列順にソートし、同時保有最大 max_pos 枠 & 同一銘柄重複なしの制約を適用する"""
    df_s = df_trades.sort_values("edt").reset_index(drop=True)
    active = []  # list of (xdt, sym)
    accepted = []
    for idx, row in df_s.iterrows():
        t_now = row["edt"]
        active = [(x, s) for (x, s) in active if x > t_now]
        if len(active) >= max_pos:
            continue
        if any(s == row["sym"] for (_, s) in active):
            continue
        active.append((row["xdt"], row["sym"]))
        accepted.append(idx)
    return df_s.loc[accepted].reset_index(drop=True)


if __name__ == "__main__":
    reg = pd.read_csv("Data/market_regime_5m_from_2023.csv")
    reg["dt"] = pd.to_datetime(reg["dt"], utc=True)
    reg["code"] = (reg.state_30d.str[0] + "," + reg.state_10d.str[0] + "," + reg.state_5d.str[0])
    reg = reg.set_index("dt")
    t0_reg = reg.index[0]

    all_rows = []
    files = sorted(glob.glob("Data/binance_candles_5m_4y/*.pkl"))
    for f in files:
        sym = os.path.basename(f).replace("_5m.pkl", "").replace("USDT", "")
        if sym in ("BTC", "ETH"):
            continue
        df = pd.read_pickle(f)
        df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df[df.dt >= t0_reg].drop_duplicates("dt").reset_index(drop=True)
        if len(df) < 30000:
            continue
        rcodes = reg["code"].reindex(df.dt, method="ffill").values
        all_rows.extend(run_walkforward_for_symbol(sym, df, rcodes))

    wf = pd.DataFrame(all_rows, columns=["scheme", "sym", "side", "role", "strat", "inv", "tier", "edt", "xdt", "regime", "pnl"])
    wf["edt"] = pd.to_datetime(wf["edt"], utc=True)
    wf["xdt"] = pd.to_datetime(wf["xdt"], utc=True)
    wf["edt_jst"] = wf["edt"].dt.tz_convert("Asia/Tokyo")
    wf["hour"] = wf["edt_jst"].dt.hour
    wf["half"] = np.where(wf["edt_jst"] < "2025-01-01", "2023-24", "2025-26")
    # セッションルール適合: 東京(9-16)はrange、欧州(16-22)はbreakout
    wf["session_ok"] = ((wf.hour >= 9) & (wf.hour < 16) & (wf.role == "range")) | \
                       ((wf.hour >= 16) & (wf.hour < 22) & (wf.role == "breakout"))
    # 3パターン規約適合
    wf["regime_3pat_ok"] = (
        ((wf.regime == "N,N,B") & (wf.strat == "VAH_BREAK")) |
        ((wf.regime.isin(["N,N,S", "N,S,S"])) & (wf.side == "short"))
    )

    wf.to_csv("scratch/walkforward_oos_trades.csv", index=False, encoding="utf-8")

    for sch in ["30d_to_7d", "90d_to_30d"]:
        sub = wf[wf.scheme == sch]
        print(f"\n======================================================================")
        print(f"📊 ウォークフォワード OOS 検証結果: {sch} (総OOSトレード={len(sub)})")
        print(f"======================================================================")

        print("\n[1] Recency Gate 段階別 (全地合い・全時間帯)")
        print("  ・Gateなし(全候補) :", stat(sub).to_dict())
        print("  ・Gate合格(Tier1+2):", stat(sub[sub.tier >= 1]).to_dict())
        print("  ・Tier1のみ(7d黒字):", stat(sub[sub.tier == 1]).to_dict())

        print("\n[2] Recency Gate合格(Tier1+2) × セッション規則 (東京=Range / 欧州=Breakout)")
        q_sess = sub[(sub.tier >= 1) & sub.session_ok]
        print("  ・Gate合格 + セッション規則 (全地合い):", stat(q_sess).to_dict())
        print(q_sess.groupby("strat", observed=True).apply(stat, include_groups=False).to_string())

        print("\n[3] 3パターン規約 (N,N,B / N,N,S / N,S,S) × Gate合格 × セッション規則")
        q_3pat_all_h = sub[(sub.tier >= 1) & sub.regime_3pat_ok]
        q_3pat_sess = sub[(sub.tier >= 1) & sub.regime_3pat_ok & sub.session_ok]
        print("  ・3パターン + Gate合格 (全時間帯)      :", stat(q_3pat_all_h).to_dict())
        print("  ・3パターン + Gate合格 + セッション規則:", stat(q_3pat_sess).to_dict())
        if len(q_3pat_sess) > 0:
            print(q_3pat_sess.groupby(["regime", "strat"], observed=True).apply(stat, include_groups=False).to_string())
            print("\n  [期間分割 2023-24 vs 2025-26 (3パターン + Gate合格 + セッション規則)]")
            print(q_3pat_sess.groupby(["regime", "half"], observed=True).apply(stat, include_groups=False).to_string())

            pf_capped = apply_portfolio_max_pos(q_3pat_sess, max_pos=2)
            print("  ・上記に同時保有最大2枠制約を適用      :", stat(pf_capped).to_dict())

        print("\n[4] Gate合格 + セッション規則 における地合い別ランキング (n>=15)")
        rg_tbl = q_sess.groupby(["regime", "side"], observed=True).apply(stat, include_groups=False)
        print(rg_tbl[rg_tbl.n >= 15].sort_values("sum$", ascending=False).to_string())
