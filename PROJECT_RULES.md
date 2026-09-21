# bitbank5_46 プロジェクト運用ルール & 開発規約

## 1. 全銘柄1時間足データダウンロード規約（ステップ2）

### 【規約】全銘柄1年分データ取得・日時付き統合ファイル保存＆Discord送信
* **対象スクリプト**: `download_historical_candles.py`
* **実行タイミング**: ボット起動時（ステップ2）または日次定期データ更新時
* **必須要件**:
  1. **全銘柄1年分（365日分）の完全取得**:
     * Bitbank 現物 JPY 全47銘柄（`btc_jpy`, `eth_jpy`, `xrp_jpy`, `sol_jpy`, `doge_jpy` ... 全47ペア）の過去365日分の1時間足OHLCVデータを網羅取得すること。
  2. **日時命名規約（重複・上書き防止）**:
     * 何回取得してもファイル名が被らないよう、生成するファイル名には **取得日付と時間（`YYYYMMDD_HHMMSS`）** を必ず付与すること。
     * ファイル命名例:
       - 日時付き統合CSV: `Data/bitbank_all_symbols_1h_YYYYMMDD_HHMMSS.csv`
       - Discord送信ZIP: `Data/bitbank_all_symbols_1h_YYYYMMDD_HHMMSS.zip`
       - ノーマライズ比較チャート: `Data/plots/bitbank_normalized_{30d|10d|5d}_YYYYMMDD_HHMMSS.png`
  3. **Discord送信仕様（環境別自動切替 ＆ 1個の統合ファイル）**:
     * 小分け送信ではなく、**全データが入った1個のファイル** として送信すること。
     * **送信先チャンネルの環境自動判別**:
       - **VPS（Linux環境）での本番稼働時**: **`real1_bitbank`** チャンネルへ自動出力
       - **Windows（win32環境）でのテスト運用時**: **`test4_test`**（旧 win32 / test4_backtest）チャンネルへ自動出力
     * Discord Webhook の容量制限（通常10MB〜25MB）を超える場合（1年分データは約26MB）、自動的に同名の日時付きZIP（約6MB）に圧縮して確実に送信すること。
  4. **スマート差分キャッシュ ＆ 重複送信防止**:
     * 個別銘柄CSV（`Data/historical_candles/{symbol}_1h.csv`）に蓄積し、未取得日のみを並行取得（`Semaphore(12)`）して高速化すること。
     * `upload_registry.py` と連携し、同一データ（MD5ハッシュ一致）の二重送信を防止すること。

---

## 2. Bitbank 取引所発注規約（※要検討項目含む）

* **現物取引（Spot / JPY建て / LONG ONLY）**:
  * Bitbank は現物取引のため、**買いエントリー（BUY）のみ（LONG ONLY）** とする。空売り（SHORT）は行わない。
* **Maker指値優先**:
  * Longエントリーは `best_bid`（買い気配最良値）または指値で発注する。
  * 未約定リトライ時は、前回の指値を安全に全キャンセル（`cancel_order`）した上で最新気配値に再配置する。
* **ロット・価格の丸め込み（Quantization）**:
  * 各通貨ペアの最小発注数量（Bitbank 現物は基本 `0.0001`）および価格小数点桁数（`sz_decimals: 4.0`, 各ペアの `price_place`）を `BITBANK_SPECS` に基づき厳格に適用する。
* **指定11銘柄**:
  * 対象銘柄: `BTC`, `ETH`, `XRP`, `SOL`, `DOGE`, `BNB`, `ARB`, `SUI`, `AVAX`, `RNDR（render_jpy）`, `LINK`
  * ※ Bitbank では RNDR は `render_jpy` へ移行済みのため、自動エイリアス正規化を行う。
* **【要検討事項】**:
  * 気配値直上/直下への指値配置オフセット幅、スプレッド急拡大時の発注見送り閾値などは今後実証データをもとに精緻化・ルール化する。

---

## 3. 資金管理・ポジション規約（※要検討項目含む）

* **レバレッジ設定**:
  * 現物取引のため、**レバレッジ1.0倍固定（`LEVERAGE_FACTOR = 1.0`）**。
* **必要資金チェック**:
  * 発注前に `current_price * lot` を算出し、口座の利用可能 JPY 残高（`jpy_available`）を下回る場合は発注を安全にスキップする。
* **ポジションサイズ**:
  * 目標投資額 15,000円（`BITBANK_TARGET_POSITION_VALUE_JPY = 15000.0`）。
* **最大同時保有数**:
  * 最大2ポジション（`MAX_ACTIVE_POSITIONS = 2`）を厳守し、資金効率とリスク分散を両立する。
* **最大監視銘柄数**:
  * 指定11銘柄すべてを常時監視・分析するため `MAX_SELECTED_SYMBOLS = 11`。
* **【要検討事項】**:
  * 口座全体の総資産に対する動的ロット計算、現物残高の余力配分比率は今後実証データをもとに最適化する。

---

## 4. Air Mode（安全監視）/ 口座切替フラグ規約

Bitbank にはデモ取引所が存在しないため、**本番口座（`BITBANK_IS_LIVE = True`）の残高・保有現物・板をリアルタイム取得し、発注のみ仮想シミュレーション（`BITBANK_IS_AIR = True` / ペーパートレード）で行う**：

```python
# [ 0 ] 安全ロック (Safety Interlock)
#       本番リアル口座への誤発注防止のため、本番実トレードは初期状態物理完全ロック
ALLOW_LIVE_TRADING = False

# [ 1 ] Bitbank 口座指定
#       True  = 本番口座 (Live Account - 実際の残高・ポジション・板データをAPIから取得)
BITBANK_IS_LIVE = True

# [ 2 ] Bitbank 注文実行・APIキー指定 (AIR Mode)
#       True  = AIRモード (ペーパートレードシミュレーション: 実発注APIを呼ばずにモック約定)
#       False = リアル注文 (ALLOW_LIVE_TRADING=True かつ --real-trade 指定時のみ実発注)
BITBANK_IS_AIR = ("--real-trade" not in sys.argv) or ("--air" in sys.argv)
```

* **安全運用方針**:
  * ボットは常に本番口座の JPY 残高および保有資産（例: 0.2434 BTC など）をスキャン・検出し、既存保有ポジションの急激な誤売却を防ぐ。
  * 実発注（リアル売買）を行う場合は、`ALLOW_LIVE_TRADING = True` への書き換えとコマンドライン引数 `--real-trade` の両方が揃わない限り発注されない二重ロックを厳守する。

---

## 5. 決済（Exit / Flatten）＆安全ロスカット規約（※要検討項目含む）

* **現物手仕舞い売り（SELL）**:
  * 買いポジションに対するエグジットは、保有現物の売却（`side="SELL"`）となる。
* **事前未約定キャンセル**:
  * 決済処理開始時に、対象銘柄の既存未約定指値をすべてキャンセル（`active_order_cancel`）する。
* **決済順序と安全制御**:
  * エグジット条件成立時（RSIMA クローズ、トレーリングストップ、または非常時手仕舞い）は、安全に指値または成行売りを発注する。
* **【要検討事項】**:
  * 手仕舞い時の板の厚さ（スリッページ）対策、成行売り vs Best Ask/Bid 指値売りの約定遅延リスク、最小発注端数残高（Dust）の取り扱い方針については今後実証検証を進めてルール化する。

---

## 6. VPS常駐・プロセス管理規約

* **自動死活監視（Cron）**:
  * `/etc/crontab` または `crontab -l` にて `check_and_restart.sh` を5分間隔（`*/5 * * * *`）で実行し、プロセスダウン時は自動再起動する。
  * 監視対象プロセス名は **`bitbank5_46_1spot_limit.py`** とする。
* **Python実行時アンバッファ**:
  * ログがリアルタイムに出力されるよう、必ず `python3 -u` オプションで起動する。
  * 起動コマンド例: `nohup nice -n 10 python3 -u "$HOME/bitbank5_46/bitbank5_46_1spot_limit.py" --air --loop --skip-history > "$HOME/bitbank5_46/bot_output.log" 2>&1 &`

---

## 7. コードデプロイ・同期運用規約（Git Push ➔ VPS Git Pull 方式）

* **SCP直接アップロードの禁止**:
  * Windowsローカル環境から VPS へプログラムコード（`.py` ファイル等）を SCP で直接上書きアップロードしてはならない（Git競合の原因となるため）。
* **標準デプロイワークフロー**:
  1. **ローカル**: コード修正 ➔ 構文・動作確認 ➔ `git add` ➔ `git commit` ➔ `git push origin main`
  2. **VPS側**: SSH接続 ➔ `cd /home/ubuntu/bingx5_46 && git pull origin main` で変更を取得・同期
  3. **再起動**: VPS側でボットプロセスを安全に再起動（`pkill` ➔ `check_and_restart.sh` または `nohup` 起動）
* **データファイル（ログ・CSV等）の取得**:
  * VPSからローカルへログファイルやCSV・画像をダウンロードする目的の SCP は許可される。

---

## 8. Windows / クロスプラットフォーム動作保証規約

* **コマンドプロンプト / PowerShell 実行互換性**:
  * Windows側のコマンドプロンプト（cmd.exe）および PowerShell からスクリプトを実行した場合でも、未捕捉例外やエンコードエラー、OS固有のファイルパスエラー（`\` と `/` の差異）、パーミッションチェックの不整合等で異常終了しないようにコードを設計・テストすること。
* **例外のフェイルセーフ防護**:
  * 外部API通信（Bitbank Public/Private REST、Discord等）でネットワーク障害やHTTPエラー（503, 502, 429等）が発生しても、`SystemExit` 等でメインプロセスが巻き込まれ終了しないよう、`Exception` や `BaseException` レベルで安全に捕捉・隔離すること。
* **文字コード・パスの標準化**:
  * すべてのファイルIO操作は `encoding="utf-8"` を明示し、パス操作には `pathlib.Path` を使用すること。
