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
    is_breakout_strategy,
    run_walkforward_from_lake,
    strategy_side,
)
from lake_store import TIMEFRAME_SECONDS, LakeStore, sec_to_iso, utc_now_sec
from symbol_universe import normalize_symbol

JST = timezone(timedelta(hours=9))

_TF_TO_INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
_INTERVAL_TO_TF = {5: "5m", 15: "15m", 60: "1h", 240: "4h", "5": "5m", "15": "15m", "60": "1h", "240": "4h"}


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
        return run_migration(history_days=history_days, sync_live=True)

    from candle_5m_store import sync_all_target_symbols_5m
    sync_all_target_symbols_5m(lookback_days=2)
    try:
        from bingx_oi_collector import collect_and_save_oi_snapshot
        from symbol_universe import LAKE_UNIVERSE_SYMBOLS
        collect_and_save_oi_snapshot(symbols=LAKE_UNIVERSE_SYMBOLS)
    except Exception:
        pass
    return {"status": "synced", "btc_5m_count": st["count"], "span_days": round(span_days, 1)}


def run_lake_screening_for_bot(
    override_regime_info: Dict[str, Any],
    db_path: Optional[Path] = None,
    wf_cfg: Optional[WalkForwardConfig] = None,
    max_selected_symbols: int = 20,
    excluded_mega_caps: Sequence[str] = ("SOL-USDT", "DOGE-USDT", "XRP-USDT"),
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
    lake.export_audit_csvs()

    winner = wf_res["winner"]
    selection = wf_res["selection"]
    cells = wf_res["cells"]
    run_rec = wf_res["run"]

    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for c in cells:
        by_sym.setdefault(normalize_symbol(c["symbol"]), []).append(c)

    mega_cap_set = {normalize_symbol(s) for s in excluded_mega_caps}
    symbol_params_map: Dict[str, Dict[str, Any]] = {}
    best_cell_by_sym: Dict[str, Dict[str, Any]] = {}
    qualified_syms: List[str] = []

    for sym, sym_cells in by_sym.items():
        p_dict = build_symbol_params_from_cells(sym_cells, selection, override_regime_info)
        symbol_params_map[sym] = p_dict
        best_cell_by_sym[sym] = max(sym_cells, key=_cell_sort_key)
        if p_dict.get("is_qualified") and sym not in mega_cap_set:
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

    # 上位5件 (参考出力)
    top5 = sorted(
        [c for c in cells if c["eval_trades"] > 0],
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
        lake.export_audit_csvs()
    except Exception as e:
        print(f"[Lake LiveBridge] 決済書き戻し警告: {e}")
