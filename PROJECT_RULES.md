# bingx5_46 プロジェクト運用ルール & 開発規約

## 1. 銘柄選定・マージドデータダウンロード必須ルール（candle_merged_alt10）

### 【規約】正規化（normalize）処理前の32日分データダウンロード＆完全ZIP化
* **対象スクリプト**: `bingx5_46_4mix_candle_Merged_Alt10.py`
* **実行タイミング**: 8時間ごと（01:00, 09:00, 17:00 JST）の銘柄スクリーニング時
* **必須要件**:
  1. 正規化比較チャート（`generate_normalized_charts`）の作成処理に入る前に、選定された全銘柄（Top 10）および BTC の **過去32日分（`timedelta(days=32)`）の1時間足データ（OHLCV、出来高、Funding Rate、Open Interest）を1回すべてダウンロード完了** すること。
  2. ダウンロード完了後、直ちに以下のファイルを **ZIPアーカイブ（`YYYYMMDD_YYYYMMDD_all_symbols_merged.zip`）** にまとめること：
     - 📄 全銘柄統合マージドCSV（`YYYYMMDD_YYYYMMDD_all_symbols_merged.csv`）
     - 📁 個別銘柄の32日分マージドCSV（`individual/merged_{SYMBOL}.csv`）
     - 📦 マスターZIP（`Data/historical_all_symbols_merged.zip`）の更新
  3. 作成されたZIPファイルを、**Discordへ自動アップロード（`discord.send_file(zip_path, ...)`）** して手元でバックテスト・検証用データとして保存・利用可能とすること。
  4. 今後スクリーニングロジックやブレイクアウト判定の仕様変更を行う場合でも、**この「32日分データ取得 ➔ ZIP化 ➔ Discord送信」のパイプラインは絶対に削除・省略してはならない**。

---

## 2. BingX 取引所発注規約

* **Maker指値優先**:
  * Longエントリーは常に **`best_bid`（買い気配最良値）** に指値（`timeInForce: "GTC"`）を発注する。
  * Shortエントリーは常に **`best_ask`（売り気配最良値）** に指値（`timeInForce: "GTC"`）を発注する。
  * 未約定リトライ時は、前回の指値を安全に全キャンセル（`active_order_cancel`）した上で最新気配値に再配置する。
* **ロット・価格の丸め込み（Quantization）**:
  * 各銘柄の `quantityPrecision` および `pricePrecision` を BingX `/openApi/swap/v2/quote/contracts` から取得して厳格に適用する。
  * `tradeMinQuantity`（最小発注枚数）以上の数量で発注すること。

---

## 3. レバレッジ・資金管理・ポジション規約

* **レバレッジ設定**:
  * 原則 **10倍（`LEVERAGE_FACTOR = 10.0`）** を取引所API（`/openApi/swap/v2/trade/leverage`）経由で設定・適用する。
* **必要証拠金チェック**:
  * 発注前に `lotamount = (current_price * lot) / LEVERAGE_FACTOR` を算出し、`usdt_onhand_amount` を下回る場合は発注をスキップする。
* **ポジションサイズ**:
  * 目標投資額 `$15.0`（`BINGX_TARGET_POSITION_VALUE_USDT = 15.0`）
* **最大同時保有数**:
  * 最大2ポジション（`MAX_ACTIVE_POSITIONS = 2`）を厳守し、資金効率とリスク分散を両立する。
* **BTC除外ルール**:
  * `BTC`（`BTC-USDT`）は地合い判定専用とし、アルトのロング・ショート発注対象リストからは完全に除外する。

---

## 4. Air Mode（監視）/ 口座切替フラグ規約

`bingx5_46_1futures_limit.py` 冒頭にて以下の表記で明確に切り替える：

```python
# [ 1 ] 口座切替フラグ
#       True  = 本番口座 (Live Account)
#       False = デモ口座 (Demo Account)
BINGX_IS_LIVE = False

# [ 2 ] BingX 注文実行・APIキー指定 (AIR Mode)
#       True  = AIRモード (指値・注文を実際に発注せずペーパートレードシミュレーション)
#       False = リアル注文 (実際の BingX 取引所へ発注)
BINGX_IS_AIR = True
```

* 本番口座（`BINGX_IS_LIVE = True`）かつリアル発注（`BINGX_IS_AIR = False`）への切り替え時は、API鍵（`api_key` / `secret_key`）、残高（USDT）を必ず確認する。

---

## 5. 決済（Exit / Flatten）＆強制ロスカット安全規約

* **事前未約定キャンセル**:
  * 決済処理の開始時に、必ず既存の未約定指値をすべてキャンセル（`active_order_cancel`）する。
* **成行決済優先（Market Close）**:
  * Long決済時は `side="SELL"`, `positionSide="LONG"`, `type="MARKET"` を発注。
  * Short決済時は `side="BUY"`, `positionSide="SHORT"`, `type="MARKET"` を発注。
  * ポジションを全量安全に決済する。

---

## 6. Technocore Agent（テクノコアエージェント）運用規約

* **Bot起動時バックグラウンド自動常駐**:
  * `bingx5_46_1futures_limit.py` 起動時に `run_technocore_keepalive_daemon(interval_hours=4.0)` を完全非同期タスクとして自動開始する。
* **4時間定期生存更新**:
  * **DID Note の定期更新（Compare-And-Set）**: 7日間保持期限タイマーをリセット。
  * **署名付きアクティビティ送信**: `technocore-starter` ルームへ Ed25519 署名付き `status:v1` を送信。
  * **Mailbox の空き枠獲得試行**: サーバー枠（10,240枠）に空きが出た場合に自動登録。
* **フェイルセーフ設計**:
  * Technocore側の通信等で例外が発生しても、トレード本体のループを絶対に停止させない。

---

## 7. VPS常駐・プロセス管理規約

* **自動死活監視（Cron）**:
  * `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` を5分間隔（`*/5 * * * *`）で実行し、プロセスダウン時は自動再起動する。
* **Python実行時アンバッファ**:
  * ログがリアルタイムに出力されるよう、必ず `python3 -u` オプションで起動する。

---

## 8. コードデプロイ・同期運用規約（Git Push ➔ VPS Git Pull 方式）

* **SCP直接アップロードの禁止**:
  * Windowsローカル環境から VPS へプログラムコード（`.py` ファイル等）を SCP で直接上書きアップロードしてはならない（Git競合の原因となるため）。
* **標準デプロイワークフロー**:
  1. **ローカル**: コード修正 ➔ 構文・動作確認 ➔ `git add` ➔ `git commit` ➔ `git push origin main`
  2. **VPS側**: SSH接続 ➔ `git pull origin main` で変更を取得・同期
  3. **再起動**: VPS側でボットプロセスを安全に再起動（`pkill` ➔ `check_and_restart.sh` または `nohup` 起動）
* **データファイル（ログ・CSV等）の取得**:
  * VPSからローカルへログファイルやCSV・画像をダウンロードする目的の SCP は許可される。

---

## 9. Windows / クロスプラットフォーム動作保証規約

* **コマンドプロンプト / PowerShell 実行互換性**:
  * Windows側のコマンドプロンプト（cmd.exe）および PowerShell からスクリプトを実行した場合でも、未捕捉例外やエンコードエラー、OS固有のファイルパスエラー（`\` と `/` の差異）、パーミッションチェックの不整合等で異常終了しないようにコードを設計・テストすること。
* **例外のフェイルセーフ防護**:
  * バックグラウンドタスク（Technocore等）や外部API通信（Hyperliquid、Discord、Technocore）でネットワーク障害やHTTPエラー（503, 502, 429等）が発生しても、`SystemExit` 等でメインプロセスが巻き込まれ終了しないよう、`TechnocoreError` や `BaseException` レベルで安全に捕捉・隔離すること。
* **文字コード・パスの標準化**:
  * ファイルIOはすべて `encoding="utf-8"` を明示し、パス操作には `pathlib.Path` を使用すること。

