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
LONG_TERM_PROTECTED_BTC: float = 0.1434

# 長期保有 BTC の基準取得単価（1,279万円・1,300万弱）
# ※ 過去の売買・利確履歴を踏まえたユーザー確定簿価
KNOWN_ENTRY_PRICE: float = 12794387.0


async def get_long_term_btc_status(mode: str = "live") -> Dict[str, Any]:
    """
    Bitbank API から長期運用 BTC/JPY の残高・取得単価・現在価格・評価額・含み損益を取得・算出する。
    """
    api = api_bitbank("btc_jpy", coin="JPY", mode=mode)

    # 1. JPY 現金残高 (onhand_amount)
    jpy_total = await api.get_account()

    # 2. 口座内全体の BTC 保有数量
    # raw asset から実際の BTC onhand を直接取得
    btc_size = 0.0
    try:
        helper = api.bitbank
        res = await helper._request_private("GET", "/v1/user/assets")
        if res.get("success") == 1 and "data" in res and "assets" in res["data"]:
            for asset_info in res["data"]["assets"]:
                if asset_info.get("asset", "").lower() == "btc":
                    btc_size = float(asset_info.get("onhand_amount", 0.0))
                    break
    except Exception as e:
        discord.print_log(f"[LongTermBTC] 残高取得エラー: {e}")

    # 保有数量が0の場合はデフォルト保護数量にフォールバック
    if btc_size <= 0:
        btc_size = LONG_TERM_PROTECTED_BTC

    # 平均取得単価: ユーザー確定簿価を採用（1,300万弱 / 12,794,387円）
    entry_px = KNOWN_ENTRY_PRICE

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
    起動時・8時間定期選定時用の詳細枠線ブロックレポート文字列を生成する。
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


def format_compact_btc_report(status: Dict[str, Any]) -> str:
    """
    毎時サイクル用の1行コンパクトサマリー文字列を生成する。
    """
    btc_size = status["btc_size"]
    entry_px = status["entry_px"]
    last_price = status["last_price"]
    eval_value = status["eval_value"]
    pnl_jpy = status["pnl_jpy"]
    pnl_pct = status["pnl_pct"]
    total_equity = status["total_equity"]
    sign = "+" if pnl_jpy >= 0 else ""

    return (
        f"💎 [長期BTC] {btc_size:.4f} BTC @ {entry_px:,.0f}円 | "
        f"現在: {last_price:,.0f}円 | 評価額: {eval_value:,.0f}円 | "
        f"含み損益: {sign}{pnl_jpy:,.0f}円 ({sign}{pnl_pct:.2f}%) | "
        f"口座総資産: {total_equity:,.0f}円"
    )


async def report_long_term_btc(mode: str = "live", to_discord: bool = True, compact: bool = False) -> str:
    """
    長期運用 BTC/JPY の状況を取得し、ターミナル出力および Discord 送信を行う。
    - compact=False: 起動時・定期選定時用の詳細枠線ブロック
    - compact=True: 毎時サイクル用の1行コンパクトサマリー
    """
    status = await get_long_term_btc_status(mode=mode)
    if compact:
        report_text = format_compact_btc_report(status)
    else:
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
    print("\n--- [1] 枠線付き詳細レポート (起動時 & 8時間選定時 / Discord送信) ---")
    asyncio.run(report_long_term_btc(mode="live", to_discord=False, compact=False))
    print("\n--- [2] 1行コンパクト要約 (毎時サイクル時 / ターミナル出力) ---")
    asyncio.run(report_long_term_btc(mode="live", to_discord=False, compact=True))
