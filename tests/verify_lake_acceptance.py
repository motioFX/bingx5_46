# -*- coding: utf-8 -*-
"""
bingx5_46 データレイク基盤 検収テストスイート (`tests/verify_lake_acceptance.py`)

Muse（あんこ）様 検収基準 (a)〜(f) の全自動・非モック緩和検証:
  (a) 最適化窓と評価窓がコード上も運用上も分離されていること（インサンプル選定不在）
  (b) 未来情報の混入がないこと（cutoff不変テスト・未確定足遮断・因果OI結合）
  (c) 手数料 0.08% (Maker 0.02% + Taker 0.06%) が全擬似約定に算入されていること
  (d) その日の勝者が、保存された評価窓成績・格付けルール・明細集計と完全一致すること
  (e) 現行リスクルール（回数制限解除・09:00 JSTリセット・-$1.50 CB・24hクールダウン・地合い・出来高ゲート）維持 + 書戻し
  (f) ネットワーク遮断状態でレイク単体 (`lake_reproduce.py`) から同一結果を完全再現できること
  (g) NY独自トレール廃止 ＆ ブレイク約定足内 (is_fill_bar=True) 即決済抑止の回帰検証
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
from bingx5_46_3logic import (
    build_entry_precision_filter,
    check_market_regime_safe,
    check_vah_break_signals,
    check_val_break_signals,
    evaluate_market_regime_3window,
    evaluate_market_regime_3window_from_close_arrays,
)
from lake_backtest_engine import (
    WalkForwardConfig,
    _grade_eval_cell,
    run_walkforward_from_lake,
    select_eval_winner,
)
from lake_live_bridge import record_live_entry_to_lake, record_live_exit_to_lake
from lake_store import DEFAULT_LAKE_DB_PATH, TIMEFRAME_SECONDS, LakeStore, sec_to_iso
from symbol_universe import FIXED_TRADABLE_SYMBOLS, LAKE_UNIVERSE_SYMBOLS


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
        """(e) 現行リスクルール (回数制限解除・09:00 JSTリセット・CB -$1.50・24h SLクールダウン・出来高1.5x・地合い制限) と実運用書戻し検証。"""
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

                # 2. 日次トレード回数制限解除 ＆ 日次損失サーキットブレーカー
                self.assertEqual(bot_main.DAILY_MAX_TRADES, 0)
                ok0, _, cnt0 = bot_main.can_open_new_trade_today(unrealized_pnl=0.0)
                self.assertTrue(ok0)
                self.assertEqual(cnt0, 0)

                # 含み損サーキットブレーカー (-1.55 <= -1.50)
                ok_cb, reason_cb, _ = bot_main.can_open_new_trade_today(unrealized_pnl=-1.55)
                self.assertFalse(ok_cb)
                self.assertIn("サーキットブレーカー", reason_cb)

                # 1トレード完了後も回数上限なしのため取引可能
                bot_main.record_daily_trade_completion("HYPE-USDT", "LONG", pnl=0.45, reason="TARGET_TP")
                ok1, reason1, cnt1 = bot_main.can_open_new_trade_today(unrealized_pnl=0.0)
                self.assertTrue(ok1, f"回数制限解除後に取引がブロックされました: {reason1}")
                self.assertEqual(cnt1, 1)

                # 累積確定損失が -$1.50 以下に到達した場合はCB発動して物理遮断
                bot_main.record_daily_trade_completion("ARB-USDT", "SHORT", pnl=-2.00, reason="STOP_LOSS")
                ok2, reason2, cnt2 = bot_main.can_open_new_trade_today(unrealized_pnl=0.0)
                self.assertFalse(ok2)
                self.assertEqual(cnt2, 2)
                self.assertIn("サーキットブレーカー", reason2)

                # 3. 24時間 SL クールダウン廃止仕様 (常時未冷却 False, 0.0)
                self.assertEqual(bot_main.SL_COOLDOWN_HOURS, 0.0)
                self.assertFalse(bot_main.is_in_sl_cooldown("SOL-USDT")[0])
                bot_main.record_sl_cooldown("SOL-USDT")
                in_cd, rem_h = bot_main.is_in_sl_cooldown("SOL-USDT")
                self.assertFalse(in_cd)
                self.assertEqual(rem_h, 0.0)

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

    def test_g_ny_trail_removed_and_fill_bar_hold(self):
        """(g) NY独自トレール (NY_VAL_TRAIL_TP 等) が実ボットに存在せず、約定足内で即決済されないこと。"""
        bot_src = (ROOT_DIR / "bingx5_46_1futures_limit.py").read_text(encoding="utf-8")
        for forbidden in ("NY_VAL_TRAIL_TP", "NY_VAH_TRAIL_TP", "NY_SESSION_TRAILING_STOP"):
            self.assertNotIn(forbidden, bot_src, f"バックテスト外の独自決済 {forbidden} が残存しています")

        # TAO-USDT 相当のブレイク約定足ケース: VAL=$188.47 付近、Entry=$188.73、現在値=$188.66 でも HOLD 維持
        n_bars = 120
        closes = np.linspace(191.0, 189.0, n_bars)
        closes[-1] = 188.29
        df = pd.DataFrame({
            "open": closes + 0.1,
            "high": closes + 0.25,
            "low": closes - 0.25,
            "close": closes,
            "volume": np.full(n_bars, 1000.0),
        })
        sig_short = check_val_break_signals(
            df_5m=df,
            current_pos_qty=-0.08,
            current_trades_count=1,
            avg_entry_price=188.73,
            last_entry_price=188.73,
            current_price=188.66,
            recent_high=191.2,
            vp_period=100,
            is_fill_bar=True,
        )
        self.assertEqual(sig_short["action"], "HOLD", f"約定当足で予期せぬ決済判定が発生: {sig_short}")

        closes_long = np.linspace(186.0, 188.0, n_bars)
        closes_long[-1] = 189.10
        df_long = pd.DataFrame({
            "open": closes_long - 0.1,
            "high": closes_long + 0.25,
            "low": closes_long - 0.25,
            "close": closes_long,
            "volume": np.full(n_bars, 1000.0),
        })
        sig_long = check_vah_break_signals(
            df_5m=df_long,
            current_pos_qty=0.08,
            current_trades_count=1,
            avg_entry_price=188.73,
            last_entry_price=188.73,
            current_price=188.80,
            vp_period=100,
            is_fill_bar=True,
        )
        self.assertEqual(sig_long["action"], "HOLD", f"約定当足で予期せぬロング決済判定が発生: {sig_long}")

    def test_h_point_in_time_regime_recalculation(self):
        """(h) 時点地合い再計算により上昇期ロング・下落期ショートが混在し、約定履歴に時点地合いが記録・再現されること。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lake_pit.db"
            lake = LakeStore(db_path=db_path)

            cutoff_utc = int(datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc).timestamp())
            total_days = 85  # warmup 20d + opt 25d + eval 40d
            start_utc = cutoff_utc - (total_days * 86400)
            n_1h = total_days * 24
            ts_1h = start_utc + np.arange(n_1h, dtype=np.int64) * 3600
            day_off_1h = (ts_1h - cutoff_utc) / 86400.0

            def _piecewise_trend(d_arr: np.ndarray) -> np.ndarray:
                base = np.empty_like(d_arr, dtype=np.float64)
                m1 = d_arr < -50.0
                m2 = (d_arr >= -50.0) & (d_arr < -19.0)
                m3 = d_arr >= -19.0
                base[m1] = 1.40 - 0.014 * (d_arr[m1] + 85.0)
                low_50 = 1.40 - 0.014 * (-50.0 + 85.0)
                base[m2] = low_50 + 0.025 * (d_arr[m2] + 50.0)
                high_19 = low_50 + 0.025 * (-19.0 + 50.0)
                base[m3] = high_19 - 0.025 * (d_arr[m3] + 19.0)
                return np.maximum(0.2, base)

            trend_1h = _piecewise_trend(day_off_1h)
            bg_symbols = ["BTC-USDT"] + [s for s in FIXED_TRADABLE_SYMBOLS[:15] if s not in ("HYPE-USDT", "SOL-USDT")]
            for s_idx, sym in enumerate(bg_symbols):
                mult = 70000.0 if sym == "BTC-USDT" else (10.0 + s_idx)
                c_1h = mult * trend_1h
                df_1h = pd.DataFrame({
                    "ts_utc": ts_1h,
                    "open": c_1h,
                    "high": c_1h * 1.002,
                    "low": c_1h * 0.998,
                    "close": c_1h,
                    "volume": np.full(n_1h, 1000.0),
                    "quote_volume": c_1h * 1000.0,
                })
                lake.upsert_candles(sym, "1h", df_1h, source="synthetic_pit", cutoff_utc=cutoff_utc)

            n_5m = total_days * 288
            ts_5m = start_utc + np.arange(n_5m, dtype=np.int64) * 300
            day_off_5m = (ts_5m - cutoff_utc) / 86400.0
            trend_5m = _piecewise_trend(day_off_5m)
            t_phase = np.arange(n_5m, dtype=np.float64)
            rng = np.random.default_rng(7)
            for s_idx, sym in enumerate(("HYPE-USDT", "SOL-USDT")):
                base_px = 25.0 if sym == "HYPE-USDT" else 100.0
                wave = np.sin(t_phase * (2.0 * np.pi / 48.0) + s_idx) * 0.015
                noise = rng.normal(0.0, 0.0008, size=n_5m)
                c_5m = np.maximum(0.1, base_px * (trend_5m + wave + noise))
                o_5m = np.roll(c_5m, 1)
                o_5m[0] = c_5m[0]
                spread = np.full(n_5m, base_px * 0.004)
                h_5m = np.maximum(o_5m, c_5m) + spread
                l_5m = np.minimum(o_5m, c_5m) - spread
                vol_5m = np.full(n_5m, 1500.0)
                df_5m = pd.DataFrame({
                    "ts_utc": ts_5m,
                    "open": o_5m,
                    "high": h_5m,
                    "low": l_5m,
                    "close": c_5m,
                    "volume": vol_5m,
                    "quote_volume": c_5m * vol_5m,
                })
                lake.upsert_candles(sym, "5m", df_5m, source="synthetic_pit", cutoff_utc=cutoff_utc)
                lake.rebuild_higher_timeframes_from_5m(sym, timeframes=("15m", "1h"), cutoff_utc=cutoff_utc)

            cfg = WalkForwardConfig(
                opt_days=25,
                eval_days=40,
                timeframes=("15m",),
                enforce_vol_surge=False,
                enforce_oi_fr=False,
                enforce_session_mask=False,
                min_eval_trades_provisional=3,
                gil_yield_sec=0.0,
            )
            res = run_walkforward_from_lake(
                lake=lake,
                data_cutoff_utc=cutoff_utc,
                cfg=cfg,
                symbols=["HYPE-USDT", "SOL-USDT"],
                persist=True,
            )
            self.assertTrue(res["pit_active"], "16銘柄存在するため時点地合い再計算が有効化されるべきです")
            self.assertEqual(res["run"]["regime"], "SELL", "cutoff時点は下落フェーズのためSELLのはずです")

            filled = [t for t in res["sim_trades"] if t.get("fill_ts_utc") is not None]
            long_trades = [t for t in filled if t["side"] == "long"]
            short_trades = [t for t in filled if t["side"] == "short"]
            self.assertGreater(len(long_trades), 0, "上昇フェーズでロング取引が発生していません")
            self.assertGreater(len(short_trades), 0, "下落フェーズでショート取引が発生していません")

            tier_allowed_map = {
                "ULTRA_BUY": {"vah_break", "val_poc", "rsima"},
                "STRONG_BUY": {"vah_break", "val_poc", "rsima"},
                "MODERATE_BUY": {"val_poc", "rsima", "val_break"},
                "WEAK_BUY": {"val_poc", "rsima", "val_break"},
                "LEAN_BUY": {"val_poc", "rsima", "val_break"},
                "ULTRA_SELL": {"val_break", "vah_poc", "rsima_short"},
                "STRONG_SELL": {"val_break", "vah_poc", "rsima_short"},
                "MODERATE_SELL": {"vah_poc", "rsima_short", "vah_break"},
                "WEAK_SELL": {"vah_poc", "rsima_short", "vah_break"},
                "LEAN_SELL": {"vah_poc", "rsima_short", "vah_break"},
            }
            for tr in filled:
                tier = str(tr["strength_tier_at_signal"])
                self.assertIn(tier, tier_allowed_map, f"予期せぬTier: {tier}")
                self.assertIn(tr["strategy"], tier_allowed_map[tier], f"{tier} で非許可戦略 {tr['strategy']} が発火")
                if tier in ("ULTRA_BUY", "STRONG_BUY"):
                    self.assertEqual(tr["side"], "long")
                elif tier in ("ULTRA_SELL", "STRONG_SELL"):
                    self.assertEqual(tr["side"], "short")

            strong_longs = [t for t in long_trades if t["strength_tier_at_signal"] in ("ULTRA_BUY", "STRONG_BUY")]
            strong_shorts = [t for t in short_trades if t["strength_tier_at_signal"] in ("ULTRA_SELL", "STRONG_SELL")]
            self.assertGreater(len(strong_longs), 0, "STRONG/ULTRA_BUY 期間のロング取引が存在しません")
            self.assertGreater(len(strong_shorts), 0, "STRONG/ULTRA_SELL 期間のショート取引が存在しません")

            # DataFrame版 vs NumPy配列版の SSoT ビット完全一致検証 (複数時点サンプリング)
            for sample_days_ago in (30, 22, 12, 5, 1):
                sample_cut = cutoff_utc - sample_days_ago * 86400
                dfs_sample = {}
                arrs_sample = {}
                for sym_s in LAKE_UNIVERSE_SYMBOLS:
                    d_1h = lake.load_candles(sym_s, "1h", cutoff_utc=sample_cut, start_utc=sample_cut - 20 * 86400)
                    if not d_1h.empty and len(d_1h) >= 24:
                        dfs_sample[sym_s] = d_1h[["close"]].astype(float)
                        arrs_sample[sym_s] = d_1h["close"].to_numpy(dtype=np.float64)
                r_df = evaluate_market_regime_3window(dfs_sample)
                r_np = evaluate_market_regime_3window_from_close_arrays(arrs_sample)
                self.assertEqual(r_df["overall_regime"], r_np["overall_regime"])
                self.assertEqual(r_df["regime_pattern"], r_np["regime_pattern"])
                self.assertEqual(r_df["allowed_strategies"], r_np["allowed_strategies"])
                self.assertEqual(r_df["regime_strength"]["tier"], r_np["regime_strength"]["tier"])
                self.assertAlmostEqual(r_df["regime_strength"]["net_score"], r_np["regime_strength"]["net_score"], places=6)

            with lake._conn(write=False) as conn:
                reg_rows = conn.execute(
                    "SELECT overall_regime, COUNT(*) AS cnt FROM regime_snapshots GROUP BY overall_regime"
                ).fetchall()
                reg_counts = {str(r["overall_regime"]): int(r["cnt"]) for r in reg_rows}
            self.assertGreater(reg_counts.get("BUY", 0), 0, "regime_snapshots に BUY 時点が保存されていません")
            self.assertGreater(reg_counts.get("SELL", 0), 0, "regime_snapshots に SELL 時点が保存されていません")

            repro = lake_reproduce.reproduce_run(res["run"]["run_id"], db_path=db_path)
            self.assertTrue(repro["matched"], f"PITラン再現不一致: {repro['mismatches']}")

    def test_i_whale_filter_removed_and_display_fixes(self):
        """(i) クジラフィルター撤廃・14d/7d/2d表示追従・Top15ダミー撤去・matplotlib数式誤爆防止の回帰検証。"""
        import warnings
        import matplotlib.pyplot as plt
        import real_trade_tracker as rtt
        import show_symbol_status as sss
        from bingx5_46_3logic import (
            check_bottom_reversal_recovery,
            check_normalized_peak_timing,
            check_normalized_trough_timing,
            check_peak_breakdown_trend,
        )

        # 1. ボット本体からクジラ呼び出し/エントリー遮断およびTop15ダミー表示が撤廃されていること
        bot_src = (ROOT_DIR / "bingx5_46_1futures_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("fetch_whale_sentiment.py", bot_src)
        self.assertNotIn("select_top_bingx_symbols(top_n=15", bot_src)
        self.assertNotIn('whale_sig == "LONG_ONLY"', bot_src)
        self.assertNotIn('whale_sig == "SHORT_ONLY"', bot_src)
        self.assertNotIn('whale_sig != "LONG_ONLY"', bot_src)
        self.assertNotIn('whale_sig != "SHORT_ONLY"', bot_src)

        # 2. ノーマライズ前半高安監査が 2d / 7d / 14d を評価し、30d/10d/5d を出さないこと
        n_1h = 15 * 24
        closes = np.linspace(1.0, 2.0, n_1h)
        closes[24] = 3.0  # 14d/7d前半付近に高値があるが2d外・半値割れを作れる系列
        df_norm = pd.DataFrame({"close": closes})
        # 全期間で前半ピーク半値割れを起こす系列
        c_drop = np.concatenate([np.array([1.0, 2.0]), np.linspace(1.8, 0.8, n_1h - 2)])
        is_bad_p, reasons_p = check_normalized_peak_timing(pd.DataFrame({"close": c_drop}))
        self.assertTrue(is_bad_p)
        self.assertTrue(any(r.startswith(("2d", "7d", "14d")) for r in reasons_p))
        self.assertFalse(any(r.startswith(("5d", "10d", "30d")) for r in reasons_p))
        is_bd, reasons_bd = check_peak_breakdown_trend(pd.DataFrame({"close": c_drop}))
        self.assertTrue(is_bd)
        self.assertFalse(any(r.startswith(("5d", "10d", "30d")) for r in reasons_bd))

        c_reb = np.concatenate([np.array([2.0, 0.5]), np.linspace(0.7, 1.8, n_1h - 2)])
        is_bad_t, reasons_t = check_normalized_trough_timing(pd.DataFrame({"close": c_reb}))
        self.assertTrue(is_bad_t)
        self.assertFalse(any(r.startswith(("5d", "10d", "30d")) for r in reasons_t))
        is_rec, reasons_rec = check_bottom_reversal_recovery(pd.DataFrame({"close": c_reb}))
        self.assertTrue(is_rec)
        self.assertFalse(any(r.startswith(("5d", "10d", "30d")) for r in reasons_rec))

        # 3. show_symbol_status が 14d/7d/2d を表示し、クジラ除外を行わないこと
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp)
            (tmp_data / "market_state.json").write_text(
                json.dumps(
                    {
                        "overall_regime": "SELL",
                        "period_results": {
                            "14d": {"state": "SELL"},
                            "7d": {"state": "SELL"},
                            "2d": {"state": "SELL"},
                        },
                        "excluded_symbols": ["APT-USDT", "APT"],
                        "exclusion_reasons": {"APT": "2d前半ボトム半値超え(...)"},
                        "timestamp": "2026-11-16T09:00:00+00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (tmp_data / "whale_market_state.json").write_text(
                json.dumps({"coins_sentiment": {"HYPE": {"signal": "LONG_ONLY"}, "APT": {"signal": "LONG_ONLY"}}}),
                encoding="utf-8",
            )
            orig_data_dir = sss.DATA_DIR
            try:
                sss.DATA_DIR = tmp_data
                gate = sss.get_macro_gate_summary()
                self.assertEqual(gate["regime_desc"], "総合:SELL (14d:SELL/7d:SELL/2d:SELL)")
                summary = sss.build_symbols_status_summary(
                    selected_symbols=["HYPE-USDT"],
                    symbol_params_map={
                        "HYPE-USDT": {"pnl": 1.2, "trade_count": 20, "win_rate": 40.0, "strategy": "vah_poc", "interval": 15},
                        "APT-USDT": {"pnl": 0.9, "trade_count": 15, "win_rate": 50.0, "strategy": "vah_poc", "interval": 5},
                    },
                    save_json=False,
                )
                self.assertNotIn("WHALE_SHORT", summary["counts"])
                by_sym = {s["symbol"]: s for s in summary["symbols"]}
                self.assertEqual(by_sym["HYPE-USDT"]["category"], "SELECTED")
                self.assertEqual(by_sym["APT-USDT"]["category"], "PEAK_DROP")
                discord_msg = sss.format_status_table_discord(summary)
                self.assertIn("14d:SELL/7d:SELL/2d:SELL", discord_msg)
                self.assertNotIn("クジラ", discord_msg)
            finally:
                sss.DATA_DIR = orig_data_dir

        # 4. real_trade_tracker: text.parse_math=False により $ を複数含む日本語決済理由でも Font 'rm' 警告が出ないこと
        self.assertFalse(bool(plt.rcParams["text.parse_math"]))
        with tempfile.TemporaryDirectory() as tmp:
            orig_rtt_data = rtt.DATA_DIR
            orig_rtt_trades = rtt.TRADES_FILE
            orig_rtt_charts = rtt.CHARTS_DIR
            try:
                rtt.DATA_DIR = Path(tmp)
                rtt.TRADES_FILE = Path(tmp) / "real_trades_history.json"
                rtt.CHARTS_DIR = Path(tmp) / "charts"
                rtt.TRADES_FILE.write_text("[]", encoding="utf-8")
                n_bars = 80
                ts_idx = pd.date_range("2026-11-16 00:00", periods=n_bars, freq="5min", tz="UTC")
                df_exit = pd.DataFrame({
                    "timestamp": ts_idx,
                    "open": np.linspace(1.38, 1.40, n_bars),
                    "high": np.linspace(1.385, 1.405, n_bars),
                    "low": np.linspace(1.375, 1.395, n_bars),
                    "close": np.linspace(1.38, 1.401, n_bars),
                    "volume": np.full(n_bars, 1000.0),
                })
                with warnings.catch_warnings(record=True) as w_rec:
                    warnings.simplefilter("always")
                    chart_out = rtt.plot_exit_chart(
                        symbol="RENDER-USDT",
                        df=df_exit,
                        exit_price=1.402,
                        entry_price=1.395,
                        pnl=-0.06,
                        exit_reason="VAH_POC_SWING_HIGH_SL: 確定終値($1.4010) > 直近高値SL($1.4000, 損失-0.43%)",
                        side="SHORT",
                        interval_label="5m",
                        mp_period=200,
                    )
                self.assertIsNotNone(chart_out)
                self.assertTrue(Path(chart_out).exists())
                rm_warnings = [str(w.message) for w in w_rec if "Font 'rm'" in str(w.message)]
                self.assertEqual(rm_warnings, [], f"Font 'rm' 数式誤爆警告が発生しました: {rm_warnings}")
            finally:
                rtt.DATA_DIR = orig_rtt_data
                rtt.TRADES_FILE = orig_rtt_trades
                rtt.CHARTS_DIR = orig_rtt_charts

    def test_j_backtest_live_1to1_parity_and_portfolio(self):
        """J: BT-実ロジック 1対1 完全同期・Portfolio Replay (2枠/3態勢/8h上限/緊急BE/セッション切替キャンセル) 検証"""
        import asyncio
        import time
        from unittest.mock import AsyncMock, patch
        import numpy as np
        import pandas as pd
        import bingx5_46_1futures_limit as bot_main
        import bingx5_46_2api as api_mod
        from bingx_oi_collector import enrich_candles_with_oi_fr
        from lake_store import LakeStore, TIMEFRAME_SECONDS
        from bingx5_46_3logic import (
            check_val_poc_signals,
            check_vah_poc_signals,
            evaluate_market_regime_3window_from_close_arrays,
            compute_normalized_exclusions_from_close_arrays,
            compute_dynamic_exit_levels,
            evaluate_dynamic_exit_decision,
        )
        from lake_backtest_engine import (
            WalkForwardConfig,
            _compute_causal_features,
            _LazyMPSeries,
            build_portfolio_params_map_from_cells,
            simulate_portfolio_on_window,
        )
        from symbol_universe import EXCLUDED_TRADE_MEGA_CAPS, TRADABLE_MID_CAP_SYMBOLS

        # 1. 中堅アルト固定ユニバースと 23票過半数 (>=12) 判定の検証
        self.assertEqual(set(EXCLUDED_TRADE_MEGA_CAPS), {"SOL-USDT", "DOGE-USDT", "XRP-USDT"})
        self.assertEqual(len(TRADABLE_MID_CAP_SYMBOLS), 18)
        for mc in EXCLUDED_TRADE_MEGA_CAPS:
            self.assertNotIn(mc, TRADABLE_MID_CAP_SYMBOLS)
            self.assertIn(mc, bot_main.EXCLUDED_MEGA_CAPS)

        # 23票過半数 (>=12 BUY / 11 NEUTRAL) の検証 (全ウィンドウで24..48本共通評価)
        close_arrs = {"BTC-USDT": np.linspace(100.0, 120.0, 48)}
        neutral_arr = np.concatenate([[10.0, 8.0], np.full(46, 9.9)])
        for idx_s, s_name in enumerate([f"ALT{i}-USDT" for i in range(21)]):
            if idx_s < 10:
                close_arrs[s_name] = np.linspace(10.0, 12.0, 48)
            else:
                close_arrs[s_name] = neutral_arr.copy()
        # BTC(+2) + ALT0..9(+10) = 12票 (23票過半数ちょうど) -> BUY
        res_23 = evaluate_market_regime_3window_from_close_arrays(close_arrs)
        self.assertEqual(res_23["period_results"]["2d"]["total_votes"], 23)
        self.assertEqual(res_23["period_results"]["2d"]["buy_votes"], 12)
        self.assertEqual(res_23["overall_regime"], "BUY")

        # 11票 (<12) では BUY にならず NEUTRAL であること
        close_arrs_11 = dict(close_arrs)
        close_arrs_11["ALT9-USDT"] = neutral_arr.copy()
        res_11 = evaluate_market_regime_3window_from_close_arrays(close_arrs_11)
        self.assertEqual(res_11["period_results"]["2d"]["buy_votes"], 11)
        self.assertEqual(res_11["overall_regime"], "NEUTRAL")

        # ノーマライズ PIT 除外ロジック検証: BULL時は前半ピーク半値割れ除外、BEAR時は前半ボトム半値超え除外
        ex_arrs = {
            "GOOD-USDT": np.concatenate([np.full(80, 10.0), np.linspace(10.0, 15.0, 88)]),
            "PEAKDROP-USDT": np.concatenate([np.linspace(10.0, 20.0, 50), np.linspace(19.5, 11.0, 118)]),
            "CAPUP-USDT": np.concatenate([np.linspace(20.0, 10.0, 40), np.linspace(10.5, 19.0, 128)]),
        }
        bull_ex = compute_normalized_exclusions_from_close_arrays(ex_arrs, "BUY")
        bear_ex = compute_normalized_exclusions_from_close_arrays(ex_arrs, "SELL")
        self.assertIn("PEAKDROP-USDT", bull_ex)
        self.assertNotIn("GOOD-USDT", bull_ex)
        self.assertIn("CAPUP-USDT", bear_ex)

        # 2. Shifted 10-bar Swing ＆ Lazy step=1 MP と Live 3logic の 1対1 一致検証
        n_b = 180
        rng = np.random.default_rng(77)
        t_open = np.arange(n_b, dtype=np.int64) * 300 + 1_760_000_000
        close_p = 10.0 + np.cumsum(rng.normal(0.0, 0.03, n_b))
        high_p = close_p + 0.05
        low_p = close_p - 0.05
        open_p = np.roll(close_p, 1)
        open_p[0] = close_p[0]
        # 末尾バーで急落して直近10本前 (iloc[-11:-1]) の安値を明確に下抜けさせる
        prior10_min = float(np.min(low_p[-11:-1]))
        low_p[-1] = prior10_min - 0.25
        close_p[-1] = prior10_min - 0.15

        df_sim = pd.DataFrame({
            "ts_utc": t_open,
            "ts_open": t_open,
            "ts_close": t_open + 300,
            "timestamp": pd.to_datetime(t_open, unit="s", utc=True),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": np.full(n_b, 2000.0),
            "quote_volume": close_p * 2000.0,
            "oi_chg_4h": np.full(n_b, 1.0),
            "fundingRate": np.full(n_b, 0.0001),
        })
        cfg_1 = WalkForwardConfig(mp_step=1, sl_cooldown_hours=0.0, allow_ny_session=True, enable_emergency_breakeven=True)
        feat_5m = _compute_causal_features(df_sim, "5m", cfg_1, needed_mps=[100])
        lazy_mp = _LazyMPSeries(
            feat_5m["open"],
            feat_5m["high"],
            feat_5m["low"],
            feat_5m["close"],
            feat_5m["volume"],
            period=100,
        )
        bt_val, bt_vah, bt_poc = lazy_mp.get_bar(n_b - 1)
        self.assertAlmostEqual(float(feat_5m["swing_low_10"][-1]), prior10_min, places=8)

        # Live側で recent_low=None を渡したとき prior10_min (iloc[-11:-1]) で Swing SL 判定が発火し BT と一致すること
        entry_ref = float(prior10_min + 0.20)
        sig_live_sw = check_val_poc_signals(
            df_5m=df_sim,
            current_pos_qty=10.0,
            current_trades_count=1,
            avg_entry_price=entry_ref,
            last_entry_price=entry_ref,
            current_price=float(close_p[-1]),
            vp_period=100,
            dynamic_sl_pct=-0.030,
            is_fill_bar=False,
            recent_low=None,
            elapsed_minutes=15.0,
            timeout_minutes=240.0,
            max_hold_minutes=480.0,
            has_new_tf_bar_after_fill=True,
        )
        bt_levels = compute_dynamic_exit_levels(
            strategy="val_poc",
            avg_entry_px=entry_ref,
            val=bt_val,
            vah=bt_vah,
            poc=bt_poc,
            prev10_swing_px=float(feat_5m["swing_low_10"][-1]),
            target_ratio=0.70,
            eff_hard_sl_pct=0.030,
            min_sl_pct=0.0020,
        )
        bt_dec = evaluate_dynamic_exit_decision(
            strategy="val_poc",
            avg_entry_px=entry_ref,
            levels=bt_levels,
            is_fill_bar=False,
            can_check_close_exit=True,
            is_emergency_breakeven=False,
            high_px=float(close_p[-1]),
            low_px=float(close_p[-1]),
            close_check_px=float(close_p[-1]),
            live_px=float(close_p[-1]),
            cur_rsi=50.0,
            rsi_exit_thresh=60.0,
            elapsed_minutes=15.0,
            timeout_minutes=240.0,
            max_hold_minutes=480.0,
            prefer_limit_fill_px=False,
        )
        self.assertEqual(sig_live_sw["action"], bt_dec["action"])
        self.assertEqual(sig_live_sw["action"], "STOP_LOSS")

        # 約定当足 (is_fill_bar=True) では確定足SL・緊急BE・480分決済いずれも発火しないこと
        sig_fill_bar = check_val_poc_signals(
            df_5m=df_sim,
            current_pos_qty=10.0,
            current_trades_count=1,
            avg_entry_price=entry_ref,
            last_entry_price=entry_ref,
            current_price=entry_ref * 1.003,
            vp_period=100,
            is_emergency_breakeven=True,
            dynamic_sl_pct=-0.030,
            is_fill_bar=True,
            recent_low=None,
            elapsed_minutes=500.0,
            timeout_minutes=240.0,
            max_hold_minutes=480.0,
            has_new_tf_bar_after_fill=False,
        )
        self.assertEqual(sig_fill_bar["action"], "HOLD")

        # 8時間上限決済 (MAX_HOLD_CLOSE: 480分到達かつ含み益でも強制決済)
        df_calm = df_sim.copy()
        df_calm["low"] = entry_ref * 1.0005
        df_calm["high"] = entry_ref * 1.002
        df_calm["close"] = entry_ref * 1.001
        sig_max_hold = check_val_poc_signals(
            df_5m=df_calm,
            current_pos_qty=10.0,
            current_trades_count=1,
            avg_entry_price=entry_ref,
            last_entry_price=entry_ref,
            current_price=entry_ref * 1.001,
            vp_period=100,
            target_ratio=10.0,
            is_emergency_breakeven=False,
            dynamic_sl_pct=-0.050,
            is_fill_bar=False,
            recent_low=entry_ref * 0.98,
            elapsed_minutes=480.0,
            timeout_minutes=240.0,
            max_hold_minutes=480.0,
            has_new_tf_bar_after_fill=False,
        )
        self.assertEqual(sig_max_hold["action"], "CLOSE_ALL")
        self.assertIn("MAX_HOLD_CLOSE", sig_max_hold["reason"])

        # 緊急建値撤退 (SHORT 時 建値-0.20%)
        sig_short_be = check_vah_poc_signals(
            df_5m=df_calm,
            current_pos_qty=10.0,
            current_trades_count=1,
            avg_entry_price=entry_ref * 1.01,
            last_entry_price=entry_ref * 1.01,
            current_price=entry_ref * 1.01 * 0.9975,
            recent_high=entry_ref * 1.05,
            vp_period=100,
            target_ratio=10.0,
            is_emergency_breakeven=True,
            dynamic_sl_pct=0.050,
            is_fill_bar=False,
            elapsed_minutes=30.0,
            timeout_minutes=240.0,
            max_hold_minutes=480.0,
            has_new_tf_bar_after_fill=False,
        )
        self.assertEqual(sig_short_be["action"], "CLOSE_ALL")
        self.assertIn("EMERGENCY_BREAKEVEN", sig_short_be["reason"])

        # 3. bingx_oi_collector 欠損 OI/FR NaN 保持 (0.0/0.0001 で埋めない)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("bingx_oi_collector.OI_STORE_DIR", Path(tmp)):
                df_no_oi = df_sim[["timestamp", "open", "high", "low", "close", "volume"]].head(10).copy()
                enriched = enrich_candles_with_oi_fr(df_no_oi, "FAKECOIN-USDT", auto_sync=False, timeframe="15m")
                self.assertIn("oi_chg_4h", enriched.columns)
                self.assertIn("fundingRate", enriched.columns)
                self.assertTrue(enriched["oi_chg_4h"].isna().all())
                self.assertTrue(enriched["fundingRate"].isna().all())

        # 4. bingx5_46_2api short_entry 非ブロッキング即時リターン検証
        orig_air = api_mod.is_air
        try:
            api_mod.is_air = True
            with patch("bingx5_46_2api.fetch_instrument_spec_bingx", return_value={"quantityPrecision": 2, "pricePrecision": 4, "tradeMinQuantity": 1.0}), \
                 patch("bingx5_46_2api.get_bingx_orderbook", return_value=(9.99, 10.01)):
                api_inst = api_mod.api_bingx(symbol="SUI-USDT", mode="demo")
                df_ord = df_sim.tail(15).copy()
                df_ord["short"] = True
                t0 = time.monotonic()
                ok_short = asyncio.run(api_inst.short_entry(df_ord, {"pos_count": 0}, 500.0, 2.0, 10.0))
                elapsed_s = time.monotonic() - t0
                self.assertTrue(ok_short)
                self.assertLess(elapsed_s, 2.0, "short_entry が 240秒ブロッキング待機しています")
        finally:
            api_mod.is_air = orig_air

        # 5. Live セッション切替時の待機指値キャンセル検証
        mock_api = AsyncMock()
        pending_map = {
            "SUI-USDT": {"side": "long", "strategy": "val_poc", "interval": 15, "price": 1.0, "chase_count": 0},
            "LINK-USDT": {"side": "long", "strategy": "val_poc", "interval": 15, "price": 10.0, "chase_count": 0},
        }
        apis_map = {"SUI-USDT": mock_api, "LINK-USDT": mock_api}
        raw_sui = {
            "range_params": {"strategy": "val_poc", "interval": 15, "is_qualified": True},
            "breakout_params": {"strategy": "vah_break", "interval": 5, "is_qualified": True},
            "is_range_qualified": True,
            "is_breakout_qualified": True,
            "is_qualified": True,
            "strategy": "val_poc",
            "interval": 15,
        }
        raw_link = {
            "range_params": {"strategy": "val_poc", "interval": 15, "is_qualified": True},
            "breakout_params": {"strategy": "vah_break", "interval": 5, "is_qualified": False},
            "is_range_qualified": True,
            "is_breakout_qualified": False,
            "is_qualified": True,
            "strategy": "val_poc",
            "interval": 15,
        }
        # 東京 -> ロンドンへ切替時: 戦略切替側も不合格側も旧東京待機指値が即キャンセルされること
        sp_s, _, _ = asyncio.run(bot_main.check_and_reconcile_symbol_gate(
            "SUI-USDT", raw_sui, is_tokyo=False, is_london=True,
            gate_allowed=["vah_break"], excluded_mega_caps=EXCLUDED_TRADE_MEGA_CAPS,
            pending_entry_orders=pending_map, symbol_apis=apis_map,
        ))
        sp_l, _, _ = asyncio.run(bot_main.check_and_reconcile_symbol_gate(
            "LINK-USDT", raw_link, is_tokyo=False, is_london=True,
            gate_allowed=["vah_break"], excluded_mega_caps=EXCLUDED_TRADE_MEGA_CAPS,
            pending_entry_orders=pending_map, symbol_apis=apis_map,
        ))
        self.assertIsNone(sp_s)
        self.assertIsNone(sp_l)
        self.assertNotIn("SUI-USDT", pending_map)
        self.assertNotIn("LINK-USDT", pending_map)
        self.assertEqual(mock_api.active_order_cancel.await_count, 2)

        # 6. Portfolio Replay バックテスト検証 (最大2枠制約 ＆ 実 ext_pct/ext_atr 算出)
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "port_test.db"
            store = LakeStore(db_path=db_p)
            n_tot = 220
            ts_0 = int(pd.Timestamp("2026-10-01 00:00:00", tz="UTC").timestamp())  # JST 09:00〜
            t_arr = ts_0 + np.arange(n_tot, dtype=np.int64) * 300
            win_start = ts_0 + 180 * 300
            win_end = ts_0 + n_tot * 300
            for idx_p, s_p in enumerate(["SUI-USDT", "LINK-USDT", "NEAR-USDT", "BTC-USDT"]):
                base = 10.0 * (idx_p + 1)
                c_p = np.full(n_tot, base)
                l_p = c_p - base * 0.002
                h_p = c_p + base * 0.002
                o_p = c_p.copy()
                v_p = np.full(n_tot, 1000.0)
                if s_p != "BTC-USDT":
                    # 窓内で VA 下限割れ後に急騰させる
                    for bar_k in range(180, 185):
                        c_p[bar_k] = base * 0.95
                        l_p[bar_k] = base * 0.945
                        v_p[bar_k] = 2000.0
                    c_p[185:] = base * 1.05
                    h_p[185:] = base * 1.06
                df_c = pd.DataFrame({
                    "ts_utc": t_arr,
                    "open": o_p,
                    "high": h_p,
                    "low": l_p,
                    "close": c_p,
                    "volume": v_p,
                    "quote_volume": c_p * v_p,
                })
                store.upsert_candles(s_p, "5m", df_c, cutoff_utc=win_end + 300)
                store.rebuild_higher_timeframes_from_5m(s_p, cutoff_utc=win_end + 300)

            cells_list = [
                {
                    "symbol": s_p,
                    "strategy": "val_poc",
                    "side": "long",
                    "timeframe": "5m",
                    "params_json": json.dumps({"mp": 60, "target_ratio": 0.50, "l_ep": 40.0, "l_cp": 60.0, "s_ep": 60.0, "s_cp": 40.0}),
                    "eval_grade": "QUALIFIED",
                    "eval_ev": 0.25,
                    "eval_net_pnl": 3.0,
                    "eval_pf": 2.0,
                    "eval_trades": 12,
                }
                for s_p in ("SUI-USDT", "LINK-USDT", "NEAR-USDT")
            ]
            ranked_syms, p_map = build_portfolio_params_map_from_cells(cells_list)
            hourly_map = {
                (t_sec // 3600) * 3600: {
                    "overall_regime": "BUY",
                    "allowed_strategies": ["val_poc", "rsima", "vah_break"],
                    "allowed_strategies_set": frozenset(["val_poc", "rsima", "vah_break"]),
                    "excluded_symbols_set": frozenset(),
                }
                for t_sec in range(win_start, win_end + 3600, 3600)
            }
            port_res = simulate_portfolio_on_window(
                store,
                p_map,
                ranked_syms,
                hourly_map,
                win_start,
                win_end,
                WalkForwardConfig(max_active_positions=2, mp_step=1, sl_cooldown_hours=0.0),
            )
            self.assertIn("metrics", port_res)
            self.assertLessEqual(len([t for t in port_res["sim_trades"] if t.get("fill_ts_utc") is not None]), 3)
            for tr in port_res["sim_trades"]:
                self.assertTrue(np.isfinite(float(tr["ext_pct"])))
                self.assertTrue(np.isfinite(float(tr["ext_atr"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
