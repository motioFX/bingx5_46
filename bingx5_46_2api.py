from __future__ import annotations
from bingx5_46_3logic import send_discord, backtester, PositionSizer, calc_add_pct, VPTrailingManager
from decimal import Decimal, ROUND_HALF_UP
import time
import pandas as pd
import numpy as np
import pybotters
import asyncio
import math
import sys
import json
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import hashlib
import hmac
import urllib.parse

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

#====================〇 API 設定値〇====================
# ユーザー指示: デモ/テストネットAPIキー確認前のため、トレードは行わない（完全安全ロック）
ALLOW_LIVE_TRADING = False  # False の場合、取引所への発注APIを物理的に完全遮断

bingx_mode = 'demo'  # 'live' または 'demo'
BINGX_TARGET_POSITION_VALUE_USDT = 15.0
LEVERAGE_FACTOR = 10.0
is_air = True  # シミュレーション・ペーパートレードモードを強制

# Helper to read values
def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

#====================〇アカウント情報ロード〇====================
bingx_config_path = Path(__file__).resolve().parent / "bingx_credentials.json"

try:
    with open(bingx_config_path, "r", encoding="utf-8") as fp:
        bingx_cfg = json.load(fp)
    apis_raw = bingx_cfg.get("apis", {})
    if "win32" in apis_raw or "default" in apis_raw or "linux" in apis_raw:
        apis_raw = apis_raw.get(sys.platform) or apis_raw.get("default") or {}
    default_key = bingx_cfg.get("api_key", "")
    default_secret = bingx_cfg.get("secret_key", "")
    trade_key = apis_raw.get("bingx_trade", {}).get("api_key") or default_key
    trade_secret = apis_raw.get("bingx_trade", {}).get("secret_key") or default_secret
    apis_bingx = {
        "bingx": {
            "api_key": apis_raw.get("bingx", {}).get("api_key") or default_key,
            "secret_key": apis_raw.get("bingx", {}).get("secret_key") or default_secret
        },
        "bingx_trade": {
            "api_key": trade_key,
            "secret_key": trade_secret
        },
        "bingx_demo": {
            "api_key": apis_raw.get("bingx_demo", {}).get("api_key") or default_key,
            "secret_key": apis_raw.get("bingx_demo", {}).get("secret_key") or default_secret
        }
    }
except Exception as e:
    apis_bingx = {
        "bingx": {"api_key": "", "secret_key": ""},
        "bingx_trade": {"api_key": "", "secret_key": ""},
        "bingx_demo": {"api_key": "", "secret_key": ""}
    }

apis = apis_bingx

# ポジションログの頻度制御（秒）。
POSITION_LOG_INTERVAL_SEC = 60 * 60
_last_position_log_ts = 0.0

RestAPI_url = {
    'bingx': 'https://open-api.bingx.com',
    'bingx_demo': 'https://open-api-vst.bingx.com',
}

discord = send_discord()

# Symbol Normalization Helpers
DEFAULT_PRODUCT_TYPE = "SWAP"

def normalize_product_type(product_type: str) -> str:
    return "SWAP"

def normalize_symbol(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    if "_" in sym:
        sym = sym.replace("_", "-")
    if sym.endswith("-USDT"):
        return sym
    if sym.endswith("USDT") and "-" not in sym:
        return f"{sym[:-4]}-USDT"
    if sym.endswith("-USDC"):
        return sym
    if sym.endswith("USDC") and "-" not in sym:
        return f"{sym[:-4]}-USDC"
    if "-" not in sym:
        return f"{sym}-USDT"
    return sym

def split_symbol_and_product_type(symbol: str, product_type: str) -> tuple[str, str]:
    return normalize_symbol(symbol), "SWAP"

def build_contract_symbol(symbol: str, product_type: str) -> str:
    return normalize_symbol(symbol)

def quantize_quantity(quantity: float, step: float) -> float:
    if step <= 0:
        return quantity
    decimal_step = Decimal(str(step)).normalize()
    return float(Decimal(str(quantity)).quantize(decimal_step, rounding=ROUND_HALF_UP))

# ==================== BingX 署名・通信ヘルパー ====================
def sign_bingx(secret_key: str, query_str: str) -> str:
    return hmac.new(
        secret_key.encode('utf-8'),
        query_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

_bingx_contracts_cache: Dict[str, Any] = {}
_bingx_contracts_last_fetch: float = 0.0

def get_bingx_contracts(base_url: Optional[str] = None, mode: str = 'demo') -> List[Dict[str, Any]]:
    global _bingx_contracts_cache, _bingx_contracts_last_fetch
    now = time.time()
    if _bingx_contracts_cache and (now - _bingx_contracts_last_fetch < 300):
        return list(_bingx_contracts_cache.values())
    if base_url is None:
        cred_key = 'bingx_demo' if mode in ('paper', 'demo', 'testnet') else 'bingx'
        base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')
    try:
        resp = requests.get(f"{base_url}/openApi/swap/v2/quote/contracts", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                contracts = data.get("data", [])
                _bingx_contracts_cache = {c["symbol"].upper(): c for c in contracts if "symbol" in c}
                _bingx_contracts_last_fetch = now
                return contracts
    except Exception as e:
        print(f"Failed to fetch BingX contracts: {e}")
    return list(_bingx_contracts_cache.values())

def fetch_instrument_spec_bingx(symbol: str, mode: str = 'demo') -> Optional[Dict[str, float]]:
    clean_sym = normalize_symbol(symbol).upper()
    get_bingx_contracts(mode=mode)
    contract = _bingx_contracts_cache.get(clean_sym)
    if contract:
        qty_precision = float(contract.get("quantityPrecision", 2))
        price_precision = float(contract.get("pricePrecision", 2))
        trade_min_qty = float(contract.get("tradeMinQuantity", 10 ** (-qty_precision) if qty_precision > 0 else 1.0))
        max_leverage = float(contract.get("maxLongLeverage", LEVERAGE_FACTOR))
        return {
            "sz_decimals": qty_precision,
            "qty_step": 10 ** (-qty_precision) if qty_precision > 0 else 1.0,
            "min_qty": trade_min_qty,
            "max_qty": float("inf"),
            "min_notional": float(contract.get("minNotional", 5.0)),
            "max_leverage": max_leverage,
            "price_place": price_precision,
        }
    return {
        "sz_decimals": 2.0,
        "qty_step": 0.01,
        "min_qty": 0.01,
        "max_qty": float("inf"),
        "min_notional": 5.0,
        "max_leverage": LEVERAGE_FACTOR,
        "price_place": 2.0,
    }

def get_bingx_universe_symbols(mode: str = 'demo') -> Set[str]:
    contracts = get_bingx_contracts(mode=mode)
    return {c["symbol"].upper() for c in contracts if "symbol" in c}

def get_bingx_orderbook(symbol: str, base_url: Optional[str] = None, mode: str = 'demo') -> Tuple[Optional[float], Optional[float]]:
    try:
        if base_url is None:
            cred_key = 'bingx_demo' if mode in ('paper', 'demo', 'testnet') else 'bingx'
            base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')
        clean_sym = normalize_symbol(symbol)
        resp = requests.get(f"{base_url}/openApi/swap/v2/quote/bookTicker?symbol={clean_sym}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                ticker = data.get("data", {})
                if "book_ticker" in ticker:
                    ticker = ticker["book_ticker"]
                best_bid = float(ticker.get("bid_price") or ticker.get("bidPrice") or 0.0) or None
                best_ask = float(ticker.get("ask_price") or ticker.get("askPrice") or 0.0) or None
                return best_bid, best_ask
    except Exception as e:
        print(f"Failed to fetch orderbook for {symbol}: {e}")
    return None, None

async def _async_bingx_request(
    method: str,
    base_url: str,
    endpoint: str,
    api_key: str,
    secret_key: str,
    params: Optional[dict] = None,
    timeout: int = 10
) -> dict:
    # 取引安全ブロック: トレード禁止またはAIRモード時は、取引所への発注/取消/レバレッジ変更等の取引系APIを物理的に完全遮断
    is_trade_endpoint = "/openApi/swap/v2/trade/" in endpoint
    if is_trade_endpoint and (is_air or not ALLOW_LIVE_TRADING):
        return {"code": 0, "msg": "MOCK_ORDER_AIR_MODE_TRADE_LOCKED", "data": {"orderId": "MOCK_AIR_ORDER"}}

    params = params.copy() if params else {}
    is_public = endpoint.startswith("/openApi/swap/v2/quote/")
    headers = {"X-BX-APIKEY": api_key} if api_key else {}
    
    if not is_public and secret_key and not secret_key.startswith("YOUR_"):
        params["timestamp"] = int(time.time() * 1000)
        sorted_params = sorted(params.items())
        query_str = urllib.parse.urlencode(sorted_params)
        signature = sign_bingx(secret_key, query_str)
        full_url = f"{base_url}{endpoint}?{query_str}&signature={signature}"
    else:
        query_str = urllib.parse.urlencode(params) if params else ""
        full_url = f"{base_url}{endpoint}" + (f"?{query_str}" if query_str else "")

    if HAS_AIOHTTP:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, full_url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    return await resp.json()
        except Exception:
            pass

    try:
        resp = requests.request(method, full_url, headers=headers, timeout=timeout)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}

# ==================== BingX API Helper クラス ====================
class api_bingx_helper:
    def __init__(self, symbol='BTC-USDT', product_type='SWAP', coin='USDT', mode='demo', instrument_spec=None):
        self.symbol = normalize_symbol(symbol)
        self.product_type = "SWAP"
        self.coin = coin
        self.mode = mode
        cred_key = 'bingx_demo' if self.mode in ('paper', 'demo', 'testnet') else 'bingx'
        self.base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if self.mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')
        
        self.creds = apis_bingx.get(cred_key) or apis_bingx.get('bingx') or {}
        self.api_key = self.creds.get("api_key", "")
        self.secret_key = self.creds.get("secret_key", "")
        self.instrument_spec = instrument_spec or fetch_instrument_spec_bingx(self.symbol, self.mode) or {}

    def is_symbol_supported(self, symbol: Optional[str] = None) -> bool:
        sym = normalize_symbol(symbol or self.symbol)
        universe = get_bingx_universe_symbols(self.mode)
        return sym in universe

    def update_instrument_spec(self, spec=None):
        self.instrument_spec = spec or fetch_instrument_spec_bingx(self.symbol, self.mode) or {}

    def _get_spec_value(self, key: str, default: float) -> float:
        spec = self.instrument_spec or {}
        value = spec.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _quantize_quantity(self, quantity: float) -> float:
        sz_decimals = int(self._get_spec_value("sz_decimals", 2.0))
        min_qty = self._get_spec_value("min_qty", 0.01)
        try:
            if sz_decimals == 0:
                q = float(int(round(quantity)))
            else:
                q = float(round(quantity, sz_decimals))
            return max(q, min_qty) if q > 0 else 0.0
        except Exception:
            return quantity

    def _quantize_price(self, price: float) -> float:
        if price is None or price <= 0:
            return price
        price_place = int(self._get_spec_value("price_place", 2.0))
        try:
            if price_place == 0:
                return float(int(round(price)))
            return float(round(price, price_place))
        except Exception:
            return float(round(price, 2))

    async def get_account(self) -> float:
        if not self.api_key or self.api_key.startswith("YOUR_") or is_air:
            return 1000.0  # Fallback demo/air balance

        try:
            res = await _async_bingx_request(
                "GET", self.base_url, "/openApi/swap/v2/user/balance",
                self.api_key, self.secret_key, timeout=10
            )
            if res.get("code") == 0:
                data = res.get("data", {})
                balance_data = data.get("balance", {})
                equity = float(balance_data.get("equity") or balance_data.get("balance") or 0.0)
                available = float(balance_data.get("availableMargin") or 0.0)
                discord.print_log(
                    f"BingX {self.coin} 口座総残高: {equity:.2f} USDT (利用可能: {available:.2f} USDT) [mode={self.mode}]",
                    level="debug"
                )
                return equity if equity > 0 else 1000.0
        except Exception as e:
            discord.print_log(f"BingX get_account error: {e}")
        return 1000.0

    async def get_positions(self, lot_size=None) -> dict:
        def build_default_position() -> dict:
            return {
                'buy': 0.00,
                'sell': 0.00,
                'buy_pos': 0.00,
                'sell_pos': 0.00,
                'profit': 0.000,
                'raw_pnl': 0.000,
                'total_fee': 0.000,
                'pos_count': 0.0,
                'buy_count': 0,
                'sell_count': 0,
                'margin_mode': 'crossed',
            }

        def finalize_position(pos: dict) -> dict:
            buy_qty = pos["buy"]
            sell_qty = pos["sell"]
            if lot_size and lot_size > 0:
                buy_size = max(round(buy_qty / lot_size), 0)
                sell_size = max(round(sell_qty / lot_size), 0)
                pos['buy_count'] = buy_size
                pos['sell_count'] = sell_size
                PositionSizer.build_cumulative_dict()
                stage = PositionSizer.get_stage_from_position_size(max(buy_size, sell_size))
                pos['pos_count'] = float(stage)
            else:
                pos['buy_count'] = 0 if buy_qty == 0 else 1
                pos['sell_count'] = 0 if sell_qty == 0 else 1
                pos['pos_count'] = float(max(pos['buy_count'], pos['sell_count']))
            return pos

        position = build_default_position()
        if not self.api_key or self.api_key.startswith("YOUR_") or is_air:
            return finalize_position(position)

        try:
            res = await _async_bingx_request(
                "GET", self.base_url, "/openApi/swap/v2/user/positions",
                self.api_key, self.secret_key, params={"symbol": self.symbol}, timeout=10
            )
            if res.get("code") == 0:
                pos_list = res.get("data", [])
                for p in pos_list:
                    sym = normalize_symbol(p.get("symbol", ""))
                    if sym == self.symbol:
                        amt = float(p.get("positionAmt", 0.0))
                        entry_px = float(p.get("entryPrice", 0.0))
                        unrealized_pnl = float(p.get("unrealizedProfit", 0.0))
                        side = str(p.get("positionSide", "")).upper()
                        
                        if side == "LONG" or (side == "BOTH" and amt > 0):
                            if abs(amt) > 0:
                                position['buy'] = abs(amt)
                                position['buy_pos'] = entry_px
                                position['profit'] = unrealized_pnl
                                position['raw_pnl'] = unrealized_pnl
                        elif side == "SHORT" or (side == "BOTH" and amt < 0):
                            if abs(amt) > 0:
                                position['sell'] = abs(amt)
                                position['sell_pos'] = entry_px
                                position['profit'] = unrealized_pnl
                                position['raw_pnl'] = unrealized_pnl
        except Exception as e:
            discord.print_log(f"BingX get_positions error: {e}")

        return finalize_position(position)

    async def get_open_orders(self) -> list[dict]:
        if not self.api_key or self.api_key.startswith("YOUR_") or is_air:
            return []
        try:
            res = await _async_bingx_request(
                "GET", self.base_url, "/openApi/swap/v2/trade/openOrders",
                self.api_key, self.secret_key, params={"symbol": self.symbol}, timeout=10
            )
            if res.get("code") == 0:
                orders = res.get("data", {}).get("orders", [])
                return [o for o in orders if normalize_symbol(o.get("symbol", "")) == self.symbol]
        except Exception as e:
            discord.print_log(f"BingX get_open_orders error: {e}")
        return []

    async def get_open_plan_orders(self) -> list[dict]:
        return await self.get_open_orders()

    async def active_order_cancel(self) -> bool:
        if is_air:
            discord.print_log(f"[AIR MODE] BingX cancel orders for {self.symbol} (Mocked)")
            return True
        if not self.api_key or self.api_key.startswith("YOUR_"):
            return True
        try:
            res = await _async_bingx_request(
                "DELETE", self.base_url, "/openApi/swap/v2/trade/allOpenOrders",
                self.api_key, self.secret_key, params={"symbol": self.symbol}, timeout=10
            )
            return res.get("code") == 0
        except Exception as e:
            discord.print_log(f"BingX active_order_cancel error: {e}")
            return False

    async def set_leverage(self, leverage: int = 10) -> bool:
        if is_air or not self.api_key or self.api_key.startswith("YOUR_"):
            return True
        try:
            for side in ["LONG", "SHORT"]:
                await _async_bingx_request(
                    "POST", self.base_url, "/openApi/swap/v2/trade/leverage",
                    self.api_key, self.secret_key,
                    params={"symbol": self.symbol, "leverage": leverage, "side": side}, timeout=5
                )
            return True
        except Exception:
            return False

    async def get_candle(self, df, interval, interval_int) -> pd.DataFrame:
        mapping = {
            '1': '1m', '3': '3m', '5': '5m', '15': '15m', '30': '30m',
            '60': '1h', '120': '2h', '240': '4h', '360': '4h', '720': '12h',
            'D': '1d',
            '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m', '30m': '30m',
            '1h': '1h', '4h': '4h', '6h': '4h', '12h': '12h', '1d': '1d'
        }
        bx_interval = mapping.get(str(interval), '1h')
        
        try:
            res = await _async_bingx_request(
                "GET", self.base_url, "/openApi/swap/v2/quote/klines",
                "", "", params={"symbol": self.symbol, "interval": bx_interval, "limit": 500}, timeout=15
            )
            if res.get("code") == 0:
                rows = res.get("data", [])
                if rows:
                    tmp_df = pd.DataFrame(rows)
                    tmp_df = tmp_df.rename(columns={
                        'time': 'timestamp',
                        'open': 'open',
                        'high': 'high',
                        'low': 'low',
                        'close': 'close',
                        'volume': 'volume'
                    })
                    numeric_cols = ['open', 'high', 'low', 'close', 'volume']
                    for col in numeric_cols:
                        tmp_df[col] = pd.to_numeric(tmp_df[col], errors='coerce')
                    tmp_df['timestamp'] = pd.to_datetime(tmp_df['timestamp'].astype(float), unit='ms')
                    return tmp_df.sort_values('timestamp').reset_index(drop=True)
        except Exception as e:
            print(f"BingX get_candle error for {self.symbol}: {e}")

        # ローカルキャッシュ CSV からのフォールバック
        data_dir = Path(__file__).resolve().parent / "Data"
        csv_path = data_dir / f"merged_{self.symbol.replace('-', '')}.csv"
        if csv_path.exists():
            try:
                c_df = pd.read_csv(csv_path)
                if not c_df.empty and 'timestamp' in c_df.columns:
                    c_df['timestamp'] = pd.to_datetime(c_df['timestamp'])
                    return c_df.sort_values('timestamp').reset_index(drop=True)
            except Exception:
                pass
        return pd.DataFrame()

# ==================== ロット計算ヘルパー ====================
def compute_bingx_lot_size(price: float, target_value: float, spec: dict) -> float:
    sz_decimals = int(spec.get("sz_decimals", 2.0))
    min_qty = float(spec.get("min_qty", 10 ** (-sz_decimals) if sz_decimals > 0 else 1.0))
    
    base_qty = target_value / price if price > 0 else min_qty
    if sz_decimals == 0:
        quantity = float(int(round(base_qty)))
    else:
        quantity = float(round(base_qty, sz_decimals))
    return max(quantity, min_qty)




# ==================== メイン API クラス (api_bingx) ====================
class api_bingx:
    def __init__(self, symbol='BTC-USDT', category='linear', coin='USDT', mode='demo', instrument_spec=None):
        self.symbol = normalize_symbol(symbol)
        self.category = category
        self.coin = coin
        self.mode = mode
        self.instrument_spec = instrument_spec or {}
        
        self.bingx_mode = bingx_mode
        self.bingx = api_bingx_helper(self.symbol, 'SWAP', coin, mode)

        try:
            bx_spec = fetch_instrument_spec_bingx(self.symbol, mode)
            if bx_spec:
                self.bingx.update_instrument_spec(bx_spec)
        except Exception as e:
            discord.print_log(f"[API] Error fetching BingX spec for {symbol}: {e}")

    def update_instrument_spec(self, spec=None):
        self.instrument_spec = spec or {}
        self.bingx.update_instrument_spec(spec)

    def _get_spec_value(self, key: str, default: float) -> float:
        return self.bingx._get_spec_value(key, default)

    def is_symbol_supported(self, symbol: Optional[str] = None) -> bool:
        return self.bingx.is_symbol_supported(symbol)

    def _quantize_quantity(self, quantity: float) -> float:
        return self.bingx._quantize_quantity(quantity)

    def _quantize_price(self, price: float) -> float:
        return self.bingx._quantize_price(price)

    async def get_account(self):
        return await self.bingx.get_account()

    async def get_candle(self, df, interval, interval_int):
        return await self.bingx.get_candle(df, interval, interval_int)

    async def get_positions(self, lot_size=None):
        return await self.bingx.get_positions(lot_size)

    async def active_order_cancel(self):
        return await self.bingx.active_order_cancel()

    async def get_open_orders(self, include_stop: bool = True) -> list[dict]:
        return await self.bingx.get_open_orders()

    async def long_entry(self, df, position, usdt_onhand_amount, lot_size, max_lot):
        if lot_size == 0:
            raise ValueError("lot_size cannot be zero")
        current_stage = int(position["pos_count"])
        if current_stage >= 1:
            return False
        if not df['long'].iloc[-1]:
            return False

        current_price = df['close'].iloc[-1]
        spec = self.bingx.instrument_spec or {}
        base_lot = compute_bingx_lot_size(current_price, BINGX_TARGET_POSITION_VALUE_USDT, spec)
        lot = self.bingx._quantize_quantity(base_lot)
        if lot <= 0:
            return False

        lotamount = (current_price * lot) / LEVERAGE_FACTOR
        if usdt_onhand_amount < lotamount:
            discord.print_log(f"Insufficient funds for BingX long entry: need {lotamount:.2f} (with {LEVERAGE_FACTOR:.0f}x leverage), have {usdt_onhand_amount:.2f}")
            return False

        # 板情報 (best_bid, best_ask) の取得
        best_bid, best_ask = get_bingx_orderbook(self.symbol, self.bingx.base_url)
        is_testnet = (self.mode in ('paper', 'demo', 'testnet')) or not BINGX_IS_LIVE

        # 板飛び許容スプレッドの判定（実勢価格に対して +0.5% 以内）
        MAX_SPREAD_TOLERANCE = 0.005  # 0.5%

        def determine_target_price(bid: Optional[float], ask: Optional[float], ref_price: float) -> tuple[float, str]:
            if is_testnet:
                # Askが実勢価格に対して0.5%以内に収まる正常な板であれば、テストネット・デモの約定率確保のためAsk約定を許可
                if ask is not None and ask <= ref_price * (1.0 + MAX_SPREAD_TOLERANCE):
                    return ask, f"Demo Normal Ask (${ask:.4f})"
                else:
                    # 板飛び（スプレッド異常拡大 または Ask欠損）: 飛んだ高値Askは掴まず、Best Bidまたは実勢価格で指値待機
                    target_bid = bid if (bid is not None and bid <= ref_price * (1.0 + MAX_SPREAD_TOLERANCE)) else ref_price
                    ask_str = f"${ask:.4f}" if ask is not None else "None"
                    return target_bid, f"Demo Safe Bid (${target_bid:.4f}, Ask={ask_str} 乖離大)"
            else:
                target_bid = bid if bid is not None else ref_price
                return target_bid, f"Live Best Bid (${target_bid:.4f})"

        order_price, price_type_str = determine_target_price(best_bid, best_ask, current_price)
        final_price = self.bingx._quantize_price(order_price)

        discord.print_log(f"[BINGX] Placing Stage 1 Long ({price_type_str}): Target Qty {lot}")
        if is_air:
            discord.print_log(f"[AIR MODE] Executed BingX Long Entry: {self.symbol} Qty={lot} Price={final_price} (Mock)")
            self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
            self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
            return True

        if not self.bingx.api_key or self.bingx.api_key.startswith("YOUR_"):
            discord.print_log(f"[BINGX] API Key not configured. Skipping live order.")
            return False

        await self.bingx.set_leverage(int(LEVERAGE_FACTOR))

        target_lot = lot
        filled_qty = 0.0
        for attempt in range(3):
            await self.active_order_cancel()
            cur_bid, cur_ask = get_bingx_orderbook(self.symbol, self.bingx.base_url)
            current_target, order_mode_label = determine_target_price(cur_bid, cur_ask, current_price)
            attempt_price = self.bingx._quantize_price(current_target)
            needed_lot = self.bingx._quantize_quantity(target_lot - filled_qty)
            if needed_lot <= 0:
                break

            order_params = {
                "symbol": self.symbol,
                "side": "BUY",
                "positionSide": "LONG",
                "type": "LIMIT",
                "price": str(attempt_price),
                "quantity": str(needed_lot),
                "timeInForce": "GTC"
            }
            try:
                res = await _async_bingx_request(
                    "POST", self.bingx.base_url, "/openApi/swap/v2/trade/order",
                    self.bingx.api_key, self.bingx.secret_key, params=order_params, timeout=10
                )
                discord.print_log(f"BingX Long Limit ({order_mode_label}) attempt {attempt+1}: price={attempt_price}, qty={needed_lot}, result={res}")
            except Exception as e:
                discord.print_log(f"BingX Long entry exception (attempt {attempt+1}): {e}")

            await asyncio.sleep(4.0)
            pos = await self.get_positions()
            filled_qty = float(pos.get("buy", 0.0))
            if filled_qty >= target_lot * 0.95:
                discord.print_log(f"[OK] Long エントリー約定完了: 保有量={filled_qty}/{target_lot} (attempt {attempt+1})")
                self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
                self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
                return True

        if filled_qty > 0:
            discord.print_log(f"[PARTIAL] Long 部分約定完了: 保有量={filled_qty}/{target_lot}")
            self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
            self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
            return True
        else:
            await self.active_order_cancel()
            discord.print_log(f"[SKIP] Long 指値 3回リトライ全て未約定。エントリー見送り。")
            return False

    async def long_close(self, df, position, commission, sl_margin_pct=1.0, strategy_type="range", is_new_candle=False, entry_candle_time=None):
        pos_qty = float(position["buy"])
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
            f"[BINGX-VP-TRAIL] LONG Step={info['step_name']}({info['step']}), "
            f"Price={current_price:.4f}, Stop={info['current_stop']:.4f}, Reason={reason}"
        )

        if should_close:
            discord.print_log(f"[BINGX] VP Trailing Triggered ({reason}): Price {current_price} < Stop {info['current_stop']}")
            self.vp_trailing = None
            await self.bingx.active_order_cancel()
            await asyncio.sleep(0.5)
            await flatten_current_position_bingx(self.symbol, self.coin, self.mode, f"VPTrailing_{reason}", force_market=True)
            return True
        return False

    async def short_entry(self, df, position, usdt_onhand_amount, lot_size, max_lot):
        if lot_size == 0:
            raise ValueError("lot_size cannot be zero")
        current_stage = int(position["pos_count"])
        if current_stage >= 1:
            return False
        if not df['short'].iloc[-1]:
            return False

        current_price = df['close'].iloc[-1]
        spec = self.bingx.instrument_spec or {}
        base_lot = compute_bingx_lot_size(current_price, BINGX_TARGET_POSITION_VALUE_USDT, spec)
        lot = self.bingx._quantize_quantity(base_lot)
        if lot <= 0:
            return False

        lotamount = (current_price * lot) / LEVERAGE_FACTOR
        if usdt_onhand_amount < lotamount:
            discord.print_log(f"Insufficient funds for BingX short entry: need {lotamount:.2f} (with {LEVERAGE_FACTOR:.0f}x leverage), have {usdt_onhand_amount:.2f}")
            return False

        # 板情報 (best_bid, best_ask) の取得
        best_bid, best_ask = get_bingx_orderbook(self.symbol, self.bingx.base_url)
        is_testnet = (self.mode in ('paper', 'demo', 'testnet')) or not BINGX_IS_LIVE

        MAX_SPREAD_TOLERANCE = 0.005  # 0.5%

        def determine_short_target_price(bid: Optional[float], ask: Optional[float], ref_price: float) -> tuple[float, str]:
            if is_testnet:
                # Bidが実勢価格に対して-0.5%以内に収まる正常な板であれば、テストネット・デモの利便性のためBid約定を許可
                if bid is not None and bid >= ref_price * (1.0 - MAX_SPREAD_TOLERANCE):
                    return bid, f"Demo Normal Bid (${bid:.4f})"
                else:
                    target_ask = ask if (ask is not None and ask >= ref_price * (1.0 - MAX_SPREAD_TOLERANCE)) else ref_price
                    bid_str = f"${bid:.4f}" if bid is not None else "None"
                    return target_ask, f"Demo Safe Ask (${target_ask:.4f}, Bid={bid_str} 乖離大)"
            else:
                target_ask = ask if ask is not None else ref_price
                return target_ask, f"Live Best Ask (${target_ask:.4f})"

        order_price, price_type_str = determine_short_target_price(best_bid, best_ask, current_price)
        final_price = self.bingx._quantize_price(order_price)

        discord.print_log(f"[BINGX] Placing Short Entry ({price_type_str}): Target Qty {lot}")
        if is_air:
            discord.print_log(f"[AIR MODE] Executed BingX Short Entry: {self.symbol} Qty={lot} Price={final_price} (Mock)")
            self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
            self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
            return True

        if not self.bingx.api_key or self.bingx.api_key.startswith("YOUR_"):
            discord.print_log(f"[BINGX] API Key not configured. Skipping live order.")
            return False

        await self.bingx.set_leverage(int(LEVERAGE_FACTOR))

        target_lot = lot
        filled_qty = 0.0
        for attempt in range(3):
            await self.active_order_cancel()
            cur_bid, cur_ask = get_bingx_orderbook(self.symbol, self.bingx.base_url)
            current_target, order_mode_label = determine_short_target_price(cur_bid, cur_ask, current_price)
            attempt_price = self.bingx._quantize_price(current_target)
            needed_lot = self.bingx._quantize_quantity(target_lot - filled_qty)
            if needed_lot <= 0:
                break

            order_params = {
                "symbol": self.symbol,
                "side": "SELL",
                "positionSide": "SHORT",
                "type": "LIMIT",
                "price": str(attempt_price),
                "quantity": str(needed_lot),
                "timeInForce": "GTC"
            }
            try:
                res = await _async_bingx_request(
                    "POST", self.bingx.base_url, "/openApi/swap/v2/trade/order",
                    self.bingx.api_key, self.bingx.secret_key, params=order_params, timeout=10
                )
                discord.print_log(f"BingX Short Limit ({order_mode_label}) attempt {attempt+1}: price={attempt_price}, qty={needed_lot}, result={res}")
            except Exception as e:
                discord.print_log(f"BingX Short entry exception (attempt {attempt+1}): {e}")

            await asyncio.sleep(4.0)
            pos = await self.get_positions()
            filled_qty = float(pos.get("sell", 0.0))
            if filled_qty >= target_lot * 0.95:
                discord.print_log(f"[OK] Short エントリー約定完了: 保有量={filled_qty}/{target_lot} (attempt {attempt+1})")
                self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
                self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
                return True

        if filled_qty > 0:
            discord.print_log(f"[PARTIAL] Short 部分約定完了: 保有量={filled_qty}/{target_lot}")
            self.entry_val = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else (current_price * 0.99)
            self.entry_vah = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else (current_price * 1.01)
            return True
        else:
            await self.active_order_cancel()
            discord.print_log(f"[SKIP] Short 指値 3回リトライ全て未約定。エントリー見送り。")
            return False

    async def short_close(self, df, position, commission, sl_margin_pct=1.0, strategy_type="range", is_new_candle=False, entry_candle_time=None):
        pos_qty = float(position["sell"])
        if pos_qty <= 0:
            self.vp_trailing = None
            return False

        current_price = float(df['close'].iloc[-1])
        close_price = float(df['close'].iloc[-1])
        avg_entry = float(position.get('sell_pos', 0)) or current_price
        val_price = float(df['VAL'].iloc[-1]) if 'VAL' in df.columns else current_price * 0.99
        poc_price = float(df['POC'].iloc[-1]) if 'POC' in df.columns else current_price
        vah_price = float(df['VAH'].iloc[-1]) if 'VAH' in df.columns else current_price * 1.01

        init_m = (sl_margin_pct / 100.0) if sl_margin_pct > 0.0 else 0.01
        if not hasattr(self, 'vp_trailing') or self.vp_trailing is None or self.vp_trailing.side != "SHORT":
            self.vp_trailing = VPTrailingManager("SHORT", avg_entry, fee_margin_pct=0.0005, initial_margin_pct=init_m, strategy_type=strategy_type)

        should_close, reason, info = self.vp_trailing.update(
            current_price=current_price, close_price=close_price,
            val=val_price, poc=poc_price, vah=vah_price
        )

        discord.print_log(
            f"[BINGX-VP-TRAIL] SHORT Step={info['step_name']}({info['step']}), "
            f"Price={current_price:.4f}, Stop={info['current_stop']:.4f}, Reason={reason}"
        )

        if should_close:
            discord.print_log(f"[BINGX] VP Trailing Triggered ({reason}): Price {current_price} > Stop {info['current_stop']}")
            self.vp_trailing = None
            await self.bingx.active_order_cancel()
            await asyncio.sleep(0.5)
            await flatten_current_position_bingx(self.symbol, self.coin, self.mode, f"VPTrailing_{reason}", force_market=True)
            return True
        return False

# ==================== グローバル関数 & エイリアス ====================
async def fetch_bingx_candles(symbol: str, interval: str = "1h", limit: int = 100) -> pd.DataFrame:
    interval_map = {'1': 1, '3': 3, '5': 5, '15': 15, '30': 30, '60': 60, '1h': 60, '2h': 120, '4h': 240, '1d': 1440}
    api = api_bingx(symbol=symbol, mode="demo")
    df = pd.DataFrame()
    interval_int = interval_map.get(interval, 60)
    return await api.get_candle(df, interval, interval_int)

async def flatten_current_position_bingx(
    symbol: str,
    coin: str = 'USDT',
    mode: str = 'demo',
    reason: str = "",
    take_profit_pct: float = 0.0015,
    force_market: bool = True,
    product_type: str = 'SWAP'
) -> bool:
    local_api = api_bingx_helper(symbol, 'SWAP', coin, mode)
    position = await local_api.get_positions()
    buy_qty = float(position.get("buy", 0.0))
    sell_qty = float(position.get("sell", 0.0))

    if buy_qty <= 0 and sell_qty <= 0:
        return True

    discord.print_log(f"[BINGX] {reason}: starting flatten (buy={buy_qty}, sell={sell_qty}).")
    if is_air:
        discord.print_log(f"[AIR MODE] BingX flatten execution skipped: Reason: {reason} (Mock only)")
        return True

    await local_api.active_order_cancel()

    if not local_api.api_key or local_api.api_key.startswith("YOUR_"):
        return True

    try:
        if buy_qty > 0:
            params = {
                "symbol": local_api.symbol,
                "side": "SELL",
                "positionSide": "LONG",
                "type": "MARKET",
                "quantity": str(local_api._quantize_quantity(buy_qty))
            }
            res = await _async_bingx_request("POST", local_api.base_url, "/openApi/swap/v2/trade/order", local_api.api_key, local_api.secret_key, params=params)
            discord.print_log(f"BingX market_close Long result: {res}")
        elif sell_qty > 0:
            params = {
                "symbol": local_api.symbol,
                "side": "BUY",
                "positionSide": "SHORT",
                "type": "MARKET",
                "quantity": str(local_api._quantize_quantity(sell_qty))
            }
            res = await _async_bingx_request("POST", local_api.base_url, "/openApi/swap/v2/trade/order", local_api.api_key, local_api.secret_key, params=params)
            discord.print_log(f"BingX market_close Short result: {res}")
        return True
    except Exception as e:
        discord.print_log(f"BingX flatten error: {e}")
        return False

async def fetch_all_position_symbols_bingx(coin: str = 'USDT', mode: str = 'demo') -> list[str]:
    cred_key = 'bingx_demo' if mode in ("paper", "demo", "testnet") else 'bingx'
    creds = apis_bingx.get(cred_key) or apis_bingx.get('bingx') or {}
    api_key = creds.get("api_key", "")
    secret_key = creds.get("secret_key", "")
    if not api_key or api_key.startswith("YOUR_") or is_air:
        return []
    base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')
    try:
        res = await _async_bingx_request("GET", base_url, "/openApi/swap/v2/user/positions", api_key, secret_key)
        if res.get("code") == 0:
            positions = res.get("data", [])
            symbols = []
            for p in positions:
                amt = float(p.get("positionAmt", 0.0))
                if abs(amt) > 0:
                    symbols.append(normalize_symbol(p.get("symbol", "")))
            return list(set(symbols))
    except Exception as exc:
        discord.print_log(f"BingX fetch_all_position_symbols error: {exc}")
    return []

async def flatten_all_positions_bingx(
    coin: str = 'USDT',
    mode: str = 'demo',
    reason: str = "",
    take_profit_pct: float = 0.0015,
    force_market: bool = True,
    product_type: str = 'SWAP'
) -> bool:
    symbols = await fetch_all_position_symbols_bingx(coin, mode)
    if not symbols:
        return True
    all_ok = True
    for sym in symbols:
        ok = await flatten_current_position_bingx(sym, coin, mode, reason, take_profit_pct, force_market)
        all_ok = all_ok and ok
    return all_ok

# ==================== 汎用エイリアス ====================
flatten_current_position = flatten_current_position_bingx
flatten_all_positions = flatten_all_positions_bingx
fetch_all_position_symbols = fetch_all_position_symbols_bingx

