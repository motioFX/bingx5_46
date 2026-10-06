# -*- coding: utf-8 -*-
"""
bingx_ws_manager.py
pybotters を用いた BingX Perpetual Swap WebSocket 管理モジュール
- 5分足 Kline ({symbol}@kline_5m) のリアルタイムストリーミング購読
- GZIP 解凍 ＆ 自動 Ping/Pong 応答（切断防止）
- 最新価格・最新5分足キャッシュの保持
- 5分足確定イベント検知
"""

import asyncio
import gzip
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Callable, Set
import pybotters

JST = timezone(timedelta(hours=9))

class BingXWSManager:
    """BingX Perpetual Swap pybotters WebSocket クライアント"""

    WS_URL = "wss://open-api-swap.bingx.com/swap-market"

    def __init__(self, symbols: Optional[List[str]] = None, on_candle_close: Optional[Callable[[str, dict], Any]] = None):
        self.symbols: Set[str] = set(symbols or [])
        self.on_candle_close = on_candle_close
        
        # 状態キャッシュ
        self.latest_prices: Dict[str, float] = {}
        self.latest_klines: Dict[str, dict] = {}
        self.last_candle_times: Dict[str, int] = {}  # symbol -> last candle T (ms)
        
        self._client: Optional[pybotters.Client] = None
        self._ws_app: Optional[Any] = None
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws_conn: Optional[Any] = None

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """指定銘柄の最新WebSocket価格を取得"""
        return self.latest_prices.get(symbol)

    def get_latest_kline(self, symbol: str) -> Optional[dict]:
        """指定銘柄の最新5分足ローソク足データを取得"""
        return self.latest_klines.get(symbol)

    async def start(self) -> None:
        """WebSocket接続を開始"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        # 初回接続確立を少し待機
        await asyncio.sleep(1.0)

    async def stop(self) -> None:
        """WebSocket接続を安全に終了"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
            self._client = None

    async def update_symbols(self, new_symbols: List[str]) -> None:
        """監視銘柄リストを動的に更新（新銘柄を購読）"""
        new_set = set(new_symbols)
        added = new_set - self.symbols
        self.symbols = new_set

        if self._ws_conn and added:
            for sym in added:
                await self._subscribe_symbol(self._ws_conn, sym)

    async def _subscribe_symbol(self, ws: Any, symbol: str) -> None:
        """指定銘柄の5分足Klineを購読"""
        data_type = f"{symbol}@kline_5m"
        req = {
            "id": f"sub_{symbol}_5m",
            "reqType": "sub",
            "dataType": data_type
        }
        try:
            await ws.send_str(json.dumps(req))
        except Exception:
            pass

    async def _run_loop(self) -> None:
        """pybotters Client を用いた常駐接続ループ"""
        while self._running:
            try:
                async with pybotters.Client() as client:
                    self._client = client

                    def on_message_bytes(msg: bytes, ws: Any) -> None:
                        self._ws_conn = ws
                        try:
                            text = gzip.decompress(msg).decode('utf-8')
                            if text.lower() == 'ping':
                                # Ping に対して Pong を即座に応答（接続維持）
                                asyncio.create_task(ws.send_str("Pong"))
                                return
                            
                            data = json.loads(text)
                            d_type = data.get("dataType", "")
                            
                            if d_type and "@kline_5m" in d_type:
                                sym = d_type.split("@")[0]
                                k_data = data.get("data")
                                if isinstance(k_data, list) and len(k_data) > 0:
                                    k = k_data[0]
                                    close_px = float(k.get("c", 0.0))
                                    open_px = float(k.get("o", 0.0))
                                    high_px = float(k.get("h", 0.0))
                                    low_px = float(k.get("l", 0.0))
                                    vol = float(k.get("v", 0.0))
                                    candle_end_ms = int(k.get("T", 0))

                                    if close_px > 0:
                                        self.latest_prices[sym] = close_px
                                    
                                    kline_obj = {
                                        "symbol": sym,
                                        "open": open_px,
                                        "high": high_px,
                                        "low": low_px,
                                        "close": close_px,
                                        "volume": vol,
                                        "end_time_ms": candle_end_ms,
                                        "timestamp": datetime.fromtimestamp(candle_end_ms / 1000, tz=timezone.utc).astimezone(JST)
                                    }
                                    self.latest_klines[sym] = kline_obj

                                    # 5分足確定判定（タイムスタンプ T が進んだ場合）
                                    prev_t = self.last_candle_times.get(sym)
                                    if prev_t is not None and candle_end_ms > prev_t:
                                        if self.on_candle_close:
                                            try:
                                                self.on_candle_close(sym, kline_obj)
                                            except Exception:
                                                pass
                                    self.last_candle_times[sym] = candle_end_ms

                        except Exception:
                            pass

                    # 初期購読リストの生成
                    sub_list = [
                        {"id": f"sub_{s}_5m", "reqType": "sub", "dataType": f"{s}@kline_5m"}
                        for s in self.symbols
                    ]

                    # 接続開始 (pybotters の自動再接続 backoff 機構を活用)
                    self._ws_app = await client.ws_connect(
                        self.WS_URL,
                        send_json=sub_list if sub_list else None,
                        hdlr_bytes=on_message_bytes,
                        autoping=True,
                        heartbeat=15.0
                    )

                    # 接続維持ループ
                    while self._running:
                        await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception:
                if self._running:
                    await asyncio.sleep(3.0)
