"""
Bitbank 5.46 API Module
Bitbank Spot & Public API Wrapper for Multi-Asset Trading Bot
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
import pybotters
import requests

from bitbank5_46_3logic import send_discord, backtester, PositionSizer, calc_add_pct, VPTrailingManager

from config_loader import (
    load_api_keys,
    BITBANK_PUBLIC_URL,
    BITBANK_REST_URL,
    BITBANK_WSS_URL,
    get_webhook_url,
)

# ==================== 動作モード・設定値 ====================
# 本番リアル口座への誤発注防止のための安全ロック
ALLOW_LIVE_TRADING = False  # 本番リアル発注を許可する場合は True に設定

bitbank_mode = 'demo'  # 'live' または 'demo'
BITBANK_TARGET_POSITION_VALUE_JPY = 15000.0  # 1ポジションあたりの目標投資額 (JPY)
LEVERAGE_FACTOR = 1.0  # 現物取引のため 1.0 倍固定
is_air = True  # AIRモード: True の場合は取引所へ実注文を出さず仮想シミュレーション実行

# ログ通知
discord = send_discord()

# ==================== ヘルパー関数 ====================
def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_symbol(symbol: str) -> str:
    """
    通貨ペア名を Bitbank 規格（小文字・アンダースコア: btc_jpy, xrp_jpy 等）に正規化
    例: 'BTC-USDT' -> 'btc_jpy', 'BTC_JPY' -> 'btc_jpy', 'BTC' -> 'btc_jpy'
    ※ RNDR は Bitbank 上で活発に取引されている render_jpy へエイリアス変換
    """
    sym = str(symbol).strip().lower()
    if "-" in sym:
        sym = sym.replace("-", "_")
    if sym.endswith("_usdt"):
        sym = sym.replace("_usdt", "_jpy")
    elif sym.endswith("usdt"):
        sym = f"{sym[:-4]}_jpy"
    elif "_" not in sym:
        sym = f"{sym}_jpy"
    if sym in ("rndr_jpy", "rndr"):
        sym = "render_jpy"
    return sym


def split_symbol(symbol: str) -> Tuple[str, str]:
    norm = normalize_symbol(symbol)
    parts = norm.split("_")
    base = parts[0]
    quote = parts[1] if len(parts) > 1 else "jpy"
    return base, quote


# ==================== Bitbank 銘柄仕様定義 ====================
# 各通貨ペアの最小発注数量・数量桁数・価格桁数 (指定11銘柄 完備)
BITBANK_SPECS: Dict[str, Dict[str, float]] = {
    "btc_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "eth_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "xrp_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "sol_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 1.0},
    "doge_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "bnb_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "arb_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "sui_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "avax_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "render_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "link_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "ltc_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "mona_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "bcc_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "xlm_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "klay_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "mkr_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 0.0},
    "matic_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "dot_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "trx_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "chz_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
    "ada_jpy": {"sz_decimals": 4.0, "qty_step": 0.0001, "min_qty": 0.0001, "price_place": 3.0},
}

# ==================== 長期保有BTC保護枠 ====================
# 口座内に存在する長期運用BTC（約0.2434 BTC）をボットの売買・決済対象から完全に除外・保護
LONG_TERM_PROTECTED_BTC: float = 0.2434


def fetch_instrument_spec_bitbank(symbol: str, mode: str = 'demo') -> Dict[str, float]:
    clean_sym = normalize_symbol(symbol)
    if clean_sym in BITBANK_SPECS:
        spec = BITBANK_SPECS[clean_sym].copy()
        spec["min_notional"] = 100.0  # 約100円
        spec["max_leverage"] = 1.0
        return spec
    # デフォルトスペック
    return {
        "sz_decimals": 4.0,
        "qty_step": 0.0001,
        "min_qty": 0.0001,
        "max_qty": float("inf"),
        "min_notional": 100.0,
        "max_leverage": 1.0,
        "price_place": 2.0,
    }


def compute_bitbank_lot_size(price: float, target_value_jpy: float, spec: dict) -> float:
    sz_decimals = int(spec.get("sz_decimals", 4.0))
    min_qty = float(spec.get("min_qty", 0.0001))
    if price <= 0:
        return min_qty
    base_qty = target_value_jpy / price
    if sz_decimals == 0:
        quantity = float(int(round(base_qty)))
    else:
        quantity = float(round(base_qty, sz_decimals))
    return max(quantity, min_qty)


# ==================== 認証・署名ヘルパー ====================
def sign_bitbank_query(secret_key: str, message: str) -> str:
    return hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


# ==================== Bitbank API Helper クラス ====================
class api_bitbank_helper:
    def __init__(self, symbol='btc_jpy', coin='JPY', mode='demo', instrument_spec=None):
        self.symbol = normalize_symbol(symbol)
        self.base_asset, self.quote_asset = split_symbol(self.symbol)
        self.coin = coin
        self.mode = mode
        
        self.public_url = BITBANK_PUBLIC_URL
        self.rest_url = BITBANK_REST_URL
        
        keys_dict = load_api_keys()
        bb_keys = keys_dict.get("bitbank", ["", ""])
        if isinstance(bb_keys, list) and len(bb_keys) >= 2:
            self.api_key = bb_keys[0]
            self.secret_key = bb_keys[1]
        elif isinstance(bb_keys, dict):
            self.api_key = bb_keys.get("api_key", "")
            self.secret_key = bb_keys.get("secret_key", "")
        else:
            self.api_key = ""
            self.secret_key = ""
            
        self.instrument_spec = instrument_spec or fetch_instrument_spec_bitbank(self.symbol, self.mode)

    def is_symbol_supported(self, symbol: Optional[str] = None) -> bool:
        sym = normalize_symbol(symbol or self.symbol)
        return sym in BITBANK_SPECS

    def update_instrument_spec(self, spec=None):
        self.instrument_spec = spec or fetch_instrument_spec_bitbank(self.symbol, self.mode)

    def _get_spec_value(self, key: str, default: float) -> float:
        return float((self.instrument_spec or {}).get(key, default))

    def _quantize_quantity(self, quantity: float) -> float:
        sz_decimals = int(self._get_spec_value("sz_decimals", 4.0))
        min_qty = self._get_spec_value("min_qty", 0.0001)
        try:
            if sz_decimals == 0:
                q = float(int(round(quantity)))
            else:
                decimal_step = Decimal(str(10 ** (-sz_decimals)))
                q = float(Decimal(str(quantity)).quantize(decimal_step, rounding=ROUND_HALF_UP))
            return max(q, min_qty) if q > 0 else 0.0
        except Exception:
            return quantity

    def _quantize_price(self, price: float) -> float:
        if price is None or price <= 0:
            return price
        price_place = int(self._get_spec_value("price_place", 0.0))
        try:
            if price_place == 0:
                return float(int(round(price)))
            decimal_step = Decimal(str(10 ** (-price_place)))
            return float(Decimal(str(price)).quantize(decimal_step, rounding=ROUND_HALF_UP))
        except Exception:
            return float(round(price, 2))

    async def _request_private(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        if not self.api_key or not self.secret_key or self.api_key.startswith("YOUR_"):
            return {"success": 0, "error": "NO_API_KEY"}
        
        async with pybotters.Client(apis={"bitbank": [self.api_key, self.secret_key]}) as client:
            try:
                url = f"{self.rest_url}{path}"
                if method.upper() == "GET":
                    res = await client.get(url, params=data or {})
                elif method.upper() == "POST":
                    res = await client.post(url, data=data or {})
                elif method.upper() == "DELETE":
                    res = await client.delete(url, data=data or {})
                else:
                    return {"success": 0, "error": f"UNSUPPORTED_METHOD_{method}"}
                return await res.json()
            except Exception as e:
                return {"success": 0, "error": str(e)}

    async def get_account(self) -> float:
        """Bitbank の JPY 総残高 (onhand_amount) を取得"""
        if not self.api_key or self.api_key.startswith("YOUR_"):
            return 100000.0  # Fallback balance (10万円)

        try:
            res = await self._request_private("GET", "/v1/user/assets")
            if res.get("success") == 1 and "data" in res and "assets" in res["data"]:
                for asset_info in res["data"]["assets"]:
                    if asset_info.get("asset", "").lower() == "jpy":
                        onhand = float(asset_info.get("onhand_amount", 0.0))
                        free = float(asset_info.get("free_amount", onhand))
                        discord.print_log(
                            f"Bitbank JPY 口座総残高: {onhand:,.0f} 円 (利用可能: {free:,.0f} 円)",
                            level="debug"
                        )
                        return onhand
        except Exception as e:
            discord.print_log(f"Bitbank get_account error: {e}")
        return 100000.0

    async def get_positions(self, lot_size=None) -> dict:
        """
        現物保有残高からポジション情報を構築
        - buy: 保有している暗号資産数量
        - buy_pos: 平均取得単価 (trade_history から算出)
        - profit / raw_pnl: 含み損益 (JPY)
        """
        position = {
            'buy': 0.00,
            'sell': 0.00,
            'buy_pos': 0.00,
            'sell_pos': 0.00,
            'profit': 0.00,
            'raw_pnl': 0.00,
            'total_fee': 0.00,
            'pos_count': 0.0,
            'buy_count': 0,
            'sell_count': 0,
            'margin_mode': 'spot',
        }

        if not self.api_key or self.api_key.startswith("YOUR_"):
            return position

        try:
            res = await self._request_private("GET", "/v1/user/assets")
            if res.get("success") == 1 and "data" in res and "assets" in res["data"]:
                onhand_coin = 0.0
                for asset_info in res["data"]["assets"]:
                    if asset_info.get("asset", "").lower() == self.base_asset:
                        onhand_coin = float(asset_info.get("onhand_amount", 0.0))
                        break

                if self.base_asset == "btc":
                    position['raw_onhand'] = onhand_coin
                    position['protected_cold'] = LONG_TERM_PROTECTED_BTC
                    position['buy'] = max(0.0, float(np.round(onhand_coin - LONG_TERM_PROTECTED_BTC, 6)))
                else:
                    position['buy'] = onhand_coin

            # 保有がある場合、平均取得単価を trade_history から算出
            if position['buy'] > 0:
                hist = await self._request_private("GET", "/v1/user/spot/trade_history", {"pair": self.symbol, "count": 100})
                if hist.get("success") == 1 and "data" in hist and "trades" in hist["data"]:
                    trades = hist["data"]["trades"]
                    buy_trades = [t for t in trades if t.get("side") == "buy"]
                    if buy_trades:
                        cum_amount = 0.0
                        weighted_sum = 0.0
                        for t in buy_trades:
                            amount = float(t.get("amount", 0))
                            price = float(t.get("price", 0))
                            remaining = position['buy'] - cum_amount
                            take = min(amount, remaining)
                            weighted_sum += take * price
                            cum_amount += take
                            if cum_amount >= position['buy']:
                                break
                        if cum_amount > 0:
                            position['buy_pos'] = float(np.round(weighted_sum / cum_amount, 2))

                # ステージ / ピラミッディング計算
                if lot_size and lot_size > 0:
                    buy_size = max(round(position['buy'] / lot_size), 0)
                    position['buy_count'] = buy_size
                    PositionSizer.build_cumulative_dict()
                    stage = PositionSizer.get_stage_from_position_size(buy_size)
                    position['pos_count'] = float(stage)
                else:
                    position['buy_count'] = 1 if position['buy'] > 0 else 0
                    position['pos_count'] = float(position['buy_count'])

        except Exception as e:
            discord.print_log(f"Bitbank get_positions error: {e}")

        return position

    async def get_open_orders(self) -> list[dict]:
        if not self.api_key or self.api_key.startswith("YOUR_"):
            return []
        try:
            res = await self._request_private("GET", "/v1/user/spot/active_orders", {"pair": self.symbol})
            if res.get("success") == 1 and "data" in res and "orders" in res["data"]:
                return res["data"]["orders"]
        except Exception as e:
            discord.print_log(f"Bitbank get_open_orders error: {e}")
        return []

    async def active_order_cancel(self) -> bool:
        """未約定の指値注文をすべてキャンセル"""
        if is_air:
            discord.print_log(f"[AIR MODE] Bitbank cancel orders for {self.symbol} (Mocked)")
            return True
        if not self.api_key or self.api_key.startswith("YOUR_"):
            return True
        try:
            orders = await self.get_open_orders()
            if not orders:
                return True
            for order in orders:
                order_id = order.get("order_id")
                if order_id:
                    await self._request_private("POST", "/v1/user/spot/cancel_order", {
                        "pair": self.symbol,
                        "order_id": order_id
                    })
            discord.print_log(f"[{self.symbol}] Bitbank 全未約定指値 ({len(orders)}件) の取り消しを実行しました。")
            return True
        except Exception as e:
            discord.print_log(f"Bitbank active_order_cancel error: {e}")
            return False

    async def get_orderbook(self) -> Tuple[Optional[float], Optional[float]]:
        """Ticker / Depth から best_bid, best_ask を取得"""
        try:
            async with pybotters.Client() as client:
                res = await client.get(f"{self.public_url}/{self.symbol}/ticker")
                data = await res.json()
                if data.get("success") == 1 and "data" in data:
                    t = data["data"]
                    best_bid = float(t.get("buy") or 0.0) or None
                    best_ask = float(t.get("sell") or 0.0) or None
                    return best_bid, best_ask
        except Exception as e:
            print(f"Bitbank orderbook fetch failed for {self.symbol}: {e}")
        return None, None

    async def get_candle(self, df: pd.DataFrame, interval: str = "1h", interval_int: int = 60, max_len: int = 300) -> pd.DataFrame:
        """
        Bitbank Public API から Candlestick (OHLCV) データを取得
        対応インターバル: '1hour' / '1h' -> '1hour', '1day' / '1d' -> '1day', '15m' -> '15min' 等
        """
        candletype = "1hour"
        interval_str = str(interval).lower()
        if "d" in interval_str or interval_str == "1day":
            candletype = "1day"
        elif "15" in interval_str:
            candletype = "15min"
        elif "5" in interval_str:
            candletype = "5min"
        elif "1" in interval_str and "h" not in interval_str:
            candletype = "1min"

        is_daily = candletype == "1day"
        days_needed = 35 if is_daily else min(30, max(2, (max_len * interval_int + 1439) // 1440))
        st_time = datetime.now(timezone.utc) - timedelta(days=days_needed)
        end_time = datetime.now(timezone.utc)

        all_rows = []
        try:
            async with pybotters.Client() as client:
                if is_daily:
                    years = range(st_time.year, end_time.year + 1)
                    for y in years:
                        url = f"{self.public_url}/{self.symbol}/candlestick/{candletype}/{y}"
                        res = await client.get(url)
                        data = await res.json()
                        if data.get("success") == 1 and "candlestick" in data.get("data", {}):
                            items = data["data"]["candlestick"]
                            if items:
                                all_rows.extend(items[0].get("ohlcv", []))
                else:
                    curr_date = st_time.date()
                    end_date = end_time.date()
                    while curr_date <= end_date:
                        date_str = curr_date.strftime("%Y%m%d")
                        url = f"{self.public_url}/{self.symbol}/candlestick/{candletype}/{date_str}"
                        res = await client.get(url)
                        data = await res.json()
                        if data.get("success") == 1 and "candlestick" in data.get("data", {}):
                            items = data["data"]["candlestick"]
                            if items:
                                all_rows.extend(items[0].get("ohlcv", []))
                        curr_date += timedelta(days=1)
        except Exception as e:
            print(f"Bitbank get_candle fetch error for {self.symbol}: {e}")

        if not all_rows:
            # ローカルキャッシュ CSV からのフォールバック
            data_dir = Path(__file__).resolve().parent / "Data"
            csv_path = data_dir / f"merged_{self.symbol.replace('_', '')}.csv"
            if csv_path.exists():
                try:
                    c_df = pd.read_csv(csv_path)
                    if not c_df.empty and 'timestamp' in c_df.columns:
                        c_df['timestamp'] = pd.to_datetime(c_df['timestamp'])
                        return c_df.sort_values('timestamp').reset_index(drop=True)
                except Exception:
                    pass
            return pd.DataFrame()

        df_out = pd.DataFrame(all_rows, columns=['open', 'high', 'low', 'close', 'volume', 'timestamp'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce')

        df_out['timestamp'] = pd.to_datetime(df_out['timestamp'].astype(float), unit='ms')
        df_out = df_out.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
        if len(df_out) > max_len:
            df_out = df_out.iloc[-max_len:].reset_index(drop=True)
        return df_out


# ==================== メイン API クラス (api_bitbank) ====================
class api_bitbank:
    def __init__(self, symbol='btc_jpy', category='spot', coin='JPY', mode='demo', instrument_spec=None):
        self.symbol = normalize_symbol(symbol)
        self.category = category
        self.coin = coin
        self.mode = mode
        self.instrument_spec = instrument_spec or {}
        
        self.bitbank = api_bitbank_helper(self.symbol, coin, mode, self.instrument_spec)
        self.bingx = self.bitbank  # 互換プロパティ

    def update_instrument_spec(self, spec=None):
        self.instrument_spec = spec or {}
        self.bitbank.update_instrument_spec(spec)

    def _get_spec_value(self, key: str, default: float) -> float:
        return self.bitbank._get_spec_value(key, default)

    def is_symbol_supported(self, symbol: Optional[str] = None) -> bool:
        return self.bitbank.is_symbol_supported(symbol)

    def _quantize_quantity(self, quantity: float) -> float:
        return self.bitbank._quantize_quantity(quantity)

    def _quantize_price(self, price: float) -> float:
        return self.bitbank._quantize_price(price)

    async def get_account(self) -> float:
        return await self.bitbank.get_account()

    async def get_candle(self, df: pd.DataFrame, interval: str = "1h", interval_int: int = 60, max_len: int = 300) -> pd.DataFrame:
        return await self.bitbank.get_candle(df, interval, interval_int, max_len=max_len)

    async def get_positions(self, lot_size=None) -> dict:
        return await self.bitbank.get_positions(lot_size)

    async def active_order_cancel(self) -> bool:
        return await self.bitbank.active_order_cancel()

    async def get_open_orders(self, include_stop: bool = True) -> list[dict]:
        return await self.bitbank.get_open_orders()

    async def long_entry(self, df, position, jpy_onhand_amount, lot_size, max_lot):
        """
        現物買いエントリー (Maker指値 post_only: True)
        """
        if lot_size == 0:
            raise ValueError("lot_size cannot be zero")
        current_stage = int(position.get("pos_count", 0))
        if current_stage >= 1:
            return False
        if not df['long'].iloc[-1]:
            return False

        current_price = float(df['close'].iloc[-1])
        spec = self.bitbank.instrument_spec or {}
        base_lot = compute_bitbank_lot_size(current_price, BITBANK_TARGET_POSITION_VALUE_JPY, spec)
        lot = self.bitbank._quantize_quantity(base_lot)
        if lot <= 0:
            return False

        lotamount = current_price * lot
        if jpy_onhand_amount < lotamount:
            discord.print_log(
                f"[Bitbank] 資金不足のためロング発注を見送ります: 必要 {lotamount:,.0f} 円, 残高 {jpy_onhand_amount:,.0f} 円"
            )
            return False

        # 気配値 (best_bid) 取得
        best_bid, best_ask = await self.bitbank.get_orderbook()
        target_bid = best_bid if best_bid is not None else current_price
        final_price = self.bitbank._quantize_price(target_bid)

        discord.print_log(f"[BITBANK] Placing Long Limit (Best Bid: {final_price:,.0f} JPY): Qty {lot}")

        if is_air:
            discord.print_log(f"[AIR MODE] Executed Bitbank Long Entry: {self.symbol} Qty={lot} Price={final_price} (Mock)")
            self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
            self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
            return True

        if not ALLOW_LIVE_TRADING:
            discord.print_log(f"[BITBANK] ALLOW_LIVE_TRADING が False のためリアル発注をブロックしました。")
            return False

        if not self.bitbank.api_key or self.bitbank.api_key.startswith("YOUR_"):
            discord.print_log(f"[BITBANK] APIキーが設定されていません。スキップします。")
            return False

        # 3回リトライ発注シーケンス
        target_lot = lot
        filled_qty = 0.0
        for attempt in range(3):
            await self.active_order_cancel()
            cur_bid, cur_ask = await self.bitbank.get_orderbook()
            attempt_price = self.bitbank._quantize_price(cur_bid if cur_bid is not None else current_price)
            needed_lot = self.bitbank._quantize_quantity(target_lot - filled_qty)
            if needed_lot <= 0:
                break

            order_params = {
                "pair": self.symbol,
                "amount": str(needed_lot),
                "price": str(attempt_price),
                "side": "buy",
                "type": "limit",
                "post_only": True
            }
            res = await self.bitbank._request_private("POST", "/v1/user/spot/order", order_params)
            discord.print_log(f"Bitbank Long Limit attempt {attempt+1}: price={attempt_price}, qty={needed_lot}, res={res}")

            await asyncio.sleep(4.0)
            pos = await self.get_positions()
            filled_qty = float(pos.get("buy", 0.0))
            if filled_qty >= target_lot * 0.95:
                discord.print_log(f"[OK] Bitbank Long エントリー約定完了: 保有量={filled_qty}/{target_lot}")
                self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
                self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
                return True

        if filled_qty > 0:
            discord.print_log(f"[PARTIAL] Bitbank Long 部分約定: 保有量={filled_qty}/{target_lot}")
            return True
        else:
            await self.active_order_cancel()
            discord.print_log(f"[SKIP] Bitbank Long 指値 3回リトライ未約定。エントリー見送り。")
            return False

    async def long_close(self, df, position, commission=0.0012, sl_margin_pct=1.0, strategy_type="range", is_new_candle=False, entry_candle_time=None):
        """
        現物決済（保有コインの売却手仕舞い）
        """
        pos_qty = float(position.get("buy", 0.0))
        if pos_qty <= 0:
            self.vp_trailing = None
            return False

        current_price = float(df['close'].iloc[-1])
        close_price = float(df['close'].iloc[-1])
        val_price = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else current_price * 0.99
        poc_price = float(df['POC'].iloc[-1]) if 'POC' in df.columns else current_price
        vah_price = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else current_price * 1.01
        avg_entry = float(position.get('buy_pos', 0)) or current_price

        init_m = (sl_margin_pct / 100.0) if sl_margin_pct > 0.0 else 0.01
        entry_val = getattr(self, 'entry_val', val_price)
        entry_vah = getattr(self, 'entry_vah', vah_price)
        if not hasattr(self, 'vp_trailing') or self.vp_trailing is None or self.vp_trailing.side != "LONG":
            self.vp_trailing = VPTrailingManager(
                "LONG", avg_entry, fee_margin_pct=0.0005, initial_margin_pct=init_m,
                strategy_type=strategy_type, entry_val=entry_val, entry_vah=entry_vah
            )

        should_close, reason, info = self.vp_trailing.update(
            current_price=current_price, close_price=close_price,
            val=val_price, poc=poc_price, vah=vah_price
        )

        discord.print_log(
            f"[BITBANK-VP-TRAIL] LONG Step={info['step_name']}({info['step']}), "
            f"Price={current_price:,.0f}, Stop={info['current_stop']:,.0f}, Reason={reason}",
            level="debug"
        )

        if should_close:
            discord.print_log(f"[BITBANK] VP Trailing Triggered ({reason}): Price {current_price} < Stop {info['current_stop']}")
            self.vp_trailing = None
            await self.bitbank.active_order_cancel()
            await asyncio.sleep(0.5)
            await flatten_current_position_bitbank(self.symbol, self.coin, self.mode, f"VPTrailing_{reason}", force_market=True)
            return True
        return False

    async def short_entry(self, df, position, usdt_onhand_amount, lot_size, max_lot):
        """Bitbank 現物取引のためショートは非対応"""
        return False

    async def short_close(self, *args, **kwargs):
        """Bitbank 現物取引のためショート決済は非対応"""
        return False


# ==================== グローバル関数 & エイリアス ====================
async def fetch_bitbank_candles(symbol: str, interval: str = "1h", limit: int = 300, mode: str = "demo") -> pd.DataFrame:
    interval_map = {'1': 1, '3': 3, '5': 5, '15': 15, '30': 30, '60': 60, '1h': 60, '2h': 120, '4h': 240, '1d': 1440}
    api = api_bitbank(symbol=symbol, mode=mode)
    df = pd.DataFrame()
    interval_int = interval_map.get(interval, 60)
    return await api.get_candle(df, interval, interval_int, max_len=limit)


async def flatten_current_position_bitbank(
    symbol: str,
    coin: str = 'JPY',
    mode: str = 'demo',
    reason: str = "",
    take_profit_pct: float = 0.0015,
    force_market: bool = True,
    product_type: str = 'SPOT'
) -> bool:
    """指定銘柄の現物ポジションを全量成行（または最良気配指値）で売却手仕舞い"""
    local_api = api_bitbank_helper(symbol, coin, mode)
    clean_sym = normalize_symbol(symbol)

    discord.print_log(f"[BITBANK] {reason}: 決済シーケンス開始。まず【{clean_sym}】の未約定指値をキャンセルします...")
    await local_api.active_order_cancel()
    await asyncio.sleep(0.5)

    pos = await local_api.get_positions()
    buy_qty = float(pos.get("buy", 0.0))
    if buy_qty <= 0:
        discord.print_log(f"[BITBANK] {reason}: 保有残高なし (0 FLAT)。決済完了。")
        return True

    order_qty = local_api._quantize_quantity(buy_qty)

    # 長期保有BTC二重安全物理ガード
    if clean_sym == "btc_jpy":
        try:
            res_assets = await local_api._request_private("GET", "/v1/user/assets")
            current_btc = 0.0
            if res_assets.get("success") == 1 and "data" in res_assets:
                for a in res_assets["data"].get("assets", []):
                    if a.get("asset", "").lower() == "btc":
                        current_btc = float(a.get("onhand_amount", 0.0))
            if current_btc <= LONG_TERM_PROTECTED_BTC:
                discord.print_log(f"🛡️ [SECURITY LOCK] BTC残高({current_btc:.4f})が長期保護枠({LONG_TERM_PROTECTED_BTC:.4f})以下のため、売却を完全遮断しました。")
                return True
            available_sell = max(0.0, current_btc - LONG_TERM_PROTECTED_BTC)
            order_qty = min(order_qty, local_api._quantize_quantity(available_sell))
            if order_qty <= 0:
                discord.print_log(f"🛡️ [SECURITY LOCK] 売却可能BTC数量が0のためスキップします。")
                return True
        except Exception as guard_err:
            discord.print_log(f"🛡️ [SECURITY LOCK ERROR] {guard_err}")
            return False

    discord.print_log(f"[BITBANK] {reason}: 保有コイン売却を実行 (数量: {order_qty} {local_api.base_asset.upper()}).")

    if is_air:
        discord.print_log(f"[AIR MODE] Bitbank flatten execution skipped: Reason: {reason} (Mock only)")
        return True

    if not ALLOW_LIVE_TRADING or not local_api.api_key or local_api.api_key.startswith("YOUR_"):
        return True

    try:
        # 成行売り発注
        order_params = {
            "pair": clean_sym,
            "amount": str(order_qty),
            "side": "sell",
            "type": "market"
        }
        res = await local_api._request_private("POST", "/v1/user/spot/order", order_params)
        discord.print_log(f"Bitbank market_close sell result: {res}")
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        discord.print_log(f"Bitbank flatten error: {e}")
        return False


async def fetch_all_position_symbols_bitbank(coin: str = 'JPY', mode: str = 'demo') -> list[str]:
    """現在残高を保有している銘柄リストを取得 (長期保護BTCは除外)"""
    helper = api_bitbank_helper("btc_jpy", coin, mode)
    if not helper.api_key or helper.api_key.startswith("YOUR_"):
        return []
    try:
        res = await helper._request_private("GET", "/v1/user/assets")
        if res.get("success") == 1 and "data" in res and "assets" in res["data"]:
            symbols = []
            for a in res["data"]["assets"]:
                asset = a.get("asset", "").lower()
                onhand = float(a.get("onhand_amount", 0.0))
                if asset != "jpy" and onhand > 0:
                    # 長期保有BTC保護: 0.2434 BTC 以下の場合はボットのアクティブポジションから除外
                    if asset == "btc" and onhand <= LONG_TERM_PROTECTED_BTC:
                        continue
                    pair = f"{asset}_jpy"
                    if pair in BITBANK_SPECS:
                        symbols.append(pair)
            return symbols
    except Exception as exc:
        discord.print_log(f"Bitbank fetch_all_position_symbols error: {exc}")
    return []


async def flatten_all_positions_bitbank(
    coin: str = 'JPY',
    mode: str = 'demo',
    reason: str = "",
    take_profit_pct: float = 0.0015,
    force_market: bool = True,
    product_type: str = 'SPOT'
) -> bool:
    symbols = await fetch_all_position_symbols_bitbank(coin, mode)
    if not symbols:
        return True
    all_ok = True
    for sym in symbols:
        ok = await flatten_current_position_bitbank(sym, coin, mode, reason, take_profit_pct, force_market)
        all_ok = all_ok and ok
    return all_ok


# ==================== 互換エイリアス ====================
api_bingx = api_bitbank
api_bingx_helper = api_bitbank_helper
fetch_bingx_candles = fetch_bitbank_candles
flatten_current_position = flatten_current_position_bitbank
flatten_all_positions = flatten_all_positions_bitbank
fetch_all_position_symbols = fetch_all_position_symbols_bitbank
fetch_all_position_symbols_bingx = fetch_all_position_symbols_bitbank
flatten_current_position_bingx = flatten_current_position_bitbank
flatten_all_positions_bingx = flatten_all_positions_bitbank
fetch_instrument_spec_bingx = fetch_instrument_spec_bitbank
compute_bingx_lot_size = compute_bitbank_lot_size
apis = load_api_keys()
RestAPI_url = {'bitbank': BITBANK_REST_URL, 'bingx': 'https://open-api.bingx.com', 'bingx_demo': 'https://open-api-vst.bingx.com'}
bingx_mode = bitbank_mode
BINGX_TARGET_POSITION_VALUE_USDT = BITBANK_TARGET_POSITION_VALUE_JPY

def sign_bingx(secret_key: str, query_str: str) -> str:
    return hmac.new(secret_key.encode('utf-8'), query_str.encode('utf-8'), hashlib.sha256).hexdigest()

def get_bingx_orderbook(symbol: str, base_url: Optional[str] = None, mode: str = 'demo') -> Tuple[Optional[float], Optional[float]]:
    try:
        norm = normalize_symbol(symbol)
        resp = requests.get(f"{BITBANK_PUBLIC_URL}/{norm}/ticker", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") == 1 and "data" in data:
                return float(data["data"].get("buy", 0.0)) or None, float(data["data"].get("sell", 0.0)) or None
    except Exception:
        pass
    return None, None
