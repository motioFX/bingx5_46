#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Japan 出金 & BingX 入金/残高確認 自動化スクリプト (pybotters 版)

【機能】
1. Binance (Binance Japan) から XRP などの暗号資産を出金 (/sapi/v1/capital/withdraw/apply)
2. BingX の入金アドレス/タグ取得、口座残高取得、現物成行売却

【使用ライブラリ】
pybotters
"""

import asyncio
import hashlib
import hmac
import time
import urllib.parse
import pybotters
from config_loader import load_config

# config_loader 経由で hyperliquid_credentials.json から鍵とユーザー情報を取得
_config = load_config()
_apis = _config.get("apis", {})
USER_INFO = _config.get("user_info", {})

BINANCE_API_KEY = _apis.get("binance", {}).get("api_key", "")
BINANCE_SECRET_KEY = _apis.get("binance", {}).get("secret_key", "")

BINGX_API_KEY = _apis.get("bingx", {}).get("api_key", "")
BINGX_SECRET_KEY = _apis.get("bingx", {}).get("secret_key", "")


# --- BingX 用 署名ヘルパー関数 ---
def sign_bingx(secret_key: str, payload_str: str) -> str:
    return hmac.new(
        secret_key.encode('utf-8'),
        payload_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


# ==========================================
# 1. Binance Japan 特有の出金 (localentity/withdraw/apply)
# ==========================================
async def withdraw_from_binance(symbol="XRP", amount=10.0, target_address="", target_tag="", vasp_code="BINGX"):
    """
    pybotters を使用して Binance Japan からトラベルルール対応（localentity）で出金する
    """
    import json
    import urllib.parse
    import yarl

    apis = {
        'binance': [BINANCE_API_KEY, BINANCE_SECRET_KEY]
    }
    base_url = "https://api.binance.com"
    
    # トラベルルール用アンケートデータ (Japan規制対応)
    questionnaire = {
        "isAddressOwner": 1,               # 1: 本人宛て送金
        "bnfType": 0,                      # 0: 個人
        "kanjiName": USER_INFO.get("kanjiName", ""),
        "kanaName": USER_INFO.get("kanaName", ""),
        "latinName": USER_INFO.get("latinName", ""),
        "country": "jp",
        "city": USER_INFO.get("city", "Tokyo"),
        "sendTo": 1,                       # 1: 暗号資産交換業者（VASP）宛て
        "vasp": vasp_code,
        "txnPurpose": 4,                   # 4: 投資（Investment）
        "isAttested": True
    }
    
    timestamp = str(int(time.time() * 1000))
    params = {
        "coin": symbol,
        "network": "XRP",
        "address": target_address,
        "amount": str(amount),
        "timestamp": timestamp,
        "questionnaire": json.dumps(questionnaire, separators=(',', ':'), ensure_ascii=False)
    }
    if target_tag:
        params["addressTag"] = str(target_tag)

    sorted_params = sorted(params.items())
    query_str = urllib.parse.urlencode(sorted_params, quote_via=urllib.parse.quote)

    signature = hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        query_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    request_url_str = f"{base_url}/sapi/v1/localentity/withdraw/apply?{query_str}&signature={signature}"
    request_url = yarl.URL(request_url_str, encoded=True)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    async with pybotters.Client(apis=apis, base_url=base_url) as client:
        print(f"[Binance Japan(pybotters)] トラベルルール出金申請送信中: {amount} {symbol} -> {target_address}")
        resp = await client._session.post(request_url, headers=headers, auth=None)
        data = await resp.json()
        print(f"[Binance Japan(pybotters)] レスポンス [{resp.status}]:", data)
        return data


# ==========================================
# 2. BingX API (アドレス取得・残高・現物売買)
# ==========================================
async def get_bingx_deposit_address(coin="XRP"):
    """
    pybotters を使用して BingX の入金アドレスと Tag を取得
    """
    base_url = "https://open-api.bingx.com"
    endpoint = "/openApi/wallets/v1/capital/deposit/address"
    
    timestamp = int(time.time() * 1000)
    query_params = {
        'coin': coin,
        'timestamp': timestamp,
    }
    
    # 署名生成
    query_str = urllib.parse.urlencode(query_params)
    signature = sign_bingx(BINGX_SECRET_KEY, query_str)
    full_url = f"{base_url}{endpoint}?{query_str}&signature={signature}"
    
    headers = {
        'X-BX-APIKEY': BINGX_API_KEY
    }

    async with pybotters.Client() as client:
        resp = await client.get(full_url, headers=headers)
        data = await resp.json()
        print(f"[BingX(pybotters)] {coin} 入金アドレス:", data)
        return data


async def get_bingx_balance():
    """
    pybotters を使用して BingX の現物アカウント残高を取得
    """
    base_url = "https://open-api.bingx.com"
    endpoint = "/openApi/spot/v1/account/balance"
    
    timestamp = int(time.time() * 1000)
    query_params = {
        'timestamp': timestamp,
    }
    
    query_str = urllib.parse.urlencode(query_params)
    signature = sign_bingx(BINGX_SECRET_KEY, query_str)
    full_url = f"{base_url}{endpoint}?{query_str}&signature={signature}"
    
    headers = {
        'X-BX-APIKEY': BINGX_API_KEY
    }

    async with pybotters.Client() as client:
        resp = await client.get(full_url, headers=headers)
        data = await resp.json()
        print(f"[BingX(pybotters)] 口座残高:", data)
        return data


async def sell_xrp_on_bingx(quantity=10.0):
    """
    pybotters を使用して BingX で XRP を成行売却 (XRP-USDT)
    """
    base_url = "https://open-api.bingx.com"
    endpoint = "/openApi/spot/v1/trade/order"
    
    timestamp = int(time.time() * 1000)
    query_params = {
        'symbol': 'XRP-USDT',
        'side': 'SELL',
        'type': 'MARKET',
        'quantity': str(quantity),
        'timestamp': timestamp,
    }
    
    query_str = urllib.parse.urlencode(query_params)
    signature = sign_bingx(BINGX_SECRET_KEY, query_str)
    full_url = f"{base_url}{endpoint}?{query_str}&signature={signature}"
    
    headers = {
        'X-BX-APIKEY': BINGX_API_KEY
    }

    async with pybotters.Client() as client:
        resp = await client.post(full_url, headers=headers)
        data = await resp.json()
        print(f"[BingX(pybotters)] 成行売却レスポンス:", data)
        return data


# ==========================================
# メイン実行関数
# ==========================================
async def main():
    print("=== pybotters による Binance 出金 & BingX 操作スクリプト ===")
    
    # 1. BingX 入金アドレス取得例
    # await get_bingx_deposit_address("XRP")

    # 2. Binance から出金例
    # await withdraw_from_binance(
    #     symbol="XRP", 
    #     amount=92.0, 
    #     target_address="rDbf8aaRm453vAXt9TGYTH4te2cHXJYgWQ", 
    #     target_tag="10156097"
    # )

    # 3. BingX 残高確認例
    # await get_bingx_balance()


if __name__ == "__main__":
    asyncio.run(main())
