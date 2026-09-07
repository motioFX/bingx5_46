"""1-Hour Whale Position Sentiment Fetcher & Aggregator for Hyperliquid.

Reads tracked whale addresses from whales_list.json, queries clearinghouseState
for each whale, aggregates positions per coin (LONG / SHORT counts & volume),
and outputs the 70%+ threshold sentiment signal to Data/whale_market_state.json.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
import requests

API_URL = "https://api.hyperliquid.xyz/info"
THRESHOLD_RATIO = 0.70  # 70% threshold for LONG / SHORT signal

BASE_DIR = Path(__file__).resolve().parent
WHALES_FILE = BASE_DIR / "whales_list.json"
OUT_DIR = BASE_DIR / "Data"
OUT_FILE = OUT_DIR / "whale_market_state.json"


def load_whales() -> List[Dict[str, str]]:
    if not WHALES_FILE.exists():
        print(f"[Warning] Whales file not found at {WHALES_FILE}")
        return []
    try:
        with open(WHALES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("whales", [])
    except Exception as e:
        print(f"[Error] Failed to load whales list: {e}")
        return []


def fetch_whale_state(address: str) -> Dict[str, Any]:
    payload = {"type": "clearinghouseState", "user": address}
    try:
        resp = requests.post(API_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"  [Error] Fetching address {address[:8]}...: {e}")
    return {}


def format_usd(val: float) -> str:
    sign = "+" if val >= 0 else "-"
    abs_v = abs(val)
    if abs_v >= 1e6:
        return f"{sign}${abs_v / 1e6:.2f}M"
    elif abs_v >= 1e3:
        return f"{sign}${abs_v / 1e3:.1f}K"
    else:
        return f"{sign}${abs_v:.0f}"

def analyze_whale_positions(whales: List[Dict[str, str]]) -> Dict[str, Any]:
    print("==================================================")
    print("  Hyperliquid 1-Hour Whale Sentiment & Net Flow Analysis")
    print(f"  Target Whales: {len(whales)} addresses")
    print("==================================================")

    # coin -> {"long_count": 0, "short_count": 0, "long_val": 0.0, "short_val": 0.0, "whales_holding": set()}
    coin_stats: Dict[str, Dict[str, Any]] = {}
    valid_whales_count = 0

    for idx, whale in enumerate(whales, 1):
        name = whale.get("name", f"Whale_{idx}")
        addr = whale.get("address", "")
        if not addr:
            continue

        print(f"[{idx:02d}/{len(whales):02d}] Fetching {name} ({addr[:10]}...)...")
        state = fetch_whale_state(addr)
        time.sleep(0.2)  # Avoid rate limit

        if not state:
            continue
        valid_whales_count += 1

        asset_positions = state.get("assetPositions", [])
        for item in asset_positions:
            pos = item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0.0))
            entry_px = float(pos.get("entryPx", 0.0))
            pos_val = abs(szi * entry_px)

            if szi == 0 or not coin:
                continue

            if coin not in coin_stats:
                coin_stats[coin] = {
                    "long_count": 0,
                    "short_count": 0,
                    "long_val": 0.0,
                    "short_val": 0.0,
                    "whales_holding": set()
                }

            coin_stats[coin]["whales_holding"].add(addr)

            if szi > 0:
                coin_stats[coin]["long_count"] += 1
                coin_stats[coin]["long_val"] += pos_val
            else:
                coin_stats[coin]["short_count"] += 1
                coin_stats[coin]["short_val"] += pos_val

    # Sentiment aggregation per coin
    sentiment_results = {}
    print("\n----------------------------------------------------------------------------------------------------")
    print(f"{'Coin':<10} | {'Whales':<6} | {'LONG':<6} | {'SHORT':<6} | {'LONG Ratio':<10} | {'Whale Net Flow ($)':<18} | {'Signal':<12}")
    print("----------------------------------------------------------------------------------------------------")

    for coin, stats in sorted(coin_stats.items(), key=lambda x: (x[1]["long_val"] - x[1]["short_val"]), reverse=True):
        holding_whales = len(stats["whales_holding"])
        longs = stats["long_count"]
        shorts = stats["short_count"]
        total_active = longs + shorts

        long_val = stats["long_val"]
        short_val = stats["short_val"]
        net_val = long_val - short_val

        long_ratio = longs / total_active if total_active > 0 else 0.0
        short_ratio = shorts / total_active if total_active > 0 else 0.0

        if long_ratio >= THRESHOLD_RATIO:
            signal = "LONG_ONLY"
        elif short_ratio >= THRESHOLD_RATIO:
            signal = "SHORT_ONLY"
        else:
            signal = "NEUTRAL"

        net_val_fmt = format_usd(net_val)

        sentiment_results[coin] = {
            "whales_holding": holding_whales,
            "long_count": longs,
            "short_count": shorts,
            "long_val_usdt": long_val,
            "short_val_usdt": short_val,
            "net_val_usd": net_val,
            "net_val_formatted": net_val_fmt,
            "long_ratio": round(long_ratio, 4),
            "short_ratio": round(short_ratio, 4),
            "signal": signal
        }

        print(f"{coin:<10} | {holding_whales:<6d} | {longs:<6d} | {shorts:<6d} | {long_ratio*100:6.1f}%     | {net_val_fmt:<18} | {signal:<12}")

    print("----------------------------------------------------------------------------------------------------")

    output_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_tracked_whales": len(whales),
        "valid_whales_checked": valid_whales_count,
        "threshold_ratio": THRESHOLD_RATIO,
        "coins_sentiment": sentiment_results
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"\n[Success] Whale sentiment & net flow analysis saved to: {OUT_FILE}")
    return output_data


def main():
    whales = load_whales()
    if not whales:
        print("[Error] No whales configured.")
        return
    analyze_whale_positions(whales)


if __name__ == "__main__":
    main()
