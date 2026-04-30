# ihealth-relay (日本語版 README)

> 英語版 README は [`../../README.md`](../../README.md) を参照.

iPhone 用アプリ **Health Auto Export — JSON+CSV** が iCloud Drive に
書き出す Apple Health の `.hae` (LZFSE 圧縮 JSON) を読み、14 種の日次
フィールド (日付 + 13 メトリクス) に集計し、複数の **pluggable Sink**
(Notion / Markdown / stdout / SQLite / Slack) に転記する macOS バッチ
ツール.

## ハイライト

- **Python 標準ライブラリのみ**: `requirements.txt` も venv も `pip install`
  も不要. Notion API は `urllib.request`, SQLite は bundled `sqlite3`,
  TOML は in-tree minimal parser (`src/ihealth/_toml.py`).
- **Apple Developer Program ($99/yr) 不要**: HealthKit entitlement を
  iPhone 側 Health Auto Export に委任することで回避.
- **Pluggable Sinks**: `--publisher notion | markdown | stdout | sqlite | slack | dryrun`.
  各 Sink は `src/ihealth/publishers/` 配下の独立モジュールで `Publisher`
  Protocol を実装.
- **冪等**: 同じ日付の再実行で Notion ページ / Markdown ファイル /
  SQLite 行を上書き. Notion 側の手入力値は保持される (`None` フィールドは
  update でスキップ).
- **永続通知**: 失敗時は Reminders.app に Reminder を作成
  (Notification Center の自動消滅とは違い、確認するまで残る).
- **macOS dataless `.hae` 対応**: iCloud Drive の `SF_DATALESS` フラグを
  検知して `brctl download` を自動実行. headless launchd 実行で起こりがちな
  silent EDEADLK を防ぐ ([詳細は case-study](../case-study/dataless-icloud.md)).

## アーキテクチャ

```
┌──────────────┐   AutoSync   ┌─────────────────────────┐
│  iPhone      │─────────────▶│ iCloud Drive            │
│  Health.app  │   .hae       │  HealthExport/AutoSync/ │
│  (HealthKit) │   (LZFSE     │   HealthMetrics/        │
│              │    JSON)     │     {metric}/           │
└──────────────┘              │       YYYYMMDD.hae      │
                              └────────┬────────────────┘
                                       │ iCloud 同期
                              ┌────────▼─────────────────┐
                              │ macOS                    │
                              │  python3 -m ihealth      │
                              │  (launchd 07:00 daily)   │
                              │  1. brctl download +     │
                              │     compression_tool     │
                              │  2. メトリクス別集計     │
                              │  3. Sink で配信          │
                              └────────┬─────────────────┘
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │ Sink:                                                    │
        │  • Notion DB (REST PATCH)                                │
        │  • Markdown (Obsidian frontmatter, atomic write)         │
        │  • stdout (JSON Lines)                                   │
        │  • SQLite UPSERT (BEGIN IMMEDIATE)                       │
        │  • Slack Incoming Webhook (mrkdwn summary)               │
        └──────────────────────────────────────────────────────────┘
```

詳細は [`docs/architecture.md`](../architecture.md).

## クイックスタート

完全な手順は [`docs/setup.md`](../setup.md) を参照. 概要:

1. iPhone に Health Auto Export をインストールし AutoSync を有効化
2. macOS でリポジトリを clone
3. `cp .env.example .env` して NOTION_SECRET / DATABASE_ID /
   HEALTH_EXPORT_DIR / BIRTH_DATE を記入
4. **(非英語列名の Notion DB を流用するなら)** `cp config.example.toml config.toml`
   して列名 override マッピングを設定
5. dry-run で動作確認:
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose
   ```
6. 本番実行 (前日分):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth
   ```
7. (任意) launchd 登録:
   ```bash
   ./bin/install-launchd.sh
   ```

## 取得データ

v0.1.0 で **Notion プロパティ名の既定が英語化** されました.
非英語の列名 (例: 日本語) を使う場合は `cp config.example.toml config.toml`
で override 可能.

| データ | 既定プロパティ名 (英語) | 日本語列名 override 例 | 単位 |
|---|---|---|---|
| 日付 | `Date` | `日付` | date |
| 歩数 | `Step Count` | `歩数 (歩)` | 歩 |
| 移動距離 | `Distance (km)` | `移動距離 (km)` | km |
| 消費カロリー | `Active Energy (kcal)` | `消費カロリー (kcal)` | kcal |
| 運動強度スコア | `Exercise Intensity Score` | `運動強度スコア` | pt |
| 平均心拍 | `Heart Rate Avg (bpm)` | `平均心拍数 (bpm)` | bpm |
| 最大心拍 | `Heart Rate Max (bpm)` | `最大心拍数 (bpm)` | bpm |
| 安静時心拍 | `Heart Rate Resting (bpm)` | `安静時心拍数 (bpm)` | bpm |
| 酸素飽和度 | `Oxygen Saturation (%)` | `酸素飽和度 (%)` | % |
| 睡眠時間 (夜主睡眠) | `Sleep Minutes` | `睡眠時間 (分)` | 分 |
| 昼寝 | `Nap Minutes` | `昼寝時間` | 分 |
| 瞑想回数 | `Mindful Sessions` | `瞑想回数 (回)` | 回 |
| 瞑想時間 | `Mindful Minutes` | `瞑想時間 (分)` | 分 |
| 体重 | `Body Mass (kg)` | `体重 (kg)` | kg |
| 体脂肪率 | `Body Fat (%)` | `体脂肪率 (%)` | % |

## プライバシーと健康データの取り扱い

> 本ツールは Apple Health 由来の健康データ (歩数, 距離, 心拍, SpO₂, 睡眠, 体重,
> 体脂肪率, 瞑想記録) を扱います. データは iCloud Drive からローカルに読み込み、
> `--publisher` で選んだ sink に転送します. 注意点:
>
> - **Notion / Slack publisher は健康データを HTTPS 経由で第三者サービスに送信**
>   します. Slack Free プランの retention は 90 日, Notion はあなたの private DB
>   に保存されます. Webhook URL や Notion token が漏れると 1 日サマリが第三者に
>   閲覧されます.
> - **Markdown / SQLite / stdout publisher はデータをローカルに保持**しますが、
>   出力先のセキュリティ (FS パーミッション, バックアップ暗号化) は利用者責任です.
> - **`BIRTH_DATE` は必須** (Tanaka 式の最大心拍数算出). `.env` は秘密ファイル
>   として扱ってください. 将来的に直接 `MAX_HEART_RATE` を渡す方式へ移行検討中.
> - **GDPR / HIPAA / 個人情報保護法**: 本ソフトウェアは規制対象の医療機器では
>   ありません. 規制環境での利用は運用者責任 (MIT 免責条項適用).
> - `dryrun` publisher は HTTPS を発射せずにログ出力のみ. 動作検証用.

## 開発

```bash
# テスト一式 (unittest, 外部依存なし)
PYTHONPATH=src /usr/bin/python3 -m unittest discover tests

# 手動実行ラッパー (launchd と同じエントリ)
./bin/ihealth-run --date 2026-04-22
```

CI は [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) で
macOS + Linux × Python 3.9-3.12 を回します (macOS arm64 では 3.9/3.10
セルは Apple ランナーが提供しないため exclude).

## 終了コード

| Code | Class | 原因 |
|---|---|---|
| 0 | — | 正常終了 |
| 1 | (catch-all) | 想定外例外 |
| 2 | argparse / `--*` validation | CLI 引数の形式エラー |
| 3 | `ConfigAppError` | `.env` / `config.toml` 設定不備 |
| 4 | `HealthExportDirError` | `HEALTH_EXPORT_DIR` 不在 / 非ディレクトリ |
| 5 | `SourceAppError` | `.hae` 取得 / 解凍失敗 |
| 6 | `NotionAppError` | Notion API 失敗 / ページ未作成 / 列名不一致 |
| 7 | `MarkdownAppError` | Markdown 書き込み失敗 |
| 8 | `StdoutAppError` | stdout 書き込み失敗 |
| 9 | `SQLiteAppError` | SQLite I/O / SQL / lock 競合 |
| 10 | `SlackAppError` | Slack Webhook HTTP 失敗 |

非ゼロ終了は Reminders.app に Reminder を立てて確認するまで残します.

## ドキュメント

- [`README.md` (英語)](../../README.md) — メイン
- [`architecture.md`](../architecture.md) — モジュール構成
- [`setup.md`](../setup.md) — 詳細セットアップ手順
- [`case-study/dataless-icloud.md`](../case-study/dataless-icloud.md) —
  silent EDEADLK の調査記録

## ライセンス

[MIT](../../LICENSE) — Atsuro Murata, 2026.
