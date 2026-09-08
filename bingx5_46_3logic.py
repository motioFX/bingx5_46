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
from bingx5_46_5backtest_mm import make_mm_pl, Backtest, AirExchange
from config_loader import get_webhook_url


def calc_add_pct(n: int) -> float:
    """Return add-on percentage based on position count."""
    if n <= 3:
        return 0.001  # 0.2%
    if n <= 5:
        return 0.002  # 0.3%
    if n <= 8:
        return 0.01   # 0.4%
    return 0.02       # 0.5%


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

    def make_logic(self, df, market_profile_period=720, er_threshold=0.3, strategy_type="range", use_decay=True, decay_half_life=None, vol_surge_mult=1.2, fr_threshold=0.00002):
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
        
        if strategy_type in ("breakout", "squeeze_breakout"):
            df['long'] = df['long_breakout']
            df['short'] = df['short_breakout']
        elif strategy_type == "adaptive":
            # 高ER時はBreakout（ER条件付き）、低ER時はRange（ER条件なし=素のシグナル）
            df['long'] = np.where(df['er'] > er_threshold, df['long_breakout'], df['long_range_raw'])
            df['short'] = np.where(df['er'] > er_threshold, df['short_breakout'], df['short_range_raw'])
        else:
            df['long'] = df['long_range']
            df['short'] = df['short_range']
        
        df['lowest_support'] = df['low'].rolling(window=market_profile_period, min_periods=10).min().ffill()
        
        df['longclose'] = False
        df['shortclose'] = False

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

        # 互換用エイリアス
        self.bingx_webhook = self.real3_webhook
        self.win32_webhook = self.test4_webhook
        self.default_webhook = self.real3_webhook
        self.webhook_url = self.test4_webhook if sys.platform == 'win32' else self.real3_webhook

        if not self.real3_webhook and not self.test4_webhook:
            print("[WARN] Discord webhook for BingX (real3_bngx / test4_backtest) is not configured.")
        
        from rich.console import Console
        self.console = Console(color_system="standard", force_terminal=True, highlight=False)

        self.lock = send_discord._shared_lock
        self.buffers = send_discord._shared_buffers
        self.timers = send_discord._shared_timers

    def _get_target_webhooks(self, text: str) -> list[str]:
        # バックテストや最適化、検証結果のメッセージは test4_backtest チャンネルへ出力
        is_backtest = any(k in text for k in ("backtest", "バックテスト", "最適化", "RANGEの最適化", "BREAKOUTの最適化", "戦略比較結果"))
        if is_backtest:
            target = self.test4_webhook or self.real3_webhook
            return [target] if target else []

        if sys.platform == 'win32':
            # Windowsローカル環境: test4_backtest（テストチャンネル）へ出力
            target = self.test4_webhook or self.real3_webhook
            return [target] if target else []
        else:
            # Linux VPS本番環境: real3_bngx（BingX本番チャンネル）へ出力
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

    def _send_file(self, content_text, file_path, file_name, mime_type):
        webhooks = self._get_target_webhooks(content_text)
        for url in webhooks:
            self.flush_buffer(url)
            try:
                payload = {"content": content_text}
                with open(file_path, f"rb") as f:
                    files = {"file": (file_name, f, mime_type)}
                    response = requests.post(url, data=payload, files=files, timeout=30)
                    response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"Failed to send file {file_name} to {url}:", e)
                pass

    def send_file(self, file_path, description=""):
        p = Path(file_path) if not isinstance(file_path, Path) else file_path
        if not p.exists():
            print(f"[send_discord] File not found: {p}")
            return
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        self._send_file(description, str(p), p.name, mime)

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
        ax1.set_ylabel("Price [USDC]")
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

        ax1.set_ylabel("close [USDC]", fontsize=9)
        ax1.grid(True, axis='y', linestyle=':', alpha=0.3)

        # 5. 右軸 (twinx): 累積 PnL
        ax2 = ax1.twinx()
        b_plot = df['pnl'] if 'pnl' in df.columns else np.zeros(len(df))
        ax2.plot(x_indices, b_plot, "C1", label="pl", linewidth=1.5)
        ax2.set_ylabel("pnl [USDC]", fontsize=9)
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
        ax1.set_title(f"{title_prefix}{label} | Final PnL: {final_pnl:.4f} USDC", fontsize=10, pad=10)

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
        self.print_log(f"best lot : {optimal_lot_usdt:.2f} USDC, onhand_amount : {onhand_amount_usdt:.2f} USDC")
        time.sleep(1)
        self.print_log("-----------------------------------------")
        time.sleep(1)


discord = send_discord()

# BingX PnL Calculator
class PnLCalculator:
    def __init__(self, apis_config=None, rest_api_url=None, symbol='BTC', mode='demo'):
        if apis_config is None:
            try:
                from bingx5_46_2api import apis
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
            from bingx5_46_2api import sign_bingx, RestAPI_url
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


def run_interval_comparison(df_60m, lot, data_equity, side_mode="long", symbol="", force_strategy=None, prefer_breakout=False):
    logic = logicinstance()
    bt_instance = backtester()
    results = {}
    fixed_initial_equity = 100.0
    os.makedirs("backtest_data", exist_ok=True)
    
    if force_strategy and force_strategy.lower() in ["breakout", "range"]:
        strategy_types = [force_strategy.lower()]
        discord.print_log(f"⚡ [{symbol}] 戦略強制モード適用: {force_strategy.upper()}_ONLY（爆上げモメンタム候補）")
    else:
        strategy_types = ["range", "breakout"]
    intervals = [60, 120, 180]
    mp_periods = [12, 24, 36, 48, 60, 72, 96, 120, 144, 168]
    er_thresholds = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    
    base_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    for extra in ['turnover', 'fundingRate', 'funding', 'funding_rate', 'openInterest', 'oi']:
        if extra in df_60m.columns and extra not in base_cols:
            base_cols.append(extra)
    df_raw = df_60m[base_cols].copy()
    
    for strat in strategy_types:
        discord.print_log(f"\n====== 戦略: {strat.upper()} の最適化を開始 ======")
        for interval in intervals:
            interval_label = f"{interval}m"
            
            df_interval = resample_candles(df_raw, interval)
            
            if df_interval is None or df_interval.empty or len(df_interval) < 30:
                continue
            
            for mp_period in mp_periods:
                atr_multi = 1.5
                
                df_copy = df_interval.copy()
                df_copy = logic.make_logic(df_copy, market_profile_period=mp_period, er_threshold=0, strategy_type=strat)
                
                for er_th in er_thresholds:
                    df_run = df_copy.copy()
                    if strat == "breakout":
                        df_run['long'] = (
                            (logic.crossover(df_run['close'], df_run['VAH'])) & 
                            (df_run['er'] > er_th) & 
                            (df_run.get('is_vol_surge', True)) & 
                            (df_run.get('is_fr_favorable', True)) & 
                            (df_run.get('is_oi_surge', True))
                        )
                        df_run['short'] = (logic.crossunder(df_run['close'], df_run['VAL'])) & (df_run['er'] > er_th)
                    else:
                        df_run['long'] = (logic.crossover(df_run['close'], df_run['VAL'])) & (df_run['er'] > er_th)
                        df_run['short'] = (logic.crossunder(df_run['close'], df_run['VAH'])) & (df_run['er'] > er_th)
                        
                    eval_bars = max(20, 120 * 60 // interval)
                    df_eval = df_run.tail(eval_bars).reset_index(drop=True)
                    
                    label = f"{strat}_{interval_label}_MP{mp_period}_ER{er_th}"
                    
                    # 動的中心値マージンの算出
                    sl_center = compute_dynamic_center_margin(df_eval, strategy_type=strat, side_mode=side_mode)
                    
                    result_df = bt_instance.run_backtest(
                        df=df_eval,
                        lot=lot,
                        data_equity=fixed_initial_equity,
                        side_mode=side_mode,
                        mp_period=mp_period,
                        atr_tp_multi=atr_multi,
                        er_threshold=er_th,
                        strategy_type=strat,
                        sl_margin_pct=sl_center
                    )
                    
                    metrics = getattr(bt_instance, 'last_metrics', {})
                    final_pnl = result_df['pnl'].iloc[-1] if not result_df.empty and 'pnl' in result_df.columns else 0
                    
                    results[label] = {
                        'strategy_type': strat,
                        'df': result_df,
                        'final_pnl': final_pnl,
                        'interval': interval,
                        'mp_period': mp_period,
                        'er_threshold': er_th,
                        'atr_multi': atr_multi,
                        'sl_center_margin': sl_center,
                        'DD_max': metrics.get('DD_max', 0),
                        'DD_per': metrics.get('DD_per', 0),
                        'max_unrealized_loss': metrics.get('max_unrealized_loss', 0),
                        'win_rate': metrics.get('win_rate', 0),
                        'PF': metrics.get('PF', float('inf')),
                        'trade_count': metrics.get('trade_count', 0)
                    }
                    print(f"{label}: PnL={final_pnl:.4f} | DD={metrics.get('DD_max', 0):.4f} | 取引={metrics.get('trade_count', 0)} (SL={sl_center}%)")
                    time.sleep(0.002)
        
    if not results:
        discord.print_log("全バックテスト結果なし")
        return results, "range", 60, 48, 0.3, 1.5
    
    active_results = {k: v for k, v in results.items() if v.get('trade_count', 0) > 0}
    eval_pool = active_results if active_results else results

    best_pnl_key = max(eval_pool.keys(), key=lambda x: eval_pool[x]['final_pnl'])
    best_pnl_disp = f"{symbol}_{best_pnl_key}" if (symbol and not str(best_pnl_key).startswith(symbol)) else best_pnl_key
    best_pnl_msg = f"★ PnL最大: {best_pnl_disp} (PnL: {eval_pool[best_pnl_key]['final_pnl']:.4f} USDT, 取引: {eval_pool[best_pnl_key].get('trade_count', 0)}回)"
    best_risk_key = min(eval_pool.keys(), key=lambda x: eval_pool[x]['DD_max'] if eval_pool[x]['DD_max'] == eval_pool[x]['DD_max'] else float('inf'))
    best_risk_disp = f"{symbol}_{best_risk_key}" if (symbol and not str(best_risk_key).startswith(symbol)) else best_risk_key
    best_risk_msg = f"★ リスク最小: {best_risk_disp} (最大DD: {eval_pool[best_risk_key]['DD_max']:.4f})"
    
    range_active = {k: v for k, v in results.items() if v['strategy_type'] == 'range' and v.get('trade_count', 0) > 0}
    best_range_key = max(range_active.keys(), key=lambda x: range_active[x]['final_pnl']) if range_active else None

    breakout_active = {k: v for k, v in results.items() if v['strategy_type'] == 'breakout' and v.get('trade_count', 0) > 0}
    best_breakout_key = max(breakout_active.keys(), key=lambda x: breakout_active[x]['final_pnl']) if breakout_active else None
    
    discord.print_log("\n====== 戦略比較結果 ======")
    if best_range_key:
        r_disp = f"{symbol}_{best_range_key}" if (symbol and not str(best_range_key).startswith(symbol)) else best_range_key
        discord.print_log(f"【レンジ戦略ベスト】: {r_disp} -> PnL: {results[best_range_key]['final_pnl']:.4f} USDT (取引: {results[best_range_key]['trade_count']}回)")
    else:
        discord.print_log("【レンジ戦略ベスト】: 該当なし (期間中トレードなし)")

    if best_breakout_key:
        b_disp = f"{symbol}_{best_breakout_key}" if (symbol and not str(best_breakout_key).startswith(symbol)) else best_breakout_key
        discord.print_log(f"【ブレイクアウト戦略ベスト】: {b_disp} -> PnL: {results[best_breakout_key]['final_pnl']:.4f} USDT (取引: {results[best_breakout_key]['trade_count']}回)")
    else:
        discord.print_log("【ブレイクアウト戦略ベスト】: 該当なし (期間中トレードなし)")
    
    discord.print_log("【時間足×戦略タイプ×MP期間×ER比較バックテスト結果 (Top 10)】")
    header = f"{'設定':<35} {'PnL':>11} {'DD_max':>8} {'含み損':>8} {'取引':>6}"
    separator = "-" * 73
    
    sorted_results = sorted(results.items(), key=lambda item: item[1]['final_pnl'], reverse=True)
    top_results = sorted_results[:10]
    
    all_rows = []
    for label, data in top_results:
        row_disp = f"{symbol}_{label}" if (symbol and not str(label).startswith(symbol)) else label
        all_rows.append(f"{row_disp:<35} {data['final_pnl']:>11.4f} {data['DD_max']:>8.4f} {data['max_unrealized_loss']:>8.4f} {data['trade_count']:>6}")
    
    table_lines = ["```", header, separator] + all_rows + ["```"]
    discord.print_log("\n".join(table_lines))
    
    time.sleep(1.0)
    discord.print_log(best_pnl_msg)
    time.sleep(1.0)
    discord.print_log(best_risk_msg)
    
    # 爆上げモメンタム候補（prefer_breakout=True）の場合のブレイクアウト優先採用判定
    if prefer_breakout and best_breakout_key:
        bo_data = results[best_breakout_key]
        rg_data = results[best_range_key] if best_range_key else None
        # ブレイクアウトでプラス収益（> 100 USDT）かつ取引実績がある場合
        if bo_data['final_pnl'] > 100.0 and bo_data.get('trade_count', 0) > 0:
            # レンジのPnLに対して80%以上あれば、爆発力のあるブレイクアウトを優先採用
            if not rg_data or bo_data['final_pnl'] >= rg_data['final_pnl'] * 0.80:
                best_pnl_key = best_breakout_key
                best_pnl_disp = f"{symbol}_{best_pnl_key}" if (symbol and not str(best_pnl_key).startswith(symbol)) else best_pnl_key
                discord.print_log(f"🚀 [{symbol}] 爆上げモメンタム優遇: BREAKOUT戦略を優先採用! (ブレイクアウト: {bo_data['final_pnl']:.2f} USDT vs レンジ: {rg_data['final_pnl'] if rg_data else 0:.2f} USDT)")

    best_pnl_data = results[best_pnl_key]
    best_strategy = best_pnl_data['strategy_type']
    best_interval = best_pnl_data['interval']
    best_mp = best_pnl_data['mp_period']
    best_er = best_pnl_data['er_threshold']
    best_atr = best_pnl_data['atr_multi']
    best_margin = best_pnl_data.get('sl_center_margin', 1.0)
    
    # 最適化設定（動的マージン適用）でのチャート生成・描画
    df_interval = resample_candles(df_raw, best_interval)
    df_logic = logic.make_logic(df_interval, market_profile_period=best_mp, er_threshold=0, strategy_type=best_strategy)
    df_run_margin = df_logic.copy()
    if best_strategy == "breakout":
        df_run_margin['long'] = (
            (logic.crossover(df_run_margin['close'], df_run_margin['VAH'])) & 
            (df_run_margin['er'] > best_er) & 
            (df_run_margin.get('is_vol_surge', True)) & 
            (df_run_margin.get('is_fr_favorable', True)) & 
            (df_run_margin.get('is_oi_surge', True))
        )
        df_run_margin['short'] = (logic.crossunder(df_run_margin['close'], df_run_margin['VAL'])) & (df_run_margin['er'] > best_er)
    else:
        df_run_margin['long'] = (logic.crossover(df_run_margin['close'], df_run_margin['VAL'])) & (df_run_margin['er'] > best_er)
        df_run_margin['short'] = (logic.crossunder(df_run_margin['close'], df_run_margin['VAH'])) & (df_run_margin['er'] > best_er)
    
    full_margin_df = bt_instance.run_backtest(
        df=df_run_margin, lot=lot, data_equity=fixed_initial_equity, side_mode=side_mode,
        mp_period=best_mp, atr_tp_multi=best_atr, er_threshold=best_er,
        strategy_type=best_strategy, sl_margin_pct=best_margin
    )
    # 1. 5日間最適化チャート (直近120h)
    eval_bars_5d = max(20, 120 * 60 // best_interval)
    chart_5d_df = full_margin_df.tail(eval_bars_5d).reset_index(drop=True)
    best_csv = f"backtest_data/klines100_{best_pnl_key}_margin{best_margin}.csv"
    chart_5d_df.to_csv(best_csv, index=False)
    discord.plot_backtest(label=f"Margin{best_margin}%", csv_file=best_csv, symbol=symbol)
    
    # 2. 10日間長期チャート (直近240h: 同一データフレームから完全転写)
    eval_bars_10d = min(len(full_margin_df), max(30, 240 * 60 // best_interval))
    chart_10d_df = full_margin_df.tail(eval_bars_10d).reset_index(drop=True)
    bg_csv = f"backtest_data/klines100_{symbol}_bingx.csv"
    chart_10d_df.to_csv(bg_csv, index=False)
    discord.plot_backtest(label=f"BingX_{symbol}", csv_file=bg_csv, symbol=symbol)

    discord.print_log(f"\n★ 採用設定: 戦略={best_strategy.upper()} {side_mode.upper()}, 時間足={best_interval}m, MP期間={best_mp}, ER閾値={best_er}, Margin={best_margin}% (出来高プロファイル動的適応)")
    return results, best_strategy, best_interval, best_mp, best_er, best_margin


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


