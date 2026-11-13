# -*- coding: utf-8 -*-
"""
現行スクリーニング vs データレイク・ウォークフォワード基盤 比較レポート生成 (`compare_screening_vs_lake.py`)

目的:
1. インサンプル (直近30日最適化=同期間評価) と ウォークフォワード OOS (Opt→Eval分離) の乖離を比較
2. `vol_ratio >= 1.5` 必須 vs 緩和のトレード件数 (`n>=10`, `n>=30`) ＆ 手数料後EV比較
3. 伸び切り度合い (`<0.5 ATR`, `0.5-1.5 ATR`, `>=1.5 ATR`) 別の OOS EV/勝率集計
4. 出力:
   - Data/lake/exports/screening_comparison_cells.csv
   - Data/lake/exports/screening_comparison_report.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from lake_backtest_engine import (
    LONG_STRATEGIES,
    SHORT_STRATEGIES,
    WalkForwardConfig,
    _compute_causal_features,
    _compute_metrics,
    _generate_signal_array,
    _simulate_window,
    compute_regime_from_lake,
    get_param_grid,
    run_walkforward_from_lake,
)
from lake_store import LAKE_EXPORTS_DIR, TIMEFRAME_SECONDS, LakeStore, sec_to_iso, utc_now_sec
from symbol_universe import FIXED_TRADABLE_SYMBOLS


def _run_legacy_insample_30d(
    lake: LakeStore,
    cutoff_utc: int,
    strategies: List[str],
    timeframes: tuple = ("5m", "15m"),
    vol_surge: bool = True,
) -> Dict[tuple, Dict[str, Any]]:
    """現行相当の「直近30日インサンプル最良セル」を集計 (比較ベースライン)。"""
    cfg = WalkForwardConfig(
        opt_days=30,
        eval_days=0,
        timeframes=timeframes,
        enforce_vol_surge=vol_surge,
        gil_yield_sec=0.0,
    )
    win_start = cutoff_utc - (30 * 86400)
    out: Dict[tuple, Dict[str, Any]] = {}
    has_vp = any(s in ("val_poc", "vah_poc", "vah_break", "val_break") for s in strategies)
    needed_mps = (100, 150, 200) if has_vp else ()

    for sym in FIXED_TRADABLE_SYMBOLS:
        df_5m = lake.load_candles(sym, "5m", cutoff_utc=cutoff_utc, start_utc=win_start - 2 * 86400)
        if df_5m.empty:
            continue
        df_oi = lake.load_oi_fr(sym, cutoff_utc=cutoff_utc, start_utc=win_start - 5 * 86400)
        for tf in timeframes:
            tf_warmup = max(2 * 86400, 220 * TIMEFRAME_SECONDS[tf])
            df_tf = lake.load_candles(sym, tf, cutoff_utc=cutoff_utc, start_utc=win_start - tf_warmup)
            if df_tf.empty or len(df_tf) < 120:
                continue
            df_tf = lake.attach_oi_fr_causal(df_tf, tf, df_oi, cutoff_utc=cutoff_utc)
            feat = _compute_causal_features(df_tf, tf, cfg, needed_mps=needed_mps)
            for strat in strategies:
                best_m = None
                best_score = (-1e18, -1e18)
                best_p = None
                for params in get_param_grid(strat):
                    sig = _generate_signal_array(feat, strat, params, cfg)
                    trs = _simulate_window(feat, sig, df_5m, sym, strat, params, "INSAMPLE_30D", win_start, cutoff_utc, cfg)
                    m = _compute_metrics(trs)
                    sc = (round(m["net_pnl"], 6), round(m["ev"], 6))
                    if best_m is None or sc > best_score:
                        best_score = sc
                        best_m = m
                        best_p = params
                if best_m is not None:
                    out[(sym, strat, tf)] = {**best_m, "params": best_p}
    return out


def _aggregate_extension_buckets(sim_trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    buckets = {"lt_0.5_atr": [], "0.5_to_1.5_atr": [], "ge_1.5_atr": []}
    for t in sim_trades:
        if t.get("fill_ts_utc") is None:
            continue
        ea = t.get("ext_atr")
        if ea is None:
            continue
        val = abs(float(ea))
        pnl = float(t["net_pnl"])
        if val < 0.5:
            buckets["lt_0.5_atr"].append(pnl)
        elif val < 1.5:
            buckets["0.5_to_1.5_atr"].append(pnl)
        else:
            buckets["ge_1.5_atr"].append(pnl)

    summary = {}
    for k, arr in buckets.items():
        if arr:
            wins = sum(1 for x in arr if x > 0)
            summary[k] = {
                "n": len(arr),
                "win_rate": wins / len(arr) * 100.0,
                "net_pnl": sum(arr),
                "ev": sum(arr) / len(arr),
            }
        else:
            summary[k] = {"n": 0, "win_rate": 0.0, "net_pnl": 0.0, "ev": 0.0}
    return summary


def generate_comparison_report(
    db_path: Optional[Path] = None,
    cutoff_utc: Optional[int] = None,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    lake = LakeStore(db_path=db_path)
    if cutoff_utc is None:
        btc_stats = lake.get_candle_stats("BTC-USDT", "5m")
        cutoff_utc = int(btc_stats["max_ts_utc"] + 300) if btc_stats["max_ts_utc"] else utc_now_sec()

    out_path = Path(out_dir) if out_dir else LAKE_EXPORTS_DIR
    out_path.mkdir(parents=True, exist_ok=True)

    regime_info = compute_regime_from_lake(lake, cutoff_utc)
    live_allowed = list(regime_info.get("allowed_strategies") or [])

    # 比較検証では現行地合いに加え、両方向全6戦略の OOS 傾向も網羅検証する
    all_strats = list(LONG_STRATEGIES + SHORT_STRATEGIES)

    # 1. インサンプル30d比較
    insample_map = _run_legacy_insample_30d(lake, cutoff_utc, all_strats, timeframes=("5m", "15m"), vol_surge=True)

    # 2. レイク WF 標準 (Opt 90d -> Eval 90d, vol_ratio >= 1.5 必須, 5m/15m)
    cfg_wf_std = WalkForwardConfig(
        opt_days=90, eval_days=90, timeframes=("5m", "15m"), enforce_vol_surge=True, gil_yield_sec=0.0
    )
    wf_std = run_walkforward_from_lake(
        lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg_wf_std,
        symbols=FIXED_TRADABLE_SYMBOLS,
        allowed_strategies_override=all_strats,
        persist=False,
    )

    # 3. レイク WF 現行地合いそのまま実行 (本番同等の勝者判定)
    wf_live_regime = run_walkforward_from_lake(
        lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg_wf_std,
        override_regime_info=regime_info,
        persist=False,
    )

    # 4. レイク WF 出来高1.5x下限解除比較 (Opt 90d -> Eval 90d, 5m/15m)
    cfg_wf_novol = WalkForwardConfig(
        opt_days=90, eval_days=90, timeframes=("5m", "15m"), enforce_vol_surge=False, gil_yield_sec=0.0
    )
    wf_novol = run_walkforward_from_lake(
        lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg_wf_novol,
        symbols=FIXED_TRADABLE_SYMBOLS,
        allowed_strategies_override=all_strats,
        persist=False,
    )

    # 5. 上位足 (1h / 4h) WF チェック
    cfg_wf_htf = WalkForwardConfig(
        opt_days=90, eval_days=90, timeframes=("1h", "4h"), enforce_vol_surge=True, gil_yield_sec=0.0
    )
    wf_htf = run_walkforward_from_lake(
        lake,
        data_cutoff_utc=cutoff_utc,
        cfg=cfg_wf_htf,
        symbols=FIXED_TRADABLE_SYMBOLS,
        allowed_strategies_override=all_strats,
        persist=False,
    )

    # CSV 出力
    novol_idx = {(c["symbol"], c["strategy"], c["timeframe"]): c for c in wf_novol["cells"]}
    rows = []
    for c in wf_std["cells"]:
        key = (c["symbol"], c["strategy"], c["timeframe"])
        ins = insample_map.get(key, {})
        nv = novol_idx.get(key, {})
        rows.append(
            {
                "symbol": c["symbol"],
                "strategy": c["strategy"],
                "side": c["side"],
                "timeframe": c["timeframe"],
                "insample_30d_trades": ins.get("trades", 0),
                "insample_30d_net_pnl": round(float(ins.get("net_pnl", 0.0)), 4),
                "wf_opt90_trades": c["opt_trades"],
                "wf_opt90_net_pnl": c["opt_net_pnl"],
                "wf_eval90_trades": c["eval_trades"],
                "wf_eval90_win_rate": c["eval_win_rate"],
                "wf_eval90_net_pnl": c["eval_net_pnl"],
                "wf_eval90_ev": c["eval_ev"],
                "wf_eval90_pf": c["eval_pf"],
                "wf_eval90_grade": c["eval_grade"],
                "novol_eval90_trades": nv.get("eval_trades", 0),
                "novol_eval90_net_pnl": nv.get("eval_net_pnl", 0.0),
                "novol_eval90_ev": nv.get("eval_ev", 0.0),
                "novol_eval90_grade": nv.get("eval_grade", ""),
            }
        )

    df_csv = pd.DataFrame(rows)
    csv_file = out_path / "screening_comparison_cells.csv"
    df_csv.to_csv(csv_file, index=False, encoding="utf-8")

    # 集計統計
    std_cells = wf_std["cells"]
    nv_cells = wf_novol["cells"]
    htf_cells = wf_htf["cells"]
    ins_pos = [v for v in insample_map.values() if v.get("net_pnl", 0.0) > 0 and v.get("trades", 0) >= 1]
    ins_top10 = sorted(insample_map.items(), key=lambda kv: kv[1].get("net_pnl", -999), reverse=True)[:10]

    # インサンプル黒字上位のうち、OOSでもプラスを維持できた割合
    std_lookup = {(c["symbol"], c["strategy"], c["timeframe"]): c for c in std_cells}
    oos_survive = sum(1 for k, _ in ins_top10 if std_lookup.get(k, {}).get("eval_net_pnl", -1.0) > 0)

    ext_std = _aggregate_extension_buckets(wf_std["sim_trades"])
    ext_nv = _aggregate_extension_buckets(wf_novol["sim_trades"])

    top_oos_std = sorted(
        [c for c in std_cells if c["eval_trades"] >= 10],
        key=lambda x: (1 if x["eval_grade"] == "QUALIFIED" else 0, x["eval_ev"], x["eval_pf"]),
        reverse=True,
    )[:10]
    top_oos_nv = sorted(
        [c for c in nv_cells if c["eval_trades"] >= 10],
        key=lambda x: (1 if x["eval_grade"] == "QUALIFIED" else 0, x["eval_ev"], x["eval_pf"]),
        reverse=True,
    )[:10]

    lines = [
        "# 現行スクリーニング vs データレイク WF基盤 比較検証レポート",
        "",
        f"- 検証基準時刻 (`data_cutoff_utc`): `{sec_to_iso(cutoff_utc)}`",
        f"- 最適化窓 (`Opt 90d`) → 評価窓 (`Eval 90d OOS`) / 手数料: 往復 `0.08%` (`Maker 0.02% + Taker 0.06%`) / Maker最良気配追随",
        f"- 基準時点の現行地合い: **`{regime_info.get('overall_regime')}`** (`{regime_info.get('regime_strength', {}).get('tier')}` | 許可戦略: `{live_allowed}`)",
        "",
        "## 1. 全体サマリー（インサンプル vs ウォークフォワード OOS）",
        "",
        "| モデル / ゲート設定 | 総評価セル | `n>=10` (`PROVISIONAL/QUALIFIED`) | `n>=30` (`QUALIFIED`候補) | `EV>0` & `n>=10` 合格 | 擬似約定総数 (Eval) | 勝者 / 状態 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
        (
            f"| 現行地合い本番同等 (`Opt90→Eval90`, `vol>=1.5`) | {len(wf_live_regime['cells'])} | "
            f"{sum(1 for c in wf_live_regime['cells'] if c['eval_trades']>=10)} | "
            f"{sum(1 for c in wf_live_regime['cells'] if c['eval_trades']>=30)} | "
            f"{sum(1 for c in wf_live_regime['cells'] if c['eval_grade'] in ('QUALIFIED','PROVISIONAL'))} | "
            f"{sum(c['eval_trades'] for c in wf_live_regime['cells'])} | "
            f"{wf_live_regime['selection']['status']} ({wf_live_regime['selection']['winner_symbol'] or 'なし'}) |"
        ),
        (
            f"| 全6戦略参考比較 (`vol>=1.5`, `5m/15m`) | {len(std_cells)} | "
            f"{sum(1 for c in std_cells if c['eval_trades']>=10)} | "
            f"{sum(1 for c in std_cells if c['eval_trades']>=30)} | "
            f"{sum(1 for c in std_cells if c['eval_grade'] in ('QUALIFIED','PROVISIONAL'))} | "
            f"{sum(c['eval_trades'] for c in std_cells)} | "
            f"{wf_std['selection']['status']} ({wf_std['selection']['winner_symbol'] or 'なし'}) |"
        ),
        (
            f"| 出来高1.5x下限解除比較 (`5m/15m`) | {len(nv_cells)} | "
            f"{sum(1 for c in nv_cells if c['eval_trades']>=10)} | "
            f"{sum(1 for c in nv_cells if c['eval_trades']>=30)} | "
            f"{sum(1 for c in nv_cells if c['eval_grade'] in ('QUALIFIED','PROVISIONAL'))} | "
            f"{sum(c['eval_trades'] for c in nv_cells)} | "
            f"{wf_novol['selection']['status']} ({wf_novol['selection']['winner_symbol'] or 'なし'}) |"
        ),
        (
            f"| 上位足比較 (`vol>=1.5`, `1h/4h`) | {len(htf_cells)} | "
            f"{sum(1 for c in htf_cells if c['eval_trades']>=10)} | "
            f"{sum(1 for c in htf_cells if c['eval_trades']>=30)} | "
            f"{sum(1 for c in htf_cells if c['eval_grade'] in ('QUALIFIED','PROVISIONAL'))} | "
            f"{sum(c['eval_trades'] for c in htf_cells)} | "
            f"{wf_htf['selection']['status']} ({wf_htf['selection']['winner_symbol'] or 'なし'}) |"
        ),
        "",
        f"- インサンプル30日上位10セルのうち、OOS 90日でも純損益>0を維持できたセル: **{oos_survive} / {len(ins_top10)} 件**",
        "",
        "## 2. 伸び切り度合い (`ext_atr`) バケット別 OOS 成績",
        "",
        "| ゲート設定 | ATR乖離区分 | 取引数 `n` | 勝率 (%) | Net PnL ($) | 手数料後 EV ($/trade) |",
        "| :--- | :--- | ---: | ---: | ---: | ---: |",
    ]
    for label, ext_d in [("vol>=1.5", ext_std), ("vol緩和", ext_nv)]:
        for b_key, b_label in [("lt_0.5_atr", "< 0.5 ATR"), ("0.5_to_1.5_atr", "0.5 - 1.5 ATR"), ("ge_1.5_atr", ">= 1.5 ATR")]:
            st = ext_d[b_key]
            lines.append(
                f"| {label} | {b_label} | {st['n']} | {st['win_rate']:.1f}% | {st['net_pnl']:+.2f} | {st['ev']:+.4f} |"
            )

    lines.extend([
        "",
        "## 3. OOS 評価上位セル (`vol>=1.5`, `n>=10`)",
        "",
        "| Symbol | Strategy | TF | Grade | Opt PnL | Eval n | Eval WR | Eval Net PnL | Eval EV | PF |",
        "| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for c in top_oos_std:
        lines.append(
            f"| {c['symbol']} | {c['strategy']} | {c['timeframe']} | {c['eval_grade']} | "
            f"{c['opt_net_pnl']:+.2f} | {c['eval_trades']} | {c['eval_win_rate']:.1f}% | "
            f"{c['eval_net_pnl']:+.2f} | {c['eval_ev']:+.4f} | {c['eval_pf']:.2f} |"
        )
    if not top_oos_std:
        lines.append("| (該当なし) | - | - | - | - | - | - | - | - | - |")

    lines.extend([
        "",
        "## 4. OOS 評価上位セル (`vol緩和参考`, `n>=10`)",
        "",
        "| Symbol | Strategy | TF | Grade | Opt PnL | Eval n | Eval WR | Eval Net PnL | Eval EV | PF |",
        "| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for c in top_oos_nv:
        lines.append(
            f"| {c['symbol']} | {c['strategy']} | {c['timeframe']} | {c['eval_grade']} | "
            f"{c['opt_net_pnl']:+.2f} | {c['eval_trades']} | {c['eval_win_rate']:.1f}% | "
            f"{c['eval_net_pnl']:+.2f} | {c['eval_ev']:+.4f} | {c['eval_pf']:.2f} |"
        )
    if not top_oos_nv:
        lines.append("| (該当なし) | - | - | - | - | - | - | - | - | - |")

    md_file = out_path / "screening_comparison_report.md"
    md_file.write_text("\n".join(lines), encoding="utf-8")

    return {
        "cutoff_utc": cutoff_utc,
        "csv_file": str(csv_file),
        "md_file": str(md_file),
        "live_selection": wf_live_regime["selection"],
        "std_selection": wf_std["selection"],
        "novol_selection": wf_novol["selection"],
        "ext_std": ext_std,
        "ext_novol": ext_nv,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare current screening vs Lake WF engine")
    parser.add_argument("--cutoff-utc", type=int, default=None, help="任意の参照時刻 (UTC秒)")
    parser.add_argument("--db-path", type=str, default=None)
    args = parser.parse_args()
    res = generate_comparison_report(
        db_path=Path(args.db_path) if args.db_path else None,
        cutoff_utc=args.cutoff_utc,
    )
    print(f"[Compare Report] CSV: {res['csv_file']}")
    print(f"[Compare Report] MD : {res['md_file']}")
    print(f"[Compare Report] Live Regime Selection: {res['live_selection']['reason_text']}")


if __name__ == "__main__":
    main()
