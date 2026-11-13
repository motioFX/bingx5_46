# -*- coding: utf-8 -*-
"""
bingx5_46 統一銘柄ユニバース定義 (Single Source of Truth)

仕様:
- 実取引対象ユニバース: 21アルト銘柄 (PROJECT_RULES.md 準拠)
- 相場環境判定専用ベンチマーク: BTC-USDT
- データレイク管理ユニバース: 合計 22銘柄 (21アルト + BTC-USDT)
- 完全除外メガキャップ: BTC-USDT, ETH-USDT (売買エントリー対象外)
"""

from __future__ import annotations

from typing import Dict, Iterable, List

BENCHMARK_SYMBOL: str = "BTC-USDT"

EXCLUDED_MEGA_CAPS: List[str] = ["BTC-USDT", "ETH-USDT"]

FIXED_TRADABLE_SYMBOLS: List[str] = [
    "HYPE-USDT",
    "SOL-USDT",
    "DOGE-USDT",
    "XRP-USDT",
    "SUI-USDT",
    "AVAX-USDT",
    "LINK-USDT",
    "APT-USDT",
    "NEAR-USDT",
    "ARB-USDT",
    "UNI-USDT",
    "OP-USDT",
    "TIA-USDT",
    "RENDER-USDT",
    "INJ-USDT",
    "TAO-USDT",
    "AAVE-USDT",
    "ENA-USDT",
    "ZEC-USDT",
    "1000PEPE-USDT",
    "WIF-USDT",
]

# データレイク蓄積対象 (実取引21銘柄 + 地合いベンチマークBTC-USDT = 計22銘柄)
LAKE_UNIVERSE_SYMBOLS: List[str] = list(FIXED_TRADABLE_SYMBOLS) + [BENCHMARK_SYMBOL]

# OKX / Binance 等の外部取引所シンボル補完時に、BingX表記へ正規化するためのエイリアス
_CANONICAL_ALIASES: Dict[str, str] = {
    "PEPE": "1000PEPE-USDT",
    "PEPE-USDT": "1000PEPE-USDT",
    "1000PEPE": "1000PEPE-USDT",
    "RNDR": "RENDER-USDT",
    "RNDR-USDT": "RENDER-USDT",
}


def normalize_symbol(symbol: str) -> str:
    """任意の銘柄表記を標準 BingX 先物形式 (`{BASE}-USDT`) に正規化する。"""
    s = str(symbol or "").strip().upper().replace("_", "-").replace("/", "-")
    if not s:
        return ""
    if s in _CANONICAL_ALIASES:
        return _CANONICAL_ALIASES[s]
    if s.endswith("-USDT"):
        base = s[:-5]
        return _CANONICAL_ALIASES.get(base, s)
    if s.endswith("USDT") and "-" not in s and len(s) > 4:
        base = s[:-4]
        return _CANONICAL_ALIASES.get(base, f"{base}-USDT")
    return _CANONICAL_ALIASES.get(s, f"{s}-USDT")


def to_base_ticker(symbol: str) -> str:
    """`{BASE}-USDT` 形式からベース通貨名のみを返す。"""
    norm = normalize_symbol(symbol)
    return norm[:-5] if norm.endswith("-USDT") else norm


def is_tradable_symbol(symbol: str) -> bool:
    """売買エントリー対象アルト21銘柄に含まれるか判定する。"""
    norm = normalize_symbol(symbol)
    return norm in FIXED_TRADABLE_SYMBOLS and norm not in EXCLUDED_MEGA_CAPS


def get_universe_records() -> List[Dict[str, object]]:
    """データレイク `universe_symbols` テーブル投入用レコード群を返す。"""
    rows: List[Dict[str, object]] = []
    for sym in FIXED_TRADABLE_SYMBOLS:
        rows.append({"symbol": sym, "role": "ALT", "is_tradable": 1})
    rows.append({"symbol": BENCHMARK_SYMBOL, "role": "BTC", "is_tradable": 0})
    rows.append({"symbol": "ETH-USDT", "role": "EXCLUDED_MEGA", "is_tradable": 0})
    return rows


def filter_tradable_symbols(symbols: Iterable[str]) -> List[str]:
    """与えられた銘柄列から除外銘柄を除き、重複なく正規化して返す。"""
    seen = set()
    out: List[str] = []
    for raw in symbols:
        norm = normalize_symbol(raw)
        if is_tradable_symbol(norm) and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
