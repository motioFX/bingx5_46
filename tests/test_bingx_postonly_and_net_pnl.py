#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BingX PostOnly Maker発注リトライ & 取引所実手数料控除(Net PnL)同期の単体テスト。"""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bingx5_46_2api as bapi
import real_trade_tracker as rtt


def _ms(iso_str: str) -> int:
    return int(datetime.fromisoformat(iso_str).timestamp() * 1000)


class TestBingxPostOnlyAndNetPnl(unittest.TestCase):
    def test_postonly_limit_entry_uses_postonly_and_retreats_on_crossing(self):
        spec = {
            "sz_decimals": 2,
            "qty_step": 0.01,
            "min_qty": 0.01,
            "max_qty": float("inf"),
            "min_notional": 5.0,
            "max_leverage": 10.0,
            "price_place": 5,
        }
        api = bapi.api_bingx(symbol="ENA-USDT", mode="demo", instrument_spec=spec)
        api.bingx.api_key = "test_key"
        api.bingx.secret_key = "test_secret"

        df_short = pd.DataFrame({
            "close": [0.11018],
            "short": [True],
            "long": [False],
            "VAL": [0.1090],
            "VAH": [0.1110],
        })
        df_long = pd.DataFrame({
            "close": [0.11017],
            "short": [False],
            "long": [True],
            "VAL": [0.1090],
            "VAH": [0.1110],
        })
        empty_pos = {"pos_count": 0, "buy": 0.0, "sell": 0.0}

        # 1. SHORT: 0ティック目(101215) -> 1ティック退避(status=CANCELLED) -> 2ティック退避(status=NEW)
        short_responses = [
            {"code": 101215, "msg": "Post Only order was cancelled because it would have been filled immediately."},
            {"code": 0, "data": {"order": {"status": "CANCELLED"}}},
            {"code": 0, "data": {"order": {"status": "NEW", "orderId": 12345}}},
        ]
        captured_short = []

        async def mock_req_short(method, base_url, endpoint, api_key, secret_key, params=None, timeout=10):
            captured_short.append(dict(params or {}))
            return short_responses[len(captured_short) - 1]

        with (
            mock.patch.object(bapi, "is_air", False),
            mock.patch.object(bapi, "get_bingx_orderbook", return_value=(0.11017, 0.11018)),
            mock.patch.object(bapi, "_async_bingx_request", side_effect=mock_req_short),
            mock.patch.object(api.bingx, "set_leverage", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(api, "active_order_cancel", new=mock.AsyncMock(return_value=True)),
        ):
            ok_short = asyncio.run(api.short_entry(df_short, empty_pos, 1000.0, 130.0, 500.0))

        self.assertTrue(ok_short)
        self.assertEqual(len(captured_short), 3)
        self.assertTrue(all(p["timeInForce"] == "PostOnly" for p in captured_short))
        self.assertEqual([float(p["price"]) for p in captured_short], [0.11018, 0.11019, 0.11020])

        # 2. LONG: 0ティック目(101215) -> -1ティック退避(NEW)
        long_responses = [
            {"code": 101215, "msg": "Post Only order was cancelled because it would have been filled immediately."},
            {"code": 0, "data": {"order": {"status": "NEW", "orderId": 22345}}},
        ]
        captured_long = []

        async def mock_req_long(method, base_url, endpoint, api_key, secret_key, params=None, timeout=10):
            captured_long.append(dict(params or {}))
            return long_responses[len(captured_long) - 1]

        with (
            mock.patch.object(bapi, "is_air", False),
            mock.patch.object(bapi, "get_bingx_orderbook", return_value=(0.11017, 0.11018)),
            mock.patch.object(bapi, "_async_bingx_request", side_effect=mock_req_long),
            mock.patch.object(api.bingx, "set_leverage", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(api, "active_order_cancel", new=mock.AsyncMock(return_value=True)),
        ):
            ok_long = asyncio.run(api.long_entry(df_long, empty_pos, 1000.0, 130.0, 500.0))

        self.assertTrue(ok_long)
        self.assertEqual(len(captured_long), 2)
        self.assertTrue(all(p["timeInForce"] == "PostOnly" for p in captured_long))
        self.assertEqual([float(p["price"]) for p in captured_long], [0.11017, 0.11016])

        # 3. PostOnly以外のエラー (証拠金不足等) はリトライせず即 False
        captured_err = []

        async def mock_req_err(method, base_url, endpoint, api_key, secret_key, params=None, timeout=10):
            captured_err.append(dict(params or {}))
            return {"code": 101204, "msg": "Insufficient margin"}

        with (
            mock.patch.object(bapi, "is_air", False),
            mock.patch.object(bapi, "get_bingx_orderbook", return_value=(0.11017, 0.11018)),
            mock.patch.object(bapi, "_async_bingx_request", side_effect=mock_req_err),
            mock.patch.object(api.bingx, "set_leverage", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(api, "active_order_cancel", new=mock.AsyncMock(return_value=True)),
        ):
            ok_err = asyncio.run(api.short_entry(df_short, empty_pos, 1000.0, 130.0, 500.0))

        self.assertFalse(ok_err)
        self.assertEqual(len(captured_err), 1)

    def test_attach_bingx_income_fees_matches_real_vst_records(self):
        real_income = [
            {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "0.1431978", "time": _ms("2026-11-19T05:15:24+00:00"), "info": "Close Short", "tranId": "p8", "tradeId": "t8"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074145", "time": _ms("2026-11-19T05:15:24+00:00"), "info": "Position closing fee", "tranId": "fc8", "tradeId": "t8"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074861", "time": _ms("2026-11-19T04:10:59+00:00"), "info": "Position opening fee", "tranId": "fo8", "tradeId": "t8_o"},
            {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "0.2237488", "time": _ms("2026-11-18T00:45:11+00:00"), "info": "Close Short", "tranId": "p7", "tradeId": "t7"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074021", "time": _ms("2026-11-18T00:45:11+00:00"), "info": "Position closing fee", "tranId": "fc7", "tradeId": "t7"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075140", "time": _ms("2026-11-17T23:45:07+00:00"), "info": "Position opening fee", "tranId": "fo7", "tradeId": "t7_o"},
            {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "-0.0012816", "time": _ms("2026-11-17T15:08:06+00:00"), "info": "Close Short", "tranId": "p6", "tradeId": "t6"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074980", "time": _ms("2026-11-17T15:08:06+00:00"), "info": "Position closing fee", "tranId": "fc6", "tradeId": "t6"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074974", "time": _ms("2026-11-17T14:50:25+00:00"), "info": "Position opening fee", "tranId": "fo6", "tradeId": "t6_o"},
            {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "0.1361146", "time": _ms("2026-11-17T05:10:50+00:00"), "info": "Close Short", "tranId": "p5", "tradeId": "t5"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074259", "time": _ms("2026-11-17T05:10:50+00:00"), "info": "Position closing fee", "tranId": "fc5", "tradeId": "t5"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0029976", "time": _ms("2026-11-17T04:40:40+00:00"), "info": "Position opening fee", "tranId": "fo5", "tradeId": "t5_o"},
            {"symbol": "RENDER-USDT", "incomeType": "REALIZED_PNL", "income": "-0.0749980", "time": _ms("2026-11-15T23:45:15+00:00"), "info": "Close Short", "tranId": "p4", "tradeId": "t4"},
            {"symbol": "RENDER-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075105", "time": _ms("2026-11-15T23:45:15+00:00"), "info": "Position closing fee", "tranId": "fc4", "tradeId": "t4"},
            {"symbol": "RENDER-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074730", "time": _ms("2026-11-15T23:08:00+00:00"), "info": "Position opening fee", "tranId": "fo4", "tradeId": "t4_o"},
            {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "-0.0397472", "time": _ms("2026-11-15T20:36:05+00:00"), "info": "Close Short", "tranId": "p3", "tradeId": "t3"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075097", "time": _ms("2026-11-15T20:36:05+00:00"), "info": "Position closing fee", "tranId": "fc3", "tradeId": "t3"},
            {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074899", "time": _ms("2026-11-15T16:35:46+00:00"), "info": "Position opening fee", "tranId": "fo3", "tradeId": "t3_o"},
            {"symbol": "ARB-USDT", "incomeType": "REALIZED_PNL", "income": "-0.0651340", "time": _ms("2026-11-15T15:10:14+00:00"), "info": "Close Short", "tranId": "p2", "tradeId": "t2"},
            {"symbol": "ARB-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075415", "time": _ms("2026-11-15T15:10:14+00:00"), "info": "Position closing fee", "tranId": "fc2", "tradeId": "t2"},
            {"symbol": "ARB-USDT", "incomeType": "TRADING_FEE", "income": "-0.0030036", "time": _ms("2026-11-15T15:05:47+00:00"), "info": "Position opening fee", "tranId": "fo2", "tradeId": "t2_o"},
            {"symbol": "TAO-USDT", "incomeType": "REALIZED_PNL", "income": "0.0000000", "time": _ms("2026-11-14T20:46:05+00:00"), "info": "Close Short", "tranId": "p1", "tradeId": "t1"},
            {"symbol": "TAO-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075171", "time": _ms("2026-11-14T20:46:05+00:00"), "info": "Position closing fee", "tranId": "fc1", "tradeId": "t1"},
            {"symbol": "TAO-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075171", "time": _ms("2026-11-14T20:45:18+00:00"), "info": "Position opening fee", "tranId": "fo1", "tradeId": "t1_o"},
        ]

        enriched = rtt.attach_bingx_income_fees(real_income)
        self.assertEqual(len(enriched), 8)
        ena_latest = enriched[-1]
        self.assertEqual(ena_latest["tranId"], "p8")
        self.assertAlmostEqual(ena_latest["gross_pnl"], 0.1431978, places=7)
        self.assertAlmostEqual(ena_latest["fee"], -0.0149006, places=7)
        self.assertAlmostEqual(ena_latest["net_pnl"], 0.1282972, places=7)

        self.assertAlmostEqual(sum(x["gross_pnl"] for x in enriched), 0.3219004, places=7)
        self.assertAlmostEqual(sum(x["fee"] for x in enriched), -0.1107980, places=7)
        self.assertAlmostEqual(sum(x["net_pnl"] for x in enriched), 0.2111024, places=7)

        # ファンディング手数料も保有期間分が合算されること
        with_funding = [
            {"symbol": "WIF-USDT", "incomeType": "TRADING_FEE", "income": "-0.0030", "time": _ms("2026-11-19T00:00:00+00:00"), "info": "Position opening fee", "tranId": "wf_o", "tradeId": "wt0"},
            {"symbol": "WIF-USDT", "incomeType": "FUNDING_FEE", "income": "-0.0020", "time": _ms("2026-11-19T01:00:00+00:00"), "info": "Funding Fee", "tranId": "wf_f", "tradeId": ""},
            {"symbol": "WIF-USDT", "incomeType": "TRADING_FEE", "income": "-0.0075", "time": _ms("2026-11-19T02:00:00+00:00"), "info": "Position closing fee", "tranId": "wf_c", "tradeId": "wt1"},
            {"symbol": "WIF-USDT", "incomeType": "REALIZED_PNL", "income": "0.2000", "time": _ms("2026-11-19T02:00:00+00:00"), "info": "Close Short", "tranId": "wf_p", "tradeId": "wt1"},
        ]
        wf_res = rtt.attach_bingx_income_fees(with_funding)
        self.assertEqual(len(wf_res), 1)
        self.assertAlmostEqual(wf_res[0]["fee"], -0.0125, places=7)
        self.assertAlmostEqual(wf_res[0]["net_pnl"], 0.1875, places=7)

    def test_sync_real_trades_backfills_fees_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            trades_json = tmp_path / "real_trades_history.json"
            trades_csv = tmp_path / "trades_history.csv"
            daily_csv = tmp_path / "daily_performance.csv"
            chart_main = tmp_path / "real_trading_performance.png"
            chart_clean = tmp_path / "real_trading_performance_clean.png"
            chart_detail = tmp_path / "real_trading_performance_detail.png"

            seeded = [
                {
                    "timestamp": "2026-11-19T04:11:04.602661+00:00",
                    "symbol": "ENA-USDT",
                    "side": "SHORT",
                    "action": "ENTRY",
                    "price": 0.11309,
                    "qty": 132.85,
                    "pnl": 0.0,
                    "note": "新規ショートエントリー約定 [5m足]",
                },
                {
                    "timestamp": "2026-11-19T05:19:22.440689+00:00",
                    "symbol": "ENA-USDT",
                    "side": "SHORT",
                    "action": "CLOSE",
                    "price": 0.11159,
                    "qty": 132.39,
                    "pnl": 0.1431978,
                    "tranId": "tran_ena_1119",
                    "tradeId": "trade_ena_1119",
                    "note": "実運用決済 (VAH_POC_TARGET_TP)",
                },
            ]
            trades_json.write_text(json.dumps(seeded, ensure_ascii=False), encoding="utf-8")

            api_rows = [
                {"symbol": "ENA-USDT", "incomeType": "REALIZED_PNL", "income": "0.1431978", "time": _ms("2026-11-19T05:15:24+00:00"), "info": "Close Short", "tranId": "tran_ena_1119", "tradeId": "trade_ena_1119"},
                {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074145", "time": _ms("2026-11-19T05:15:24+00:00"), "info": "Position closing fee", "tranId": "fee_c", "tradeId": "trade_ena_1119"},
                {"symbol": "ENA-USDT", "incomeType": "TRADING_FEE", "income": "-0.0074861", "time": _ms("2026-11-19T04:10:59+00:00"), "info": "Position opening fee", "tranId": "fee_o", "tradeId": "open_ena_1119"},
            ]
            mock_req = mock.AsyncMock(return_value={"code": 0, "data": api_rows})

            with (
                mock.patch.object(rtt, "DATA_DIR", tmp_path),
                mock.patch.object(rtt, "TRADES_FILE", trades_json),
                mock.patch.object(rtt, "TRADES_CSV", trades_csv),
                mock.patch.object(rtt, "DAILY_PERF_CSV", daily_csv),
                mock.patch.object(rtt, "CHART_FILE", chart_main),
                mock.patch.object(rtt, "CHART_FILE_CLEAN", chart_clean),
                mock.patch.object(rtt, "CHART_FILE_DETAIL", chart_detail),
                mock.patch.object(bapi, "is_air", False),
                mock.patch.object(bapi, "apis_bingx", {"bingx_demo": {"api_key": "k", "secret_key": "s"}}),
                mock.patch.object(bapi, "_async_bingx_request", mock_req),
            ):
                changed_1 = asyncio.run(rtt.sync_real_trades_from_bingx_api(mode="demo"))
                self.assertEqual(changed_1, 1)
                self.assertIsNone(mock_req.call_args.kwargs["params"].get("incomeType"))

                saved = json.loads(trades_json.read_text(encoding="utf-8"))
                self.assertAlmostEqual(saved[1]["gross_pnl"], 0.1431978, places=7)
                self.assertAlmostEqual(saved[1]["fee"], -0.0149006, places=7)
                self.assertAlmostEqual(saved[1]["pnl"], 0.1282972, places=7)
                self.assertTrue(saved[1]["fee_synced"])

                changed_2 = asyncio.run(rtt.sync_real_trades_from_bingx_api(mode="demo"))
                self.assertEqual(changed_2, 0)


if __name__ == "__main__":
    unittest.main()
