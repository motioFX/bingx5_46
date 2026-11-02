#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_4y_session_regime.py
過去4年超（2021年〜2026年）の 5分足データ（20銘柄 + BTC）を用いた
「8時間ごと（01:00 / 09:00 / 17:00 JST）3態勢地合い判定 × 東京レンジ＆欧州ブレイク」
ウォークフォワード・ポートフォリオ検証スクリプト
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
    simulate_vah_break_fast,
    simulate_val_break_fast,
    optimize_rsima3_fast,
    optimize_rsima3_short_fast,
)


def calculate_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -1.0 * delta.clip(upper=0.0)
    ma_up = up.ewm(alpha=1.0 / length, adjust=False).mean()
    ma_down = down.ewm(alpha=1.0 / length, adjust=False).mean()
    rs = ma_up / ma_down.replace(0.0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


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
        return (vr >= 1.3) & (vr <= 4.0)
    else:
        return (vr >= 0.6) & (vr <= 2.5)


def optimize_symbol_30d_fast(df_5m_30d: pd.DataFrame, side_mode: str = "long", trade_size: float = 15.0) -> Dict[str, Any]:
    c5 = df_5m_30d["close"].values
    h5 = df_5m_30d["high"].values
    l5 = df_5m_30d["low"].values
    o5 = df_5m_30d["open"].values
    v5 = df_5m_30d["volume"].values

    rsi5 = calculate_rsi(pd.Series(c5), 14).values
    rsima5 = pd.Series(rsi5).ewm(span=7, adjust=False).mean().values

    vf_rng = build_vol_filter(v5, is_breakout=False)
    vf_brk = build_vol_filter(v5, is_breakout=True)

    mp_candidates = [150, 200, 300]
    tr_candidates = [0.60, 0.70]

    best_rng = None
    best_rng_score = -1e9
    best_brk = None
    best_brk_score = -1e9

    def _score_cand(res: Dict[str, Any], min_tr: int = 8) -> Tuple[float, bool, str]:
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
        elif cnt >= 4 and pnl > 0:
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
                sim_b = simulate_vah_break_fast(
                    c5, h5, l5, val, vah, poc, vf_brk,
                    initial_equity=trade_size, max_trades=1, cooldown_bars=288, timeout_bars=48, target_ratio=tr
                )
                b_strat = "vah_break"
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

            sc_r, q_r, ql_r = _score_cand(sim_r, min_tr=6)
            if sc_r > best_rng_score:
                best_rng_score = sc_r
                best_rng = {
                    "strategy": r_strat, "interval": 5, "mp": mp, "target_ratio": tr,
                    "pnl": sim_r["final_pnl"], "trade_count": sim_r["trade_count"],
                    "win_rate": sim_r["win_rate"], "is_qualified": q_r, "qual_level": ql_r
                }

            sc_b, q_b, ql_b = _score_cand(sim_b, min_tr=3)
            if not q_b and sim_b["trade_count"] >= 3 and sim_b["final_pnl"] > 0 and sim_b["pnl_second_half"] > 0:
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

    # RSIMA / RSIMA_SHORT は完全削除 (純粋 Volume Profile のみ)

    return {
        "range_params": best_rng,
        "breakout_params": best_brk,
        "is_range_qualified": bool(best_rng and best_rng.get("is_qualified", False)),
        "is_breakout_qualified": bool(best_brk and best_brk.get("is_qualified", False)),
    }


def simulate_oos_window_8h_regime(
    df_5m_full_slice: pd.DataFrame,
    oos_start_idx: int,
    dual_params: Dict[str, Any],
    regime_8h_times: np.ndarray,
    regime_8h_data: List[Tuple[str, List[str]]],
    symbol: str,
    side_mode: str = "long",
    trade_size: float = 15.0,
) -> List[Dict[str, Any]]:
    """
    1銘柄について、oos_start_idx 以降の期間を
    - 8時間ごと（01:00, 09:00, 17:00 JST）の地合い判定結果をリアルタイム参照
    - その時点で地合いが一致（BUY->long / SELL->short）し、かつ推奨銘柄に入っている時間帯のみ新規エントリー許可
    - 地合いが外れた瞬間（NEUTRALまたは逆方向へ反転）は、待機指値即時キャンセル ＆ 保有ポジションは建値+0.20%で早期撤退
    """
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

    r_strat = str(rng_p.get("strategy", "rsima")).lower()
    r_mp = int(rng_p.get("mp", 200))
    r_tr = float(rng_p.get("target_ratio", 0.70))
    b_strat = str(brk_p.get("strategy", "vah_break" if side_mode == "long" else "val_break")).lower()
    b_mp = int(brk_p.get("mp", 200))
    b_tr = float(brk_p.get("target_ratio", 0.70))

    poc_r, val_r, vah_r = compute_fast_vp(c, h, l, o, v, period=r_mp, num_bins=40, step=6)
    if b_mp == r_mp:
        poc_b, val_b, vah_b = poc_r, val_r, vah_r
    else:
        poc_b, val_b, vah_b = compute_fast_vp(c, h, l, o, v, period=b_mp, num_bins=40, step=6)

    rsi = calculate_rsi(pd.Series(c), 14).values
    rsima = pd.Series(rsi).ewm(span=7, adjust=False).mean().values

    vf_rng = build_vol_filter(v, is_breakout=False)
    vf_brk = build_vol_filter(v, is_breakout=True)

    maker_fee = 0.0002
    taker_fee = 0.0006
    cooldown_bars = 288  # 24h
    timeout_bars = 48    # 4h
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

    # 各バー時点での最新8時間地合いスロットのインデックスを searchsorted で事前マッピング
    slot_indices = np.searchsorted(regime_8h_times, ts_utc_np, side="right") - 1
    slot_indices = np.clip(slot_indices, 0, len(regime_8h_data) - 1)

    for i in range(max(12, oos_start_idx), n):
        ci = c[i]
        hi = h[i]
        li = l[i]
        hr_jst = int(jst_hours_arr[i])
        
        # 8時間スロットごとの地合い判定結果を参照
        slot_idx = int(slot_indices[i])
        cur_reg, cur_rec_syms = regime_8h_data[slot_idx]
        is_regime_active_now = (cur_reg == expected_reg) and (symbol in cur_rec_syms)

        is_tokyo = (9 <= hr_jst < 16)
        is_london = (16 <= hr_jst < 22)

        # 地合いが外れた場合は待機指値を即座にキャンセル
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
                if entry_strat == "vah_break":
                    va_d = max(0.0, vah_b[i] - val_b[i]) if not np.isnan(vah_b[i]) else 0.0
                    raw_sl = vah_b[i] - va_d * 0.35 if va_d > 0 else avg_px * 0.985
                    poc_fl = poc_b[i] * 1.001 if (not np.isnan(poc_b[i]) and poc_b[i] < avg_px) else raw_sl
                    sl_dist = max(avg_px * 0.004, avg_px - max(raw_sl, poc_fl))
                    entry_sl_px = avg_px - sl_dist
                    target_tp = avg_px + sl_dist * 2.0
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

        # 1. 保有中ポジションの決済監視 (24時間フル稼働 + 8時間地合い変化時の建値+0.20%早期撤退)
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
                elif entry_strat == "vah_break" and not np.isnan(poc_b[i]) and ci < poc_b[i] and ci < avg_px * 0.996:
                    is_exit = True
                    exit_px = ci
                    exit_type = "SL_POC"
                elif (i - entry_bar_idx) >= timeout_bars and ci < avg_px:
                    is_exit = True
                    exit_px = ci
                    exit_type = "TIMEOUT"
                else:
                    if entry_strat in ("val_poc", "vah_break"):
                        tp_now = target_tp
                        if entry_strat == "val_poc" and not np.isnan(val_r[i]) and not np.isnan(vah_r[i]) and vah_r[i] > val_r[i]:
                            tp_now = val_r[i] + (vah_r[i] - val_r[i]) * r_tr
                        if (hi >= tp_now or ci >= tp_now) and tp_now >= avg_px * 1.0040:
                            is_exit = True
                            exit_px = max(ci, tp_now) if hi >= tp_now else ci
                            exit_type = "TP"
                    else:
                        l_cp = float(rng_p.get("l_cp", 65.0))
                        if rsi[i] > l_cp and ci >= avg_px * 1.0040:
                            is_exit = True
                            exit_px = ci
                            exit_type = "TP_RSI"

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
                    else:
                        s_cp = float(rng_p.get("s_cp", 35.0))
                        if rsi[i] < s_cp and ci <= avg_px * 0.9960:
                            is_exit = True
                            exit_px = ci
                            exit_type = "TP_RSI"

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

        # 2. 新規エントリー判定 (最新8時間の地合いが一致しているスロットのみ)
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
                        l_ep = float(rng_p.get("l_ep", 35.0))
                        gc = (rsi[i] > rsima[i]) and (rsi[i - 1] <= rsima[i - 1]) and (rsima[i] >= rsima[i - 1])
                        if gc and (rsima[i] < l_ep or rsima[i - 1] < l_ep):
                            pending_px = ci * 0.9998
                            pending_session = "TOKYO_RANGE"
                            pending_strat = "rsima"
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
                    else:
                        s_ep = float(rng_p.get("s_ep", 65.0))
                        dc = (rsi[i] < rsima[i]) and (rsi[i - 1] >= rsima[i - 1]) and (rsima[i] <= rsima[i - 1])
                        if dc and (rsima[i] > s_ep or rsima[i - 1] > s_ep):
                            pending_px = ci * 1.0002
                            pending_session = "TOKYO_RANGE"
                            pending_strat = "rsima_short"

            elif is_london and is_brk_q and vf_brk[i]:
                if side_mode == "long":
                    if not np.isnan(vah_b[i]) and not np.isnan(vah_b[i - 1]) and not np.isnan(val_b[i]):
                        va_d = max(0.0, vah_b[i] - val_b[i])
                        vah_brk = (c[i - 1] <= vah_b[i - 1]) and (ci > vah_b[i]) and (ci <= vah_b[i] + va_d * 0.35)
                        va_w = va_d / val_b[i] if val_b[i] > 0 else 0.0
                        if vah_brk and va_w >= 0.015:
                            pending_px = ci * 0.9998
                            pending_session = "LONDON_BREAK"
                            pending_strat = "vah_break"
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
    print("🚀 【過去4年超 (2021-2026) 8時間ごと(01/09/17 JST)地合い判定 × 東京レンジ＆欧州ブレイク検証】")
    print("=" * 95)

    # 1. 全21銘柄の 5m Pickle ロード ＆ 1H リサンプル生成
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

    print(f"✅ 全 {len(dfs_5m)} 銘柄の 5m & 1H 時系列ロード完了 ({time.time() - t0:.1f}秒)")

    # 2. 8時間ごと (01:00, 09:00, 17:00 JST / 16:00, 00:00, 08:00 UTC) の地合い判定を先行一括算出
    btc_1h = dfs_1h["BTC-USDT"]
    start_dt = pd.Timestamp("2021-02-05 00:00:00", tz="UTC")
    end_dt = pd.to_datetime(btc_1h["timestamp"].iloc[-1], utc=True)

    # JST 01:00 (前日16:00 UTC), JST 09:00 (00:00 UTC), JST 17:00 (08:00 UTC)
    # 00:00 UTC から 8時間間隔のグリッドを生成
    slot_times_utc = pd.date_range(start=start_dt, end=end_dt, freq="8h")
    print(f"🔍 全期間 ({len(slot_times_utc)} スロット) の「8時間ごと地合い判定 (01/09/17 JST)」を計算中...")
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
        f"✅ 8時間地合い判定 完了 ({tot_slots} スロット / {time.time() - t_reg0:.1f}秒) -> "
        f"🟢BUY: {slot_counts['BUY']}回 ({slot_counts['BUY']/max(1,tot_slots)*100:.1f}%), "
        f"🔴SELL: {slot_counts['SELL']}回 ({slot_counts['SELL']/max(1,tot_slots)*100:.1f}%), "
        f"⚪NEUTRAL: {slot_counts['NEUTRAL']}回 ({slot_counts['NEUTRAL']/max(1,tot_slots)*100:.1f}%)"
    )

    # 3. ウォークフォワード検証（7日ごとに直近30日最適化を更新し、8時間ごとのリアルタイム地合いに従ってOOSトレード）
    ts_5m_map = {s: d["timestamp"].values for s, d in dfs_5m.items()}
    step_days = 7
    curr_dt = start_dt
    all_oos_trades: List[Dict[str, Any]] = []
    step_idx = 0

    while curr_dt < end_dt:
        next_dt = curr_dt + pd.Timedelta(days=step_days)

        # この7日間の8時間スロットの中に BUY/SELL が点灯した銘柄を集計
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

                dual_p = optimize_symbol_30d_fast(train_5m, side_mode=side_mode, trade_size=15.0)
                if not dual_p["is_range_qualified"] and not dual_p["is_breakout_qualified"]:
                    continue

                sym_oos_trades = simulate_oos_window_8h_regime(
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

        # ポートフォリオ同時保有枠制限 (MAX_ACTIVE_POSITIONS = 2 枠厳守)
        cand_trades_pool.sort(key=lambda x: x["entry_time"])
        active_exit_times: List[pd.Timestamp] = []
        for tr in cand_trades_pool:
            ent_t = tr["entry_time"]
            active_exit_times = [et for et in active_exit_times if et > ent_t]
            if len(active_exit_times) < 2:
                active_exit_times.append(tr["exit_time"])
                all_oos_trades.append(tr)

        step_idx += 1
        if step_idx % 26 == 0:
            print(f"  ... {curr_dt.strftime('%Y-%m-%d')} まで検証完了 (累計OOSトレード: {len(all_oos_trades)} 回 | 経過: {time.time() - t0:.1f}秒)")
        curr_dt = next_dt

    # 4. 集計レポート出力
    print("\n" + "=" * 95)
    print("📊 【4年超ウォークフォワード検証結果 (8時間更新 01/09/17 JST / $15単発固定・最大2枠)】")
    print("=" * 95)
    print(f"■ 8時間地合い判定内訳 (全 {tot_slots} スロット):")
    print(f"  ・🟢 BUY (買い態勢)     : {slot_counts['BUY']:4d} 回 ({slot_counts['BUY']/max(1,tot_slots)*100:.1f}%)")
    print(f"  ・🔴 SELL (売り態勢)    : {slot_counts['SELL']:4d} 回 ({slot_counts['SELL']/max(1,tot_slots)*100:.1f}%)")
    print(f"  ・⚪ NEUTRAL (待機態勢) : {slot_counts['NEUTRAL']:4d} 回 ({slot_counts['NEUTRAL']/max(1,tot_slots)*100:.1f}%)")

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

    df_vp_only = df_tr[df_tr["strategy"].isin(["val_poc", "vah_poc", "vah_break", "val_break"])]
    df_vp_3core = df_tr[df_tr["strategy"].isin(["val_poc", "vah_poc", "val_break"])]  # 欧州買いブレイクを除いた真のVPコア

    print("\n■ 総合・セッション別成績 ($15 USDT / 1ポジション単発固定):")
    _print_stats("🏆 全体総合 (全6戦略: VP+RSIMA)", df_tr)
    _print_stats("💎 純粋VP 4戦略 (RSIMA除外)", df_vp_only)
    _print_stats("🌟 厳選VP 3戦略 (VAL_POC/VAH_POC/VAL_BRK)", df_vp_3core)
    _print_stats("🗼 東京時間 (09-16h レンジ逆張り全体)", df_tr[df_tr["session"] == "TOKYO_RANGE"])
    _print_stats("🏰 欧州時間 (16-22h ブレイクアウト全体)", df_tr[df_tr["session"] == "LONDON_BREAK"])

    print("\n■ 地合い（LONG vs SHORT）× セッション別内訳:")
    _print_stats("🟢 BUY地合い × 🗼 東京レンジ(LONG)", df_tr[(df_tr["side"] == "LONG") & (df_tr["session"] == "TOKYO_RANGE")])
    _print_stats("🟢 BUY地合い × 🏰 欧州ブレイク(LONG)", df_tr[(df_tr["side"] == "LONG") & (df_tr["session"] == "LONDON_BREAK")])
    _print_stats("🔴 SELL地合い × 🗼 東京レンジ(SHORT)", df_tr[(df_tr["side"] == "SHORT") & (df_tr["session"] == "TOKYO_RANGE")])
    _print_stats("🔴 SELL地合い × 🏰 欧州ブレイク(SHORT)", df_tr[(df_tr["side"] == "SHORT") & (df_tr["session"] == "LONDON_BREAK")])

    print("\n■ 戦略別内訳:")
    for st_name in ["val_poc", "rsima", "vah_break", "vah_poc", "rsima_short", "val_break"]:
        _print_stats(f"📌 {st_name.upper()}", df_tr[df_tr["strategy"] == st_name])

    print("\n■ 年次推移 (2021年 〜 2026年):")
    for yr in sorted(df_tr["year"].unique()):
        sub_y = df_tr[df_tr["year"] == yr]
        sub_vp = df_vp_only[df_vp_only["year"] == yr]
        sub_tk = sub_y[sub_y["session"] == "TOKYO_RANGE"]
        sub_ld = sub_y[sub_y["session"] == "LONDON_BREAK"]
        print(
            f"  [{yr}年] ➔ 全体: {sub_y['pnl'].sum():+7.2f} ({len(sub_y):3d}回) | "
            f"💎純粋VP: {sub_vp['pnl'].sum():+7.2f} ({len(sub_vp):3d}回) | "
            f"🗼東京: {sub_tk['pnl'].sum():+7.2f} | 🏰欧州: {sub_ld['pnl'].sum():+7.2f}"
        )

    print("\n■ 銘柄別ランキング Top 15:")
    sym_grp = df_tr.groupby("symbol").agg(
        trades=("pnl", "count"),
        win_rate=("pnl", lambda s: (s > 0).mean() * 100.0),
        total_pnl=("pnl", "sum"),
        tokyo_pnl=("pnl", lambda s: s[df_tr.loc[s.index, "session"] == "TOKYO_RANGE"].sum()),
        london_pnl=("pnl", lambda s: s[df_tr.loc[s.index, "session"] == "LONDON_BREAK"].sum()),
        vp_pnl=("pnl", lambda s: s[df_tr.loc[s.index, "strategy"].isin(["val_poc", "vah_poc", "val_break"])].sum()),
    ).sort_values("total_pnl", ascending=False)
    for sym_n, r in sym_grp.head(15).iterrows():
        print(
            f"  ・{sym_n:12s}: 全体 {r['total_pnl']:+7.2f} USDT ({int(r['trades']):3d}回/勝率{r['win_rate']:4.1f}%) "
            f"[厳選VP: {r['vp_pnl']:+6.2f} | 🗼東京: {r['tokyo_pnl']:+6.2f} | 🏰欧州: {r['london_pnl']:+6.2f}]"
        )

    # チャート保存
    df_tr = df_tr.sort_values("exit_time").reset_index(drop=True)
    df_tr["cum_pnl"] = df_tr["pnl"].cumsum()
    df_tr["tokyo_pnl_step"] = np.where(df_tr["session"] == "TOKYO_RANGE", df_tr["pnl"], 0.0)
    df_tr["london_pnl_step"] = np.where(df_tr["session"] == "LONDON_BREAK", df_tr["pnl"], 0.0)
    df_tr["vp_only_step"] = np.where(df_tr["strategy"].isin(["val_poc", "vah_poc", "vah_break", "val_break"]), df_tr["pnl"], 0.0)
    df_tr["vp3_pnl_step"] = np.where(df_tr["strategy"].isin(["val_poc", "vah_poc", "val_break"]), df_tr["pnl"], 0.0)
    df_tr["rsima_pnl_step"] = np.where(df_tr["strategy"].isin(["rsima", "rsima_short"]), df_tr["pnl"], 0.0)

    df_tr["cum_tokyo"] = df_tr["tokyo_pnl_step"].cumsum()
    df_tr["cum_london"] = df_tr["london_pnl_step"].cumsum()
    df_tr["cum_vp_only"] = df_tr["vp_only_step"].cumsum()
    df_tr["cum_vp3"] = df_tr["vp3_pnl_step"].cumsum()
    df_tr["cum_rsima"] = df_tr["rsima_pnl_step"].cumsum()

    fig, ax = plt.subplots(figsize=(13.5, 7.0))
    ax.plot(df_tr["exit_time"], df_tr["cum_pnl"], label=f"Total All 6 Strategies ({df_tr['cum_pnl'].iloc[-1]:+.2f} USDT)", color="black", linewidth=2.0)
    ax.plot(df_tr["exit_time"], df_tr["cum_vp_only"], label=f"Pure VP 4 Strategies ({df_tr['cum_vp_only'].iloc[-1]:+.2f} USDT)", color="blue", linewidth=1.8)
    ax.plot(df_tr["exit_time"], df_tr["cum_vp3"], label=f"Selected VP 3 Core (VAL_POC + VAH_POC + VAL_BREAK) ({df_tr['cum_vp3'].iloc[-1]:+.2f} USDT)", color="limegreen", linewidth=2.4)
    ax.plot(df_tr["exit_time"], df_tr["cum_tokyo"], label=f"Tokyo Session Range (09-16 JST) ({df_tr['cum_tokyo'].iloc[-1]:+.2f} USDT)", color="dodgerblue", linewidth=1.4, alpha=0.85)
    ax.plot(df_tr["exit_time"], df_tr["cum_london"], label=f"London Session Breakout (16-22 JST) ({df_tr['cum_london'].iloc[-1]:+.2f} USDT)", color="darkorange", linewidth=1.4, alpha=0.85)
    ax.plot(df_tr["exit_time"], df_tr["cum_rsima"], label=f"RSIMA Long/Short Only ({df_tr['cum_rsima'].iloc[-1]:+.2f} USDT)", color="crimson", linestyle="--", linewidth=1.5, alpha=0.8)
    ax.axhline(0, color="gray", linestyle="--", linewidth=1.0)
    ax.set_title("4-Year Walk-Forward Backtest (8-Hour Regime Update: 01/09/17 JST) [$15 Single Lot, Max 2 Pos]", fontsize=12)
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Cumulative Net PnL (USDT)")
    ax.legend(loc="upper left")
    ax.grid(True, linestyle=":", alpha=0.5)
    plt.tight_layout()
    chart_out = BASE_DIR / "Data" / "backtest_4y_session_regime.png"
    plt.savefig(chart_out, dpi=150)
    plt.close()
    print(f"\n📈 4年間累積損益チャート(8時間更新版)保存完了: {chart_out}")
    print(f"⏱️ 総計算時間: {time.time() - t0:.1f}秒")


if __name__ == "__main__":
    main()
