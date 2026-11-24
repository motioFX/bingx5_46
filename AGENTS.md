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

* **Maker 指値優先発注 ＆ 非ブロッキング確定足追随（Long/Short完全対称）**:
  - **Long エントリー**: 常に **`best_bid`（買い気配最良値）** に指値（`timeInForce: "PostOnly"` / 交差リジェクト時は1〜2ティック外側へ自動リトライ）を発注する。
  - **Short エントリー**: 常に **`best_ask`（売り気配最良値）** に指値（`timeInForce: "PostOnly"` / 交差リジェクト時は1〜2ティック外側へ自動リトライ）を発注する。
  - **非ブロッキング待機と追随上限**: Long / Short ともに単発指値配置後に即リターンし、メインループ内の `pending_entry_orders` で確定足追随を統一管理する（5m足最大3本 / 15m足1本、同一確定足での重複再発注禁止）。
  - **足確定・セッション遷移時のクリーンアップ**: サイン終了、追随上限到達、セッション切替（東京→欧州等）、地合い中立化、日次CB発動時は未約定指値を即座にキャンセルする。
  - **相場板情報 (bookTicker) の本番 API 単一参照規約（デモ・本番共通）**:
    BingX Web UI の板および約定判定はデモでも本番リアル板に完全同期している。デモ専用APIの気配値は本番と乖離しているため、板気配値（`best_bid` / `best_ask`）はデモ運用時であっても**常に本番取引所公開 API（`https://open-api.bingx.com`）から取得**し、画面の板最前列と 100% 同期した Maker 指値を配置する。発注・ポジション照会のみデモ API（`open-api-vst.bingx.com`）へルーティングする。
* **ロット・価格の厳格な丸め込み (Quantization)**:
  - 取引所 API（`/openApi/swap/v2/quote/contracts`）より `quantityPrecision`、`pricePrecision`、`tradeMinQuantity`（最小発注枚数）を取得し、端数処理による発注拒否エラーを厳格に防止する。
* **レバレッジ & 資金管理設定**:
  - レバレッジ: 原則 **10 倍（`LEVERAGE_FACTOR = 10.0`）** をAPI経由で設定・適用。
  - 目標投資額: **$15.0 USDT**（`BINGX_TARGET_POSITION_VALUE_USDT = 15.0`）。
  - 最大想定元本ガード: `MAX_POSITION_NOTIONAL_LIMIT = base_notional * 3.15`。
  - 有効レバレッジガード: ポートフォリオ有効レバレッジが 3.0倍 を超えるエントリーは自動ブロック。
* **最大同時保有ポジション数 & ポジション枠**:
  - **最大 2 銘柄（`MAX_ACTIVE_POSITIONS = 2`）** 厳守（保有 + 待機指値合計）。
  - **全戦略・全方向（ロング・ショート共通）: 単発 1 枠固定（初期 1 本のみ、ナンピン完全ゼロ、目標投資額 100% フルサイズ $15.0 USDT）**。
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

## 5. F7 Two-Pillar 戦略体系 ＆ 3窓地合い連動モデル (Strategy & F7 Architecture)

* **F7 Two-Pillar 戦略体系 (`strategy_f7_two_pillar.py` SSoT / 2021-2026全6年黒字検証済み仕様)**:
  - **Pillar 1 (`val_poc_30m` / LONG)**: `30m` 足、**`ULTRA_BUY` ティアのみ**、`not is_norm_excluded`、**欧州時間（16:00〜22:00 JST）除外 (`NoEU`)**、指数減衰 MP(`120`, `half_life=60`, `bins=50`, `va=0.70`) による `VAL * 0.965 <= close <= VAL * 1.002` 押し目買い (`vol_ratio >= 1.2`)。TP=`max(VAL + (VAH - VAL)*0.70, epx*1.006)`、SL=`epx - min(2.0*ATR14, epx*0.040)`、FlipBE `+0.20%`（**`overall_regime == "SELL"` 時のみ**）、最大保有 `1440分`（24h）。
  - **Pillar 2 (`h3b_squeeze_1h` / SHORT)**: `1h` 足、**`overall_regime == "SELL"`**、`not is_norm_excluded`、**疲れ切り除外 (`NoExh`)** (`btc_ret14d >= -10%` かつ `sym_ret14d >= -20%`)、**因果 90日 WF ヒステリシスゲート (`WF90d`)**（直近90日確定ペーパー台帳 `n>=10` で `net<-3.00 & pf<0.85` 休止 / `pf>=1.00 or net>=0` 解除）通過時のみ。BB(20) 幅 `<= q25(168h)` (`shift(1)`) のボラ収縮後に `vol_ratio >= 1.5` かつ `close < rolling_48h_min.shift(1)` でショート。**S8b 段階的トレール**（`init_sl = epx + 2.0*ATR14`、翌足以降 `min(init_sl, trough + 2.0*prev_atr)`、`+1.5*entry_atr` 到達で建値 `-0.20%` ロック `BE_LOCK_SL`）、FlipBE `-0.20%`（**`overall_regime == "BUY"` 時のみ**）、最大保有 `7200分`（120h）。
  - **ポートフォリオ競合裁定 & 共通ガード**: 同時候補は **`(vol_ratio DESC, symbol ASC)`** でソートし、空き枠（最大 `MAX_ACTIVE_POSITIONS = 2`、単発 `$15.0 USDT` 固定・ナンピンゼロ）に配分。
  - **日次損失サーキットブレーカー**: JST 09:00 リセットで当日確定+含み損が `-$1.50 USDT` 到達時は新規エントリー停止（24時間 SL クールダウンは撤廃済み）。

---

## 6. データパイプライン (5分足 SSoT ＆ データレイク蓄積)

* **単一データ源泉 (SSoT: Single Source of Truth)**:
  - `Data/lake/bingx_lake.db`（WAL SQLite）および固定選定21銘柄＋`BTC-USDT`（計22銘柄）の `5m` 足永続ストアから、確定済み `5m` 足を決定論的リサンプリングして **`15m` / `30m` / `1h` / `4h`** を構築・格納（F7 では `30m` および `1h` を実売買評価に直結）。
  - 過去分不足時は OKX REST API（`/api/v5/market/history-candles`）等から自動補完し、`>= 4,800` 本の `5m` 足を確保する（`H3b` の 168h 分位＋BB20 ウォームアップ保証）。
* **建玉 (OI) ＆ ファンディングレート (FR) 自前蓄積**:
  - 毎時00分に BingX 全対象銘柄の建玉・FRスナップショットを `Data/oi_store/` へ自動蓄積。
* **ノーマライズ比較チャート & 先月高安 (PMH/PML)**:
  - 14d / 7d / 2d ノーマライズ比較チャート（計3枚）を起動時および8時間スクリーニング時に生成・Discord送信。
  - 先月高安（PMH/PML）を記憶（`Data/monthly_key_levels.json`）し、14dチャート上に破線・点線で描画。

---

## 7. レポーティング ＆ Discord 通知フォーマット統一規約 (Reporting)

* **文字化け完全防止 (`payload_json`)**:
  - Discord Webhook へのマルチパートファイル送信時は、必ず `payload_json` パラメータを使用し、日本語や絵文字の文字化けを永久に防止すること。
* **毎時ステータスサマリーテーブル (正時 00:05 JST / `hype5_45` 統一)**:
  - 銘柄名は `-USDT` を省略してティッカー表示。
  - 3カラム構成（`銘柄` / `現在価格` / `ポジション`）で固定幅整形、未保有時は **`ノーポジ`** と表記。
* **スライディング10日間実トレード損益レポート**:
  - 起動時・定期選定時・決済直後に、Clean版（SNS用）および Detail版（千鳥配置アノテーション付き）の2枚の累積損益チャートを生成・送信。
* **起動・定期選定アスキーアートバナー即時送信規約 (`force_send=True`)**:
  - ボット起動・再起動時および定期選定時刻到達時は、`AUTO TRADING SYSTEM START` および `PERIODIC SCREENING & OPTIMIZE` のアスキーアート・システム概要バナーを `force_send=True` で Discord へ即座に送信する。
  - 戦略表記は最新の「3態勢地合い連動モデル（BULL:買い / NEUTRAL:待機 / BEAR:売り）」に統一する。
* **足種統一規約**:
  - エントリーおよび決済チャートは、個別最適化で採用された足種（5m/15m/30m/60m）に完全統一して描画。
* **チャート画像出力集約規約（エントリー画像廃止 ＆ エグジット軌跡チャート集約）**:
  - **エントリー時**: チャート画像出力を廃止（不要ノイズ削減・高速化）。約定価格、目標TP、損切り仕様の詳細テキスト通知のみを即時送信する。
  - **エグジット時（利確・損切り確定時）**: エントリー位置からエグジット位置までのトレード軌跡（点線）、確定損益（PnL）、決済理由を描画した決済チャート画像（`plot_exit_chart`）のみを出力・送信する。

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
