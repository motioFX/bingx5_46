from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Any, Optional, List
from pathlib import Path
import asyncio
import time
import os
import requests
import pybotters
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as md
from rich import print
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bitbank5_46_5backtest_mm import make_mm_pl, Backtest, AirExchange
from config_loader import get_webhook_url


def calc_add_pct(n: int) -> float:
    """Return add-on percentage based on position count (Pine Script RSIMA3 spec)."""
    if n <= 3:
        return 0.002  # 0.2%
    if n <= 5:
        return 0.003  # 0.3%
    if n <= 8:
        return 0.004  # 0.4%
    return 0.005       # 0.5%


def calc_rma(series: pd.Series, length: int) -> pd.Series:
    """Pine Script ta.rma (Wilder's Moving Average)"""
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()


def calc_rsi(series: pd.Series, length: int = 9) -> pd.Series:
    """Pine Script ta.rsi based on RMA"""
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    
    rma_up = calc_rma(up, length)
    rma_down = calc_rma(down, length)
    
    rs = rma_up / rma_down.replace(0.0, np.nan)
    rsi_vals = np.where(rma_down == 0.0, 100.0, np.where(rma_up == 0.0, 0.0, 100.0 - (100.0 / (1.0 + rs))))
    return pd.Series(rsi_vals, index=series.index).fillna(50.0)


def calc_ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential Moving Average (EMA)"""
    return series.ewm(span=length, adjust=False).mean()


class logicinstance:
    def crossover(self, x, y):
        return ((x - y) > 0) & ((x.shift(1) - y.shift(1)) <= 0)
    def crossunder(self, x, y):
        return ((x - y) < 0) & ((x.shift(1) - y.shift(1)) >= 0)
    def crossover_t(self, x, y):
        return ((x - y) > 0) & ((x.shift(1) - y) <= 0)
    def crossunder_t(self, x, y):
        return ((x - y) < 0) & ((x.shift(1) - y) >= 0)

    def highest(self, df, period=5):
        maxvalue = df["close"].shift(1).rolling(period).max()
        return maxvalue

    def lowest(self, df, period=5):
        lowvalue = df["close"].shift(1).rolling(period).min()
        return lowvalue

    def make_atr(self, df, span=14):
        high = df['high']
        low = df['low']
        close = df['close']
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=span, adjust=False).mean()
        return round(atr, 1)

    def make_market_profile(self, df, period=100, value_area_pct=0.70, num_bins=100, use_decay=True, decay_half_life=None):
        """
        最新・標準仕様に準拠した高精度マーケットプロファイル（ボリュームプロファイル）生成関数
        - 入出力は完全互換 (POC, VAH, VAL を付加して返す)
        - 実体とヒゲの出来高密度配分、サブビン重心補正、CBOT/TradingView標準Value Area探索を実装
        - デフォルトで直近重視・指数減衰型（EMA型）ボリュームプロファイルが有効 (use_decay=True)
        """
        df = df.copy()
        n = len(df)
        if n == 0:
            df['POC'] = np.nan
            df['VAH'] = np.nan
            df['VAL'] = np.nan
            return df
            
        poc_arr = np.full(n, np.nan)
        vah_arr = np.full(n, np.nan)
        val_arr = np.full(n, np.nan)
        
        min_required = min(5, period)
        
        highs = df['high'].astype(float).values
        lows = df['low'].astype(float).values
        closes = df['close'].astype(float).values
        opens = df['open'].astype(float).values if 'open' in df.columns else closes
        volumes = df['volume'].astype(float).values if 'volume' in df.columns else np.ones(n)
        
        if np.all(volumes <= 0):
            volumes = np.ones(n)
            
        half_life = float(decay_half_life) if decay_half_life is not None else max(5.0, period / 2.0)
        decay_factor = 0.5 ** (1.0 / half_life) if use_decay else 1.0
            
        for i in range(min_required, n):
            start_idx = max(0, i - period + 1)
            win_len = i - start_idx + 1
            
            w_highs = highs[start_idx:i+1]
            w_lows = lows[start_idx:i+1]
            w_closes = closes[start_idx:i+1]
            w_opens = opens[start_idx:i+1]
            w_volumes = volumes[start_idx:i+1].copy()
            
            # 指数減衰の適用（直近足 age=0 が 1.0、古い足ほど減衰）
            if use_decay:
                ages = np.arange(win_len - 1, -1, -1)
                weights = decay_factor ** ages
                w_volumes = w_volumes * weights
            
            p_max = w_highs.max()
            p_min = w_lows.min()
            
            if p_max <= p_min or np.isnan(p_max) or np.isnan(p_min):
                curr_c = closes[i]
                poc_arr[i] = curr_c
                vah_arr[i] = curr_c
                val_arr[i] = curr_c
                continue
            
            price_bins = np.linspace(p_min, p_max, num_bins + 1)
            bin_centers = (price_bins[:-1] + price_bins[1:]) / 2
            
            body_lows = np.minimum(w_opens, w_closes)
            body_highs = np.maximum(w_opens, w_closes)
            
            w_h_2d = w_highs[:, np.newaxis]
            w_l_2d = w_lows[:, np.newaxis]
            w_bh_2d = body_highs[:, np.newaxis]
            w_bl_2d = body_lows[:, np.newaxis]
            w_v_2d = w_volumes[:, np.newaxis]
            
            bin_lows_2d = price_bins[:-1][np.newaxis, :]
            bin_highs_2d = price_bins[1:][np.newaxis, :]
            
            full_overlap = np.maximum(0.0, np.minimum(w_h_2d, bin_highs_2d) - np.maximum(w_l_2d, bin_lows_2d))
            full_range = np.maximum(w_h_2d - w_l_2d, 1e-12)
            full_ratio = full_overlap / full_range
            
            body_overlap = np.maximum(0.0, np.minimum(w_bh_2d, bin_highs_2d) - np.maximum(w_bl_2d, bin_lows_2d))
            body_range = np.maximum(w_bh_2d - w_bl_2d, 1e-12)
            body_ratio = body_overlap / body_range
            
            has_body = (body_highs - body_lows) > 1e-12
            has_body_2d = has_body[:, np.newaxis]
            
            eff_ratio = np.where(has_body_2d, 0.60 * body_ratio + 0.40 * full_ratio, full_ratio)
            
            vol_profile = (w_v_2d * eff_ratio).sum(axis=0)
            
            total_vol = vol_profile.sum()
            if total_vol <= 0:
                curr_c = closes[i]
                poc_arr[i] = curr_c
                vah_arr[i] = curr_c
                val_arr[i] = curr_c
                continue
                
            poc_idx = int(np.argmax(vol_profile))
            
            idx_start = max(0, poc_idx - 1)
            idx_end = min(num_bins, poc_idx + 2)
            local_vols = vol_profile[idx_start:idx_end]
            local_centers = bin_centers[idx_start:idx_end]
            local_sum = local_vols.sum()
            poc_price = (local_vols * local_centers).sum() / local_sum if local_sum > 0 else bin_centers[poc_idx]
            
            target_vol = total_vol * value_area_pct
            current_va_vol = vol_profile[poc_idx]
            
            upper_idx = poc_idx
            lower_idx = poc_idx
            
            while current_va_vol < target_vol:
                can_up = upper_idx < num_bins - 1
                can_down = lower_idx > 0
                
                if not can_up and not can_down:
                    break
                    
                up_vol = vol_profile[upper_idx + 1] if can_up else -1.0
                down_vol = vol_profile[lower_idx - 1] if can_down else -1.0
                
                if can_up and can_down:
                    if up_vol > down_vol:
                        upper_idx += 1
                        current_va_vol += vol_profile[upper_idx]
                    elif down_vol > up_vol:
                        lower_idx -= 1
                        current_va_vol += vol_profile[lower_idx]
                    else:
                        upper_idx += 1
                        current_va_vol += vol_profile[upper_idx]
                        if current_va_vol < target_vol and lower_idx > 0:
                            lower_idx -= 1
                            current_va_vol += vol_profile[lower_idx]
                elif can_up:
                    upper_idx += 1
                    current_va_vol += vol_profile[upper_idx]
                elif can_down:
                    lower_idx -= 1
                    current_va_vol += vol_profile[lower_idx]
            
            vah_price = price_bins[upper_idx + 1]
            val_price = price_bins[lower_idx]
            
            poc_arr[i] = poc_price
            vah_arr[i] = vah_price
            val_arr[i] = val_price
            
        df['POC'] = poc_arr
        df['VAH'] = vah_arr
        df['VAL'] = val_arr
        
        df['POC'] = df['POC'].ffill().bfill()
        df['VAH'] = df['VAH'].ffill().bfill()
        df['VAL'] = df['VAL'].ffill().bfill()
        
        return df

    def make_logic(self, df, market_profile_period=720, er_threshold=0.3, strategy_type="range", use_decay=True, decay_half_life=None, vol_surge_mult=1.2, fr_threshold=0.00002, **kwargs):
        atr_period = 10
        df[f'atr_{atr_period}'] = self.make_atr(df, atr_period)
        
        df = self.make_market_profile(df, period=market_profile_period, use_decay=use_decay, decay_half_life=decay_half_life)
        
        er_period = 10
        direction = abs(df['close'] - df['close'].shift(er_period))
        volatility = df['close'].diff().abs().rolling(er_period).sum()
        df['er'] = (direction / volatility).fillna(0)

        # 1. 出来高急増判定 (Volume Surge: 過去20期間SMAの1.2倍以上)
        if 'volume' in df.columns:
            vol_sma20 = df['volume'].rolling(20, min_periods=5).mean()
            df['vol_surge_ratio'] = (df['volume'] / (vol_sma20 + 1e-9)).fillna(1.0)
            df['is_vol_surge'] = df['vol_surge_ratio'] >= vol_surge_mult
        else:
            df['vol_surge_ratio'] = 1.0
            df['is_vol_surge'] = True

        # 2. ファンディングレート判定 (FRマイナス / 売り偏重判定)
        fr_col = next((c for c in ['funding', 'fundingRate', 'funding_rate', 'funding_rate_1h_pct'] if c in df.columns), None)
        if fr_col:
            # 1h FR が fr_threshold 以下（マイナスまたは0.00002以下の低値）
            df['is_fr_favorable'] = df[fr_col] <= fr_threshold
        else:
            df['is_fr_favorable'] = True

        # 3. 建玉(OI)判定 (OI急増 / 投げ売り清算なし)
        oi_col = next((c for c in ['openInterest', 'open_interest', 'oi', 'openInterestCoins', 'open_interest_usd'] if c in df.columns), None)
        if oi_col and df[oi_col].nunique() > 1:
            oi_pct = df[oi_col].pct_change(1).fillna(0.0)
            df['is_oi_surge'] = oi_pct >= -0.01  # OIが急減しておらず維持または増加
        else:
            df['is_oi_surge'] = True

        # ER条件なしの素のクロスオーバーシグナル
        df['long_breakout_raw'] = self.crossover(df['close'], df['VAH'])
        df['short_breakout_raw'] = self.crossunder(df['close'], df['VAL'])
        df['long_range_raw'] = self.crossover(df['close'], df['VAL'])
        df['short_range_raw'] = self.crossunder(df['close'], df['VAH'])

        # ER + Squeeze条件付きシグナル (ショートスクイーズ・踏み上げブレイクアウト)
        df['long_breakout'] = (
            df['long_breakout_raw'] & 
            (df['er'] > er_threshold) & 
            df['is_vol_surge'] & 
            df['is_fr_favorable'] & 
            df['is_oi_surge']
        )
        df['short_breakout'] = df['short_breakout_raw'] & (df['er'] > er_threshold)
        df['long_range'] = df['long_range_raw'] & (df['er'] > er_threshold)
        df['short_range'] = df['short_range_raw'] & (df['er'] > er_threshold)
        
        # --- エンベロープ (Envelope) 戦略シグナル ---
        env_l = int(kwargs.get('env_len', 15))
        env_lp = float(kwargs.get('env_lower_pct', 2.0))
        env_up = float(kwargs.get('env_upper_pct', 2.0))
        env_ml = int(kwargs.get('env_malen', 200))
        basis = calc_ema(df['close'], env_l)
        mabasis = calc_ema(df['close'], env_ml) if len(df) >= env_ml else basis
        df['basis'] = basis
        df['mabasis'] = mabasis
        df['env_lower'] = basis * (1.0 - env_lp / 100.0)
        df['env_upper'] = basis * (1.0 + env_up / 100.0)
        df['long_envelope'] = df['close'] < df['env_lower']
        df['longclose_envelope'] = df['close'] > df['basis']
        
        # --- RSI MA (RSIMA3) 戦略シグナル ---
        r_len = int(kwargs.get('rsi_len', 9))
        l_len = int(kwargs.get('lma_len', 7))
        r_lep = float(kwargs.get('lEp', 40.0))
        r_lcp = float(kwargs.get('lCp', 60.0))
        rsi_series = calc_rsi(df['close'], r_len)
        lrsiMA_series = calc_ema(rsi_series, l_len)
        df['rsi'] = rsi_series
        df['lrsiMA'] = lrsiMA_series
        rsi_gc = self.crossover(df['rsi'], df['lrsiMA'])
        df['long_rsima'] = rsi_gc & (df['lrsiMA'] < r_lep) & (df['rsi'] < r_lcp)
        df['longclose_rsima'] = df['rsi'] > r_lcp

        strat_lower = str(strategy_type).lower()
        if strat_lower == "envelope":
            df['long'] = df['long_envelope']
            df['short'] = False
            df['longclose'] = df['longclose_envelope']
            df['shortclose'] = False
        elif strat_lower == "rsima":
            df['long'] = df['long_rsima']
            df['short'] = False
            df['longclose'] = df['longclose_rsima']
            df['shortclose'] = False
        elif strat_lower in ("breakout", "squeeze_breakout"):
            df['long'] = df['long_breakout']
            df['short'] = df['short_breakout']
            df['longclose'] = False
            df['shortclose'] = False
        elif strat_lower == "adaptive":
            df['long'] = np.where(df['er'] > er_threshold, df['long_breakout'], df['long_range_raw'])
            df['short'] = np.where(df['er'] > er_threshold, df['short_breakout'], df['short_range_raw'])
            df['longclose'] = False
            df['shortclose'] = False
        else:
            df['long'] = df['long_range']
            df['short'] = df['short_range']
            df['longclose'] = False
            df['shortclose'] = False
        
        df['lowest_support'] = df['low'].rolling(window=max(1, int(market_profile_period)), min_periods=1).min().ffill()

        rows_before = len(df)
        required_cols = [c for c in ['open', 'high', 'low', 'close', 'POC', 'VAH', 'VAL', 'er'] if c in df.columns]
        df_cleaned = df.dropna(subset=required_cols) if required_cols else df.dropna()
        rows_after = len(df_cleaned)
        
        if rows_after == 0 and rows_before > 0:
            print("[WARNING] dropna removed all rows, using bfill/ffill instead")
            df = df.bfill().ffill()
        else:
            df = df_cleaned
            df = df.bfill()
        return df


import threading

class send_discord:
    _shared_lock = threading.Lock()
    _shared_buffers = {}
    _shared_timers = {}

    def __init__(self):
        self.real1_webhook = get_webhook_url("real1_bitbank")
        self.real2_webhook = get_webhook_url("real2_hype")
        self.real3_webhook = get_webhook_url("real3_bngx")
        self.test4_webhook = get_webhook_url("test4_backtest")

        # 互換用エイリアス（BingX関連はすべて #real3_bngx へ全集約）
        self.bingx_webhook = self.real3_webhook
        self.win32_webhook = self.real3_webhook
        self.default_webhook = self.real3_webhook
        self.webhook_url = self.real3_webhook or self.test4_webhook

        if not self.real3_webhook and not self.test4_webhook:
            print("[WARN] Discord webhook for BingX (real3_bngx / test4_backtest) is not configured.")
        
        from rich.console import Console
        self.console = Console(color_system="standard", force_terminal=True, highlight=False)

        self.lock = send_discord._shared_lock
        self.buffers = send_discord._shared_buffers
        self.timers = send_discord._shared_timers

    def _get_target_webhooks(self, text: str = "") -> list[str]:
        # BingX関連の全通知（バックテスト・最適化・ログ・チャート含む）を #real3_bngx へ全集約
        target = self.real3_webhook or self.test4_webhook
        return [target] if target else []

    def flush_buffer(self, url):
        with self.lock:
            if url not in self.buffers or not self.buffers[url]:
                return
            entries = self.buffers[url]
            self.buffers[url] = []
            if url in self.timers:
                self.timers[url] = None
        
        chunk_limit = 1900
        chunks = []
        current_chunk = []
        current_length = 0

        for line in entries:
            line_len = len(line) + 1
            if current_length + line_len > chunk_limit and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_length = line_len
            else:
                current_chunk.append(line)
                current_length += line_len
        
        if current_chunk:
            chunks.append("\n".join(current_chunk))

        for chunk in chunks:
            discord_message = f"```ansi\n{chunk}\n```"
            try:
                payload = {"content": discord_message}
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"Failed to send buffered message to {url}:", e)

    def flush_all(self):
        with self.lock:
            urls = list(self.buffers.keys())
        for url in urls:
            self.flush_buffer(url)

    def print_log(self, text, level="info"):
        from rich.markup import escape
        is_balance = any(kw in text for kw in ["証拠金残高", "残高", "Account Info", "balance", "Balance"])
        if is_balance:
            styled_text = f"[bold green]{escape(text)}[/bold green]"
        else:
            styled_text = escape(text)
        
        with self.console.capture() as capture:
            self.console.print(styled_text, end="")
        ansi_text = capture.get()
        
        try:
            print(ansi_text)
        except Exception:
            try:
                sys.stdout.buffer.write((ansi_text + "\n").encode("utf-8", errors="replace"))
                sys.stdout.flush()
            except Exception:
                pass
        
        # debugレベルのメッセージはDiscord送信しない（コンソールのみ出力）
        if level == "debug":
            return

        webhooks = self._get_target_webhooks(text)
        
        if "```" in text:
            for url in webhooks:
                self.flush_buffer(url)
                try:
                    payload = {"content": text}
                    response = requests.post(url, json=payload, timeout=10)
                    response.raise_for_status()
                except Exception as e:
                    print(f"Failed to send message to {url}:", e)
            return

        with self.lock:
            for url in webhooks:
                if url not in self.buffers:
                    self.buffers[url] = []
                self.buffers[url].append(ansi_text)
                
                if url in self.timers and self.timers[url] is not None:
                    self.timers[url].cancel()
                
                timer = threading.Timer(1.2, self.flush_buffer, args=[url])
                self.timers[url] = timer
                timer.start()

    def _send_file(self, content_text, file_path, file_name, mime_type) -> bool:
        webhooks = self._get_target_webhooks(content_text)
        if not webhooks:
            return False
        success = True
        for url in webhooks:
            self.flush_buffer(url)
            try:
                payload = {"content": content_text}
                with open(file_path, "rb") as f:
                    files = {"file": (file_name, f, mime_type)}
                    response = requests.post(url, data=payload, files=files, timeout=90)
                    response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"Failed to send file {file_name} to {url}:", e)
                success = False
        return success

    def send_file(self, file_path, description="") -> bool:
        p = Path(file_path) if not isinstance(file_path, Path) else file_path
        if not p.exists():
            print(f"[send_discord] File not found: {p}")
            return False
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        return bool(self._send_file(description, str(p), p.name, mime))

    def plot_kline(self, symbol=""):
        df = pd.read_csv("backtest_data/klines100.csv")
        date = pd.to_datetime(df["timestamp"])
        a_plot = df["close"]
        
        has_market_profile = all(col in df.columns for col in ['POC', 'VAH', 'VAL'])

        matplotlib.rcParams["timezone"] = "Asia/Tokyo"
        fig = plt.figure(figsize=(8, 3))
        fig.subplots_adjust(left=0.1, bottom=0.15, right=0.9, top=0.85)
        
        ax1 = fig.add_subplot(1, 1, 1)
        
        try:
            d_series = pd.to_datetime(date)
            tz_info = d_series.dt.tz if hasattr(d_series.dt, "tz") else None
            day_starts = pd.date_range(
                start=d_series.iloc[0].normalize(),
                end=d_series.iloc[-1].normalize() + pd.Timedelta(days=1),
                freq="D",
                tz=tz_info
            )
            for day_start in day_starts:
                if d_series.iloc[0] <= day_start <= d_series.iloc[-1]:
                    ax1.axvline(day_start, color="gray", linestyle=":", linewidth=0.8, alpha=0.4, zorder=1)
        except Exception:
            pass

        ax1.plot(date, a_plot, "C0", label="close", linewidth=1.5)
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))
        
        if has_market_profile:
            poc_plot = df['POC']
            vah_plot = df['VAH']
            val_plot = df['VAL']
            
            ax1.plot(date, poc_plot, color='red', linestyle='-', linewidth=2, label='POC', alpha=0.8)
            ax1.plot(date, vah_plot, color='green', linestyle='--', linewidth=1.5, label='VAH', alpha=0.7)
            ax1.plot(date, val_plot, color='blue', linestyle='--', linewidth=1.5, label='VAL', alpha=0.7)
            ax1.fill_between(date, val_plot, vah_plot, alpha=0.15, color='purple')
            
            latest_close = df['close'].iloc[-1]
            latest_poc = df['POC'].iloc[-1]
            latest_vah = df['VAH'].iloc[-1]
            latest_val = df['VAL'].iloc[-1]
            
            ax1.annotate(f'close: {latest_close:.2f}', xy=(1.01, latest_close), xycoords=('axes fraction', 'data'), fontsize=8, color='C0', va='center')
            ax1.annotate(f'POC: {latest_poc:.2f}', xy=(1.01, latest_poc), xycoords=('axes fraction', 'data'), fontsize=8, color='red', va='center')
            ax1.annotate(f'VAH: {latest_vah:.2f}', xy=(1.01, latest_vah), xycoords=('axes fraction', 'data'), fontsize=8, color='green', va='center')
            ax1.annotate(f'VAL: {latest_val:.2f}', xy=(1.01, latest_val), xycoords=('axes fraction', 'data'), fontsize=8, color='blue', va='center')
        
        ax1.legend(loc='upper left', fontsize=8)
        ax1.set_ylabel("Price [USDT]")
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(md.DateFormatter("%m/%d %H:%M"))
        fig.autofmt_xdate(rotation=10)
        
        plt.close()
        os.makedirs("backtest_data", exist_ok=True)
        fig.savefig("backtest_data/kline_img.jpg", format='jpg', dpi=80)

        self._send_file(f"{symbol} klines with Market Profile", "backtest_data/kline_img.jpg", "kline_img.jpg", "image/jpeg")

    def plot_backtest(self, label="MP", csv_file="backtest_data/klines100.csv", img_file=None, symbol=""):
        if symbol and not str(label).startswith(symbol):
            display_label = f"{symbol}_{label}"
            title_prefix = f"{symbol} | "
        else:
            display_label = label
            title_prefix = f"{symbol} | " if symbol else ""

        if img_file is None:
            img_file = f"backtest_data/backtest_{display_label}_img.jpg"
        
        df = pd.read_csv(csv_file)
        if "timestamp" not in df.columns or df.empty:
            return None

        df["dt"] = pd.to_datetime(df["timestamp"])
        if df["dt"].dt.tz is None:
            df["dt"] = df["dt"].dt.tz_localize("UTC").dt.tz_convert("Asia/Tokyo")
        else:
            df["dt"] = df["dt"].dt.tz_convert("Asia/Tokyo")

        matplotlib.rcParams["timezone"] = "Asia/Tokyo"
        fig = plt.figure(figsize=(9, 4), dpi=120)
        fig.subplots_adjust(left=0.1, bottom=0.18, right=0.9, top=0.88)
        ax1 = fig.add_subplot(1, 1, 1)

        x_indices = np.arange(len(df))
        width = 0.6

        # 1. 日付区切り垂直線
        try:
            d_series = df["dt"]
            day_starts = pd.date_range(
                start=d_series.iloc[0].normalize(),
                end=d_series.iloc[-1].normalize() + pd.Timedelta(days=1),
                freq="D",
                tz=d_series.iloc[0].tz
            )
            for day_start in day_starts:
                match_indices = np.where(d_series.dt.floor("D") == day_start)[0]
                if len(match_indices) > 0:
                    first_idx = match_indices[0]
                    ax1.axvline(first_idx, color="gray", linestyle=":", linewidth=0.8, alpha=0.4, zorder=1)
        except Exception:
            pass

        # 2. Market Profile (Volume Profile)
        has_market_profile = all(col in df.columns for col in ['POC', 'VAH', 'VAL'])
        if has_market_profile:
            poc_plot = df['POC'].values
            vah_plot = df['VAH'].values
            val_plot = df['VAL'].values
            
            ax1.plot(x_indices, poc_plot, color='red', linestyle='-', linewidth=1.4, label='POC', alpha=0.75, zorder=2)
            ax1.plot(x_indices, vah_plot, color='green', linestyle='--', linewidth=1.1, label='VAH', alpha=0.7, zorder=2)
            ax1.plot(x_indices, val_plot, color='blue', linestyle='--', linewidth=1.1, label='VAL', alpha=0.7, zorder=2)
            ax1.fill_between(x_indices, val_plot, vah_plot, alpha=0.08, color='purple', zorder=1)

        # 3. ローソク足の描画
        has_ohlc = all(col in df.columns for col in ['open', 'high', 'low', 'close'])
        if has_ohlc:
            for i, row in df.iterrows():
                op = float(row["open"])
                hi = float(row["high"])
                lo = float(row["low"])
                cl = float(row["close"])
                color = "#2e7d32" if cl >= op else "#c62828"  # 陽線: 緑, 陰線: 赤

                # ヒゲ
                ax1.plot([i, i], [lo, hi], color=color, linewidth=1.0, zorder=3)
                # 実体
                body_bottom = min(op, cl)
                body_height = abs(cl - op)
                if body_height == 0:
                    body_height = (hi - lo) * 0.01 if hi != lo else 0.0001
                rect = plt.Rectangle((i - width / 2, body_bottom), width, body_height,
                                     facecolor=color, edgecolor=color, zorder=4)
                ax1.add_patch(rect)
        else:
            ax1.plot(x_indices, df['close'], "C0", label="close", linewidth=1.5, zorder=3)

        # 4. 売買マーカー (exec buy: 鮮やかブルー / exec sell: 鮮やかマゼンタ、黒フチ付き)
        if 'exec_buy_price' in df.columns:
            buy_mask = df['exec_buy_price'].notna()
            if buy_mask.any():
                ax1.scatter(
                    x_indices[buy_mask],
                    df.loc[buy_mask, 'exec_buy_price'],
                    marker='^',
                    color='#0091ea',  # エレクトリックブルー (ローソク足の緑/赤と完全差別化)
                    edgecolors='black',
                    linewidths=1.0,
                    label='exec buy',
                    zorder=6,
                    s=70,
                )
        if 'exec_sell_price' in df.columns:
            sell_mask = df['exec_sell_price'].notna()
            if sell_mask.any():
                ax1.scatter(
                    x_indices[sell_mask],
                    df.loc[sell_mask, 'exec_sell_price'],
                    marker='v',
                    color='#d500f9',  # ネオンマゼンタ (ローソク足の緑/赤と完全差別化)
                    edgecolors='black',
                    linewidths=1.0,
                    label='exec sell',
                    zorder=6,
                    s=70,
                )

        ax1.set_ylabel("close [USDT]", fontsize=9)
        ax1.grid(True, axis='y', linestyle=':', alpha=0.3)

        # 5. 右軸 (twinx): 累積 PnL
        ax2 = ax1.twinx()
        b_plot = df['pnl'] if 'pnl' in df.columns else np.zeros(len(df))
        ax2.plot(x_indices, b_plot, "C1", label="pl", linewidth=1.5)
        ax2.set_ylabel("pnl [USDT]", fontsize=9)
        ax2.grid(False)

        # 6. X軸目盛り設定 (日付表示)
        step = max(1, len(df) // 8)
        tick_indices = list(range(0, len(df), step))
        if (len(df) - 1) not in tick_indices:
            tick_indices.append(len(df) - 1)
        tick_labels = [df.iloc[idx]["dt"].strftime("%Y-%m-%d %H:%M") for idx in tick_indices]
        ax1.set_xticks(tick_indices)
        ax1.set_xticklabels(tick_labels, rotation=15, ha="right", fontsize=8)

        final_pnl = df['pnl'].iloc[-1] if 'pnl' in df.columns else 0.0
        ax1.set_title(f"{title_prefix}{label} | Final PnL: {final_pnl:.4f} USDT", fontsize=10, pad=10)

        # 7. 凡例統合
        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, loc='lower left', fontsize=8)

        os.makedirs(os.path.dirname(img_file) if os.path.dirname(img_file) else "backtest_data", exist_ok=True)
        plt.savefig(img_file, format='jpg', dpi=100)
        plt.close(fig)
        plt.close('all')
        import gc
        gc.collect()

        self._send_file(f"backtest pnl ({display_label})", img_file, f"backtest_{display_label}.jpg", "image/jpeg")
        return img_file

    def print_logs(self, df, jpy_onhand_amount, onhand_amount, max_lot, target_position_value=None, trade_side="long"):
        if df is None or df.empty or len(df) == 0:
            self.print_log("ログ出力スキップ: データフレームが空です")
            return
        
        self.print_log(f"1 timestamp : {df['timestamp'].iloc[-1]}")
        time.sleep(1)

        if str(trade_side).lower() == "short":
            short_val = df['short'].iloc[-1] if 'short' in df.columns else False
            shortclose_val = df['shortclose'].iloc[-1] if 'shortclose' in df.columns else (df['stshortclose'].iloc[-1] if 'stshortclose' in df.columns else False)
            self.print_log(f"close:{df['close'].iloc[-1]} short:{short_val} shortclose:{shortclose_val}")
        else:
            long_val = df['long'].iloc[-1] if 'long' in df.columns else False
            longclose_val = df['longclose'].iloc[-1] if 'longclose' in df.columns else (df['stlongclose'].iloc[-1] if 'stlongclose' in df.columns else False)
            self.print_log(f"close:{df['close'].iloc[-1]} long:{long_val} longclose:{longclose_val}")
        time.sleep(1)

        price = df['close'].iloc[-1]
        if target_position_value is not None and target_position_value > 0:
            optimal_lot_usdt = target_position_value
        else:
            optimal_lot = jpy_onhand_amount / price / max_lot * 100
            optimal_lot_usdt = optimal_lot * price

        onhand_amount_usdt = onhand_amount * price
        self.print_log(f"best lot : {optimal_lot_usdt:.2f} USDT, onhand_amount : {onhand_amount_usdt:.2f} USDT")
        time.sleep(1)
        self.print_log("-----------------------------------------")
        time.sleep(1)


discord = send_discord()

# BingX PnL Calculator
class PnLCalculator:
    def __init__(self, apis_config=None, rest_api_url=None, symbol='BTC', mode='demo'):
        if apis_config is None:
            try:
                from bitbank5_46_2api import apis
                apis_config = apis
            except Exception:
                apis_config = {}
        self.apis = apis_config
        self.mode = mode
        self.symbol = symbol

    async def calculate_pnl(self, days_back=30, symbol='BTC', symbols=None):
        fills = await self.get_bingx_trade_history(self.apis, days_back=days_back)
        if not fills:
            discord.print_log("BingX取引履歴が見つかりませんでした（新規口座、または最近の取引がない可能性があります）")
            return None
            
        df = pd.DataFrame(fills)
        combined = self.calculate_bingx_pnl_from_df(df)
        return combined

    async def get_bingx_trade_history(self, apis_bx, days_back=30):
        try:
            from bitbank5_46_2api import sign_bingx, RestAPI_url
            cred_key = 'bingx_demo' if self.mode in ('paper', 'demo', 'testnet') else 'bingx'
            creds = apis_bx.get(cred_key) or {}
            if isinstance(creds, list):
                creds = {}
            api_key = creds.get("api_key", "")
            secret_key = creds.get("secret_key", "")
            if not api_key or not secret_key:
                return []
            base_url = RestAPI_url.get(cred_key, 'https://open-api.bingx.com')
            timestamp = int(time.time() * 1000)
            query_str = f"timestamp={timestamp}"
            sign = sign_bingx(secret_key, query_str)
            headers = {"X-BX-APIKEY": api_key}
            resp = requests.get(
                f"{base_url}/openApi/swap/v2/trade/allOrders?{query_str}&signature={sign}",
                headers=headers,
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    orders = data.get("data", {}).get("orders", [])
                    if isinstance(orders, list):
                        return orders
        except Exception as e:
            discord.print_log(f"BingX trade history fetch error: {e}")
        return []

    def calculate_bingx_pnl_from_df(self, df):
        if df.empty:
            return None
        df = df.copy()
        time_col = 'updateTime' if 'updateTime' in df.columns else ('time' if 'time' in df.columns else None)
        if time_col:
            df['execTime'] = pd.to_datetime(pd.to_numeric(df[time_col]), unit='ms')
        else:
            return None
        df = df.sort_values('execTime')
        
        for col in ('price', 'avgPrice', 'executedQty', 'fee', 'profit'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
                
        df['realized_pnl'] = 0.0
        df['cumulative_pnl'] = 0.0
        
        if 'profit' in df.columns:
            fee_val = df['fee'] if 'fee' in df.columns else 0.0
            df['realized_pnl'] = df['profit'] - fee_val
            df['cumulative_pnl'] = df['realized_pnl'].cumsum()
            return df
            
        return df



class PositionSizer:
    fibolot_dict = {
        0: 1, 1: 2, 2: 3, 3: 5, 4: 8, 5: 13, 6: 21, 7: 21, 8: 21, 9: 21, 10: 21
    }

    cumulative_position_size_dict = {}

    @staticmethod
    def build_cumulative_dict():
        total_long = 0
        for i in range(0, 11):
            total_long += PositionSizer.fibolot_dict.get(i, 21)
            PositionSizer.cumulative_position_size_dict[i] = total_long

    @staticmethod
    def get_stage_from_position_size(pos_size: float) -> int:
        PositionSizer.build_cumulative_dict()
        if pos_size > 0:
            for stage in range(0, 11):
                if pos_size <= PositionSizer.cumulative_position_size_dict[stage]:
                    return max(0, stage + 1)
            return 10
        else:
            return 0

    @staticmethod
    def get_next_lot(pos_size: float) -> int:
        stage = PositionSizer.get_stage_from_position_size(pos_size)
        return PositionSizer.fibolot_dict.get(stage, 1)


class MPStrategy(Backtest):
    @staticmethod
    def fibolot(n):
        fibolot_dict = {
            0:0,1:1,2:2,3:3,4:5,5:8,6:13,7:21,8:34,9:55,10:89,
            -1:-1,-2:-2,-3:-3,-4:-5,-5:-8,-6:-13,-7:-21,-8:-34,-9:-55,-10:-89,
        }
        return fibolot_dict.get(n, 0)

    def __init__(self, air_exchange, df, size, columns, side_mode="long", atr_period=10, atr_tp_multi=3, er_threshold=0.3, strategy_type="range", sl_margin_pct=1.0):
        super().__init__(air_exchange=air_exchange, df=df, columns=columns)
        self.size = size
        self.side_mode = side_mode
        self.atr_period = atr_period
        self.atr_tp_multi = atr_tp_multi
        self.er_threshold = er_threshold
        self.strategy_type = strategy_type
        self.sl_margin_pct = sl_margin_pct
        self.mean_prices = []
        self.stlong = []
        self.stlongclose = []
        self.stshort = []
        self.stshortclose = []
        self.max_price_since_entry = None
        self.min_price_since_entry = None
        self.crossed_vah = False
        self.crossed_val = False

    def action(self):
        position = np.float64(self._get_position()["qty"])
        mean = self._get_position()["avgEntry"]
        self.mean_prices.append(mean)

        close = self.array[self.column_dic["close"]]
        vah = self.array[self.column_dic["VAH"]]
        val = self.array[self.column_dic["VAL"]]
        poc = self.array[self.column_dic["POC"]]
        atr = self.array[self.column_dic[f"atr_{self.atr_period}"]]

        long_signal = bool(self.array[self.column_dic["long"]]) if "long" in self.column_dic else False
        short_signal = bool(self.array[self.column_dic["short"]]) if "short" in self.column_dic else False

        stlong = False
        stshort = False
        stlongclose = False
        stshortclose = False

        is_short_mode = str(self.side_mode).lower() == "short"

        if is_short_mode:
            if position < 0:
                if not hasattr(self, 'entry_price') or self.entry_price is None:
                    self.entry_price = close
                    self.initial_sl_line = vah
                    self.crossed_val = False
                    margin_pct = getattr(self, 'sl_margin_pct', 1.0)
                    self.current_sl = vah * (1.0 + margin_pct / 100.0)

                be_price = self.entry_price * (1.0 - 0.0005)
                is_profitable = close <= be_price

                if is_profitable:
                    if getattr(self, 'strategy_type', 'range') == 'breakout':
                        base_line = val
                    else:
                        if close < val:
                            self.crossed_val = True
                        base_line = val if getattr(self, 'crossed_val', False) else poc
                    # 実運用(VPTrailingManager)と完全一致: ラチェット更新（切り上げ禁止）
                    self.current_sl = min(getattr(self, 'current_sl', be_price), be_price, base_line)

                if close > getattr(self, 'current_sl', float('inf')):
                    stshortclose = True
                    self._cancel_all_orders()
                    self._market_order(size=-position)
                    self.entry_price = None
                    self.initial_sl_line = None
                    self.current_sl = None
                    self.crossed_val = False
            else:
                self.entry_price = None
                self.initial_sl_line = None
                self.current_sl = None
                self.crossed_val = False

            if not stshortclose:
                if position == 0:
                    if short_signal:
                        stshort = True
                        self._cancel_all_orders()
                        self._market_order(size=-self.size)
                        self.entry_price = close
                        self.initial_sl_line = vah
                        margin_pct = getattr(self, 'sl_margin_pct', 1.0)
                        self.current_sl = vah * (1.0 + margin_pct / 100.0)
                        self.crossed_val = False
        else:
            if position > 0:
                if not hasattr(self, 'entry_price') or self.entry_price is None:
                    self.entry_price = close
                    self.initial_sl_line = val
                    self.crossed_vah = False
                    margin_pct = getattr(self, 'sl_margin_pct', 1.0)
                    self.current_sl = val * (1.0 - margin_pct / 100.0)

                be_price = self.entry_price * (1.0 + 0.0005)
                # ロング含み益か否か（建値＋手数料カバー価格以上か）
                is_profitable = close >= be_price

                if is_profitable:
                    if getattr(self, 'strategy_type', 'range') == 'breakout':
                        base_line = vah
                    else:
                        if close > vah:
                            self.crossed_vah = True
                        base_line = vah if getattr(self, 'crossed_vah', False) else poc
                    # 実運用(VPTrailingManager)と完全一致: ラチェット更新（切り下げ禁止）
                    self.current_sl = max(getattr(self, 'current_sl', be_price), be_price, base_line)

                if close < getattr(self, 'current_sl', -float('inf')):
                    stlongclose = True
                    self._cancel_all_orders()
                    self._market_order(size=-position)
                    self.entry_price = None
                    self.initial_sl_line = None
                    self.current_sl = None
                    self.crossed_vah = False
            else:
                self.entry_price = None
                self.initial_sl_line = None
                self.current_sl = None
                self.crossed_vah = False

            if not stlongclose:
                if position == 0:
                    if long_signal:
                        stlong = True
                        self._cancel_all_orders()
                        self._market_order(size=self.size)
                        self.entry_price = close
                        self.initial_sl_line = val
                        margin_pct = getattr(self, 'sl_margin_pct', 1.0)
                        self.current_sl = val * (1.0 - margin_pct / 100.0)
                        self.crossed_vah = False

        self.stlong.append(stlong)
        self.stlongclose.append(stlongclose)
        self.stshort.append(stshort)
        self.stshortclose.append(stshortclose)

    def run(self):
        super().run()
        self.mean_prices = pd.Series(self.mean_prices, index=self.df.index)
        self.stlong = pd.Series(self.stlong, index=self.df.index)
        self.stlongclose = pd.Series(self.stlongclose, index=self.df.index)
        self.stshort = pd.Series(self.stshort, index=self.df.index)
        self.stshortclose = pd.Series(self.stshortclose, index=self.df.index)

    def get_data(self):
        return pd.DataFrame({
            'timestamp': pd.to_datetime(self.df.index),
            'stlong': self.stlong,
            'stlongclose': self.stlongclose,
            'stshort': self.stshort,
            'stshortclose': self.stshortclose,
            'mean_price': self.mean_prices
        })

class backtester:
    def run_backtest(self, df, lot, data_equity, side_mode="long", mp_period=120, atr_tp_multi=3, er_threshold=0.3, bybit_exec_history=None, bybit_lot_size=None, strategy_type="range", sl_margin_pct=1.0):
        if df is None or df.empty:
            print("エラー: データフレームが空です。APIからデータを取得できませんでした。")
            discord.print_log("バックテスト失敗: ローソク足データが取得できませんでした。")
            empty_df = pd.DataFrame(columns=['timestamp', 'close', 'PL_graph', 'pnl', 'des'])
            return empty_df
        
        import copy
        df_sorted = df.copy()
        df_sorted["timestamp"] = pd.to_datetime(df_sorted["timestamp"])
        
        for col in ['PL_graph', 'pnl', 'exec_buy_price', 'exec_sell_price']:
            if col in df_sorted.columns:
                df_sorted = df_sorted.drop(columns=[col])
        
        self.exec_history = []
        
        if bybit_exec_history is not None:
            air_exchange = AirExchange()
            price_map = {}
            for idx, row in df_sorted.iterrows():
                ts = pd.to_datetime(row['timestamp'])
                price_map[ts] = float(row['close'])
                
            scale = 1.0
            if bybit_lot_size is not None and bybit_lot_size > 0 and lot > 0:
                scale = lot / bybit_lot_size
                
            for entry in bybit_exec_history:
                executed = copy.copy(entry)
                entry_ts = pd.to_datetime(entry["timestamp"])
                
                executed["size"] = entry["size"] * scale
                
                if entry_ts in price_map:
                    executed["price"] = price_map[entry_ts]
                else:
                    closest_ts = min(price_map.keys(), key=lambda t: abs(t - entry_ts))
                    executed["price"] = price_map[closest_ts]
                    
                executed["timestamp"] = entry_ts
                air_exchange.exec_history.append(executed)
                
            self.exec_history = air_exchange.exec_history
        else:
            air_exchange = AirExchange()
            # バックテストのポジションサイズ: 資金100ドル基準（または指定のdata_equity）でドル価値を均一化
            size_in_usdt = float(data_equity) if (data_equity is not None and data_equity > 0) else 100.0
            
            bt = MPStrategy(
                air_exchange=air_exchange, 
                df=df_sorted, 
                size=size_in_usdt,
                columns=["open", "atr_10", "POC", "VAH", "VAL", "long", "short", "er", "lowest_support", "close"],
                side_mode=side_mode,
                atr_period=10,
                atr_tp_multi=atr_tp_multi,
                er_threshold=er_threshold,
                strategy_type=strategy_type,
                sl_margin_pct=sl_margin_pct,
            )
            bt.run()
    
            bt_data = bt.get_data()
            bt_data['timestamp'] = pd.to_datetime(bt_data['timestamp'])
            
            # タイムゾーンの型不一致 (tz-aware vs tz-naive) による pd.merge エラーを統一防止
            if hasattr(df_sorted['timestamp'].dt, 'tz') and df_sorted['timestamp'].dt.tz is not None:
                df_sorted['timestamp'] = df_sorted['timestamp'].dt.tz_localize(None)
            if hasattr(bt_data['timestamp'].dt, 'tz') and bt_data['timestamp'].dt.tz is not None:
                bt_data['timestamp'] = bt_data['timestamp'].dt.tz_localize(None)

            bt_cols = ['timestamp', 'stlong', 'stlongclose', 'stshort', 'stshortclose']
            bt_cols_exist = [c for c in bt_cols if c in bt_data.columns]
            df_sorted = pd.merge(df_sorted, bt_data[bt_cols_exist], on='timestamp', how='left')
            
            self.exec_history = bt.air_exchange.exec_history
            exec_df = pd.DataFrame(bt.air_exchange.exec_history)
    
        if not exec_df.empty:
            exec_df["timestamp"] = pd.to_datetime(exec_df["timestamp"])
            if hasattr(exec_df['timestamp'].dt, 'tz') and exec_df['timestamp'].dt.tz is not None:
                exec_df['timestamp'] = exec_df['timestamp'].dt.tz_localize(None)
            buy_exec = exec_df[exec_df["size"] > 0][["timestamp", "price"]].copy()
            buy_exec = buy_exec.groupby("timestamp").last().rename(columns={"price": "exec_buy_price"})
            sell_exec = exec_df[exec_df["size"] < 0][["timestamp", "price"]].copy()
            sell_exec = sell_exec.groupby("timestamp").last().rename(columns={"price": "exec_sell_price"})
            exec_prices = buy_exec.join(sell_exec, how="outer").reset_index()
            if hasattr(exec_prices['timestamp'].dt, 'tz') and exec_prices['timestamp'].dt.tz is not None:
                exec_prices['timestamp'] = exec_prices['timestamp'].dt.tz_localize(None)
            df_sorted = pd.merge(df_sorted, exec_prices, on="timestamp", how="left")
        else:
            df_sorted["exec_buy_price"] = np.nan
            df_sorted["exec_sell_price"] = np.nan
    
        # 各ローソク足時点での時価評価 (Mark-to-Market) による資産曲線・PnL算出
        initial_capital = float(data_equity) if (data_equity is not None and data_equity > 0) else 100.0
        maker_fee = 0.0002
        taker_fee = 0.0006

        execs_by_ts = {}
        if self.exec_history:
            for ex in self.exec_history:
                ts = pd.to_datetime(ex["timestamp"])
                if hasattr(ts, "tz") and ts.tz is not None:
                    ts = ts.tz_localize(None)
                if ts not in execs_by_ts:
                    execs_by_ts[ts] = []
                execs_by_ts[ts].append(ex)

        pos_size = 0.0  # ドル換算ポジション数量 (+: ロング, -: ショート)
        avg_entry_price = 0.0
        cum_realized_pnl = 0.0
        cum_fees = 0.0

        equity_list = []
        unrealized_pnl_list = []
        pos_size_list = []
        trades = []
        current_trade = None

        for idx, row in df_sorted.iterrows():
            ts = pd.to_datetime(row['timestamp'])
            if hasattr(ts, "tz") and ts.tz is not None:
                ts = ts.tz_localize(None)
            close_p = float(row['close']) if (row['close'] == row['close'] and row['close'] > 0) else 1.0

            # 1. この足で約定が発生した場合の処理
            if ts in execs_by_ts:
                for ex in execs_by_ts[ts]:
                    ex_size = float(ex['size'])
                    ex_price = float(ex['price'])
                    ex_ord_type = ex.get('ord_type', 'Market')
                    fee_rate = taker_fee if ex_ord_type in ['Market', 'Stop'] else maker_fee
                    cum_fees += abs(ex_size) * fee_rate

                    if pos_size == 0.0:
                        # 新規エントリー
                        pos_size = ex_size
                        avg_entry_price = ex_price
                        current_trade = {
                            'entry_time': ts,
                            'entry_price': ex_price,
                            'size': ex_size,
                            'side': 'LONG' if ex_size > 0 else 'SHORT',
                            'pnl': 0.0,
                            'status': 'OPEN'
                        }
                    else:
                        # ポジションの決済または更新
                        if (pos_size > 0 and ex_size < 0) or (pos_size < 0 and ex_size > 0):
                            # 反対売買による決済
                            close_qty = min(abs(pos_size), abs(ex_size))
                            direction = 1.0 if pos_size > 0 else -1.0
                            trade_pnl = close_qty * direction * ((ex_price - avg_entry_price) / avg_entry_price)
                            cum_realized_pnl += trade_pnl

                            if current_trade:
                                current_trade['exit_time'] = ts
                                current_trade['exit_price'] = ex_price
                                current_trade['pnl'] = trade_pnl
                                current_trade['status'] = 'CLOSED'
                                trades.append(current_trade)
                                current_trade = None

                            new_pos = pos_size + ex_size
                            if abs(new_pos) < 1e-6:
                                pos_size = 0.0
                                avg_entry_price = 0.0
                            else:
                                pos_size = new_pos
                                if (pos_size > 0 and ex_size > 0) or (pos_size < 0 and ex_size < 0):
                                    avg_entry_price = ex_price
                        else:
                            # 同方向への買い増し
                            new_pos = pos_size + ex_size
                            avg_entry_price = (pos_size * avg_entry_price + ex_size * ex_price) / new_pos
                            pos_size = new_pos

            # 2. この足での未決済損益 (Unrealized PnL)
            if pos_size != 0.0 and avg_entry_price > 0:
                direction = 1.0 if pos_size > 0 else -1.0
                unrealized_pnl = abs(pos_size) * direction * ((close_p - avg_entry_price) / avg_entry_price)
            else:
                unrealized_pnl = 0.0

            total_equity = initial_capital + cum_realized_pnl + unrealized_pnl - cum_fees
            equity_list.append(total_equity)
            unrealized_pnl_list.append(unrealized_pnl)
            pos_size_list.append(pos_size)

        # バックテスト終了時に未決済ポジションがある場合、最終終値での含み損益をトレードとして集計
        if current_trade and current_trade['status'] == 'OPEN':
            current_trade['exit_time'] = df_sorted['timestamp'].iloc[-1]
            current_trade['exit_price'] = df_sorted['close'].iloc[-1]
            current_trade['pnl'] = unrealized_pnl_list[-1] if unrealized_pnl_list else 0.0
            current_trade['status'] = 'OPEN_AT_END'
            trades.append(current_trade)

        df_sorted['PL_graph'] = equity_list
        df_sorted['pnl'] = equity_list

        # 指標の計算
        equity_series = pd.Series(equity_list)
        running_max = equity_series.cummax()
        drawdown = running_max - equity_series
        dd_max = float(drawdown.max()) if not drawdown.empty else 0.0
        dd_per = float((drawdown / running_max * 100).max()) if (not running_max.empty and running_max.max() > 0) else 0.0

        trade_count = len(trades)
        winning_trades = [t for t in trades if t['pnl'] > 0]
        losing_trades = [t for t in trades if t['pnl'] < 0]
        win_rate = (len(winning_trades) / trade_count * 100) if trade_count > 0 else 0.0

        gross_profit = sum(t['pnl'] for t in winning_trades)
        gross_loss = abs(sum(t['pnl'] for t in losing_trades))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
        min_unrealized = float(min(unrealized_pnl_list)) if unrealized_pnl_list else 0.0

        self.last_metrics = {
            'DD_max': dd_max,
            'DD_per': dd_per,
            'max_unrealized_loss': min_unrealized,
            'win_rate': win_rate,
            'PF': pf,
            'trade_count': trade_count,
            'trades': trades
        }

        return df_sorted 


def resample_candles(df, interval_minutes):
    if interval_minutes <= 60:
        return df.copy()
    
    df_copy = df.copy()
    df_copy['timestamp'] = pd.to_datetime(df_copy['timestamp'])
    df_copy = df_copy.sort_values('timestamp').reset_index(drop=True)
    
    rule = f'{interval_minutes}min'
    df_copy = df_copy.set_index('timestamp')
    
    agg_dict = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
    }
    if 'turnover' in df_copy.columns:
        agg_dict['turnover'] = 'sum'
    
    resampled = df_copy.resample(rule, closed='left', label='left').agg(agg_dict)
    resampled = resampled.dropna(subset=['open', 'close'])
    resampled = resampled.reset_index()
    
    return resampled


def compute_dynamic_center_margin(df: pd.DataFrame, strategy_type: str = "breakout", side_mode: str = "long") -> float:
    """出来高プロファイル幅(VA幅)またはレンジ下限幅に基づいて動的SLマージン中心値(%)を算出する"""
    if df is None or df.empty:
        return 2.5 if strategy_type == "breakout" else 1.0

    closes = df["close"].values
    if len(closes) == 0 or np.all(np.isnan(closes)):
        return 2.5 if strategy_type == "breakout" else 1.0

    if strategy_type == "breakout":
        # BREAKOUT: (VAH - VAL) / Close * 100
        if "VAH" in df.columns and "VAL" in df.columns:
            vahs = df["VAH"].values
            vals = df["VAL"].values
            va_diffs = np.maximum(0.0, vahs - vals)
            va_pcts = (va_diffs / np.maximum(1e-9, closes)) * 100.0
            valid_pcts = va_pcts[~np.isnan(va_pcts) & (va_pcts > 0)]
            raw_margin = float(np.nanmedian(valid_pcts)) if len(valid_pcts) > 0 else 2.5
        else:
            raw_margin = 2.5
        
        # 安全ガード: 1.0% 〜 8.0%
        center_margin = max(1.0, min(8.0, raw_margin))
    else:
        # RANGE: ロングなら (VAL - lowest_support) / Close * 100
        if "VAL" in df.columns:
            vals = df["VAL"].values
            if "lowest_support" in df.columns:
                lows = df["lowest_support"].values
            elif "low" in df.columns:
                lows = df["low"].values
            else:
                lows = vals * 0.99
            
            diffs = np.maximum(0.0, vals - lows)
            diff_pcts = (diffs / np.maximum(1e-9, closes)) * 100.0
            valid_pcts = diff_pcts[~np.isnan(diff_pcts) & (diff_pcts > 0.05)]
            if len(valid_pcts) > 0:
                raw_margin = float(np.nanmedian(valid_pcts))
            else:
                # フォールバック: VAH-VAL の 0.5倍
                if "VAH" in df.columns:
                    v_diff = np.maximum(0.0, df["VAH"].values - vals)
                    raw_margin = float(np.nanmedian((v_diff / np.maximum(1e-9, closes)) * 50.0))
                else:
                    raw_margin = 1.0
        else:
            raw_margin = 1.0
        
        # 安全ガード: 0.3% 〜 4.0%
        center_margin = max(0.3, min(4.0, raw_margin))

    return round(float(center_margin), 1)


def simulate_envelope_strategy(
    df: pd.DataFrame,
    length: int = 15,
    lower_pct: float = 2.0,
    upper_pct: float = 2.0,
    malen: int = 200,
    max_trades: int = 1,
    initial_equity: float = 100.0,
    fee_rate: float = 0.0006
) -> dict:
    closes = df["close"].values
    n = len(closes)
    if n < max(length, 10):
        return {"final_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "DD_max": 0.0, "max_unrealized_loss": 0.0}

    close_s = pd.Series(closes)
    basis = calc_ema(close_s, length).values
    mabasis = calc_ema(close_s, malen).values if malen <= n else basis
    
    lower = basis * (1.0 - lower_pct / 100.0)
    upper = basis * (1.0 + upper_pct / 100.0)
    
    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    
    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]
    unrealized_list = [0.0]
    exec_history = []
    
    trade_size_usdt = initial_equity / max_trades
    
    for i in range(1, n):
        c = closes[i]
        b = basis[i]
        low_band = lower[i]
        ts = df["timestamp"].iloc[i] if "timestamp" in df.columns else i
        
        # 決済チェック (close > basis かつ avg_price 以上で利確)
        if pos_qty > 0:
            if c > b and c > avg_price:
                sell_val = pos_qty * c
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                exec_history.append({"timestamp": ts, "price": c, "size": -pos_qty, "type": "SELL"})
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                
        # エントリーチェック (close < lower)
        if pos_count < max_trades:
            can_enter = False
            if pos_count == 0:
                if c < low_band:
                    can_enter = True
            else:
                add_pct = calc_add_pct(pos_count)
                if c < low_band and c < avg_price * (1.0 - add_pct):
                    can_enter = True
                    
            if can_enter:
                buy_val = trade_size_usdt
                qty = buy_val / c
                fee = buy_val * fee_rate
                cum_fees += fee
                pos_cost += buy_val
                pos_qty += qty
                avg_price = pos_cost / pos_qty
                pos_count += 1
                exec_history.append({"timestamp": ts, "price": c, "size": qty, "type": "BUY"})
                
        unrealized = (pos_qty * c - pos_cost) if pos_qty > 0 else 0.0
        unrealized_list.append(unrealized)
        current_eq = initial_equity + cum_realized_pnl + unrealized - cum_fees
        equity_curve.append(current_eq)
        
    eq_series = pd.Series(equity_curve)
    peak = eq_series.cummax()
    dd = peak - eq_series
    dd_max = float(dd.max()) if not dd.empty else 0.0
    
    trade_cnt = len(trades)
    win_cnt = sum(1 for t in trades if t > 0)
    win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
    final_pnl = float(equity_curve[-1]) - initial_equity
    min_unrealized = float(min(unrealized_list)) if unrealized_list else 0.0
    
    return {
        "final_pnl": final_pnl,
        "trade_count": trade_cnt,
        "win_rate": win_rate,
        "DD_max": dd_max,
        "max_unrealized_loss": min_unrealized,
        "strategy": "envelope",
        "equity_curve": equity_curve,
        "exec_history": exec_history,
        "params": {
            "length": length,
            "lower_pct": lower_pct,
            "upper_pct": upper_pct,
            "malen": malen,
            "max_trades": max_trades
        }
    }


def simulate_rsima_strategy(
    df: pd.DataFrame,
    rsi_len: int = 9,
    lma_len: int = 7,
    lEp: float = 40.0,
    lCp: float = 60.0,
    max_trades: int = 1,
    initial_equity: float = 100.0,
    fee_rate: float = 0.0006
) -> dict:
    closes = df["close"].values
    n = len(closes)
    if n < max(rsi_len, lma_len) + 5:
        return {"final_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "DD_max": 0.0, "max_unrealized_loss": 0.0}

    close_s = pd.Series(closes)
    rsi_s = calc_rsi(close_s, rsi_len)
    lrsiMA_s = calc_ema(rsi_s, lma_len)
    
    rsi = rsi_s.values
    lrsiMA = lrsiMA_s.values
    
    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    
    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]
    unrealized_list = [0.0]
    exec_history = []
    
    trade_size_usdt = initial_equity / max_trades
    
    for i in range(1, n):
        c = closes[i]
        r = rsi[i]
        r_prev = rsi[i-1]
        ma_val = lrsiMA[i]
        ma_prev = lrsiMA[i-1]
        ts = df["timestamp"].iloc[i] if "timestamp" in df.columns else i
        
        # ゴールデンクロス判定: rsi > lrsiMA かつ 前足では rsi <= lrsiMA
        gc = (r > ma_val) and (r_prev <= ma_prev)
        
        # 決済チェック: rsi > lCp かつ c > avg_price で利確
        if pos_qty > 0:
            if r > lCp and c > avg_price:
                sell_val = pos_qty * c
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                exec_history.append({"timestamp": ts, "price": c, "size": -pos_qty, "type": "SELL"})
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                
        # エントリーチェック: lrsiMA < lEp and rsi < lCp
        if pos_count < max_trades and gc:
            if ma_val < lEp and r < lCp:
                can_enter = False
                if pos_count == 0:
                    can_enter = True
                else:
                    add_pct = calc_add_pct(pos_count)
                    if c < avg_price * (1.0 - add_pct):
                        can_enter = True
                        
                if can_enter:
                    buy_val = trade_size_usdt
                    qty = buy_val / c
                    fee = buy_val * fee_rate
                    cum_fees += fee
                    pos_cost += buy_val
                    pos_qty += qty
                    avg_price = pos_cost / pos_qty
                    pos_count += 1
                    exec_history.append({"timestamp": ts, "price": c, "size": qty, "type": "BUY"})
                    
        unrealized = (pos_qty * c - pos_cost) if pos_qty > 0 else 0.0
        unrealized_list.append(unrealized)
        current_eq = initial_equity + cum_realized_pnl + unrealized - cum_fees
        equity_curve.append(current_eq)
        
    eq_series = pd.Series(equity_curve)
    peak = eq_series.cummax()
    dd = peak - eq_series
    dd_max = float(dd.max()) if not dd.empty else 0.0
    
    trade_cnt = len(trades)
    win_cnt = sum(1 for t in trades if t > 0)
    win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
    final_pnl = float(equity_curve[-1]) - initial_equity
    min_unrealized = float(min(unrealized_list)) if unrealized_list else 0.0
    
    return {
        "final_pnl": final_pnl,
        "trade_count": trade_cnt,
        "win_rate": win_rate,
        "DD_max": dd_max,
        "max_unrealized_loss": min_unrealized,
        "strategy": "rsima",
        "equity_curve": equity_curve,
        "exec_history": exec_history,
        "params": {
            "rsi_len": rsi_len,
            "lma_len": lma_len,
            "lEp": lEp,
            "lCp": lCp,
            "max_trades": max_trades
        }
    }


def optimize_symbol_strategy(
    df: pd.DataFrame,
    symbol: str = "",
    max_trades: int = 1,
    initial_equity: float = 100.0,
    force_strategy: Optional[str] = None
) -> Tuple[str, Dict[str, Any], Dict[str, Any], list]:
    """
    対象銘柄に対して Envelope 戦略と RSI MA 戦略のグリッドサーチを実行し、
    PnLが最大となる戦略と最適パラメータを決定する。
    """
    results = {}
    
    # 1. Envelope 戦略グリッドサーチ
    if force_strategy is None or force_strategy.lower() == "envelope":
        env_lengths = [10, 15, 20, 25]
        env_lower_pcts = [1.5, 2.0, 2.5, 3.0]
        env_malens = [100, 200]
        
        for l in env_lengths:
            for lp in env_lower_pcts:
                for ml in env_malens:
                    res = simulate_envelope_strategy(
                        df, length=l, lower_pct=lp, upper_pct=lp, malen=ml,
                        max_trades=max_trades, initial_equity=initial_equity
                    )
                    label = f"Envelope_L{l}_P{lp}_MA{ml}"
                    results[label] = res

    # 2. RSIMA 戦略グリッドサーチ
    if force_strategy is None or force_strategy.lower() == "rsima":
        rsi_lengths = [7, 9, 14]
        lma_lengths = [5, 7, 10]
        lEps = [30, 35, 40, 45]
        lCps = [55, 60, 65, 70]
        
        for rl in rsi_lengths:
            for ml in lma_lengths:
                for ep in lEps:
                    for cp in lCps:
                        res = simulate_rsima_strategy(
                            df, rsi_len=rl, lma_len=ml, lEp=ep, lCp=cp,
                            max_trades=max_trades, initial_equity=initial_equity
                        )
                        label = f"RSIMA_R{rl}_M{ml}_Ep{ep}_Cp{cp}"
                        results[label] = res

    if not results:
        default_res = {
            "strategy": "envelope",
            "final_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "DD_max": 0.0,
            "max_unrealized_loss": 0.0,
            "params": {"length": 15, "lower_pct": 2.0, "upper_pct": 2.0, "malen": 200, "max_trades": max_trades}
        }
        return "envelope", default_res["params"], default_res, []

    active_results = {k: v for k, v in results.items() if v.get("trade_count", 0) > 0}
    eval_pool = active_results if active_results else results

    best_key = max(eval_pool.keys(), key=lambda k: eval_pool[k]["final_pnl"])
    best_res = eval_pool[best_key]
    best_strat = best_res["strategy"]
    best_params = best_res["params"]

    sorted_results = sorted(results.items(), key=lambda x: x[1]["final_pnl"], reverse=True)
    top10 = sorted_results[:10]

    return best_strat, best_params, best_res, top10


def run_interval_comparison(df_60m, lot=1.0, data_equity=100.0, side_mode="long", symbol="", force_strategy=None, prefer_breakout=False, max_trades=1):
    """
    エンベロープ戦略および RSI MA 戦略の網羅的グリッドサーチを実行し、
    各銘柄のPnLが最大となる戦略と最適パラメータを選定する。
    """
    logic = logicinstance()
    os.makedirs("backtest_data", exist_ok=True)
    fixed_initial_equity = float(data_equity) if (data_equity is not None and data_equity > 0) else 100.0
    
    discord.print_log(f"\n====== [{symbol}] エンベロープ ＆ RSI MA 個別最適化（PnL最大化）開始 ======")
    best_strat, best_params, best_res, top10 = optimize_symbol_strategy(
        df=df_60m,
        symbol=symbol,
        max_trades=max_trades,
        initial_equity=fixed_initial_equity,
        force_strategy=force_strategy
    )
    
    # 探索結果辞書の再フォーマット（呼び出し元互換用）
    results = {}
    for label, data in top10:
        results[label] = {
            'strategy_type': data['strategy'],
            'final_pnl': data['final_pnl'],
            'interval': 60,
            'mp_period': data['params'].get('malen', data['params'].get('lma_len', 7)),
            'er_threshold': data['params'].get('lower_pct', data['params'].get('lEp', 40)),
            'atr_multi': 1.5,
            'sl_center_margin': data['params'].get('lower_pct', 2.0),
            'DD_max': data.get('DD_max', 0.0),
            'max_unrealized_loss': data.get('max_unrealized_loss', 0.0),
            'win_rate': data.get('win_rate', 0.0),
            'trade_count': data.get('trade_count', 0),
            'params': data['params']
        }
    
    discord.print_log("【個別最適化バックテスト結果 (Top 10)】")
    header = f"{'設定':<35} {'PnL [USDT]':>12} {'DD_max':>8} {'勝率':>7} {'取引':>6}"
    separator = "-" * 73
    all_rows = []
    for label, data in top10:
        row_disp = f"{symbol}_{label}" if (symbol and not str(label).startswith(symbol)) else label
        all_rows.append(f"{row_disp:<35} {data['final_pnl']:>+12.4f} {data['DD_max']:>8.4f} {data['win_rate']:>6.1f}% {data['trade_count']:>6}")
    table_lines = ["```", header, separator] + all_rows + ["```"]
    discord.print_log("\n".join(table_lines))
    
    best_disp = f"{symbol}_{best_strat.upper()}"
    discord.print_log(f"★ PnL最大選定: {best_disp} -> 純利益: {best_res['final_pnl']:+.4f} USDT (勝率: {best_res['win_rate']:.1f}%, 取引: {best_res['trade_count']}回, 最大DD: {best_res['DD_max']:.4f})")
    discord.print_log(f"   └ 採用パラメータ: {best_params}")

    # バックテストチャートの生成
    try:
        df_chart = df_60m.copy()
        df_chart['timestamp'] = pd.to_datetime(df_chart['timestamp'])
        if 'equity_curve' in best_res and len(best_res['equity_curve']) == len(df_chart):
            df_chart['pnl'] = best_res['equity_curve']
            df_chart['PL_graph'] = best_res['equity_curve']
        else:
            df_chart['pnl'] = fixed_initial_equity + best_res['final_pnl']
            df_chart['PL_graph'] = fixed_initial_equity + best_res['final_pnl']
            
        df_chart['exec_buy_price'] = np.nan
        df_chart['exec_sell_price'] = np.nan
        for ex in best_res.get('exec_history', []):
            ex_ts = pd.to_datetime(ex['timestamp'])
            mask = df_chart['timestamp'] == ex_ts
            if mask.any():
                if ex['size'] > 0:
                    df_chart.loc[mask, 'exec_buy_price'] = ex['price']
                else:
                    df_chart.loc[mask, 'exec_sell_price'] = ex['price']
                    
        eval_bars_10d = min(len(df_chart), 240)
        chart_10d_df = df_chart.tail(eval_bars_10d).reset_index(drop=True)
        bg_csv = f"backtest_data/klines100_{symbol}_bingx.csv"
        chart_10d_df.to_csv(bg_csv, index=False)
        discord.plot_backtest(label=f"BingX_{symbol}_{best_strat.upper()}", csv_file=bg_csv, symbol=symbol)
    except Exception as ch_err:
        print(f"[Chart Error] {symbol}: {ch_err}")

    cand_mp = best_params.get('malen', best_params.get('lma_len', 7))
    cand_er = best_params.get('lower_pct', best_params.get('lEp', 40))
    cand_margin = best_params.get('lower_pct', 2.0)
    return results, best_strat, 60, cand_mp, cand_er, cand_margin


class VPTrailingManager:
    """ボリュームプロファイル（VAL / POC / VAH）に基づく階層型ステップトレーリング管理クラス"""
    def __init__(
        self,
        side: str,
        entry_price: float,
        fee_margin_pct: float = 0.0005,      # 手数料カバーマージン (0.05%)
        initial_margin_pct: float = 0.0005,  # VAL/VAH 初期マージン (0.05%)
        strategy_type: str = "range",        # 戦略タイプ（range時はマージン拡大）
        entry_val: Optional[float] = None,   # エントリー時の VAL（固定初期SL基準）
        entry_vah: Optional[float] = None    # エントリー時の VAH（固定初期SL基準）
    ):
        self.side = side.upper()
        self.entry_price = float(entry_price)
        self.fee_margin_pct = float(fee_margin_pct)
        self.initial_margin_pct = float(initial_margin_pct)
        self.strategy_type = strategy_type
        # レンジ戦略はエントリーがVAL/VAH付近なので、初期マージンを2倍に拡大
        if strategy_type == "range":
            self.initial_margin_pct = self.initial_margin_pct * 2
        
        self.step = 0  # 0: INITIAL (VAL/VAH-margin), 1: POC, 2: VAH/VAL
        self.step_names = {0: "INITIAL", 1: "POC", 2: "VAH" if self.side == "LONG" else "VAL"}
        
        # エントリー時のVAL/VAHに基づく初期ストップの完全固定（相場変動で勝手に切り上がらないようにする）
        if self.side == "LONG":
            base_val = float(entry_val) if (entry_val is not None and entry_val > 0) else (self.entry_price * 0.99)
            self.current_stop = base_val * (1.0 - self.initial_margin_pct)
        else:
            base_vah = float(entry_vah) if (entry_vah is not None and entry_vah > 0) else (self.entry_price * 1.01)
            self.current_stop = base_vah * (1.0 + self.initial_margin_pct)

    def update(
        self,
        current_price: float,
        close_price: float,
        val: float,
        poc: float,
        vah: float
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """現在の価格情報を受け取り、トレーリング撤退ラインの更新と決済判定を返却

        Returns:
            Tuple[bool, str, Dict[str, Any]]:
                - should_close (bool): 成行クローズすべきか
                - reason (str): クローズ理由または現状維持メッセージ
                - info (dict): 現在のステータス情報
        """
        cur_px = float(current_price)
        cls_px = float(close_price)
        val_px = float(val)
        poc_px = float(poc)
        vah_px = float(vah)

        if self.side == "LONG":
            be_price = self.entry_price * (1.0 + self.fee_margin_pct)
            is_profitable = cur_px >= be_price

            # 1. 段階ごとの引き上げ・ステップアップ判定
            # 【重要】建値＋手数料カバー価格以上の含み益の時のみ引き上げを許可
            if is_profitable:
                # Step 2 (VAH) への昇格チェック
                if cur_px > vah_px:
                    self.step = 2
                    self.current_stop = max(self.current_stop, be_price, vah_px)
                # Step 1 (POC) への昇格チェック
                elif cur_px > poc_px or self.step >= 1:
                    self.step = max(self.step, 1)
                    target_sl = max(be_price, poc_px)
                    self.current_stop = max(self.current_stop, target_sl)
            else:
                # 含み損の間は初期ストップラインから絶対に引き上げない (step 0 を維持)
                if self.step == 0:
                    pass
                else:
                    # 既に昇格済みの場合は建値(手数料込)を下回らないように維持
                    self.current_stop = max(self.current_stop, be_price)

            # 2. クローズ条件（終値判定）
            should_close = cls_px < self.current_stop
            reason = f"LONG_VP_{self.step_names[self.step]}_BREAK" if should_close else "HOLD"

        else:  # SHORT
            be_price = self.entry_price * (1.0 - self.fee_margin_pct)
            is_profitable = cur_px <= be_price

            # 1. 段階ごとの引き下げ・ステップアップ判定
            if is_profitable:
                # Step 2 (VAL) への昇格チェック
                if cur_px < val_px:
                    self.step = 2
                    self.current_stop = min(self.current_stop, be_price, val_px) if self.current_stop > 0 else min(be_price, val_px)
                # Step 1 (POC) への昇格チェック
                elif cur_px < poc_px or self.step >= 1:
                    self.step = max(self.step, 1)
                    target_sl = min(be_price, poc_px)
                    self.current_stop = min(self.current_stop, target_sl) if self.current_stop > 0 else target_sl
            else:
                if self.step == 0:
                    pass
                else:
                    self.current_stop = min(self.current_stop, be_price) if self.current_stop > 0 else be_price

            # 2. クローズ条件（終値判定）
            should_close = cls_px > self.current_stop
            reason = f"SHORT_VP_{self.step_names[self.step]}_BREAK" if should_close else "HOLD"

        info = {
            "step": self.step,
            "step_name": self.step_names[self.step],
            "current_stop": self.current_stop,
            "close_price": cls_px,
            "current_price": cur_px
        }
        return should_close, reason, info


