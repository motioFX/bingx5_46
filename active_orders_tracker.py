from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bitbank5_46_2api import api_bitbank
from bitbank5_46_3logic import discord

JST = timezone(timedelta(hours=9))

# チェック対象ペア (主要11銘柄 + render_jpy / rndr_jpy)
TRACKED_PAIRS = [
    "render_jpy", "rndr_jpy", "btc_jpy", "eth_jpy", "xrp_jpy",
    "sol_jpy", "doge_jpy", "bnb_jpy", "arb_jpy", "sui_jpy",
    "avax_jpy", "link_jpy"
]


async def fetch_active_orders(mode: str = "live") -> List[Dict[str, Any]]:
    """BitbankプライベートAPIから全銘柄のアクティブ指値注文を取得"""
    api = api_bitbank("btc_jpy", coin="JPY", mode=mode)
    helper = api.bitbank
    all_orders = []

    # 重複なしペアリスト
    unique_pairs = sorted(list(set(TRACKED_PAIRS)))

    for pair in unique_pairs:
        try:
            res = await helper._request_private("GET", f"/v1/user/spot/active_orders?pair={pair}")
            if res.get("success") == 1 and "data" in res and "orders" in res["data"]:
                for o in res["data"]["orders"]:
                    all_orders.append({
                        "order_id": o.get("order_id"),
                        "pair": pair,
                        "side": o.get("side", "").upper(),
                        "type": o.get("type", "").upper(),
                        "price": float(o.get("price", 0.0)),
                        "start_amount": float(o.get("start_amount", 0.0)),
                        "remaining_amount": float(o.get("remaining_amount", 0.0)),
                        "executed_amount": float(o.get("executed_amount", 0.0)),
                        "ordered_at": o.get("ordered_at"),
                    })
        except Exception as e:
            # 注文がないペアや取得失敗はスキップ
            pass

    return all_orders


async def report_active_orders(mode: str = "live", to_discord: bool = True) -> str:
    """現在出ている指値注文の状況をフォーマットして表示＆Discord送信"""
    orders = await fetch_active_orders(mode=mode)

    # 現在価格を取得して乖離を計算
    tickers: Dict[str, float] = {}
    for o in orders:
        p = o["pair"]
        if p not in tickers:
            try:
                r = requests.get(f"https://public.bitbank.cc/{p}/ticker", timeout=5).json()
                tickers[p] = float(r.get("data", {}).get("last", 0.0))
            except Exception:
                tickers[p] = 0.0

    lines = [
        "==================================================",
        "📋 【Bitbank 口座 現在配置中 指値・未約定注文 状況】",
        "=================================================="
    ]

    if not orders:
        lines.append("  現在、未約定の指値注文はありません。（全注文FLAT）")
        lines.append("==================================================")
        report_text = "\n".join(lines)
        discord.print_log(report_text)
        if to_discord:
            try:
                discord.send(report_text)
            except Exception:
                pass
        return report_text

    # ペアごとに集計
    by_pair: Dict[str, List[Dict[str, Any]]] = {}
    total_locked_jpy = 0.0

    for o in orders:
        p = o["pair"]
        by_pair.setdefault(p, []).append(o)

    for p, ord_list in by_pair.items():
        curr_px = tickers.get(p, 0.0)
        curr_str = f"{curr_px:,.1f} 円" if curr_px > 0 else "取得不可"
        lines.append(f"■ 通貨ペア: 【{p.upper()}】 (現在値: {curr_str})")
        lines.append(f"  配置注文数: {len(ord_list)} 件")

        pair_qty_sum = 0.0
        pair_val_sum = 0.0

        for idx, o in enumerate(ord_list, 1):
            px = o["price"]
            rem_qty = o["remaining_amount"]
            val_jpy = px * rem_qty
            pair_qty_sum += rem_qty
            pair_val_sum += val_jpy

            diff_str = ""
            if curr_px > 0:
                diff_pct = (px / curr_px - 1.0) * 100.0
                diff_str = f" [現値比: {diff_pct:+.1f}%]"

            lines.append(
                f"   #{idx} {o['side']} 指値: {px:,.1f} 円 | 数量: {rem_qty:,.1f} | 拘束額: {val_jpy:,.0f} 円{diff_str}"
            )

        avg_price = pair_val_sum / pair_qty_sum if pair_qty_sum > 0 else 0.0
        lines.append(f"  -> 合計数量: {pair_qty_sum:,.1f} {p.split('_')[0].upper()} | 全約定時平均建値: {avg_price:,.1f} 円 | 拘束額計: {pair_val_sum:,.0f} 円")
        lines.append("--------------------------------------------------")
        total_locked_jpy += pair_val_sum

    lines.append(f"  合計未約定注文数: {len(orders)} 件")
    lines.append(f"  指値による拘束JPY総額: 約 {total_locked_jpy:,.0f} 円")
    lines.append("==================================================")

    report_text = "\n".join(lines)
    discord.print_log(report_text)

    if to_discord:
        try:
            discord.send(report_text)
        except Exception:
            pass

    return report_text


if __name__ == "__main__":
    asyncio.run(report_active_orders(mode="live", to_discord=False))
