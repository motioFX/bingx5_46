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


def _sanitize_for_json(obj: Any) -> Any:
    """pandas DataFrame, Series, numpy型などを再帰的にJSONシリアライズ可能なPython基本型に変換・除外する"""
    if isinstance(obj, dict):
        return {
            str(k): _sanitize_for_json(v)
            for k, v in obj.items()
            if k != "chart_df" and not (hasattr(v, "__class__") and v.__class__.__name__ in ("DataFrame", "Series"))
        }
    elif isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(x) for x in obj]
    elif hasattr(obj, "__class__") and obj.__class__.__name__ in ("DataFrame", "Series"):
        return None
    elif hasattr(obj, "item"):
        return obj.item()
    return obj


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
ALLOW_NY_SESSION_LIVE: bool = True  # 本番口座(Live)でも米国セッション(22:00〜翌09:00 JST)をバックテストSSoT準拠で24時間稼働
USE_F7_TWO_PILLAR_MODE: bool = True  # F7 Two-Pillar 戦略体系 (val_poc_30m + h3b_squeeze_1h) SSoT モード

from strategy_f7_two_pillar import (
    F7_LONG_STRATEGY,
    F7_MODE_NAME,
    F7_SHORT_STRATEGY,
    calc_atr_series as _f7_calc_atr_series,
    check_f7_no_exhaustion,
    compute_h3b_squeeze_1h_entry_levels,
    compute_insurance_stop_price,
    compute_val_poc_30m_entry_levels,
    evaluate_h3b_squeeze_1h_entry,
    evaluate_h3b_squeeze_1h_exit,
    evaluate_val_poc_30m_entry,
    evaluate_val_poc_30m_exit,
    format_f7_hourly_status_line,
    is_eu_session_jst,
    is_f7_strategy,
    should_update_insurance_stop,
)

# [ 3 ] ポジション・ロット設定 (PROJECT_RULES.md 準拠)
BINGX_TARGET_POSITION_VALUE_USDT = 15.0  # 目標投資額 $15.0 USDT (1枠基準)
TARGET_POSITION_VALUE_USDT = BINGX_TARGET_POSITION_VALUE_USDT
LEVERAGE_FACTOR = 10.0
MAX_ACTIVE_POSITIONS: int = 2  # 最大同時保有ポジション数 (資金効率と分散を最適化)
MAX_SELECTED_SYMBOLS: int = 20  # 最大選定・監視銘柄数 (Hyperliquid完全統一: バックテスト合格全銘柄を同時監視)

# [ 4 ] ナンピン数設定 (初期1回 + ナンピン最大2回 = 最大3枠厳守)
# [ 4 ] ナンピン完全撤廃・全戦略単発1本固定 (PROJECT_RULES.md 準拠: max_trades=1)
MAX_TRADES_COUNT: int = 1

# [ 5 ] 固定選定銘柄 (主要アルトコイン20銘柄 / Hyperliquid hype5_45 完全統一)
from symbol_universe import FIXED_TRADABLE_SYMBOLS, EXCLUDED_TRADE_MEGA_CAPS

FIXED_SYMBOLS: List[str] = list(FIXED_TRADABLE_SYMBOLS)

# [ 6 ] 定期銘柄選定・リセット時刻（JST時間: 0〜23時）
#       デフォルト: [1, 5, 9, 13, 17, 21] (4時間ごと / 節目 01,09,17 JST + 中間監視 + 毎時軽量地合い反転検知)。
#       CLI引数 (--analysis-hours / --reset-hours / --reset-hour) での上書き指定も可能。
ANALYSIS_HOURS: List[int] = [1, 5, 9, 13, 17, 21]
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
if __name__ == "__main__" and any(arg in sys.argv for arg in ("--status", "--symbols", "-s")):
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
    check_vah_break_signals,
    check_val_break_signals,
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

# ==============================================================================
# 🛡️ 日次リスク管理規約 (回数制限解除 ＆ 日次損失サーキットブレーカー - PROJECT_RULES 準拠)
# ==============================================================================
DAILY_TRADE_STATE_FILE = Path(__file__).resolve().parent / "Data" / "daily_trade_state.json"
DAILY_MAX_TRADES = 0  # 0=1日トレード回数制限なし (回数制限解除 / 最大2枠・損失CB -$1.50・24h冷却なし)
DAILY_MAX_LOSS_USDT = 1.50  # 1日最大許容損失額 (目標投資額 $15.0 の 10% 到達で即座に当日新規停止)
# 🚀 中堅アルト ＆ 出来高急増 (Volume Surge 1.5倍) ゲート規約 (PROJECT_RULES 準拠)
# 直近検証においてメガキャップ3銘柄をエントリー対象外として統一（地合い投票は23票維持）
EXCLUDE_MEGAS_FOR_SPEED = True
EXCLUDED_MEGA_CAPS = list(EXCLUDED_TRADE_MEGA_CAPS)
MIN_VOLUME_SURGE_RATIO = 1.5  # 直近20本平均比 1.5倍以上必須 (日次勝率 57.1% / PF 2.13 実証最適値)

def get_current_trading_day() -> str:
    """
    東京セッション開始時刻 (09:00 JST = 00:00 UTC) を境にした営業日文字列 (YYYY-MM-DD) を取得。
    09:00 JST 〜 翌日 08:59 JST が同一営業日となる。
    """
    now_utc = datetime.now(timezone.utc)
    return now_utc.strftime("%Y-%m-%d")

def load_daily_trade_state() -> Dict[str, Any]:
    """当日の日次トレード状態を読み込む。翌朝09:00 JSTを跨いでいれば自動リセット。"""
    cur_day = get_current_trading_day()
    default_state = {
        "trade_date": cur_day,
        "trades_count": 0,
        "daily_pnl_usdt": 0.0,
        "circuit_broken": False,
        "last_exit_time": None,
        "last_pnl": 0.0,
        "last_symbol": None,
        "last_side": None,
        "last_reason": None,
    }
    DAILY_TRADE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DAILY_TRADE_STATE_FILE.exists():
        try:
            with open(DAILY_TRADE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(default_state, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return default_state

    try:
        with open(DAILY_TRADE_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("trade_date") != cur_day:
            state = default_state
            with open(DAILY_TRADE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            limit_desc = f"{DAILY_MAX_TRADES}回" if DAILY_MAX_TRADES > 0 else "なし"
            discord.print_log(f"🌅 [新営業日開始] 09:00 JST到達により日次トレード集計および損失サーキットブレーカーをリセットしました (回数制限: {limit_desc} / 最大許容損失: -${DAILY_MAX_LOSS_USDT:.2f})")
        return state
    except Exception as e:
        discord.print_log(f"⚠️ 日次トレード状態読み込みエラー: {e} -> デフォルト初期化", level="warning")
        return default_state

def record_daily_trade_completion(symbol: str, side: str, pnl: float, reason: str):
    """ポジション決済完了時に呼び出し、当日のトレード回数および累積PnLを加算して永続化する"""
    try:
        state = load_daily_trade_state()
        state["trades_count"] = int(state.get("trades_count", 0)) + 1
        current_daily_pnl = float(state.get("daily_pnl_usdt", 0.0)) + float(pnl)
        state["daily_pnl_usdt"] = round(current_daily_pnl, 4)
        if current_daily_pnl <= -DAILY_MAX_LOSS_USDT:
            state["circuit_broken"] = True

        now_jst_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S JST")
        state["last_exit_time"] = now_jst_str
        state["last_symbol"] = symbol
        state["last_side"] = side
        state["last_pnl"] = float(pnl)
        state["last_reason"] = reason
        with open(DAILY_TRADE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        if state.get("circuit_broken"):
            cb_warning = f" 🚨【損失サーキットブレーカー発動】累計損失 -${abs(current_daily_pnl):.2f} 到達のため翌朝09:00 JSTまで新規エントリーを物理遮断します！"
        else:
            cb_warning = ""
        count_desc = f"{state['trades_count']}/{DAILY_MAX_TRADES}回完了" if DAILY_MAX_TRADES > 0 else f"本日{state['trades_count']}回完了 / 回数制限なし"
        discord.print_log(
            f"📝 [日次トレード記録] 本日のトレード完了を記録しました: "
            f"({count_desc}, 本日累計PnL: ${current_daily_pnl:+.2f}) {symbol} {side} PnL: {pnl:+.2f} USDT ({reason}){cb_warning}"
        )
    except Exception as e:
        discord.print_log(f"⚠️ 日次トレード状態保存エラー: {e}", level="warning")

def can_open_new_trade_today(unrealized_pnl: float = 0.0) -> Tuple[bool, str, int]:
    """当日の新規エントリーが可能か判定する (可能フラグ, 理由文字列, 当日完了回数、含み損考慮)"""
    state = load_daily_trade_state()
    cnt = int(state.get("trades_count", 0))
    daily_pnl = float(state.get("daily_pnl_usdt", 0.0))
    effective_loss = daily_pnl + min(0.0, float(unrealized_pnl))
    if state.get("circuit_broken", False) or effective_loss <= -DAILY_MAX_LOSS_USDT:
        return False, f"🚨 日次損失サーキットブレーカー発動中 (本日実現+含み損: ${effective_loss:+.2f} <= -${DAILY_MAX_LOSS_USDT:.2f} USDT) - 翌朝09:00 JSTまで新規エントリー完全遮断", cnt
    if DAILY_MAX_TRADES > 0 and cnt >= DAILY_MAX_TRADES:
        return False, f"本日トレード上限到達 ({cnt}/{DAILY_MAX_TRADES}回完了) - 翌朝09:00 JSTまで新規エントリー休止", cnt
    count_desc = f"{cnt}/{DAILY_MAX_TRADES}回" if DAILY_MAX_TRADES > 0 else f"回数制限なし / 本日{cnt}回完了"
    return True, f"本日トレード可能 ({count_desc}, 実効PnL: ${effective_loss:+.2f})", cnt

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
        "regime_strength": {},
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
            "strength_tier": str((data.get("regime_strength") or {}).get("tier", data.get("strength_tier", "BALANCED"))).upper(),
            "regime_pattern": data.get("regime_pattern", ""),
            "allowed_strategies": data.get("allowed_strategies", []),
            "regime_strength": data.get("regime_strength", {}),
            "excluded_symbols": list(excluded),
            "btc_ret14d": float(data.get("btc_ret14d", 0.0) or 0.0),
            "sym_ret14d_map": dict(data.get("sym_ret14d_map") or {}),
            "h3b_wf90d": dict(data.get("h3b_wf90d") or {}),
            "is_buy_regime": is_buy,
            "is_sell_regime": is_sell,
            "is_peak_dropped": is_peak_dropped,
        }
    except Exception:
        return default_res


def _load_f7_wf90d_paused(regime_info_dict: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
    """F7 H3b 因果 90日 WF 休止フラグを market_state または h3b_wf90d_state.json から取得する."""
    wf_d = (regime_info_dict or {}).get("h3b_wf90d")
    if isinstance(wf_d, dict) and "paused" in wf_d:
        return bool(wf_d.get("paused", False)), wf_d
    wf_file = MIX_SCRIPT_PATH.parent / "Data" / "h3b_wf90d_state.json"
    if wf_file.exists():
        try:
            loaded = json.loads(wf_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return bool(loaded.get("paused", False)), loaded
        except Exception:
            pass
    return False, {"paused": False, "window_trades": 0, "window_net": 0.0, "window_pf": 0.0}


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
    """5分足 SSoT ストアから最新のローソク足を取得（確定足のみを抽出 / >=4800本マージ対応）"""
    from candle_5m_store import sync_symbol_5m_candles, load_5m_candles, resample_from_5m
    try:
        gran_upper = str(granularity or "5m").upper()
        df_sync = sync_symbol_5m_candles(symbol, target_days=3)
        if int(limit or 0) > 800 or gran_upper in ("30M", "1H"):
            df_hist = load_5m_candles(symbol)
            if df_hist is not None and not df_hist.empty and df_sync is not None and not df_sync.empty:
                df = pd.concat([df_hist, df_sync], ignore_index=True)
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
            elif df_hist is not None and not df_hist.empty:
                df = df_hist
            else:
                df = df_sync
        else:
            df = df_sync if (df_sync is not None and not df_sync.empty) else load_5m_candles(symbol)

        if df is not None and not df.empty:
            iv_min = {"15M": 15, "30M": 30, "1H": 60, "60M": 60}.get(gran_upper, 5)
            if iv_min > 5:
                df = resample_from_5m(df, interval_minutes=iv_min)

            df = df.tail(limit).copy().reset_index(drop=True)

            now_utc = datetime.now(timezone.utc)
            gran_sec = iv_min * 60
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

    # 3態勢地合い連動モデルに基づく候補選定 & トレード態勢決定 (実証最適・初動エッジ特化4大規約)
    reg_pat = market_state_info.get("regime_pattern", "")
    allowed_strats = market_state_info.get("allowed_strategies", [])

    if overall_reg == "BUY":
        actual_side = "long"
        raw_cands = [
            s for s in recommended_symbols
            if get_clean_symbol(s) != "BTC" and get_clean_symbol(s) not in excluded_clean
        ]
        if not raw_cands:
            period_res = market_state_info.get("period_results", {})
            any_buy_passed = set()
            for w_info in period_res.values():
                for s in w_info.get("buy_passed", []):
                    cl_s = get_clean_symbol(s)
                    if cl_s != "BTC" and cl_s not in excluded_clean:
                        any_buy_passed.add(cl_s)
            raw_cands = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) in any_buy_passed]
        if not raw_cands:
            raw_cands = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) not in excluded_clean]

        # 🚀 【PROJECT_RULES 準拠】 超大型銘柄事前除外オプション (SOL, DOGE, XRP: 直近不合格データに基づくスクリーニング高速化)
        excluded_megas = set(EXCLUDED_MEGA_CAPS + [get_clean_symbol(x) for x in EXCLUDED_MEGA_CAPS]) if EXCLUDE_MEGAS_FOR_SPEED else set()
        all_candidates = []
        for cand in raw_cands:
            cand_clean = get_clean_symbol(cand)
            if cand in excluded_megas or cand_clean in excluded_megas:
                continue
            all_candidates.append(cand)

        discord.print_log(
            f"【全体地合い判定: BUY 🟢】買い推奨中堅アルト銘柄（計 {len(all_candidates)} 銘柄: {', '.join(all_candidates) if all_candidates else 'なし'} / 超大型除外済）"
            f"のロングPure3大戦略個別最適化（108通り: 単発厳選 ➔ 3枠ナンピン検証 ※ブレイクは単発固定）を開始します..."
        )
    elif overall_reg == "SELL":
        actual_side = "short"
        raw_cands = [
            s for s in recommended_symbols
            if get_clean_symbol(s) != "BTC" and get_clean_symbol(s) not in excluded_clean
        ]
        if not raw_cands:
            # 3期間完全合致が0件の場合、売り票を獲得した銘柄から補完選定
            period_res = market_state_info.get("period_results", {})
            any_sell_passed = set()
            for w_info in period_res.values():
                for s in w_info.get("sell_passed", []):
                    cl_s = get_clean_symbol(s)
                    if cl_s != "BTC" and cl_s not in excluded_clean:
                        any_sell_passed.add(cl_s)
            raw_cands = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) in any_sell_passed]
        if not raw_cands:
            # 売り票合致も0件の場合、固定20銘柄から売り除外(底打ち反発)を除いた銘柄をバックテスト対象とする
            raw_cands = [s for s in FIXED_SYMBOLS if get_clean_symbol(s) not in excluded_clean]

        # 🚀 【PROJECT_RULES 準拠】 超大型銘柄事前除外オプション (SOL, DOGE, XRP: 直近不合格データに基づくスクリーニング高速化)
        excluded_megas = set(EXCLUDED_MEGA_CAPS + [get_clean_symbol(x) for x in EXCLUDED_MEGA_CAPS]) if EXCLUDE_MEGAS_FOR_SPEED else set()
        all_candidates = []
        for cand in raw_cands:
            cand_clean = get_clean_symbol(cand)
            if cand in excluded_megas or cand_clean in excluded_megas:
                continue
            all_candidates.append(cand)

        discord.print_log(
            f"【全体地合い判定: SELL 🔴】売り推奨中堅アルト銘柄（計 {len(all_candidates)} 銘柄: {', '.join(all_candidates) if all_candidates else 'なし'} / 超大型除外済）"
            f"のショートPure3大戦略個別最適化（108通り: 単発1本固定・ナンピン完全なし）を開始します..."
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

        # 1. SSoT 5分足時系列ストアから取得 (1H足/日足生成は廃止し5分足ストアから直接個別最適化)
        cand_df_5m = load_5m_candles(cand)
        if cand_df_5m.empty or len(cand_df_5m) < 500:
            cand_df_5m = await asyncio.to_thread(sync_symbol_5m_candles, cand, 35)

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

                pnl_7d = float(best_params_dict.get("pnl_recent_7d", 0.0))
                cnt_7d = int(best_params_dict.get("trades_recent_7d", 0))
                pnl_1st = float(best_params_dict.get("pnl_first_half", 0.0))
                pnl_2nd = float(best_params_dict.get("pnl_second_half", 0.0))

                rng_params = best_params_dict.get("range_params", {})
                brk_params = best_params_dict.get("breakout_params", {})
                is_rng_qual = bool(best_params_dict.get("is_range_qualified", rng_params.get("is_qualified", False)))
                is_brk_qual = bool(best_params_dict.get("is_breakout_qualified", brk_params.get("is_qualified", False)))
                rng_q_lvl = str(best_params_dict.get("range_qual_level", rng_params.get("qual_level", "UNQUALIFIED")))
                brk_q_lvl = str(best_params_dict.get("breakout_qual_level", brk_params.get("qual_level", "UNQUALIFIED")))

                cand_params = {
                    **best_params_dict,
                    "strategy": cand_strat,
                    "interval": cand_inv,
                    "mp": cand_mp,
                    "margin": cand_margin,
                    "pnl": cand_pnl,
                    "trade_count": trade_cnt,
                    "win_rate": win_rt,
                    "max_dd": max_dd,
                    "single_pnl": best_params_dict.get("single_pnl", cand_pnl),
                    "single_trades": best_params_dict.get("single_trades", trade_cnt),
                    "single_win_rate": best_params_dict.get("single_win_rate", win_rt),
                    "pnl_first_half": pnl_1st,
                    "pnl_second_half": pnl_2nd,
                    "pnl_recent_7d": pnl_7d,
                    "trades_recent_7d": cnt_7d,
                    "range_params": rng_params,
                    "breakout_params": brk_params,
                    "is_range_qualified": is_rng_qual,
                    "is_breakout_qualified": is_brk_qual,
                    "range_qual_level": rng_q_lvl,
                    "breakout_qual_level": brk_q_lvl,
                    "is_qualified": bool(is_rng_qual or is_brk_qual or best_params_dict.get("is_qualified", False)),
                    "chart_df": best_params_dict.get("chart_df"),
                    "params": best_params_dict,
                }
                symbol_params_map[cand] = cand_params

                # 4大完全必須条件 (東京レンジ または 欧州ブレイク のいずれかが合格していれば監視対象に選抜)
                # 🚀 【W3 強化】直近7日取引回数を最低2回以上 (cnt_7d >= 2) へ強化し、1回限りの偶発的トレードによる「見せかけTier 1」を排除
                # 4大完全必須条件 (Triple-Stage Recency Gate 準拠: cnt_7d >= 1)
                is_tier1 = bool(
                    trade_cnt >= 10
                    and pnl_1st > 0.0
                    and pnl_2nd > pnl_1st
                    and pnl_7d > 0.0
                    and cnt_7d >= 1
                )
                is_tier2 = bool(
                    trade_cnt >= 10
                    and pnl_1st > 0.0
                    and pnl_2nd > pnl_1st
                )
                # 初動エッジ特化: N,N,B (上放れ初動) はブレイクアウト順張りのみ許可 (レンジ逆張り除外)
                if allowed_strats == ["vah_break"]:
                    is_qual = bool(is_brk_qual)
                    cand_params["strategy"] = "vah_break"
                    cand_params["params"] = brk_params
                else:
                    is_qual = bool(is_rng_qual or is_brk_qual or is_tier1 or is_tier2)

                # デュアル傾向表示文字列の構築
                rng_st_str = str(rng_params.get("strategy", "-")).upper()
                rng_iv_str = f"{rng_params.get('interval', 5)}m"
                rng_pnl_v = float(rng_params.get("pnl", 0.0))
                rng_tr_v = int(rng_params.get("trade_count", 0))
                rng_wr_v = float(rng_params.get("win_rate", 0.0))
                rng_badge = f"🟢合格({rng_q_lvl})" if is_rng_qual else "🔴未達"

                brk_st_str = str(brk_params.get("strategy", "-")).upper()
                brk_iv_str = f"{brk_params.get('interval', 5)}m"
                brk_pnl_v = float(brk_params.get("pnl", 0.0))
                brk_tr_v = int(brk_params.get("trade_count", 0))
                brk_wr_v = float(brk_params.get("win_rate", 0.0))
                brk_badge = f"🟢合格({brk_q_lvl})" if is_brk_qual else "🔴未達"

                dual_tendency_str = (
                    f"🗼東京レンジ[{rng_badge}: {rng_st_str}({rng_iv_str}) ${rng_pnl_v:+.2f}/{rng_tr_v}回/{rng_wr_v:.0f}%] | "
                    f"🏰欧州ブレイク[{brk_badge}: {brk_st_str}({brk_iv_str}) ${brk_pnl_v:+.2f}/{brk_tr_v}回/{brk_wr_v:.0f}%]"
                )

                if is_qual:
                    if is_rng_qual and is_brk_qual:
                        qual_tag = "両刀合格(東京レンジ+欧州ブレイク)"
                    elif is_rng_qual:
                        qual_tag = f"東京レンジ専任({rng_q_lvl})"
                    elif is_brk_qual:
                        qual_tag = f"欧州ブレイク専任({brk_q_lvl})"
                    else:
                        qual_tag = "Tier1(直近7d黒字&後半加速)" if is_tier1 else "Tier2(後半加速)"
                    discord.print_log(
                        f"   [合格 🟢 - {qual_tag}] {cand}: {dual_tendency_str} -> "
                        f"代表PnL: ${cand_pnl:+.2f} USDT [前半: ${pnl_1st:+.2f} / 後半: ${pnl_2nd:+.2f} / 直近7d: ${pnl_7d:+.2f}({cnt_7d}回), 取引: {trade_cnt}回, 勝率: {win_rt:.1f}%, DD: {max_dd:.1f}%]"
                    )
                    profitable_cands.append(cand)
                else:
                    discord.print_log(
                        f"   [不合格 🔴] {cand}: {dual_tendency_str} (二重頑健性未達: 前半黒字かつ後半加速が必須)"
                    )

            except Exception as opt_err:
                discord.print_log(f"   [最適化エラー] {cand}: {opt_err}")

        await asyncio.sleep(0.1)

    # 1ヶ月間バックテスト 最優秀成績銘柄 チャート1枚出力 (PROJECT_RULES.md 準拠: Tier 1直近7日黒字銘柄から総合最高スコアを選抜)
    best_sym = None
    target_pool = profitable_cands if profitable_cands else list(symbol_params_map.keys())
    if target_pool:
        def _calc_overall_best_score(sym):
            p = symbol_params_map[sym]
            pnl_tot = float(p.get("pnl", 0.0))
            pnl_2nd = float(p.get("pnl_second_half", 0.0))
            pnl_7d = float(p.get("pnl_recent_7d", 0.0))
            win_rt = float(p.get("win_rate", 0.0))
            cnt_7d = int(p.get("trades_recent_7d", 0))
            # 総合純利益に最大ウェイト(1.0)、後半加速(0.5)、直近7日利益(0.5)、勝率ボーナス(win_rt*0.05)、直近頻度ボーナス(min(cnt_7d, 5)*0.2)
            return pnl_tot * 1.0 + pnl_2nd * 0.5 + pnl_7d * 0.5 + (win_rt * 0.05) + (min(cnt_7d, 5) * 0.2)

        # Tier 1: 4大完全必須条件 (前半黒字 & 後半加速 & 直近7日取引1回以上 & 直近7日黒字) クリア銘柄を最優先選抜
        tier1_cands = [
            s for s in target_pool
            if symbol_params_map[s].get("pnl_first_half", 0.0) > 0
            and symbol_params_map[s].get("pnl_second_half", 0.0) > symbol_params_map[s].get("pnl_first_half", 0.0)
            and symbol_params_map[s].get("pnl_recent_7d", 0.0) > 0
            and symbol_params_map[s].get("trades_recent_7d", 0) >= 1
        ]
        if tier1_cands:
            best_sym = max(tier1_cands, key=_calc_overall_best_score)
        else:
            best_sym = max(target_pool, key=_calc_overall_best_score)

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
            else:
                cand_df = load_5m_candles(best_sym)
                if not cand_df.empty:
                    if best_inv > 5:
                        cand_df = resample_from_5m(cand_df, interval_minutes=best_inv)
                    cand_df.tail(min(len(cand_df), 720)).reset_index(drop=True).to_csv(bg_csv, index=False)

            strat_tag = str(best_strat).upper()
            chart_label = f"Best1Month_{strat_tag}_{best_inv}m"
            b_p1 = float(best_p.get('pnl_first_half', 0.0))
            b_p2 = float(best_p.get('pnl_second_half', 0.0))
            b_p7 = float(best_p.get('pnl_recent_7d', 0.0))
            b_c7 = int(best_p.get('trades_recent_7d', 0))
            discord.print_log(
                f"🏆 **【バックテスト 最優秀成績銘柄 (直近1週間実効性 ＆ 後半加速クリア)】**\n"
                f"・銘柄: `{best_sym}` (採用足種: `{best_inv}m足` / 戦略: `{strat_tag}`)\n"
                f"・純利益: `+${best_pnl:.2f} USDT` [前半: +${b_p1:.2f} / 後半: +${b_p2:.2f} / 直近7d: `+${b_p7:.2f}` ({b_c7}回)] (取引: {best_cnt}回, 勝率: {best_win:.1f}%, MaxDD: `-${best_dd:.2f} USDT (-{best_dd:.1f}%)`)\n"
                f"・直近モメンタム最優秀成績チャート1枚を出力します。"
            )
            discord.plot_backtest(label=chart_label, csv_file=bg_csv, symbol=best_sym)
        except Exception as ch_err:
            print(f"Best cand chart error {best_sym}: {ch_err}")

    # チャート出力完了後はメモリ解放
    for sym_k in list(symbol_params_map.keys()):
        cand_dict = symbol_params_map[sym_k]
        cand_dict.pop("chart_df", None)
        if isinstance(cand_dict.get("params"), dict):
            cand_dict["params"].pop("chart_df", None)
        if isinstance(cand_dict.get("range_params"), dict):
            cand_dict["range_params"].pop("chart_df", None)
        if isinstance(cand_dict.get("breakout_params"), dict):
            cand_dict["breakout_params"].pop("chart_df", None)

    if best_sym:
        default_best_params = symbol_params_map.get(best_sym, default_best_params)
    elif profitable_cands:
        first_sym = profitable_cands[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)
    elif symbol_params_map:
        first_sym = list(symbol_params_map.keys())[0]
        default_best_params = symbol_params_map.get(first_sym, default_best_params)

    return profitable_cands, symbol_params_map, default_best_params


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
SL_COOLDOWN_HOURS = 0.0

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
    """24時間 SL クールダウン廃止仕様: 互換維持のための no-op。"""
    return None

def is_in_sl_cooldown(symbol: str) -> Tuple[bool, float]:
    """24時間 SL クールダウン廃止仕様: 常に未冷却 (False, 0.0) を返す。"""
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
                        try:
                            from lake_live_bridge import record_live_exit_to_lake
                            record_live_exit_to_lake(sym, "LONG", entry_px, cur_px, buy_qty, pnl_est, exit_reason, state_meta=dict(scale_state))
                        except Exception:
                            pass
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
                        try:
                            from lake_live_bridge import record_live_exit_to_lake
                            record_live_exit_to_lake(sym, "LONG", entry_px, cur_px, buy_qty, pnl_est, exit_reason, state_meta=dict(scale_state))
                        except Exception:
                            pass
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
                            f"[{sym}] [REALTIME NANPIN] 📉 最後の建値(${last_px:,.4f})からVA50%(-{req_drop_pct*100:.1f}%)下落検出 (${cur_px:,.4f} <= ${drop_target:,.4f})\n"
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
    print("\n[Phase A] 銘柄スクリーニング・パラメータ最適化を開始します (3-Regime Adaptive)...")
    mix_script = Path(__file__).resolve().parent / "bingx5_46_4mix_candle_Merged_Alt10.py"
    mix_ok = False
    if mix_script.exists():
        try:
            cmd = [sys.executable, str(mix_script)]
            if not send_charts:
                cmd.append("--no-chart-send")
            if skip_zip:
                cmd.append("--skip-zip")
            subprocess.run(cmd, check=True)
            mix_ok = True
        except Exception as sub_err:
            print(f"[Screening Error] 銘柄スクリーニング実行エラー: {sub_err}")

    # 2. 3態勢地合い連動 戦略モード判定 (クジラフィルター廃止済み)
    market_state_file = MIX_SCRIPT_PATH.parent / "Data" / "market_state.json"
    overall_reg_mode = "NEUTRAL"
    ms_data: Dict[str, Any] = {}
    if market_state_file.exists():
        try:
            with open(market_state_file, "r", encoding="utf-8") as f:
                ms_data = json.load(f)
                overall_reg_mode = ms_data.get("overall_regime", "NEUTRAL")
        except Exception:
            pass

    if overall_reg_mode == "BUY":
        trade_side = "long"
        strat_mode_desc = "LONG ONLY (買い相場特化・単発1本固定)"
    elif overall_reg_mode == "SELL":
        trade_side = "short"
        strat_mode_desc = "SHORT ONLY (売り相場特化・単発1本固定/ナンピンなし)"
    else:
        trade_side = "none"
        strat_mode_desc = "FLAT / NO TRADING (中立相場・新規エントリー完全停止・撤退のみ)"

    print(f"[Market State] Regime: {overall_reg_mode} | Strategy Mode: {strat_mode_desc} | Whale Filter: OFF")

    # 3. データレイク 2段階ウォークフォワード評価による監視銘柄の選定 (OOS 判定)
    if not mix_ok:
        try:
            from candle_5m_store import sync_all_target_symbols_5m
            sync_all_target_symbols_5m(lookback_days=2)
        except Exception as sync_err:
            print(f"[Lake Sync Warning] 差分5m足同期警告: {sync_err}")

    # ノーマライズ前半高安監査の除外リストを辞書形式へ正規化 (クジラ除外は完全撤廃)
    ms_excl_raw = ms_data.get("excluded_symbols") or {}
    ms_reasons = ms_data.get("exclusion_reasons") or {}
    if isinstance(ms_excl_raw, dict):
        norm_excluded: Dict[str, str] = {normalize_symbol(str(k)): str(v) for k, v in ms_excl_raw.items()}
    else:
        ms_excl_clean = {get_clean_symbol(str(s)) for s in ms_excl_raw}
        norm_excluded = {
            s: str(ms_reasons.get(get_clean_symbol(s), "NORMALIZED_AUDIT_EXCLUDED"))
            for s in FIXED_SYMBOLS
            if (s in ms_excl_raw or get_clean_symbol(s) in ms_excl_clean)
        }

    override_regime = dict(ms_data) if ms_data else {
        "overall_regime": overall_reg_mode,
        "regime_pattern": "",
        "regime_strength": {"tier": "BALANCED", "net_score": 0.0, "mean_buy_votes": 0.0, "mean_sell_votes": 0.0},
        "allowed_strategies": [],
        "recommended_symbols": list(FIXED_SYMBOLS),
    }
    override_regime["excluded_symbols"] = norm_excluded
    override_regime["whale_snapshot"] = {"source": "disabled", "filter_enabled": False}

    from lake_live_bridge import run_f7_screening_for_bot, run_lake_screening_for_bot
    if USE_F7_TWO_PILLAR_MODE:
        lake_out = await asyncio.to_thread(
            run_f7_screening_for_bot,
            override_regime,
            max_selected_symbols=MAX_SELECTED_SYMBOLS,
            excluded_mega_caps=EXCLUDED_MEGA_CAPS,
            gil_yield_sec=0.005,
        )
    else:
        lake_out = await asyncio.to_thread(
            run_lake_screening_for_bot,
            override_regime,
            max_selected_symbols=MAX_SELECTED_SYMBOLS,
            excluded_mega_caps=EXCLUDED_MEGA_CAPS,
        )
    selected_symbols = [
        s for s in lake_out["selected_symbols"]
        if s not in EXCLUDED_MEGA_CAPS and get_clean_symbol(s) not in EXCLUDED_MEGA_CAPS
    ]
    symbol_params_map = lake_out["symbol_params_map"]
    default_best_params = lake_out["default_best_params"] or {
        "strategy": (F7_SHORT_STRATEGY if trade_side == "short" else F7_LONG_STRATEGY) if USE_F7_TWO_PILLAR_MODE else ("rsima_short" if trade_side == "short" else "rsima"),
        "interval": (60 if trade_side == "short" else 30) if USE_F7_TWO_PILLAR_MODE else 5,
        "mp": 120 if USE_F7_TWO_PILLAR_MODE else 150,
        "target_ratio": 0.70,
        "l_ep": 40.0,
        "l_cp": 60.0,
        "s_ep": 60.0,
        "s_cp": 40.0,
        "pnl": 0.0,
        "win_rate": 0.0,
        "trade_count": 0,
        "max_dd": 0.0,
    }
    discord.print_log(lake_out["summary_log"])
    print(f"\n[Selection Result] 当日適格・監視採用 ({len(selected_symbols)}銘柄): {', '.join(selected_symbols) if selected_symbols else 'なし (見送り / NO_CANDIDATE)'}")

    # 直近の選定結果を JSON に永続化 (show_symbol_status 用 / アトミック書き込み)
    last_res_file = Path(__file__).resolve().parent / "Data" / "last_selection_result.json"
    try:
        clean_map = _sanitize_for_json(symbol_params_map)
        tmp_file = last_res_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({
                "f7_mode": bool(USE_F7_TWO_PILLAR_MODE),
                "h3b_wf90d": lake_out.get("h3b_wf90d"),
                "btc_ret14d": lake_out.get("btc_ret14d"),
                "selected_symbols": selected_symbols,
                "symbol_params_map": clean_map,
                "evaluated_at": datetime.now(timezone.utc).isoformat()
            }, f, indent=2, ensure_ascii=False)
        tmp_file.replace(last_res_file)
    except Exception as e:
        discord.print_log(f"[Warning] last_selection_result.json 保存エラー: {e}")

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

    if USE_F7_TWO_PILLAR_MODE:
        long_q_n = sum(1 for s in selected_symbols if symbol_params_map.get(s, {}).get("f7_long_eligible_now"))
        short_q_n = sum(1 for s in selected_symbols if symbol_params_map.get(s, {}).get("f7_short_eligible_now"))
        discord.print_log(
            f"\n★ 【{F7_MODE_NAME} 監視設定】 {len(selected_symbols)}中堅アルト | "
            f"LONG(`val_poc_30m`/30m/ULTRA_BUY/NoEU)={long_q_n}銘柄 | "
            f"SHORT(`h3b_squeeze_1h`/1h/SELL/S8b+NoExh+WF90d)={short_q_n}銘柄"
        )
    # 選定銘柄の個別最適化パラメータをDiscordログ表示 (旧6戦略モード)
    elif selected_symbols:
        side_strat_label = "ショートPure3大戦略 (VAH_POC / VAL_BREAK / RSIMA_SHORT)" if trade_side == "short" else "ロングPure3大戦略 (VAL_POC / VAH_BREAK / RSIMA)"
        discord.print_log(f"\n★ 【合格銘柄 デュアル最適化パラメータ一覧 ({side_strat_label} 126通り検証 / 東京レンジ＆欧州ブレイク2種保持)】")
        for sym in selected_symbols:
            p = symbol_params_map.get(sym, default_best_params)
            rng_p = p.get("range_params") or p
            brk_p = p.get("breakout_params") or p
            is_rng_q = bool(p.get("is_range_qualified", rng_p.get("is_qualified", False)))
            is_brk_q = bool(p.get("is_breakout_qualified", brk_p.get("is_qualified", False)))

            def _fmt_sub_param(sub_d: dict, is_q: bool, is_brk_mode: bool) -> str:
                st_u = str(sub_d.get("strategy", "rsima")).upper()
                iv_u = int(sub_d.get("interval", 5) or 5)
                pnl_u = float(sub_d.get("pnl", 0.0))
                tc_u = int(sub_d.get("trade_count", 0))
                wr_u = float(sub_d.get("win_rate", 0.0))
                q_mark = "🟢稼働" if is_q else "⚪待機(未達)"
                if is_brk_mode or st_u in ("VAH_BREAK", "VAL_BREAK"):
                    mp_u = int(sub_d.get("mp", 200))
                    tr_u = int(float(sub_d.get("target_ratio", 0.70)) * 100)
                    spec_s = f"{st_u}({iv_u}m/MP={mp_u}/TR={tr_u}%)"
                elif "POC" in st_u:
                    mp_u = int(sub_d.get("mp", 200))
                    tr_u = int(float(sub_d.get("target_ratio", 0.70)) * 100)
                    spec_s = f"{st_u}({iv_u}m/MP={mp_u}/TR={tr_u}%)"
                else:
                    is_sh = (trade_side == "short" or "SHORT" in st_u)
                    ep_u = int(float(sub_d.get("s_ep" if is_sh else "l_ep", 60 if is_sh else 40)))
                    cp_u = int(float(sub_d.get("s_cp" if is_sh else "l_cp", 40 if is_sh else 60)))
                    spec_s = f"{st_u}({iv_u}m/EP={ep_u}/CP={cp_u})"
                return f"{q_mark} {spec_s} [${pnl_u:+.2f}/{tc_u}回/勝率{wr_u:.0f}%]"

            rng_summary = _fmt_sub_param(rng_p, is_rng_q, False)
            brk_summary = _fmt_sub_param(brk_p, is_brk_q, True)

            discord.print_log(
                f"   📌 [{sym}] (単発1本固定) | 🗼東京(09-16JST): {rng_summary} ／ 🏰欧州(16-22JST): {brk_summary}"
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
        return float(current_balance)
    except Exception as e:
        discord.print_log(f"[Performance Report Error] 実績レポート送信失敗: {e}")
        return 0.0


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
    whale_sig: str = "",
    whale_flow_val: float = 0.0,
    interval_label: str = "5m",
    side: str = "LONG"
):
    """新規エントリー約定後のトレード記録・チャート生成・Discord画像送信を完全非同期実行する"""
    try:
        from real_trade_tracker import record_real_trade
        is_short = (side.upper() == "SHORT")
        side_clean = "SHORT" if is_short else "LONG"
        record_real_trade(sym, side_clean, "ENTRY", entry_px, lot_size, 0.0, f"新規{'ショート' if is_short else 'ロング'}エントリー約定 [{interval_label}足]")

        entry_notional = entry_px * lot_size
        if is_short:
            target_tp_val = vah_val - (vah_val - val_val) * 0.50 if (vah_val > val_val and val_val > 0) else poc_val
            target_tp_desc = f"VAH-VAL 50%水準 (${target_tp_val:.6f}) / RSI冷却利確"
            nanpin_desc = "単発1本固定 (ナンピン完全なし)"
            sl_desc = "直近10本高値確定足ストップ (Swing High SL) / 8h上限決済"
            side_badge = "SHORT 🔴"
            header_title = f"🚀🚀🚀 **【新規ショートエントリー約定 [{interval_label}足]】** 🚀🚀🚀"
        else:
            target_tp_val = val_val + (vah_val - val_val) * 0.50 if (vah_val > val_val and val_val > 0) else poc_val
            target_tp_desc = f"VAL-VAH 50%水準 (${target_tp_val:.6f}) / RSI過熱利確"
            nanpin_desc = "単発1本固定 (ナンピン完全なし)"
            sl_desc = "直近10本安値確定足ストップ (Swing Low SL) / 8h上限決済"
            side_badge = "LONG 🟢"
            header_title = f"🚀🚀🚀 **【新規ロングエントリー約定 [{interval_label}足]】** 🚀🚀🚀"

        entry_msg = (
            f"{header_title}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **銘柄 / 方向**: `{sym}` ({side_badge} / 採用足種: `{interval_label}足`)\n"
            f"⚡ **検知トリガー**: `{entry_full_reason}`\n"
            f"💰 **約定価格**: `${entry_px:.6f}`\n"
            f"📦 **発注数量**: `{lot_size:,.2f} {sym}` (約 `${entry_notional:,.2f} USDT`)\n"
            f"⚙️ **適用戦略**: `{strat_desc}`\n"
            f"🎯 **利確目標**: `{target_tp_desc}`\n"
            f"📉 **ナンピン設計**: `{nanpin_desc}`\n"
            f"🛑 **損切り仕様**: `{sl_desc}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # PROJECT_RULES準拠: エントリー時は画像出力を廃止し、詳細テキスト通知のみを即時送信
        discord.print_log(entry_msg, force_send=True)
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
    whale_sig: str = "",
    cycle_balance: float = 0.0,
    interval_label: str = "5m",
    mp_period: int = 100,
    mode: str = "demo",
):
    """決済完了後のトレード記録・取引所実手数料同期(Net PnL)・チャート生成・Discord画像送信を完全非同期実行する"""
    try:
        from real_trade_tracker import (
            record_real_trade,
            plot_real_trading_performance,
            plot_exit_chart,
            sync_real_trades_from_bingx_api,
            load_real_trades,
        )
        side_upper = str(side).upper()
        side_icon = "🟢" if side_upper == "LONG" else "🔴"
        side_label_ja = "ロング" if side_upper == "LONG" else "ショート"

        est_fee = (
            (entry_px * close_qty) * 0.0002 + (reported_exit_px * close_qty) * 0.0005
            if (entry_px > 0 and close_qty > 0 and reported_exit_px > 0)
            else 0.0
        )
        est_net_pnl = round(float(pnl) - est_fee, 4)
        recorded_entry = record_real_trade(
            sym, side_upper, "CLOSE", reported_exit_px, close_qty, est_net_pnl, f"実運用決済 ({exit_reason})"
        )
        recorded_ts = recorded_entry.get("timestamp") if isinstance(recorded_entry, dict) else None

        display_pnl = est_net_pnl
        if not BINGX_IS_AIR:
            try:
                await asyncio.sleep(2.0)
                await sync_real_trades_from_bingx_api(mode=mode)
                latest_trades = load_real_trades()
                for t_rec in reversed(latest_trades):
                    if t_rec.get("action") != "CLOSE" or t_rec.get("symbol") != sym:
                        continue
                    if not t_rec.get("fee_synced"):
                        continue
                    if recorded_ts and t_rec.get("timestamp") == recorded_ts:
                        display_pnl = float(t_rec.get("pnl", est_net_pnl))
                        break
            except Exception as sync_ex_err:
                print(f"[{sym}] 決済直後API純損益同期警告: {sync_ex_err}")

        discord.print_log(
            f"[{sym}] [CLOSE] {side_icon} {side_label_ja}ポジション決済完了 [{interval_label}足] "
            f"(Net PnL: {display_pnl:+.2f} USDT, 理由: {exit_reason})"
        )

        # 1. 決済トレードチャート画像の生成 & 送信
        exit_chart_file = await asyncio.to_thread(
            plot_exit_chart,
            symbol=sym, df=df_chart, exit_price=reported_exit_px, entry_price=entry_px,
            pnl=display_pnl, exit_reason=exit_reason, side=side_upper,
            interval_label=interval_label, mp_period=mp_period
        )
        if exit_chart_file and Path(exit_chart_file).exists():
            discord.send_file(
                exit_chart_file,
                f"📊 【決済チャート】{sym} {side_upper} 決済完了 [{interval_label}足] (Net PnL: {display_pnl:+.2f} USDT)"
            )

        # 2. スライディング10日間 累積 PnL パフォーマンスチャート (Clean版 & Detail版 2枚)
        clean_file, detail_file = await asyncio.to_thread(plot_real_trading_performance, 10, cycle_balance)
        if clean_file and Path(clean_file).exists():
            discord.send_file(
                clean_file,
                f"📈 【実運用実績】累積損益パフォーマンス (直近10日 - Clean版 / 今回決済: {display_pnl:+.2f} USDT)"
            )
        if detail_file and Path(detail_file).exists():
            discord.send_file(
                detail_file,
                f"🔍 【実運用実績】累積損益パフォーマンス (直近10日 - Detail版 / 今回決済: {display_pnl:+.2f} USDT)"
            )
    except Exception as e:
        discord.print_log(f"⚠️ [{sym}] 決済レポート非同期タスクエラー: {e}", level="warning")


def _extract_latest_close_ts_utc(df_eval: Optional[pd.DataFrame], interval_min: int) -> int:
    """確定足DataFrameの最新バー終端UTC秒を取得する (5m/15m約定バー・同一バー再エントリー抑止用)。"""
    if df_eval is None or df_eval.empty:
        return 0
    inv_sec = max(1, int(interval_min or 5)) * 60
    try:
        if "ts_close" in df_eval.columns:
            return int(df_eval["ts_close"].iloc[-1])
        if "ts_open" in df_eval.columns:
            return int(df_eval["ts_open"].iloc[-1]) + inv_sec
        if "timestamp" in df_eval.columns:
            val = df_eval["timestamp"].iloc[-1]
            if isinstance(val, (int, float, np.integer, np.floating)):
                sec = int(float(val) // 1000) if float(val) > 1e11 else int(val)
                return sec + inv_sec
            return int(pd.to_datetime(val, utc=True).timestamp()) + inv_sec
        if isinstance(df_eval.index, pd.DatetimeIndex):
            return int(pd.to_datetime(df_eval.index[-1], utc=True).timestamp()) + inv_sec
    except Exception:
        pass
    return 0


def _evaluate_strategy_signal(
    cand_strat: str,
    df_eval: pd.DataFrame,
    current_pos_qty: float,
    current_trades_count: int,
    avg_entry_price: float,
    last_entry_price: float,
    current_price: float,
    sym_params: Dict[str, Any],
    val_price: float,
    vah_price: float,
    pos_mp: int,
    pos_tr: float,
    pos_l_ep: float,
    pos_l_cp: float,
    pos_s_ep: float,
    pos_s_cp: float,
    is_cooldown_active: bool = False,
    is_emergency_breakeven: bool = False,
    dynamic_sl_pct: float = -0.030,
    bars_held: int = 0,
    timeout_bars: int = 48,
    is_fill_bar: bool = False,
    elapsed_minutes: Optional[float] = None,
    timeout_minutes: float = 240.0,
    max_hold_minutes: float = 480.0,
    has_new_tf_bar_after_fill: bool = True,
) -> Dict[str, Any]:
    """全6戦略 (LONG/SHORT) 共通のエントリー・エグジット判定ディスパッチャ (SSoT準拠)。"""
    from bingx5_46_3logic import (
        check_rsima3_signals,
        check_val_poc_signals,
        check_vah_poc_signals,
        check_rsima3_short_signals,
        check_vah_break_signals,
        check_val_break_signals,
    )

    strat = str(cand_strat or "rsima").lower()
    num_bins = int(sym_params.get("num_bins", 50))
    va_pct = float(sym_params.get("value_area_pct", 0.70))
    use_ema = ("ema" in strat)

    if strat == "val_break":
        return check_val_break_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            last_entry_price=last_entry_price,
            current_price=current_price,
            recent_high=None,
            vp_period=pos_mp,
            num_bins=num_bins,
            value_area_pct=va_pct,
            target_ratio=pos_tr,
            is_cooldown_active=is_cooldown_active,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            timeout_bars=timeout_bars,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    if strat.startswith("vah_poc"):
        return check_vah_poc_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            last_entry_price=last_entry_price,
            current_price=current_price,
            recent_high=None,
            vp_period=pos_mp,
            num_bins=num_bins,
            value_area_pct=va_pct,
            ma_type=str(sym_params.get("ma_type", "EMA" if use_ema else "NONE")),
            ma_condition=str(sym_params.get("ma_condition", "close_below" if use_ema else "none")),
            max_trades=1,
            target_ratio=pos_tr,
            sl_pct=abs(dynamic_sl_pct),
            is_cooldown_active=is_cooldown_active,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            use_ema_filter=use_ema,
            timeout_bars=timeout_bars,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    if strat.startswith("rsima_short"):
        return check_rsima3_short_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            current_price=current_price,
            recent_high=None,
            rsi_len=9,
            ema_len=7,
            s_ep=pos_s_ep,
            s_cp=pos_s_cp,
            sl_pct=abs(dynamic_sl_pct),
            max_trades=1,
            last_entry_price=last_entry_price,
            val_price=val_price,
            vah_price=vah_price,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            use_ema_filter=use_ema,
            timeout_bars=timeout_bars,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    if strat == "vah_break":
        return check_vah_break_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            last_entry_price=last_entry_price,
            current_price=current_price,
            vp_period=pos_mp,
            num_bins=num_bins,
            value_area_pct=va_pct,
            target_ratio=pos_tr,
            is_cooldown_active=is_cooldown_active,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            timeout_bars=timeout_bars,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    if strat.startswith("val_poc"):
        return check_val_poc_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            last_entry_price=last_entry_price,
            current_price=current_price,
            vp_period=pos_mp,
            num_bins=num_bins,
            value_area_pct=va_pct,
            ma_type=str(sym_params.get("ma_type", "EMA" if use_ema else "NONE")),
            ma_condition=str(sym_params.get("ma_condition", "close_above" if use_ema else "none")),
            max_trades=MAX_TRADES_COUNT,
            target_ratio=pos_tr,
            sl_pct=abs(dynamic_sl_pct),
            is_cooldown_active=is_cooldown_active,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            use_ema_filter=use_ema,
            timeout_bars=timeout_bars,
            recent_low=None,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    if strat.startswith("rsima"):
        return check_rsima3_signals(
            df_5m=df_eval,
            current_pos_qty=current_pos_qty,
            current_trades_count=current_trades_count,
            avg_entry_price=avg_entry_price,
            current_price=current_price,
            rsi_len=9,
            ema_len=7,
            l_ep=pos_l_ep,
            l_cp=pos_l_cp,
            sl_pct=abs(dynamic_sl_pct),
            max_trades=MAX_TRADES_COUNT,
            last_entry_price=last_entry_price,
            val_price=val_price,
            vah_price=vah_price,
            is_cooldown_active=is_cooldown_active,
            is_emergency_breakeven=is_emergency_breakeven,
            dynamic_sl_pct=dynamic_sl_pct,
            bars_held=bars_held,
            use_ema_filter=use_ema,
            timeout_bars=timeout_bars,
            recent_low=None,
            is_fill_bar=is_fill_bar,
            elapsed_minutes=elapsed_minutes,
            timeout_minutes=timeout_minutes,
            max_hold_minutes=max_hold_minutes,
            has_new_tf_bar_after_fill=has_new_tf_bar_after_fill,
        )
    return {"action": "NONE", "reason": f"UNKNOWN_STRATEGY({strat})", "lot_multiplier": 1.0, "metrics": {}}


async def _cancel_pending_order_for_symbol(
    sym: str,
    reason: str,
    pending_entry_orders: Dict[str, Dict[str, Any]],
    symbol_apis: Dict[str, Any],
) -> bool:
    """指定銘柄の未約定指値を安全キャンセルし pending_entry_orders から除去する。"""
    if sym not in pending_entry_orders:
        return False
    p_api = symbol_apis.get(sym)
    if p_api:
        try:
            await p_api.active_order_cancel()
        except Exception:
            pass
    pending_entry_orders.pop(sym, None)
    discord.print_log(f"🛡️ [{sym}] 未約定指値を安全キャンセルしました ({reason})")
    return True


async def _cancel_all_pending_orders(
    reason: str,
    pending_entry_orders: Dict[str, Dict[str, Any]],
    symbol_apis: Dict[str, Any],
) -> int:
    """現在残っている全未約定指値を安全キャンセルする。"""
    cnt = 0
    for p_sym in list(pending_entry_orders.keys()):
        if await _cancel_pending_order_for_symbol(p_sym, reason, pending_entry_orders, symbol_apis):
            cnt += 1
    return cnt


def _build_and_store_live_fill_state(
    sym: str,
    side: str,
    actual_fill_px: float,
    lot_size: float,
    df_eval: Optional[pd.DataFrame],
    sym_params_e: Dict[str, Any],
    cand_interval_e: int,
    cand_strat_e: str,
    entry_reason: str,
    vah_e: float,
    val_e: float,
    poc_e: float,
    scale_in_states: Dict[str, Any],
    trade_counts: Dict[str, int],
    last_trade_time: Dict[str, float],
    last_nanpin_time: Dict[str, float],
    tracked_active_positions: Set[str],
    pending_info: Optional[Dict[str, Any]] = None,
    fill_ts_override: Optional[float] = None,
    spawn_reporting: bool = True,
) -> Dict[str, Any]:
    """約定直後の状態 (fill_5m_open_ts / Data Lake / scale_in_states / 非同期通知) を共通構築する。"""
    side_low = "short" if str(side).lower() == "short" else "long"
    is_short = (side_low == "short")
    now_val = float(fill_ts_override if fill_ts_override is not None else time.time())
    fill_5m_open_ts = (int(now_val) // 300) * 300

    dyn_sl_pct_e = float(sym_params_e.get("dynamic_mae_stop", -0.030))
    dyn_to_bars_e = int(sym_params_e.get("dynamic_timeout_bars", max(3, 240 // max(1, cand_interval_e))))
    rec_h_val = 0.0
    rec_l_val = 0.0
    sl_px_val = 0.0
    if df_eval is not None and not df_eval.empty:
        if is_short:
            h_eval = df_eval["high"].astype(float) if "high" in df_eval.columns else df_eval["close"].astype(float)
            rec_h_val = float(h_eval.iloc[-10:].max()) if len(h_eval) >= 10 else float(h_eval.max())
            sl_px_val = min(max(rec_h_val, actual_fill_px * 1.0020), actual_fill_px * (1.0 + abs(dyn_sl_pct_e)))
        else:
            l_eval = df_eval["low"].astype(float) if "low" in df_eval.columns else df_eval["close"].astype(float)
            rec_l_val = float(l_eval.iloc[-10:].min()) if len(l_eval) >= 10 else float(l_eval.min())
            sl_px_val = max(min(rec_l_val, actual_fill_px * 0.9980), actual_fill_px * (1.0 - abs(dyn_sl_pct_e)))

    lake_entry_meta: Dict[str, Any] = {}
    tf_p = {5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}.get(int(cand_interval_e or 5), "5m")
    try:
        from lake_live_bridge import record_live_entry_to_lake, interval_to_timeframe
        tf_p = interval_to_timeframe(cand_interval_e)
        lake_params = dict(sym_params_e)
        lake_params.update({
            "strategy": cand_strat_e,
            "interval": cand_interval_e,
            "timeframe": tf_p,
        })
        if pending_info:
            lake_params["lake_selection_id"] = pending_info.get("lake_selection_id") or sym_params_e.get("lake_selection_id")
            lake_params["lake_run_id"] = pending_info.get("lake_run_id") or sym_params_e.get("lake_run_id")
        lake_entry_meta = record_live_entry_to_lake(sym, side_low, actual_fill_px, lot_size, params=lake_params)
    except Exception:
        lake_entry_meta = {}

    # F7 Two-Pillar 状態構築 (実約定価格とシグナル確定足 ATR14 を基準に再算出)
    is_f7_pos = is_f7_strategy(cand_strat_e)
    f7_extra: Dict[str, Any] = {}
    if is_f7_pos:
        src_p = pending_info or sym_params_e or {}
        entry_atr_val = float(src_p.get("entry_atr") or 0.0)
        if entry_atr_val <= 0.0 and df_eval is not None and not df_eval.empty:
            try:
                if "atr14" in df_eval.columns:
                    entry_atr_val = float(df_eval["atr14"].iloc[-1] or 0.0)
                else:
                    h_a = df_eval["high"].astype(float).to_numpy()
                    l_a = df_eval["low"].astype(float).to_numpy()
                    c_a = df_eval["close"].astype(float).to_numpy()
                    atr_s = _f7_calc_atr_series(h_a, l_a, c_a, 14)
                    if len(atr_s) > 0:
                        entry_atr_val = float(atr_s[-1])
            except Exception:
                entry_atr_val = 0.0
        tf_sec_f7 = 1800 if cand_strat_e.lower() == F7_LONG_STRATEGY else 3600
        fill_tf_open_ts = (int(now_val) // tf_sec_f7) * tf_sec_f7
        if cand_strat_e.lower() == F7_LONG_STRATEGY:
            val_lv = float(src_p.get("val") or val_e or 0.0)
            vah_lv = float(src_p.get("vah") or vah_e or 0.0)
            poc_lv = float(src_p.get("poc") or poc_e or 0.0)
            f7_lv = compute_val_poc_30m_entry_levels(actual_fill_px, entry_atr_val, val_lv, vah_lv, poc_lv)
            f7_extra = {
                "f7_mode": True,
                "fill_tf_open_ts": fill_tf_open_ts,
                "entry_atr": entry_atr_val,
                "init_sl": f7_lv["init_sl"],
                "target_tp": f7_lv["target_tp"],
                "be_px": f7_lv["be_px"],
            }
            sl_px_val = f7_lv["init_sl"]
        else:
            f7_lv = compute_h3b_squeeze_1h_entry_levels(actual_fill_px, entry_atr_val)
            f7_extra = {
                "f7_mode": True,
                "fill_tf_open_ts": fill_tf_open_ts,
                "entry_atr": entry_atr_val,
                "init_sl": f7_lv["init_sl"],
                "target_tp": 0.0,
                "be_px": f7_lv["be_px"],
                "trough_price": float(actual_fill_px),
                "live_trough_price": float(actual_fill_px),
                "last_trough_bucket_open": fill_tf_open_ts,
                "be_locked": False,
            }
            sl_px_val = f7_lv["init_sl"]

    scale_state_new: Dict[str, Any] = {
        "nanpin_count": 0,
        "last_entry_price": actual_fill_px,
        "avg_price": actual_fill_px,
        "initial_entry_time": now_val,
        "entry_ts_utc": int(now_val),
        "fill_5m_open_ts": fill_5m_open_ts,
        "qty": float(lot_size),
        "side": side_low,
        "interval": cand_interval_e,
        "timeframe": tf_p,
        "strategy": cand_strat_e,
        "dynamic_mae_stop": dyn_sl_pct_e,
        "dynamic_timeout_bars": dyn_to_bars_e,
        "mp": int(sym_params_e.get("mp", 120 if is_f7_pos else 200)),
        "target_ratio": float(sym_params_e.get("target_ratio", 0.70)),
        "s_ep": float(sym_params_e.get("s_ep", 60.0)),
        "s_cp": float(sym_params_e.get("s_cp", 40.0)),
        "l_ep": float(sym_params_e.get("l_ep", 40.0)),
        "l_cp": float(sym_params_e.get("l_cp", 60.0)),
        "recent_high": rec_h_val,
        "recent_low": rec_l_val,
        "sl_price": sl_px_val,
        **f7_extra,
        **lake_entry_meta,
    }
    scale_in_states[sym] = scale_state_new
    save_scale_in_states(scale_in_states)
    trade_counts[sym] = 1
    last_trade_time[sym] = now_val
    last_nanpin_time[sym] = now_val
    tracked_active_positions.add(sym)

    if spawn_reporting:
        inv_label_e = f"{cand_interval_e}m" if cand_interval_e < 60 else f"{cand_interval_e // 60}h"
        side_label = "SHORT" if is_short else "LONG"
        strat_desc = f"{side_label} {cand_strat_e.upper()} ({inv_label_e}足 / MP={sym_params_e.get('mp', '-')})"
        df_rep = df_eval if (df_eval is not None and not df_eval.empty) else pd.DataFrame()
        try:
            asyncio.create_task(_async_post_entry_reporting(
                sym=sym, entry_px=actual_fill_px, lot_size=lot_size, entry_full_reason=entry_reason,
                df_chart=df_rep, vah_val=vah_e, val_val=val_e, poc_val=poc_e,
                strat_desc=strat_desc, interval_label=inv_label_e, side=side_label,
            ))
        except RuntimeError:
            pass

    return scale_state_new


def should_block_ny_session_entry(
    is_live: bool,
    is_ny: bool,
    allow_ny_live: Optional[bool] = None,
) -> bool:
    """米国セッション(22:00〜翌09:00 JST)で新規エントリーを停止すべきか判定する。"""
    allow = ALLOW_NY_SESSION_LIVE if allow_ny_live is None else bool(allow_ny_live)
    return bool(is_ny and is_live and not allow)


def select_session_entry_spec(
    raw_sym_params: Dict[str, Any],
    is_tokyo: bool,
    is_london: bool,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """JST時間帯に応じた最適パラメータと合格ゲートを検証する (BT _pick_session_spec 完全対称)。"""
    if is_tokyo:
        sp = raw_sym_params.get("range_params") or raw_sym_params
        if not bool(raw_sym_params.get("is_range_qualified", sp.get("is_qualified", False))):
            return None, "東京RANGE不合格"
        strat = str(sp.get("strategy", "rsima")).lower()
        if strat in ("vah_break", "val_break"):
            return None, f"東京ブレイク対象外({strat})"
        return sp, "OK"
    if is_london:
        sp = raw_sym_params.get("breakout_params") or raw_sym_params
        if not bool(raw_sym_params.get("is_breakout_qualified", sp.get("is_qualified", False))):
            return None, "欧州BREAKOUT不合格"
        strat = str(sp.get("strategy", "rsima")).lower()
        if strat not in ("vah_break", "val_break"):
            return None, f"欧州レンジ対象外({strat})"
        return sp, "OK"
    if not bool(raw_sym_params.get("is_qualified", True)):
        return None, "NY総合OOS不合格"
    return raw_sym_params, "OK"


def resolve_strategy_side(cand_strat: str) -> Optional[str]:
    """戦略名から売買方向 ('LONG' / 'SHORT') を決定する。"""
    s = str(cand_strat or "").lower()
    if s in ("val_poc", "vah_break", "rsima") or s.startswith("val_poc") or (s.startswith("rsima") and not s.startswith("rsima_short")):
        return "LONG"
    if s in ("vah_poc", "val_break", "rsima_short", "h3b_squeeze_1h") or s.startswith("vah_poc") or s.startswith("val_break") or s.startswith("rsima_short") or s.startswith("h3b"):
        return "SHORT"
    return None


async def check_and_reconcile_symbol_gate(
    sym: str,
    raw_sym_params: Dict[str, Any],
    is_tokyo: bool,
    is_london: bool,
    gate_allowed: List[str],
    excluded_mega_caps: List[str],
    pending_entry_orders: Dict[str, Dict[str, Any]],
    symbol_apis: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """時間帯合格ゲート・許可戦略・方向を検証し、不適合・切替時は既存待機指値を安全キャンセルする。"""
    if sym in excluded_mega_caps:
        await _cancel_pending_order_for_symbol(sym, "除外対象メガキャップ", pending_entry_orders, symbol_apis)
        return None, "", None

    sym_params_e, skip_reason = select_session_entry_spec(raw_sym_params, is_tokyo, is_london)
    if sym_params_e is None:
        await _cancel_pending_order_for_symbol(sym, skip_reason, pending_entry_orders, symbol_apis)
        return None, "", None

    cand_strat_e = str(sym_params_e.get("strategy", "rsima")).lower()
    cand_side = resolve_strategy_side(cand_strat_e)
    if cand_side is None:
        await _cancel_pending_order_for_symbol(sym, f"方向不明戦略({cand_strat_e})", pending_entry_orders, symbol_apis)
        return None, "", None

    if gate_allowed and cand_strat_e not in gate_allowed:
        await _cancel_pending_order_for_symbol(sym, f"地合い許可外({cand_strat_e})", pending_entry_orders, symbol_apis)
        return None, "", None

    if sym in pending_entry_orders:
        po_prev = pending_entry_orders[sym]
        cand_interval_e = int(sym_params_e.get("interval", 5) or 5)
        same_pending_spec = (
            str(po_prev.get("side", "")).lower() == cand_side.lower()
            and str(po_prev.get("strategy", "")).lower() == cand_strat_e
            and int(po_prev.get("interval", 5) or 5) == cand_interval_e
        )
        if not same_pending_spec:
            await _cancel_pending_order_for_symbol(
                sym,
                f"セッション/戦略切替({po_prev.get('strategy')}/{po_prev.get('interval')}m -> {cand_strat_e}/{cand_interval_e}m)",
                pending_entry_orders,
                symbol_apis,
            )
            return None, "", None

    return sym_params_e, cand_strat_e, cand_side


async def start(mode: str = 'demo', max_lot: float = 10.0, interval: str = '60'):
    from bingx5_46_2api import (
        api_bingx,
        fetch_instrument_spec_bingx,
        flatten_current_position_bingx,
    )
    from bingx5_46_3logic import PnLCalculator

    account_mode_str = "[LIVE Account] (本番口座)" if mode == "live" else "[DEMO Account] (BingX VST デモ口座)"
    air_mode_str = "[AIR MODE ON] (シミュレーション・仮想ペーパートレード)" if bingx5_46_2api.is_air else "[REAL VST ORDER ON] (実際にBingX VST取引所へ発注・約定・管理)"

    if len(ANALYSIS_HOURS) == 24:
        cycle_str = "毎時 00:05 JST (1時間サイクル / 地合い判定毎時追従)"
    else:
        hours_str = ", ".join([f"{h:02d}:00" for h in sorted(ANALYSIS_HOURS)])
        cycle_str = f"毎日 {hours_str} JST ({24 // max(1, len(ANALYSIS_HOURS))}時間ごと)"
    strat_banner_label = (
        f"{F7_MODE_NAME} (`val_poc_30m`[30m/ULTRA_BUY/NoEU] + `h3b_squeeze_1h`[1h/SELL/S8b+WF90d])"
        if USE_F7_TWO_PILLAR_MODE
        else "3態勢地合い連動 (BULL:買い / NEUTRAL:待機 / BEAR:売り)"
    )
    candle_banner_label = (
        "5分足 SSoT ➔ 30m (LONG) & 1h (SHORT) リサンプリング + pybotters WebSocket"
        if USE_F7_TWO_PILLAR_MODE
        else "5分足 (5m) & pybotters WebSocket リアルタイム配信"
    )
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
        f"🚀 **【BingX Auto Trading System 起動 ({'F7 Two-Pillar' if USE_F7_TWO_PILLAR_MODE else '3態勢地合い連動'})】**\n"
        "```text\n"
        "==================================================\n"
        f"  取引口座設定 : {account_mode_str}\n"
        f"  発注モード   : {air_mode_str}\n"
        f"  戦略態勢     : {strat_banner_label}\n"
        f"  監視足種     : {candle_banner_label}\n"
        f"  銘柄選定周期 : {cycle_str}\n"
        f"  ポジション枠 : 最大 {MAX_ACTIVE_POSITIONS} 銘柄同時保有\n"
        f"  買い (BULL)  : 単発1本固定 / 30m VAL_TP (ULTRA_BUY・欧州16-22JST除外 / SL=min(2ATR,4%))\n"
        f"  売り (BEAR)  : 単発1本固定 / 1h H3b Squeeze (SELL・NoExh・WF90d / S8b+1.5ATR BE/2ATRトレール)\n"
        f"  日次リスク管理: 回数制限なし / 損失CB -${DAILY_MAX_LOSS_USDT:.2f} (JST 09:00 リセット)\n"
        f"  銘柄＆出来高 : 中堅アルト18銘柄特化 (SOL/DOGE/XRP除外 / 競合時は vol_ratio 降順優先)\n"
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

    # ========== 【トレード前準備 ステップ2】: データレイク SSoT 永続ストア同期 (21アルト+BTC / 5m,15m,1h,4h) ==========
    from lake_live_bridge import ensure_lake_ready_on_startup
    discord.print_log("\n📦 【トレード前準備: ステップ2】 22銘柄 (21アルト+BTC) のデータレイク (bingx_lake.db) 同期を確認します...")
    try:
        lake_init_res = await asyncio.to_thread(ensure_lake_ready_on_startup, 210)
        discord.print_log(f"✅ データレイク SSoT ストアの同期が完了しました ({lake_init_res}).")
    except Exception as sync_err:
        discord.print_log(f"⚠️ データレイク SSoT 同期で警告が発生しました (後続処理を継続): {sync_err}")

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
    startup_balance = await send_periodic_performance_report(symbol_apis, mode, label="起動時初期レポート")

    # 起動時1回: 直近14日間 バックテスト vs 実運用 比較レポート＆白ベース推移グラフ送信（09:00日次枠は消費しない）
    try:
        from real_trade_tracker import check_and_send_daily_14d_report
        await asyncio.to_thread(
            check_and_send_daily_14d_report,
            current_balance=(startup_balance if startup_balance and startup_balance > 0 else None),
            force=True,
            mark_daily_sent=False,
            label="起動時 14日間レポート",
        )
    except Exception as d14_start_err:
        discord.print_log(f"[Startup 14d Report Warning] 起動時14日比較レポート警告: {d14_start_err}", level="warning")

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
    last_signal_tf_close_map: Dict[Tuple[str, str, int], int] = {}
    last_trade_time: Dict[str, float] = {}
    last_nanpin_time: Dict[str, float] = {}
    cached_5m_dfs: Dict[str, pd.DataFrame] = {}
    previous_hourly_oi: Dict[str, float] = {}
    last_hourly_slot = None
    cached_positions_map: Dict[str, dict] = {}
    missing_position_counts: Dict[str, int] = {}
    last_neutral_notice_hour: Optional[int] = None
    last_ny_notice_hour: Optional[int] = None
    last_daily_limit_notice_hour: Optional[int] = None
    regime_info = get_regime_gate_info("BTC-USDT")
    overall_regime = regime_info.get("overall_regime", "NEUTRAL")
    regime_strength = regime_info.get("regime_strength", {})
    last_screened_overall_regime = str(overall_regime).upper()
    force_regime_flip_screening = False

    # ========== フェーズB: MTFリアルタイム監視ループ (30秒サイクル省エネ運用) ==========
    while True:
        cycle_count += 1
        now_jst = datetime.now(JST)

        # -------------------------------------------------------------
        # [フェーズ C] 定期銘柄選定 (4時間ごと) & 地合い反転時オフスケジュール再選定
        # -------------------------------------------------------------
        today_date = now_jst.date()
        current_slot = (today_date, now_jst.hour)
        is_periodic_reset_time = (
            now_jst.hour in ANALYSIS_HOURS
            and now_jst.minute < 5
            and current_slot != last_screening_slot
        )
        if is_periodic_reset_time or force_regime_flip_screening:
            trigger_reason = "定期" if is_periodic_reset_time else "地合い反転"
            force_regime_flip_screening = False
            # 8時間節目（01:00, 09:00, 17:00 JST）の定期枠のみアスキーアートバナーおよびフルチャート画像を送信
            is_major_slot = bool(is_periodic_reset_time and now_jst.hour in [1, 9, 17])
            if is_major_slot:
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
                    f"  戦略態勢     : {strat_banner_label}\n"
                    f"  ローソク足   : {candle_banner_label}\n"
                    "==================================================\n"
                    "```"
                )
                discord.print_log(periodic_banner, force_send=True)
            discord.print_log(f"[Phase C] 定期銘柄選定時刻 ({now_jst.hour:02d}:00 JST) 到達。スクリーニング＆地合い判定を実行します (節目={is_major_slot})。")

            # 1. 定期スクリーニング＆最適化（チャート画像は節目 01/09/17 JST のみ送信、中間時間はスキップ）
            trade_side, new_symbol_params_map, best_params, selected_symbols = await run_screening_and_optimization(
                mode, send_charts=is_major_slot
            )

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

            if is_periodic_reset_time:
                last_screening_slot = current_slot
            regime_info = get_regime_gate_info("BTC-USDT")
            overall_regime = regime_info.get("overall_regime", "NEUTRAL")
            regime_strength = regime_info.get("regime_strength", {})
            last_screened_overall_regime = str(overall_regime).upper()
            discord.print_log(f"[Periodic Reset Complete ({trigger_reason})] 新しい選定銘柄 ({trade_side.upper()}): {', '.join(selected_symbols)} | 現在の全監視対象: {', '.join(symbol_apis.keys())}")

            # 8時間ごとの実運用トレード実績レポート
            # 8時間節目（01:00, 09:00, 17:00 JST）のみ実運用トレード実績レポート画像を送信
            if is_major_slot:
                await send_periodic_performance_report(symbol_apis, mode, label="定期パフォーマンスレポート")

        # -------------------------------------------------------------
        # [フェーズ B1] 毎時タスク (正時00:05 JST: OI蓄積 & 毎時サマリー送信)
        # -------------------------------------------------------------
        is_new_hour = (now_jst.hour != last_hourly_slot)
        if is_new_hour and now_jst.minute < 5:
            last_hourly_slot = now_jst.hour
            try:
                # BingX 全対象銘柄の建玉(OI)＆FRスナップショット自動蓄積
                try:
                    from bingx_oi_collector import collect_and_save_oi_snapshot
                    await asyncio.to_thread(collect_and_save_oi_snapshot)
                except Exception as oi_err:
                    discord.print_log(f"[OI Collector Warning] 建玉スナップショット保存警告: {oi_err}", level="debug")

                # 中間時間帯（定期 WF 非実行時）は軽量 1h 地合いのみ更新し、全体地合い反転時のみ Phase C を予約
                if now_jst.hour not in ANALYSIS_HOURS:
                    try:
                        from lake_live_bridge import refresh_hourly_regime_for_bot
                        reg_refresh = await asyncio.to_thread(refresh_hourly_regime_for_bot, sync_live=True)
                        regime_info = get_regime_gate_info("BTC-USDT")
                        overall_regime = regime_info.get("overall_regime", "NEUTRAL")
                        regime_strength = regime_info.get("regime_strength", {})
                        new_reg_up = str(reg_refresh.get("overall_regime", overall_regime)).upper()
                        if reg_refresh.get("tier_changed_ultra_buy"):
                            discord.print_log(
                                f"🔔 [F7 Tier Shift] ULTRA_BUY 状態変化: "
                                f"{reg_refresh.get('prev_strength_tier')} -> {reg_refresh.get('strength_tier')}",
                                force_send=True,
                            )
                        wf_ref = reg_refresh.get("h3b_wf90d") or {}
                        if wf_ref:
                            discord.print_log(
                                f"📊 [F7 Hourly Context] Tier={reg_refresh.get('strength_tier')} | "
                                f"BTC14d={float(reg_refresh.get('btc_ret14d', 0.0) or 0.0)*100:+.1f}% | "
                                f"WF90d={'PAUSED' if wf_ref.get('paused') else 'ACTIVE'}(n={wf_ref.get('window_trades', 0)},Net=${float(wf_ref.get('window_net', 0.0)):+.2f})"
                            )
                        if (
                            last_screened_overall_regime in ("BUY", "SELL", "NEUTRAL")
                            and new_reg_up in ("BUY", "SELL", "NEUTRAL")
                            and new_reg_up != last_screened_overall_regime
                        ):
                            force_regime_flip_screening = True
                            discord.print_log(
                                f"⚡ [Regime Flip] 毎時軽量判定で全体地合い反転を検知 "
                                f"({last_screened_overall_regime} -> {new_reg_up})。再スクリーニングを即時実行します。",
                                force_send=True,
                            )
                    except Exception as reg_err:
                        discord.print_log(f"[Hourly Regime Check Warning] 毎時地合い監視警告: {reg_err}", level="debug")

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
                    h_qty_buy = float(h_pos.get("buy", 0.0))
                    h_qty_sell = float(h_pos.get("sell", 0.0))
                    h_has_long = h_qty_buy > 0
                    h_has_short = h_qty_sell > 0
                    h_pnl = float(h_pos.get("profit", 0.0))
                    h_entry_px = float(h_pos.get("sell_pos" if h_has_short else "buy_pos", 0.0))
                    h_px = ws_mgr.get_latest_price(h_sym) or h_entry_px
                    if not h_px or h_px <= 0:
                        df_h = cached_5m_dfs.get(h_sym)
                        if df_h is not None and not df_h.empty and "close" in df_h.columns:
                            h_px = float(df_h["close"].iloc[-1])
                        else:
                            try:
                                bid, ask = get_bingx_orderbook(h_sym, h_api.bingx.base_url if h_api else None)
                                if h_has_short and ask and ask > 0:
                                    h_px = ask
                                elif bid and bid > 0:
                                    h_px = bid
                            except Exception:
                                pass
                    if not h_px or h_px <= 0:
                        h_px = 1.0

                    if h_has_short:
                        h_sign = "+" if h_pnl >= 0 else "-"
                        h_pos_tag = f"保有S({h_sign}${abs(h_pnl):.2f}/1枠)"
                    elif h_has_long:
                        h_sign = "+" if h_pnl >= 0 else "-"
                        h_pos_tag = f"保有L({h_sign}${abs(h_pnl):.2f}/1枠)"
                    elif h_sym in pending_entry_orders:
                        p_px = pending_entry_orders[h_sym].get("price", 0.0)
                        h_pos_tag = f"指値(${p_px:.4f})" if p_px < 10 else f"指値(${p_px:.2f})"
                    else:
                        h_pos_tag = "ノーポジ"

                    hourly_summary_rows.append({
                        "sym": h_sym,
                        "price": h_px,
                        "pos": h_pos_tag,
                    })

                if hourly_summary_rows:
                    tbl_header = f"{_pad_display('銘柄', 6)} {_pad_display('現在価格', 8, 'right')} {_pad_display('ポジション', 16)}"
                    tbl_divider = "────────────────────────────────"
                    acct_mode_str = "Live Account" if BINGX_IS_LIVE else "VST Demo Mode"
                    can_trade_today_h, _, daily_cnt_h = can_open_new_trade_today()
                    if DAILY_MAX_TRADES > 0:
                        daily_badge = f"🎯 本日トレード: [待機中 {daily_cnt_h}/{DAILY_MAX_TRADES}回]" if can_trade_today_h else f"🏁 本日トレード: [完了 {daily_cnt_h}/{DAILY_MAX_TRADES}回 - 翌日09:00まで休止]"
                    else:
                        daily_badge = f"🎯 本日トレード: [回数制限なし / {daily_cnt_h}回完了]" if can_trade_today_h else f"🚨 本日トレード: [CB停止中 ({daily_cnt_h}回) - 翌日09:00まで休止]"
                    summary_lines = [
                        f"⏰ **【BingX 毎時ステータスサマリー ({now_jst.strftime('%H:00')} JST)】**",
                        f"💰 残高: ${h_balance:,.2f} ({acct_mode_str}) | {daily_badge}",
                        f"🌐 地合い: {overall_regime} {regime_strength.get('strength_desc', '')}",
                    ]
                    if USE_F7_TWO_PILLAR_MODE:
                        _, wf90d_sum = _load_f7_wf90d_paused(regime_info)
                        f7_line_h = format_f7_hourly_status_line(
                            overall_regime=overall_regime,
                            strength_tier=regime_info.get("strength_tier", "BALANCED"),
                            btc_ret14d=float(regime_info.get("btc_ret14d", 0.0) or 0.0),
                            sym_ret14d_map=regime_info.get("sym_ret14d_map") or {},
                            wf90d=wf90d_sum,
                            excluded_symbols=regime_info.get("excluded_symbols") or [],
                        )
                        summary_lines.append(f7_line_h)
                    summary_lines.extend([
                        "```text",
                        tbl_header,
                        tbl_divider,
                    ])
                    for r in hourly_summary_rows:
                        px = r["price"]
                        px_str = f"${px:,.4f}" if px < 10 else (f"${px:,.2f}" if px < 1000 else f"${px:,.1f}")
                        clean_sym = r["sym"].replace("-USDT", "").replace("USDT", "")
                        summary_lines.append(
                            f"{_pad_display(clean_sym, 6)} {_pad_display(px_str, 8, 'right')} {_pad_display(r['pos'], 16)}"
                        )
                    summary_lines.append(tbl_divider)
                    next_hour_jst = (now_jst.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).strftime('%H:00')
                    summary_lines.append(f"次回定期更新: {next_hour_jst} JST (1時間ごとに定期配信中)")
                    summary_lines.append("```")
                    discord.print_log("\n".join(summary_lines), force_send=True)

                # 1日1回（毎日09:00 JST）直近14日間 バックテスト vs 実運用 日別比較レポート＆白ベース推移グラフ
                if now_jst.hour == 9:
                    try:
                        from real_trade_tracker import check_and_send_daily_14d_report
                        await asyncio.to_thread(
                            check_and_send_daily_14d_report,
                            current_balance=h_balance,
                            send_hour_jst=9,
                            exact_hour=True,
                            mark_daily_sent=True,
                            label="日次 14日間レポート",
                            now_jst=now_jst,
                        )
                    except Exception as d14_err:
                        discord.print_log(f"[Daily 14d Report Warning] 日次14日比較レポート警告: {d14_err}", level="warning")
            except Exception as hourly_err:
                discord.print_log(f"[Hourly Update Error] 毎時タスクエラー: {hourly_err}", level="warning")

        # -------------------------------------------------------------
        # [フェーズ B0] リアルタイム (30秒ごと): 全体地合い状態の判定 ＆ 5分足確定フラグ判定
        # -------------------------------------------------------------
        regime_info = get_regime_gate_info("BTC-USDT")
        overall_regime = regime_info.get("overall_regime", "NEUTRAL")
        regime_strength = regime_info.get("regime_strength", {})
        is_emergency_be = (overall_regime != "BUY")
        is_regime_safe = (regime_info.get("regime_gate") == "ALLOW")

        cur_5m_bucket = (now_jst.hour * 12) + (now_jst.minute // 5)
        is_candle_closed = (cur_5m_bucket != last_candle_sync_minute)
        if is_candle_closed:
            last_candle_sync_minute = cur_5m_bucket

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
        cycle_balance = await first_api.get_account(silent=True) if first_api else 1000.0

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
                            scale_in_states.pop(sym, None)
                            save_scale_in_states(scale_in_states)
                            trailing_tp_states.pop(sym, None)
                            save_trailing_tp_states(trailing_tp_states)
                            try:
                                await api.active_order_cancel()
                            except Exception:
                                pass
                            try:
                                from real_trade_tracker import sync_real_trades_from_bingx_api
                                await sync_real_trades_from_bingx_api(mode=mode)
                            except Exception:
                                pass
                            discord.print_log(f"⚠️ [{sym}] 取引所側でのポジション解消（外部決済または清算・ネイティブSL約定等）を検知・清掃しました。", level="warning")
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

                # リアルタイム現在価格（WebSocket鮮度切れ時はREST気配値へ確実にフォールバック）
                cur_px = ws_mgr.get_latest_price(sym)
                if not cur_px or cur_px <= 0:
                    cur_bid, cur_ask = get_bingx_orderbook(sym, api.bingx.base_url)
                    if is_short_pos:
                        cur_px = cur_ask or cur_bid or entry_px
                    else:
                        cur_px = cur_bid or cur_ask or entry_px

                # ローソク足取得（5分足確定時・未キャッシュ時・10分以上未更新時は最新5分足ストアから必ず同期更新）
                df_sym = cached_5m_dfs.get(sym)
                need_candle_refresh = is_candle_closed or (df_sym is None) or df_sym.empty
                if not need_candle_refresh and "timestamp" in df_sym.columns and len(df_sym) > 0:
                    try:
                        last_c_ts = pd.to_datetime(df_sym["timestamp"].iloc[-1], utc=True)
                        if (datetime.now(timezone.utc) - last_c_ts).total_seconds() > 600:
                            need_candle_refresh = True
                    except Exception:
                        need_candle_refresh = True

                if need_candle_refresh:
                    df_raw_init = await fetch_bingx_candles(sym, "5m", limit=5000, mode=mode)
                    if not df_raw_init.empty:
                        cached_5m_dfs[sym] = df_raw_init
                        df_sym = df_raw_init

                # 保有中: 出しっぱなし指値の約定検知！
                if sym in pending_entry_orders:
                    p_order = pending_entry_orders.pop(sym)
                    sym_params_p = dict(symbol_params_map.get(sym, best_params))
                    opt_inv_p = int(p_order.get("interval", 0) or sym_params_p.get("interval", 5) or 5)
                    pos_strat_p = str(p_order.get("strategy", "") or sym_params_p.get("strategy", "rsima")).lower()
                    for pk in (
                        "dynamic_mae_stop", "dynamic_timeout_bars", "mp", "target_ratio",
                        "s_ep", "s_cp", "l_ep", "l_cp",
                        "entry_atr", "init_sl", "target_tp", "be_px", "val", "vah", "poc",
                        "trough_price", "live_trough_price", "last_trough_bucket_open", "be_locked",
                    ):
                        if pk in p_order and p_order[pk] is not None:
                            sym_params_p[pk] = p_order[pk]

                    api_upd_ms = float(pos.get("updateTime", 0) or 0) if isinstance(pos, dict) else 0.0
                    fill_event_ts = (
                        api_upd_ms / 1000.0
                        if (api_upd_ms > 0 and (api_upd_ms / 1000.0) >= float(p_order.get("placed_at", 0) or 0) - 300)
                        else time.time()
                    )
                    df_p_eval = (
                        (resample_candles(df_sym, opt_inv_p, drop_unclosed=True) if opt_inv_p > 5 else df_sym.copy())
                        if (df_sym is not None and not df_sym.empty)
                        else pd.DataFrame()
                    )
                    vah_val = getattr(api, "entry_vah", entry_px * 1.02)
                    val_val = getattr(api, "entry_val", entry_px * 0.98)
                    poc_val = (vah_val + val_val) / 2.0

                    _build_and_store_live_fill_state(
                        sym=sym,
                        side="short" if is_short_pos else "long",
                        actual_fill_px=entry_px,
                        lot_size=pos_qty,
                        df_eval=df_p_eval,
                        sym_params_e=sym_params_p,
                        cand_interval_e=opt_inv_p,
                        cand_strat_e=pos_strat_p,
                        entry_reason=p_order.get("reason", "指値約定"),
                        vah_e=vah_val,
                        val_e=val_val,
                        poc_e=poc_val,
                        scale_in_states=scale_in_states,
                        trade_counts=trade_counts,
                        last_trade_time=last_trade_time,
                        last_nanpin_time=last_nanpin_time,
                        tracked_active_positions=tracked_active_positions,
                        pending_info=p_order,
                        fill_ts_override=fill_event_ts,
                        spawn_reporting=True,
                    )
                    # 余剰指値キャンセル
                    try:
                        await api.active_order_cancel()
                    except Exception:
                        pass

                tracked_active_positions.add(sym)
                active_positions[sym] = (has_pos, pos, entry_px, pos_qty, pnl_current)

                if df_sym is None or df_sym.empty or len(df_sym) < 20:
                    continue

                sym_params = symbol_params_map.get(sym, best_params)
                scale_state_cur = scale_in_states.get(sym, {})
                # ポジション保有中は、エントリー時の足種・戦略・パラメータを最優先で維持する (すり替わり完全防止)
                pos_interval = int(scale_state_cur.get("interval", 0) or sym_params.get("interval", 5) or 5)
                cand_interval = pos_interval
                inv_label_p = f"{cand_interval}m" if cand_interval < 60 else f"{cand_interval // 60}h"
                pos_strat = str(scale_state_cur.get("strategy", "") or sym_params.get("strategy", "rsima")).lower()
                cand_strat = pos_strat
                pos_mp = int(scale_state_cur.get("mp", 0) or sym_params.get("mp", 200))
                pos_tr = float(scale_state_cur.get("target_ratio", 0.0) or sym_params.get("target_ratio", 0.70))
                pos_s_ep = float(scale_state_cur.get("s_ep", 0.0) or sym_params.get("s_ep", 60.0))
                pos_s_cp = float(scale_state_cur.get("s_cp", 0.0) or sym_params.get("s_cp", 40.0))
                pos_l_ep = float(scale_state_cur.get("l_ep", 0.0) or sym_params.get("l_ep", 40.0))
                pos_l_cp = float(scale_state_cur.get("l_cp", 0.0) or sym_params.get("l_cp", 60.0))

                # 採用足リサンプリング (未確定足を除外し確定足のみで評価)
                df_eval = resample_candles(df_sym, cand_interval, drop_unclosed=True) if cand_interval > 5 else df_sym.copy()
                if is_f7_strategy(cand_strat):
                    entry_ts = float(scale_state_cur.get("initial_entry_time", 0.0))
                    if entry_ts <= 0:
                        api_update_ms = float(pos.get("updateTime", 0) or 0) if isinstance(pos, dict) else 0.0
                        entry_ts = (api_update_ms / 1000.0) if api_update_ms > 0 else time.time()
                        scale_state_cur["initial_entry_time"] = entry_ts
                        scale_in_states[sym] = scale_state_cur
                        save_scale_in_states(scale_in_states)
                    tf_sec_f7 = 1800 if cand_strat == F7_LONG_STRATEGY else 3600
                    fill_tf_open_ts = int(scale_state_cur.get("fill_tf_open_ts", 0) or ((int(entry_ts) // tf_sec_f7) * tf_sec_f7))
                    entry_atr_pos = float(scale_state_cur.get("entry_atr", 0.0) or 0.0)
                    if entry_atr_pos <= 0.0 and df_eval is not None and not df_eval.empty:
                        atr_arr_fb = _f7_calc_atr_series(
                            df_eval["high"].astype(float).to_numpy(),
                            df_eval["low"].astype(float).to_numpy(),
                            df_eval["close"].astype(float).to_numpy(),
                            14,
                        )
                        if len(atr_arr_fb) > 0:
                            entry_atr_pos = float(atr_arr_fb[-1])
                            scale_state_cur["entry_atr"] = entry_atr_pos
                    if cand_strat == F7_LONG_STRATEGY:
                        f7_ex = evaluate_val_poc_30m_exit(
                            entry_price=entry_px,
                            entry_atr=entry_atr_pos,
                            target_tp=float(scale_state_cur.get("target_tp", 0.0) or 0.0),
                            init_sl=scale_state_cur.get("init_sl"),
                            fill_bar_open_ts=fill_tf_open_ts,
                            current_price=cur_px,
                            overall_regime=overall_regime,
                        )
                    else:
                        f7_ex = evaluate_h3b_squeeze_1h_exit(
                            entry_price=entry_px,
                            entry_atr=entry_atr_pos,
                            init_sl=scale_state_cur.get("init_sl"),
                            fill_bar_open_ts=fill_tf_open_ts,
                            current_price=cur_px,
                            overall_regime=overall_regime,
                            closed_1h_df=df_eval,
                            trough_price=scale_state_cur.get("trough_price"),
                            be_locked=bool(scale_state_cur.get("be_locked", False)),
                            live_trough_price=scale_state_cur.get("live_trough_price"),
                            last_trough_bucket_open=int(scale_state_cur.get("last_trough_bucket_open", 0) or 0),
                        )
                        if (
                            scale_state_cur.get("trough_price") != f7_ex["trough_price"]
                            or scale_state_cur.get("live_trough_price") != f7_ex["live_trough_price"]
                            or int(scale_state_cur.get("last_trough_bucket_open", 0) or 0) != f7_ex["last_trough_bucket_open"]
                            or bool(scale_state_cur.get("be_locked", False)) != bool(f7_ex["be_locked"])
                        ):
                            scale_state_cur["trough_price"] = f7_ex["trough_price"]
                            scale_state_cur["live_trough_price"] = f7_ex["live_trough_price"]
                            scale_state_cur["last_trough_bucket_open"] = f7_ex["last_trough_bucket_open"]
                            scale_state_cur["be_locked"] = f7_ex["be_locked"]
                            scale_state_cur["sl_price"] = f7_ex["eff_sl"]
                            scale_in_states[sym] = scale_state_cur
                            save_scale_in_states(scale_in_states)
                    exit_reason = str(f7_ex.get("exit_reason") or "HOLD")
                    if f7_ex.get("should_exit"):
                        pos_action = "STOP_LOSS" if exit_reason == "STOP_LOSS" else "CLOSE_ALL"
                    else:
                        pos_action = "HOLD"
                        try:
                            eff_sl_cur = float(f7_ex.get("eff_sl", 0.0) or scale_state_cur.get("sl_price", 0.0) or 0.0)
                            side_str_ins = "short" if is_short_pos else "long"
                            ins_cand_px = compute_insurance_stop_price(side_str_ins, eff_sl_cur, cur_px)
                            cur_ins_px = float(scale_state_cur.get("protective_sl_price", 0.0) or 0.0)
                            cur_ins_oid = scale_state_cur.get("protective_sl_order_id")
                            fail_ts = float(scale_state_cur.get("protective_sl_fail_ts", 0.0) or 0.0)
                            if should_update_insurance_stop(side_str_ins, cur_ins_px, ins_cand_px) and (time.time() - fail_ts) >= 120.0:
                                new_oid = await api.sync_protective_stop_order(
                                    side_str_ins, pos_qty, ins_cand_px, prev_order_id=cur_ins_oid
                                )
                                if new_oid:
                                    scale_state_cur["protective_sl_price"] = float(api._quantize_price(ins_cand_px))
                                    scale_state_cur["protective_sl_order_id"] = str(new_oid)
                                    scale_state_cur.pop("protective_sl_fail_ts", None)
                                else:
                                    scale_state_cur["protective_sl_fail_ts"] = time.time()
                                scale_in_states[sym] = scale_state_cur
                                save_scale_in_states(scale_in_states)
                        except Exception as ins_err:
                            discord.print_log(f"⚠️ [{sym}] ネイティブ保険SL同期例外: {ins_err}", level="debug")
                else:
                    df_eval = logic.make_logic(
                        df_eval,
                        market_profile_period=pos_mp,
                        strategy_type=cand_strat,
                        num_bins=int(sym_params.get("num_bins", 50)),
                        value_area_pct=float(sym_params.get("value_area_pct", 0.70)),
                        tail_only=5,
                        lEp=pos_l_ep if not is_short_pos else pos_s_ep,
                        lCp=pos_l_cp if not is_short_pos else pos_s_cp,
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
                    fill_5m_open_ts = int(scale_state_cur.get("fill_5m_open_ts", 0) or ((int(entry_ts) // 300) * 300))
                    if int(scale_state_cur.get("fill_5m_open_ts", 0) or 0) <= 0:
                        scale_state_cur["fill_5m_open_ts"] = fill_5m_open_ts
                        scale_in_states[sym] = scale_state_cur
                        save_scale_in_states(scale_in_states)
                    cur_5m_open_ts = (int(time.time()) // 300) * 300
                    is_fill_bar_cur = (cur_5m_open_ts == fill_5m_open_ts)
                    latest_tf_close_ts = _extract_latest_close_ts_utc(df_eval, cand_interval)
                    has_new_tf_bar = (
                        (not is_fill_bar_cur)
                        and (latest_tf_close_ts > fill_5m_open_ts)
                        and (cand_interval <= 5 or cur_5m_open_ts == latest_tf_close_ts)
                    )
                    bars_held_cur = int(elapsed_min // max(1, cand_interval))
                    dyn_sl_pct = float(scale_state_cur.get("dynamic_mae_stop", 0.0) or sym_params.get("dynamic_mae_stop", -0.030))
                    dyn_to_bars = int(scale_state_cur.get("dynamic_timeout_bars", 0) or sym_params.get("dynamic_timeout_bars", max(3, 240 // max(1, cand_interval))))

                    # 地合い変化による緊急建値撤退判定
                    # ロング保有中かつ非BUY ➔ 建値+0.20%撤退 / ショート保有中かつ非SELL ➔ 建値-0.20%撤退
                    pos_emergency_be = (overall_regime != "SELL") if is_short_pos else (overall_regime != "BUY")

                    # 毎足動的再計算 (SSoT): recent_low/recent_high は None を渡し 3logic 側で iloc[-11:-1] を毎回最新化
                    sig_eval = _evaluate_strategy_signal(
                        cand_strat=cand_strat,
                        df_eval=df_eval,
                        current_pos_qty=pos_qty if is_short_pos else buy_qty,
                        current_trades_count=1 if is_short_pos else t_count,
                        avg_entry_price=entry_px,
                        last_entry_price=last_px_cur,
                        current_price=cur_px,
                        sym_params=sym_params,
                        val_price=val,
                        vah_price=vah,
                        pos_mp=pos_mp,
                        pos_tr=pos_tr,
                        pos_l_ep=pos_l_ep,
                        pos_l_cp=pos_l_cp,
                        pos_s_ep=pos_s_ep,
                        pos_s_cp=pos_s_cp,
                        is_cooldown_active=False,
                        is_emergency_breakeven=pos_emergency_be,
                        dynamic_sl_pct=dyn_sl_pct,
                        bars_held=bars_held_cur,
                        timeout_bars=dyn_to_bars,
                        is_fill_bar=is_fill_bar_cur,
                        elapsed_minutes=elapsed_min,
                        timeout_minutes=240.0,
                        max_hold_minutes=480.0,
                        has_new_tf_bar_after_fill=has_new_tf_bar,
                    )

                    # 決済判定はバックテスト SSoT (lake_backtest_engine / bingx5_46_3logic) と完全統一
                    pos_action = sig_eval.get("action", "HOLD")
                    exit_reason = sig_eval.get("reason", "HOLD")

                closed = False
                if pos_action == "STOP_LOSS":
                    discord.print_log(f"🛑 [{sym}] [{cand_strat.upper()} DYNAMIC MAE STOP LOSS] {exit_reason} ➔ 成行損切り", level="warning")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    closed = True
                elif pos_action == "TIMEOUT_EXIT":
                    discord.print_log(f"⏰ [{sym}] [{cand_strat.upper()} DYNAMIC TIMEOUT EXIT] {exit_reason} ➔ 動的タイムアウト成行撤退", level="warning")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    closed = True
                elif pos_action == "CLOSE_ALL":
                    discord.print_log(f"🎯 [{sym}] [{cand_strat.upper()} CLOSE ALL] {exit_reason} ➔ 一括成行決済")
                    await flatten_current_position_bingx(sym, "USDT", mode, exit_reason, force_market=True)
                    closed = True
                elif pos_action == "ADD_ENTRY" and not is_short_pos:
                    # 全戦略単発1本固定 (ナンピン完全停止)
                    if MAX_TRADES_COUNT <= 1:
                        pass
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
                    closed_meta = dict(scale_in_states.get(sym) or {})
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
                    est_fee_cb = (
                        (entry_px * pos_qty) * 0.0002 + (cur_px * pos_qty) * 0.0005
                        if (entry_px > 0 and pos_qty > 0 and cur_px > 0)
                        else 0.0
                    )
                    est_net_cb = round(float(pnl_current) - est_fee_cb, 4)
                    # 日次トレード実績および損失サーキットブレーカー用累計の記録 (推定手数料控除後)
                    record_daily_trade_completion(
                        symbol=sym,
                        side=side_tag,
                        pnl=est_net_cb,
                        reason=exit_reason
                    )
                    try:
                        from lake_live_bridge import record_live_exit_to_lake
                        record_live_exit_to_lake(
                            symbol=sym,
                            side=side_tag,
                            entry_px=entry_px,
                            exit_px=cur_px,
                            qty=pos_qty,
                            net_pnl_usdt=pnl_current,
                            exit_reason=exit_reason,
                            state_meta=closed_meta,
                        )
                    except Exception as lake_ex_err:
                        print(f"[Lake Write-Back Warning] {sym}: {lake_ex_err}")
                    asyncio.create_task(_async_post_exit_reporting(
                        sym=sym, side=side_tag, close_qty=pos_qty, reported_exit_px=cur_px,
                        entry_px=entry_px, pnl=pnl_current, exit_reason=exit_reason,
                        df_chart=df_eval, cycle_balance=cycle_balance,
                        interval_label=inv_label_p, mp_period=pos_mp, mode=mode
                    ))
            except Exception as pos_err:
                discord.print_log(f"⚠️ [{sym}] ポジション監視例外: {pos_err}", level="warning")

        # -------------------------------------------------------------
        # [フェーズ B3] リアルタイム (30秒ごと): F7 Two-Pillar / 3態勢地合いディフェンス ＆ 新規エントリー判定
        # -------------------------------------------------------------
        if USE_F7_TWO_PILLAR_MODE:
            now_utc_f7 = int(time.time())
            overall_u_f7 = str(overall_regime or "NEUTRAL").upper()
            tier_u_f7 = str(regime_info.get("strength_tier") or (regime_strength or {}).get("tier") or "BALANCED").upper()
            wf90d_paused_f7, wf90d_diag_f7 = _load_f7_wf90d_paused(regime_info)
            eu_now_f7 = is_eu_session_jst(now_utc_f7)
            long_active_f7 = (tier_u_f7 == "ULTRA_BUY") and (not eu_now_f7)
            short_active_f7 = (overall_u_f7 == "SELL") and (not wf90d_paused_f7)

            if not (long_active_f7 or short_active_f7):
                if tier_u_f7 == "ULTRA_BUY" and eu_now_f7:
                    idle_reason_f7 = "EU時間帯除外 (16:00-22:00 JST / val_poc_30m 新規停止)"
                elif overall_u_f7 == "SELL" and wf90d_paused_f7:
                    idle_reason_f7 = f"H3b WF90d休止中 (n={wf90d_diag_f7.get('window_trades',0)}, Net=${float(wf90d_diag_f7.get('window_net',0.0)):+.2f})"
                else:
                    idle_reason_f7 = f"F7待機態勢 (overall={overall_u_f7}, tier={tier_u_f7})"
                if now_jst.hour != last_neutral_notice_hour:
                    last_neutral_notice_hour = now_jst.hour
                    discord.print_log(f"⚪ [F7待機] {idle_reason_f7}", level="info")
                elif cycle_count % 20 == 0:
                    discord.print_log(f"⚪ [F7待機] {idle_reason_f7}", level="debug")
                await _cancel_all_pending_orders(idle_reason_f7, pending_entry_orders, symbol_apis)
            else:
                total_unrealized = sum(float(v[4]) for v in active_positions.values()) if active_positions else 0.0
                can_trade_today, daily_reason, _ = can_open_new_trade_today(unrealized_pnl=total_unrealized)
                if not can_trade_today:
                    await _cancel_all_pending_orders("日次損失CBガード", pending_entry_orders, symbol_apis)
                    if now_jst.hour != last_daily_limit_notice_hour:
                        last_daily_limit_notice_hour = now_jst.hour
                        discord.print_log(f"🛡️ [日次リスク管理] {daily_reason} (既存ポジション決済監視は継続)", level="info")
                    elif cycle_count % 30 == 0:
                        discord.print_log(f"🛡️ [日次リスク管理] {daily_reason}", level="debug")
                else:
                    active_strat_f7 = F7_LONG_STRATEGY if long_active_f7 else F7_SHORT_STRATEGY
                    active_iv_f7 = 30 if long_active_f7 else 60
                    is_short_f7 = not long_active_f7
                    active_side_low_f7 = "short" if is_short_f7 else "long"
                    active_side_up_f7 = "SHORT" if is_short_f7 else "LONG"
                    book_label_f7 = "Best Ask" if is_short_f7 else "Best Bid"
                    sig_col_f7 = "short" if is_short_f7 else "long"
                    pos_qty_key_f7 = "sell" if is_short_f7 else "buy"
                    pos_px_key_f7 = "sell_pos" if is_short_f7 else "buy_pos"

                    norm_excl_set_f7 = {normalize_symbol(str(x)) for x in (regime_info.get("excluded_symbols") or [])}
                    btc_ret14d_f7 = float(regime_info.get("btc_ret14d", 0.0) or 0.0)
                    sym_ret14d_map_f7 = regime_info.get("sym_ret14d_map") or {}

                    def _f7_gate_ok_for_sym(sym_chk: str) -> Tuple[bool, str]:
                        ns_chk = normalize_symbol(sym_chk)
                        if sym_chk in EXCLUDED_MEGA_CAPS or get_clean_symbol(sym_chk) in EXCLUDED_MEGA_CAPS:
                            return False, "除外メガキャップ"
                        if ns_chk in norm_excl_set_f7:
                            return False, "ノーマライズ監査除外"
                        if is_short_f7:
                            sr_14 = float(sym_ret14d_map_f7.get(ns_chk, 0.0) or 0.0)
                            noexh_ok, noexh_r = check_f7_no_exhaustion(btc_ret14d_f7, sr_14)
                            if not noexh_ok:
                                return False, noexh_r
                        return True, "OK"

                    async def _eval_f7_symbol_signal(sym_ev: str) -> Optional[Dict[str, Any]]:
                        if is_candle_closed or sym_ev not in cached_5m_dfs or cached_5m_dfs[sym_ev].empty or len(cached_5m_dfs[sym_ev]) < 4800:
                            df_raw_ev = await fetch_bingx_candles(sym_ev, "5m", limit=5000, mode=mode)
                            if not df_raw_ev.empty:
                                cached_5m_dfs[sym_ev] = df_raw_ev
                        df_5m_ev = cached_5m_dfs.get(sym_ev)
                        if df_5m_ev is None or df_5m_ev.empty or len(df_5m_ev) < 200:
                            return None
                        df_tf_ev = resample_candles(df_5m_ev, active_iv_f7, drop_unclosed=True)
                        if df_tf_ev is None or df_tf_ev.empty:
                            return None
                        ns_ev = normalize_symbol(sym_ev)
                        is_nex = ns_ev in norm_excl_set_f7
                        if is_short_f7:
                            sr_ev = float(sym_ret14d_map_f7.get(ns_ev, 0.0) or 0.0)
                            ev_r = evaluate_h3b_squeeze_1h_entry(
                                df_tf_ev,
                                overall_regime=overall_u_f7,
                                is_norm_excluded=is_nex,
                                btc_ret14d=btc_ret14d_f7,
                                sym_ret14d=sr_ev,
                                wf90d_paused=wf90d_paused_f7,
                            )
                        else:
                            ev_r = evaluate_val_poc_30m_entry(
                                df_tf_ev,
                                strength_tier=tier_u_f7,
                                is_norm_excluded=is_nex,
                                now_utc=now_utc_f7,
                            )
                        ev_r["symbol"] = sym_ev
                        return ev_r

                    # --- Pass 1: 既存の未約定待機指値を検証 & 5分足確定追随 ---
                    for sym_p in list(pending_entry_orders.keys()):
                        try:
                            if sym_p in active_positions:
                                pending_entry_orders.pop(sym_p, None)
                                continue
                            po = pending_entry_orders[sym_p]
                            if str(po.get("strategy", "")).lower() != active_strat_f7:
                                await _cancel_pending_order_for_symbol(sym_p, "F7稼働戦略切替", pending_entry_orders, symbol_apis)
                                continue
                            g_ok_p, g_reason_p = _f7_gate_ok_for_sym(sym_p)
                            if not g_ok_p:
                                await _cancel_pending_order_for_symbol(sym_p, g_reason_p, pending_entry_orders, symbol_apis)
                                continue
                            if not is_candle_closed:
                                continue

                            max_chase = 3
                            chase_used = int(po.get("chase_count", 0)) + 1
                            po["chase_count"] = chase_used
                            ev_re = await _eval_f7_symbol_signal(sym_p)
                            if ev_re and int(ev_re.get("sig_bar_close_ts", 0) or 0) > 0:
                                last_signal_tf_close_map[(sym_p, active_strat_f7, active_iv_f7)] = int(ev_re["sig_bar_close_ts"])
                            if chase_used >= max_chase:
                                await _cancel_pending_order_for_symbol(sym_p, f"F7追随上限({chase_used}/{max_chase})", pending_entry_orders, symbol_apis)
                                continue
                            if not ev_re or not ev_re.get("should_enter"):
                                await _cancel_pending_order_for_symbol(sym_p, "F7シグナル終了", pending_entry_orders, symbol_apis)
                                continue

                            api = symbol_apis.get(sym_p)
                            if api:
                                cur_px_p = ws_mgr.get_latest_price(sym_p)
                                if not cur_px_p or cur_px_p <= 0:
                                    bid_p, ask_p = get_bingx_orderbook(sym_p, api.bingx.base_url)
                                    cur_px_p = (ask_p if is_short_f7 else bid_p) or float(ev_re.get("close", 0.0))
                                spec = api.bingx.instrument_spec or {}
                                lot_size = compute_lot_size(cur_px_p, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                                df_re = ev_re["signal_df"].copy()
                                df_re[sig_col_f7] = True
                                entry_fn = api.short_entry if is_short_f7 else api.long_entry
                                entered_re = await entry_fn(df_re, {}, cycle_balance, lot_size, max_lot)
                                if entered_re:
                                    new_pos = await api.get_positions()
                                    if float(new_pos.get(pos_qty_key_f7, 0.0)) > 0:
                                        actual_fill_px = float(new_pos.get(pos_px_key_f7, 0.0)) or cur_px_p
                                        pending_entry_orders.pop(sym_p, None)
                                        _build_and_store_live_fill_state(
                                            sym=sym_p,
                                            side=active_side_low_f7,
                                            actual_fill_px=actual_fill_px,
                                            lot_size=lot_size,
                                            df_eval=ev_re["signal_df"],
                                            sym_params_e={**(symbol_params_map.get(sym_p) or {}), **ev_re},
                                            cand_interval_e=active_iv_f7,
                                            cand_strat_e=active_strat_f7,
                                            entry_reason=f"F7 {active_strat_f7} 指値追随約定 (vr={ev_re.get('vol_ratio', 0.0):.2f})",
                                            vah_e=float(ev_re.get("vah", 0.0) or cur_px_p * 1.02),
                                            val_e=float(ev_re.get("val", 0.0) or cur_px_p * 0.98),
                                            poc_e=float(ev_re.get("poc", 0.0) or cur_px_p),
                                            scale_in_states=scale_in_states,
                                            trade_counts=trade_counts,
                                            last_trade_time=last_trade_time,
                                            last_nanpin_time=last_nanpin_time,
                                            tracked_active_positions=tracked_active_positions,
                                            pending_info={**po, **ev_re},
                                            spawn_reporting=True,
                                        )
                                    else:
                                        po["price"] = cur_px_p
                                        discord.print_log(
                                            f"📌 [{sym_p}] {active_side_up_f7}指値追随 ({chase_used}/{max_chase - 1}, {book_label_f7}: ~${cur_px_p:.4f})"
                                        )
                        except Exception as pe_err:
                            discord.print_log(f"⚠️ [{sym_p}] F7追随例外: {pe_err}", level="warning")

                    # --- Pass 2: 新規候補を全選定し (vol_ratio DESC, symbol ASC) で空き枠に配分 ---
                    free_slots_f7 = MAX_ACTIVE_POSITIONS - (len(active_positions) + len(pending_entry_orders))
                    if free_slots_f7 > 0:
                        f7_candidates: List[Dict[str, Any]] = []
                        for sym_c in selected_symbols:
                            if sym_c in active_positions or sym_c in pending_entry_orders:
                                continue
                            g_ok_c, _ = _f7_gate_ok_for_sym(sym_c)
                            if not g_ok_c:
                                continue
                            try:
                                ev_c = await _eval_f7_symbol_signal(sym_c)
                                if not ev_c or not ev_c.get("should_enter"):
                                    continue
                                sig_close_c = int(ev_c.get("sig_bar_close_ts", 0) or 0)
                                if sig_close_c > 0 and last_signal_tf_close_map.get((sym_c, active_strat_f7, active_iv_f7)) == sig_close_c:
                                    continue
                                f7_candidates.append(ev_c)
                            except Exception as cand_err:
                                discord.print_log(f"⚠️ [{sym_c}] F7候補評価例外: {cand_err}", level="warning")

                        f7_candidates.sort(key=lambda c: (-float(c.get("vol_ratio", 0.0)), normalize_symbol(str(c["symbol"]))))
                        for ev_win in f7_candidates[:free_slots_f7]:
                            sym_w = str(ev_win["symbol"])
                            try:
                                sig_close_w = int(ev_win.get("sig_bar_close_ts", 0) or 0)
                                df_w = ev_win["signal_df"]
                                api = symbol_apis.get(sym_w)
                                if not api:
                                    api = api_bingx(symbol=sym_w, mode=mode)
                                    spec = fetch_instrument_spec_bingx(sym_w, mode)
                                    if spec:
                                        api.update_instrument_spec(spec)
                                    symbol_apis[sym_w] = api

                                cur_px_w = ws_mgr.get_latest_price(sym_w)
                                if not cur_px_w or cur_px_w <= 0:
                                    bid_w, ask_w = get_bingx_orderbook(sym_w, api.bingx.base_url)
                                    cur_px_w = (ask_w if is_short_f7 else bid_w) or float(ev_win.get("close", 0.0))

                                spec = api.bingx.instrument_spec or {}
                                lot_size = compute_lot_size(cur_px_w, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                                if lot_size <= 0:
                                    continue

                                pos = await api.get_positions()
                                if float(pos.get(pos_qty_key_f7, 0.0)) > 0:
                                    continue

                                val_w = float(ev_win.get("val", 0.0) or cur_px_w * 0.98)
                                vah_w = float(ev_win.get("vah", 0.0) or cur_px_w * 1.02)
                                poc_w = float(ev_win.get("poc", 0.0) or cur_px_w)
                                api.entry_val = val_w
                                api.entry_vah = vah_w

                                entry_reason_w = f"F7 {active_strat_f7} ({active_iv_f7}m / vr={ev_win.get('vol_ratio', 0.0):.2f} / ATR14={ev_win.get('atr14', 0.0):.4g})"
                                discord.print_log(f"⚡ [{sym_w}] [{active_strat_f7.upper()} 初期{active_side_up_f7}エントリー>>] {entry_reason_w}")
                                df_send = df_w.copy()
                                df_send[sig_col_f7] = True
                                entry_fn = api.short_entry if is_short_f7 else api.long_entry
                                entered = await entry_fn(df_send, pos, cycle_balance, lot_size, max_lot)

                                if entered:
                                    if sig_close_w > 0:
                                        last_signal_tf_close_map[(sym_w, active_strat_f7, active_iv_f7)] = sig_close_w
                                    new_pos = await api.get_positions()
                                    sym_params_w = {**(symbol_params_map.get(sym_w) or {}), "entry_atr": float(ev_win.get("atr14", 0.0)), "val": val_w, "vah": vah_w, "poc": poc_w}
                                    if float(new_pos.get(pos_qty_key_f7, 0.0)) > 0:
                                        actual_fill_px = float(new_pos.get(pos_px_key_f7, 0.0)) or cur_px_w
                                        _build_and_store_live_fill_state(
                                            sym=sym_w,
                                            side=active_side_low_f7,
                                            actual_fill_px=actual_fill_px,
                                            lot_size=lot_size,
                                            df_eval=df_w,
                                            sym_params_e=sym_params_w,
                                            cand_interval_e=active_iv_f7,
                                            cand_strat_e=active_strat_f7,
                                            entry_reason=entry_reason_w,
                                            vah_e=vah_w,
                                            val_e=val_w,
                                            poc_e=poc_w,
                                            scale_in_states=scale_in_states,
                                            trade_counts=trade_counts,
                                            last_trade_time=last_trade_time,
                                            last_nanpin_time=last_nanpin_time,
                                            tracked_active_positions=tracked_active_positions,
                                            pending_info=sym_params_w,
                                            spawn_reporting=True,
                                        )
                                    else:
                                        pending_entry_orders[sym_w] = {
                                            "placed_at": time.time(),
                                            "price": cur_px_w,
                                            "lot": lot_size,
                                            "reason": entry_reason_w,
                                            "interval": active_iv_f7,
                                            "strategy": active_strat_f7,
                                            "chase_count": 0,
                                            "entry_atr": float(ev_win.get("atr14", 0.0)),
                                            "init_sl": float(ev_win.get("init_sl", 0.0)),
                                            "target_tp": float(ev_win.get("target_tp", 0.0)),
                                            "be_px": float(ev_win.get("be_px", 0.0)),
                                            "val": val_w,
                                            "vah": vah_w,
                                            "poc": poc_w,
                                            "mp": 120,
                                            "target_ratio": 0.70,
                                            "side": active_side_low_f7,
                                            "vol_ratio": float(ev_win.get("vol_ratio", 0.0)),
                                        }
                                        discord.print_log(f"📌 [{sym_w}] {book_label_f7} {active_side_up_f7}指値を配置し待機中 (数量={lot_size}, 価格=~${cur_px_w:.4f})")
                            except Exception as w_err:
                                discord.print_log(f"⚠️ [{sym_w}] F7発注例外: {w_err}", level="warning")
        else:
            gate_allowed_macro = regime_info.get("allowed_strategies", []) if "regime_info" in locals() else []
            is_neutral_stop = (overall_regime == "NEUTRAL" and not gate_allowed_macro)

            if is_neutral_stop:
                # ===== NEUTRAL (拮抗中立 BALANCED 相場): 新規エントリー完全停止 ＆ 待機指値即時キャンセル =====
                # 1時間に1回のみDiscord通知 (毎時初回)、それ以外のループはローカルdebugログのみ
                strength_desc = ""
                if regime_strength:
                    t_jp = regime_strength.get("tier_jp", "中立")
                    n_sc = regime_strength.get("net_score", 0.0)
                    sign_sc = f"+{n_sc:.2f}" if n_sc > 0 else f"{n_sc:.2f}"
                    strength_desc = f"【{t_jp} (スコア: {sign_sc})】"

                notice_msg = f"⚪ [地合い中立待機] 総合地合い中立{strength_desc}のため新規エントリーを完全停止中 (ノーポジ待機・既存ポジション建値早期撤退のみ)"

                if now_jst.hour != last_neutral_notice_hour:
                    last_neutral_notice_hour = now_jst.hour
                    discord.print_log(notice_msg, level="info")
                elif cycle_count % 20 == 0:
                    discord.print_log(notice_msg, level="debug")
                await _cancel_all_pending_orders("地合い中立退避", pending_entry_orders, symbol_apis)
            else:
                # ===== トレード稼働態勢 (BUY / SELL / LEAN_BUY / LEAN_SELL) =====
                is_sell_regime = (overall_regime == "SELL")

                # JST 時間帯ロール制判定
                now_jst_b3 = datetime.now(timezone.utc) + timedelta(hours=9)
                hour_jst_b3 = now_jst_b3.hour
                is_tokyo_b3 = (9 <= hour_jst_b3 < 16)
                is_london_b3 = (16 <= hour_jst_b3 < 22)
                is_ny_b3 = not (is_tokyo_b3 or is_london_b3)

                if is_ny_b3:
                    mode_tag = "本番リアル運用" if BINGX_IS_LIVE else "デモアクティブ検証"
                    if should_block_ny_session_entry(BINGX_IS_LIVE, is_ny_b3):
                        if now_jst_b3.hour != last_ny_notice_hour:
                            last_ny_notice_hour = now_jst_b3.hour
                            discord.print_log(f"🌙 [{mode_tag}] 米国セッション(22:00〜翌09:00 JST) 新規エントリー停止中 (既存ポジション標準出口監視のみ)", level="info")
                        await _cancel_all_pending_orders("NYセッション停止", pending_entry_orders, symbol_apis)
                        continue
                    if now_jst_b3.hour != last_ny_notice_hour:
                        last_ny_notice_hour = now_jst_b3.hour
                        discord.print_log(f"🌙 [{mode_tag}] 米国セッション(22:00〜翌09:00 JST) 24時間フル稼働中 (バックテストSSoT準拠監視)", level="info")
                    elif cycle_count % 30 == 0:
                        discord.print_log(f"🌙 [{mode_tag}] 米国セッション(22:00〜翌09:00 JST) 24時間フル稼働中 (バックテストSSoT準拠監視)", level="debug")

                # 🛡️ 日次リスクガードチェック (含み損考慮サーキットブレーカー / 回数制限解除済み)
                total_unrealized = sum(float(v[4]) for v in active_positions.values()) if active_positions else 0.0
                can_trade_today, daily_reason, daily_cnt = can_open_new_trade_today(unrealized_pnl=total_unrealized)
                if not can_trade_today:
                    await _cancel_all_pending_orders("日次損失CBガード", pending_entry_orders, symbol_apis)
                    if now_jst_b3.hour != last_daily_limit_notice_hour:
                        last_daily_limit_notice_hour = now_jst_b3.hour
                        discord.print_log(f"🛡️ [日次リスク管理] {daily_reason} (既存ポジション決済監視は継続)", level="info")
                    elif cycle_count % 30 == 0:
                        discord.print_log(f"🛡️ [日次リスク管理] {daily_reason}", level="debug")
                    continue

                gate_allowed = regime_info.get("allowed_strategies", []) if "regime_info" in locals() else []

                for sym in selected_symbols:
                    if sym in active_positions:
                        continue

                    try:
                        raw_sym_params = symbol_params_map.get(sym, best_params)
                        sym_params_e, cand_strat_e, cand_side = await check_and_reconcile_symbol_gate(
                            sym=sym,
                            raw_sym_params=raw_sym_params,
                            is_tokyo=is_tokyo_b3,
                            is_london=is_london_b3,
                            gate_allowed=gate_allowed,
                            excluded_mega_caps=EXCLUDED_MEGA_CAPS,
                            pending_entry_orders=pending_entry_orders,
                            symbol_apis=symbol_apis,
                        )
                        if sym_params_e is None or cand_side is None:
                            continue

                        cand_interval_e = int(sym_params_e.get("interval", 5) or 5)

                        # 既に同スペックの指値待機中かつ5m境界未到達なら注文キュー優先順位を維持
                        if sym in pending_entry_orders and not is_candle_closed:
                            continue

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
                            cur_px = (cur_ask if cand_side == "SHORT" else cur_bid) or float(df_5m["close"].iloc[-1])

                        df_entry_eval = resample_candles(df_5m, cand_interval_e, drop_unclosed=True) if cand_interval_e > 5 else df_5m.copy()
                        try:
                            from bingx_oi_collector import enrich_candles_with_oi_fr
                            df_entry_eval = enrich_candles_with_oi_fr(
                                df_entry_eval,
                                sym,
                                auto_sync=True,
                                timeframe=f"{cand_interval_e}m",
                            )
                        except Exception:
                            pass
                        df_entry_eval = logic.make_logic(
                            df_entry_eval,
                            market_profile_period=int(sym_params_e.get("mp", 200)),
                            strategy_type=cand_strat_e,
                            num_bins=int(sym_params_e.get("num_bins", 50)),
                            value_area_pct=float(sym_params_e.get("value_area_pct", 0.70)),
                            tail_only=5,
                            lEp=float(sym_params_e.get("s_ep", 60.0) if cand_side == "SHORT" else sym_params_e.get("l_ep", 40.0)),
                            lCp=float(sym_params_e.get("s_cp", 40.0) if cand_side == "SHORT" else sym_params_e.get("l_cp", 60.0)),
                        )

                        vah_e = float(df_entry_eval["VAH"].iloc[-1]) if "VAH" in df_entry_eval.columns else cur_px * 1.02
                        val_e = float(df_entry_eval["VAL"].iloc[-1]) if "VAL" in df_entry_eval.columns else cur_px * 0.98
                        poc_e = float(df_entry_eval["POC"].iloc[-1]) if "POC" in df_entry_eval.columns else cur_px

                        sig_entry = _evaluate_strategy_signal(
                            cand_strat=cand_strat_e,
                            df_eval=df_entry_eval,
                            current_pos_qty=0.0,
                            current_trades_count=0,
                            avg_entry_price=0.0,
                            last_entry_price=0.0,
                            current_price=cur_px,
                            sym_params=sym_params_e,
                            val_price=val_e,
                            vah_price=vah_e,
                            pos_mp=int(sym_params_e.get("mp", 200)),
                            pos_tr=float(sym_params_e.get("target_ratio", 0.70)),
                            pos_l_ep=float(sym_params_e.get("l_ep", 40.0)),
                            pos_l_cp=float(sym_params_e.get("l_cp", 60.0)),
                            pos_s_ep=float(sym_params_e.get("s_ep", 60.0)),
                            pos_s_cp=float(sym_params_e.get("s_cp", 40.0)),
                            is_cooldown_active=False,
                        )

                        is_short_e = (cand_side == "SHORT")
                        expected_action = "INITIAL_ENTRY_SHORT" if is_short_e else "INITIAL_ENTRY"
                        pos_qty_key = "sell" if is_short_e else "buy"
                        pos_px_key = "sell_pos" if is_short_e else "buy_pos"
                        sig_col_name = "short" if is_short_e else "long"
                        book_label = "Best Ask" if is_short_e else "Best Bid"
                        side_low = "short" if is_short_e else "long"

                        entry_action = sig_entry.get("action", "NONE")
                        entry_reason = sig_entry.get("reason", expected_action)
                        sig_tf_close_ts = _extract_latest_close_ts_utc(df_entry_eval, cand_interval_e)

                        # ケース 1: 同スペックの指値待機中 (5m=最大3回追随 / 15m=最大1本待機で終了)
                        if sym in pending_entry_orders:
                            if is_candle_closed:
                                po = pending_entry_orders[sym]
                                max_chase = 3 if cand_interval_e == 5 else 1
                                chase_used = int(po.get("chase_count", 0)) + 1
                                po["chase_count"] = chase_used
                                if sig_tf_close_ts > 0:
                                    last_signal_tf_close_map[(sym, cand_strat_e, cand_interval_e)] = sig_tf_close_ts
                                if chase_used >= max_chase:
                                    await _cancel_pending_order_for_symbol(
                                        sym,
                                        f"追随上限到達({chase_used}/{max_chase})",
                                        pending_entry_orders,
                                        symbol_apis,
                                    )
                                elif entry_action != expected_action:
                                    await _cancel_pending_order_for_symbol(
                                        sym,
                                        f"{cand_side}サイン終了",
                                        pending_entry_orders,
                                        symbol_apis,
                                    )
                                else:
                                    api = symbol_apis.get(sym)
                                    if api:
                                        spec = api.bingx.instrument_spec or {}
                                        lot_size = compute_lot_size(cur_px, BINGX_TARGET_POSITION_VALUE_USDT, spec)
                                        df_re = df_entry_eval.copy()
                                        df_re[sig_col_name] = True
                                        entry_fn = api.short_entry if is_short_e else api.long_entry
                                        entered_re = await entry_fn(df_re, {}, cycle_balance, lot_size, max_lot)
                                        if entered_re:
                                            new_pos = await api.get_positions()
                                            if float(new_pos.get(pos_qty_key, 0.0)) > 0:
                                                actual_fill_px = float(new_pos.get(pos_px_key, 0.0)) or cur_px
                                                pending_entry_orders.pop(sym, None)
                                                _build_and_store_live_fill_state(
                                                    sym=sym,
                                                    side=side_low,
                                                    actual_fill_px=actual_fill_px,
                                                    lot_size=lot_size,
                                                    df_eval=df_entry_eval,
                                                    sym_params_e=sym_params_e,
                                                    cand_interval_e=cand_interval_e,
                                                    cand_strat_e=cand_strat_e,
                                                    entry_reason=entry_reason,
                                                    vah_e=vah_e,
                                                    val_e=val_e,
                                                    poc_e=poc_e,
                                                    scale_in_states=scale_in_states,
                                                    trade_counts=trade_counts,
                                                    last_trade_time=last_trade_time,
                                                    last_nanpin_time=last_nanpin_time,
                                                    tracked_active_positions=tracked_active_positions,
                                                    pending_info=sym_params_e,
                                                    spawn_reporting=True,
                                                )
                                            else:
                                                pending_entry_orders[sym]["price"] = cur_px
                                                discord.print_log(
                                                    f"📌 [{sym}] {cand_side}指値追随 ({chase_used}/{max_chase - 1}, {book_label}: ~${cur_px:.4f})"
                                                )
                            continue

                        # ケース 2: 新規指値発注 (保有+待機 <= MAX_ACTIVE_POSITIONS かつ 同一確定足重複除外)
                        if (len(active_positions) + len(pending_entry_orders)) >= MAX_ACTIVE_POSITIONS:
                            continue
                        if entry_action != expected_action:
                            continue
                        if sig_tf_close_ts > 0 and last_signal_tf_close_map.get((sym, cand_strat_e, cand_interval_e)) == sig_tf_close_ts:
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
                        if float(pos.get(pos_qty_key, 0.0)) > 0:
                            continue

                        api.entry_val = val_e
                        api.entry_vah = vah_e

                        discord.print_log(f"⚡ [{sym}] [{cand_strat_e.upper()} 初期{cand_side}エントリー>>] {entry_reason}")
                        df_entry_send = df_entry_eval.copy()
                        df_entry_send[sig_col_name] = True
                        entry_fn = api.short_entry if is_short_e else api.long_entry
                        entered = await entry_fn(df_entry_send, pos, cycle_balance, lot_size, max_lot)

                        if entered:
                            if sig_tf_close_ts > 0:
                                last_signal_tf_close_map[(sym, cand_strat_e, cand_interval_e)] = sig_tf_close_ts
                            new_pos = await api.get_positions()
                            if float(new_pos.get(pos_qty_key, 0.0)) > 0:
                                actual_fill_px = float(new_pos.get(pos_px_key, 0.0)) or cur_px
                                _build_and_store_live_fill_state(
                                    sym=sym,
                                    side=side_low,
                                    actual_fill_px=actual_fill_px,
                                    lot_size=lot_size,
                                    df_eval=df_entry_eval,
                                    sym_params_e=sym_params_e,
                                    cand_interval_e=cand_interval_e,
                                    cand_strat_e=cand_strat_e,
                                    entry_reason=entry_reason,
                                    vah_e=vah_e,
                                    val_e=val_e,
                                    poc_e=poc_e,
                                    scale_in_states=scale_in_states,
                                    trade_counts=trade_counts,
                                    last_trade_time=last_trade_time,
                                    last_nanpin_time=last_nanpin_time,
                                    tracked_active_positions=tracked_active_positions,
                                    pending_info=sym_params_e,
                                    spawn_reporting=True,
                                )
                            else:
                                pending_entry_orders[sym] = {
                                    "placed_at": time.time(),
                                    "price": cur_px,
                                    "lot": lot_size,
                                    "reason": entry_reason,
                                    "interval": cand_interval_e,
                                    "strategy": cand_strat_e,
                                    "chase_count": 0,
                                    "dynamic_mae_stop": float(sym_params_e.get("dynamic_mae_stop", -0.030)),
                                    "dynamic_timeout_bars": int(sym_params_e.get("dynamic_timeout_bars", max(3, 240 // max(1, cand_interval_e)))),
                                    "mp": int(sym_params_e.get("mp", 200)),
                                    "target_ratio": float(sym_params_e.get("target_ratio", 0.70)),
                                    "s_ep": float(sym_params_e.get("s_ep", 60.0)),
                                    "s_cp": float(sym_params_e.get("s_cp", 40.0)),
                                    "l_ep": float(sym_params_e.get("l_ep", 40.0)),
                                    "l_cp": float(sym_params_e.get("l_cp", 60.0)),
                                    "side": side_low,
                                    "lake_selection_id": sym_params_e.get("lake_selection_id"),
                                    "lake_run_id": sym_params_e.get("lake_run_id"),
                                }
                                discord.print_log(f"📌 [{sym}] {book_label} {cand_side}指値を配置し待機中 (数量={lot_size}, 価格=~${cur_px:.4f})")
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

