"""4大取引所（Bitbank, Binance Japan, Hyperliquid, BingX）全銘柄マルチ時間足統合データ収集オーケストレーター

8時間ごと (01:00, 09:00, 17:00 JST) またはボット起動時に呼び出され、
低CPU負荷・低メモリ消費で4大取引所のデータ収集と時間分割Discord配信を順次実行します。
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Sequence

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 各取引所のパイプライン関数をインポート
import download_historical_candles as bitbank_pipeline
import download_binance_candles as binance_pipeline
import download_hyperliquid_candles as hyperliquid_pipeline
import download_bingx_candles as bingx_pipeline

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

JST = timezone(timedelta(hours=9))


def log(message: str) -> None:
    stamp = datetime.now(JST).strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


async def run_all_exchanges_pipeline(
    days_1d: int = 1460,
    days_1h: int = 1460,
    days_15m: int = 180,
    days_5m: int = 90,
    days_1m: int = 30,
    intervals: Optional[Sequence[str]] = None,
    exchanges: Optional[Sequence[str]] = None,
    force: bool = False,
    force_upload: bool = False,
    skip_upload: bool = False,
    skip_charts: bool = False,
    incremental_hours: int = 8
) -> None:
    """海外取引所（Hyperliquid, BingX）および国内取引所のデータ収集パイプラインを安全・低負荷で順次実行（8時間差分つけ足し対応）"""
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    target_intervals = list(intervals) if intervals else ["1d", "1h", "15m", "5m"]
    target_exchanges = [e.lower() for e in exchanges] if exchanges else ["hyperliquid", "bingx"]

    mode_label = f"8時間差分つけ足し更新 (直近{incremental_hours}H)" if incremental_hours > 0 and not force else "全期間フル同期"
    log("=" * 80)
    log(f"🌍 【取引所 全銘柄マルチ時間足 統合データ収集パイプライン開始】 ({mode_label})")
    log(f"   開始時刻: {now_jst}")
    log(f"   対象取引所: {target_exchanges}")
    log(f"   対象足種: {target_intervals}")
    log("=" * 80)

    # 1. Bitbank パイプライン (全47銘柄)
    if "bitbank" in target_exchanges:
        log("\n" + "#" * 35 + " Bitbank " + "#" * 35)
        try:
            await bitbank_pipeline.run_pipeline(
                days_1d=days_1d,
                days_1h=days_1h,
                days_15m=days_15m,
                days_5m=days_5m,
                days_1m=days_1m,
                intervals=target_intervals,
                force=force,
                force_upload=force_upload,
                skip_upload=skip_upload,
                skip_charts=skip_charts,
                incremental_hours=incremental_hours
            )
        except Exception as e:
            log(f"⚠️ [Bitbank パイプライン エラー]: {e}")
        gc.collect()
        await asyncio.sleep(2.0)
    else:
        log("⏭️ [Bitbank] 対象外のためスキップします。")

    # 2. Binance Japan パイプライン (全27銘柄)
    if "binance" in target_exchanges or "binance_japan" in target_exchanges:
        log("\n" + "#" * 33 + " Binance Japan " + "#" * 33)
        try:
            await binance_pipeline.run_pipeline(
                days_1d=days_1d,
                days_1h=days_1h,
                days_15m=days_15m,
                days_5m=days_5m,
                days_1m=days_1m,
                intervals=target_intervals,
                force=force,
                force_upload=force_upload,
                skip_upload=skip_upload,
                incremental_hours=incremental_hours
            )
        except Exception as e:
            log(f"⚠️ [Binance Japan パイプライン エラー]: {e}")
        gc.collect()
        await asyncio.sleep(2.0)
    else:
        log("⏭️ [Binance Japan] 対象外のためスキップします。")

    # 3. Hyperliquid パイプライン (全230+銘柄)
    if "hyperliquid" in target_exchanges:
        log("\n" + "#" * 33 + " Hyperliquid " + "#" * 33)
        try:
            await hyperliquid_pipeline.run_pipeline(
                days_1d=days_1d,
                days_1h=days_1h,
                days_15m=days_15m,
                days_5m=days_5m,
                days_1m=days_1m,
                intervals=target_intervals,
                force=force,
                force_upload=force_upload,
                skip_upload=skip_upload,
                incremental_hours=incremental_hours
            )
        except Exception as e:
            log(f"⚠️ [Hyperliquid パイプライン エラー]: {e}")
        gc.collect()
        await asyncio.sleep(5.0)
    else:
        log("⏭️ [Hyperliquid] 対象外のためスキップします。")

    # 4. BingX パイプライン (FX除外 全1100+銘柄)
    if "bingx" in target_exchanges:
        log("\n" + "#" * 35 + " BingX " + "#" * 35)
        try:
            await bingx_pipeline.run_pipeline(
                days_1d=days_1d,
                days_1h=days_1h,
                days_15m=days_15m,
                days_5m=days_5m,
                days_1m=days_1m,
                intervals=target_intervals,
                force=force,
                force_upload=force_upload,
                skip_upload=skip_upload,
                incremental_hours=incremental_hours
            )
        except Exception as e:
            log(f"⚠️ [BingX パイプライン エラー]: {e}")
        gc.collect()
    else:
        log("⏭️ [BingX] 対象外のためスキップします。")

    elapsed_min = (time.time() - start_time) / 60.0
    log("\n" + "=" * 80)
    log(f"🎉 【データ収集・時間分割配信パイプライン 完了】(総所要時間: {elapsed_min:.1f} 分)")
    log("=" * 80 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="取引所 全銘柄マルチ時間足統合データ収集パイプライン")
    parser.add_argument("--days-1d", type=int, default=1460, help="日足取得期間（日）")
    parser.add_argument("--days-1h", type=int, default=1460, help="1時間足取得期間（日）")
    parser.add_argument("--days-15m", type=int, default=180, help="15分足取得期間（日）")
    parser.add_argument("--days-5m", type=int, default=90, help="5分足取得期間（日）")
    parser.add_argument("--days-1m", type=int, default=30, help="1分足取得期間（日）")
    parser.add_argument("--intervals", nargs="+", default=["1d", "1h", "15m", "5m"], help="実行する足種 (デフォルト: 1d 1h 15m 5m)")
    parser.add_argument("--exchanges", nargs="+", default=["hyperliquid", "bingx"], help="実行する取引所 (デフォルト: hyperliquid bingx)")
    parser.add_argument("--force", action="store_true", help="既存キャッシュを無視して全件再取得")
    parser.add_argument("--force-upload", action="store_true", help="ハッシュを無視して強制アップロード")
    parser.add_argument("--skip-upload", action="store_true", help="Discordアップロードをスキップ")
    parser.add_argument("--skip-charts", action="store_true", help="チャート生成スキップ")
    parser.add_argument("--incremental-hours", type=int, default=8, help="直近N時間分の差分つけ足し取得・送信（デフォルト8時間）")

    args = parser.parse_args()

    asyncio.run(run_all_exchanges_pipeline(
        days_1d=args.days_1d,
        days_1h=args.days_1h,
        days_15m=args.days_15m,
        days_5m=args.days_5m,
        days_1m=args.days_1m,
        intervals=args.intervals,
        exchanges=args.exchanges,
        force=args.force,
        force_upload=args.force_upload,
        skip_upload=args.skip_upload,
        skip_charts=args.skip_charts,
        incremental_hours=args.incremental_hours
    ))
