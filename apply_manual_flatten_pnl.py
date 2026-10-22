from __future__ import annotations
"""
apply_manual_flatten_pnl.py
2026-10-22 17:36 JST に手動一括成行決済（Manual Flatten All）された5銘柄の
確定損益（PnL）を real_trades_history.json および trades_history.csv に反映し、
累積損益チャート（Clean版 / Detail版）を再生成して Discord へ送信するスクリプト。
"""
import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from real_trade_tracker import (
    TRADES_FILE,
    load_real_trades,
    sync_trades_to_csv,
    plot_real_trading_performance,
    send_periodic_pnl_report_discord,
    get_real_trading_summary
)

# 2026-10-22 17:36:08 JST に実行された手動一括損切りの5件
flatten_trades = [
    {
        "timestamp": "2026-10-22T08:36:08.100000+00:00",
        "symbol": "AVAX-USDT",
        "side": "LONG",
        "action": "CLOSE",
        "price": 9.188,
        "qty": 4.0,
        "pnl": -2.24,
        "note": "実運用決済 (成行手動損切り: ポジション一括解消)"
    },
    {
        "timestamp": "2026-10-22T08:36:08.200000+00:00",
        "symbol": "AAVE-USDT",
        "side": "LONG",
        "action": "CLOSE",
        "price": 147.70,
        "qty": 0.2,
        "pnl": -1.606,
        "note": "実運用決済 (成行手動損切り: ポジション一括解消)"
    },
    {
        "timestamp": "2026-10-22T08:36:08.300000+00:00",
        "symbol": "NEAR-USDT",
        "side": "LONG",
        "action": "CLOSE",
        "price": 4.960,
        "qty": 3.0,
        "pnl": -0.3219,
        "note": "実運用決済 (成行手動損切り: ポジション一括解消)"
    },
    {
        "timestamp": "2026-10-22T08:36:08.400000+00:00",
        "symbol": "RENDER-USDT",
        "side": "LONG",
        "action": "CLOSE",
        "price": 1.804,
        "qty": 8.056,
        "pnl": -0.4203,
        "note": "実運用決済 (成行手動損切り: ポジション一括解消)"
    },
    {
        "timestamp": "2026-10-22T08:36:08.500000+00:00",
        "symbol": "TAO-USDT",
        "side": "LONG",
        "action": "CLOSE",
        "price": 258.58,
        "qty": 0.0546,
        "pnl": -0.8534,
        "note": "実運用決済 (成行手動損切り: ポジション一括解消)"
    }
]


def main():
    trades = load_real_trades()
    print(f"既存トレード記録数: {len(trades)}件")

    existing_timestamps = {t.get("timestamp") for t in trades}
    added_count = 0
    total_loss = 0.0
    for t in flatten_trades:
        if t["timestamp"] not in existing_timestamps:
            trades.append(t)
            added_count += 1
            total_loss += t["pnl"]
            print(f"  + 追加: {t['symbol']} {t['action']} PnL: ${t['pnl']:+.4f} USDT")
        else:
            print(f"  - スキップ（登録済み）: {t['symbol']}")

    if added_count > 0:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        print(f"✅ {added_count}件の損切りトレードを {TRADES_FILE} に保存しました (合計損益: ${total_loss:+.4f} USDT)")

        sync_trades_to_csv()
        print("✅ trades_history.csv および daily_performance.csv を同期更新しました")

        clean_file, detail_file = plot_real_trading_performance(days=10)
        print(f"✅ チャート再描画完了: {clean_file}, {detail_file}")

        print("📡 最新PnLサマリーとチャートをDiscordへ送信中...")
        send_periodic_pnl_report_discord(days=10)
        print("✅ Discord送信完了")
    else:
        print("追加対象の新規レコードはありませんでした。")

    summary = get_real_trading_summary(days=10)
    print("\n=== 直近10日間 実トレード損益サマリー ===")
    print(f"取引回数: {summary['total_trades']}回 (勝: {summary['wins']}, 負: {summary['losses']})")
    print(f"勝率: {summary['win_rate']}%")
    print(f"プロフィットファクター (PF): {summary['profit_factor']:.2f}")
    print(f"純利益 (Net PnL): ${summary['net_pnl']:+.4f} USDT")


if __name__ == "__main__":
    main()
