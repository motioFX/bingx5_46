import os
import sys
import math
import datetime
from datetime import timezone, timedelta
import numpy as np
import pandas as pd
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 同一ディレクトリの bingx5_46_5backtest_mm から PnL 集計ロジックをインポート
sys.path.append(str(Path(__file__).resolve().parent))
try:
    from bingx5_46_5backtest_mm import make_mm_pl
except Exception as e:
    print(f"Warning: Failed to import make_mm_pl from bingx5_46_5backtest_mm.py: {e}")
    make_mm_pl = None

# --- パラメータ（ウェイト係数）の定義 ---
# バックテスト実行時にこれらの値を変えることで、銘柄選定ロジックを最適化できます。
W_PERF = 1.0
W_PERF_BTC = 0.8
W_MOMENTUM = 0.5
W_DD = 0.5
W_VOL = 0.5
W_OI = 0.5
W_FUNDING = 0.3
W_CB_PREMIUM = 0.5  # コインベースプレミアムのウェイト

script_dir = Path(__file__).resolve().parent
data_dir = script_dir / "Data"
input_path = data_dir / "historical_all_symbols_merged.csv"
output_path = data_dir / "historical_selection_scores_1year.csv"

# --- Zスコア計算関数 ---
def get_zscore(series: pd.Series, invert: bool = False) -> pd.Series:
    std = series.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(0.0, index=series.index)
    z = (series - series.mean()) / std
    return -z if invert else z

# --- 地合い判定関数（本番と共通化するための判定ロジック） ---
def determine_market_state(mean_norm: float, mean_cb_premium: float) -> str:
    # 平均騰落率が1.0（前日比フラット）以上、またはコインベースプレミアム平均がプラスの場合に強気と判定
    if mean_norm >= 1.0 or mean_cb_premium > 0.0:
        return "long_only"
    else:
        return "short_only"

def main():
    print("Loading integrated master dataset...")
    if not input_path.exists():
        print(f"Error: Integrated dataset not found at {input_path}! Run download_historical_candles.py first.")
        sys.exit(1)
        
    df_master_raw = pd.read_csv(input_path)
    df_master_raw['timestamp'] = pd.to_datetime(df_master_raw['timestamp'], utc=True).dt.tz_convert(JST)
    
    # タイムスタンプでソート
    df_master_raw = df_master_raw.sort_values(by=['timestamp', 'symbol']).reset_index(drop=True)
    
    symbols = sorted(df_master_raw['symbol'].unique())
    min_ts = df_master_raw['timestamp'].min()
    max_ts = df_master_raw['timestamp'].max()
    print(f"Loaded data for {len(symbols)} symbols. Range: {min_ts} to {max_ts}")
    
    # 銘柄ごとに分割して辞書に保持（処理高速化のため）
    dfs = {}
    for sym in symbols:
        dfs[sym] = df_master_raw[df_master_raw['symbol'] == sym].sort_values('timestamp').reset_index(drop=True)
        
    # 日次の判定時点を設定（ルックバック最大20日に対応するため、開始日を21日後とする）
    start_day = (min_ts + timedelta(days=21)).replace(hour=11, minute=0, second=0, microsecond=0)
    end_day = max_ts.replace(hour=11, minute=0, second=0, microsecond=0)
    
    trade_dates = []
    curr = start_day
    while curr <= end_day:
        # 現在の判定日時点のデータが存在する日のみリストアップ
        trade_dates.append(curr)
        curr += timedelta(days=1)
        
    print(f"Generating scores and simulating backtest for {len(trade_dates)} days (from {start_day.date()} to {end_day.date()})...")
    
    all_daily_records = []
    daily_pnls = []      # 日々のポートフォリオ平均損益率
    simulated_dates = [] # シミュレーションが正常実行された日付
    
    for idx, target_dt in enumerate(trade_dates, 1):
        if idx % 30 == 0 or idx == len(trade_dates):
            print(f"  Processing day {idx}/{len(trade_dates)}: {target_dt.strftime('%Y-%m-%d')}...")
            
        # A. 地合い判定フェーズ (仮の5日ルックバックで計算)
        window_start_temp = target_dt - timedelta(days=5)
        temp_metrics = []
        
        # Helper for column resolution
        def get_col(df, primary, fallback):
            return df[primary] if primary in df.columns else (df[fallback] if fallback in df.columns else pd.Series(0.0, index=df.index))

        # BTC データの取得
        btc_series_temp = None
        if "BTCUSDT" in dfs:
            df_btc = dfs["BTCUSDT"]
            df_btc_5d = df_btc[(df_btc['timestamp'] >= window_start_temp) & (df_btc['timestamp'] <= target_dt)]
            if len(df_btc_5d) >= 24 * 4:
                btc_series_temp = get_col(df_btc_5d, 'close', 'bybit_close').astype(float)
                
        for symbol, df_sym in dfs.items():
            df_5d = df_sym[(df_sym['timestamp'] >= window_start_temp) & (df_sym['timestamp'] <= target_dt)]
            if len(df_5d) < 24 * 4:
                continue
            close_series_5d = get_col(df_5d, 'close', 'bybit_close').astype(float)
            close_now = float(close_series_5d.iloc[-1])
            close_5d_ago = float(close_series_5d.iloc[0])
            norm_perf_5d = close_now / close_5d_ago if close_5d_ago > 0 else 1.0
            
            cb_val = float(df_5d['coinbase_premium'].iloc[-1]) if 'coinbase_premium' in df_5d.columns and not df_5d['coinbase_premium'].empty else 0.0
            temp_metrics.append({
                "symbol": symbol,
                "norm_perf_5d": norm_perf_5d,
                "coinbase_premium": cb_val
            })
            
        if not temp_metrics:
            continue
            
        df_temp = pd.DataFrame(temp_metrics)
        mean_norm = float(df_temp["norm_perf_5d"].mean())
        mean_cb_premium = float(df_temp["coinbase_premium"].mean())
        
        # 地合い判定の実行
        market_state = determine_market_state(mean_norm, mean_cb_premium)
        
        # B. 本番スコア計算フェーズ
        window_days = 30
        window_start = target_dt - timedelta(days=window_days)
        
        # 本番集計用のBTCデータの取得
        btc_series = None
        df_btc_window = None
        if "BTCUSDT" in dfs:
            df_btc = dfs["BTCUSDT"]
            df_btc_window = df_btc[(df_btc['timestamp'] >= window_start) & (df_btc['timestamp'] <= target_dt)].copy()
            if len(df_btc_window) >= 24 * (window_days - 1):
                btc_series = get_col(df_btc_window, 'close', 'bybit_close').astype(float)
                
        metrics_list = []
        for symbol, df_sym in dfs.items():
            df_w = df_sym[(df_sym['timestamp'] >= window_start) & (df_sym['timestamp'] <= target_dt)].copy()
            if len(df_w) < 24 * (window_days - 1):
                continue
                
            close_series = get_col(df_w, 'close', 'bybit_close').astype(float)
            close_now = float(close_series.iloc[-1])
            
            # --- 1. 30d, 10d, 5d ノーマライズ変化率の算出 ---
            # 30d (全期間)
            close_30d_start = float(close_series.iloc[0])
            norm_30d = (close_now / close_30d_start - 1.0) if close_30d_start > 0 else 0.0
            
            # 10d (直近240本)
            idx_10d = max(0, len(close_series) - 24 * 10)
            close_10d_start = float(close_series.iloc[idx_10d])
            norm_10d = (close_now / close_10d_start - 1.0) if close_10d_start > 0 else 0.0
            
            # 5d (直近120本)
            idx_5d = max(0, len(close_series) - 24 * 5)
            close_5d_start = float(close_series.iloc[idx_5d])
            norm_5d = (close_now / close_5d_start - 1.0) if close_5d_start > 0 else 0.0
            
            # --- 2. 日次換算モメンタム和 (ユーザー指定スコア公式: 30d/30 + 10d/10 + 5d/5) ---
            daily_momentum_score = ((norm_30d / 30.0) + (norm_10d / 10.0) + (norm_5d / 5.0)) * 100.0
            
            # --- 大口センチメント & 大口インパクト比率 (%) は別個の情報として読み込み ---
            whale_file = Path(__file__).resolve().parent / "Data" / "whale_market_state.json"
            whale_impact_ratio = 0.0
            if whale_file.exists():
                try:
                    with open(whale_file, "r", encoding="utf-8") as wf:
                        w_data = json.load(wf)
                        coins_sent = w_data.get("coins_sentiment", {})
                        clean_sym = symbol.upper().replace("USDT", "").replace("USDC", "")
                        if clean_sym in coins_sent:
                            w_info = coins_sent[clean_sym]
                            net_usd = float(w_info.get("net_val_usd", 0.0))
                            est_oi = max(daily_turnover * 2.0, 1000000.0)
                            whale_impact_ratio = (net_usd / est_oi) * 100.0
                except Exception:
                    pass

            # 純粋なノーマライズモメンタム和で順位決定
            final_ultimate_score = daily_momentum_score

            # --- 3. 期間前半高値/安値フィルター (30d, 10d, 5d) ---
            # 期間の前半50%以内で最高値を付けたものはロング禁止 (ban_long = 1)
            # 期間の前半50%以内で最安値を付けたものはショート禁止 (ban_short = 1)
            ban_long = 0
            ban_short = 0
            
            for days, sub_idx in [(30, 0), (10, idx_10d), (5, idx_5d)]:
                sub_series = close_series.iloc[sub_idx:].reset_index(drop=True)
                N = len(sub_series)
                if N >= 10:
                    max_pos = int(sub_series.idxmax())
                    min_pos = int(sub_series.idxmin())
                    half_N = N * 0.5
                    
                    # 前半50%で最高値を記録 -> ピークアウト (ロング禁止)
                    if max_pos < half_N:
                        ban_long = 1
                    # 前半50%で最安値を記録 -> 底打ち回復 (ショート禁止)
                    if min_pos < half_N:
                        ban_short = 1
            
            # 流動性チェック
            vol_series = get_col(df_w, 'volume', 'bybit_volume').astype(float).replace(0.0, np.nan).dropna()
            daily_turnover = float((vol_series * close_series).dropna().mean() * 24) if not vol_series.empty else 0.0
            is_low_liq = 1 if daily_turnover < 5000000.0 else 0
            if is_low_liq:
                ban_short = 1
            
            metrics_list.append({
                'symbol': symbol,
                'score': final_ultimate_score,
                'whale_impact_ratio': whale_impact_ratio,
                'norm_30d': norm_30d,
                'norm_10d': norm_10d,
                'norm_5d': norm_5d,
                'ban_long': ban_long,
                'ban_short': ban_short,
                'daily_turnover': daily_turnover,
                'is_low_liq': is_low_liq
            })
            
        if not metrics_list:
            continue
            
        df_daily = pd.DataFrame(metrics_list)
        df_daily['date'] = target_dt.strftime('%Y-%m-%d %H:%M:%S')
        df_daily['market_state'] = market_state
        
        # 保存用にソートして蓄積
        df_daily_sorted = df_daily.sort_values(by='score', ascending=False).reset_index(drop=True)
        all_daily_records.append(df_daily_sorted)
        
        # --- 簡易バックテストシミュレータ ---
        # 翌日11:00の価格情報を取り出すために翌日タイムスタンプを設定
        next_dt = target_dt + timedelta(days=1)
        
        # エントリー銘柄の選定（上位3銘柄）
        selected_trades = []
        if market_state == "long_only":
            # 降順でスコアが高い上位3銘柄 (ban_long = 1 の銘柄は除外)
            candidates = df_daily_sorted.head(15) # 候補多めに取得
            count = 0
            for _, row in candidates.iterrows():
                if count >= 3:
                    break
                if row.get('ban_long', 0) == 1:
                    # 前半高値(ピークアウト)銘柄はスキップ
                    continue
                selected_trades.append({"symbol": row['symbol'], "side": "Long"})
                count += 1
        else: # short_only
            # 昇順でスコアが低い順（マイナスに大きい順）から、ban_shortではない上位3銘柄
            candidates_desc = df_daily_sorted.iloc[::-1] # スコア低い順
            count = 0
            for _, row in candidates_desc.iterrows():
                if count >= 3:
                    break
                if row.get('ban_short', 0) == 1:
                    # ショート禁止銘柄はスキップ
                    continue
                selected_trades.append({"symbol": row['symbol'], "side": "Short"})
                count += 1
                
        # 各銘柄の損益率（PnL %）をシミュレート
        trade_pnls = []
        for trade in selected_trades:
            sym = trade["symbol"]
            side = trade["side"]
            
            df_sym = dfs.get(sym)
            if df_sym is not None:
                # 当日11:00時点の価格（始値）
                row_curr = df_sym[df_sym['timestamp'] == target_dt]
                # 翌日11:00時点の価格（終値）
                row_next = df_sym[df_sym['timestamp'] == next_dt]
                
                if not row_curr.empty and not row_next.empty:
                    o_col = 'open' if 'open' in row_curr.columns else 'bybit_open'
                    c_col = 'close' if 'close' in row_next.columns else 'bybit_close'
                    open_price = float(row_curr[o_col].iloc[0])
                    close_price = float(row_next[c_col].iloc[0])
                    
                    if open_price > 0:
                        if side == "Long":
                            pnl = (close_price - open_price) / open_price
                        else: # Short
                            pnl = (open_price - close_price) / open_price
                        trade_pnls.append(pnl)
                        
        if len(trade_pnls) > 0:
            # 3銘柄への等金額分散として平均を算出
            daily_pnl = sum(trade_pnls) / len(trade_pnls)
            daily_pnls.append(daily_pnl)
            simulated_dates.append(target_dt)
            
    # 全日程ループ終了後
    if not all_daily_records:
        print("Error: No daily scores could be computed.")
        sys.exit(1)
        
    # スコアマスタCSVの結合と保存
    df_master = pd.concat(all_daily_records, ignore_index=True)
    cols = ["date", "symbol", "score", "ban_long", "ban_short", "norm_30d", "norm_10d", "norm_5d", "is_low_liq", "market_state"]
    other_cols = [c for c in df_master.columns if c not in cols]
    df_master = df_master[cols + other_cols]
    
    print(f"Saving selection scores history to {output_path}...")
    df_master.to_csv(output_path, index=False)
    print("SUCCESS: Selection scores database updated!")
    
    # --- バックテスト結果の評価集計 ---
    if len(daily_pnls) > 0:
        print("\n=== Running Backtest Performance Evaluation ===")
        equity = 100.0
        history_rows = []
        
        # 最初の日の取引エントリー
        history_rows.append({
            "time": simulated_dates[0].strftime("%Y-%m-%d %H:%M:%S"),
            "sizes": 1.0,  # 1ユニット分の投資
            "price": equity,
            "high": equity,
            "low": equity
        })
        
        for dt, pnl_rate in zip(simulated_dates[1:], daily_pnls[1:]):
            prev_equity = equity
            equity *= (1.0 + pnl_rate)
            high_val = max(prev_equity, equity)
            low_val = min(prev_equity, equity)
            
            history_rows.append({
                "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "sizes": 0.0,  # ポジション維持
                "price": equity,
                "high": high_val,
                "low": low_val
            })
            
        # 最終日のクローズ
        history_rows[-1]["sizes"] = -1.0
        
        df_pl_input = pd.DataFrame(history_rows)
        
        if make_mm_pl is not None:
            # make_mm_pl を実行して詳細な統計指標を算出
            # 手数料(片道 0.05% と仮定)を引いて実態に近づける
            res_df, final_pnl, metrics = make_mm_pl(
                df_pl_input, 
                maker_fee=0.0005, 
                taker_fee=0.0005, 
                initial=100.0, 
                has_ordertype=False
            )
            
            # 結果の出力
            print("\n------------------------------------------------")
            print("  Backtest Summary (Equal-Weight Top 3 Portfolio)")
            print("------------------------------------------------")
            print(f"シミュレーション期間: {simulated_dates[0].date()} 〜 {simulated_dates[-1].date()}")
            print(f"対象日数          : {len(simulated_dates)} 日")
            print(f"初期資金          : 100.00 USDT")
            print(f"最終評価額        : {equity:.2f} USDT")
            print(f"実現損益 (PnL)    : {final_pnl:.2f} USDT ({final_pnl:.2f}%)")
            print(f"プロフィットファクター: {metrics['PF']:.4f}")
            print(f"勝率 (日次ベース) : {metrics['win_rate']*100:.2f}%")
            print(f"最大ドローダウン  : {metrics['DD_max']:.2f} USDT ({metrics['DD_per']:.4f}%)")
            print(f"最大含み損        : {metrics['max_unrealized_loss']:.2f} USDT")
            print("------------------------------------------------\n")
        else:
            # 単純集計によるフォールバック
            win_days = sum(1 for pnl in daily_pnls if pnl > 0)
            win_rate = win_days / len(daily_pnls) if len(daily_pnls) > 0 else 0
            cumulative_ret = (equity - 100.0)
            print(f"シミュレーション日数: {len(simulated_dates)} 日")
            print(f"累積リターン        : {cumulative_ret:.2f}%")
            print(f"勝率 (日次ベース)   : {win_rate*100:.2f}%")
            print(f"最終資産            : {equity:.2f} USDT")
            print("Warning: Detailed metrics unavailable as make_mm_pl could not be imported.")
    else:
        print("No trades were simulated. Make sure you have downloaded continuous daily data.")

if __name__ == "__main__":
    main()
