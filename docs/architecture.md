# Architecture

This document explains the module layout and runtime data flow of
ihealth-relay. Read this after the top-level [README](../README.md) to
understand *how* the code is organized; read the
[setup guide](setup.md) to understand *what* you have to configure.

## Module layout

```
src/ihealth/
├── __main__.py        # CLI entry point: argparse → load_config → workflow
├── config.py          # .env reader (no external deps)
├── config_file.py     # config.toml reader (uses _toml.parse)
├── _toml.py           # in-tree minimal TOML 1.0 subset parser
├── errors.py          # AppError hierarchy → exit code mapping
├── logger.py          # logging setup (stderr + rotating file)
├── models.py          # DailyHealthData (frozen dataclass, 14 fields)
├── notifier.py        # AppleScript → Reminders.app (failure persistence)
├── parser.py          # 13 metric-specific aggregators
├── source.py          # AutoSync .hae discovery, brctl materialize, decompress
├── workflow.py        # run_pipeline + composition root (build_default_deps)
└── publishers/
    ├── __init__.py
    ├── base.py        # Publisher Protocol (publish(health_data) -> None)
    ├── _payload.py    # FIELD_SPECS + coerce_int / coerce_float / build_payload
    ├── notion.py      # NotionClient + NotionPublisher + DryRun helpers
    ├── dryrun.py      # DryRunPublisher (logs the Notion payload)
    ├── markdown.py    # MarkdownPublisher (Obsidian frontmatter, atomic write)
    ├── stdout.py      # StdoutPublisher (JSON line + BrokenPipe-safe)
    ├── sqlite.py      # SQLitePublisher (UPSERT with BEGIN IMMEDIATE)
    └── slack.py       # SlackPublisher (Incoming Webhook + URL allow-list)
```

Tests mirror the source structure under `tests/`.

## Runtime data flow

### 1. iPhone → iCloud Drive

Health Auto Export (Premium AutoSync) writes one `.hae` file per metric per
day to:

```
~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/HealthMetrics/{metric}/YYYYMMDD.hae
```

Each `.hae` is **LZFSE-compressed UTF-8 JSON**, not a proprietary binary.

### 2. macOS launchd fires `python3 -m ihealth`

`__main__.py` does the following, in order:

1. Parses `argparse` flags (`--date`, `--dry-run`, `--publisher`,
   `--markdown-out`, `--sqlite-path`, `--slack-webhook`).
2. Decides if Notion secrets are required (`require_notion`) — only when
   `--publisher` resolves to `notion` (and `--dry-run` is not set).
3. Loads `.env` via `config.py:load_config`.
4. Configures the logger (stderr + `logs/run.log`).
5. Calls `workflow.build_default_deps` to assemble the `PipelineDeps`
   (fetcher, parser, publisher, sleep merge helper).
6. Calls `workflow.run_pipeline` and maps any `AppError` subclass to the
   appropriate exit code + Reminders notification.

### 3. `workflow.run_pipeline`

```
HEALTH_EXPORT_DIR check
  → fetch_all_metrics (source.py): per-metric .hae discovery + brctl + decompress
  → parse_all (parser.py): 13 aggregators → DailyHealthData
  → publisher.publish(DailyHealthData): one of 5 sinks
```

If any step raises an `AppError` subclass it bubbles up to `__main__`. If a
`Publisher`-specific exception (`MarkdownPublishError`,
`SlackPublishError`, ...) bubbles up, `run_pipeline` rewraps it into the
matching `AppError`.

### 4. Sinks

All publishers implement the `Publisher` Protocol:

```python
class Publisher(Protocol):
    def publish(self, health_data: DailyHealthData) -> None: ...
```

Each sink validates its own boundary and raises a publisher-specific error
on failure. The `_payload.build_payload` helper provides a single source of
truth for type-strict value coercion (no silent `int(True) → 1`,
`int(1.9) → 1`, NaN/Inf, or bool-as-number).

### 5. Failure path

Every non-zero exit:

```
AppError.exit_code → process exit code
AppError.title + body → notifier.notify_failure → AppleScript → Reminders.app
```

Reminders persist until you acknowledge them (unlike Notification Center
banners that auto-dismiss).

## Key design decisions

- **Standard library only** so the daemon survives macOS upgrades that
  occasionally invalidate `pip`-installed environments.
- **`config.toml` separate from `.env`**: secrets stay in `.env`,
  structured config (Notion property names, date column name) goes into
  `config.toml`. Both are gitignored.
- **`AppError` instead of `sys.exit`**: every internal failure raises a
  typed exception with `exit_code`, `title`, `body`. The CLI surface
  (`__main__`) is the only place that maps these to OS-level effects.
- **Atomic writes**: Markdown publisher writes via
  `tempfile.mkstemp + os.fsync + os.replace + parent dir fsync` for
  POSIX-durability across power loss. SQLite publisher uses
  `BEGIN IMMEDIATE` so `CREATE TABLE` + UPSERT roll back together.
- **Secret hygiene**: Slack webhook URLs are masked to `hooks.slack.com`
  in every error message; `__cause__` is suppressed via `raise ... from None`
  so `traceback` doesn't leak the secret either.
- **Pluggable Sinks**: adding a new sink (e.g., InfluxDB, RSS) only
  requires implementing `Publisher` and listing the kind in
  `workflow.PUBLISHER_KINDS`.

## Where to look next

- The dataless-`.hae` workaround is non-obvious; see
  [`docs/case-study/dataless-icloud.md`](case-study/dataless-icloud.md).
- For the per-metric aggregation rules (e.g., why `sleep_analysis`
  reads two days at a time), see `src/ihealth/parser.py` and
  `src/ihealth/workflow.py:_build_merged_sleep_payload`.
