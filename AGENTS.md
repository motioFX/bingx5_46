# bingx5_46 AI Agent Rules & Guidelines (PROJECT_RULES 準拠)

本ドキュメントは、**bingx5_46** プロジェクトにおいて AI エージェント（Antigravity、Cursor、Cline 等）が設計・実装・運用・デプロイを行う際に遵守すべき最優先の行動規範および運用ガイドラインです。すべての開発・変更作業は `PROJECT_RULES.md` の仕様に完全に準拠しなければなりません。

---

## 1. 基本方針 & ドキュメント運用規約 (Core Directives)

* **日本語作成の義務 (Implementation Plan & Walkthrough)**:
  - 計画書（`implementation_plan.md`）および完了報告書（`walkthrough.md`）等のアーティファクトは、**例外なくすべて日本語** で記述すること。
* **仕様原本の厳守 (PROJECT_RULES.md Policy)**:
  - 取引ロジック、リスク管理、発注仕様、運用規則に関する唯一の仕様原本は [`PROJECT_RULES.md`](file:///d:/python_bitcoin/motiobtc/bingx/bingx5_46/PROJECT_RULES.md) である。
  - 実装や改修を行う前に、必ず `PROJECT_RULES.md` の記述と完全な整合性を確認すること。
* **生きたドキュメント運用 (Incremental Rule Refinement)**:
  - ユーザーから新しい要件、制約、または運用変更の指示があった場合は、速やかに `PROJECT_RULES.md` を更新・体系化（日本語）してからコード修正を行うこと。

---

## 2. 本番環境 & Git デプロイ運用規約 (Deployment & Synchronization)

* **本番運用 VPS (Oracle No.2) 接続情報**:
  - **ホスト**: `158.101.157.117`
  - **ユーザー**: `ubuntu`
  - **SSH 秘密鍵**: `C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2`
  - **SSH 接続コマンド**:
    ```bash
    ssh -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2" ubuntu@158.101.157.117
    ```
  - **プロジェクトディレクトリ**: `/home/ubuntu/bingx5_46`
  - **Python 仮想環境 (Venv)**: `/home/ubuntu/pybot-env`
* **SCP によるプログラムコード上書きの厳禁**:
  - **Windows ローカルから VPS へ Python ファイル（`.py`）を SCP 等で直接上書きアップロードすることは絶対に禁止する**（Git競合および環境不整合の防止）。
* **標準 Git デプロイワークフロー**:
  1. **ローカル検証**: Windows 環境でコード修正 ➔ `python -m py_compile` 等で構文・動作チェック。
  2. **コミット & プッシュ**: `git add .` ➔ `git commit -m "..."` ➔ `git push origin main`。
  3. **VPS への同期**: SSH 接続の上、`cd /home/ubuntu/bingx5_46 && git pull origin main` を実行。
  4. **プロセスの安全再起動・検証**: ボットプロセスを安全に再起動し、ログ（`bot_output.log`）で正常稼働を確認。
* **自動死活監視 (Cron)**:
  - `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` が 5 分間隔（`*/5 * * * *`）で自動監視。プロセス停止時は自動復旧する。

---

## 3. BingX 取引所発注 & 資金・ポジション管理規約 (Order & Risk Management)

* **Maker 指値優先発注**:
  - **Long エントリー**: 常に **`best_bid`（買い気配最良値）** に指値（`timeInForce: "GTC"`）を発注する。
  - **Short エントリー**: 常に **`best_ask`（売り気配最良値）** に指値（`timeInForce: "GTC"`）を発注する。
  - **未約定安全キャンセル**: 未約定リトライ時は、前回の未約定指値を安全に全キャンセル（`active_order_cancel`）した上で最新気配値に再配置する。
* **ロット・価格の厳格な丸め込み (Quantization)**:
  - 取引所 API（`/openApi/swap/v2/quote/contracts`）より `quantityPrecision`、`pricePrecision`、`tradeMinQuantity`（最小発注枚数）を取得し、端数処理による発注拒否エラーを厳格に防止する。
* **レバレッジ設定**:
  - 原則 **10 倍（`LEVERAGE_FACTOR = 10.0`）** を取引所 API 経由で設定・適用する。
* **ポジションサイズ & 必要証拠金チェック**:
  - 目標投資額 **$15.0 USDT**（`BINGX_TARGET_POSITION_VALUE_USDT = 15.0`）。
  - 発注前に必要証拠金（`(current_price * lot) / LEVERAGE_FACTOR`）を算出し、口座の利用可能残高（`usdt_onhand_amount`）を下回る場合は安全に発注をスキップする。
* **最大同時保有ポジション数**:
  - **最大 2 ポジション（`MAX_ACTIVE_POSITIONS = 2`）** を厳守し、資金効率とリスク分散を両立する。
* **BTC 除外ルール**:
  - `BTC`（`BTC-USDT`）は相場環境・大口判定専用とし、アルトコインの売買エントリー対象からは完全に除外する。

---

## 4. Air Mode (安全監視) / 口座管理 & 資産保護規約

* **口座切替フラグの明確化**:
  - `bingx5_46_1futures_limit.py` 冒頭にて以下を厳密に管理する：
    ```python
    BINGX_IS_LIVE = False  # True: 本番口座, False: デモ口座 (VST)
    BINGX_IS_AIR  = True   # True: AIRモード (シミュレーション), False: 取引所発注
    ```
* **誤発注防止二重ロック**:
  - 本番リアル発注へ切り替える際は、コード内フラグ（`ALLOW_LIVE_TRADING = True`）および起動時引数の両方が揃う二重ロック構造を維持する。
* **長期保有資産の隔離・保護**:
  - 取引口座内に存在する長期保有資産（現物BTC等）は、ボットの自動売買・自動決済対象から完全に隔離・保護する。
  - 定期サイクルログおよび Discord 通知にて、保有数量・平均建値・評価額・含み損益を明示する。

---

## 5. 戦略ロジック & ボリュームプロファイルトレーリング (Strategy & Trailing)

* **2 段階最適化プロセス (探索 ➔ トレーリング評価)**:
  1. **第 1 段階（基礎戦略パラメータ最適化）**:
     - **Envelope 戦略（戻りエントリー）**: `env_lower = EMA(close, len) * (1 - lower_pct / 100)`。下限を割り込んだ後、確定足で下限を上抜けてバンド内へ復帰した初動でエントリー。
     - **RSIMA 戦略（反転エントリー）**: RSI が平滑線 lrsiMA を下から上にゴールデンクロスした確定足でエントリー。
  2. **第 2 段階（ボリュームプロファイルトレーリング適用）**:
     - 最適化された基礎設定に対し、ボリュームプロファイル（VP）によるトレーリングストップを適用して利益最大化とドローダウン抑制を評価。
* **ボリュームプロファイルトレーリング (VP トレーリング) 詳細仕様**:
  - 直近ローソク足から算出された出来高プロファイル（VAH, VAL, POC）に基づき、以下のステージアップ式でストップラインを切り上げる：
    1. **エントリー直後・含み損時**: ストップライン = **`VAL - α%`**（バリュー下限割れ損切り）。
    2. **POC 上抜け (第1段階)**: ストップライン = **POC**（最多出来高価格帯）へ引き上げ。
    3. **VAH 上抜け (第2段階)**: ストップライン = **VAH**（バリュー上限）へ引き上げ、利益確保。
    4. **大相場ブレイクアウト**: `max(高値 × (1 - コールバック幅), VAH, 建値)` により利益を無制限に伸ばす。
* **決済 (Exit / Flatten) 安全規約**:
  - 決済シーケンス開始時、対象銘柄の既存未約定指値を必ず全キャンセル（`active_order_cancel`）してから成行決済（`MARKET`）を行う。

---

## 6. 全銘柄データ取得・アーカイブ & Discord 送信規約 (Data Pipeline)

* **データ取得期間 & 命名規則**:
  - **期間**: 過去 2 ヶ月分（60 日間 / 約 1,440 時間足）〜 4 ヶ月分（120 日間）。
  - **ファイル命名規則**:
    - CSV: `bingx_all_markets_1h_YYYYMMDD_HHh.csv`
    - ZIP: `bingx_all_markets_1h_YYYYMMDD_HHh.zip`
* **Discord 25MB ファイルサイズ制限の厳格遵守**:
  - 全 600 銘柄以上のデータを ZIP 圧縮する際、Discord の Webhook 上限（25MB）を超過しないよう、浮動小数点精度を最適化（`float_format="%.6g"`）して出力すること。
* **文字化け完全防止 (payload_json 規約)**:
  - Discord Webhook へのマルチパートファイル送信時は、必ず `payload_json` パラメータ（`json.dumps({"content": description}, ensure_ascii=False)`）を使用し、日本語や絵文字が `?` に化ける問題を永久に防止すること。
* **重複送信防止 (`upload_registry.py`)**:
  - 同一ハッシュ（MD5）のデータは二重送信をスキップし、ネットワークおよび Discord レートリミットを保護すること。

---

## 7. クロスプラットフォーム & フェイルセーフ堅牢化規約 (Cross-Platform & Safety)

* **Windows / Linux 完全互換**:
  - Windows Command Prompt (`cmd.exe`)、PowerShell、および Linux (Ubuntu) のいずれから実行しても、未捕捉例外やエンコーディングエラー、パス差異によるクラッシュを起こさないこと。
* **文字コード & パス操作の標準化**:
  - すべてのファイル IO 操作は **`encoding="utf-8"`** を明示的に指定する。
  - パス操作には必ず **`pathlib.Path`** を使用する。
* **フェイルセーフ例外隔離 (プロセス常駐保証)**:
  - 外部 API 通信（HTTP 502/503、タイムアウト、レート制限等）やバックグラウンド処理は、フェイルセーフな例外ブロック（`except (Exception, BaseException):`）で隔離し、メインループが絶対に異常終了しない構造を維持する。
* **Python アンバッファ起動**:
  - VPS 上でログがリアルタイムに出力されるよう、必ず **`python3 -u`** オプションで実行する。
