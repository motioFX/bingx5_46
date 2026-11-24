# -*- coding: utf-8 -*-
"""F7 Two-Pillar 戦略体系 Single Source of Truth (SSoT).

構成 (`F7_30mNoEU_S8bNoExh_WF90d`):
  1. Pillar 1 (LONG): `val_poc_30m` (`30m`足, `ULTRA_BUY`限定, BUYノーマライズ除外なし, 欧州16-22 JST除外)
  2. Pillar 2 (SHORT): `h3b_squeeze_1h` (`1h`足, `overall_regime == "SELL"`, SELLノーマライズ除外なし,
                       NoExh `btc_ret14d >= -10% & sym_ret14d >= -20%`, 因果 WF90d ヒステリシスゲート)

バックテスト (`simulate_f7_portfolio_from_lake`) とライブ運用 (`evaluate_val_poc_30m_entry/exit`,
`evaluate_h3b_squeeze_1h_entry/exit`, `evaluate_h3b_wf90d_gate_from_lake`) の数式・閾値・決済優先順位を
100% 同一に保つ単一モジュール。
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from symbol_universe import (
    BENCHMARK_SYMBOL,
    TRADABLE_MID_CAP_SYMBOLS,
    normalize_symbol,
)

# ---------------------------------------------------------------------------
# F7 検証済み固定パラメータ (Single Source of Truth)
# ---------------------------------------------------------------------------
F7_MODE_NAME: str = "F7_30mNoEU_S8bNoExh_WF90d"
F7_LONG_STRATEGY: str = "val_poc_30m"
F7_SHORT_STRATEGY: str = "h3b_squeeze_1h"
F7_STRATEGIES: Tuple[str, str] = (F7_LONG_STRATEGY, F7_SHORT_STRATEGY)

F7_MAX_ACTIVE_POSITIONS: int = 2
F7_POSITION_COST_USDT: float = 15.0
F7_FEE_MAKER: float = 0.0002
F7_FEE_TAKER: float = 0.0006
F7_DAILY_CB_LIMIT_USDT: float = 1.50

# Pillar 1: val_poc_30m (LONG)
F7_LONG_TIMEFRAME: str = "30m"
F7_LONG_TF_SEC: int = 1800
F7_LONG_MP_PERIOD: int = 120
F7_LONG_MP_HALF_LIFE: float = 60.0
F7_LONG_MP_BINS: int = 50
F7_LONG_MP_VA_PCT: float = 0.70
F7_LONG_TARGET_RATIO: float = 0.70
F7_LONG_MIN_TP_PCT: float = 0.006
F7_LONG_SL_ATR_MULT: float = 2.0
F7_LONG_MAX_SL_PCT: float = 0.040
F7_LONG_FLIP_BE_PCT: float = 0.0020
F7_LONG_VOL_RATIO_MIN: float = 1.2
F7_LONG_VAL_LOW_MULT: float = 0.965
F7_LONG_VAL_HIGH_MULT: float = 1.002
F7_LONG_WARMUP_BARS: int = 155
F7_LONG_MAX_HOLD_BARS: int = 48
F7_LONG_MAX_HOLD_MINUTES: int = 1440
F7_LONG_EU_BLOCK_START_JST: int = 16
F7_LONG_EU_BLOCK_END_JST: int = 22

# Pillar 2: h3b_squeeze_1h (SHORT / S8b)
F7_SHORT_TIMEFRAME: str = "1h"
F7_SHORT_TF_SEC: int = 3600
F7_SHORT_BB_PERIOD: int = 20
F7_SHORT_SQ_LOOKBACK: int = 168
F7_SHORT_SQ_MIN_PERIODS: int = 50
F7_SHORT_SQ_QUANTILE: float = 0.25
F7_SHORT_DONCHIAN_PERIOD: int = 48
F7_SHORT_VOL_RATIO_MIN: float = 1.5
F7_SHORT_WARMUP_BARS: int = 170
F7_SHORT_INIT_SL_ATR_MULT: float = 2.0
F7_SHORT_TRAIL_ATR_MULT: float = 2.0
F7_SHORT_BE_LOCK_ATR_MULT: float = 1.5
F7_SHORT_BE_LOCK_PX_MULT: float = 0.9980
F7_SHORT_FLIP_BE_PCT: float = 0.0020
F7_SHORT_MAX_HOLD_BARS: int = 120
F7_SHORT_MAX_HOLD_MINUTES: int = 7200

# Short 疲れ切り除外 (NoExh) & 因果 WF90d ヒステリシスゲート
F7_NOEXH_BTC_RET14D_MIN: float = -0.10
F7_NOEXH_SYM_RET14D_MIN: float = -0.20
F7_WF90D_WINDOW_DAYS: int = 90
F7_WF90D_MIN_TRADES: int = 10
F7_WF90D_PAUSE_NET_LT: float = -3.00
F7_WF90D_PAUSE_PF_LT: float = 0.85
F7_WF90D_RESUME_PF_GTE: float = 1.00
F7_WF90D_RESUME_NET_GTE: float = 0.0

JST_TZ = timezone(timedelta(hours=9))
_DECAY_WEIGHTS_120_HL60: np.ndarray = (0.5 ** (1.0 / F7_LONG_MP_HALF_LIFE)) ** np.arange(
    F7_LONG_MP_PERIOD - 1, -1, -1, dtype=np.float64
)


def _compute_mp_levels(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    end_idx: int,
    period: int = F7_LONG_MP_PERIOD,
    num_bins: int = F7_LONG_MP_BINS,
    value_area_pct: float = F7_LONG_MP_VA_PCT,
    full_weights: Optional[np.ndarray] = _DECAY_WEIGHTS_120_HL60,
) -> Tuple[float, float, float]:
    """`*_3logic.compute_mp_levels_at_bar` を単一正本として (poc, val, vah) を返す."""
    try:
        from bingx5_46_3logic import compute_mp_levels_at_bar
    except ImportError:
        from hype5_45_3logic import compute_mp_levels_at_bar  # type: ignore
    poc, val, vah, _, _ = compute_mp_levels_at_bar(
        opens,
        highs,
        lows,
        closes,
        volumes,
        end_idx=int(end_idx),
        period=int(period),
        num_bins=int(num_bins),
        value_area_pct=float(value_area_pct),
        full_weights=full_weights,
        compute_80=False,
    )
    return float(poc), float(val), float(vah)


# ---------------------------------------------------------------------------
# 共通テクニカル・時間・正規化・NoExh 計算
# ---------------------------------------------------------------------------
def is_f7_strategy(strategy_name: Optional[str]) -> bool:
    return str(strategy_name or "").strip().lower() in F7_STRATEGIES


def resolve_f7_strategy_side(strategy_name: Optional[str]) -> str:
    s = str(strategy_name or "").strip().lower()
    if s == F7_SHORT_STRATEGY:
        return "short"
    if s == F7_LONG_STRATEGY:
        return "long"
    return "short" if "short" in s or "vah_poc" in s or "val_break" in s else "long"


def is_eu_session_jst(ts_sec: Optional[int] = None) -> bool:
    """JST 16:00〜22:00（欧州時間）であれば True を返す."""
    eff = int(ts_sec) if ts_sec is not None else int(time.time())
    hr_jst = ((eff + 9 * 3600) // 3600) % 24
    return F7_LONG_EU_BLOCK_START_JST <= hr_jst < F7_LONG_EU_BLOCK_END_JST


def calc_atr_series(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    window: int = 14,
) -> np.ndarray:
    """SMA ベースの ATR14 (`min_periods=1`) を算出する."""
    h = np.asarray(highs, dtype=np.float64)
    l = np.asarray(lows, dtype=np.float64)
    c = np.asarray(closes, dtype=np.float64)
    if len(c) == 0:
        return np.array([], dtype=np.float64)
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(int(window), min_periods=1).mean().to_numpy(dtype=np.float64)


def calc_vol_ratio_series(
    volumes: np.ndarray,
    window: int = 20,
    min_periods: int = 5,
) -> np.ndarray:
    """確定出来高 / 直前20本平均出来高 (`shift(1)`) の倍率を算出する."""
    v = np.asarray(volumes, dtype=np.float64)
    if len(v) == 0:
        return np.array([], dtype=np.float64)
    ma = pd.Series(v).rolling(int(window), min_periods=int(min_periods)).mean().shift(1).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((ma > 0.0) & np.isfinite(ma), v / ma, 1.0)


def compute_ret14d_from_1h_close(close_arr: np.ndarray) -> float:
    """確定 1h 終値配列から直近 14日 (336本) リターンを因果算出する (`idx[-336] -> idx[-1]`)."""
    arr = np.asarray(close_arr, dtype=np.float64)
    if len(arr) < 336:
        return 0.0
    c0 = float(arr[-336])
    c1 = float(arr[-1])
    if not (np.isfinite(c0) and np.isfinite(c1) and c0 > 0.0):
        return 0.0
    return (c1 / c0) - 1.0


def check_f7_no_exhaustion(
    btc_ret14d: Optional[float],
    sym_ret14d: Optional[float],
) -> Tuple[bool, str]:
    """H3b NoExh ゲート (`btc_ret14d >= -10%` かつ `sym_ret14d >= -20%`)."""
    b_val = float(btc_ret14d) if (btc_ret14d is not None and np.isfinite(btc_ret14d)) else 0.0
    s_val = float(sym_ret14d) if (sym_ret14d is not None and np.isfinite(sym_ret14d)) else 0.0
    if b_val < F7_NOEXH_BTC_RET14D_MIN:
        return False, f"BTC_EXHAUSTED({b_val * 100.0:+.1f}%<{F7_NOEXH_BTC_RET14D_MIN * 100.0:.0f}%)"
    if s_val < F7_NOEXH_SYM_RET14D_MIN:
        return False, f"SYM_EXHAUSTED({s_val * 100.0:+.1f}%<{F7_NOEXH_SYM_RET14D_MIN * 100.0:.0f}%)"
    return True, ""


def _calc_profit_factor(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    gp = float(pnls[pnls > 0.0].sum())
    gl = float(-pnls[pnls <= 0.0].sum())
    return (gp / gl) if gl > 0.0 else (99.9 if gp > 0.0 else 0.0)


def _normalize_candle_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts_utc", "open", "high", "low", "close", "volume"])
    out = df.copy()
    if "ts_utc" not in out.columns:
        if "timestamp" in out.columns:
            ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
            out["ts_utc"] = (ts.astype("int64") // 1_000_000_000).astype(np.int64)
        else:
            return pd.DataFrame(columns=["ts_utc", "open", "high", "low", "close", "volume"])
    out["ts_utc"] = pd.to_numeric(out["ts_utc"], errors="coerce")
    out = out.dropna(subset=["ts_utc", "open", "high", "low", "close"]).copy()
    out["ts_utc"] = out["ts_utc"].astype(np.int64)
    if "volume" not in out.columns:
        out["volume"] = 0.0
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(np.float64)
    out = out.dropna(subset=["open", "high", "low", "close"]).sort_values("ts_utc").drop_duplicates("ts_utc", keep="last")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Pillar 1: val_poc_30m (LONG) エントリー / ターゲット / エグジット
# ---------------------------------------------------------------------------
def compute_val_poc_30m_entry_levels(
    entry_price: float,
    atr_at_signal: float,
    val: float,
    vah: float,
    poc: float,
) -> Dict[str, float]:
    epx = float(entry_price)
    cur_atr = float(atr_at_signal)
    sl_dist = min(F7_LONG_SL_ATR_MULT * cur_atr, epx * F7_LONG_MAX_SL_PCT) if (epx > 0.0 and cur_atr > 0.0) else epx * F7_LONG_MAX_SL_PCT
    init_sl = max(0.0, epx - sl_dist)
    be_px = epx * (1.0 + F7_LONG_FLIP_BE_PCT)
    valid_va = (vah > val > 0.0) and np.isfinite(vah) and np.isfinite(val)
    if valid_va:
        tp_cand = float(val) + (float(vah) - float(val)) * F7_LONG_TARGET_RATIO
    elif poc > 0.0 and np.isfinite(poc):
        tp_cand = float(poc)
    else:
        tp_cand = epx * (1.0 + F7_LONG_MIN_TP_PCT)
    target_tp = max(tp_cand, epx * (1.0 + F7_LONG_MIN_TP_PCT))
    return {
        "init_sl": float(init_sl),
        "be_px": float(be_px),
        "target_tp": float(target_tp),
        "entry_atr": float(cur_atr),
    }


def evaluate_val_poc_30m_entry(
    df_30m: Optional[pd.DataFrame],
    strength_tier: str,
    is_norm_excluded: bool = False,
    now_utc: Optional[int] = None,
) -> Dict[str, Any]:
    """`val_poc_30m` の最新確定 30m 足でのエントリー可否およびシグナル補助 DataFrame を返す."""
    df = _normalize_candle_df(df_30m)
    res: Dict[str, Any] = {
        "strategy": F7_LONG_STRATEGY,
        "side": "long",
        "timeframe": F7_LONG_TIMEFRAME,
        "should_enter": False,
        "gate_ok": False,
        "block_reason": "",
        "vol_ratio": 0.0,
        "atr14": 0.0,
        "close": 0.0,
        "poc": 0.0,
        "val": 0.0,
        "vah": 0.0,
        "sig_bar_open_ts": 0,
        "sig_bar_close_ts": 0,
        "signal_df": df,
    }
    if len(df) < (F7_LONG_WARMUP_BARS + 1):
        res["block_reason"] = f"INSUFFICIENT_30M_BARS({len(df)}<{F7_LONG_WARMUP_BARS + 1})"
        return res

    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)
    v_mp = np.ones(len(c), dtype=np.float64) if np.all(v <= 0.0) else v

    atr = calc_atr_series(h, l, c, 14)
    vr = calc_vol_ratio_series(v, 20, 5)

    end_idx = len(df) - 1
    sig_open_ts = int(df["ts_utc"].iloc[end_idx])
    sig_close_ts = sig_open_ts + F7_LONG_TF_SEC
    last_close = float(c[end_idx])
    last_vr = float(vr[end_idx])
    last_atr = float(atr[end_idx])

    poc, val, vah = _compute_mp_levels(o, h, l, c, v_mp, end_idx=end_idx)
    valid_va = (vah > val > 0.0) and (poc > 0.0) and np.isfinite(vah) and np.isfinite(val) and np.isfinite(poc)

    df_sig = df.copy()
    df_sig["atr14"] = atr
    df_sig["vol_ratio"] = vr
    df_sig["POC"] = poc
    df_sig["VAL"] = val
    df_sig["VAH"] = vah
    df_sig["long"] = False
    df_sig["short"] = False

    res.update(
        {
            "vol_ratio": last_vr,
            "atr14": last_atr,
            "close": last_close,
            "poc": poc,
            "val": val,
            "vah": vah,
            "sig_bar_open_ts": sig_open_ts,
            "sig_bar_close_ts": sig_close_ts,
            "signal_df": df_sig,
        }
    )

    tier_u = str(strength_tier or "").upper()
    if tier_u != "ULTRA_BUY":
        res["block_reason"] = f"TIER_NOT_ULTRA_BUY({tier_u})"
        return res
    if is_norm_excluded:
        res["block_reason"] = "NORM_PEAK_EXCLUDED"
        return res
    if is_eu_session_jst(sig_close_ts) or (now_utc is not None and is_eu_session_jst(int(now_utc))):
        res["block_reason"] = "EU_SESSION_BLOCK(16-22JST)"
        return res
    res["gate_ok"] = True

    if not valid_va:
        res["block_reason"] = "INVALID_VA"
        return res
    if not (last_atr > 0.0 and np.isfinite(last_atr)):
        res["block_reason"] = "INVALID_ATR14"
        return res
    if last_vr < F7_LONG_VOL_RATIO_MIN:
        res["block_reason"] = f"VOL_RATIO_LOW({last_vr:.2f}<{F7_LONG_VOL_RATIO_MIN:.2f})"
        return res
    if not (val * F7_LONG_VAL_LOW_MULT <= last_close <= val * F7_LONG_VAL_HIGH_MULT):
        res["block_reason"] = f"OUT_OF_VAL_BAND({last_close:.6g} not in [{val * F7_LONG_VAL_LOW_MULT:.6g}, {val * F7_LONG_VAL_HIGH_MULT:.6g}])"
        return res

    df_sig.iat[end_idx, df_sig.columns.get_loc("long")] = True
    lv = compute_val_poc_30m_entry_levels(last_close, last_atr, val, vah, poc)
    res.update(lv)
    res["should_enter"] = True
    return res


def evaluate_val_poc_30m_exit(
    entry_price: float,
    entry_atr: float,
    target_tp: float,
    init_sl: Optional[float],
    fill_bar_open_ts: int,
    current_price: float,
    overall_regime: str,
    now_utc: Optional[int] = None,
) -> Dict[str, Any]:
    """`val_poc_30m` のリアルタイム / 確定足整合エグジット判定 (`_resolve_long_val_tp_nb` 1:1準拠)."""
    epx = float(entry_price)
    px = float(current_price)
    eff_now = int(now_utc) if now_utc is not None else int(time.time())
    cur_bucket_open = (eff_now // F7_LONG_TF_SEC) * F7_LONG_TF_SEC
    fill_open = (int(fill_bar_open_ts) // F7_LONG_TF_SEC) * F7_LONG_TF_SEC if int(fill_bar_open_ts) > 0 else cur_bucket_open
    is_fill_bar = cur_bucket_open <= fill_open
    bars_held = max(0, (cur_bucket_open - fill_open) // F7_LONG_TF_SEC)
    held_minutes = max(0.0, (eff_now - fill_open) / 60.0)

    if init_sl is not None and float(init_sl) > 0.0:
        eff_sl = float(init_sl)
    else:
        cur_atr = float(entry_atr)
        sl_dist = min(F7_LONG_SL_ATR_MULT * cur_atr, epx * F7_LONG_MAX_SL_PCT) if cur_atr > 0.0 else (epx * F7_LONG_MAX_SL_PCT)
        eff_sl = epx - sl_dist
    be_px = epx * (1.0 + F7_LONG_FLIP_BE_PCT)
    tp_px = max(float(target_tp), epx * (1.0 + F7_LONG_MIN_TP_PCT)) if float(target_tp) > 0.0 else (epx * (1.0 + F7_LONG_MIN_TP_PCT))

    should_exit = False
    exit_reason = ""
    detail_reason = ""
    reg_u = str(overall_regime or "").upper()

    if epx > 0.0 and px > 0.0:
        if px <= eff_sl:
            should_exit = True
            exit_reason = "STOP_LOSS"
            detail_reason = "INIT_SL"
        elif not is_fill_bar:
            if reg_u == "SELL" and px >= be_px:
                should_exit = True
                exit_reason = "EMERGENCY_BREAKEVEN_TP"
                detail_reason = "REG_FLIP_BE"
            elif px >= tp_px:
                should_exit = True
                exit_reason = "TARGET_TP"
                detail_reason = "TARGET_TP"

        if not should_exit and (bars_held >= F7_LONG_MAX_HOLD_BARS or held_minutes >= F7_LONG_MAX_HOLD_MINUTES):
            should_exit = True
            exit_reason = "MAX_HOLD_CLOSE"
            detail_reason = "MAX_HOLD"

    return {
        "should_exit": should_exit,
        "exit_reason": exit_reason,
        "detail_reason": detail_reason,
        "eff_sl": float(eff_sl),
        "target_tp": float(tp_px),
        "be_px": float(be_px),
        "is_fill_bar": is_fill_bar,
        "bars_held": int(bars_held),
        "held_minutes": float(held_minutes),
    }


# ---------------------------------------------------------------------------
# Pillar 2: h3b_squeeze_1h (SHORT / S8b) エントリー / ターゲット / エグジット
# ---------------------------------------------------------------------------
def compute_h3b_squeeze_1h_entry_levels(
    entry_price: float,
    atr_at_signal: float,
) -> Dict[str, Any]:
    epx = float(entry_price)
    cur_atr = float(atr_at_signal)
    init_sl = epx + (F7_SHORT_INIT_SL_ATR_MULT * cur_atr if cur_atr > 0.0 else epx * 0.04)
    be_px = epx * F7_SHORT_BE_LOCK_PX_MULT
    return {
        "init_sl": float(init_sl),
        "be_px": float(be_px),
        "target_tp": 0.0,
        "entry_atr": float(cur_atr),
        "trough_price": float(epx),
        "live_trough_price": float(epx),
        "last_trough_bucket_open": 0,
        "be_locked": False,
    }


def evaluate_h3b_squeeze_1h_entry(
    df_1h: Optional[pd.DataFrame],
    overall_regime: str,
    is_norm_excluded: bool = False,
    btc_ret14d: Optional[float] = 0.0,
    sym_ret14d: Optional[float] = 0.0,
    wf90d_paused: bool = False,
) -> Dict[str, Any]:
    """`h3b_squeeze_1h` の最新確定 1h 足でのエントリー可否およびシグナル補助 DataFrame を返す."""
    df = _normalize_candle_df(df_1h)
    res: Dict[str, Any] = {
        "strategy": F7_SHORT_STRATEGY,
        "side": "short",
        "timeframe": F7_SHORT_TIMEFRAME,
        "should_enter": False,
        "gate_ok": False,
        "block_reason": "",
        "vol_ratio": 0.0,
        "atr14": 0.0,
        "close": 0.0,
        "is_squeeze": False,
        "don48_low": 0.0,
        "sig_bar_open_ts": 0,
        "sig_bar_close_ts": 0,
        "signal_df": df,
    }
    if len(df) < (F7_SHORT_WARMUP_BARS + 1):
        res["block_reason"] = f"INSUFFICIENT_1H_BARS({len(df)}<{F7_SHORT_WARMUP_BARS + 1})"
        return res

    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)

    atr = calc_atr_series(h, l, c, 14)
    vr = calc_vol_ratio_series(v, 20, 5)
    s_c = pd.Series(c)
    bb_mid = s_c.rolling(F7_SHORT_BB_PERIOD, min_periods=F7_SHORT_BB_PERIOD).mean()
    bb_std = s_c.rolling(F7_SHORT_BB_PERIOD, min_periods=F7_SHORT_BB_PERIOD).std(ddof=0)
    bw = (4.0 * bb_std / bb_mid.replace(0.0, np.nan)).fillna(0.0)
    q25 = bw.rolling(F7_SHORT_SQ_LOOKBACK, min_periods=F7_SHORT_SQ_MIN_PERIODS).quantile(F7_SHORT_SQ_QUANTILE).shift(1)
    sq_arr = (bw.shift(1) <= q25).fillna(False).to_numpy(dtype=bool)
    don48_arr = pd.Series(l).rolling(F7_SHORT_DONCHIAN_PERIOD, min_periods=F7_SHORT_DONCHIAN_PERIOD).min().shift(1).to_numpy(dtype=np.float64)

    end_idx = len(df) - 1
    sig_open_ts = int(df["ts_utc"].iloc[end_idx])
    sig_close_ts = sig_open_ts + F7_SHORT_TF_SEC
    last_close = float(c[end_idx])
    last_vr = float(vr[end_idx])
    last_atr = float(atr[end_idx])
    last_sq = bool(sq_arr[end_idx])
    last_don48 = float(don48_arr[end_idx]) if np.isfinite(don48_arr[end_idx]) else 0.0

    df_sig = df.copy()
    df_sig["atr14"] = atr
    df_sig["vol_ratio"] = vr
    df_sig["POC"] = last_close
    df_sig["VAL"] = last_don48 if last_don48 > 0.0 else last_close
    df_sig["VAH"] = last_close + (2.0 * last_atr if last_atr > 0.0 else 0.0)
    df_sig["long"] = False
    df_sig["short"] = False

    res.update(
        {
            "vol_ratio": last_vr,
            "atr14": last_atr,
            "close": last_close,
            "is_squeeze": last_sq,
            "don48_low": last_don48,
            "sig_bar_open_ts": sig_open_ts,
            "sig_bar_close_ts": sig_close_ts,
            "signal_df": df_sig,
        }
    )

    reg_u = str(overall_regime or "").upper()
    if reg_u != "SELL":
        res["block_reason"] = f"REGIME_NOT_SELL({reg_u})"
        return res
    if is_norm_excluded:
        res["block_reason"] = "NORM_TROUGH_EXCLUDED"
        return res
    noexh_ok, noexh_reason = check_f7_no_exhaustion(btc_ret14d, sym_ret14d)
    if not noexh_ok:
        res["block_reason"] = noexh_reason
        return res
    if wf90d_paused:
        res["block_reason"] = "H3B_WF90D_PAUSED"
        return res
    res["gate_ok"] = True

    if not (last_atr > 0.0 and np.isfinite(last_atr)):
        res["block_reason"] = "INVALID_ATR14"
        return res
    if not last_sq:
        res["block_reason"] = "NO_BB_SQUEEZE"
        return res
    if last_vr < F7_SHORT_VOL_RATIO_MIN:
        res["block_reason"] = f"VOL_RATIO_LOW({last_vr:.2f}<{F7_SHORT_VOL_RATIO_MIN:.2f})"
        return res
    if not (last_don48 > 0.0 and last_close < last_don48):
        res["block_reason"] = f"NO_48H_BREAKDOWN({last_close:.6g}>={last_don48:.6g})"
        return res

    df_sig.iat[end_idx, df_sig.columns.get_loc("short")] = True
    lv = compute_h3b_squeeze_1h_entry_levels(last_close, last_atr)
    res.update(lv)
    res["should_enter"] = True
    return res


def evaluate_h3b_squeeze_1h_exit(
    entry_price: float,
    entry_atr: float,
    init_sl: Optional[float],
    fill_bar_open_ts: int,
    current_price: float,
    overall_regime: str,
    closed_1h_df: Optional[pd.DataFrame] = None,
    trough_price: Optional[float] = None,
    be_locked: bool = False,
    now_utc: Optional[int] = None,
    live_trough_price: Optional[float] = None,
    last_trough_bucket_open: int = 0,
) -> Dict[str, Any]:
    """`h3b_squeeze_1h` (S8b: +1.5 ATR BE ロック + 2.0 ATR トレール) の厳密エグジット判定."""
    epx = float(entry_price)
    px = float(current_price)
    cur_atr = float(entry_atr)
    eff_now = int(now_utc) if now_utc is not None else int(time.time())
    cur_bucket_open = (eff_now // F7_SHORT_TF_SEC) * F7_SHORT_TF_SEC
    fill_open = (int(fill_bar_open_ts) // F7_SHORT_TF_SEC) * F7_SHORT_TF_SEC if int(fill_bar_open_ts) > 0 else cur_bucket_open
    is_fill_bar = cur_bucket_open <= fill_open
    bars_held = max(0, (cur_bucket_open - fill_open) // F7_SHORT_TF_SEC)
    held_minutes = max(0.0, (eff_now - fill_open) / 60.0)

    eff_init_sl = (
        float(init_sl)
        if (init_sl is not None and float(init_sl) > 0.0)
        else (epx + (F7_SHORT_INIT_SL_ATR_MULT * cur_atr if cur_atr > 0.0 else epx * 0.04))
    )
    be_px = epx * F7_SHORT_BE_LOCK_PX_MULT

    # 前バー確定までの最安値 (`trough_price`) のみで SL/BEロックを確定評価し、
    # 約定当足エントリー前の過去安値リークおよびバー内 BE ロックちらつきを遮断する
    bar_open_trough = float(trough_price) if (trough_price is not None and float(trough_price) > 0.0) else epx
    prev_bucket_open = int(last_trough_bucket_open or 0)
    if prev_bucket_open > 0 and cur_bucket_open > prev_bucket_open:
        if live_trough_price is not None and float(live_trough_price) > 0.0:
            bar_open_trough = min(bar_open_trough, float(live_trough_price))
    bar_open_be_locked = bool(be_locked)
    prev_bar_atr = cur_atr

    df1 = _normalize_candle_df(closed_1h_df)
    if not df1.empty:
        h_arr = df1["high"].to_numpy(dtype=np.float64)
        l_arr = df1["low"].to_numpy(dtype=np.float64)
        c_arr = df1["close"].to_numpy(dtype=np.float64)
        ts_arr = df1["ts_utc"].to_numpy(dtype=np.int64)
        atr_arr = calc_atr_series(h_arr, l_arr, c_arr, 14)

        prior_bars = ts_arr < cur_bucket_open
        if np.any(prior_bars):
            last_prior_atr = float(atr_arr[prior_bars][-1])
            if np.isfinite(last_prior_atr) and last_prior_atr > 0.0:
                prev_bar_atr = last_prior_atr

        # fill_open の完全確定後バー (`ts_arr > fill_open`) のみ同期し、fill 当足のエントリー前安値を除外
        completed_held = (ts_arr > fill_open) & (ts_arr < cur_bucket_open)
        if np.any(completed_held):
            min_completed_low = float(np.min(l_arr[completed_held]))
            if min_completed_low > 0.0 and min_completed_low < bar_open_trough:
                bar_open_trough = min_completed_low

    if cur_atr > 0.0 and (epx - bar_open_trough) >= (F7_SHORT_BE_LOCK_ATR_MULT * cur_atr):
        bar_open_be_locked = True

    eff_sl = eff_init_sl
    sl_detail = "INIT_SL"
    if not is_fill_bar:
        if prev_bar_atr > 0.0 and np.isfinite(prev_bar_atr):
            trail_px = bar_open_trough + F7_SHORT_TRAIL_ATR_MULT * prev_bar_atr
            if trail_px < eff_sl:
                eff_sl = trail_px
                sl_detail = "TRAIL_SL"
        if bar_open_be_locked and be_px < eff_sl:
            eff_sl = be_px
            sl_detail = "BE_LOCK"

    should_exit = False
    exit_reason = ""
    detail_reason = ""
    reg_u = str(overall_regime or "").upper()

    if epx > 0.0 and px > 0.0:
        if px >= eff_sl:
            should_exit = True
            exit_reason = "STOP_LOSS" if sl_detail == "INIT_SL" else sl_detail
            detail_reason = sl_detail
        elif not is_fill_bar:
            if reg_u == "BUY" and px <= be_px:
                should_exit = True
                exit_reason = "EMERGENCY_BREAKEVEN_TP"
                detail_reason = "REG_FLIP_BE"

        if not should_exit and (bars_held >= F7_SHORT_MAX_HOLD_BARS or held_minutes >= F7_SHORT_MAX_HOLD_MINUTES):
            should_exit = True
            exit_reason = "MAX_HOLD_CLOSE"
            detail_reason = "MAX_HOLD"

    # 当バー内の約定後最安値は次バー確定時のロールオーバー用に別管理
    if prev_bucket_open == cur_bucket_open and live_trough_price is not None and float(live_trough_price) > 0.0:
        base_live_trough = min(bar_open_trough, float(live_trough_price))
    else:
        base_live_trough = bar_open_trough
    updated_live_trough = min(base_live_trough, px) if px > 0.0 else base_live_trough

    return {
        "should_exit": should_exit,
        "exit_reason": exit_reason,
        "detail_reason": detail_reason,
        "eff_sl": float(eff_sl),
        "init_sl": float(eff_init_sl),
        "be_px": float(be_px),
        "prev_bar_atr": float(prev_bar_atr),
        "trough_price": float(bar_open_trough),
        "live_trough_price": float(updated_live_trough),
        "last_trough_bucket_open": int(cur_bucket_open),
        "be_locked": bool(bar_open_be_locked),
        "is_fill_bar": is_fill_bar,
        "bars_held": int(bars_held),
        "held_minutes": float(held_minutes),
    }


# ---------------------------------------------------------------------------
# 共通 ポートフォリオ・シミュレータ ＆ 因果 90日 WF ヒステリシスゲート
# ---------------------------------------------------------------------------
def build_causal_wf_90d_timeline(
    paper_trades: Sequence[Dict[str, Any]],
) -> Tuple[List[int], List[bool], Dict[str, Any]]:
    """確定済み H3b ペーパー台帳 (`exit_ts` 昇順) のみから 90日因果 ON/OFF タイムラインを構築."""
    sorted_pt = sorted(paper_trades, key=lambda x: (int(x["exit_ts"]), int(x["entry_ts"])))
    change_ts: List[int] = [0]
    states: List[bool] = [False]
    paused = False
    closed_ts: List[int] = []
    closed_pnl: List[float] = []
    latest_n = 0
    latest_net = 0.0
    latest_pf = 0.0

    i = 0
    n_pt = len(sorted_pt)
    while i < n_pt:
        cur_exit = int(sorted_pt[i]["exit_ts"])
        while i < n_pt and int(sorted_pt[i]["exit_ts"]) == cur_exit:
            closed_ts.append(int(sorted_pt[i]["exit_ts"]))
            closed_pnl.append(float(sorted_pt[i]["net_pnl"]))
            i += 1

        cutoff = cur_exit - F7_WF90D_WINDOW_DAYS * 86400
        j0 = bisect_right(closed_ts, cutoff - 1)
        win = np.array(closed_pnl[j0:], dtype=np.float64)
        latest_n = int(len(win))
        if latest_n > 0:
            latest_net = float(win.sum())
            latest_pf = _calc_profit_factor(win)
        if latest_n < F7_WF90D_MIN_TRADES:
            continue

        if (not paused) and (latest_net < F7_WF90D_PAUSE_NET_LT and latest_pf < F7_WF90D_PAUSE_PF_LT):
            paused = True
            change_ts.append(cur_exit)
            states.append(True)
        elif paused and (latest_pf >= F7_WF90D_RESUME_PF_GTE or latest_net >= F7_WF90D_RESUME_NET_GTE):
            paused = False
            change_ts.append(cur_exit)
            states.append(False)

    last_diag = {
        "paused": bool(paused),
        "window_days": F7_WF90D_WINDOW_DAYS,
        "window_trades": latest_n,
        "window_net": round(latest_net, 4),
        "window_pf": round(latest_pf, 4),
        "total_paper_trades": len(sorted_pt),
    }
    return change_ts, states, last_diag


def apply_causal_wf_90d_filter(
    h3b_candidates: Sequence[Dict[str, Any]],
    fee_rate: float = 0.0008,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """H3b ペーパー台帳を因果生成し、WF90d 稼働中の候補のみを残す."""
    if not h3b_candidates:
        return [], {
            "paused": False,
            "window_days": F7_WF90D_WINDOW_DAYS,
            "window_trades": 0,
            "window_net": 0.0,
            "window_pf": 0.0,
            "total_paper_trades": 0,
        }
    s_ts = int(start_ts) if start_ts is not None else min(int(c["sig_ts"]) for c in h3b_candidates)
    e_ts = int(end_ts) if end_ts is not None else max(int(c["exit_ts"]) for c in h3b_candidates)
    paper_res = simulate_f7_portfolio(
        h3b_candidates,
        max_pos=F7_MAX_ACTIVE_POSITIONS,
        fee_rate=fee_rate,
        daily_cb_limit=F7_DAILY_CB_LIMIT_USDT,
        start_ts=s_ts,
        end_ts=e_ts,
        use_norm_excl=True,
    )
    change_ts, states, diag = build_causal_wf_90d_timeline(paper_res["trades"])
    filtered: List[Dict[str, Any]] = []
    for ev in h3b_candidates:
        pos = bisect_right(change_ts, int(ev["sig_ts"])) - 1
        is_paused = states[pos] if pos >= 0 else False
        if not is_paused:
            filtered.append(ev)
    return filtered, diag


def simulate_f7_portfolio(
    candidates: Sequence[Dict[str, Any]],
    max_pos: int = F7_MAX_ACTIVE_POSITIONS,
    fee_rate: float = 0.0008,
    daily_cb_limit: Optional[float] = F7_DAILY_CB_LIMIT_USDT,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    use_norm_excl: bool = True,
) -> Dict[str, Any]:
    """F7 イベント駆動ポートフォリオ・シミュレータ (`vol_ratio DESC, symbol ASC` 競合裁定)."""
    by_ts: Dict[int, List[Dict[str, Any]]] = {}
    for ev in candidates:
        if use_norm_excl and ev.get("norm_excl", False):
            continue
        by_ts.setdefault(int(ev["sig_ts"]), []).append(ev)

    if not by_ts:
        return {
            "n": 0, "wr": 0.0, "net_pnl": 0.0, "ev": 0.0, "pf": 0.0, "max_dd": 0.0, "ret_dd": 0.0,
            "trades": [], "eq_ts": [], "eq_vals": [],
        }

    s_ts = int(start_ts) if start_ts is not None else min(by_ts.keys())
    e_ts = int(end_ts) if end_ts is not None else max(by_ts.keys())
    fee_in = F7_FEE_MAKER if abs(float(fee_rate) - 0.0008) < 1e-9 else F7_FEE_TAKER
    fee_out = F7_FEE_TAKER

    sorted_ts = sorted(by_ts.keys())
    active_slots: Dict[str, Dict[str, Any]] = {}
    executed: List[Dict[str, Any]] = []
    cur_utc_day = -1
    daily_realized = 0.0
    cb_broken = False

    def _release_up_to(t_lim: int) -> None:
        nonlocal cur_utc_day, daily_realized, cb_broken
        due = [s for s, info in active_slots.items() if int(info["exit_ts"]) <= t_lim]
        if not due:
            return
        due.sort(key=lambda s: int(active_slots[s]["exit_ts"]))
        for s in due:
            info = active_slots.pop(s)
            x_ts = int(info["exit_ts"])
            x_day = x_ts // 86400  # 00:00 UTC == 09:00 JST リセット
            if x_day != cur_utc_day:
                cur_utc_day = x_day
                daily_realized = 0.0
                cb_broken = False
            daily_realized += float(info["net_pnl"])
            if daily_cb_limit is not None and daily_realized <= -abs(float(daily_cb_limit)):
                cb_broken = True

    for t in sorted_ts:
        _release_up_to(t)
        day_t = t // 86400
        if day_t != cur_utc_day:
            cur_utc_day = day_t
            daily_realized = 0.0
            cb_broken = False
        if cb_broken:
            continue
        free_slots = int(max_pos) - len(active_slots)
        if free_slots <= 0:
            continue

        best_per_sym: Dict[str, Dict[str, Any]] = {}
        for c in by_ts[t]:
            sym = str(c["symbol"])
            if sym in active_slots:
                continue
            prev = best_per_sym.get(sym)
            if prev is None or float(c["vr"]) > float(prev["vr"]):
                best_per_sym[sym] = c

        ranked = sorted(
            best_per_sym.values(),
            key=lambda x: (-float(x["vr"]), str(x["symbol"])),
        )
        for c in ranked[:free_slots]:
            f_px = float(c["fill_px"])
            x_px = float(c["exit_px"])
            if f_px <= 0.0 or x_px <= 0.0:
                continue
            qty = F7_POSITION_COST_USDT / f_px
            gross = ((x_px - f_px) * qty) if c["side"] == "long" else ((f_px - x_px) * qty)
            net = gross - (F7_POSITION_COST_USDT * fee_in + qty * x_px * fee_out)

            rec = {
                "sig_ts": int(c["sig_ts"]),
                "entry_ts": int(c["fill_ts"]),
                "exit_ts": int(c["exit_ts"]),
                "symbol": str(c["symbol"]),
                "strategy": str(c["strategy"]),
                "side": str(c["side"]),
                "tier": str(c.get("tier", "")),
                "vol_ratio": float(c["vr"]),
                "fill_px": f_px,
                "exit_px": x_px,
                "gross_pnl": float(gross),
                "net_pnl": float(net),
                "exit_reason": str(c["exit_reason"]),
                "entry_time_utc": int(c["fill_ts"]),
                "exit_time_utc": int(c["exit_ts"]),
            }
            executed.append(rec)
            active_slots[str(c["symbol"])] = {
                "sig_ts": int(c["sig_ts"]),
                "entry_ts": int(c["fill_ts"]),
                "exit_ts": int(c["exit_ts"]),
                "net_pnl": float(net),
            }

    _release_up_to(10**12)
    tr_exit = sorted(executed, key=lambda x: (int(x["exit_ts"]), int(x["entry_ts"])))
    if not tr_exit:
        return {
            "n": 0, "wr": 0.0, "net_pnl": 0.0, "ev": 0.0, "pf": 0.0, "max_dd": 0.0, "ret_dd": 0.0,
            "trades": [], "eq_ts": [], "eq_vals": [],
        }

    pnls = np.array([float(x["net_pnl"]) for x in tr_exit], dtype=np.float64)
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
    dd = peak - eq
    max_dd = float(dd.max()) if len(dd) > 0 else 0.0
    net = float(pnls.sum())
    return {
        "n": int(len(pnls)),
        "wr": float((pnls > 0.0).mean() * 100.0),
        "net_pnl": net,
        "ev": float(net / len(pnls)),
        "pf": _calc_profit_factor(pnls),
        "max_dd": max_dd,
        "ret_dd": (net / max_dd) if max_dd > 0.0 else 99.9,
        "trades": tr_exit,
        "eq_ts": [datetime.fromtimestamp(int(x["exit_ts"]), tz=timezone.utc) for x in tr_exit],
        "eq_vals": eq.tolist(),
        "start_utc": s_ts,
        "end_utc": e_ts,
    }


def _resolve_val_poc_30m_candidates_from_arrays(
    sym: str,
    df_30m: pd.DataFrame,
    hourly_regime_map: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if df_30m is None or len(df_30m) < (F7_LONG_WARMUP_BARS + 2):
        return []
    o = df_30m["open"].to_numpy(dtype=np.float64)
    h = df_30m["high"].to_numpy(dtype=np.float64)
    l = df_30m["low"].to_numpy(dtype=np.float64)
    c = df_30m["close"].to_numpy(dtype=np.float64)
    v = df_30m["volume"].to_numpy(dtype=np.float64)
    v_mp = np.ones(len(c), dtype=np.float64) if np.all(v <= 0.0) else v
    ts_o = df_30m["ts_utc"].to_numpy(dtype=np.int64)
    ts_c = ts_o + F7_LONG_TF_SEC
    atr = calc_atr_series(h, l, c, 14)
    vr = calc_vol_ratio_series(v, 20, 5)
    n = len(c)

    out: List[Dict[str, Any]] = []
    norm_sym = normalize_symbol(sym)
    for i in range(F7_LONG_WARMUP_BARS, n - 1):
        if vr[i] < F7_LONG_VOL_RATIO_MIN:
            continue
        sc = int(ts_c[i])
        if is_eu_session_jst(sc):
            continue
        hk = (sc // 3600) * 3600
        reg_info = hourly_regime_map.get(hk)
        if not reg_info or str(reg_info.get("strength_tier", "")).upper() != "ULTRA_BUY":
            continue

        poc, val, vah = _compute_mp_levels(o, h, l, c, v_mp, end_idx=i)
        if not (vah > val > 0.0 and poc > 0.0):
            continue
        if not (val * F7_LONG_VAL_LOW_MULT <= c[i] <= val * F7_LONG_VAL_HIGH_MULT):
            continue

        eb = i + 1
        epx = float(o[eb])
        cur_atr = float(atr[i])
        if epx <= 0.0 or not np.isfinite(epx) or cur_atr <= 0.0 or not np.isfinite(cur_atr):
            continue

        lv = compute_val_poc_30m_entry_levels(epx, cur_atr, val, vah, poc)
        init_sl = lv["init_sl"]
        be_px = lv["be_px"]
        fixed_tp_px = lv["target_tp"]

        end_b = min(n - 1, eb + F7_LONG_MAX_HOLD_BARS)
        x_px = float(c[end_b])
        x_ts = int(ts_c[end_b])
        reason = "MAX_HOLD"
        for k in range(eb, end_b + 1):
            is_fill = (k == eb)
            if l[k] <= init_sl:
                x_px = init_sl
                x_ts = int(ts_c[k])
                reason = "INIT_SL"
                break
            if not is_fill:
                hk_k = (int(ts_c[k]) // 3600) * 3600
                reg_k = hourly_regime_map.get(hk_k)
                if reg_k and str(reg_k.get("overall_regime", "")).upper() == "SELL":
                    if o[k] >= be_px:
                        x_px = float(o[k])
                        x_ts = int(ts_c[k])
                        reason = "REG_FLIP_BE"
                        break
                    if h[k] >= be_px:
                        x_px = float(be_px)
                        x_ts = int(ts_c[k])
                        reason = "REG_FLIP_BE"
                        break
                if h[k] >= fixed_tp_px:
                    x_px = float(fixed_tp_px)
                    x_ts = int(ts_c[k])
                    reason = "TARGET_TP"
                    break
            if (k - eb) >= F7_LONG_MAX_HOLD_BARS:
                x_px = float(c[k])
                x_ts = int(ts_c[k])
                reason = "MAX_HOLD"
                break

        excl_set = reg_info.get("excluded_symbols_set", frozenset())
        out.append(
            {
                "sig_ts": sc,
                "symbol": norm_sym,
                "strategy": F7_LONG_STRATEGY,
                "side": "long",
                "tier": "ULTRA_BUY",
                "norm_excl": norm_sym in excl_set,
                "vr": float(vr[i]),
                "fill_ts": int(ts_o[eb]),
                "exit_ts": x_ts,
                "fill_px": epx,
                "exit_px": x_px,
                "exit_reason": reason,
            }
        )
    return out


def _resolve_h3b_1h_candidates_from_arrays(
    sym: str,
    df_1h: pd.DataFrame,
    btc_1h_df: pd.DataFrame,
    hourly_regime_map: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if df_1h is None or len(df_1h) < (F7_SHORT_WARMUP_BARS + 2):
        return []
    o = df_1h["open"].to_numpy(dtype=np.float64)
    h = df_1h["high"].to_numpy(dtype=np.float64)
    l = df_1h["low"].to_numpy(dtype=np.float64)
    c = df_1h["close"].to_numpy(dtype=np.float64)
    v = df_1h["volume"].to_numpy(dtype=np.float64)
    ts_o = df_1h["ts_utc"].to_numpy(dtype=np.int64)
    ts_c = ts_o + F7_SHORT_TF_SEC
    atr = calc_atr_series(h, l, c, 14)
    vr = calc_vol_ratio_series(v, 20, 5)

    s_c = pd.Series(c)
    bb_mid = s_c.rolling(F7_SHORT_BB_PERIOD, min_periods=F7_SHORT_BB_PERIOD).mean()
    bb_std = s_c.rolling(F7_SHORT_BB_PERIOD, min_periods=F7_SHORT_BB_PERIOD).std(ddof=0)
    bw = (4.0 * bb_std / bb_mid.replace(0.0, np.nan)).fillna(0.0)
    q25 = bw.rolling(F7_SHORT_SQ_LOOKBACK, min_periods=F7_SHORT_SQ_MIN_PERIODS).quantile(F7_SHORT_SQ_QUANTILE).shift(1)
    sq_arr = (bw.shift(1) <= q25).fillna(False).to_numpy(dtype=bool)
    don48_l = pd.Series(l).rolling(F7_SHORT_DONCHIAN_PERIOD, min_periods=F7_SHORT_DONCHIAN_PERIOD).min().shift(1).to_numpy(dtype=np.float64)

    btc_ts_c = (btc_1h_df["ts_utc"].to_numpy(dtype=np.int64) + 3600) if (btc_1h_df is not None and not btc_1h_df.empty) else np.array([], dtype=np.int64)
    btc_c = btc_1h_df["close"].to_numpy(dtype=np.float64) if (btc_1h_df is not None and not btc_1h_df.empty) else np.array([], dtype=np.float64)

    n = len(c)
    norm_sym = normalize_symbol(sym)
    out: List[Dict[str, Any]] = []

    for i in range(F7_SHORT_WARMUP_BARS, n - 1):
        if not (sq_arr[i] and vr[i] >= F7_SHORT_VOL_RATIO_MIN and np.isfinite(don48_l[i]) and c[i] < don48_l[i]):
            continue
        sc = int(ts_c[i])
        hk = (sc // 3600) * 3600
        reg_info = hourly_regime_map.get(hk)
        if not reg_info or str(reg_info.get("overall_regime", "")).upper() != "SELL":
            continue

        # NoExh 因果チェック
        sym_ret14d = compute_ret14d_from_1h_close(c[: i + 1])
        btc_ret14d = 0.0
        if len(btc_ts_c) >= 336:
            bi = bisect_right(btc_ts_c, sc)
            if bi >= 336:
                btc_ret14d = compute_ret14d_from_1h_close(btc_c[:bi])
        noexh_ok, _ = check_f7_no_exhaustion(btc_ret14d, sym_ret14d)
        if not noexh_ok:
            continue

        eb = i + 1
        epx = float(o[eb])
        cur_atr = float(atr[i])
        if epx <= 0.0 or not np.isfinite(epx) or cur_atr <= 0.0 or not np.isfinite(cur_atr):
            continue

        init_sl = epx + F7_SHORT_INIT_SL_ATR_MULT * cur_atr
        be_px = epx * F7_SHORT_BE_LOCK_PX_MULT
        trough_p = epx
        be_locked = False
        end_b = min(n - 1, eb + F7_SHORT_MAX_HOLD_BARS)
        x_px = float(c[end_b])
        x_ts = int(ts_c[end_b])
        reason = "MAX_HOLD"

        for k in range(eb, end_b + 1):
            is_fill = (k == eb)
            eff_sl = init_sl
            sl_mode = "INIT_SL"
            if not is_fill:
                tr_p = trough_p + F7_SHORT_TRAIL_ATR_MULT * float(atr[k - 1])
                if tr_p < eff_sl:
                    eff_sl = tr_p
                    sl_mode = "TRAIL_SL"
                if be_locked and be_px < eff_sl:
                    eff_sl = be_px
                    sl_mode = "BE_LOCK"

            if h[k] >= eff_sl:
                x_px = float(eff_sl)
                x_ts = int(ts_c[k])
                reason = sl_mode
                break

            if not is_fill:
                hk_k = (int(ts_c[k]) // 3600) * 3600
                reg_k = hourly_regime_map.get(hk_k)
                if reg_k and str(reg_k.get("overall_regime", "")).upper() == "BUY":
                    if o[k] <= be_px:
                        x_px = float(o[k])
                        x_ts = int(ts_c[k])
                        reason = "REG_FLIP_BE"
                        break
                    if l[k] <= be_px:
                        x_px = float(be_px)
                        x_ts = int(ts_c[k])
                        reason = "REG_FLIP_BE"
                        break

            if (k - eb) >= F7_SHORT_MAX_HOLD_BARS:
                x_px = float(c[k])
                x_ts = int(ts_c[k])
                reason = "MAX_HOLD"
                break

            if l[k] < trough_p:
                trough_p = float(l[k])
            if (epx - trough_p) >= F7_SHORT_BE_LOCK_ATR_MULT * cur_atr:
                be_locked = True

        excl_set = reg_info.get("excluded_symbols_set", frozenset())
        out.append(
            {
                "sig_ts": sc,
                "symbol": norm_sym,
                "strategy": F7_SHORT_STRATEGY,
                "side": "short",
                "tier": str(reg_info.get("strength_tier", "")),
                "norm_excl": norm_sym in excl_set,
                "vr": float(vr[i]),
                "fill_ts": int(ts_o[eb]),
                "exit_ts": x_ts,
                "fill_px": epx,
                "exit_px": x_px,
                "exit_reason": reason,
            }
        )
    return out


def _collect_f7_candidates_from_lake(
    lake: Any,
    start_utc: int,
    end_utc: int,
    include_long: bool = True,
    include_short: bool = True,
    hourly_regime_map: Optional[Dict[int, Dict[str, Any]]] = None,
    gil_yield_sec: float = 0.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    if hourly_regime_map is None:
        from lake_backtest_engine import compute_hourly_regime_series_from_lake

        hourly_regime_map, _ = compute_hourly_regime_series_from_lake(
            lake,
            start_utc=int(start_utc),
            end_utc=int(end_utc),
            use_point_in_time_regime=True,
            gil_yield_sec=gil_yield_sec,
        )

    bench_sym = normalize_symbol(BENCHMARK_SYMBOL)
    pre_1h_start = int(start_utc) - (25 * 86400)
    pre_30m_start = int(start_utc) - (10 * 86400)
    btc_1h_df = _normalize_candle_df(lake.load_candles(bench_sym, "1h", cutoff_utc=int(end_utc), start_utc=pre_1h_start))

    long_cands: List[Dict[str, Any]] = []
    short_cands: List[Dict[str, Any]] = []

    for sym in TRADABLE_MID_CAP_SYMBOLS:
        norm_sym = normalize_symbol(sym)
        if norm_sym == bench_sym:
            continue
        if include_long:
            df_30m = _normalize_candle_df(lake.load_candles(norm_sym, "30m", cutoff_utc=int(end_utc), start_utc=pre_30m_start))
            c_l = _resolve_val_poc_30m_candidates_from_arrays(norm_sym, df_30m, hourly_regime_map)
            long_cands.extend(c_l)
        if include_short:
            df_1h = _normalize_candle_df(lake.load_candles(norm_sym, "1h", cutoff_utc=int(end_utc), start_utc=pre_1h_start))
            c_s = _resolve_h3b_1h_candidates_from_arrays(norm_sym, df_1h, btc_1h_df, hourly_regime_map)
            short_cands.extend(c_s)
        if gil_yield_sec > 0.0:
            time.sleep(gil_yield_sec)

    return long_cands, short_cands, hourly_regime_map


def evaluate_h3b_wf90d_gate_from_lake(
    lake: Any,
    cutoff_utc: Optional[int] = None,
    lookback_days: int = 180,
    fee_rate: float = 0.0008,
    hourly_regime_map: Optional[Dict[int, Dict[str, Any]]] = None,
    gil_yield_sec: float = 0.0,
) -> Dict[str, Any]:
    """SQLite Lake から因果的 H3b WF90d ゲート (paused + 直近90d成績) を評価して返す."""
    eff_end = int(cutoff_utc) if cutoff_utc is not None else int(time.time())
    eff_start = eff_end - max(int(lookback_days), F7_WF90D_WINDOW_DAYS + 30) * 86400
    _, short_cands, reg_map = _collect_f7_candidates_from_lake(
        lake,
        start_utc=eff_start,
        end_utc=eff_end,
        include_long=False,
        include_short=True,
        hourly_regime_map=hourly_regime_map,
        gil_yield_sec=gil_yield_sec,
    )
    _, diag = apply_causal_wf_90d_filter(
        short_cands,
        fee_rate=fee_rate,
        start_ts=eff_start,
        end_ts=eff_end,
    )
    return {
        "paused": bool(diag["paused"]),
        "window_days": int(diag["window_days"]),
        "window_trades": int(diag["window_trades"]),
        "window_net": float(diag["window_net"]),
        "window_pf": float(diag["window_pf"]),
        "total_paper_trades": int(diag["total_paper_trades"]),
        "evaluated_at_utc": eff_end,
        "_hourly_regime_map": reg_map,
    }


def simulate_f7_portfolio_from_lake(
    lake: Any,
    start_utc: int,
    end_utc: int,
    max_pos: int = F7_MAX_ACTIVE_POSITIONS,
    fee_rate: float = 0.0008,
    daily_cb_limit: float = F7_DAILY_CB_LIMIT_USDT,
    wf_history_days: int = 180,
    hourly_regime_map: Optional[Dict[int, Dict[str, Any]]] = None,
    gil_yield_sec: float = 0.0,
) -> Dict[str, Any]:
    """日次14日比較レポート等向けに、Lake から因果 F7 Two-Pillar ポートフォリオを再現実行する."""
    s_ts = int(start_utc)
    e_ts = int(end_utc)
    wf_start = min(s_ts, e_ts - max(int(wf_history_days), F7_WF90D_WINDOW_DAYS + 30) * 86400)

    long_cands, short_cands, reg_map = _collect_f7_candidates_from_lake(
        lake,
        start_utc=wf_start,
        end_utc=e_ts,
        include_long=True,
        include_short=True,
        hourly_regime_map=hourly_regime_map,
        gil_yield_sec=gil_yield_sec,
    )
    short_filtered, wf_diag = apply_causal_wf_90d_filter(
        short_cands,
        fee_rate=fee_rate,
        start_ts=wf_start,
        end_ts=e_ts,
    )

    win_cands = [
        c for c in (long_cands + short_filtered)
        if s_ts <= int(c["sig_ts"]) <= e_ts
    ]
    sim = simulate_f7_portfolio(
        win_cands,
        max_pos=max_pos,
        fee_rate=fee_rate,
        daily_cb_limit=daily_cb_limit,
        start_ts=s_ts,
        end_ts=e_ts,
        use_norm_excl=True,
    )
    sim["wf90d_diag"] = wf_diag
    sim["pit_active"] = len(reg_map) > 0
    return sim
