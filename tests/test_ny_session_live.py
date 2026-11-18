#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本番口座・デモ口座共通の米国セッション(22:00〜翌09:00 JST)稼働ゲート検証テスト。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bingx5_46_1futures_limit as bot_main


class TestNySessionLiveGate(unittest.TestCase):
    def test_ny_session_allowed_in_live_and_demo_by_default(self):
        self.assertTrue(bot_main.ALLOW_NY_SESSION_LIVE)
        self.assertFalse(bot_main.BINGX_IS_LIVE)
        self.assertFalse(bot_main.ALLOW_LIVE_TRADING)

        # 本番・デモともに米国時間でブロックされないこと
        self.assertFalse(bot_main.should_block_ny_session_entry(is_live=True, is_ny=True))
        self.assertFalse(bot_main.should_block_ny_session_entry(is_live=False, is_ny=True))
        self.assertFalse(bot_main.should_block_ny_session_entry(is_live=True, is_ny=False))

        # 明示的に無効化した場合は本番のみ停止すること
        self.assertTrue(bot_main.should_block_ny_session_entry(is_live=True, is_ny=True, allow_ny_live=False))
        self.assertFalse(bot_main.should_block_ny_session_entry(is_live=False, is_ny=True, allow_ny_live=False))

    def test_ny_session_uses_best_overall_qualified_spec(self):
        raw_params = {
            "strategy": "rsima_short",
            "interval": 5,
            "is_qualified": True,
            "is_range_qualified": True,
            "is_breakout_qualified": False,
            "range_params": {"strategy": "rsima_short", "interval": 5, "is_qualified": True},
            "breakout_params": {"strategy": "val_break", "interval": 5, "is_qualified": False},
        }
        spec_london, reason_london = bot_main.select_session_entry_spec(raw_params, is_tokyo=False, is_london=True)
        self.assertIsNone(spec_london)
        self.assertEqual(reason_london, "欧州BREAKOUT不合格")

        spec_ny, reason_ny = bot_main.select_session_entry_spec(raw_params, is_tokyo=False, is_london=False)
        self.assertIsNotNone(spec_ny)
        self.assertEqual(reason_ny, "OK")
        self.assertEqual(spec_ny["strategy"], "rsima_short")


if __name__ == "__main__":
    unittest.main()
