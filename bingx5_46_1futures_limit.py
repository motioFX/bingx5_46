# === Time Synchronization & Import Path Setup ===
from __future__ import annotations
import sys
from pathlib import Path
import time
import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Automatic Dual Logging: Output to both Terminal and Data/bot_output.log
class TeeLogger:
    def __init__(self, filepath: Path, original_stdout, max_size_mb: int = 15):
        self.terminal = original_stdout
        self.filepath = filepath
        self.max_bytes = max_size_mb * 1024 * 1024
        self._ansi_regex = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def write(self, message):
        if self.terminal:
            try:
                self.terminal.write(message)
            except Exception:
                pass
        try:
            # ログファイルが上限サイズを超えた場合は古い部分をローテーション
            if self.filepath.exists() and self.filepath.stat().st_size > self.max_bytes:
                content = self.filepath.read_text(encoding="utf-8", errors="ignore")
                keep_content = content[-int(self.max_bytes // 2):]  # 後半半分を残す
                self.filepath.write_text(keep_content, encoding="utf-8", errors="ignore")

            clean_msg = self._ansi_regex.sub('', message)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(clean_msg)
        except Exception:
            pass

    def flush(self):
        if self.terminal:
            try:
                self.terminal.flush()
            except Exception:
                pass

log_dir = Path(__file__).resolve().parent / "Data"
log_dir.mkdir(parents=True, exist_ok=True)
log_file_path = log_dir / "bot_output.log"
sys.stdout = TeeLogger(log_file_path, sys.stdout)
sys.stderr = TeeLogger(log_file_path, sys.stderr)

# Prioritize the directory containing this script for imports
script_dir = str(Path(__file__).resolve().parent)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

try:
    # Measure clock offset or test connectivity to BingX
    requests.get('https://open-api.bingx.com/openApi/swap/v2/quote/contracts', timeout=5)
except Exception as e:
    pass
# ==================== 動作モード設定 ====================
# [ 0 ] 安全ロック (Safety Interlock)
#       ユーザー指示: デモ/テストネットキー確認前のためトレード完全禁止
ALLOW_LIVE_TRADING = False

# [ 1 ] BingX 口座指定
#       True  = 本番口座 (Live Account)
#       False = デモ口座 (Demo Account)
BINGX_IS_LIVE = False if not ALLOW_LIVE_TRADING else False

# [ 2 ] BingX 注文実行・APIキー指定 (AIR Mode)
#       True  = AIRモード (指値・注文を実際に発注せずペーパートレードシミュレーション)
#       False = リアル注文 (実際の BingX 取引所へ発注)
BINGX_IS_AIR = True if not ALLOW_LIVE_TRADING else True

# [ 3 ] ポジション・ロット設定
BINGX_TARGET_POSITION_VALUE_USDT = 15.0
TARGET_POSITION_VALUE_USDT = BINGX_TARGET_POSITION_VALUE_USDT
LEVERAGE_FACTOR = 10.0
MAX_ACTIVE_POSITIONS: int = 2  # 最大同時保有ポジション数 (資金効率と分散を最適化)
MAX_SELECTED_SYMBOLS: int = 10  # 最大選定・監視銘柄数 (10銘柄体制へ拡張)

# [ 4 ] 定期銘柄選定・リセット時刻（JST時間: 0〜23時）
#       デフォルト: [1, 9, 17] (01:00, 09:00, 17:00 JST / 8時間ごと・主要セッション＆FR節目)。
#       CLI引数 (--analysis-hours / --reset-hours / --reset-hour) での上書き指定も可能。
ANALYSIS_HOURS: List[int] = [1, 9, 17]
DAILY_ANALYSIS_MINUTE = 0

# コマンドライン引数からの選定時刻オーバーライド処理
for _idx, _arg in enumerate(sys.argv):
    if _arg in ("--analysis-hours", "--reset-hours", "--screening-hours") and _idx + 1 < len(sys.argv):
        try:
            ANALYSIS_HOURS = [int(h.strip()) for h in sys.argv[_idx + 1].split(",") if h.strip()]
        except ValueError:
            pass
    elif _arg in ("--reset-hour", "--screening-hour") and _idx + 1 < len(sys.argv):
        try:
            ANALYSIS_HOURS = [int(sys.argv[_idx + 1])]
        except ValueError:
            pass
    elif _arg.startswith("--analysis-hours=") or _arg.startswith("--reset-hours="):
        try:
            ANALYSIS_HOURS = [int(h.strip()) for h in _arg.split("=")[1].split(",") if h.strip()]
        except ValueError:
            pass
    elif _arg.startswith("--reset-hour=") or _arg.startswith("--screening-hour="):
        try:
            ANALYSIS_HOURS = [int(_arg.split("=")[1])]
        except ValueError:
            pass
# ========================================================================

import bingx5_46_2api
bingx5_46_2api.bingx_mode = 'live' if BINGX_IS_LIVE else 'demo'
bingx5_46_2api.is_air = BINGX_IS_AIR
bingx5_46_2api.BINGX_TARGET_POSITION_VALUE_USDT = BINGX_TARGET_POSITION_VALUE_USDT
bingx5_46_2api.LEVERAGE_FACTOR = LEVERAGE_FACTOR

from bingx5_46_2api import (
    api_bingx,
    apis,
    RestAPI_url,
    flatten_current_position,
    flatten_all_positions,
    fetch_all_position_symbols,
    bingx_mode,
    fetch_instrument_spec_bingx,
    compute_bingx_lot_size,
    normalize_symbol,
)
from bingx5_46_3logic import send_discord, logicinstance, PnLCalculator, MPStrategy, backtester, run_interval_comparison, resample_candles

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import asyncio
import json
import math
import numpy as np
import pandas as pd
import pybotters
import gc

def clear_and_reset_daily_memory(symbol_apis: dict) -> None:
    """1日1回、古いメモリ・キャッシュ・変数を完全に初期化・物理破棄する"""
    symbol_apis.clear()
    gc.collect()
    from bingx5_46_3logic import send_discord
    discord = send_discord()
    discord.print_log("[Daily Reset] 1日1回のデイリー状態・メモリを完全クリア＆リセットしました。")

import requests
import subprocess
import sys
import time
from rich import print
from typing import Any, Dict, List, Optional, Set, Tuple
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
else:
    asyncio.set_event_loop_policy(None)

SKIP_MIX_ANALYSIS: bool = True
is_air: bool = BINGX_IS_AIR
current_strategy_type: str = "range"

import signal
def signal_handler(sig, frame):
    print('\nプログラムを終了します')
    try:
        loop = asyncio.get_running_loop()
        loop.stop()
    except RuntimeError:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

print('実行中... Ctrl+Cで停止できます')

Bybittimescale=True
if(Bybittimescale):
    interval_list = ['1', '3', '5', '15', '30', '60', '120', '240', '360', '720', 'D']
    interval_map ={'1':1,'3':3,'5':5,'15':15,'30':30,'60':60,'120':120,'240':240,'360':360,'720':720,'D':1440}
else:
    interval_list = ['1m','3m','5m','15m','30m','1h','4h','6h','12h','1d']
    interval_map ={'1m':1,'3m':3,'5m':5,'15m':15,'30m':30,'1h':60,'4h':240,'6h':360,'12h':720,'1d':1440}

JST = timezone(timedelta(hours=9))
MIX_SCRIPT_PATH = Path(__file__).resolve().parent / "bingx5_46_4mix_candle_Merged_Alt10.py"
SCORES_CSV_PATH = MIX_SCRIPT_PATH.parent / "Data" / "symbol_selection_scores.csv"

TARGET_POSITION_VALUE_USDT = 100.0
LEVERAGE_FACTOR = 10.0
LEVERAGE_USAGE_RATIO = 0.25
TARGET_LEVERAGE = 8.5

TAKE_PROFIT_PCT_FOR_FLATTEN = 0.0015
SYMBOL_SWITCH_FLATTEN_START_HOUR = 12
SYMBOL_SWITCH_FORCE_CLOSE_HOUR = 15
FLATTEN_REISSUE_MINUTES = 60
PAUSED_FORCE_MARKET_DELAY_HOURS = 2

DEFAULT_INSTRUMENT_SPEC = {
    "qty_step": 0.01,
    "min_qty": 0.01,
    "max_qty": float("inf"),
    "min_notional": 0.0,
    "max_leverage": LEVERAGE_FACTOR,
}

instrument_spec: Dict[str, float] = DEFAULT_INSTRUMENT_SPEC.copy()
pnl_symbols: List[str] = []
trade_paused: bool = False
entry_candle_time = None

discord = send_discord()

def format_state_message(symbol: str, position: Dict[str, Any], open_orders_count: int, pending_flatten: bool, trade_side: str = "long") -> str:
    buy_qty = position.get("buy", 0.0)
    sell_qty = position.get("sell", 0.0)
    pnl = position.get("profit", 0.0)
    raw_pnl = position.get("raw_pnl", pnl)
    total_fee = position.get("total_fee", 0.0)

    fee_info = f" (価格差: {raw_pnl:+.2f}, 手数料: -{total_fee:.2f})" if total_fee > 0 else ""

    if buy_qty > 0:
        pos_str = f"🟢 保有: LONG {buy_qty} | 実質含み損益: {pnl:+.2f} USDT{fee_info}"
    elif sell_qty > 0:
        pos_str = f"🔴 保有: SHORT {sell_qty} | 実質含み損益: {pnl:+.2f} USDT{fee_info}"
    else:
        pos_str = f"⚪ 保有: なし (ノーポジ) | 方向: {trade_side.upper()}"

    extra = f" | 注文数: {open_orders_count}"
    if pending_flatten:
        extra += " | 全決済処理中"

    return f"{symbol} | {pos_str}{extra}"


def format_decision_message(exit_reason: Optional[str], evaluated: bool, fade_signal: bool, time_window: str, pnl: float, trade_side: str = "long") -> str:
    return f"exit_reason={exit_reason} (evaluated={evaluated}, fade_signal={fade_signal}, time_window={time_window}, net_pnl={pnl:+.2f})"


def log_state(message: str) -> None:
    discord.print_log(f"[STATE] {message}")


def log_decision(message: str) -> None:
    discord.print_log(f"[DECISION] {message}")


def log_action(message: str) -> None:
    discord.print_log(f"[ACTION] {message}")


def decide_exit_reason(ctx: Dict[str, Any]) -> Optional[str]:
    if ctx.get("force_flatten"):
        return "force_flatten"
    if ctx.get("fade_take_profit"):
        return "fade_take_profit"
    return None


def extract_scoring_candidates(trade_plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if not trade_plan:
        return candidates
    scoring = trade_plan.get("scoring")
    if not isinstance(scoring, dict):
        return candidates
    raw_candidates = scoring.get("top_candidates")
    if not isinstance(raw_candidates, list):
        return candidates
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        symbol = raw.get("symbol")
        if not symbol:
            continue
        symbol_str = str(symbol).upper()
        score = raw.get("score", 0.0)
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            score_value = 0.0
        components_raw = raw.get("components", {})
        components: Dict[str, float] = {}
        if isinstance(components_raw, dict):
            for key, value in components_raw.items():
                try:
                    components[key] = float(value)
                except (TypeError, ValueError):
                    continue
        candidates.append(
            {
                "symbol": symbol_str,
                "score": score_value,
                "components": components,
            }
        )
    return candidates


def select_symbol_from_trade_plan(trade_plan: Optional[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]], str]:
    candidates = extract_scoring_candidates(trade_plan)
    side = str((trade_plan or {}).get("trade_side", "long")).lower()
    if candidates:
        return candidates[0]["symbol"], candidates, side
    fallback_symbol = (trade_plan or {}).get("trade_symbol")
    if fallback_symbol:
        return str(fallback_symbol).upper(), candidates, side
    return None, candidates, side


def log_scoring_candidates(candidates: List[Dict[str, Any]]) -> None:
    if not candidates:
        return
    discord.print_log("Weighted score top candidates:")
    for idx, candidate in enumerate(candidates, 1):
        components = candidate.get("components") or {}
        component_parts: List[str] = []
        for label in ("full", "30d", "10d"):
            if label in components:
                component_parts.append(f"{label}={components[label]:.4f}")
        if not component_parts:
            component_parts.append("components=NA")
        discord.print_log(
            f"  {idx}. {candidate['symbol']} score={candidate['score']:.4f} ({', '.join(component_parts)})"
        )


async def fetch_last_price(symbol: str, product_type: str, mode: str) -> Optional[float]:
    return await fetch_last_price_bingx(symbol, product_type or 'SWAP', mode)


async def fetch_last_price_bingx(symbol: str, product_type: str, mode: str) -> Optional[float]:
    try:
        from bingx5_46_2api import get_bingx_orderbook
        bid, ask = get_bingx_orderbook(symbol)
        if bid is not None and ask is not None:
            return round((bid + ask) / 2.0, 6)
        elif bid is not None:
            return bid
        elif ask is not None:
            return ask
    except Exception as exc:
        discord.print_log(f"BingX 価格取得エラー ({symbol}): {exc}")
    return None



async def get_btc_performance(mode: str) -> Tuple[float, float]:
    return 1.0, 1.0


async def get_btc_lot_multiplier(mode: str, current_side: str) -> Tuple[float, str]:
    return 1.0, "NORMAL"


def wait_until_analysis_time(target_hour: int = 9, target_minute: int = 0) -> None:
    now_jst = datetime.now(JST)
    target = now_jst.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if now_jst >= target:
        discord.print_log(f"Past {target_hour}:{target_minute:02d}, running analysis immediately.")
        return
    wait_seconds = (target - now_jst).total_seconds()
    minutes = wait_seconds / 60
    discord.print_log(f"Waiting about {minutes:.1f} minutes until {target_hour}:{target_minute:02d}.")
    time.sleep(wait_seconds)


def get_whale_sentiment_info(symbol: str) -> Dict[str, Any]:
    """Data/whale_market_state.json より銘柄のクジラセンチメント情報 (signal, long_ratio, short_ratio) を取得"""
    whale_file = Path(__file__).resolve().parent / "Data" / "whale_market_state.json"
    default_res = {
        "signal": "NEUTRAL",
        "long_ratio": 0.5,
        "short_ratio": 0.5,
        "whales_holding": 0,
        "net_val_usd": 0.0,
        "net_val_formatted": "$0"
    }
    if not whale_file.exists():
        return default_res
    try:
        with open(whale_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            coins_sentiment = data.get("coins_sentiment", {})

            # 1. 完全一致チェック
            if symbol in coins_sentiment:
                return coins_sentiment[symbol]

            # 2. 通貨コード正規化 (-USDT, -USDC, _, / を除去)
            clean_coin = str(symbol).upper().split("-")[0].split("_")[0].split("/")[0]
            clean_coin = clean_coin.replace("USDT", "").replace("USDC", "")
            if clean_coin in coins_sentiment:
                return coins_sentiment[clean_coin]

            # 3. 1000倍プレフィックス銘柄対応 (1000SHIB -> kSHIB, 1000PEPE -> kPEPE, etc.)
            if clean_coin.startswith("1000"):
                k_coin = "k" + clean_coin[4:]
                if k_coin in coins_sentiment:
                    return coins_sentiment[k_coin]

            # 4. BTC自身のリクエストの場合はBTCを返す
            if clean_coin == "BTC" and "BTC" in coins_sentiment:
                return coins_sentiment["BTC"]

            # 5. 個別クジラデータが存在しないアルト銘柄は「中立 (NEUTRAL)」として扱う（BTC誤フォールバック防止）
            return default_res
    except Exception as e:
        discord.print_log(f"[Whale Sentiment Error] 読み込み失敗: {e}")
    return default_res


def load_top_symbol_from_scores(trade_side: str = "long") -> Optional[str]:
    csv_candidates = [
        SCORES_CSV_PATH,
        MIX_SCRIPT_PATH.parent / "Data" / "symbol_selection_scores.csv",
        MIX_SCRIPT_PATH.parent / "Data" / "symbol_selection_scores_scores.csv"
    ]
    df = None
    for path in csv_candidates:
        if path.exists():
            try:
                df = pd.read_csv(path)
                break
            except Exception:
                continue

    if df is None or df.empty:
        return "ETH-USDT"

    score_col = "score" if "score" in df.columns else None
    if not score_col:
        return "ETH-USDT"

    # BTC をエントリー対象から除外
    clean_syms = df["symbol"].astype(str).str.upper().str.replace("-", "", regex=False).str.replace("_", "", regex=False).str.replace("USDT", "", regex=False).str.replace("USDC", "", regex=False)
    df = df[clean_syms != "BTC"]
    if df.empty:
        return "ETH-USDT"

    ascending_flag = (trade_side == "short")
    df_sorted = df.sort_values(score_col, ascending=ascending_flag).reset_index(drop=True)
    
    if not df_sorted.empty and "symbol" in df_sorted.columns:
        return normalize_symbol(str(df_sorted.iloc[0]["symbol"]))

    return "ETH-USDT"


def select_candidates_from_scores(trade_side: str = "long", limit: int = 15) -> List[str]:
    try:
        df = pd.read_csv(SCORES_CSV_PATH)
    except Exception:
        return ["ETH-USDT", "SOL-USDT", "DOGE-USDT"]

    if df.empty or "symbol" not in df.columns:
        return ["ETH-USDT", "SOL-USDT", "DOGE-USDT"]

    # BTC をエントリー対象から完全除外
    clean_syms = df["symbol"].astype(str).str.upper().str.replace("-", "", regex=False).str.replace("_", "", regex=False).str.replace("USDT", "", regex=False).str.replace("USDC", "", regex=False)
    df = df[clean_syms != "BTC"]
    if df.empty:
        return ["ETH-USDT", "SOL-USDT", "DOGE-USDT"]

    if "priority_rank" in df.columns:
        df_sorted = df.sort_values("priority_rank", ascending=True)
    else:
        score_col = "score" if "score" in df.columns else "score"
        ascending_flag = (trade_side == "short")
        df_sorted = df.sort_values(score_col, ascending=ascending_flag)

    targets = []
    for _, row in df_sorted.iterrows():
        sym = str(row.get("symbol", "")).upper()
        if not sym or sym == "BTC":
            continue
        if trade_side == "long" and row.get("ban_long", 0) == 1:
            continue
        if trade_side == "short" and row.get("ban_short", 0) == 1:
            continue
        targets.append(sym)
        if limit > 0 and len(targets) >= limit:
            break

    return targets if targets else ["HYPE", "SOL", "ETH"]


def get_latest_analysis_slot_dt(now_dt: datetime, hours: List[int]) -> datetime:
    """直近の選定スロットの datetime を取得する"""
    sorted_hours = sorted(hours)
    for h in reversed(sorted_hours):
        slot_cand = now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        if now_dt >= slot_cand:
            return slot_cand
    prev_day = now_dt - timedelta(days=1)
    return prev_day.replace(hour=sorted_hours[-1], minute=0, second=0, microsecond=0)


def check_skip_mix_analysis() -> bool:
    if "--force" in sys.argv or "--force-mix" in sys.argv or "--force-analysis" in sys.argv:
        return False
    if not SCORES_CSV_PATH.exists():
        return False
    mtime = SCORES_CSV_PATH.stat().st_mtime
    mtime_dt = datetime.fromtimestamp(mtime, tz=JST)
    now_dt = datetime.now(JST)
    latest_slot_dt = get_latest_analysis_slot_dt(now_dt, ANALYSIS_HOURS)
    return mtime_dt >= latest_slot_dt


def run_mix_analysis(mode: str) -> Optional[Dict[str, Any]]:
    global SKIP_MIX_ANALYSIS
    SKIP_MIX_ANALYSIS = check_skip_mix_analysis()

    market_state_file = MIX_SCRIPT_PATH.parent / "Data" / "market_state.json"
    trade_side = "long"
    if market_state_file.exists():
        try:
            with open(market_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                state = data.get("market_state", "long_only")
                if state == "short_only":
                    trade_side = "short"
        except Exception as e:
            pass

    if SKIP_MIX_ANALYSIS:
        discord.print_log("SKIP_MIX_ANALYSIS=True: 既存銘柄スコアを使用します。")
        top_symbol = load_top_symbol_from_scores(trade_side=trade_side)
        return {"trade_symbol": top_symbol, "trade_side": trade_side}

    if not MIX_SCRIPT_PATH.exists():
        top_symbol = load_top_symbol_from_scores(trade_side=trade_side)
        return {"trade_symbol": top_symbol, "trade_side": trade_side}

    cmd = [sys.executable, str(MIX_SCRIPT_PATH)]
    cmd.extend(["--demo-mode", "demo" if mode == "demo" else "live"])
    try:
        discord.print_log("銘柄選定分析スクリプトを実行します。")
        subprocess.run(cmd, check=True)
    except Exception as exc:
        discord.print_log(f"銘柄選定分析実行エラー: {exc}")

    top_symbol = load_top_symbol_from_scores(trade_side=trade_side)
    return {"trade_symbol": top_symbol, "trade_side": trade_side}


TRADED_SYMBOLS_PATH = MIX_SCRIPT_PATH.parent / "traded_symbols.json"
TRADED_SYMBOL_RETENTION_DAYS = 30


def load_traded_symbols() -> List[str]:
    try:
        with open(TRADED_SYMBOLS_PATH, "r") as fp:
            data = json.load(fp)
            if isinstance(data, list):
                return [d.get("symbol") for d in data if isinstance(d, dict) and d.get("symbol")]
    except Exception:
        pass
    return []


async def fetch_all_asset_contexts(mode: str = 'demo') -> Dict[str, Dict[str, Any]]:
    """BingX Swap API から全銘柄のリアルタイム FR / 価格 / 出来高を取得"""
    res: Dict[str, Dict[str, Any]] = {}
    cred_key = 'bingx_demo' if mode in ('paper', 'demo', 'testnet') else 'bingx'
    base_url = RestAPI_url.get(cred_key, 'https://open-api-vst.bingx.com' if mode in ('paper', 'demo', 'testnet') else 'https://open-api.bingx.com')
    try:
        # 1. premiumIndex から fundingRate と markPrice を取得
        url_fr = f"{base_url}/openApi/swap/v2/quote/premiumIndex"
        resp_fr = requests.get(url_fr, timeout=10)
        if resp_fr.status_code == 200:
            data_fr = resp_fr.json()
            for item in data_fr.get("data", []):
                sym = normalize_symbol(item.get("symbol", ""))
                coin = sym.replace("-USDT", "")
                info = {
                    "funding": float(item.get("lastFundingRate") or 0.0),
                    "openInterest": 0.0,
                    "dayNtlVlm": 0.0,
                    "markPx": float(item.get("markPrice") or 0.0),
                    "prevDayPx": 0.0,
                }
                res[sym] = info
                res[coin] = info

        # 2. ticker から quoteVolume と openPrice を取得
        url_ticker = f"{base_url}/openApi/swap/v2/quote/ticker"
        resp_ticker = requests.get(url_ticker, timeout=10)
        if resp_ticker.status_code == 200:
            data_ticker = resp_ticker.json()
            for item in data_ticker.get("data", []):
                sym = normalize_symbol(item.get("symbol", ""))
                coin = sym.replace("-USDT", "")
                vol = float(item.get("quoteVolume") or 0.0)
                open_px = float(item.get("openPrice") or 0.0)
                last_px = float(item.get("lastPrice") or 0.0)

                for k in (sym, coin):
                    if k not in res:
                        res[k] = {
                            "funding": 0.0,
                            "openInterest": 0.0,
                            "dayNtlVlm": vol,
                            "markPx": last_px,
                            "prevDayPx": open_px,
                        }
                    else:
                        res[k]["dayNtlVlm"] = vol
                        res[k]["prevDayPx"] = open_px
                        if res[k]["markPx"] == 0.0:
                            res[k]["markPx"] = last_px
    except Exception as e:
        print(f"[Warning] fetch_all_asset_contexts (BingX) error: {e}")
    return res


def remember_symbols(symbols: List[str]) -> List[str]:
    return symbols


def build_pnl_symbol_pool(trade_plan: Optional[Dict[str, Any]], current_symbol: str) -> List[str]:
    candidate_syms = [c["symbol"] for c in extract_scoring_candidates(trade_plan)][:20]
    history = load_traded_symbols()
    merged = list(dict.fromkeys([current_symbol] + candidate_syms + history))
    return merged


def compute_lot_size(
    price: Optional[float],
    target_position_value_usdt: float,
    spec: Dict[str, float],
) -> float:
    sz_decimals = int(spec.get("sz_decimals", 4.0))
    min_qty = spec.get("min_qty", 10**(-sz_decimals))
    if price is None or price <= 0:
        return min_qty
    base_qty = target_position_value_usdt / price
    return round(base_qty, sz_decimals)


def get_bingx_granularity(bybit_interval: str) -> str:
    mapping = {'1': '1m', '3': '3m', '5': '5m', '15': '15m', '30': '30m', '60': '1h', '120': '2h', '240': '4h', 'D': '1d'}
    return mapping.get(str(bybit_interval), '1h')


async def fetch_bingx_candles(symbol: str, granularity: str, limit: int = 300, mode: str = 'demo') -> pd.DataFrame:
    from bingx5_46_4mix_candle_Merged_Alt10 import fetch_bingx_ohlcv
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (limit * 3600 * 1000)
    try:
        rows = await fetch_bingx_ohlcv(symbol, "SWAP", granularity.upper(), start_ms, now_ms, mode=mode)
        if rows:
            df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'quote_volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            df = df.sort_values('timestamp').reset_index(drop=True)

            # --- 確定足（クローズ完了足）のみを厳格に抽出 ---
            # 最新行が現在進行形で形成中の未確定足である場合を除外し、前足確定バーのみを返す
            if not df.empty:
                granularity_sec_map = {"1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400}
                gran_sec = granularity_sec_map.get(granularity.upper(), 3600)
                now_utc = datetime.now(timezone.utc)
                last_ts = pd.to_datetime(df['timestamp'].iloc[-1], utc=True)
                candle_close_time = last_ts + timedelta(seconds=gran_sec)
                # 現在時刻が足のクローズ予定時刻に達していなければ未確定足なので除外
                if candle_close_time > now_utc:
                    df = df.iloc[:-1].reset_index(drop=True)

            return df
    except Exception as e:
        print(f"fetch_bingx_candles error: {e}")
    return pd.DataFrame()


async def generate_bingx_backtest_chart(
    symbol: str, interval: str, df_loop: pd.DataFrame, best_mp: int, best_margin: float, best_er: float, trade_side: str, api, current_bg_target_value: float, bybit_exec_history=None, bybit_lot_size=None,
    strategy_type: str = "range", best_interval: int = 60
) -> None:
    try:
        if df_loop is not None and not df_loop.empty and len(df_loop) >= 30:
            from bingx5_46_3logic import generate_backtest_chart_with_trades
            img_path = generate_backtest_chart_with_trades(
                symbol=symbol,
                interval=interval,
                df=df_loop,
                best_mp=best_mp,
                best_margin=best_margin,
                best_er=best_er,
                trade_side=trade_side,
                api=api,
                current_bg_target_value=current_bg_target_value,
                bybit_exec_history=bybit_exec_history,
                bybit_lot_size=bybit_lot_size,
                strategy_type=strategy_type,
                best_interval=best_interval,
            )
            if img_path and os.path.exists(img_path):
                discord.send_file(img_path, f"【{symbol} 最適化バックテスト結果】 PnL & トレード履歴チャート ({best_interval}m)")
    except Exception as e:
        print(f"[Chart Error] {symbol} チャート生成失敗: {e}")


async def load_local_or_api_candles(symbol: str, limit: int = 300) -> pd.DataFrame:
    """
    4mix処理で既に生成済みのローカルCSV (Data/merged_{symbol}.csv) を最優先で読み込み、
    存在しない場合のみ API から取得する。
    標準OHLCV列および FR / OI 列を安全に抽出して返す。
    """
    clean_sym = symbol.replace("USDT", "").replace("USDC", "").upper()
    data_dir = Path(__file__).resolve().parent / "Data"
    cand_csv = data_dir / f"merged_{clean_sym}.csv"
    
    target_cols = ["timestamp", "open", "high", "low", "close", "volume", "fundingRate", "openInterest", "funding", "oi"]

    if cand_csv.exists():
        try:
            df = pd.read_csv(cand_csv)
            if not df.empty and "close" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                available_cols = [c for c in target_cols if c in df.columns]
                df = df[available_cols].copy()
                if limit and len(df) > limit:
                    df = df.tail(limit).reset_index(drop=True)
                return df
        except Exception:
            pass

    all_merged_csv = data_dir / "historical_all_symbols_merged.csv"
    if all_merged_csv.exists():
        try:
            df_all = pd.read_csv(all_merged_csv)
            if "symbol" in df_all.columns:
                df_sym = df_all[df_all["symbol"].str.upper() == clean_sym].copy()
                if not df_sym.empty and "close" in df_sym.columns:
                    df_sym["timestamp"] = pd.to_datetime(df_sym["timestamp"])
                    df_sym = df_sym.sort_values("timestamp").reset_index(drop=True)
                    available_cols = [c for c in target_cols if c in df_sym.columns]
                    df_sym = df_sym[available_cols].copy()
                    if limit and len(df_sym) > limit:
                        df_sym = df_sym.tail(limit).reset_index(drop=True)
                    return df_sym
        except Exception:
            pass

    from bingx5_46_2api import fetch_bingx_candles
    return await fetch_bingx_candles(symbol, "1h", limit=limit)


async def validate_profitable_candidates(trade_side: str, mode: str, base_symbol: str, interval: str = "60") -> Tuple[List[str], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """
    クジラ優先上位候補銘柄に対して個別最適化（MTF/MP/ER比較バックテスト）を実行し、
    各銘柄自身の最適パラメータでプラス成績（PnL > 100 USDT かつ 取引数 > 0）となる上位3銘柄を選定。
    銘柄ごとの個別最適化パラメータ辞書 (symbol_params_map) を生成・返却する。
    """
    from bingx5_46_2api import api_bingx
    from bingx5_46_3logic import run_interval_comparison, backtester, logicinstance, resample_candles

    discord.print_log("候補銘柄（クジラ優先上位）の個別パラメータ最適化＆バックテスト検証を開始します...")
    all_candidates_info = await select_top_bingx_symbols(top_n=15, mode=mode)
    all_candidates = [info["symbol"] for info in all_candidates_info]
    
    profitable_cands = []
    symbol_params_map: Dict[str, Dict[str, Any]] = {}
    default_best_params: Dict[str, Any] = {
        "strategy": "breakout",
        "interval": 60,
        "mp": 48,
        "er": 0.1,
        "margin": 8.0,
    }

    for cand_idx, cand in enumerate(all_candidates):
        if cand.upper() == "BTC" or cand.upper().replace("-USDT", "").replace("USDT", "") == "BTC":
            continue

        # クジラ判定で売り優勢（SHORT_ONLY）の銘柄はロング専用戦略の選定対象から除外
        cand_whale_info = get_whale_sentiment_info(cand)
        cand_whale_sig = cand_whale_info.get("signal", "NEUTRAL")
        if cand_whale_sig == "SHORT_ONLY":
            discord.print_log(f"   [スキップ] {cand}: クジラ判定が売り優勢 ({cand_whale_sig}) のため選定候補から除外。")
            continue

        if len(profitable_cands) >= MAX_SELECTED_SYMBOLS:
            break
            
        cand_df = await load_local_or_api_candles(cand, limit=300)
        
        # 爆上げモメンタム候補（prefer_breakout）の判定:
        # 1. 前兆スコア上位3銘柄 (cand_idx < 3)
        # 2. または出来高急増（直近出来高が24h平均の2.0倍以上）かつクジラ買い優勢 (cand_whale_sig == "LONG_ONLY")
        is_momentum_candidate = (cand_idx < 3)
        if not is_momentum_candidate and cand_df is not None and len(cand_df) >= 24:
            vol_mean_24h = float(cand_df["volume"].tail(24).mean())
            curr_vol = float(cand_df["volume"].iloc[-1])
            if vol_mean_24h > 0 and (curr_vol / vol_mean_24h >= 2.0) and cand_whale_sig == "LONG_ONLY":
                is_momentum_candidate = True

        momentum_tag = " 🚀[爆上げモメンタム候補: BREAKOUT優先]" if is_momentum_candidate else ""
        discord.print_log(f"個別最適化中: {cand} (Rank #{cand_idx+1}{momentum_tag}) ...")
        
        if cand_df is not None and not cand_df.empty and len(cand_df) > 30:
            # 下落トレンド除外チェック (LONG ONLY 厳格安全フィルター)
            curr_c = float(cand_df["close"].iloc[-1])
            ret_24h = (curr_c - float(cand_df["close"].iloc[-24])) / float(cand_df["close"].iloc[-24]) if len(cand_df) >= 24 else 0.0
            ret_72h = (curr_c - float(cand_df["close"].iloc[-72])) / float(cand_df["close"].iloc[-72]) if len(cand_df) >= 72 else ret_24h
            if ret_72h < -0.07 or ret_24h < -0.05:
                discord.print_log(f"   [スキップ] {cand}: 直近下落トレンド (24h: {ret_24h*100:+.1f}%, 3d: {ret_72h*100:+.1f}%) のため選定候補から除外。")
                continue

            # 30d / 10d / 5d ノーマライズ前半高値下落型チェック
            is_peak_first_half = False
            for w_name, w_h in [("5d", 120), ("10d", 240), ("30d", 720)]:
                if len(cand_df) >= 24:
                    sub_c = cand_df["close"].tail(w_h).astype(float)
                    if len(sub_c) >= 24 and sub_c.iloc[0] > 0:
                        norm_c = sub_c / sub_c.iloc[0]
                        p_idx = int(norm_c.argmax())
                        p_ratio = p_idx / len(norm_c)
                        f_norm = float(norm_c.iloc[-1])
                        p_val = float(norm_c.max())
                        if p_ratio < 0.50 and (f_norm < p_val * 0.90) and (f_norm < 1.0 or f_norm < p_val * 0.80):
                            is_peak_first_half = True
                            discord.print_log(f"   [スキップ] {cand}: {w_name}ノーマライズ前半高値下落型 (peak={p_ratio:.2f}, {p_val:.2f}x->{f_norm:.2f}x) のため除外。")
                            break
            if is_peak_first_half:
                continue

            try:
                results, cand_strat, cand_inv, cand_mp, cand_er, cand_margin = run_interval_comparison(
                    df_60m=cand_df, lot=1.0, data_equity=100.0, side_mode=trade_side, symbol=cand,
                    prefer_breakout=is_momentum_candidate
                )
                
                # 有効な取引がある最良結果を抽出
                active_results = {k: v for k, v in results.items() if v.get('trade_count', 0) > 0}
                if active_results:
                    best_key = max(active_results.keys(), key=lambda x: active_results[x]['final_pnl'])
                    best_res = active_results[best_key]
                    cand_pnl = float(best_res.get('final_pnl', 0.0))
                    trade_cnt = int(best_res.get('trade_count', 0))
                    win_rt = float(best_res.get('win_rate', 0.0))
                    max_dd = float(best_res.get('DD_max', 0.0))
                else:
                    cand_pnl = 0.0
                    trade_cnt = 0
                    win_rt = 0.0
                    max_dd = 0.0

                cand_params = {
                    "strategy": cand_strat,
                    "interval": cand_inv,
                    "mp": cand_mp,
                    "er": cand_er,
                    "margin": cand_margin,
                    "pnl": cand_pnl,
                    "trade_count": trade_cnt,
                    "win_rate": win_rt,
                    "max_dd": max_dd,
                }
                symbol_params_map[cand] = cand_params

                # 基準残高100ドルに対してプラス収支かつ取引実績あり
                if cand_pnl > 100.0 and trade_cnt > 0:
                    profit_val = cand_pnl - 100.0
                    discord.print_log(
                        f"   [合格] {cand}: 戦略={cand_strat.upper()} {trade_side.upper()} "
                        f"(MP={cand_mp}, ER={cand_er}, Margin={cand_margin}%, {cand_inv}m) -> "
                        f"純利益: +${profit_val:.2f} (取引: {trade_cnt}回, 勝率: {win_rt:.1f}%)"
                    )
                    profitable_cands.append(cand)
                    
                    # 合格銘柄の長期バックテストチャートを出力
                    try:
                        bt = backtester()
                        logic = logicinstance()
                        df_interval = resample_candles(cand_df, int(cand_inv))
                        df_logic = logic.make_logic(
                            df_interval,
                            market_profile_period=cand_mp,
                            er_threshold=cand_er,
                            strategy_type=cand_strat,
                        )
                        full_cand_bt = bt.run_backtest(
                            df=df_logic,
                            lot=1.0,
                            data_equity=100.0,
                            side_mode=trade_side,
                            mp_period=cand_mp,
                            atr_tp_multi=1.5,
                            er_threshold=cand_er,
                            strategy_type=cand_strat,
                            sl_margin_pct=cand_margin,
                        )
                        eval_bars_10d = min(len(full_cand_bt), max(30, 240 * 60 // int(cand_inv if cand_inv else 60)))
                        chart_10d_df = full_cand_bt.tail(eval_bars_10d).reset_index(drop=True)
                        bg_csv = f"backtest_data/klines100_{cand}_bingx.csv"
                        chart_10d_df.to_csv(bg_csv, index=False)
                        discord.plot_backtest(label=f"BingX_{cand}", csv_file=bg_csv, symbol=cand)
                    except Exception as ch_err:
                        print(f"Cand chart error {cand}: {ch_err}")
                else:
                    discord.print_log(f"   [不合格] {cand}: PnL = ${cand_pnl:.2f} (取引: {trade_cnt}回)")

            except Exception as opt_err:
                discord.print_log(f"   [最適化エラー] {cand}: {opt_err}")

        await asyncio.sleep(0.05)

    # 1位または合格リストから代表パラメータを設定
    if profitable_cands:
        first_sym = profitable_cands[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)
    elif symbol_params_map:
        first_sym = list(symbol_params_map.keys())[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)

    return profitable_cands, symbol_params_map, default_best_params


async def select_top_bingx_symbols(top_n: int = 10, mode: str = 'demo') -> List[Dict[str, Any]]:
    """Data/symbol_selection_scores.csv が存在しない場合は自動再計算し、スコアから上位銘柄を選定"""
    scores_file = Path(__file__).resolve().parent / "Data" / "symbol_selection_scores.csv"
    
    # スコアファイルが存在しない場合は、銘柄選定スクリプトを自動発注・再計算
    if not scores_file.exists():
        print("[Symbol Selection] symbol_selection_scores.csv が見つかりません。最新データを自動スクリーニング・生成します...")
        mix_script = Path(__file__).resolve().parent / "bingx5_46_4mix_candle_Merged_Alt10.py"
        if mix_script.exists():
            try:
                import subprocess
                subprocess.run([sys.executable, str(mix_script)], check=True)
            except Exception as sub_err:
                print(f"[Symbol Selection Error] 銘柄選定スクリプト実行エラー: {sub_err}")
            
    tickers = []
    if scores_file.exists():
        print(f"[Symbol Selection] {scores_file.name} からクジラ優先銘柄を選定中...")
        try:
            df_scores = pd.read_csv(scores_file)
            # priority_rank または score の降順でソート
            if "priority_rank" in df_scores.columns:
                df_scores = df_scores.sort_values("priority_rank", ascending=True)
            elif "score" in df_scores.columns:
                df_scores = df_scores.sort_values("score", ascending=False)
            tickers = df_scores.to_dict(orient="records")
        except Exception as e:
            print(f"[Symbol Selection Error] {e}")
            from bingx5_46_4mix_candle_Merged_Alt10 import fetch_bingx_tickers
            tickers = fetch_bingx_tickers()
    else:
        from bingx5_46_4mix_candle_Merged_Alt10 import fetch_bingx_tickers
        tickers = fetch_bingx_tickers()

    if not tickers:
        print("[Symbol Selection] 銘柄情報の取得に失敗。デフォルト銘柄を使用します。")
        return [{"symbol": "ETH-USDT", "lastPr": 2500.0, "usdtVolume": 1e8}, {"symbol": "SOL-USDT", "lastPr": 105.0, "usdtVolume": 1e8}]
    
    # BTCを除外して優先順序通りに抽出
    def _is_btc_sym(s: str) -> bool:
        clean = str(s).upper().replace("-", "").replace("_", "").replace("USDT", "").replace("USDC", "")
        return clean == "BTC"

    sorted_tickers = [t for t in tickers if not _is_btc_sym(t.get("symbol", ""))]

    # 取引所の universe に存在する銘柄のみにフィルタリング
    try:
        from bingx5_46_2api import get_bingx_universe_symbols
        valid_universe = get_bingx_universe_symbols(mode=mode)
        if valid_universe:
            filtered = [t for t in sorted_tickers if normalize_symbol(t.get("symbol", "")).upper() in valid_universe]
            if filtered:
                sorted_tickers = filtered
    except Exception as filter_err:
        print(f"[Symbol Selection Warning] Universe フィルタエラー: {filter_err}")

    return sorted_tickers[:top_n]


async def wait_until_next_hour():
    """毎時00分05秒まで待機する（前足確定の安全マージン5秒）"""
    now = datetime.now(JST)
    next_hour = now.replace(minute=0, second=5, microsecond=0) + timedelta(hours=1)
    wait_seconds = (next_hour - now).total_seconds()
    if wait_seconds > 0:
        discord.print_log(f"[Wait] 次の1時間足確定まで {wait_seconds/60:.1f} 分待機 (次回: {next_hour.strftime('%H:%M:%S')} JST)", level="debug")
        await asyncio.sleep(wait_seconds)


async def run_screening_and_optimization(mode: str, send_charts: bool = False) -> tuple:
    """フェーズA: 銘柄スクリーニング + パラメータ最適化 + 合格銘柄選抜（ロング専用）"""
    from bingx5_46_2api import api_bingx

    # 1. 銘柄スクリーニング実行
    print("\n[Phase A] 銘柄スクリーニング・パラメータ最適化を開始します (LONG ONLY)...")
    mix_script = Path(__file__).resolve().parent / "bingx5_46_4mix_candle_Merged_Alt10.py"
    if mix_script.exists():
        try:
            cmd = [sys.executable, str(mix_script)]
            if not send_charts:
                cmd.append("--no-chart-send")
            subprocess.run(cmd, check=True)
        except Exception as sub_err:
            print(f"[Screening Error] 銘柄スクリーニング実行エラー: {sub_err}")

    # 2. クジラセンチメント更新
    whale_script = Path(__file__).resolve().parent / "fetch_whale_sentiment.py"
    if whale_script.exists():
        try:
            subprocess.run([sys.executable, str(whale_script)], check=True)
        except Exception:
            pass

    # 3. 上位候補銘柄取得 (前兆スコア & クジラ優先順位付き)
    selected_symbols_info = await select_top_bingx_symbols(top_n=15, mode=mode)
    print(f"\n[Selection] Top {len(selected_symbols_info)} Selected Symbols (Precursor & Whale Prioritized):")
    for rank, info in enumerate(selected_symbols_info, 1):
        sym = info.get("symbol", "")
        last_pr = float(info.get("lastPr", 0))
        tier = info.get("tier", 2)
        tag = info.get("whale_tag", "⚪ クジラ中立")
        w_fmt = info.get("whale_net_val_fmt", "$0")
        score_val = float(info.get("score", 0.0))
        gk_r = float(info.get("gk_vol_rank", 0.5))
        fr_pct = float(info.get("fr_annual_pct", 0.0))
        r_pos = float(info.get("range_pos_24", 0.5))
        print(f"   #{rank:2d} [Tier {tier}] {sym:8s} | Px: ${last_pr:9.4f} | Score: {score_val:5.1f} | GK_Vol: {gk_r:.2f} | FR: {fr_pct:+6.1f}%/年 | Pos: {r_pos:.2f} | Whale: {w_fmt} ({tag})")

    # 4. 戦略モード（ロング専用に固定）
    trade_side = "long"
    btc_whale_info = get_whale_sentiment_info("BTC")
    btc_whale_sig = btc_whale_info.get("signal", "NEUTRAL")
    print(f"[Market State] Strategy Mode: LONG ONLY | BTC Whale Sentiment: {btc_whale_sig}")

    # 5. パラメータ最適化 & バックテスト合格銘柄選抜 (銘柄個別最適化: 最大10銘柄)
    base_sym = selected_symbols_info[0]["symbol"] if selected_symbols_info else "HYPE"
    profitable_cands, symbol_params_map, default_best_params = await validate_profitable_candidates(
        trade_side=trade_side, mode=mode, base_symbol=base_sym
    )

    # バックテスト合格銘柄 + クジラ適格上位で補完し、確実にMAX_SELECTED_SYMBOLS銘柄を選定
    selected_set = set(profitable_cands)
    for info in selected_symbols_info:
        sym_cand = info.get("symbol", "")
        if sym_cand.upper() == "BTC" or sym_cand in selected_set:
            continue
        cand_whale = get_whale_sentiment_info(sym_cand).get("signal", "NEUTRAL")
        if cand_whale != "SHORT_ONLY":
            profitable_cands.append(sym_cand)
            selected_set.add(sym_cand)
        if len(profitable_cands) >= MAX_SELECTED_SYMBOLS:
            break

    # それでもMAX_SELECTED_SYMBOLS銘柄に満たない場合はスコア順で補完
    if len(profitable_cands) < MAX_SELECTED_SYMBOLS:
        for info in selected_symbols_info:
            sym_cand = info.get("symbol", "")
            if sym_cand.upper() != "BTC" and sym_cand not in selected_set:
                profitable_cands.append(sym_cand)
                selected_set.add(sym_cand)
            if len(profitable_cands) >= MAX_SELECTED_SYMBOLS:
                break

    selected_symbols = profitable_cands[:MAX_SELECTED_SYMBOLS]
    print(f"\n[Selection Result] 選定{len(selected_symbols)}銘柄 (クジラ適格 & 前兆上位): {', '.join(selected_symbols)}")

    # 選定銘柄の個別最適化パラメータをログ表示
    discord.print_log("\n★ 【銘柄別 個別最適化パラメータ一覧】")
    for sym in selected_symbols:
        p = symbol_params_map.get(sym, default_best_params)
        pnl_val = p.get('pnl', 0.0)
        wr_val = p.get('win_rate', 0.0)
        tc_val = p.get('trade_count', 0)
        profit_str = f"+${(pnl_val - 100.0):.2f}" if pnl_val >= 100.0 else f"${pnl_val:.2f}"
        discord.print_log(
            f"   📌 [{sym}] 戦略={p['strategy'].upper()} {trade_side.upper()} | 確定足={p['interval']}m | "
            f"MP={p['mp']} | ER={p['er']} | Margin={p['margin']}% | "
            f"純利益: {profit_str} (取引: {tc_val}回, 勝率: {wr_val:.1f}%)"
        )

    global current_strategy_type
    current_strategy_type = default_best_params["strategy"]

    return trade_side, symbol_params_map, default_best_params, selected_symbols


async def audit_and_retain_positions(
    selected_symbols: List[str],
    symbol_apis: Dict[str, Any],
    symbol_params_map: Dict[str, Dict[str, Any]],
    best_params: Dict[str, Any],
    mode: str,
    old_apis: Optional[Dict[str, Any]] = None,
    old_params: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """
    BingX口座内の全PerpポジションをAPIから取得し、
    selected_symbols に含まれない残存ポジションがあっても強制成行決済せず、
    Graceful Exit（通常決済完了までの独立エグジット監視）として symbol_apis / symbol_params_map に登録・維持する。
    """
    from bingx5_46_2api import (
        fetch_all_position_symbols_bingx,
        fetch_instrument_spec_bingx,
        api_bingx_helper,
        api_bingx,
    )

    discord.print_log(f"[Account Audit] 口座全体の全ポジションをスキャン中... (新選定銘柄: {', '.join(selected_symbols)})")
    try:
        all_pos_symbols = await fetch_all_position_symbols_bingx(mode=mode)
    except Exception as scan_err:
        discord.print_log(f"[Account Audit Error] 全ポジション取得失敗: {scan_err}")
        return symbol_apis, symbol_params_map

    if not all_pos_symbols:
        discord.print_log("[Account Audit] 口座内に残存ポジションはありません。（全FLAT確認済み）")
        return symbol_apis, symbol_params_map

    discord.print_log(f"[Account Audit] 口座内検出ポジション銘柄: {', '.join(all_pos_symbols)}")
    selected_set = set(selected_symbols)

    for sym in all_pos_symbols:
        try:
            local_api = api_bingx_helper(sym, 'SWAP', 'USDT', mode)
            pos = await local_api.get_positions()
            buy_qty = float(pos.get("buy", 0.0))
            sell_qty = float(pos.get("sell", 0.0))
            pnl = float(pos.get("profit", 0.0))
            current_qty = buy_qty if buy_qty > 0 else sell_qty
            side = "LONG" if buy_qty > 0 else ("SHORT" if sell_qty > 0 else "FLAT")

            if current_qty <= 0:
                continue

            if sym in selected_set:
                discord.print_log(
                    f"💎 [Position Retained] 【{sym}】新監視銘柄リストに選定されたためポジションを継続保有します。"
                    f" (方向: {side}, 数量: {current_qty}, 含み損益: {pnl:+.2f} USDT)"
                )
            else:
                discord.print_log(
                    f"🛡️ [Graceful Exit] 【{sym}】新選定リスト外ですがポジション保有中です。"
                    f" (方向: {side}, 数量: {current_qty}, 損益: {pnl:+.2f} USDT)"
                    f" → 強制決済せず、通常のエグジット判定完了まで独立監視を継続します。"
                )
                # symbol_apis に登録（既存のインスタンスがあれば引き継ぎ、なければ新設）
                if sym not in symbol_apis:
                    if old_apis and sym in old_apis and old_apis[sym] is not None:
                        symbol_apis[sym] = old_apis[sym]
                    else:
                        api = api_bingx(symbol=sym, mode=mode)
                        spec = fetch_instrument_spec_bingx(sym, mode)
                        if spec:
                            api.update_instrument_spec(spec)
                        symbol_apis[sym] = api
                # symbol_params_map に登録（旧パラメータがあれば引き継ぎ、なければbest_params）
                if sym not in symbol_params_map:
                    if old_params and sym in old_params:
                        symbol_params_map[sym] = old_params[sym]
                    else:
                        symbol_params_map[sym] = best_params

        except Exception as audit_err:
            discord.print_log(f"[Account Audit Error] {sym}: {audit_err}")

    return symbol_apis, symbol_params_map

async def start(mode: str = 'demo', max_lot: float = 10.0, interval: str = '60'):
    from bingx5_46_2api import (
        api_bingx,
        fetch_instrument_spec_bingx,
        flatten_current_position_bingx,
    )
    from bingx5_46_3logic import PnLCalculator

    account_mode_str = "[LIVE Account] (本番口座)" if mode == "live" else "[DEMO Account] (デモ / テストネット口座)"
    air_mode_str = "[AIR MODE ON] (発注監視のみ / API注文送信なし)" if is_air else "[REAL ORDER ON] (実際にBingXへ注文送信)"

    hours_str = ", ".join([f"{h:02d}:00" for h in sorted(ANALYSIS_HOURS)])
    start_msg = (
        "```\n"
        " ____  _               __  __\n"
        "| __ )(_)_ __   __ _  \\ \\/ /\n"
        "|  _ \\| | '_ \\ / _` |  \\  / \n"
        "| |_) | | | | | (_| |  /  \\ \n"
        "|____/|_|_| |_|\\__, | /_/\\_\\\n"
        "               |___/        \n"
        "----------------------------\n"
        "  AUTO TRADING SYSTEM START \n"
        "----------------------------\n"
        "```\n"
        f"[BingX Auto Trading System (LONG ONLY)]\n"
        f"==================================================\n"
        f"  取引口座設定 : {account_mode_str}\n"
        f"  発注モード   : {air_mode_str}\n"
        f"  戦略方向     : LONG ONLY (上昇特化)\n"
        f"  ローソク足   : 1時間足 (1H)\n"
        f"  銘柄選定時刻 : 毎日 {hours_str} JST (8時間ごと)\n"
        f"=================================================="
    )
    discord.print_log(start_msg)

    # ========== フェーズA: 起動時スクリーニング & 最適化 (チャート付き) ==========
    trade_side, symbol_params_map, best_params, selected_symbols = await run_screening_and_optimization(mode, send_charts=True)

    # 各銘柄の api インスタンスを保持（トレーリングSL状態を維持するため）
    symbol_apis: Dict[str, Any] = {}
    for sym in selected_symbols:
        api = api_bingx(symbol=sym, mode=mode)
        spec = fetch_instrument_spec_bingx(sym, mode)
        if spec:
            api.update_instrument_spec(spec)
        symbol_apis[sym] = api

    # 起動時にも口座内のポジションを監査し、選定外でも保有中ならGraceful Exitとして引き継ぐ
    symbol_apis, symbol_params_map = await audit_and_retain_positions(
        selected_symbols, symbol_apis, symbol_params_map, best_params, mode
    )

    logic = logicinstance()
    last_screening_slot = (datetime.now(JST).date(), datetime.now(JST).hour)
    cycle_count = 0
    previous_hourly_oi: Dict[str, float] = {}

    discord.print_log(f"[Phase B] 1時間足エントリー/クローズループを開始します。新選定銘柄: {', '.join(selected_symbols)} (全監視: {', '.join(symbol_apis.keys())})")

    # ========== フェーズB: 1時間足ループ ==========
    while True:
        cycle_count += 1
        now_jst = datetime.now(JST)

        # 初回サイクルでも、正時直後（00分00秒〜00分30秒の確定直後安全枠）でない限り、
        # 確定足と完全に同期するため次の正時（毎時00分05秒）まで待機する
        if cycle_count == 1:
            if "--loop" in sys.argv and not (now_jst.minute == 0 and now_jst.second < 30):
                discord.print_log(f"[Sync] 初回起動時刻: {now_jst.strftime('%H:%M:%S')} JST。確定足と同期するため次の正時まで待機します。")
                await wait_until_next_hour()
                now_jst = datetime.now(JST)
        else:
            await wait_until_next_hour()
            now_jst = datetime.now(JST)

        # --loop フラグがない場合は1サイクルのみ実行
        if cycle_count > 1 and "--loop" not in sys.argv:
            print("\n==================================================")
            print("  [Success] 1サイクルの選定・最適化・トレード判定が完走しました。")
            print("==================================================")
            break

        # ========== フェーズC: 定期銘柄選定 & Graceful Exit 管理 & スロットリセット ==========
        today_date = now_jst.date()
        current_slot = (today_date, now_jst.hour)
        is_periodic_reset_time = (
            now_jst.hour in ANALYSIS_HOURS
            and now_jst.minute < 5
            and current_slot != last_screening_slot
        )
        if is_periodic_reset_time:
            discord.print_log(f"[Phase C] 定期銘柄選定時刻 ({now_jst.hour:02d}:00 JST) 到達。スクリーニング＆最適化を実行します。")

            # 1. 定期フルスクリーニング＆最適化（チャート送信付き）
            trade_side, new_symbol_params_map, best_params, selected_symbols = await run_screening_and_optimization(mode, send_charts=True)

            # 2. 旧インスタンスの引き継ぎと新選定銘柄の symbol_apis 構築
            old_apis = symbol_apis
            old_params = symbol_params_map
            symbol_apis = {}
            symbol_params_map = new_symbol_params_map

            for sym in selected_symbols:
                if sym in old_apis and old_apis[sym] is not None:
                    symbol_apis[sym] = old_apis[sym]
                else:
                    api = api_bingx(symbol=sym, mode=mode)
                    spec = fetch_instrument_spec_bingx(sym, mode)
                    if spec:
                        api.update_instrument_spec(spec)
                    symbol_apis[sym] = api

            # 3. 口座全体の全ポジションをスキャンし、選定外でも保有中の銘柄はGraceful Exitとして引き継ぐ（強制成行決済は廃止）
            symbol_apis, symbol_params_map = await audit_and_retain_positions(
                selected_symbols, symbol_apis, symbol_params_map, best_params, mode, old_apis=old_apis, old_params=old_params
            )

            # 4. ポジションのない旧選定銘柄のインスタンスをメモリ破棄
            for old_sym in list(old_apis.keys()):
                if old_sym not in symbol_apis:
                    old_apis[old_sym] = None
            gc.collect()

            last_screening_slot = current_slot
            logic = logicinstance()
            discord.print_log(f"[Periodic Reset Complete] 新しい選定銘柄 (LONG ONLY): {', '.join(selected_symbols)} | 現在の全監視対象: {', '.join(symbol_apis.keys())}")
            continue

        # ========== 毎時: クジラセンチメント更新のみ（パラメータ最適化は8時間ごと定期選定時のみ） ==========
        if cycle_count > 1:
            try:
                whale_script = Path(__file__).resolve().parent / "fetch_whale_sentiment.py"
                if whale_script.exists():
                    try:
                        subprocess.run([sys.executable, str(whale_script)], check=True, timeout=60)
                    except Exception:
                        pass

                # 保有中ポジションの確認
                held_symbols = set()
                for sym, api in symbol_apis.items():
                    pos = await api.get_positions()
                    if float(pos.get("buy", 0)) > 0:
                        held_symbols.add(sym)

                # 銘柄リストは日次最適化結果を維持（ポジション保有中の銘柄が消えないようにする）
                updated_symbols = list(held_symbols)
                for sym in selected_symbols:
                    if sym not in updated_symbols:
                        updated_symbols.append(sym)

                # symbol_apis の同期更新
                new_symbol_apis = {}
                for sym in updated_symbols:
                    if sym in symbol_apis:
                        new_symbol_apis[sym] = symbol_apis[sym]
                    else:
                        api = api_bingx(symbol=sym, mode=mode)
                        spec = fetch_instrument_spec_bingx(sym, mode)
                        if spec:
                            api.update_instrument_spec(spec)
                        new_symbol_apis[sym] = api
                symbol_apis = new_symbol_apis

            except Exception as rot_err:
                discord.print_log(f"[Hourly Update Error] クジラセンチメント更新エラー: {rot_err}")

        # ========== 各銘柄のトレード判定 (LONG ONLY) ==========
        discord.print_log(f"\n[Cycle #{cycle_count}] {now_jst.strftime('%Y-%m-%d %H:%M')} JST | 対象: {', '.join(symbol_apis.keys())}", level="debug")

        try:
            active_positions = {}
            valid_entry_candidates = []
            selected_set = set(selected_symbols)
            freed_symbols = []

            # 1. 全銘柄のリアルタイム資金調達率(FR)と建玉(OI)を一括取得
            all_asset_ctxs = await fetch_all_asset_contexts(mode=mode)

            # 口座総残高をサイクル開始時に1回取得
            first_api = next(iter(symbol_apis.values())) if symbol_apis else None
            cycle_balance = await first_api.get_account() if first_api else 1000.0
            hourly_summary_rows = []

            # 2. 全銘柄のポジション状況とシグナル判定を一括評価
            for sym, api in list(symbol_apis.items()):
                df = await fetch_bingx_candles(sym, "1h", limit=300, mode=mode)
                if df.empty or len(df) < 20:
                    discord.print_log(f"[{sym}] ローソク足データ不足 (rows={len(df)}). スキップ。")
                    continue

                # リアルタイム FR / OI の付与
                ctx = all_asset_ctxs.get(sym, {})
                funding_val = ctx.get("funding", 0.0)
                curr_oi_val = ctx.get("openInterest", 0.0)
                df["funding"] = funding_val
                df["openInterest"] = curr_oi_val

                # OI変化率の計算 (前時間比)
                prev_oi = previous_hourly_oi.get(sym, curr_oi_val)
                oi_delta_pct = ((curr_oi_val - prev_oi) / prev_oi * 100.0) if prev_oi > 0 else 0.0
                previous_hourly_oi[sym] = curr_oi_val

                # 各銘柄の個別最適化パラメータを取得
                sym_params = symbol_params_map.get(sym, best_params)
                cand_interval = int(sym_params.get("interval", 60))

                # 60分を超える時間足（120分、180分など）で個別最適化されている場合はリサンプリング
                if cand_interval > 60:
                    df = resample_candles(df, cand_interval)
                    if not df.empty:
                        last_ts = pd.to_datetime(df['timestamp'].iloc[-1], utc=True)
                        candle_close_time = last_ts + timedelta(minutes=cand_interval)
                        if candle_close_time > datetime.now(timezone.utc):
                            df = df.iloc[:-1].reset_index(drop=True)

                if df.empty or len(df) < 15:
                    discord.print_log(f"[{sym}] データ不足 (rows={len(df)}). スキップ。")
                    continue

                df = logic.make_logic(
                    df,
                    market_profile_period=sym_params["mp"],
                    er_threshold=sym_params["er"],
                    strategy_type=sym_params["strategy"],
                )

                required_cols = ["long", "VAH", "VAL", "POC"]
                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    discord.print_log(f"[{sym}] シグナルカラム不足: {missing}. スキップ。")
                    continue

                current_price = df["close"].iloc[-1]
                long_signal = bool(df["long"].iloc[-1])
                vah = df["VAH"].iloc[-1]
                val = df["VAL"].iloc[-1]
                poc = df["POC"].iloc[-1]
                vol_surge = float(df["vol_surge_ratio"].iloc[-1]) if "vol_surge_ratio" in df.columns else 1.0

                position = await api.get_positions()
                balance = cycle_balance
                has_long = float(position.get("buy", 0)) > 0

                spec = api.bingx.instrument_spec or {}
                lot_size = compute_lot_size(current_price, TARGET_POSITION_VALUE_USDT, spec)

                pos_str = "LONG" if has_long else "FLAT"
                sig_str = "LONG↑" if long_signal else "---"
                pnl_current = float(position.get("profit", 0.0)) if has_long else 0.0
                pnl_str = f" | 含み損益: {pnl_current:+.2f} USDT" if has_long else ""

                whale_info = get_whale_sentiment_info(sym)
                whale_sig = whale_info.get("signal", "NEUTRAL")

                strat_name = sym_params.get("strategy", "RANGE").upper()
                discord.print_log(
                    f"[{sym}] Price: ${current_price:,.4f} | Pos: {pos_str}{pnl_str} | Signal: {sig_str} ({strat_name}) | "
                    f"FR: {funding_val:+.6f} | VolSurge: {vol_surge:.1f}x | OIΔ: {oi_delta_pct:+.1f}% | "
                    f"VAH: ${vah:,.4f} | VAL: ${val:,.4f} | POC: ${poc:,.4f} | 口座残高: ${balance:.2f}",
                    level="debug"
                )

                whale_tag_short = "🟢買い" if whale_sig == "LONG_ONLY" else ("🔴売り" if whale_sig == "SHORT_ONLY" else "⚪中立")
                pos_tag_short = f"LONG({pnl_current:+.1f})" if has_long else "FLAT"
                sig_tag_short = "LONG↑" if long_signal else "---"
                hourly_summary_rows.append({
                    "sym": sym,
                    "price": current_price,
                    "pos": pos_tag_short,
                    "sig": sig_tag_short,
                    "whale": whale_tag_short,
                })

                if has_long:
                    active_positions[sym] = (has_long, df, position, current_price, pnl_current)
                else:
                    # ポジション未保有（FLAT）の場合
                    # 旧選定銘柄で既にFLATであれば、監視リストから解放
                    if sym not in selected_set:
                        discord.print_log(f"[{sym}] 旧選定銘柄ですがポジションがFLATのため、監視リストから解放します。")
                        freed_symbols.append(sym)
                        continue

                    # クジラ判定が SHORT_ONLY でなければロング許可
                    is_long_allowed = whale_sig in ("LONG_ONLY", "NEUTRAL")

                    if long_signal:
                        if is_long_allowed:
                            valid_entry_candidates.append({
                                "symbol": sym,
                                "side": "LONG",
                                "df": df,
                                "position": position,
                                "balance": balance,
                                "lot_size": lot_size,
                                "current_price": current_price,
                                "whale_sig": whale_sig
                            })
                        else:
                            discord.print_log(f"[{sym}] [FILTER] ロングシグナル検出も、クジラセンチメント ({whale_sig}) が売り優勢のため見送り。", level="debug")
                    else:
                        discord.print_log(f"[{sym}] [--] シグナルなし。エントリー見送り (クジラ判定: {whale_sig})。", level="debug")

                await asyncio.sleep(0.1)

            # スマホ1画面完結サマリーのDiscord送信 (16行程度・スクロール不要)
            if hourly_summary_rows:
                next_hour_str = (now_jst.replace(minute=0, second=5, microsecond=0) + timedelta(hours=1)).strftime('%H:%M:%S')
                summary_lines = [
                    f"⏱ [Cycle #{cycle_count}] {now_jst.strftime('%H:%M')} JST | 残高: ${cycle_balance:.2f}",
                    "───────────────────────────────────",
                    "銘柄     現在値    保有   シグナル  大口",
                    "───────────────────────────────────",
                ]
                for r in hourly_summary_rows:
                    px = r["price"]
                    px_str = f"${px:,.4f}" if px < 10 else (f"${px:,.2f}" if px < 1000 else f"${px:,.1f}")
                    summary_lines.append(f"{r['sym']:<6s} {px_str:>9s}  {r['pos']:<6s}  {r['sig']:^6s}  {r['whale']}")
                summary_lines.append("───────────────────────────────────")
                summary_lines.append(f"次回確定: {next_hour_str} JST")
                discord.print_log("```text\n" + "\n".join(summary_lines) + "\n```")

            # FLATになった旧選定銘柄のメモリ解放
            for fsym in freed_symbols:
                if fsym in symbol_apis:
                    del symbol_apis[fsym]
                if fsym in symbol_params_map:
                    del symbol_params_map[fsym]

            # 2. 既存ポジションの決済・トレーリングSL判定
            from real_trade_tracker import record_real_trade, plot_real_trading_performance, plot_exit_chart

            for sym, (has_long, df, position, current_price, pnl_current) in list(active_positions.items()):
                api = symbol_apis[sym]
                entry_px = float(position.get("buy_pos", 0)) or current_price
                sym_params = symbol_params_map.get(sym, best_params)
                whale_sig = get_whale_sentiment_info(sym).get("signal", "NEUTRAL")

                if has_long:
                    closed = await api.long_close(
                        df, position, commission=0.0,
                        sl_margin_pct=sym_params["margin"],
                        strategy_type=sym_params["strategy"],
                    )
                    if closed:
                        record_real_trade(sym, "LONG", "CLOSE", current_price, float(position.get("buy", 0)), pnl_current, "実運用決済")
                        discord.print_log(f"[{sym}] [CLOSE] 🟢 ロングポジション決済完了 (PnL: {pnl_current:+.2f} USDT)")
                        
                        # 1. 決済トレードチャート画像の生成 & 送信
                        exit_chart_file = plot_exit_chart(
                            symbol=sym, df=df, exit_price=current_price, entry_price=entry_px,
                            pnl=pnl_current, exit_reason="VP_Trailing/Exit_Rule", side="LONG", whale_signal=whale_sig
                        )
                        if exit_chart_file and exit_chart_file.exists():
                            discord.send_file(exit_chart_file, f"📊 【決済チャート】{sym} LONG 決済完了 (PnL: {pnl_current:+.2f} USDT)")

                        # 2. 累積 PnL パフォーマンスチャートの生成 & 送信
                        chart_file = plot_real_trading_performance()
                        if chart_file and chart_file.exists():
                            discord.send_file(chart_file, f"📈 【実運用実績】累積損益パフォーマンス更新 (PnL: {pnl_current:+.2f} USDT)")
                        del active_positions[sym]

                        # 旧選定銘柄の決済完了時は監視リストから解放
                        if sym not in selected_set:
                            discord.print_log(f"[{sym}] [Graceful Exit Completed] 旧選定銘柄のポジション決済が完了したため、監視リストから解放します。")
                            if sym in symbol_apis:
                                del symbol_apis[sym]
                            if sym in symbol_params_map:
                                del symbol_params_map[sym]
                    else:
                        pnl_icon = "🟢" if pnl_current >= 0 else "🔴"
                        discord.print_log(f"──────────────────────────────────────────────────────────")
                        discord.print_log(f"💰 【{sym} 現在の含み損益】: {pnl_current:+.2f} USDT {pnl_icon} (ロング継続保有中)")
                        discord.print_log(f"──────────────────────────────────────────────────────────")

            # 3. 新規エントリー実行 (最大同時保有ポジション数制限: MAX_ACTIVE_POSITIONS)
            current_pos_count = len(active_positions)
            if current_pos_count >= MAX_ACTIVE_POSITIONS:
                held_syms = ", ".join(active_positions.keys())
                discord.print_log(f"[ENTRY BLOCK] 現在最大ポジション枠({current_pos_count}/{MAX_ACTIVE_POSITIONS})保有中のため新規エントリーは見送ります。(保有中: {held_syms})")
            elif valid_entry_candidates:
                # 既にポジション保有中の銘柄はエントリー候補から除外
                candidates_to_enter = [c for c in valid_entry_candidates if c["symbol"] not in active_positions]

                if not candidates_to_enter:
                    discord.print_log(f"[ENTRY BLOCK] シグナル検出銘柄はすでに保有中のためエントリーをスキップします。")
                else:
                    sym_rank_map = {sym: i for i, sym in enumerate(selected_symbols)}
                    candidates_to_enter.sort(key=lambda x: sym_rank_map.get(x["symbol"], 999))

                    available_slots = MAX_ACTIVE_POSITIONS - current_pos_count
                    all_cand_syms = ", ".join([c["symbol"] for c in candidates_to_enter])
                    discord.print_log(
                        f"[CANDIDATES] エントリーシグナル検出: {all_cand_syms} (空き枠: {available_slots}/{MAX_ACTIVE_POSITIONS})"
                    )

                    for cand_idx, best_cand in enumerate(candidates_to_enter):
                        # 空き枠チェック（約定するたびに active_positions が増加）
                        if len(active_positions) >= MAX_ACTIVE_POSITIONS:
                            discord.print_log(
                                f"[ENTRY LIMIT] 最大ポジション枠({MAX_ACTIVE_POSITIONS})に達したため、残りの候補エントリーを終了します。"
                            )
                            break

                        best_sym = best_cand["symbol"]
                        api = symbol_apis[best_sym]
                        df = best_cand["df"]
                        position = best_cand["position"]
                        balance = best_cand["balance"]
                        lot_size = best_cand["lot_size"]
                        current_price = best_cand["current_price"]
                        whale_sig = best_cand["whale_sig"]

                        discord.print_log(f"[{best_sym}] [ENTRY>>] ロングエントリー試行 ({cand_idx+1}/{len(candidates_to_enter)}) (クジラ判定: {whale_sig})")
                        entered = await api.long_entry(df, position, balance, lot_size, max_lot)
                        if entered:
                            active_positions[best_sym] = (True, df, position, current_price, 0.0)
                            # 1. 実運用トレード履歴の記録
                            record_real_trade(best_sym, "LONG", "ENTRY", current_price, lot_size, 0.0, "実運用新規エントリー")
                            
                            # 2. 指標値・クジラ情報の取得
                            last_row = df.iloc[-1] if not df.empty else None
                            vah_val = float(last_row.get("VAH", 0.0)) if last_row is not None and "VAH" in last_row else 0.0
                            val_val = float(last_row.get("VAL", 0.0)) if last_row is not None and "VAL" in last_row else 0.0
                            poc_val = float(last_row.get("POC", 0.0)) if last_row is not None and "POC" in last_row else 0.0
                            
                            entry_notional = current_price * lot_size
                            best_sym_params = symbol_params_map.get(best_sym, best_params)
                            strat_desc = f"{best_sym_params.get('strategy', 'BREAKOUT').upper()} (MP={best_sym_params.get('mp', '-')}, ER={best_sym_params.get('er', '-')})"
                            whale_info = get_whale_sentiment_info(best_sym)
                            whale_flow_val = float(whale_info.get("net_flow", 0.0)) if whale_info else 0.0
                            
                            # 3. Discord バッファをフラッシュ
                            discord.flush_all()
                            
                            # 4. エントリーチャート画像の生成 & Discord送信
                            from real_trade_tracker import plot_entry_chart
                            entry_chart_file = plot_entry_chart(
                                symbol=best_sym,
                                df=df,
                                entry_price=current_price,
                                vah=vah_val,
                                val=val_val,
                                poc=poc_val,
                                strategy_name=strat_desc,
                                whale_signal=whale_sig,
                                whale_flow=whale_flow_val
                            )
                            
                            entry_msg = (
                                f"🚀🚀🚀 **【新規ロングエントリー約定】** 🚀🚀🚀\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📌 **銘柄 / 方向**: `{best_sym}` (LONG 🟢)\n"
                                f"💰 **約定価格**: `${current_price:.6f}`\n"
                                f"📦 **発注数量**: `{lot_size:,.2f} {best_sym}` (約 `${entry_notional:,.2f} USDT`)\n"
                                f"⚙️ **適用戦略**: `{strat_desc}`\n"
                                f"🐋 **クジラ判定**: `{whale_sig}` (流入: `${whale_flow_val:+,.0f}`)\n"
                                f"📐 **Volume Profile 指標**:\n"
                                f"  - **VAH (上値抵抗)**: `${vah_val:.6f}`\n"
                                f"  - **POC (中心値)**  : `${poc_val:.6f}`\n"
                                f"  - **VAL (初期SL)** : `${val_val:.6f}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            )
                            
                            if entry_chart_file and Path(entry_chart_file).exists():
                                discord.send_file(entry_chart_file, description=entry_msg)
                            else:
                                discord.print_log(entry_msg)
                        else:
                            discord.print_log(f"[{best_sym}] [FALLTHROUGH] 約定しなかったため見送ります。次の候補銘柄へ移行します。")



            # PnL 損益グラフ更新 (初回のみ)
            if cycle_count == 1:
                try:
                    from bingx5_46_2api import apis
                    pnl_calc = PnLCalculator(apis_config=apis, mode=mode)
                    df_pnl = await pnl_calc.get_bingx_trade_history(apis)
                    if isinstance(df_pnl, list) and df_pnl:
                        pnl_calc.calculate_bingx_pnl_from_df(pd.DataFrame(df_pnl))
                except Exception as pnl_err:
                    discord.print_log(f"[PnL Warning]: {pnl_err}")

        except Exception as exc:
            discord.print_log(f"[Cycle #{cycle_count}] Exception (Auto Recovery - Retrying in 60s): {exc}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(60)
            continue

        await asyncio.sleep(1)


if __name__ == "__main__":
    mode = 'live' if BINGX_IS_LIVE else 'demo'
    max_lot = 10.0
    interval = '60'
    try:
        asyncio.run(start(mode, max_lot, interval))
    except KeyboardInterrupt:
        print("\nProgram stopped by user.")

