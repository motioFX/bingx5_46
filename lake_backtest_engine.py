# -*- coding: utf-8 -*-
"""
bingx5_46 データレイク対応 ウォークフォワード バックテスト基盤 (`lake_backtest_engine.py`)

仕様・検収基準:
(a) Stage 1 (最適化窓 Opt) と Stage 2 (評価窓 OOS Eval) の物理分離・パージ
(b) 未来情報リーク禁止 (`ts_utc + tf_sec <= data_cutoff_utc` ＆ 全指標厳密因果計算)
(c) 往復 0.08% 手数料 (Maker 0.02% + Taker 0.06%) の全擬似約定算入・個別記録
(d) 評価窓成績に基づく勝者1件選出 (n<10 不可, 10<=n<30 参考, n>=30 本採用, EV>0 必須)
(e) Maker 最良気配指値追随 ＆ シグナル終了キャンセル (`CANCEL_SIGNAL_END`) 再現
(f) 伸び切り度合い (`%` ＆ `ATR_14`) の全シグナル付与とバケット別成績集計
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from bingx5_46_3logic import (
    calc_rsi_ema,
    compute_dynamic_exit_levels,
    compute_mp_levels_at_bar,
    compute_normalized_exclusions_from_close_arrays,
    evaluate_dynamic_exit_decision,
    evaluate_market_regime_3window,
    evaluate_market_regime_3window_from_close_arrays,
    make_market_profile_dual_va,
)
from lake_store import (
    TIMEFRAME_SECONDS,
    LakeStore,
    get_git_sha_short,
    sec_to_iso,
    utc_now_sec,
)
from strategy_f7_two_pillar import compute_norm_2d_vol_metrics_from_close_arrays
from symbol_universe import (
    BENCHMARK_SYMBOL,
    EXCLUDED_TRADE_MEGA_CAPS,
    FIXED_TRADABLE_SYMBOLS,
    LAKE_UNIVERSE_SYMBOLS,
    TRADABLE_MID_CAP_SYMBOLS,
    normalize_symbol,
)

JST = timezone(timedelta(hours=9))

LONG_STRATEGIES = ("val_poc", "vah_break", "rsima")
SHORT_STRATEGIES = ("vah_poc", "val_break", "rsima_short")
ALL_STRATEGIES = LONG_STRATEGIES + SHORT_STRATEGIES
_PIT_MIN_SYMBOLS = 15


@dataclass(frozen=True)
class WalkForwardConfig:
    opt_days: int = 90
    eval_days: int = 90
    embargo_bars: int = 0
    timeframes: Tuple[str, ...] = ("5m", "15m")
    fee_maker: float = 0.0002
    fee_taker: float = 0.0006
    position_cost_usdt: float = 15.0
    sl_cooldown_hours: float = 0.0
    hard_sl_pct: float = 0.030
    min_sl_pct: float = 0.0020
    timeout_minutes: int = 240
    max_hold_minutes: int = 480
    enforce_vol_surge: bool = True
    vol_min_ratio: float = 1.5
    enforce_oi_fr: bool = True
    oi_missing_policy: str = "when_available"  # "when_available" | "strict_zero_fill"
    enforce_session_mask: bool = True
    allow_ny_session: bool = True
    enable_emergency_breakeven: bool = True
    mp_step: int = 1
    max_active_positions: int = 2
    daily_max_loss_usdt: float = 1.50
    use_point_in_time_regime: bool = True
    min_eval_trades_provisional: int = 10
    min_eval_trades_qualified: int = 30
    gil_yield_sec: float = 0.005

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timeframes"] = list(self.timeframes)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WalkForwardConfig":
        valid_fields = cls.__dataclass_fields__.keys()
        clean = {k: v for k, v in data.items() if k in valid_fields}
        if "timeframes" in clean and isinstance(clean["timeframes"], list):
            clean["timeframes"] = tuple(str(x) for x in clean["timeframes"])
        return cls(**clean)


def strategy_side(strategy: str) -> str:
    return "short" if strategy in SHORT_STRATEGIES else "long"


def is_breakout_strategy(strategy: str) -> bool:
    return strategy in ("vah_break", "val_break")


def get_param_grid(strategy: str) -> List[Dict[str, Any]]:
    """戦略ごとの決定論的パラメータ探索グリッド。"""
    if strategy in ("val_poc", "vah_poc"):
        return [
            {"mp": mp, "target_ratio": tr}
            for mp in (70, 100, 150)
            for tr in (0.55, 0.70, 0.85)
        ]
    if strategy in ("vah_break", "val_break"):
        return [
            {"mp": mp, "target_ratio": tr}
            for mp in (70, 100, 150)
            for tr in (0.50, 0.70, 0.90)
        ]
    if strategy == "rsima":
        return [
            {"l_ep": ep, "l_cp": cp}
            for ep, cp in ((35.0, 60.0), (40.0, 60.0), (40.0, 65.0), (45.0, 65.0), (45.0, 70.0))
        ]
    if strategy == "rsima_short":
        return [
            {"s_ep": ep, "s_cp": cp}
            for ep, cp in ((65.0, 40.0), (60.0, 40.0), (60.0, 35.0), (55.0, 35.0), (55.0, 30.0))
        ]
    return []


def compute_regime_from_lake(
    lake: LakeStore,
    data_cutoff_utc: int,
    override_regime_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    `data_cutoff_utc` 以前のデータのみを使って地合い判定を行う。
    `override_regime_info` (ライブ時のクジラ監査込み判定等) が与えられた場合はそれを優先し、
    再計算や過去再現時はレイクの 1h 確定足のみから完全に因果的に算出する。
    """
    if override_regime_info is not None:
        return override_regime_info

    start_1h = int(data_cutoff_utc) - (20 * 86400)
    symbol_1h_dfs: Dict[str, pd.DataFrame] = {}
    for sym in LAKE_UNIVERSE_SYMBOLS:
        df_1h = lake.load_candles(sym, "1h", cutoff_utc=data_cutoff_utc, start_utc=start_1h)
        if not df_1h.empty:
            symbol_1h_dfs[sym] = df_1h

    if not symbol_1h_dfs:
        return {
            "overall_regime": "NEUTRAL",
            "regime_pattern": "(N,N,N)",
            "allowed_strategies": [],
            "regime_strength": {
                "tier": "BALANCED",
                "net_score": 0.0,
                "mean_buy_votes": 0.0,
                "mean_sell_votes": 0.0,
            },
            "recommended_symbols": [],
            "excluded_symbols": {},
            "norm_amp_2d": None,
            "norm_disp_2d": None,
            "whale_snapshot": {"source": "lake_empty"},
        }

    res = evaluate_market_regime_3window(symbol_1h_dfs)
    arr_map = {s: d["close"].to_numpy(dtype=np.float64) for s, d in symbol_1h_dfs.items()}
    res["excluded_symbols"] = compute_normalized_exclusions_from_close_arrays(arr_map, res.get("overall_regime", "NEUTRAL"))
    vol2d = compute_norm_2d_vol_metrics_from_close_arrays(arr_map)
    res["norm_amp_2d"] = vol2d.get("norm_amp_2d")
    res["norm_disp_2d"] = vol2d.get("norm_disp_2d")
    res["whale_snapshot"] = {"source": "lake_offline_1h"}
    return res


def _compact_period_results(period_results: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    compact: Dict[str, Dict[str, Any]] = {}
    if not period_results:
        return compact
    for w_name in ("14d", "7d", "2d"):
        w = period_results.get(w_name)
        if not isinstance(w, dict):
            continue
        compact[w_name] = {
            "state": str(w.get("state", "NEUTRAL")),
            "buy_votes": int(w.get("buy_votes", 0)),
            "sell_votes": int(w.get("sell_votes", 0)),
            "total_votes": int(w.get("total_votes", 0)),
        }
    return compact


def _snapshot_from_regime_dict(
    ts_hr: int,
    reg: Dict[str, Any],
    source_label: str,
    fallback_all_strategies: bool = False,
) -> Dict[str, Any]:
    strength_obj = reg.get("regime_strength", {}) or {}
    allowed_raw = [str(s) for s in reg.get("allowed_strategies", []) if str(s) in ALL_STRATEGIES]
    if not allowed_raw and fallback_all_strategies:
        allowed_raw = list(ALL_STRATEGIES)
    raw_excl = reg.get("excluded_symbols", {})
    excl_dict = raw_excl if isinstance(raw_excl, dict) else {str(s): "EXCLUDED" for s in raw_excl}
    return {
        "cutoff_utc": int(ts_hr),
        "cutoff_iso": sec_to_iso(ts_hr),
        "overall_regime": str(reg.get("overall_regime", "NEUTRAL")).upper(),
        "regime_pattern": str(reg.get("regime_pattern", "(N,N,N)")),
        "strength_tier": str(strength_obj.get("tier", "BALANCED")),
        "net_score": float(strength_obj.get("net_score", 0.0) or 0.0),
        "mean_buy_votes": float(strength_obj.get("mean_buy_votes", 0.0) or 0.0),
        "mean_sell_votes": float(strength_obj.get("mean_sell_votes", 0.0) or 0.0),
        "allowed_strategies": allowed_raw,
        "allowed_strategies_set": frozenset(allowed_raw),
        "recommended_symbols": list(reg.get("recommended_symbols", [])),
        "excluded_symbols": excl_dict,
        "excluded_symbols_set": frozenset(normalize_symbol(str(s)) for s in excl_dict.keys()),
        "norm_amp_2d": reg.get("norm_amp_2d"),
        "norm_disp_2d": reg.get("norm_disp_2d"),
        "whale_snapshot": {
            "source": source_label,
            "period_results": _compact_period_results(reg.get("period_results")),
        },
    }


def compute_hourly_regime_series_from_lake(
    lake: LakeStore,
    start_utc: int,
    end_utc: int,
    override_regime_info: Optional[Dict[str, Any]] = None,
    use_point_in_time_regime: bool = True,
    gil_yield_sec: float = 0.0,
) -> Tuple[Dict[int, Dict[str, Any]], bool]:
    """
    `[start_utc, end_utc]` 内の全正時 (`3600s` 刻み) における時点地合い系列を構築する。
    返却: `(hourly_regime_map, pit_active)`
      - 本番相当 (`use_point_in_time_regime=True` かつ 1h足保有銘柄 >= 15):
        `override_regime_info` が渡されていても、各正時 `t_hr` について `ts_utc + 3600 <= t_hr`
        を満たす確定 `1h` 足のみから SSoT `evaluate_market_regime_3window` を因果再計算する (`pit_active=True`)。
      - 小規模合成DB (< 15銘柄) または `use_point_in_time_regime=False`:
        互換性のため `override_regime_info`（未指定時は合成フォールバック）を全時間へ適用する (`pit_active=False`)。
    """
    start_hr = (int(start_utc) // 3600) * 3600
    end_hr = (int(end_utc) // 3600) * 3600
    if end_hr < start_hr:
        return {}, False

    pre_start = start_hr - (20 * 86400)
    preloaded_1h: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for sym in LAKE_UNIVERSE_SYMBOLS:
        df_1h = lake.load_candles(sym, "1h", cutoff_utc=end_utc, start_utc=pre_start)
        if not df_1h.empty and len(df_1h) >= 24:
            ts_close_arr = df_1h["ts_utc"].to_numpy(dtype=np.int64) + 3600
            close_arr = df_1h["close"].to_numpy(dtype=np.float64)
            preloaded_1h[sym] = (ts_close_arr, close_arr)

    pit_active = bool(use_point_in_time_regime and len(preloaded_1h) >= _PIT_MIN_SYMBOLS)
    hourly_map: Dict[int, Dict[str, Any]] = {}

    if not pit_active:
        base_reg = override_regime_info or {
            "overall_regime": "NEUTRAL",
            "regime_pattern": "(N,N,N)",
            "regime_strength": {"tier": "BALANCED", "net_score": 0.0, "mean_buy_votes": 0.0, "mean_sell_votes": 0.0},
            "allowed_strategies": list(ALL_STRATEGIES),
            "recommended_symbols": [],
            "excluded_symbols": {},
        }
        src = "override_static" if override_regime_info is not None else "synthetic_fallback"
        fallback_all = override_regime_info is None
        for t_hr in range(start_hr, end_hr + 1, 3600):
            hourly_map[t_hr] = _snapshot_from_regime_dict(
                t_hr, base_reg, source_label=src, fallback_all_strategies=fallback_all
            )
        return hourly_map, False

    max_window_bars = 14 * 24  # 336h (evaluate_market_regime_3window 最大参照長)
    t_hrs = np.arange(start_hr, end_hr + 1, 3600, dtype=np.int64)
    idx_by_sym: Dict[str, Tuple[np.ndarray, np.ndarray]] = {
        sym: (np.searchsorted(ts_close_arr, t_hrs, side="right"), close_arr)
        for sym, (ts_close_arr, close_arr) in preloaded_1h.items()
    }
    for step_idx, t_hr_np in enumerate(t_hrs):
        t_hr = int(t_hr_np)
        hourly_arrays_at_t: Dict[str, np.ndarray] = {}
        for sym, (idx_arr, close_arr) in idx_by_sym.items():
            idx = int(idx_arr[step_idx])
            if idx >= 24:
                lo = max(0, idx - max_window_bars)
                hourly_arrays_at_t[sym] = close_arr[lo:idx]

        if len(hourly_arrays_at_t) < _PIT_MIN_SYMBOLS:
            empty_reg = {
                "overall_regime": "NEUTRAL",
                "regime_pattern": "(N,N,N)",
                "regime_strength": {"tier": "BALANCED", "net_score": 0.0, "mean_buy_votes": 0.0, "mean_sell_votes": 0.0},
                "allowed_strategies": [],
                "recommended_symbols": [],
            }
            hourly_map[t_hr] = _snapshot_from_regime_dict(t_hr, empty_reg, source_label="lake_hourly_warmup")
        else:
            reg_t = evaluate_market_regime_3window_from_close_arrays(hourly_arrays_at_t)
            reg_t["excluded_symbols"] = compute_normalized_exclusions_from_close_arrays(
                hourly_arrays_at_t, reg_t.get("overall_regime", "NEUTRAL")
            )
            vol2d_t = compute_norm_2d_vol_metrics_from_close_arrays(hourly_arrays_at_t)
            reg_t["norm_amp_2d"] = vol2d_t.get("norm_amp_2d")
            reg_t["norm_disp_2d"] = vol2d_t.get("norm_disp_2d")
            hourly_map[t_hr] = _snapshot_from_regime_dict(t_hr, reg_t, source_label="lake_hourly_1h")

        if gil_yield_sec > 0 and ((step_idx + 1) % 200) == 0:
            time.sleep(gil_yield_sec)

    return hourly_map, True


class _LazyMPSeries:
    """要求された確定足インデックスのみ厳密 `step=1` で Market Profile を算出しメモ化する遅延キャッシュ。"""

    def __init__(
        self,
        o: np.ndarray,
        h: np.ndarray,
        l: np.ndarray,
        c: np.ndarray,
        v: np.ndarray,
        period: int,
        num_bins: int = 50,
        value_area_pct: float = 0.70,
        use_decay: bool = True,
    ):
        n = len(c)
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v
        self.period = max(10, int(period))
        self.num_bins = int(num_bins)
        self.value_area_pct = float(value_area_pct)
        half_life = max(5.0, self.period / 2.0)
        decay_factor = 0.5 ** (1.0 / half_life) if use_decay else 1.0
        self.full_weights = (decay_factor ** np.arange(self.period - 1, -1, -1)) if use_decay else np.ones(self.period)
        self.val = np.full(n, np.nan, dtype=np.float64)
        self.vah = np.full(n, np.nan, dtype=np.float64)
        self.poc = np.full(n, np.nan, dtype=np.float64)
        self.done = np.zeros(n, dtype=bool)

    def ensure_indices(self, indices: Sequence[int]) -> None:
        n = len(self.done)
        for i in indices:
            idx = int(i)
            if 0 <= idx < n and not self.done[idx]:
                p, vl, vh, _, _ = compute_mp_levels_at_bar(
                    self.o, self.h, self.l, self.c, self.v,
                    end_idx=idx, period=self.period, num_bins=self.num_bins,
                    value_area_pct=self.value_area_pct, full_weights=self.full_weights, compute_80=False,
                )
                self.poc[idx] = p
                self.val[idx] = vl
                self.vah[idx] = vh
                self.done[idx] = True

    def get_bar(self, idx: int) -> Tuple[float, float, float]:
        i = int(idx)
        if i < 0 or i >= len(self.done):
            return 0.0, 0.0, 0.0
        if not self.done[i]:
            p, vl, vh, _, _ = compute_mp_levels_at_bar(
                self.o, self.h, self.l, self.c, self.v,
                end_idx=i, period=self.period, num_bins=self.num_bins,
                value_area_pct=self.value_area_pct, full_weights=self.full_weights, compute_80=False,
            )
            self.poc[i] = p
            self.val[i] = vl
            self.vah[i] = vh
            self.done[i] = True
        return float(self.val[i]), float(self.vah[i]), float(self.poc[i])


class _StaticMPSeries:
    """旧互換 `mp_step > 1` 用ラッパー。"""

    def __init__(self, val: np.ndarray, vah: np.ndarray, poc: np.ndarray):
        self.val = val
        self.vah = vah
        self.poc = poc

    def ensure_indices(self, indices: Sequence[int]) -> None:
        return

    def get_bar(self, idx: int) -> Tuple[float, float, float]:
        i = int(idx)
        if i < 0 or i >= len(self.val):
            return 0.0, 0.0, 0.0
        return float(self.val[i]), float(self.vah[i]), float(self.poc[i])


def _compute_causal_features(
    df_tf: pd.DataFrame,
    timeframe: str,
    cfg: WalkForwardConfig,
    needed_mps: Sequence[int] = (70, 100, 150),
) -> Dict[str, Any]:
    """
    指定足種 DataFrame (すでに `ts_utc + tf_sec <= end_cutoff` 済) に対して、
    完全に因果的な配列（遅延/間引きMP, RSI-EMA, ATR_14, range_high/low, shift(1)直前10本高安, precision, session）を事前計算する。
    """
    n = len(df_tf)
    tf_sec = TIMEFRAME_SECONDS[timeframe]
    ts_open = df_tf["ts_utc"].to_numpy(dtype=np.int64)
    ts_close = ts_open + tf_sec
    o = df_tf["open"].to_numpy(dtype=np.float64)
    h = df_tf["high"].to_numpy(dtype=np.float64)
    l = df_tf["low"].to_numpy(dtype=np.float64)
    c = df_tf["close"].to_numpy(dtype=np.float64)
    v = df_tf["volume"].to_numpy(dtype=np.float64)
    v_mp = np.ones(n, dtype=np.float64) if np.all(v <= 0) else v

    # ATR(14) - strictly causal
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_14 = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy(dtype=np.float64)

    s_h = pd.Series(h)
    s_l = pd.Series(l)
    range_high_24 = s_h.rolling(24, min_periods=1).max().to_numpy(dtype=np.float64)
    range_low_24 = s_l.rolling(24, min_periods=1).min().to_numpy(dtype=np.float64)

    # 当該確定足自身を含めない直前10本スイング高安 (`shift(1)` = 実運用の `iloc[-11:-1]` と完全一致)
    if n > 0:
        swing_high_10 = s_h.rolling(10, min_periods=1).max().shift(1).fillna(float(h[0])).to_numpy(dtype=np.float64)
        swing_low_10 = s_l.rolling(10, min_periods=1).min().shift(1).fillna(float(l[0])).to_numpy(dtype=np.float64)
    else:
        swing_high_10 = np.empty(0, dtype=np.float64)
        swing_low_10 = np.empty(0, dtype=np.float64)

    # Volume Ratio (直前20本移動平均は shift(1) で当足除外＝完全因果)
    v_ma20 = pd.Series(v).rolling(20, min_periods=5).mean().shift(1).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where((v_ma20 > 0) & np.isfinite(v_ma20), v / v_ma20, 1.0)

    # OI & FR (既に causal merge_asof 済)
    oi_chg = df_tf["oi_chg_4h"].to_numpy(dtype=np.float64) if "oi_chg_4h" in df_tf.columns else np.full(n, np.nan)
    fr = df_tf["funding_rate"].to_numpy(dtype=np.float64) if "funding_rate" in df_tf.columns else np.full(n, np.nan)

    # Session mask (JST: 足確定時刻 `ts_close` 基準 = 実運用の `datetime.now(JST).hour` と完全一致)
    dt_jst = pd.to_datetime(ts_close, unit="s", utc=True).tz_convert(JST)
    hrs_jst = dt_jst.hour.to_numpy(dtype=np.int32)
    tokyo_mask = (hrs_jst >= 9) & (hrs_jst < 16)
    london_mask = (hrs_jst >= 16) & (hrs_jst < 22)
    ny_mask = (hrs_jst >= 22) | (hrs_jst < 9)

    # RSI(9) & RSI-EMA(7)
    rsi_s, rsi_ma_s = calc_rsi_ema(pd.Series(c), rsi_period=9, ma_period=7)
    rsi = rsi_s.to_numpy(dtype=np.float64)
    rsi_ma = rsi_ma_s.to_numpy(dtype=np.float64)

    # Market Profile Cache (`mp_step == 1` は要求足のみ遅延厳密計算で実運用と100%一致)
    mp_cache: Dict[int, Any] = {}
    eff_step = max(1, int(getattr(cfg, "mp_step", 1)))
    for mp_p in sorted(set(int(x) for x in needed_mps)):
        if eff_step <= 1:
            mp_cache[mp_p] = _LazyMPSeries(o, h, l, c, v_mp, period=mp_p, num_bins=50, value_area_pct=0.70, use_decay=True)
        else:
            step_val = eff_step if timeframe == "5m" else max(1, eff_step // 3)
            mp_res = make_market_profile_dual_va(df_tf, period=mp_p, num_bins=50, step=step_val, use_decay=True)
            mp_cache[mp_p] = _StaticMPSeries(
                np.asarray(mp_res["val_70"], dtype=np.float64),
                np.asarray(mp_res["vah_70"], dtype=np.float64),
                np.asarray(mp_res["poc"], dtype=np.float64),
            )

    # 精度フィルター事前マスク (戦略種別×方向別)
    def _precision_mask(side: str, is_brk: bool) -> np.ndarray:
        m = np.ones(n, dtype=bool)
        if cfg.enforce_vol_surge:
            if is_brk:
                m &= (vol_ratio >= cfg.vol_min_ratio) & (vol_ratio <= 4.0)
            else:
                m &= (vol_ratio >= cfg.vol_min_ratio) & (vol_ratio <= 2.5)
        if cfg.enforce_oi_fr:
            if cfg.oi_missing_policy == "strict_zero_fill":
                oi_use = np.where(np.isfinite(oi_chg), oi_chg, 0.0)
                fr_use = np.where(np.isfinite(fr), fr, 0.0)
                oi_ok = np.ones(n, dtype=bool)
                fr_ok = np.ones(n, dtype=bool)
            else:
                oi_use = oi_chg
                fr_use = fr
                oi_ok = np.isfinite(oi_use)
                fr_ok = np.isfinite(fr_use)

            if is_brk:
                cond_oi = (oi_use >= 0.8) & (oi_use <= 5.0)
                m &= np.where(oi_ok, cond_oi, True if cfg.oi_missing_policy != "strict_zero_fill" else False)
            else:
                cond_oi = (oi_use >= -3.0) & (oi_use <= (3.0 if side == "short" else 5.0))
                m &= np.where(oi_ok, cond_oi, True)

            if side == "short":
                cond_fr = fr_use >= -0.00005
            else:
                cond_fr = (fr_use >= -0.00005) & (fr_use <= 0.00025)
            m &= np.where(fr_ok, cond_fr, True)
        return m

    precision_masks = {
        ("long", False): _precision_mask("long", False),
        ("long", True): _precision_mask("long", True),
        ("short", False): _precision_mask("short", False),
        ("short", True): _precision_mask("short", True),
    }

    return {
        "n": n,
        "timeframe": timeframe,
        "tf_sec": tf_sec,
        "ts_open": ts_open,
        "ts_close": ts_close,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "atr_14": atr_14,
        "range_high_24": range_high_24,
        "range_low_24": range_low_24,
        "swing_high_10": swing_high_10,
        "swing_low_10": swing_low_10,
        "tokyo_mask": tokyo_mask,
        "london_mask": london_mask,
        "ny_mask": ny_mask,
        "rsi": rsi,
        "rsi_ma": rsi_ma,
        "mp_cache": mp_cache,
        "precision_masks": precision_masks,
    }


def _generate_signal_array(
    feat: Dict[str, Any],
    strategy: str,
    params: Dict[str, Any],
    cfg: WalkForwardConfig,
) -> Dict[str, Any]:
    """事前計算済み因果特徴量から、シグナル発火配列と MP インスタンス・伸び切り基準情報を生成する。"""
    n = feat["n"]
    tf = feat["timeframe"]
    c = feat["close"]
    side = strategy_side(strategy)
    is_brk = is_breakout_strategy(strategy)

    sig = np.zeros(n, dtype=bool)
    p_mask = feat["precision_masks"][(side, is_brk)]

    if cfg.enforce_session_mask and tf in ("5m", "15m"):
        if getattr(cfg, "allow_ny_session", True):
            sess_mask = (feat["london_mask"] | feat["ny_mask"]) if is_brk else (feat["tokyo_mask"] | feat["ny_mask"])
        else:
            sess_mask = feat["london_mask"] if is_brk else feat["tokyo_mask"]
    else:
        sess_mask = np.ones(n, dtype=bool)

    base_mask = p_mask & sess_mask
    warmup_bars = int(params.get("mp", 30)) if "mp" in params else 30
    mp_obj = None

    if strategy in ("val_poc", "vah_poc", "vah_break", "val_break"):
        mp_p = int(params["mp"])
        mp_obj = feat["mp_cache"][mp_p]
        cand_idxs = np.flatnonzero(base_mask)
        cand_idxs = cand_idxs[cand_idxs >= warmup_bars]
        if len(cand_idxs) > 0:
            if is_brk:
                need_idxs = np.unique(np.concatenate([cand_idxs, cand_idxs - 1]))
                mp_obj.ensure_indices(need_idxs)
            else:
                mp_obj.ensure_indices(cand_idxs)

        val = mp_obj.val
        vah = mp_obj.vah
        poc = mp_obj.poc
        valid_mp = (val > 0) & (vah > val) & (poc > 0)

        if strategy == "val_poc":
            cond = valid_mp & (c <= val * 1.001) & (c >= val * 0.970)
        elif strategy == "vah_poc":
            cond = valid_mp & (c >= vah * 0.999) & (c <= vah * 1.030)
        elif strategy == "vah_break":
            prev_c = np.roll(c, 1)
            prev_vah = np.roll(vah, 1)
            prev_c[0] = c[0]
            cond = valid_mp & (prev_c <= prev_vah) & (c > vah) & (c < vah * 1.04)
        else:  # val_break
            prev_c = np.roll(c, 1)
            prev_val = np.roll(val, 1)
            prev_c[0] = c[0]
            cond = valid_mp & (prev_c >= prev_val) & (c < val) & (c > val * 0.96)

        sig = cond & base_mask
        va_upper = vah
        va_lower = val
        poc_arr = poc
    else:
        rsi = feat["rsi"]
        rsi_ma = feat["rsi_ma"]
        prev_rsi = np.roll(rsi, 1)
        prev_ma = np.roll(rsi_ma, 1)
        if strategy == "rsima":
            ep = float(params["l_ep"])
            cond = (rsi > rsi_ma) & (prev_rsi <= prev_ma) & (rsi_ma < ep)
        else:
            ep = float(params["s_ep"])
            cond = (rsi < rsi_ma) & (prev_rsi >= prev_ma) & (rsi_ma > ep)
        sig = cond & base_mask
        if n > warmup_bars:
            sig[:warmup_bars] = False
        else:
            sig[:] = False

        mp_ref = feat["mp_cache"].get(150)
        if mp_ref is None and feat["mp_cache"]:
            mp_ref = next(iter(feat["mp_cache"].values()))
        if mp_ref is not None:
            sig_idxs = np.flatnonzero(sig)
            if len(sig_idxs) > 0:
                mp_ref.ensure_indices(sig_idxs)
            va_upper = mp_ref.vah
            va_lower = mp_ref.val
            poc_arr = mp_ref.poc
        else:
            va_upper = feat["range_high_24"]
            va_lower = feat["range_low_24"]
            poc_arr = (va_upper + va_lower) * 0.5

    if n > warmup_bars:
        sig[:warmup_bars] = False
    else:
        sig[:] = False

    return {
        "sig": sig,
        "va_upper": va_upper,
        "va_lower": va_lower,
        "poc": poc_arr,
        "mp_obj": mp_obj,
        "sess_mask": sess_mask,
    }


def _compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    filled = [t for t in trades if t.get("fill_ts_utc") is not None]
    n = len(filled)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "total_fees": 0.0,
            "net_pnl": 0.0,
            "ev": 0.0,
            "pf": 0.0,
            "max_dd": 0.0,
            "ext_stats": {},
        }

    net_arr = np.array([float(t["net_pnl"]) for t in filled], dtype=np.float64)
    gross_arr = np.array([float(t["gross_pnl"]) for t in filled], dtype=np.float64)
    fee_arr = np.array([float(t["entry_fee_usdt"]) + float(t["exit_fee_usdt"]) for t in filled], dtype=np.float64)

    wins = int(np.sum(net_arr > 0.0))
    win_rate = (wins / n) * 100.0
    gross_pnl = float(np.sum(gross_arr))
    total_fees = float(np.sum(fee_arr))
    net_pnl = float(np.sum(net_arr))
    ev = net_pnl / n

    sum_pos = float(np.sum(net_arr[net_arr > 0.0]))
    sum_neg = float(np.abs(np.sum(net_arr[net_arr < 0.0])))
    if sum_neg > 0:
        pf = sum_pos / sum_neg
    elif sum_pos > 0:
        pf = 99.99
    else:
        pf = 0.0

    cum = np.cumsum(net_arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    dd = peak - cum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    # 伸び切り度合いバケット集計
    buckets_atr = {"lt_0.5_atr": [], "0.5_to_1.5_atr": [], "ge_1.5_atr": []}
    for t in filled:
        ea = t.get("ext_atr")
        if ea is None or not math.isfinite(float(ea)):
            continue
        ea_f = abs(float(ea))
        pnl_f = float(t["net_pnl"])
        if ea_f < 0.5:
            buckets_atr["lt_0.5_atr"].append(pnl_f)
        elif ea_f < 1.5:
            buckets_atr["0.5_to_1.5_atr"].append(pnl_f)
        else:
            buckets_atr["ge_1.5_atr"].append(pnl_f)

    ext_summary: Dict[str, Any] = {
        "cancelled_signals": int(sum(1 for t in trades if t.get("cancel_reason") is not None)),
        "avg_ext_pct": float(np.nanmean([t["ext_pct"] for t in filled if t.get("ext_pct") is not None])) if filled else 0.0,
        "avg_ext_atr": float(np.nanmean([t["ext_atr"] for t in filled if t.get("ext_atr") is not None])) if filled else 0.0,
        "buckets_atr": {},
    }
    for b_name, vals in buckets_atr.items():
        if vals:
            b_wins = sum(1 for x in vals if x > 0)
            ext_summary["buckets_atr"][b_name] = {
                "n": len(vals),
                "win_rate": round(b_wins / len(vals) * 100.0, 2),
                "net_pnl": round(sum(vals), 4),
                "ev": round(sum(vals) / len(vals), 5),
            }
        else:
            ext_summary["buckets_atr"][b_name] = {"n": 0, "win_rate": 0.0, "net_pnl": 0.0, "ev": 0.0}

    return {
        "trades": n,
        "wins": wins,
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "total_fees": total_fees,
        "net_pnl": net_pnl,
        "ev": ev,
        "pf": pf,
        "max_dd": max_dd,
        "ext_stats": ext_summary,
    }


def _session_allows_strategy(ts_utc: int, is_brk: bool, cfg: WalkForwardConfig) -> bool:
    if not cfg.enforce_session_mask:
        return True
    hr_jst = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).astimezone(JST).hour
    if 9 <= hr_jst < 16:
        return not is_brk
    if 16 <= hr_jst < 22:
        return is_brk
    return bool(getattr(cfg, "allow_ny_session", True))


def _simulate_window(
    feat_tf: Dict[str, Any],
    sig_info: Dict[str, Any],
    df_5m: pd.DataFrame,
    symbol: str,
    strategy: str,
    params: Dict[str, Any],
    window_type: str,
    win_start_utc: int,
    win_end_utc: int,
    cfg: WalkForwardConfig,
    hourly_regime_map: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    指定窓 `[win_start_utc, win_end_utc]` 上で実ボットと 1:1 同一シーケンスを実行する：
    - 5m 足ベース非ブロッキング Maker 指値追随・シグナル終了/セッション転換キャンセル
    - 保有中は確定足ごとに Market Profile (`VAH/VAL/POC`)・直前10本高安 (`shift(1)`) を動的再計算
    - 地合い転換時緊急建値撤退 (`±0.20%`)・240m タイムアウト撤退・480m `MAX_HOLD_CLOSE`・往復 0.08% 手数料控除
    """
    if df_5m.empty or feat_tf["n"] == 0:
        return []

    norm_sym = normalize_symbol(symbol)
    side = strategy_side(strategy)
    is_brk = is_breakout_strategy(strategy)
    is_vp_strat = strategy in ("val_poc", "vah_poc", "vah_break", "val_break")
    tf = feat_tf["timeframe"]

    ts5 = df_5m["ts_utc"].to_numpy(dtype=np.int64)
    h5 = df_5m["high"].to_numpy(dtype=np.float64)
    l5 = df_5m["low"].to_numpy(dtype=np.float64)
    c5 = df_5m["close"].to_numpy(dtype=np.float64)

    valid_5m_end = int(np.searchsorted(ts5 + 300, win_end_utc, side="right"))
    if valid_5m_end <= 0:
        return []

    sig = sig_info["sig"]
    mp_obj = sig_info.get("mp_obj")
    ts_close_tf = feat_tf["ts_close"]
    c_tf = feat_tf["close"]
    atr_tf = feat_tf["atr_14"]
    rhigh_tf = feat_tf["range_high_24"]
    rlow_tf = feat_tf["range_low_24"]
    sh10_tf = feat_tf["swing_high_10"]
    sl10_tf = feat_tf["swing_low_10"]
    vah_tf = sig_info["va_upper"]
    val_tf = sig_info["va_lower"]
    poc_tf = sig_info["poc"]
    rsi_tf = feat_tf["rsi"]

    tf_at_5m_close = np.searchsorted(ts_close_tf, ts5[:valid_5m_end] + 300, side="right") - 1

    cooldown_sec = int(cfg.sl_cooldown_hours * 3600) if cfg.sl_cooldown_hours > 0 else 0
    cooldown_until_utc = 0
    trades: List[Dict[str, Any]] = []

    i_start = int(np.searchsorted(ts_close_tf, win_start_utc, side="left"))
    i_end = int(np.searchsorted(ts_close_tf, win_end_utc, side="left"))
    next_allowed_order_utc = win_start_utc
    seq_no = 0
    tr_ratio = float(params.get("target_ratio", 0.70))
    rsi_exit_thresh = float(params.get("s_cp" if side == "short" else "l_cp", 40.0 if side == "short" else 60.0))
    enable_emerg_be = bool(getattr(cfg, "enable_emergency_breakeven", True))

    for i in range(i_start, i_end):
        if not sig[i]:
            continue
        sig_close_utc = int(ts_close_tf[i])
        if sig_close_utc < cooldown_until_utc or sig_close_utc < next_allowed_order_utc:
            continue

        sig_regime_meta: Dict[str, Any] = {
            "regime_at_signal": None,
            "strength_tier_at_signal": None,
            "net_score_at_signal": None,
            "buy_votes_at_signal": None,
            "sell_votes_at_signal": None,
        }
        if hourly_regime_map is not None:
            t_hr = (sig_close_utc // 3600) * 3600
            snap = hourly_regime_map.get(t_hr)
            if (
                snap is None
                or strategy not in snap["allowed_strategies_set"]
                or norm_sym in snap.get("excluded_symbols_set", frozenset())
            ):
                continue
            sig_regime_meta = {
                "regime_at_signal": snap["overall_regime"],
                "strength_tier_at_signal": snap["strength_tier"],
                "net_score_at_signal": snap["net_score"],
                "buy_votes_at_signal": snap["mean_buy_votes"],
                "sell_votes_at_signal": snap["mean_sell_votes"],
            }

        m_start = int(np.searchsorted(ts5[:valid_5m_end], sig_close_utc, side="left"))
        if m_start >= valid_5m_end:
            break

        sig_px = float(c_tf[i])
        if sig_px <= 0:
            continue

        atr_val = float(atr_tf[i]) if atr_tf[i] > 0 else (sig_px * 0.005)
        va_u = float(vah_tf[i]) if math.isfinite(vah_tf[i]) and vah_tf[i] > 0 else sig_px
        va_l = float(val_tf[i]) if math.isfinite(val_tf[i]) and val_tf[i] > 0 else sig_px
        rh = float(rhigh_tf[i]) if math.isfinite(rhigh_tf[i]) and rhigh_tf[i] > 0 else sig_px
        rl = float(rlow_tf[i]) if math.isfinite(rlow_tf[i]) and rlow_tf[i] > 0 else sig_px

        if side == "long":
            ref_base = va_u if is_brk else rl
            ext_pct = ((sig_px - ref_base) / ref_base * 100.0) if ref_base > 0 else 0.0
            ext_atr = (sig_px - ref_base) / atr_val if atr_val > 0 else 0.0
        else:
            ref_base = va_l if is_brk else rh
            ext_pct = ((ref_base - sig_px) / ref_base * 100.0) if ref_base > 0 else 0.0
            ext_atr = (ref_base - sig_px) / atr_val if atr_val > 0 else 0.0

        max_chase_bars = 3 if tf == "5m" else 1
        filled_bar = -1
        order_px = 0.0
        fill_px = 0.0
        first_order_ts = int(ts5[m_start])
        first_order_px = sig_px * (1.0 - cfg.fee_maker if side == "long" else 1.0 + cfg.fee_maker)
        cancel_ts: Optional[int] = None
        cancel_reason: Optional[str] = None

        for offset in range(max_chase_bars):
            m = m_start + offset
            if m >= valid_5m_end or int(ts5[m]) + 300 > win_end_utc:
                cancel_ts = int(ts5[min(m, valid_5m_end - 1)])
                cancel_reason = "CANCEL_WINDOW_END"
                break

            if offset == 0:
                order_px = first_order_px
            else:
                bar_decision_ts = int(ts5[m])
                if tf in ("5m", "15m") and not _session_allows_strategy(bar_decision_ts, is_brk, cfg):
                    cancel_ts = bar_decision_ts
                    cancel_reason = "CANCEL_SIGNAL_END"
                    break
                if hourly_regime_map is not None:
                    snap_c = hourly_regime_map.get((bar_decision_ts // 3600) * 3600)
                    if snap_c is None or strategy not in snap_c["allowed_strategies_set"] or norm_sym in snap_c.get("excluded_symbols_set", frozenset()):
                        cancel_ts = bar_decision_ts
                        cancel_reason = "CANCEL_SIGNAL_END"
                        break
                tf_idx_prev = int(tf_at_5m_close[m - 1])
                if tf_idx_prev < 0 or not sig[tf_idx_prev]:
                    cancel_ts = bar_decision_ts
                    cancel_reason = "CANCEL_SIGNAL_END"
                    break
                order_px = float(c5[m - 1]) * (1.0 - cfg.fee_maker if side == "long" else 1.0 + cfg.fee_maker)

            if side == "long":
                if l5[m] <= order_px:
                    filled_bar = m
                    fill_px = order_px
                    break
            else:
                if h5[m] >= order_px:
                    filled_bar = m
                    fill_px = order_px
                    break

            if offset == max_chase_bars - 1:
                cancel_ts = int(ts5[m]) + 300
                cancel_reason = "CANCEL_SIGNAL_END"

        seq_no += 1
        qty = cfg.position_cost_usdt / (fill_px if filled_bar >= 0 else first_order_px)

        if filled_bar < 0:
            trades.append(
                {
                    "seq": seq_no,
                    "window_type": window_type,
                    "symbol": symbol,
                    "strategy": strategy,
                    "side": side,
                    "timeframe": tf,
                    "signal_ts_utc": int(feat_tf["ts_open"][i]),
                    "order_ts_utc": first_order_ts,
                    "order_px": round(first_order_px, 8),
                    "fill_ts_utc": None,
                    "fill_px": None,
                    "cancel_ts_utc": cancel_ts,
                    "cancel_reason": cancel_reason or "CANCEL_SIGNAL_END",
                    "exit_ts_utc": None,
                    "exit_px": None,
                    "exit_reason": None,
                    "qty": round(qty, 8),
                    "pos_cost_usdt": cfg.position_cost_usdt,
                    "gross_pnl": 0.0,
                    "entry_fee_usdt": 0.0,
                    "exit_fee_usdt": 0.0,
                    "net_pnl": 0.0,
                    "ext_pct": round(ext_pct, 4),
                    "ext_atr": round(ext_atr, 4),
                    "va_upper": round(va_u, 8),
                    "va_lower": round(va_l, 8),
                    "range_high": round(rh, 8),
                    "range_low": round(rl, 8),
                    **sig_regime_meta,
                }
            )
            next_allowed_order_utc = int(cancel_ts or (first_order_ts + 300))
            continue

        # ===== 約定後エグジット評価 (毎足動的TP/SL + 地合い転換建値 + 4hタイムアウト + 8h MAX_HOLD) =====
        fill_ts = int(ts5[filled_bar])
        exit_bar = -1
        exit_px = 0.0
        exit_reason = ""
        exit_ts = 0

        max_hold_bars_5m = max(1, cfg.max_hold_minutes // 5)
        k_limit = min(valid_5m_end, filled_bar + max_hold_bars_5m)
        req_reg = "SELL" if side == "short" else "BUY"

        for k in range(filled_bar, k_limit):
            is_fill_bar = (k == filled_bar)
            cur_5m_close_utc = int(ts5[k]) + 300
            tf_k = int(tf_at_5m_close[k])
            can_check_close_exit = (
                (not is_fill_bar)
                and (tf_k >= 0)
                and (int(ts_close_tf[tf_k]) > fill_ts)
                and (tf == "5m" or cur_5m_close_utc == int(ts_close_tf[tf_k]))
            )

            if tf_k >= 0:
                if is_vp_strat and mp_obj is not None:
                    v_l, v_u, v_p = mp_obj.get_bar(tf_k)
                else:
                    v_l = float(val_tf[tf_k]) if math.isfinite(val_tf[tf_k]) and val_tf[tf_k] > 0 else fill_px
                    v_u = float(vah_tf[tf_k]) if math.isfinite(vah_tf[tf_k]) and vah_tf[tf_k] > 0 else fill_px
                    v_p = float(poc_tf[tf_k]) if math.isfinite(poc_tf[tf_k]) and poc_tf[tf_k] > 0 else fill_px
                prev10_sw = float(sh10_tf[tf_k] if side == "short" else sl10_tf[tf_k])
                cur_rsi_k = float(rsi_tf[tf_k])
            else:
                v_l = v_u = v_p = prev10_sw = fill_px
                cur_rsi_k = 50.0

            levels = compute_dynamic_exit_levels(
                strategy=strategy,
                avg_entry_px=fill_px,
                val=v_l,
                vah=v_u,
                poc=v_p,
                prev10_swing_px=prev10_sw,
                target_ratio=tr_ratio,
                eff_hard_sl_pct=cfg.hard_sl_pct,
                min_sl_pct=cfg.min_sl_pct,
            )

            is_emerg_be = False
            if enable_emerg_be and hourly_regime_map is not None:
                snap_k = hourly_regime_map.get((cur_5m_close_utc // 3600) * 3600)
                if snap_k is not None:
                    is_emerg_be = (snap_k["overall_regime"] != req_reg)

            elapsed_min_k = float(cur_5m_close_utc - fill_ts) / 60.0
            dec = evaluate_dynamic_exit_decision(
                strategy=strategy,
                avg_entry_px=fill_px,
                levels=levels,
                is_fill_bar=is_fill_bar,
                can_check_close_exit=can_check_close_exit,
                is_emergency_breakeven=is_emerg_be,
                elapsed_minutes=elapsed_min_k,
                low_px=float(l5[k]),
                high_px=float(h5[k]),
                close_check_px=float(c5[k]),
                live_px=float(c5[k]),
                cur_rsi=cur_rsi_k,
                rsi_exit_thresh=rsi_exit_thresh,
                timeout_minutes=float(cfg.timeout_minutes),
                max_hold_minutes=float(cfg.max_hold_minutes),
                prefer_limit_fill_px=True,
            )
            if dec["action"] != "HOLD":
                exit_bar = k
                exit_px = float(dec["exit_px"])
                exit_reason = str(dec["code"])
                exit_ts = cur_5m_close_utc
                break

        if exit_bar < 0:
            last_k = min(k_limit - 1, valid_5m_end - 1)
            exit_bar = last_k
            exit_px = float(c5[last_k])
            exit_ts = int(ts5[last_k]) + 300
            exit_reason = "WINDOW_END_FLATTEN" if last_k == valid_5m_end - 1 else "MAX_HOLD_CLOSE"

        entry_fee = cfg.position_cost_usdt * cfg.fee_maker
        exit_val = qty * exit_px
        exit_fee = exit_val * cfg.fee_taker
        if side == "long":
            gross_pnl = (exit_px - fill_px) * qty
        else:
            gross_pnl = (fill_px - exit_px) * qty
        net_pnl = gross_pnl - entry_fee - exit_fee

        if cooldown_sec > 0 and (("SL" in exit_reason) or ("TIMEOUT" in exit_reason)):
            cooldown_until_utc = exit_ts + cooldown_sec
        next_allowed_order_utc = exit_ts

        trades.append(
            {
                "seq": seq_no,
                "window_type": window_type,
                "symbol": symbol,
                "strategy": strategy,
                "side": side,
                "timeframe": tf,
                "signal_ts_utc": int(feat_tf["ts_open"][i]),
                "order_ts_utc": first_order_ts,
                "order_px": round(first_order_px, 8),
                "fill_ts_utc": fill_ts,
                "fill_px": round(fill_px, 8),
                "cancel_ts_utc": None,
                "cancel_reason": None,
                "exit_ts_utc": exit_ts,
                "exit_px": round(exit_px, 8),
                "exit_reason": exit_reason,
                "qty": qty,
                "pos_cost_usdt": cfg.position_cost_usdt,
                "gross_pnl": gross_pnl,
                "entry_fee_usdt": entry_fee,
                "exit_fee_usdt": exit_fee,
                "net_pnl": net_pnl,
                "ext_pct": round(ext_pct, 4),
                "ext_atr": round(ext_atr, 4),
                "va_upper": round(va_u, 8),
                "va_lower": round(va_l, 8),
                "range_high": round(rh, 8),
                "range_low": round(rl, 8),
                **sig_regime_meta,
            }
        )

    return trades


def _grade_eval_cell(eval_trades: int, eval_ev: float, cfg: WalkForwardConfig) -> str:
    if eval_trades < cfg.min_eval_trades_provisional:
        return "INSUFFICIENT_N"
    if eval_ev <= 0.0:
        return "REJECTED_EV"
    if eval_trades >= cfg.min_eval_trades_qualified:
        return "QUALIFIED"
    return "PROVISIONAL"


def select_eval_winner(
    cells: Sequence[Dict[str, Any]],
    cfg: WalkForwardConfig,
    allowed_strategies: Optional[Sequence[str]] = None,
    excluded_symbols: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    評価窓 (OOS) 成績のみから当日の勝者1件を決定論的に選出する。
    `allowed_strategies` / `excluded_symbols` が指定された場合は現時点の許可条件に合致するセルのみ対象とする。
    `QUALIFIED` (n>=30) を `PROVISIONAL` (10<=n<30) より優先し、
    同一格付け内では (eval_ev, eval_pf, eval_net_pnl, eval_trades) の降順で決定する。
    """
    allowed_set = {str(s) for s in allowed_strategies} if allowed_strategies is not None else None
    excluded_set = {normalize_symbol(str(s)) for s in excluded_symbols} if excluded_symbols else set()
    eligible = [
        c for c in cells
        if c["eval_grade"] in ("QUALIFIED", "PROVISIONAL")
        and float(c["eval_ev"]) > 0.0
        and (allowed_set is None or str(c["strategy"]) in allowed_set)
        and normalize_symbol(str(c["symbol"])) not in excluded_set
    ]
    if not eligible:
        return None

    def _sort_key(c: Dict[str, Any]) -> Tuple[int, float, float, float, int, str]:
        grade_rank = 2 if c["eval_grade"] == "QUALIFIED" else 1
        return (
            grade_rank,
            round(float(c["eval_ev"]), 8),
            round(float(c["eval_pf"]), 6),
            round(float(c["eval_net_pnl"]), 6),
            int(c["eval_trades"]),
            f"{c['symbol']}|{c['strategy']}|{c['timeframe']}",
        )

    eligible.sort(key=_sort_key, reverse=True)
    return eligible[0]


def run_walkforward_from_lake(
    lake: LakeStore,
    data_cutoff_utc: Optional[int] = None,
    cfg: Optional[WalkForwardConfig] = None,
    override_regime_info: Optional[Dict[str, Any]] = None,
    symbols: Optional[Sequence[str]] = None,
    allowed_strategies_override: Optional[Sequence[str]] = None,
    run_id: Optional[str] = None,
    selection_id: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    データレイク上のデータのみを使用して 2段階ウォークフォワード評価 (Opt → Eval OOS) を実行し、
    全結果をレイク (`backtest_runs`, `backtest_cells`, `backtest_simulated_trades`, `daily_selections`)
    に保存して勝者1件 (または見送り) を返す。
    """
    cfg = cfg or WalkForwardConfig()
    cutoff = int(data_cutoff_utc) if data_cutoff_utc is not None else utc_now_sec()
    now_run = utc_now_sec()

    # Stage 1 [opt_start, opt_end), Stage 2 [eval_start, eval_end] (opt_end <= eval_start)
    eval_end_utc = cutoff
    eval_start_utc = eval_end_utc - (cfg.eval_days * 86400)
    opt_end_utc = eval_start_utc - (cfg.embargo_bars * 300)
    opt_start_utc = opt_end_utc - (cfg.opt_days * 86400)

    if opt_end_utc > eval_start_utc:
        raise ValueError("opt_end_utc must be <= eval_start_utc")

    regime_info = compute_regime_from_lake(lake, cutoff, override_regime_info=override_regime_info)
    overall_regime = str(regime_info.get("overall_regime", "NEUTRAL")).upper()
    strength_obj = regime_info.get("regime_strength", {}) or {}
    strength_tier = str(strength_obj.get("tier", "BALANCED"))

    hourly_regime_map, pit_active = compute_hourly_regime_series_from_lake(
        lake=lake,
        start_utc=opt_start_utc,
        end_utc=eval_end_utc,
        override_regime_info=override_regime_info,
        use_point_in_time_regime=cfg.use_point_in_time_regime,
        gil_yield_sec=cfg.gil_yield_sec,
    )

    raw_excl = regime_info.get("excluded_symbols") or {}
    cutoff_excluded = set(
        normalize_symbol(str(s))
        for s in (raw_excl.keys() if isinstance(raw_excl, dict) else raw_excl)
    )
    if override_regime_info is not None:
        cutoff_allowed = [str(s) for s in regime_info.get("allowed_strategies", []) if str(s) in ALL_STRATEGIES]
    elif allowed_strategies_override is not None:
        cutoff_allowed = [str(s) for s in allowed_strategies_override if str(s) in ALL_STRATEGIES]
    else:
        cutoff_allowed = [str(s) for s in regime_info.get("allowed_strategies", []) if str(s) in ALL_STRATEGIES]

    if pit_active:
        eval_strategies = (
            [str(s) for s in allowed_strategies_override if str(s) in ALL_STRATEGIES]
            if allowed_strategies_override is not None
            else list(ALL_STRATEGIES)
        )
        if symbols is not None:
            target_symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s) != BENCHMARK_SYMBOL]
        else:
            target_symbols = list(TRADABLE_MID_CAP_SYMBOLS)
    else:
        if allowed_strategies_override is not None:
            eval_strategies = [str(s) for s in allowed_strategies_override if str(s) in ALL_STRATEGIES]
        elif override_regime_info is not None:
            eval_strategies = list(cutoff_allowed)
        else:
            eval_strategies = list(ALL_STRATEGIES)

        rec_syms = regime_info.get("recommended_symbols")
        if symbols is not None:
            target_symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s) != BENCHMARK_SYMBOL]
        elif rec_syms:
            target_symbols = [normalize_symbol(s) for s in rec_syms if normalize_symbol(s) != BENCHMARK_SYMBOL]
        else:
            target_symbols = list(TRADABLE_MID_CAP_SYMBOLS)
        target_symbols = [s for s in target_symbols if s not in cutoff_excluded]

    cutoff_rec_syms = regime_info.get("recommended_symbols")
    cutoff_snap_rec = (
        [normalize_symbol(s) for s in cutoff_rec_syms if normalize_symbol(s) != BENCHMARK_SYMBOL]
        if cutoff_rec_syms
        else list(target_symbols)
    )
    cutoff_snap = {
        "cutoff_utc": cutoff,
        "cutoff_iso": sec_to_iso(cutoff),
        "overall_regime": overall_regime,
        "regime_pattern": str(regime_info.get("regime_pattern", "")),
        "strength_tier": strength_tier,
        "net_score": float(strength_obj.get("net_score", 0.0) or 0.0),
        "mean_buy_votes": float(strength_obj.get("mean_buy_votes", 0.0) or 0.0),
        "mean_sell_votes": float(strength_obj.get("mean_sell_votes", 0.0) or 0.0),
        "allowed_strategies": cutoff_allowed,
        "recommended_symbols": cutoff_snap_rec,
        "excluded_symbols": regime_info.get("excluded_symbols", {}),
        "whale_snapshot": regime_info.get("whale_snapshot", {}),
    }

    cfg_json = json.dumps(cfg.to_dict(), sort_keys=True, ensure_ascii=False)
    if run_id is None:
        id_seed = f"{cutoff}|{cfg_json}|{overall_regime}|{','.join(sorted(cutoff_allowed))}|{','.join(sorted(target_symbols))}"
        run_id = f"wf_{cutoff}_{hashlib.sha1(id_seed.encode('utf-8')).hexdigest()[:10]}"
    if selection_id is None:
        selection_id = f"sel_{run_id}"

    run_row = {
        "run_id": run_id,
        "run_at_utc": now_run,
        "data_cutoff_utc": cutoff,
        "opt_start_utc": opt_start_utc,
        "opt_end_utc": opt_end_utc,
        "eval_start_utc": eval_start_utc,
        "eval_end_utc": eval_end_utc,
        "embargo_bars": cfg.embargo_bars,
        "regime": overall_regime,
        "strength_tier": strength_tier,
        "allowed_strategies_json": json.dumps(cutoff_allowed, ensure_ascii=False),
        "fee_maker": cfg.fee_maker,
        "fee_taker": cfg.fee_taker,
        "code_git_sha": get_git_sha_short(),
        "config_json": cfg_json,
    }

    # ウォームアップ用余白は個別最適化/評価ループ側で足種ごとに最小限 (max(2d, 220*tf)) を厳密切り出しする
    has_vp_strat = any(s in ("val_poc", "vah_poc", "vah_break", "val_break") for s in eval_strategies)
    opt_mps: Tuple[int, ...] = (70, 100, 150) if has_vp_strat else ()

    cell_rows: List[Dict[str, Any]] = []
    sim_trade_rows: List[Dict[str, Any]] = []

    if eval_strategies and target_symbols:
        opt_warmup_5m = max(2 * 86400, 220 * 300)
        eval_warmup_5m = max(2 * 86400, 220 * 300)
        for sym in target_symbols:
            df_5m_opt = lake.load_candles(sym, "5m", cutoff_utc=opt_end_utc, start_utc=opt_start_utc - opt_warmup_5m)
            df_5m_eval = lake.load_candles(sym, "5m", cutoff_utc=eval_end_utc, start_utc=eval_start_utc - eval_warmup_5m)
            if df_5m_opt.empty or df_5m_eval.empty:
                continue
            df_oi_opt = lake.load_oi_fr(sym, cutoff_utc=opt_end_utc, start_utc=opt_start_utc - 5 * 86400)
            df_oi_eval = lake.load_oi_fr(sym, cutoff_utc=eval_end_utc, start_utc=eval_start_utc - 5 * 86400)

            for tf in cfg.timeframes:
                tf_sec = TIMEFRAME_SECONDS[tf]
                tf_warmup = max(2 * 86400, 220 * tf_sec)
                df_tf_opt = lake.load_candles(sym, tf, cutoff_utc=opt_end_utc, start_utc=opt_start_utc - tf_warmup)
                df_tf_eval = lake.load_candles(sym, tf, cutoff_utc=eval_end_utc, start_utc=eval_start_utc - tf_warmup)
                if len(df_tf_opt) < 120 or len(df_tf_eval) < 120:
                    continue

                df_tf_opt = lake.attach_oi_fr_causal(df_tf_opt, tf, df_oi_opt, cutoff_utc=opt_end_utc)
                df_tf_eval = lake.attach_oi_fr_causal(df_tf_eval, tf, df_oi_eval, cutoff_utc=eval_end_utc)

                feat_opt = _compute_causal_features(df_tf_opt, tf, cfg, needed_mps=opt_mps)

                # Stage 1: 対象戦略を最適化窓のみで探索し、Stage 2 に渡すパラメータと必要な MP のみを特定
                frozen_choices: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
                eval_needed_mps = set()
                for strat in eval_strategies:
                    grid = get_param_grid(strat)
                    best_opt_score = (-1e18, -1e18, -1e18)
                    best_params: Optional[Dict[str, Any]] = None
                    best_opt_metrics: Optional[Dict[str, Any]] = None

                    for params in grid:
                        sig_opt = _generate_signal_array(feat_opt, strat, params, cfg)
                        opt_trades_list = _simulate_window(
                            feat_opt, sig_opt, df_5m_opt, sym, strat, params,
                            window_type="OPT", win_start_utc=opt_start_utc, win_end_utc=opt_end_utc, cfg=cfg,
                            hourly_regime_map=hourly_regime_map,
                        )
                        m_opt = _compute_metrics(opt_trades_list)
                        has_min = 1 if m_opt["trades"] >= 3 else 0
                        score_tup = (
                            has_min,
                            round(m_opt["net_pnl"], 6),
                            round(m_opt["ev"], 6),
                            round(m_opt["pf"], 4),
                        )
                        if best_params is None or score_tup > best_opt_score:
                            best_opt_score = score_tup
                            best_params = params
                            best_opt_metrics = m_opt

                    if best_params is not None and best_opt_metrics is not None:
                        frozen_choices.append((strat, best_params, best_opt_metrics))
                        if "mp" in best_params:
                            eval_needed_mps.add(int(best_params["mp"]))

                if not frozen_choices:
                    continue

                # Stage 2: 凍結パラメータが必要とする MP のみを計算して OOS 評価 (時点地合いゲート適用)
                feat_eval = _compute_causal_features(df_tf_eval, tf, cfg, needed_mps=sorted(eval_needed_mps))

                for strat, best_params, best_opt_metrics in frozen_choices:
                    sig_eval = _generate_signal_array(feat_eval, strat, best_params, cfg)
                    eval_trades_list = _simulate_window(
                        feat_eval, sig_eval, df_5m_eval, sym, strat, best_params,
                        window_type="EVAL", win_start_utc=eval_start_utc, win_end_utc=eval_end_utc, cfg=cfg,
                        hourly_regime_map=hourly_regime_map,
                    )
                    m_eval = _compute_metrics(eval_trades_list)
                    eval_grade = _grade_eval_cell(m_eval["trades"], m_eval["ev"], cfg)

                    cell_id = f"{run_id}__{sym}__{strat}__{tf}"
                    cell_row = {
                        "cell_id": cell_id,
                        "run_id": run_id,
                        "symbol": sym,
                        "strategy": strat,
                        "side": strategy_side(strat),
                        "timeframe": tf,
                        "params_json": json.dumps(best_params, sort_keys=True, ensure_ascii=False),
                        "opt_trades": int(best_opt_metrics["trades"]),
                        "opt_win_rate": round(float(best_opt_metrics["win_rate"]), 4),
                        "opt_net_pnl": round(float(best_opt_metrics["net_pnl"]), 6),
                        "opt_ev": round(float(best_opt_metrics["ev"]), 6),
                        "opt_pf": round(float(best_opt_metrics["pf"]), 4),
                        "opt_max_dd": round(float(best_opt_metrics["max_dd"]), 6),
                        "eval_trades": int(m_eval["trades"]),
                        "eval_wins": int(m_eval["wins"]),
                        "eval_win_rate": round(float(m_eval["win_rate"]), 4),
                        "eval_gross_pnl": round(float(m_eval["gross_pnl"]), 6),
                        "eval_total_fees": round(float(m_eval["total_fees"]), 6),
                        "eval_net_pnl": round(float(m_eval["net_pnl"]), 6),
                        "eval_ev": round(float(m_eval["ev"]), 6),
                        "eval_pf": round(float(m_eval["pf"]), 4),
                        "eval_max_dd": round(float(m_eval["max_dd"]), 6),
                        "eval_grade": eval_grade,
                        "eval_ext_stats_json": json.dumps(m_eval["ext_stats"], sort_keys=True, ensure_ascii=False),
                        "is_opt_best": 1,
                    }
                    cell_rows.append(cell_row)

                    for tr in eval_trades_list:
                        sim_trade_rows.append(
                            {
                                "sim_trade_id": f"{cell_id}__EVAL__{tr['seq']}",
                                "run_id": run_id,
                                "cell_id": cell_id,
                                **tr,
                            }
                        )

            if cfg.gil_yield_sec > 0:
                time.sleep(cfg.gil_yield_sec)

    winner = select_eval_winner(
        cell_rows,
        cfg,
        allowed_strategies=cutoff_allowed if pit_active else eval_strategies,
        excluded_symbols=cutoff_excluded,
    )
    trade_date_jst = datetime.fromtimestamp(cutoff, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d")

    if winner is not None:
        reason_text = (
            f"OOS勝者選定: {winner['symbol']} {winner['strategy'].upper()}({winner['timeframe']}) "
            f"[{winner['eval_grade']}] n={winner['eval_trades']}, WR={winner['eval_win_rate']:.1f}%, "
            f"EV=${winner['eval_ev']:+.4f}/trade, Net=${winner['eval_net_pnl']:+.2f}, PF={winner['eval_pf']:.2f} "
            f"| 評価期間={sec_to_iso(eval_start_utc)[:10]}..{sec_to_iso(eval_end_utc)[:10]}"
        )
        selection_row = {
            "selection_id": selection_id,
            "run_id": run_id,
            "selected_at_utc": now_run,
            "trade_date_jst": trade_date_jst,
            "regime": overall_regime,
            "winner_symbol": winner["symbol"],
            "winner_strategy": winner["strategy"],
            "winner_side": winner["side"],
            "winner_timeframe": winner["timeframe"],
            "winner_params_json": winner["params_json"],
            "winner_cell_id": winner["cell_id"],
            "eval_trades": winner["eval_trades"],
            "eval_ev": winner["eval_ev"],
            "eval_net_pnl": winner["eval_net_pnl"],
            "eval_pf": winner["eval_pf"],
            "eval_win_rate": winner["eval_win_rate"],
            "eval_grade": winner["eval_grade"],
            "status": "SELECTED",
            "reason_text": reason_text,
        }
    else:
        max_eval_n = max((c["eval_trades"] for c in cell_rows), default=0)
        pos_ev_cnt = sum(1 for c in cell_rows if c["eval_ev"] > 0 and c["eval_trades"] >= cfg.min_eval_trades_provisional)
        reason_text = (
            f"取引見送り (NO_CANDIDATE): 地合い={overall_regime}({strength_tier}), "
            f"評価セル数={len(cell_rows)}, 最大OOS件数={max_eval_n}, 適格候補(n>={cfg.min_eval_trades_provisional} & EV>0)={pos_ev_cnt}件"
        )
        selection_row = {
            "selection_id": selection_id,
            "run_id": run_id,
            "selected_at_utc": now_run,
            "trade_date_jst": trade_date_jst,
            "regime": overall_regime,
            "winner_symbol": None,
            "winner_strategy": None,
            "winner_side": None,
            "winner_timeframe": None,
            "winner_params_json": None,
            "winner_cell_id": None,
            "eval_trades": None,
            "eval_ev": None,
            "eval_net_pnl": None,
            "eval_pf": None,
            "eval_win_rate": None,
            "eval_grade": None,
            "status": "NO_CANDIDATE",
            "reason_text": reason_text,
        }

    if persist:
        if pit_active and hourly_regime_map:
            lake.upsert_regime_snapshots_batch(list(hourly_regime_map.values()))
        lake.upsert_regime_snapshot(cutoff_snap)
        lake.save_backtest_bundle(run_row, cell_rows, sim_trade_rows, selection_row=selection_row)

    return {
        "run": run_row,
        "regime_info": regime_info,
        "cells": cell_rows,
        "sim_trades": sim_trade_rows,
        "winner": winner,
        "selection": selection_row,
        "pit_active": pit_active,
        "hourly_regime_count": len(hourly_regime_map) if pit_active else 0,
    }


def _cell_rank_key_for_portfolio(c: Dict[str, Any]) -> Tuple[int, float, float, float, int, str]:
    g = str(c.get("eval_grade", ""))
    grade_rank = 2 if g == "QUALIFIED" else (1 if g == "PROVISIONAL" else (0 if g == "REJECTED_EV" else -1))
    return (
        grade_rank,
        round(float(c.get("eval_ev", 0.0) or 0.0), 8),
        round(float(c.get("eval_pf", 0.0) or 0.0), 6),
        round(float(c.get("eval_net_pnl", 0.0) or 0.0), 6),
        int(c.get("eval_trades", 0) or 0),
        f"{c.get('symbol', '')}|{c.get('strategy', '')}|{c.get('timeframe', '')}",
    )


def _is_portfolio_cell_qualified(c: Optional[Dict[str, Any]]) -> bool:
    if not c:
        return False
    return (
        str(c.get("eval_grade", "")) in ("QUALIFIED", "PROVISIONAL")
        and float(c.get("eval_ev", 0.0) or 0.0) > 0.0
        and float(c.get("eval_net_pnl", 0.0) or 0.0) > 0.0
    )


def build_portfolio_params_map_from_cells(
    cells: Sequence[Dict[str, Any]],
    excluded_mega_caps: Sequence[str] = EXCLUDED_TRADE_MEGA_CAPS,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """OOS 評価済みセル集合から、実運用と同一のデュアルセッション用パラメータマップおよび監視順位を構築する。"""
    mega_set = {normalize_symbol(str(s)) for s in excluded_mega_caps}
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for c in cells:
        sym = normalize_symbol(str(c["symbol"]))
        if sym in mega_set:
            continue
        by_sym.setdefault(sym, []).append(c)

    params_map: Dict[str, Dict[str, Any]] = {}
    best_by_sym: Dict[str, Dict[str, Any]] = {}
    ranked_syms: List[str] = []

    def _to_spec(cell: Dict[str, Any], is_q: bool) -> Dict[str, Any]:
        raw_p = json.loads(cell["params_json"])
        tf = str(cell["timeframe"])
        return {
            "strategy": str(cell["strategy"]),
            "side": str(cell["side"]),
            "timeframe": tf,
            "interval": {"5m": 5, "15m": 15, "1h": 60}.get(tf, 5),
            "mp": int(raw_p.get("mp", 150)),
            "target_ratio": float(raw_p.get("target_ratio", 0.70)),
            "l_ep": float(raw_p.get("l_ep", 40.0)),
            "l_cp": float(raw_p.get("l_cp", 60.0)),
            "s_ep": float(raw_p.get("s_ep", 60.0)),
            "s_cp": float(raw_p.get("s_cp", 40.0)),
            "is_qualified": bool(is_q),
            "eval_grade": str(cell["eval_grade"]),
            "eval_ev": float(cell["eval_ev"]),
        }

    def _build_dual_spec(cell_group: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        rng_cells = [c for c in cell_group if not is_breakout_strategy(str(c["strategy"]))]
        brk_cells = [c for c in cell_group if is_breakout_strategy(str(c["strategy"]))]
        best_rng = max(rng_cells, key=_cell_rank_key_for_portfolio) if rng_cells else None
        best_brk = max(brk_cells, key=_cell_rank_key_for_portfolio) if brk_cells else None
        best_all = max(cell_group, key=_cell_rank_key_for_portfolio)

        rng_q = _is_portfolio_cell_qualified(best_rng)
        brk_q = _is_portfolio_cell_qualified(best_brk)
        is_q = rng_q or brk_q

        spec_obj = _to_spec(best_all, is_q)
        spec_obj["is_range_qualified"] = rng_q
        spec_obj["is_breakout_qualified"] = brk_q
        if best_rng is not None:
            spec_obj["range_params"] = _to_spec(best_rng, rng_q)
        if best_brk is not None:
            spec_obj["breakout_params"] = _to_spec(best_brk, brk_q)
        return spec_obj, best_all, is_q

    for sym, sym_cells in by_sym.items():
        spec, best_all, is_q = _build_dual_spec(sym_cells)
        side_specs: Dict[str, Dict[str, Any]] = {}
        for side_key in ("long", "short"):
            s_cells = [c for c in sym_cells if str(c.get("side", "")) == side_key]
            if s_cells:
                s_spec, _, s_q = _build_dual_spec(s_cells)
                if s_q:
                    side_specs[side_key] = s_spec
        if side_specs:
            spec["side_specs"] = side_specs
        is_any_q = is_q or bool(side_specs)
        spec["is_qualified"] = is_any_q
        params_map[sym] = spec
        best_by_sym[sym] = best_all
        if is_any_q:
            ranked_syms.append(sym)

    ranked_syms.sort(key=lambda s: _cell_rank_key_for_portfolio(best_by_sym[s]), reverse=True)
    return ranked_syms, params_map


def simulate_portfolio_on_window(
    lake: LakeStore,
    symbol_params_map: Dict[str, Dict[str, Any]],
    ranked_symbols: Sequence[str],
    hourly_regime_map: Optional[Dict[int, Dict[str, Any]]],
    win_start_utc: int,
    win_end_utc: int,
    cfg: WalkForwardConfig,
) -> Dict[str, Any]:
    """
    実ボット (`bingx5_46_1futures_limit.py`) の全ポートフォリオ制約を 1:1 で再現する統合バックテスト:
      - 同時保有＋待機指値上限 (`cfg.max_active_positions = 2`)
      - セッション連動切替 (東京=`range_params`, 欧州=`breakout_params`, 米国=`best_overall`)
      - 毎足動的 Market Profile / Swing 10 (`shift(1)`) 更新・緊急建値撤退 (`±0.20%`)・480分 `MAX_HOLD`
      - 24時間SLクールダウンなし / 日次サーキットブレーカー (`cfg.daily_max_loss_usdt = 1.50`, 09:00 JST リセット)
    """
    syms = [normalize_symbol(s) for s in ranked_symbols if normalize_symbol(s) in symbol_params_map]
    if not syms or win_end_utc <= win_start_utc:
        empty_m = _compute_metrics([])
        return {"metrics": empty_m, "sim_trades": [], "by_session": {}, "by_strategy": {}}

    # 全選定銘柄の必要足種・特徴量・シグナル配列を一括構築 (すべて win_end_utc 以前のみ参照)
    sym_5m: Dict[str, Dict[str, Any]] = {}
    sym_feat: Dict[Tuple[str, str], Dict[str, Any]] = {}
    sym_sig: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    all_ts_5m: set[int] = set()

    for sym in syms:
        df5 = lake.load_candles(sym, "5m", cutoff_utc=win_end_utc, start_utc=win_start_utc - max(2 * 86400, 220 * 300))
        if df5.empty:
            continue
        ts5 = df5["ts_utc"].to_numpy(dtype=np.int64)
        valid_end = int(np.searchsorted(ts5 + 300, win_end_utc, side="right"))
        if valid_end <= 0:
            continue
        ts5_v = ts5[:valid_end]
        ts_to_idx5 = {int(ts5_v[idx]): idx for idx in range(valid_end)}
        sym_5m[sym] = {
            "ts": ts5_v,
            "o": df5["open"].to_numpy(dtype=np.float64)[:valid_end],
            "h": df5["high"].to_numpy(dtype=np.float64)[:valid_end],
            "l": df5["low"].to_numpy(dtype=np.float64)[:valid_end],
            "c": df5["close"].to_numpy(dtype=np.float64)[:valid_end],
            "idx_map": ts_to_idx5,
        }
        for t in ts5_v:
            if win_start_utc <= int(t) < win_end_utc:
                all_ts_5m.add(int(t))

        sp = symbol_params_map[sym]
        candidates: List[Dict[str, Any]] = []
        for base_sp in [sp, *list((sp.get("side_specs") or {}).values())]:
            candidates.append(base_sp)
            if "range_params" in base_sp:
                candidates.append(base_sp["range_params"])
            if "breakout_params" in base_sp:
                candidates.append(base_sp["breakout_params"])

        by_tf_mps: Dict[str, set[int]] = {}
        for cand in candidates:
            tf = str(cand.get("timeframe", "5m"))
            by_tf_mps.setdefault(tf, set()).add(int(cand.get("mp", 150)))

        df_oi = lake.load_oi_fr(sym, cutoff_utc=win_end_utc, start_utc=win_start_utc - 5 * 86400)
        for tf, mps in by_tf_mps.items():
            tf_sec = TIMEFRAME_SECONDS[tf]
            df_tf = lake.load_candles(sym, tf, cutoff_utc=win_end_utc, start_utc=win_start_utc - max(2 * 86400, 220 * tf_sec))
            if len(df_tf) < 30:
                continue
            df_tf = lake.attach_oi_fr_causal(df_tf, tf, df_oi, cutoff_utc=win_end_utc)
            feat_tf = _compute_causal_features(df_tf, tf, cfg, needed_mps=sorted(mps))
            sym_feat[(sym, tf)] = feat_tf

        for cand in candidates:
            strat = str(cand["strategy"])
            tf = str(cand.get("timeframe", "5m"))
            key = (sym, strat, tf)
            if (sym, tf) in sym_feat and key not in sym_sig:
                sym_sig[key] = _generate_signal_array(sym_feat[(sym, tf)], strat, cand, cfg)

    timeline = sorted(all_ts_5m)
    if not timeline:
        empty_m = _compute_metrics([])
        return {"metrics": empty_m, "sim_trades": [], "by_session": {}, "by_strategy": {}}

    active_pos: Dict[str, Dict[str, Any]] = {}
    pending_ord: Dict[str, Dict[str, Any]] = {}
    last_signal_tf_close: Dict[Tuple[str, str, str], int] = {}
    sim_trades: List[Dict[str, Any]] = []
    seq_no = 0
    enable_emerg_be = bool(getattr(cfg, "enable_emergency_breakeven", True))
    max_pos_limit = max(1, int(getattr(cfg, "max_active_positions", 2)))
    cb_limit = float(getattr(cfg, "daily_max_loss_usdt", 1.50))

    cur_trade_day = ""
    daily_realized_pnl = 0.0
    cb_broken = False

    def _pick_from_spec_obj(spec_obj: Dict[str, Any], hr_jst: int) -> Optional[Dict[str, Any]]:
        if 9 <= hr_jst < 16:
            cand = spec_obj.get("range_params") or spec_obj
            if not spec_obj.get("is_range_qualified") or is_breakout_strategy(str(cand["strategy"])):
                return None
            return cand
        if 16 <= hr_jst < 22:
            cand = spec_obj.get("breakout_params") or spec_obj
            if not spec_obj.get("is_breakout_qualified") or not is_breakout_strategy(str(cand["strategy"])):
                return None
            return cand
        if not getattr(cfg, "allow_ny_session", True):
            return None
        if not spec_obj.get("is_qualified"):
            return None
        return spec_obj

    def _pick_session_spec(sym_name: str, hr_jst: int, snap: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        sp = symbol_params_map[sym_name]
        side_specs = sp.get("side_specs") or {}
        if snap is not None and snap.get("allowed_strategies_set") and side_specs:
            allowed_set = snap["allowed_strategies_set"]
            matches = []
            for side_sp in side_specs.values():
                cand = _pick_from_spec_obj(side_sp, hr_jst)
                if cand is not None and str(cand.get("strategy", "")) in allowed_set:
                    matches.append(cand)
            if matches:
                return max(matches, key=lambda c: (float(c.get("eval_ev", 0.0) or 0.0), str(c.get("strategy", ""))))
            return None
        return _pick_from_spec_obj(sp, hr_jst)

    def _session_label(hr_jst: int) -> str:
        if 9 <= hr_jst < 16:
            return "TOKYO"
        if 16 <= hr_jst < 22:
            return "LONDON"
        return "NY"

    def _latest_tf_idx(feat_tf: Dict[str, Any], close_utc: int) -> int:
        ts_c = feat_tf["ts_close"]
        return int(np.searchsorted(ts_c, close_utc, side="right")) - 1

    for t5_open in timeline:
        t5_close = int(t5_open) + 300
        dt_utc = datetime.fromtimestamp(t5_close, tz=timezone.utc)
        day_str = dt_utc.strftime("%Y-%m-%d")
        if day_str != cur_trade_day:
            cur_trade_day = day_str
            daily_realized_pnl = 0.0
            cb_broken = False
        hr_jst = dt_utc.astimezone(JST).hour
        sess_lbl = _session_label(hr_jst)
        snap = hourly_regime_map.get((t5_close // 3600) * 3600) if hourly_regime_map else None

        # 1. 保有前からの待機指値について、本 5m 足 [t5_open, t5_close) 内の約定判定
        for sym in list(pending_ord.keys()):
            po = pending_ord[sym]
            s5 = sym_5m.get(sym)
            if not s5 or t5_open not in s5["idx_map"]:
                continue
            m_idx = s5["idx_map"][t5_open]
            o_px = float(po["order_px"])
            hit = (float(s5["l"][m_idx]) <= o_px) if po["side"] == "long" else (float(s5["h"][m_idx]) >= o_px)
            if hit:
                pending_ord.pop(sym)
                active_pos[sym] = {
                    **po,
                    "fill_ts": int(t5_open),
                    "fill_px": o_px,
                    "qty": cfg.position_cost_usdt / o_px,
                }

        # 2. 保有中ポジションの決済判定 (今約定したばかりのポジションは is_fill_bar=True 扱い)
        for sym in list(active_pos.keys()):
            ap = active_pos[sym]
            s5 = sym_5m.get(sym)
            if not s5 or t5_open not in s5["idx_map"]:
                continue
            m_idx = s5["idx_map"][t5_open]
            strat = str(ap["strategy"])
            tf = str(ap["timeframe"])
            side = str(ap["side"])
            fill_ts = int(ap["fill_ts"])
            fill_px = float(ap["fill_px"])
            is_fill_bar = (int(t5_open) == fill_ts)

            feat_tf = sym_feat[(sym, tf)]
            s_info = sym_sig[(sym, strat, tf)]
            tf_k = _latest_tf_idx(feat_tf, t5_close)
            can_check_close = (
                (not is_fill_bar)
                and (tf_k >= 0)
                and (int(feat_tf["ts_close"][tf_k]) > fill_ts)
                and (tf == "5m" or t5_close == int(feat_tf["ts_close"][tf_k]))
            )

            mp_obj = s_info.get("mp_obj")
            if tf_k >= 0:
                if mp_obj is not None and strat in ("val_poc", "vah_poc", "vah_break", "val_break"):
                    v_l, v_u, v_p = mp_obj.get_bar(tf_k)
                else:
                    v_l = float(s_info["va_lower"][tf_k]) if math.isfinite(s_info["va_lower"][tf_k]) and s_info["va_lower"][tf_k] > 0 else fill_px
                    v_u = float(s_info["va_upper"][tf_k]) if math.isfinite(s_info["va_upper"][tf_k]) and s_info["va_upper"][tf_k] > 0 else fill_px
                    v_p = float(s_info["poc"][tf_k]) if math.isfinite(s_info["poc"][tf_k]) and s_info["poc"][tf_k] > 0 else fill_px
                prev10_sw = float(feat_tf["swing_high_10"][tf_k] if side == "short" else feat_tf["swing_low_10"][tf_k])
                cur_rsi_k = float(feat_tf["rsi"][tf_k])
            else:
                v_l = v_u = v_p = prev10_sw = fill_px
                cur_rsi_k = 50.0

            levels = compute_dynamic_exit_levels(
                strategy=strat,
                avg_entry_px=fill_px,
                val=v_l,
                vah=v_u,
                poc=v_p,
                prev10_swing_px=prev10_sw,
                target_ratio=float(ap.get("target_ratio", 0.70)),
                eff_hard_sl_pct=cfg.hard_sl_pct,
                min_sl_pct=cfg.min_sl_pct,
            )
            is_emerg_be = False
            if enable_emerg_be and snap is not None:
                is_emerg_be = (snap["overall_regime"] != ("SELL" if side == "short" else "BUY"))

            elapsed_min_k = float(t5_close - fill_ts) / 60.0
            rsi_cp = float(ap.get("s_cp" if side == "short" else "l_cp", 40.0 if side == "short" else 60.0))
            dec = evaluate_dynamic_exit_decision(
                strategy=strat,
                avg_entry_px=fill_px,
                levels=levels,
                is_fill_bar=is_fill_bar,
                can_check_close_exit=can_check_close,
                is_emergency_breakeven=is_emerg_be,
                elapsed_minutes=elapsed_min_k,
                low_px=float(s5["l"][m_idx]),
                high_px=float(s5["h"][m_idx]),
                close_check_px=float(s5["c"][m_idx]),
                live_px=float(s5["c"][m_idx]),
                cur_rsi=cur_rsi_k,
                rsi_exit_thresh=rsi_cp,
                timeout_minutes=float(cfg.timeout_minutes),
                max_hold_minutes=float(cfg.max_hold_minutes),
                prefer_limit_fill_px=True,
            )
            if dec["action"] != "HOLD":
                exit_px = float(dec["exit_px"])
                qty = float(ap["qty"])
                entry_fee = cfg.position_cost_usdt * cfg.fee_maker
                exit_fee = (qty * exit_px) * cfg.fee_taker
                gross_pnl = ((exit_px - fill_px) * qty) if side == "long" else ((fill_px - exit_px) * qty)
                net_pnl = gross_pnl - entry_fee - exit_fee
                daily_realized_pnl += net_pnl
                if cb_limit > 0 and daily_realized_pnl <= -cb_limit:
                    cb_broken = True

                seq_no += 1
                sim_trades.append({
                    "seq": seq_no,
                    "window_type": "PORTFOLIO",
                    "session_at_entry": ap.get("session_at_entry", sess_lbl),
                    "symbol": sym,
                    "strategy": strat,
                    "side": side,
                    "timeframe": tf,
                    "signal_ts_utc": ap["signal_ts_utc"],
                    "order_ts_utc": ap["order_ts_utc"],
                    "order_px": round(float(ap["first_order_px"]), 8),
                    "fill_ts_utc": fill_ts,
                    "fill_px": round(fill_px, 8),
                    "cancel_ts_utc": None,
                    "cancel_reason": None,
                    "exit_ts_utc": t5_close,
                    "exit_px": round(exit_px, 8),
                    "exit_reason": str(dec["code"]),
                    "qty": qty,
                    "pos_cost_usdt": cfg.position_cost_usdt,
                    "gross_pnl": gross_pnl,
                    "entry_fee_usdt": entry_fee,
                    "exit_fee_usdt": exit_fee,
                    "net_pnl": net_pnl,
                    "ext_pct": ap.get("ext_pct"),
                    "ext_atr": ap.get("ext_atr"),
                })
                active_pos.pop(sym)

        # 3. CB 判定 (含み損考慮) & 地合い中立/CB時の待機指値全キャンセル
        unrealized_now = 0.0
        for sym, ap in active_pos.items():
            s5 = sym_5m.get(sym)
            if s5 and t5_open in s5["idx_map"]:
                c_now = float(s5["c"][s5["idx_map"][t5_open]])
                qty = float(ap["qty"])
                unrealized_now += ((c_now - ap["fill_px"]) * qty) if ap["side"] == "long" else ((ap["fill_px"] - c_now) * qty)
        cb_active_now = cb_broken or (cb_limit > 0 and (daily_realized_pnl + min(0.0, unrealized_now)) <= -cb_limit)
        neutral_stop = (snap is not None and snap["overall_regime"] == "NEUTRAL" and not snap["allowed_strategies"])

        if cb_active_now or neutral_stop:
            for sym in list(pending_ord.keys()):
                po = pending_ord.pop(sym)
                seq_no += 1
                sim_trades.append({
                    "seq": seq_no, "window_type": "PORTFOLIO", "symbol": sym, "strategy": po["strategy"],
                    "side": po["side"], "timeframe": po["timeframe"], "signal_ts_utc": po["signal_ts_utc"],
                    "order_ts_utc": po["order_ts_utc"], "order_px": round(float(po["first_order_px"]), 8),
                    "fill_ts_utc": None, "fill_px": None, "cancel_ts_utc": t5_close,
                    "cancel_reason": "CANCEL_CB" if cb_active_now else "CANCEL_NEUTRAL",
                    "exit_ts_utc": None, "exit_px": None, "exit_reason": None,
                    "qty": cfg.position_cost_usdt / float(po["first_order_px"]),
                    "pos_cost_usdt": cfg.position_cost_usdt, "gross_pnl": 0.0, "entry_fee_usdt": 0.0,
                    "exit_fee_usdt": 0.0, "net_pnl": 0.0, "ext_pct": po.get("ext_pct"), "ext_atr": po.get("ext_atr"),
                })
            continue

        # 4. 各候補銘柄の 待機指値更新 / 新規エントリー配置 (`t5_close` 確定後、次足 `[t5_close, t5_close+300)` 用)
        if t5_close >= win_end_utc:
            continue

        for sym in syms:
            if sym in active_pos:
                continue
            cand = _pick_session_spec(sym, hr_jst, snap=snap)
            strat_e = str(cand["strategy"]) if cand else ""
            tf_e = str(cand["timeframe"]) if cand else ""

            valid_reg_sym = (
                cand is not None
                and (snap is None or (strat_e in snap["allowed_strategies_set"] and sym not in snap.get("excluded_symbols_set", frozenset())))
            )

            if sym in pending_ord:
                po = pending_ord[sym]
                same_spec = valid_reg_sym and (po["strategy"] == strat_e and po["timeframe"] == tf_e and po["side"] == strategy_side(strat_e))
                max_chase = 3 if po["timeframe"] == "5m" else 1
                po["chase_used"] += 1
                keep_alive = False
                if same_spec and po["chase_used"] < max_chase and (sym, strat_e, tf_e) in sym_sig:
                    feat_tf = sym_feat[(sym, tf_e)]
                    tf_k = _latest_tf_idx(feat_tf, t5_close)
                    if tf_k >= 0 and sym_sig[(sym, strat_e, tf_e)]["sig"][tf_k]:
                        s5 = sym_5m.get(sym)
                        if s5 and t5_open in s5["idx_map"]:
                            ref_c = float(s5["c"][s5["idx_map"][t5_open]])
                            po["order_px"] = ref_c * (1.0 - cfg.fee_maker if po["side"] == "long" else 1.0 + cfg.fee_maker)
                            keep_alive = True
                if not keep_alive:
                    pending_ord.pop(sym)
                    seq_no += 1
                    sim_trades.append({
                        "seq": seq_no, "window_type": "PORTFOLIO", "symbol": sym, "strategy": po["strategy"],
                        "side": po["side"], "timeframe": po["timeframe"], "signal_ts_utc": po["signal_ts_utc"],
                        "order_ts_utc": po["order_ts_utc"], "order_px": round(float(po["first_order_px"]), 8),
                        "fill_ts_utc": None, "fill_px": None, "cancel_ts_utc": t5_close,
                        "cancel_reason": "CANCEL_SIGNAL_END",
                        "exit_ts_utc": None, "exit_px": None, "exit_reason": None,
                        "qty": cfg.position_cost_usdt / float(po["first_order_px"]),
                        "pos_cost_usdt": cfg.position_cost_usdt, "gross_pnl": 0.0, "entry_fee_usdt": 0.0,
                        "exit_fee_usdt": 0.0, "net_pnl": 0.0, "ext_pct": po.get("ext_pct"), "ext_atr": po.get("ext_atr"),
                    })
                continue

            if not valid_reg_sym:
                continue
            if (len(active_pos) + len(pending_ord)) >= max_pos_limit:
                continue
            if (sym, strat_e, tf_e) not in sym_sig:
                continue

            feat_tf = sym_feat[(sym, tf_e)]
            s_info = sym_sig[(sym, strat_e, tf_e)]
            tf_k = _latest_tf_idx(feat_tf, t5_close)
            if tf_k < 0 or not s_info["sig"][tf_k]:
                continue
            tf_close_ts = int(feat_tf["ts_close"][tf_k])
            if last_signal_tf_close.get((sym, strat_e, tf_e)) == tf_close_ts:
                continue

            last_signal_tf_close[(sym, strat_e, tf_e)] = tf_close_ts
            side_e = strategy_side(strat_e)
            s5 = sym_5m.get(sym)
            sig_px = float(s5["c"][s5["idx_map"][t5_open]]) if (s5 and t5_open in s5["idx_map"]) else float(feat_tf["close"][tf_k])
            ord_px = sig_px * (1.0 - cfg.fee_maker if side_e == "long" else 1.0 + cfg.fee_maker)
            atr_val_e = float(feat_tf["atr_14"][tf_k]) if feat_tf["atr_14"][tf_k] > 0 else (sig_px * 0.005)
            mp_obj_e = s_info.get("mp_obj")
            if mp_obj_e is not None and strat_e in ("val_poc", "vah_poc", "vah_break", "val_break"):
                va_l_e, va_u_e, _ = mp_obj_e.get_bar(tf_k)
            else:
                va_u_e = float(s_info["va_upper"][tf_k]) if math.isfinite(s_info["va_upper"][tf_k]) and s_info["va_upper"][tf_k] > 0 else sig_px
                va_l_e = float(s_info["va_lower"][tf_k]) if math.isfinite(s_info["va_lower"][tf_k]) and s_info["va_lower"][tf_k] > 0 else sig_px
            rh_e = float(feat_tf["range_high_24"][tf_k]) if math.isfinite(feat_tf["range_high_24"][tf_k]) and feat_tf["range_high_24"][tf_k] > 0 else sig_px
            rl_e = float(feat_tf["range_low_24"][tf_k]) if math.isfinite(feat_tf["range_low_24"][tf_k]) and feat_tf["range_low_24"][tf_k] > 0 else sig_px
            is_brk_e = is_breakout_strategy(strat_e)
            if side_e == "long":
                ref_base_e = va_u_e if is_brk_e else rl_e
                ext_pct_e = ((sig_px - ref_base_e) / ref_base_e * 100.0) if ref_base_e > 0 else 0.0
                ext_atr_e = (sig_px - ref_base_e) / atr_val_e if atr_val_e > 0 else 0.0
            else:
                ref_base_e = va_l_e if is_brk_e else rh_e
                ext_pct_e = ((ref_base_e - sig_px) / ref_base_e * 100.0) if ref_base_e > 0 else 0.0
                ext_atr_e = (ref_base_e - sig_px) / atr_val_e if atr_val_e > 0 else 0.0
            pending_ord[sym] = {
                "symbol": sym,
                "strategy": strat_e,
                "side": side_e,
                "timeframe": tf_e,
                "target_ratio": float(cand.get("target_ratio", 0.70)),
                "l_cp": float(cand.get("l_cp", 60.0)),
                "s_cp": float(cand.get("s_cp", 40.0)),
                "order_px": ord_px,
                "first_order_px": ord_px,
                "order_ts_utc": t5_close,
                "signal_ts_utc": int(feat_tf["ts_open"][tf_k]),
                "chase_used": 0,
                "session_at_entry": sess_lbl,
                "ext_pct": round(ext_pct_e, 4),
                "ext_atr": round(ext_atr_e, 4),
            }

    # 窓終了時に未決済ポジションをクローズ
    for sym, ap in list(active_pos.items()):
        s5 = sym_5m.get(sym)
        last_px = float(s5["c"][-1]) if (s5 and len(s5["c"]) > 0) else float(ap["fill_px"])
        qty = float(ap["qty"])
        side = str(ap["side"])
        entry_fee = cfg.position_cost_usdt * cfg.fee_maker
        exit_fee = (qty * last_px) * cfg.fee_taker
        gross_pnl = ((last_px - ap["fill_px"]) * qty) if side == "long" else ((ap["fill_px"] - last_px) * qty)
        net_pnl = gross_pnl - entry_fee - exit_fee
        seq_no += 1
        sim_trades.append({
            "seq": seq_no, "window_type": "PORTFOLIO", "session_at_entry": ap.get("session_at_entry", ""),
            "symbol": sym, "strategy": ap["strategy"], "side": side, "timeframe": ap["timeframe"],
            "signal_ts_utc": ap["signal_ts_utc"], "order_ts_utc": ap["order_ts_utc"],
            "order_px": round(float(ap["first_order_px"]), 8), "fill_ts_utc": ap["fill_ts"],
            "fill_px": round(float(ap["fill_px"]), 8), "cancel_ts_utc": None, "cancel_reason": None,
            "exit_ts_utc": win_end_utc, "exit_px": round(last_px, 8), "exit_reason": "WINDOW_END_FLATTEN",
            "qty": qty, "pos_cost_usdt": cfg.position_cost_usdt, "gross_pnl": gross_pnl,
            "entry_fee_usdt": entry_fee, "exit_fee_usdt": exit_fee, "net_pnl": net_pnl,
            "ext_pct": ap.get("ext_pct"), "ext_atr": ap.get("ext_atr"),
        })

    metrics = _compute_metrics(sim_trades)
    filled = [t for t in sim_trades if t.get("fill_ts_utc") is not None]
    by_session: Dict[str, Dict[str, Any]] = {}
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for s_key in ("TOKYO", "LONDON", "NY"):
        sub = [t for t in filled if t.get("session_at_entry") == s_key]
        by_session[s_key] = {"trades": len(sub), "net_pnl": round(sum(float(t["net_pnl"]) for t in sub), 4)}
    for st_key in ALL_STRATEGIES:
        sub = [t for t in filled if t.get("strategy") == st_key]
        if sub:
            by_strategy[st_key] = {"trades": len(sub), "net_pnl": round(sum(float(t["net_pnl"]) for t in sub), 4)}

    return {
        "metrics": metrics,
        "sim_trades": sim_trades,
        "by_session": by_session,
        "by_strategy": by_strategy,
    }


def run_portfolio_backtest_from_lake(
    lake: LakeStore,
    data_cutoff_utc: Optional[int] = None,
    forward_days: int = 30,
    rebalance_days: int = 7,
    cfg: Optional[WalkForwardConfig] = None,
) -> Dict[str, Any]:
    """
    ローリング・ウォークフォワードで「各時点の因果 OOS 選定 → 次期間 (`rebalance_days`) の実運用ポートフォリオ再現」を
    連鎖実行し、同時保有2枠・日次CB-$1.50・セッション切替・24hクールダウンなしの統合実力値を算出する。
    """
    cfg = cfg or WalkForwardConfig()
    cutoff = int(data_cutoff_utc) if data_cutoff_utc is not None else utc_now_sec()
    total_start = cutoff - (max(1, int(forward_days)) * 86400)
    step_sec = max(1, int(rebalance_days)) * 86400

    hourly_regime_map, pit_active = compute_hourly_regime_series_from_lake(
        lake=lake,
        start_utc=total_start,
        end_utc=cutoff,
        use_point_in_time_regime=cfg.use_point_in_time_regime,
        gil_yield_sec=cfg.gil_yield_sec,
    )

    all_trades: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    cur_t = total_start

    while cur_t < cutoff:
        chunk_end = min(cutoff, cur_t + step_sec)
        wf_res = run_walkforward_from_lake(
            lake=lake,
            data_cutoff_utc=cur_t,
            cfg=cfg,
            persist=False,
        )
        ranked_syms, sym_map = build_portfolio_params_map_from_cells(wf_res["cells"])
        p_res = simulate_portfolio_on_window(
            lake=lake,
            symbol_params_map=sym_map,
            ranked_symbols=ranked_syms,
            hourly_regime_map=hourly_regime_map if pit_active else None,
            win_start_utc=cur_t,
            win_end_utc=chunk_end,
            cfg=cfg,
        )
        all_trades.extend(p_res["sim_trades"])
        chunks.append({
            "start_iso": sec_to_iso(cur_t),
            "end_iso": sec_to_iso(chunk_end),
            "selected_symbols": ranked_syms,
            "trades": p_res["metrics"]["trades"],
            "net_pnl": round(p_res["metrics"]["net_pnl"], 4),
        })
        cur_t = chunk_end

    agg = _compute_metrics(all_trades)
    filled = [t for t in all_trades if t.get("fill_ts_utc") is not None]
    by_session = {
        s_k: {
            "trades": sum(1 for t in filled if t.get("session_at_entry") == s_k),
            "net_pnl": round(sum(float(t["net_pnl"]) for t in filled if t.get("session_at_entry") == s_k), 4),
        }
        for s_k in ("TOKYO", "LONDON", "NY")
    }
    by_strategy = {
        st_k: {
            "trades": sum(1 for t in filled if t.get("strategy") == st_k),
            "net_pnl": round(sum(float(t["net_pnl"]) for t in filled if t.get("strategy") == st_k), 4),
        }
        for st_k in ALL_STRATEGIES
        if any(t.get("strategy") == st_k for t in filled)
    }
    return {
        "metrics": agg,
        "sim_trades": all_trades,
        "chunks": chunks,
        "by_session": by_session,
        "by_strategy": by_strategy,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BingX Data Lake Walk-Forward / Portfolio Backtest")
    parser.add_argument("--portfolio", action="store_true", help="同時2ポジ・24h再現ポートフォリオBTを実行")
    parser.add_argument("--days", type=int, default=30, help="ポートフォリオ前方検証日数 (既定: 30日)")
    parser.add_argument("--rebalance-days", type=int, default=7, help="再選定周期日数 (既定: 7日)")
    args = parser.parse_args()

    lake_inst = LakeStore()
    if args.portfolio:
        res_p = run_portfolio_backtest_from_lake(lake_inst, forward_days=args.days, rebalance_days=args.rebalance_days)
        m_p = res_p["metrics"]
        print(
            f"[Portfolio BT {args.days}d / reb={args.rebalance_days}d] "
            f"Trades={m_p['trades']} | WR={m_p['win_rate']:.1f}% | Net=${m_p['net_pnl']:+.2f} | "
            f"Fees=${m_p['total_fees']:.2f} | EV=${m_p['ev']:+.4f} | PF={m_p['pf']:.2f} | MaxDD=${m_p['max_dd']:.2f}"
        )
        print("  By Session :", res_p["by_session"])
        print("  By Strategy:", res_p["by_strategy"])
    else:
        res_w = run_walkforward_from_lake(lake_inst, persist=False)
        print(res_w["selection"]["reason_text"])
