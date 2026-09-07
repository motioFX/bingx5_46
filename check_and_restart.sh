#!/bin/bash

# 監視対象のプロセス名
PROCESS_NAME="bingx5_46_1futures_limit.py"

# プロセスが実行中かチェック
if ! pgrep -f "$PROCESS_NAME" >/dev/null; then
    echo "$(date) - $PROCESS_NAME が停止していたため再起動します..."
    nohup nice -n 10 "$HOME/pybot-env/bin/python3" -u "$HOME/bingx5_46/bingx5_46_1futures_limit.py" --loop > "$HOME/bot_output.log" 2>&1 &
    
    echo "30分間待機して起動後の生存判定を行います..."
    # 30分間 (1800秒) 待機して起動後の生存判定
    sleep 1800
    if pgrep -f "$PROCESS_NAME" >/dev/null; then
        echo "✅ [30分後判定: 成功] $PROCESS_NAME は30分後も正常に常駐稼働を継続しています。"
    else
        echo "❌ [30分後判定: 失敗] $PROCESS_NAME は起動後30分以内に異常終了しました。ログ末尾:"
        tail -n 25 "$HOME/bot_output.log"
    fi
else
    echo "🟢 [稼働中] $PROCESS_NAME は正常に実行されています。"
fi
