#!/usr/bin/env python
# coding: utf-8
"""
Regime Filter Backtest Verification Script
パターンA: 高値圏ロング停止＆出来高細り検知 ＋ ジリ下げ静観 ＋ セリクラ下ヒゲ陽線ロング再開

検証目的:
- レジームフィルター適用前（Baseline）と適用後（With Regime Filter）の成績を比較
- 損益(PnL), 勝率(Win Rate), 最大ドローダウン(MaxDD), プロフィットファクター(PF), トレード回数の変化を定量測定
"""

import sys
import math
from pathlib import Path
from typing import Dict, List, Tuple, Any
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# --- 指標計算ユーティリティ ---
def calc_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    rma_up = calc_rma(up, length)
    rma_down = calc_rma(down, length)
    rs = rma_up / rma_down.replace(0.0, np.nan)
    rsi = np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))
    return pd.Series(rsi, index=series.index).fillna(50.0)

def calc_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


class RegimeFilterSimulator:
    def __init__(
        self,
        lookback_high: int = 48,
        stagnation_bars: int = 24,
        vol_decay_ratio: float = 0.80,
        btc_drop_pct: float = -0.05,
        alt_drop_pct: float = -0.10,
        vol_surge_mult: float = 2.0,
        rsi_bottom_th: float = 35.0,
        lower_wick_ratio: float = 0.35,
    ):
        self.lookback_high = lookback_high
        self.stagnation_bars = stagnation_bars
        self.vol_decay_ratio = vol_decay_ratio
        self.btc_drop_pct = btc_drop_pct
        self.alt_drop_pct = alt_drop_pct
        self.vol_surge_mult = vol_surge_mult
        self.rsi_bottom_th = rsi_bottom_th
        self.lower_wick_ratio = lower_wick_ratio

    def compute_regime(self, df: pd.DataFrame, is_btc: bool = False) -> pd.Series:
        """
        各バー時点でのロング許可状態 (True: 許可, False: 停止) を算出する
        State Machine:
          0: NORMAL (ロング許可)
          1: TOP_EXHAUSTION (高値圏・出来高細り検知 -> 新規ロング停止)
          2: DOWN_WAITING (下落待機中 -> ジリ下げ静観)
          3: BOTTOM_WATCHING (出来高急増 + RSI売られすぎ検知 -> 下ヒゲ陽線待ち)
        """
        n = len(df)
        long_allowed = np.ones(n, dtype=bool)
        
        # 必要な指標の事前計算
        close = df['close'].values
        open_ = df['open'].values
        high = df['high'].values
        low = df['low'].values
        vol = df['volume'].values
        
        vol_s = pd.Series(vol)
        vol_sma20 = vol_s.rolling(20, min_periods=5).mean().values
        vol_sma24 = vol_s.rolling(24, min_periods=5).mean().values
        vol_sma72 = vol_s.rolling(72, min_periods=10).mean().values
        
        rsi14 = calc_rsi(df['close'], 14).values
        
        # 下ヒゲ陽線判定
        is_bullish = close > open_
        body = np.abs(close - open_)
        lower_wick = np.where(is_bullish, open_ - low, close - low)
        total_candle_range = high - low + 1e-9
        
        is_hammer_bull = (
            is_bullish & 
            (lower_wick >= body * 0.8) & 
            ((lower_wick / total_candle_range) >= self.lower_wick_ratio)
        )
        
        state = 0  # 0: NORMAL
        recent_peak_price = close[0]
        peak_bar_idx = 0
        target_drop = self.btc_drop_pct if is_btc else self.alt_drop_pct
        
        regime_states = np.zeros(n, dtype=int)
        
        for i in range(1, n):
            c = close[i]
            v = vol[i]
            
            # 直近高値の追従
            lookback_start = max(0, i - self.lookback_high)
            curr_window_high = np.max(high[lookback_start:i+1])
            curr_window_high_idx = lookback_start + np.argmax(high[lookback_start:i+1])
            
            # 高値更新があったか
            bars_since_new_high = i - curr_window_high_idx
            
            # 出来高細り判定
            is_vol_decay = False
            if i >= 72 and vol_sma72[i] > 0:
                is_vol_decay = vol_sma24[i] < (vol_sma72[i] * self.vol_decay_ratio)
                
            # ピークからの下落率
            drop_from_peak = (c - curr_window_high) / curr_window_high
            
            # --- 状態遷移ロジック ---
            if state == 0:  # NORMAL (ロング許可)
                # 高値圏（最高値から2%以内）＋24h以上新高値なし＋出来高細り
                is_near_high = (curr_window_high - c) / curr_window_high <= 0.025
                if bars_since_new_high >= self.stagnation_bars and is_near_high and is_vol_decay:
                    state = 1  # TOP_EXHAUSTION (高値圏・ロング停止)
                    recent_peak_price = curr_window_high
                    peak_bar_idx = curr_window_high_idx
                long_allowed[i] = True

            elif state == 1:  # TOP_EXHAUSTION (新規ロング停止)
                long_allowed[i] = False
                # 高値を更新して勢い復活した場合はNORMALへ復帰
                if c > recent_peak_price * 1.005:
                    state = 0
                    long_allowed[i] = True
                # 下落が本格化してきたらDOWN_WAITINGへ
                elif drop_from_peak <= -0.02:
                    state = 2  # DOWN_WAITING

            elif state == 2:  # DOWN_WAITING (ジリ下げ静観)
                long_allowed[i] = False
                # 規定の下落幅（BTC -5%〜-8%、アルト -10%〜-20%）に到達したか確認
                if drop_from_peak <= target_drop:
                    state = 3  # BOTTOM_WATCHING (監視開始)
                # もし急反発して高値を抜けた場合はNORMAL復帰
                elif c > recent_peak_price:
                    state = 0
                    long_allowed[i] = True

            elif state == 3:  # BOTTOM_WATCHING (セリクラ監視・下ヒゲ待ち)
                long_allowed[i] = False
                
                # 出来高急増（通常2倍以上）または RSI売られすぎ
                is_climax = (v >= vol_sma20[i] * self.vol_surge_mult) or (rsi14[i] <= self.rsi_bottom_th)
                
                # 下ヒゲ陽線確定が来たらロング再開！
                if is_climax and is_hammer_bull[i]:
                    state = 0  # NORMAL復帰
                    long_allowed[i] = True
                elif is_hammer_bull[i] and (rsi14[i] <= self.rsi_bottom_th + 5.0):
                    # RSIが35〜40付近でも下ヒゲ陽線で反発確認なら復帰
                    state = 0
                    long_allowed[i] = True
                    
            regime_states[i] = state

        df['regime_state'] = regime_states
        df['long_allowed'] = long_allowed
        df['is_hammer_bull'] = is_hammer_bull
        return pd.Series(long_allowed, index=df.index)


def run_strategy_backtest(
    df: pd.DataFrame,
    strategy_name: str = "rsima",
    use_regime_filter: bool = False,
    sl_pct: float = 0.03,
    tp_pct: float = 0.06,
    is_btc: bool = False,
) -> Dict[str, Any]:
    """
    1時間足単一銘柄のバックテスト実行
    """
    df = df.copy().reset_index(drop=True)
    
    # 戦略シグナルの生成
    close = df['close']
    
    if strategy_name == "rsima":
        # RSI(9) EMA(7) ゴールデンクロス戦略
        rsi = calc_rsi(close, 9)
        rsi_ma = calc_ema(rsi, 7)
        long_signal = (rsi > rsi_ma) & (rsi.shift(1) <= rsi_ma.shift(1)) & (rsi < 45)
        exit_signal = rsi > 65
    elif strategy_name == "trend_pullback":
        # EMA(12) > EMA(26) かつ 押し目（終値がEMA12を上抜け）
        ema12 = calc_ema(close, 12)
        ema26 = calc_ema(close, 26)
        long_signal = (ema12 > ema26) & (close > ema12) & (close.shift(1) <= ema12.shift(1))
        exit_signal = close < ema26
    elif strategy_name == "breakout":
        # 直近20期間高値ブレイクアウト
        highest20 = close.shift(1).rolling(20).max()
        lowest10 = close.shift(1).rolling(10).min()
        long_signal = close > highest20
        exit_signal = close < lowest10
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    # レジームフィルター適用
    if use_regime_filter:
        simulator = RegimeFilterSimulator()
        long_allowed = simulator.compute_regime(df, is_btc=is_btc)
        # フィルターによる制限
        effective_entry = long_signal & long_allowed
        # 天井圏（state == 1）に入った瞬間に利確・手仕舞い
        early_exit = (df['regime_state'] == 1) & (df['regime_state'].shift(1) == 0)
        effective_exit = exit_signal | early_exit
    else:
        effective_entry = long_signal
        effective_exit = exit_signal

    # --- トレードシミュレーション ---
    trades = []
    in_pos = False
    entry_price = 0.0
    entry_idx = 0
    entry_low = 0.0  # 下ヒゲ安値割れSL用
    
    for i in range(1, len(df)):
        c = df['close'].iloc[i]
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        
        if in_pos:
            # 損切りチェック
            # ヒゲ安値割れSL（フィルター使用時）または固定SL
            curr_sl = min(entry_price * (1.0 - sl_pct), entry_low) if use_regime_filter else (entry_price * (1.0 - sl_pct))
            
            # SL判定
            if l <= curr_sl:
                ret = (curr_sl - entry_price) / entry_price - 0.0008  # 手数料0.08%
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "entry_price": entry_price, "exit_price": curr_sl,
                    "pnl_pct": ret, "reason": "SL"
                })
                in_pos = False
            # 利確・シグナル決済判定
            elif effective_exit.iloc[i] or (c >= entry_price * (1.0 + tp_pct)):
                exit_price = entry_price * (1.0 + tp_pct) if c >= entry_price * (1.0 + tp_pct) else c
                ret = (exit_price - entry_price) / entry_price - 0.0008
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "entry_price": entry_price, "exit_price": exit_price,
                    "pnl_pct": ret, "reason": "TP/Signal"
                })
                in_pos = False
        else:
            if effective_entry.iloc[i]:
                in_pos = True
                entry_price = c
                entry_idx = i
                entry_low = df['low'].iloc[i]  # 当該足の安値

    # --- パフォーマンス集計 ---
    if not trades:
        return {
            "strategy": strategy_name,
            "filter": use_regime_filter,
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl_pct": 0.0,
            "profit_factor": 0.0,
            "max_dd_pct": 0.0,
        }

    pnl_list = [t['pnl_pct'] for t in trades]
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    
    total_trades = len(trades)
    win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    
    # 累積リターンとドローダウン
    cum_equity = np.cumprod(1.0 + np.array(pnl_list))
    peak = np.maximum.accumulate(cum_equity)
    dd = (cum_equity - peak) / peak
    max_dd_pct = abs(np.min(dd)) * 100.0 if len(dd) > 0 else 0.0
    total_pnl_pct = (cum_equity[-1] - 1.0) * 100.0 if len(cum_equity) > 0 else 0.0

    return {
        "strategy": strategy_name,
        "filter": use_regime_filter,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "gross_profit_pct": round(gross_profit * 100, 2),
        "gross_loss_pct": round(gross_loss * 100, 2),
        "trades": trades,
    }


def main():
    print("=" * 80)
    print("【レジームフィルター検証バックテスト】Pattern A: 高値圏停止 ＆ セリクラ底打ち再開")
    print("=" * 80)

    symbols = [
        "BTC-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
        "AVAX-USDT", "LINK-USDT", "NEAR-USDT", "UNI-USDT", "ENA-USDT", "ZEC-USDT"
    ]
    
    results_summary = []
    
    for sym in symbols:
        csv_file = Path(f"Data/individual/merged_{sym}.csv")
        if not csv_file.exists():
            continue
            
        df = pd.read_csv(csv_file)
        if len(df) < 150:
            continue
            
        is_btc = (sym == "BTC-USDT")
        
        # 2つの代表的戦略でテスト (1. RSIMAオシレーター反転, 2. トレンド押し目)
        for strat in ["rsima", "trend_pullback"]:
            res_raw = run_strategy_backtest(df, strategy_name=strat, use_regime_filter=False, is_btc=is_btc)
            res_fil = run_strategy_backtest(df, strategy_name=strat, use_regime_filter=True, is_btc=is_btc)
            
            pnl_diff = res_fil["total_pnl_pct"] - res_raw["total_pnl_pct"]
            dd_diff = res_raw["max_dd_pct"] - res_fil["max_dd_pct"]  # DD削減量（プラスなら改善）
            
            results_summary.append({
                "symbol": sym,
                "strategy": strat,
                "raw_trades": res_raw["total_trades"],
                "raw_pnl": res_raw["total_pnl_pct"],
                "raw_wr": res_raw["win_rate"],
                "raw_pf": res_raw["profit_factor"],
                "raw_dd": res_raw["max_dd_pct"],
                "fil_trades": res_fil["total_trades"],
                "fil_pnl": res_fil["total_pnl_pct"],
                "fil_wr": res_fil["win_rate"],
                "fil_pf": res_fil["profit_factor"],
                "fil_dd": res_fil["max_dd_pct"],
                "pnl_diff": pnl_diff,
                "dd_improvement": dd_diff,
            })

    df_res = pd.DataFrame(results_summary)
    
    # 総合結果の出力
    print("\n【個別銘柄バックテスト詳細結果テーブル】")
    print(f"{'銘柄':<10} | {'戦略':<14} | {'素トレード(損益%/DD%/PF/回数)':<30} | {'フィルター有(損益%/DD%/PF/回数)':<30} | {'損益改善':<8} | {'DD改善':<8}")
    print("-" * 110)
    
    for _, r in df_res.iterrows():
        raw_str = f"{r['raw_pnl']:+6.1f}% / {r['raw_dd']:4.1f}% / PF:{r['raw_pf']:4.2f} ({r['raw_trades']}回)"
        fil_str = f"{r['fil_pnl']:+6.1f}% / {r['fil_dd']:4.1f}% / PF:{r['fil_pf']:4.2f} ({r['fil_trades']}回)"
        pnl_diff_str = f"{r['pnl_diff']:+6.1f}%"
        dd_diff_str = f"{r['dd_improvement']:+5.1f}%"
        print(f"{r['symbol']:<10} | {r['strategy']:<14} | {raw_str:<30} | {fil_str:<30} | {pnl_diff_str:<8} | {dd_diff_str:<8}")

    print("\n" + "=" * 80)
    print("【全銘柄・全戦略 平均パフォーマンス比較】")
    print("=" * 80)
    print(f"■ 平均 累積損益 (PnL):     フィルターなし {df_res['raw_pnl'].mean():+6.2f}%  ==>  フィルターあり {df_res['fil_pnl'].mean():+6.2f}% (改善: {df_res['pnl_diff'].mean():+6.2f}%)")
    print(f"■ 平均 最大ドローダウン:   フィルターなし {df_res['raw_dd'].mean():6.2f}%  ==>  フィルターあり {df_res['fil_dd'].mean():6.2f}% (削減: {df_res['dd_improvement'].mean():+6.2f}%)")
    print(f"■ 平均 プロフィットファクター: フィルターなし {df_res['raw_pf'].mean():6.2f}  ==>  フィルターあり {df_res['fil_pf'].mean():6.2f}")
    print(f"■ 平均 勝率 (Win Rate):    フィルターなし {df_res['raw_wr'].mean():6.1f}%  ==>  フィルターあり {df_res['fil_wr'].mean():6.1f}%")
    print(f"■ 平均 トレード回数:       フィルターなし {df_res['raw_trades'].mean():5.1f}回 ==>  フィルターあり {df_res['fil_trades'].mean():5.1f}回 (無駄トレード削減: {df_res['raw_trades'].mean() - df_res['fil_trades'].mean():.1f}回)")
    print("=" * 80)


if __name__ == "__main__":
    main()
