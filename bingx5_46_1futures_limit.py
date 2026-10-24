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
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple
from candle_5m_store import (
    load_5m_candles,
    sync_symbol_5m_candles,
    resample_from_5m,
    get_clean_symbol,
    normalize_symbol_name,
)


def _display_width(text: str) -> int:
    w = 0
    for ch in str(text):
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ('F', 'W', 'A') else 1
    return w


def _pad_display(text: str, width: int, align: str = 'left') -> str:
    text_str = str(text)
    dw = _display_width(text_str)
    pad = max(0, width - dw)
    if align == 'right':
        return ' ' * pad + text_str
    elif align == 'center':
        left = pad // 2
        right = pad - left
        return ' ' * left + text_str + ' ' * right
    else:
        return text_str + ' ' * pad


# Automatic Dual Logging: Output to Terminal, Data/bot_output.log, and Data/logs/bot_YYYY-MM-DD.log
class TeeLogger:
    def __init__(self, filepath: Path, original_stdout, max_size_mb: int = 15):
        self.terminal = original_stdout
        self.filepath = filepath
        self.daily_log_dir = filepath.parent / "logs"
        self.daily_log_dir.mkdir(parents=True, exist_ok=True)
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
            # 日次ログアーカイブ (Data/logs/bot_YYYY-MM-DD.log) への同時追記
            from datetime import datetime, timezone, timedelta
            jst_date = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            daily_file = self.daily_log_dir / f"bot_{jst_date}.log"
            with open(daily_file, "a", encoding="utf-8") as df_log:
                df_log.write(clean_msg)
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
#       本番口座(Live)への誤発注防止のため、本番トレードは完全安全ロック
ALLOW_LIVE_TRADING = False  # 本番リアル口座への発注APIを物理的に完全遮断

# [ 1 ] BingX 口座指定
#       True  = 本番口座 (Live Account)
#       False = デモ口座 (Demo Account / VST: Virtual Simulation Trading)
BINGX_IS_LIVE = False

# [ 2 ] BingX 注文実行・APIキー指定 (AIR Mode)
#       False = リアル注文 (BingX VST デモ取引所へ実際に発注・約定・管理)
#       True  = AIRモード (--air または --mock 引数指定時のみ仮想シミュレーション)
BINGX_IS_AIR = ("--air" in sys.argv or "--mock" in sys.argv)

# [ 3 ] ポジション・ロット設定 (PROJECT_RULES.md 準拠)
BINGX_TARGET_POSITION_VALUE_USDT = 15.0  # 目標投資額 $15.0 USDT (1枠基準)
TARGET_POSITION_VALUE_USDT = BINGX_TARGET_POSITION_VALUE_USDT
LEVERAGE_FACTOR = 10.0
MAX_ACTIVE_POSITIONS: int = 2  # 最大同時保有ポジション数 (資金効率と分散を最適化)
MAX_SELECTED_SYMBOLS: int = 20  # 最大選定・監視銘柄数 (Hyperliquid完全統一: バックテスト合格全銘柄を同時監視)

# [ 4 ] ナンピン数設定 (初期1回 + ナンピン最大2回 = 最大3枠厳守)
MAX_TRADES_COUNT: int = 3

# [ 5 ] 固定選定銘柄 (主要アルトコイン20銘柄 / Hyperliquid hype5_45 完全統一)
FIXED_SYMBOLS: List[str] = [
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "APT-USDT", "NEAR-USDT", "ARB-USDT",
    "UNI-USDT", "OP-USDT", "TIA-USDT", "RENDER-USDT", "INJ-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]

# [ 6 ] 定期銘柄選定・リセット時刻（JST時間: 0〜23時）
#       デフォルト: [1, 9, 17] (01:00, 09:00, 17:00 JST / 8時間ごと・主要セッション＆FR節目)。
#       CLI引数 (--analysis-hours / --reset-hours / --reset-hour) での上書き指定も可能。
ANALYSIS_HOURS: List[int] = [1, 9, 17]
DAILY_ANALYSIS_MINUTE = 0

# コマンドライン引数からのナンピン数オーバーライド
for _idx, _arg in enumerate(sys.argv):
    if _arg in ("--max-trades", "--pyramiding", "--nanpin") and _idx + 1 < len(sys.argv):
        try:
            MAX_TRADES_COUNT = max(1, min(10, int(sys.argv[_idx + 1].strip())))
        except ValueError:
            pass
    elif _arg.startswith("--max-trades=") or _arg.startswith("--pyramiding=") or _arg.startswith("--nanpin="):
        try:
            MAX_TRADES_COUNT = max(1, min(10, int(_arg.split("=")[1].strip())))
        except ValueError:
            pass

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

# コマンドライン引数からの戦略指定オーバーライド (デフォルト: None = 銘柄ごとにVAL_POC/RSIMAを動的採用)
FORCE_STRATEGY: Optional[str] = None
for _idx, _arg in enumerate(sys.argv):
    if _arg == "--val-poc":
        FORCE_STRATEGY = "val_poc"
    elif _arg in ("--strategy", "--strat") and _idx + 1 < len(sys.argv):
        FORCE_STRATEGY = sys.argv[_idx + 1].strip().lower()
    elif _arg.startswith("--strategy=") or _arg.startswith("--strat="):
        FORCE_STRATEGY = _arg.split("=")[1].strip().lower()


# コマンドライン引数からのステータス照会 (--status / --symbols)
if any(arg in sys.argv for arg in ("--status", "--symbols", "-s")):
    try:
        from show_symbol_status import display_and_send_symbols_status
        send_d = "--discord" in sys.argv
        print("[Status Mode] 主要アルト20銘柄の最新選定・除外ステータスを出力します...")
        display_and_send_symbols_status(send_to_discord=send_d)
    except Exception as e:
        print(f"[Status Mode Error] {e}")
    sys.exit(0)
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
    get_bingx_orderbook,
)
from bingx5_46_3logic import (
    send_discord,
    logicinstance,
    PnLCalculator,
    MPStrategy,
    backtester,
    run_interval_comparison,
    resample_candles,
    calc_add_pct,
    check_val_poc_signals,
    check_rsima3_signals,
    check_vah_poc_signals,
    check_rsima3_short_signals,
    check_normalized_peak_timing,
    check_market_regime_safe,
)

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

TARGET_POSITION_VALUE_USDT = BINGX_TARGET_POSITION_VALUE_USDT
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


def get_regime_gate_info(symbol: str) -> Dict[str, Any]:
    """Data/market_state.json より3段階全体地合い情報（BULL / NEUTRAL / BEAR）および個別前半ピークアウト除外判定を取得 (PROJECT_RULES.md 準拠)"""
    state_file = MIX_SCRIPT_PATH.parent / "Data" / "market_state.json"
    clean_sym = symbol.upper()
    ticker = clean_sym.replace("-USDT", "").replace("USDT", "")

    default_res = {
        "regime_gate": "ALLOW",
        "regime_reason": "DEFAULT",
        "action": "NORMAL",
        "overall_regime": "NEUTRAL",
        "is_buy_regime": False,
        "is_sell_regime": False,
        "is_peak_dropped": False,
    }
    if not state_file.exists():
        return default_res
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        overall_reg = data.get("overall_regime", "NEUTRAL")
        is_buy = (overall_reg == "BUY")
        is_sell = (overall_reg == "SELL")
        excluded = data.get("excluded_symbols", [])
        is_peak_dropped = (clean_sym in excluded or ticker in excluded)

        if overall_reg == "SELL":
            effective_gate = "ALLOW" if not is_peak_dropped else "STOP"
            effective_reason = "REGIME_SELL_SHORT" if not is_peak_dropped else "TROUGH_REBOUND_DROPPED"
            effective_action = "SHORT" if not is_peak_dropped else "STOP_REBOUND"
        elif overall_reg == "BUY":
            effective_gate = "ALLOW" if not is_peak_dropped else "STOP"
            effective_reason = "REGIME_BUY_LONG" if not is_peak_dropped else "PEAK_OUT_DROPPED"
            effective_action = "LONG" if not is_peak_dropped else "STOP_PEAK_DROP"
        else:
            effective_gate = "STOP"
            effective_reason = "REGIME_NEUTRAL_HOLD"
            effective_action = "STOP_NEUTRAL"

        return {
            "regime_gate": effective_gate,
            "regime_reason": effective_reason,
            "action": effective_action,
            "overall_regime": overall_reg,
            "is_buy_regime": is_buy,
            "is_sell_regime": is_sell,
            "is_peak_dropped": is_peak_dropped,
        }
    except Exception:
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


async def fetch_all_asset_contexts(mode: str = 'demo', target_symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """BingX Swap API から全銘柄のリアルタイム FR / 価格 / 出来高 / OI を取得"""
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

        # 3. 指定銘柄のリアルタイム建玉 (openInterest) を取得
        if target_symbols:
            oi_syms = set(normalize_symbol(s) for s in target_symbols)
            for s in oi_syms:
                try:
                    url_oi = f"{base_url}/openApi/swap/v2/quote/openInterest"
                    resp_oi = requests.get(url_oi, params={"symbol": s}, timeout=5)
                    if resp_oi.status_code == 200:
                        oi_data = resp_oi.json()
                        if oi_data.get("code") == 0:
                            oi_val = float(oi_data.get("data", {}).get("openInterest") or 0.0)
                            coin = s.replace("-USDT", "")
                            if s in res:
                                res[s]["openInterest"] = oi_val
                            if coin in res:
                                res[coin]["openInterest"] = oi_val
                except Exception:
                    pass
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


async def fetch_bingx_candles(symbol: str, granularity: str = "5m", limit: int = 300, mode: str = 'demo') -> pd.DataFrame:
    """5分足 SSoT ストアから最新のローソク足を取得（確定足のみを抽出）"""
    from candle_5m_store import sync_symbol_5m_candles, load_5m_candles, resample_from_5m
    try:
        df = sync_symbol_5m_candles(symbol, target_days=3)
        if df.empty:
            df = load_5m_candles(symbol)

        if not df.empty:
            gran_upper = granularity.upper()
            if gran_upper in ("15M", "30M", "1H", "60M", "1D"):
                rule_map = {"15M": "15min", "30M": "30min", "1H": "1h", "60M": "1h", "1D": "1d"}
                rule = rule_map.get(gran_upper, "15min")
                df = resample_from_5m(df, rule=rule)

            df = df.tail(limit).copy().reset_index(drop=True)

            now_utc = datetime.now(timezone.utc)
            granularity_sec_map = {"1M": 60, "3M": 180, "5M": 300, "15M": 900, "30M": 1800, "1H": 3600, "60M": 3600, "1D": 86400}
            gran_sec = granularity_sec_map.get(gran_upper, 300)
            if not df.empty:
                last_ts = pd.to_datetime(df['timestamp'].iloc[-1], utc=True)
                candle_close_time = last_ts + timedelta(seconds=gran_sec)
                if candle_close_time > now_utc:
                    df = df.iloc[:-1].reset_index(drop=True)
            return df
    except Exception as e:
        print(f"[{symbol}] fetch_bingx_candles error: {e}")
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


async def load_local_or_api_candles(symbol: str, limit: int = 1440) -> pd.DataFrame:
    """
    1年分蓄積データ (Data/historical_candles/{symbol}_1h.csv) を最優先で読み込み、
    直近指定本数 (デフォルト: 1440本 = 約2ヶ月分) を返す。
    存在しない場合は merged_{symbol}.csv や API から取得する。
    """
    clean_sym = symbol.replace("USDT", "").replace("USDC", "").upper()
    data_dir = Path(__file__).resolve().parent / "Data"
    target_cols = ["timestamp", "open", "high", "low", "close", "volume", "fundingRate", "openInterest", "funding", "oi"]

    # 1. 過去1年分蓄積ローソク足CSV (historical_candles/{symbol}_1h.csv) を最優先探索
    candles_dir = data_dir / "historical_candles"
    for cand_name in [f"{symbol}_1h.csv", f"{clean_sym}-USDT_1h.csv", f"{clean_sym}_1h.csv"]:
        cand_path = candles_dir / cand_name
        if cand_path.exists():
            try:
                df = pd.read_csv(cand_path)
                if not df.empty and "close" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms" if pd.to_numeric(df["timestamp"], errors="coerce").notna().all() else None)
                    df = df.sort_values("timestamp").reset_index(drop=True)
                    available_cols = [c for c in target_cols if c in df.columns]
                    df = df[available_cols].copy()
                    if limit and len(df) > limit:
                        df = df.tail(limit).reset_index(drop=True)
                    if len(df) >= 20:
                        return df
            except Exception:
                pass

    # 2. 32日分マージドデータ (Data/individual/merged_{clean_sym}.csv または Data/merged_{clean_sym}.csv)
    cand_csv = data_dir / "individual" / f"merged_{clean_sym}.csv"
    if not cand_csv.exists():
        cand_csv = data_dir / f"merged_{clean_sym}.csv"
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

    # 3. 32日分統合マスターCSV
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

    # 4. APIから直近足を取得
    from bingx5_46_2api import fetch_bingx_candles
    return await fetch_bingx_candles(symbol, "1h", limit=min(limit, 1000))


async def validate_profitable_candidates(trade_side: str, mode: str, base_symbol: str, interval: str = "60") -> Tuple[List[str], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """
    【PROJECT_RULES 準拠】2段階72通り個別最適化 (4足種×18通り: VAL_POC 36 + RSIMA 36)
    - 第1段階: シングルポジション(max_trades=1)で trade_count >= 10 かつ PnL > 0 を満たす自力優良パラメータのみ選抜
    - 第2段階: 厳選パラメータに対してのみ3枠ナンピン検証
    - 成績最優秀銘柄の資産曲線チャート (MaxDD USDT/% 併記) を 1 枚出力
    """
    from bingx5_46_2api import api_bingx
    from bingx5_46_3logic import run_interval_comparison, logicinstance, resample_candles

    market_state_file = MIX_SCRIPT_PATH.parent / "Data" / "market_state.json"
    market_state_info = {}
    if market_state_file.exists():
        try:
            with open(market_state_file, "r", encoding="utf-8") as f:
                market_state_info = json.load(f)
        except Exception:
            market_state_info = {}

    overall_reg = market_state_info.get("overall_regime", "NEUTRAL")
    recommended_symbols = market_state_info.get("recommended_symbols", [])
    excluded_raw = [str(s).upper() for s in market_state_info.get("excluded_symbols", [])]
    excluded_clean = set(get_clean_symbol(s) for s in excluded_raw)

    default_best_params: Dict[str, Any] = {
        "strategy": "rsima",
        "interval": 5,
        "mp": 200,
        "margin": 1.0,
        "params": {"rsi_len": 9, "lma_len": 7, "lEp": 40.0, "lCp": 60.0, "max_trades": MAX_TRADES_COUNT}
    }

    # 3態勢地合い連動モデルに基づく候補選定 & トレード態勢決定
    if overall_reg == "BUY":
        actual_side = "long"
        all_candidates = [
            s for s in recommended_symbols
            if get_clean_symbol(s) != "BTC" and get_clean_symbol(s) not in excluded_clean
        ]
        if not all_candidates:
            period_res = market_state_info.get("period_results", {})
            any_buy_passed = set()
            for w_info in period_res.values():
                for s in w_info.get("buy_passed", []):
                    cl_s = get_clean_symbol(s)
                    if cl_s != "BTC" and cl_s not in excluded_clean:
                        any_buy_passed.add(cl_s)
            all_candidates = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) in any_buy_passed]
        if not all_candidates:
            all_candidates = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) not in excluded_clean]

        discord.print_log(
            f"【全体地合い判定: BUY 🟢】3期間買い推奨銘柄（計 {len(all_candidates)} 銘柄: {', '.join(all_candidates) if all_candidates else 'なし'}）"
            f"のロング個別最適化（144通り: 単発厳選 ➔ 3枠ナンピン検証）を開始します..."
        )
    elif overall_reg == "SELL":
        actual_side = "short"
        all_candidates = [
            s for s in recommended_symbols
            if get_clean_symbol(s) != "BTC" and get_clean_symbol(s) not in excluded_clean
        ]
        if not all_candidates:
            # 3期間完全合致が0件の場合、売り票を獲得した銘柄から補完選定
            period_res = market_state_info.get("period_results", {})
            any_sell_passed = set()
            for w_info in period_res.values():
                for s in w_info.get("sell_passed", []):
                    cl_s = get_clean_symbol(s)
                    if cl_s != "BTC" and cl_s not in excluded_clean:
                        any_sell_passed.add(cl_s)
            all_candidates = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) in any_sell_passed]
        if not all_candidates:
            # 売り票合致も0件の場合、固定20銘柄から売り除外(底打ち反発)を除いた銘柄をバックテスト対象とする
            all_candidates = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) not in excluded_clean]

        discord.print_log(
            f"【全体地合い判定: SELL 🔴】3期間売り推奨銘柄（計 {len(all_candidates)} 銘柄: {', '.join(all_candidates) if all_candidates else 'なし'}）"
            f"のショート個別最適化（144通り: 単発1本固定・ナンピン完全なし）を開始します..."
        )
    else:
        # NEUTRAL (中立): 新規エントリー完全停止 ＆ 最適化スキップ (ノーポジ待機)
        discord.print_log(
            f"【全体地合い判定: NEUTRAL ⚪】中立相場のため新規個別最適化をスキップし、新規エントリーを完全停止（ノーポジ待機・既存ポジション建値早期撤退のみ）します。"
        )
        return [], {}, default_best_params

    if not all_candidates:
        discord.print_log("【全体地合い判定】候補銘柄が 0 件のためバックテストを待機します。")
        return [], {}, default_best_params

    profitable_cands = []
    symbol_params_map: Dict[str, Dict[str, Any]] = {}
    raw_dfs: Dict[str, pd.DataFrame] = {}

    for cand_idx, cand in enumerate(all_candidates):
        cand_clean = get_clean_symbol(cand)
        if cand_clean == "BTC":
            continue

        cand_whale_info = get_whale_sentiment_info(cand)
        cand_whale_sig = cand_whale_info.get("signal", "NEUTRAL")

        # 1. SSoT 5分足時系列ストアから取得
        cand_df_5m = load_5m_candles(cand)
        if cand_df_5m.empty or len(cand_df_5m) < 500:
            cand_df_5m = await asyncio.to_thread(sync_symbol_5m_candles, cand, 35)

        # 2. 1H足トレンド判定用データを 5分足から数学的完全整合リサンプリング
        if not cand_df_5m.empty and len(cand_df_5m) >= 60:
            cand_df = resample_from_5m(cand_df_5m, 60)
        else:
            cand_df = await load_local_or_api_candles(cand, limit=768)

        if cand_df is not None and not cand_df.empty:
            raw_dfs[cand] = cand_df

        discord.print_log(f"個別最適化中: {cand} (Rank #{cand_idx+1}) [方向: {actual_side.upper()}] ...")

        if cand_df_5m is not None and not cand_df_5m.empty and len(cand_df_5m) >= 120:
            try:
                results, cand_strat, cand_inv, cand_mp, cand_margin = await asyncio.to_thread(
                    run_interval_comparison,
                    df_5m=cand_df_5m, lot=1.0, data_equity=100.0, side_mode=actual_side, symbol=cand
                )

                best_params_dict = results.get("_best_params_", {})
                cand_pnl = float(best_params_dict.get("pnl", 0.0))
                trade_cnt = int(best_params_dict.get("trade_count", 0))
                win_rt = float(best_params_dict.get("win_rate", 0.0))
                max_dd = float(best_params_dict.get("DD_max", 0.0))

                cand_params = {
                    "strategy": cand_strat,
                    "interval": cand_inv,
                    "mp": cand_mp,
                    "margin": cand_margin,
                    "num_bins": best_params_dict.get("num_bins", 50),
                    "value_area_pct": best_params_dict.get("value_area_pct", 0.70),
                    "ma_type": best_params_dict.get("ma_type", "EMA"),
                    "ma_condition": best_params_dict.get("ma_condition", "close_above"),
                    "l_ep": best_params_dict.get("l_ep", 40.0),
                    "l_cp": best_params_dict.get("l_cp", 60.0),
                    "sl_pct": best_params_dict.get("sl_pct", 0.025),
                    "dynamic_mae_stop": float(best_params_dict.get("dynamic_mae_stop", -0.025)),
                    "pnl": cand_pnl,
                    "trade_count": trade_cnt,
                    "win_rate": win_rt,
                    "max_dd": max_dd,
                    "single_pnl": best_params_dict.get("single_pnl", cand_pnl),
                    "single_trades": best_params_dict.get("single_trades", trade_cnt),
                    "single_win_rate": best_params_dict.get("single_win_rate", win_rt),
                    "chart_df": best_params_dict.get("chart_df"),
                    "params": best_params_dict,
                }
                symbol_params_map[cand] = cand_params

                # プラス収支かつ最低取引回数(10回以上)クリアで合格
                if cand_pnl > 0 and trade_cnt >= 10:
                    dyn_sl_disp = cand_params.get("dynamic_mae_stop", -0.025)
                    if cand_strat == "val_poc":
                        detail_str = f"MP={cand_mp}, Bins={cand_params['num_bins']}, VA={int(cand_params['value_area_pct']*100)}%, {cand_params['ma_type']}_{cand_params['ma_condition']}, 動的SL={dyn_sl_disp:.2%}"
                    else:
                        detail_str = f"EP={int(cand_params['l_ep'])}, CP={int(cand_params['l_cp'])}, 動的SL={dyn_sl_disp:.2%}"
                    s_pnl = cand_params.get('single_pnl', cand_pnl)
                    s_cnt = cand_params.get('single_trades', trade_cnt)
                    s_win = cand_params.get('single_win_rate', win_rt)
                    discord.print_log(
                        f"   [合格 🟢] {cand}: 戦略={cand_strat.upper()} ({cand_inv}m / {detail_str}) -> "
                        f"純利益: +${cand_pnl:.2f} USDT (取引: {trade_cnt}回, 勝率: {win_rt:.1f}%, DD: {max_dd:.2f} USDT ({max_dd:.1f}%)) "
                        f"[シングル1枠: +${s_pnl:.2f} / {s_cnt}回 / {s_win:.1f}%]"
                    )
                    profitable_cands.append(cand)
                else:
                    discord.print_log(f"   [不合格 🔴] {cand}: PnL = ${cand_pnl:+.2f} USDT (取引: {trade_cnt}回 < 最低10回基準またはマイナス)")

            except Exception as opt_err:
                discord.print_log(f"   [最適化エラー] {cand}: {opt_err}")

        await asyncio.sleep(0.1)

    # 1ヶ月間バックテスト 最優秀成績銘柄 チャート1枚出力 (PROJECT_RULES.md 準拠)
    best_sym = None
    if profitable_cands:
        best_sym = max(profitable_cands, key=lambda s: symbol_params_map[s].get("pnl", 0.0))
    elif symbol_params_map:
        best_sym = max(symbol_params_map.keys(), key=lambda s: symbol_params_map[s].get("pnl", 0.0))

    if best_sym and best_sym in symbol_params_map:
        try:
            best_p = symbol_params_map[best_sym]
            best_strat = best_p["strategy"]
            best_inv = best_p.get("interval", 5)
            best_mp = best_p.get("mp", 200)
            best_pnl = float(best_p.get("pnl", 0.0))
            best_cnt = int(best_p.get("trade_count", 0))
            best_win = float(best_p.get("win_rate", 0.0))
            best_dd = float(best_p.get("max_dd", 0.0))

            bg_csv = f"backtest_data/klines100_{get_clean_symbol(best_sym)}_bingx.csv"
            Path("backtest_data").mkdir(parents=True, exist_ok=True)
            chart_df = best_p.get("chart_df")

            if isinstance(chart_df, pd.DataFrame) and not chart_df.empty:
                chart_df.to_csv(bg_csv, index=False)
            elif best_sym in raw_dfs:
                cand_df = raw_dfs[best_sym]
                cand_df.tail(min(len(cand_df), 720)).reset_index(drop=True).to_csv(bg_csv, index=False)

            strat_tag = "RSIMA3" if best_strat in ["rsima", "rsima3"] else "VAL_POC"
            chart_label = f"Best1Month_{strat_tag}_{best_inv}m"
            discord.print_log(
                f"🏆 **【バックテスト 最優秀成績銘柄 (1ヶ月間)】**\n"
                f"・銘柄: `{best_sym}` (採用足種: `{best_inv}m足` / 戦略: `{strat_tag}`)\n"
                f"・純利益: `+${best_pnl:.2f} USDT` (取引: {best_cnt}回, 勝率: {best_win:.1f}%, MaxDD: `-${best_dd:.2f} USDT (-{best_dd:.1f}%)`)\n"
                f"・最優秀成績チャート1枚を出力します。"
            )
            discord.plot_backtest(label=chart_label, csv_file=bg_csv, symbol=best_sym)
        except Exception as ch_err:
            print(f"Best cand chart error {best_sym}: {ch_err}")

    # チャート出力完了後はメモリ解放
    for sym_k in symbol_params_map:
        symbol_params_map[sym_k].pop("chart_df", None)

    if best_sym:
        default_best_params = symbol_params_map.get(best_sym, default_best_params)
    elif profitable_cands:
        first_sym = profitable_cands[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)
    elif symbol_params_map:
        first_sym = list(symbol_params_map.keys())[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)

    return profitable_cands, symbol_params_map, default_best_params


async def select_top_bingx_symbols(top_n: int = 20, mode: str = 'demo') -> List[Dict[str, Any]]:
    """固定選定20銘柄および market_state.json の推奨銘柄に基づいて選定候補を返す"""
    state_file = Path(__file__).resolve().parent / "Data" / "market_state.json"
    target_symbols = list(FIXED_SYMBOLS)

    # 1. market_state.json が存在する場合は推奨銘柄を優先配置
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
            rec_syms = st.get("recommended_symbols", [])
            if rec_syms:
                # 推奨銘柄を先頭にし、残りの固定銘柄を後ろに配置
                combined = []
                for s in rec_syms:
                    if s in FIXED_SYMBOLS and s not in combined:
                        combined.append(s)
                for s in FIXED_SYMBOLS:
                    if s not in combined:
                        combined.append(s)
                target_symbols = combined
        except Exception as st_err:
            print(f"[Symbol Selection Warning] market_state.json 読み込み失敗: {st_err}")

    # 2. 取引所 universe 存在チェック
    valid_symbols = target_symbols
    try:
        from bingx5_46_2api import get_bingx_universe_symbols
        valid_universe = get_bingx_universe_symbols(mode=mode)
        if valid_universe:
            filtered = [s for s in target_symbols if normalize_symbol(s).upper() in valid_universe]
            if filtered:
                valid_symbols = filtered
    except Exception as filter_err:
        print(f"[Symbol Selection Warning] Universe フィルタエラー: {filter_err}")

    results = [{"symbol": s, "lastPr": 1.0, "usdtVolume": 1e8} for s in valid_symbols[:top_n]]
    print(f"[Symbol Selection] 候補銘柄選定完了 ({len(results)}銘柄): {', '.join([r['symbol'] for r in results])}")
    return results


TRAILING_TP_FILE = Path(__file__).resolve().parent / "Data" / "trailing_tp_state.json"
SCALE_IN_FILE = Path(__file__).resolve().parent / "Data" / "scale_in_state.json"
ACTIVE_POS_META_FILE = Path(__file__).resolve().parent / "Data" / "active_positions_meta.json"

def load_trailing_tp_states() -> dict:
    if TRAILING_TP_FILE.exists():
        try:
            with open(TRAILING_TP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_trailing_tp_states(states: dict):
    try:
        TRAILING_TP_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAILING_TP_FILE, "w", encoding="utf-8") as f:
            json.dump(states, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def load_scale_in_states() -> dict:
    for path in (SCALE_IN_FILE, ACTIVE_POS_META_FILE):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data:
                        return data
            except Exception:
                pass
    return {}

def save_scale_in_states(states: dict):
    for path in (SCALE_IN_FILE, ACTIVE_POS_META_FILE):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(states, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

SL_COOLDOWN_FILE = Path(__file__).resolve().parent / "Data" / "sl_cooldown_states.json"
SL_COOLDOWN_HOURS = 24.0

def load_sl_cooldown_states() -> dict:
    if SL_COOLDOWN_FILE.exists():
        try:
            with open(SL_COOLDOWN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sl_cooldown_states(states: dict):
    try:
        SL_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SL_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(states, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def record_sl_cooldown(symbol: str):
    """損切り発生時に24時間クールダウンを記録"""
    states = load_sl_cooldown_states()
    states[symbol] = time.time()
    save_sl_cooldown_states(states)

def is_in_sl_cooldown(symbol: str) -> Tuple[bool, float]:
    """過去24時間以内に損切りが発生した銘柄かを判定 (残り時間を返却)"""
    states = load_sl_cooldown_states()
    last_t = float(states.get(symbol, 0.0))
    if last_t <= 0:
        return False, 0.0
    elapsed = time.time() - last_t
    rem_hours = SL_COOLDOWN_HOURS - (elapsed / 3600.0)
    if rem_hours > 0:
        return True, rem_hours
    return False, 0.0



async def wait_until_next_candle(
    symbol_apis: Optional[dict] = None,
    trailing_tp_states: Optional[dict] = None,
    scale_in_states: Optional[dict] = None,
    mode: str = "demo",
    interval_minutes: int = 5,
    ws_mgr: Optional[Any] = None
):
    """次の5分足確定（毎時00分, 05分, 10分...55分の02秒）まで待機する。
    ポジション保有中の銘柄がある場合、pybotters WebSocket から取得したリアルタイム価格を用いて、
    - 利確トレーリング(TTP)の最高値追従および反落成行利確
    - VAL_POC戦略のリアルタイムPOC到達クローズ
    - 段階的ナンピン（1〜3回目: 0.2%, 4〜5回目: 0.3%, 最大5回）
    - 3%固定ストップロス
    を超低遅延で即応実行する。"""
    now = datetime.now(JST)
    rem_min = interval_minutes - (now.minute % interval_minutes)
    next_candle = (now + timedelta(minutes=rem_min)).replace(second=2, microsecond=0)
    wait_seconds = (next_candle - now).total_seconds()
    if wait_seconds > 0:
        discord.print_log(f"[Wait] 次の{interval_minutes}分足確定まで {wait_seconds:.1f} 秒待機 (次回: {next_candle.strftime('%H:%M:%S')} JST)", level="debug")
    
    while True:
        now = datetime.now(JST)
        rem_sec = (next_candle - now).total_seconds()
        if rem_sec <= 1.0:
            if rem_sec > 0:
                await asyncio.sleep(rem_sec)
            break
            
        sleep_step = min(5.0, max(rem_sec - 0.5, 0.5))
        await asyncio.sleep(sleep_step)
        
        now = datetime.now(JST)
        if now >= next_candle:
            break
            
        # 1. ポジション保有中銘柄のリアルタイム利確トレーリング監視
        if symbol_apis and trailing_tp_states:
            active_keys = [k for k, v in list(trailing_tp_states.items()) if v.get("active")]
            for sym in active_keys:
                api = symbol_apis.get(sym)
                if not api:
                    continue
                try:
                    pos = await api.get_positions()
                    buy_qty = float(pos.get("buy", 0.0))
                    if buy_qty <= 0:
                        trailing_tp_states.pop(sym, None)
                        save_trailing_tp_states(trailing_tp_states)
                        continue
                    
                    entry_px = float(pos.get("buy_pos", 0.0))
                    if entry_px <= 0:
                        continue
                        
                    # WebSocket リアルタイム価格を最優先参照
                    cur_px = (ws_mgr.get_latest_price(sym) if ws_mgr else None)
                    if not cur_px or cur_px <= 0:
                        from bingx5_46_2api import get_bingx_orderbook
                        best_bid, _ = get_bingx_orderbook(sym, api.bingx.base_url)
                        cur_px = best_bid if (best_bid and best_bid > 0) else entry_px
                    
                    ttp = trailing_tp_states.get(sym)
                    if not ttp or not ttp.get("active"):
                        continue
                    
                    # 最高値の追従更新
                    if cur_px > ttp.get("peak_price", entry_px):
                        ttp["peak_price"] = cur_px
                    
                    callback_pct = 0.008  # 0.8%
                    min_guarantee = entry_px * 1.001  # 建値+手数料0.1%保証
                    ttp["trail_stop"] = max(ttp["peak_price"] * (1.0 - callback_pct), min_guarantee)
                    trailing_tp_states[sym] = ttp
                    save_trailing_tp_states(trailing_tp_states)
                    
                    # 反落成行利確チェック
                    if cur_px < ttp["trail_stop"]:
                        strat = ttp.get("strategy", "TTP").upper()
                        exit_reason = f"TrailingTP_{strat}"
                        pnl_est = (cur_px - entry_px) * buy_qty
                        discord.print_log(
                            f"[{sym}] [REALTIME TRAILING TP] 🎯 リアルタイム利確トレーリング成立!\n"
                            f"   └ 現在値: ${cur_px:,.4f} < 利確ライン: ${ttp['trail_stop']:,.4f} (最高値: ${ttp['peak_price']:,.4f} から反落) | 概算PnL: {pnl_est:+.2f} USDT ➔ 成行利確"
                        )
                        from bingx5_46_2api import flatten_current_position_bingx
                        await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                        from real_trade_tracker import record_real_trade
                        record_real_trade(sym, "LONG", "CLOSE", cur_px, buy_qty, pnl_est, f"実運用決済 ({exit_reason})")
                        trailing_tp_states.pop(sym, None)
                        save_trailing_tp_states(trailing_tp_states)
                except Exception as mon_err:
                    pass

        # 2. VAL_POC 戦略のリアルタイム監視（POC到達クローズ ＆ 0.2%下落ナンピン）
        if symbol_apis and scale_in_states:
            active_scale_syms = list(scale_in_states.keys())
            for sym in active_scale_syms:
                api = symbol_apis.get(sym)
                if not api:
                    continue
                try:
                    pos = await api.get_positions()
                    buy_qty = float(pos.get("buy", 0.0))
                    if buy_qty <= 0:
                        scale_in_states.pop(sym, None)
                        save_scale_in_states(scale_in_states)
                        continue

                    entry_px = float(pos.get("buy_pos", 0.0))
                    if entry_px <= 0:
                        continue

                    # WebSocket リアルタイム価格を最優先参照
                    cur_px = (ws_mgr.get_latest_price(sym) if ws_mgr else None)
                    if not cur_px or cur_px <= 0:
                        from bingx5_46_2api import get_bingx_orderbook
                        best_bid, _ = get_bingx_orderbook(sym, api.bingx.base_url)
                        cur_px = best_bid if (best_bid and best_bid > 0) else entry_px

                    scale_state = scale_in_states.get(sym)
                    if not scale_state:
                        continue

                    poc_val = float(scale_state.get("poc_level", 0.0))
                    last_px = float(scale_state.get("last_entry_price", entry_px)) or entry_px
                    cur_nanpin = int(scale_state.get("nanpin_count", 0))
                    strat_type = scale_state.get("strategy", "val_poc")

                    # 地合いディフェンス判定取得 (3段階: BULL / NEUTRAL / BEAR)
                    regime_info = get_regime_gate_info(sym)
                    overall_regime = regime_info.get("overall_regime", "NEUTRAL")

                    # 地合い悪化（NEUTRAL / BEAR）時の緊急建値撤退 (+0.20% 到達で即時成行決済)
                    if overall_regime != "BUY" and entry_px > 0 and cur_px >= entry_px * 1.0020:
                        exit_reason = "Emergency_Breakeven_Exit_+0.20%"
                        pnl_est = (cur_px - entry_px) * buy_qty
                        discord.print_log(
                            f"[{sym}] [EMERGENCY BREAKEVEN EXIT] ⚠️ 地合い非BUY ({overall_regime}) 検知により建値+0.20%早期撤退成立!\n"
                            f"   └ 現在値: ${cur_px:,.4f} >= 撤退ライン: ${entry_px * 1.0020:,.4f} (建値: ${entry_px:,.4f}) | 概算PnL: {pnl_est:+.2f} USDT ➔ 成行決済"
                        )
                        from bingx5_46_2api import flatten_current_position_bingx
                        await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                        from real_trade_tracker import record_real_trade
                        record_real_trade(sym, "LONG", "CLOSE", cur_px, buy_qty, pnl_est, f"実運用決済 ({exit_reason})")
                        scale_in_states.pop(sym, None)
                        save_scale_in_states(scale_in_states)
                        continue

                    # A. リアルタイム VAL-VAH 60% 利確判定 (Only Exit Above Avg Price: 建値プラス域限定)
                    target_tp = float(scale_state.get("target_tp", 0.0)) or poc_val
                    if strat_type in ("val_poc", "vp_val_gc") and target_tp > 0 and cur_px >= target_tp and cur_px > entry_px * 1.0006:
                        exit_reason = "VAL_VAH_60_TP_REALTIME"
                        pnl_est = (cur_px - entry_px) * buy_qty
                        discord.print_log(
                            f"[{sym}] [REALTIME 60% TP] 🎯 リアルタイムVAL-VAH 60%利確成立!\n"
                            f"   └ 現在値: ${cur_px:,.4f} >= 目標値: ${target_tp:,.4f} (平均建値: ${entry_px:,.4f}) | 概算PnL: {pnl_est:+.2f} USDT ➔ 成行利確"
                        )
                        from bingx5_46_2api import flatten_current_position_bingx
                        await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                        from real_trade_tracker import record_real_trade
                        record_real_trade(sym, "LONG", "CLOSE", cur_px, buy_qty, pnl_est, f"実運用決済 ({exit_reason})")
                        scale_in_states.pop(sym, None)
                        save_scale_in_states(scale_in_states)
                        continue

                    # B. リアルタイム 段階的ナンピン判定 (PROJECT_RULES準拠: 初期1+ナンピン最大2=計3枠, 等倍1.0x, VA60%間隔)
                    # 地合いが BUY の時のみナンピン許可 (NEUTRAL/BEAR時は即時凍結)
                    if overall_regime != "BUY":
                        continue

                    max_allowed_nanpins = 2
                    vah_lvl = float(scale_state.get("vah_level", 0.0))
                    val_lvl = float(scale_state.get("val_level", 0.0))
                    va_spacing = (vah_lvl - val_lvl) * 0.50 if (vah_lvl > val_lvl and val_lvl > 0) else 0.0
                    drop_dist = max(last_px * 0.010, va_spacing) if va_spacing > 0 else (last_px * calc_add_pct(cur_nanpin + 1, strategy=strat_type))
                    drop_target = last_px - drop_dist
                    req_drop_pct = (drop_dist / last_px) if last_px > 0 else 0.030
                    spec_rt = getattr(api.bingx, "instrument_spec", {}) or {}
                    base_lot_rt = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec_rt)
                    actual_lots_rt = int(round(buy_qty / max(base_lot_rt, 1e-6))) if base_lot_rt > 0 else 1
                    eff_nanpin = max(cur_nanpin, actual_lots_rt - 1)
                    cur_notional = buy_qty * cur_px
                    add_notional = base_lot_rt * cur_px
                    max_notional_limit = BINGX_TARGET_POSITION_VALUE_USDT * 3.15

                    if eff_nanpin < max_allowed_nanpins and cur_px <= drop_target and base_lot_rt > 0:
                        if (cur_notional + add_notional) > max_notional_limit:
                            discord.print_log(
                                f"[{sym}] [NANPIN BLOCKED] 🛑 想定元本上限ガード到達 (${cur_notional + add_notional:.2f} > ${max_notional_limit:.2f}) ➔ ナンピン中止",
                                level="warning"
                            )
                            continue

                        discord.print_log(
                            f"[{sym}] [REALTIME NANPIN] 📉 最後の建値(${last_px:,.4f})からVA60%(-{req_drop_pct*100:.1f}%)下落検出 (${cur_px:,.4f} <= ${drop_target:,.4f})\n"
                            f"   └ ナンピン発動 ({eff_nanpin + 1}/{max_allowed_nanpins} 回目, 1x均等ロット={base_lot_rt})...",
                            level="debug"
                        )
                        bal_val = float(pos.get("margin", 0.0)) or 100.0
                        entered_add = await api.long_entry(
                            None, pos, bal_val, base_lot_rt, max_lot=10.0,
                            max_stage=max_allowed_nanpins + 1, is_scale_in=True, stage_num=eff_nanpin + 2
                        )
                        if entered_add:
                            scale_state["nanpin_count"] = eff_nanpin + 1
                            scale_state["last_entry_price"] = cur_px
                            scale_in_states[sym] = scale_state
                            save_scale_in_states(scale_in_states)
                            from real_trade_tracker import record_real_trade
                            record_real_trade(sym, "LONG", "SCALE_IN", cur_px, base_lot_rt, 0.0, f"実運用ナンピン買い増し ({eff_nanpin + 1}/{max_allowed_nanpins})")
                            discord.print_log(
                                f"🚀🚀🚀 **【リアルタイム ナンピン買い増し約定】** 🚀🚀🚀\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📌 **銘柄 / 方向**: `{sym}` (LONG 🟢 / {strat_type.upper()})\n"
                                f"💰 **約定価格**: `${cur_px:.6f}`\n"
                                f"📦 **追加数量**: `{base_lot_rt:,.2f} {sym}` (1x均等ロット)\n"
                                f"🔢 **ナンピン段階**: `{eff_nanpin + 1}/{max_allowed_nanpins} 回目` (合計 {eff_nanpin + 2}/{max_allowed_nanpins + 1} 枠)\n"
                                f"🎯 **目標60%利確値**: `${target_tp:.6f}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            )

                except Exception as scale_err:
                    pass

# 下位互換エイリアス
wait_until_next_hour = wait_until_next_candle


async def run_screening_and_optimization(mode: str, send_charts: bool = False, skip_zip: bool = False) -> tuple:
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
            if skip_zip:
                cmd.append("--skip-zip")
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

    # 4. 3態勢地合い連動 戦略モード判定
    market_state_file = MIX_SCRIPT_PATH.parent / "Data" / "market_state.json"
    overall_reg_mode = "NEUTRAL"
    if market_state_file.exists():
        try:
            with open(market_state_file, "r", encoding="utf-8") as f:
                ms_data = json.load(f)
                overall_reg_mode = ms_data.get("overall_regime", "NEUTRAL")
        except Exception:
            pass

    if overall_reg_mode == "BUY":
        trade_side = "long"
        strat_mode_desc = "LONG ONLY (買い相場特化・最大3枠ナンピン)"
    elif overall_reg_mode == "SELL":
        trade_side = "short"
        strat_mode_desc = "SHORT ONLY (売り相場特化・単発1本固定/ナンピンなし)"
    else:
        trade_side = "none"
        strat_mode_desc = "FLAT / NO TRADING (中立相場・新規エントリー完全停止・撤退のみ)"

    btc_whale_info = get_whale_sentiment_info("BTC")
    btc_whale_sig = btc_whale_info.get("signal", "NEUTRAL")
    print(f"[Market State] Regime: {overall_reg_mode} | Strategy Mode: {strat_mode_desc} | BTC Whale: {btc_whale_sig}")

    # 5. パラメータ最適化 & バックテスト合格銘柄選抜 (MTF適格 ＆ PnL > 0)
    base_sym = selected_symbols_info[0]["symbol"] if selected_symbols_info else "HYPE"
    profitable_cands, symbol_params_map, default_best_params = await validate_profitable_candidates(
        trade_side=trade_side, mode=mode, base_symbol=base_sym
    )

    # MTF適格 かつ バックテスト合格銘柄のみを選定（不適格銘柄の強制追加は完全撤廃）
    selected_symbols = list(profitable_cands)[:MAX_SELECTED_SYMBOLS]
    print(f"\n[Selection Result] 選定{len(selected_symbols)}銘柄 (サイクル適格 ＆ 個別最適化合格): {', '.join(selected_symbols) if selected_symbols else 'なし (全銘柄見送り・完全ノーポジ維持)'}")

    # 直近の選定結果を JSON に永続化 (show_symbol_status 用)
    last_res_file = Path(__file__).resolve().parent / "Data" / "last_selection_result.json"
    try:
        with open(last_res_file, "w", encoding="utf-8") as f:
            json.dump({
                "selected_symbols": selected_symbols,
                "symbol_params_map": symbol_params_map,
                "evaluated_at": datetime.now(timezone.utc).isoformat()
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        pass

    # 全20銘柄の選定・除外ステータス完全一覧ダッシュボードを出力 ＆ Discord送信 (PROJECT_RULES.md 準拠)
    try:
        from show_symbol_status import display_and_send_symbols_status
        display_and_send_symbols_status(
            selected_symbols=selected_symbols,
            symbol_params_map=symbol_params_map,
            send_to_discord=True
        )
    except Exception as e:
        discord.print_log(f"[Warning] 全銘柄ステータスダッシュボード出力エラー: {e}")


    # 選定銘柄の個別最適化パラメータをDiscordログ表示
    if selected_symbols:
        discord.print_log("\n★ 【合格銘柄 個別最適化パラメータ一覧 (VAL_POC / RSIMA 2段階72通り検証)】")
        for sym in selected_symbols:
            p = symbol_params_map.get(sym, default_best_params)
            pnl_val = p.get('pnl', 0.0)
            wr_val = p.get('win_rate', 0.0)
            tc_val = p.get('trade_count', 0)
            dd_val = p.get('max_dd', 0.0)
            strat = p.get('strategy', 'rsima').upper()
            inv_val = int(p.get('interval', 5))
            
            if strat in ("VAL_POC", "VP_VAL_GC"):
                mp_v = p.get('mp', 200)
                bins_v = p.get('num_bins', 50)
                va_v = int(float(p.get('value_area_pct', 0.70)) * 100)
                ma_v = f"{p.get('ma_type', 'EMA')}_{p.get('ma_condition', 'close_above')}"
                param_str = f"{inv_val}m / MP={mp_v}, Bins={bins_v}, VA={va_v}%, {ma_v} ➔ VA60%間隔ナンピン(最大2回/計3枠) ➔ VA60%利確"
            else:
                ep_v = int(float(p.get('l_ep', 40)))
                cp_v = int(float(p.get('l_cp', 60)))
                sl_v = float(p.get('sl_pct', 0.090)) * 100
                param_str = f"{inv_val}m / RSI=9, EMA=7, EP={ep_v}, CP={cp_v}, SL={sl_v:.1f}% ➔ VA60%間隔ナンピン(最大2回/計3枠)"

            discord.print_log(
                f"   📌 [{sym}] 戦略={strat} (最大{MAX_TRADES_COUNT}枠/1x均等) | {param_str} | "
                f"純利益: {pnl_val:+.4f} USDT (取引: {tc_val}回, 勝率: {wr_val:.1f}%, DD: {dd_val:.2f} USDT ({dd_val:.1f}%))"
            )
    else:
        discord.print_log("\n⚠️ 【トレード適格銘柄なし】本日MTF条件を満たす銘柄がないため、新規エントリーは全停止しノーポジ待機します。")

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


async def send_periodic_performance_report(symbol_apis: Dict[str, Any], mode: str, label: str = "8時間定期レポート"):
    """実運用トレード実績（直近10日間 Clean版/Detail版 2枚の累積PnLチャート画像 ＆ 現在の証拠金残高）をDiscordへ送信 (PROJECT_RULES 準拠)"""
    try:
        from real_trade_tracker import (
            sync_real_trades_from_bingx_api,
            print_real_trading_table,
            send_periodic_pnl_report_discord,
            check_and_send_monthly_report_on_first_day,
            get_real_trading_summary,
        )
        from bingx5_46_2api import api_bingx

        # 1. 取引所 API から最新の確定実現損益データを直接同期（手動損切り・外部決済も完全網羅）
        try:
            await sync_real_trades_from_bingx_api(mode=mode)
        except Exception as sync_pnl_err:
            discord.print_log(f"[Performance Report] 取引所API実現損益同期警告: {sync_pnl_err}")

        # 2. 口座残高（証拠金）の取得
        current_balance = 0.0
        if symbol_apis:
            first_api = next(iter(symbol_apis.values()))
            try:
                current_balance = await first_api.get_account()
            except Exception:
                pass
        if current_balance <= 0:
            try:
                temp_api = api_bingx(symbol="BTC-USDT", mode=mode)
                current_balance = await temp_api.get_account()
            except Exception:
                current_balance = 1000.0

        # 3. コンソールログへ実運用損益表を出力
        print_real_trading_table(days=10, current_balance=current_balance)

        # 4. スライディング10日間 Clean版・Detail版2枚出力 ＆ 月次レポート判定
        summary = get_real_trading_summary(days=10, current_balance=current_balance)
        await asyncio.to_thread(send_periodic_pnl_report_discord, 10, current_balance)
        await asyncio.to_thread(check_and_send_monthly_report_on_first_day)
        discord.print_log(f"[Performance Report] {label} 送信完了 (残高: ${current_balance:.2f}, 累計損益: ${summary['net_pnl']:+.2f})")
    except Exception as e:
        discord.print_log(f"[Performance Report Error] 実績レポート送信失敗: {e}")


async def _async_post_entry_reporting(
    sym: str,
    entry_px: float,
    lot_size: float,
    entry_full_reason: str,
    df_chart: pd.DataFrame,
    vah_val: float,
    val_val: float,
    poc_val: float,
    strat_desc: str,
    whale_sig: str,
    whale_flow_val: float,
    interval_label: str = "5m"
):
    """新規エントリー約定後のトレード記録・チャート生成・Discord画像送信を完全非同期実行する"""
    try:
        from real_trade_tracker import record_real_trade, plot_entry_chart
        record_real_trade(sym, "LONG", "ENTRY", entry_px, lot_size, 0.0, f"新規エントリー約定 [{interval_label}足]")

        entry_notional = entry_px * lot_size
        target_tp_val = val_val + (vah_val - val_val) * 0.50 if (vah_val > val_val and val_val > 0) else poc_val

        entry_msg = (
            f"🚀🚀🚀 **【新規ロングエントリー約定 [{interval_label}足]】** 🚀🚀🚀\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **銘柄 / 方向**: `{sym}` (LONG 🟢 / 採用足種: `{interval_label}足`)\n"
            f"⚡ **検知トリガー**: `{entry_full_reason}`\n"
            f"💰 **約定価格**: `${entry_px:.6f}`\n"
            f"📦 **発注数量**: `{lot_size:,.2f} {sym}` (約 `${entry_notional:,.2f} USDT`)\n"
            f"⚙️ **適用戦略**: `{strat_desc}`\n"
            f"🎯 **利確目標**: `VAL-VAH 60%水準 (${target_tp_val:.6f}) / RSI過熱利確`\n"
            f"📉 **ナンピン設計**: `VA 60%間隔・均等1倍ロット (最大2回追加 / 合計最大3枠)`\n"
            f"🛑 **損切り仕様**: `最終建値から VA 60%割れ (確定足終値判定) ＋ 24hクールダウン`\n"
            f"🐋 **クジラ判定**: `{whale_sig}` (流入: `${whale_flow_val:+,.0f}`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        entry_chart_file = await asyncio.to_thread(
            plot_entry_chart,
            symbol=sym,
            df=df_chart,
            entry_price=entry_px,
            vah=vah_val,
            val=val_val,
            poc=poc_val,
            strategy_name=strat_desc,
            whale_signal=whale_sig,
            whale_flow=whale_flow_val,
            interval_label=interval_label,
        )
        if entry_chart_file and Path(entry_chart_file).exists():
            discord.send_file(entry_chart_file, description=entry_msg)
        else:
            discord.print_log(entry_msg)
    except Exception as e:
        discord.print_log(f"⚠️ [{sym}] エントリーレポート非同期タスクエラー: {e}", level="warning")


async def _async_post_exit_reporting(
    sym: str,
    side: str,
    close_qty: float,
    reported_exit_px: float,
    entry_px: float,
    pnl: float,
    exit_reason: str,
    df_chart: pd.DataFrame,
    whale_sig: str,
    cycle_balance: float,
    interval_label: str = "5m"
):
    """決済完了後のトレード記録・チャート生成・Discord画像送信をメインループから隔離して完全非同期実行する"""
    try:
        from real_trade_tracker import record_real_trade, plot_real_trading_performance, plot_exit_chart
        record_real_trade(sym, side, "CLOSE", reported_exit_px, close_qty, pnl, f"実運用決済 ({exit_reason})")
        discord.print_log(f"[{sym}] [CLOSE] 🟢 ロングポジション決済完了 [{interval_label}足] (PnL: {pnl:+.2f} USDT, 理由: {exit_reason})")

        # 1. 決済トレードチャート画像の生成 & 送信
        exit_chart_file = await asyncio.to_thread(
            plot_exit_chart,
            symbol=sym, df=df_chart, exit_price=reported_exit_px, entry_price=entry_px,
            pnl=pnl, exit_reason=exit_reason, side=side, whale_signal=whale_sig,
            interval_label=interval_label
        )
        if exit_chart_file and Path(exit_chart_file).exists():
            discord.send_file(exit_chart_file, f"📊 【決済チャート】{sym} LONG 決済完了 [{interval_label}足] (PnL: {pnl:+.2f} USDT)")

        # 2. スライディング10日間 累積 PnL パフォーマンスチャート (Clean版 & Detail版 2枚)
        clean_file, detail_file = await asyncio.to_thread(plot_real_trading_performance, 10, cycle_balance)
        if clean_file and Path(clean_file).exists():
            discord.send_file(clean_file, f"📈 【実運用実績】累積損益パフォーマンス (直近10日 - Clean版 / 今回決済: {pnl:+.2f} USDT)")
        if detail_file and Path(detail_file).exists():
            discord.send_file(detail_file, f"🔍 【実運用実績】累積損益パフォーマンス (直近10日 - Detail版 / 今回決済: {pnl:+.2f} USDT)")
    except Exception as e:
        discord.print_log(f"⚠️ [{sym}] 決済レポート非同期タスクエラー: {e}", level="warning")


async def start(mode: str = 'demo', max_lot: float = 10.0, interval: str = '60'):
    from bingx5_46_2api import (
        api_bingx,
        fetch_instrument_spec_bingx,
        flatten_current_position_bingx,
    )
    from bingx5_46_3logic import PnLCalculator

    account_mode_str = "[LIVE Account] (本番口座)" if mode == "live" else "[DEMO Account] (BingX VST デモ口座)"
    air_mode_str = "[AIR MODE ON] (シミュレーション・仮想ペーパートレード)" if bingx5_46_2api.is_air else "[REAL VST ORDER ON] (実際にBingX VST取引所へ発注・約定・管理)"

    hours_str = ", ".join([f"{h:02d}:00" for h in sorted(ANALYSIS_HOURS)])
    start_msg = (
        "```text\n"
        " ____  _               __  __\n"
        "| __ )(_)_ __   __ _  \\ \\/ /\n"
        "|  _ \\| | '_ \\ / _` |  \\  / \n"
        "| |_) | | | | | (_| |  /  \\ \n"
        "|____/|_|_| |_|\\__, | /_/\\_\\\n"
        "               |___/        \n"
        "============================\n"
        "  AUTO TRADING SYSTEM START \n"
        "============================\n"
        "```\n"
        "🚀 **【BingX Auto Trading System 起動 (3態勢地合い連動)】**\n"
        "```text\n"
        "==================================================\n"
        f"  取引口座設定 : {account_mode_str}\n"
        f"  発注モード   : {air_mode_str}\n"
        f"  戦略態勢     : 3態勢地合い連動 (BULL:買い / NEUTRAL:待機 / BEAR:売り)\n"
        f"  監視足種     : 5分足 (5m) & pybotters WebSocket リアルタイム配信\n"
        f"  銘柄選定周期 : 毎日 {hours_str} JST (8時間ごと)\n"
        f"  ポジション枠 : 最大 {MAX_ACTIVE_POSITIONS} 銘柄同時保有\n"
        f"  買い (BULL)  : 最大3枠ナンピン (1.0x等倍) / 動的MAE損切り\n"
        f"  売り (BEAR)  : 単発1本固定 (ナンピンなし) / 動的MAE損切り\n"
        "==================================================\n"
        "```"
    )
    if "--no-banner" not in sys.argv:
        discord.print_log(start_msg, force_send=True)
    else:
        print("[Banner Skipped by --no-banner]")

    # ========== 【トレード前準備 ステップ1.5】: 実運用損益の取引所 API 直接同期 & 実運用損益表出力 ==========
    try:
        from real_trade_tracker import sync_real_trades_from_bingx_api, print_real_trading_table
        await sync_real_trades_from_bingx_api(mode=mode)
        print_real_trading_table(days=10)
    except Exception as init_pnl_err:
        print(f"[Startup PnL Sync] 起動時実運用損益同期警告: {init_pnl_err}")

    # ========== 【トレード前準備 ステップ2】: 5分足 SSoT 永続ストア同期 (直近65日分/2ヶ月分蓄積) ==========
    from candle_5m_store import sync_all_target_symbols_5m
    discord.print_log("\n📦 【トレード前準備: ステップ2】 固定選定20銘柄 + BTC-USDT の 5分足 SSoT 同期を実行します (過去65日分)...")
    try:
        sync_all_target_symbols_5m(lookback_days=65)
        discord.print_log("✅ 5分足 SSoT ストアの同期が正常に完了しました。")
    except Exception as sync_err:
        discord.print_log(f"⚠️ 5分足 SSoT 同期で警告が発生しました (後続処理を継続): {sync_err}")

    # ========== 【トレード前準備 ステップ3 & 4 & 5】: チャート出力 ➔ MTF乖離判定＆ブレイクアウトTop10 ➔ 個別最適化 ==========
    trade_side, symbol_params_map, best_params, selected_symbols = await run_screening_and_optimization(
        mode, send_charts=True, skip_zip=True
    )

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

    if FORCE_STRATEGY:
        discord.print_log(f"[STRATEGY OVERRIDE] CLI引数により全銘柄の戦略を強制適用します: {FORCE_STRATEGY.upper()}")
        for sym in symbol_params_map:
            symbol_params_map[sym]["strategy"] = FORCE_STRATEGY

    # 実運用トレード実績（直近PnLチャート画像 ＆ 現在の証拠金残高）をDiscordへ送信
    await send_periodic_performance_report(symbol_apis, mode, label="起動時初期レポート")

    # pybotters BingX WebSocket マネージャー初期化 & バックグラウンドストリーミング開始
    from bingx_ws_manager import BingXWSManager
    ws_mgr = BingXWSManager(symbols=list(symbol_apis.keys()))
    await ws_mgr.start()

    logic = logicinstance()
    last_screening_slot = (datetime.now(JST).date(), datetime.now(JST).hour)
    last_summary_hour_slot = None
    cycle_count = 0
    previous_hourly_oi: Dict[str, float] = {}
    trailing_tp_states = load_trailing_tp_states()
    scale_in_states = load_scale_in_states()

    discord.print_log(f"[Phase B] 固定20銘柄 + BTC単独地合い リアルタイム監視ループを開始します (30秒インターバル省エネ仕様 / 監視銘柄: {', '.join(selected_symbols)})")

    tracked_active_positions: Set[str] = set()
    pending_entry_orders: Dict[str, Dict[str, Any]] = {}
    trade_counts: Dict[str, int] = {}
    last_candle_sync_minute: int = -1
    last_trade_time: Dict[str, float] = {}
    last_nanpin_time: Dict[str, float] = {}
    cached_5m_dfs: Dict[str, pd.DataFrame] = {}
    previous_hourly_oi: Dict[str, float] = {}
    last_hourly_slot = None
    cached_positions_map: Dict[str, dict] = {}
    missing_position_counts: Dict[str, int] = {}

    # ========== フェーズB: MTFリアルタイム監視ループ (30秒サイクル省エネ運用) ==========
    while True:
        cycle_count += 1
        now_jst = datetime.now(JST)

        # -------------------------------------------------------------
        # [フェーズ C] 8時間ごと定期銘柄選定 & スロットリセット
        # -------------------------------------------------------------
        today_date = now_jst.date()
        current_slot = (today_date, now_jst.hour)
        is_periodic_reset_time = (
            now_jst.hour in ANALYSIS_HOURS
            and now_jst.minute < 5
            and current_slot != last_screening_slot
        )
        if is_periodic_reset_time:
            periodic_banner = (
                "```text\n"
                " ____  _               __  __\n"
                "| __ )(_)_ __   __ _  \\ \\/ /\n"
                "|  _ \\| | '_ \\ / _` |  \\  / \n"
                "| |_) | | | | | (_| |  /  \\ \n"
                "|____/|_|_| |_|\\__, | /_/\\_\\\n"
                "               |___/        \n"
                "================================\n"
                " PERIODIC SCREENING & OPTIMIZE  \n"
                "================================\n"
                "```\n"
                "🔄 **【BingX 定期銘柄スクリーニング＆パラメータ最適化】**\n"
                "```text\n"
                "==================================================\n"
                f"  実行時刻     : {now_jst.strftime('%Y-%m-%d %H:%M')} JST\n"
                f"  取引口座設定 : {account_mode_str}\n"
                f"  戦略態勢     : 3態勢地合い連動 (BULL:買い / NEUTRAL:待機 / BEAR:売り)\n"
                f"  ローソク足   : 5分足 (5m) & pybotters WebSocket リアルタイム配信\n"
                "==================================================\n"
                "```"
            )
            discord.print_log(periodic_banner, force_send=True)
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

            # 3. 口座全体の全ポジションをスキャンし、選定外でも保有中の銘柄はGraceful Exitとして引き継ぐ
            symbol_apis, symbol_params_map = await audit_and_retain_positions(
                selected_symbols, symbol_apis, symbol_params_map, best_params, mode, old_apis=old_apis, old_params=old_params
            )

            if FORCE_STRATEGY:
                for sym in symbol_params_map:
                    symbol_params_map[sym]["strategy"] = FORCE_STRATEGY

            # 4. ポジションのない旧選定銘柄のメモリ破棄 ＆ 未約定指値キャンセル
            for old_sym in list(old_apis.keys()):
                if old_sym not in symbol_apis:
                    if old_sym in pending_entry_orders:
                        old_api = old_apis.get(old_sym)
                        if old_api:
                            try:
                                await old_api.active_order_cancel()
                                discord.print_log(f"[{old_sym}] スクリーニング除外に伴い、未約定指値を安全にキャンセルしました。")
                            except Exception:
                                pass
                        pending_entry_orders.pop(old_sym, None)
                    old_apis[old_sym] = None
            gc.collect()

            # 5. WebSocket 購読銘柄を最新の symbol_apis に更新
            await ws_mgr.update_symbols(list(symbol_apis.keys()))
            cached_5m_dfs.clear()

            last_screening_slot = current_slot
            discord.print_log(f"[Periodic Reset Complete] 新しい選定銘柄 (LONG ONLY): {', '.join(selected_symbols)} | 現在の全監視対象: {', '.join(symbol_apis.keys())}")

            # 8時間ごとの実運用トレード実績レポート
            await send_periodic_performance_report(symbol_apis, mode, label="8時間定期レポート")

        # -------------------------------------------------------------
        # [フェーズ B1] 毎時タスク (正時00:05 JST: クジラ更新 & OI蓄積 & 毎時サマリー送信)
        # -------------------------------------------------------------
        is_new_hour = (now_jst.hour != last_hourly_slot)
        if is_new_hour and now_jst.minute < 5:
            last_hourly_slot = now_jst.hour
            try:
                whale_script = Path(__file__).resolve().parent / "fetch_whale_sentiment.py"
                if whale_script.exists():
                    try:
                        subprocess.run([sys.executable, str(whale_script)], check=True, timeout=60)
                    except Exception:
                        pass

                # BingX 全対象銘柄の建玉(OI)＆FRスナップショット自動蓄積
                try:
                    from bingx_oi_collector import collect_and_save_oi_snapshot
                    await asyncio.to_thread(collect_and_save_oi_snapshot)
                except Exception as oi_err:
                    discord.print_log(f"[OI Collector Warning] 建玉スナップショット保存警告: {oi_err}", level="debug")

                # 月初（毎月1日）判定 & 月次確定レポート・先月高安(PMH/PML)自動更新
                if now_jst.hour == 0:
                    try:
                        from real_trade_tracker import check_and_send_monthly_report_on_first_day
                        from monthly_key_levels import get_or_update_monthly_key_levels
                        await asyncio.to_thread(check_and_send_monthly_report_on_first_day)
                        await asyncio.to_thread(get_or_update_monthly_key_levels, list(symbol_apis.keys()), MIX_SCRIPT_PATH.parent / "Data")
                    except Exception as m_err:
                        discord.print_log(f"[Monthly Check Warning] 月次処理警告: {m_err}", level="debug")

                # 毎時ステータスサマリーテーブルの作成 & Discord送信
                hourly_summary_rows = []
                first_api = next(iter(symbol_apis.values())) if symbol_apis else None
                h_balance = await first_api.get_account() if first_api else 1000.0

                for h_sym, h_api in symbol_apis.items():
                    h_pos = cached_positions_map.get(h_sym, {})
                    h_qty = float(h_pos.get("buy", 0.0))
                    h_has_long = h_qty > 0
                    h_pnl = float(h_pos.get("profit", 0.0))
                    h_px = ws_mgr.get_latest_price(h_sym) or float(h_pos.get("buy_pos", 0.0))
                    if not h_px or h_px <= 0:
                        df_h = cached_5m_dfs.get(h_sym)
                        if df_h is not None and not df_h.empty and "close" in df_h.columns:
                            h_px = float(df_h["close"].iloc[-1])
                        else:
                            try:
                                bid, _ = get_bingx_orderbook(h_sym, h_api.bingx.base_url if h_api else None)
                                if bid and bid > 0:
                                    h_px = bid
                            except Exception:
                                pass
                    if not h_px or h_px <= 0:
                        h_px = 1.0
                    h_whale_info = get_whale_sentiment_info(h_sym)
                    h_whale_sig = h_whale_info.get("signal", "NEUTRAL")
                    h_whale_tag = "🟢買い" if h_whale_sig == "LONG_ONLY" else ("🔴売り" if h_whale_sig == "SHORT_ONLY" else "⚪中立")

                    if h_has_long:
                        h_sign = "+" if h_pnl >= 0 else "-"
                        h_t_cnt = trade_counts.get(h_sym, 1)
                        if h_t_cnt <= 0:
                            h_t_cnt = 1
                        h_pos_tag = f"保有({h_sign}${abs(h_pnl):.2f}/{h_t_cnt}本)"
                    elif h_sym in pending_entry_orders:
                        p_px = pending_entry_orders[h_sym].get("price", 0.0)
                        h_pos_tag = f"指値(${p_px:.4f})" if p_px < 10 else f"指値(${p_px:.2f})"
                    else:
                        h_pos_tag = "ノーポジ"

                    hourly_summary_rows.append({
                        "sym": h_sym,
                        "price": h_px,
                        "pos": h_pos_tag,
                        "whale": h_whale_tag,
                    })

                if hourly_summary_rows:
                    tbl_header = f"{_pad_display('銘柄', 6)} {_pad_display('現在価格', 8, 'right')} {_pad_display('ポジション', 16)} {_pad_display('クジラ', 6)}"
                    tbl_divider = "────────────────────────────────────────"
                    acct_mode_str = "Live Account" if BINGX_IS_LIVE else "VST Demo Mode"
                    summary_lines = [
                        f"⏰ **【BingX 毎時ステータスサマリー ({now_jst.strftime('%H:00')} JST)】**",
                        f"💰 残高: ${h_balance:,.2f} ({acct_mode_str}) | 監視体制: 5分足 WebSocketリアルタイム稼働中",
                        "```text",
                        tbl_header,
                        tbl_divider,
                    ]
                    for r in hourly_summary_rows:
                        px = r["price"]
                        px_str = f"${px:,.4f}" if px < 10 else (f"${px:,.2f}" if px < 1000 else f"${px:,.1f}")
                        clean_sym = r["sym"].replace("-USDT", "").replace("USDT", "")
                        summary_lines.append(
                            f"{_pad_display(clean_sym, 6)} {_pad_display(px_str, 8, 'right')} {_pad_display(r['pos'], 16)} {_pad_display(r['whale'], 6)}"
                        )
                    summary_lines.append(tbl_divider)
                    next_hour_jst = (now_jst.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).strftime('%H:00')
                    summary_lines.append(f"次回定期更新: {next_hour_jst} JST (1時間ごとに定期配信中)")
                    summary_lines.append("```")
                    discord.print_log("\n".join(summary_lines), force_send=True)
            except Exception as hourly_err:
                discord.print_log(f"[Hourly Update Error] 毎時タスクエラー: {hourly_err}", level="warning")

        # -------------------------------------------------------------
        # [フェーズ B0] リアルタイム (30秒ごと): 全体地合い状態の判定
        # -------------------------------------------------------------
        regime_info = get_regime_gate_info("BTC-USDT")
        overall_regime = regime_info.get("overall_regime", "NEUTRAL")
        is_emergency_be = (overall_regime != "BUY")
        is_regime_safe = (regime_info.get("regime_gate") == "ALLOW")

        # -------------------------------------------------------------
        # [フェーズ B2] リアルタイム (30秒ごと): 全ポジション一括取得 & 監視 & 決済判定
        # -------------------------------------------------------------
        from bingx5_46_2api import fetch_all_positions_dict_bingx
        fresh_positions_map = await fetch_all_positions_dict_bingx(mode=mode)

        # 【多重防御 1: 全銘柄一斉消失サーキットブレーカー】
        # 直前に2銘柄以上保有していたのに突然全件空リストが返ってきた場合、取引所APIの一時グリッチと判定し前回キャッシュを保護
        if cached_positions_map and len(cached_positions_map) >= 2 and fresh_positions_map is not None and len(fresh_positions_map) == 0:
            discord.print_log("🛡️ [Circuit Breaker] 取引所APIが一時的に全件空リストを返却したため、グリッチと判定し前回ポジションを保護維持します。", level="debug")
        elif fresh_positions_map is not None:
            cached_positions_map = fresh_positions_map

        active_positions = {}
        first_api = next(iter(symbol_apis.values())) if symbol_apis else None
        cycle_balance = await first_api.get_account() if first_api else 1000.0

        for sym, api in list(symbol_apis.items()):
            try:
                pos = cached_positions_map.get(sym, {
                    'buy': 0.0, 'sell': 0.0, 'buy_pos': 0.0, 'sell_pos': 0.0, 'profit': 0.0, 'pos_count': 0.0
                })
                buy_qty = float(pos.get("buy", 0.0))
                sell_qty = float(pos.get("sell", 0.0))
                has_long = buy_qty > 0
                has_short = sell_qty > 0
                is_short_pos = has_short and not has_long
                has_pos = has_long or has_short

                pos_qty = sell_qty if is_short_pos else buy_qty
                entry_px = float(pos.get("sell_pos" if is_short_pos else "buy_pos", 0.0))
                pnl_current = float(pos.get("profit", 0.0))

                if not has_pos:
                    if sym in tracked_active_positions:
                        # 【多重防御 2: 連続2回確認デバウンス】
                        miss_cnt = missing_position_counts.get(sym, 0) + 1
                        missing_position_counts[sym] = miss_cnt
                        if miss_cnt >= 2:
                            tracked_active_positions.discard(sym)
                            trade_counts[sym] = 0
                            last_nanpin_time.pop(sym, None)
                            last_trade_time[sym] = time.time()
                            missing_position_counts.pop(sym, None)
                            discord.print_log(f"⚠️ [{sym}] 取引所側でのポジション解消（外部決済または清算）を検知しました。", level="warning")
                    else:
                        missing_position_counts.pop(sym, None)

                    if sym in pending_entry_orders:
                        try:
                            orders = await api.get_open_orders()
                            if not orders:
                                pending_entry_orders.pop(sym, None)
                        except Exception:
                            pass
                    continue
                else:
                    missing_position_counts.pop(sym, None)

                # 保有中: 出しっぱなし指値の約定検知！
                if sym in pending_entry_orders:
                    p_order = pending_entry_orders.pop(sym)
                    trade_counts[sym] = 1
                    last_trade_time[sym] = time.time()
                    last_nanpin_time[sym] = time.time()
                    tracked_active_positions.add(sym)
                    scale_state_new = {
                        "nanpin_count": 0,
                        "last_entry_price": entry_px,
                        "avg_price": entry_px,
                        "initial_entry_time": time.time(),
                        "side": "short" if is_short_pos else "long"
                    }
                    scale_in_states[sym] = scale_state_new
                    save_scale_in_states(scale_in_states)
                    entry_reason_val = p_order.get("reason", "指値約定")
                    opt_inv_p = p_order.get("interval", 5)
                    inv_label_p = f"{opt_inv_p}m" if opt_inv_p < 60 else f"{opt_inv_p // 60}h"

                    # 余剰指値キャンセル
                    try:
                        await api.active_order_cancel()
                    except Exception:
                        pass

                    # エントリー後非同期レポーティング
                    vah_val = getattr(api, "entry_vah", entry_px * 1.02)
                    val_val = getattr(api, "entry_val", entry_px * 0.98)
                    poc_val = (vah_val + val_val) / 2.0
                    whale_info = get_whale_sentiment_info(sym)
                    whale_sig = whale_info.get("signal", "NEUTRAL")
                    whale_flow_val = float(whale_info.get("net_flow", 0.0)) if whale_info else 0.0
                    sym_params = symbol_params_map.get(sym, best_params)
                    cur_strat = str(sym_params.get("strategy", "rsima")).upper()
                    side_label = "SHORT" if is_short_pos else "LONG"
                    strat_desc = f"{side_label} {cur_strat} ({inv_label_p}足 / MP={sym_params.get('mp', '-')})"

                    df_report = cached_5m_dfs.get(sym, pd.DataFrame())
                    asyncio.create_task(_async_post_entry_reporting(
                        sym=sym, entry_px=entry_px, lot_size=pos_qty, entry_full_reason=entry_reason_val,
                        df_chart=df_report, vah_val=vah_val, val_val=val_val, poc_val=poc_val,
                        strat_desc=strat_desc, whale_sig=whale_sig, whale_flow_val=whale_flow_val,
                        interval_label=inv_label_p
                    ))

                tracked_active_positions.add(sym)
                active_positions[sym] = (has_pos, pos, entry_px, pos_qty, pnl_current)

                # リアルタイム現在価格
                cur_px = ws_mgr.get_latest_price(sym)
                if not cur_px or cur_px <= 0:
                    cur_bid, cur_ask = get_bingx_orderbook(sym, api.bingx.base_url)
                    cur_px = cur_bid if cur_bid else entry_px

                # ローソク足取得（キャッシュ優先）
                df_sym = cached_5m_dfs.get(sym)
                if df_sym is None or df_sym.empty:
                    df_raw_init = await fetch_bingx_candles(sym, "5m", limit=3200, mode=mode)
                    if not df_raw_init.empty:
                        cached_5m_dfs[sym] = df_raw_init
                        df_sym = df_raw_init

                if df_sym is None or df_sym.empty or len(df_sym) < 20:
                    continue

                sym_params = symbol_params_map.get(sym, best_params)
                cand_interval = int(sym_params.get("interval", 5) or 5)
                inv_label_p = f"{cand_interval}m" if cand_interval < 60 else f"{cand_interval // 60}h"
                cand_strat = str(sym_params.get("strategy", "rsima")).lower()

                # 採用足リサンプリング
                df_eval = resample_candles(df_sym, cand_interval) if cand_interval > 5 else df_sym.copy()
                df_eval = logic.make_logic(
                    df_eval,
                    market_profile_period=int(sym_params.get("mp", 200)),
                    strategy_type=cand_strat,
                    num_bins=int(sym_params.get("num_bins", 50)),
                    value_area_pct=float(sym_params.get("value_area_pct", 0.70)),
                    lEp=float(sym_params.get("l_ep", 40.0) if not is_short_pos else sym_params.get("s_ep", 60.0)),
                    lCp=float(sym_params.get("l_cp", 60.0) if not is_short_pos else sym_params.get("s_cp", 40.0)),
                )

                vah = float(df_eval["VAH"].iloc[-1]) if "VAH" in df_eval.columns else cur_px * 1.02
                val = float(df_eval["VAL"].iloc[-1]) if "VAL" in df_eval.columns else cur_px * 0.98
                poc = float(df_eval["POC"].iloc[-1]) if "POC" in df_eval.columns else cur_px

                # 保有枠数の厳密算出 (0本バグ防止 & 実ロット連動)
                spec_pos = api.bingx.instrument_spec or {}
                base_lot = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec_pos)
                actual_lots = max(1, int(round(pos_qty / max(base_lot, 1e-6)))) if base_lot > 0 else 1
                scale_state_cur = scale_in_states.get(sym, {})
                cur_nanpin_cnt = int(scale_state_cur.get("nanpin_count", 0))
                t_count = max(trade_counts.get(sym, 1), actual_lots, cur_nanpin_cnt + 1, 1)
                trade_counts[sym] = t_count
                last_px_cur = float(scale_state_cur.get("last_entry_price", entry_px))

                # エントリー経過時間 (分) および 5分足換算バー数 (4時間タイムアウト判定用)
                entry_ts = float(scale_state_cur.get("initial_entry_time", 0.0))
                if entry_ts <= 0:
                    api_update_ms = float(pos.get("updateTime", 0) or 0) if isinstance(pos, dict) else 0.0
                    if api_update_ms > 0:
                        entry_ts = api_update_ms / 1000.0
                    else:
                        entry_ts = time.time()
                    scale_state_cur["initial_entry_time"] = entry_ts
                    scale_in_states[sym] = scale_state_cur
                    save_scale_in_states(scale_in_states)
                elapsed_min = max(0.0, (time.time() - entry_ts) / 60.0)
                bars_held_cur = int(elapsed_min // 5)
                dyn_sl_pct = float(sym_params.get("dynamic_mae_stop", -0.025))

                in_cooldown, rem_h = is_in_sl_cooldown(sym)

                # 地合い変化による緊急建値撤退判定
                # ロング保有中かつ非BUY ➔ 建値+0.20%撤退 / ショート保有中かつ非SELL ➔ 建値-0.20%撤退
                pos_emergency_be = (overall_regime != "SELL") if is_short_pos else (overall_regime != "BUY")

                # 戦略シグナル評価 (ロング・ショート完全分岐)
                if is_short_pos:
                    # ===== SHORT ポジション監視 =====
                    if cand_strat.startswith("vah_poc"):
                        use_ema_v = ("ema" in cand_strat)
                        sig_eval = check_vah_poc_signals(
                            df_5m=df_eval,
                            current_pos_qty=pos_qty,
                            current_trades_count=1,
                            avg_entry_price=entry_px,
                            last_entry_price=last_px_cur,
                            current_price=cur_px,
                            vp_period=int(sym_params.get("mp", 200)),
                            num_bins=int(sym_params.get("num_bins", 50)),
                            value_area_pct=float(sym_params.get("value_area_pct", 0.70)),
                            ma_type=str(sym_params.get("ma_type", "EMA" if use_ema_v else "NONE")),
                            ma_condition=str(sym_params.get("ma_condition", "close_below" if use_ema_v else "none")),
                            max_trades=1,
                            target_ratio=0.50,
                            sl_pct=abs(dyn_sl_pct),
                            is_cooldown_active=in_cooldown,
                            is_emergency_breakeven=pos_emergency_be,
                            dynamic_sl_pct=dyn_sl_pct,
                            bars_held=bars_held_cur,
                            use_ema_filter=use_ema_v,
                        )
                    else:
                        use_ema_r = ("ema" in cand_strat)
                        sig_eval = check_rsima3_short_signals(
                            df_5m=df_eval,
                            current_pos_qty=pos_qty,
                            current_trades_count=1,
                            avg_entry_price=entry_px,
                            current_price=cur_px,
                            rsi_len=9,
                            ema_len=7,
                            s_ep=float(sym_params.get("s_ep", 60.0)),
                            s_cp=float(sym_params.get("s_cp", 40.0)),
                            sl_pct=abs(dyn_sl_pct),
                            max_trades=1,
                            last_entry_price=last_px_cur,
                            val_price=val,
                            vah_price=vah,
                            is_emergency_breakeven=pos_emergency_be,
                            dynamic_sl_pct=dyn_sl_pct,
                            bars_held=bars_held_cur,
                            use_ema_filter=use_ema_r,
                        )
                else:
                    # ===== LONG ポジション監視 =====
                    if cand_strat.startswith("val_poc"):
                        use_ema_v = ("ema" in cand_strat)
                        sig_eval = check_val_poc_signals(
                            df_5m=df_eval,
                            current_pos_qty=buy_qty,
                            current_trades_count=t_count,
                            avg_entry_price=entry_px,
                            last_entry_price=last_px_cur,
                            current_price=cur_px,
                            vp_period=int(sym_params.get("mp", 200)),
                            num_bins=int(sym_params.get("num_bins", 50)),
                            value_area_pct=float(sym_params.get("value_area_pct", 0.70)),
                            ma_type=str(sym_params.get("ma_type", "EMA" if use_ema_v else "NONE")),
                            ma_condition=str(sym_params.get("ma_condition", "close_above" if use_ema_v else "none")),
                            max_trades=MAX_TRADES_COUNT,
                            target_ratio=0.50,
                            sl_pct=abs(dyn_sl_pct),
                            is_cooldown_active=in_cooldown,
                            is_emergency_breakeven=pos_emergency_be,
                            dynamic_sl_pct=dyn_sl_pct,
                            bars_held=bars_held_cur,
                            use_ema_filter=use_ema_v,
                        )
                    else:
                        use_ema_r = ("ema" in cand_strat)
                        sig_eval = check_rsima3_signals(
                            df_5m=df_eval,
                            current_pos_qty=buy_qty,
                            current_trades_count=t_count,
                            avg_entry_price=entry_px,
                            current_price=cur_px,
                            rsi_len=9,
                            ema_len=7,
                            l_ep=float(sym_params.get("l_ep", 40.0)),
                            l_cp=float(sym_params.get("l_cp", 60.0)),
                            sl_pct=abs(dyn_sl_pct),
                            max_trades=MAX_TRADES_COUNT,
                            last_entry_price=last_px_cur,
                            val_price=val,
                            vah_price=vah,
                            is_emergency_breakeven=pos_emergency_be,
                            dynamic_sl_pct=dyn_sl_pct,
                            bars_held=bars_held_cur,
                            use_ema_filter=use_ema_r,
                        )

                pos_action = sig_eval.get("action", "HOLD")
                exit_reason = sig_eval.get("reason", "HOLD")
                whale_info = get_whale_sentiment_info(sym)
                whale_sig = whale_info.get("signal", "NEUTRAL")

                closed = False
                if pos_action == "STOP_LOSS":
                    discord.print_log(f"🛑 [{sym}] [{cand_strat.upper()} DYNAMIC MAE STOP LOSS] {exit_reason} ➔ 成行損切り", level="warning")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    record_sl_cooldown(sym)
                    closed = True
                elif pos_action == "TIMEOUT_EXIT":
                    discord.print_log(f"⏰ [{sym}] [{cand_strat.upper()} 4H TIMEOUT EXIT] {exit_reason} ➔ 4時間タイムアウト成行微損撤退", level="warning")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    closed = True
                elif pos_action == "CLOSE_ALL":
                    discord.print_log(f"🎯 [{sym}] [{cand_strat.upper()} CLOSE ALL] {exit_reason} ➔ 一括成行決済")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    closed = True
                elif pos_action == "ADD_ENTRY" and not is_short_pos:
                    # ナンピンはロングのみ許可 (ショートは単発固定)
                    if pos_emergency_be:
                        pass
                    elif t_count >= MAX_TRADES_COUNT:
                        pass
                    elif time.time() - last_nanpin_time.get(sym, 0) < 60.0:
                        pass
                    else:
                        spec_pos = api.bingx.instrument_spec or {}
                        base_lot_add = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec_pos)
                        nanpin_lot = base_lot_add * float(sig_eval.get("lot_multiplier", 1.0))
                        if nanpin_lot > 0:
                            max_notional_limit = BINGX_TARGET_POSITION_VALUE_USDT * 3.15
                            cur_notional = buy_qty * cur_px
                            add_notional = nanpin_lot * cur_px
                            if (cur_notional + add_notional) <= max_notional_limit:
                                df_add = df_eval.copy()
                                df_add["long"] = True
                                entered_add = await api.long_entry(df_add, pos, cycle_balance, nanpin_lot, max_lot, is_nanpin=True, stage_num=t_count+1)
                                if entered_add:
                                    last_nanpin_time[sym] = time.time()
                                    trade_counts[sym] = t_count + 1
                                    scale_state = scale_in_states.get(sym, {})
                                    scale_state.update({
                                        "nanpin_count": t_count,
                                        "last_entry_price": cur_px,
                                        "avg_price": entry_px,
                                    })
                                    scale_in_states[sym] = scale_state
                                    save_scale_in_states(scale_in_states)
                                    record_real_trade(sym, "LONG", "ADD_ENTRY", cur_px, nanpin_lot, 0.0, f"実運用ナンピン ({t_count}/{MAX_TRADES_COUNT - 1})")
                                    discord.print_log(f"🚀🚀🚀 【ナンピン買い増し約定】 {sym} @ ${cur_px:.4f} (保有枠: {t_count+1}/{MAX_TRADES_COUNT})")

                if closed:
                    trailing_tp_states.pop(sym, None)
                    save_trailing_tp_states(trailing_tp_states)
                    scale_in_states.pop(sym, None)
                    save_scale_in_states(scale_in_states)
                    trade_counts[sym] = 0
                    last_trade_time[sym] = time.time()
                    last_nanpin_time.pop(sym, None)
                    del active_positions[sym]
                    tracked_active_positions.discard(sym)

                    side_tag = "SHORT" if is_short_pos else "LONG"
                    asyncio.create_task(_async_post_exit_reporting(
                        sym=sym, side=side_tag, close_qty=pos_qty, reported_exit_px=cur_px,
                        entry_px=entry_px, pnl=pnl_current, exit_reason=exit_reason,
                        df_chart=df_eval, whale_sig=whale_sig, cycle_balance=cycle_balance,
                        interval_label=inv_label_p
                    ))
            except Exception as pos_err:
                discord.print_log(f"⚠️ [{sym}] ポジション監視例外: {pos_err}", level="warning")

        # -------------------------------------------------------------
        # [フェーズ B3] リアルタイム (30秒ごと): 3態勢地合いディフェンス ＆ 新規エントリー判定
        # -------------------------------------------------------------
        if overall_regime == "NEUTRAL":
            # ===== NEUTRAL (中立相場): 新規エントリー完全停止 ＆ 待機指値即時キャンセル =====
            if cycle_count % 20 == 0:
                discord.print_log(f"⚪ [地合い中立待機] 総合地合い中立(NEUTRAL)のため新規エントリーを完全停止中 (ノーポジ待機・既存ポジション建値早期撤退のみ)", level="info")
            if pending_entry_orders:
                for p_sym in list(pending_entry_orders.keys()):
                    p_api = symbol_apis.get(p_sym)
                    if p_api:
                        try:
                            await p_api.active_order_cancel()
                            discord.print_log(f"🛡️ [中立相場退避] 地合い中立に伴い {p_sym} の未約定指値を安全にキャンセルしました。")
                        except Exception:
                            pass
                pending_entry_orders.clear()
        else:
            # ===== BULL (買い相場: ロング) または BEAR (売り相場: ショート) =====
            is_sell_regime = (overall_regime == "SELL")
            total_active_count = len(active_positions) + len(pending_entry_orders)
            is_candle_closed = (now_jst.minute % 5 == 0 and now_jst.minute != last_candle_sync_minute)
            if is_candle_closed:
                last_candle_sync_minute = now_jst.minute

            for sym in selected_symbols:
                if sym in active_positions:
                    continue

                try:
                    # 確定足タイミングまたは未キャッシュ時はストアから取得
                    if is_candle_closed or sym not in cached_5m_dfs or cached_5m_dfs[sym].empty:
                        df_raw = await fetch_bingx_candles(sym, "5m", limit=3200, mode=mode)
                        if not df_raw.empty:
                            cached_5m_dfs[sym] = df_raw

                    df_5m = cached_5m_dfs.get(sym)
                    if df_5m is None or df_5m.empty or len(df_5m) < 20:
                        continue

                    cur_px = ws_mgr.get_latest_price(sym)
                    if not cur_px or cur_px <= 0:
                        cur_bid, cur_ask = get_bingx_orderbook(sym, api_bingx(symbol=sym, mode=mode).bingx.base_url)
                        cur_px = (cur_ask if is_sell_regime else cur_bid) or float(df_5m["close"].iloc[-1])

                    in_cooldown, rem_h = is_in_sl_cooldown(sym)
                    if in_cooldown:
                        continue

                    # クールダウンチェック (直近20分以内の取引除外)
                    if time.time() - last_trade_time.get(sym, 0) < 1200:
                        continue

                    sym_params_e = symbol_params_map.get(sym, best_params)
                    cand_interval_e = int(sym_params_e.get("interval", 5) or 5)
                    cand_strat_e = str(sym_params_e.get("strategy", "rsima")).lower()

                    df_entry_eval = resample_candles(df_5m, cand_interval_e) if cand_interval_e > 5 else df_5m.copy()
                    df_entry_eval = logic.make_logic(
                        df_entry_eval,
                        market_profile_period=int(sym_params_e.get("mp", 200)),
                        strategy_type=cand_strat_e,
                        num_bins=int(sym_params_e.get("num_bins", 50)),
                        value_area_pct=float(sym_params_e.get("value_area_pct", 0.70)),
                        lEp=float(sym_params_e.get("s_ep", 60.0) if is_sell_regime else sym_params_e.get("l_ep", 40.0)),
                        lCp=float(sym_params_e.get("s_cp", 40.0) if is_sell_regime else sym_params_e.get("l_cp", 60.0)),
                    )

                    vah_e = float(df_entry_eval["VAH"].iloc[-1]) if "VAH" in df_entry_eval.columns else cur_px * 1.02
                    val_e = float(df_entry_eval["VAL"].iloc[-1]) if "VAL" in df_entry_eval.columns else cur_px * 0.98
                    poc_e = float(df_entry_eval["POC"].iloc[-1]) if "POC" in df_entry_eval.columns else cur_px

                    whale_info = get_whale_sentiment_info(sym)
                    whale_sig = whale_info.get("signal", "NEUTRAL")

                    if is_sell_regime:
                        # --------------------------------------------------
                        # 【BEAR相場】ショート新規エントリー判定 (単発1本固定)
                        # --------------------------------------------------
                        if cand_strat_e.startswith("vah_poc"):
                            use_ema_v = ("ema" in cand_strat_e)
                            sig_entry = check_vah_poc_signals(
                                df_5m=df_entry_eval,
                                current_pos_qty=0.0,
                                current_trades_count=0,
                                avg_entry_price=0.0,
                                last_entry_price=0.0,
                                current_price=cur_px,
                                vp_period=int(sym_params_e.get("mp", 200)),
                                num_bins=int(sym_params_e.get("num_bins", 50)),
                                value_area_pct=float(sym_params_e.get("value_area_pct", 0.70)),
                                ma_type=str(sym_params_e.get("ma_type", "EMA" if use_ema_v else "NONE")),
                                ma_condition=str(sym_params_e.get("ma_condition", "close_below" if use_ema_v else "none")),
                                max_trades=1,
                                target_ratio=0.50,
                                sl_pct=float(sym_params_e.get("sl_pct", 0.090)),
                                is_cooldown_active=in_cooldown,
                                use_ema_filter=use_ema_v,
                            )
                        else:
                            use_ema_r = ("ema" in cand_strat_e)
                            sig_entry = check_rsima3_short_signals(
                                df_5m=df_entry_eval,
                                current_pos_qty=0.0,
                                current_trades_count=0,
                                avg_entry_price=0.0,
                                current_price=cur_px,
                                rsi_len=9,
                                ema_len=7,
                                s_ep=float(sym_params_e.get("s_ep", 60.0)),
                                s_cp=float(sym_params_e.get("s_cp", 40.0)),
                                sl_pct=float(sym_params_e.get("sl_pct", 0.090)),
                                max_trades=1,
                                last_entry_price=0.0,
                                val_price=val_e,
                                vah_price=vah_e,
                                use_ema_filter=use_ema_r,
                            )

                        entry_action = sig_entry.get("action", "NONE")
                        entry_reason = sig_entry.get("reason", "INITIAL_ENTRY_SHORT")

                        # ケース 1: すでにショート指値待機中
                        if sym in pending_entry_orders:
                            if is_candle_closed:
                                if entry_action == "INITIAL_ENTRY_SHORT" and whale_sig != "LONG_ONLY":
                                    api = symbol_apis.get(sym)
                                    if api:
                                        spec = api.bingx.instrument_spec or {}
                                        lot_size = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                                        df_re = df_entry_eval.copy()
                                        df_re["short"] = True
                                        entered_re = await api.short_entry(df_re, {}, cycle_balance, lot_size, max_lot)
                                        if entered_re:
                                            new_pos = await api.get_positions()
                                            if float(new_pos.get("sell", 0.0)) > 0:
                                                pending_entry_orders.pop(sym, None)
                                                trade_counts[sym] = 1
                                                last_trade_time[sym] = time.time()
                                                tracked_active_positions.add(sym)
                                            else:
                                                pending_entry_orders[sym]["price"] = cur_px
                                                discord.print_log(f"📌 [{sym}] 新しいショート指値に更新完了 (Best Ask 待機継続: 約 ${cur_px:.4f})")
                                else:
                                    api = symbol_apis.get(sym)
                                    if api:
                                        try:
                                            await api.active_order_cancel()
                                        except Exception:
                                            pass
                                    pending_entry_orders.pop(sym, None)
                                    discord.print_log(f"🛡️ [{sym}] 5分足SHORTサインが終了したため、未約定指値を安全にキャンセルしました。")
                            continue

                        # ケース 2: 新規ショート指値発注 (Maker Best Ask)
                        if total_active_count >= MAX_ACTIVE_POSITIONS:
                            continue
                        if entry_action != "INITIAL_ENTRY_SHORT":
                            continue
                        if whale_sig == "LONG_ONLY":
                            continue

                        api = symbol_apis.get(sym)
                        if not api:
                            api = api_bingx(symbol=sym, mode=mode)
                            spec = fetch_instrument_spec_bingx(sym, mode)
                            if spec:
                                api.update_instrument_spec(spec)
                            symbol_apis[sym] = api

                        spec = api.bingx.instrument_spec or {}
                        lot_size = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                        if lot_size <= 0:
                            continue

                        pos = await api.get_positions()
                        if float(pos.get("sell", 0.0)) > 0:
                            continue

                        api.entry_val = val_e
                        api.entry_vah = vah_e

                        discord.print_log(f"⚡ [{sym}] [{cand_strat_e.upper()} 初期ショートエントリー>>] {entry_reason} (クジラ: {whale_sig})")
                        df_entry_send = df_entry_eval.copy()
                        df_entry_send["short"] = True
                        entered = await api.short_entry(df_entry_send, pos, cycle_balance, lot_size, max_lot)

                        if entered:
                            new_pos = await api.get_positions()
                            if float(new_pos.get("sell", 0.0)) > 0:
                                last_trade_time[sym] = time.time()
                                trade_counts[sym] = 1
                                last_nanpin_time[sym] = time.time()
                                tracked_active_positions.add(sym)
                                scale_state_new = {
                                    "nanpin_count": 0,
                                    "last_entry_price": cur_px,
                                    "avg_price": cur_px,
                                    "initial_entry_time": time.time(),
                                    "side": "short"
                                }
                                scale_in_states[sym] = scale_state_new
                                save_scale_in_states(scale_in_states)
                                total_active_count += 1
                                inv_label_e = f"{cand_interval_e}m" if cand_interval_e < 60 else f"{cand_interval_e // 60}h"
                                whale_flow_val = float(whale_info.get("net_flow", 0.0)) if whale_info else 0.0
                                strat_desc = f"SHORT {cand_strat_e.upper()} ({inv_label_e}足 / MP={sym_params_e.get('mp', '-')})"
                                asyncio.create_task(_async_post_entry_reporting(
                                    sym=sym, entry_px=cur_px, lot_size=lot_size, entry_full_reason=entry_reason,
                                    df_chart=df_entry_eval, vah_val=vah_e, val_val=val_e, poc_val=poc_e,
                                    strat_desc=strat_desc, whale_sig=whale_sig, whale_flow_val=whale_flow_val,
                                    interval_label=inv_label_e
                                ))
                            else:
                                pending_entry_orders[sym] = {
                                    "placed_at": time.time(),
                                    "price": cur_px,
                                    "lot": lot_size,
                                    "reason": entry_reason,
                                    "interval": cand_interval_e,
                                    "side": "short"
                                }
                                total_active_count += 1
                                discord.print_log(f"📌 [{sym}] Best Ask ショート指値を配置し待機中 (数量={lot_size}, 価格=~${cur_px:.4f})")

                    else:
                        # --------------------------------------------------
                        # 【BULL相場】ロング新規エントリー判定 (最大3枠ナンピン)
                        # --------------------------------------------------
                        if cand_strat_e.startswith("val_poc"):
                            use_ema_v = ("ema" in cand_strat_e)
                            sig_entry = check_val_poc_signals(
                                df_5m=df_entry_eval,
                                current_pos_qty=0.0,
                                current_trades_count=0,
                                avg_entry_price=0.0,
                                last_entry_price=0.0,
                                current_price=cur_px,
                                vp_period=int(sym_params_e.get("mp", 200)),
                                num_bins=int(sym_params_e.get("num_bins", 50)),
                                value_area_pct=float(sym_params_e.get("value_area_pct", 0.70)),
                                ma_type=str(sym_params_e.get("ma_type", "EMA" if use_ema_v else "NONE")),
                                ma_condition=str(sym_params_e.get("ma_condition", "close_above" if use_ema_v else "none")),
                                max_trades=MAX_TRADES_COUNT,
                                target_ratio=0.50,
                                sl_pct=float(sym_params_e.get("sl_pct", 0.090)),
                                is_cooldown_active=in_cooldown,
                                use_ema_filter=use_ema_v,
                            )
                        else:
                            use_ema_r = ("ema" in cand_strat_e)
                            sig_entry = check_rsima3_signals(
                                df_5m=df_entry_eval,
                                current_pos_qty=0.0,
                                current_trades_count=0,
                                avg_entry_price=0.0,
                                current_price=cur_px,
                                rsi_len=9,
                                ema_len=7,
                                l_ep=float(sym_params_e.get("l_ep", 40.0)),
                                l_cp=float(sym_params_e.get("l_cp", 60.0)),
                                sl_pct=float(sym_params_e.get("sl_pct", 0.090)),
                                max_trades=MAX_TRADES_COUNT,
                                last_entry_price=0.0,
                                val_price=val_e,
                                vah_price=vah_e,
                                use_ema_filter=use_ema_r,
                            )

                        entry_action = sig_entry.get("action", "NONE")
                        entry_reason = sig_entry.get("reason", "INITIAL_ENTRY")

                        # ケース 1: すでにロング指値待機中
                        if sym in pending_entry_orders:
                            if is_candle_closed:
                                if entry_action == "INITIAL_ENTRY" and whale_sig != "SHORT_ONLY":
                                    api = symbol_apis.get(sym)
                                    if api:
                                        spec = api.bingx.instrument_spec or {}
                                        lot_size = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                                        df_re = df_entry_eval.copy()
                                        df_re["long"] = True
                                        entered_re = await api.long_entry(df_re, {}, cycle_balance, lot_size, max_lot)
                                        if entered_re:
                                            new_pos = await api.get_positions()
                                            if float(new_pos.get("buy", 0.0)) > 0:
                                                pending_entry_orders.pop(sym, None)
                                                trade_counts[sym] = 1
                                                last_trade_time[sym] = time.time()
                                                tracked_active_positions.add(sym)
                                            else:
                                                pending_entry_orders[sym]["price"] = cur_px
                                                discord.print_log(f"📌 [{sym}] 新しい指値に更新完了 (Best Bid 待機継続: 約 ${cur_px:.4f})")
                                else:
                                    api = symbol_apis.get(sym)
                                    if api:
                                        try:
                                            await api.active_order_cancel()
                                        except Exception:
                                            pass
                                    pending_entry_orders.pop(sym, None)
                                    discord.print_log(f"🛡️ [{sym}] 5分足LONGサインが終了したため、未約定指値を安全にキャンセルしました (下落ナイフ防止)。")
                            continue

                        # ケース 2: 新規ロング指値発注 (Maker Best Bid)
                        if total_active_count >= MAX_ACTIVE_POSITIONS:
                            continue
                        if entry_action != "INITIAL_ENTRY":
                            continue
                        if whale_sig == "SHORT_ONLY":
                            continue

                        api = symbol_apis.get(sym)
                        if not api:
                            api = api_bingx(symbol=sym, mode=mode)
                            spec = fetch_instrument_spec_bingx(sym, mode)
                            if spec:
                                api.update_instrument_spec(spec)
                            symbol_apis[sym] = api

                        spec = api.bingx.instrument_spec or {}
                        lot_size = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                        if lot_size <= 0:
                            continue

                        pos = await api.get_positions()
                        if float(pos.get("buy", 0.0)) > 0:
                            continue

                        api.entry_val = val_e
                        api.entry_vah = vah_e

                        discord.print_log(f"⚡ [{sym}] [{cand_strat_e.upper()} 初期エントリー>>] {entry_reason} (クジラ: {whale_sig})")
                        df_entry_send = df_entry_eval.copy()
                        df_entry_send["long"] = True
                        entered = await api.long_entry(df_entry_send, pos, cycle_balance, lot_size, max_lot)

                        if entered:
                            new_pos = await api.get_positions()
                            if float(new_pos.get("buy", 0.0)) > 0:
                                last_trade_time[sym] = time.time()
                                trade_counts[sym] = 1
                                last_nanpin_time[sym] = time.time()
                                tracked_active_positions.add(sym)
                                scale_state_new = {
                                    "nanpin_count": 0,
                                    "last_entry_price": cur_px,
                                    "avg_price": cur_px,
                                    "initial_entry_time": time.time(),
                                    "side": "long"
                                }
                                scale_in_states[sym] = scale_state_new
                                save_scale_in_states(scale_in_states)
                                total_active_count += 1
                                inv_label_e = f"{cand_interval_e}m" if cand_interval_e < 60 else f"{cand_interval_e // 60}h"
                                whale_flow_val = float(whale_info.get("net_flow", 0.0)) if whale_info else 0.0
                                strat_desc = f"{cand_strat_e.upper()} ({inv_label_e}足 / MP={sym_params_e.get('mp', '-')})"
                                asyncio.create_task(_async_post_entry_reporting(
                                    sym=sym, entry_px=cur_px, lot_size=lot_size, entry_full_reason=entry_reason,
                                    df_chart=df_entry_eval, vah_val=vah_e, val_val=val_e, poc_val=poc_e,
                                    strat_desc=strat_desc, whale_sig=whale_sig, whale_flow_val=whale_flow_val,
                                    interval_label=inv_label_e
                                ))
                            else:
                                pending_entry_orders[sym] = {
                                    "placed_at": time.time(),
                                    "price": cur_px,
                                    "lot": lot_size,
                                    "reason": entry_reason,
                                    "interval": cand_interval_e,
                                    "side": "long"
                                }
                                total_active_count += 1
                                discord.print_log(f"📌 [{sym}] Best Bid 指値を配置し待機中 (数量={lot_size}, 価格=~${cur_px:.4f})")
                except Exception as ent_err:
                    discord.print_log(f"⚠️ [{sym}] 新規エントリー判定例外: {ent_err}", level="warning")

        # --- ハートビート記録 (Data/bot_heartbeat.json) ---
        try:
            hb_file = Path(__file__).resolve().parent / "Data" / "bot_heartbeat.json"
            hb_file.parent.mkdir(parents=True, exist_ok=True)
            hb_data = {
                "timestamp": time.time(),
                "datetime_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                "cycle_count": cycle_count,
                "monitored_symbols": list(symbol_apis.keys()),
                "active_positions_count": len(active_positions),
                "active_symbols": list(active_positions.keys()),
                "pending_entry_count": len(pending_entry_orders),
                "pending_symbols": list(pending_entry_orders.keys()),
            }
            with open(hb_file, "w", encoding="utf-8") as f:
                json.dump(hb_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        # --loop フラグがない場合は1サイクルのみ実行
        if "--loop" not in sys.argv:
            print("\n==================================================")
            print("  [Success] 1サイクルの選定・最適化・トレード判定が完走しました。")
            print("==================================================")
            break

        # 省エネ30秒待機 (CPU使用率5%以下を維持)
        await asyncio.sleep(30)


if __name__ == "__main__":
    mode = 'live' if BINGX_IS_LIVE else 'demo'
    max_lot = 10.0
    interval = '5'
    try:
        asyncio.run(start(mode, max_lot, interval))
    except KeyboardInterrupt:
        print("\nProgram stopped by user.")

