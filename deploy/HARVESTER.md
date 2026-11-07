# FeeGate harvest automation

Requires Linux and Python 3.9+; no Python packages or API keys are required.
Commands resolve the repository relative to the scripts and work from any cwd.

```bash
python3 scripts/test_catalog_integrity.py
python3 scripts/harvest_daemon.py --dry-run
python3 scripts/harvest_daemon.py --once
python3 scripts/harvest_daemon.py --interval-hours 6
```

The validator checks exactly four product IDs and their local datasets, including
SHA-256 digests, record counts, expected schema versions, matching ingestion
timestamps, embedded observation timestamps and catalog generation order. UTC
timestamps may use `Z` or `+00:00`. It accepts archived snapshots; it does not impose
a maximum dataset age. Exit status is 0 for success and 1 for integrity failures.

Digests cover the **records arrays**, using each publisher's serialization:
sovereign schema 1.0.0 uses sorted keys, compact separators and UTF-8 characters;
legacy schema 2.0.0 uses sorted keys, default spaced separators and ASCII escaping.
These checks detect changes relative to stored digests; they are not signatures.

Each daemon cycle runs `sovereign_yield_harvester.py` against a staged copy of the
data, preserving the existing yield cache. It synchronizes sovereign provenance
in the staged catalog and runs `test_catalog_integrity.py` against all four
products. A successful validation publishes the new yield dataset and catalog.
The other three datasets retain their existing contents. The legacy daily
`feegate_harvester.py` refreshes those three and now retains the sovereign entry.

Each published JSON replacement is atomic. Ordinary publication errors restore
the previous files. A host/process crash between the two replacements may leave
the pair inconsistent; validation detects this, and the next successful harvest
repairs it. The data-directory lock prevents overlapping daemon instances.
Changes by another writer during harvesting abort publication. Coordinate legacy
jobs separately because they do not acquire this lock.

`--once` exits after one cycle and returns 1 if harvesting, catalog synchronization,
validation or publication fails. Continuous mode runs immediately, waits the
specified interval after each cycle, and retries on the next interval after a
failure. SIGINT/SIGTERM interrupt waits and terminate active child processes;
an interrupted cycle records a failure. `--step-timeout` bounds each child process
(default 900 seconds). The service allows 35 minutes for the complete cycle.

`--dry-run` performs one read-only validation and prints the planned operations.
It does not fetch data, publish files, create locks, or change persistent logs or
metrics. A failing validation still returns 1. `--data-dir PATH` and
`--state-dir PATH` support isolated runs and alternative deployment locations.

Execution output appears on stdout (and therefore in the systemd journal).
Normal runs also write `harvest.log` in the state directory, rotating at 5 MiB
with three backups. `metrics.json` is atomically updated at cycle start and finish
and persists across daemon restarts. It records cumulative runs, successes,
failures, consecutive failures, failures by step, last run timestamps/duration,
last error and yield quality counts. `last_status: running` indicates an active
or abruptly terminated cycle. Invalid metrics files cause startup to fail rather
than silently discard history.

The underlying harvester can successfully produce cached or explicitly synthetic
records during outages. These are counted in `degraded_runs_total` and
`last_quality_summary`, and logged as warnings; process/validation failures are
counted separately. Integrity success does not imply that observations are live.

## systemd user timer

The supplied units target a user checkout at `~/Projects/Yotoha0303`; edit both
repository paths in the service if needed. Enable the timer to run once every six
hours at 00:00, 06:00, 12:00 and 18:00 UTC, plus up to five minutes of jitter.
`Persistent=true` catches up a missed scheduled run when the user manager starts.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/feegate-harvester.{service,timer} ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/feegate-harvester.{service,timer}
systemctl --user daemon-reload
systemctl --user enable --now feegate-harvester.timer
systemctl --user start feegate-harvester.service
systemctl --user list-timers feegate-harvester.timer
journalctl --user -u feegate-harvester.service
```

The service runs the daemon with `--once`; the timer owns scheduling. Manual
continuous mode owns its own interval. Use one scheduling mode per data directory.
Systemd stores state under `%S/feegate-harvester` (normally
`~/.local/state/feegate-harvester`). Manual runs default to
`var/feegate-harvester/` in the repository. For unattended operation after logout,
the user's systemd manager must remain running, for example via login lingering.
The Docker deployment retains its separate daily legacy harvester schedule.

## Verification

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 scripts/test_catalog_integrity.py
python3 scripts/harvest_daemon.py --once --dry-run
systemd-analyze --user verify deploy/feegate-harvester.{service,timer}
```

The regression suite uses temporary datasets and offline harvests. It covers
record corruption, schema/count/timestamp errors, legacy catalog preservation,
failed child processes, timeouts, signal handling, locking, rollback, log
rotation, persistent metrics, interval scheduling and read-only dry runs.
