#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1日1回 14日間比較レポート & 方向別ポートフォリオ選定の単体テスト。"""

import json
import sys
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lake_backtest_engine as lbe
import real_trade_tracker as rtt
from lake_store import LakeStore

JST = timezone(timedelta(hours=9))


class TestDaily14dReportAndSideAwarePortfolio(unittest.TestCase):
    def _build_synthetic_lake(self, db_path: Path, start_utc: int, end_utc: int) -> str:
        store = LakeStore(db_path=db_path)
        ts_arr = np.arange(start_utc - 16 * 86400, end_utc, 300, dtype=np.int64)
        n = len(ts_arr)
        # 前半上昇・後半下降の波形を作成し、long/short双方に売買が起きるようにする
        wave = 100.0 + 3.0 * np.sin(np.linspace(0, 12 * np.pi, n))
        df_c = pd.DataFrame({
            "ts_utc": ts_arr,
            "open": wave,
            "high": wave + 0.6,
            "low": wave - 0.6,
            "close": wave + 0.1 * np.cos(np.linspace(0, 24 * np.pi, n)),
            "volume": np.full(n, 1000.0),
        })
        for sym in ("BTC-USDT", "ENA-USDT"):
            store.upsert_candles(sym, "5m", df_c, cutoff_utc=end_utc + 300)
            store.rebuild_higher_timeframes_from_5m(sym, cutoff_utc=end_utc + 300)

        run_id = "run_test_14d"
        cells = [
            {
                "cell_id": f"{run_id}__ENA-USDT__val_poc__5m",
                "run_id": run_id,
                "symbol": "ENA-USDT",
                "strategy": "val_poc",
                "side": "long",
                "timeframe": "5m",
                "params_json": json.dumps({"mp": 60, "target_ratio": 0.55}),
                "opt_trades": 15,
                "opt_win_rate": 60.0,
                "opt_net_pnl": 1.2,
                "opt_ev": 0.08,
                "opt_pf": 2.0,
                "opt_max_dd": 0.3,
                "eval_trades": 20,
                "eval_wins": 12,
                "eval_win_rate": 60.0,
                "eval_gross_pnl": 2.2,
                "eval_total_fees": 0.2,
                "eval_net_pnl": 2.0,
                "eval_ev": 0.10,
                "eval_pf": 2.5,
                "eval_max_dd": 0.4,
                "eval_grade": "PROVISIONAL",
                "eval_ext_stats_json": "{}",
                "is_opt_best": 1,
            },
            {
                "cell_id": f"{run_id}__ENA-USDT__rsima_short__5m",
                "run_id": run_id,
                "symbol": "ENA-USDT",
                "strategy": "rsima_short",
                "side": "short",
                "timeframe": "5m",
                "params_json": json.dumps({"s_ep": 55.0, "s_cp": 35.0}),
                "opt_trades": 14,
                "opt_win_rate": 57.0,
                "opt_net_pnl": 0.9,
                "opt_ev": 0.06,
                "opt_pf": 1.8,
                "opt_max_dd": 0.3,
                "eval_trades": 18,
                "eval_wins": 11,
                "eval_win_rate": 61.1,
                "eval_gross_pnl": 1.1,
                "eval_total_fees": 0.1,
                "eval_net_pnl": 1.0,
                "eval_ev": 0.055,
                "eval_pf": 2.2,
                "eval_max_dd": 0.2,
                "eval_grade": "PROVISIONAL",
                "eval_ext_stats_json": "{}",
                "is_opt_best": 1,
            },
        ]
        run_row = {
            "run_id": run_id,
            "run_at_utc": end_utc,
            "data_cutoff_utc": end_utc,
            "opt_start_utc": start_utc - 2 * 86400,
            "opt_end_utc": start_utc,
            "eval_start_utc": start_utc,
            "eval_end_utc": end_utc,
            "embargo_bars": 0,
            "regime": "SELL",
            "strength_tier": "STRONG_SELL",
            "allowed_strategies_json": json.dumps(["val_break", "vah_poc", "rsima_short"]),
            "fee_maker": 0.0002,
            "fee_taker": 0.0006,
            "code_git_sha": "test",
            "config_json": json.dumps(lbe.WalkForwardConfig().to_dict()),
        }
        store.save_backtest_bundle(run_row, cells, [])
        return run_id

    def test_side_aware_portfolio_picks_short_in_sell_regime(self):
        now_jst = datetime(2026, 11, 19, 9, 5, 0, tzinfo=JST)
        end_utc = int(now_jst.timestamp())
        start_utc = end_utc - 6 * 86400
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "lake.db"
            run_id = self._build_synthetic_lake(db_path, start_utc, end_utc)
            store = LakeStore(db_path=db_path)
            cells = store.get_cells_for_run(run_id)
            ranked, p_map = lbe.build_portfolio_params_map_from_cells(cells)
            self.assertEqual(ranked, ["ENA-USDT"])
            self.assertIn("side_specs", p_map["ENA-USDT"])
            self.assertIn("long", p_map["ENA-USDT"]["side_specs"])
            self.assertIn("short", p_map["ENA-USDT"]["side_specs"])
            # 総合1位は long でも、SELL 地合い下では short 側 (rsima_short) が選ばれて動くこと
            self.assertEqual(p_map["ENA-USDT"]["side"], "long")

            hourly_map = {
                (t // 3600) * 3600: {
                    "overall_regime": "SELL",
                    "regime_pattern": "(S,S,S)",
                    "allowed_strategies": ["val_break", "vah_poc", "rsima_short"],
                    "allowed_strategies_set": frozenset(["val_break", "vah_poc", "rsima_short"]),
                    "excluded_symbols_set": frozenset(),
                }
                for t in range(start_utc, end_utc + 3600, 3600)
            }
            res = lbe.simulate_portfolio_on_window(
                store, p_map, ranked, hourly_map, start_utc, end_utc,
                lbe.WalkForwardConfig(allow_ny_session=True, enforce_vol_surge=False, gil_yield_sec=0.0),
            )
            short_trades = [t for t in res["sim_trades"] if t.get("side") == "short"]
            long_trades = [t for t in res["sim_trades"] if t.get("side") == "long"]
            self.assertGreater(len(short_trades), 0)
            self.assertEqual(len(long_trades), 0)

    def test_daily_14d_report_build_and_idempotent_send(self):
        now_jst = datetime(2026, 11, 19, 9, 5, 0, tzinfo=JST)
        before_9_jst = datetime(2026, 11, 19, 8, 55, 0, tzinfo=JST)
        end_utc = int(now_jst.timestamp())
        start_utc = end_utc - 14 * 86400

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "lake.db"
            self._build_synthetic_lake(db_path, start_utc, end_utc)

            trades_json = tmp_path / "real_trades_history.json"
            state_json = tmp_path / "last_daily_14d_report_sent.json"
            chart_png = tmp_path / "daily_14d_bt_vs_live.png"
            sample_real = [
                {
                    "timestamp": "2026-11-17T05:11:53.151433+00:00",
                    "symbol": "ENA-USDT",
                    "side": "SHORT",
                    "action": "CLOSE",
                    "price": 0.11568,
                    "qty": 128.41,
                    "pnl": 0.1361,
                    "note": "real 1",
                },
                {
                    "timestamp": "2026-11-17T15:08:06+00:00",
                    "symbol": "ENA-USDT",
                    "side": "SHORT",
                    "action": "CLOSE",
                    "price": 0.0,
                    "qty": 0.0,
                    "pnl": -0.0012,
                    "note": "api sync",
                },
                {
                    "timestamp": "2026-11-18T00:46:32.453428+00:00",
                    "symbol": "ENA-USDT",
                    "side": "SHORT",
                    "action": "CLOSE",
                    "price": 0.11643,
                    "qty": 127.13,
                    "pnl": 0.2237,
                    "note": "real 2",
                },
            ]
            trades_json.write_text(json.dumps(sample_real, ensure_ascii=False), encoding="utf-8")

            fake_discord = mock.MagicMock()
            with (
                mock.patch.object(rtt, "DATA_DIR", tmp_path),
                mock.patch.object(rtt, "TRADES_FILE", trades_json),
                mock.patch.object(rtt, "DAILY_14D_REPORT_STATE_FILE", state_json),
                mock.patch.object(rtt, "DAILY_14D_CHART_FILE", chart_png),
                mock.patch("bingx5_46_3logic.discord", fake_discord),
            ):
                rep = rtt.build_daily_14d_comparison_report(
                    days=14, current_balance=99994.34, db_path=db_path, now_jst=now_jst
                )
                self.assertEqual(len(rep["daily_rows"]), 14)
                self.assertEqual(rep["real_summary"]["trades"], 3)
                self.assertAlmostEqual(rep["real_summary"]["net_pnl"], 0.3586, places=4)
                self.assertLess(len(rep["report_text"]), 1900)
                self.assertIn("11/18", rep["report_text"])

                # 白背景チャートPNG生成とFont/mathtext警告ゼロ検証
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    out_chart = rtt.plot_daily_14d_comparison_chart(rep, output_path=chart_png)
                font_rm_warnings = [w for w in caught if "Font 'rm'" in str(w.message)]
                self.assertEqual(font_rm_warnings, [])
                self.assertFalse(bool(plt.rcParams["text.parse_math"]))
                self.assertIsNotNone(out_chart)
                self.assertTrue(chart_png.exists())
                self.assertGreater(chart_png.stat().st_size, 1000)
                img = mpimg.imread(str(chart_png))
                self.assertGreaterEqual(float(img[0, 0, 0]), 0.99)
                self.assertGreaterEqual(float(img[0, 0, 1]), 0.99)
                self.assertGreaterEqual(float(img[0, 0, 2]), 0.99)

                # 起動時送信: force=True, mark_daily_sent=False なら09:00枠を消費せずテキスト+画像送信
                self.assertTrue(
                    rtt.check_and_send_daily_14d_report(
                        current_balance=99994.34,
                        db_path=db_path,
                        force=True,
                        mark_daily_sent=False,
                        label="起動時 14日間レポート",
                        now_jst=before_9_jst,
                        chart_output_path=chart_png,
                    )
                )
                self.assertEqual(fake_discord.print_log.call_count, 1)
                self.assertIn("起動時 14日間レポート", fake_discord.print_log.call_args[0][0])
                self.assertEqual(fake_discord.send_file.call_count, 1)
                self.assertEqual(Path(fake_discord.send_file.call_args[0][0]), chart_png)
                self.assertFalse(state_json.exists())

                # 09:00 JST 未満 (08:55) および 09:00 台以外 (10:00) は定時送信しない
                self.assertFalse(
                    rtt.check_and_send_daily_14d_report(
                        current_balance=99994.34,
                        db_path=db_path,
                        exact_hour=True,
                        now_jst=before_9_jst,
                        chart_output_path=chart_png,
                    )
                )
                self.assertFalse(
                    rtt.check_and_send_daily_14d_report(
                        current_balance=99994.34,
                        db_path=db_path,
                        exact_hour=True,
                        now_jst=datetime(2026, 11, 19, 10, 0, 0, tzinfo=JST),
                        chart_output_path=chart_png,
                    )
                )
                self.assertEqual(fake_discord.print_log.call_count, 1)
                self.assertEqual(fake_discord.send_file.call_count, 1)

                # 09:00 台 (09:05) は起動時送信とは別に1日1回送信される
                self.assertTrue(
                    rtt.check_and_send_daily_14d_report(
                        current_balance=99994.34,
                        db_path=db_path,
                        exact_hour=True,
                        mark_daily_sent=True,
                        label="日次 14日間レポート",
                        now_jst=now_jst,
                        chart_output_path=chart_png,
                    )
                )
                self.assertEqual(fake_discord.print_log.call_count, 2)
                self.assertIn("日次 14日間レポート", fake_discord.print_log.call_args[0][0])
                self.assertEqual(fake_discord.send_file.call_count, 2)
                self.assertTrue(state_json.exists())
                saved_state = json.loads(state_json.read_text(encoding="utf-8"))
                self.assertIn("2026-11-19", saved_state)

                # 同日 09:30 の2回目は二重送信防止でスキップ
                self.assertFalse(
                    rtt.check_and_send_daily_14d_report(
                        current_balance=99994.34,
                        db_path=db_path,
                        exact_hour=True,
                        now_jst=now_jst + timedelta(minutes=25),
                        chart_output_path=chart_png,
                    )
                )
                self.assertEqual(fake_discord.print_log.call_count, 2)
                self.assertEqual(fake_discord.send_file.call_count, 2)

    def test_vp_param_grid_mp_values(self):
        for strat in ("val_poc", "vah_poc", "vah_break", "val_break"):
            grid = lbe.get_param_grid(strat)
            self.assertEqual(len(grid), 9)
            self.assertEqual({p["mp"] for p in grid}, {70, 100, 150})


if __name__ == "__main__":
    unittest.main()
