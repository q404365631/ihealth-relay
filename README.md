# ihealth-relay

A macOS daemon that pulls Apple Health data exported by the iPhone app
**Health Auto Export — JSON+CSV** (AutoSync, `.hae` LZFSE-compressed JSON in
iCloud Drive), aggregates them into 14 daily fields (date + 13 metrics), and pushes the result to
**pluggable sinks**: Notion, Markdown (Obsidian), stdout (JSON line),
SQLite, Slack — with zero external Python dependencies.

[日本語版 README はこちら](docs/ja/README.ja.md)

## Highlights

- **Zero external Python dependencies**: standard library only — no
  `requirements.txt`, no `venv`, no `pip install`. Notion API is called via
  `urllib.request`; SQLite uses the bundled `sqlite3`; TOML parsing uses a
  minimal in-tree parser (`src/ihealth/_toml.py`).
  *(Note: this refers to PyPI packages only. Network access to Notion / Slack
  endpoints and iCloud Drive / Health Auto Export are obvious external
  dependencies of the chosen sinks.)*
- **No Apple Developer Program ($99/yr) required**: avoids
  `com.apple.developer.healthkit` entitlement by delegating data extraction
  to Health Auto Export on the iPhone.
- **Pluggable Sinks**: `--publisher notion | markdown | stdout | sqlite | slack | dryrun`.
  Each sink is an independent module under `src/ihealth/publishers/` that
  implements the small `Publisher` Protocol.
- **Idempotent**: re-running for the same date overwrites the existing
  Notion page / Markdown file / SQLite row. Manually-entered values in
  Notion are preserved (`None` fields are skipped on update).
- **Persistent failure notifications**: failures create a Reminder in
  Reminders.app (no auto-dismiss like Notification Center).
- **macOS dataless-`.hae` aware**: detects iCloud Drive's `SF_DATALESS`
  flag, triggers `brctl download`, and waits for materialization before
  decompressing. Fixes silent EDEADLK in headless launchd autoruns.

## Architecture

```
┌──────────────┐   AutoSync   ┌─────────────────────────┐
│  iPhone      │─────────────▶│ iCloud Drive            │
│  Health.app  │   .hae       │  HealthExport/AutoSync/ │
│  (HealthKit) │   (LZFSE     │   HealthMetrics/        │
│              │    JSON)     │     {metric}/           │
└──────────────┘              │       YYYYMMDD.hae      │
                              └────────┬────────────────┘
                                       │ iCloud sync
                              ┌────────▼─────────────────┐
                              │ macOS                    │
                              │  python3 -m ihealth      │
                              │  (launchd 07:00 daily)   │
                              │  1. brctl download +     │
                              │     compression_tool     │
                              │  2. per-metric aggregate │
                              │  3. publish via sink(s)  │
                              └────────┬─────────────────┘
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │ Sink:                                                    │
        │  • Notion DB (REST PATCH)                                │
        │  • Markdown file (Obsidian frontmatter, atomic write)    │
        │  • stdout (JSON Lines)                                   │
        │  • SQLite UPSERT (BEGIN IMMEDIATE)                       │
        │  • Slack Incoming Webhook (mrkdwn summary)               │
        └──────────────────────────────────────────────────────────┘
```

See [`docs/architecture.md`](docs/architecture.md) for module-level details.

## Quick Start

### Requirements

- macOS 14+ (Sonoma) on Apple Silicon (also tested on Intel)
- Python 3.9+ (`/usr/bin/python3` is bundled with macOS as 3.9.x)
- iPhone with iCloud syncing on, and **Health Auto Export — JSON+CSV**
  (App ID 1115567069) Premium one-time purchase

### Setup

1. **iPhone**: install Health Auto Export, grant HealthKit permissions for
   the 13 metrics below, enable **AutoSync** to iCloud Drive (Premium feature).
2. **macOS**: clone this repository.
3. Copy `.env.example` to `.env` and fill in:
   ```bash
   NOTION_SECRET     # Notion integration token (only if --publisher notion)
   DATABASE_ID       # Notion DB ID (URL last 32 chars; only if notion)
   HEALTH_EXPORT_DIR # AutoSync root in iCloud Drive
   BIRTH_DATE        # YYYY-MM-DD; used to compute max heart rate (Tanaka formula)
   # Optional sink-specific:
   MARKDOWN_OUTPUT_DIR   # required if --publisher markdown
   SQLITE_DB_PATH        # required if --publisher sqlite
   SLACK_WEBHOOK_URL     # required if --publisher slack
   ```
4. **(Optional) Notion column-name override**: if your Notion DB uses
   non-default column names (e.g., legacy Japanese names like `歩数 (歩)`),
   copy the template:
   ```bash
   cp config.example.toml config.toml
   # Edit `config.toml` to map internal field names → your column names.
   # `config.toml` is gitignored (per-user setting).
   ```
5. Try a dry-run first (no HTTP, no file write):
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose
   ```
6. Real run for yesterday:
   ```bash
   PYTHONPATH=src /usr/bin/python3 -m ihealth
   ```
7. **Schedule via launchd** (optional):
   ```bash
   ./bin/install-launchd.sh   # installs LaunchAgent/com.ihealthrelay.daemon.plist
   ```

See [`docs/setup.md`](docs/setup.md) for detailed instructions, including
Notion DB schema setup and per-sink configuration.

## Privacy & Health Data Handling

> **This tool processes Apple Health data** (steps, distance, heart rate,
> SpO₂, sleep stages, weight, body fat, mindfulness sessions). Data is
> read locally from iCloud Drive and forwarded to whichever sink you
> configure. Be aware:
>
> - **Notion / Slack publishers transmit health data over HTTPS to those
>   third-party services.** Slack messages are retained per Slack's data
>   policy (90 days on the Free tier). Notion stores them in your private
>   database. Webhook URL or Notion token leakage exposes your daily
>   health summary.
> - **Markdown / SQLite / stdout publishers keep data local**, but the
>   destination directory or DB file is your responsibility to secure
>   (filesystem permissions, full-disk backup encryption, etc.).
> - **`BIRTH_DATE` is required** to compute max heart rate (Tanaka
>   formula). Treat `.env` as a secret file. A future release may switch
>   to direct `MAX_HEART_RATE` configuration to avoid storing date of
>   birth (PRs welcome).
> - **GDPR / HIPAA / 個人情報保護法**: This software is **not** a
>   regulated medical device. Use in regulated environments is the
>   operator's responsibility. The MIT license disclaimer applies.
> - The `dryrun` publisher prints the would-be Notion payload to logs
>   without HTTPS — useful for verifying behavior without external
>   transmission.

## Sinks

Pick one with `--publisher`. Default is `notion`. `--dry-run` always
overrides to `dryrun` regardless of `--publisher`.

| Sink | CLI | Output | Required config |
|---|---|---|---|
| `notion` (default) | `python3 -m ihealth` | Daily journal page in Notion | `NOTION_SECRET`, `DATABASE_ID` in `.env` |
| `markdown` | `--publisher markdown` | One `YYYY-MM-DD.md` per day in `output_dir` | `MARKDOWN_OUTPUT_DIR` in `.env` or `--markdown-out PATH` |
| `stdout` | `--publisher stdout` | Single JSON line on stdout | (none) |
| `sqlite` | `--publisher sqlite` | UPSERT into `daily_health` table | `SQLITE_DB_PATH` in `.env` or `--sqlite-path PATH` |
| `slack` | `--publisher slack` | Daily summary via Incoming Webhook | `SLACK_WEBHOOK_URL` in `.env` or `--slack-webhook URL` |
| `dryrun` | `--dry-run` or `--publisher dryrun` | Logs the Notion payload (no HTTP) | (none) |

Examples:

```bash
# Pipe to jq for ad-hoc queries
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher stdout --date 2026-04-22 \
  | jq '.step_count'

# Build a local SQLite warehouse
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher sqlite \
  --sqlite-path ~/health.db --date 2026-04-22

# Daily Slack summary
PYTHONPATH=src /usr/bin/python3 -m ihealth --publisher slack \
  --slack-webhook https://hooks.slack.com/services/T0/B0/secret
```

## Collected Data

Default Notion property names (English, since v0.1.0). Customize via
`config.toml` if your DB uses different names.

| Field | Default Notion property | Unit |
|---|---|---|
| Date (filter key) | `Date` | date |
| Step count | `Step Count` | count |
| Distance | `Distance (km)` | km |
| Active energy | `Active Energy (kcal)` | kcal |
| Exercise intensity (Heart Points equivalent) | `Exercise Intensity Score` | pt |
| Heart rate (avg) | `Heart Rate Avg (bpm)` | bpm |
| Heart rate (max) | `Heart Rate Max (bpm)` | bpm |
| Heart rate (resting) | `Heart Rate Resting (bpm)` | bpm |
| Oxygen saturation | `Oxygen Saturation (%)` | % |
| Sleep (main night sleep) | `Sleep Minutes` | min |
| Nap | `Nap Minutes` | min |
| Mindful sessions | `Mindful Sessions` | count |
| Mindful minutes | `Mindful Minutes` | min |
| Body mass | `Body Mass (kg)` | kg |
| Body fat | `Body Fat (%)` | % |

> **Exercise Intensity Score** is a *Heart-Points-style* approximation
> (the term "Heart Points" is a trademark of Google LLC; this implementation
> is independent and not affiliated with or endorsed by Google). Each
> heart rate sample is bucketed into a 1-minute window (`int(start // 60)`),
> and each bucket's mean `avg` selects a zone: `≥ 70% max_hr → 2 pt`,
> `≥ 50% → 1 pt`, otherwise `0 pt`. Bucket points are summed. Max heart
> rate uses the Tanaka formula (`205.8 − 0.685 × age`), with age derived
> from `.env`'s `BIRTH_DATE`. Health Auto Export emits sparse instantaneous
> samples (often hundreds of seconds apart), so strict "1 continuous
> minute" matching as Google Fit specifies is not achievable from this
> data shape — the bucket-mean approximation slightly overcounts compared
> to the official Google Fit value (tracked in issue #13).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — module-level overview
- [`docs/setup.md`](docs/setup.md) — detailed setup including Notion DB
- [`docs/case-study/dataless-icloud.md`](docs/case-study/dataless-icloud.md) —
  debugging silent EDEADLK from iCloud Drive dataless placeholders
- [`docs/ja/README.ja.md`](docs/ja/README.ja.md) — original Japanese README

## Development

```bash
# Run all tests (unittest, no external deps)
PYTHONPATH=src /usr/bin/python3 -m unittest discover tests

# Manual run wrapper (same env as launchd)
./bin/ihealth-run --date 2026-04-22
```

CI: see [`.github/workflows/ci.yml`](.github/workflows/ci.yml). Tests run
on macOS + Linux against Python 3.9 / 3.10 / 3.11 / 3.12.

## Exit Codes

| Code | Class | Cause |
|---|---|---|
| 0 | — | Success |
| 1 | (catch-all) | Unexpected exception |
| 2 | argparse / `--*` validation | CLI argument format error |
| 3 | `ConfigAppError` | `.env` / `config.toml` misconfigured |
| 4 | `HealthExportDirError` | `HEALTH_EXPORT_DIR` missing or not a directory |
| 5 | `SourceAppError` | `.hae` fetch / decompression failed |
| 6 | `NotionAppError` | Notion API failure / page not found / column-name mismatch |
| 7 | `MarkdownAppError` | Markdown write failed (disk full, permission, …) |
| 8 | `StdoutAppError` | stdout write failed (closed, encoding, …) |
| 9 | `SQLiteAppError` | SQLite I/O / SQL / lock contention |
| 10 | `SlackAppError` | Slack webhook HTTP failure |

Every non-zero exit creates a Reminders.app reminder so the failure stays
visible until acknowledged.

## License

[MIT](LICENSE) — Atsuro Murata, 2026.
