#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_2024_retest_regime.py
2024年〜2026年（現代相場 ETF後）特化
「8時間ごと（01:00 / 09:00 / 17:00 JST）3態勢地合い判定 × 東京レンジ ＆ 欧州リテスト型ブレイク」
ウォークフォワード検証スクリプト
"""
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_4Y_DIR = BASE_DIR / "Data" / "binance_candles_5m_4y"

from bingx5_46_3logic import (
    evaluate_market_regime_3window,
    check_normalized_peak_timing,
    check_normalized_trough_timing,
    check_bottom_reversal_recovery,
    check_peak_breakdown_trend,
    simulate_val_poc_fast,
    simulate_vah_poc_fast,
    simulate_val_break_fast,
)

JST = timezone(timedelta(hours=9))

SYMBOLS_MAP = {
    "BTC-USDT": "BTCUSDT",
    "SOL-USDT": "SOLUSDT",
    "DOGE-USDT": "DOGEUSDT",
    "XRP-USDT": "XRPUSDT",
    "SUI-USDT": "SUIUSDT",
    "AVAX-USDT": "AVAXUSDT",
    "LINK-USDT": "LINKUSDT",
    "APT-USDT": "APTUSDT",
    "NEAR-USDT": "NEARUSDT",
    "ARB-USDT": "ARBUSDT",
    "UNI-USDT": "UNIUSDT",
    "OP-USDT": "OPUSDT",
    "TIA-USDT": "TIAUSDT",
    "RENDER-USDT": "RENDERUSDT",
    "INJ-USDT": "INJUSDT",
    "FET-USDT": "FETUSDT",
    "TAO-USDT": "TAOUSDT",
    "AAVE-USDT": "AAVEUSDT",
    "ENA-USDT": "ENAUSDT",
    "ZEC-USDT": "ZECUSDT",
    "HYPE-USDT": "HYPEUSDT",
}


def compute_fast_vp(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    opens: np.ndarray,
    volumes: np.ndarray,
    period: int = 200,
    num_bins: int = 40,
    step: int = 12,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(closes)
    min_req = min(max(10, period // 2), period)
    val_70 = np.full(n, np.nan)
    vah_70 = np.full(n, np.nan)
    poc_arr = np.full(n, np.nan)

    half_life = max(5.0, period / 2.0)
    decay_factor = 0.5 ** (1.0 / half_life)
    full_weights = decay_factor ** np.arange(period - 1, -1, -1)

    last_poc = np.nan
    last_v70 = np.nan
    last_vh70 = np.nan

    for i in range(min_req, n):
        if step > 1 and (i - min_req) % step != 0 and not np.isnan(last_poc):
            poc_arr[i] = last_poc
            val_70[i] = last_v70
            vah_70[i] = last_vh70
            continue

        s_idx = max(0, i - period + 1)
        w_len = i - s_idx + 1
        w_h = highs[s_idx:i + 1]
        w_l = lows[s_idx:i + 1]
        w_v = volumes[s_idx:i + 1] * full_weights[-w_len:]

        p_max = w_h.max()
        p_min = w_l.min()
        curr_c = closes[i]
        if p_max <= p_min or np.isnan(p_max) or np.isnan(p_min):
            last_poc = last_v70 = last_vh70 = curr_c
            poc_arr[i] = val_70[i] = vah_70[i] = curr_c
            continue

        price_bins = np.linspace(p_min, p_max, num_bins + 1)
        bin_centers = (price_bins[:-1] + price_bins[1:]) * 0.5
        bin_lows_2d = price_bins[:-1][np.newaxis, :]
        bin_highs_2d = price_bins[1:][np.newaxis, :]

        full_overlap = np.maximum(0.0, np.minimum(w_h[:, np.newaxis], bin_highs_2d) - np.maximum(w_l[:, np.newaxis], bin_lows_2d))
        full_range = np.maximum(w_h - w_l, 1e-12)[:, np.newaxis]
        vol_profile = (w_v[:, np.newaxis] * (full_overlap / full_range)).sum(axis=0)

        tot_v = vol_profile.sum()
        if tot_v <= 0:
            last_poc = last_v70 = last_vh70 = curr_c
            poc_arr[i] = val_70[i] = vah_70[i] = curr_c
            continue

        poc_idx = int(np.argmax(vol_profile))
        last_poc = bin_centers[poc_idx]
        poc_arr[i] = last_poc

        target_v = tot_v * 0.70
        curr_v = vol_profile[poc_idx]
        u_idx = poc_idx
        d_idx = poc_idx
        while (u_idx < num_bins - 1 or d_idx > 0) and curr_v < target_v:
            up_v = vol_profile[u_idx + 1] if u_idx < num_bins - 1 else -1.0
            dn_v = vol_profile[d_idx - 1] if d_idx > 0 else -1.0
            if up_v >= dn_v:
                u_idx += 1
                curr_v += vol_profile[u_idx]
            else:
                d_idx -= 1
                curr_v += vol_profile[d_idx]
        last_v70 = price_bins[d_idx]
        last_vh70 = price_bins[u_idx + 1]
        val_70[i] = last_v70
        vah_70[i] = last_vh70

    return poc_arr, val_70, vah_70


def build_vol_filter(volumes: np.ndarray, is_breakout: bool = False) -> np.ndarray:
    n = len(volumes)
    if n == 0:
        return np.ones(0, dtype=bool)
    v_s = pd.Series(volumes)
    v_ma = v_s.rolling(20, min_periods=5).mean().shift(1).values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where((v_ma > 0) & (~np.isnan(v_ma)), volumes / v_ma, 1.0)
    if is_breakout:
        return (vr >= 1.2) & (vr <= 4.0)
    else:
        return (vr >= 0.6) & (vr <= 2.5)


def simulate_vah_retest_fast(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    opens: np.ndarray,
    vals: np.ndarray,
    vahs: np.ndarray,
    pocs: np.ndarray,
    vol_filter: Optional[np.ndarray] = None,
    initial_equity: float = 15.0,
    target_ratio: float = 0.70,
    cooldown_bars: int = 288,
    timeout_bars: int = 48,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.0006,
) -> Dict[str, Any]:
    """
    【新仕様】VAH_RETEST 高速リテスト型ロングブレイクシミュレータ
    - 終値で VAH を上抜け後、1〜12本以内（1時間以内）に VAH 付近（VAH * 0.998〜1.003）まで押し、
      陽線（Close > Open かつ Close >= VAH）でサポート確認が完了した瞬間に成行/Makerエントリー。
    - SL: VAH の直下割れ（VAH * 0.992、わずか -0.8%）➔ リスク極小
    - TP: リスク幅の 2.0 倍（RR 1:2 完全保証）
    """
    n = len(closes)
    half_idx = n // 2
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    trade_size = initial_equity

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    entry_sl_px = 0.0
    target_tp = 0.0
    entry_bar_idx = -1
    cooldown_until = -1
    cum_pnl = 0.0
    pnl_first_half = 0.0
    pnl_second_half = 0.0
    pnl_recent_7d = 0.0
    trades_recent_7d = 0
    trades = []
    peak_eq = initial_equity
    max_dd = 0.0

    last_break_bar = -999

    for i in range(1, n):
        ci = closes[i]
        hi = highs[i]
        li = lows[i]
        oi = opens[i]
        val = vals[i]
        vah = vahs[i]

        # 1. 保有中ポジションの決済チェック
        if pos_qty > 0:
            is_exit = False
            sl_exec_px = ci

            if entry_sl_px > 0 and ci <= entry_sl_px:
                is_exit = True
                sl_exec_px = min(ci, entry_sl_px)
            elif (i - entry_bar_idx) >= timeout_bars and ci < avg_price:
                is_exit = True
                sl_exec_px = ci
            elif (hi >= target_tp or ci >= target_tp) and target_tp > 0:
                is_exit = True
                sl_exec_px = max(ci, target_tp) if hi >= target_tp else ci

            if is_exit:
                sell_val = pos_qty * sl_exec_px
                fee = sell_val * taker_fee
                entry_fee = pos_cost * maker_fee
                pnl = sell_val - pos_cost - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx:
                    pnl_first_half += pnl
                else:
                    pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl
                    trades_recent_7d += 1
                trades.append(pnl)

                if sl_exec_px <= avg_price:
                    cooldown_until = i + cooldown_bars
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                entry_bar_idx = -1

        # 2. ブレイク検知 ＆ リテスト反発エントリー判定
        if pos_qty == 0 and i >= cooldown_until:
            if not np.isnan(vah) and not np.isnan(vahs[i-1]):
                # VAH 上抜け検知
                if closes[i-1] <= vahs[i-1] and ci > vah:
                    last_break_bar = i
                # 上抜け後 1〜12本以内でのリテスト反発判定
                elif 1 <= (i - last_break_bar) <= 12:
                    is_tested = (li <= vah * 1.003)
                    is_bullish_support = (ci >= vah * 0.999) and (ci > oi)
                    va_w = (vah - val) / val if val > 0 else 0.0
                    vol_ok = (vol_filter is None or vol_filter[i])

                    if is_tested and is_bullish_support and va_w >= 0.015 and vol_ok:
                        px = ci
                        pos_qty = trade_size / px
                        pos_cost = trade_size
                        avg_price = px
                        entry_bar_idx = i
                        # VAH 直下割れ（わずか -0.8%）の超タイトSL
                        entry_sl_px = vah * 0.992
                        risk_dist = max(avg_price * 0.004, avg_price - entry_sl_px)
                        target_tp = avg_price + risk_dist * 2.0  # RR 1:2
                        last_break_bar = -999

        cur_eq = initial_equity + cum_pnl + (pos_qty * ci - pos_cost if pos_qty > 0 else 0.0)
        if cur_eq > peak_eq:
            peak_eq = cur_eq
        cur_dd = peak_eq - cur_eq
        if cur_dd > max_dd:
            max_dd = cur_dd

    trade_cnt = len(trades)
    win_cnt = sum(1 for t in trades if t > 0)
    win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
    return {
        "final_pnl": float(cum_pnl),
        "pnl_first_half": float(pnl_first_half),
        "pnl_second_half": float(pnl_second_half),
        "pnl_recent_7d": float(pnl_recent_7d),
        "trades_recent_7d": int(trades_recent_7d),
        "trade_count": trade_cnt,
        "win_rate": win_rate,
        "DD_max": float(max_dd),
    }


def optimize_symbol_30d_pure_vp(df_5m_30d: pd.DataFrame, side_mode: str = "long", trade_size: float = 15.0) -> Dict[str, Any]:
    c5 = df_5m_30d["close"].values
    h5 = df_5m_30d["high"].values
    l5 = df_5m_30d["low"].values
    o5 = df_5m_30d["open"].values
    v5 = df_5m_30d["volume"].values

    vf_rng = build_vol_filter(v5, is_breakout=False)
    vf_brk = build_vol_filter(v5, is_breakout=True)

    mp_candidates = [150, 200, 300]
    tr_candidates = [0.60, 0.70]

    best_rng = None
    best_rng_score = -1e9
    best_brk = None
    best_brk_score = -1e9

    def _score_cand(res: Dict[str, Any], min_tr: int = 6) -> Tuple[float, bool, str]:
        cnt = int(res.get("trade_count", 0))
        pnl = float(res.get("final_pnl", 0.0))
        p1 = float(res.get("pnl_first_half", 0.0))
        p2 = float(res.get("pnl_second_half", 0.0))
        p7 = float(res.get("pnl_recent_7d", 0.0))
        c7 = int(res.get("trades_recent_7d", 0))
        if cnt >= min_tr and p1 > 0 and p2 > p1 and p7 > 0 and c7 >= 1:
            return 1000.0 + p2 + p7 + pnl * 0.2, True, "Tier1"
        elif cnt >= min_tr and p1 > 0 and p2 > p1:
            return 500.0 + p2 + pnl * 0.1, True, "Tier2"
        elif cnt >= 3 and pnl > 0:
            return pnl, False, "Fallback"
        return -1000.0 + pnl, False, "Unqualified"

    for mp in mp_candidates:
        poc, val, vah = compute_fast_vp(c5, h5, l5, o5, v5, period=mp, num_bins=40, step=16)
        for tr in tr_candidates:
            if side_mode == "long":
                sim_r = simulate_val_poc_fast(
                    c5, h5, l5, val, vah, poc, vf_rng,
                    initial_equity=trade_size, max_trades=1, cooldown_bars=288, timeout_bars=48, target_ratio=tr
                )
                r_strat = "val_poc"
                # 【新仕様】リテスト型ロングブレイク
                sim_b = simulate_vah_retest_fast(
                    c5, h5, l5, o5, val, vah, poc, vf_brk,
                    initial_equity=trade_size, target_ratio=tr, cooldown_bars=288, timeout_bars=48
                )
                b_strat = "vah_retest"
            else:
                sim_r = simulate_vah_poc_fast(
                    c5, h5, l5, val, vah, poc, vf_rng,
                    initial_equity=trade_size, max_trades=1, cooldown_bars=288, timeout_bars=48, target_ratio=tr
                )
                r_strat = "vah_poc"
                sim_b = simulate_val_break_fast(
                    c5, h5, l5, val, vah, poc, vf_brk,
                    initial_equity=trade_size, max_trades=1, cooldown_bars=288, timeout_bars=48, target_ratio=tr
                )
                b_strat = "val_break"

            sc_r, q_r, ql_r = _score_cand(sim_r, min_tr=5)
            if sc_r > best_rng_score:
                best_rng_score = sc_r
                best_rng = {
                    "strategy": r_strat, "interval": 5, "mp": mp, "target_ratio": tr,
                    "pnl": sim_r["final_pnl"], "trade_count": sim_r["trade_count"],
                    "win_rate": sim_r["win_rate"], "is_qualified": q_r, "qual_level": ql_r
                }

            sc_b, q_b, ql_b = _score_cand(sim_b, min_tr=3)
            if not q_b and sim_b["trade_count"] >= 3 and sim_b["final_pnl"] > 0:
                q_b = True
                ql_b = "BrkActive"
                sc_b = 300.0 + sim_b["final_pnl"]
            if sc_b > best_brk_score:
                best_brk_score = sc_b
                best_brk = {
                    "strategy": b_strat, "interval": 5, "mp": mp, "target_ratio": tr,
                    "pnl": sim_b["final_pnl"], "trade_count": sim_b["trade_count"],
                    "win_rate": sim_b["win_rate"], "is_qualified": q_b, "qual_level": ql_b
                }

    return {
        "range_params": best_rng,
        "breakout_params": best_brk,
        "is_range_qualified": bool(best_rng and best_rng.get("is_qualified", False)),
        "is_breakout_qualified": bool(best_brk and best_brk.get("is_qualified", False)),
    }


def simulate_oos_window_8h_retest(
    df_5m_full_slice: pd.DataFrame,
    oos_start_idx: int,
    dual_params: Dict[str, Any],
    regime_8h_times: np.ndarray,
    regime_8h_data: List[Tuple[str, List[str]]],
    symbol: str,
    side_mode: str = "long",
    trade_size: float = 15.0,
) -> List[Dict[str, Any]]:
    rng_p = dual_params.get("range_params") or {}
    brk_p = dual_params.get("breakout_params") or {}
    is_rng_q = bool(dual_params.get("is_range_qualified", False))
    is_brk_q = bool(dual_params.get("is_breakout_qualified", False))

    if not is_rng_q and not is_brk_q:
        return []

    n = len(df_5m_full_slice)
    if oos_start_idx >= n - 2:
        return []

    c = df_5m_full_slice["close"].values
    h = df_5m_full_slice["high"].values
    l = df_5m_full_slice["low"].values
    o = df_5m_full_slice["open"].values
    v = df_5m_full_slice["volume"].values
    ts_utc = pd.to_datetime(df_5m_full_slice["timestamp"], utc=True)
    jst_dt = ts_utc + pd.Timedelta(hours=9)
    jst_hours_arr = jst_dt.dt.hour.values
    ts_utc_np = ts_utc.values

    r_strat = str(rng_p.get("strategy", "val_poc" if side_mode == "long" else "vah_poc")).lower()
    r_mp = int(rng_p.get("mp", 200))
    r_tr = float(rng_p.get("target_ratio", 0.70))
    b_strat = str(brk_p.get("strategy", "vah_retest" if side_mode == "long" else "val_break")).lower()
    b_mp = int(brk_p.get("mp", 200))
    b_tr = float(brk_p.get("target_ratio", 0.70))

    poc_r, val_r, vah_r = compute_fast_vp(c, h, l, o, v, period=r_mp, num_bins=40, step=6)
    if b_mp == r_mp:
        poc_b, val_b, vah_b = poc_r, val_r, vah_r
    else:
        poc_b, val_b, vah_b = compute_fast_vp(c, h, l, o, v, period=b_mp, num_bins=40, step=6)

    vf_rng = build_vol_filter(v, is_breakout=False)
    vf_brk = build_vol_filter(v, is_breakout=True)

    maker_fee = 0.0002
    taker_fee = 0.0006
    cooldown_bars = 288
    timeout_bars = 48
    expected_reg = "BUY" if side_mode == "long" else "SELL"

    trades_out = []
    pos_qty = 0.0
    pos_cost = 0.0
    avg_px = 0.0
    entry_sl_px = 0.0
    target_tp = 0.0
    entry_bar_idx = -1
    entry_session = ""
    entry_strat = ""
    entry_ts = None
    cooldown_until = -1
    pending_px = -1.0
    pending_session = ""
    pending_strat = ""
    last_break_bar_l = -999

    slot_indices = np.searchsorted(regime_8h_times, ts_utc_np, side="right") - 1
    slot_indices = np.clip(slot_indices, 0, len(regime_8h_data) - 1)

    for i in range(max(12, oos_start_idx), n):
        ci = c[i]
        hi = h[i]
        li = l[i]
        oi = o[i]
        hr_jst = int(jst_hours_arr[i])
        
        slot_idx = int(slot_indices[i])
        cur_reg, cur_rec_syms = regime_8h_data[slot_idx]
        is_regime_active_now = (cur_reg == expected_reg) and (symbol in cur_rec_syms)

        is_tokyo = (9 <= hr_jst < 16)
        is_london = (16 <= hr_jst < 22)

        if not is_regime_active_now and pending_px > 0:
            pending_px = -1.0

        # 0. 指値約定チェック
        if pending_px > 0:
            if side_mode == "long" and li <= pending_px:
                px = pending_px
                pos_qty = trade_size / px
                pos_cost = trade_size + trade_size * maker_fee
                avg_px = pos_cost / pos_qty
                entry_bar_idx = i
                entry_session = pending_session
                entry_strat = pending_strat
                entry_ts = ts_utc.iloc[i]
                if entry_strat == "vah_retest":
                    entry_sl_px = vah_b[i] * 0.992
                    risk_d = max(avg_px * 0.004, avg_px - entry_sl_px)
                    target_tp = avg_px + risk_d * 2.0
                else:
                    rec_l = float(np.min(l[max(0, i - 10):i + 1]))
                    entry_sl_px = max(min(rec_l, avg_px * 0.9980), avg_px * (1.0 - 0.025))
                    va_d = max(0.0, vah_r[i] - val_r[i]) if not np.isnan(vah_r[i]) else 0.0
                    target_tp = val_r[i] + va_d * r_tr if va_d > 0 else avg_px * 1.015
            elif side_mode == "short" and hi >= pending_px:
                px = pending_px
                pos_qty = trade_size / px
                pos_cost = trade_size
                avg_px = px
                entry_bar_idx = i
                entry_session = pending_session
                entry_strat = pending_strat
                entry_ts = ts_utc.iloc[i]
                if entry_strat == "val_break":
                    va_d = max(0.0, vah_b[i] - val_b[i]) if not np.isnan(vah_b[i]) else 0.0
                    raw_sl = val_b[i] + va_d * 0.35 if va_d > 0 else avg_px * 1.015
                    poc_cl = poc_b[i] * 0.999 if (not np.isnan(poc_b[i]) and poc_b[i] > avg_px) else raw_sl
                    sl_dist = max(avg_px * 0.004, min(raw_sl, poc_cl) - avg_px)
                    entry_sl_px = avg_px + sl_dist
                    target_tp = max(avg_px * 0.5, avg_px - sl_dist * 2.0)
                else:
                    rec_h = float(np.max(h[max(0, i - 10):i + 1]))
                    entry_sl_px = min(max(rec_h, avg_px * 1.0020), avg_px * (1.0 + 0.025))
                    va_d = max(0.0, vah_r[i] - val_r[i]) if not np.isnan(vah_r[i]) else 0.0
                    target_tp = vah_r[i] - va_d * r_tr if va_d > 0 else avg_px * 0.985
            pending_px = -1.0

        # 1. 保有中ポジションの決済監視 (24h)
        if pos_qty > 0:
            is_exit = False
            exit_px = ci
            exit_type = ""

            if side_mode == "long":
                if cur_reg != "BUY" and (hi >= avg_px * 1.0020 or ci >= avg_px * 1.0020):
                    is_exit = True
                    exit_px = max(ci, avg_px * 1.0020) if hi >= avg_px * 1.0020 else ci
                    exit_type = "REGIME_BE_EXIT"
                elif entry_sl_px > 0 and ci <= entry_sl_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL"
                elif li <= avg_px * 0.970:
                    is_exit = True
                    exit_px = avg_px * 0.970
                    exit_type = "SL_3%"
                elif (i - entry_bar_idx) >= timeout_bars and ci < avg_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "TIMEOUT"
                elif (hi >= target_tp or ci >= target_tp) and target_tp >= avg_px * 1.0040:
                    is_exit = True
                    exit_px = max(ci, target_tp) if hi >= target_tp else ci
                    exit_type = "TP"

                if is_exit:
                    sell_val = pos_qty * exit_px
                    fee = sell_val * taker_fee
                    pnl = sell_val - pos_cost - fee
                    trades_out.append({
                        "entry_time": entry_ts,
                        "exit_time": ts_utc.iloc[i],
                        "side": "LONG",
                        "session": entry_session,
                        "strategy": entry_strat,
                        "pnl": float(pnl),
                        "exit_type": exit_type,
                    })
                    if exit_type.startswith("SL") or exit_type == "TIMEOUT":
                        cooldown_until = i + cooldown_bars
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_px = 0.0
                    entry_sl_px = 0.0
                    entry_bar_idx = -1
            else:
                if cur_reg != "SELL" and (li <= avg_px * 0.9980 or ci <= avg_px * 0.9980):
                    is_exit = True
                    exit_px = min(ci, avg_px * 0.9980) if li <= avg_px * 0.9980 else ci
                    exit_type = "REGIME_BE_EXIT"
                elif entry_sl_px > 0 and ci >= entry_sl_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL"
                elif hi >= avg_px * 1.030:
                    is_exit = True
                    exit_px = avg_px * 1.030
                    exit_type = "SL_3%"
                elif entry_strat == "val_break" and not np.isnan(poc_b[i]) and ci > poc_b[i] and ci > avg_px * 1.004:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL_POC"
                elif (i - entry_bar_idx) >= timeout_bars and ci > avg_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "TIMEOUT"
                else:
                    if entry_strat in ("vah_poc", "val_break"):
                        tp_now = target_tp
                        if entry_strat == "vah_poc" and not np.isnan(val_r[i]) and not np.isnan(vah_r[i]) and vah_r[i] > val_r[i]:
                            tp_now = vah_r[i] - (vah_r[i] - val_r[i]) * r_tr
                        if (li <= tp_now or ci <= tp_now) and tp_now <= avg_px * 0.9960:
                            is_exit = True
                            exit_px = min(ci, tp_now) if li <= tp_now else ci
                            exit_type = "TP"

                if is_exit:
                    buy_val = pos_qty * exit_px
                    fee = buy_val * taker_fee
                    entry_fee = pos_cost * maker_fee
                    pnl = pos_cost - buy_val - fee - entry_fee
                    trades_out.append({
                        "entry_time": entry_ts,
                        "exit_time": ts_utc.iloc[i],
                        "side": "SHORT",
                        "session": entry_session,
                        "strategy": entry_strat,
                        "pnl": float(pnl),
                        "exit_type": exit_type,
                    })
                    if exit_type.startswith("SL") or exit_type == "TIMEOUT":
                        cooldown_until = i + cooldown_bars
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_px = 0.0
                    entry_sl_px = 0.0
                    entry_bar_idx = -1

        # 2. 新規エントリー判定
        if is_regime_active_now and pos_qty == 0.0 and pending_px < 0 and i >= cooldown_until:
            if is_tokyo and is_rng_q and vf_rng[i]:
                if side_mode == "long":
                    if r_strat == "val_poc":
                        if not np.isnan(val_r[i]) and not np.isnan(val_r[i - 1]) and not np.isnan(vah_r[i]):
                            val_gc = (c[i - 1] <= val_r[i - 1]) and (ci > val_r[i]) and (ci <= val_r[i] + (vah_r[i] - val_r[i]) * 0.5)
                            va_w = (vah_r[i] - val_r[i]) / val_r[i] if val_r[i] > 0 else 0.0
                            w_c = c[max(0, i - 20):i + 1]
                            res_r = np.sum((w_c >= val_r[i] * 0.998) & (w_c <= vah_r[i] * 1.002)) / max(1, len(w_c))
                            if val_gc and va_w >= 0.015 and res_r >= 0.70:
                                pending_px = ci * 0.9998
                                pending_session = "TOKYO_RANGE"
                                pending_strat = "val_poc"
                else:
                    if r_strat == "vah_poc":
                        if not np.isnan(vah_r[i]) and not np.isnan(vah_r[i - 1]) and not np.isnan(val_r[i]):
                            vah_dc = (c[i - 1] >= vah_r[i - 1]) and (ci < vah_r[i]) and (ci >= val_r[i] + (vah_r[i] - val_r[i]) * 0.5)
                            va_w = (vah_r[i] - val_r[i]) / val_r[i] if val_r[i] > 0 else 0.0
                            w_c = c[max(0, i - 20):i + 1]
                            res_r = np.sum((w_c >= val_r[i] * 0.998) & (w_c <= vah_r[i] * 1.002)) / max(1, len(w_c))
                            if vah_dc and va_w >= 0.015 and res_r >= 0.70:
                                pending_px = ci * 1.0002
                                pending_session = "TOKYO_RANGE"
                                pending_strat = "vah_poc"

            elif is_london and is_brk_q and vf_brk[i]:
                if side_mode == "long":
                    # 【新仕様】欧州 VAH リテスト反発判定
                    if not np.isnan(vah_b[i]) and not np.isnan(vah_b[i - 1]):
                        if c[i - 1] <= vah_b[i - 1] and ci > vah_b[i]:
                            last_break_bar_l = i
                        elif 1 <= (i - last_break_bar_l) <= 12:
                            is_test = (li <= vah_b[i] * 1.003)
                            is_sup = (ci >= vah_b[i] * 0.999) and (ci > oi)
                            va_w = (vah_b[i] - val_b[i]) / val_b[i] if val_b[i] > 0 else 0.0
                            if is_test and is_sup and va_w >= 0.015:
                                pending_px = ci * 0.9998
                                pending_session = "LONDON_BREAK"
                                pending_strat = "vah_retest"
                                last_break_bar_l = -999
                else:
                    if not np.isnan(val_b[i]) and not np.isnan(val_b[i - 1]) and not np.isnan(vah_b[i]):
                        va_d = max(0.0, vah_b[i] - val_b[i])
                        val_brk = (c[i - 1] >= val_b[i - 1]) and (ci < val_b[i]) and (ci >= val_b[i] - va_d * 0.35)
                        va_w = va_d / val_b[i] if val_b[i] > 0 else 0.0
                        if val_brk and va_w >= 0.015:
                            pending_px = ci * 1.0002
                            pending_session = "LONDON_BREAK"
                            pending_strat = "val_break"

    return trades_out


def main():
    t0 = time.time()
    print("=" * 95)
    print("🚀 【2024年〜2026年特化検証】8時間地合い判定 × 東京VPレンジ ＆ 欧州リテスト型ブレイク")
    print("=" * 95)

    # 1. 全21銘柄ロード
    dfs_5m: Dict[str, pd.DataFrame] = {}
    dfs_1h: Dict[str, pd.DataFrame] = {}

    for sym_dash, sym_bin in SYMBOLS_MAP.items():
        pkl_p = DATA_4Y_DIR / f"{sym_bin}_5m.pkl"
        if not pkl_p.exists():
            continue
        df = pd.read_pickle(pkl_p)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        dfs_5m[sym_dash] = df

        df_idx = df.set_index("timestamp")
        d1h = df_idx.resample("60min", closed="left", label="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna(subset=["open", "close"]).reset_index()
        d1h["symbol"] = sym_dash
        dfs_1h[sym_dash] = d1h

    print(f"✅ 全 {len(dfs_5m)} 銘柄ロード完了 ({time.time() - t0:.1f}秒)")

    # 2. 2024年1月1日以降の 8時間スロットを生成
    btc_1h = dfs_1h["BTC-USDT"]
    start_dt = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
    end_dt = pd.to_datetime(btc_1h["timestamp"].iloc[-1], utc=True)

    slot_times_utc = pd.date_range(start=start_dt, end=end_dt, freq="8h")
    print(f"🔍 2024年以降 ({len(slot_times_utc)} スロット) の「8時間ごと地合い判定 (01/09/17 JST)」を計算中...")
    t_reg0 = time.time()

    ts_1h_map = {s: d["timestamp"].values for s, d in dfs_1h.items()}
    regime_8h_list: List[Tuple[str, List[str]]] = []
    slot_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}

    for slot_t in slot_times_utc:
        t_end_np = slot_t.to_numpy()
        t_start_np = (slot_t - pd.Timedelta(days=32)).to_numpy()

        slice_1h_map = {}
        for sym_d, d1h in dfs_1h.items():
            ts_arr = ts_1h_map[sym_d]
            i1 = int(np.searchsorted(ts_arr, t_end_np, side="right"))
            i0 = int(np.searchsorted(ts_arr, t_start_np, side="left"))
            if i1 - i0 >= 120:
                slice_1h_map[sym_d] = d1h.iloc[i0:i1]

        reg_res = evaluate_market_regime_3window(slice_1h_map)
        overall_reg = reg_res.get("overall_regime", "NEUTRAL")
        rec_syms = list(reg_res.get("recommended_symbols", []))

        for sym_d, sub_1h in slice_1h_map.items():
            if sym_d == "BTC-USDT":
                continue
            if overall_reg == "BUY":
                is_bad, _ = check_normalized_peak_timing(sub_1h)
                if is_bad:
                    if sym_d in rec_syms:
                        rec_syms.remove(sym_d)
                else:
                    is_rec, _ = check_bottom_reversal_recovery(sub_1h)
                    if is_rec and sym_d not in rec_syms:
                        rec_syms.append(sym_d)
            elif overall_reg == "SELL":
                is_bad, _ = check_normalized_trough_timing(sub_1h)
                if is_bad:
                    if sym_d in rec_syms:
                        rec_syms.remove(sym_d)
                else:
                    is_bd, _ = check_peak_breakdown_trend(sub_1h)
                    if is_bd and sym_d not in rec_syms:
                        rec_syms.append(sym_d)

        regime_8h_list.append((overall_reg, sorted(list(set(rec_syms)))))
        slot_counts[overall_reg] = slot_counts.get(overall_reg, 0) + 1

    regime_8h_times = slot_times_utc.values
    tot_slots = len(regime_8h_list)
    print(
        f"✅ 2024年以降 8時間地合い判定 完了 ({tot_slots} スロット / {time.time() - t_reg0:.1f}秒) -> "
        f"🟢BUY: {slot_counts['BUY']}回 ({slot_counts['BUY']/max(1,tot_slots)*100:.1f}%), "
        f"🔴SELL: {slot_counts['SELL']}回 ({slot_counts['SELL']/max(1,tot_slots)*100:.1f}%), "
        f"⚪NEUTRAL: {slot_counts['NEUTRAL']}回 ({slot_counts['NEUTRAL']/max(1,tot_slots)*100:.1f}%)"
    )

    # 3. 2024年以降のウォークフォワード検証
    ts_5m_map = {s: d["timestamp"].values for s, d in dfs_5m.items()}
    step_days = 7
    curr_dt = start_dt
    all_oos_trades: List[Dict[str, Any]] = []

    while curr_dt < end_dt:
        next_dt = curr_dt + pd.Timedelta(days=step_days)

        s_i0 = int(np.searchsorted(regime_8h_times, curr_dt.to_numpy(), side="left"))
        s_i1 = int(np.searchsorted(regime_8h_times, next_dt.to_numpy(), side="right"))
        week_slots = regime_8h_list[s_i0:s_i1]

        buy_syms_in_week = set()
        sell_syms_in_week = set()
        for sl_reg, sl_syms in week_slots:
            if sl_reg == "BUY":
                buy_syms_in_week.update(sl_syms)
            elif sl_reg == "SELL":
                sell_syms_in_week.update(sl_syms)

        cand_trades_pool = []

        for side_mode, target_syms in [("long", sorted(list(buy_syms_in_week))), ("short", sorted(list(sell_syms_in_week)))]:
            if not target_syms:
                continue
            for sym_d in target_syms[:12]:
                df5 = dfs_5m.get(sym_d)
                if df5 is None:
                    continue
                ts_arr5 = ts_5m_map[sym_d]
                t_train_start = (curr_dt - pd.Timedelta(days=30)).to_numpy()
                t_train_end = curr_dt.to_numpy()
                t_oos_end = next_dt.to_numpy()

                i0 = int(np.searchsorted(ts_arr5, t_train_start, side="left"))
                i_split = int(np.searchsorted(ts_arr5, t_train_end, side="right"))
                i1 = int(np.searchsorted(ts_arr5, t_oos_end, side="right"))

                if (i_split - i0) < 3000 or (i1 - i_split) < 10:
                    continue

                sub_5m = df5.iloc[i0:i1].reset_index(drop=True)
                oos_start_idx = i_split - i0
                train_5m = sub_5m.iloc[:oos_start_idx]

                dual_p = optimize_symbol_30d_pure_vp(train_5m, side_mode=side_mode, trade_size=15.0)
                if not dual_p["is_range_qualified"] and not dual_p["is_breakout_qualified"]:
                    continue

                sym_oos_trades = simulate_oos_window_8h_retest(
                    sub_5m,
                    oos_start_idx=oos_start_idx,
                    dual_params=dual_p,
                    regime_8h_times=regime_8h_times,
                    regime_8h_data=regime_8h_list,
                    symbol=sym_d,
                    side_mode=side_mode,
                    trade_size=15.0,
                )
                for t in sym_oos_trades:
                    t["symbol"] = sym_d
                    t["regime"] = "BUY" if side_mode == "long" else "SELL"
                    cand_trades_pool.append(t)

        cand_trades_pool.sort(key=lambda x: x["entry_time"])
        active_exit_times: List[pd.Timestamp] = []
        for tr in cand_trades_pool:
            ent_t = tr["entry_time"]
            active_exit_times = [et for et in active_exit_times if et > ent_t]
            if len(active_exit_times) < 2:
                active_exit_times.append(tr["exit_time"])
                all_oos_trades.append(tr)

        curr_dt = next_dt

    # 4. 集計レポート
    print("\n" + "=" * 95)
    print("📊 【2024年〜2026年 リテスト型ブレイク検証結果 ($15単発固定・最大2枠)】")
    print("=" * 95)

    if not all_oos_trades:
        print("トレードが発生しませんでした。")
        return

    df_tr = pd.DataFrame(all_oos_trades)
    df_tr["year"] = pd.to_datetime(df_tr["entry_time"]).dt.year

    def _print_stats(label: str, sub: pd.DataFrame):
        if sub.empty:
            print(f"  {label:36s} | 取引:    0回 | 純利益:   +$0.00")
            return
        cnt = len(sub)
        wins = (sub["pnl"] > 0).sum()
        wr = wins / cnt * 100.0
        tot_pnl = sub["pnl"].sum()
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.9
        eq = sub["pnl"].cumsum()
        dd = (eq.cummax() - eq).max()
        print(f"  {label:36s} | 取引: {cnt:4d}回 | 勝率: {wr:5.1f}% | PF: {pf:4.2f} | 純利益: {tot_pnl:+8.2f} USDT | MaxDD: {dd:6.2f} USDT")

    print("\n■ 2024年〜2026年 総合・セッション別成績:")
    _print_stats("🏆 全体総合 (リテスト型VP4戦略)", df_tr)
    _print_stats("🗼 東京時間 (09-16h レンジ逆張り)", df_tr[df_tr["session"] == "TOKYO_RANGE"])
    _print_stats("🏰 欧州時間 (16-22h ブレイク・リテスト)", df_tr[df_tr["session"] == "LONDON_BREAK"])

    print("\n■ 地合い（LONG vs SHORT）× セッション別内訳:")
    _print_stats("🟢 BUY地合い × 🗼 東京レンジ(LONG)", df_tr[(df_tr["side"] == "LONG") & (df_tr["session"] == "TOKYO_RANGE")])
    _print_stats("🟢 BUY地合い × 🏰 欧州リテスト(LONG)", df_tr[(df_tr["side"] == "LONG") & (df_tr["session"] == "LONDON_BREAK")])
    _print_stats("🔴 SELL地合い × 🗼 東京レンジ(SHORT)", df_tr[(df_tr["side"] == "SHORT") & (df_tr["session"] == "TOKYO_RANGE")])
    _print_stats("🔴 SELL地合い × 🏰 欧州ブレイク(SHORT)", df_tr[(df_tr["side"] == "SHORT") & (df_tr["session"] == "LONDON_BREAK")])

    print("\n■ 戦略別内訳 (リテスト効果の確認):")
    for st_name in ["val_poc", "vah_retest", "vah_poc", "val_break"]:
        _print_stats(f"📌 {st_name.upper()}", df_tr[df_tr["strategy"] == st_name])

    print("\n■ 年次推移 (2024年 〜 2026年):")
    for yr in sorted(df_tr["year"].unique()):
        sub_y = df_tr[df_tr["year"] == yr]
        sub_tk = sub_y[sub_y["session"] == "TOKYO_RANGE"]
        sub_ld = sub_y[sub_y["session"] == "LONDON_BREAK"]
        print(
            f"  [{yr}年] ➔ 全体: {sub_y['pnl'].sum():+7.2f} ({len(sub_y):3d}回/勝率{(sub_y['pnl']>0).mean()*100:4.1f}%) | "
            f"🗼東京: {sub_tk['pnl'].sum():+7.2f} | 🏰欧州: {sub_ld['pnl'].sum():+7.2f}"
        )

    # チャート保存
    df_tr = df_tr.sort_values("exit_time").reset_index(drop=True)
    df_tr["cum_pnl"] = df_tr["pnl"].cumsum()
    df_tr["tokyo_pnl_step"] = np.where(df_tr["session"] == "TOKYO_RANGE", df_tr["pnl"], 0.0)
    df_tr["london_pnl_step"] = np.where(df_tr["session"] == "LONDON_BREAK", df_tr["pnl"], 0.0)
    df_tr["cum_tokyo"] = df_tr["tokyo_pnl_step"].cumsum()
    df_tr["cum_london"] = df_tr["london_pnl_step"].cumsum()

    fig, ax = plt.subplots(figsize=(13.0, 6.5))
    ax.plot(df_tr["exit_time"], df_tr["cum_pnl"], label=f"Total 2024-2026 Portfolio ({df_tr['cum_pnl'].iloc[-1]:+.2f} USDT)", color="limegreen", linewidth=2.4)
    ax.plot(df_tr["exit_time"], df_tr["cum_tokyo"], label=f"Tokyo Range (VAL_POC / VAH_POC) ({df_tr['cum_tokyo'].iloc[-1]:+.2f} USDT)", color="dodgerblue", linewidth=1.5)
    ax.plot(df_tr["exit_time"], df_tr["cum_london"], label=f"London Break & Retest (VAH_RETEST / VAL_BREAK) ({df_tr['cum_london'].iloc[-1]:+.2f} USDT)", color="darkorange", linewidth=1.5)
    ax.axhline(0, color="gray", linestyle="--", linewidth=1.0)
    ax.set_title("2024-2026 Walk-Forward Backtest (8H Regime x Tokyo VP Range & London VAH-Retest) [$15 Single Lot, Max 2 Pos]", fontsize=12)
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Cumulative Net PnL (USDT)")
    ax.legend(loc="upper left")
    ax.grid(True, linestyle=":", alpha=0.5)
    plt.tight_layout()
    chart_out = BASE_DIR / "Data" / "backtest_2024_retest.png"
    plt.savefig(chart_out, dpi=150)
    plt.close()
    print(f"\n📈 2024年以降リテスト検証チャート保存完了: {chart_out}")
    print(f"⏱️ 総計算時間: {time.time() - t0:.1f}秒")


if __name__ == "__main__":
    main()
