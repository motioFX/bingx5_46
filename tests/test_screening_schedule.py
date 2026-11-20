"""定期スクリーニング周期および毎時軽量地合い反転検知テスト"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bingx5_46_1futures_limit as bot_main
from lake_live_bridge import refresh_hourly_regime_for_bot


class TestBingxScreeningSchedule(unittest.TestCase):
    def test_default_analysis_hours_is_4h_schedule(self) -> None:
        self.assertEqual(bot_main.ANALYSIS_HOURS, [1, 5, 9, 13, 17, 21])

    def test_refresh_hourly_regime_detects_flip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            db_path = td_p / "reg.db"
            ms_path = td_p / "market_state.json"
            ms_path.write_text(json.dumps({"overall_regime": "SELL"}), encoding="utf-8")

            fake_regime = {
                "overall_regime": "BUY",
                "regime_pattern": "(B,B,B)",
                "regime_strength": {"tier": "STRONG_BUY", "tier_jp": "強気買い", "net_score": 0.65},
                "period_results": {},
                "allowed_strategies": ["val_poc", "vah_break"],
                "recommended_symbols": ["HYPE-USDT"],
                "excluded_symbols": {},
            }
            with mock.patch("lake_live_bridge.compute_regime_from_lake", return_value=fake_regime):
                res = refresh_hourly_regime_for_bot(db_path=db_path, sync_live=False, state_file_path=ms_path)

            self.assertEqual(res["prev_overall_regime"], "SELL")
            self.assertEqual(res["overall_regime"], "BUY")
            self.assertTrue(res["regime_flipped"])
            saved = json.loads(ms_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["overall_regime"], "BUY")
            self.assertIn("strength_desc", saved["regime_strength"])


if __name__ == "__main__":
    unittest.main()
