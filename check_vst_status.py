#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BingX VSTデモ口座・安全ロック診断スクリプト。"""

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# プロジェクトパス
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bingx5_46_1futures_limit import BINGX_IS_LIVE, ALLOW_LIVE_TRADING
from bingx5_46_2api import api_bingx

async def test():
    print("=" * 60)
    print("🔍 【BingX 取引モード & 口座設定 診断レポート】")
    print("=" * 60)
    print(f"🔒 BINGX_IS_LIVE      : {BINGX_IS_LIVE}  (False = VSTデモ口座 / True = 本番リアル口座)")
    print(f"🛡️  ALLOW_LIVE_TRADING : {ALLOW_LIVE_TRADING}  (False = リアル発注物理遮断ロック)")
    
    bx = api_bingx(symbol="BTC-USDT", mode="demo")
    helper = bx.bingx
    print(f"🌐 API Base URL       : {helper.base_url}")
    print(f"🏷️  API Mode           : {helper.mode}")
    print(f"🔑 API Key Configured : {bool(helper.api_key and not helper.api_key.startswith('YOUR_'))}")
    
    # 残高照会
    bal = await helper.get_account()
    print(f"💰 VST デモ口座残高   : {bal:.2f} VST/USDT")
    
    # ポジション照会
    pos_dict = await helper.get_positions()
    print(f"📦 保有ポジション     : Long: {pos_dict.get('buy', 0)} / Short: {pos_dict.get('sell', 0)}")
    print("=" * 60)
    if "open-api-vst.bingx.com" in helper.base_url and not BINGX_IS_LIVE and not ALLOW_LIVE_TRADING:
        print("✅ 【診断結果】完全な VST デモトレード環境（実API発注）で安全に稼働しています！")
        print("   ※ 本番リアル口座への発注はコード物理ロック（二重ロック）により 100% 遮断されています。")
    else:
        print("⚠️ 【警告】デモ口座設定に不整合があります！")
    print("=" * 60)

if __name__ == '__main__':
    asyncio.run(test())
