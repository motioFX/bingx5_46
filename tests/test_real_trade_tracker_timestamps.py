#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""real_trade_tracker の混在ISO8601タイムスタンプ解析とCSV同期の単体テスト。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import real_trade_tracker as rtt


class TestRealTradeTrackerTimestamps(unittest.TestCase):
    def test_mixed_iso8601_sync_and_summaries(self):
        sample_trades = [
            {
                "timestamp": "2026-11-17T14:50:25.033861+00:00",
                "symbol": "ENA-USDT",
                "side": "SHORT",
                "action": "ENTRY",
                "price": 0.11699,
                "qty": 128.27,
                "pnl": 0.0,
                "note": "entry",
            },
            {
                "timestamp": "2026-11-17T15:09:12.055736+00:00",
                "symbol": "ENA-USDT",
                "side": "SHORT",
                "action": "CLOSE",
                "price": 0.11569,
                "qty": 128.16,
                "pnl": 0.1009,
                "note": "close 1",
            },
            {
                # マイクロ秒なし (API同期由来レコード)
                "timestamp": "2026-11-17T15:08:06+00:00",
                "symbol": "ENA-USDT",
                "side": "SHORT",
                "action": "CLOSE",
                "price": 0.0,
                "qty": 0.0,
                "pnl": -0.0012816,
                "note": "api sync",
            },
            {
                "timestamp": "2026-11-18T00:46:32.453428+00:00",
                "symbol": "ENA-USDT",
                "side": "SHORT",
                "action": "CLOSE",
                "price": 0.11643,
                "qty": 127.13,
                "pnl": 0.2237488,
                "note": "close 2",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            trades_json = tmp_path / "real_trades_history.json"
            trades_csv = tmp_path / "trades_history.csv"
            daily_csv = tmp_path / "daily_performance.csv"
            trades_json.write_text(json.dumps(sample_trades, ensure_ascii=False), encoding="utf-8")

            with (
                mock.patch.object(rtt, "DATA_DIR", tmp_path),
                mock.patch.object(rtt, "TRADES_FILE", trades_json),
                mock.patch.object(rtt, "TRADES_CSV", trades_csv),
                mock.patch.object(rtt, "DAILY_PERF_CSV", daily_csv),
            ):
                rtt.sync_trades_to_csv()
                df_csv = pd.read_csv(trades_csv)
                self.assertEqual(len(df_csv), 4)
                self.assertFalse(df_csv["dt_jst"].isna().any(), f"dt_jst contains NaN:\n{df_csv}")
                self.assertEqual(df_csv.loc[2, "dt_jst"], "2026-11-18 00:08:06")

                df_daily = pd.read_csv(daily_csv)
                self.assertEqual(len(df_daily), 1)
                self.assertEqual(int(df_daily.loc[0, "closed_trades"]), 3)

                monthly = rtt.get_monthly_performance_summary(2026, 11)
                self.assertEqual(monthly["status"], "ok")
                self.assertEqual(monthly["total_trades"], 3)

    def test_parse_trade_jst_dt_prefers_timestamp_and_handles_naive_dt_jst(self):
        dt_from_ts = rtt._parse_trade_jst_dt({"timestamp": "2026-11-17T15:08:06+00:00"})
        self.assertIsNotNone(dt_from_ts)
        self.assertEqual(dt_from_ts.strftime("%Y-%m-%d %H:%M:%S"), "2026-11-18 00:08:06")

        dt_from_naive = rtt._parse_trade_jst_dt({"dt_jst": "2026-11-18 09:46:32"})
        self.assertIsNotNone(dt_from_naive)
        self.assertEqual(str(dt_from_naive.tzinfo), "Asia/Tokyo")
        self.assertEqual(dt_from_naive.strftime("%Y-%m-%d %H:%M:%S"), "2026-11-18 09:46:32")


if __name__ == "__main__":
    unittest.main()
