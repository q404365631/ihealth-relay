# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-29

Initial public OSS release. ihealth-relay reads Apple Health exports
produced by the iPhone app **Health Auto Export — JSON+CSV** (AutoSync,
`.hae` LZFSE-compressed JSON in iCloud Drive), aggregates them into 14
daily fields (date + 13 metrics), and pushes the result to **pluggable
sinks**:

### Added

- **Pluggable Publisher architecture**: new `Publisher` Protocol in
  `src/ihealth/publishers/base.py` and shared payload validation layer
  (`_payload.py`) used by every sink for type-strict, fail-fast value
  coercion.
- **Notion publisher**: updates a daily journal page in Notion via
  `urllib.request` (no external HTTP library). Retry with exponential
  backoff on HTTP 429 / 5xx. Column-name overrides via `config.toml`.
- **Markdown publisher**: writes one Obsidian-compatible YAML frontmatter +
  body file per day. Atomic write via `tempfile.mkstemp` + `os.fsync` +
  parent-directory `fsync`. Process-safe under concurrent daemon launches.
- **Stdout publisher**: emits a single newline-terminated JSON line on
  stdout for Unix-pipe composability
  (`python3 -m ihealth --publisher stdout | jq '.step_count'`).
  `BrokenPipeError` is silenced cleanly via `os.dup2(/dev/null, sys.stdout)`.
- **SQLite publisher**: UPSERTs one row per day into a `daily_health`
  table. `BEGIN IMMEDIATE` + `COALESCE` for partial-update safety.
  Schema-drift detection.
- **Slack publisher**: posts a daily summary via Incoming Webhook. Strict
  URL validator (host allow-list: `hooks.slack.com` / `hooks.slack-gov.com`,
  path must be `/services/{T}/{B}/{token}`, no userinfo / non-443 port /
  query / fragment / non-ASCII / control char). Secret token never leaks
  in logs / errors / `__cause__`.
- **`config.toml` external configuration**: map the internal field names
  (`step_count`, `distance_km`, `sleep_hours`, ...) to arbitrary Notion
  property names. Custom minimal TOML 1.0 subset parser
  (`src/ihealth/_toml.py`) — no external dependencies.
- **English property names by default**: `Step Count` / `Distance (km)` /
  `Date` etc. Notion DBs with non-English column names (e.g., Japanese
  legacy DBs) remain supported via `cp config.example.toml config.toml`.
- **dataless `.hae` auto-materialize**: detects iCloud Drive's `SF_DATALESS`
  flag (`0x40000000`) before decompressing, triggers `brctl download`,
  polls until materialized, then proceeds. Fixes silent EDEADLK in
  launchd autoruns when iCloud has evicted the file.
- **`AppError` hierarchy**: every failure path maps to a deterministic exit
  code (3=Config, 4=HealthExportDir, 5=Source, 6=Notion, 7=Markdown,
  8=Stdout, 9=SQLite, 10=Slack). launchd / Reminders.app receive a
  user-friendly title + body for each.
- **MIT LICENSE**, **CHANGELOG**, **GitHub Actions CI** (this release).

### Changed

- **Notion property name defaults** switched from Japanese (`歩数 (歩)` etc.)
  to English Title Case + units. Users with non-English column names
  (e.g., legacy Japanese DBs) must run `cp config.example.toml config.toml`
  and edit the override mapping before the next launchd run.
- `NotionClient.DATE_PROPERTY_NAME` constant retained as a backward-compat
  alias of `DEFAULT_DATE_PROPERTY_NAME`.

### Compatibility

- macOS 14+ (Sonoma); Apple Silicon
- Python 3.9+ (`/usr/bin/python3`); standard library only
- iPhone-side: Health Auto Export — JSON+CSV (App ID 1115567069), Premium
  buy-once license

### Tests

- 545+ unit tests, no external HTTP / Notion / Slack contact during testing
- macOS `/usr/bin/python3` 3.9.6 verified

[Unreleased]: https://github.com/arkatom/ihealth-relay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/arkatom/ihealth-relay/releases/tag/v0.1.0
