# Setup Guide

End-to-end setup for a fresh macOS machine. Follow the sections in order.

## 1. Prerequisites

- macOS 14+ (Sonoma) on Apple Silicon or Intel
- Python 3.9+ (`/usr/bin/python3` is bundled with macOS as 3.9.x — verify
  with `/usr/bin/python3 --version`)
- An iPhone signed in to the same iCloud account as the Mac

## 2. iPhone-side: Health Auto Export

1. Install **Health Auto Export — JSON+CSV** from the App Store
   (App ID 1115567069).
2. Buy the **Premium one-time license** (no subscription).
3. Grant HealthKit permissions for these 13 metrics:
   - Step Count
   - Walking + Running Distance
   - Active Energy
   - Heart Rate
   - Resting Heart Rate
   - Blood Oxygen Saturation
   - Sleep Analysis
   - Mindful Minutes
   - Weight & Body Mass
   - Body Fat Percentage
4. Enable **AutoSync** (Premium feature):
   - Output destination: **iCloud Drive** (default — Premium)
   - Sync interval: every 1 hour is plenty for a daily 07:00 launchd run
   - Frequency: tap *Sync now* once to confirm files appear under
     `iCloud Drive › Health Auto Export › AutoSync › HealthMetrics › ...`

## 3. macOS-side: clone the repository

```bash
git clone https://github.com/arkatom/ihealth-relay.git ~/src/ihealth-relay
cd ~/src/ihealth-relay
```

## 4. Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Required keys:

| Key | Required when | Example |
|---|---|---|
| `NOTION_SECRET` | Notion publisher | `secret_aaaa…` |
| `DATABASE_ID` | Notion publisher | 32-char DB UUID from the page URL |
| `HEALTH_EXPORT_DIR` | Always | `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync` |
| `BIRTH_DATE` | Always | `1990-01-01` |
| `MARKDOWN_OUTPUT_DIR` | Markdown publisher | `~/notes/health` (e.g., an Obsidian vault) |
| `SQLITE_DB_PATH` | SQLite publisher | `~/health.db` |
| `SLACK_WEBHOOK_URL` | Slack publisher | `https://hooks.slack.com/services/T0/B0/secret` |
| `LOG_LEVEL` | Optional | `info` (default) / `debug` / `warning` / `error` |

`HEALTH_EXPORT_DIR` accepts `~/`-relative paths; the rest must be absolute.

## 5. Notion DB setup (only if using `--publisher notion`)

1. Create a Notion database with **at least these columns**, using one of:

   - **English defaults** (recommended for new users): `Date` (date type),
     `Step Count` (number), `Distance (km)` (number), …
     — see [README ▸ Collected Data](../README.md#collected-data) for the
     full list.
   - **Legacy Japanese** (legacy column-name compat): `日付` (date), `歩数 (歩)`
     (number), `移動距離 (km)` (number), … — and run
     `cp config.example.toml config.toml` to map internal field names to
     these column names.

2. Create a Notion **internal integration** at
   <https://www.notion.so/my-integrations>, copy the secret into
   `NOTION_SECRET`, and **share the database with that integration** via
   the database's "Connections" menu.

3. Copy the DB UUID from the database URL into `DATABASE_ID` (the 32
   hex chars, no dashes).

## 6. First run (dry-run)

```bash
PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose --date 2026-04-22
```

This:

- Reads `.env` and `config.toml` (if present).
- Looks up `2026-04-22` worth of `.hae` files under `HEALTH_EXPORT_DIR`.
- `brctl download`s any dataless files (see
  [case study](case-study/dataless-icloud.md)).
- Decompresses with `/usr/bin/compression_tool`.
- Aggregates into `DailyHealthData`.
- **Logs the would-be Notion payload without making any HTTP request.**

If the dry-run succeeds you are ready for a real run.

## 7. Pick a sink

| Goal | Command |
|---|---|
| Update Notion daily | `PYTHONPATH=src /usr/bin/python3 -m ihealth` (default `--publisher notion`) |
| Write Markdown files (Obsidian-compatible YAML frontmatter) | `... --publisher markdown --markdown-out ~/notes/health` |
| Pipe into `jq` | `... --publisher stdout \| jq '.step_count'` |
| Append to local SQLite | `... --publisher sqlite --sqlite-path ~/health.db` |
| Post Slack summary | `... --publisher slack --slack-webhook https://hooks.slack.com/services/...` |

You can also configure paths via `.env` so plain `python3 -m ihealth
--publisher markdown` works without CLI overrides.

## 8. Schedule with launchd (optional)

```bash
./bin/install-launchd.sh
```

Installs `LaunchAgent/com.ihealthrelay.daemon.plist` to
`~/Library/LaunchAgents/` and registers it. The default schedule runs at
**07:00 every day** processing yesterday's data.

To verify:

```bash
launchctl list | grep ihealthrelay
```

To unregister:

```bash
launchctl bootout "gui/$(id -u)/com.ihealthrelay.daemon" 2>/dev/null \
    || launchctl unload ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
```

## 9. Daily operations

- **Failures**: a Reminder appears in Reminders.app with title and body.
  Reminders persist until acknowledged (unlike Notification Center banners
  which auto-dismiss).
- **Logs**: rotating daily under `logs/run.log` in the project root,
  30-day retention.
- **Re-run a specific date**: `python3 -m ihealth --date 2026-04-22`.
  Re-running is idempotent (Notion overwrites, SQLite UPSERTs, Markdown
  rewrites, Slack reposts).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HealthExportDirError` (exit 4) | `HEALTH_EXPORT_DIR` typo or iCloud not synced | verify path under `~/Library/Mobile Documents/...` |
| `SourceAppError` (exit 5) — "data not arrived" | iPhone hasn't synced yet | open Health Auto Export on the iPhone and tap *Sync now* |
| `SourceAppError` — "EDEADLK" / "Resource deadlock avoided" | iCloud Drive evicted the file (dataless) | usually auto-recovered by the in-tree `brctl download` step; see [dataless case study](case-study/dataless-icloud.md) |
| `NotionAppError` — "page not found" (exit 6) | no Notion page exists for that date | create the daily page in Notion first |
| `NotionAppError` — "column name mismatch" (exit 6) | DB columns differ from `PROPERTY_MAP` | run `cp config.example.toml config.toml` and edit |
| `MarkdownAppError` (exit 7) | `--markdown-out` is a file (not a dir) or not writable | provide an existing/creatable directory path |
| `SQLiteAppError` (exit 9) — "database is locked" | another process has an exclusive lock | wait or close the other tool; the publisher uses SQLite's connection-level `timeout=10.0` so brief contention auto-resolves, longer contention fails fast |
| `SlackAppError` (exit 10) — "host not in allow-list" | webhook URL host is not `hooks.slack.com` / `hooks.slack-gov.com` | regenerate webhook from Slack |
| Reminder shows `想定外エラー` ("Unexpected error", exit 1) | unhandled exception | check `logs/run.log`, file an issue |

## Uninstalling

```bash
launchctl bootout "gui/$(id -u)/com.ihealthrelay.daemon" 2>/dev/null \
    || launchctl unload ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
rm ~/Library/LaunchAgents/com.ihealthrelay.daemon.plist
rm -rf ~/src/ihealth-relay   # or wherever you cloned it
```
