# CLAUDE.md - ihealth-relay

## プロジェクト概要

iPhone の Health Auto Export アプリが Apple Health のデータを `.hae` (LZFSE 圧縮 JSON) で iCloud Drive に吐き出し、macOS 側の Python バッチがそれを解凍・集計して、複数の pluggable sink (Notion / Markdown / stdout / SQLite / Slack / dryrun) に自動転記するツール。
前身の GoogleFitNotionIntegration (Python / Google Fit / Cloud Functions) からの移行プロジェクト。

> **実装状況 (2026-04-30 / v0.1.0 リリース済み)**: Sink プラガブル化で `--publisher` フラグ経由で notion / markdown / stdout / sqlite / slack / dryrun を切り替え可能 (既定は `notion`). Notion PROPERTY_MAP は英語 Title Case 名が既定 (英語以外の DB 列名を持つユーザーは `config.toml` で override 可能). 主要メトリクス欠落時の retry (120s × 5 回, `step_count`/`heart_rate` のみ critical), launchd 自動実行 (07:00), Reminders.app 永続通知, dataless `.hae` の自動 materialize (`brctl download` + polling) も完了済. 残作業は Issue #11 (時間プロパティの hours 表記移行) と #13 (Heart Points 厳密 Google Fit 準拠).

**設計上の重要な選択**: Swift + HealthKit 直叩きは `com.apple.developer.healthkit` entitlement のために Apple Developer Program (年 99 USD) への加入が必須だったため断念し、iPhone 側の Health Auto Export にデータ抽出を委任する構成に変更した。

**`.hae` について (2026-04-24 再調査で判明)**: Health Auto Export が AutoSync で出力する `.hae` は独自バイナリではなく、**LZFSE (Apple 公開の圧縮アルゴリズム) で圧縮された JSON** である。macOS 標準の `/usr/bin/compression_tool` で解凍できるため、外部依存ゼロ原則を維持したまま Python から扱える。

## 技術スタック

- **言語**: Python 3.9+（macOS 標準同梱の `/usr/bin/python3` が 3.9.6 で凍結されているため、互換性を確保するのが最優先）
- **ランタイム**: `/usr/bin/python3`（macOS 14/15 いずれも Command Line Tools 経由で 3.9.x を提供、追加インストール不要）
- **プラットフォーム**: macOS 14+ (Sonoma), Apple Silicon (M4)
- **依存ライブラリ**: なし（Python 標準ライブラリのみ、`requirements.txt` も venv も不要）
- **iPhone 側**: Health Auto Export - JSON+CSV (App Store, App ID 1115567069, 買い切り Premium)
- **実行方式**: `python3 -m ihealth` → launchd で定時実行
- **通知**: AppleScript 経由で Reminders.app にリマインダーを作成（自動消滅しない、iPhone にも同期される）

## 開発コマンド

```bash
python3 -m ihealth                           # 昨日分のデータを処理
python3 -m ihealth --date 2026-04-10         # 指定日のデータを処理
python3 -m ihealth --dry-run                 # Notion に書き込まない動作確認
python3 -m ihealth --verbose                 # 詳細ログ
python3 -m unittest discover tests           # テスト実行
./bin/ihealth-run [--date YYYY-MM-DD]        # 手動実行ラッパー (launchd と同じエントリ)
./bin/install-launchd.sh                     # launchd へ登録
```

## ディレクトリ構成

- `src/ihealth/` - メインコード
  - `__main__.py` - エントリポイント (`python3 -m ihealth` で起動)
  - `config.py` - `.env` 読み込み
  - `logger.py` - ログ設定
  - `source.py` - Health Auto Export が iCloud Drive AutoSync 配下に出力した `.hae` の検出・LZFSE 解凍・アーカイブ
  - `models.py` - `DailyHealthData` dataclass
  - `publishers/notion.py` - Notion API クライアント (`urllib.request` ベース)
  - `notifier.py` - Reminders.app 連携
- `bin/` - 実行ラッパー・セットアップスクリプト
- `data/health/` - 受信した JSON のアーカイブ（`YYYY-MM-DD/{metric}.json` 形式、`.gitignore` 対象）
- `tests/` - ユニットテスト (`unittest`)
- `LaunchAgent/` - `com.ihealthrelay.daemon.plist`

## データフロー

1. **iPhone (常時)**: Health Auto Export の **AutoSync** (Premium) が HealthKit のデータを **メトリクス別 1 日 1 ファイル**の `.hae` (LZFSE 圧縮 JSON) として iCloud Drive 上の専用フォルダに書き出す。パスは `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/HealthMetrics/{metric_snake}/{YYYYMMDD}.hae`
2. **iCloud 同期**: `.hae` が macOS 側の iCloud Drive 配下から見えるようになる（Health Auto Export の iCloud Drive 専用領域なので、常に上記の固定パス）
3. **macOS (07:00)**: launchd が `python3 -m ihealth` を起動
4. **Python**: `HEALTH_EXPORT_DIR`（= `AutoSync` ディレクトリ）配下から対象日の 13 メトリクス分の `.hae` を検出
5. **Python**: `/usr/bin/compression_tool -decode -a lzfse` で各ファイルを解凍し、`./data/health/YYYY-MM-DD/{metric}.json` にアーカイブ（以降の処理はコピー先が正）
6. **Python**: メトリクスごとに JSON をパース → 単位フィルタ・重複除去・日次集計（SUM / MEAN / MAX） → `DailyHealthData` dataclass に変換
7. **Python**: 選択された publisher (`--publisher` 既定 `notion`) で出力. Notion なら日付検索 → 該当ページを PATCH で更新, Markdown なら frontmatter ファイルを atomic write, SQLite なら UPSERT, Slack なら Webhook 投稿, stdout なら JSON 1 行を標準出力
8. **失敗時**: Reminders.app にリマインダーを追加（ユーザーが完了するまで残り、iPhone 側にも iCloud 同期で届く）

## 重要な設計判断

1. **外部依存ゼロ**: サブスク・外部サービス依存なしが要件。HTTP は `urllib.request`、JSON は `json`、ファイル操作は `pathlib` で処理
2. **Apple Developer Program 回避**: HealthKit 直叩きをやめ、iPhone 側 Health Auto Export に委任
3. **CLI ファースト**: GUI なし。バックグラウンドで静かに動くことが優先
4. **`.env` ベースの設定**: `HEALTH_EXPORT_DIR` / `BIRTH_DATE` は常時必須. `NOTION_SECRET` / `DATABASE_ID` は `--publisher notion` を選んだ場合のみ必須で、`--dry-run` や他 sink (markdown / stdout / sqlite / slack) では不要. `BIRTH_DATE` は運動強度スコア (Heart Points) の最大心拍数 (Tanaka 式) 算出に使う
5. **冪等実装**: 同じ日付で何度実行しても Notion ページ上書き / Markdown ファイル再生成 / SQLite UPSERT / Slack 再投稿になるだけ、履歴管理不要
6. **エラー時の通知は永続的**: 通知センター（自動消滅する）ではなく Reminders.app を採用
7. **成功時は静か**: 成功ログのみ残し通知は出さない、ユーザー介入ゼロ運用

## Notion 日記DB のプロパティマッピング

`HealthMetrics/{dir}/{YYYYMMDD}.hae` を解凍すると `{"metric":..., "data":[...], "date":...}` 形式の JSON が得られる。13 メトリクスの対応と集計ルールは以下:

| 内部フィールド | HAE ディレクトリ (`HealthMetrics/<dir>/`) | HAE metric 名 | 集計ルール | 優先単位 | Notion プロパティ名 (英語既定) | 書き込み単位 |
|---|---|---|---|---|---|---|
| step_count | `step_count` | Step Count | SUM(qty) | count | `Step Count` | 歩 |
| distance_km | `walking_running_distance` | Walking + Running Distance | SUM(qty) | km | `Distance (km)` | km (round 1 桁) |
| active_energy_kcal | `active_energy` | Active Energy | SUM(qty) | **kcal** (kJ は捨てる) | `Active Energy (kcal)` | kcal (round 1 桁) |
| exercise_intensity_score | `heart_rate` | Heart Rate | Heart Points 近似: 各サンプルを `int(start // 60)` で 1 分 bucket に振り分け、bucket 平均 `avg` で zone 判定 (≥ 70% max_hr → 2pt, ≥ 50% → 1pt, それ未満 → 0pt) を総和 | — | `Exercise Intensity Score` | pt (round 1 桁) |
| heart_rate_avg | `heart_rate` | Heart Rate | MEAN(avg) | count/min | `Heart Rate Avg (bpm)` | bpm (round 1 桁) |
| heart_rate_max | `heart_rate` | Heart Rate | MAX(max) | count/min | `Heart Rate Max (bpm)` | bpm (round 1 桁) |
| heart_rate_resting | `resting_heart_rate` | Resting Heart Rate | MEAN(qty where unit="count/min") | count/min | `Heart Rate Resting (bpm)` | bpm (round 1 桁) |
| oxygen_saturation | `blood_oxygen_saturation` | Blood Oxygen Saturation | MEAN(avg or qty) | % | `Oxygen Saturation (%)` | % (round 1 桁) |
| sleep_hours | `sleep_analysis` | Sleep Analysis | **夜主睡眠**: end.hour JST ∈ [0,14) の asleep をソース別 SUM → MAX | hr | `Sleep Minutes` | **分** (hr × 60, 整数) |
| nap_hours | `sleep_analysis` | Sleep Analysis | **昼寝**: end.hour JST ∈ [14,20) の asleep をソース別 SUM → MAX | hr | `Nap Minutes` | **分** (hr × 60, 整数) |
| mindful_sessions | `mindful_minutes` | Mindful Minutes | COUNT(data) | — | `Mindful Sessions` | 回 |
| mindful_minutes | `mindful_minutes` | Mindful Minutes | SUM((end - start) / 60) | — | `Mindful Minutes` | 分 (round 整数) |
| body_mass_kg | **`weight_body_mass`** | Weight & Body Mass | 最新 qty | **kg** (lb は捨てる) | `Body Mass (kg)` | kg (round 1 桁) |
| body_fat_percentage | `body_fat_percentage` | Body Fat Percentage | 最新 qty | % | `Body Fat (%)` | % (round 1 桁) |

📌 **Notion プロパティ名の表記ルール** (Phase 1 B2 で 2026-04-29 に英語化):
- 既定は `Step Count` / `Distance (km)` / `Body Mass (kg)` 等の **Title Case + 単位 (km, kcal, bpm, %)**. Notion API で文字列完全一致が必要なので、空白・括弧の幅 (半角) も含めて正確に揃える.
- 既存の Notion DB が英語以外の列名 (例: 日本語 `歩数 (歩)`) の場合は `config.toml` でフィールドごとに override する. `config.example.toml` に日本語マッピング例を記載済み.
- 日付列の名前 (`find_page_by_date` の filter キー) も `Date` を既定とし、別名なら `[publishers.notion] date_property = "日付"` 等で override する.
- Notion API で DB に存在しないプロパティは `publishers/notion.py` が**警告ログだけ残してスキップ**する設計なので、追加プロパティ (例: `Nap Minutes`) が DB に無くても他の書き込みは壊れない.

📌 **将来 TODO**: 当面は `Sleep Minutes` / `Nap Minutes` / `Mindful Minutes` のように分単位で書き込むが、小数 2 桁の時間表記 (例: `7.2h`) のほうが直感的なので、プロパティ名ごと後日リネーム予定 (Issue #11)。

⚠️ **罠ポイント**:
- `body_mass_kg` のディレクトリは `weight_body_mass` で、`body_mass_index` (BMI) とは**別物**。誤って BMI を書き込まないよう、明示的ホワイトリスト `METRIC_DIRS` で紐付ける
- `active_energy` / `weight_body_mass` / `apple_sleeping_wrist_temperature` などは **同じ qty を複数単位で重複出力**するため、メトリクスごとに優先単位を固定し、それ以外を捨てる
- `sleep_analysis` は AutoSleep と Apple Watch から**同じ期間の睡眠データが重複**して来る。ソースごとに合計 → 最大ソースを採用（単純 SUM は二重計上 / 単純 MAX は 1 フェーズしか拾えない）
- `sleep_analysis` の `.hae` は**開始日基準**で分割されるので、当日ファイルに「前夜→当日朝」の睡眠と「当日夜→翌朝」の未完就寝が混在する。`end.hour` JST で切り分けて `sleep_hours` (夜主睡眠) と `nap_hours` (昼寝) に振り分ける
- 秒単位タイムスタンプ (`start`/`end`) は **Mac absolute time** (2001-01-01 UTC 起点) なので、必要に応じて `datetime(2001,1,1) + timedelta(seconds=...)` で変換

## コーディング規約

- **Python 3.9 互換を維持**（`/usr/bin/python3` がこのバージョンのため）
- 各モジュール先頭に `from __future__ import annotations` を置き、`dict[str, str]` / `Path | None` などの新しい型ヒント構文を文字列として保持する（runtime では評価されないので 3.9 でも動く）
- **使ってはいけない機能**: `match` 文 (3.10+), `dataclass(slots=True)` (3.10+), `typing.Self` (3.11+), ExceptionGroup (3.11+)
- PEP 8 準拠（`snake_case`, 4 スペースインデント）
- 型ヒント (type hints) 必須
- エラーハンドリングは `try...except` で明示的に
- `@dataclass(frozen=True)` と `pathlib.Path` を積極利用
- コメントは日本語 OK
- 標準ライブラリ以外は使わない（`requests` ですら使わない、`urllib.request` で統一）

## 注意事項

- **`/usr/bin/python3` のバージョン確認**: Python 3.9 未満では動かない。起動時に `sys.version_info` でチェックして明示的に失敗させる。macOS の標準 Python は長年 3.9.x で凍結されているため、通常このチェックは通過する
- **iPhone ロック中は AutoSync が走らない可能性**: iOS の制約。就寝中は充電器接続＋AOD ON 運用が推奨。ロック解除が確実でない場合は、起床後に iPhone 側で Health Auto Export を一度開いて同期を確実にさせ、macOS 側で `./bin/ihealth-run` を手動実行する動線を使う
- **Health Auto Export 出力先は `.env` で指定**: `HEALTH_EXPORT_DIR` に絶対パスで指定する。出力先は Health Auto Export の **AutoSync** (Premium) を使用する。AutoSync は iCloud Drive 上の固定パス (`~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync`) にメトリクス別の `.hae` を書き出す。パスに iCloud 系の `iCloud~com~ifunography~HealthExport` が含まれるが、中間の `~` は iCloud フォルダ名の一部なので `Path.expanduser()` が先頭の `~/` だけを展開する挙動に依存する
- **`.hae` の解凍には `/usr/bin/compression_tool` を使う**: Xcode Command Line Tools に含まれる macOS 標準ツール。Python からは `subprocess.run(["/usr/bin/compression_tool", "-decode", "-a", "lzfse", "-i", src, "-o", dst])` で呼び出す。LZFSE は Apple が公開しているアルゴリズム（[GitHub: lzfse/lzfse](https://github.com/lzfse/lzfse)）
- **`.hae` の実体は JSON**: 「独自バイナリ」ではなく LZFSE で圧縮された UTF-8 JSON。解凍後のスキーマはメトリクスによって異なる（秒サンプル / 事前集計済み avg-min-max / 睡眠ステージなど）ので、メトリクスごとに集計戦略を分岐する
- **Notion API のレート制限**: 平均 3 リクエスト/秒。429 応答時は指数バックオフでリトライ（`publishers/notion.py` に実装）
- **`.env` の必須キー**: 常時必須キーは `HEALTH_EXPORT_DIR` / `BIRTH_DATE` で、`NOTION_SECRET` / `DATABASE_ID` は `--publisher notion` 選択時のみ必須 (markdown / stdout / sqlite / slack / dryrun では空でも起動できる)。`.env` には ihealth-relay が参照しないキーを残さないこと (`gitignore` 済とはいえ、誤コミット時の漏洩リスクを最小化する)。
