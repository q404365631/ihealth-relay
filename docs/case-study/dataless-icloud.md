# Case Study: Silent EDEADLK from iCloud Drive Dataless Placeholders

How a one-character iCloud Drive flag silently broke daily launchd autoruns,
how it was diagnosed, and how it was fixed.

> **TL;DR**: macOS iCloud Drive can evict synced files to disk-only stubs
> ("dataless placeholders") with the `SF_DATALESS = 0x40000000` flag. Reading
> a dataless file from a foreground app triggers the iOS / macOS File
> Provider system to materialize the file on demand. Reading from a
> headless launchd daemon does **not** trigger materialization — instead
> `read(2)` returns `EDEADLK` ("Resource deadlock avoided"). For 3 months,
> our daily 07:00 job silently failed every time iCloud had evicted the
> latest `.hae` overnight, while the same `python3 -m ihealth` invocation
> from a Terminal worked fine.

## Symptoms

The daily launchd run completed with exit code 1, leaving a Reminders.app
reminder titled `ihealth-relay: 想定外エラー`. `logs/run.log` showed:

```
ERROR ihealth.source: compression_tool が失敗しました (returncode=1):
  src=/Users/foo/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/HealthMetrics/step_count/20260427.hae
  stderr='read: Resource deadlock avoided'
```

But running the same command from a Terminal worked:

```bash
$ /usr/bin/python3 -m ihealth --date 2026-04-27
... (success) ...
```

This was the smoking gun: **the failure depended on whether the process had
a foreground UI session**, not on the data or the code.

## Investigation

### Step 1: rule out File Provider permissions (TCC)

The first hypothesis was the macOS Transparency, Consent, and Control (TCC)
system blocking access to iCloud Drive from launchd. We checked
`Settings ▸ Privacy & Security ▸ Files and Folders` and confirmed
`/usr/bin/python3` had iCloud Drive access. We also checked
`~/Library/Application Support/com.apple.TCC/TCC.db` and saw no denials
recorded for iCloud Drive on the launchd-started PID.

That ruled out TCC.

### Step 2: notice `EDEADLK` is the kernel signal, not a userspace error

`Resource deadlock avoided` is `errno = EDEADLK`. POSIX uses it for
`fcntl()` advisory locks, but macOS XNU also uses it from the File
Provider extension when a dataless inode would block waiting for sync to
materialize and the requesting process has no UI session to drive a
progress indicator.

Confirmed by reading
[`xnu/bsd/sys/decmpfs.h`](https://github.com/apple-oss-distributions/xnu/blob/xnu-10063.121.3/bsd/sys/decmpfs.h)
and the `cluster_io.c` source: when a dataless inode is read by a
"non-interactive" process (no controlling tty, no Aqua session), `vnode_io`
returns `EDEADLK` to avoid hanging the headless caller.

### Step 3: confirm the file was indeed dataless

`ls -lO` on the offending `.hae`:

```
-rw-r--r--@ 1 foo  staff  dataless  482 Apr 27 03:14 20260427.hae
```

`@` indicates extended attributes; the `dataless` keyword is the human-readable
display of `SF_DATALESS = 0x40000000` from `<sys/stat.h>`:

```c
#define SF_DATALESS 0x40000000  /* file is dataless object */
```

Confirmed via Python:

```python
>>> os.stat(path).st_flags & 0x40000000
1073741824   # = 0x40000000, SF_DATALESS set
```

### Step 4: foreground vs. background reproduction

```bash
# Pin the file path to an env var so both processes see it.
export IHEALTH_TEST_PATH="$HOME/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/HealthMetrics/step_count/20260427.hae"

# Foreground (succeeds — File Provider materializes on read)
/usr/bin/python3 -c 'import os; open(os.environ["IHEALTH_TEST_PATH"], "rb").read(1)'

# Background (fails — kernel returns EDEADLK)
launchctl asuser "$UID" /usr/bin/python3 -c \
  'import os; open(os.environ["IHEALTH_TEST_PATH"], "rb").read(1)'
# read: Resource deadlock avoided
```

100% reproducible.

### Step 5: how does macOS Spotlight / Time Machine handle this?

Both use private File Provider APIs (`fileproviderctl`) to *request*
materialization without reading. The user-facing tool is `brctl`:

```bash
$ /usr/bin/brctl download ~/Library/.../HealthMetrics/step_count/20260427.hae
$ ls -lO ~/Library/.../HealthMetrics/step_count/20260427.hae
-rw-r--r--@ 1 foo  staff  -  482 Apr 27 03:14 20260427.hae
                          ↑ dataless flag now cleared
```

`brctl` returns immediately; materialization happens asynchronously. We
need to **poll for the flag clearing** before reading.

## Fix

`src/ihealth/source.py` (commit `9ee6766`):

```python
_SF_DATALESS = 0x40000000
_BRCTL = Path("/usr/bin/brctl")
_MATERIALIZE_TIMEOUT_SEC = 60.0
_MATERIALIZE_POLL_INTERVAL_SEC = 0.5


def _is_dataless(path: Path) -> bool:
    try:
        return bool(os.stat(path).st_flags & _SF_DATALESS)
    except (OSError, AttributeError):
        # AttributeError: st_flags is BSD/macOS-only
        return False


def _ensure_materialized(path: Path, log: logging.Logger) -> None:
    """If `path` is a dataless placeholder, trigger materialization and wait."""
    if not _is_dataless(path):
        return
    log.info("dataless detected, triggering brctl download: %s", path)
    try:
        subprocess.run(
            [str(_BRCTL), "download", str(path)],
            check=True, capture_output=True, timeout=10.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"brctl download failed for {path}: {exc}") from exc

    deadline = time.monotonic() + _MATERIALIZE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not _is_dataless(path):
            log.info("materialized: %s", path)
            return
        time.sleep(_MATERIALIZE_POLL_INTERVAL_SEC)
    raise SourceError(
        f"materialization timed out after {_MATERIALIZE_TIMEOUT_SEC}s: {path}"
    )
```

Called from both `_fetch_once` and `fetch_metric_for_date` immediately
before `decompress_hae(...)`. Tests in
`tests/test_source.py:TestEnsureMaterialized` cover the polling, brctl
mocking, and timeout paths.

## Lessons

1. **`EDEADLK` from a `read(2)` is suspicious**: POSIX would normally
   only return it from `fcntl()` lock attempts. Anything else is a vendor
   extension worth investigating.
2. **Test with the same process supervisor as production**: a launchd
   service has no controlling tty, and that single difference flipped
   the kernel's behavior. Always reproduce with `launchctl asuser`,
   not Terminal.
3. **iCloud Drive's eviction policy is opaque**: the file you wrote
   yesterday may be a stub today. Any production code that touches
   iCloud Drive paths must handle dataless materialization explicitly.
4. **`brctl` is the supported escape hatch**: undocumented but stable
   since macOS 10.10 and unlikely to disappear (Spotlight, Backup, and
   File Provider all depend on it).
5. **`os.stat().st_flags & SF_DATALESS` is the cheapest probe**: no
   syscalls beyond `stat(2)`, no risk of triggering the failing read.

## See also

- [`src/ihealth/source.py`](../../src/ihealth/source.py) — the in-tree
  implementation
- [`tests/test_source.py`](../../tests/test_source.py) —
  `TestIsDataless` (4) + `TestEnsureMaterialized` (5) +
  `TestFetchOnceMaterializeIntegration` (1)
- Apple OSS XNU source:
  <https://github.com/apple-oss-distributions/xnu>
- `<sys/stat.h>`:
  ```
  #define SF_DATALESS 0x40000000  /* file is dataless object */
  ```
