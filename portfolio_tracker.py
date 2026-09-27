"""Bitbank & Binance Japan 総合暗号資産ポートフォリオ・指値注文トラッカー

毎時間サイクルおよび定期スクリーニング時に、購入済みの全暗号資産（現物）の保有量、
平均取得単価（建値）、現在価格、評価額、含み損益、未約定指値注文を自動監査し、
ターミナルおよび Discord へ報告します。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import ccxt

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import load_binance_api_keys, load_config
from bitbank5_46_2api import api_bitbank
from bitbank5_46_3logic import discord

JST = timezone(timedelta(hours=9))


# =====================================================================
# 1. Bitbank ポジション & 指値監査
# =====================================================================
async def fetch_bitbank_portfolio(mode: str = "live") -> Dict[str, Any]:
    """Bitbank の保有暗号資産、取得単価、損益、指値注文を取得"""
    api = api_bitbank("btc_jpy", coin="JPY", mode=mode)
    helper = api.bitbank

    result: Dict[str, Any] = {
        "jpy": {"total": 0.0, "free": 0.0, "locked": 0.0},
        "assets": [],
        "orders": [],
        "total_valuation_jpy": 0.0,
        "total_pnl_jpy": 0.0,
    }

    raw_assets = []
    for attempt in range(3):
        try:
            res_assets = await helper._request_private("GET", "/v1/user/assets")
            if res_assets.get("success") == 1 and "data" in res_assets and "assets" in res_assets["data"]:
                raw_assets = res_assets["data"]["assets"]
                break
            else:
                await asyncio.sleep(0.8)
        except Exception as e:
            if attempt == 2:
                discord.print_log(f"[Bitbank Portfolio Error] 資産取得失敗: {e}", level="debug")
            await asyncio.sleep(0.8)

    if not raw_assets:
        return result

    # JPY および 保有暗号資産の抽出
    crypto_assets = []
    for a in raw_assets:
        asset_name = a.get("asset", "").lower()
        onhand = float(a.get("onhand_amount", 0.0))
        free = float(a.get("free_amount", 0.0))
        locked = float(a.get("locked_amount", 0.0))

        if asset_name == "jpy":
            result["jpy"] = {"total": onhand, "free": free, "locked": locked}
        elif onhand > 0.00001:  # 微小残高除外
            crypto_assets.append({
                "asset": asset_name.upper(),
                "symbol": f"{asset_name}_jpy",
                "amount": onhand,
                "free": free,
                "locked": locked,
            })

    # 各暗号資産の現在値、約定履歴（建値計算）、評価損益
    for ca in crypto_assets:
        pair = ca["symbol"]
        amount = ca["amount"]

        # 現在価格 (Ticker)
        current_price = 0.0
        try:
            r = requests.get(f"https://public.bitbank.cc/{pair}/ticker", timeout=5).json()
            if r.get("success") == 1:
                current_price = float(r.get("data", {}).get("last", 0.0))
        except Exception:
            pass

        # 約定履歴から移動加重平均建値を計算
        avg_entry = 0.0
        try:
            # BTCの場合は既存の長期BTC建値設定を参照
            if ca["asset"] == "BTC":
                from long_term_btc_tracker import KNOWN_ENTRY_PRICE
                avg_entry = KNOWN_ENTRY_PRICE
            else:
                res_trades = await helper._request_private("GET", f"/v1/user/spot/trade_history?pair={pair}&count=100")
                if res_trades.get("success") == 1:
                    trades = res_trades.get("data", {}).get("trades", [])
                    sorted_trades = sorted(trades, key=lambda x: x.get("executed_at", 0))
                    t_qty = 0.0
                    t_cost = 0.0
                    for t in sorted_trades:
                        px = float(t.get("price", 0.0))
                        amt = float(t.get("amount", 0.0))
                        side = t.get("side", "").lower()
                        if side == "buy":
                            t_qty += amt
                            t_cost += px * amt
                        elif side == "sell":
                            if t_qty > 0:
                                current_avg = t_cost / t_qty
                                sell_amt = min(amt, t_qty)
                                t_qty -= sell_amt
                                t_cost = t_qty * current_avg
                    if t_qty > 0:
                        avg_entry = t_cost / t_qty
        except Exception:
            pass

        # 評価額と損益
        valuation = amount * current_price if current_price > 0 else 0.0
        cost = amount * avg_entry if avg_entry > 0 else valuation
        pnl = valuation - cost if avg_entry > 0 else 0.0
        pnl_pct = (current_price / avg_entry - 1.0) * 100.0 if avg_entry > 0 else 0.0

        if valuation < 10.0:  # 評価額10円未満のゴミ残高はスキップ
            continue

        ca["current_price"] = current_price
        ca["avg_entry"] = avg_entry
        ca["valuation"] = valuation
        ca["cost"] = cost
        ca["pnl"] = pnl
        ca["pnl_pct"] = pnl_pct

        result["total_valuation_jpy"] += valuation
        result["total_pnl_jpy"] += pnl
        result["assets"].append(ca)

    # 未約定アクティブ指値注文の取得
    try:
        from active_orders_tracker import fetch_active_orders
        result["orders"] = await fetch_active_orders(mode=mode)
    except Exception as e:
        discord.print_log(f"[Bitbank Orders Error] 注文取得失敗: {e}", level="debug")

    return result


# =====================================================================
# 2. Binance Japan ポジション & 指値監査
# =====================================================================
def fetch_binance_portfolio() -> Dict[str, Any]:
    """Binance Japan の保有暗号資産、取得単価、損益、指値注文を取得"""
    result: Dict[str, Any] = {
        "jpy": {"total": 0.0, "free": 0.0, "locked": 0.0},
        "assets": [],
        "orders": [],
        "total_valuation_jpy": 0.0,
        "total_pnl_jpy": 0.0,
    }

    binance_keys = load_binance_api_keys()
    if not binance_keys or len(binance_keys) < 2 or not binance_keys[0]:
        return result

    api_key, secret_key = binance_keys[0], binance_keys[1]

    try:
        exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": secret_key,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
                "recvWindow": 60000,
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            }
        })
        balance = exchange.fetch_balance()
    except Exception as e:
        discord.print_log(f"[Binance Portfolio Error] 残高取得失敗: {e}", level="debug")
        return result

    # JPY および 保有暗号資産の抽出
    crypto_assets = []
    if "total" in balance:
        for asset, total in balance["total"].items():
            if total <= 0.0001:  # 微小残高除外
                continue
            free = float(balance.get(asset, {}).get("free", 0.0))
            locked = float(balance.get(asset, {}).get("used", 0.0))

            if asset.upper() == "JPY":
                result["jpy"] = {"total": total, "free": free, "locked": locked}
            else:
                crypto_assets.append({
                    "asset": asset.upper(),
                    "symbol": f"{asset.upper()}/JPY",
                    "raw_symbol": f"{asset.upper()}JPY",
                    "amount": total,
                    "free": free,
                    "locked": locked,
                })

    # 各暗号資産の現在値、約定履歴、評価損益
    for ca in crypto_assets:
        sym = ca["symbol"]
        raw_sym = ca["raw_symbol"]
        amount = ca["amount"]

        # 現在価格
        current_price = 0.0
        try:
            t = exchange.fetch_ticker(sym)
            current_price = float(t.get("last", 0.0))
        except Exception:
            pass

        # 約定履歴から移動加重平均建値を計算
        avg_entry = 0.0
        try:
            trades = exchange.fetch_my_trades(sym, limit=100)
            if trades:
                sorted_trades = sorted(trades, key=lambda x: x.get("timestamp", 0))
                t_qty = 0.0
                t_cost = 0.0
                for t in sorted_trades:
                    side = t.get("side", "").upper()
                    amt = float(t.get("amount", 0.0))
                    px = float(t.get("price", 0.0))
                    cost = float(t.get("cost", amt * px))
                    if side == "BUY":
                        t_qty += amt
                        t_cost += cost
                    elif side == "SELL":
                        if t_qty > 0:
                            current_avg = t_cost / t_qty
                            sell_amt = min(amt, t_qty)
                            t_qty -= sell_amt
                            t_cost = t_qty * current_avg
                if t_qty > 0:
                    avg_entry = t_cost / t_qty
        except Exception:
            pass

        valuation = amount * current_price if current_price > 0 else 0.0
        cost = amount * avg_entry if avg_entry > 0 else valuation
        pnl = valuation - cost if avg_entry > 0 else 0.0
        pnl_pct = (current_price / avg_entry - 1.0) * 100.0 if avg_entry > 0 else 0.0

        if valuation < 10.0:  # 評価額10円未満は除外
            continue

        ca["current_price"] = current_price
        ca["avg_entry"] = avg_entry
        ca["valuation"] = valuation
        ca["cost"] = cost
        ca["pnl"] = pnl
        ca["pnl_pct"] = pnl_pct

        result["total_valuation_jpy"] += valuation
        result["total_pnl_jpy"] += pnl
        result["assets"].append(ca)

    # 未約定指値注文 (fetch_open_orders)
    try:
        open_orders = []
        # まず保有中通貨のペア（例: NEAR/JPY）で個別に取得
        checked_symbols = set()
        for ca in crypto_assets:
            s = ca["symbol"]
            checked_symbols.add(s)
            try:
                ords = exchange.fetch_open_orders(s)
                if ords:
                    open_orders.extend(ords)
            except Exception:
                pass
        
        # シンボルなし全体も念のため確認
        try:
            all_ords = exchange.fetch_open_orders()
            for o in all_ords:
                if not any(o.get("id") == x.get("id") for x in open_orders):
                    open_orders.append(o)
        except Exception:
            pass

        for o in open_orders:
            dt_str = o.get("datetime", "")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(JST)
                    dt_str = dt.strftime("%m-%d %H:%M")
                except Exception:
                    pass
            result["orders"].append({
                "order_id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side", "").upper(),
                "type": o.get("type", "").upper(),
                "price": float(o.get("price", 0.0)),
                "amount": float(o.get("amount", 0.0)),
                "remaining": float(o.get("remaining", o.get("amount", 0.0))),
                "datetime": dt_str,
            })
    except Exception as e:
        discord.print_log(f"[Binance Orders Error] 注文取得失敗: {e}", level="debug")

    return result


# =====================================================================
# 3. 総合レポート生成 & Discord送信
# =====================================================================
async def report_all_positions(
    mode: str = "live",
    to_discord: bool = True,
    header_title: Optional[str] = None,
    compact: bool = False
) -> str:
    """Bitbank & Binance Japan の全ポジション・指値状況を包括レポート"""
    # 並行取得
    bb_task = fetch_bitbank_portfolio(mode=mode)
    loop = asyncio.get_running_loop()
    binance_task = loop.run_in_executor(None, fetch_binance_portfolio)

    bb_res, binance_res = await asyncio.gather(bb_task, binance_task)

    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    title = header_title or f"🏦 【全保有暗号資産 総合ポジション監査】 ({now_str} JST)"

    lines = [
        "==================================================",
        f"📋 {title}",
        "=================================================="
    ]

    total_crypto_val = bb_res["total_valuation_jpy"] + binance_res["total_valuation_jpy"]
    total_crypto_pnl = bb_res["total_pnl_jpy"] + binance_res["total_pnl_jpy"]
    total_jpy_equity = bb_res["jpy"]["total"] + binance_res["jpy"]["total"]
    grand_total_equity = total_jpy_equity + total_crypto_val

    # ---------- A. Bitbank セクション ----------
    lines.append("\n🔶 [Bitbank 口座]")
    bb_jpy = bb_res["jpy"]
    lines.append(f"  ・JPY残高: {bb_jpy['total']:,.0f} 円 (利用可能: {bb_jpy['free']:,.0f} 円 | 指値拘束: {bb_jpy['locked']:,.0f} 円)")
    if bb_res["assets"]:
        for a in bb_res["assets"]:
            pnl_icon = "🟢" if a["pnl"] >= 0 else "🔴"
            px_fmt = f"{a['current_price']:,.3f}円" if a['current_price'] < 1000 else f"{a['current_price']:,.0f}円"
            entry_fmt = f"{a['avg_entry']:,.3f}円" if a['avg_entry'] < 1000 else f"{a['avg_entry']:,.0f}円"
            lines.append(
                f"  ・{a['asset']}: {a['amount']:,.4f} 枚 | 建値: {entry_fmt} -> 現値: {px_fmt} | 評価額: {a['valuation']:,.0f}円 | 損益: {a['pnl']:+,.0f}円 ({a['pnl_pct']:+.2f}%) {pnl_icon}"
            )
    else:
        lines.append("  ・保有暗号資産なし")

    if bb_res["orders"]:
        lines.append(f"  ・未約定指値: {len(bb_res['orders'])} 件")
        for o in bb_res["orders"]:
            val = o["price"] * o["remaining_amount"]
            lines.append(f"     └ {o['pair'].upper()} {o['side']} 指値: {o['price']:,.1f}円 | 数量: {o['remaining_amount']:,.1f} | 拘束: {val:,.0f}円")
    else:
        lines.append("  ・未約定指値なし")

    # ---------- B. Binance Japan セクション ----------
    lines.append("\n🔶 [Binance Japan 口座]")
    bin_jpy = binance_res["jpy"]
    lines.append(f"  ・JPY残高: {bin_jpy['total']:,.0f} 円 (利用可能: {bin_jpy['free']:,.0f} 円 | 指値拘束: {bin_jpy['locked']:,.0f} 円)")
    if binance_res["assets"]:
        for a in binance_res["assets"]:
            pnl_icon = "🟢" if a["pnl"] >= 0 else "🔴"
            px_fmt = f"{a['current_price']:,.2f}円" if a['current_price'] < 1000 else f"{a['current_price']:,.0f}円"
            entry_fmt = f"{a['avg_entry']:,.2f}円" if a['avg_entry'] < 1000 else f"{a['avg_entry']:,.0f}円"
            lines.append(
                f"  ・{a['asset']}: {a['amount']:,.4f} 枚 | 建値: {entry_fmt} -> 現値: {px_fmt} | 評価額: {a['valuation']:,.0f}円 | 損益: {a['pnl']:+,.0f}円 ({a['pnl_pct']:+.2f}%) {pnl_icon}"
            )
    else:
        lines.append("  ・保有暗号資産なし")

    if binance_res["orders"]:
        lines.append(f"  ・未約定指値: {len(binance_res['orders'])} 件")
        for o in binance_res["orders"]:
            val = o["price"] * o["remaining"]
            lines.append(f"     └ {o['symbol']} {o['side']} 指値: {o['price']:,.1f}円 | 数量: {o['remaining']:,.1f} | 拘束: {val:,.0f}円")
    else:
        lines.append("  ・未約定指値なし")

    # ---------- C. 総合総括 ----------
    pnl_overall_icon = "🟢" if total_crypto_pnl >= 0 else "🔴"
    lines.append("--------------------------------------------------")
    lines.append(f"💰 暗号資産 評価総額: 約 {total_crypto_val:,.0f} 円")
    lines.append(f"📊 暗号資産 含み損益計: 約 {total_crypto_pnl:+,.0f} 円 {pnl_overall_icon}")
    lines.append(f"🏦 全口座 純資産総額 (現金+現物): 約 {grand_total_equity:,.0f} 円")
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
    asyncio.run(report_all_positions(mode="live", to_discord=False))
