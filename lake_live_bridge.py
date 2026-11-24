# -*- coding: utf-8 -*-
"""
実運用ループ (`bingx5_46_1futures_limit.py`) ⇄ データレイク基盤 (`lake_store`, `lake_backtest_engine`) 接続ブリッジ

機能:
1. 毎時スクリーニング時に `run_walkforward_from_lake` を実行し、評価窓最良の1件 (`winner`) または
   基準未達時 `NO_CANDIDATE` (空リスト) とその選定根拠ログを返す。
2. 勝者パラメータを `bingx5_46_1futures_limit.py` 互換の `symbol_params_map` 形式へ変換し、
   `selection_id` / `run_id` を受け渡す。
3. 実エントリー時の因果的伸び切り指標 (`ext_pct`, `ext_atr`) を算出する。
4. 実トレード決済時に `live_trades` へ自動書き戻し、`daily_selections.status = 'EXECUTED'` へ更新する。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from bingx5_46_3logic import make_market_profile_dual_va
from lake_backtest_engine import (
    WalkForwardConfig,
    compute_regime_from_lake,
    is_breakout_strategy,
    run_walkforward_from_lake,
    strategy_side,
)
from lake_store import TIMEFRAME_SECONDS, LakeStore, sec_to_iso, utc_now_sec
from strategy_f7_two_pillar import (
    F7_LONG_MP_PERIOD,
    F7_LONG_STRATEGY,
    F7_LONG_TARGET_RATIO,
    F7_LONG_TIMEFRAME,
    F7_MODE_NAME,
    F7_SHORT_STRATEGY,
    F7_SHORT_TIMEFRAME,
    check_f7_no_exhaustion,
    compute_ret14d_from_1h_close,
    evaluate_h3b_wf90d_gate_from_lake,
    is_eu_session_jst,
)
from symbol_universe import (
    BENCHMARK_SYMBOL,
    EXCLUDED_TRADE_MEGA_CAPS,
    LAKE_UNIVERSE_SYMBOLS,
    TRADABLE_MID_CAP_SYMBOLS,
    normalize_symbol,
)

JST = timezone(timedelta(hours=9))

_TF_TO_INTERVAL_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
_INTERVAL_TO_TF = {
    5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h",
    "5": "5m", "15": "15m", "30": "30m", "60": "1h", "240": "4h",
}


def interval_to_timeframe(interval: Any) -> str:
    return _INTERVAL_TO_TF.get(interval, "5m")


def compute_live_entry_extension(
    symbol: str,
    strategy: str,
    side: str,
    timeframe_or_interval: Any,
    ref_price: float,
    cutoff_utc: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """エントリー直前確定足までを使って伸び切り指標 (`ext_pct`, `ext_atr`) を因果算出する。"""
    try:
        lake = LakeStore(db_path=db_path)
        tf = timeframe_or_interval if timeframe_or_interval in TIMEFRAME_SECONDS else interval_to_timeframe(timeframe_or_interval)
        cutoff = int(cutoff_utc) if cutoff_utc is not None else utc_now_sec()
        df = lake.load_candles(normalize_symbol(symbol), tf, cutoff_utc=cutoff, limit=250)
        if df.empty or len(df) < 30 or ref_price <= 0:
            return None, None

        h = df["high"].to_numpy(dtype=np.float64)
        l = df["low"].to_numpy(dtype=np.float64)
        c = df["close"].to_numpy(dtype=np.float64)
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr_14 = float(pd.Series(tr).rolling(14, min_periods=1).mean().iloc[-1])
        if atr_14 <= 0:
            atr_14 = float(ref_price) * 0.005

        range_high = float(pd.Series(h).tail(24).max())
        range_low = float(pd.Series(l).tail(24).min())

        mp_res = make_market_profile_dual_va(df, period=min(150, len(df)), num_bins=50, step=1, use_decay=True)
        va_u = float(mp_res["vah_70"][-1]) if len(mp_res["vah_70"]) > 0 else float(ref_price)
        va_l = float(mp_res["val_70"][-1]) if len(mp_res["val_70"]) > 0 else float(ref_price)

        is_brk = is_breakout_strategy(str(strategy).lower())
        side_l = str(side).lower()
        if side_l == "long":
            base = va_u if (is_brk and va_u > 0) else range_low
            ext_pct = ((ref_price - base) / base * 100.0) if base > 0 else 0.0
            ext_atr = (ref_price - base) / atr_14 if atr_14 > 0 else 0.0
        else:
            base = va_l if (is_brk and va_l > 0) else range_high
            ext_pct = ((base - ref_price) / base * 100.0) if base > 0 else 0.0
            ext_atr = (base - ref_price) / atr_14 if atr_14 > 0 else 0.0

        return round(float(ext_pct), 4), round(float(ext_atr), 4)
    except Exception:
        return None, None


def _is_cell_qualified(c: Optional[Dict[str, Any]]) -> bool:
    if not c:
        return False
    return (
        str(c.get("eval_grade", "")) in ("QUALIFIED", "PROVISIONAL")
        and float(c.get("eval_ev", 0.0) or 0.0) > 0.0
        and float(c.get("eval_net_pnl", 0.0) or 0.0) > 0.0
    )


def _cell_sort_key(c: Dict[str, Any]) -> Tuple[int, float, float, float, int, str]:
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


def _build_sub_spec_from_cell(cell: Dict[str, Any], selection: Dict[str, Any], is_qualified: bool) -> Dict[str, Any]:
    strat = str(cell["strategy"])
    tf = str(cell["timeframe"])
    interval_min = _TF_TO_INTERVAL_MIN.get(tf, 5)
    raw_params = json.loads(cell["params_json"])
    side = str(cell["side"])
    return {
        "strategy": strat,
        "trade_side": side,
        "interval": interval_min,
        "timeframe": tf,
        "mp": int(raw_params.get("mp", 150)),
        "target_ratio": float(raw_params.get("target_ratio", 0.70)),
        "l_ep": float(raw_params.get("l_ep", 40.0)),
        "l_cp": float(raw_params.get("l_cp", 60.0)),
        "s_ep": float(raw_params.get("s_ep", 60.0)),
        "s_cp": float(raw_params.get("s_cp", 40.0)),
        "num_bins": 50,
        "value_area_pct": 0.70,
        "dynamic_mae_stop": -0.030,
        "pnl": float(cell["eval_net_pnl"]),
        "win_rate": float(cell["eval_win_rate"]),
        "trade_count": int(cell["eval_trades"]),
        "max_dd": float(cell["eval_max_dd"]),
        "ev": float(cell["eval_ev"]),
        "pf": float(cell["eval_pf"]),
        "is_qualified": bool(is_qualified),
        "lake_selection_id": selection["selection_id"],
        "lake_run_id": selection["run_id"],
        "lake_winner_cell_id": cell["cell_id"],
        "lake_eval_grade": cell["eval_grade"],
    }


def build_symbol_params_from_cells(
    sym_cells: List[Dict[str, Any]],
    selection: Dict[str, Any],
    regime_info: Dict[str, Any],
) -> Dict[str, Any]:
    """銘柄内の全評価セルから東京レンジ用・欧州ブレイク用を含む統合パラメータ辞書を構築する。"""
    if not sym_cells:
        return {}

    rng_cells = [c for c in sym_cells if not is_breakout_strategy(str(c["strategy"]))]
    brk_cells = [c for c in sym_cells if is_breakout_strategy(str(c["strategy"]))]

    best_rng = max(rng_cells, key=_cell_sort_key) if rng_cells else None
    best_brk = max(brk_cells, key=_cell_sort_key) if brk_cells else None
    best_overall = max(sym_cells, key=_cell_sort_key)

    is_rng_q = _is_cell_qualified(best_rng)
    is_brk_q = _is_cell_qualified(best_brk)
    is_q = is_rng_q or is_brk_q

    p_dict = _build_sub_spec_from_cell(best_overall, selection, is_q)
    p_dict.update(
        {
            "regime_at_opt": str(regime_info.get("overall_regime", "NEUTRAL")),
            "is_range_qualified": is_rng_q,
            "is_breakout_qualified": is_brk_q,
            "lake_eval_trades": int(best_overall["eval_trades"]),
            "lake_eval_ev": float(best_overall["eval_ev"]),
            "lake_eval_pf": float(best_overall["eval_pf"]),
            "lake_eval_net_pnl": float(best_overall["eval_net_pnl"]),
            "lake_reason_text": selection.get("reason_text", ""),
        }
    )
    if best_rng is not None:
        p_dict["range_params"] = _build_sub_spec_from_cell(best_rng, selection, is_rng_q)
    if best_brk is not None:
        p_dict["breakout_params"] = _build_sub_spec_from_cell(best_brk, selection, is_brk_q)
    return p_dict


def build_live_params_from_winner(winner: Dict[str, Any], selection: Dict[str, Any], regime_info: Dict[str, Any]) -> Dict[str, Any]:
    """データレイク選定勝者を `bingx5_46_1futures_limit.py` の `symbol_params_map` 互換辞書へ変換する。"""
    return build_symbol_params_from_cells([winner], selection, regime_info)


def _ensure_30m_candles_in_lake(lake: LakeStore, cutoff_utc: int) -> int:
    """F7 `val_poc_30m` 用に既存の 5m 足から未生成の 30m 足を決定論的に補完生成する."""
    rebuilt = 0
    for sym in LAKE_UNIVERSE_SYMBOLS:
        try:
            st_5m = lake.get_candle_stats(sym, "5m")
            st_30m = lake.get_candle_stats(sym, "30m")
            cnt_5m = int(st_5m.get("count", 0) or 0)
            cnt_30m = int(st_30m.get("count", 0) or 0)
            if cnt_5m >= 24 and (cnt_30m == 0 or (cnt_5m >= 600 and cnt_30m < max(50, (cnt_5m // 6) - 200))):
                res = lake.rebuild_higher_timeframes_from_5m(sym, cutoff_utc=cutoff_utc, timeframes=("30m",))
                rebuilt += int(res.get("30m", 0) or 0)
        except Exception:
            pass
    return rebuilt


def _compute_f7_ret14d_context(
    lake: LakeStore,
    cutoff_1h: int,
) -> Tuple[float, Dict[str, float]]:
    """BTC および中堅アルト各銘柄の直近14日 (336h) リターンを Lake 確定 1h 足から因果算出する."""
    btc_df = lake.load_candles(normalize_symbol(BENCHMARK_SYMBOL), "1h", cutoff_utc=cutoff_1h, limit=400)
    btc_ret14d = compute_ret14d_from_1h_close(btc_df["close"].to_numpy(dtype=np.float64)) if not btc_df.empty else 0.0
    sym_ret14d_map: Dict[str, float] = {}
    for sym in TRADABLE_MID_CAP_SYMBOLS:
        ns = normalize_symbol(sym)
        df_s = lake.load_candles(ns, "1h", cutoff_utc=cutoff_1h, limit=400)
        sym_ret14d_map[ns] = compute_ret14d_from_1h_close(df_s["close"].to_numpy(dtype=np.float64)) if not df_s.empty else 0.0
    return float(btc_ret14d), sym_ret14d_map


def _count_1h_symbols_in_lake(lake: LakeStore, cutoff_1h: int) -> int:
    start_1h = int(cutoff_1h) - 20 * 86400
    cnt = 0
    for sym in LAKE_UNIVERSE_SYMBOLS:
        df_s = lake.load_candles(normalize_symbol(sym), "1h", cutoff_utc=cutoff_1h, start_utc=start_1h, limit=30)
        if not df_s.empty and len(df_s) >= 24:
            cnt += 1
    return cnt


def ensure_lake_ready_on_startup(history_days: int = 210) -> Dict[str, Any]:
    """
    ボット起動時に Data/lake/bingx_lake.db の存在および過去履歴深度を確認し、
    未構築または120日未満の場合は migrate_to_lake で初期統合・バックフィルを実行する。
    """
    lake = LakeStore()
    st = lake.get_candle_stats("BTC-USDT", "5m")
    span_days = ((st["max_ts_utc"] - st["min_ts_utc"]) / 86400.0) if (st["min_ts_utc"] and st["max_ts_utc"]) else 0.0
    if st["count"] == 0 or span_days < 120.0:
        from migrate_to_lake import run_migration
        res = run_migration(history_days=history_days, sync_live=True)
        _ensure_30m_candles_in_lake(lake, cutoff_utc=utc_now_sec())
        return res

    from candle_5m_store import sync_all_target_symbols_5m
    sync_all_target_symbols_5m(lookback_days=2)
    _ensure_30m_candles_in_lake(lake, cutoff_utc=utc_now_sec())
    try:
        from bingx_oi_collector import collect_and_save_oi_snapshot
        collect_and_save_oi_snapshot(symbols=LAKE_UNIVERSE_SYMBOLS)
    except Exception:
        pass
    return {"status": "synced", "btc_5m_count": st["count"], "span_days": round(span_days, 1)}


def refresh_hourly_regime_for_bot(
    db_path: Optional[Path] = None,
    sync_live: bool = True,
    state_file_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """毎時正時用の軽量地合い判定・反転検知・F7 14d疲れ切り/WF90d更新。

    WF 探索を実行せず直近 5m/1h 確定足のみで 3窓地合いを再評価し、
    `Data/market_state.json` を最新化した上で前回地合いからの反転有無を返す。
    """
    lake = LakeStore(db_path=db_path)
    now_sec = utc_now_sec()
    if sync_live and db_path is None:
        try:
            from candle_5m_store import sync_all_target_symbols_5m
            sync_all_target_symbols_5m(lookback_days=1)
        except Exception:
            pass

    ms_file = Path(state_file_path) if state_file_path is not None else (Path(__file__).resolve().parent / "Data" / "market_state.json")
    prev_regime = ""
    prev_tier = ""
    if ms_file.exists():
        try:
            prev_data = json.loads(ms_file.read_text(encoding="utf-8"))
            if isinstance(prev_data, dict):
                prev_regime = str(prev_data.get("overall_regime", "")).upper()
                prev_tier = str(
                    (prev_data.get("regime_strength") or {}).get("tier", prev_data.get("strength_tier", ""))
                ).upper()
        except Exception:
            pass

    cutoff_1h = (now_sec // 3600) * 3600
    reg_eval = compute_regime_from_lake(lake, data_cutoff_utc=cutoff_1h)
    new_regime = str(reg_eval.get("overall_regime", "NEUTRAL")).upper()
    regime_flipped = bool(prev_regime in ("BUY", "SELL", "NEUTRAL") and new_regime != prev_regime)

    strength_obj = dict(reg_eval.get("regime_strength") or {})
    new_tier = str(strength_obj.get("tier", "BALANCED")).upper()
    tier_changed_ultra_buy = bool(prev_tier and ((prev_tier == "ULTRA_BUY") != (new_tier == "ULTRA_BUY")))
    if "strength_desc" not in strength_obj and strength_obj:
        t_jp = str(strength_obj.get("tier_jp", strength_obj.get("tier", "")))
        net_sc = float(strength_obj.get("net_score", 0.0) or 0.0)
        strength_obj["strength_desc"] = f"【{t_jp} ({net_sc * 100:+.1f}%)】"

    btc_ret14d, sym_ret14d_map = _compute_f7_ret14d_context(lake, cutoff_1h)
    h3b_wf90d = {"paused": False, "window_days": 90, "window_trades": 0, "window_net": 0.0, "window_pf": 0.0, "total_paper_trades": 0}
    if db_path is None and _count_1h_symbols_in_lake(lake, cutoff_1h) >= 15:
        try:
            raw_wf = evaluate_h3b_wf90d_gate_from_lake(lake, cutoff_utc=cutoff_1h)
            h3b_wf90d = {k: v for k, v in raw_wf.items() if not str(k).startswith("_")}
            wf_file = Path(__file__).resolve().parent / "Data" / "h3b_wf90d_state.json"
            wf_file.parent.mkdir(parents=True, exist_ok=True)
            wf_tmp = wf_file.with_suffix(".tmp")
            wf_tmp.write_text(json.dumps(h3b_wf90d, ensure_ascii=False, indent=2), encoding="utf-8")
            wf_tmp.replace(wf_file)
        except Exception:
            pass

    excl_raw = reg_eval.get("excluded_symbols") or {}
    excl_list = list(excl_raw.keys()) if isinstance(excl_raw, dict) else list(excl_raw)
    excl_reasons = excl_raw if isinstance(excl_raw, dict) else {}
    now_iso = datetime.now(timezone.utc).isoformat()
    ms_payload = {
        "overall_regime": new_regime,
        "strength_tier": new_tier,
        "regime_pattern": reg_eval.get("regime_pattern", ""),
        "regime_strength": strength_obj,
        "period_results": reg_eval.get("period_results", {}),
        "allowed_strategies": reg_eval.get("allowed_strategies", []),
        "recommended_symbols": reg_eval.get("recommended_symbols", []),
        "excluded_symbols": excl_list,
        "exclusion_reasons": excl_reasons,
        "btc_ret14d": btc_ret14d,
        "sym_ret14d_map": sym_ret14d_map,
        "h3b_wf90d": h3b_wf90d,
        "is_buy_regime": (new_regime == "BUY"),
        "is_sell_regime": (new_regime == "SELL"),
        "evaluated_at": now_iso,
        "timestamp": now_iso,
    }
    try:
        ms_file.parent.mkdir(parents=True, exist_ok=True)
        ms_tmp = ms_file.with_suffix(".tmp")
        ms_tmp.write_text(json.dumps(ms_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ms_tmp.replace(ms_file)
    except Exception:
        pass

    return {
        "prev_overall_regime": prev_regime,
        "overall_regime": new_regime,
        "prev_strength_tier": prev_tier,
        "strength_tier": new_tier,
        "tier_changed_ultra_buy": tier_changed_ultra_buy,
        "regime_flipped": regime_flipped,
        "regime_eval": reg_eval,
        "btc_ret14d": btc_ret14d,
        "sym_ret14d_map": sym_ret14d_map,
        "h3b_wf90d": h3b_wf90d,
    }


def run_lake_screening_for_bot(
    override_regime_info: Dict[str, Any],
    db_path: Optional[Path] = None,
    wf_cfg: Optional[WalkForwardConfig] = None,
    max_selected_symbols: int = 20,
    excluded_mega_caps: Sequence[str] = EXCLUDED_TRADE_MEGA_CAPS,
) -> Dict[str, Any]:
    """
    `bingx5_46_1futures_limit.py` Phase A/C 毎時スクリーニング用のレイク勝者選定エントリポイント。
    返却:
      selected_symbols: OOS合格中堅アルト全銘柄 (eval_grade in QUALIFIED/PROVISIONAL & EV>0, 上位 max_selected_symbols 件)
      symbol_params_map: {sym: range_params/breakout_params 統合辞書}
      default_best_params: 最上位銘柄の params または None
      selection: daily_selections レコード
      summary_log: Discord/コンソール用の根拠ログ
    """
    lake = LakeStore(db_path=db_path)
    now_sec = utc_now_sec()
    cutoff_utc = (now_sec // 300) * 300  # 直近確定済み 5m 足境界に固定
    cfg = wf_cfg or WalkForwardConfig()

    wf_res = run_walkforward_from_lake(
        lake=lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg,
        override_regime_info=override_regime_info,
        persist=True,
    )
    run_rec = wf_res["run"]
    try:
        lake.export_audit_csvs(latest_run_id=run_rec.get("run_id") if isinstance(run_rec, dict) else None)
    except Exception:
        pass

    winner = wf_res["winner"]
    selection = wf_res["selection"]
    cells = wf_res["cells"]
    cur_regime = wf_res.get("regime_info") or override_regime_info or {}
    cur_allowed = {str(s) for s in cur_regime.get("allowed_strategies", [])}
    raw_excl = cur_regime.get("excluded_symbols") or {}
    cur_excluded = {
        normalize_symbol(str(s))
        for s in (raw_excl.keys() if isinstance(raw_excl, dict) else raw_excl)
    }

    live_cells = [c for c in cells if str(c["strategy"]) in cur_allowed]
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for c in live_cells:
        by_sym.setdefault(normalize_symbol(c["symbol"]), []).append(c)

    mega_cap_set = {normalize_symbol(s) for s in excluded_mega_caps}
    symbol_params_map: Dict[str, Dict[str, Any]] = {}
    best_cell_by_sym: Dict[str, Dict[str, Any]] = {}
    qualified_syms: List[str] = []

    for sym, sym_cells in by_sym.items():
        p_dict = build_symbol_params_from_cells(sym_cells, selection, override_regime_info)
        symbol_params_map[sym] = p_dict
        best_cell_by_sym[sym] = max(sym_cells, key=_cell_sort_key)
        if p_dict.get("is_qualified") and sym not in mega_cap_set and sym not in cur_excluded:
            qualified_syms.append(sym)

    qualified_syms.sort(key=lambda s: _cell_sort_key(best_cell_by_sym[s]), reverse=True)
    selected_symbols = qualified_syms[: max(1, int(max_selected_symbols))] if qualified_syms else []

    if selected_symbols:
        default_best_params = dict(symbol_params_map[selected_symbols[0]])
    elif winner is not None:
        w_sym = normalize_symbol(winner["symbol"])
        default_best_params = dict(symbol_params_map.get(w_sym) or build_live_params_from_winner(winner, selection, override_regime_info))
    else:
        default_best_params = None

    # 上位5件 (参考出力: 現時点許可戦略のセル優先、なければ全評価セル)
    top5_pool = live_cells if live_cells else cells
    top5 = sorted(
        [c for c in top5_pool if c["eval_trades"] > 0],
        key=_cell_sort_key,
        reverse=True,
    )[:5]

    top5_lines = []
    for idx, c in enumerate(top5, 1):
        top5_lines.append(
            f"   {idx}. {c['symbol']} {c['strategy'].upper()}({c['timeframe']}) [{c['eval_grade']}] "
            f"n={c['eval_trades']} WR={c['eval_win_rate']:.1f}% EV=${c['eval_ev']:+.4f} Net=${c['eval_net_pnl']:+.2f} PF={c['eval_pf']:.2f}"
        )

    tokyo_q_cnt = sum(1 for s in selected_symbols if symbol_params_map[s].get("is_range_qualified"))
    london_q_cnt = sum(1 for s in selected_symbols if symbol_params_map[s].get("is_breakout_qualified"))
    monitored_str = ", ".join(s.replace("-USDT", "") for s in selected_symbols) if selected_symbols else "なし"

    ext_b = {}
    if winner is not None:
        try:
            ext_b = json.loads(winner.get("eval_ext_stats_json") or "{}").get("buckets_atr", {})
        except Exception:
            ext_b = {}
    ext_str = ""
    if ext_b:
        parts = []
        for k, lbl in (("lt_0.5_atr", "<0.5ATR"), ("0.5_to_1.5_atr", "0.5-1.5ATR"), ("ge_1.5_atr", ">=1.5ATR")):
            b = ext_b.get(k, {})
            if b.get("n", 0) > 0:
                parts.append(f"{lbl}:n={b['n']}/EV=${b['ev']:+.4f}")
        if parts:
            ext_str = "\n   └ 伸び切り別EV: " + " | ".join(parts)

    summary_log = (
        f"🏦 **【データレイク・ウォークフォワード選定結果】**\n"
        f"   ├ Run ID: `{run_rec['run_id']}` | Selection ID: `{selection['selection_id']}`\n"
        f"   ├ 窓分離: Opt `{sec_to_iso(run_rec['opt_start_utc'])[:10]}..{sec_to_iso(run_rec['opt_end_utc'])[:10]}` "
        f"→ Eval(OOS) `{sec_to_iso(run_rec['eval_start_utc'])[:10]}..{sec_to_iso(run_rec['eval_end_utc'])[:10]}`\n"
        f"   ├ 監視採用銘柄数: **{len(selected_symbols)}銘柄** (🗼東京レンジ適格: {tokyo_q_cnt} / 🏰欧州ブレイク適格: {london_q_cnt}) ➔ `{monitored_str}`\n"
        f"   ├ 結論: **{selection['reason_text']}**{ext_str}"
    )
    if top5_lines:
        summary_log += "\n   └ 評価窓OOS上位:\n" + "\n".join(top5_lines)

    return {
        "selected_symbols": selected_symbols,
        "symbol_params_map": symbol_params_map,
        "default_best_params": default_best_params,
        "winner": winner,
        "selection": selection,
        "wf_res": wf_res,
        "summary_log": summary_log,
    }


def run_f7_screening_for_bot(
    override_regime_info: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    max_selected_symbols: int = 20,
    excluded_mega_caps: Sequence[str] = EXCLUDED_TRADE_MEGA_CAPS,
    wf90d_state_path: Optional[Path] = None,
    gil_yield_sec: float = 0.0,
) -> Dict[str, Any]:
    """F7 Two-Pillar 体系用のスクリーニング・WF90d ゲート評価・全18中堅アルトマップ構築."""
    lake = LakeStore(db_path=db_path)
    now_sec = utc_now_sec()
    cutoff_1h = (now_sec // 3600) * 3600
    _ensure_30m_candles_in_lake(lake, cutoff_utc=now_sec)

    cur_regime = dict(override_regime_info) if override_regime_info else compute_regime_from_lake(lake, data_cutoff_utc=cutoff_1h)
    overall = str(cur_regime.get("overall_regime", "NEUTRAL")).upper()
    strength_obj = dict(cur_regime.get("regime_strength") or {})
    tier = str(strength_obj.get("tier", "BALANCED")).upper()
    raw_excl = cur_regime.get("excluded_symbols") or {}
    cur_excluded = {
        normalize_symbol(str(s))
        for s in (raw_excl.keys() if isinstance(raw_excl, dict) else raw_excl)
    }

    btc_ret14d, sym_ret14d_map = _compute_f7_ret14d_context(lake, cutoff_1h)
    raw_wf90d = evaluate_h3b_wf90d_gate_from_lake(lake, cutoff_utc=cutoff_1h, gil_yield_sec=gil_yield_sec)
    h3b_wf90d = {k: v for k, v in raw_wf90d.items() if not str(k).startswith("_")}

    wf_out_file = (
        Path(wf90d_state_path)
        if wf90d_state_path is not None
        else (Path(__file__).resolve().parent / "Data" / "h3b_wf90d_state.json" if db_path is None else None)
    )
    if wf_out_file is not None:
        try:
            wf_out_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_p = wf_out_file.with_suffix(".tmp")
            tmp_p.write_text(json.dumps(h3b_wf90d, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_p.replace(wf_out_file)
        except Exception:
            pass

    if db_path is None:
        try:
            ms_file = Path(__file__).resolve().parent / "Data" / "market_state.json"
            existing_ms: Dict[str, Any] = {}
            if ms_file.exists():
                loaded_ms = json.loads(ms_file.read_text(encoding="utf-8"))
                if isinstance(loaded_ms, dict):
                    existing_ms = loaded_ms
            excl_raw_ms = cur_regime.get("excluded_symbols") or existing_ms.get("excluded_symbols") or {}
            excl_list_ms = list(excl_raw_ms.keys()) if isinstance(excl_raw_ms, dict) else list(excl_raw_ms)
            excl_reasons_ms = excl_raw_ms if isinstance(excl_raw_ms, dict) else dict(existing_ms.get("exclusion_reasons") or {})
            now_iso_ms = datetime.now(timezone.utc).isoformat()
            merged_ms = {
                **existing_ms,
                "overall_regime": overall,
                "strength_tier": tier,
                "regime_pattern": cur_regime.get("regime_pattern", existing_ms.get("regime_pattern", "")),
                "regime_strength": strength_obj or existing_ms.get("regime_strength", {}),
                "period_results": cur_regime.get("period_results", existing_ms.get("period_results", {})),
                "allowed_strategies": cur_regime.get("allowed_strategies", existing_ms.get("allowed_strategies", [])),
                "recommended_symbols": cur_regime.get("recommended_symbols", existing_ms.get("recommended_symbols", [])),
                "excluded_symbols": excl_list_ms,
                "exclusion_reasons": excl_reasons_ms,
                "btc_ret14d": btc_ret14d,
                "sym_ret14d_map": sym_ret14d_map,
                "h3b_wf90d": h3b_wf90d,
                "is_buy_regime": (overall == "BUY"),
                "is_sell_regime": (overall == "SELL"),
                "evaluated_at": now_iso_ms,
                "timestamp": now_iso_ms,
            }
            ms_file.parent.mkdir(parents=True, exist_ok=True)
            ms_tmp = ms_file.with_suffix(".tmp")
            ms_tmp.write_text(json.dumps(merged_ms, ensure_ascii=False, indent=2), encoding="utf-8")
            ms_tmp.replace(ms_file)
        except Exception:
            pass

    wf90d_paused = bool(h3b_wf90d.get("paused", False))
    eu_now = is_eu_session_jst(now_sec)
    mega_cap_set = {normalize_symbol(s) for s in excluded_mega_caps}
    symbol_params_map: Dict[str, Dict[str, Any]] = {}
    selected_symbols: List[str] = []

    for sym in TRADABLE_MID_CAP_SYMBOLS:
        ns = normalize_symbol(sym)
        if ns in mega_cap_set:
            continue
        is_norm_excl = ns in cur_excluded
        sym_ret14d = float(sym_ret14d_map.get(ns, 0.0))
        noexh_ok, noexh_reason = check_f7_no_exhaustion(btc_ret14d, sym_ret14d)

        long_eligible = (tier == "ULTRA_BUY") and (not is_norm_excl) and (not eu_now)
        short_eligible = (overall == "SELL") and (not is_norm_excl) and noexh_ok and (not wf90d_paused)

        active_strat = F7_LONG_STRATEGY if overall == "BUY" else (F7_SHORT_STRATEGY if overall == "SELL" else F7_LONG_STRATEGY)
        active_side = "long" if active_strat == F7_LONG_STRATEGY else "short"
        active_tf = F7_LONG_TIMEFRAME if active_strat == F7_LONG_STRATEGY else F7_SHORT_TIMEFRAME
        active_int = 30 if active_strat == F7_LONG_STRATEGY else 60

        long_spec = {
            "strategy": F7_LONG_STRATEGY,
            "side": "long",
            "interval": 30,
            "timeframe": F7_LONG_TIMEFRAME,
            "mp": F7_LONG_MP_PERIOD,
            "tr": F7_LONG_TARGET_RATIO,
            "is_qualified": True,
            "eval_grade": "F7_SSOT",
            "eligible_now": long_eligible,
        }
        short_spec = {
            "strategy": F7_SHORT_STRATEGY,
            "side": "short",
            "interval": 60,
            "timeframe": F7_SHORT_TIMEFRAME,
            "is_qualified": True,
            "eval_grade": "F7_SSOT",
            "eligible_now": short_eligible,
            "wf90d_paused": wf90d_paused,
            "noexh_ok": noexh_ok,
        }
        active_spec = dict(long_spec if active_strat == F7_LONG_STRATEGY else short_spec)

        symbol_params_map[ns] = {
            **active_spec,
            "f7_mode": True,
            "f7_mode_name": F7_MODE_NAME,
            "is_range_qualified": True,
            "is_breakout_qualified": True,
            "range_params": long_spec,
            "breakout_params": short_spec,
            "best_overall": active_spec,
            "is_norm_excluded": is_norm_excl,
            "btc_ret14d": btc_ret14d,
            "sym_ret14d": sym_ret14d,
            "noexh_ok": noexh_ok,
            "noexh_reason": noexh_reason,
            "wf90d_paused": wf90d_paused,
            "f7_long_eligible_now": long_eligible,
            "f7_short_eligible_now": short_eligible,
        }
        selected_symbols.append(ns)

    selected_symbols = selected_symbols[: max(1, int(max_selected_symbols))]
    default_best_params = dict(symbol_params_map[selected_symbols[0]]) if selected_symbols else None
    long_elig_cnt = sum(1 for s in selected_symbols if symbol_params_map[s].get("f7_long_eligible_now"))
    short_elig_cnt = sum(1 for s in selected_symbols if symbol_params_map[s].get("f7_short_eligible_now"))
    wf_status_lbl = "PAUSED" if wf90d_paused else "ACTIVE"

    summary_log = (
        f"🏛️ **【{F7_MODE_NAME} スクリーニング結果】**\n"
        f"   ├ 地合い/強度: `{overall}` / `{tier}` (EU除外: {'YES(16-22JST)' if eu_now else 'NO'}) | BTC14d: `{btc_ret14d * 100:+.1f}%`\n"
        f"   ├ H3b WF90dゲート: **`{wf_status_lbl}`** (90d: n={h3b_wf90d['window_trades']}, Net=${h3b_wf90d['window_net']:+.2f}, PF={h3b_wf90d['window_pf']:.2f})\n"
        f"   └ 監視対象: **{len(selected_symbols)}中堅アルト** | 現時点稼働可: `val_poc_30m`={long_elig_cnt}銘柄 / `h3b_squeeze_1h`={short_elig_cnt}銘柄"
    )
    return {
        "f7_mode": True,
        "selected_symbols": selected_symbols,
        "symbol_params_map": symbol_params_map,
        "default_best_params": default_best_params,
        "winner": None,
        "selection": {"selection_id": f"f7_{cutoff_1h}", "reason_text": F7_MODE_NAME},
        "regime_eval": cur_regime,
        "btc_ret14d": btc_ret14d,
        "sym_ret14d_map": sym_ret14d_map,
        "h3b_wf90d": h3b_wf90d,
        "summary_log": summary_log,
    }


def record_live_entry_to_lake(
    symbol: str,
    side: str,
    entry_px: float,
    qty: float,
    params: Optional[Dict[str, Any]] = None,
    entry_ts_utc: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """実約定直後に `live_trades` へ初期レコードを書き出し、追跡メタ (`trade_id`, `ext_pct`, `ext_atr`) を返す。"""
    p = params or {}
    now_ts = int(entry_ts_utc) if entry_ts_utc is not None else utc_now_sec()
    norm_sym = normalize_symbol(symbol)
    strat = str(p.get("strategy", "val_poc"))
    tf = str(p.get("timeframe") or interval_to_timeframe(p.get("interval", 5)))
    sel_id = p.get("lake_selection_id") or p.get("selection_id")
    run_id = p.get("lake_run_id") or p.get("run_id")

    ext_pct, ext_atr = compute_live_entry_extension(norm_sym, strat, side, tf, entry_px, cutoff_utc=now_ts, db_path=db_path)

    dt_jst = datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d")
    trade_id = p.get("lake_trade_id") or f"live_{now_ts}_{hashlib.sha1(f'{norm_sym}|{side}|{now_ts}'.encode('utf-8')).hexdigest()[:8]}"

    try:
        lake = LakeStore(db_path=db_path)
        lake.record_live_trade(
            {
                "trade_id": trade_id,
                "selection_id": sel_id,
                "run_id": run_id,
                "trade_date_jst": dt_jst,
                "symbol": norm_sym,
                "strategy": strat,
                "side": str(side).lower(),
                "timeframe": tf,
                "signal_ts_utc": (now_ts // TIMEFRAME_SECONDS.get(tf, 300)) * TIMEFRAME_SECONDS.get(tf, 300),
                "entry_ts_utc": now_ts,
                "entry_px": float(entry_px),
                "qty": float(qty),
                "notional_usdt": round(float(entry_px) * float(qty), 4),
                "exit_ts_utc": None,
                "exit_px": None,
                "exit_reason": None,
                "gross_pnl_usdt": None,
                "fee_usdt": None,
                "net_pnl_usdt": None,
                "ext_pct_at_entry": ext_pct,
                "ext_atr_at_entry": ext_atr,
                "meta": {"selection_id": sel_id, "run_id": run_id},
            }
        )
    except Exception as e:
        print(f"[Lake LiveBridge] エントリー書込警告: {e}")

    return {
        "lake_trade_id": trade_id,
        "lake_selection_id": sel_id,
        "lake_run_id": run_id,
        "ext_pct_at_entry": ext_pct,
        "ext_atr_at_entry": ext_atr,
    }


def record_live_exit_to_lake(
    symbol: str,
    side: str,
    entry_px: float,
    exit_px: float,
    qty: float,
    net_pnl_usdt: float,
    exit_reason: str,
    state_meta: Optional[Dict[str, Any]] = None,
    exit_ts_utc: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> None:
    """実トレード決済完了時に `live_trades` へ損益・理由を書き戻し、`daily_selections` を EXECUTED へ更新する。"""
    st = state_meta or {}
    now_ts = int(exit_ts_utc) if exit_ts_utc is not None else utc_now_sec()
    norm_sym = normalize_symbol(symbol)
    entry_ts = int(st.get("entry_ts_utc") or now_ts)
    dt_jst = datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d")
    strat = str(st.get("strategy", "unknown"))
    tf = str(st.get("timeframe") or interval_to_timeframe(st.get("interval", 5)))
    sel_id = st.get("lake_selection_id") or st.get("selection_id")
    run_id = st.get("lake_run_id") or st.get("run_id")
    trade_id = st.get("lake_trade_id") or f"live_{entry_ts}_{hashlib.sha1(f'{norm_sym}|{side}|{entry_ts}'.encode('utf-8')).hexdigest()[:8]}"

    e_px = float(entry_px) if entry_px and entry_px > 0 else float(st.get("avg_price") or st.get("last_entry_price") or exit_px)
    q = float(qty) if qty and qty > 0 else float(st.get("qty") or 0.0)
    notional = round(e_px * q, 4)
    side_l = str(side).lower()
    gross_est = ((float(exit_px) - e_px) * q) if side_l == "long" else ((e_px - float(exit_px)) * q)
    fee_est = round(notional * 0.0002 + (float(exit_px) * q) * 0.0006, 6)

    try:
        lake = LakeStore(db_path=db_path)
        lake.record_live_trade(
            {
                "trade_id": trade_id,
                "selection_id": sel_id,
                "run_id": run_id,
                "trade_date_jst": dt_jst,
                "symbol": norm_sym,
                "strategy": strat,
                "side": side_l,
                "timeframe": tf,
                "signal_ts_utc": st.get("signal_ts_utc"),
                "entry_ts_utc": entry_ts,
                "entry_px": e_px,
                "qty": q,
                "notional_usdt": notional,
                "exit_ts_utc": now_ts,
                "exit_px": float(exit_px),
                "exit_reason": str(exit_reason),
                "gross_pnl_usdt": round(gross_est, 6),
                "fee_usdt": fee_est,
                "net_pnl_usdt": float(net_pnl_usdt),
                "ext_pct_at_entry": st.get("ext_pct_at_entry"),
                "ext_atr_at_entry": st.get("ext_atr_at_entry"),
                "meta": {"exit_reason": exit_reason, "selection_id": sel_id, "run_id": run_id},
            }
        )
        lake.export_audit_csvs(tables=("daily_selections", "live_trades"))
    except Exception as e:
        print(f"[Lake LiveBridge] 決済書き戻し警告: {e}")
