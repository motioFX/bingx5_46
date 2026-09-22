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

## 6. VPS常駐・プロセス管理規約

* **指定運用VPS（オラクルナンバー2）接続情報**:
  * **ホスト**: `158.101.157.117`
  * **ユーザー**: `ubuntu`
  * **秘密鍵**: `C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2`
  * **SSH接続コマンド**:
    ```bash
    ssh -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2" ubuntu@158.101.157.117
    ```
  * **VPSプロジェクトパス**: `/home/ubuntu/bingx5_46`
  * **仮想環境パス**: `/home/ubuntu/pybot-env`
* **自動死活監視（Cron）**:
  * `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` を5分間隔（`*/5 * * * *`）で実行し、プロセスダウン時は自動再起動する。
* **Python実行時アンバッファ**:
  * ログがリアルタイムに出力されるよう、必ず `python3 -u` オプションで起動する。

---

## 7. コードデプロイ・同期運用規約（Git Push ➔ VPS Git Pull 方式）

* **SCP直接アップロードの禁止**:
  * Windowsローカル環境から VPS へプログラムコード（`.py` ファイル等）を SCP で直接上書きアップロードしてはならない（Git競合の原因となるため）。
* **標準デプロイワークフロー**:
  1. **ローカル**: コード修正 ➔ 構文・動作確認 ➔ `git add` ➔ `git commit` ➔ `git push origin main`
  2. **VPS側**: SSH接続 (`ssh -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2" ubuntu@158.101.157.117`) ➔ `cd /home/ubuntu/bingx5_46 && git pull origin main` で変更を取得・同期
  3. **再起動**: VPS側でボットプロセスを安全に再起動（`pkill` ➔ `nohup` または `check_and_restart.sh` 起動）
* **データファイル（ログ・CSV等）の取得**:
  * VPSからローカルへログファイルやCSV・画像をダウンロードする目的の SCP は許可される。
    ```bash
    scp -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2" ubuntu@158.101.157.117:/home/ubuntu/bingx5_46/bot_output.log ./scratch/
    ```

---

## 8. Windows / クロスプラットフォーム動作保証規約

* **コマンドプロンプト / PowerShell 実行互換性**:
  * Windows側のコマンドプロンプト（cmd.exe）および PowerShell からスクリプトを実行した場合でも、未捕捉例外やエンコードエラー、OS固有のファイルパスエラー（`\` と `/` の差異）、パーミッションチェックの不整合等で異常終了しないようにコードを設計・テストすること。
* **例外のフェイルセーフ防護**:
  * 外部API通信（Hyperliquid、BingX、Discord等）でネットワーク障害やHTTPエラー（503, 502, 429等）が発生しても、`SystemExit` 等でメインプロセスが巻き込まれ終了しないよう、`Exception` や `BaseException` レベルで安全に捕捉・隔離すること。
* **文字コード・パスの標準化**:
  * ファイルIOはすべて `encoding="utf-8"` を明示し、パス操作には `pathlib.Path` を使用すること。

---

## 9. 戦略ロジック規約（Envelope 戻りエントリー ＆ RSIMA 比較前提運用）

### 【基本運用方針】RSIMA 比較前提の自動最適化
* **二大戦略の常時コンペティション**:
  * 単一戦略に固定せず、8時間ごとの銘柄見直し・個別最適化（`optimize_symbol_strategy`）時に、監視対象全銘柄について **「Envelope戦略」と「RSIMA戦略」を同一条件下でバックテスト比較** する。
  * 過去32日データで PnL・勝率・ドローダウン（DD）を総合評価し、各銘柄の特性に合致した最優秀戦略を動的に採用する。
* **戦略キャラクターの住み分け**:
  * **Envelope戦略**: ドローダウンが極小（3〜6 USDT）かつ高勝率（65〜80%）。階段状に資産を伸ばす安定型（ARB, UNI等で優位）。
  * **RSIMA戦略**: トレンド発生時の一撃爆発力（+40〜65 USDT）が高い攻撃型（NEAR, ZEC等で優位）。

### 【Envelope 戦略】詳細仕様
* **インジケーター定義**:
  * 中心線: `basis = EMA(close, length)` （探索範囲: `length` = 10, 15, 20, 25）
  * 下限バンド: `env_lower = basis * (1.0 - lower_pct / 100.0)` （探索範囲: `lower_pct` = 1.5, 2.0, 2.5, 3.0%）
  * 上限バンド: `env_upper = basis * (1.0 + upper_pct / 100.0)`
* **エントリー規約（戻りエントリー）**:
  * 落ちてくるナイフを拾うリスクを排除するため、「下限バンド割れ即買い」を禁止し、**「一度下限ライン（`env_lower`）を割り込んだ後、確定足の終値がバンド下限を上抜けてバンド内へ復帰したタイミング（ゴールデンクロス・反発初動確認）」** でのみエントリーする。
  * 戻り確認と固定SL（3%）の組み合わせにより、長期MAフィルターは解除し、下落・レンジ相場からの強い自律反発・底打ち初動を広く安全に捕捉する。
* **イグジット規約（過剰最適化ゼロの1行ラチェット式 VAH 連携トレーリング）**:
  * **利確トリガー**: `close > basis`（中心線EMA上抜け）かつ `現在値 > 建値` でトレーリングTP発動。
  * **ストップライン計算式**:
    $$\text{trail\_stop} = \max(\text{最高値} \times (1.0 - 0.015), \text{VAH}, \text{建値} \times 1.001)$$
    - **平常相場**: コールバック幅 **1.5%**（旧0.8%から適正化）のゆったりした幅でノイズを耐え、利益を伸ばす。
    - **大相場突入時**: 価格が VAH（出来高集中帯上限）を上に突き抜けた場合、条件分岐なしで自動的に **「VAH割れまで無制限ホールド」** に昇格。
  * **損切り**: 建値から -3.0% 下落で「固定ストップロス（3.0% SL）」即時成行決済。

### 【RSIMA 戦略】詳細仕様
* **インジケーター定義**:
  * RSI: `calc_rsi(close, rsi_len)` （`rsi_len` = 7, 9, 14）
  * 平滑線: `lrsiMA = calc_ema(rsi, lma_len)` （`lma_len` = 5, 7, 10）
* **エントリー規約**:
  * RSI が平滑線 lrsiMA を下から上にゴールデンクロスし、かつ `lrsiMA < lEp`（売られすぎ水準: 30〜45）の確定足でエントリー。
* **イグジット規約（過剰最適化ゼロの1行ラチェット式 VAH 連携トレーリング）**:
  * **利確トリガー**: `rsi > lCp`（利確閾値: 55〜70）到達かつ `現在値 > 建値` でトレーリングTP発動。
  * **ストップライン計算式**: Envelope と同様の 1行ラチェット式（1.5%幅 ＋ VAH大相場ホールド）を適用。
  * **損切り**: 建値から -3.0% 下落で固定SL即時成行決済。



