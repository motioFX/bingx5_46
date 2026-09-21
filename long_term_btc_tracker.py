from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import requests
import numpy as np

# プロジェクトルートをインポートパスに追加
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bitbank5_46_2api import api_bitbank
from bitbank5_46_3logic import discord

# 長期保有 BTC の保護数量（この数量はボットの売買から隔離）
LONG_TERM_PROTECTED_BTC: float = 0.2434


async def get_long_term_btc_status(mode: str = "live") -> Dict[str, Any]:
    """
    Bitbank API から長期運用 BTC/JPY の残高・取得単価・現在価格・評価額・含み損益を取得・算出する。
    """
    api = api_bitbank("btc_jpy", coin="JPY", mode=mode)

    # 1. JPY 現金残高 (onhand_amount)
    jpy_total = await api.get_account()

    # 2. 口座内全体の BTC 保有数量と平均取得単価
    # raw asset から実際の BTC onhand を直接取得
    btc_size = 0.0
    entry_px = 0.0

    try:
        helper = api.bitbank
        res = await helper._request_private("GET", "/v1/user/assets")
        if res.get("success") == 1 and "data" in res and "assets" in res["data"]:
            for asset_info in res["data"]["assets"]:
                if asset_info.get("asset", "").lower() == "btc":
                    btc_size = float(asset_info.get("onhand_amount", 0.0))
                    break

        if btc_size > 0:
            hist = await helper._request_private("GET", "/v1/user/spot/trade_history", {"pair": "btc_jpy", "count": 100})
            if hist.get("success") == 1 and "data" in hist and "trades" in hist["data"]:
                trades = hist["data"]["trades"]
                buy_trades = [t for t in trades if t.get("side") == "buy"]
                if buy_trades:
                    cum_amount = 0.0
                    weighted_sum = 0.0
                    for t in buy_trades:
                        amount = float(t.get("amount", 0))
                        price = float(t.get("price", 0))
                        remaining = btc_size - cum_amount
                        take = min(amount, remaining)
                        weighted_sum += take * price
                        cum_amount += take
                        if cum_amount >= btc_size:
                            break
                    if cum_amount > 0:
                        entry_px = float(np.round(weighted_sum / cum_amount, 2))
    except Exception as e:
        discord.print_log(f"[LongTermBTC] 残高取得エラー: {e}")

    # もし履歴から取得単価が取得できなかった場合のフォールバック（既知の取得単価）
    if entry_px <= 0 and btc_size > 0:
        entry_px = 12794387.0

    # 3. BTC 現在価格
    last_price = 0.0
    try:
        t_resp = requests.get("https://public.bitbank.cc/btc_jpy/ticker", timeout=5).json()
        last_price = float(t_resp.get("data", {}).get("last", 0.0))
    except Exception as e:
        discord.print_log(f"[LongTermBTC] Ticker取得エラー: {e}")

    eval_value = btc_size * last_price
    cost_value = btc_size * entry_px
    pnl_jpy = eval_value - cost_value
    pnl_pct = (pnl_jpy / cost_value * 100.0) if cost_value > 0 else 0.0
    total_equity = jpy_total + eval_value

    return {
        "jpy_total": jpy_total,
        "btc_size": btc_size,
        "entry_px": entry_px,
        "last_price": last_price,
        "eval_value": eval_value,
        "cost_value": cost_value,
        "pnl_jpy": pnl_jpy,
        "pnl_pct": pnl_pct,
        "total_equity": total_equity,
    }


def format_long_term_btc_report(status: Dict[str, Any]) -> str:
    """
    指定フォーマットでレポート文字列を生成する。
    """
    jpy_total = status["jpy_total"]
    btc_size = status["btc_size"]
    entry_px = status["entry_px"]
    last_price = status["last_price"]
    eval_value = status["eval_value"]
    cost_value = status["cost_value"]
    pnl_jpy = status["pnl_jpy"]
    pnl_pct = status["pnl_pct"]
    total_equity = status["total_equity"]

    sign = "+" if pnl_jpy >= 0 else ""

    lines = [
        "==================================================",
        "💎 【Bitbank 口座 現物保有・長期運用 BTC/JPY 状況】",
        "==================================================",
        f"  JPY 現金残高 : {jpy_total:,.0f} 円",
        f"  BTC 保有数量 : {btc_size:.4f} BTC",
        f"  平均取得単価 : {entry_px:,.0f} 円",
        f"  BTC 現在価格 : {last_price:,.0f} 円",
        f"  BTC 評価額   : {eval_value:,.0f} 円",
        f"  BTC 投資元本 : {cost_value:,.0f} 円",
        f"  BTC 含み損益 : {sign}{pnl_jpy:,.0f} 円 ({sign}{pnl_pct:.2f}%)",
        f"  口座総資産額 : {total_equity:,.0f} 円",
        "==================================================",
    ]
    return "\n".join(lines)


async def report_long_term_btc(mode: str = "live", to_discord: bool = True) -> str:
    """
    長期運用 BTC/JPY の状況を取得し、ターミナル出力および Discord 送信を行う。
    """
    status = await get_long_term_btc_status(mode=mode)
    report_text = format_long_term_btc_report(status)

    # ターミナルログ出力
    print(report_text)

    # Discord 通知
    if to_discord:
        try:
            discord_msg = f"```text\n{report_text}\n```"
            discord.print_log(discord_msg)
            discord.flush_all()
        except Exception as e:
            print(f"[LongTermBTC] Discord送信エラー: {e}")

    return report_text


if __name__ == "__main__":
    asyncio.run(report_long_term_btc(mode="live", to_discord=True))
