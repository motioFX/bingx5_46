import asyncio
import json
from pathlib import Path
import sys
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from bingx5_46_2api import api_hyperliquid

async def main():
    print("=== Hyperliquid 実運用（アカウント）約定履歴 & 実績データ取得 ===")
    api = api_hyperliquid(symbol="BTC", mode="demo")
    
    # 1. 口座残高
    balance = await api.get_account()
    print(f"\n[口座残高] 現在のデモ/テスト残高: {balance:.2f} USDC")

    # 2. ポジション情報
    pos = await api.get_positions()
    print(f"[現在ポジション] {pos}")

    # 3. 本物アカウントの約定履歴 (userFills)
    user_address = "0xDA473FC7Cc55a8dCdDacf5BdA6c338538C3933B1"
    print(f"\n[照会対象アカウントアドレス] {user_address}")

    try:
        payload = {"type": "userFills", "user": user_address}
        resp = requests.post("https://api.hyperliquid.xyz/info", json=payload, timeout=10)
        if resp.status_code == 200:
            fills = resp.json()
            print(f"[約定履歴] オンチェーン全約定件数: {len(fills)} 件")
            if fills:
                print("\n直近の約定取引一覧 (最新 10 件):")
                print("-" * 85)
                print(f"{'日時 (JST)':<20} | {'通貨':<8} | {'売買':<6} | {'価格 ($)':<12} | {'数量':<10} | {'実現損益 ($)':<12}")
                print("-" * 85)
                total_pnl = 0.0
                for f in fills[:10]:
                    coin = f.get("coin", "")
                    side = "BUY" if f.get("side") == "B" else "SELL"
                    px = float(f.get("px", 0))
                    sz = float(f.get("sz", 0))
                    pnl = float(f.get("closedPnl", 0))
                    total_pnl += pnl
                    time_ms = f.get("time", 0)
                    dt_str = (pd.to_datetime(time_ms, unit="ms") + pd.Timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S") if time_ms else ""
                    print(f"{dt_str:<20} | {coin:<8} | {side:<6} | ${px:<11.2f} | {sz:<10.4f} | ${pnl:<11.2f}")
                print("-" * 85)
                print(f"★ 直近約定での累計実現損益 (Closed PnL): ${total_pnl:.2f} USDC")
            else:
                print("※ 本アカウントでの決済済み約定はまだ記録されていません。")
    except Exception as e:
        print(f"約定履歴の取得エラー: {e}")

if __name__ == "__main__":
    asyncio.run(main())
