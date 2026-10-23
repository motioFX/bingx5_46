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
* **誇張表現の厳禁 & 客観的データ記述規約 (Professional Tone)**:
  - 「黄金」「絶対」「究極」等の誇張的・情報商材的な表現は一切禁止する。
  - 常に「検証済み最適仕様」「実証最適値」「データ実証値」など、客観的・データドリブン・プロフェッショナルな実務表現を用いること。

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
  - **Windows ローカルから VPS へ Python ファイル（`.py`）等を SCP で直接上書きアップロードすることは絶対に禁止する**（Git競合および環境不整合の防止）。
* **標準 Git デプロイワークフロー**:
  1. **ローカル検証**: Windows 環境でコード修正 ➔ `python -m py_compile` 等で構文・動作チェック。
  2. **コミット & プッシュ**: `git add .` ➔ `git commit -m "..."` ➔ `git push origin main`。
  3. **VPS への同期**: SSH 接続の上、`cd /home/ubuntu/bingx5_46 && git pull origin main` を実行。
  4. **プロセスの安全再起動・検証**: ボットプロセスを安全に再起動し、ログ（`bot_output.log`）で正常稼働を確認。
* **自動死活監視 (Cron)**:
  - `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` が 5 分間隔（`*/5 * * * *`）で自動監視。プロセス停止時は自動復旧する。

---

## 3. BingX 取引所発注 & 資金・ポジション管理規約 (Order & Risk Management)

* **Maker 指値優先発注 ＆ 確定足スマート追随**:
  - **Long エントリー**: 常に **`best_bid`（買い気配最良値）** に指値（`timeInForce: "GTC"`）を発注する。
  - **押し目待機と追随**: 確定足成立後、最大約4分間（240秒）指値を維持して待機。気配値不変なら注文キュー優先順位を維持し、乖離時のみ旧指値を安全キャンセル（`active_order_cancel`）して最新 `best_bid` に再配置する。
  - **足確定時のクリーンアップ**: サイン終了時は未約定指値を即座にキャンセル。
* **ロット・価格の厳格な丸め込み (Quantization)**:
  - 取引所 API（`/openApi/swap/v2/quote/contracts`）より `quantityPrecision`、`pricePrecision`、`tradeMinQuantity`（最小発注枚数）を取得し、端数処理による発注拒否エラーを厳格に防止する。
* **レバレッジ & 資金管理設定**:
  - レバレッジ: 原則 **10 倍（`LEVERAGE_FACTOR = 10.0`）** をAPI経由で設定・適用。
  - 目標投資額: **$15.0 USDT**（`BINGX_TARGET_POSITION_VALUE_USDT = 15.0`）。
  - 最大想定元本ガード: `MAX_POSITION_NOTIONAL_LIMIT = base_notional * 3.15`。
  - 有効レバレッジガード: ポートフォリオ有効レバレッジが 3.0倍 を超えるナンピンは自動ブロック。
* **最大同時保有ポジション数 & ポジション枠**:
  - **最大 2 銘柄（`MAX_ACTIVE_POSITIONS = 2`）** 厳守（保有 + 待機指値合計）。
  - **最大 3 枠（初期 1 本 ＋ ナンピン最大 2 回、等倍ロット 1.0x）**。
* **平均取得価格の厳格参照 (`avgPrice`)**:
  - BingX 先物 API は平均取得価格を **`avgPrice`** で返却するため、建値・損益・ストップ判定は必ず `avgPrice` を参照する。
* **決済 (Exit / Flatten) 安全規約**:
  - 利確・損切り・建値撤退の成行発注直前、対象銘柄の既存未約定指値を必ず全キャンセル（`active_order_cancel`）してから決済を実行する。
* **BTC 除外ルール**:
  - `BTC-USDT` は相場環境・地合い判定専用（2票）とし、アルトコインの売買エントリー対象からは完全に除外する（ETHも完全除外）。

---

## 4. デモ口座 (VST) フル活用 & API取引検証スタイル規約 (Account & Verification Mode)

* **VSTデモ口座のフル活用による実API取引検証**:
  - BingX公式のVSTデモ取引所（`https://open-api-vst.bingx.com`）をフル活用する。
  - **デモ口座で実API取引（発注・未約定キャンセル・約定照会・レバレッジ設定・ポジション管理）を実際に実行し、リアルタイムで検証していくスタイル** を標準とする。
* **口座切替フラグの明確化**:
  - `bingx5_46_1futures_limit.py` 冒頭にて以下を厳密に管理する：
    ```python
    ALLOW_LIVE_TRADING = False  # 本番リアル口座への発注APIを物理的に完全遮断
    BINGX_IS_LIVE = False       # True: 本番口座, False: デモ口座 (VST: Virtual Simulation Trading)
    BINGX_IS_AIR = ("--air" in sys.argv or "--mock" in sys.argv)  # True: 仮想AIRシミュレーション, False: 取引所API発注
    ```
* **誤発注防止二重ロック**:
  - 本番リアル発注へ切り替える際は、コード内安全ロック（`ALLOW_LIVE_TRADING = True` かつ `BINGX_IS_LIVE = True`）および起動時引数の両方が揃う二重ロック構造を維持する。

---

## 5. 基本戦略体系 ＆ 2段階個別最適化・地合いディフェンス (Strategy & Defense)

* **2大基本戦略 (LONG ONLY)**:
  1. **VAL_POC戦略**: ボリュームプロファイル（VAH, VAL, POC）の出来高構造リバウンド ＋ EMA(200) トレンドフィルター ＋ VAL-VAH 60%水準利確（Normal TP）。
  2. **RSIMA戦略**: RSI（期間9）× RSI-EMA（期間7）の反転ゴールデンクロススキャルピング ＋ RSI過熱利確。
* **2段階（72通り）個別パラメータ最適化**:
  - 4足種（5m, 15m, 30m, 60m）× 各18通り ＝ 計72通りをシミュレーション。
  - **第1段階（単発厳選）**: シングルポジション（max_trades=1）でバックテストし、取引回数10回以上かつ純利益プラス（`PnL > 0`）の自力優良パラメータのみを選抜。
  - **第2段階（ナンピン検証）**: 選抜パラメータにのみ最大3枠ナンピン（均等1倍ロット、VA 60%間隔）を適用して最終評価。
* **地合いディフェンス (30d / 10d / 5d 3段階判定連動)**:
  - **BULL (買い)**: 通常トレード（最大3枠ナンピン・通常TP）。
  - **NEUTRAL (中立)**: 慎重運用モード（追加ナンピン即時凍結＝最大1枠、平均建値+0.20%早期撤退）。
  - **BEAR (売り)**: 新規ロング全面停止（ノーポジ待機、既存ポジションGraceful Exit）。
* **損切り ＆ 24時間クールダウン**:
  - 最終建値から `(VAH - VAL) * 0.60` 割れ（確定足終値判定）で全決済し、該当銘柄は24時間新規エントリーを遮断。

---

## 6. データパイプライン (5分足 SSoT ＆ 自前OI/FR蓄積)

* **単一データ源泉 (SSoT: Single Source of Truth)**:
  - 巨大ZIPや全市場600銘柄ダウンロードは完全廃止。
  - 固定選定20銘柄 ＋ `BTC-USDT`（計21銘柄）の **5分足（5m）永続ローカル時系列ストア（`Data/candles_5m/candles_5m_{SYMBOL}.csv`）** を単一のデータ源泉とし、差分追記（Append）で軽量管理。
  - 検証・運用に必要な `15m`, `30m`, `1h`, `1d` は、すべて5分足ストアから `resample_from_5m` で動的に生成。
* **建玉 (OI) ＆ ファンディングレート (FR) 自前蓄積**:
  - 毎時00分に BingX 全対象銘柄の建玉・FRスナップショットを `Data/oi_store/` へ自動蓄積。
* **ノーマライズ比較チャート & 先月高安 (PMH/PML)**:
  - 30d / 10d / 5d ノーマライズ比較チャート（計3枚）を起動時および8時間スクリーニング時に生成・Discord送信。
  - 先月高安（PMH/PML）を記憶（`Data/monthly_key_levels.json`）し、30dチャート上に破線・点線で描画。

---

## 7. レポーティング ＆ Discord 通知フォーマット統一規約 (Reporting)

* **文字化け完全防止 (`payload_json`)**:
  - Discord Webhook へのマルチパートファイル送信時は、必ず `payload_json` パラメータを使用し、日本語や絵文字の文字化けを永久に防止すること。
* **毎時ステータスサマリーテーブル (正時 00:05 JST / `hype5_45` 完全統一)**:
  - 銘柄名は `-USDT` を省略してティッカー表示。
  - 4カラム構成（`銘柄` / `現在価格` / `ポジション` / `クジラ`）で固定幅整形、未保有時は **`ノーポジ`** と表記。
* **スライディング10日間実トレード損益レポート**:
  - 起動時・定期選定時・決済直後に、Clean版（SNS用）および Detail版（千鳥配置アノテーション付き）の2枚の累積損益チャートを生成・送信。
* **足種統一規約**:
  - エントリーおよび決済チャートは、個別最適化で採用された足種（5m/15m/30m/60m）に完全統一して描画。

---

## 8. クロスプラットフォーム & フェイルセーフ堅牢化規約 (Cross-Platform & Safety)

* **Windows / Linux 完全互換**:
  - すべてのファイル IO は **`encoding="utf-8"`**、パス操作は **`pathlib.Path`** を使用。
* **フェイルセーフ例外隔離 (プロセス常駐保証)**:
  - 外部 API 通信やバックグラウンド処理はフェイルセーフな例外ブロック（`except (Exception, BaseException):`）で隔離し、メインループが絶対に異常終了しない構造を維持する。
* **Python アンバッファ起動**:
  - VPS 上でリアルタイムログ出力を保証するため、必ず **`python3 -u`** で実行する。
* **WebSocket 通信堅牢化 ＆ 未回収タスク例外（`Task exception was never retrieved`）完全根絶**:
  - `ws.send_str()` / `ws.send_json()` の直前に `getattr(ws, "closed", False)` をチェックし、送信処理は `try...except` で完全隔離（`_safe_send_str`）。
  - `asyncio.create_task` で起動するタスク（Pong応答、購読等）は、必ずインスタンスセット（`self._bg_tasks`）に強参照を保持し、`add_done_callback(self._bg_tasks.discard)` で管理。GCによる未完了回収や未回収例外の発生を永久に根絶する。
  - pybotters 等の通信ライブラリは常に最新安定版（v1.11.2以上）を使用する。
* **CPUバウンド計算（個別最適化・バックテスト）時の GIL 協調解放**:
  - 重いバックテスト計算ループ（`run_interval_comparison` 等）の合間に `time.sleep(0.005)` を挿入し、OSレベルで確実に GIL を解放。メインスレッドの WebSocket Keep-Alive（Ping/Pong）や価格更新ループが遅延・切断されるのを物理的に防止する。
