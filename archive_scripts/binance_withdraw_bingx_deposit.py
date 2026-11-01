#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Japan 出金 & BingX 入金/残高確認 自動化スクリプト例

【機能】
1. Binance (Binance Japan) から XRP などの暗号資産を指定アドレス・タグ宛に出金
2. BingX の入金アドレス/タグ取得、残高確認、現物売買（両替）

【必要ライブラリ】
pip install ccxt
"""

import sys
import time
import ccxt

# ==========================================
# 認証情報設定（ご自身のAPIキーを入力してください）
# ==========================================
BINANCE_API_KEY = "YOUR_BINANCE_API_KEY"
BINANCE_SECRET_KEY = "YOUR_BINANCE_SECRET_KEY"

BINGX_API_KEY = "YOUR_BINGX_API_KEY"
BINGX_SECRET_KEY = "YOUR_BINGX_SECRET_KEY"


def init_binance():
    """Binance (Binance Japan) インスタンスの初期化"""
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
        }
    })
    return exchange


def init_bingx():
    """BingX インスタンスの初期化"""
    exchange = ccxt.bingx({
        'apiKey': BINGX_API_KEY,
        'secret': BINGX_SECRET_KEY,
        'enableRateLimit': True,
    })
    return exchange


# ==========================================
# 1. Binance からの出金 (Withdraw)
# ==========================================
def withdraw_from_binance(symbol="XRP", amount=10.0, target_address="", target_tag=""):
    """
    Binance から指定のアドレス・タグ宛に暗号資産を出金する
    """
    try:
        exchange = init_binance()
        print(f"[Binance] 出金処理を開始します: {amount} {symbol} -> {target_address} (Tag: {target_tag})")
        
        # ccxt 出金メソッド
        params = {}
        if target_tag:
            params['tag'] = str(target_tag)  # XRPの場合はDestination Tagが必要
        params['network'] = 'XRP'  # ネットワーク名
        
        response = exchange.withdraw(
            code=symbol,
            amount=amount,
            address=target_address,
            tag=target_tag,
            params=params
        )
        print("[Binance] 出金成功Response:", response)
        return response
    except Exception as e:
        print(f"[Binance] 出金エラー: {e}")
        return None


# ==========================================
# 2. BingX の入金アドレス・残高取得・現物売買
# ==========================================
def get_bingx_deposit_address(code="XRP"):
    """BingX の指定通貨の入金アドレスと Tag を取得"""
    try:
        exchange = init_bingx()
        address_info = exchange.fetch_deposit_address(code)
        print(f"[BingX] {code} 入金アドレス情報:", address_info)
        return address_info
    except Exception as e:
        print(f"[BingX] アドレス取得エラー: {e}")
        return None


def get_bingx_balance():
    """BingX の現物残高を取得"""
    try:
        exchange = init_bingx()
        balance = exchange.fetch_balance()
        free_balance = {k: v for k, v in balance['free'].items() if v > 0}
        print("[BingX] 保有中フリー残高:", free_balance)
        return free_balance
    except Exception as e:
        print(f"[BingX] 残高取得エラー: {e}")
        return None


def sell_xrp_for_usdt_on_bingx(amount=None):
    """
    BingX で XRP を成行で売却して USDT に両替する
    """
    try:
        exchange = init_bingx()
        symbol = 'XRP/USDT'
        
        if amount is None:
            # 成行全額指定の場合
            balance = exchange.fetch_balance()
            amount = balance['free'].get('XRP', 0)
        
        if amount <= 0:
            print("[BingX] 売却可能な XRP がありません。")
            return None

        print(f"[BingX] {amount} XRP を成行売却 (XRP/USDT) します...")
        order = exchange.create_market_sell_order(symbol, amount)
        print("[BingX] 売却注文成功:", order)
        return order
    except Exception as e:
        print(f"[BingX] 売却エラー: {e}")
        return None


if __name__ == "__main__":
    print("=== Binance 出金 / BingX 入金自動化スクリプト ===")
    # 使い方例:
    # 1. BingX の XRP 入金アドレスと Tag を取得
    # address_info = get_bingx_deposit_address("XRP")
    # 
    # 2. Binance から BingX へ出金
    # withdraw_from_binance(
    #     symbol="XRP", 
    #     amount=90.0, 
    #     target_address="rDbf8aaRm453vAXt9TGYTH4te2cHXJYgWQ", 
    #     target_tag="10156097"
    # )
    #
    # 3. BingX 残高確認
    # get_bingx_balance()
