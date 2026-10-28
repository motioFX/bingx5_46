from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Any, Optional, List
from pathlib import Path
import asyncio
import time
import os
import requests
import json
import pybotters
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as md
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich import print
from bingx5_46_5backtest_mm import make_mm_pl, Backtest, AirExchange
from config_loader import get_webhook_url


def calc_add_pct(n: int, strategy: str = "val_poc") -> float:
    """Return add-on percentage based on position count.
    VAL_POC: 3.0% (BingX実証最適値: 浅いノイズでの建玉肥大化を防ぎ損失リスク半減)
    RSIMA: 0.4%〜1.0% (Pine Script RSIMA3 spec)
    """
    strat = str(strategy).lower()
    if strat in ("val_poc", "vp_val_gc"):
        return 0.030  # -3.0% 下落ナンピン
    if n <= 3:
        return 0.004  # 0.4%
    if n <= 5:
        return 0.006  # 0.6%
    if n <= 8:
        return 0.008  # 0.8%
    return 0.010       # 1.0%


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

    def make_market_profile(self, df, period=100, value_area_pct=0.70, num_bins=100, use_decay=True, decay_half_life=None, tail_only=None):
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
            
        try:
            period = max(10, int(period or 100))
        except Exception:
            period = 100

        poc_arr = np.full(n, np.nan)
        vah_arr = np.full(n, np.nan)
        val_arr = np.full(n, np.nan)
        
        min_required = min(5, period)
        start_loop = max(min_required, n - int(tail_only)) if (tail_only is not None and tail_only > 0) else min_required
        
        highs = df['high'].astype(float).values
        lows = df['low'].astype(float).values
        closes = df['close'].astype(float).values
        opens = df['open'].astype(float).values if 'open' in df.columns else closes
        volumes = df['volume'].astype(float).values if 'volume' in df.columns else np.ones(n)
        
        if np.all(volumes <= 0):
            volumes = np.ones(n)
            
        half_life = float(decay_half_life) if decay_half_life is not None else max(5.0, period / 2.0)
        decay_factor = 0.5 ** (1.0 / half_life) if use_decay else 1.0
            
        for i in range(start_loop, n):
            # CPU負荷抑制のためのジェントルスリープ (50足ごとに2ミリ秒休止)
            if (i - start_loop) > 0 and (i - start_loop) % 50 == 0:
                time.sleep(0.002)

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
            
            if len(w_highs) == 0 or len(w_lows) == 0:
                curr_c = closes[i] if i < len(closes) else 0.0
                poc_arr[i] = curr_c
                vah_arr[i] = curr_c
                val_arr[i] = curr_c
                continue

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

    def make_logic(self, df, market_profile_period=720, strategy_type="range", use_decay=True, decay_half_life=None, vol_surge_mult=1.2, fr_threshold=0.00002, **kwargs):
        # パラメータ安全化ガード (0, None, 10未満を安全なデフォルト150に補正)
        try:
            mp_safe = int(market_profile_period) if market_profile_period is not None else 150
        except Exception:
            mp_safe = 150
        if mp_safe < 10:
            mp_safe = 150
        market_profile_period = mp_safe

        atr_period = 10
        df[f'atr_{atr_period}'] = self.make_atr(df, atr_period)
        
        df = self.make_market_profile(df, period=market_profile_period, use_decay=use_decay, decay_half_life=decay_half_life)

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

        # 素のクロスオーバーシグナル
        df['long_breakout_raw'] = self.crossover(df['close'], df['VAH'])
        df['short_breakout_raw'] = self.crossunder(df['close'], df['VAL'])
        df['long_range_raw'] = self.crossover(df['close'], df['VAL'])
        df['short_range_raw'] = self.crossunder(df['close'], df['VAH'])

        # Squeeze条件付きシグナル (ショートスクイーズ・踏み上げブレイクアウト)
        df['long_breakout'] = (
            df['long_breakout_raw'] & 
            df['is_vol_surge'] & 
            df['is_fr_favorable'] & 
            df['is_oi_surge']
        )
        df['short_breakout'] = df['short_breakout_raw']
        df['long_range'] = df['long_range_raw']
        df['short_range'] = df['short_range_raw']
        
        if strategy_type in ("breakout", "squeeze_breakout"):
            df['long'] = df['long_breakout']
            df['short'] = df['short_breakout']
        else:
            df['long'] = df['long_range']
            df['short'] = df['short_range']
        
        roll_win = max(10, int(market_profile_period))
        df['lowest_support'] = df['low'].rolling(window=roll_win, min_periods=min(10, roll_win)).min().ffill()
        
        df['longclose'] = False
        df['shortclose'] = False

        rows_before = len(df)
        required_cols = [c for c in ['open', 'high', 'low', 'close', 'POC', 'VAH', 'VAL'] if c in df.columns]
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
    hourly_only: bool = True  # 【新仕様】Discord発信は1時間に1回（毎時ステータスサマリー）に完全集約

    def __init__(self, hourly_only: bool = True):
        self.hourly_only = hourly_only
        self.real1_webhook = get_webhook_url("real1_bitbank")
        self.real2_webhook = get_webhook_url("real2_hype")
        self.real3_webhook = get_webhook_url("real3_bngx")
        self.test4_webhook = get_webhook_url("test4_backtest")
        self.bingx_webhook = self.real3_webhook
        self.win32_webhook = self.real3_webhook
        self.default_webhook = self.real3_webhook
        self.webhook_url = self.real3_webhook or self.test4_webhook
        if not self.webhook_url and not self.default_webhook and not self.win32_webhook:
            # Fallback warning instead of crash
            print("Discord webhook URL is not configured.")
        
        from rich.console import Console
        self.console = Console(color_system="standard", force_terminal=True, highlight=False)

        self.lock = send_discord._shared_lock
        self.buffers = send_discord._shared_buffers
        self.timers = send_discord._shared_timers

    def _get_target_webhooks(self, text: str) -> list[str]:
        # BingX関連はすべて #real3_bngx (フォールバック: #test4_backtest) へ全集約
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

    def print_log(self, text, level="info", force_send=False):
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

        # 重要メッセージ（起動バナー、約定、決済、損切り、定期レポート、最適化結果、警告/エラー）は自動で即時送信
        is_essential = (
            force_send
            or level in ("warning", "error", "critical")
            or any(kw in text for kw in [
                "AUTO TRADING SYSTEM START",
                "BingX RSIMA3 Scalping System",
                "BingX Auto Trading System",
                "PERIODIC SCREENING",
                "【AIR/実トレード損益レポート",
                "【実運用実績】",
                "[ENTRY]",
                "[CLOSE]",
                "HARD STOP LOSS",
                "NORMAL TP",
                "新規エントリー",
                "一括成行利確",
                "成行損切り",
                "【毎時ステータスサマリー】",
                "★ 【銘柄別 個別最適化パラメータ一覧】",
                "[Selection Result]",
                "選定銘柄",
                "定期銘柄選定時刻",
            ])
        )

        # 【Discord 1時間に1回化】hourly_onlyが有効な場合、重要メッセージ以外はスキップ
        if getattr(self, "hourly_only", True) and not is_essential:
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

    def _send_file(self, content_text, file_path, file_name, mime_type, force_send=True):
        if getattr(self, "hourly_only", True) and not force_send:
            return
        webhooks = self._get_target_webhooks(content_text)
        for url in webhooks:
            self.flush_buffer(url)
            try:
                payload = {"payload_json": json.dumps({"content": content_text}, ensure_ascii=False)}
                with open(file_path, f"rb") as f:
                    files = {"file": (file_name, f, mime_type)}
                    response = requests.post(url, data=payload, files=files, timeout=30)
                    response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"Failed to send file {file_name} to {url}:", e)
                pass

    def send_file(self, file_path, description="", force_send=True):
        if getattr(self, "hourly_only", True) and not force_send:
            return
        p = Path(file_path) if not isinstance(file_path, Path) else file_path
        if not p.exists():
            print(f"[send_discord] File not found: {p}")
            return
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        self._send_file(description, str(p), p.name, mime, force_send=force_send)

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
            if len(df) > 600:
                ops = df["open"].astype(float).values
                his = df["high"].astype(float).values
                los = df["low"].astype(float).values
                cls = df["close"].astype(float).values
                colors = np.where(cls >= ops, "#2e7d32", "#c62828")
                ax1.vlines(x_indices, los, his, color=colors, linewidth=0.6, alpha=0.85, zorder=3)
                body_min = np.minimum(ops, cls)
                body_max = np.maximum(ops, cls)
                same_mask = (body_max - body_min) == 0
                body_max = np.where(same_mask, body_min + np.maximum((his - los) * 0.01, 1e-6), body_max)
                ax1.vlines(x_indices, body_min, body_max, color=colors, linewidth=1.4, zorder=4)
            else:
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
        first_pnl = df['pnl'].iloc[0] if 'pnl' in df.columns and len(df) > 0 else 0.0
        if first_pnl >= 50.0:
            net_profit = final_pnl - first_pnl
            title_text = f"{title_prefix}{label} | Equity: ${final_pnl:.2f} (Net: {net_profit:+.2f} USDC)"
        else:
            title_text = f"{title_prefix}{label} | Final PnL: {final_pnl:+.2f} USDC"
        ax1.set_title(title_text, fontsize=10, pad=10)

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
        fills = await self.get_hyperliquid_trade_history(self.apis, days_back=days_back)
        if not fills:
            discord.print_log("BingX取引履歴が見つかりませんでした（新規口座、または最近の取引がない可能性があります）")
            return None
            
        df = pd.DataFrame(fills)
        combined = self.calculate_hyperliquid_pnl_from_df(df)
        return combined

    async def get_hyperliquid_trade_history(self, apis_hl, days_back=30):
        cred_key = 'hyperliquid_testnet' if self.mode in ('paper', 'demo', 'testnet') else 'hyperliquid'
        creds = apis_hl.get(cred_key) or apis_hl.get('hyperliquid_demo') or {}
        if isinstance(creds, list):
            creds = {}
        account_address = creds.get("account_address", "")
        if not account_address or account_address.startswith("0x000"):
            return []

        base_url = "https://api.hyperliquid-testnet.xyz" if self.mode in ('paper', 'demo') else "https://api.hyperliquid.xyz"

        try:
            resp = requests.post(
                f"{base_url}/info",
                json={"type": "userFills", "user": account_address},
                timeout=15
            )
            data = resp.json()
            if isinstance(data, list):
                return data
        except Exception as e:
            discord.print_log(f"BingX trade history fetch error: {e}")
        return []


    def calculate_hyperliquid_pnl_from_df(self, df):
        if df.empty:
            return None
        df = df.copy()
        if 'time' in df.columns:
            df['execTime'] = pd.to_datetime(pd.to_numeric(df['time']), unit='ms')
        else:
            return None
        df = df.sort_values('execTime')
        
        if 'sz' in df.columns:
            df['size'] = df['sz']
        if 'px' in df.columns:
            df['price'] = df['px']
        if 'fee' not in df.columns:
            df['fee'] = 0.0

        for col in ('price', 'size', 'fee', 'closedPnl'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
                
        df['realized_pnl'] = 0.0
        df['cumulative_pnl'] = 0.0
        
        if 'closedPnl' in df.columns:
            df['realized_pnl'] = df['closedPnl'] - df['fee']
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

    def __init__(self, air_exchange, df, size, columns, side_mode="long", atr_period=10, atr_tp_multi=3, strategy_type="range", sl_margin_pct=1.0):
        super().__init__(air_exchange=air_exchange, df=df, columns=columns)
        self.size = size
        self.side_mode = side_mode
        self.atr_period = atr_period
        self.atr_tp_multi = atr_tp_multi
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
        high = self.array[self.column_dic["high"]] if "high" in self.column_dic else close
        low = self.array[self.column_dic["low"]] if "low" in self.column_dic else close
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
                    self.risk_r = max(self.current_sl - self.entry_price, self.entry_price * 0.005)
                    self.tp1 = self.entry_price - self.risk_r * 1.0
                    self.tp2 = self.entry_price - self.risk_r * 2.0
                    self.lock_sl = self.entry_price - self.risk_r * 1.2
                    self.bt_step = 0

                be_price = self.entry_price * (1.0 - 0.0005)
                strat_type = str(getattr(self, 'strategy_type', 'range')).lower()

                if strat_type == 'breakout':
                    # 実運用(VPTrailingManager)と完全一致: 段階的リスクリワード・トレイリング
                    if low <= getattr(self, 'tp2', -float('inf')) or getattr(self, 'bt_step', 0) >= 2:
                        self.bt_step = max(getattr(self, 'bt_step', 0), 2)
                        self.current_sl = min(getattr(self, 'current_sl', self.lock_sl), self.lock_sl, be_price)
                        trail_candidate = min(vah, poc)
                        if trail_candidate < self.current_sl and trail_candidate > close:
                            self.bt_step = 3
                            self.current_sl = trail_candidate
                    elif low <= getattr(self, 'tp1', -float('inf')) or getattr(self, 'bt_step', 0) >= 1:
                        self.bt_step = max(getattr(self, 'bt_step', 0), 1)
                        self.current_sl = min(getattr(self, 'current_sl', be_price), be_price)
                    else:
                        pass
                else:
                    is_profitable = close <= be_price
                    if is_profitable:
                        if close < val:
                            self.crossed_val = True
                        base_line = val if getattr(self, 'crossed_val', False) else poc
                        self.current_sl = min(getattr(self, 'current_sl', be_price), be_price, base_line)

                if close > getattr(self, 'current_sl', float('inf')):
                    stshortclose = True
                    self._cancel_all_orders()
                    self._market_order(size=-position)
                    self.entry_price = None
                    self.initial_sl_line = None
                    self.current_sl = None
                    self.crossed_val = False
                    self.bt_step = 0
            else:
                self.entry_price = None
                self.initial_sl_line = None
                self.current_sl = None
                self.crossed_val = False
                self.bt_step = 0

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
                        self.risk_r = max(self.current_sl - self.entry_price, self.entry_price * 0.005)
                        self.tp1 = self.entry_price - self.risk_r * 1.0
                        self.tp2 = self.entry_price - self.risk_r * 2.0
                        self.lock_sl = self.entry_price - self.risk_r * 1.2
                        self.crossed_val = False
                        self.bt_step = 0
        else:
            if position > 0:
                if not hasattr(self, 'entry_price') or self.entry_price is None:
                    self.entry_price = close
                    self.initial_sl_line = val
                    self.crossed_vah = False
                    margin_pct = getattr(self, 'sl_margin_pct', 1.0)
                    self.current_sl = val * (1.0 - margin_pct / 100.0)
                    self.risk_r = max(self.entry_price - self.current_sl, self.entry_price * 0.005)
                    self.tp1 = self.entry_price + self.risk_r * 1.0
                    self.tp2 = self.entry_price + self.risk_r * 2.0
                    self.lock_sl = self.entry_price + self.risk_r * 1.2
                    self.bt_step = 0

                be_price = self.entry_price * (1.0 + 0.0005)
                strat_type = str(getattr(self, 'strategy_type', 'range')).lower()

                if strat_type == 'breakout':
                    # 実運用(VPTrailingManager)と完全一致: 段階的リスクリワード・トレイリング
                    if high >= getattr(self, 'tp2', float('inf')) or getattr(self, 'bt_step', 0) >= 2:
                        self.bt_step = max(getattr(self, 'bt_step', 0), 2)
                        self.current_sl = max(getattr(self, 'current_sl', self.lock_sl), self.lock_sl, be_price)
                        trail_candidate = max(val, poc)
                        if trail_candidate > self.current_sl and trail_candidate < close:
                            self.bt_step = 3
                            self.current_sl = trail_candidate
                    elif high >= getattr(self, 'tp1', float('inf')) or getattr(self, 'bt_step', 0) >= 1:
                        self.bt_step = max(getattr(self, 'bt_step', 0), 1)
                        self.current_sl = max(getattr(self, 'current_sl', be_price), be_price)
                    else:
                        pass
                else:
                    is_profitable = close >= be_price
                    if is_profitable:
                        if close > vah:
                            self.crossed_vah = True
                        base_line = vah if getattr(self, 'crossed_vah', False) else poc
                        self.current_sl = max(getattr(self, 'current_sl', be_price), be_price, base_line)

                if close < getattr(self, 'current_sl', -float('inf')):
                    stlongclose = True
                    self._cancel_all_orders()
                    self._market_order(size=-position)
                    self.entry_price = None
                    self.initial_sl_line = None
                    self.current_sl = None
                    self.crossed_vah = False
                    self.bt_step = 0
            else:
                self.entry_price = None
                self.initial_sl_line = None
                self.current_sl = None
                self.crossed_vah = False
                self.bt_step = 0

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
                        self.risk_r = max(self.entry_price - self.current_sl, self.entry_price * 0.005)
                        self.tp1 = self.entry_price + self.risk_r * 1.0
                        self.tp2 = self.entry_price + self.risk_r * 2.0
                        self.lock_sl = self.entry_price + self.risk_r * 1.2
                        self.crossed_vah = False
                        self.bt_step = 0

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
    def run_backtest(self, df, lot, data_equity, side_mode="long", mp_period=120, atr_tp_multi=3, bybit_exec_history=None, bybit_lot_size=None, strategy_type="range", sl_margin_pct=1.0):
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
                columns=["open", "high", "low", "atr_10", "POC", "VAH", "VAL", "long", "short", "lowest_support", "close"],
                side_mode=side_mode,
                atr_period=10,
                atr_tp_multi=atr_tp_multi,
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
        taker_fee = 0.0005

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
    if df is None or df.empty or interval_minutes <= 5:
        return df.copy() if df is not None else df
    
    df_copy = df.copy()
    df_copy['timestamp'] = pd.to_datetime(df_copy['timestamp'], utc=True)
    df_copy = df_copy.sort_values('timestamp').reset_index(drop=True)
    
    # 元データの時間間隔（分）を自動判定し、同間隔以下ならそのまま返す
    if len(df_copy) >= 2:
        try:
            base_diff_min = (df_copy['timestamp'].iloc[-1] - df_copy['timestamp'].iloc[-2]).total_seconds() / 60.0
            if interval_minutes <= base_diff_min + 0.5:
                return df_copy
        except Exception:
            pass
    
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
    for extra_col in ('openInterest', 'oi_chg_4h', 'fundingRate'):
        if extra_col in df_copy.columns:
            agg_dict[extra_col] = 'last'
    
    resampled = df_copy.resample(rule, closed='left', label='left').agg(agg_dict)
    resampled = resampled.dropna(subset=['open', 'close'])
    resampled = resampled.reset_index()
    
    return resampled


def build_entry_precision_filter(df_inv: pd.DataFrame, side: str = "short", is_vp: bool = True) -> np.ndarray:
    """
    Volume × OI × FR 3要素エントリー精度フィルター配列 (bool ndarray) を生成する。
    バックテストとリアルタイム判定で 100% 同一基準を適用する。
    """
    n = len(df_inv)
    if n == 0:
        return np.ones(0, dtype=bool)
    mask = np.ones(n, dtype=bool)
    side_lower = str(side).lower()

    # 1. Volume 条件 (直前20本平均出来高に対する倍率)
    if 'volume' in df_inv.columns:
        v = pd.to_numeric(df_inv['volume'], errors='coerce').fillna(0.0).values
        v_ma20 = pd.Series(v).rolling(20, min_periods=5).mean().shift(1).values
        with np.errstate(divide='ignore', invalid='ignore'):
            vol_r = np.where((v_ma20 > 0) & (~np.isnan(v_ma20)), v / v_ma20, 1.0)
        # 全戦略共通: 異常スパイク足 (> 2.5x) を遮断
        mask &= (vol_r <= 2.5)
        # VP系 (VAL_POC / VAH_POC): 極端な薄商いダマシ (< 0.6x) も遮断
        if is_vp:
            mask &= (vol_r >= 0.6)

    # 2. OI 4時間変化率条件 (oi_chg_4h, %)
    if 'oi_chg_4h' in df_inv.columns:
        oi_c = pd.to_numeric(df_inv['oi_chg_4h'], errors='coerce').fillna(0.0).values
        if side_lower == "short":
            mask &= (oi_c >= -3.0) & (oi_c <= 3.0)
        else:
            mask &= (oi_c >= -3.0) & (oi_c <= 5.0)

    # 3. ファンディングレート条件 (fundingRate: 8h decimal rate, e.g. 0.0001 = +0.01%)
    if 'fundingRate' in df_inv.columns:
        fr = pd.to_numeric(df_inv['fundingRate'], errors='coerce').fillna(0.0).values
        if side_lower == "short":
            # 強いマイナスFR (< -0.005%) での踏み上げショート遮断
            mask &= (fr >= -0.00005)
        else:
            # 過熱ロングFR (> +0.025%) や強い逆風 (< -0.005%) を遮断
            mask &= (fr >= -0.00005) & (fr <= 0.00025)

    return mask


def evaluate_entry_precision_gate(df_eval: pd.DataFrame, side: str = "short", is_vp: bool = True) -> Tuple[bool, str, Dict[str, float]]:
    """
    最新確定足 (iloc[-1]) に対して Volume × OI × FR 3要素エントリー精度フィルターを判定する。
    """
    info: Dict[str, float] = {"vol_ratio": 1.0, "oi_chg_4h": 0.0, "funding_rate_pct": 0.0}
    if df_eval is None or df_eval.empty:
        return True, "OK", info

    side_lower = str(side).lower()

    # 1. Volume 判定
    if "volume" in df_eval.columns and len(df_eval) >= 6:
        v_ser = pd.to_numeric(df_eval["volume"], errors="coerce").fillna(0.0)
        curr_v = float(v_ser.iloc[-1])
        prev_v_ma = float(v_ser.iloc[:-1].tail(20).mean())
        vol_r = (curr_v / prev_v_ma) if prev_v_ma > 0 else 1.0
        info["vol_ratio"] = round(vol_r, 2)
        if vol_r > 2.5:
            return False, f"VOL_SPIKE_BLOCK(Vol={vol_r:.2f}x > 2.5x)", info
        if is_vp and vol_r < 0.6:
            return False, f"VOL_THIN_BLOCK(Vol={vol_r:.2f}x < 0.6x)", info

    # 2. OI 4h変化率判定
    if "oi_chg_4h" in df_eval.columns:
        val_oi = pd.to_numeric(df_eval["oi_chg_4h"], errors="coerce").iloc[-1]
        if pd.notna(val_oi):
            oi_c = float(val_oi)
            info["oi_chg_4h"] = round(oi_c, 2)
            if side_lower == "short" and not (-3.0 <= oi_c <= 3.0):
                return False, f"OI_SURGE_BLOCK(dOI_4h={oi_c:+.2f}% outside [-3%,+3%])", info
            if side_lower != "short" and not (-3.0 <= oi_c <= 5.0):
                return False, f"OI_ANOMALY_BLOCK(dOI_4h={oi_c:+.2f}% outside [-3%,+5%])", info

    # 3. Funding Rate 判定
    if "fundingRate" in df_eval.columns:
        val_fr = pd.to_numeric(df_eval["fundingRate"], errors="coerce").iloc[-1]
        if pd.notna(val_fr):
            fr = float(val_fr)
            fr_pct = fr * 100.0
            info["funding_rate_pct"] = round(fr_pct, 4)
            if side_lower == "short" and fr < -0.00005:
                return False, f"FR_SQUEEZE_BLOCK(FR={fr_pct:+.4f}% < -0.005%)", info
            if side_lower != "short" and not (-0.00005 <= fr <= 0.00025):
                return False, f"FR_CROWD_BLOCK(FR={fr_pct:+.4f}% outside [-0.005%,+0.025%])", info

    tag = f"Vol={info['vol_ratio']:.2f}x, dOI4h={info['oi_chg_4h']:+.2f}%, FR={info['funding_rate_pct']:+.4f}%"
    return True, tag, info


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



def make_market_profile_dual_va(
    df: pd.DataFrame,
    period: int = 100,
    num_bins: int = 100,
    step: int = 1,
    use_decay: bool = True,
    decay_half_life: Optional[float] = None
) -> Dict[str, np.ndarray]:
    """1パスの出来高ビン集計で VA 70% と VA 80% の両方の VAL/VAH を一挙算出する関数 (指数減衰・step間引き対応)"""
    n = len(df)
    min_required = min(max(10, period // 2), period)
    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values if 'open' in df.columns else closes
    volumes = df['volume'].astype(float).values if 'volume' in df.columns else np.ones(n)
    if np.all(volumes <= 0):
        volumes = np.ones(n)

    half_life = float(decay_half_life) if decay_half_life is not None else max(5.0, period / 2.0)
    decay_factor = 0.5 ** (1.0 / half_life) if use_decay else 1.0
    full_weights = (decay_factor ** np.arange(period - 1, -1, -1)) if use_decay else np.ones(period)

    val_70 = np.full(n, np.nan)
    vah_70 = np.full(n, np.nan)
    val_80 = np.full(n, np.nan)
    vah_80 = np.full(n, np.nan)
    poc_arr = np.full(n, np.nan)

    last_poc = np.nan
    last_v70 = np.nan
    last_vh70 = np.nan
    last_v80 = np.nan
    last_vh80 = np.nan
    calc_count = 0

    for i in range(min_required, n):
        # step間引き: 直前計算値のフォワードフィル (計算量を1/stepに削減)
        if step > 1 and (i - min_required) % step != 0 and not np.isnan(last_poc):
            poc_arr[i] = last_poc
            val_70[i] = last_v70
            vah_70[i] = last_vh70
            val_80[i] = last_v80
            vah_80[i] = last_vh80
            continue

        calc_count += 1
        # CPU負荷を20〜30%以下に抑えるためのジェントルスリープ
        if calc_count % 40 == 0:
            time.sleep(0.015)

        start_idx = max(0, i - period + 1)
        win_len = i - start_idx + 1
        w_highs = highs[start_idx:i+1]
        w_lows = lows[start_idx:i+1]
        w_closes = closes[start_idx:i+1]
        w_opens = opens[start_idx:i+1]
        w_volumes = volumes[start_idx:i+1] * full_weights[-win_len:]

        if len(w_highs) == 0 or len(w_lows) == 0:
            curr_c = closes[i] if i < len(closes) else 0.0
            last_poc = last_v70 = last_vh70 = last_v80 = last_vh80 = curr_c
            poc_arr[i] = val_70[i] = vah_70[i] = val_80[i] = vah_80[i] = curr_c
            continue

        p_max = w_highs.max()
        p_min = w_lows.min()
        curr_c = closes[i]

        if p_max <= p_min or np.isnan(p_max) or np.isnan(p_min):
            last_poc = last_v70 = last_vh70 = last_v80 = last_vh80 = curr_c
            poc_arr[i] = val_70[i] = vah_70[i] = val_80[i] = vah_80[i] = curr_c
            continue

        price_bins = np.linspace(p_min, p_max, num_bins + 1)
        bin_centers = (price_bins[:-1] + price_bins[1:]) / 2
        bin_lows_2d = price_bins[:-1][np.newaxis, :]
        bin_highs_2d = price_bins[1:][np.newaxis, :]

        body_lows = np.minimum(w_opens, w_closes)
        body_highs = np.maximum(w_opens, w_closes)
        w_h_2d = w_highs[:, np.newaxis]
        w_l_2d = w_lows[:, np.newaxis]
        w_bh_2d = body_highs[:, np.newaxis]
        w_bl_2d = body_lows[:, np.newaxis]
        w_v_2d = w_volumes[:, np.newaxis]

        full_overlap = np.maximum(0.0, np.minimum(w_h_2d, bin_highs_2d) - np.maximum(w_l_2d, bin_lows_2d))
        full_range = np.maximum(w_h_2d - w_l_2d, 1e-12)
        full_ratio = full_overlap / full_range

        body_overlap = np.maximum(0.0, np.minimum(w_bh_2d, bin_highs_2d) - np.maximum(w_bl_2d, bin_lows_2d))
        body_range = np.maximum(w_bh_2d - w_bl_2d, 1e-12)
        body_ratio = body_overlap / body_range

        has_body_2d = ((body_highs - body_lows) > 1e-12)[:, np.newaxis]
        eff_ratio = np.where(has_body_2d, 0.60 * body_ratio + 0.40 * full_ratio, full_ratio)
        vol_profile = (w_v_2d * eff_ratio).sum(axis=0)

        total_vol = vol_profile.sum()
        if total_vol <= 0:
            last_poc = last_v70 = last_vh70 = last_v80 = last_vh80 = curr_c
            poc_arr[i] = val_70[i] = vah_70[i] = val_80[i] = vah_80[i] = curr_c
            continue

        poc_idx = int(np.argmax(vol_profile))
        idx_start = max(0, poc_idx - 1)
        idx_end = min(num_bins, poc_idx + 2)
        local_vols = vol_profile[idx_start:idx_end]
        local_centers = bin_centers[idx_start:idx_end]
        local_sum = local_vols.sum()
        last_poc = (local_vols * local_centers).sum() / local_sum if local_sum > 0 else bin_centers[poc_idx]
        poc_arr[i] = last_poc

        target_vol_70 = total_vol * 0.70
        target_vol_80 = total_vol * 0.80
        curr_va_vol = vol_profile[poc_idx]
        upper_idx = poc_idx
        lower_idx = poc_idx
        found_70 = False

        while upper_idx < num_bins - 1 or lower_idx > 0:
            if not found_70 and curr_va_vol >= target_vol_70:
                last_v70 = price_bins[lower_idx]
                last_vh70 = price_bins[upper_idx + 1]
                found_70 = True
            if curr_va_vol >= target_vol_80:
                break

            can_up = upper_idx < num_bins - 1
            can_down = lower_idx > 0
            up_vol = vol_profile[upper_idx + 1] if can_up else -1.0
            down_vol = vol_profile[lower_idx - 1] if can_down else -1.0

            if can_up and can_down:
                if up_vol > down_vol:
                    upper_idx += 1
                    curr_va_vol += vol_profile[upper_idx]
                elif down_vol > up_vol:
                    lower_idx -= 1
                    curr_va_vol += vol_profile[lower_idx]
                else:
                    upper_idx += 1
                    curr_va_vol += vol_profile[upper_idx]
                    if curr_va_vol < target_vol_80 and lower_idx > 0:
                        lower_idx -= 1
                        curr_va_vol += vol_profile[lower_idx]
            elif can_up:
                upper_idx += 1
                curr_va_vol += vol_profile[upper_idx]
            elif can_down:
                lower_idx -= 1
                curr_va_vol += vol_profile[lower_idx]

        if not found_70:
            last_v70 = price_bins[lower_idx]
            last_vh70 = price_bins[upper_idx + 1]
        last_v80 = price_bins[lower_idx]
        last_vh80 = price_bins[upper_idx + 1]

        val_70[i] = last_v70
        vah_70[i] = last_vh70
        val_80[i] = last_v80
        vah_80[i] = last_vh80

    return {
        'poc': poc_arr,
        'val_70': val_70, 'vah_70': vah_70,
        'val_80': val_80, 'vah_80': vah_80
    }


def simulate_val_poc_fast(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    vals: np.ndarray,
    vahs: np.ndarray,
    pocs: np.ndarray,
    ma_filter: Optional[np.ndarray] = None,
    initial_equity: float = 100.0,
    max_trades: int = 3,
    nanpin_drop_pct: float = 0.030,
    sl_pct: float = 0.025,
    target_ratio: float = 0.70,
    cooldown_bars: int = 96,
    timeout_bars: int = 48,
    fee_rate: float = 0.0006,
    maker_fee: float = 0.0002,
    record_chart: bool = False
) -> Dict[str, Any]:
    """VAL_POC 高速バックテストシミュレータ (動的ストップ・4時間タイムアウト・最大3枠対応)"""
    n = len(closes)
    half_idx = n // 2
    trade_size = initial_equity / max_trades
    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    last_entry = 0.0
    entry_bar_idx = -1
    cooldown_until = -1
    pending_limit = -1.0
    cum_pnl = 0.0
    pnl_first_half = 0.0
    pnl_second_half = 0.0
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    pnl_recent_7d = 0.0
    trades_recent_7d = 0
    trades = []
    peak_eq = initial_equity
    max_dd = 0.0

    if record_chart:
        exec_buy = np.full(n, np.nan)
        exec_sell = np.full(n, np.nan)
        pnl_curve = np.full(n, initial_equity, dtype=float)
    else:
        exec_buy = None
        exec_sell = None
        pnl_curve = None

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        vah = vahs[i]

        # 0. 指値約定チェック
        if pending_limit > 0:
            if l <= pending_limit:
                px = pending_limit
                qty = trade_size / px
                fee = trade_size * maker_fee
                pos_cost += trade_size + fee
                pos_qty += qty
                avg_price = pos_cost / pos_qty
                last_entry = px
                pos_count += 1
                if pos_count == 1:
                    entry_bar_idx = i
                if record_chart and exec_buy is not None:
                    exec_buy[i] = px
            pending_limit = -1.0

        # 1. 決済チェック
        if pos_qty > 0:
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * target_ratio if va_diff > 0 else (last_entry * nanpin_drop_pct)
            drop_dist = max(last_entry * 0.010, va_spacing) if last_entry > 0 else 0.0

            # 損切り: 動的ストップ (sl_pct) または 4時間タイムアウト (timeout_bars本経過で含み損)
            is_sl = False
            if sl_pct > 0 and c <= avg_price * (1.0 - sl_pct):
                is_sl = True
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_price:
                is_sl = True

            if is_sl:
                sell_val = pos_qty * c
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                if i < half_idx:
                    pnl_first_half += pnl
                else:
                    pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl
                    trades_recent_7d += 1
                trades.append(pnl)
                if record_chart and exec_sell is not None:
                    exec_sell[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry = 0.0
                entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                # 利確 (VAL-VAH 70%水準 到達かつ建値+0.40%以上利益)
                target_tp = val + va_spacing if va_spacing > 0 else pocs[i]
                if (h >= target_tp or c >= target_tp) and target_tp > 0:
                    exit_px = max(c, target_tp) if h >= target_tp else c
                    if exit_px >= avg_price * 1.0040:
                        sell_val = pos_qty * exit_px
                        fee = sell_val * fee_rate
                        pnl = sell_val - pos_cost - fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades.append(pnl)
                        if record_chart and exec_sell is not None:
                            exec_sell[i] = exit_px
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_price = 0.0
                        pos_count = 0
                        last_entry = 0.0
                        entry_bar_idx = -1

        # 2. エントリー / ナンピン判定
        if pending_limit < 0:
            if pos_count == 0 and i >= cooldown_until:
                if not np.isnan(val) and not np.isnan(vals[i-1]):
                    val_gc = (closes[i-1] <= vals[i-1]) and (c > val)
                    va_width = (vah - val) / val if val > 0 else 0.0
                    if val_gc and (va_width >= 0.015) and (ma_filter is None or ma_filter[i]):
                        pending_limit = c * 0.9998
            elif 1 <= pos_count < max_trades:
                va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                va_spacing = va_diff * target_ratio if va_diff > 0 else (last_entry * nanpin_drop_pct)
                drop_dist = max(last_entry * 0.010, va_spacing) if last_entry > 0 else 0.0
                drop_target = last_entry - drop_dist
                if c <= drop_target or l <= drop_target:
                    pending_limit = c * 0.9998

        if pos_qty > 0:
            unrealized = pos_qty * c - pos_cost
            cur_eq = initial_equity + cum_pnl + unrealized
            if cur_eq > peak_eq:
                peak_eq = cur_eq
            cur_dd = peak_eq - cur_eq
            if cur_dd > max_dd:
                max_dd = cur_dd
        else:
            cur_eq = initial_equity + cum_pnl
            if cur_eq > peak_eq:
                peak_eq = cur_eq
        if record_chart and pnl_curve is not None:
            pnl_curve[i] = initial_equity + cum_pnl

    trade_cnt = len(trades)
    win_cnt = sum(1 for t in trades if t > 0)
    win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
    res_dict: Dict[str, Any] = {
        'final_pnl': float(cum_pnl),
        'pnl_first_half': float(pnl_first_half),
        'pnl_second_half': float(pnl_second_half),
        'pnl_recent_7d': float(pnl_recent_7d),
        'trades_recent_7d': int(trades_recent_7d),
        'trade_count': trade_cnt,
        'win_rate': win_rate,
        'DD_max': float(max_dd)
    }
    if record_chart:
        res_dict['exec_buy_price'] = exec_buy
        res_dict['exec_sell_price'] = exec_sell
        res_dict['pnl_curve'] = pnl_curve
    return res_dict


def simulate_vah_poc_fast(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    vals: np.ndarray,
    vahs: np.ndarray,
    pocs: np.ndarray,
    ma_filter: Optional[np.ndarray] = None,
    initial_equity: float = 100.0,
    max_trades: int = 1,
    nanpin_drop_pct: float = 0.030,
    sl_pct: float = 0.025,
    target_ratio: float = 0.70,
    cooldown_bars: int = 96,
    timeout_bars: int = 48,
    fee_rate: float = 0.0006,
    maker_fee: float = 0.0002,
    record_chart: bool = False
) -> Dict[str, Any]:
    """VAH_POC 高速ショートバックテストシミュレータ (単発1本固定・動的ストップ・4時間タイムアウト)"""
    n = len(closes)
    half_idx = n // 2
    trade_size = initial_equity  # ショートはナンピンなし・単発100%フルサイズ固定
    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    entry_bar_idx = -1
    cooldown_until = -1
    pending_limit = -1.0
    cum_pnl = 0.0
    pnl_first_half = 0.0
    pnl_second_half = 0.0
    recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
    pnl_recent_7d = 0.0
    trades_recent_7d = 0
    trades = []
    peak_eq = initial_equity
    max_dd = 0.0

    if record_chart:
        exec_sell = np.full(n, np.nan)
        exec_buy = np.full(n, np.nan)
        pnl_curve = np.full(n, initial_equity, dtype=float)
    else:
        exec_sell = None
        exec_buy = None
        pnl_curve = None

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        vah = vahs[i]

        # 0. ショート指値約定チェック (高値が指値以上で売り約定)
        if pending_limit > 0:
            if h >= pending_limit:
                px = pending_limit
                qty = trade_size / px
                fee = trade_size * maker_fee
                pos_cost = trade_size
                pos_qty = qty
                avg_price = px
                pos_count = 1
                entry_bar_idx = i
                if record_chart and exec_sell is not None:
                    exec_sell[i] = px
            pending_limit = -1.0

        # 1. ショート決済チェック
        if pos_qty > 0:
            va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
            va_spacing = va_diff * target_ratio if va_diff > 0 else (avg_price * nanpin_drop_pct)

            # 損切り: 動的ストップ (sl_pct 逆行上昇) または 4時間タイムアウト (timeout_bars本経過で含み損=価格上昇)
            is_sl = False
            if sl_pct > 0 and c >= avg_price * (1.0 + sl_pct):
                is_sl = True
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_price:
                is_sl = True

            if is_sl:
                buy_val = pos_qty * c
                fee = buy_val * fee_rate
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                if i < half_idx:
                    pnl_first_half += pnl
                else:
                    pnl_second_half += pnl
                if i >= recent_7d_idx:
                    pnl_recent_7d += pnl
                    trades_recent_7d += 1
                trades.append(pnl)
                if record_chart and exec_buy is not None:
                    exec_buy[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            else:
                # ショート利確 (VAH - (VAH-VAL)*70%水準 到達かつ建値-0.40%以下利益)
                target_tp = vah - va_spacing if va_spacing > 0 else pocs[i]
                if (l <= target_tp or c <= target_tp) and target_tp > 0:
                    exit_px = min(c, target_tp) if l <= target_tp else c
                    if exit_px <= avg_price * 0.9960:
                        buy_val = pos_qty * exit_px
                        fee = buy_val * fee_rate
                        entry_fee = pos_cost * maker_fee
                        pnl = pos_cost - buy_val - fee - entry_fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades.append(pnl)
                        if record_chart and exec_buy is not None:
                            exec_buy[i] = exit_px
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_price = 0.0
                        pos_count = 0
                        entry_bar_idx = -1

        # 2. ショート新規エントリー判定 (単発1本固定・ナンピンなし)
        if pending_limit < 0:
            if pos_count == 0 and i >= cooldown_until:
                if not np.isnan(vah) and not np.isnan(vahs[i-1]):
                    vah_dc = (closes[i-1] >= vahs[i-1]) and (c < vah)  # VAH下抜け
                    va_width = (vah - val) / val if val > 0 else 0.0
                    if vah_dc and (va_width >= 0.015) and (ma_filter is None or ma_filter[i]):
                        pending_limit = c * 1.0002  # Best Ask 相当の指値待機

        if pos_qty > 0:
            unrealized = pos_cost - pos_qty * c
            cur_eq = initial_equity + cum_pnl + unrealized
            if cur_eq > peak_eq:
                peak_eq = cur_eq
            cur_dd = peak_eq - cur_eq
            if cur_dd > max_dd:
                max_dd = cur_dd
        else:
            cur_eq = initial_equity + cum_pnl
            if cur_eq > peak_eq:
                peak_eq = cur_eq
        if record_chart and pnl_curve is not None:
            pnl_curve[i] = initial_equity + cum_pnl

    trade_cnt = len(trades)
    win_cnt = sum(1 for t in trades if t > 0)
    win_rate = (win_cnt / trade_cnt * 100.0) if trade_cnt > 0 else 0.0
    res_dict: Dict[str, Any] = {
        'final_pnl': float(cum_pnl),
        'pnl_first_half': float(pnl_first_half),
        'pnl_second_half': float(pnl_second_half),
        'pnl_recent_7d': float(pnl_recent_7d),
        'trades_recent_7d': int(trades_recent_7d),
        'trade_count': trade_cnt,
        'win_rate': win_rate,
        'DD_max': float(max_dd)
    }
    if record_chart:
        res_dict['exec_buy_price'] = exec_buy
        res_dict['exec_sell_price'] = exec_sell
        res_dict['pnl_curve'] = pnl_curve
    return res_dict


def _touch_bot_heartbeat():
    """最適化ループ中も死活監視 (check_and_restart.sh) がハングアップと誤認しないようハートビートを更新"""
    try:
        hb_path = os.path.join("Data", "bot_heartbeat.json")
        if os.path.exists(hb_path):
            os.utime(hb_path, None)
    except Exception:
        pass


def optimize_rsima3_fast(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    rsi: np.ndarray,
    rsi_ma: np.ndarray,
    initial_equity: float = 100.0,
    max_trades: int = 3,
    min_trades: int = 10,
    val_arr: Optional[np.ndarray] = None,
    vah_arr: Optional[np.ndarray] = None,
    default_sl_pct: float = 0.025,
    ma_filter: Optional[np.ndarray] = None,
    timeout_bars: int = 48,
    cooldown_bars: int = 288
) -> Dict[str, Any]:
    """RSIMA 9通り (EP 30,35,40 × CP 60,65,70, 動的ストップ・4時間タイムアウト) 超高速最適化 (MIN_TRADES=10)"""
    n = len(closes)
    half_idx = n // 2
    trade_size = initial_equity / max_trades
    maker_fee = 0.0002
    taker_fee = 0.0006

    eps = [30.0, 35.0, 40.0]
    cps = [60.0, 65.0, 70.0]

    # ゴールデンクロス判定を事前ベクトル計算してフラット時の無駄なループ処理をスキップ
    gc_arr = np.zeros(n, dtype=bool)
    if n > 2:
        gc_arr[1:] = (rsi[1:] > rsi_ma[1:]) & (rsi[:-1] <= rsi_ma[:-1])

    best_pnl = -float('inf')
    best_res: Optional[Dict[str, Any]] = None
    fallback_pnl = -float('inf')
    fallback_res: Dict[str, Any] = {
        'l_ep': 40.0, 'l_cp': 60.0, 'sl_pct': default_sl_pct,
        'final_pnl': 0.0, 'pnl_first_half': 0.0, 'pnl_second_half': 0.0,
        'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    use_va = (val_arr is not None and vah_arr is not None and len(val_arr) == n)

    for ep in eps:
        ep_sig = gc_arr.copy()
        if n > 2:
            ep_sig[1:] = gc_arr[1:] & ((rsi_ma[1:] < ep) | (rsi_ma[:-1] < ep))
        if ma_filter is not None and len(ma_filter) == n:
            ep_sig = ep_sig & ma_filter
        ep_indices = np.flatnonzero(ep_sig[2:]) + 2
        n_ep = len(ep_indices)

        for cp in cps:
            pos_qty = 0.0
            pos_cost = 0.0
            avg_px = 0.0
            last_entry_px = 0.0
            pos_cnt = 0
            entry_bar_idx = -1
            cum_pnl = 0.0
            pnl_first_half = 0.0
            pnl_second_half = 0.0
            recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
            pnl_recent_7d = 0.0
            trades_recent_7d = 0
            trades_cnt = 0
            wins_cnt = 0
            pending_px = -1.0
            cooldown_until = -1
            peak_eq = initial_equity
            max_dd = 0.0

            i = 2
            ep_ptr = 0
            while i < n:
                if pos_qty == 0.0 and pending_px < 0.0:
                    target_i = i if i >= cooldown_until else cooldown_until
                    while ep_ptr < n_ep and ep_indices[ep_ptr] < target_i:
                        ep_ptr += 1
                    if ep_ptr >= n_ep:
                        break
                    i = int(ep_indices[ep_ptr])
                    pending_px = closes[i] * 0.9998
                    i += 1
                    continue

                c = closes[i]
                l = lows[i]

                if pending_px > 0:
                    if l <= pending_px:
                        px = pending_px
                        qty = trade_size / px
                        pos_cost += trade_size + trade_size * maker_fee
                        pos_qty += qty
                        last_entry_px = px
                        avg_px = pos_cost / pos_qty
                        pos_cnt += 1
                        if pos_cnt == 1:
                            entry_bar_idx = i
                    pending_px = -1.0

                if pos_qty > 0:
                    # 損切り判定: 動的ストップ率 default_sl_pct 割れ (枠数不問発動) または 4時間タイムアウト
                    is_sl = False
                    if default_sl_pct > 0 and c <= avg_px * (1.0 - default_sl_pct):
                        is_sl = True
                    elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_px:
                        is_sl = True

                    if is_sl:
                        sell_val = pos_qty * c
                        fee = sell_val * taker_fee
                        pnl = sell_val - pos_cost - fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades_cnt += 1
                        if pnl > 0:
                            wins_cnt += 1
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_px = 0.0
                        last_entry_px = 0.0
                        pos_cnt = 0
                        entry_bar_idx = -1
                        cooldown_until = i + cooldown_bars
                        cur_eq = initial_equity + cum_pnl
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                        cur_dd = peak_eq - cur_eq
                        if cur_dd > max_dd:
                            max_dd = cur_dd
                    elif rsi[i] > cp and c >= avg_px * 1.004:
                        # 利確判定: RSI > cp かつ 平均建値以上プラス
                        sell_val = pos_qty * c
                        fee = sell_val * taker_fee
                        pnl = sell_val - pos_cost - fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades_cnt += 1
                        if pnl > 0:
                            wins_cnt += 1
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_px = 0.0
                        last_entry_px = 0.0
                        pos_cnt = 0
                        entry_bar_idx = -1
                        cur_eq = initial_equity + cum_pnl
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                    else:
                        # ナンピン判定: 直近約定価格から (VAH - VAL) * 0.50 下落 (最大3枠)
                        if pending_px < 0 and 1 <= pos_cnt < max_trades:
                            if use_va and not np.isnan(val_arr[i]) and not np.isnan(vah_arr[i]) and vah_arr[i] > val_arr[i]:
                                dip_req = (vah_arr[i] - val_arr[i]) * 0.50
                                is_dip = (c <= last_entry_px - dip_req)
                            else:
                                is_dip = (c < avg_px * 0.994)
                            if ep_sig[i] and is_dip:
                                pending_px = c * 0.9998
                        cur_eq = initial_equity + cum_pnl + (pos_qty * c - pos_cost)
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                        cur_dd = peak_eq - cur_eq
                        if cur_dd > max_dd:
                            max_dd = cur_dd
                elif pending_px < 0 and ep_sig[i] and i >= cooldown_until:
                    pending_px = c * 0.9998

                i += 1

            win_rt = (wins_cnt / trades_cnt * 100.0) if trades_cnt > 0 else 0.0
            cand_entry = {
                'l_ep': float(ep),
                'l_cp': float(cp),
                'sl_pct': float(default_sl_pct),
                'final_pnl': float(cum_pnl),
                'pnl_first_half': float(pnl_first_half),
                'pnl_second_half': float(pnl_second_half),
                'pnl_recent_7d': float(pnl_recent_7d),
                'trades_recent_7d': int(trades_recent_7d),
                'trade_count': int(trades_cnt),
                'win_rate': float(win_rt),
                'DD_max': float(max_dd)
            }
            # 三段階頑健性 (Tier 1: 直近7日黒字 & 取引発生を最優先)
            is_tier1 = (trades_cnt >= min_trades and cum_pnl > 0.0 and pnl_recent_7d > 0.0 and trades_recent_7d >= 1)
            is_tier2 = (trades_cnt >= min_trades and cum_pnl > 0.0 and pnl_first_half > 0.0 and pnl_second_half > pnl_first_half)
            
            # スコアリング: Tier 1最優先(1000点ゲタ+直近7d重視), Tier 2(500点ゲタ+後半重視), Tier 3(総合PnL)
            if is_tier1:
                score = 1000.0 + pnl_recent_7d * 2.0 + pnl_second_half + cum_pnl * 0.1
            elif is_tier2:
                score = 500.0 + pnl_second_half + cum_pnl * 0.1
            elif trades_cnt >= 5 and cum_pnl > 0.0:
                score = cum_pnl
            else:
                score = -1000.0 + cum_pnl

            if score > best_pnl:
                best_pnl = score
                best_res = cand_entry
            if trades_cnt >= 5 and cum_pnl > fallback_pnl:
                fallback_pnl = cum_pnl
                fallback_res = cand_entry

    return best_res if best_res is not None else fallback_res


def optimize_rsima3_short_fast(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    rsi: np.ndarray,
    rsi_ma: np.ndarray,
    initial_equity: float = 100.0,
    max_trades: int = 1,
    min_trades: int = 10,
    val_arr: Optional[np.ndarray] = None,
    vah_arr: Optional[np.ndarray] = None,
    default_sl_pct: float = 0.025,
    ma_filter: Optional[np.ndarray] = None,
    timeout_bars: int = 48,
    cooldown_bars: int = 288
) -> Dict[str, Any]:
    """RSIMA ショート 9通り (EP 60,65,70 × CP 30,35,40, 単発1本固定・動的ストップ・4時間タイムアウト) 高速最適化"""
    n = len(closes)
    half_idx = n // 2
    trade_size = initial_equity  # ショートはナンピンなし・単発100%フルサイズ固定
    maker_fee = 0.0002
    taker_fee = 0.0006

    eps = [60.0, 65.0, 70.0]
    cps = [30.0, 35.0, 40.0]

    # デッドクロス判定を事前ベクトル計算 (RSI-EMA下向き転換＝戻り天井確認を必須化)
    dc_arr = np.zeros(n, dtype=bool)
    if n > 2:
        dc_arr[1:] = (rsi[1:] < rsi_ma[1:]) & (rsi[:-1] >= rsi_ma[:-1]) & (rsi_ma[1:] <= rsi_ma[:-1])

    best_pnl = -float('inf')
    best_res: Optional[Dict[str, Any]] = None
    fallback_pnl = -float('inf')
    fallback_res: Dict[str, Any] = {
        's_ep': 60.0, 's_cp': 40.0, 'sl_pct': default_sl_pct,
        'final_pnl': 0.0, 'pnl_first_half': 0.0, 'pnl_second_half': 0.0,
        'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    for ep in eps:
        ep_sig = dc_arr.copy()
        if n > 2:
            ep_sig[1:] = dc_arr[1:] & ((rsi_ma[1:] > ep) | (rsi_ma[:-1] > ep))
        if ma_filter is not None and len(ma_filter) == n:
            ep_sig = ep_sig & ma_filter
        ep_indices = np.flatnonzero(ep_sig[2:]) + 2
        n_ep = len(ep_indices)

        for cp in cps:
            pos_qty = 0.0
            pos_cost = 0.0
            avg_px = 0.0
            pos_cnt = 0
            entry_bar_idx = -1
            cum_pnl = 0.0
            pnl_first_half = 0.0
            pnl_second_half = 0.0
            recent_7d_idx = max(0, n - int(n * (7.0 / 30.0)))
            pnl_recent_7d = 0.0
            trades_recent_7d = 0
            trades_cnt = 0
            wins_cnt = 0
            pending_px = -1.0
            cooldown_until = -1
            peak_eq = initial_equity
            max_dd = 0.0

            i = 2
            ep_ptr = 0
            while i < n:
                if pos_qty == 0.0 and pending_px < 0.0:
                    target_i = i if i >= cooldown_until else cooldown_until
                    while ep_ptr < n_ep and ep_indices[ep_ptr] < target_i:
                        ep_ptr += 1
                    if ep_ptr >= n_ep:
                        break
                    i = int(ep_indices[ep_ptr])
                    pending_px = closes[i] * 1.0002  # Best Ask 指値
                    i += 1
                    continue

                c = closes[i]
                h = highs[i]

                # ショート指値約定チェック (高値が指値以上で売り約定)
                if pending_px > 0:
                    if h >= pending_px:
                        px = pending_px
                        qty = trade_size / px
                        pos_cost = trade_size
                        pos_qty = qty
                        avg_px = px
                        pos_cnt = 1
                        entry_bar_idx = i
                    pending_px = -1.0

                if pos_qty > 0:
                    # 損切り判定: 動的ストップ率 default_sl_pct 逆行上昇 または 4時間タイムアウト
                    is_sl = False
                    if default_sl_pct > 0 and c >= avg_px * (1.0 + default_sl_pct):
                        is_sl = True
                    elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_px:
                        is_sl = True

                    if is_sl:
                        buy_val = pos_qty * c
                        fee = buy_val * taker_fee
                        entry_fee = pos_cost * maker_fee
                        pnl = pos_cost - buy_val - fee - entry_fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades_cnt += 1
                        if pnl > 0:
                            wins_cnt += 1
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_px = 0.0
                        pos_cnt = 0
                        entry_bar_idx = -1
                        cooldown_until = i + cooldown_bars
                        cur_eq = initial_equity + cum_pnl
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                        cur_dd = peak_eq - cur_eq
                        if cur_dd > max_dd:
                            max_dd = cur_dd
                    elif rsi[i] < cp and c <= avg_px * 0.996:
                        # 利確判定: RSI < cp かつ 平均建値以下利益
                        buy_val = pos_qty * c
                        fee = buy_val * taker_fee
                        entry_fee = pos_cost * maker_fee
                        pnl = pos_cost - buy_val - fee - entry_fee
                        cum_pnl += pnl
                        if i < half_idx:
                            pnl_first_half += pnl
                        else:
                            pnl_second_half += pnl
                        if i >= recent_7d_idx:
                            pnl_recent_7d += pnl
                            trades_recent_7d += 1
                        trades_cnt += 1
                        if pnl > 0:
                            wins_cnt += 1
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_px = 0.0
                        pos_cnt = 0
                        entry_bar_idx = -1
                        cur_eq = initial_equity + cum_pnl
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                    else:
                        # ショートはナンピンなし (単発1本固定)
                        cur_eq = initial_equity + cum_pnl + (pos_cost - pos_qty * c)
                        if cur_eq > peak_eq:
                            peak_eq = cur_eq
                        cur_dd = peak_eq - cur_eq
                        if cur_dd > max_dd:
                            max_dd = cur_dd
                elif pending_px < 0 and ep_sig[i] and i >= cooldown_until:
                    pending_px = closes[i] * 1.0002

                i += 1

            win_rt = (wins_cnt / trades_cnt * 100.0) if trades_cnt > 0 else 0.0
            cand_entry = {
                's_ep': float(ep),
                's_cp': float(cp),
                'sl_pct': float(default_sl_pct),
                'final_pnl': float(cum_pnl),
                'pnl_first_half': float(pnl_first_half),
                'pnl_second_half': float(pnl_second_half),
                'pnl_recent_7d': float(pnl_recent_7d),
                'trades_recent_7d': int(trades_recent_7d),
                'trade_count': int(trades_cnt),
                'win_rate': float(win_rt),
                'DD_max': float(max_dd)
            }
            # 三段階頑健性 (Tier 1: 直近7日黒字 & 取引発生を最優先)
            is_tier1 = (trades_cnt >= min_trades and cum_pnl > 0.0 and pnl_recent_7d > 0.0 and trades_recent_7d >= 1)
            is_tier2 = (trades_cnt >= min_trades and cum_pnl > 0.0 and pnl_first_half > 0.0 and pnl_second_half > pnl_first_half)
            
            # スコアリング: Tier 1最優先(1000点ゲタ+直近7d重視), Tier 2(500点ゲタ+後半重視), Tier 3(総合PnL)
            if is_tier1:
                score = 1000.0 + pnl_recent_7d * 2.0 + pnl_second_half + cum_pnl * 0.1
            elif is_tier2:
                score = 500.0 + pnl_second_half + cum_pnl * 0.1
            elif trades_cnt >= 5 and cum_pnl > 0.0:
                score = cum_pnl
            else:
                score = -1000.0 + cum_pnl

            if score > best_pnl:
                best_pnl = score
                best_res = cand_entry
            if trades_cnt >= 5 and cum_pnl > fallback_pnl:
                fallback_pnl = cum_pnl
                fallback_res = cand_entry

    return best_res if best_res is not None else fallback_res


def build_rsima3_short_chart_df(
    df_5m: pd.DataFrame,
    rsi: np.ndarray,
    rsi_ma: np.ndarray,
    s_ep: float,
    s_cp: float,
    sl: float,
    initial_equity: float = 100.0,
    ma_filter: Optional[np.ndarray] = None,
    cooldown_bars: int = 288,
    timeout_bars: int = 48
) -> pd.DataFrame:
    """RSIMA3 ショート最良パラメータでの売買マーカー・累積損益曲線付きDataFrameを生成 (optimize_rsima3_short_fastと100%完全同期)"""
    out_df = df_5m.copy().reset_index(drop=True)
    n = len(out_df)
    closes = out_df['close'].astype(float).values
    highs = out_df['high'].astype(float).values if 'high' in out_df.columns else closes
    trade_size = initial_equity  # 単発100%フルサイズ
    maker_fee = 0.0002
    taker_fee = 0.0006

    exec_sell = np.full(n, np.nan)
    exec_buy = np.full(n, np.nan)
    pnl_curve = np.full(n, initial_equity, dtype=float)

    dc_arr = np.zeros(n, dtype=bool)
    if n > 2:
        dc_arr[1:] = (rsi[1:] < rsi_ma[1:]) & (rsi[:-1] >= rsi_ma[:-1]) & (rsi_ma[1:] <= rsi_ma[:-1])
    ep_sig = dc_arr.copy()
    if n > 2:
        ep_sig[1:] = dc_arr[1:] & ((rsi_ma[1:] > s_ep) | (rsi_ma[:-1] > s_ep))
    if ma_filter is not None and len(ma_filter) == n:
        ep_sig = ep_sig & ma_filter

    pos_qty = 0.0
    pos_cost = 0.0
    avg_px = 0.0
    pos_cnt = 0
    cum_pnl = 0.0
    pending_px = -1.0
    cooldown_until = -1
    entry_bar_idx = -1

    for i in range(2, n):
        c = closes[i]
        h = highs[i]

        if pending_px > 0:
            if h >= pending_px:
                px = pending_px
                qty = trade_size / px
                pos_cost = trade_size
                pos_qty = qty
                avg_px = px
                pos_cnt = 1
                entry_bar_idx = i
                exec_sell[i] = px
            pending_px = -1.0

        if pos_qty > 0:
            is_sl = False
            if sl > 0 and c >= avg_px * (1.0 + sl):
                is_sl = True
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c > avg_px:
                is_sl = True

            if is_sl:
                buy_val = pos_qty * c
                fee = buy_val * taker_fee
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                exec_buy[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_px = 0.0
                pos_cnt = 0
                entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            elif rsi[i] < s_cp and c <= avg_px * 0.996:
                buy_val = pos_qty * c
                fee = buy_val * taker_fee
                entry_fee = pos_cost * maker_fee
                pnl = pos_cost - buy_val - fee - entry_fee
                cum_pnl += pnl
                exec_buy[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_px = 0.0
                pos_cnt = 0
                entry_bar_idx = -1
        elif pending_px < 0 and ep_sig[i] and i >= cooldown_until:
            pending_px = c * 1.0002

        pnl_curve[i] = initial_equity + cum_pnl + (pos_cost - pos_qty * c if pos_qty > 0 else 0.0)

    out_df['exec_buy_price'] = exec_buy
    out_df['exec_sell_price'] = exec_sell
    out_df['pnl'] = pnl_curve
    return out_df


def build_rsima3_chart_df(
    df_5m: pd.DataFrame,
    rsi: np.ndarray,
    rsi_ma: np.ndarray,
    ep: float,
    cp: float,
    sl: float,
    initial_equity: float = 100.0,
    max_trades: int = 5,
    ma_filter: Optional[np.ndarray] = None,
    cooldown_bars: int = 288,
    timeout_bars: int = 48
) -> pd.DataFrame:
    """RSIMA3 最良パラメータでの売買マーカー・累積損益曲線付きDataFrameを生成 (optimize_rsima3_fastと100%完全同期)"""
    out_df = df_5m.copy().reset_index(drop=True)
    n = len(out_df)
    closes = out_df['close'].astype(float).values
    lows = out_df['low'].astype(float).values if 'low' in out_df.columns else closes
    trade_size = initial_equity / max_trades
    maker_fee = 0.0002
    taker_fee = 0.0006

    exec_buy = np.full(n, np.nan)
    exec_sell = np.full(n, np.nan)
    pnl_curve = np.full(n, initial_equity, dtype=float)

    gc_arr = np.zeros(n, dtype=bool)
    if n > 2:
        gc_arr[1:] = (rsi[1:] > rsi_ma[1:]) & (rsi[:-1] <= rsi_ma[:-1]) & (rsi_ma[1:] >= rsi_ma[:-1])
    ep_sig = gc_arr.copy()
    if n > 2:
        ep_sig[1:] = gc_arr[1:] & ((rsi_ma[1:] < ep) | (rsi_ma[:-1] < ep))
    if ma_filter is not None and len(ma_filter) == n:
        ep_sig = ep_sig & ma_filter

    pos_qty = 0.0
    pos_cost = 0.0
    avg_px = 0.0
    last_entry_px = 0.0
    pos_cnt = 0
    cum_pnl = 0.0
    pending_px = -1.0
    cooldown_until = -1
    entry_bar_idx = -1

    for i in range(2, n):
        c = closes[i]
        l = lows[i]

        if pending_px > 0:
            if l <= pending_px:
                px = pending_px
                qty = trade_size / px
                pos_cost += trade_size + trade_size * maker_fee
                pos_qty += qty
                last_entry_px = px
                avg_px = pos_cost / pos_qty
                pos_cnt += 1
                if pos_cnt == 1:
                    entry_bar_idx = i
                exec_buy[i] = px
            pending_px = -1.0

        if pos_qty > 0:
            is_sl = False
            if sl > 0 and c <= avg_px * (1.0 - sl):
                is_sl = True
            elif entry_bar_idx > 0 and (i - entry_bar_idx) >= timeout_bars and c < avg_px:
                is_sl = True

            if is_sl:
                sell_val = pos_qty * c
                fee = sell_val * taker_fee
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                exec_sell[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_px = 0.0
                pos_cnt = 0
                entry_bar_idx = -1
                cooldown_until = i + cooldown_bars
            elif rsi[i] > cp and c >= avg_px * 1.006:
                sell_val = pos_qty * c
                fee = sell_val * taker_fee
                pnl = sell_val - pos_cost - fee
                cum_pnl += pnl
                exec_sell[i] = c
                pos_qty = 0.0
                pos_cost = 0.0
                avg_px = 0.0
                pos_cnt = 0
                entry_bar_idx = -1
        elif pending_px < 0 and i >= cooldown_until:
            if pos_cnt == 0:
                if ep_sig[i]:
                    pending_px = c * 0.9998
            elif 1 <= pos_cnt < max_trades:
                if ep_sig[i] and c < last_entry_px * 0.990:
                    pending_px = c * 0.9998

        pnl_curve[i] = initial_equity + cum_pnl + (pos_qty * c - pos_cost if pos_qty > 0 else 0.0)

    out_df['exec_buy_price'] = exec_buy
    out_df['exec_sell_price'] = exec_sell
    out_df['pnl'] = pnl_curve
    return out_df


def calculate_dynamic_mae_stop(
    df_5m: pd.DataFrame,
    strategy: str = "val_poc",
    params: Optional[Dict[str, Any]] = None,
    default_stop: float = -0.025,
    min_stop: float = -0.015,  # 上限クランプ (-1.5%: 浅すぎてノイズで切られるのを防ぐ)
    max_stop: float = -0.032,  # 下限クランプ (-3.2%: 深すぎて大損失になるのを防ぐ)
    return_details: bool = False,
) -> Any:
    """
    直近30日のデータから、勝ちトレードの最大逆行幅 (MAE: Maximum Adverse Excursion) 95%ile および
    勝ちトレード保有時間分布 (動的タイムアウト) を動的に算出
    - 採用足種 (interval: 5m, 15m, 30m, 60m) へのリサンプリング
    - 勝ちトレードの95%が耐え切って利確に至った最大の深さを特定 (動的ストップ)
    - 勝ちトレードの90%が決着した保有バー数に安全バッファを加算 (動的タイムアウト)
    - 安全バンド:
        ストップ: [min_stop, max_stop] にクランプ (基準値: -2.50%)
        タイムアウト: [最低1時間相当, 最大4時間相当] にクランプ
    """
    default_interval = int(params.get("interval", 5)) if params else 5
    default_timeout_bars = max(3, 240 // default_interval)
    default_profile = {
        "dynamic_mae_stop": default_stop,
        "dynamic_timeout_bars": default_timeout_bars,
        "dynamic_timeout_minutes": default_timeout_bars * default_interval,
        "avg_hold_bars": float(default_timeout_bars),
        "avg_hold_minutes": float(default_timeout_bars * default_interval),
        "p90_hold_bars": default_timeout_bars,
        "p90_hold_minutes": float(default_timeout_bars * default_interval),
        "max_hold_bars": default_timeout_bars,
        "max_hold_minutes": float(default_timeout_bars * default_interval),
        "winning_trades_count": 0,
    }

    if df_5m is None or len(df_5m) < 200:
        return (default_stop, default_profile) if return_details else default_stop

    try:
        interval = int(params.get("interval", 5)) if params else 5
        df_use = resample_candles(df_5m, interval) if interval > 5 else df_5m.copy()
        if df_use is None or len(df_use) < 30:
            df_use = df_5m.copy()
            interval = 5

        c = df_use["close"].astype(float).values
        h = df_use["high"].astype(float).values if "high" in df_use.columns else c
        l = df_use["low"].astype(float).values if "low" in df_use.columns else c
        n = len(df_use)

        timeout_bars = max(1, 240 // interval)  # 4時間 = 240分に相当するバー本数

        strat_type = str(strategy).lower()
        is_short_strat = ("short" in strat_type or "vah" in strat_type)

        if "rsi" in strat_type:
            rsi_s, ma_s = calc_rsi_ema(df_use["close"].astype(float), 9, 7)
            rsi_v = rsi_s.values
            ma_v = ma_s.values
            if is_short_strat:
                s_ep = float(params.get("s_ep", 60.0)) if params else 60.0
                s_cp = float(params.get("s_cp", 40.0)) if params else 40.0
            else:
                l_ep = float(params.get("l_ep", 40.0)) if params else 40.0
                l_cp = float(params.get("l_cp", 60.0)) if params else 60.0
        else:
            mp_period = int(params.get("mp", 150)) if params else 150
            ema200 = calc_ema(df_use["close"].astype(float), 200).values
            ma_filter = (c <= ema200) if is_short_strat else (c >= ema200)
            step_calc = max(1, 4 // max(1, interval // 5))
            mp_res = make_market_profile_dual_va(df_use, period=mp_period, num_bins=50, step=step_calc, use_decay=True)
            vals = mp_res["val_70"]
            vahs = mp_res["vah_70"]
            pocs = mp_res["poc"]

        pos_qty = 0.0
        pos_cost = 0.0
        avg_price = 0.0
        entry_idx = -1
        trade_min_l = float("inf")
        trade_max_h = 0.0
        winning_mae = []
        winning_bars = []

        for i in range(1, n):
            cur_c = c[i]
            cur_h = h[i]
            cur_l = l[i]

            if pos_qty == 0.0:
                if is_short_strat:
                    # ショートエントリー
                    if "rsi" in strat_type:
                        if (cur_c > 0 and rsi_v[i] < ma_v[i] and rsi_v[i-1] >= ma_v[i-1] and ma_v[i] > s_ep):
                            pos_qty = 100.0 / cur_c
                            pos_cost = 100.0
                            avg_price = cur_c
                            trade_max_h = cur_h
                            entry_idx = i
                    else:
                        use_ef = params.get("use_ema_filter", True) if params else True
                        cond_ok = (ma_filter[i]) if use_ef else True
                        if (cond_ok and not np.isnan(vahs[i]) and c[i-1] >= vahs[i-1] and cur_c < vahs[i]):
                            pos_qty = 100.0 / cur_c
                            pos_cost = 100.0
                            avg_price = cur_c
                            trade_max_h = cur_h
                            entry_idx = i
                else:
                    # ロングエントリー
                    if "rsi" in strat_type:
                        if (cur_c > 0 and rsi_v[i] > ma_v[i] and rsi_v[i-1] <= ma_v[i-1] and ma_v[i] < l_ep):
                            pos_qty = 100.0 / cur_c
                            pos_cost = 100.0
                            avg_price = cur_c
                            trade_min_l = cur_l
                            entry_idx = i
                    else:
                        use_ef = params.get("use_ema_filter", True) if params else True
                        cond_ok = (ma_filter[i]) if use_ef else True
                        if (cond_ok and not np.isnan(vals[i]) and c[i-1] <= vals[i-1] and cur_c > vals[i]):
                            pos_qty = 100.0 / cur_c
                            pos_cost = 100.0
                            avg_price = cur_c
                            trade_min_l = cur_l
                            entry_idx = i
                continue

            bars_held = i - entry_idx

            if is_short_strat:
                trade_max_h = max(trade_max_h, cur_h)
                # 1. 4時間タイムアウト微損撤退
                if bars_held >= timeout_bars and cur_c > avg_price:
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1
                    continue
                # 2. 損切りチェック (+3.5%逆行上昇カット)
                if cur_c >= avg_price * 1.035:
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1
                    continue
                # 3. ショート利確チェック
                is_tp = False
                if "rsi" in strat_type:
                    if rsi_v[i] < s_cp and cur_c < avg_price * 0.996:
                        is_tp = True
                else:
                    val = vals[i]
                    vah = vahs[i]
                    va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                    tp_target = vah - va_diff * 0.50 if va_diff > 0 else pocs[i]
                    if (cur_l <= tp_target or cur_c <= tp_target) and cur_c < avg_price * 0.9993:
                        is_tp = True
                if is_tp:
                    pnl = pos_cost - pos_qty * cur_c
                    if pnl > 0 and avg_price > 0:
                        mae = (avg_price - trade_max_h) / avg_price  # 逆行幅 (負の値)
                        winning_mae.append(mae)
                        winning_bars.append(bars_held)
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1
            else:
                trade_min_l = min(trade_min_l, cur_l)
                # 1. 4時間タイムアウト微損撤退
                if bars_held >= timeout_bars and cur_c < avg_price:
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1
                    continue
                # 2. 損切りチェック (-3.5%カット)
                if cur_c <= avg_price * 0.965:
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1
                    continue
                # 3. ロング利確チェック
                is_tp = False
                if "rsi" in strat_type:
                    if rsi_v[i] > l_cp and cur_c > avg_price * 1.004:
                        is_tp = True
                else:
                    val = vals[i]
                    vah = vahs[i]
                    va_diff = (vah - val) if (not np.isnan(val) and not np.isnan(vah) and vah > val) else 0.0
                    tp_target = val + va_diff * 0.50 if va_diff > 0 else pocs[i]
                    if (cur_h >= tp_target or cur_c >= tp_target) and cur_c > avg_price * 1.0007:
                        is_tp = True
                if is_tp:
                    pnl = pos_qty * cur_c - pos_cost
                    if pnl > 0 and avg_price > 0:
                        mae = (trade_min_l - avg_price) / avg_price
                        winning_mae.append(mae)
                        winning_bars.append(bars_held)
                    pos_qty = 0.0
                    pos_cost = 0.0
                    entry_idx = -1

        # 動的MAEストップ算出
        if len(winning_mae) >= 5:
            mae_95 = float(np.percentile(winning_mae, 5))  # 下位5% (95%のトレードが耐えた逆行幅)
            clamped = max(min(mae_95, min_stop), max_stop)
            final_stop = round(clamped, 4)
        else:
            final_stop = default_stop

        # 動的タイムアウト算出 (安全ガード: 最低1時間相当 〜 最大4時間相当)
        min_to_bars = max(3, 60 // interval)            # 最低1時間 (5mなら12本)
        max_to_bars = max(min_to_bars, 240 // interval)  # 最大4時間 (5mなら48本)

        if len(winning_bars) >= 5:
            p90_bars = int(np.ceil(np.percentile(winning_bars, 90)))
            avg_bars = float(np.mean(winning_bars))
            max_bars = int(np.max(winning_bars))
            # 勝ちトレードの90%が決着する時間に安全バッファ (+20% または 最低+2本)
            buffered_bars = max(p90_bars + 2, int(np.ceil(p90_bars * 1.2)))
            dyn_to_bars = min(max_to_bars, max(min_to_bars, buffered_bars))
        else:
            dyn_to_bars = max_to_bars
            p90_bars = max_to_bars
            avg_bars = float(max_to_bars)
            max_bars = max_to_bars

        profile = {
            "dynamic_mae_stop": final_stop,
            "dynamic_timeout_bars": dyn_to_bars,
            "dynamic_timeout_minutes": dyn_to_bars * interval,
            "avg_hold_bars": round(avg_bars, 1),
            "avg_hold_minutes": round(avg_bars * interval, 1),
            "p90_hold_bars": p90_bars,
            "p90_hold_minutes": round(p90_bars * interval, 1),
            "max_hold_bars": max_bars,
            "max_hold_minutes": round(max_bars * interval, 1),
            "winning_trades_count": len(winning_bars),
        }

        if return_details:
            return final_stop, profile
        return final_stop
    except Exception:
        return (default_stop, default_profile) if return_details else default_stop


def _run_interval_comparison_short(df_5m: pd.DataFrame, lot: float = 1.0, data_equity: float = 100.0, symbol: str = ""):
    """
    【売り相場 BEAR時専用】ショート4大基本戦略体系 (単発1本固定・EMA200分離モデル / 計144通り) 低負荷ジェントル最適化
    - 評価データ: 5分足 直近30日分 (8,640本 = 30d × 24h × 12) フル活用
    - 最低取引回数フィルター: MIN_TRADES = 10回以上 (再現性の低い一発屋・過学習を完全排除)
    - ショートはナンピン完全なし (max_trades=1 単発固定・目標投資額100%フルサイズ)
    - VAH_POC: 4足種 × MP 9種 × 2条件 (EMAなし/あり) ＝ 72通り
    - RSIMA_SHORT: 4足種 × 9パラメータ (EP 60,65,70 × CP 30,35,40) × 2条件 (EMAなし/あり) ＝ 72通り
    """
    results: Dict[str, Any] = {}
    fixed_initial_equity = float(data_equity) if data_equity > 0 else 100.0
    MIN_TRADES = 10
    os.makedirs("backtest_data", exist_ok=True)
    _touch_bot_heartbeat()

    if df_5m is None or df_5m.empty:
        discord.print_log(f"[{symbol}] 最適化エラー: データが空です。")
        return results, "vah_poc", 5, 200, 0.0, 9.0

    is_1h = False
    if len(df_5m) >= 2 and 'timestamp' in df_5m.columns:
        try:
            diff_min = (pd.to_datetime(df_5m['timestamp'].iloc[-1], utc=True) - pd.to_datetime(df_5m['timestamp'].iloc[-2], utc=True)).total_seconds() / 60.0
            if diff_min >= 45:
                is_1h = True
        except Exception:
            pass

    if len(df_5m) > 8640:
        df_5m = df_5m.tail(8640).reset_index(drop=True)

    if symbol and not is_1h:
        try:
            from bingx_oi_collector import enrich_candles_with_oi_fr
            df_5m = enrich_candles_with_oi_fr(df_5m, symbol, auto_sync=True)
        except Exception as _e:
            print(f"   ⚠️ [{symbol}] OI/FR履歴結合スキップ: {_e}", flush=True)

    discord.print_log(f"\n====== 🐻 [{symbol}] ショート4大基本戦略体系 (単発1本固定/EMA200分離モデル / 計144通り) 低負荷ジェントル最適化 (5m足 {len(df_5m)}本 [30日分] / 最低取引{MIN_TRADES}回以上) ======")

    # 1. VAH_POC ＆ VAH_POC_EMA 最適化 (4足種 × MP 9種 × 2条件 ＝ 72通り)
    intervals = [5, 15, 30, 60] if not is_1h else [60]
    mp_periods = [100, 150, 200, 250, 300, 350, 400, 450, 500] if not is_1h else [24, 48, 72, 96]
    bins_fixed = 50
    va_pct_fixed = 0.70
    va_name_fixed = '70%'

    # VAH_POC (EMAなし)
    best_vp_none_pnl = -float('inf')
    best_vp_none_cfg: Optional[Dict[str, Any]] = None
    fallback_vp_none_pnl = -float('inf')
    fallback_vp_none_cfg: Dict[str, Any] = {
        'strategy': 'vah_poc', 'interval': 5, 'mp_period': 200, 'num_bins': bins_fixed, 'va': va_name_fixed,
        'value_area_pct': va_pct_fixed, 'target_ratio': 0.70, 'ma_type': 'NONE', 'condition': 'none',
        'pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    # VAH_POC_EMA (EMAあり: close <= EMA200)
    best_vp_ema_pnl = -float('inf')
    best_vp_ema_cfg: Optional[Dict[str, Any]] = None
    fallback_vp_ema_pnl = -float('inf')
    fallback_vp_ema_cfg: Dict[str, Any] = {
        'strategy': 'vah_poc_ema', 'interval': 5, 'mp_period': 200, 'num_bins': bins_fixed, 'va': va_name_fixed,
        'value_area_pct': va_pct_fixed, 'target_ratio': 0.70, 'ma_type': 'EMA', 'condition': 'close_below',
        'pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    vp_eval_count = 0
    inv_dfs: Dict[int, pd.DataFrame] = {}

    for inv in intervals:
        _touch_bot_heartbeat()
        df_inv = resample_candles(df_5m, inv) if inv > 5 else df_5m.copy()
        if df_inv is None or len(df_inv) < 120:
            continue
        inv_dfs[inv] = df_inv

        c_inv = df_inv['close'].astype(float).values
        h_inv = df_inv['high'].astype(float).values if 'high' in df_inv.columns else c_inv
        l_inv = df_inv['low'].astype(float).values if 'low' in df_inv.columns else c_inv

        ema200 = calc_ema(df_inv['close'].astype(float), 200).values
        prec_vp = build_entry_precision_filter(df_inv, side="short", is_vp=True)
        ma_filter_ema = (c_inv <= ema200) & prec_vp  # ショートは close <= EMA200 かつ 3要素フィルター
        ma_filter_none = prec_vp

        cd_bars = max(10, 1440 // inv) if not is_1h else 24
        to_bars = max(2, 240 // inv) if not is_1h else 4
        calc_step = 8 if inv == 5 else (3 if inv == 15 else (2 if inv == 30 else 1))

        target_ratios = [0.50, 0.60, 0.70, 0.80]

        for mp in mp_periods:
            time.sleep(0.005)
            if len(df_inv) < mp + 100:
                continue
            _touch_bot_heartbeat()
            mp_res = make_market_profile_dual_va(df_inv, period=mp, num_bins=bins_fixed, step=calc_step, use_decay=True)
            pocs = mp_res['poc']
            val_arr = mp_res['val_70']
            vah_arr = mp_res['vah_70']

            for tr in target_ratios:
                # --- [A] VAH_POC (EMAなし) ---
                sim_none = simulate_vah_poc_fast(
                    c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_none,
                    initial_equity=fixed_initial_equity, max_trades=1, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                )
                vp_eval_count += 1
                lbl_none = f"VAH_POC_{inv}m_MP{mp}_B{bins_fixed}_VA{va_name_fixed}_TR{int(tr*100)}_NoEMA"
                results[lbl_none] = {
                    'strategy_type': 'vah_poc', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + sim_none['final_pnl'],
                    'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'value_area_pct': va_pct_fixed,
                    'target_ratio': tr,
                    'ma_type': 'NONE', 'ma_condition': 'none', 'atr_multi': 0.0, 'sl_center_margin': 9.0,
                    'DD_max': sim_none['DD_max'], 'win_rate': sim_none['win_rate'], 'trade_count': sim_none['trade_count'],
                    'single_pnl': sim_none['final_pnl'], 'single_trades': sim_none['trade_count'], 'single_win_rate': sim_none['win_rate']
                }
                cand_none = {
                    'strategy': 'vah_poc', 'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'va': va_name_fixed,
                    'value_area_pct': va_pct_fixed, 'target_ratio': tr, 'ma_type': 'NONE', 'condition': 'none',
                    'pnl': sim_none['final_pnl'], 'pnl_first_half': sim_none.get('pnl_first_half', 0.0), 'pnl_second_half': sim_none.get('pnl_second_half', 0.0),
                    'pnl_recent_7d': sim_none.get('pnl_recent_7d', 0.0), 'trades_recent_7d': sim_none.get('trades_recent_7d', 0),
                    'trade_count': sim_none['trade_count'],
                    'win_rate': sim_none['win_rate'], 'DD_max': sim_none['DD_max'],
                    'single_pnl': sim_none['final_pnl'], 'single_trades': sim_none['trade_count'], 'single_win_rate': sim_none['win_rate']
                }
                vp_cnt = sim_none['trade_count']
                vp_pnl = sim_none['final_pnl']
                vp_p1st = sim_none.get('pnl_first_half', 0.0)
                vp_p2nd = sim_none.get('pnl_second_half', 0.0)
                vp_p7d = sim_none.get('pnl_recent_7d', 0.0)
                vp_c7d = sim_none.get('trades_recent_7d', 0)
                is_vp_tier1 = (vp_cnt >= MIN_TRADES and vp_pnl > 0.0 and vp_p7d > 0.0 and vp_c7d >= 1)
                is_vp_tier2 = (vp_cnt >= MIN_TRADES and vp_pnl > 0.0 and vp_p1st > 0.0 and vp_p2nd > vp_p1st)
                if is_vp_tier1:
                    vp_score = 1000.0 + vp_p7d * 2.0 + vp_p2nd + vp_pnl * 0.1
                elif is_vp_tier2:
                    vp_score = 500.0 + vp_p2nd + vp_pnl * 0.1
                elif vp_cnt >= 5 and vp_pnl > 0.0:
                    vp_score = vp_pnl
                else:
                    vp_score = -1000.0 + vp_pnl

                if vp_score > best_vp_none_pnl:
                    best_vp_none_pnl = vp_score
                    best_vp_none_cfg = cand_none
                if vp_cnt >= 5 and vp_pnl > fallback_vp_none_pnl:
                    fallback_vp_none_pnl = vp_pnl
                    fallback_vp_none_cfg = cand_none

                # --- [B] VAH_POC_EMA (EMAあり) ---
                sim_ema = simulate_vah_poc_fast(
                    c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_ema,
                    initial_equity=fixed_initial_equity, max_trades=1, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                )
                vp_eval_count += 1
                lbl_ema = f"VAH_POC_EMA_{inv}m_MP{mp}_B{bins_fixed}_VA{va_name_fixed}_TR{int(tr*100)}_EMA200"
                results[lbl_ema] = {
                    'strategy_type': 'vah_poc_ema', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + sim_ema['final_pnl'],
                    'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'value_area_pct': va_pct_fixed,
                    'target_ratio': tr,
                    'ma_type': 'EMA', 'ma_condition': 'close_below', 'atr_multi': 0.0, 'sl_center_margin': 9.0,
                    'DD_max': sim_ema['DD_max'], 'win_rate': sim_ema['win_rate'], 'trade_count': sim_ema['trade_count'],
                    'single_pnl': sim_ema['final_pnl'], 'single_trades': sim_ema['trade_count'], 'single_win_rate': sim_ema['win_rate']
                }
                cand_ema = {
                    'strategy': 'vah_poc_ema', 'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'va': va_name_fixed,
                    'value_area_pct': va_pct_fixed, 'target_ratio': tr, 'ma_type': 'EMA', 'condition': 'close_below',
                    'pnl': sim_ema['final_pnl'], 'pnl_first_half': sim_ema.get('pnl_first_half', 0.0), 'pnl_second_half': sim_ema.get('pnl_second_half', 0.0),
                    'pnl_recent_7d': sim_ema.get('pnl_recent_7d', 0.0), 'trades_recent_7d': sim_ema.get('trades_recent_7d', 0),
                    'trade_count': sim_ema['trade_count'],
                    'win_rate': sim_ema['win_rate'], 'DD_max': sim_ema['DD_max'],
                    'single_pnl': sim_ema['final_pnl'], 'single_trades': sim_ema['trade_count'], 'single_win_rate': sim_ema['win_rate']
                }
                vpe_cnt = sim_ema['trade_count']
                vpe_pnl = sim_ema['final_pnl']
                vpe_p1st = sim_ema.get('pnl_first_half', 0.0)
                vpe_p2nd = sim_ema.get('pnl_second_half', 0.0)
                vpe_p7d = sim_ema.get('pnl_recent_7d', 0.0)
                vpe_c7d = sim_ema.get('trades_recent_7d', 0)
                is_vpe_tier1 = (vpe_cnt >= MIN_TRADES and vpe_pnl > 0.0 and vpe_p7d > 0.0 and vpe_c7d >= 1)
                is_vpe_tier2 = (vpe_cnt >= MIN_TRADES and vpe_pnl > 0.0 and vpe_p1st > 0.0 and vpe_p2nd > vpe_p1st)
                if is_vpe_tier1:
                    vpe_score = 1000.0 + vpe_p7d * 2.0 + vpe_p2nd + vpe_pnl * 0.1
                elif is_vpe_tier2:
                    vpe_score = 500.0 + vpe_p2nd + vpe_pnl * 0.1
                elif vpe_cnt >= 5 and vpe_pnl > 0.0:
                    vpe_score = vpe_pnl
                else:
                    vpe_score = -1000.0 + vpe_pnl

                if vpe_score > best_vp_ema_pnl:
                    best_vp_ema_pnl = vpe_score
                    best_vp_ema_cfg = cand_ema
                if vpe_cnt >= 5 and vpe_pnl > fallback_vp_ema_pnl:
                    fallback_vp_ema_pnl = vpe_pnl
                    fallback_vp_ema_cfg = cand_ema

        print(f"   [{symbol}] VAH_POC系 (ショートEMAなし/あり計2種) {inv}m足 ({len(df_inv)}本) 検証完了 (累計 {vp_eval_count} パターン評価済)", flush=True)
        time.sleep(0.01)

    if best_vp_none_cfg is None:
        best_vp_none_cfg = fallback_vp_none_cfg
    if best_vp_ema_cfg is None:
        best_vp_ema_cfg = fallback_vp_ema_cfg

    # 2. RSIMA_SHORT ＆ RSIMA_SHORT_EMA 最適化 (4足種 × 9 パラメータ × 2条件 ＝ 72通り)
    best_rsi_none_pnl = -float('inf')
    best_rsi_none_cfg: Optional[Dict[str, Any]] = None
    fallback_rsi_none_pnl = -float('inf')
    fallback_rsi_none_cfg: Dict[str, Any] = {
        'strategy': 'rsima_short', 'interval': 5, 's_ep': 60.0, 's_cp': 40.0, 'sl_pct': 0.05,
        'final_pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0, 'chart_df': pd.DataFrame(),
        'use_ema': False
    }

    best_rsi_ema_pnl = -float('inf')
    best_rsi_ema_cfg: Optional[Dict[str, Any]] = None
    fallback_rsi_ema_pnl = -float('inf')
    fallback_rsi_ema_cfg: Dict[str, Any] = {
        'strategy': 'rsima_short_ema', 'interval': 5, 's_ep': 60.0, 's_cp': 40.0, 'sl_pct': 0.05,
        'final_pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0, 'chart_df': pd.DataFrame(),
        'use_ema': True
    }

    for r_inv in intervals:
        time.sleep(0.01)
        _touch_bot_heartbeat()
        df_r_inv = inv_dfs.get(r_inv)
        if df_r_inv is None:
            df_r_inv = resample_candles(df_5m, r_inv) if r_inv > 5 else df_5m.copy()
            if df_r_inv is None or len(df_r_inv) < 50:
                continue

        r_rsi, r_rsi_ma = calc_rsi_ema(df_r_inv['close'].astype(float), rsi_period=9, ma_period=7)
        c_r = df_r_inv['close'].astype(float).values
        h_r = df_r_inv['high'].astype(float).values if 'high' in df_r_inv.columns else c_r
        l_r = df_r_inv['low'].astype(float).values if 'low' in df_r_inv.columns else c_r

        ema200_r = calc_ema(df_r_inv['close'].astype(float), 200).values
        prec_rsi = build_entry_precision_filter(df_r_inv, side="short", is_vp=False)
        ma_filter_r_ema = (c_r <= ema200_r) & prec_rsi  # ショートは close <= EMA200 かつ 3要素フィルター
        ma_filter_r_none = prec_rsi

        val_arr_r, vah_arr_r = None, None
        try:
            r_step = max(1, len(df_r_inv) // 100)
            mp_tmp = make_market_profile_dual_va(df_r_inv, period=100, num_bins=50, step=r_step, use_decay=True)
            val_arr_r = mp_tmp['val_70']
            vah_arr_r = mp_tmp['vah_70']
        except Exception:
            pass

        # --- [A] RSIMA_SHORT (EMAなし) ---
        cd_bars_r = max(10, 1440 // r_inv) if not is_1h else 24
        to_bars_r = max(2, 240 // r_inv) if not is_1h else 4
        res_r_none = optimize_rsima3_short_fast(
            c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
            initial_equity=fixed_initial_equity, max_trades=1, min_trades=MIN_TRADES,
            val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_none,
            cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
        )
        rsima_none_key = f"RSIMA_SHORT_{r_inv}m_EP{int(res_r_none['s_ep'])}_CP{int(res_r_none['s_cp'])}_NoEMA"
        results[rsima_none_key] = {
            'strategy_type': 'rsima_short', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + res_r_none['final_pnl'],
            'interval': r_inv, 'mp_period': 100, 'num_bins': 50, 'value_area_pct': 0.70,
            'ma_type': 'NONE', 'ma_condition': 'NONE', 'atr_multi': 0.0, 'sl_center_margin': 0.0,
            's_ep': res_r_none['s_ep'], 's_cp': res_r_none['s_cp'], 'sl_pct': res_r_none['sl_pct'],
            'DD_max': res_r_none['DD_max'], 'win_rate': res_r_none['win_rate'], 'trade_count': res_r_none['trade_count'],
            'single_pnl': res_r_none['final_pnl'], 'single_trades': res_r_none['trade_count'], 'single_win_rate': res_r_none['win_rate']
        }
        r_none_cand = {**res_r_none, 'strategy': 'rsima_short', 'interval': r_inv, 'df_inv': df_r_inv, 'rsi_s': r_rsi, 'rsi_ma_s': r_rsi_ma, 'use_ema': False, 'ma_filter': ma_filter_r_none}
        rn_cnt = res_r_none['trade_count']
        rn_pnl = res_r_none['final_pnl']
        rn_p1st = res_r_none.get('pnl_first_half', 0.0)
        rn_p2nd = res_r_none.get('pnl_second_half', 0.0)
        rn_p7d = res_r_none.get('pnl_recent_7d', 0.0)
        rn_c7d = res_r_none.get('trades_recent_7d', 0)
        is_rn_tier1 = (rn_cnt >= MIN_TRADES and rn_pnl > 0.0 and rn_p7d > 0.0 and rn_c7d >= 1)
        is_rn_tier2 = (rn_cnt >= MIN_TRADES and rn_pnl > 0.0 and rn_p1st > 0.0 and rn_p2nd > rn_p1st)
        if is_rn_tier1:
            rn_score = 1000.0 + rn_p7d * 2.0 + rn_p2nd + rn_pnl * 0.1
        elif is_rn_tier2:
            rn_score = 500.0 + rn_p2nd + rn_pnl * 0.1
        elif rn_cnt >= 5 and rn_pnl > 0.0:
            rn_score = rn_pnl
        else:
            rn_score = -1000.0 + rn_pnl

        if rn_score > best_rsi_none_pnl:
            best_rsi_none_pnl = rn_score
            best_rsi_none_cfg = r_none_cand
        if rn_cnt >= 5 and rn_pnl > fallback_rsi_none_pnl:
            fallback_rsi_none_pnl = rn_pnl
            fallback_rsi_none_cfg = r_none_cand

        # --- [B] RSIMA_SHORT_EMA (EMAあり) ---
        res_r_ema = optimize_rsima3_short_fast(
            c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
            initial_equity=fixed_initial_equity, max_trades=1, min_trades=MIN_TRADES,
            val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_ema,
            cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
        )
        rsima_ema_key = f"RSIMA_SHORT_EMA_{r_inv}m_EP{int(res_r_ema['s_ep'])}_CP{int(res_r_ema['s_cp'])}_EMA200"
        results[rsima_ema_key] = {
            'strategy_type': 'rsima_short_ema', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + res_r_ema['final_pnl'],
            'interval': r_inv, 'mp_period': 100, 'num_bins': 50, 'value_area_pct': 0.70,
            'ma_type': 'EMA', 'ma_condition': 'close_below', 'atr_multi': 0.0, 'sl_center_margin': 0.0,
            's_ep': res_r_ema['s_ep'], 's_cp': res_r_ema['s_cp'], 'sl_pct': res_r_ema['sl_pct'],
            'DD_max': res_r_ema['DD_max'], 'win_rate': res_r_ema['win_rate'], 'trade_count': res_r_ema['trade_count'],
            'single_pnl': res_r_ema['final_pnl'], 'single_trades': res_r_ema['trade_count'], 'single_win_rate': res_r_ema['win_rate']
        }
        r_ema_cand = {**res_r_ema, 'strategy': 'rsima_short_ema', 'interval': r_inv, 'df_inv': df_r_inv, 'rsi_s': r_rsi, 'rsi_ma_s': r_rsi_ma, 'use_ema': True, 'ma_filter': ma_filter_r_ema}
        re_cnt = res_r_ema['trade_count']
        re_pnl = res_r_ema['final_pnl']
        re_p1st = res_r_ema.get('pnl_first_half', 0.0)
        re_p2nd = res_r_ema.get('pnl_second_half', 0.0)
        re_p7d = res_r_ema.get('pnl_recent_7d', 0.0)
        re_c7d = res_r_ema.get('trades_recent_7d', 0)
        is_re_tier1 = (re_cnt >= MIN_TRADES and re_pnl > 0.0 and re_p7d > 0.0 and re_c7d >= 1)
        is_re_tier2 = (re_cnt >= MIN_TRADES and re_pnl > 0.0 and re_p1st > 0.0 and re_p2nd > re_p1st)
        if is_re_tier1:
            re_score = 1000.0 + re_p7d * 2.0 + re_p2nd + re_pnl * 0.1
        elif is_re_tier2:
            re_score = 500.0 + re_p2nd + re_pnl * 0.1
        elif re_cnt >= 5 and re_pnl > 0.0:
            re_score = re_pnl
        else:
            re_score = -1000.0 + re_pnl

        if re_score > best_rsi_ema_pnl:
            best_rsi_ema_pnl = re_score
            best_rsi_ema_cfg = r_ema_cand
        if re_cnt >= 5 and re_pnl > fallback_rsi_ema_pnl:
            fallback_rsi_ema_pnl = re_pnl
            fallback_rsi_ema_cfg = r_ema_cand

    if best_rsi_none_cfg is None:
        best_rsi_none_cfg = fallback_rsi_none_cfg
    if best_rsi_ema_cfg is None:
        best_rsi_ema_cfg = fallback_rsi_ema_cfg

    # 3. ショート4大基本戦略体系 直接比較 & 動的採用決定
    candidates = [
        best_vp_none_cfg,
        best_vp_ema_cfg,
        best_rsi_none_cfg,
        best_rsi_ema_cfg
    ]

    pnl_vpn = float(best_vp_none_cfg['pnl'])
    cnt_vpn = int(best_vp_none_cfg['trade_count'])
    win_vpn = float(best_vp_none_cfg['win_rate'])

    pnl_vpe = float(best_vp_ema_cfg['pnl'])
    cnt_vpe = int(best_vp_ema_cfg['trade_count'])
    win_vpe = float(best_vp_ema_cfg['win_rate'])

    pnl_rsn = float(best_rsi_none_cfg['final_pnl'])
    cnt_rsn = int(best_rsi_none_cfg['trade_count'])
    win_rsn = float(best_rsi_none_cfg['win_rate'])

    pnl_rse = float(best_rsi_ema_cfg['final_pnl'])
    cnt_rse = int(best_rsi_ema_cfg['trade_count'])
    win_rse = float(best_rsi_ema_cfg['win_rate'])

    tr_vpn = int(best_vp_none_cfg.get('target_ratio', 0.70) * 100)
    tr_vpe = int(best_vp_ema_cfg.get('target_ratio', 0.70) * 100)
    discord.print_log(f"\n📊 【ショート4大基本戦略 最適化結果直接比較】 ({symbol})")
    discord.print_log(f"🔹 [VAH_POC ({best_vp_none_cfg['interval']}m/MP{best_vp_none_cfg['mp_period']}/TR{tr_vpn}%/EMAなし)]: 純利益 = {pnl_vpn:+.2f} USDC (前半: {best_vp_none_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_vp_none_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_vpn}回, 勝率: {win_vpn:.1f}%)")
    discord.print_log(f"🔹 [VAH_POC_EMA ({best_vp_ema_cfg['interval']}m/MP{best_vp_ema_cfg['mp_period']}/TR{tr_vpe}%/EMAあり)]: 純利益 = {pnl_vpe:+.2f} USDC (前半: {best_vp_ema_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_vp_ema_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_vpe}回, 勝率: {win_vpe:.1f}%)")
    discord.print_log(f"🔸 [RSIMA_SHORT ({best_rsi_none_cfg['interval']}m/EP{int(best_rsi_none_cfg['s_ep'])}/CP{int(best_rsi_none_cfg['s_cp'])}/EMAなし)]: 純利益 = {pnl_rsn:+.2f} USDC (前半: {best_rsi_none_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_rsi_none_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_rsn}回, 勝率: {win_rsn:.1f}%)")
    discord.print_log(f"🔸 [RSIMA_SHORT_EMA ({best_rsi_ema_cfg['interval']}m/EP{int(best_rsi_ema_cfg['s_ep'])}/CP{int(best_rsi_ema_cfg['s_cp'])}/EMAあり)]: 純利益 = {pnl_rse:+.2f} USDC (前半: {best_rsi_ema_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_rsi_ema_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_rse}回, 勝率: {win_rse:.1f}%)")

    valid_cands_tier1 = []  # Tier 1: 直近7日黒字 & 取引発生 (最優先実効性)
    valid_cands_tier2 = []  # Tier 2: 従来の二重頑健性クリア (後半加速)
    for c in candidates:
        c_cnt = int(c.get('trade_count', 0))
        c_pnl = float(c.get('final_pnl', c.get('pnl', 0.0)))
        c_pnl_1st = float(c.get('pnl_first_half', 0.0))
        c_pnl_2nd = float(c.get('pnl_second_half', 0.0))
        c_pnl_7d = float(c.get('pnl_recent_7d', 0.0))
        c_cnt_7d = int(c.get('trades_recent_7d', 0))
        if c_cnt >= MIN_TRADES and c_pnl > 0.0 and c_pnl_7d > 0.0 and c_cnt_7d >= 1:
            # Tier 1: 直近7日黒字 & 取引発生を最優先ソート
            valid_cands_tier1.append((c_pnl_7d * 2.0 + c_pnl_2nd + c_pnl * 0.1, c))
        elif c_cnt >= MIN_TRADES and c_pnl > 0.0 and c_pnl_1st > 0.0 and c_pnl_2nd > c_pnl_1st:
            valid_cands_tier2.append((c_pnl_2nd, c))

    if valid_cands_tier1:
        valid_cands_tier1.sort(key=lambda x: x[0], reverse=True)
        winner = valid_cands_tier1[0][1]
        is_winner_qual = True
        qual_level = "Tier1(直近7d黒字&後半加速)"
    elif valid_cands_tier2:
        valid_cands_tier2.sort(key=lambda x: x[0], reverse=True)
        winner = valid_cands_tier2[0][1]
        is_winner_qual = True
        qual_level = "Tier2(後半加速)"
    else:
        candidates.sort(key=lambda c: float(c.get('final_pnl', c.get('pnl', -float('inf')))), reverse=True)
        winner = candidates[0]
        is_winner_qual = False
        qual_level = "Tier3(二重頑健性未達)" 

    chosen_strat = winner.get('strategy', 'vah_poc')
    best_interval = int(winner['interval'])

    if chosen_strat in ('rsima_short', 'rsima_short_ema'):
        best_strategy = chosen_strat
        best_mp = 100
        best_margin = winner.get('sl_pct', 0.05) * 100.0
        df_chart_base = winner.get('df_inv', df_5m)
        cd_bars_chart = max(10, 1440 // best_interval) if best_interval < 60 else 24
        to_bars_chart = max(2, 240 // best_interval) if best_interval < 60 else 4
        chart_df = build_rsima3_short_chart_df(
            df_chart_base, winner['rsi_s'].values, winner['rsi_ma_s'].values,
            winner['s_ep'], winner['s_cp'], winner.get('sl_pct', 0.05),
            initial_equity=fixed_initial_equity, ma_filter=winner.get('ma_filter'),
            cooldown_bars=cd_bars_chart, timeout_bars=to_bars_chart
        )
        pnl_final = float(winner['final_pnl'])
        cnt_final = int(winner['trade_count'])
        win_final = float(winner['win_rate'])
        best_params = {
            'strategy': best_strategy,
            'trade_side': 'short',
            'use_ema_filter': winner.get('use_ema', False),
            'interval': best_interval,
            'mp': 100,
            's_ep': winner['s_ep'],
            's_cp': winner['s_cp'],
            'sl_pct': winner.get('sl_pct', 0.05),
            'margin': best_margin,
            'pnl': pnl_final,
            'trade_count': cnt_final,
            'win_rate': win_final,
            'DD_max': winner['DD_max'],
            'single_pnl': pnl_final,
            'single_trades': cnt_final,
            'single_win_rate': win_final,
            'pnl_first_half': float(winner.get('pnl_first_half', 0.0)),
            'pnl_second_half': float(winner.get('pnl_second_half', 0.0)),
            'pnl_recent_7d': float(winner.get('pnl_recent_7d', 0.0)),
            'trades_recent_7d': int(winner.get('trades_recent_7d', 0)),
            'is_qualified': is_winner_qual,
            'chart_df': chart_df
        }
        ema_tag = "(EMAあり)" if winner.get('use_ema') else "(EMAなし)"
        p1st = winner.get('pnl_first_half', 0.0)
        p2nd = winner.get('pnl_second_half', 0.0)
        p7d = winner.get('pnl_recent_7d', 0.0)
        c7d = winner.get('trades_recent_7d', 0)
        qual_tag = f"🟢合格({qual_level})" if is_winner_qual else "🔴二重頑健性未達"
        discord.print_log(f"🏆 [{symbol}] 採用ショート戦略: 【{best_strategy.upper()} ({best_interval}m/{ema_tag})】を動的採用！ (純利益: {pnl_final:+.2f} USDC [前半: {p1st:+.2f} / 後半: {p2nd:+.2f} / 直近7d: {p7d:+.2f}({c7d}回) / 単発{cnt_final}回/{win_final:.1f}%] - {qual_tag})")
    else:
        best_strategy = chosen_strat
        best_mp = int(winner['mp_period'])
        best_margin = 9.0

        df_best_inv = inv_dfs.get(best_interval)
        if df_best_inv is None:
            df_best_inv = resample_candles(df_5m, best_interval) if best_interval > 5 else df_5m.copy()
        df_chart = df_best_inv.copy().reset_index(drop=True)
        c_b = df_chart['close'].astype(float).values
        h_b = df_chart['high'].astype(float).values if 'high' in df_chart.columns else c_b
        l_b = df_chart['low'].astype(float).values if 'low' in df_chart.columns else c_b
        step_b = 6 if best_interval == 5 else (2 if best_interval == 15 else 1)
        mp_b = make_market_profile_dual_va(df_chart, period=best_mp, num_bins=int(winner['num_bins']), step=step_b, use_decay=True)
        val_b = mp_b['val_70'] if winner['va'] == '70%' else mp_b['val_80']
        vah_b = mp_b['vah_70'] if winner['va'] == '70%' else mp_b['vah_80']
        poc_b = mp_b['poc']

        use_ema_val = (best_strategy == 'vah_poc_ema')
        prec_b = build_entry_precision_filter(df_chart, side="short", is_vp=True)
        if use_ema_val:
            ma_b = calc_ema(df_chart['close'].astype(float), 200).values
            mf_b = (c_b <= ma_b) & prec_b
        else:
            mf_b = prec_b

        cd_b = max(10, 1440 // best_interval) if not is_1h else 24
        to_b = max(2, 240 // best_interval) if not is_1h else 4
        winner_tr = float(winner.get('target_ratio', 0.70))
        sim_b = simulate_vah_poc_fast(
            c_b, h_b, l_b, val_b, vah_b, poc_b, mf_b,
            initial_equity=fixed_initial_equity, max_trades=1, cooldown_bars=cd_b, timeout_bars=to_b, target_ratio=winner_tr, record_chart=True
        )
        df_chart['POC'] = pd.Series(poc_b).ffill().bfill().values
        df_chart['VAH'] = pd.Series(vah_b).ffill().bfill().values
        df_chart['VAL'] = pd.Series(val_b).ffill().bfill().values
        df_chart['exec_buy_price'] = sim_b['exec_buy_price']
        df_chart['exec_sell_price'] = sim_b['exec_sell_price']
        df_chart['pnl'] = sim_b['pnl_curve']

        pnl_final = float(winner['pnl'])
        cnt_final = int(winner['trade_count'])
        win_final = float(winner['win_rate'])
        best_params = {
            'strategy': best_strategy,
            'trade_side': 'short',
            'use_ema_filter': use_ema_val,
            'interval': best_interval,
            'mp': best_mp,
            'num_bins': winner['num_bins'],
            'value_area_pct': winner['value_area_pct'],
            'target_ratio': winner_tr,
            'ma_type': 'EMA' if use_ema_val else 'NONE',
            'ma_condition': 'close_below' if use_ema_val else 'none',
            'margin': 9.0,
            'pnl': pnl_final,
            'trade_count': cnt_final,
            'win_rate': win_final,
            'DD_max': winner['DD_max'],
            'single_pnl': pnl_final,
            'single_trades': cnt_final,
            'single_win_rate': win_final,
            'pnl_first_half': float(winner.get('pnl_first_half', 0.0)),
            'pnl_second_half': float(winner.get('pnl_second_half', 0.0)),
            'pnl_recent_7d': float(winner.get('pnl_recent_7d', 0.0)),
            'trades_recent_7d': int(winner.get('trades_recent_7d', 0)),
            'is_qualified': is_winner_qual,
            'chart_df': df_chart
        }
        ema_tag = "(EMAあり)" if use_ema_val else "(EMAなし)"
        p1st = winner.get('pnl_first_half', 0.0)
        p2nd = winner.get('pnl_second_half', 0.0)
        p7d = winner.get('pnl_recent_7d', 0.0)
        c7d = winner.get('trades_recent_7d', 0)
        qual_tag = f"🟢合格({qual_level})" if is_winner_qual else "🔴二重頑健性未達"
        discord.print_log(f"🏆 [{symbol}] 採用ショート戦略: 【{best_strategy.upper()} ({best_interval}m/{ema_tag})】を動的採用！ (純利益: {pnl_final:+.2f} USDC [前半: {p1st:+.2f} / 後半: {p2nd:+.2f} / 直近7d: {p7d:+.2f}({c7d}回) / 単発{cnt_final}回/{win_final:.1f}%] - {qual_tag})")

    dyn_stop, risk_prof = calculate_dynamic_mae_stop(
        df_5m=df_5m,
        strategy=best_strategy,
        params=best_params,
        default_stop=-0.025,
        min_stop=-0.015,
        max_stop=-0.032,
        return_details=True
    )
    best_params['dynamic_mae_stop'] = dyn_stop
    best_params['sl_pct'] = abs(dyn_stop)
    best_params['dynamic_timeout_bars'] = risk_prof['dynamic_timeout_bars']
    best_params['dynamic_timeout_minutes'] = risk_prof['dynamic_timeout_minutes']
    best_params['avg_hold_minutes'] = risk_prof['avg_hold_minutes']
    best_params['p90_hold_minutes'] = risk_prof['p90_hold_minutes']
    best_params['max_hold_minutes'] = risk_prof['max_hold_minutes']
    to_min = risk_prof['dynamic_timeout_minutes']
    to_b = risk_prof['dynamic_timeout_bars']
    p90_m = risk_prof['p90_hold_minutes']
    discord.print_log(f"🛡️ [{symbol}] ショート動的リスク算出: 動的SL={dyn_stop:.2%} (バンド: -1.50%〜-3.20%) | 動的タイムアウト={to_min}分 ({to_b}本, 勝ち90%ile={p90_m}分)")

    results["_best_params_"] = best_params
    return results, best_strategy, best_interval, best_mp, best_margin


def run_interval_comparison(df_5m: pd.DataFrame, lot: float = 1.0, data_equity: float = 100.0, side_mode: str = "long", symbol: str = "", force_strategy: Optional[str] = None, prefer_breakout: bool = False):
    """
    【新仕様】3態勢地合い連動モデル (買い: ロング144通り / 売り: ショート144通り 単発固定) 低負荷ジェントル最適化
    - 評価データ: 5分足 直近30日分 (8,640本 = 30d × 24h × 12) フル活用
    - 最低取引回数フィルター: MIN_TRADES = 10回以上 (再現性の低い一発屋・過学習を完全排除)
    """
    if str(side_mode).lower() == "short":
        return _run_interval_comparison_short(df_5m=df_5m, lot=lot, data_equity=data_equity, symbol=symbol)

    results: Dict[str, Any] = {}
    fixed_initial_equity = float(data_equity) if data_equity > 0 else 100.0
    MIN_TRADES = 10
    os.makedirs("backtest_data", exist_ok=True)
    _touch_bot_heartbeat()

    if df_5m is None or df_5m.empty:
        discord.print_log(f"[{symbol}] 最適化エラー: データが空です。")
        return results, "val_poc", 5, 200, 0.0, 9.0

    # 1H足が渡された場合の互換性フォールバック（タイムスタンプ間隔が50分以上の場合は1H足と判定）
    is_1h = False
    if len(df_5m) >= 2 and 'timestamp' in df_5m.columns:
        try:
            diff_min = (pd.to_datetime(df_5m['timestamp'].iloc[-1], utc=True) - pd.to_datetime(df_5m['timestamp'].iloc[-2], utc=True)).total_seconds() / 60.0
            if diff_min >= 45:
                is_1h = True
        except Exception:
            pass

    # 評価データ: 5分足は直近30日分（8,640本 = 30d × 24h × 12）をフル活用して堅牢性を確保
    if len(df_5m) > 8640:
        df_5m = df_5m.tail(8640).reset_index(drop=True)

    if symbol and not is_1h:
        try:
            from bingx_oi_collector import enrich_candles_with_oi_fr
            df_5m = enrich_candles_with_oi_fr(df_5m, symbol, auto_sync=True)
        except Exception as _e:
            print(f"   ⚠️ [{symbol}] OI/FR履歴結合スキップ: {_e}", flush=True)

    discord.print_log(f"\n====== 🌿 [{symbol}] 4大基本戦略体系 (EMA200分離モデル / 計144通り) 低負荷ジェントル最適化 (5m足 {len(df_5m)}本 [30日分] / 最低取引{MIN_TRADES}回以上) ======")

    # ==============================================================================
    # 1. VAL_POC ＆ VAL_POC_EMA 最適化 (4足種 × MP 9種 × 2条件 ＝ 72通り)
    # ==============================================================================
    intervals = [5, 15, 30, 60] if not is_1h else [60]
    mp_periods = [100, 150, 200, 250, 300, 350, 400, 450, 500] if not is_1h else [24, 48, 72, 96]
    bins_fixed = 50
    va_pct_fixed = 0.70
    va_name_fixed = '70%'

    # VAL_POC (EMAなし)
    best_vp_none_pnl = -float('inf')
    best_vp_none_cfg: Optional[Dict[str, Any]] = None
    fallback_vp_none_pnl = -float('inf')
    fallback_vp_none_cfg: Dict[str, Any] = {
        'strategy': 'val_poc', 'interval': 5, 'mp_period': 200, 'num_bins': bins_fixed, 'va': va_name_fixed,
        'value_area_pct': va_pct_fixed, 'target_ratio': 0.70, 'ma_type': 'NONE', 'condition': 'none',
        'pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    # VAL_POC_EMA (EMAあり)
    best_vp_ema_pnl = -float('inf')
    best_vp_ema_cfg: Optional[Dict[str, Any]] = None
    fallback_vp_ema_pnl = -float('inf')
    fallback_vp_ema_cfg: Dict[str, Any] = {
        'strategy': 'val_poc_ema', 'interval': 5, 'mp_period': 200, 'num_bins': bins_fixed, 'va': va_name_fixed,
        'value_area_pct': va_pct_fixed, 'target_ratio': 0.70, 'ma_type': 'EMA', 'condition': 'close_above',
        'pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0
    }

    vp_eval_count = 0
    inv_dfs: Dict[int, pd.DataFrame] = {}

    for inv in intervals:
        _touch_bot_heartbeat()
        df_inv = resample_candles(df_5m, inv) if inv > 5 else df_5m.copy()
        if df_inv is None or len(df_inv) < 120:
            continue
        inv_dfs[inv] = df_inv

        c_inv = df_inv['close'].astype(float).values
        h_inv = df_inv['high'].astype(float).values if 'high' in df_inv.columns else c_inv
        l_inv = df_inv['low'].astype(float).values if 'low' in df_inv.columns else c_inv

        ema200 = calc_ema(df_inv['close'].astype(float), 200).values
        prec_vp = build_entry_precision_filter(df_inv, side="long", is_vp=True)
        ma_filter_ema = (c_inv >= ema200) & prec_vp
        ma_filter_none = prec_vp

        cd_bars = max(10, 1440 // inv) if not is_1h else 24
        to_bars = max(2, 240 // inv) if not is_1h else 4
        calc_step = 8 if inv == 5 else (3 if inv == 15 else (2 if inv == 30 else 1))

        target_ratios = [0.50, 0.60, 0.70, 0.80]

        for mp in mp_periods:
            time.sleep(0.005)  # 協調的コンテキストスイッチ: GILを解放してメインイベントループ(WebSocket KeepAlive)を保護
            if len(df_inv) < mp + 100:
                continue
            _touch_bot_heartbeat()
            # Bins=50 固定で Market Profile 生成
            mp_res = make_market_profile_dual_va(df_inv, period=mp, num_bins=bins_fixed, step=calc_step, use_decay=True)
            pocs = mp_res['poc']
            val_arr = mp_res['val_70']
            vah_arr = mp_res['vah_70']

            for tr in target_ratios:
                # --- [A] VAL_POC (EMAなし) シミュレーション ---
                sim_single_none = simulate_val_poc_fast(
                    c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_none,
                    initial_equity=fixed_initial_equity, max_trades=1, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                )
                vp_eval_count += 1
                is_single_none_qual = (
                    sim_single_none['trade_count'] >= MIN_TRADES
                    and sim_single_none.get('pnl_first_half', 0.0) > 0.0
                    and sim_single_none.get('pnl_second_half', 0.0) > sim_single_none.get('pnl_first_half', 0.0)
                )
                if is_single_none_qual:
                    sim_none = simulate_val_poc_fast(
                        c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_none,
                        initial_equity=fixed_initial_equity, max_trades=3, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                    )
                else:
                    sim_none = sim_single_none
                sim_none['single_pnl'] = sim_single_none['final_pnl']
                sim_none['single_trades'] = sim_single_none['trade_count']
                sim_none['single_win_rate'] = sim_single_none['win_rate']

                lbl_none = f"VAL_POC_{inv}m_MP{mp}_B{bins_fixed}_VA{va_name_fixed}_TR{int(tr*100)}_NoEMA"
                results[lbl_none] = {
                    'strategy_type': 'val_poc', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + sim_none['final_pnl'],
                    'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'value_area_pct': va_pct_fixed,
                    'target_ratio': tr,
                    'ma_type': 'NONE', 'ma_condition': 'none', 'atr_multi': 0.0, 'sl_center_margin': 9.0,
                    'DD_max': sim_none['DD_max'], 'win_rate': sim_none['win_rate'], 'trade_count': sim_none['trade_count'],
                    'single_pnl': sim_none['single_pnl'], 'single_trades': sim_none['single_trades'], 'single_win_rate': sim_none['single_win_rate']
                }
                cand_none = {
                    'strategy': 'val_poc', 'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'va': va_name_fixed,
                    'value_area_pct': va_pct_fixed, 'target_ratio': tr, 'ma_type': 'NONE', 'condition': 'none',
                    'pnl': sim_none['final_pnl'], 'pnl_first_half': sim_none.get('pnl_first_half', 0.0), 'pnl_second_half': sim_none.get('pnl_second_half', 0.0),
                    'pnl_recent_7d': sim_none.get('pnl_recent_7d', 0.0), 'trades_recent_7d': sim_none.get('trades_recent_7d', 0),
                    'trade_count': sim_none['trade_count'],
                    'win_rate': sim_none['win_rate'], 'DD_max': sim_none['DD_max'],
                    'single_pnl': sim_none['single_pnl'], 'single_trades': sim_none['single_trades'], 'single_win_rate': sim_none['single_win_rate']
                }
                vp_cnt = sim_none['trade_count']
                vp_pnl = sim_none['final_pnl']
                vp_p1st = sim_none.get('pnl_first_half', 0.0)
                vp_p2nd = sim_none.get('pnl_second_half', 0.0)
                vp_p7d = sim_none.get('pnl_recent_7d', 0.0)
                vp_c7d = sim_none.get('trades_recent_7d', 0)
                is_vp_tier1 = (vp_cnt >= MIN_TRADES and vp_pnl > 0.0 and vp_p7d > 0.0 and vp_c7d >= 1)
                is_vp_tier2 = (vp_cnt >= MIN_TRADES and vp_pnl > 0.0 and vp_p1st > 0.0 and vp_p2nd > vp_p1st)
                if is_vp_tier1:
                    vp_score = 1000.0 + vp_p7d * 2.0 + vp_p2nd + vp_pnl * 0.1
                elif is_vp_tier2:
                    vp_score = 500.0 + vp_p2nd + vp_pnl * 0.1
                elif vp_cnt >= 5 and vp_pnl > 0.0:
                    vp_score = vp_pnl
                else:
                    vp_score = -1000.0 + vp_pnl

                if vp_score > best_vp_none_pnl:
                    best_vp_none_pnl = vp_score
                    best_vp_none_cfg = cand_none
                if vp_cnt >= 5 and vp_pnl > fallback_vp_none_pnl:
                    fallback_vp_none_pnl = vp_pnl
                    fallback_vp_none_cfg = cand_none

                # --- [B] VAL_POC_EMA (EMAあり) シミュレーション ---
                sim_single_ema = simulate_val_poc_fast(
                    c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_ema,
                    initial_equity=fixed_initial_equity, max_trades=1, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                )
                vp_eval_count += 1
                is_single_ema_qual = (
                    sim_single_ema['trade_count'] >= MIN_TRADES
                    and sim_single_ema.get('pnl_first_half', 0.0) > 0.0
                    and sim_single_ema.get('pnl_second_half', 0.0) > sim_single_ema.get('pnl_first_half', 0.0)
                )
                if is_single_ema_qual:
                    sim_ema = simulate_val_poc_fast(
                        c_inv, h_inv, l_inv, val_arr, vah_arr, pocs, ma_filter_ema,
                        initial_equity=fixed_initial_equity, max_trades=3, cooldown_bars=cd_bars, timeout_bars=to_bars, target_ratio=tr
                    )
                else:
                    sim_ema = sim_single_ema
                sim_ema['single_pnl'] = sim_single_ema['final_pnl']
                sim_ema['single_trades'] = sim_single_ema['trade_count']
                sim_ema['single_win_rate'] = sim_single_ema['win_rate']

                lbl_ema = f"VAL_POC_EMA_{inv}m_MP{mp}_B{bins_fixed}_VA{va_name_fixed}_TR{int(tr*100)}_EMA200"
                results[lbl_ema] = {
                    'strategy_type': 'val_poc_ema', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + sim_ema['final_pnl'],
                    'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'value_area_pct': va_pct_fixed,
                    'target_ratio': tr,
                    'ma_type': 'EMA', 'ma_condition': 'close_above', 'atr_multi': 0.0, 'sl_center_margin': 9.0,
                    'DD_max': sim_ema['DD_max'], 'win_rate': sim_ema['win_rate'], 'trade_count': sim_ema['trade_count'],
                    'single_pnl': sim_ema['single_pnl'], 'single_trades': sim_ema['single_trades'], 'single_win_rate': sim_ema['single_win_rate']
                }
                cand_ema = {
                    'strategy': 'val_poc_ema', 'interval': inv, 'mp_period': mp, 'num_bins': bins_fixed, 'va': va_name_fixed,
                    'value_area_pct': va_pct_fixed, 'target_ratio': tr, 'ma_type': 'EMA', 'condition': 'close_above',
                    'pnl': sim_ema['final_pnl'], 'pnl_first_half': sim_ema.get('pnl_first_half', 0.0), 'pnl_second_half': sim_ema.get('pnl_second_half', 0.0),
                    'pnl_recent_7d': sim_ema.get('pnl_recent_7d', 0.0), 'trades_recent_7d': sim_ema.get('trades_recent_7d', 0),
                    'trade_count': sim_ema['trade_count'],
                    'win_rate': sim_ema['win_rate'], 'DD_max': sim_ema['DD_max'],
                    'single_pnl': sim_ema['single_pnl'], 'single_trades': sim_ema['single_trades'], 'single_win_rate': sim_ema['single_win_rate']
                }
                vpe_cnt = sim_ema['trade_count']
                vpe_pnl = sim_ema['final_pnl']
                vpe_p1st = sim_ema.get('pnl_first_half', 0.0)
                vpe_p2nd = sim_ema.get('pnl_second_half', 0.0)
                vpe_p7d = sim_ema.get('pnl_recent_7d', 0.0)
                vpe_c7d = sim_ema.get('trades_recent_7d', 0)
                is_vpe_tier1 = (vpe_cnt >= MIN_TRADES and vpe_pnl > 0.0 and vpe_p7d > 0.0 and vpe_c7d >= 1)
                is_vpe_tier2 = (vpe_cnt >= MIN_TRADES and vpe_pnl > 0.0 and vpe_p1st > 0.0 and vpe_p2nd > vpe_p1st)
                if is_vpe_tier1:
                    vpe_score = 1000.0 + vpe_p7d * 2.0 + vpe_p2nd + vpe_pnl * 0.1
                elif is_vpe_tier2:
                    vpe_score = 500.0 + vpe_p2nd + vpe_pnl * 0.1
                elif vpe_cnt >= 5 and vpe_pnl > 0.0:
                    vpe_score = vpe_pnl
                else:
                    vpe_score = -1000.0 + vpe_pnl

                if vpe_score > best_vp_ema_pnl:
                    best_vp_ema_pnl = vpe_score
                    best_vp_ema_cfg = cand_ema
                if vpe_cnt >= 5 and vpe_pnl > fallback_vp_ema_pnl:
                    fallback_vp_ema_pnl = vpe_pnl
                    fallback_vp_ema_cfg = cand_ema

        print(f"   [{symbol}] VAL_POC系 (EMAなし/あり計2種) {inv}m足 ({len(df_inv)}本) 検証完了 (累計 {vp_eval_count} パターン評価済)", flush=True)
        time.sleep(0.01)

    if best_vp_none_cfg is None:
        best_vp_none_cfg = fallback_vp_none_cfg
    if best_vp_ema_cfg is None:
        best_vp_ema_cfg = fallback_vp_ema_cfg

    # ==============================================================================
    # 2. RSIMA ＆ RSIMA_EMA 最適化 (4足種 × 9 パラメータ × 2条件 ＝ 72通り)
    # ==============================================================================
    # RSIMA (EMAなし)
    best_rsi_none_pnl = -float('inf')
    best_rsi_none_cfg: Optional[Dict[str, Any]] = None
    fallback_rsi_none_pnl = -float('inf')
    fallback_rsi_none_cfg: Dict[str, Any] = {
        'strategy': 'rsima', 'interval': 5, 'l_ep': 40.0, 'l_cp': 60.0, 'sl_pct': 0.05,
        'final_pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0, 'chart_df': pd.DataFrame(),
        'use_ema': False
    }

    # RSIMA_EMA (EMAあり)
    best_rsi_ema_pnl = -float('inf')
    best_rsi_ema_cfg: Optional[Dict[str, Any]] = None
    fallback_rsi_ema_pnl = -float('inf')
    fallback_rsi_ema_cfg: Dict[str, Any] = {
        'strategy': 'rsima_ema', 'interval': 5, 'l_ep': 40.0, 'l_cp': 60.0, 'sl_pct': 0.05,
        'final_pnl': 0.0, 'trade_count': 0, 'win_rate': 0.0, 'DD_max': 0.0, 'chart_df': pd.DataFrame(),
        'use_ema': True
    }

    for r_inv in intervals:
        time.sleep(0.01)  # 協調的コンテキストスイッチ: GILを解放してメインイベントループ(WebSocket KeepAlive)を保護
        _touch_bot_heartbeat()
        df_r_inv = inv_dfs.get(r_inv)
        if df_r_inv is None:
            df_r_inv = resample_candles(df_5m, r_inv) if r_inv > 5 else df_5m.copy()
            if df_r_inv is None or len(df_r_inv) < 50:
                continue

        r_rsi, r_rsi_ma = calc_rsi_ema(df_r_inv['close'].astype(float), rsi_period=9, ma_period=7)
        c_r = df_r_inv['close'].astype(float).values
        h_r = df_r_inv['high'].astype(float).values if 'high' in df_r_inv.columns else c_r
        l_r = df_r_inv['low'].astype(float).values if 'low' in df_r_inv.columns else c_r

        ema200_r = calc_ema(df_r_inv['close'].astype(float), 200).values
        prec_rsi = build_entry_precision_filter(df_r_inv, side="long", is_vp=False)
        ma_filter_r_ema = (c_r >= ema200_r) & prec_rsi
        ma_filter_r_none = prec_rsi

        # 直近 VAL/VAH の取得（step間引きで高速取得）
        val_arr_r, vah_arr_r = None, None
        try:
            r_step = max(1, len(df_r_inv) // 100)
            mp_tmp = make_market_profile_dual_va(df_r_inv, period=100, num_bins=50, step=r_step, use_decay=True)
            val_arr_r = mp_tmp['val_70']
            vah_arr_r = mp_tmp['vah_70']
        except Exception:
            pass

        # --- [A] RSIMA (EMAなし) ---
        cd_bars_r = max(10, 1440 // r_inv) if not is_1h else 24
        to_bars_r = max(2, 240 // r_inv) if not is_1h else 4
        res_r_none_single = optimize_rsima3_fast(
            c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
            initial_equity=fixed_initial_equity, max_trades=1, min_trades=MIN_TRADES,
            val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_none,
            cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
        )
        is_single_r_none_qual = (
            res_r_none_single['trade_count'] >= MIN_TRADES
            and res_r_none_single.get('pnl_first_half', 0.0) > 0.0
            and res_r_none_single.get('pnl_second_half', 0.0) > res_r_none_single.get('pnl_first_half', 0.0)
        )
        if is_single_r_none_qual:
            res_r_none = optimize_rsima3_fast(
                c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
                initial_equity=fixed_initial_equity, max_trades=3, min_trades=MIN_TRADES,
                val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_none,
                cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
            )
        else:
            res_r_none = res_r_none_single
        res_r_none['single_pnl'] = res_r_none_single['final_pnl']
        res_r_none['single_trades'] = res_r_none_single['trade_count']
        res_r_none['single_win_rate'] = res_r_none_single['win_rate']

        rsima_none_key = f"RSIMA_{r_inv}m_EP{int(res_r_none['l_ep'])}_CP{int(res_r_none['l_cp'])}_NoEMA"
        results[rsima_none_key] = {
            'strategy_type': 'rsima', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + res_r_none['final_pnl'],
            'interval': r_inv, 'mp_period': 100, 'num_bins': 50, 'value_area_pct': 0.70,
            'ma_type': 'NONE', 'ma_condition': 'NONE', 'atr_multi': 0.0, 'sl_center_margin': 0.0,
            'l_ep': res_r_none['l_ep'], 'l_cp': res_r_none['l_cp'], 'sl_pct': res_r_none['sl_pct'],
            'DD_max': res_r_none['DD_max'], 'win_rate': res_r_none['win_rate'], 'trade_count': res_r_none['trade_count'],
            'single_pnl': res_r_none['single_pnl'], 'single_trades': res_r_none['single_trades'], 'single_win_rate': res_r_none['single_win_rate']
        }
        r_none_cand = {**res_r_none, 'strategy': 'rsima', 'interval': r_inv, 'df_inv': df_r_inv, 'rsi_s': r_rsi, 'rsi_ma_s': r_rsi_ma, 'use_ema': False, 'ma_filter': ma_filter_r_none}
        rn_cnt = res_r_none['trade_count']
        rn_pnl = res_r_none['final_pnl']
        rn_p1st = res_r_none.get('pnl_first_half', 0.0)
        rn_p2nd = res_r_none.get('pnl_second_half', 0.0)
        rn_p7d = res_r_none.get('pnl_recent_7d', 0.0)
        rn_c7d = res_r_none.get('trades_recent_7d', 0)
        is_rn_tier1 = (rn_cnt >= MIN_TRADES and rn_pnl > 0.0 and rn_p7d > 0.0 and rn_c7d >= 1)
        is_rn_tier2 = (rn_cnt >= MIN_TRADES and rn_pnl > 0.0 and rn_p1st > 0.0 and rn_p2nd > rn_p1st)
        if is_rn_tier1:
            rn_score = 1000.0 + rn_p7d * 2.0 + rn_p2nd + rn_pnl * 0.1
        elif is_rn_tier2:
            rn_score = 500.0 + rn_p2nd + rn_pnl * 0.1
        elif rn_cnt >= 5 and rn_pnl > 0.0:
            rn_score = rn_pnl
        else:
            rn_score = -1000.0 + rn_pnl

        if rn_score > best_rsi_none_pnl:
            best_rsi_none_pnl = rn_score
            best_rsi_none_cfg = r_none_cand
        if rn_cnt >= 5 and rn_pnl > fallback_rsi_none_pnl:
            fallback_rsi_none_pnl = rn_pnl
            fallback_rsi_none_cfg = r_none_cand

        # --- [B] RSIMA_EMA (EMAあり) ---
        res_r_ema_single = optimize_rsima3_fast(
            c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
            initial_equity=fixed_initial_equity, max_trades=1, min_trades=MIN_TRADES,
            val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_ema,
            cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
        )
        is_single_r_ema_qual = (
            res_r_ema_single['trade_count'] >= MIN_TRADES
            and res_r_ema_single.get('pnl_first_half', 0.0) > 0.0
            and res_r_ema_single.get('pnl_second_half', 0.0) > res_r_ema_single.get('pnl_first_half', 0.0)
        )
        if is_single_r_ema_qual:
            res_r_ema = optimize_rsima3_fast(
                c_r, h_r, l_r, r_rsi.values, r_rsi_ma.values,
                initial_equity=fixed_initial_equity, max_trades=3, min_trades=MIN_TRADES,
                val_arr=val_arr_r, vah_arr=vah_arr_r, ma_filter=ma_filter_r_ema,
                cooldown_bars=cd_bars_r, timeout_bars=to_bars_r
            )
        else:
            res_r_ema = res_r_ema_single
        res_r_ema['single_pnl'] = res_r_ema_single['final_pnl']
        res_r_ema['single_trades'] = res_r_ema_single['trade_count']
        res_r_ema['single_win_rate'] = res_r_ema_single['win_rate']

        rsima_ema_key = f"RSIMA_EMA_{r_inv}m_EP{int(res_r_ema['l_ep'])}_CP{int(res_r_ema['l_cp'])}_EMA200"
        results[rsima_ema_key] = {
            'strategy_type': 'rsima_ema', 'df': pd.DataFrame(), 'final_pnl': fixed_initial_equity + res_r_ema['final_pnl'],
            'interval': r_inv, 'mp_period': 100, 'num_bins': 50, 'value_area_pct': 0.70,
            'ma_type': 'EMA', 'ma_condition': 'close_above', 'atr_multi': 0.0, 'sl_center_margin': 0.0,
            'l_ep': res_r_ema['l_ep'], 'l_cp': res_r_ema['l_cp'], 'sl_pct': res_r_ema['sl_pct'],
            'DD_max': res_r_ema['DD_max'], 'win_rate': res_r_ema['win_rate'], 'trade_count': res_r_ema['trade_count'],
            'single_pnl': res_r_ema['single_pnl'], 'single_trades': res_r_ema['single_trades'], 'single_win_rate': res_r_ema['single_win_rate']
        }
        r_ema_cand = {**res_r_ema, 'strategy': 'rsima_ema', 'interval': r_inv, 'df_inv': df_r_inv, 'rsi_s': r_rsi, 'rsi_ma_s': r_rsi_ma, 'use_ema': True, 'ma_filter': ma_filter_r_ema}
        re_cnt = res_r_ema['trade_count']
        re_pnl = res_r_ema['final_pnl']
        re_p1st = res_r_ema.get('pnl_first_half', 0.0)
        re_p2nd = res_r_ema.get('pnl_second_half', 0.0)
        re_p7d = res_r_ema.get('pnl_recent_7d', 0.0)
        re_c7d = res_r_ema.get('trades_recent_7d', 0)
        is_re_tier1 = (re_cnt >= MIN_TRADES and re_pnl > 0.0 and re_p7d > 0.0 and re_c7d >= 1)
        is_re_tier2 = (re_cnt >= MIN_TRADES and re_pnl > 0.0 and re_p1st > 0.0 and re_p2nd > re_p1st)
        if is_re_tier1:
            re_score = 1000.0 + re_p7d * 2.0 + re_p2nd + re_pnl * 0.1
        elif is_re_tier2:
            re_score = 500.0 + re_p2nd + re_pnl * 0.1
        elif re_cnt >= 5 and re_pnl > 0.0:
            re_score = re_pnl
        else:
            re_score = -1000.0 + re_pnl

        if re_score > best_rsi_ema_pnl:
            best_rsi_ema_pnl = re_score
            best_rsi_ema_cfg = r_ema_cand
        if re_cnt >= 5 and re_pnl > fallback_rsi_ema_pnl:
            fallback_rsi_ema_pnl = re_pnl
            fallback_rsi_ema_cfg = r_ema_cand

    if best_rsi_none_cfg is None:
        best_rsi_none_cfg = fallback_rsi_none_cfg
    if best_rsi_ema_cfg is None:
        best_rsi_ema_cfg = fallback_rsi_ema_cfg

    # ==============================================================================
    # 3. 4大基本戦略体系 直接比較 & 動的採用決定 (最低取引回数フィルター考慮)
    # ==============================================================================
    candidates = [
        best_vp_none_cfg,
        best_vp_ema_cfg,
        best_rsi_none_cfg,
        best_rsi_ema_cfg
    ]

    pnl_vpn = float(best_vp_none_cfg['pnl'])
    cnt_vpn = int(best_vp_none_cfg['trade_count'])
    win_vpn = float(best_vp_none_cfg['win_rate'])

    pnl_vpe = float(best_vp_ema_cfg['pnl'])
    cnt_vpe = int(best_vp_ema_cfg['trade_count'])
    win_vpe = float(best_vp_ema_cfg['win_rate'])

    pnl_rsn = float(best_rsi_none_cfg['final_pnl'])
    cnt_rsn = int(best_rsi_none_cfg['trade_count'])
    win_rsn = float(best_rsi_none_cfg['win_rate'])

    pnl_rse = float(best_rsi_ema_cfg['final_pnl'])
    cnt_rse = int(best_rsi_ema_cfg['trade_count'])
    win_rse = float(best_rsi_ema_cfg['win_rate'])

    tr_vpn = int(best_vp_none_cfg.get('target_ratio', 0.70) * 100)
    tr_vpe = int(best_vp_ema_cfg.get('target_ratio', 0.70) * 100)
    discord.print_log(f"\n📊 【4大基本戦略 最適化結果直接比較】 ({symbol})")
    discord.print_log(
        f"🔹 [VAL_POC ({best_vp_none_cfg['interval']}m/MP{best_vp_none_cfg['mp_period']}/TR{tr_vpn}%/EMAなし)]: "
        f"純利益 = {pnl_vpn:+.2f} USDC (前半: {best_vp_none_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_vp_none_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_vpn}回, 勝率: {win_vpn:.1f}%)"
    )
    discord.print_log(
        f"🔹 [VAL_POC_EMA ({best_vp_ema_cfg['interval']}m/MP{best_vp_ema_cfg['mp_period']}/TR{tr_vpe}%/EMAあり)]: "
        f"純利益 = {pnl_vpe:+.2f} USDC (前半: {best_vp_ema_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_vp_ema_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_vpe}回, 勝率: {win_vpe:.1f}%)"
    )
    discord.print_log(
        f"🔸 [RSIMA ({best_rsi_none_cfg['interval']}m/EP{int(best_rsi_none_cfg['l_ep'])}/CP{int(best_rsi_none_cfg['l_cp'])}/EMAなし)]: "
        f"純利益 = {pnl_rsn:+.2f} USDC (前半: {best_rsi_none_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_rsi_none_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_rsn}回, 勝率: {win_rsn:.1f}%)"
    )
    discord.print_log(
        f"🔸 [RSIMA_EMA ({best_rsi_ema_cfg['interval']}m/EP{int(best_rsi_ema_cfg['l_ep'])}/CP{int(best_rsi_ema_cfg['l_cp'])}/EMAあり)]: "
        f"純利益 = {pnl_rse:+.2f} USDC (前半: {best_rsi_ema_cfg.get('pnl_first_half', 0.0):+.2f} / 後半: {best_rsi_ema_cfg.get('pnl_second_half', 0.0):+.2f}, 取引: {cnt_rse}回, 勝率: {win_rse:.1f}%)"
    )

    valid_cands_tier1 = []  # Tier 1: 直近7日黒字 & 取引発生 (最優先実効性)
    valid_cands_tier2 = []  # Tier 2: 従来の二重頑健性クリア (後半加速)
    for c in candidates:
        c_cnt = int(c.get('trade_count', 0))
        c_pnl = float(c.get('final_pnl', c.get('pnl', 0.0)))
        c_pnl_1st = float(c.get('pnl_first_half', 0.0))
        c_pnl_2nd = float(c.get('pnl_second_half', 0.0))
        c_pnl_7d = float(c.get('pnl_recent_7d', 0.0))
        c_cnt_7d = int(c.get('trades_recent_7d', 0))
        if c_cnt >= MIN_TRADES and c_pnl > 0.0 and c_pnl_7d > 0.0 and c_cnt_7d >= 1:
            valid_cands_tier1.append((c_pnl_7d * 2.0 + c_pnl_2nd + c_pnl * 0.1, c))
        elif c_cnt >= MIN_TRADES and c_pnl > 0.0 and c_pnl_1st > 0.0 and c_pnl_2nd > c_pnl_1st:
            valid_cands_tier2.append((c_pnl_2nd, c))

    if valid_cands_tier1:
        valid_cands_tier1.sort(key=lambda x: x[0], reverse=True)
        winner = valid_cands_tier1[0][1]
        is_winner_qual = True
        qual_level = "Tier1(直近7d黒字&後半加速)"
    elif valid_cands_tier2:
        valid_cands_tier2.sort(key=lambda x: x[0], reverse=True)
        winner = valid_cands_tier2[0][1]
        is_winner_qual = True
        qual_level = "Tier2(後半加速)"
    else:
        candidates.sort(key=lambda c: float(c.get('final_pnl', c.get('pnl', -float('inf')))), reverse=True)
        winner = candidates[0]
        is_winner_qual = False
        qual_level = "Tier3(二重頑健性未達)" 

    chosen_strat = winner.get('strategy', 'val_poc')
    best_interval = int(winner['interval'])

    if chosen_strat in ('rsima', 'rsima_ema'):
        best_strategy = chosen_strat
        best_mp = 100
        best_margin = winner.get('sl_pct', 0.05) * 100.0
        df_chart_base = winner.get('df_inv', df_5m)
        cd_bars_chart = max(10, 1440 // best_interval) if best_interval < 60 else 24
        to_bars_chart = max(2, 240 // best_interval) if best_interval < 60 else 4
        chart_df = build_rsima3_chart_df(
            df_chart_base, winner['rsi_s'].values, winner['rsi_ma_s'].values,
            winner['l_ep'], winner['l_cp'], winner.get('sl_pct', 0.05),
            initial_equity=fixed_initial_equity, max_trades=3, ma_filter=winner.get('ma_filter'),
            cooldown_bars=cd_bars_chart, timeout_bars=to_bars_chart
        )
        pnl_final = float(winner['final_pnl'])
        cnt_final = int(winner['trade_count'])
        win_final = float(winner['win_rate'])
        best_params = {
            'strategy': best_strategy,
            'use_ema_filter': winner.get('use_ema', False),
            'interval': best_interval,
            'mp': 100,
            'l_ep': winner['l_ep'],
            'l_cp': winner['l_cp'],
            'sl_pct': winner.get('sl_pct', 0.05),
            'margin': best_margin,
            'pnl': pnl_final,
            'trade_count': cnt_final,
            'win_rate': win_final,
            'DD_max': winner['DD_max'],
            'single_pnl': winner.get('single_pnl', pnl_final),
            'single_trades': winner.get('single_trades', cnt_final),
            'single_win_rate': winner.get('single_win_rate', win_final),
            'pnl_first_half': float(winner.get('pnl_first_half', 0.0)),
            'pnl_second_half': float(winner.get('pnl_second_half', 0.0)),
            'pnl_recent_7d': float(winner.get('pnl_recent_7d', 0.0)),
            'trades_recent_7d': int(winner.get('trades_recent_7d', 0)),
            'is_qualified': is_winner_qual,
            'chart_df': chart_df
        }
        s_p = best_params['single_pnl']
        s_c = best_params['single_trades']
        s_w = best_params['single_win_rate']
        ema_tag = "(EMAあり)" if winner.get('use_ema') else "(EMAなし)"
        p1st = winner.get('pnl_first_half', 0.0)
        p2nd = winner.get('pnl_second_half', 0.0)
        p7d = winner.get('pnl_recent_7d', 0.0)
        c7d = winner.get('trades_recent_7d', 0)
        qual_tag = f"🟢合格({qual_level})" if is_winner_qual else "🔴二重頑健性未達"
        discord.print_log(f"🏆 [{symbol}] 採用戦略: 【{best_strategy.upper()} ({best_interval}m/{ema_tag})】を動的採用！ (純利益: {pnl_final:+.2f} USDC [前半: {p1st:+.2f} / 後半: {p2nd:+.2f} / 直近7d: {p7d:+.2f}({c7d}回) / ナンピン{cnt_final}回/{win_final:.1f}%] vs シングル: {s_p:+.2f} USDC [{s_c}回/{s_w:.1f}%] - {qual_tag})")
    else:
        best_strategy = chosen_strat
        best_mp = int(winner['mp_period'])
        best_margin = 9.0

        # 最良VAL_POC設定でのチャート用DataFrameを生成
        df_best_inv = inv_dfs.get(best_interval)
        if df_best_inv is None:
            df_best_inv = resample_candles(df_5m, best_interval) if best_interval > 5 else df_5m.copy()
        df_chart = df_best_inv.copy().reset_index(drop=True)
        c_b = df_chart['close'].astype(float).values
        h_b = df_chart['high'].astype(float).values if 'high' in df_chart.columns else c_b
        l_b = df_chart['low'].astype(float).values if 'low' in df_chart.columns else c_b
        step_b = 6 if best_interval == 5 else (2 if best_interval == 15 else 1)
        mp_b = make_market_profile_dual_va(df_chart, period=best_mp, num_bins=int(winner['num_bins']), step=step_b, use_decay=True)
        val_b = mp_b['val_70'] if winner['va'] == '70%' else mp_b['val_80']
        vah_b = mp_b['vah_70'] if winner['va'] == '70%' else mp_b['vah_80']
        poc_b = mp_b['poc']

        use_ema_val = (best_strategy == 'val_poc_ema')
        prec_b = build_entry_precision_filter(df_chart, side="long", is_vp=True)
        if use_ema_val:
            ma_b = calc_ema(df_chart['close'].astype(float), 200).values
            mf_b = (c_b >= ma_b) & prec_b
        else:
            mf_b = prec_b

        cd_b = max(10, 1440 // best_interval) if not is_1h else 24
        to_b = max(2, 240 // best_interval) if not is_1h else 4
        winner_tr = float(winner.get('target_ratio', 0.70))
        sim_b = simulate_val_poc_fast(
            c_b, h_b, l_b, val_b, vah_b, poc_b, mf_b,
            initial_equity=fixed_initial_equity, cooldown_bars=cd_b, timeout_bars=to_b, target_ratio=winner_tr, record_chart=True
        )
        df_chart['POC'] = pd.Series(poc_b).ffill().bfill().values
        df_chart['VAH'] = pd.Series(vah_b).ffill().bfill().values
        df_chart['VAL'] = pd.Series(val_b).ffill().bfill().values
        df_chart['exec_buy_price'] = sim_b['exec_buy_price']
        df_chart['exec_sell_price'] = sim_b['exec_sell_price']
        df_chart['pnl'] = sim_b['pnl_curve']

        pnl_final = float(winner['pnl'])
        cnt_final = int(winner['trade_count'])
        win_final = float(winner['win_rate'])
        best_params = {
            'strategy': best_strategy,
            'use_ema_filter': use_ema_val,
            'interval': best_interval,
            'mp': best_mp,
            'num_bins': winner['num_bins'],
            'value_area_pct': winner['value_area_pct'],
            'target_ratio': winner_tr,
            'ma_type': 'EMA' if use_ema_val else 'NONE',
            'ma_condition': 'close_above' if use_ema_val else 'none',
            'margin': 9.0,
            'pnl': pnl_final,
            'trade_count': cnt_final,
            'win_rate': win_final,
            'DD_max': winner['DD_max'],
            'single_pnl': winner.get('single_pnl', pnl_final),
            'single_trades': winner.get('single_trades', cnt_final),
            'single_win_rate': winner.get('single_win_rate', win_final),
            'pnl_first_half': float(winner.get('pnl_first_half', 0.0)),
            'pnl_second_half': float(winner.get('pnl_second_half', 0.0)),
            'pnl_recent_7d': float(winner.get('pnl_recent_7d', 0.0)),
            'trades_recent_7d': int(winner.get('trades_recent_7d', 0)),
            'is_qualified': is_winner_qual,
            'chart_df': df_chart
        }
        s_p = best_params['single_pnl']
        s_c = best_params['single_trades']
        s_w = best_params['single_win_rate']
        ema_tag = "(EMAあり)" if use_ema_val else "(EMAなし)"
        p1st = winner.get('pnl_first_half', 0.0)
        p2nd = winner.get('pnl_second_half', 0.0)
        p7d = winner.get('pnl_recent_7d', 0.0)
        c7d = winner.get('trades_recent_7d', 0)
        qual_tag = f"🟢合格({qual_level})" if is_winner_qual else "🔴二重頑健性未達"
        discord.print_log(f"🏆 [{symbol}] 採用戦略: 【{best_strategy.upper()} ({best_interval}m/{ema_tag})】を動的採用！ (純利益: {pnl_final:+.2f} USDC [前半: {p1st:+.2f} / 後半: {p2nd:+.2f} / 直近7d: {p7d:+.2f}({c7d}回) / ナンピン{cnt_final}回/{win_final:.1f}%] vs シングル: {s_p:+.2f} USDC [{s_c}回/{s_w:.1f}%] - {qual_tag})")

    # 直近30日の勝ちトレード MAE 95%ile に基づく動的ストップ率を算出 (安全バンド: -1.5% 〜 -3.2%)
    dyn_stop, risk_prof = calculate_dynamic_mae_stop(
        df_5m=df_5m,
        strategy=best_strategy,
        params=best_params,
        default_stop=-0.025,
        min_stop=-0.015,
        max_stop=-0.032,
        return_details=True
    )
    best_params['dynamic_mae_stop'] = dyn_stop
    best_params['sl_pct'] = abs(dyn_stop)
    best_params['dynamic_timeout_bars'] = risk_prof['dynamic_timeout_bars']
    best_params['dynamic_timeout_minutes'] = risk_prof['dynamic_timeout_minutes']
    best_params['avg_hold_minutes'] = risk_prof['avg_hold_minutes']
    best_params['p90_hold_minutes'] = risk_prof['p90_hold_minutes']
    best_params['max_hold_minutes'] = risk_prof['max_hold_minutes']
    to_min = risk_prof['dynamic_timeout_minutes']
    to_b = risk_prof['dynamic_timeout_bars']
    p90_m = risk_prof['p90_hold_minutes']
    discord.print_log(f"🛡️ [{symbol}] 動的リスク算出: 動的SL={dyn_stop:.2%} (バンド: -1.50%〜-3.20%) | 動的タイムアウト={to_min}分 ({to_b}本, 勝ち90%ile={p90_m}分)")

    results["_best_params_"] = best_params
    return results, best_strategy, best_interval, best_mp, best_margin



class VPTrailingManager:
    """ボリュームプロファイル（VAL / POC / VAH）に基づく階層型リスクリワード＆ステップトレーリング管理クラス"""
    def __init__(
        self,
        side: str,
        entry_price: float,
        fee_margin_pct: float = 0.0005,      # 手数料カバーマージン (0.05%)
        initial_margin_pct: float = 0.0005,  # VAL/VAH 初期マージン (0.05%)
        strategy_type: str = "range",        # 戦略タイプ ('range' または 'breakout')
        entry_val: Optional[float] = None,   # エントリー時の VAL（固定初期SL基準）
        entry_vah: Optional[float] = None,   # エントリー時の VAH（固定初期SL基準）
        max_hard_sl_pct: float = 0.035,      # 最大ハードストップ率 (デフォルト3.5%に厳格制限)
        initial_tight_sl_pct: Optional[float] = 0.020  # 初動タイトストップ率 (急落時の即死ガード: 2.0%)
    ):
        self.side = side.upper()
        self.entry_price = float(entry_price)
        self.fee_margin_pct = float(fee_margin_pct)
        self.initial_margin_pct = float(initial_margin_pct)
        self.strategy_type = str(strategy_type).lower()
        self.max_hard_sl_pct = float(max_hard_sl_pct)
        self.initial_tight_sl_pct = float(initial_tight_sl_pct) if initial_tight_sl_pct is not None else None
        # レンジ戦略はエントリーがVAL/VAH付近なので、初期マージンを2倍に拡大
        if self.strategy_type == "range":
            self.initial_margin_pct = self.initial_margin_pct * 2
        
        self.step = 0
        if self.strategy_type == "breakout":
            self.step_names = {
                0: "INITIAL(TIGHT_SL)" if self.initial_tight_sl_pct else ("INITIAL(VAL)" if self.side == "LONG" else "INITIAL(VAH)"),
                1: "BREAKEVEN(+1R)",
                2: "TARGET_LOCK(+2R)",
                3: "RUNNER_TRAIL"
            }
        else:
            self.step_names = {
                0: "INITIAL",
                1: "POC",
                2: "VAH" if self.side == "LONG" else "VAL"
            }
        
        # エントリー時のVAL/VAHに基づく初期ストップとリスク幅Rの算出（最大損失を -3.5% に厳格制限、初動タイトストップ適用）
        if self.side == "LONG":
            base_val = float(entry_val) if (entry_val is not None and entry_val > 0) else (self.entry_price * 0.99)
            val_stop = base_val * (1.0 - self.initial_margin_pct)
            hard_stop = self.entry_price * (1.0 - self.max_hard_sl_pct)
            base_stop = max(val_stop, hard_stop)
            if self.initial_tight_sl_pct is not None:
                tight_stop = self.entry_price * (1.0 - self.initial_tight_sl_pct)
                self.current_stop = max(base_stop, tight_stop)
            else:
                self.current_stop = base_stop
            # 初期リスク幅 R (最低でも価格の0.5%を保証)
            self.risk_r = max(self.entry_price - self.current_stop, self.entry_price * 0.005)
            self.tp1 = self.entry_price + self.risk_r * 1.0       # +1.0R (建値移動ライン)
            self.tp2 = self.entry_price + self.risk_r * 2.0       # +2.0R (目標利益到達ライン)
            self.lock_sl = self.entry_price + self.risk_r * 1.2   # +1.2R (利益確定ロック水準)
            self.be_price = self.entry_price * (1.0 + self.fee_margin_pct)
        else:
            base_vah = float(entry_vah) if (entry_vah is not None and entry_vah > 0) else (self.entry_price * 1.01)
            vah_stop = base_vah * (1.0 + self.initial_margin_pct)
            hard_stop = self.entry_price * (1.0 + self.max_hard_sl_pct)
            base_stop = min(vah_stop, hard_stop)
            if self.initial_tight_sl_pct is not None:
                tight_stop = self.entry_price * (1.0 + self.initial_tight_sl_pct)
                self.current_stop = min(base_stop, tight_stop)
            else:
                self.current_stop = base_stop
            self.risk_r = max(self.current_stop - self.entry_price, self.entry_price * 0.005)
            self.tp1 = self.entry_price - self.risk_r * 1.0       # +1.0R
            self.tp2 = self.entry_price - self.risk_r * 2.0       # +2.0R
            self.lock_sl = self.entry_price - self.risk_r * 1.2   # +1.2R
            self.be_price = self.entry_price * (1.0 - self.fee_margin_pct)

    def update(
        self,
        current_price: float,
        close_price: float,
        val: float,
        poc: float,
        vah: float,
        high_price: Optional[float] = None,
        low_price: Optional[float] = None
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
        hi_px = float(high_price) if high_price is not None else cur_px
        lo_px = float(low_price) if low_price is not None else cur_px

        if self.side == "LONG":
            current_r = (cur_px - self.entry_price) / self.risk_r if self.risk_r > 0 else 0.0

            if self.strategy_type == "breakout":
                # ブレイクアウト戦略: 段階的リスクリワード・トレイリング
                if hi_px >= self.tp2 or self.step >= 2:
                    if self.step < 2:
                        self.step = 2
                    # 最低でも +1.2R にロック
                    self.current_stop = max(self.current_stop, self.lock_sl, self.be_price)
                    # さらなる上昇追従: 直下サポート(val_px または poc_px)がロックラインを上回り、かつ現在価格未満なら追従
                    trail_candidate = max(val_px, poc_px)
                    if trail_candidate > self.current_stop and trail_candidate < cur_px:
                        self.step = 3
                        self.current_stop = trail_candidate
                elif hi_px >= self.tp1 or self.step >= 1:
                    self.step = max(self.step, 1)
                    self.current_stop = max(self.current_stop, self.be_price)
                else:
                    # +1.0R未満の間は初期VALストップを厳格維持（ノイズ・押し目を耐える）
                    pass
            else:
                # レンジ戦略: 従来のPOC/VAHラチェット
                is_profitable = cur_px >= self.be_price
                if is_profitable:
                    if cur_px > vah_px:
                        self.step = 2
                        self.current_stop = max(self.current_stop, self.be_price, vah_px)
                    elif cur_px > poc_px or self.step >= 1:
                        self.step = max(self.step, 1)
                        target_sl = max(self.be_price, poc_px)
                        self.current_stop = max(self.current_stop, target_sl)
                else:
                    if self.step > 0:
                        self.current_stop = max(self.current_stop, self.be_price)

            # クローズ条件（終値判定）
            should_close = cls_px < self.current_stop
            step_name = self.step_names.get(self.step, f"STEP_{self.step}")
            reason = f"LONG_VP_{step_name}_BREAK" if should_close else "HOLD"

        else:  # SHORT
            current_r = (self.entry_price - cur_px) / self.risk_r if self.risk_r > 0 else 0.0

            if self.strategy_type == "breakout":
                # ブレイクアウト戦略: 段階的リスクリワード・トレイリング
                if lo_px <= self.tp2 or self.step >= 2:
                    if self.step < 2:
                        self.step = 2
                    # 最低でも +1.2R (entry - 1.2R) にロック
                    self.current_stop = min(self.current_stop, self.lock_sl, self.be_price)
                    # さらなる下落追従: 直上レジスタンス(vah_px または poc_px)がロックラインを下回り、かつ現在価格超なら追従
                    trail_candidate = min(vah_px, poc_px)
                    if trail_candidate < self.current_stop and trail_candidate > cur_px:
                        self.step = 3
                        self.current_stop = trail_candidate
                elif lo_px <= self.tp1 or self.step >= 1:
                    self.step = max(self.step, 1)
                    self.current_stop = min(self.current_stop, self.be_price)
                else:
                    pass
            else:
                is_profitable = cur_px <= self.be_price
                if is_profitable:
                    if cur_px < val_px:
                        self.step = 2
                        self.current_stop = min(self.current_stop, self.be_price, val_px) if self.current_stop > 0 else min(self.be_price, val_px)
                    elif cur_px < poc_px or self.step >= 1:
                        self.step = max(self.step, 1)
                        target_sl = min(self.be_price, poc_px)
                        self.current_stop = min(self.current_stop, target_sl) if self.current_stop > 0 else target_sl
                else:
                    if self.step > 0:
                        self.current_stop = min(self.current_stop, self.be_price) if self.current_stop > 0 else self.be_price

            should_close = cls_px > self.current_stop
            step_name = self.step_names.get(self.step, f"STEP_{self.step}")
            reason = f"SHORT_VP_{step_name}_BREAK" if should_close else "HOLD"

        info = {
            "step": self.step,
            "step_name": self.step_names.get(self.step, f"STEP_{self.step}"),
            "current_stop": self.current_stop,
            "close_price": cls_px,
            "current_price": cur_px,
            "current_r": current_r,
            "risk_r": self.risk_r,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "lock_sl": self.lock_sl,
            "be_price": self.be_price
        }
        return should_close, reason, info


def check_5m_breakout_signal(
    df_5m: pd.DataFrame,
    df_1h: Optional[pd.DataFrame] = None,
    vol_surge_threshold: float = 1.8,
    lookback_high_bars: int = 12
) -> Tuple[bool, str, Dict[str, Any]]:
    """5分足データに基づくリアルタイム・ブレイクアウト判定

    判定条件:
    1. 5分足の出来高急増（直近20本の平均出来高に対して vol_surge_threshold 倍以上）
    2. 直近レンジ高値（lookback_high_bars = 12本: 過去1時間分の最高値）突破、または 1H VAH 突破
    3. 5分足が陽線（close > open）かつ短期モメンタム確認（EMA9 > EMA21）

    Returns:
        (is_signal: bool, reason: str, metrics: Dict[str, Any])
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 25:
        return False, "5M_DATA_INSUFFICIENT", {}

    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    h = df_5m["high"].astype(float)
    l = df_5m["low"].astype(float)
    v = df_5m["volume"].astype(float)

    curr_c = float(c.iloc[-1])
    curr_o = float(o.iloc[-1])
    curr_v = float(v.iloc[-1])

    # 1. 出来高スパイク判定 (直近20本の平均に対して)
    vol_ma20 = float(v.iloc[-21:-1].mean()) if len(v) >= 21 else float(v.mean())
    vol_ratio = (curr_v / vol_ma20) if vol_ma20 > 0 else 1.0
    has_vol_surge = vol_ratio >= vol_surge_threshold

    # 2. 直近高値ブレイク判定
    h_slice = h.iloc[-(lookback_high_bars + 1):-1]
    recent_high = float(h_slice.max()) if len(h_slice) > 0 else curr_c
    breaks_recent_high = curr_c > recent_high

    # 1H VAH がある場合はそれも参照
    vah_1h = None
    breaks_vah = False
    if df_1h is not None and not df_1h.empty and "VAH" in df_1h.columns:
        vah_1h = float(df_1h["VAH"].iloc[-1])
        breaks_vah = curr_c > vah_1h

    # 3. 陽線判定
    is_bullish = curr_c > curr_o

    # 4. 短期EMA判定
    ema9 = float(c.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(c.ewm(span=21, adjust=False).mean().iloc[-1])
    ema_bullish = ema9 >= ema21

    is_breakout = (breaks_recent_high or breaks_vah) and has_vol_surge and is_bullish and ema_bullish

    metrics = {
        "curr_price": curr_c,
        "recent_high": recent_high,
        "vah_1h": vah_1h,
        "vol_ratio": vol_ratio,
        "vol_ma20": vol_ma20,
        "ema9": ema9,
        "ema21": ema21,
        "is_bullish": is_bullish,
        "has_vol_surge": has_vol_surge,
        "breaks_recent_high": breaks_recent_high,
        "breaks_vah": breaks_vah,
    }

    if is_breakout:
        trig_type = "VAH_AND_HIGH" if (breaks_vah and breaks_recent_high) else ("VAH" if breaks_vah else "LOCAL_HIGH")
        reason = f"5M_BREAKOUT_{trig_type}(Vol={vol_ratio:.1f}x, Px={curr_c:.4f} > High={recent_high:.4f})"
        return True, reason, metrics

    return False, "NO_SIGNAL", metrics


def check_5m_range_signal(
    df_5m: pd.DataFrame,
    df_1h: Optional[pd.DataFrame] = None,
    vol_surge_threshold: float = 1.4,
    val_proximity_pct: float = 0.015,
) -> Tuple[bool, str, Dict[str, Any]]:
    """5分足データに基づくレンジ戦略（RANGE LONG）用安値・下限反発スナイプ判定

    1時間足の環境認識:
    - 価格が 1H の VAL（出来高集中帯下限）付近、または下回る押し目ゾーンにあること (curr_c <= VAL * (1 + val_proximity_pct))
    - レンジ上限（VAH）近辺でのロングは完全禁止 (curr_c >= VAH * 0.985 は即NG)

    5分足の反発トリガー:
    1. 陽線（close > open）
    2. 出来高急増（直近20本平均の vol_surge_threshold 倍以上）または下ヒゲ反発（下ヒゲ長 >= 実体長 * 1.2）
    3. 安値切り上げ（直近数本の最安値から反発して close が上回っている）

    Returns:
        (is_signal: bool, reason: str, metrics: Dict[str, Any])
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 25:
        return False, "5M_DATA_INSUFFICIENT", {}

    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    h = df_5m["high"].astype(float)
    l = df_5m["low"].astype(float)
    v = df_5m["volume"].astype(float)

    curr_c = float(c.iloc[-1])
    curr_o = float(o.iloc[-1])
    curr_h = float(h.iloc[-1])
    curr_l = float(l.iloc[-1])
    curr_v = float(v.iloc[-1])

    # 1. 1時間足の環境認識
    val_1h = None
    vah_1h = None
    poc_1h = None
    in_val_zone = False
    too_high_near_vah = False

    if df_1h is not None and not df_1h.empty:
        if "VAL" in df_1h.columns:
            val_1h = float(df_1h["VAL"].iloc[-1])
        if "VAH" in df_1h.columns:
            vah_1h = float(df_1h["VAH"].iloc[-1])
        if "POC" in df_1h.columns:
            poc_1h = float(df_1h["POC"].iloc[-1])

    if val_1h is not None and val_1h > 0:
        # VAL から +val_proximity_pct 以内、またはそれ以下（安値圏）
        in_val_zone = curr_c <= (val_1h * (1.0 + val_proximity_pct))
    else:
        # 1H VAH/VALがない場合は直近48本の5分足（4時間分）の下位25%圏を判定
        lookback_4h = min(len(df_5m), 48)
        l_slice = l.iloc[-lookback_4h:]
        h_slice = h.iloc[-lookback_4h:]
        low_4h = float(l_slice.min()) if len(l_slice) > 0 else curr_c
        high_4h = float(h_slice.max()) if len(h_slice) > 0 else curr_c
        rng = high_4h - low_4h
        val_1h = low_4h + rng * 0.25
        vah_1h = high_4h - rng * 0.25
        in_val_zone = curr_c <= val_1h

    if vah_1h is not None and vah_1h > 0:
        # VAHから1.5%以内の高値圏はレンジ天井なので絶対にロング禁止
        too_high_near_vah = curr_c >= (vah_1h * 0.985)

    # 2. 5分足の反発判定
    is_bullish = curr_c > curr_o
    body = abs(curr_c - curr_o)
    lower_wick = (curr_o - curr_l) if is_bullish else (curr_c - curr_l)
    has_hammer_wick = (lower_wick >= body * 1.2) if body > 0 else (lower_wick > 0)

    vol_ma20 = float(v.iloc[-21:-1].mean()) if len(v) >= 21 else float(v.mean())
    vol_ratio = (curr_v / vol_ma20) if vol_ma20 > 0 else 1.0
    has_vol_surge = vol_ratio >= vol_surge_threshold

    # 直近3本の安値更新ストップ判定
    recent_min_low = float(l.iloc[-4:-1].min()) if len(l) >= 4 else curr_l
    holding_low = curr_c >= recent_min_low

    is_range_signal = in_val_zone and (not too_high_near_vah) and is_bullish and (has_vol_surge or has_hammer_wick) and holding_low

    metrics = {
        "curr_price": curr_c,
        "val_1h": val_1h,
        "vah_1h": vah_1h,
        "poc_1h": poc_1h,
        "in_val_zone": in_val_zone,
        "too_high_near_vah": too_high_near_vah,
        "vol_ratio": vol_ratio,
        "vol_ma20": vol_ma20,
        "is_bullish": is_bullish,
        "has_vol_surge": has_vol_surge,
        "has_hammer_wick": has_hammer_wick,
    }

    if is_range_signal:
        trig_type = "VAL_SURGE" if has_vol_surge else "VAL_HAMMER"
        val_str = f"${val_1h:.4f}" if val_1h else "N/A"
        reason = f"5M_RANGE_{trig_type}(Vol={vol_ratio:.1f}x, Px={curr_c:.4f} near VAL={val_str})"
        return True, reason, metrics

    return False, "NO_SIGNAL", metrics


def check_1h_crossover_condition(
    df_1h: Optional[pd.DataFrame],
    curr_price: float,
    strategy_type: str = "breakout",
    val_proximity_pct: float = 0.015
) -> Tuple[bool, str, Dict[str, Any]]:
    """1時間足の出来高プロファイル（VAL / VAH）に対するクロスオーバー判定

    - RANGE戦略: VALクロスオーバー（直近足がVAL以下から、現在価格がVAL以上へ反発上抜け）
      ※ レンジ上限（VAH）近辺（VAH * 0.985以上）での天井買いは100%ブロック。
    - BREAKOUT戦略: VAHクロスオーバー（直近足がVAH以下から、現在価格がVAHを上抜けブレイク）
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 2:
        return False, "1H_DATA_INSUFFICIENT", {}

    val_1h = float(df_1h["VAL"].iloc[-1]) if "VAL" in df_1h.columns else None
    vah_1h = float(df_1h["VAH"].iloc[-1]) if "VAH" in df_1h.columns else None
    poc_1h = float(df_1h["POC"].iloc[-1]) if "POC" in df_1h.columns else None

    # 直近足（確定前足）
    prev_c = float(df_1h["close"].iloc[-2]) if len(df_1h) >= 2 else curr_price
    prev_l = float(df_1h["low"].iloc[-2]) if len(df_1h) >= 2 else curr_price
    prev_h = float(df_1h["high"].iloc[-2]) if len(df_1h) >= 2 else curr_price

    strat = str(strategy_type).lower().strip()
    metrics = {
        "val_1h": val_1h, "vah_1h": vah_1h, "poc_1h": poc_1h,
        "curr_price": curr_price, "prev_close": prev_c, "prev_low": prev_l
    }

    if "range" in strat:
        if val_1h is None:
            return False, "RANGE_NO_VAL", metrics
        # 天井ブロック: VAH近辺でのロングは完全禁止
        if vah_1h is not None and curr_price >= (vah_1h * 0.985):
            return False, "RANGE_BLOCK_NEAR_VAH_CEILING", metrics

        # VALクロスオーバー判定: 直近足の安値/終値がVAL以下、かつ現在価格がVAL以上（反発）
        is_val_rebound = (prev_l <= val_1h * (1.0 + val_proximity_pct)) and (curr_price >= val_1h * 0.998)
        # またはVAL直下〜直上（+1.5%以内）の下限ゾーンで推移
        is_in_val_zone = (curr_price <= val_1h * (1.0 + val_proximity_pct)) and (curr_price >= val_1h * 0.97)

        if is_val_rebound or is_in_val_zone:
            reason = f"1H_RANGE_VAL_CROSSOVER(Px={curr_price:.4f} >= VAL={val_1h:.4f}, PrevLow={prev_l:.4f})"
            return True, reason, metrics
        return False, "RANGE_NOT_AT_VAL", metrics

    else:
        # BREAKOUT戦略
        if vah_1h is None:
            return False, "BREAKOUT_NO_VAH", metrics
        # VAHクロスオーバー判定: 直近足がVAH以下、かつ現在価格がVAHを突破
        is_vah_breakout = (prev_c <= vah_1h * 1.002) and (curr_price > vah_1h)
        # または直近高値更新
        recent_1h_high = float(df_1h["high"].iloc[-13:-1].max()) if len(df_1h) >= 13 else vah_1h
        is_high_breakout = (prev_c <= recent_1h_high) and (curr_price > recent_1h_high)

        if is_vah_breakout or is_high_breakout:
            reason = f"1H_BREAKOUT_VAH_CROSSOVER(Px={curr_price:.4f} > VAH={vah_1h:.4f})"
            return True, reason, metrics
        return False, "BREAKOUT_NOT_AT_VAH", metrics


def check_5m_momentum_trigger(
    df_5m: pd.DataFrame,
    vol_surge_threshold: float = 1.0,
    check_oi: bool = True
) -> Tuple[bool, str, Dict[str, Any]]:
    """5分足データに基づく勢い（モメンタム・流動性）の確認トリガー

    判定条件:
    1. 5分足が陽線 (close > open)
    2. 出来高が直近20本平均以上 (vol >= vol_ma20 * vol_surge_threshold)
    3. 短期モメンタム確認 (EMA9 >= EMA21 または 直近高値更新)
    4. OI（Open Interest）の増加（データが存在する場合）
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 20:
        return False, "5M_DATA_INSUFFICIENT", {}

    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    h = df_5m["high"].astype(float)
    l = df_5m["low"].astype(float)
    v = df_5m["volume"].astype(float)

    curr_c = float(c.iloc[-1])
    curr_o = float(o.iloc[-1])
    curr_v = float(v.iloc[-1])

    is_bullish = curr_c > curr_o

    vol_ma20 = float(v.iloc[-21:-1].mean()) if len(v) >= 21 else float(v.mean())
    vol_ratio = (curr_v / vol_ma20) if vol_ma20 > 0 else 1.0
    has_vol = vol_ratio >= vol_surge_threshold

    ema9 = float(c.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(c.ewm(span=21, adjust=False).mean().iloc[-1])
    ema_bullish = ema9 >= ema21

    # 直近3本の高値更新
    recent_high_3 = float(h.iloc[-4:-1].max()) if len(h) >= 4 else curr_c
    making_higher_high = curr_c >= recent_high_3 * 0.999

    # OI判定（カラムがある場合）
    oi_bullish = True
    oi_col = next((col for col in ["openInterest", "open_interest", "oi"] if col in df_5m.columns), None)
    if check_oi and oi_col:
        try:
            curr_oi = float(df_5m[oi_col].iloc[-1])
            prev_oi = float(df_5m[oi_col].iloc[-2]) if len(df_5m) >= 2 else curr_oi
            if curr_oi < prev_oi * 0.999:
                oi_bullish = False  # OI減少（買い手の撤退）ならブロック
        except Exception:
            pass

    has_momentum = is_bullish and has_vol and (ema_bullish or making_higher_high) and oi_bullish

    metrics = {
        "curr_price": curr_c,
        "is_bullish": is_bullish,
        "vol_ratio": vol_ratio,
        "vol_ma20": vol_ma20,
        "ema_bullish": ema_bullish,
        "making_higher_high": making_higher_high,
        "oi_bullish": oi_bullish
    }

    if has_momentum:
        reason = f"5M_MOMENTUM_CONFIRMED(Vol={vol_ratio:.1f}x, BullishCandle, EMA={ema_bullish})"
        return True, reason, metrics

    return False, "5M_LACKS_MOMENTUM", metrics


def check_mtf_execution_signal(
    df_5m: pd.DataFrame,
    df_1h: Optional[pd.DataFrame],
    strategy_type: str = "breakout",
    vol_surge_threshold: float = 1.0
) -> Tuple[bool, str, Dict[str, Any]]:
    """1時間足クロスオーバー（大前提）× 5分足勢い確認（執行トリガー）の統合判定"""
    if df_5m is None or df_5m.empty:
        return False, "NO_5M_DATA", {}

    curr_px = float(df_5m["close"].iloc[-1])

    # 1. 1時間足の節目クロスオーバー判定
    is_1h_crossover, reason_1h, metrics_1h = check_1h_crossover_condition(
        df_1h=df_1h, curr_price=curr_px, strategy_type=strategy_type
    )
    if not is_1h_crossover:
        return False, reason_1h, metrics_1h

    # 2. 5分足の勢い確認トリガー
    is_5m_momentum, reason_5m, metrics_5m = check_5m_momentum_trigger(
        df_5m=df_5m, vol_surge_threshold=vol_surge_threshold
    )
    if not is_5m_momentum:
        return False, f"{reason_1h} BUT {reason_5m}", {**metrics_1h, **metrics_5m}

    strat_upper = str(strategy_type).upper()
    full_reason = f"MTF_{strat_upper}_SNIPE: {reason_1h} & {reason_5m}"
    combined_metrics = {**metrics_1h, **metrics_5m, "curr_price": curr_px}
    return True, full_reason, combined_metrics


def check_5m_fast_exit_condition(
    df_5m: pd.DataFrame,
    df_1h: Optional[pd.DataFrame],
    curr_price: float,
    entry_price: float,
    strategy_type: str = "breakout",
    current_stop_price: Optional[float] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """5分足データに基づく急落・早期クローズ判定（下げの勢いが強い時の即時終了）

    判定条件:
    1. 防衛ライン（BREAKOUT: VAH割れ / RANGE: VAL割れまたはPOC達成後のPOC割れ / またはTrailing Stop割れ）
    2. 5分足で下げの勢いが強い:
       - 5分足が陰線 (close < open)
       - かつ 出来高急増（直近20本平均の 1.2倍以上の売り浴びせ）
       - または 大陰線（実体長が直近平均レンジの1.2倍以上）

    Returns:
        (should_exit: bool, reason: str, metrics: Dict[str, Any])
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 10:
        return False, "5M_DATA_INSUFFICIENT", {}

    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    v = df_5m["volume"].astype(float)

    curr_c = float(c.iloc[-1])
    curr_o = float(o.iloc[-1])
    curr_v = float(v.iloc[-1])

    # 1. 防衛ラインの特定
    val_1h = float(df_1h["VAL"].iloc[-1]) if (df_1h is not None and not df_1h.empty and "VAL" in df_1h.columns) else None
    vah_1h = float(df_1h["VAH"].iloc[-1]) if (df_1h is not None and not df_1h.empty and "VAH" in df_1h.columns) else None
    poc_1h = float(df_1h["POC"].iloc[-1]) if (df_1h is not None and not df_1h.empty and "POC" in df_1h.columns) else None

    strat = str(strategy_type).lower().strip()
    defense_line = None
    line_name = "TRAILING_STOP"

    if "breakout" in strat and vah_1h is not None:
        # ブレイクアウト銘柄: VAHそのものではなく、健全な押し目（リテスト）を許容するバッファ（-0.8%）を付与して早漏損切りを防止
        defense_line = vah_1h * 0.992
        line_name = "VAH_DEFENSE(BUFFERED)"
    elif "range" in strat:
        if poc_1h is not None and curr_price > poc_1h:
            defense_line = poc_1h * 0.995
            line_name = "POC_DEFENSE"
        elif val_1h is not None:
            defense_line = val_1h * 0.992
            line_name = "VAL_DEFENSE"

    if current_stop_price is not None and current_stop_price > 0:
        if defense_line is None or current_stop_price > defense_line:
            defense_line = current_stop_price
            line_name = "TRAILING_SL_DEFENSE"

    if defense_line is None:
        defense_line = entry_price * 0.98  # フォールバック: -2.0%

    # 防衛ラインを下回っているか（クロスアンダー）
    is_under_defense = curr_c < defense_line

    # 2. 5分足の下げの勢い判定（通常ノイズを除外し、明確な売り浴びせのみ検知して利確を伸ばす）
    is_bearish = curr_c < curr_o
    body = abs(curr_c - curr_o)
    vol_ma20 = float(v.iloc[-21:-1].mean()) if len(v) >= 21 else float(v.mean())
    vol_ratio = (curr_v / vol_ma20) if vol_ma20 > 0 else 1.0
    # 出来高比率を 1.2倍 -> 2.0倍 に引き上げ（明確な売り浴びせのみ検知）
    has_heavy_sell_vol = vol_ratio >= 2.0

    # 平均レンジ（直近10本の実体）
    recent_bodies = (c.iloc[-11:-1] - o.iloc[-11:-1]).abs()
    avg_body = float(recent_bodies.mean()) if len(recent_bodies) > 0 else body
    # 大陰線判定を 1.2倍 -> 1.8倍 に引き上げ
    is_large_red = is_bearish and (body >= avg_body * 1.8)

    strong_downward_momentum = is_bearish and (has_heavy_sell_vol or is_large_red)

    # さらに確認に確認を重ねる: 直前の確定5分足（iloc[-2]）も陰線または高値切り下げ傾向であることを確認
    prev_weak = (float(c.iloc[-2]) <= float(o.iloc[-2])) if len(c) >= 2 else True
    should_exit = is_under_defense and strong_downward_momentum and prev_weak

    metrics = {
        "curr_price": curr_c,
        "defense_line": defense_line,
        "line_name": line_name,
        "is_under_defense": is_under_defense,
        "is_bearish": is_bearish,
        "vol_ratio": vol_ratio,
        "has_heavy_sell_vol": has_heavy_sell_vol,
        "is_large_red": is_large_red,
    }

    if should_exit:
        reason = f"5M_FAST_EXIT: {line_name}割れ (${curr_c:.4f} < ${defense_line:.4f}) ＋ 強い下げ勢い(Vol={vol_ratio:.1f}x, 大陰線)"
        return True, reason, metrics

    return False, "HOLD_SAFE", metrics


# 後方互換性エイリアス
check_5m_entry_signal = check_mtf_execution_signal


# ==============================================================================
# 【TradingView完全移植】RSIMA3_MaxGambleMoon
# ==============================================================================

# ロット倍率 (ユーザー指示: ロット倍率撤廃、全て1倍固定)
FIBO_LOT_MULTIPLIERS = [1] * 11

def get_tv_fibo_lot(opentrades: int, base_lot: float) -> float:
    """ナンピンロット計算（全て1倍固定）"""
    return base_lot * 1.0


def get_tv_add_pct(position_count: int, pct1: float = 0.004, pct2: float = 0.006, pct3: float = 0.008, pct4: float = 0.010) -> float:
    """TradingView get_add_pct(strategy.position_size) と完全一致するナンピン間隔（2倍化: 0.4%〜1.0%）"""
    n = int(position_count)
    if n <= 3:
        return pct1  # 0.004 (0.4%)
    elif n <= 5:
        return pct2  # 0.006 (0.6%)
    elif n <= 8:
        return pct3  # 0.008 (0.8%)
    else:
        return pct4  # 0.010 (1.0%)


def calc_rsi(series: pd.Series, period: int = 9) -> pd.Series:
    """TradingView ta.rsi(close, 9) と完全一致する Wilder's Smoothing RSI
    
    TradingViewの実装:
    up = ta.rma(math.max(ta.change(src), 0), len)
    down = ta.rma(-math.min(ta.change(src), 0), len)
    rsi = down == 0 ? 100 : up == 0 ? 0 : 100 - (100 / (1 + up / down))
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # ta.rma は alpha = 1 / period の 指数平滑化
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # ゼロ除算対策
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # down == 0 ? 100 : up == 0 ? 0
    rsi = rsi.where(avg_loss > 0.0, 100.0)
    rsi = rsi.where(avg_gain > 0.0, 0.0)
    return rsi.fillna(50.0)


def calc_rsi_ema(
    series: pd.Series,
    rsi_period: int = 9,
    ma_period: int = 7
) -> Tuple[pd.Series, pd.Series]:
    """TradingView lrsiMA = ma(rsi, 7, 'EMA') と完全一致する RSI & RSI-EMA"""
    rsi = calc_rsi(series, period=rsi_period)
    lrsi_ma = rsi.ewm(span=ma_period, adjust=False).mean()
    return rsi, lrsi_ma


def check_rsima3_signals(
    df_5m: pd.DataFrame,
    current_pos_qty: float,
    current_trades_count: int,
    avg_entry_price: float,
    current_price: float,
    rsi_len: int = 9,
    ema_len: int = 7,
    l_ep: float = 40.0,
    l_cp: float = 60.0,
    sl_pct: float = 0.090,
    max_trades: int = 3,
    last_entry_price: Optional[float] = None,
    val_price: Optional[float] = None,
    vah_price: Optional[float] = None,
    is_cooldown_active: bool = False,
    is_emergency_breakeven: bool = False,
    dynamic_sl_pct: float = -0.025,
    bars_held: int = 0,
    use_ema_filter: bool = False,
    timeout_bars: int = 48,
) -> Dict[str, Any]:
    """TradingView『RSIMA3_MaxGambleMoon』の完全移植判定 (新仕様最適化完全連動)
    
    Returns:
        dict:
            action: 'INITIAL_ENTRY' | 'ADD_ENTRY' | 'CLOSE_ALL' | 'STOP_LOSS' | 'HOLD' | 'NONE'
            reason: str
            lot_multiplier: float
            metrics: dict
    """
    if df_5m is None or df_5m.empty or len(df_5m) < rsi_len + ema_len + 5:
        return {"action": "NONE", "reason": "5M_DATA_INSUFFICIENT", "lot_multiplier": 1.0, "metrics": {}}

    c = df_5m["close"].astype(float)
    rsi, lrsi_ma = calc_rsi_ema(c, rsi_period=rsi_len, ma_period=ema_len)

    curr_rsi = float(rsi.iloc[-1])
    prev_rsi = float(rsi.iloc[-2])
    curr_ma = float(lrsi_ma.iloc[-1])
    prev_ma = float(lrsi_ma.iloc[-2])

    prev_close = float(c.iloc[-2]) if len(c) >= 2 else current_price

    # 1. ゴールデンクロス判定: LE = ta.crossover(rsi, lrsiMA)
    is_crossover_curr = (curr_rsi > curr_ma) and (prev_rsi <= prev_ma)
    prev2_rsi = float(rsi.iloc[-3]) if len(rsi) >= 3 else prev_rsi
    prev2_ma = float(lrsi_ma.iloc[-3]) if len(lrsi_ma) >= 3 else prev_ma
    is_crossover_prev = (prev_rsi > prev_ma) and (prev2_rsi <= prev2_ma) and (curr_rsi >= curr_ma)

    LE = is_crossover_curr or is_crossover_prev

    # 2. 水準判定: LC = lrsiMA < lEp and strategy.opentrades < op_len (最大3枠: 初期1本+ナンピン2回)
    LC = (curr_ma < l_ep or prev_ma < l_ep) and (current_trades_count < max_trades)

    lpo = (current_pos_qty > 0)
    poz = (current_pos_qty <= 0)

    # ナンピン幅判定 (ユーザー新指示: 直近の約定価格から (VAH - VAL) * 0.50 以上下落)
    last_ep = last_entry_price if (last_entry_price is not None and last_entry_price > 0) else avg_entry_price
    if val_price is not None and vah_price is not None and vah_price > val_price and last_ep > 0:
        va_dip_req = (vah_price - val_price) * 0.50
        longadd = (prev_close <= last_ep - va_dip_req) or (current_price <= last_ep - va_dip_req)
        # 利確用の add_pct (+0.4%〜+1.0%) とナンピン幅を分離
        tp_add_pct = get_tv_add_pct(max(current_trades_count, 1))
    else:
        va_dip_req = last_ep * 0.010 if last_ep > 0 else 0.0
        tp_add_pct = get_tv_add_pct(max(current_trades_count, 1))
        longadd = (prev_close < avg_entry_price * (1.0 - tp_add_pct)) if (avg_entry_price > 0 and lpo) else True

    # shortadd = avg_price ? close[1] > strategy.position_avg_price * (1 + addPct) : true
    shortadd = (prev_close > avg_entry_price * (1.0 + tp_add_pct)) if (avg_entry_price > 0 and lpo) else False
    shortadd_realtime = (current_price > avg_entry_price * (1.0 + tp_add_pct)) if (avg_entry_price > 0 and lpo) else False

    # LRS = rsi > lCp (rsi > 60)
    LRS = (curr_rsi > l_cp)

    # base_long_exit = LRS and shortadd_realtime (必ずリアルタイム現在価格が利確ライン超えであることを確認)
    base_long_exit = LRS and shortadd_realtime

    metrics = {
        "curr_price": current_price,
        "prev_close": prev_close,
        "avg_entry_price": avg_entry_price,
        "last_entry_price": last_ep,
        "tp_add_pct": tp_add_pct,
        "va_dip_req": va_dip_req,
        "rsi": curr_rsi,
        "lrsi_ma": curr_ma,
        "prev_rsi": prev_rsi,
        "prev_ma": prev_ma,
        "LE": LE,
        "LC": LC,
        "opentrades": current_trades_count,
        "longadd": longadd,
        "shortadd": shortadd_realtime,
        "LRS": LRS,
        "is_emergency_breakeven": is_emergency_breakeven,
    }

    # === [0] 動的 MAE ストップロス判定 (直近30d MAE 95%ile 割れ: 枠数不問常時発動) ===
    # PROJECT_RULES: 1枠目でもナンピン後でも常時発動、ナンピン凍結中の無防備な塩漬け(デッドロック)を完全根絶
    if lpo and avg_entry_price > 0:
        eff_sl_pct = abs(dynamic_sl_pct) if dynamic_sl_pct != 0 else 0.025
        sl_price = avg_entry_price * (1.0 - eff_sl_pct)
        if current_price <= sl_price:
            loss_pct = (current_price - avg_entry_price) / avg_entry_price
            reason = f"RSIMA3_DYNAMIC_MAE_SL: 価格(${current_price:.4f}) <= 動的SL(${sl_price:.4f}, SL率-{eff_sl_pct:.2%}, 損失{loss_pct:.2%})"
            return {"action": "STOP_LOSS", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [0-1] 動的タイムアウト微損撤退判定 (timeout_bars本経過で含み損なら成行撤退) ===
    # PROJECT_RULES: バックテスト保有時間最適化(timeout_bars本)経過で含み損ならモメンタム喪失とみなし微損撤退
    if lpo and bars_held >= timeout_bars and avg_entry_price > 0 and current_price < avg_entry_price:
        loss_pct = (current_price - avg_entry_price) / avg_entry_price
        reason = f"RSIMA3_TIMEOUT_EXIT: 保有タイムアウト({bars_held}本>={timeout_bars}本) & 含み損({loss_pct:.2%}) ➔ 動的タイムアウト成行微損撤退"
        return {"action": "TIMEOUT_EXIT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-0] 地合い悪化時の緊急建値撤退判定 (全体地合い非BUY時: 平均建値 +0.20% で早期撤退) ===
    if lpo and is_emergency_breakeven and avg_entry_price > 0:
        breakeven_target = avg_entry_price * 1.0020  # +0.20% (往復手数料+スリップ考慮で確実にプラス)
        if current_price >= breakeven_target:
            pnl_pct = (current_price - avg_entry_price) / avg_entry_price
            reason = f"RSIMA3_EMERGENCY_BREAKEVEN_TP: 地合い悪化(非BUY)に伴う緊急建値撤退 (目標: ${breakeven_target:.4f}, PnL: {pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1] エグジット判定 (一括利確: strategy.close_all) ===
    # ロング保有中に、RSI > l_cp かつ 平均建値から +tp_add_pct 以上で全決済
    if lpo and base_long_exit:
        pnl_pct = (current_price - avg_entry_price) / avg_entry_price if avg_entry_price > 0 else 0.0
        reason = f"RSIMA3_NORMAL_TP: RSI({curr_rsi:.1f} > {l_cp:.0f}) & PnL(+{pnl_pct:.2%} >= +{tp_add_pct:.2%})"
        return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # EMA 200 トレンドフィルター判定 (use_ema_filter=True 時のみ必須)
    curr_c = float(c.iloc[-1])
    ema200 = calc_ema(c, 200)
    curr_ema200 = float(ema200.iloc[-1]) if len(ema200) > 0 else curr_c
    trend_ok = (curr_c >= curr_ema200) if use_ema_filter else True

    # === [2] 新規エントリー判定 (if LE and LC and poz and 3要素フィルター) ===
    if poz and not is_cooldown_active and LE and LC and trend_ok:
        prec_ok, prec_msg, prec_info = evaluate_entry_precision_gate(df_5m, side="long", is_vp=False)
        metrics.update(prec_info)
        if prec_ok:
            lot_m = 1.0  # 全部1倍 (ユーザー指示: ロット倍率撤廃)
            ema_str = f" & EMA200(${curr_ema200:.4f})条件OK" if use_ema_filter else ""
            strat_tag = "RSIMA_EMA" if use_ema_filter else "RSIMA"
            reason = f"{strat_tag}_INITIAL_ENTRY: GC(RSI={curr_rsi:.1f} > MA={curr_ma:.1f} < {l_ep:.0f}){ema_str} [{prec_msg}]"
            return {"action": "INITIAL_ENTRY", "reason": reason, "lot_multiplier": float(lot_m), "metrics": metrics}
        else:
            return {"action": "NONE", "reason": f"RSIMA3_PRECISION_FILTER_WAIT: {prec_msg}", "lot_multiplier": 1.0, "metrics": metrics}

    # === [3] ナンピン追加エントリー判定 (if LE and LC and lpo and longadd) ===
    # 地合い悪化時 (is_emergency_breakeven=True) は追加ナンピンを即時凍結 (ADD禁止)
    if lpo and not is_emergency_breakeven and LE and LC and longadd and (current_trades_count < max_trades):
        lot_m = 1.0  # 全部1倍 (ユーザー指示: ロット倍率撤廃)
        diff_pct = (current_price - last_ep) / last_ep if last_ep > 0 else 0.0
        dip_target_pct = (va_dip_req / last_ep) if last_ep > 0 else tp_add_pct
        reason = f"RSIMA3_ADD_ENTRY (Trade #{current_trades_count+1}, 1x): GC & Dip({diff_pct:.2%} <= -{dip_target_pct:.2%})"
        return {"action": "ADD_ENTRY", "reason": reason, "lot_multiplier": float(lot_m), "metrics": metrics}

    if lpo:
        return {"action": "HOLD", "reason": "RSIMA3_HOLD", "lot_multiplier": 1.0, "metrics": metrics}

    return {"action": "NONE", "reason": "RSIMA3_WAIT", "lot_multiplier": 1.0, "metrics": metrics}


# 後方互換性エイリアス
def check_5m_rsi_scalp_entry(df_5m, rsi_period=9, ma_period=7, max_rsima_threshold=40.0):
    sig = check_rsima3_signals(df_5m, current_pos_qty=0.0, current_trades_count=0, avg_entry_price=0.0, current_price=float(df_5m["close"].iloc[-1]) if df_5m is not None and not df_5m.empty else 0.0, rsi_len=rsi_period, ema_len=ma_period, l_ep=max_rsima_threshold)
    is_ok = sig["action"] == "INITIAL_ENTRY"
    return is_ok, sig["reason"], sig["metrics"]

def check_5m_rsi_scalp_exit(df_5m, curr_price, entry_price, rsi_period=9, ma_period=7, min_rsi_exit=60.0):
    sig = check_rsima3_signals(df_5m, current_pos_qty=1.0, current_trades_count=1, avg_entry_price=entry_price, current_price=curr_price, rsi_len=rsi_period, ema_len=ma_period, l_cp=min_rsi_exit)
    is_exit = sig["action"] == "CLOSE_ALL"
    return is_exit, sig["reason"], sig["metrics"]


# ==============================================================================
# 【BingX実証黄金最適化】ボリュームプロファイル VAL_POC ＋ EMA 200 戦略
# ==============================================================================

def check_val_poc_signals(
    df_5m: pd.DataFrame,
    current_pos_qty: float,
    current_trades_count: int,
    avg_entry_price: float,
    last_entry_price: float,
    current_price: float,
    vp_period: int = 150,
    num_bins: int = 50,
    value_area_pct: float = 0.70,
    ma_type: str = "EMA",
    ma_condition: str = "close_above",
    max_trades: int = 3,
    target_ratio: float = 0.70,
    min_va_width: float = 0.015,
    nanpin_drop_pct: float = 0.030,
    sl_pct: float = 0.090,
    is_cooldown_active: bool = False,
    is_emergency_breakeven: bool = False,
    dynamic_sl_pct: float = -0.025,
    bars_held: int = 0,
    use_ema_filter: bool = True,
    timeout_bars: int = 48,
) -> Dict[str, Any]:
    """VAL_POC ＋ 200期間MA (EMA/SMA) トレンドフィルター戦略 リアルタイム判定 (新仕様最適化完全連動)
    1. エントリー: VALゴールデンクロス (クロスオーバー) ＆ 200期間MA条件 ＆ バリュー幅>=1.5% ＆ クールダウン解除済
    2. 利確: VAL-VAH中間値50%水準 (VAL + (VAH-VAL)*target_ratio, デフォルト50%) 到達かつ建値プラス域
    3. ナンピン: 最後の建値から (VAH-VAL)*0.50 下落 (最大2回追加、初期1本＋ナンピン2回＝計3枠)
    4. 損切り: 最終建値から (VAH-VAL)*0.50 割れ (5分足終値確定判定) ＋ 24時間クールダウン
    5. 緊急建値撤退: 全体地合い非BUY時、平均建値+0.20%到達で即座に早期撤退 (ナンピンは即時凍結)
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 20:
        return {"action": "NONE", "reason": "5M_DATA_INSUFFICIENT", "lot_multiplier": 1.0, "metrics": {}}

    c = df_5m["close"].astype(float)
    h = df_5m["high"].astype(float) if "high" in df_5m.columns else c
    l = df_5m["low"].astype(float) if "low" in df_5m.columns else c

    # 1. 200期間 MA (EMA または SMA) 算出
    if str(ma_type).upper() == "SMA":
        ma200 = c.rolling(200, min_periods=1).mean()
    else:
        ma200 = calc_ema(c, 200)
    curr_ma200 = float(ma200.iloc[-1])
    prev_ma200 = float(ma200.iloc[-2]) if len(ma200) >= 2 else curr_ma200

    # 2. Market Profile 算出 (VAL, VAH, POC: リアルタイム監視では直近2本のみ算出して高速化)
    if "VAL" not in df_5m.columns or "POC" not in df_5m.columns or "VAH" not in df_5m.columns or df_5m["VAL"].isna().all():
        logic = logicinstance()
        df_mp = logic.make_market_profile(df_5m, period=vp_period, num_bins=num_bins, value_area_pct=value_area_pct, tail_only=2)
        val_s = df_mp["VAL"]
        vah_s = df_mp["VAH"]
        poc_s = df_mp["POC"]
    else:
        val_s = df_5m["VAL"]
        vah_s = df_5m["VAH"]
        poc_s = df_5m["POC"]

    curr_val = float(val_s.iloc[-1])
    prev_val = float(val_s.iloc[-2]) if len(val_s) >= 2 else curr_val
    curr_vah = float(vah_s.iloc[-1])
    curr_poc = float(poc_s.iloc[-1])

    curr_c = float(c.iloc[-1])
    prev_c = float(c.iloc[-2]) if len(c) >= 2 else curr_c
    curr_h = float(h.iloc[-1])
    curr_l = float(l.iloc[-1])

    va_width_pct = ((curr_vah - curr_val) / curr_val) if curr_val > 0 else 0.0
    val_gc = (prev_c <= prev_val) and (curr_c > curr_val)

    # トレンド条件判定: use_ema_filter が False の場合はフィルターなし (Pure VAL_POC)
    cond = str(ma_condition).lower()
    if not use_ema_filter or cond == "none" or str(ma_type).upper() == "NONE":
        trend_ok = True
    elif cond == "ma_rising":
        trend_ok = (curr_ma200 > prev_ma200)
    elif cond == "both":
        trend_ok = (curr_c >= curr_ma200) and (curr_ma200 > prev_ma200)
    else:  # "close_above" (デフォルト)
        trend_ok = (curr_c >= curr_ma200) if not np.isnan(curr_ma200) else True

    # 利確目標価格: VAL + (VAH - VAL) * 0.50
    va_diff = (curr_vah - curr_val) if (curr_vah > curr_val and curr_val > 0) else 0.0
    va_spacing = va_diff * target_ratio if va_diff > 0 else 0.0
    target_tp = curr_val + va_spacing if va_spacing > 0 else curr_poc

    ref_last_px = last_entry_price if last_entry_price > 0 else avg_entry_price
    # 安全下限ガード (最低 1.0%)
    drop_dist = max(ref_last_px * 0.010, va_spacing) if (ref_last_px > 0 and va_spacing > 0) else (ref_last_px * nanpin_drop_pct)
    # 最終建値から (VAH - VAL) * 0.50 割れ
    sl_price = ref_last_px - drop_dist if ref_last_px > 0 else 0.0
    nanpin_target = ref_last_px - drop_dist if ref_last_px > 0 else 0.0

    lpo = (current_pos_qty > 0)
    poz = (current_pos_qty <= 0)

    metrics = {
        "curr_price": current_price,
        "curr_c": curr_c,
        "prev_c": prev_c,
        "val": curr_val,
        "prev_val": prev_val,
        "vah": curr_vah,
        "poc": curr_poc,
        "ema200": curr_ma200,
        "ma200": curr_ma200,
        "va_width_pct": va_width_pct,
        "val_gc": val_gc,
        "trend_ok": trend_ok,
        "target_tp": target_tp,
        "sl_price": sl_price,
        "nanpin_target": nanpin_target,
        "drop_dist": drop_dist,
        "avg_entry_price": avg_entry_price,
        "last_entry_price": last_entry_price,
        "trades_count": current_trades_count,
        "is_emergency_breakeven": is_emergency_breakeven,
    }

    # === [1] 動的 MAE ストップロス判定 (直近30d MAE 95%ile 割れ: 枠数不問常時発動) ===
    # PROJECT_RULES: 1枠目でもナンピン後でも常時発動、ナンピン凍結中の無防備な塩漬け(デッドロック)を完全根絶
    if lpo and avg_entry_price > 0:
        eff_sl_pct = abs(dynamic_sl_pct) if dynamic_sl_pct != 0 else 0.025
        sl_price_dyn = avg_entry_price * (1.0 - eff_sl_pct)
        if current_price <= sl_price_dyn:
            trigger_px = current_price
            loss_pct = (trigger_px - avg_entry_price) / avg_entry_price
            reason = f"VAL_POC_DYNAMIC_MAE_SL: 価格(${trigger_px:.4f}) <= 動的SL(${sl_price_dyn:.4f}, SL率-{eff_sl_pct:.2%}, 損失{loss_pct:.2%})"
            return {"action": "STOP_LOSS", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-1] 動的タイムアウト微損撤退判定 (timeout_bars本経過で含み損なら成行撤退) ===
    # PROJECT_RULES: バックテスト保有時間最適化(timeout_bars本)経過で含み損ならモメンタム喪失とみなし微損撤退
    if lpo and bars_held >= timeout_bars and avg_entry_price > 0 and current_price < avg_entry_price:
        loss_pct = (current_price - avg_entry_price) / avg_entry_price
        reason = f"VAL_POC_TIMEOUT_EXIT: 保有タイムアウト({bars_held}本>={timeout_bars}本) & 含み損({loss_pct:.2%}) ➔ 動的タイムアウト成行微損撤退"
        return {"action": "TIMEOUT_EXIT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-0] 地合い悪化時の緊急建値撤退判定 (全体地合い非BUY時: 平均建値 +0.20% で早期撤退) ===
    if lpo and is_emergency_breakeven and avg_entry_price > 0:
        breakeven_target = avg_entry_price * 1.0020  # +0.20% (往復手数料+スリップ考慮で確実にプラス)
        if current_price >= breakeven_target:
            pnl_pct = (current_price - avg_entry_price) / avg_entry_price
            reason = f"VAL_POC_EMERGENCY_BREAKEVEN_TP: 地合い悪化(非BUY)に伴う緊急建値撤退 (目標: ${breakeven_target:.4f}, PnL: {pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [2] 利確判定 (VAL-VAH 最適比率水準 到達かつ建値+0.40%以上利益) ===
    if lpo and target_tp > 0:
        is_tp_hit = (current_price >= target_tp)
        is_profit = (current_price >= avg_entry_price * 1.0040)
        if is_tp_hit and is_profit:
            pnl_pct = (current_price - avg_entry_price) / avg_entry_price if avg_entry_price > 0 else 0.0
            reason = f"VAL_POC_TP (TR={target_ratio:.2f}): 目標(${target_tp:.4f})到達 & 建値利益(PnL={pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [3] 新規エントリー判定 (VALゴールデンクロス ＆ MA200 ＆ バリュー幅>=1.5% ＆ 3要素フィルター ＆ クールダウン解除) ===
    if poz and not is_cooldown_active:
        if val_gc and (va_width_pct >= min_va_width) and trend_ok:
            prec_ok, prec_msg, prec_info = evaluate_entry_precision_gate(df_5m, side="long", is_vp=True)
            metrics.update(prec_info)
            if prec_ok:
                strat_tag = "VAL_POC_EMA" if (use_ema_filter and str(ma_condition).lower() != "none" and str(ma_type).upper() != "NONE") else "VAL_POC"
                ema_str = f" & {ma_type}200(${curr_ma200:.4f})条件OK" if "EMA" in strat_tag else ""
                reason = f"{strat_tag}_INITIAL_ENTRY: VALブレイク(${curr_c:.4f} > ${curr_val:.4f}){ema_str} & 幅({va_width_pct:.1%}>=1.5%) [{prec_msg}]"
                return {"action": "INITIAL_ENTRY", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}
            else:
                return {"action": "NONE", "reason": f"VAL_POC_PRECISION_FILTER_WAIT: {prec_msg}", "lot_multiplier": 1.0, "metrics": metrics}

    # === [4] ナンピン判定 (最終建値から (VAH - VAL) * 0.50 下落、最大2回追加 = 計3枠) ===
    # 地合い悪化時 (is_emergency_breakeven=True) は追加ナンピンを即時凍結 (ADD禁止)
    if lpo and not is_emergency_breakeven and (1 <= current_trades_count < max_trades) and ref_last_px > 0 and nanpin_target > 0:
        if (curr_c <= nanpin_target) or (curr_l <= nanpin_target) or (current_price <= nanpin_target):
            drop_pct = (current_price - ref_last_px) / ref_last_px
            va_pct_drop = (drop_dist / ref_last_px) * 100.0
            reason = f"VAL_POC_ADD_ENTRY (Trade #{current_trades_count+1}, 1x): 最終建値(${ref_last_px:.4f})からVA50%下落({drop_pct:.2%} <= -{va_pct_drop:.1f}%)"
            return {"action": "ADD_ENTRY", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    if lpo:
        return {"action": "HOLD", "reason": "VAL_POC_HOLD", "lot_multiplier": 1.0, "metrics": metrics}

    return {"action": "NONE", "reason": "VAL_POC_WAIT", "lot_multiplier": 1.0, "metrics": metrics}


def check_vah_poc_signals(
    df_5m: pd.DataFrame,
    current_pos_qty: float,
    current_trades_count: int,
    avg_entry_price: float,
    last_entry_price: float,
    current_price: float,
    vp_period: int = 150,
    num_bins: int = 50,
    value_area_pct: float = 0.70,
    ma_type: str = "EMA",
    ma_condition: str = "close_below",
    max_trades: int = 1,
    target_ratio: float = 0.70,
    min_va_width: float = 0.015,
    sl_pct: float = 0.090,
    is_cooldown_active: bool = False,
    is_emergency_breakeven: bool = False,
    dynamic_sl_pct: float = 0.025,
    bars_held: int = 0,
    use_ema_filter: bool = True,
    timeout_bars: int = 48,
) -> Dict[str, Any]:
    """VAH_POC ＋ 200期間MA (EMA/SMA) ショート戦略 リアルタイム判定 (単発1本固定・ナンピン完全なし)
    1. エントリー: VAHデッドクロス (クロスアンダー) ＆ 200期間MA条件 (close <= EMA200) ＆ バリュー幅>=1.5% ＆ クールダウン解除済
    2. 利確: VAH-VAL中間値50%水準 (VAH - (VAH-VAL)*target_ratio) 到達かつ建値プラス利益圏
    3. 損切り: 動的MAEストップ (価格上昇逆行) または 4時間タイムアウト微損撤退
    4. 緊急建値撤退: 全体地合い非BEAR時、平均建値 -0.20% 到達で即座に早期撤退
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 20:
        return {"action": "NONE", "reason": "5M_DATA_INSUFFICIENT", "lot_multiplier": 1.0, "metrics": {}}

    c = df_5m["close"].astype(float)
    h = df_5m["high"].astype(float) if "high" in df_5m.columns else c
    l = df_5m["low"].astype(float) if "low" in df_5m.columns else c

    # 1. 200期間 MA 算出
    if str(ma_type).upper() == "SMA":
        ma200 = c.rolling(200, min_periods=1).mean()
    else:
        ma200 = calc_ema(c, 200)
    curr_ma200 = float(ma200.iloc[-1])
    prev_ma200 = float(ma200.iloc[-2]) if len(ma200) >= 2 else curr_ma200

    # 2. Market Profile 算出
    if "VAL" not in df_5m.columns or "POC" not in df_5m.columns or "VAH" not in df_5m.columns or df_5m["VAL"].isna().all():
        logic = logicinstance()
        df_mp = logic.make_market_profile(df_5m, period=vp_period, num_bins=num_bins, value_area_pct=value_area_pct, tail_only=2)
        val_s = df_mp["VAL"]
        vah_s = df_mp["VAH"]
        poc_s = df_mp["POC"]
    else:
        val_s = df_5m["VAL"]
        vah_s = df_5m["VAH"]
        poc_s = df_5m["POC"]

    curr_val = float(val_s.iloc[-1])
    curr_vah = float(vah_s.iloc[-1])
    prev_vah = float(vah_s.iloc[-2]) if len(vah_s) >= 2 else curr_vah
    curr_poc = float(poc_s.iloc[-1])

    curr_c = float(c.iloc[-1])
    prev_c = float(c.iloc[-2]) if len(c) >= 2 else curr_c
    curr_h = float(h.iloc[-1])
    curr_l = float(l.iloc[-1])

    va_width_pct = ((curr_vah - curr_val) / curr_val) if curr_val > 0 else 0.0
    vah_dc = (prev_c >= prev_vah) and (curr_c < curr_vah)  # VAH下抜けブレイクダウン

    # トレンド条件判定
    cond = str(ma_condition).lower()
    if not use_ema_filter or cond == "none" or str(ma_type).upper() == "NONE":
        trend_ok = True
    elif cond == "ma_falling":
        trend_ok = (curr_ma200 < prev_ma200)
    elif cond == "both":
        trend_ok = (curr_c <= curr_ma200) and (curr_ma200 < prev_ma200)
    else:  # "close_below"
        trend_ok = (curr_c <= curr_ma200) if not np.isnan(curr_ma200) else True

    # ショート利確目標価格: VAH - (VAH - VAL) * target_ratio
    va_diff = (curr_vah - curr_val) if (curr_vah > curr_val and curr_val > 0) else 0.0
    va_spacing = va_diff * target_ratio if va_diff > 0 else 0.0
    target_tp = curr_vah - va_spacing if va_spacing > 0 else curr_poc

    spo = (current_pos_qty < 0 or (current_pos_qty > 0 and current_trades_count > 0 and avg_entry_price > 0))  # ショートポジション保有中
    poz = (current_pos_qty == 0 and current_trades_count == 0)

    metrics = {
        "curr_price": current_price,
        "curr_c": curr_c,
        "prev_c": prev_c,
        "val": curr_val,
        "vah": curr_vah,
        "prev_vah": prev_vah,
        "poc": curr_poc,
        "ema200": curr_ma200,
        "va_width_pct": va_width_pct,
        "vah_dc": vah_dc,
        "trend_ok": trend_ok,
        "target_tp": target_tp,
        "avg_entry_price": avg_entry_price,
        "last_entry_price": last_entry_price,
        "trades_count": current_trades_count,
        "is_emergency_breakeven": is_emergency_breakeven,
    }

    # === [1] ショート動的 MAE ストップロス判定 (価格上昇逆行) ===
    if spo and avg_entry_price > 0:
        eff_sl_pct = abs(dynamic_sl_pct) if dynamic_sl_pct != 0 else 0.025
        sl_price_dyn = avg_entry_price * (1.0 + eff_sl_pct)
        if current_price >= sl_price_dyn:
            trigger_px = current_price
            loss_pct = (avg_entry_price - trigger_px) / avg_entry_price
            reason = f"VAH_POC_DYNAMIC_MAE_SL: 価格(${trigger_px:.4f}) >= 動的SL(${sl_price_dyn:.4f}, SL率+{eff_sl_pct:.2%}, 損失{loss_pct:.2%})"
            return {"action": "STOP_LOSS", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-1] 動的タイムアウト微損撤退判定 (timeout_bars本経過で含み損なら成行買戻し撤退) ===
    if spo and bars_held >= timeout_bars and avg_entry_price > 0 and current_price > avg_entry_price:
        loss_pct = (avg_entry_price - current_price) / avg_entry_price
        reason = f"VAH_POC_TIMEOUT_EXIT: 保有タイムアウト({bars_held}本>={timeout_bars}本) & ショート含み損({loss_pct:.2%}) ➔ 動的タイムアウト成行買戻し撤退"
        return {"action": "TIMEOUT_EXIT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-0] 地合い非BEAR時の緊急建値撤退判定 (平均建値 -0.20% で早期買戻し) ===
    if spo and is_emergency_breakeven and avg_entry_price > 0:
        breakeven_target = avg_entry_price * 0.9980  # -0.20% (ショート利益圏)
        if current_price <= breakeven_target:
            pnl_pct = (avg_entry_price - current_price) / avg_entry_price
            reason = f"VAH_POC_EMERGENCY_BREAKEVEN_TP: 地合い変化(非BEAR)に伴う緊急建値買戻し撤退 (目標: ${breakeven_target:.4f}, PnL: {pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [2] ショート利確判定 (VAH-VAL 最適比率水準 到達かつ建値-0.40%以下利益) ===
    if spo and target_tp > 0:
        is_tp_hit = (current_price <= target_tp)
        is_profit = (current_price <= avg_entry_price * 0.9960)
        if is_tp_hit and is_profit:
            pnl_pct = (avg_entry_price - current_price) / avg_entry_price if avg_entry_price > 0 else 0.0
            reason = f"VAH_POC_TP (TR={target_ratio:.2f}): 目標(${target_tp:.4f})到達 & 建値利益(PnL={pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [3] 新規ショートエントリー判定 (VAHデッドクロス ＆ MA200 ＆ バリュー幅>=1.5% ＆ 3要素フィルター ＆ クールダウン解除) ===
    if poz and not is_cooldown_active:
        if vah_dc and (va_width_pct >= min_va_width) and trend_ok:
            prec_ok, prec_msg, prec_info = evaluate_entry_precision_gate(df_5m, side="short", is_vp=True)
            metrics.update(prec_info)
            if prec_ok:
                strat_tag = "VAH_POC_EMA" if (use_ema_filter and str(ma_condition).lower() != "none" and str(ma_type).upper() != "NONE") else "VAH_POC"
                ema_str = f" & {ma_type}200(${curr_ma200:.4f})条件OK" if "EMA" in strat_tag else ""
                reason = f"{strat_tag}_INITIAL_ENTRY_SHORT: VAH割れ(${curr_c:.4f} < ${curr_vah:.4f}){ema_str} & 幅({va_width_pct:.1%}>=1.5%) [{prec_msg}]"
                return {"action": "INITIAL_ENTRY_SHORT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}
            else:
                return {"action": "NONE", "reason": f"VAH_POC_PRECISION_FILTER_WAIT: {prec_msg}", "lot_multiplier": 1.0, "metrics": metrics}

    if spo:
        return {"action": "HOLD", "reason": "VAH_POC_HOLD", "lot_multiplier": 1.0, "metrics": metrics}

    return {"action": "NONE", "reason": "VAH_POC_WAIT", "lot_multiplier": 1.0, "metrics": metrics}


def check_rsima3_short_signals(
    df_5m: pd.DataFrame,
    current_pos_qty: float,
    current_trades_count: int,
    avg_entry_price: float,
    current_price: float,
    rsi_len: int = 9,
    ema_len: int = 7,
    s_ep: float = 60.0,
    s_cp: float = 40.0,
    sl_pct: float = 0.090,
    max_trades: int = 1,
    last_entry_price: Optional[float] = None,
    val_price: Optional[float] = None,
    vah_price: Optional[float] = None,
    is_cooldown_active: bool = False,
    is_emergency_breakeven: bool = False,
    dynamic_sl_pct: float = 0.025,
    bars_held: int = 0,
    use_ema_filter: bool = False,
    timeout_bars: int = 48,
) -> Dict[str, Any]:
    """RSIMA3 ショート判定 (単発1本固定・ナンピン完全なし)
    1. エントリー: RSI×RSI-EMA デッドクロス ＆ RSI-EMA > s_ep ＆ (EMA200フィルター)
    2. 利確: RSI < s_cp かつ 平均建値以下利益浮上
    3. 損切り: 動的MAEストップ (価格逆行上昇) または 4時間タイムアウト
    4. 緊急建値撤退: 地合い非BEAR時、平均建値 -0.20% で早期買戻し
    """
    if df_5m is None or df_5m.empty or len(df_5m) < rsi_len + ema_len + 5:
        return {"action": "NONE", "reason": "5M_DATA_INSUFFICIENT", "lot_multiplier": 1.0, "metrics": {}}

    c = df_5m["close"].astype(float)
    rsi, lrsi_ma = calc_rsi_ema(c, rsi_period=rsi_len, ma_period=ema_len)

    curr_rsi = float(rsi.iloc[-1])
    prev_rsi = float(rsi.iloc[-2])
    curr_ma = float(lrsi_ma.iloc[-1])
    prev_ma = float(lrsi_ma.iloc[-2])

    curr_close = float(c.iloc[-1])
    prev_close = float(c.iloc[-2]) if len(c) >= 2 else current_price

    # 1. デッドクロス判定 & RSI-EMA下向き戻り天井確認
    is_crossunder_curr = (curr_rsi < curr_ma) and (prev_rsi >= prev_ma)
    prev2_rsi = float(rsi.iloc[-3]) if len(rsi) >= 3 else prev_rsi
    prev2_ma = float(lrsi_ma.iloc[-3]) if len(lrsi_ma) >= 3 else prev_ma
    is_crossunder_prev = (prev_rsi < prev_ma) and (prev2_rsi >= prev2_ma) and (curr_rsi <= curr_ma)

    ma_falling = (curr_ma <= prev_ma)  # 上昇途中の押し目を物理遮断し、戻り天井確認
    SE = (is_crossunder_curr or is_crossunder_prev) and ma_falling
    SC = (curr_ma > s_ep) or (prev_ma > s_ep)

    # 2. EMA(200) トレンドフィルター
    if use_ema_filter:
        ema200 = calc_ema(c, 200)
        curr_ema200 = float(ema200.iloc[-1])
        trend_ok = (curr_close <= curr_ema200) if not np.isnan(curr_ema200) else True
    else:
        curr_ema200 = 0.0
        trend_ok = True

    spo = (current_pos_qty < 0 or (current_pos_qty > 0 and current_trades_count > 0 and avg_entry_price > 0))
    poz = (current_pos_qty == 0 and current_trades_count == 0)

    metrics = {
        "rsi": curr_rsi,
        "rsi_ma": curr_ma,
        "s_ep": s_ep,
        "s_cp": s_cp,
        "SE": SE,
        "SC": SC,
        "trend_ok": trend_ok,
        "ema200": curr_ema200,
        "current_price": current_price,
        "avg_entry_price": avg_entry_price,
        "trades_count": current_trades_count,
        "is_emergency_breakeven": is_emergency_breakeven,
    }

    # === [1] 動的 MAE ストップロス判定 (価格上昇逆行) ===
    if spo and avg_entry_price > 0:
        eff_sl_pct = abs(dynamic_sl_pct) if dynamic_sl_pct != 0 else 0.025
        sl_price_dyn = avg_entry_price * (1.0 + eff_sl_pct)
        if current_price >= sl_price_dyn:
            trigger_px = current_price
            loss_pct = (avg_entry_price - trigger_px) / avg_entry_price
            reason = f"RSIMA_SHORT_DYNAMIC_MAE_SL: 価格(${trigger_px:.4f}) >= 動的SL(${sl_price_dyn:.4f}, SL率+{eff_sl_pct:.2%}, 損失{loss_pct:.2%})"
            return {"action": "STOP_LOSS", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-1] 動的タイムアウト微損撤退判定 ===
    if spo and bars_held >= timeout_bars and avg_entry_price > 0 and current_price > avg_entry_price:
        loss_pct = (avg_entry_price - current_price) / avg_entry_price
        reason = f"RSIMA_SHORT_TIMEOUT_EXIT: 保有タイムアウト({bars_held}本>={timeout_bars}本) & ショート含み損({loss_pct:.2%}) ➔ 動的タイムアウト成行買戻し撤退"
        return {"action": "TIMEOUT_EXIT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [1-0] 地合い非BEAR時の緊急建値撤退判定 ===
    if spo and is_emergency_breakeven and avg_entry_price > 0:
        breakeven_target = avg_entry_price * 0.9980
        if current_price <= breakeven_target:
            pnl_pct = (avg_entry_price - current_price) / avg_entry_price
            reason = f"RSIMA_SHORT_EMERGENCY_BREAKEVEN_TP: 地合い変化(非BEAR)に伴う緊急建値買戻し撤退 (目標: ${breakeven_target:.4f}, PnL: {pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [2] ショート利確判定 (RSI < s_cp かつ 建値利益圏) ===
    if spo and avg_entry_price > 0:
        if curr_rsi < s_cp and current_price <= avg_entry_price * 0.996:
            pnl_pct = (avg_entry_price - current_price) / avg_entry_price
            reason = f"RSIMA_SHORT_TP: RSI({curr_rsi:.1f}) < s_cp({s_cp:.0f}) & 建値利益(PnL={pnl_pct:+.2%})"
            return {"action": "CLOSE_ALL", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}

    # === [3] 新規ショートエントリー判定 (3要素フィルター統合) ===
    if poz and not is_cooldown_active:
        if SE and SC and trend_ok:
            prec_ok, prec_msg, prec_info = evaluate_entry_precision_gate(df_5m, side="short", is_vp=False)
            metrics.update(prec_info)
            if prec_ok:
                strat_name = "RSIMA_SHORT_EMA" if use_ema_filter else "RSIMA_SHORT"
                ema_str = f" & EMA200(${curr_ema200:.4f})条件OK" if use_ema_filter else ""
                reason = f"{strat_name}_INITIAL_ENTRY_SHORT: デッドクロス & lrsiMA({curr_ma:.1f}>{s_ep:.0f}){ema_str} [{prec_msg}]"
                return {"action": "INITIAL_ENTRY_SHORT", "reason": reason, "lot_multiplier": 1.0, "metrics": metrics}
            else:
                return {"action": "NONE", "reason": f"RSIMA_SHORT_PRECISION_FILTER_WAIT: {prec_msg}", "lot_multiplier": 1.0, "metrics": metrics}

    if spo:
        return {"action": "HOLD", "reason": "RSIMA_SHORT_HOLD", "lot_multiplier": 1.0, "metrics": metrics}

    return {"action": "NONE", "reason": "RSIMA_SHORT_WAIT", "lot_multiplier": 1.0, "metrics": metrics}


def simulate_rsima3_strategy(
    df: pd.DataFrame,
    rsi_len: int = 9,
    ema_len: int = 7,
    l_ep: float = 40.0,
    l_cp: float = 60.0,
    max_trades: int = 5,
    initial_equity: float = 100.0,
    sl_pct: float = 0.025,
    fee_rate: float = 0.0006,
) -> dict:
    """
    RSIMA3 モメンタムスキャルピング戦略 (LONG ONLY) バックテストシミュレーション (PROJECT_RULES 第3項準拠)
    - エントリー: RSI(9)が RSI-EMA(7)を下から上にゴールデンクロス かつ 水準 < 40
    - ナンピン: 保有中かつ最大5枠未満、再度GCかつ水準<40、前足終値が平均建値から -addPct(0.4%〜0.6%)下落
    - 利確: RSI > 60 かつ 平均建値から +addPct(0.4%〜1.0%)以上プラス浮上で全保有数量を一括成行決済
    - 損切り: 平均建値から -9.0% (終値確定判定) ＋ 24時間クールダウン
    """
    if df is None or df.empty or len(df) < rsi_len + ema_len + 10:
        return {"final_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "DD_max": 0.0, "max_unrealized_loss": 0.0, "strategy": "rsima"}

    closes = df["close"].values
    highs = df["high"].values if "high" in df.columns else closes
    lows = df["low"].values if "low" in df.columns else closes
    n = len(closes)

    rsi, lrsi_ma = calc_rsi_ema(df["close"].astype(float), rsi_period=rsi_len, ma_period=ema_len)
    rsi_vals = rsi.values
    ma_vals = lrsi_ma.values

    total_max_stages = max_trades
    trade_size_usdt = initial_equity / total_max_stages

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    last_entry_price = 0.0
    sl_cooldown_until = -1

    pending_entry = None
    maker_fee_rate = 0.0002

    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]
    unrealized_list = [0.0]
    exec_history = []

    for i in range(2, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        curr_rsi = rsi_vals[i]
        prev_rsi = rsi_vals[i-1]
        curr_ma = ma_vals[i]
        prev_ma = ma_vals[i-1]
        prev_close = closes[i-1]
        ts = df["timestamp"].iloc[i] if "timestamp" in df.columns else i

        # 0. 前足で配置されたエントリー/ナンピン指値の約定チェック
        if pending_entry is not None:
            limit_px = pending_entry["limit_price"]
            if l <= limit_px:
                exec_price = limit_px
                buy_val = trade_size_usdt
                qty = buy_val / exec_price
                fee = buy_val * maker_fee_rate
                cum_fees += fee
                pos_cost += buy_val
                pos_qty += qty
                last_entry_price = exec_price
                avg_price = pos_cost / pos_qty
                pos_count += 1
                exec_history.append({"timestamp": ts, "price": exec_price, "size": qty, "type": pending_entry["entry_type"]})
            pending_entry = None

        # 1. 決済チェック (保有ポジションがある場合)
        if pos_qty > 0:
            # A. 損切り判定 (sl_pct)
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sl_price = c
                sell_val = pos_qty * sl_price
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                exec_history.append({"timestamp": ts, "price": sl_price, "size": -pos_qty, "type": "RSIMA_STOP_LOSS", "pnl": pnl})
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry_price = 0.0
                sl_cooldown_until = i + 288
            else:
                # B. 利確判定 (RSI > 60 かつ +addPct 以上浮上)
                add_pct = get_tv_add_pct(max(pos_count, 1))
                if curr_rsi > l_cp and c >= avg_price * (1.0 + add_pct):
                    exit_price = c
                    sell_val = pos_qty * exit_price
                    fee = sell_val * fee_rate
                    pnl = sell_val - pos_cost - fee
                    cum_realized_pnl += pnl
                    cum_fees += fee
                    trades.append(pnl)
                    exec_history.append({"timestamp": ts, "price": exit_price, "size": -pos_qty, "type": "RSIMA_TP", "pnl": pnl})
                    pos_qty = 0.0
                    pos_cost = 0.0
                    avg_price = 0.0
                    pos_count = 0
                    last_entry_price = 0.0

        # 2. エントリー / ナンピン判定
        if pending_entry is None and not np.isnan(curr_rsi) and not np.isnan(curr_ma):
            is_crossover = (curr_rsi > curr_ma) and (prev_rsi <= prev_ma)
            is_level_ok = (curr_ma < l_ep or prev_ma < l_ep)

            # 初期エントリー
            if pos_count == 0:
                if i >= sl_cooldown_until and is_crossover and is_level_ok:
                    limit_price = c * (1.0 - 0.001)
                    pending_entry = {
                        "limit_price": limit_price,
                        "entry_type": "ENTRY_RSIMA_GC",
                        "placed_ts": ts
                    }
            # ナンピン追加
            elif 1 <= pos_count < total_max_stages:
                add_pct = get_tv_add_pct(pos_count)
                if is_crossover and is_level_ok and (prev_close < avg_price * (1.0 - add_pct)):
                    limit_price = c * (1.0 - 0.001)
                    pending_entry = {
                        "limit_price": limit_price,
                        "entry_type": f"NANPIN_RSIMA_{pos_count}",
                        "placed_ts": ts
                    }

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
            "ema_len": ema_len,
            "l_ep": l_ep,
            "l_cp": l_cp,
            "max_trades": total_max_stages,
        }
    }


def simulate_val_poc_strategy(
    df: pd.DataFrame,
    vp_period: int = 150,
    num_bins: int = 50,
    max_nanpin: int = 4,
    nanpin_drop_pct: Optional[float] = 0.030,
    initial_equity: float = 100.0,
    sl_pct: float = 0.025,
    fee_rate: float = 0.0006,
    target_ratio: float = 0.50,
) -> dict:
    """
    VAL ゴールデンクロス ＆ EMA200 ＆ バリュー幅>=1.5% ＆ VAL-VAH 中間値50%利確 ＆ -3%ナンピン最大4回 (VAL_POC 最適仕様)
    - エントリー: VALクロスオーバー (close > VAL かつ prev_close <= prev_VAL)
    - トレンドフィルター: close >= EMA(close, 200)
    - バリュー幅フィルター: (VAH - VAL) / VAL >= 1.5%
    - 利確: POCが下すぎる問題を解消するため、VAL-VAH中間値50%水準 (VAL + (VAH - VAL) * target_ratio) 到達かつ建値プラス域
    - ナンピン: 直近建値から -3.0% 下落ごと、最大4回 (初期1 + ナンピン4 = 計5枠)
    - 損切り: 9.0% (5分足確定終値判定) ＋ 24時間 (288本) クールダウン
    """
    closes = df["close"].values
    highs = df["high"].values if "high" in df.columns else closes
    lows = df["low"].values if "low" in df.columns else closes
    n = len(closes)

    # 1. EMA 200 算出
    ema200 = calc_ema(df["close"], 200).values

    # 2. Market Profile 算出
    if "VAL" not in df.columns or "POC" not in df.columns or "VAH" not in df.columns or df["VAL"].isna().all():
        logic = logicinstance()
        df_mp = logic.make_market_profile(df, period=vp_period, num_bins=num_bins)
        vals = df_mp["VAL"].values
        vahs = df_mp["VAH"].values
        pocs = df_mp["POC"].values
    else:
        vals = df["VAL"].values
        vahs = df["VAH"].values if "VAH" in df.columns else (vals * 1.02)
        pocs = df["POC"].values

    if n < 10:
        return {"final_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "DD_max": 0.0, "max_unrealized_loss": 0.0}

    total_max_stages = max_nanpin + 1  # 初期1 + ナンピン4 = 計5枠
    trade_size_usdt = initial_equity / total_max_stages

    pos_qty = 0.0
    pos_cost = 0.0
    avg_price = 0.0
    pos_count = 0
    last_entry_price = 0.0
    sl_cooldown_until = -1  # 24h (288本) クールダウン

    pending_entry = None  # Maker指値待機用
    maker_fee_rate = 0.0002  # Maker手数料 0.02%

    cum_realized_pnl = 0.0
    cum_fees = 0.0
    trades = []
    equity_curve = [initial_equity]
    unrealized_list = [0.0]
    exec_history = []

    for i in range(1, n):
        c = closes[i]
        h = highs[i]
        l = lows[i]
        val = vals[i]
        prev_val = vals[i-1]
        vah = vahs[i]
        poc = pocs[i]
        e200 = ema200[i]
        ts = df["timestamp"].iloc[i] if "timestamp" in df.columns else i

        # 0. 前足で配置されたエントリー/ナンピン指値の約定チェック
        if pending_entry is not None:
            limit_px = pending_entry["limit_price"]
            if l <= limit_px:
                exec_price = limit_px
                buy_val = trade_size_usdt
                qty = buy_val / exec_price
                fee = buy_val * maker_fee_rate
                cum_fees += fee
                pos_cost += buy_val
                pos_qty += qty
                last_entry_price = exec_price
                avg_price = pos_cost / pos_qty
                pos_count += 1
                exec_history.append({"timestamp": ts, "price": exec_price, "size": qty, "type": pending_entry["entry_type"]})
            pending_entry = None

        # 1. 決済チェック (保有ポジションがある場合、確定足Closeでの成行決済)
        if pos_qty > 0:
            # A. 固定損切り (9.0% SL: 5分確定足終値判定 Close)
            if sl_pct > 0 and c < avg_price * (1.0 - sl_pct):
                sl_price = c
                sell_val = pos_qty * sl_price
                fee = sell_val * fee_rate
                pnl = sell_val - pos_cost - fee
                cum_realized_pnl += pnl
                cum_fees += fee
                trades.append(pnl)
                exec_history.append({"timestamp": ts, "price": sl_price, "size": -pos_qty, "type": "STOP_LOSS", "pnl": pnl})
                pos_qty = 0.0
                pos_cost = 0.0
                avg_price = 0.0
                pos_count = 0
                last_entry_price = 0.0
                sl_cooldown_until = i + 288  # 24時間 (288足) クールダウン発動
            # B. VAL-VAH 中間値50%〜最適化60%水準 利確クローズ (高値または終値が target_tp に到達かつ建値以上)
            else:
                target_tp = val + (vah - val) * target_ratio if (vah > val and val > 0) else poc
                if not np.isnan(target_tp) and (h >= target_tp or c >= target_tp):
                    exit_price = max(c, target_tp) if h >= target_tp else c
                    if exit_price > avg_price * (1.0 + fee_rate):
                        sell_val = pos_qty * exit_price
                        fee = sell_val * fee_rate
                        pnl = sell_val - pos_cost - fee
                        cum_realized_pnl += pnl
                        cum_fees += fee
                        trades.append(pnl)
                        exec_history.append({"timestamp": ts, "price": exit_price, "size": -pos_qty, "type": "VAL_VAH_60_TP", "pnl": pnl})
                        pos_qty = 0.0
                        pos_cost = 0.0
                        avg_price = 0.0
                        pos_count = 0
                        last_entry_price = 0.0

        # 2. エントリー / ナンピン判定
        if not np.isnan(val) and not np.isnan(prev_val) and pending_entry is None:
            # 初期エントリー: ポジションなし ＆ クールダウン解除済 ＆ VALゴールデンクロス ＆ バリュー幅>=1.5% ＆ Close >= EMA200
            if pos_count == 0:
                if i >= sl_cooldown_until:
                    va_width_pct = ((vah - val) / val) if val > 0 else 0.0
                    val_gc = (closes[i-1] <= prev_val) and (c > val)
                    trend_ok = (c >= e200) if not np.isnan(e200) else True
                    if val_gc and (va_width_pct >= 0.015) and trend_ok:
                        limit_price = c * (1.0 - 0.001)
                        pending_entry = {
                            "limit_price": limit_price,
                            "entry_type": "ENTRY_VAL_GC",
                            "placed_ts": ts
                        }
            # ナンピン: 最後の建値から -3.0% 下落、最大4回追加 (初期1+ナンピン4=計5枠)
            elif 1 <= pos_count < total_max_stages:
                drop_pct = nanpin_drop_pct or 0.030
                if (c <= last_entry_price * (1.0 - drop_pct)) or (l <= last_entry_price * (1.0 - drop_pct)):
                    limit_price = c * (1.0 - 0.001)
                    pending_entry = {
                        "limit_price": limit_price,
                        "entry_type": f"NANPIN_{pos_count}",
                        "placed_ts": ts
                    }

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
        "strategy": "val_poc",
        "equity_curve": equity_curve,
        "exec_history": exec_history,
        "params": {
            "max_nanpin": max_nanpin,
            "nanpin_drop_pct": nanpin_drop_pct,
            "max_trades": total_max_stages,
            "sl_pct": sl_pct
        }
    }



# 【新戦略】固定20銘柄 + BTC単独地合い監視 + 日足押し目 + 1H VPブレイク + 5M動的決済
# ==============================================================================

def check_normalized_peak_timing(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    【PROJECT_RULES 第2項準拠】3期間ノーマライズ前半高値検出規約 (ピークアウト除外フィルター)
    対象期間: 5d (120h) / 10d (240h) / 30d (720h) 全期間統一基準
    判定条件: 最高値(Max)が「前半50%以内」に位置している場合、
             現在値が期間レンジの半値ライン (Min + (Max - Min) * 0.5) を維持できているか検証。
    除外基準: 前半ピーク かつ 現在値がレンジ半値未満（維持率50%未満）に沈んでいる銘柄は、
             「ピークアウト（行って来い崩壊）または長期衰退中で反発力なし」とみなして除外。
             ※一度大きく下落しても、現在値がレンジ半値以上まで買い戻されて盛り上がっている銘柄はセーフ。
    """
    windows = [
        ("5d", 5 * 24),    # 120h
        ("10d", 10 * 24),  # 240h
        ("30d", 30 * 24),  # 720h
    ]
    reasons = []
    is_bad = False

    for w_name, w_hours in windows:
        if len(df) < 24:
            continue
        sub = df.tail(w_hours).reset_index(drop=True)
        if len(sub) < 24:
            continue

        c_series = sub["close"].astype(float)
        base_c = c_series.iloc[0]
        if base_c <= 0:
            continue
        norm_c = c_series / base_c
        final_norm = float(norm_c.iloc[-1])
        c_min = float(norm_c.min())
        c_max = float(norm_c.max())
        half_level = c_min + (c_max - c_min) * 0.5

        peak_idx = int(norm_c.argmax())
        peak_ratio = peak_idx / len(norm_c)

        if peak_ratio < 0.50:
            if final_norm < half_level:
                is_bad = True
                reasons.append(
                    f"{w_name}前半ピーク半値割れ(peak={peak_ratio:.2f}, max={c_max:.2f}x, curr={final_norm:.2f}x < half={half_level:.2f}x)"
                )

    return is_bad, reasons


def check_normalized_trough_timing(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    【PROJECT_RULES 第2項準拠】3期間ノーマライズ前半安値検出規約 (ショート用: 底打ち反発・踏み上げ除外フィルター)
    対象期間: 5d (120h) / 10d (240h) / 30d (720h) 全期間統一基準
    判定条件: 最安値(Min)が「前半50%以内」に位置している場合、
             現在値が期間レンジの半値ライン (Min + (Max - Min) * 0.5) を上回っているか検証。
    除外基準: 前半ボトム かつ 現在値がレンジ半値以上（戻し率50%以上）に反発・上昇している銘柄は、
             「底打ち反転（踏み上げショートスクイズリスク大）」とみなしてショート候補から除外。
             ※前半高値をつけて下落基調の銘柄や、安値圏で低迷している下落トレンド銘柄はショート適格としてセーフ。
    """
    windows = [
        ("5d", 5 * 24),    # 120h
        ("10d", 10 * 24),  # 240h
        ("30d", 30 * 24),  # 720h
    ]
    reasons = []
    is_bad = False

    for w_name, w_hours in windows:
        if len(df) < 24:
            continue
        sub = df.tail(w_hours).reset_index(drop=True)
        if len(sub) < 24:
            continue

        c_series = sub["close"].astype(float)
        base_c = c_series.iloc[0]
        if base_c <= 0:
            continue
        norm_c = c_series / base_c
        final_norm = float(norm_c.iloc[-1])
        c_min = float(norm_c.min())
        c_max = float(norm_c.max())
        half_level = c_min + (c_max - c_min) * 0.5

        trough_idx = int(norm_c.argmin())
        trough_ratio = trough_idx / len(norm_c)

        if trough_ratio < 0.50:
            if final_norm > half_level:
                is_bad = True
                reasons.append(
                    f"{w_name}前半ボトム半値超え(trough={trough_ratio:.2f}, min={c_min:.2f}x, curr={final_norm:.2f}x > half={half_level:.2f}x)"
                )

    return is_bad, reasons


def check_normalized_timing_by_regime(df: pd.DataFrame, regime: str = "BUY") -> Tuple[bool, List[str]]:
    """
    地合い（regime: BUY / SELL / NEUTRAL）に応じたノーマライズ前半高安監査の統合インターフェース
    - BUY時: 前半ピーク半値割れ除外（ピークアウト警戒）
    - SELL時: 前半ボトム半値超え除外（底打ち反発警戒）
    - NEUTRAL時: 新規停止のため除外判定なし（False, []）
    """
    reg_upper = str(regime).upper()
    if reg_upper == "BUY":
        return check_normalized_peak_timing(df)
    elif reg_upper == "SELL":
        return check_normalized_trough_timing(df)
    return False, []


def check_bottom_reversal_recovery(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    【PROJECT_RULES 第2項準拠】3期間ノーマライズ前半安値反発検出規約 (買い相場用: セリクラ底打ち反転・V字回復銘柄)
    対象期間: 5d (120h) / 10d (240h) / 30d (720h)
    判定条件: 最安値(Min)が「前半50%以内」に位置し、現在値がレンジ半値ライン
             (Min + (Max - Min) * 0.5) 以上まで力強く買い戻されているか検証。
    採用基準: 前半に大底を打ち、後半にかけてレンジ半値以上へ急反発している銘柄は、
             「セリクラ底打ち反転・上昇トレンド転換（V字回復）」とみなして買い候補銘柄に追加採用する。
    """
    windows = [
        ("5d", 5 * 24),    # 120h
        ("10d", 10 * 24),  # 240h
        ("30d", 30 * 24),  # 720h
    ]
    reasons = []
    is_recovery = False

    for w_name, w_hours in windows:
        if len(df) < 24:
            continue
        sub = df.tail(w_hours).reset_index(drop=True)
        if len(sub) < 24:
            continue

        c_series = sub["close"].astype(float)
        base_c = c_series.iloc[0]
        if base_c <= 0:
            continue
        norm_c = c_series / base_c
        final_norm = float(norm_c.iloc[-1])
        c_min = float(norm_c.min())
        c_max = float(norm_c.max())
        half_level = c_min + (c_max - c_min) * 0.5

        trough_idx = int(norm_c.argmin())
        trough_ratio = trough_idx / len(norm_c)

        if trough_ratio < 0.50 and final_norm >= half_level:
            is_recovery = True
            reasons.append(
                f"{w_name}底打ち反転(trough={trough_ratio:.2f}, min={c_min:.2f}x, curr={final_norm:.2f}x >= half={half_level:.2f}x)"
            )

    return is_recovery, reasons


def check_peak_breakdown_trend(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    【PROJECT_RULES 第2項準拠】3期間ノーマライズ前半高値急落検出規約 (売り相場用: 天井崩壊トレンド銘柄)
    対象期間: 5d (120h) / 10d (240h) / 30d (720h)
    判定条件: 最高値(Max)が「前半50%以内」に位置し、現在値がレンジ半値ライン
             (Min + (Max - Min) * 0.5) 以下に沈み込んでいるか検証。
    採用基準: 前半に天井をつけ、後半にかけてレンジ半値以下へ崩落している銘柄は、
             「天井崩壊トレンド・下落モメンタム発生」とみなして売り候補銘柄に追加採用する。
    """
    windows = [
        ("5d", 5 * 24),    # 120h
        ("10d", 10 * 24),  # 240h
        ("30d", 30 * 24),  # 720h
    ]
    reasons = []
    is_breakdown = False

    for w_name, w_hours in windows:
        if len(df) < 24:
            continue
        sub = df.tail(w_hours).reset_index(drop=True)
        if len(sub) < 24:
            continue

        c_series = sub["close"].astype(float)
        base_c = c_series.iloc[0]
        if base_c <= 0:
            continue
        norm_c = c_series / base_c
        final_norm = float(norm_c.iloc[-1])
        c_min = float(norm_c.min())
        c_max = float(norm_c.max())
        half_level = c_min + (c_max - c_min) * 0.5

        peak_idx = int(norm_c.argmax())
        peak_ratio = peak_idx / len(norm_c)

        if peak_ratio < 0.50 and final_norm <= half_level:
            is_breakdown = True
            reasons.append(
                f"{w_name}天井崩壊下落(peak={peak_ratio:.2f}, max={c_max:.2f}x, curr={final_norm:.2f}x <= half={half_level:.2f}x)"
            )

    return is_breakdown, reasons


def evaluate_market_regime_3window(
    symbol_1h_dfs: Dict[str, pd.DataFrame]
) -> Dict[str, Any]:
    """
    【PROJECT_RULES 第2項準拠】新・全体地合い判定規約
    30d / 10d / 5d の 3期間ノーマライズ比較チャート（20銘柄＋BTC 2票＝計22票）を用いて
    各期間の買い/売り票（前半高値・安値の半値維持監査）を集計し、
    3期間すべてが買い判定なら総合地合い「BUY」および買い推奨銘柄を返す。
    3期間すべてが売り判定なら総合地合い「SELL」を返す。
    それ以外は総合地合い「NEUTRAL」（トレードなし）。
    """
    windows = [
        ("30d", 30 * 24),  # 720h
        ("10d", 10 * 24),  # 240h
        ("5d", 5 * 24),    # 120h
    ]

    period_results = {}
    eligible_buy_by_window = {}
    eligible_sell_by_window = {}
    peak_breakdown_by_window = {}
    btc_peak_breakdown_by_window = {}
    market_peak_bd_by_window = {}
    bottom_reversal_by_window = {}
    btc_bottom_rev_by_window = {}
    market_bottom_rev_by_window = {}

    # 銘柄の重複キーを排除（ティッカー単位でユニーク化して計22票を厳格保証）
    unique_symbol_dfs = {}
    for k_sym, k_df in symbol_1h_dfs.items():
        k_ticker = k_sym.upper().replace('-USDT', '').replace('USDT', '')
        if k_ticker not in unique_symbol_dfs:
            unique_symbol_dfs[k_ticker] = (k_sym, k_df)

    for w_name, w_hours in windows:
        buy_votes = 0
        sell_votes = 0
        buy_passed = set()
        sell_passed = set()
        peak_breakdown_passed = set()
        btc_peak_breakdown = False
        bottom_reversal_passed = set()
        btc_bottom_reversal = False
        norm_series_list = []
        total_votes = 0

        for ticker, (sym, df) in unique_symbol_dfs.items():
            if df is None or df.empty or len(df) < 24:
                continue
            sub = df.tail(w_hours).reset_index(drop=True)
            if len(sub) < 24:
                continue

            c_series = sub["close"].astype(float)
            base_c = c_series.iloc[0]
            if base_c <= 0:
                continue
            norm_c = c_series / base_c
            final_norm = float(norm_c.iloc[-1])
            c_min = float(norm_c.min())
            c_max = float(norm_c.max())
            half_level = c_min + (c_max - c_min) * 0.5

            peak_idx = int(norm_c.argmax())
            peak_ratio = peak_idx / len(norm_c)
            trough_idx = int(norm_c.argmin())
            trough_ratio = trough_idx / len(norm_c)

            votes = 2 if sym.upper().replace("-USDT", "").replace("USDT", "") == "BTC" else 1
            total_votes += votes

            # 天井崩壊判定 (前半50%以内に最高値があり、現在値がレンジ半値以下へ崩落)
            is_peak_bd = (peak_ratio < 0.50 and final_norm <= half_level)
            if is_peak_bd:
                peak_breakdown_passed.add(sym)
                if ticker == "BTC":
                    btc_peak_breakdown = True

            # 底打ち反転判定 (前半50%以内に最安値があり、現在値がレンジ半値以上へ急反発)
            is_bottom_rev = (trough_ratio < 0.50 and final_norm >= half_level)
            if is_bottom_rev:
                bottom_reversal_passed.add(sym)
                if ticker == "BTC":
                    btc_bottom_reversal = True

            norm_series_list.append(norm_c)

            # 買い候補判定 (プラスかつ前半高値半値維持)
            if final_norm >= 1.0:
                if peak_ratio >= 0.50 or final_norm >= half_level:
                    buy_votes += votes
                    buy_passed.add(sym)

            # 売り候補判定 (マイナスかつ前半安値半値維持)
            if final_norm < 1.0:
                if trough_ratio >= 0.50 or final_norm <= half_level:
                    sell_votes += votes
                    sell_passed.add(sym)

        # 市場平均ノーマライズ系列の天井崩壊・底打ち反転判定
        market_peak_bd = False
        market_bottom_rev = False
        if norm_series_list:
            min_l = min(len(s) for s in norm_series_list)
            if min_l >= 24:
                stacked = np.array([s.iloc[-min_l:].values for s in norm_series_list])
                m_mean = np.mean(stacked, axis=0)
                m_min = float(np.min(m_mean))
                m_max = float(np.max(m_mean))
                m_half = m_min + (m_max - m_min) * 0.5
                m_final = float(m_mean[-1])

                m_peak_idx = int(np.argmax(m_mean))
                m_peak_ratio = m_peak_idx / len(m_mean)
                if m_peak_ratio < 0.50 and m_final <= m_half:
                    market_peak_bd = True

                m_trough_idx = int(np.argmin(m_mean))
                m_trough_ratio = m_trough_idx / len(m_mean)
                if m_trough_ratio < 0.50 and m_final >= m_half:
                    market_bottom_rev = True

        # 判定基準: 半分以上 (11票以上 / 22票)
        state = "NEUTRAL"
        if buy_votes >= 11:
            state = "BUY"
        elif sell_votes >= 11:
            state = "SELL"

        period_results[w_name] = {
            "state": state,
            "buy_votes": buy_votes,
            "sell_votes": sell_votes,
            "total_votes": total_votes,
            "buy_passed": list(buy_passed),
            "sell_passed": list(sell_passed),
            "peak_breakdown_passed": list(peak_breakdown_passed),
            "btc_peak_breakdown": btc_peak_breakdown,
            "market_peak_bd": market_peak_bd,
            "bottom_reversal_passed": list(bottom_reversal_passed),
            "btc_bottom_reversal": btc_bottom_reversal,
            "market_bottom_rev": market_bottom_rev,
        }
        eligible_buy_by_window[w_name] = buy_passed
        eligible_sell_by_window[w_name] = sell_passed
        peak_breakdown_by_window[w_name] = peak_breakdown_passed
        btc_peak_breakdown_by_window[w_name] = btc_peak_breakdown
        market_peak_bd_by_window[w_name] = market_peak_bd
        bottom_reversal_by_window[w_name] = bottom_reversal_passed
        btc_bottom_rev_by_window[w_name] = btc_bottom_reversal
        market_bottom_rev_by_window[w_name] = market_bottom_rev

    # 30d 天井崩壊判定の集約 (BTC天井崩壊、市場平均天井崩壊、またはアルト等天井崩壊8銘柄以上)
    is_30d_peak_breakdown = (
        btc_peak_breakdown_by_window.get("30d", False)
        or market_peak_bd_by_window.get("30d", False)
        or len(peak_breakdown_by_window.get("30d", set())) >= 8
    )

    # 30d 底打ち反転判定の集約 (BTC底打ち反転、市場平均底打ち反転、またはアルト等底打ち反転8銘柄以上)
    is_30d_bottom_reversal = (
        btc_bottom_rev_by_window.get("30d", False)
        or market_bottom_rev_by_window.get("30d", False)
        or len(bottom_reversal_by_window.get("30d", set())) >= 8
    )

    # 総合地合い判定 (3期間すべてが一致しているか、または30d天井崩壊/底打ち反転緩和)
    states = [period_results[w]["state"] for w in ["30d", "10d", "5d"]]
    recommended_symbols = []
    regime_reason = ""

    if states == ["BUY", "BUY", "BUY"]:
        overall_regime = "BUY"
        regime_reason = "3期間すべて買い (BUY一致)"
        common_buy = set.intersection(*(eligible_buy_by_window[w] for w in ["30d", "10d", "5d"]))
        recommended_symbols = sorted([s for s in common_buy if s.upper().replace("-USDT", "").replace("USDT", "") != "BTC"])
    elif states == ["SELL", "SELL", "SELL"]:
        overall_regime = "SELL"
        regime_reason = "3期間すべて売り (SELL一致)"
        common_sell = set.intersection(*(eligible_sell_by_window[w] for w in ["30d", "10d", "5d"]))
        recommended_symbols = sorted([s for s in common_sell if s.upper().replace("-USDT", "").replace("USDT", "") != "BTC"])
    elif states[1] == "BUY" and states[2] == "BUY" and is_30d_bottom_reversal:
        overall_regime = "BUY"
        rev_detail = []
        if btc_bottom_rev_by_window.get("30d", False):
            rev_detail.append("BTC底打ち反転")
        if market_bottom_rev_by_window.get("30d", False):
            rev_detail.append("市場平均底打ち反転")
        rev_count = len(bottom_reversal_by_window.get("30d", set()))
        if rev_count > 0:
            rev_detail.append(f"底打ち反転{rev_count}銘柄")
        detail_str = "/".join(rev_detail) if rev_detail else "底打ち反転検知"
        regime_reason = f"30d底打ち反転上昇({detail_str}) ＆ 10d/5d買い一致 (30d={states[0]}から買い判定緩和)"

        common_10_5 = eligible_buy_by_window["10d"].intersection(eligible_buy_by_window["5d"])
        eligible_30d_long = eligible_buy_by_window["30d"].union(bottom_reversal_by_window.get("30d", set()))
        common_buy = common_10_5.intersection(eligible_30d_long)
        if not common_buy:
            common_buy = common_10_5
        recommended_symbols = sorted([s for s in common_buy if s.upper().replace("-USDT", "").replace("USDT", "") != "BTC"])
    elif states[1] == "SELL" and states[2] == "SELL" and is_30d_peak_breakdown:
        overall_regime = "SELL"
        bd_detail = []
        if btc_peak_breakdown_by_window.get("30d", False):
            bd_detail.append("BTC天井崩壊")
        if market_peak_bd_by_window.get("30d", False):
            bd_detail.append("市場平均天井崩壊")
        bd_count = len(peak_breakdown_by_window.get("30d", set()))
        if bd_count > 0:
            bd_detail.append(f"天井崩壊{bd_count}銘柄")
        detail_str = "/".join(bd_detail) if bd_detail else "天井崩壊検知"
        regime_reason = f"30d天井崩壊下落({detail_str}) ＆ 10d/5d売り一致 (30d={states[0]}から売り判定緩和)"

        common_10_5 = eligible_sell_by_window["10d"].intersection(eligible_sell_by_window["5d"])
        eligible_30d_short = eligible_sell_by_window["30d"].union(peak_breakdown_by_window.get("30d", set()))
        common_sell = common_10_5.intersection(eligible_30d_short)
        if not common_sell:
            common_sell = common_10_5
        recommended_symbols = sorted([s for s in common_sell if s.upper().replace("-USDT", "").replace("USDT", "") != "BTC"])
    else:
        overall_regime = "NEUTRAL"
        regime_reason = f"地合い不一致または中立 (30d={states[0]}, 10d={states[1]}, 5d={states[2]})"
        recommended_symbols = []

    return {
        "overall_regime": overall_regime,
        "is_buy_regime": (overall_regime == "BUY"),
        "is_sell_regime": (overall_regime == "SELL"),
        "regime_reason": regime_reason,
        "is_30d_peak_breakdown": is_30d_peak_breakdown,
        "is_30d_bottom_reversal": is_30d_bottom_reversal,
        "period_results": period_results,
        "recommended_symbols": recommended_symbols,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def check_market_regime_safe(
    df_btc_1h: Optional[pd.DataFrame] = None,
    max_24h_drop_pct: float = -0.035,
    market_state_info: Optional[Dict[str, Any]] = None,
    *args,
    **kwargs,
) -> Tuple[bool, str, Dict[str, Any]]:
    """【PROJECT_RULES 第2項準拠】市場全体地合い判定
    総合地合い判定 (market_state_info 連携):
       - 3期間 (30d/10d/5d) すべてが「買い (BUY)」であれば安全（新規エントリー可）。
       - 買い (BUY) でなければ新規ロングを即座にブロック（ノーポジ待機）。
       ※BTC単独パニック急落チェックは3期間ノーマライズ判定（BTC 2票/計22票）に統合され不要のため廃止。
    """
    metrics: Dict[str, Any] = {}

    # 全体地合い判定 (SELL時は暴落危険のため新規エントリー完全停止、BUYおよびNEUTRAL時はエントリー許可)
    if market_state_info and isinstance(market_state_info, dict):
        overall = market_state_info.get("overall_regime")
        metrics["overall_regime"] = overall
        if overall == "SELL":
            return False, f"REGIME_SELL_BLOCKED(overall_regime={overall})", metrics
        if overall == "BUY":
            return True, f"REGIME_BUY_SAFE(overall_regime={overall})", metrics
        if overall == "NEUTRAL":
            return True, f"REGIME_NEUTRAL_SAFE(overall_regime={overall})", metrics

    return True, "REGIME_SAFE(DEFAULT)", metrics


def check_daily_pullback_condition(
    df_daily: Optional[pd.DataFrame],
    curr_price: float,
    min_pullback_pct: float = 0.015,
    max_pullback_pct: float = 0.120,
) -> Tuple[bool, str, Dict[str, Any]]:
    """日足（1D）データに基づく「上昇トレンド中の健全な押し目（Dip）」判定。

    判定条件:
    1. 上昇トレンド維持: 日足EMA50以上、または直近の日足EMA20がEMA50以上
    2. 直近高値からの健全な調整（押し目）:
       過去14〜30日の最高値から -1.5% 〜 -12% の押し目ゾーンにあること
       （過熱状態の高値掴みを避け、かつ暴落ナイフは除外）
    3. サポート反発・下げ止まり気配:
       現在値が直近日足安値から反発している、または日足EMA20近辺で下支え
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 10:
        # 日足データ蓄積中のフォールバック（初期化時）
        return True, "DAILY_WARMUP_PASS", {}

    c = df_daily["close"].astype(float)
    h = df_daily["high"].astype(float)
    l = df_daily["low"].astype(float)

    # 1. 日足EMAの計算
    span_short = min(20, len(c))
    span_mid = min(50, len(c))
    ema20 = float(c.ewm(span=span_short, adjust=False).mean().iloc[-1])
    ema50 = float(c.ewm(span=span_mid, adjust=False).mean().iloc[-1])

    # 上昇トレンド判定: 現在価格がEMA50以上、またはEMA20 >= EMA50
    is_uptrend = (curr_price >= ema50 * 0.98) or (ema20 >= ema50)

    # 2. 直近高値（過去14日〜30日）と押し目率
    lookback = min(len(df_daily), 30)
    recent_high = float(h.tail(lookback).max())
    recent_low = float(l.tail(lookback).min())

    pullback_pct = (recent_high - curr_price) / recent_high if recent_high > 0 else 0.0
    # 健全な押し目範囲内か
    is_sound_pullback = min_pullback_pct <= pullback_pct <= max_pullback_pct

    # また、直近2〜3日の安値から反発しているか（下げ止まり確認）
    short_low = float(l.tail(3).min())
    is_rebounding = curr_price >= short_low * 1.005  # 最安値から+0.5%以上反発

    metrics = {
        "curr_price": curr_price,
        "recent_high": recent_high,
        "pullback_pct": pullback_pct,
        "ema20": ema20,
        "ema50": ema50,
        "is_uptrend": is_uptrend,
        "is_sound_pullback": is_sound_pullback,
        "is_rebounding": is_rebounding,
    }

    if not is_uptrend:
        return False, f"DAILY_NOT_UPTREND(Px={curr_price:.4f} < EMA50={ema50:.4f})", metrics

    if pullback_pct < min_pullback_pct:
        # 高値圏すぎる（過熱感あり）
        # ただしEMA20近辺で新高値トライ中の場合は条件付き許可
        if curr_price >= recent_high * 0.99:
            return True, f"DAILY_HIGH_MOMENTUM(HighBreak {pullback_pct:.1%})", metrics
        return False, f"DAILY_TOO_CLOSE_TO_PEAK(Pullback={pullback_pct:.1%} < {min_pullback_pct:.1%})", metrics

    if pullback_pct > max_pullback_pct:
        # 下落しすぎ（暴落リスク）
        return False, f"DAILY_DEEP_DROP_RISK(Pullback={pullback_pct:.1%} > {max_pullback_pct:.1%})", metrics

    if is_sound_pullback and is_rebounding:
        reason = f"DAILY_SOUND_PULLBACK(High={recent_high:.4f}, Dip={pullback_pct:.1%}, Rebound=True)"
        return True, reason, metrics

    # 押し目ゾーン内なら許容
    return True, f"DAILY_PULLBACK_ZONE(Dip={pullback_pct:.1%})", metrics


def check_1h_vp_breakout(
    df_1h: Optional[pd.DataFrame],
    curr_price: float
) -> Tuple[bool, str, Dict[str, Any]]:
    """1時間足の出来高プロファイル（VAH）または直近レンジ高値のブレイクアウト判定。"""
    if df_1h is None or df_1h.empty or len(df_1h) < 5:
        return False, "1H_DATA_INSUFFICIENT", {}

    vah_1h = float(df_1h["VAH"].iloc[-1]) if "VAH" in df_1h.columns else None
    poc_1h = float(df_1h["POC"].iloc[-1]) if "POC" in df_1h.columns else None
    val_1h = float(df_1h["VAL"].iloc[-1]) if "VAL" in df_1h.columns else None

    prev_c = float(df_1h["close"].iloc[-2]) if len(df_1h) >= 2 else curr_price
    recent_high = float(df_1h["high"].iloc[-13:-1].max()) if len(df_1h) >= 13 else curr_price

    metrics = {
        "curr_price": curr_price,
        "vah_1h": vah_1h,
        "poc_1h": poc_1h,
        "val_1h": val_1h,
        "prev_close": prev_c,
        "recent_high": recent_high,
    }

    # 1. VAHクロスオーバー上抜け
    if vah_1h is not None:
        if (prev_c <= vah_1h * 1.003) and (curr_price > vah_1h):
            reason = f"1H_VAH_BREAKOUT(Px={curr_price:.4f} > VAH={vah_1h:.4f})"
            return True, reason, metrics

    # 2. 直近12時間レンジ高値ブレイクアウト
    if (prev_c <= recent_high * 1.002) and (curr_price > recent_high):
        reason = f"1H_RANGE_HIGH_BREAKOUT(Px={curr_price:.4f} > High={recent_high:.4f})"
        return True, reason, metrics

    return False, "1H_NO_BREAKOUT", metrics


def evaluate_dynamic_close_vs_hold(
    df_5m: Optional[pd.DataFrame],
    curr_price: float,
    stop_price: float,
    target_price: Optional[float] = None,
    entry_price: Optional[float] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """CLOSEシグナル（利確やトレイリングストップ）点灯時に、5分足WebSocketで直前の勢いを動的判定。
    
    - 反発・下ヒゲ・買い支えが確認できる場合 ➔ 'HOLD'（利を最大限に伸ばす）
    - 大陰線・失速・売り浴びせが確認できる場合 ➔ 'CLOSE'（安全に確定手仕舞い）
    """
    metrics: Dict[str, Any] = {
        "curr_price": curr_price,
        "stop_price": stop_price,
        "target_price": target_price,
    }

    # ストップ価格を大幅（-1.0%以上）に割り込んでいる場合は安全最優先で即CLOSE
    if stop_price > 0 and curr_price < stop_price * 0.990:
        return "CLOSE", f"DYNAMIC_EXIT: ストップ大幅割れ (${curr_price:.4f} < ${stop_price:.4f})", metrics

    if df_5m is None or df_5m.empty or len(df_5m) < 5:
        # 5Mデータがない場合は安全側に倒して通常CLOSE
        return "CLOSE", "DYNAMIC_EXIT: 5Mデータ不足のため安全CLOSE", metrics

    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    h = df_5m["high"].astype(float)
    l = df_5m["low"].astype(float)
    v = df_5m["volume"].astype(float)

    curr_c = float(c.iloc[-1])
    curr_o = float(o.iloc[-1])
    curr_h = float(h.iloc[-1])
    curr_l = float(l.iloc[-1])
    curr_v = float(v.iloc[-1])

    # 足の長さとヒゲの計算
    candle_range = curr_h - curr_l
    body = abs(curr_c - curr_o)
    is_bullish = curr_c >= curr_o
    lower_wick = (min(curr_c, curr_o) - curr_l) if candle_range > 0 else 0.0

    vol_ma = float(v.tail(10).mean()) if len(v) >= 10 else curr_v
    vol_ratio = curr_v / vol_ma if vol_ma > 0 else 1.0

    metrics.update({
        "is_bullish": is_bullish,
        "vol_ratio": vol_ratio,
        "lower_wick_ratio": (lower_wick / candle_range) if candle_range > 0 else 0.0,
    })

    # 【HOLD判定条件 (反発・利伸ばし)】
    # 1. 5分足が陽線で反発中、または顕著な下ヒゲ（下ヒゲが全体の40%以上）で買い支えられている
    # 2. 直近安値（curr_l）から現在値がしっかり戻している
    has_strong_lower_wick = candle_range > 0 and (lower_wick / candle_range) >= 0.40
    is_rebounding_now = is_bullish and (curr_c > curr_l * 1.002)

    # また、直前の確定足（iloc[-2]）が陽線でサポートされている
    prev_bullish = (float(c.iloc[-2]) > float(o.iloc[-2])) if len(c) >= 2 else False

    if (has_strong_lower_wick or is_rebounding_now or prev_bullish) and curr_price >= stop_price * 0.996:
        reason = f"DYNAMIC_HOLD: 5分足で反発・買い支え検知 (陽線={is_bullish}, 下ヒゲ={lower_wick/candle_range if candle_range>0 else 0:.1%}, Vol={vol_ratio:.1f}x) ➔ 利益を伸ばします"
        return "HOLD", reason, metrics

    # 【CLOSE判定条件 (失速・手仕舞い)】
    # 陰線で戻り売りが先行している、または大陰線
    reason = f"DYNAMIC_CLOSE: 5分足で下落・失速検知 (陰線, Vol={vol_ratio:.1f}x) ➔ 安全手仕舞い"
    return "CLOSE", reason, metrics
