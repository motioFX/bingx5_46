# -*- coding: utf-8 -*-
"""F7 Two-Pillar 戦略体系 総合検証テスト (`bingx5_46`)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bingx5_46_1futures_limit as bot_main
from lake_live_bridge import run_f7_screening_for_bot
from lake_store import LakeStore, SUPPORTED_TIMEFRAMES, TIMEFRAME_SECONDS
from strategy_f7_two_pillar import (
    F7_LONG_STRATEGY,
    F7_SHORT_STRATEGY,
    apply_causal_wf_90d_filter,
    build_causal_wf_90d_timeline,
    check_f7_no_exhaustion,
    compute_h3b_squeeze_1h_entry_levels,
    compute_val_poc_30m_entry_levels,
    evaluate_h3b_squeeze_1h_entry,
    evaluate_h3b_squeeze_1h_exit,
    evaluate_val_poc_30m_entry,
    evaluate_val_poc_30m_exit,
    is_eu_session_jst,
    simulate_f7_portfolio,
)


def _make_val_poc_30m_fixture(n_bars: int = 165, end_ts_jst_hour: int = 10) -> Tuple[pd.DataFrame, int]:
    """`val_poc_30m` が最終バーで確実にトリガーする合成 30m 足 DataFrame を生成する."""
    # JST hour -> UTC hour: ts_close_utc % 86400
    base_close_utc = 1_750_000_800  # 1800s 境界
    # jst = utc + 9h -> 調整して sig_close の JST 時刻を end_ts_jst_hour に合わせる
    cur_jst_hr = ((base_close_utc + 9 * 3600) // 3600) % 24
    shift_h = (end_ts_jst_hour - cur_jst_hr) % 24
    last_close_utc = base_close_utc + shift_h * 3600
    ts_opens = np.array([last_close_utc - (n_bars - idx) * 1800 for idx in range(n_bars)], dtype=np.int64)

    # 前半 164 本は 100〜104 の安定レンジ、最終バーで VAL 近辺 (~99.2) へ押し目 & 出来高急増
    closes = np.full(n_bars, 102.0, dtype=np.float64)
    opens = np.full(n_bars, 102.0, dtype=np.float64)
    highs = np.full(n_bars, 104.0, dtype=np.float64)
    lows = np.full(n_bars, 100.0, dtype=np.float64)
    vols = np.full(n_bars, 1000.0, dtype=np.float64)
    for k in range(n_bars - 1):
        base = 101.0 + float(k % 4)
        opens[k] = base - 0.5
        closes[k] = base + 0.5
        lows[k] = base - 1.0
        highs[k] = base + 1.0
    # 最終確定バー: 押し目だが VAL*0.965 <= close <= VAL*1.002 内、かつ vr >= 1.2
    opens[-1] = 101.5
    highs[-1] = 101.8
    lows[-1] = 99.8
    closes[-1] = 100.3
    vols[-1] = 2200.0

    df = pd.DataFrame(
        {
            "ts_utc": ts_opens,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        }
    )
    return df, last_close_utc


def _make_h3b_1h_fixture(n_bars: int = 190) -> Tuple[pd.DataFrame, int]:
    """`h3b_squeeze_1h` が最終バーで BB 圧縮＋出来高急増＋48h 安値割れを起こす合成 1h 足を生成."""
    last_close_utc = 1_750_032_000  # 3600s 境界
    ts_opens = np.array([last_close_utc - (n_bars - idx) * 3600 for idx in range(n_bars)], dtype=np.int64)

    opens = np.full(n_bars, 100.0, dtype=np.float64)
    highs = np.full(n_bars, 101.0, dtype=np.float64)
    lows = np.full(n_bars, 99.0, dtype=np.float64)
    closes = np.full(n_bars, 100.0, dtype=np.float64)
    vols = np.full(n_bars, 1000.0, dtype=np.float64)

    # 先行 120 本は大ボラ (100±6) → q25 水準を広げる
    for k in range(120):
        swing = 6.0 if (k % 2 == 0) else -6.0
        closes[k] = 100.0 + swing
        opens[k] = 100.0
        highs[k] = 107.0
        lows[k] = 93.0

    # 続く ~69 本 (k=120 .. n-2) は超低ボラ収縮 (99.8〜100.2) → shift(1) で sq1h=True になる
    for k in range(120, n_bars - 1):
        closes[k] = 100.0 + (0.05 if k % 2 == 0 else -0.05)
        opens[k] = 100.0
        highs[k] = 100.3
        lows[k] = 99.7

    # 最終バーで出来高 2.5 倍 & 48h 最安値 (99.7) 明確ブレイクダウン
    opens[-1] = 99.8
    highs[-1] = 99.9
    lows[-1] = 97.5
    closes[-1] = 98.0
    vols[-1] = 2500.0

    df = pd.DataFrame(
        {
            "ts_utc": ts_opens,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        }
    )
    return df, last_close_utc


class TestF7TwoPillar(unittest.TestCase):
    def test_safety_locks_and_mode_flags_preserved(self) -> None:
        self.assertFalse(bot_main.ALLOW_LIVE_TRADING)
        self.assertFalse(bot_main.BINGX_IS_LIVE)
        self.assertTrue(bot_main.USE_F7_TWO_PILLAR_MODE)
        self.assertEqual(bot_main.MAX_ACTIVE_POSITIONS, 2)
        self.assertEqual(bot_main.MAX_TRADES_COUNT, 1)
        self.assertEqual(bot_main.resolve_strategy_side(F7_LONG_STRATEGY), "LONG")
        self.assertEqual(bot_main.resolve_strategy_side(F7_SHORT_STRATEGY), "SHORT")

    def test_val_poc_30m_entry_gates_and_exit_rules(self) -> None:
        df_asia, close_ts_asia = _make_val_poc_30m_fixture(end_ts_jst_hour=10)
        res_ok = evaluate_val_poc_30m_entry(df_asia, strength_tier="ULTRA_BUY", is_norm_excluded=False, now_utc=close_ts_asia)
        self.assertTrue(res_ok["should_enter"], res_ok.get("block_reason"))
        self.assertTrue(bool(res_ok["signal_df"]["long"].iloc[-1]))
        self.assertGreaterEqual(res_ok["vol_ratio"], 1.2)

        # Tier gate: STRONG_BUY must NOT enter
        res_strong = evaluate_val_poc_30m_entry(df_asia, strength_tier="STRONG_BUY", is_norm_excluded=False, now_utc=close_ts_asia)
        self.assertFalse(res_strong["should_enter"])
        self.assertIn("TIER_NOT_ULTRA_BUY", res_strong["block_reason"])

        # norm_excl gate
        res_norm = evaluate_val_poc_30m_entry(df_asia, strength_tier="ULTRA_BUY", is_norm_excluded=True, now_utc=close_ts_asia)
        self.assertFalse(res_norm["should_enter"])

        # EU Session gate (16:00 - 22:00 JST)
        df_eu, close_ts_eu = _make_val_poc_30m_fixture(end_ts_jst_hour=17)
        self.assertTrue(is_eu_session_jst(close_ts_eu))
        res_eu = evaluate_val_poc_30m_entry(df_eu, strength_tier="ULTRA_BUY", is_norm_excluded=False, now_utc=close_ts_eu)
        self.assertFalse(res_eu["should_enter"])
        self.assertIn("EU_SESSION_BLOCK", res_eu["block_reason"])

        # Warmup gate
        res_short_bars = evaluate_val_poc_30m_entry(df_asia.iloc[:150], strength_tier="ULTRA_BUY", is_norm_excluded=False, now_utc=close_ts_asia)
        self.assertFalse(res_short_bars["should_enter"])

        # Entry levels: 4% cap SL & +0.6% floor TP
        lv_cap = compute_val_poc_30m_entry_levels(100.0, atr_at_signal=5.0, val=100.0, vah=100.2, poc=100.1)
        self.assertAlmostEqual(lv_cap["init_sl"], 96.0, places=6)
        self.assertAlmostEqual(lv_cap["target_tp"], 100.6, places=6)

        # Exit rules: FlipBE triggers ONLY on SELL (not NEUTRAL) after fill bar
        fill_open = 1_750_000_800
        ex_neutral = evaluate_val_poc_30m_exit(
            entry_price=100.0, entry_atr=1.0, target_tp=103.0, init_sl=98.0,
            fill_bar_open_ts=fill_open, current_price=100.25, overall_regime="NEUTRAL",
            now_utc=fill_open + 1800,
        )
        self.assertFalse(ex_neutral["should_exit"])

        ex_sell_fill = evaluate_val_poc_30m_exit(
            entry_price=100.0, entry_atr=1.0, target_tp=103.0, init_sl=98.0,
            fill_bar_open_ts=fill_open, current_price=100.25, overall_regime="SELL",
            now_utc=fill_open + 60,  # same fill bar -> FlipBE blocked
        )
        self.assertFalse(ex_sell_fill["should_exit"])

        ex_sell_next = evaluate_val_poc_30m_exit(
            entry_price=100.0, entry_atr=1.0, target_tp=103.0, init_sl=98.0,
            fill_bar_open_ts=fill_open, current_price=100.25, overall_regime="SELL",
            now_utc=fill_open + 1800,
        )
        self.assertTrue(ex_sell_next["should_exit"])
        self.assertEqual(ex_sell_next["exit_reason"], "EMERGENCY_BREAKEVEN_TP")

        # MAX_HOLD 1440m (48 * 30m bars)
        ex_max_hold = evaluate_val_poc_30m_exit(
            entry_price=100.0, entry_atr=1.0, target_tp=103.0, init_sl=98.0,
            fill_bar_open_ts=fill_open, current_price=100.0, overall_regime="BUY",
            now_utc=fill_open + 48 * 1800,
        )
        self.assertTrue(ex_max_hold["should_exit"])
        self.assertEqual(ex_max_hold["exit_reason"], "MAX_HOLD_CLOSE")

    def test_h3b_squeeze_1h_entry_gates_and_s8b_exit_no_leak(self) -> None:
        df_1h, _ = _make_h3b_1h_fixture()
        res_ok = evaluate_h3b_squeeze_1h_entry(
            df_1h, overall_regime="SELL", is_norm_excluded=False, btc_ret14d=-0.05, sym_ret14d=-0.10, wf90d_paused=False,
        )
        self.assertTrue(res_ok["should_enter"], res_ok.get("block_reason"))
        self.assertTrue(bool(res_ok["signal_df"]["short"].iloc[-1]))
        self.assertTrue(res_ok["is_squeeze"])

        # Regime, NoExh, WF90d gates
        self.assertFalse(evaluate_h3b_squeeze_1h_entry(df_1h, overall_regime="NEUTRAL")["should_enter"])
        ok_exh, _ = check_f7_no_exhaustion(-0.11, -0.05)
        self.assertFalse(ok_exh)
        self.assertFalse(evaluate_h3b_squeeze_1h_entry(df_1h, overall_regime="SELL", btc_ret14d=-0.12, sym_ret14d=-0.05)["should_enter"])
        self.assertFalse(evaluate_h3b_squeeze_1h_entry(df_1h, overall_regime="SELL", btc_ret14d=-0.02, sym_ret14d=-0.21)["should_enter"])
        self.assertFalse(evaluate_h3b_squeeze_1h_entry(df_1h, overall_regime="SELL", wf90d_paused=True)["should_enter"])

        # S8b exit progression without fill-bar low leak
        fill_open = int(df_1h["ts_utc"].iloc[-1]) + 3600
        # Fill bar had pre-entry low of 90.0 inside closed_1h_df; must NOT trigger BE_LOCK or tighten trail on bar+1
        closed_with_fill = pd.concat(
            [
                df_1h,
                pd.DataFrame([{"ts_utc": fill_open, "open": 100.0, "high": 101.0, "low": 90.0, "close": 100.0, "volume": 1000.0}]),
            ],
            ignore_index=True,
        )
        # Next bar (bar 1 = fill_open + 3600): if price rebounds to 100.5, since only entry=100 is strictly post-fill, be_locked must still be False!
        ex_no_leak = evaluate_h3b_squeeze_1h_exit(
            entry_price=100.0, entry_atr=2.0, init_sl=104.0, fill_bar_open_ts=fill_open,
            current_price=100.5, overall_regime="SELL", closed_1h_df=closed_with_fill,
            trough_price=100.0, be_locked=False, now_utc=fill_open + 3600 + 60,
            live_trough_price=99.5, last_trough_bucket_open=fill_open,
        )
        self.assertFalse(ex_no_leak["be_locked"])
        self.assertFalse(ex_no_leak["should_exit"])
        self.assertEqual(ex_no_leak["trough_price"], 99.5)

        # Bar 1 drops to 96.5 (-3.5 >= +1.5 * atr=3.0) mid-bar -> intrabar eff_sl MUST NOT lock BE until Bar 2!
        ex_midbar = evaluate_h3b_squeeze_1h_exit(
            entry_price=100.0, entry_atr=2.0, init_sl=104.0, fill_bar_open_ts=fill_open,
            current_price=96.5, overall_regime="SELL", closed_1h_df=closed_with_fill,
            trough_price=ex_no_leak["trough_price"], be_locked=False, now_utc=fill_open + 3600 + 300,
            live_trough_price=ex_no_leak["live_trough_price"], last_trough_bucket_open=ex_no_leak["last_trough_bucket_open"],
        )
        self.assertFalse(ex_midbar["be_locked"])
        self.assertEqual(ex_midbar["live_trough_price"], 96.5)

        # Bar 2 opens: rollover picks up live_trough_price=96.5 -> locks BE_LOCK (99.80)
        # First with closed_1h_df=None (prev_bar_atr == entry_atr == 2.0 -> trail_px=100.5 > 99.80 -> BE_LOCK)
        ex_bar2 = evaluate_h3b_squeeze_1h_exit(
            entry_price=100.0, entry_atr=2.0, init_sl=104.0, fill_bar_open_ts=fill_open,
            current_price=99.82, overall_regime="SELL", closed_1h_df=None,
            trough_price=ex_midbar["trough_price"], be_locked=ex_midbar["be_locked"], now_utc=fill_open + 7200 + 60,
            live_trough_price=ex_midbar["live_trough_price"], last_trough_bucket_open=ex_midbar["last_trough_bucket_open"],
        )
        self.assertTrue(ex_bar2["be_locked"])
        self.assertTrue(ex_bar2["should_exit"])
        self.assertAlmostEqual(ex_bar2["eff_sl"], 99.80, places=6)
        self.assertEqual(ex_bar2["exit_reason"], "BE_LOCK")

        # When compressed prev_bar_atr from closed_1h_df makes trail_px (~97.9) tighter than be_px (99.80), TRAIL_SL wins
        ex_bar2_trail = evaluate_h3b_squeeze_1h_exit(
            entry_price=100.0, entry_atr=2.0, init_sl=104.0, fill_bar_open_ts=fill_open,
            current_price=99.82, overall_regime="SELL", closed_1h_df=closed_with_fill,
            trough_price=ex_midbar["trough_price"], be_locked=ex_midbar["be_locked"], now_utc=fill_open + 7200 + 60,
            live_trough_price=ex_midbar["live_trough_price"], last_trough_bucket_open=ex_midbar["last_trough_bucket_open"],
        )
        self.assertTrue(ex_bar2_trail["should_exit"])
        self.assertEqual(ex_bar2_trail["exit_reason"], "TRAIL_SL")

        # FlipBE only on BUY (not NEUTRAL)
        ex_buy_flip = evaluate_h3b_squeeze_1h_exit(
            entry_price=100.0, entry_atr=2.0, init_sl=104.0, fill_bar_open_ts=fill_open,
            current_price=99.75, overall_regime="BUY", closed_1h_df=closed_with_fill,
            trough_price=100.0, be_locked=False, now_utc=fill_open + 3600 + 60,
        )
        self.assertTrue(ex_buy_flip["should_exit"])
        self.assertEqual(ex_buy_flip["exit_reason"], "EMERGENCY_BREAKEVEN_TP")

    def test_causal_wf90d_hysteresis_and_portfolio_tie_break(self) -> None:
        base_ts = 1_750_000_000
        # 10 losing trades (-0.50 each -> net=-5.00 < -3.00, pf=0 < 0.85) -> PAUSED
        losses = [
            {"entry_ts": base_ts + i * 3600, "exit_ts": base_ts + i * 3600 + 1800, "net_pnl": -0.50}
            for i in range(10)
        ]
        cts, st, d_paused = build_causal_wf_90d_timeline(losses)
        self.assertTrue(d_paused["paused"])
        self.assertTrue(st[-1])

        # Small gain +2.50 keeps net=-2.50, pf=2.5/5.0=0.50 (<1.00) -> STILL PAUSED
        partial = losses + [{"entry_ts": base_ts + 11 * 3600, "exit_ts": base_ts + 11 * 3600 + 1800, "net_pnl": 2.50}]
        _, st2, d_still = build_causal_wf_90d_timeline(partial)
        self.assertTrue(d_still["paused"])
        self.assertTrue(st2[-1])

        # Big gain +3.00 flips net=+0.50 >= 0.0 -> RESUMED
        recovered = partial + [{"entry_ts": base_ts + 12 * 3600, "exit_ts": base_ts + 12 * 3600 + 1800, "net_pnl": 3.00}]
        _, st3, d_resumed = build_causal_wf_90d_timeline(recovered)
        self.assertFalse(d_resumed["paused"])
        self.assertFalse(st3[-1])

        # Portfolio arbitration: 3 concurrent candidates on 2 free slots -> sort by (-vr, symbol)
        # Expect ZEC-USDT (vr=3.0) and ARB-USDT (vr=2.0, beats WIF-USDT vr=2.0 alphabetically)
        sig_t = 1_750_000_800
        cands = [
            {"sig_ts": sig_t, "fill_ts": sig_t, "exit_ts": sig_t + 3600, "symbol": "WIF-USDT", "strategy": F7_LONG_STRATEGY, "side": "long", "vr": 2.0, "fill_px": 100.0, "exit_px": 101.0, "exit_reason": "TARGET_TP", "norm_excl": False},
            {"sig_ts": sig_t, "fill_ts": sig_t, "exit_ts": sig_t + 3600, "symbol": "ARB-USDT", "strategy": F7_LONG_STRATEGY, "side": "long", "vr": 2.0, "fill_px": 100.0, "exit_px": 101.0, "exit_reason": "TARGET_TP", "norm_excl": False},
            {"sig_ts": sig_t, "fill_ts": sig_t, "exit_ts": sig_t + 3600, "symbol": "ZEC-USDT", "strategy": F7_LONG_STRATEGY, "side": "long", "vr": 3.0, "fill_px": 100.0, "exit_px": 101.0, "exit_reason": "TARGET_TP", "norm_excl": False},
        ]
        sim = simulate_f7_portfolio(cands, max_pos=2, fee_rate=0.0008, daily_cb_limit=1.50)
        picked = [t["symbol"] for t in sim["trades"]]
        self.assertEqual(picked, ["ZEC-USDT", "ARB-USDT"])

        # Daily CB -$1.50 triggers block on same UTC day (00:00 UTC == 09:00 JST) & resets next UTC day
        day0 = (1_750_000_000 // 86400) * 86400 + 3600
        day1 = (day0 // 86400 + 1) * 86400 + 3600
        cb_cands = [
            {"sig_ts": day0, "fill_ts": day0, "exit_ts": day0 + 1800, "symbol": "HYPE-USDT", "strategy": F7_SHORT_STRATEGY, "side": "short", "vr": 2.0, "fill_px": 100.0, "exit_px": 112.0, "exit_reason": "INIT_SL", "norm_excl": False},
            {"sig_ts": day0 + 3600, "fill_ts": day0 + 3600, "exit_ts": day0 + 5400, "symbol": "SUI-USDT", "strategy": F7_SHORT_STRATEGY, "side": "short", "vr": 2.0, "fill_px": 100.0, "exit_px": 99.0, "exit_reason": "BE_LOCK", "norm_excl": False},
            {"sig_ts": day1, "fill_ts": day1, "exit_ts": day1 + 1800, "symbol": "SUI-USDT", "strategy": F7_SHORT_STRATEGY, "side": "short", "vr": 2.0, "fill_px": 100.0, "exit_px": 99.0, "exit_reason": "BE_LOCK", "norm_excl": False},
        ]
        sim_cb = simulate_f7_portfolio(cb_cands, max_pos=2, fee_rate=0.0008, daily_cb_limit=1.50)
        self.assertEqual(sim_cb["n"], 2)
        self.assertEqual(sim_cb["trades"][1]["sig_ts"], day1)

    def test_lake_30m_rebuild_no_leak_and_f7_screening(self) -> None:
        self.assertIn("30m", SUPPORTED_TIMEFRAMES)
        self.assertEqual(TIMEFRAME_SECONDS["30m"], 1800)

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            db_path = td_p / "f7_lake.db"
            wf_path = td_p / "wf90d.json"
            lake = LakeStore(db_path=db_path)

            # 12 bars of 5m starting at 00:00 -> two 30m bars (00:00..00:30 and 00:30..01:00)
            t0 = 1_750_000_000 // 3600 * 3600
            rows_5m = [
                {
                    "ts_utc": t0 + k * 300,
                    "open": 100.0 + k,
                    "high": 100.5 + k,
                    "low": 99.5 + k,
                    "close": 100.2 + k,
                    "volume": 10.0,
                }
                for k in range(12)
            ]
            lake.upsert_candles("BTC-USDT", "5m", pd.DataFrame(rows_5m), cutoff_utc=t0 + 3600)
            lake.rebuild_higher_timeframes_from_5m("BTC-USDT", cutoff_utc=t0 + 1800, timeframes=("30m",))
            df30_mid = lake.load_candles("BTC-USDT", "30m", cutoff_utc=t0 + 1800)
            self.assertEqual(len(df30_mid), 1)
            self.assertEqual(int(df30_mid["ts_utc"].iloc[0]), t0)

            lake.rebuild_higher_timeframes_from_5m("BTC-USDT", cutoff_utc=t0 + 3600, timeframes=("30m",))
            df30_full = lake.load_candles("BTC-USDT", "30m", cutoff_utc=t0 + 3600)
            self.assertEqual(len(df30_full), 2)

            res_screen = run_f7_screening_for_bot(
                override_regime_info={
                    "overall_regime": "BUY",
                    "regime_pattern": "(B,B,B)",
                    "regime_strength": {"tier": "ULTRA_BUY", "net_score": 0.9},
                    "excluded_symbols": {"ARB-USDT": "PEAK_DROP"},
                },
                db_path=db_path,
                wf90d_state_path=wf_path,
            )
            self.assertTrue(res_screen["f7_mode"])
            self.assertNotIn("SOL-USDT", res_screen["selected_symbols"])
            self.assertIn("HYPE-USDT", res_screen["selected_symbols"])
            self.assertTrue(res_screen["symbol_params_map"]["ARB-USDT"]["is_norm_excluded"])
            self.assertTrue(wf_path.exists())


if __name__ == "__main__":
    unittest.main()
