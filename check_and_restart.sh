#!/bin/bash

# 監視対象のプロセス名
PROCESS_NAME="bingx5_46_1futures_limit.py"
LOG_FILE="$HOME/bingx5_46/bot_output.log"

# プロセスが実行中かチェック (Python実行プロセスに限定)
if ! pgrep -f "python.*$PROCESS_NAME" >/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $PROCESS_NAME が停止していたため再起動します..."
    
    # 既存ログを退避してクラッシュ時のログ消失を防止 (最大5世代保持)
    if [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
        CRASH_LOG="$HOME/bingx5_46/bot_output_prev_$(date '+%Y%m%d_%H%M%S').log"
        cp "$LOG_FILE" "$CRASH_LOG"
        # 5世代より古い退避ログを自動クリーンアップ
        ls -t "$HOME/bingx5_46"/bot_output_prev_*.log 2>/dev/null | tail -n +6 | xargs -r rm --
    fi

    # プロセス起動 (アンバッファ & nice優先度設定)
    nohup nice -n 10 "$HOME/pybot-env/bin/python3" -u "$HOME/bingx5_46/$PROCESS_NAME" --demo --loop --skip-history > "$LOG_FILE" 2>&1 &
    
    # 起動直後の初期クラッシュ検知 (5秒待機)
    sleep 5
    if pgrep -f "python.*$PROCESS_NAME" >/dev/null; then
        echo "✅ [起動成功] $PROCESS_NAME が正常に起動しました。"
    else
        echo "❌ [起動直後クラッシュ] $PROCESS_NAME が起動直後に停止しました。ログ末尾:"
        tail -n 25 "$LOG_FILE"
    fi
else
    echo "🟢 [稼働中] $PROCESS_NAME は正常に実行されています。"
fi
