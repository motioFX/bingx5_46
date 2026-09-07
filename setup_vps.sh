#!/bin/bash
set -e

echo "=================================================="
echo "  Hyperliquid Bot (Hype5_45) VPS Auto Setup"
echo "=================================================="

INSTALL_DIR="$HOME/bingx5_46"
VENV_DIR="$HOME/pybot-env"

# 1. ディレクトリ準備
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# 2. Python 仮想環境の作成
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/4] Python仮想環境 ($VENV_DIR) を作成中..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/4] 既存のPython仮想環境 ($VENV_DIR) を使用します。"
fi

# 3. 依存パッケージのインストール
echo "[2/4] 必須ライブラリをインストール中..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install pybotters hyperliquid-python-sdk pandas numpy rich requests matplotlib cryptography

# 4. 実行権限の設定
echo "[3/4] 監視スクリプトに実行権限を付与中..."
if [ -f "$INSTALL_DIR/check_and_restart.sh" ]; then
    chmod +x "$INSTALL_DIR/check_and_restart.sh"
fi

# 5. crontab 登録の補助
echo "[4/4] crontab 自動監視の設定準備..."
(crontab -l 2>/dev/null | grep -v "check_and_restart.sh"; echo "*/5 * * * * $INSTALL_DIR/check_and_restart.sh >/dev/null 2>&1") | crontab -

echo "=================================================="
echo "  Setup Completed Successfully!"
echo "  Bot log file: $HOME/bot_output.log"
echo "=================================================="
