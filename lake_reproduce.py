# -*- coding: utf-8 -*-
"""
データレイク単体再現 CLI (`lake_reproduce.py`)

外部 API / ネットワークに一切接続せず、`Data/lake/bingx_lake.db` のみから
指定された `run_id` のウォークフォワードバックテスト結果および日次勝者選定結果を再計算し、
保存済みの `backtest_cells`・`daily_selections` との数値完全一致を検証する。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from lake_backtest_engine import WalkForwardConfig, run_walkforward_from_lake
from lake_store import LakeStore


def reproduce_run(
    run_id: str,
    db_path: Optional[Path] = None,
    tolerance: float = 1e-6,
) -> Dict[str, Any]:
    lake = LakeStore(db_path=db_path)
    run_rec = lake.get_run_record(run_id)
    if not run_rec:
        raise KeyError(f"run_id が見つかりません: {run_id}")

    stored_cells = lake.get_cells_for_run(run_id)
    stored_sel = lake.get_selection_for_run(run_id)

    cfg = WalkForwardConfig.from_dict(json.loads(run_rec["config_json"]))
    allowed_strategies = json.loads(run_rec["allowed_strategies_json"])
    cutoff_utc = int(run_rec["data_cutoff_utc"])

    # regime_snapshots に記録されている当時の推奨銘柄・除外銘柄・地合いをそのまま復元 (オフライン)
    with lake._conn(write=False) as conn:
        r_snap = conn.execute("SELECT * FROM regime_snapshots WHERE cutoff_utc = ?", (cutoff_utc,)).fetchone()

    override_regime_info: Optional[Dict[str, Any]] = None
    symbols_override: Optional[List[str]] = None
    if r_snap:
        rec_syms = json.loads(r_snap["recommended_symbols_json"])
        exc_syms = json.loads(r_snap["excluded_symbols_json"])
        override_regime_info = {
            "overall_regime": r_snap["overall_regime"],
            "regime_pattern": r_snap["regime_pattern"],
            "regime_strength": {
                "tier": r_snap["strength_tier"],
                "net_score": r_snap["net_score"],
                "mean_buy_votes": r_snap["mean_buy_votes"],
                "mean_sell_votes": r_snap["mean_sell_votes"],
            },
            "allowed_strategies": allowed_strategies,
            "recommended_symbols": rec_syms,
            "excluded_symbols": exc_syms if isinstance(exc_syms, dict) else {},
            "whale_snapshot": json.loads(r_snap["whale_snapshot_json"]),
        }
        symbols_override = sorted(set(c["symbol"] for c in stored_cells)) if stored_cells else rec_syms

    re_res = run_walkforward_from_lake(
        lake=lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg,
        override_regime_info=override_regime_info,
        symbols=symbols_override,
        allowed_strategies_override=allowed_strategies,
        run_id=run_id,
        persist=False,
    )

    re_cells_by_key = {(c["symbol"], c["strategy"], c["timeframe"]): c for c in re_res["cells"]}
    st_cells_by_key = {(c["symbol"], c["strategy"], c["timeframe"]): c for c in stored_cells}

    mismatches: List[str] = []
    if set(re_cells_by_key.keys()) != set(st_cells_by_key.keys()):
        mismatches.append(f"セル集合不一致: stored={len(st_cells_by_key)}, reproduced={len(re_cells_by_key)}")
    else:
        for k, sc in st_cells_by_key.items():
            rc = re_cells_by_key[k]
            if sc["params_json"] != rc["params_json"]:
                mismatches.append(f"{k} params不一致: {sc['params_json']} != {rc['params_json']}")
            for col in ("opt_trades", "eval_trades", "eval_wins"):
                if int(sc[col]) != int(rc[col]):
                    mismatches.append(f"{k} {col}不一致: {sc[col]} != {rc[col]}")
            for col in ("opt_net_pnl", "eval_gross_pnl", "eval_total_fees", "eval_net_pnl", "eval_ev", "eval_pf"):
                if not math.isclose(float(sc[col]), float(rc[col]), abs_tol=tolerance, rel_tol=1e-6):
                    mismatches.append(f"{k} {col}不一致: {sc[col]} != {rc[col]}")
            if sc["eval_grade"] != rc["eval_grade"]:
                mismatches.append(f"{k} eval_grade不一致: {sc['eval_grade']} != {rc['eval_grade']}")

    if stored_sel is not None:
        rs = re_res["selection"]
        for col in ("winner_symbol", "winner_strategy", "winner_timeframe", "winner_params_json", "eval_grade"):
            if stored_sel.get(col) != rs.get(col):
                mismatches.append(f"selection {col}不一致: {stored_sel.get(col)} != {rs.get(col)}")
        if stored_sel.get("winner_symbol") is not None:
            for col in ("eval_trades",):
                if stored_sel.get(col) != rs.get(col):
                    mismatches.append(f"selection {col}不一致: {stored_sel.get(col)} != {rs.get(col)}")
            for col in ("eval_ev", "eval_net_pnl", "eval_pf"):
                if not math.isclose(float(stored_sel[col]), float(rs[col]), abs_tol=tolerance, rel_tol=1e-6):
                    mismatches.append(f"selection {col}不一致: {stored_sel[col]} != {rs[col]}")

    return {
        "run_id": run_id,
        "matched": len(mismatches) == 0,
        "cells_checked": len(stored_cells),
        "mismatches": mismatches,
        "stored_selection": stored_sel,
        "reproduced_selection": re_res["selection"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Walk-Forward Reproducibility Verifier")
    parser.add_argument("--run-id", type=str, required=True, help="再現検証する backtest_runs.run_id")
    parser.add_argument("--db-path", type=str, default=None, help="任意の SQLite DB パス")
    args = parser.parse_args()

    res = reproduce_run(args.run_id, db_path=Path(args.db_path) if args.db_path else None)
    print(f"[Lake Reproduce] run_id={res['run_id']} | cells={res['cells_checked']} | matched={res['matched']}")
    if res["mismatches"]:
        for m in res["mismatches"][:20]:
            print(f"  MISMATCH: {m}")
        raise SystemExit(1)
    sel = res["reproduced_selection"]
    print(f"  Selection: status={sel['status']} | winner={sel['winner_symbol']} {sel['winner_strategy']} ({sel['winner_timeframe']})")


if __name__ == "__main__":
    main()
