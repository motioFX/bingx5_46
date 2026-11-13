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

from bingx5_46_3logic import calc_rsi_ema, evaluate_market_regime_3window, make_market_profile_dual_va
from lake_store import (
    TIMEFRAME_SECONDS,
    LakeStore,
    get_git_sha_short,
    sec_to_iso,
    utc_now_sec,
)
from symbol_universe import BENCHMARK_SYMBOL, FIXED_TRADABLE_SYMBOLS, LAKE_UNIVERSE_SYMBOLS, normalize_symbol

JST = timezone(timedelta(hours=9))

LONG_STRATEGIES = ("val_poc", "vah_break", "rsima")
SHORT_STRATEGIES = ("vah_poc", "val_break", "rsima_short")


@dataclass(frozen=True)
class WalkForwardConfig:
    opt_days: int = 90
    eval_days: int = 90
    embargo_bars: int = 0
    timeframes: Tuple[str, ...] = ("5m", "15m")
    fee_maker: float = 0.0002
    fee_taker: float = 0.0006
    position_cost_usdt: float = 15.0
    sl_cooldown_hours: float = 24.0
    hard_sl_pct: float = 0.030
    min_sl_pct: float = 0.0020
    timeout_minutes: int = 240
    max_hold_minutes: int = 480
    enforce_vol_surge: bool = True
    vol_min_ratio: float = 1.5
    enforce_oi_fr: bool = True
    oi_missing_policy: str = "when_available"  # "when_available" | "strict_zero_fill"
    enforce_session_mask: bool = True
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
            for mp in (100, 150, 200)
            for tr in (0.55, 0.70, 0.85)
        ]
    if strategy in ("vah_break", "val_break"):
        return [
            {"mp": mp, "target_ratio": tr}
            for mp in (100, 150, 200)
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
            "whale_snapshot": {"source": "lake_empty"},
        }

    res = evaluate_market_regime_3window(symbol_1h_dfs)
    res["excluded_symbols"] = {}
    res["whale_snapshot"] = {"source": "lake_offline_1h"}
    return res


def _compute_causal_features(
    df_tf: pd.DataFrame,
    timeframe: str,
    cfg: WalkForwardConfig,
    needed_mps: Sequence[int] = (100, 150, 200),
) -> Dict[str, Any]:
    """
    指定足種 DataFrame (すでに `ts_utc + tf_sec <= end_cutoff` 済) に対して、
    完全に因果的な配列（必要なMPのみ, RSI-EMA, ATR_14, range_high/low, swing10, precision, session）を事前計算する。
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

    # ATR(14) - strictly causal
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_14 = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy(dtype=np.float64)

    # 直近レンジ高安 (24本 = 5mなら2h, 15mなら6h, 1hなら24h, 4hなら4d) - 因果的
    s_h = pd.Series(h)
    s_l = pd.Series(l)
    range_high_24 = s_h.rolling(24, min_periods=1).max().to_numpy(dtype=np.float64)
    range_low_24 = s_l.rolling(24, min_periods=1).min().to_numpy(dtype=np.float64)

    # 直近10本 スイング高安 (SL計算用)
    swing_high_10 = s_h.rolling(10, min_periods=1).max().to_numpy(dtype=np.float64)
    swing_low_10 = s_l.rolling(10, min_periods=1).min().to_numpy(dtype=np.float64)

    # Volume Ratio (直前20本移動平均は shift(1) で当足除外＝完全因果)
    v_ma20 = pd.Series(v).rolling(20, min_periods=5).mean().shift(1).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where((v_ma20 > 0) & np.isfinite(v_ma20), v / v_ma20, 1.0)

    # OI & FR (既に causal merge_asof 済)
    oi_chg = df_tf["oi_chg_4h"].to_numpy(dtype=np.float64) if "oi_chg_4h" in df_tf.columns else np.full(n, np.nan)
    fr = df_tf["funding_rate"].to_numpy(dtype=np.float64) if "funding_rate" in df_tf.columns else np.full(n, np.nan)

    # Session mask (JST時刻)
    dt_jst = pd.to_datetime(ts_open, unit="s", utc=True).tz_convert(JST)
    hrs_jst = dt_jst.hour.to_numpy(dtype=np.int32)
    tokyo_mask = (hrs_jst >= 9) & (hrs_jst < 16)
    london_mask = (hrs_jst >= 16) & (hrs_jst < 22)

    # RSI(9) & RSI-EMA(7)
    rsi_s, rsi_ma_s = calc_rsi_ema(pd.Series(c), rsi_period=9, ma_period=7)
    rsi = rsi_s.to_numpy(dtype=np.float64)
    rsi_ma = rsi_ma_s.to_numpy(dtype=np.float64)

    # Market Profile (必要な period のみ算出 / 5m=24ステップ(2h更新)等で高速因果算出)
    mp_step = {"5m": 24, "15m": 8, "1h": 2, "4h": 1}.get(timeframe, 4)
    mp_cache: Dict[int, Dict[str, np.ndarray]] = {}
    for mp_p in sorted(set(int(x) for x in needed_mps)):
        mp_res = make_market_profile_dual_va(df_tf, period=mp_p, num_bins=50, step=mp_step, use_decay=True)
        mp_cache[mp_p] = {
            "val": np.asarray(mp_res["val_70"], dtype=np.float64),
            "vah": np.asarray(mp_res["vah_70"], dtype=np.float64),
            "poc": np.asarray(mp_res["poc"], dtype=np.float64),
        }

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
                # 過去欠損期間 (collector開始前) は強制ゼロ埋めせず、観測値がある場合のみ厳格判定
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
    """事前計算済み因果特徴量から、シグナル発火配列と各足の基準TP/SL・伸び切り情報を生成する。"""
    n = feat["n"]
    tf = feat["timeframe"]
    c = feat["close"]
    side = strategy_side(strategy)
    is_brk = is_breakout_strategy(strategy)

    sig = np.zeros(n, dtype=bool)
    p_mask = feat["precision_masks"][(side, is_brk)]

    if cfg.enforce_session_mask and tf in ("5m", "15m"):
        sess_mask = feat["london_mask"] if is_brk else feat["tokyo_mask"]
    else:
        sess_mask = np.ones(n, dtype=bool)

    base_mask = p_mask & sess_mask
    # 最初の100本（またはMP期間）まではウォームアップ不足のため除外
    warmup_bars = int(params.get("mp", 30)) if "mp" in params else 30

    if strategy in ("val_poc", "vah_poc", "vah_break", "val_break"):
        mp_p = int(params["mp"])
        mp = feat["mp_cache"][mp_p]
        val = mp["val"]
        vah = mp["vah"]
        poc = mp["poc"]
        valid_mp = (val > 0) & (vah > val) & (poc > 0)
        prev_c = np.roll(c, 1)
        prev_vah = np.roll(vah, 1)
        prev_val = np.roll(val, 1)
        prev_c[0] = c[0]

        if strategy == "val_poc":
            # レンジ内回帰ロング: 確定足が VAL 以下へ押し込み、かつ極端な下抜け(-3%超)でない
            cond = valid_mp & (c <= val * 1.001) & (c >= val * 0.970)
        elif strategy == "vah_poc":
            # レンジ内回帰ショート: 確定足が VAH 以上へ吹き上げ、かつ極端な上抜け(+3%超)でない
            cond = valid_mp & (c >= vah * 0.999) & (c <= vah * 1.030)
        elif strategy == "vah_break":
            # VAH 上抜けブレイク (前足VAH以下 -> 今足VAH超)
            cond = valid_mp & (prev_c <= prev_vah) & (c > vah) & (c < vah * 1.04)
        else:  # val_break
            # VAL 下抜けブレイク (前足VAL以上 -> 今足VAL割れ)
            cond = valid_mp & (prev_c >= prev_val) & (c < val) & (c > val * 0.96)

        sig = cond & base_mask
        va_upper = vah
        va_lower = val
        poc_arr = poc
    else:
        # RSIMA / RSIMA_SHORT
        rsi = feat["rsi"]
        rsi_ma = feat["rsi_ma"]
        mp_ref = feat["mp_cache"].get(150)
        if mp_ref is None and feat["mp_cache"]:
            mp_ref = next(iter(feat["mp_cache"].values()))
        if mp_ref is not None:
            va_upper = mp_ref["vah"]
            va_lower = mp_ref["val"]
            poc_arr = mp_ref["poc"]
        else:
            # VP 未計算時は直近24本レンジを VA フォールバックとし、計算コストをゼロにする
            va_upper = feat["range_high_24"]
            va_lower = feat["range_low_24"]
            poc_arr = (va_upper + va_lower) * 0.5

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

    return {
        "sig": sig,
        "va_upper": va_upper,
        "va_lower": va_lower,
        "poc": poc_arr,
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
) -> List[Dict[str, Any]]:
    """
    指定窓 `[win_start_utc, win_end_utc]` 上で 5m 足ベースの厳密 Maker 指値追随・
    シグナル終了キャンセル・保守的 SL 先行決済・0.08% 手数料控除を実行する。
    """
    if df_5m.empty or feat_tf["n"] == 0:
        return []

    side = strategy_side(strategy)
    is_brk = is_breakout_strategy(strategy)
    tf = feat_tf["timeframe"]
    tf_sec = feat_tf["tf_sec"]

    ts5 = df_5m["ts_utc"].to_numpy(dtype=np.int64)
    o5 = df_5m["open"].to_numpy(dtype=np.float64)
    h5 = df_5m["high"].to_numpy(dtype=np.float64)
    l5 = df_5m["low"].to_numpy(dtype=np.float64)
    c5 = df_5m["close"].to_numpy(dtype=np.float64)
    n5 = len(ts5)

    # 窓上限以降の 5m 足は物理遮断
    valid_5m_end = int(np.searchsorted(ts5 + 300, win_end_utc, side="right"))
    if valid_5m_end <= 0:
        return []

    sig = sig_info["sig"]
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
    # 5m タイムスタンプ -> 最新確定済み strategy-tf インデックスの因果マップ (`ts_close_tf <= ts5 + 300`)
    # 5m bar k の確定時点 (= ts5[k] + 300) 時点で参照可能な最新 strategy-tf バー:
    tf_at_5m_close = np.searchsorted(ts_close_tf, ts5[:valid_5m_end] + 300, side="right") - 1

    cooldown_sec = int(cfg.sl_cooldown_hours * 3600)
    cooldown_until_utc = 0
    trades: List[Dict[str, Any]] = []

    # 戦略足インデックス i のスキャン: `win_start_utc <= ts_close_tf[i] < win_end_utc`
    i_start = int(np.searchsorted(ts_close_tf, win_start_utc, side="left"))
    i_end = int(np.searchsorted(ts_close_tf, win_end_utc, side="left"))
    next_allowed_order_utc = win_start_utc
    seq_no = 0

    for i in range(i_start, i_end):
        if not sig[i]:
            continue
        sig_close_utc = int(ts_close_tf[i])
        if sig_close_utc < cooldown_until_utc or sig_close_utc < next_allowed_order_utc:
            continue

        # 次 5m 足 (t_order >= sig_close_utc) を検索
        m_start = int(np.searchsorted(ts5[:valid_5m_end], sig_close_utc, side="left"))
        if m_start >= valid_5m_end:
            break

        # 実ボット (bingx5_46_2api) 準拠: シグナル後の 4〜5 分待機窓 (`5m` 1本分, またはシグナル継続時次足追随)
        # 5m 戦略でシグナルが次足も継続なら最大 2 本追随、そうでなければ即 CANCEL_SIGNAL_END
        sig_px = float(c_tf[i])
        if sig_px <= 0:
            continue

        # シグナル確定時点の因果的伸び切り指標 (base: VA境界 & 直近24本高安)
        atr_val = float(atr_tf[i]) if atr_tf[i] > 0 else (sig_px * 0.005)
        va_u = float(vah_tf[i]) if math.isfinite(vah_tf[i]) and vah_tf[i] > 0 else sig_px
        va_l = float(val_tf[i]) if math.isfinite(val_tf[i]) and val_tf[i] > 0 else sig_px
        rh = float(rhigh_tf[i]) if math.isfinite(rhigh_tf[i]) and rhigh_tf[i] > 0 else sig_px
        rl = float(rlow_tf[i]) if math.isfinite(rlow_tf[i]) and rlow_tf[i] > 0 else sig_px

        if side == "long":
            # ロング時: 直近レンジ安値ベース (or VAHブレイク時はVAH超え) からの上昇伸び切り
            ref_base = va_u if is_brk else rl
            ext_pct = ((sig_px - ref_base) / ref_base * 100.0) if ref_base > 0 else 0.0
            ext_atr = (sig_px - ref_base) / atr_val if atr_val > 0 else 0.0
        else:
            ref_base = va_l if is_brk else rh
            ext_pct = ((ref_base - sig_px) / ref_base * 100.0) if ref_base > 0 else 0.0
            ext_atr = (ref_base - sig_px) / atr_val if atr_val > 0 else 0.0

        # Maker 指値配置 ＆ 追随ループ (成行約定なし)
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
                # 次の確定足でもシグナルが継続している場合のみ最新気配に再配置 (追随)
                tf_idx_prev = int(tf_at_5m_close[m - 1])
                if tf_idx_prev < 0 or not sig[tf_idx_prev]:
                    cancel_ts = int(ts5[m])
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
                }
            )
            next_allowed_order_utc = int(cancel_ts or (first_order_ts + 300))
            continue

        # ===== 約定後の利確・損切り水準確定 =====
        fill_ts = int(ts5[filled_bar])
        tr_ratio = float(params.get("target_ratio", 0.70))
        poc_lvl = float(poc_tf[i]) if math.isfinite(poc_tf[i]) and poc_tf[i] > 0 else fill_px
        swing_lo = float(sl10_tf[i]) if math.isfinite(sl10_tf[i]) and sl10_tf[i] > 0 else fill_px * (1.0 - cfg.hard_sl_pct)
        swing_hi = float(sh10_tf[i]) if math.isfinite(sh10_tf[i]) and sh10_tf[i] > 0 else fill_px * (1.0 + cfg.hard_sl_pct)

        tp_px: Optional[float] = None
        if strategy == "val_poc":
            tp_cand = va_l + (va_u - va_l) * tr_ratio
            tp_px = max(tp_cand, fill_px * 1.004)
            sl_confirm_px = min(swing_lo, fill_px * (1.0 - cfg.min_sl_pct))
            hard_sl_px = fill_px * (1.0 - cfg.hard_sl_pct)
        elif strategy == "vah_poc":
            tp_cand = va_u - (va_u - va_l) * tr_ratio
            tp_px = min(tp_cand, fill_px * 0.996)
            sl_confirm_px = max(swing_hi, fill_px * (1.0 + cfg.min_sl_pct))
            hard_sl_px = fill_px * (1.0 + cfg.hard_sl_pct)
        elif strategy == "vah_break":
            tp_cand = va_u + (va_u - va_l) * tr_ratio
            tp_px = max(tp_cand, fill_px * 1.008)
            tp_dist = max(tp_px - fill_px, fill_px * 0.008)
            # RR 1:2 保証防衛SL (POC割れ または 1/2 TP幅の狭い方)
            poc_sl = min(poc_lvl, fill_px * 0.995)
            sl_confirm_px = max(poc_sl, fill_px - tp_dist * 0.5)
            hard_sl_px = max(sl_confirm_px, fill_px * (1.0 - cfg.hard_sl_pct))
        elif strategy == "val_break":
            tp_cand = va_l - (va_u - va_l) * tr_ratio
            tp_px = min(tp_cand, fill_px * 0.992)
            tp_dist = max(fill_px - tp_px, fill_px * 0.008)
            poc_sl = max(poc_lvl, fill_px * 1.005)
            sl_confirm_px = min(poc_sl, fill_px + tp_dist * 0.5)
            hard_sl_px = min(sl_confirm_px, fill_px * (1.0 + cfg.hard_sl_pct))
        elif strategy == "rsima":
            # RSI過熱利確は bars>=filled_bar+1 の確定足で判定。急変防止用に RR1:2 相当 TP上限も併用
            sl_confirm_px = min(swing_lo, fill_px * (1.0 - cfg.min_sl_pct))
            hard_sl_px = fill_px * (1.0 - cfg.hard_sl_pct)
            l_cp = float(params["l_cp"])
        else:  # rsima_short
            sl_confirm_px = max(swing_hi, fill_px * (1.0 + cfg.min_sl_pct))
            hard_sl_px = fill_px * (1.0 + cfg.hard_sl_pct)
            s_cp = float(params["s_cp"])

        exit_bar = -1
        exit_px = 0.0
        exit_reason = ""
        exit_ts = 0

        max_hold_bars_5m = max(1, cfg.max_hold_minutes // 5)
        timeout_bars_5m = max(1, cfg.timeout_minutes // 5)
        k_limit = min(valid_5m_end, filled_bar + max_hold_bars_5m + 1)

        for k in range(filled_bar, k_limit):
            is_fill_bar = (k == filled_bar)
            # 1. 保守的: ハードSL (またはブレイクアウトPOC/RRストップ) を利確より常に先行判定
            if side == "long":
                if l5[k] <= hard_sl_px:
                    exit_bar = k
                    exit_px = hard_sl_px
                    exit_reason = "HARD_SL" if not is_brk else "POC_RR_SL"
                    exit_ts = int(ts5[k]) + 300
                    break
            else:
                if h5[k] >= hard_sl_px:
                    exit_bar = k
                    exit_px = hard_sl_px
                    exit_reason = "HARD_SL" if not is_brk else "POC_RR_SL"
                    exit_ts = int(ts5[k]) + 300
                    break

            # 約定当足では未知の足内順序による偽TP利確を禁止
            if not is_fill_bar:
                # 2. 確定足終値スイングSL判定
                if side == "long" and c5[k] < sl_confirm_px:
                    exit_bar = k
                    exit_px = float(c5[k])
                    exit_reason = "SWING_SL_CLOSE" if not is_brk else "POC_SL_CLOSE"
                    exit_ts = int(ts5[k]) + 300
                    break
                if side == "short" and c5[k] > sl_confirm_px:
                    exit_bar = k
                    exit_px = float(c5[k])
                    exit_reason = "SWING_SL_CLOSE" if not is_brk else "POC_SL_CLOSE"
                    exit_ts = int(ts5[k]) + 300
                    break

                # 3. 目標 TP / RSI 利確判定
                if tp_px is not None:
                    if side == "long" and h5[k] >= tp_px:
                        exit_bar = k
                        exit_px = tp_px
                        exit_reason = "TARGET_TP"
                        exit_ts = int(ts5[k]) + 300
                        break
                    if side == "short" and l5[k] <= tp_px:
                        exit_bar = k
                        exit_px = tp_px
                        exit_reason = "TARGET_TP"
                        exit_ts = int(ts5[k]) + 300
                        break
                else:
                    # RSIMA 利確: 採用足確定時 (または5m終値) のRSI到達＋手数料以上含み益
                    tf_k = int(tf_at_5m_close[k])
                    if tf_k >= 0 and (int(ts5[k]) + 300 == int(ts_close_tf[tf_k]) or tf == "5m"):
                        cur_rsi = float(rsi_tf[tf_k])
                        if side == "long" and cur_rsi > l_cp and c5[k] > fill_px * 1.0016:
                            exit_bar = k
                            exit_px = float(c5[k])
                            exit_reason = "RSI_TP"
                            exit_ts = int(ts5[k]) + 300
                            break
                        if side == "short" and cur_rsi < s_cp and c5[k] < fill_px * 0.9984:
                            exit_bar = k
                            exit_px = float(c5[k])
                            exit_reason = "RSI_TP"
                            exit_ts = int(ts5[k]) + 300
                            break

                # 4. 動的タイムアウト撤退 (4h経過時に含み損なら微損撤退)
                bars_held = k - filled_bar
                if bars_held == timeout_bars_5m:
                    in_loss = (c5[k] <= fill_px) if side == "long" else (c5[k] >= fill_px)
                    if in_loss:
                        exit_bar = k
                        exit_px = float(c5[k])
                        exit_reason = "TIMEOUT_LOSS"
                        exit_ts = int(ts5[k]) + 300
                        break

        if exit_bar < 0:
            last_k = min(k_limit - 1, valid_5m_end - 1)
            exit_bar = last_k
            exit_px = float(c5[last_k])
            exit_ts = int(ts5[last_k]) + 300
            exit_reason = "WINDOW_END_FLATTEN" if last_k == valid_5m_end - 1 else "MAX_HOLD_CLOSE"

        # 手数料計算: 往復 0.08% (Maker 0.02% + Taker 0.06%)
        entry_fee = cfg.position_cost_usdt * cfg.fee_maker
        exit_val = qty * exit_px
        exit_fee = exit_val * cfg.fee_taker
        if side == "long":
            gross_pnl = (exit_px - fill_px) * qty
        else:
            gross_pnl = (fill_px - exit_px) * qty
        net_pnl = gross_pnl - entry_fee - exit_fee

        is_sl_exit = ("SL" in exit_reason) or ("TIMEOUT" in exit_reason)
        if is_sl_exit:
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


def select_eval_winner(cells: Sequence[Dict[str, Any]], cfg: WalkForwardConfig) -> Optional[Dict[str, Any]]:
    """
    評価窓 (OOS) 成績のみから当日の勝者1件を決定論的に選出する。
    `QUALIFIED` (n>=30) を `PROVISIONAL` (10<=n<30) より優先し、
    同一格付け内では (eval_ev, eval_pf, eval_net_pnl, eval_trades) の降順で決定する。
    """
    eligible = [c for c in cells if c["eval_grade"] in ("QUALIFIED", "PROVISIONAL") and c["eval_ev"] > 0.0]
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

    if allowed_strategies_override is not None:
        allowed_strategies = [str(s) for s in allowed_strategies_override if str(s) in (LONG_STRATEGIES + SHORT_STRATEGIES)]
    else:
        raw_allowed = regime_info.get("allowed_strategies", [])
        allowed_strategies = [str(s) for s in raw_allowed if str(s) in (LONG_STRATEGIES + SHORT_STRATEGIES)]

    rec_syms = regime_info.get("recommended_symbols")
    if symbols is not None:
        target_symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s) != BENCHMARK_SYMBOL]
    elif rec_syms:
        target_symbols = [normalize_symbol(s) for s in rec_syms if normalize_symbol(s) != BENCHMARK_SYMBOL]
    else:
        target_symbols = list(FIXED_TRADABLE_SYMBOLS)

    excluded_syms = set(normalize_symbol(s) for s in (regime_info.get("excluded_symbols") or {}).keys())
    target_symbols = [s for s in target_symbols if s not in excluded_syms]

    lake.upsert_regime_snapshot(
        {
            "cutoff_utc": cutoff,
            "cutoff_iso": sec_to_iso(cutoff),
            "overall_regime": overall_regime,
            "regime_pattern": str(regime_info.get("regime_pattern", "")),
            "strength_tier": strength_tier,
            "net_score": float(strength_obj.get("net_score", 0.0) or 0.0),
            "mean_buy_votes": float(strength_obj.get("mean_buy_votes", 0.0) or 0.0),
            "mean_sell_votes": float(strength_obj.get("mean_sell_votes", 0.0) or 0.0),
            "allowed_strategies": allowed_strategies,
            "recommended_symbols": target_symbols,
            "excluded_symbols": regime_info.get("excluded_symbols", {}),
            "whale_snapshot": regime_info.get("whale_snapshot", {}),
        }
    )

    cfg_json = json.dumps(cfg.to_dict(), sort_keys=True, ensure_ascii=False)
    if run_id is None:
        id_seed = f"{cutoff}|{cfg_json}|{overall_regime}|{','.join(sorted(allowed_strategies))}|{','.join(sorted(target_symbols))}"
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
        "allowed_strategies_json": json.dumps(allowed_strategies, ensure_ascii=False),
        "fee_maker": cfg.fee_maker,
        "fee_taker": cfg.fee_taker,
        "code_git_sha": get_git_sha_short(),
        "config_json": cfg_json,
    }

    # ウォームアップ用余白は個別最適化/評価ループ側で足種ごとに最小限 (max(2d, 220*tf)) を厳密切り出しする
    has_vp_strat = any(s in ("val_poc", "vah_poc", "vah_break", "val_break") for s in allowed_strategies)
    opt_mps: Tuple[int, ...] = (100, 150, 200) if has_vp_strat else ()

    cell_rows: List[Dict[str, Any]] = []
    sim_trade_rows: List[Dict[str, Any]] = []

    if allowed_strategies and target_symbols:
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

                # Stage 1: 全許可戦略を最適化窓のみで探索し、Stage 2 に渡すパラメータと必要な MP のみを特定
                frozen_choices: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
                eval_needed_mps = set()
                for strat in allowed_strategies:
                    grid = get_param_grid(strat)
                    best_opt_score = (-1e18, -1e18, -1e18)
                    best_params: Optional[Dict[str, Any]] = None
                    best_opt_metrics: Optional[Dict[str, Any]] = None

                    for params in grid:
                        sig_opt = _generate_signal_array(feat_opt, strat, params, cfg)
                        opt_trades_list = _simulate_window(
                            feat_opt, sig_opt, df_5m_opt, sym, strat, params,
                            window_type="OPT", win_start_utc=opt_start_utc, win_end_utc=opt_end_utc, cfg=cfg
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

                # Stage 2: 凍結パラメータが必要とする MP のみを計算して OOS 評価
                feat_eval = _compute_causal_features(df_tf_eval, tf, cfg, needed_mps=sorted(eval_needed_mps))

                for strat, best_params, best_opt_metrics in frozen_choices:
                    sig_eval = _generate_signal_array(feat_eval, strat, best_params, cfg)
                    eval_trades_list = _simulate_window(
                        feat_eval, sig_eval, df_5m_eval, sym, strat, best_params,
                        window_type="EVAL", win_start_utc=eval_start_utc, win_end_utc=eval_end_utc, cfg=cfg
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

    winner = select_eval_winner(cell_rows, cfg)
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
        lake.save_backtest_bundle(run_row, cell_rows, sim_trade_rows, selection_row=selection_row)

    return {
        "run": run_row,
        "regime_info": regime_info,
        "cells": cell_rows,
        "sim_trades": sim_trade_rows,
        "winner": winner,
        "selection": selection_row,
    }
