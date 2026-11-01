import asyncio
import json
from pathlib import Path
import sys
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from bingx5_46_2api import api_bingx

async def main():
    print("=== BingX 実運用（アカウント）約定履歴 & 実績データ取得 ===")
    api = api_bingx(symbol="BTC-USDT", mode="demo")
    
    # 1. 口座残高
    balance = await api.get_account()
    print(f"\n[口座残高] 現在のデモ/テスト残高: {balance:.2f} USDT")

    # 2. ポジション情報
    pos = await api.get_positions()
    print(f"[現在ポジション] {pos}")

    # 3. BingXの約定履歴
    try:
        from bingx5_46_3logic import PnLCalculator
        from bingx5_46_2api import apis
        pnl_calc = PnLCalculator(apis_config=apis, mode="demo")
        orders = await pnl_calc.get_bingx_trade_history(apis)
        print(f"[約定履歴] 全約定件数: {len(orders)} 件")
        if orders:
            print("\n直近の約定取引一覧 (最新 10 件):")
            print("-" * 85)
            print(f"{'日時 (JST)':<20} | {'通貨':<12} | {'売買':<6} | {'価格 ($)':<12} | {'数量':<10} | {'実現損益 ($)':<12}")
            print("-" * 85)
            total_pnl = 0.0
            for o in orders[:10]:
                sym = o.get("symbol", "")
                side = o.get("side", "")
                px = float(o.get("avgPrice") or o.get("price") or 0)
                sz = float(o.get("executedQty") or 0)
                pnl = float(o.get("profit") or 0)
                total_pnl += pnl
                time_ms = o.get("updateTime") or o.get("time") or 0
                dt_str = (pd.to_datetime(time_ms, unit="ms") + pd.Timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S") if time_ms else ""
                print(f"{dt_str:<20} | {sym:<12} | {side:<6} | ${px:<11.2f} | {sz:<10.4f} | ${pnl:<11.2f}")
            print("-" * 85)
            print(f"★ 直近約定での累計実現損益 (Closed PnL): ${total_pnl:.2f} USDT")
        else:
            print("※ 本アカウントでの決済済み約定はまだ記録されていません。")
    except Exception as e:
        print(f"約定履歴の取得エラー: {e}")

if __name__ == "__main__":
    asyncio.run(main())
