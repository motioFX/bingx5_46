# bitbank5_46 プロジェクト運用ルール & 開発規約

## 1. 全銘柄データダウンロード規約（ヒストリカルデータ / hyper-rigid-bot準拠）

### 【規約】4ヶ月分（120日）データ取得・期間2分割アーカイブ保存＆Discord送信（2取引所対応）
* **対象スクリプト**: 
  - Bitbank: `download_historical_candles.py`
  - Binance Japan: `download_binance_candles.py`
* **実行タイミング**: ボット起動時（ステップ2）および定期選定時刻（8時間ごと: 01:00, 09:00, 17:00 JST）
* **必須要件**:
  1. **全銘柄4ヶ月分（120日分）の完全取得**:
     - **Bitbank**: 現物 JPY 全47銘柄（`btc_jpy`, `eth_jpy`, `xrp_jpy`, `sol_jpy` ... 全ペア）の過去120日分の1時間足OHLCVデータを取得。ローカルCSV（`Data/historical_candles/{symbol}_1h.csv`）に差分蓄積。
     - **Binance Japan**: 現物 JPY 全27銘柄（`ADAJPY`, `BTCJPY`, `ETHJPY`, `NEARJPY`, `SOLJPY` ... 全現物JPYペア）の過去120日分の1時間足OHLCVデータを取得。ローカルCSV（`Data/historical_candles_binance/{symbol}_1h.csv`）に蓄積。
  2. **期間別2分割アーカイブ生成（100%完全収録）**:
     - タイムスタンプの中間点でデータを2分割し、全銘柄を網羅した2本の独立ZIPアーカイブを生成すること：
       - **Part 1/2 【過去データ (前半60日)】**:
         - Bitbank: `Data/bitbank_all_symbols_past_{YYYYMMDD_HHMMSS}.zip`
         - Binance Japan: `Data/binance_japan_all_symbols_past_{YYYYMMDD_HHMMSS}.zip`
         - 用途: 過去ヒストリー検証・長期バックテスト用
       - **Part 2/2 【直近データ (後半60日)】**:
         - Bitbank: `Data/bitbank_all_symbols_recent_{YYYYMMDD_HHMMSS}.zip`
         - Binance Japan: `Data/binance_japan_all_symbols_recent_{YYYYMMDD_HHMMSS}.zip`
         - 用途: 直近相場分析・**スマホGemini Pro（Google AI Pro）丸ごと投入用（全銘柄入り・約95万トークンで超快適・高精度動作）**
     - 全期間マスターCSV（`Data/historical_all_symbols_merged.csv`, `Data/binance_japan_all_symbols_merged.csv`）もローカル検証用に最新化保存すること。
  3. **レートリミット対策 ＆ CPU負荷極小化設計**:
     - Binance Japan のデータ取得時は、各銘柄間に `0.3秒`、ページネーション間に `0.05秒` のウェイトを挿入し、逐次（シングルスレッド）で丁寧に取得する。
     - ZIP圧縮は超高速・低負荷圧縮（圧縮レベル最軽量）を採用し、VPSのCPUクレジット消費やCPUスパイクを恒久的に防ぐ。
  4. **Discord送信仕様（環境自動判別）**:
     - VPS（Linux環境）稼働時: **`real1_bitbank`** チャンネルへ自動出力
     - Windows（win32環境）テスト時: **`test4_test`** チャンネルへ自動出力
  5. **データファイルの最新保持・自動クリーンアップ規約**:
     - 生成されたZIPファイル（BitbankおよびBinance Japanの `past`, `recent`）およびチャート画像は**「常に最新のものだけを維持する」**ことを厳守する。
     - Discord送信完了後の保管期間は **直近24時間（最大1日分）** とし、24時間を超過した古いZIPアーカイブおよび画像ファイルは自動削除（クリーンアップ）する。
     - 最新の1セット分（past/recent各1本）は経過時間に関わらず常時保護する。

---

## 2. アーキテクチャ特性（1時間足確定足 ＆ REST APIのみ）

* **5分足ループの不採用**:
  - 5分ごとのループや5分足の監視・シグナル判定は行わない。
  - ボットは完全に **「1時間足（1h）確定足」**（毎時00分05秒）のタイミングでのみ起動・評価を行う。
* **WebSocketの不採用（HTTP REST APIポーリング方式）**:
  - WebSocket（WSS）常時接続は使用せず、すべての価格取得・残高照会・発注は標準の **HTTP REST API（GET / POST）** で完結させる。
  - これにより、常時接続切断トラブルやPing/Pongタイムアウト、メモリリークのリスクを排除し、毎時00分に数十秒稼働した後は `sleep` 待機するため、VPSのCPUクレジット消費を極小化して長期間安定稼働させる。

---

## 3. オラクルNo.1 監視運用 ＆ エアモード（ペーパートレード）規約

* **オラクルNo.1の運用方針**:
  - オラクルNo.1ボットは相場監視・シグナル通知専用機として運用するため、実口座への本番発注は行わず、**エアトレード（`BITBANK_IS_AIR = True`）** として動作させる。
* **本番データ接続 ＆ 仮想シミュレーション発注**:
  - Live API（`BITBANK_IS_LIVE = True`）から本番リアルタイムレート・板情報・実残高を取得しつつ、発注処理はペーパートレードとして仮想執行する。
* **シグナル通知 ＆ チャート配信**:
  - 戦略ロジックがロングエントリーシグナルを判定した瞬間に、「🚀 **【シグナル通知: LONG ENTRY】**」（現在値、戦略名、クジラ判定等）をDiscordへ送信する。
  - 利確・手仕舞い（クローズ）シグナル判定時には、「🎯 **【シグナル通知: LONG CLOSE】**」および決済トレードチャート画像をDiscordへ送信する。

---

## 4. 全暗号資産 総合ポジション・指値監査規約（Cross-Exchange Portfolio Tracking）

* **対象スクリプト**: `portfolio_tracker.py`
* **実行タイミング**: 
  - ボット起動時
  - 毎時00分の確定足サイクル開始時（毎時間チェック）
  - 定期選定時（8時間ごと: 01:00, 09:00, 17:00 JST）
* **監査対象取引所・暗号資産**:
  - **Bitbank**: 保有中の全暗号資産（現物 RENDER, BTC 等）の残高、約定履歴からの加重平均建値、現在価格、評価額、含み損益、未約定指値（拘束JPY）。
  - **Binance Japan**: 保有中の全暗号資産（現物 NEAR 等）の残高、約定履歴からの加重平均建値、現在価格、評価額、含み損益、未約定指値（拘束JPY）。
* **通知仕様**:
  - 取引所ごとのJPY現金残高、保有暗号資産の建値/現値/損益、未約定指値の内訳、および全口座の純資産総額（現金＋現物評価額）を美しくフォーマットしてターミナルログおよびDiscordへ自動報告する。
* **現物長期保有BTCの隔離・保護**:
  - 口座内の長期保有現物BTC（0.1434 BTC等、基準建値 1,279万円）は、ボットの売買から完全に隔離・保護する。

---

## 5. Bitbank 取引所発注規約

* **現物取引（Spot / JPY建て / LONG ONLY）**:
  - Bitbank は現物取引のため、エッジ（金利・Funding Rate等）は加味せず純粋な現物売買とする。
  - **買いエントリー（BUY）のみ（LONG ONLY）**。空売り（SHORT）は行わない。
* **Maker指値優先**:
  - Longエントリーは常に **`best_bid`（買い気配最良値）** または指値で発注する。
  - 未約定リトライ時は、前回の指値を安全に全キャンセル（`cancel_order`）した上で最新気配値に再配置する。
* **ロット・価格の丸め込み（Quantization）**:
  - 各通貨ペアの最小発注数量（Bitbank 現物は基本 `0.0001` 等）および価格小数点桁数（`sz_decimals: 4.0`, 各ペアの `price_place`）を `BITBANK_SPECS` に基づき厳格に適用する。
* **主要銘柄**:
  - 対象銘柄例: `BTC`, `ETH`, `XRP`, `SOL`, `DOGE`, `BNB`, `ARB`, `SUI`, `AVAX`, `RNDR（render_jpy）`, `LINK`, `NEAR（Binance Japan現物連携）` 等

---

## 6. 資金管理・ポジション規約

* **レバレッジ設定**:
  - 現物取引のため、**レバレッジ1.0倍固定（`LEVERAGE_FACTOR = 1.0`）**。
* **ポジションサイズ**:
  - 目標投資額 15,000円（`BITBANK_TARGET_POSITION_VALUE_JPY = 15000.0`）に基づき動的にロット算出。
* **必要資金チェック**:
  - 発注前に `current_price * lot` を算出し、口座の利用可能 JPY 残高（`jpy_available`）を下回る場合は発注を安全にスキップする。
* **最大同時保有数**:
  - 最大2ポジション（`MAX_ACTIVE_POSITIONS = 2`）を厳守し、資金効率とリスク分散を両立する。

---

## 7. 戦略ロジック ＆ バックテスト規約

### 【2段階最適化プロセス】ベスト PnL 探索 ➔ トレーリングモード
1. **第1段階（基礎戦略パラメータ最適化）**:
   - **「Envelope戦略（戻りエントリー）」** と **「RSI戦略（RSIMA）」** のロジックを用いて、各銘柄におけるベスト PnL（最大利益・最適パラメータ）を探索・算出する。
2. **第2段階（トレーリングモード適用・総合評価）**:
   - ベスト PnL が得られた基礎設定に対し、**「ボリュームプロファイルトレーリング（VPトレーリング）」** を組み込み、利益の最大化とドローダウン抑制を検証・評価する。

### 【ボリュームプロファイルトレーリング（VPトレーリング）】詳細仕様
* 直近一定期間（例: 48時間）のローソク足から出来高プロファイル（VAH, VAL, POC）を算出。
* **含み損時**: ストップライン = `VAL - α%`（サポート割れ損切り）
* **POC 上抜け**: トレーリングライン = POC（最多出来高帯）へ引き上げ
* **VAH 上抜け**: トレーリングライン = VAH（バリュー上限）へ引き上げ
* **大相場ブレイクアウト**: `max(高値 × (1 - コールバック幅), VAH, 建値)` で利益追従

---

## 8. VPS常駐・接続・プロセス管理規約

* **指定運用VPS（オラクルNo.1）接続情報**:
  - **ホスト**: `141.147.160.15`
  - **ユーザー**: `ubuntu`
  - **秘密鍵**: `C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle`
  - **SSH接続コマンド**:
    ```bash
    ssh -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle" ubuntu@141.147.160.15
    ```
* **自動死活監視（Cron）**:
  - `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` を5分間隔（`*/5 * * * *`）で実行し、プロセスダウン時は自動再起動する。
  - 監視対象プロセス名: `bitbank5_46_1spot_limit.py`
* **Python実行時アンバッファ**:
  - ログがリアルタイムに出力されるよう、必ず `python3 -u` オプションで起動する。

---

## 9. コードデプロイ・同期運用規約（Git Push ➔ VPS Git Pull 方式）

* **SCP直接アップロードの禁止**:
  - Windowsローカル環境から VPS へプログラムコード（`.py` ファイル等）を SCP で直接上書きアップロードしてはならない（Git競合防止）。
* **標準デプロイワークフロー**:
  1. **ローカル**: コード修正 ➔ 構文・動作確認 ➔ `git add` ➔ `git commit` ➔ `git push origin bitbank`
  2. **VPS側**: SSH接続 ➔ `cd /home/ubuntu/bitbank5_46 && git pull origin bitbank` で同期
  3. **再起動**: VPS側でボットプロセスを安全に再起動（`python3 bitbank5_46_1spot_limit.py --loop`）
* **データファイル（ログ・CSV等）の取得**:
  - VPSからローカルへログファイルやCSV・画像をダウンロードする目的の SCP は許可される。

---

## 10. Windows / クロスプラットフォーム動作保証規約

* **コマンドプロンプト / PowerShell 実行互換性**:
  - Windows側のコマンドプロンプト（cmd.exe）および PowerShell からスクリプトを実行した場合でも、未捕捉例外やエンコードエラー、パス差異等で異常終了しないようコードを設計・テストすること。
* **例外のフェイルセーフ防護**:
  - 外部API通信（Bitbank/Binance Public/Private REST、Discord等）でネットワーク障害やHTTPエラー（503, 502, 429等）が発生しても、`SystemExit` 等でメインプロセスが終了しないよう、`Exception` や `BaseException` レベルで安全に捕捉・隔離すること。
* **文字コード・パスの標準化**:
  - すべてのファイルIO操作は `encoding="utf-8"` を明示し、パス操作には `pathlib.Path` を使用すること。

---

## 11. データファイル最新保持・ディスク容量管理規約

* **保持期間と自動クリーンアップ**:
  - **ZIPファイル（`Data/*.zip`）**: 直近24時間（1日分）のみ保持。24時間超過分は毎時および定期同期時に自動削除。
  - **チャート画像（`Data/plots/*.png`）**: 最新セットのみ保持。24時間超過分は自動削除。
  - **一時CSV（`Data/*_1h_*.csv`）**: 生成後24時間経過で自動削除。
  - **保護対象**: キャッシュCSV（`historical_candles/`, `historical_candles_binance/`）、最新マスターCSV（`historical_all_symbols_merged.csv`, `binance_japan_all_symbols_merged.csv`）、送信台帳（`uploaded_files_registry.json`）は保護し、不要な重複ファイルを生成しない。
* **常駐プロセスのログ管理**:
  - `bot_output.log` 等のログファイルは、15MB上限の自動ローテーションを適用し、VPSのディスク容量を圧迫しないよう恒久的に監視・制御する。
