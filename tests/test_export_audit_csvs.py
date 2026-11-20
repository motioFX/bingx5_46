"""export_audit_csvs の軽量出力・チャンク分割出力テスト"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lake_store import LakeStore, sec_to_iso, utc_now_sec


class TestExportAuditCsvs(unittest.TestCase):
    def _seed_store(self, db_path: Path) -> LakeStore:
        store = LakeStore(db_path=db_path)
        now_sec = (utc_now_sec() // 3600) * 3600
        old_sec = now_sec - 30 * 86400

        store.upsert_regime_snapshots_batch(
            [
                {
                    "cutoff_utc": old_sec,
                    "cutoff_iso": sec_to_iso(old_sec),
                    "overall_regime": "BUY",
                    "regime_pattern": "BBB",
                    "strength_tier": "STRONG_BUY",
                    "net_score": 0.5,
                    "mean_buy_votes": 17.0,
                    "mean_sell_votes": 4.0,
                    "allowed_strategies": ["val_poc"],
                    "recommended_symbols": ["HYPE-USDT"],
                    "excluded_symbols": {},
                    "whale_snapshot": {},
                },
                {
                    "cutoff_utc": now_sec,
                    "cutoff_iso": sec_to_iso(now_sec),
                    "overall_regime": "SELL",
                    "regime_pattern": "SSS",
                    "strength_tier": "ULTRA_SELL",
                    "net_score": -0.8,
                    "mean_buy_votes": 2.0,
                    "mean_sell_votes": 20.0,
                    "allowed_strategies": ["vah_poc"],
                    "recommended_symbols": ["ENA-USDT"],
                    "excluded_symbols": {},
                    "whale_snapshot": {},
                },
            ]
        )

        for idx, run_id in enumerate(("run_old", "run_new")):
            store.save_backtest_bundle(
                run_row={
                    "run_id": run_id,
                    "run_at_utc": now_sec + idx * 10,
                    "data_cutoff_utc": now_sec,
                    "opt_start_utc": old_sec,
                    "opt_end_utc": now_sec - 86400,
                    "eval_start_utc": now_sec - 86400,
                    "eval_end_utc": now_sec,
                    "embargo_bars": 0,
                    "regime": "SELL",
                    "strength_tier": "ULTRA_SELL",
                    "allowed_strategies_json": '["vah_poc"]',
                    "fee_maker": 0.0002,
                    "fee_taker": 0.0006,
                    "code_git_sha": "test",
                    "config_json": "{}",
                },
                cell_rows=[
                    {
                        "cell_id": f"{run_id}_cell",
                        "run_id": run_id,
                        "symbol": "ENA-USDT",
                        "strategy": "vah_poc",
                        "side": "short",
                        "timeframe": "5m",
                        "params_json": "{}",
                        "opt_trades": 10,
                        "opt_win_rate": 60.0,
                        "opt_net_pnl": 1.0,
                        "opt_ev": 0.1,
                        "opt_pf": 1.5,
                        "opt_max_dd": 0.2,
                        "eval_trades": 30,
                        "eval_wins": 20,
                        "eval_win_rate": 66.7,
                        "eval_gross_pnl": 2.02,
                        "eval_total_fees": 0.02,
                        "eval_net_pnl": 2.0,
                        "eval_pf": 2.0,
                        "eval_ev": 0.066,
                        "eval_max_dd": 0.3,
                        "eval_grade": "QUALIFIED",
                        "eval_ext_stats_json": "{}",
                        "is_opt_best": 1,
                    }
                ],
                sim_trade_rows=[
                    {
                        "sim_trade_id": f"{run_id}_tr1",
                        "run_id": run_id,
                        "cell_id": f"{run_id}_cell",
                        "window_type": "eval",
                        "symbol": "ENA-USDT",
                        "strategy": "vah_poc",
                        "side": "short",
                        "timeframe": "5m",
                        "signal_ts_utc": now_sec - 600,
                        "order_ts_utc": now_sec - 300,
                        "order_px": 0.5,
                        "fill_ts_utc": now_sec - 300,
                        "fill_px": 0.5,
                        "exit_ts_utc": now_sec,
                        "exit_px": 0.49,
                        "exit_reason": "TARGET_TP",
                        "qty": 30.0,
                        "pos_cost_usdt": 15.0,
                        "gross_pnl": 0.3,
                        "entry_fee_usdt": 0.003,
                        "exit_fee_usdt": 0.009,
                        "net_pnl": 0.288,
                        "ext_pct": 0.1,
                        "ext_atr": 0.2,
                        "va_upper": 0.505,
                        "va_lower": 0.485,
                        "range_high": 0.51,
                        "range_low": 0.48,
                        "regime_at_signal": "SELL",
                        "strength_tier_at_signal": "ULTRA_SELL",
                        "net_score_at_signal": -0.8,
                        "buy_votes_at_signal": 2.0,
                        "sell_votes_at_signal": 20.0,
                    }
                ],
            )
        return store

    def test_lightweight_default_and_scoped_exports(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            store = self._seed_store(td_p / "test.db")
            out_dir = td_p / "exports"

            written = store.export_audit_csvs(out_dir=out_dir)
            self.assertNotIn("backtest_simulated_trades", written)
            self.assertNotIn("backtest_cells", written)
            self.assertFalse((out_dir / "backtest_simulated_trades.csv").exists())
            self.assertTrue((out_dir / "backtest_simulated_trades_latest.csv").exists())
            self.assertTrue((out_dir / "backtest_cells_latest.csv").exists())
            self.assertTrue((out_dir / "regime_snapshots_recent.csv").exists())

            latest_tr = (out_dir / "backtest_simulated_trades_latest.csv").read_text(encoding="utf-8")
            self.assertIn("run_new", latest_tr)
            self.assertNotIn("run_old", latest_tr)

            out_sub = td_p / "exports_sub"
            written_sub = store.export_audit_csvs(out_dir=out_sub, tables=("daily_selections", "live_trades"))
            self.assertEqual(set(written_sub.keys()), {"daily_selections", "live_trades"})
            self.assertTrue((out_sub / "live_trades.csv").exists())

            out_full = td_p / "exports_full"
            written_full = store.export_audit_csvs(out_dir=out_full, include_heavy_history=True)
            self.assertIn("backtest_simulated_trades", written_full)
            full_tr = (out_full / "backtest_simulated_trades.csv").read_text(encoding="utf-8")
            self.assertIn("run_old", full_tr)
            self.assertIn("run_new", full_tr)

    def test_empty_db_exports_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            store = LakeStore(db_path=td_p / "empty.db")
            out_dir = td_p / "exports"
            written = store.export_audit_csvs(out_dir=out_dir)
            self.assertIn("live_trades", written)
            self.assertTrue((out_dir / "live_trades.csv").exists())
            written_t = store.export_audit_csvs(out_dir=td_p / "exports_t", tables=("live_trades", "daily_selections"))
            self.assertEqual(set(written_t.keys()), {"live_trades", "daily_selections"})


if __name__ == "__main__":
    unittest.main()
