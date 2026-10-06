#!/bin/bash

# 監視対象のプロセス名
PROCESS_NAME="bitbank5_46_1spot_limit.py"
PYTHON_BIN="/home/ubuntu/pybot-env310/bin/python"
BOT_DIR="/home/ubuntu/bitbank5_46"

# プロセスが実行中かチェック
if ! pgrep -f "$PROCESS_NAME" >/dev/null; then
    echo "$(date) - $PROCESS_NAME が停止していたため再起動します..."
    cd "$BOT_DIR" || exit 1
    if [ -f "$BOT_DIR/bot_output.log" ]; then
        mv "$BOT_DIR/bot_output.log" "$BOT_DIR/bot_output.log.old"
    fi
    nohup "$PYTHON_BIN" -u "$BOT_DIR/$PROCESS_NAME" --loop > "$BOT_DIR/bot_output.log" 2>&1 &
    
    sleep 3
    if pgrep -f "$PROCESS_NAME" >/dev/null; then
        echo "✅ [自動復旧成功] $PROCESS_NAME を再起動しました。(PID: $(pgrep -f "$PROCESS_NAME" | tr '\n' ' '))"
    else
        echo "❌ [自動復旧失敗] $PROCESS_NAME の再起動に失敗しました。ログ末尾:"
        tail -n 25 "$BOT_DIR/bot_output.log"
    fi
else
    # 正常稼働中
    exit 0
fi

