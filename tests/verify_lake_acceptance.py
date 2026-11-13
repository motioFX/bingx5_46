# -*- coding: utf-8 -*-
"""
bingx5_46 データレイク基盤 検収テストスイート (`tests/verify_lake_acceptance.py`)

Muse（あんこ）様 検収基準 (a)〜(f) の全自動・非モック緩和検証:
  (a) 最適化窓と評価窓がコード上も運用上も分離されていること（インサンプル選定不在）
  (b) 未来情報の混入がないこと（cutoff不変テスト・未確定足遮断・因果OI結合）
  (c) 手数料 0.08% (Maker 0.02% + Taker 0.06%) が全擬似約定に算入されていること
  (d) その日の勝者が、保存された評価窓成績・格付けルール・明細集計と完全一致すること
  (e) 現行リスクルール（1日1件・09:00 JSTリセット・-$1.50 CB・24hクールダウン・地合い・出来高ゲート）維持 + 書戻し
  (f) ネットワーク遮断状態でレイク単体 (`lake_reproduce.py`) から同一結果を完全再現できること
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import bingx5_46_1futures_limit as bot_main
import lake_reproduce
from bingx5_46_3logic import build_entry_precision_filter, check_market_regime_safe
from lake_backtest_engine import (
    WalkForwardConfig,
    _grade_eval_cell,
    run_walkforward_from_lake,
    select_eval_winner,
)
from lake_live_bridge import record_live_entry_to_lake, record_live_exit_to_lake
from lake_store import DEFAULT_LAKE_DB_PATH, TIMEFRAME_SECONDS, LakeStore, sec_to_iso


def _populate_synthetic_lake(db_path: Path) -> dict:
    """決定論的データでテスト用データレイクを構築 (Opt 25d + Eval 25d + Warmup 15d + Future 5d)。"""
    lake = LakeStore(db_path=db_path)
    # 基準時刻 cutoff: 2026-10-01 00:00:00 UTC
    cutoff_utc = int(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    start_utc = cutoff_utc - (65 * 86400)
    future_end_utc = cutoff_utc + (5 * 86400)

    n_bars = (future_end_utc - start_utc) // 300
    ts_arr = start_utc + np.arange(n_bars, dtype=np.int64) * 300

    # BTC-USDT + 2アルト (HYPE-USDT, SOL-USDT) の 5m 足 (東京・欧州両方で周期反発+出来高急増を作る)
    rng = np.random.default_rng(42)
    symbols = ["BTC-USDT", "HYPE-USDT", "SOL-USDT"]
    for s_idx, sym in enumerate(symbols):
        t_phase = np.arange(n_bars, dtype=np.float64)
        base_px = 100.0 if sym == "SOL-USDT" else (25.0 if sym == "HYPE-USDT" else 70000.0)
        # 約4時間周期の明確な波 (RSI/VPクロスを多発させる) + ゆるやかな上昇トレンド
        wave = np.sin(t_phase * (2.0 * np.pi / 48.0) + s_idx) * (base_px * 0.015)
        drift = (t_phase / n_bars) * (base_px * 0.08)
        noise = rng.normal(0.0, base_px * 0.001, size=n_bars)
        close = np.maximum(0.1, base_px + wave + drift + noise)
        open_p = np.roll(close, 1)
        open_p[0] = close[0]
        spread = np.abs(rng.normal(0.003, 0.0015, size=n_bars)) * base_px
        high = np.maximum(open_p, close) + spread
        low = np.minimum(open_p, close) - spread
        # 5本ごとに出来高1.8倍〜2.1倍のサージを含める
        vol = np.where((np.arange(n_bars) % 5) == 0, 2200.0, 1000.0)
        df = pd.DataFrame(
            {
                "ts_utc": ts_arr,
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "volume": vol,
                "quote_volume": close * vol,
            }
        )
        # Upsert all bars using future_end_utc as write cap so future bars DO sit inside DB!
        lake.upsert_candles(sym, "5m", df, source="bingx", cutoff_utc=future_end_utc)
        lake.rebuild_higher_timeframes_from_5m(sym, cutoff_utc=future_end_utc)

        # OI/FR (1時間おき、一部にのみ変動値)
        oi_ts = ts_arr[::12]
        df_oi = pd.DataFrame(
            {
                "ts_utc": oi_ts,
                "open_interest": 100000.0 * (1.0 + 0.015 * np.sin(np.arange(len(oi_ts)) / 6.0)),
                "open_interest_val": 1000000.0,
                "mark_price": close[::12],
                "funding_rate": np.full(len(oi_ts), 0.0001),
            }
        )
        lake.upsert_oi_fr(sym, df_oi, source="bingx_oi", cutoff_utc=future_end_utc)

    return {
        "lake": lake,
        "cutoff_utc": cutoff_utc,
        "start_utc": start_utc,
        "future_end_utc": future_end_utc,
    }


class TestDataLakeAcceptance(unittest.TestCase):
    def test_a_opt_eval_separation_no_insample_selection(self):
        """(a) 評価窓のみを大きく改変しても、Stage 1 (最適化窓) の選出パラメータ・成績が1ビットも変わらないこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            db1 = Path(tmp) / "lake_sep.db"
            ctx = _populate_synthetic_lake(db1)
            lake = ctx["lake"]
            cutoff = ctx["cutoff_utc"]

            cfg = WalkForwardConfig(
                opt_days=25,
                eval_days=25,
                timeframes=("5m", "15m"),
                min_eval_trades_provisional=5,
                gil_yield_sec=0.0,
            )
            override_regime = {
                "overall_regime": "BUY",
                "regime_pattern": "(B,B,B)",
                "regime_strength": {"tier": "ULTRA_BUY", "net_score": 0.9, "mean_buy_votes": 20.0, "mean_sell_votes": 1.0},
                "allowed_strategies": ["val_poc", "rsima", "vah_break"],
                "recommended_symbols": ["HYPE-USDT", "SOL-USDT"],
                "excluded_symbols": {},
            }

            res_before = run_walkforward_from_lake(
                lake, data_cutoff_utc=cutoff, cfg=cfg, override_regime_info=override_regime, persist=True
            )
            run_row = res_before["run"]
            self.assertLessEqual(run_row["opt_end_utc"], run_row["eval_start_utc"])
            self.assertGreater(len(res_before["cells"]), 0)

            # 評価窓 (eval_start_utc <= ts_utc < cutoff) の価格・出来高を意図的に暴落改変する
            eval_start = int(run_row["eval_start_utc"])
            with lake._conn(write=True) as conn:
                conn.execute(
                    "UPDATE market_candles SET close = close * 0.65, high = high * 0.68, low = low * 0.60, open = open * 0.65 "
                    "WHERE ts_utc >= ? AND ts_utc <= ?",
                    (eval_start, cutoff),
                )

            res_after = run_walkforward_from_lake(
                lake, data_cutoff_utc=cutoff, cfg=cfg, override_regime_info=override_regime, persist=False
            )

            before_opt = {(c["symbol"], c["strategy"], c["timeframe"]): (c["params_json"], c["opt_trades"], c["opt_net_pnl"], c["opt_ev"]) for c in res_before["cells"]}
            after_opt = {(c["symbol"], c["strategy"], c["timeframe"]): (c["params_json"], c["opt_trades"], c["opt_net_pnl"], c["opt_ev"]) for c in res_after["cells"]}
            self.assertEqual(before_opt, after_opt, "Stage 1 最適化窓の結果が評価窓の変更に影響されました (インサンプルリーク)")

            # Stage 2 評価窓成績自体は暴落改変により変化しているはず (テスト自体の有効性検証)
            before_eval = sum(c["eval_net_pnl"] for c in res_before["cells"])
            after_eval = sum(c["eval_net_pnl"] for c in res_after["cells"])
            self.assertNotAlmostEqual(before_eval, after_eval, places=4)

    def test_b_no_future_leakage_invariance_and_closed_bars(self):
        """(b) cutoff=T 以降および未確定足をDBへ後から極端改変追記しても、cutoff=T の全出力が完全不変であること。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lake_leak.db"
            ctx = _populate_synthetic_lake(db_path)
            lake = ctx["lake"]
            cutoff = ctx["cutoff_utc"]

            cfg = WalkForwardConfig(opt_days=25, eval_days=25, timeframes=("5m", "15m"), min_eval_trades_provisional=5, gil_yield_sec=0.0)
            res_1 = run_walkforward_from_lake(
                lake, data_cutoff_utc=cutoff, cfg=cfg, symbols=["HYPE-USDT", "SOL-USDT"], persist=False
            )

            # cutoff と同じ〜未来の足 (一部は cutoff を跨ぐ未確定足: ts = cutoff - 60, close_time = cutoff + 240 > cutoff) を極端な値で汚染
            for tf in ("5m", "15m", "1h", "4h"):
                tf_sec = TIMEFRAME_SECONDS[tf]
                df_loaded = lake.load_candles("HYPE-USDT", tf, cutoff_utc=cutoff)
                self.assertTrue((df_loaded["ts_utc"] + tf_sec <= cutoff).all(), f"{tf} に未確定足が含まれています")

            with lake._conn(write=True) as conn:
                # 未来の確定足 (ts >= cutoff) を +900% へ破壊的変更
                conn.execute(
                    "UPDATE market_candles SET high = high * 10.0, low = low * 0.1, close = close * 9.0, volume = volume * 50.0 "
                    "WHERE ts_utc >= ?",
                    (cutoff,),
                )
                # 未確定足境界テスト: 1h足で ts_utc = cutoff - 1800 -> ts_utc + 3600 = cutoff + 1800 > cutoff
                conn.execute(
                    "INSERT OR REPLACE INTO market_candles (symbol, timeframe, ts_utc, ts_iso, open, high, low, close, volume, quote_volume, source) "
                    "VALUES (?, '1h', ?, ?, 99999, 99999, 1, 99999, 999999, 999999, 'leak_probe')",
                    ("HYPE-USDT", cutoff - 1800, sec_to_iso(cutoff - 1800)),
                )
                # 未来OIスパイク注入
                conn.execute(
                    "UPDATE market_oi_fr SET open_interest = open_interest * 50.0, funding_rate = -0.05 WHERE ts_utc > ?",
                    (cutoff,),
                )

            res_2 = run_walkforward_from_lake(
                lake, data_cutoff_utc=cutoff, cfg=cfg, symbols=["HYPE-USDT", "SOL-USDT"], persist=False
            )

            self.assertEqual(res_1["regime_info"]["overall_regime"], res_2["regime_info"]["overall_regime"])
            self.assertEqual(res_1["cells"], res_2["cells"], "未来情報の注入により cutoff=T の結果が変化しました")
            self.assertEqual(res_1["sim_trades"], res_2["sim_trades"], "未来情報の注入により約定履歴が変化しました")
            self.assertEqual(res_1["selection"]["winner_symbol"], res_2["selection"]["winner_symbol"])

    def test_c_fees_008_pct_and_maker_simulation_arithmetic(self):
        """(c) 往復 0.08% 手数料 (Maker 0.02% + Taker 0.06%)・Makerキャンセル・保守的SL先行がサンプル手計算と完全一致すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lake_fees.db"
            ctx = _populate_synthetic_lake(db_path)
            lake = ctx["lake"]
            cutoff = ctx["cutoff_utc"]

            cfg = WalkForwardConfig(
                opt_days=25,
                eval_days=25,
                timeframes=("5m", "15m"),
                min_eval_trades_provisional=5,
                enforce_vol_surge=False,  # 多様なTP/SL/Long/Shortサンプルを十分確保
                gil_yield_sec=0.0,
            )
            override_regime = {
                "overall_regime": "BUY",
                "regime_pattern": "(B,B,B)",
                "regime_strength": {"tier": "ULTRA_BUY", "net_score": 0.8, "mean_buy_votes": 19.0, "mean_sell_votes": 2.0},
                "allowed_strategies": ["val_poc", "vah_break", "rsima", "vah_poc", "val_break", "rsima_short"],
                "recommended_symbols": ["HYPE-USDT", "SOL-USDT"],
                "excluded_symbols": {},
            }
            res = run_walkforward_from_lake(lake, data_cutoff_utc=cutoff, cfg=cfg, override_regime_info=override_regime, persist=True)

            filled_trades = [t for t in res["sim_trades"] if t.get("fill_ts_utc") is not None]
            cancelled_trades = [t for t in res["sim_trades"] if t.get("cancel_reason") == "CANCEL_SIGNAL_END"]
            self.assertGreater(len(filled_trades), 10, "約定トレードサンプルが不足しています")
            self.assertGreater(len(cancelled_trades), 0, "Maker未約定キャンセル (`CANCEL_SIGNAL_END`) が発生していません")

            # 手計算とのビット/浮動小数点精度一致検証 (全約定トレード)
            for t in filled_trades:
                pos_cost = float(t["pos_cost_usdt"])
                qty = float(t["qty"])
                fill_px = float(t["fill_px"])
                exit_px = float(t["exit_px"])

                expected_entry_fee = pos_cost * 0.0002
                expected_exit_fee = (qty * exit_px) * 0.0006
                if t["side"] == "long":
                    expected_gross = (exit_px - fill_px) * qty
                else:
                    expected_gross = (fill_px - exit_px) * qty
                expected_net = expected_gross - expected_entry_fee - expected_exit_fee

                self.assertAlmostEqual(float(t["entry_fee_usdt"]), expected_entry_fee, places=7)
                self.assertAlmostEqual(float(t["exit_fee_usdt"]), expected_exit_fee, places=7)
                self.assertAlmostEqual(float(t["gross_pnl"]), expected_gross, places=5)
                self.assertAlmostEqual(float(t["net_pnl"]), expected_net, places=6)
                self.assertIsNotNone(t["ext_pct"])
                self.assertIsNotNone(t["ext_atr"])
                # 約定当足 (exit_ts == fill_ts + 300) では偽TP利確を絶対に許さない (SLのみ許可)
                if int(t["exit_ts_utc"]) == int(t["fill_ts_utc"]) + 300:
                    self.assertIn("SL", str(t["exit_reason"]))

            for ct in cancelled_trades:
                self.assertIsNone(ct["fill_ts_utc"])
                self.assertEqual(float(ct["net_pnl"]), 0.0)
                self.assertEqual(float(ct["entry_fee_usdt"]), 0.0)
                self.assertEqual(float(ct["exit_fee_usdt"]), 0.0)

    def test_d_winner_matches_eval_stats_and_grading(self):
        """(d) 勝者選定が評価窓成績・格付け (`n<10`不可, `10<=n<30`参考, `n>=30`本採用)・明細再集計と完全一致すること。"""
        cfg = WalkForwardConfig()
        self.assertEqual(_grade_eval_cell(9, 1.50, cfg), "INSUFFICIENT_N")
        self.assertEqual(_grade_eval_cell(10, -0.01, cfg), "REJECTED_EV")
        self.assertEqual(_grade_eval_cell(25, 0.20, cfg), "PROVISIONAL")
        self.assertEqual(_grade_eval_cell(30, 0.05, cfg), "QUALIFIED")

        sample_cells = [
            {"symbol": "A-USDT", "strategy": "val_poc", "timeframe": "5m", "eval_trades": 9, "eval_ev": 5.0, "eval_pf": 9.0, "eval_net_pnl": 45.0, "eval_grade": "INSUFFICIENT_N"},
            {"symbol": "B-USDT", "strategy": "val_poc", "timeframe": "5m", "eval_trades": 25, "eval_ev": 0.8, "eval_pf": 3.0, "eval_net_pnl": 20.0, "eval_grade": "PROVISIONAL"},
            {"symbol": "C-USDT", "strategy": "rsima", "timeframe": "15m", "eval_trades": 32, "eval_ev": 0.3, "eval_pf": 1.8, "eval_net_pnl": 9.6, "eval_grade": "QUALIFIED"},
        ]
        # QUALIFIED (n>=30) が PROVISIONAL (n<30) や INSUFFICIENT_N より優先される
        picked = select_eval_winner(sample_cells, cfg)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["symbol"], "C-USDT")
        # 全滅時は None (NO_CANDIDATE)
        self.assertIsNone(select_eval_winner([sample_cells[0]], cfg))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lake_win.db"
            ctx = _populate_synthetic_lake(db_path)
            lake = ctx["lake"]
            cutoff = ctx["cutoff_utc"]
            wf_cfg = WalkForwardConfig(
                opt_days=25, eval_days=25, timeframes=("5m", "15m"), enforce_vol_surge=False,
                min_eval_trades_provisional=10, min_eval_trades_qualified=30, gil_yield_sec=0.0
            )
            override_regime = {
                "overall_regime": "BUY",
                "regime_pattern": "(B,B,B)",
                "regime_strength": {"tier": "ULTRA_BUY", "net_score": 0.9, "mean_buy_votes": 20.0, "mean_sell_votes": 1.0},
                "allowed_strategies": ["val_poc", "rsima", "vah_break"],
                "recommended_symbols": ["HYPE-USDT", "SOL-USDT"],
                "excluded_symbols": {},
            }
            res = run_walkforward_from_lake(lake, data_cutoff_utc=cutoff, cfg=wf_cfg, override_regime_info=override_regime, persist=True)
            sel = lake.get_selection_for_run(res["run"]["run_id"])
            self.assertIsNotNone(sel)
            if sel["winner_cell_id"] is not None:
                with lake._conn(write=False) as conn:
                    cell = dict(conn.execute("SELECT * FROM backtest_cells WHERE cell_id = ?", (sel["winner_cell_id"],)).fetchone())
                    agg = dict(conn.execute(
                        "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl), 0.0) AS s_net FROM backtest_simulated_trades "
                        "WHERE cell_id = ? AND fill_ts_utc IS NOT NULL",
                        (sel["winner_cell_id"],),
                    ).fetchone())
                self.assertEqual(sel["winner_symbol"], cell["symbol"])
                self.assertEqual(sel["winner_strategy"], cell["strategy"])
                self.assertEqual(sel["eval_trades"], cell["eval_trades"])
                self.assertEqual(int(agg["n"]), int(sel["eval_trades"]))
                self.assertAlmostEqual(float(sel["eval_net_pnl"]), float(cell["eval_net_pnl"]), places=6)
                self.assertAlmostEqual(float(agg["s_net"]), float(sel["eval_net_pnl"]), places=4)
                self.assertAlmostEqual(float(sel["eval_ev"]), float(cell["eval_ev"]), places=6)

    def test_e_risk_rules_and_live_writeback(self):
        """(e) 現行リスクルール (1日1件・09:00 JSTリセット・CB -$1.50・24h SLクールダウン・出来高1.5x・地合い制限) と実運用書戻し検証。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            orig_daily_file = bot_main.DAILY_TRADE_STATE_FILE
            orig_cd_file = bot_main.SL_COOLDOWN_FILE
            try:
                bot_main.DAILY_TRADE_STATE_FILE = tmp_dir / "daily_trade_state.json"
                bot_main.SL_COOLDOWN_FILE = tmp_dir / "sl_cooldowns.json"

                # 1. JST 08:59 vs 09:00 リセット検証 (09:00 JST = 00:00 UTC 境界)
                dt_0859_jst = datetime(2026, 11, 13, 8, 59, tzinfo=timezone(bot_main.timedelta(hours=9)))
                dt_0900_jst = datetime(2026, 11, 13, 9, 0, tzinfo=timezone(bot_main.timedelta(hours=9)))
                with mock.patch.object(bot_main, "datetime") as mock_dt:
                    mock_dt.now.return_value = dt_0859_jst.astimezone(timezone.utc)
                    self.assertEqual(bot_main.get_current_trading_day(), "2026-11-12")
                    mock_dt.now.return_value = dt_0900_jst.astimezone(timezone.utc)
                    self.assertEqual(bot_main.get_current_trading_day(), "2026-11-13")

                # 2. 日次上限 (1日1トレード)
                ok0, _, cnt0 = bot_main.can_open_new_trade_today(unrealized_pnl=0.0)
                self.assertTrue(ok0)
                self.assertEqual(cnt0, 0)

                # 含み損サーキットブレーカー (-1.55 <= -1.50)
                ok_cb, reason_cb, _ = bot_main.can_open_new_trade_today(unrealized_pnl=-1.55)
                self.assertFalse(ok_cb)
                self.assertIn("サーキットブレーカー", reason_cb)

                # 1トレード完了後ブロック
                bot_main.record_daily_trade_completion("HYPE-USDT", "LONG", pnl=0.45, reason="TARGET_TP")
                ok1, reason1, cnt1 = bot_main.can_open_new_trade_today(unrealized_pnl=0.0)
                self.assertFalse(ok1)
                self.assertEqual(cnt1, 1)

                # 3. 24時間 SL クールダウン
                self.assertFalse(bot_main.is_in_sl_cooldown("SOL-USDT")[0])
                bot_main.record_sl_cooldown("SOL-USDT")
                in_cd, rem_h = bot_main.is_in_sl_cooldown("SOL-USDT")
                self.assertTrue(in_cd)
                self.assertGreater(rem_h, 23.0)

                # 4. 出来高 1.5倍サージゲート
                n_v = 25
                df_vol = pd.DataFrame({
                    "volume": [100.0] * 24 + [110.0],  # 末尾は 1.1x (< 1.5x)
                    "oi_chg_4h": [1.0] * n_v,
                    "fundingRate": [0.0001] * n_v,
                })
                mask_low = build_entry_precision_filter(df_vol, side="long", is_vp=True, is_breakout=False)
                self.assertFalse(bool(mask_low[-1]), "出来高 1.1x が 1.5x ゲートを通過してしまいました")
                df_vol.loc[24, "volume"] = 180.0  # 1.8x (>= 1.5x)
                mask_hi = build_entry_precision_filter(df_vol, side="long", is_vp=True, is_breakout=False)
                self.assertTrue(bool(mask_hi[-1]), "出来高 1.8x が 1.5x ゲートで不正拒否されました")

                # 5. 地合い制限 (SELL地合いでの買いガード)
                safe_ok, safe_msg, _ = check_market_regime_safe(market_state_info={"overall_regime": "SELL"})
                self.assertFalse(safe_ok)
                self.assertIn("SELL_BLOCKED", safe_msg)

                # 6. live_trades 書戻し ＆ daily_selections.status = 'EXECUTED' 連動
                db_path = tmp_dir / "lake_live.db"
                ctx = _populate_synthetic_lake(db_path)
                lake = ctx["lake"]
                cutoff = ctx["cutoff_utc"]
                wf_cfg = WalkForwardConfig(
                    opt_days=25, eval_days=25, timeframes=("5m",), enforce_vol_surge=False,
                    min_eval_trades_provisional=5, gil_yield_sec=0.0
                )
                override_regime = {
                    "overall_regime": "BUY",
                    "regime_pattern": "(B,B,B)",
                    "regime_strength": {"tier": "ULTRA_BUY", "net_score": 0.9, "mean_buy_votes": 20.0, "mean_sell_votes": 1.0},
                    "allowed_strategies": ["val_poc", "rsima"],
                    "recommended_symbols": ["HYPE-USDT", "SOL-USDT"],
                    "excluded_symbols": {},
                }
                res = run_walkforward_from_lake(lake, data_cutoff_utc=cutoff, cfg=wf_cfg, override_regime_info=override_regime, persist=True)
                sel = res["selection"]
                self.assertEqual(sel["status"], "SELECTED")
                entry_meta = record_live_entry_to_lake(
                    symbol=sel["winner_symbol"],
                    side=sel["winner_side"],
                    entry_px=25.0,
                    qty=0.6,
                    params={
                        "strategy": sel["winner_strategy"],
                        "timeframe": sel["winner_timeframe"],
                        "lake_selection_id": sel["selection_id"],
                        "lake_run_id": sel["run_id"],
                    },
                    entry_ts_utc=cutoff,
                    db_path=db_path,
                )
                record_live_exit_to_lake(
                    symbol=sel["winner_symbol"],
                    side=sel["winner_side"],
                    entry_px=25.0,
                    exit_px=25.5,
                    qty=0.6,
                    net_pnl_usdt=0.288,
                    exit_reason="TARGET_TP",
                    state_meta={
                        **entry_meta,
                        "strategy": sel["winner_strategy"],
                        "timeframe": sel["winner_timeframe"],
                        "entry_ts_utc": cutoff,
                        "qty": 0.6,
                    },
                    exit_ts_utc=cutoff + 900,
                    db_path=db_path,
                )
                sel_after = lake.get_selection_for_run(res["run"]["run_id"])
                self.assertEqual(sel_after["status"], "EXECUTED")
                with lake._conn(write=False) as conn:
                    lt = dict(conn.execute("SELECT * FROM live_trades WHERE selection_id = ?", (sel["selection_id"],)).fetchone())
                self.assertAlmostEqual(float(lt["net_pnl_usdt"]), 0.288, places=4)
                self.assertIsNotNone(lt["ext_pct_at_entry"])
                self.assertIsNotNone(lt["ext_atr_at_entry"])
            finally:
                bot_main.DAILY_TRADE_STATE_FILE = orig_daily_file
                bot_main.SL_COOLDOWN_FILE = orig_cd_file

    def test_f_offline_reproducibility_real_and_synthetic_db(self):
        """(f) ネットワーク完全遮断状態で `Data/lake/bingx_lake.db` 単体から同一バックテスト結果を再現できること。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lake_repro.db"
            ctx = _populate_synthetic_lake(db_path)
            lake = ctx["lake"]
            cutoff = ctx["cutoff_utc"]

            cfg = WalkForwardConfig(opt_days=25, eval_days=25, timeframes=("5m", "15m"), enforce_vol_surge=False, min_eval_trades_provisional=5, gil_yield_sec=0.0)
            res = run_walkforward_from_lake(
                lake,
                data_cutoff_utc=cutoff,
                cfg=cfg,
                symbols=["HYPE-USDT", "SOL-USDT"],
                allowed_strategies_override=["val_poc", "rsima"],
                persist=True,
            )
            run_id = res["run"]["run_id"]

            # ネットワーク関数をすべて遮断して完全オフライン再現検証
            with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("OFFLINE_NO_NETWORK")), \
                 mock.patch("requests.get", side_effect=RuntimeError("OFFLINE_NO_NETWORK")), \
                 mock.patch("requests.post", side_effect=RuntimeError("OFFLINE_NO_NETWORK")):
                repro = lake_reproduce.reproduce_run(run_id, db_path=db_path)
            self.assertTrue(repro["matched"], f"再現不一致: {repro['mismatches']}")
            self.assertGreater(repro["cells_checked"], 0)

        # 実データレイク (migrate_to_lake 生成済み DB) 上でも 1 run 保存→オフライン再現検証
        if DEFAULT_LAKE_DB_PATH.exists():
            real_lake = LakeStore(DEFAULT_LAKE_DB_PATH)
            btc_st = real_lake.get_candle_stats("BTC-USDT", "5m")
            if btc_st["max_ts_utc"]:
                cutoff_real = int(btc_st["max_ts_utc"]) + 300
                cfg_real = WalkForwardConfig(opt_days=60, eval_days=60, timeframes=("15m",), gil_yield_sec=0.0)
                with mock.patch("requests.get", side_effect=RuntimeError("OFFLINE_NO_NETWORK")):
                    real_res = run_walkforward_from_lake(
                        real_lake,
                        data_cutoff_utc=cutoff_real,
                        cfg=cfg_real,
                        symbols=["HYPE-USDT", "SOL-USDT"],
                        allowed_strategies_override=["val_poc", "rsima"],
                        persist=True,
                    )
                    repro_real = lake_reproduce.reproduce_run(real_res["run"]["run_id"], db_path=DEFAULT_LAKE_DB_PATH)
                self.assertTrue(repro_real["matched"], f"実DB再現不一致: {repro_real['mismatches']}")
                # 検収用の一時テストrunを削除して実DBをクリーンに保つ
                with real_lake._conn(write=True) as conn:
                    conn.execute("DELETE FROM daily_selections WHERE run_id = ?", (real_res["run"]["run_id"],))
                    conn.execute("DELETE FROM backtest_runs WHERE run_id = ?", (real_res["run"]["run_id"],))


if __name__ == "__main__":
    unittest.main(verbosity=2)
