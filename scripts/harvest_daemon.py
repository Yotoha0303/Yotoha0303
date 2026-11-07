#!/usr/bin/env python3
"""Harvest, validate and publish FeeGate yields using only the standard library.

Run immediately, then wait --interval-hours (default 6) after each completed run.
--once runs a single cycle and returns nonzero on failure. --dry-run performs one
read-only catalog check and prints the harvest plan, with no network calls,
dataset writes, persistent logs or metric changes. Paths are independent of cwd.

Each real cycle seeds a staging directory with the current four datasets and
catalog, runs the sovereign harvester there (preserving its cache), updates only
the sovereign catalog provenance, and runs the standalone integrity validator.
Only validated output is published. JSON replacements are individually atomic;
ordinary publication errors roll back both files. A process/host crash between
the two replacements can leave a mismatch, which the validator detects and the
next successful cycle repairs. All daemon instances share a data-directory lock.

Rotating UTC execution logs and atomic cumulative metrics live in --state-dir
(default: var/feegate-harvester under the repository). Cached/mock records are
reported as degraded runs separately from process/validation failures.
"""

import argparse
import fcntl
import json
import logging
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from test_catalog_integrity import (
    CATALOG_FILE, DEFAULT_DATA_DIR, PRODUCT_SCHEMAS, SOVEREIGN_ID,
    product_metadata, read_json,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = SCRIPT_DIR.parent / "var/feegate-harvester"
DATA_FILES = [CATALOG_FILE] + [name + ".json" for name in PRODUCT_SCHEMAS]
COUNTERS = ("runs_total", "successes_total", "failures_total", "consecutive_failures", "degraded_runs_total")


class RunFailure(RuntimeError):
    pass


class Stopped(RunFailure):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, contents):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))


@contextmanager
def exclusive_lock(data_dir):
    # Never unlink the lock file: a waiting process might still hold its inode.
    with (data_dir / ".feegate-harvest.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunFailure("another harvester owns this data directory") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def setup_logging(state_dir=None):
    logger = logging.getLogger("feegate.harvester")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handlers = [logging.StreamHandler(sys.stdout)]
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(state_dir / "harvest.log", maxBytes=5 * 1024 * 1024,
                                           backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def load_metrics(path):
    if not path.exists():
        return {**dict.fromkeys(COUNTERS, 0), "step_failures": {}}
    metrics = read_json(path)
    for key in COUNTERS:
        if type(metrics.get(key)) is not int or metrics[key] < 0:
            raise ValueError(f"invalid persisted metric: {key}")
    failures = metrics.get("step_failures")
    if not isinstance(failures, dict) or any(type(value) is not int or value < 0 for value in failures.values()):
        raise ValueError("invalid persisted metric: step_failures")
    return metrics


def stop_process(process):
    # Also terminate descendants when the daemon runs without systemd.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def run_step(command, label, logger, stop, timeout):
    if stop.is_set():
        raise Stopped("shutdown requested")
    logger.info("step=%s started command=%s", label, command)
    started = time.monotonic()
    with subprocess.Popen(command, cwd=SCRIPT_DIR.parent, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          start_new_session=True) as process:
        failure = None
        while True:
            if stop.is_set():
                failure = Stopped("shutdown requested")
            elif time.monotonic() - started >= timeout:
                failure = RunFailure(f"{label} timed out after {timeout:g} seconds")
            if failure is not None:
                stdout, stderr = stop_process(process)
                break
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, timeout))
                break
            except subprocess.TimeoutExpired:
                continue
        for line in stdout.splitlines():
            logger.info("step=%s stdout=%s", label, line)
        for line in stderr.splitlines():
            logger.warning("step=%s stderr=%s", label, line)
        logger.info("step=%s exit_code=%s duration_seconds=%.3f", label, process.returncode,
                    time.monotonic() - started)
        if failure is not None:
            raise failure
        if process.returncode:
            raise RunFailure(f"{label} exited with code {process.returncode}")


def synchronize_catalog(staging):
    catalog = read_json(staging / CATALOG_FILE)
    payload = read_json(staging / (SOVEREIGN_ID + ".json"))
    entries = [item for item in catalog["products"] if item.get("catalog_id") == SOVEREIGN_ID]
    if len(entries) != 1:
        raise RunFailure("catalog must contain exactly one sovereign product")
    entries[0]["provenance"].update(product_metadata(SOVEREIGN_ID, payload))
    catalog["generated_at_utc"] = utc_now()
    write_json(staging / CATALOG_FILE, catalog)
    return dict(Counter(record["provenance"]["data_status"] for record in payload["records"]))


def publish(staging, data_dir, originals):
    # Detect changes from writers that do not participate in our advisory lock.
    if any((data_dir / name).read_bytes() != contents for name, contents in originals.items()):
        raise RunFailure("data changed during harvest; refusing to overwrite another writer")
    names = (SOVEREIGN_ID + ".json", CATALOG_FILE)
    # Allocate rollback copies before changing any public file, so a full disk
    # during publication does not require allocating space to restore old data.
    backups = {name: staging / (".previous-" + name) for name in names}
    for name, backup in backups.items():
        atomic_write(backup, originals[name])
    replaced = []
    try:
        for name in names:
            os.replace(staging / name, data_dir / name)
            replaced.append(name)
    except OSError:
        for name in reversed(replaced):
            os.replace(backups[name], data_dir / name)
        raise


def run_cycle(args, logger, stop, metrics):
    started = time.monotonic()
    metrics["runs_total"] += 1
    metrics["last_started_at_utc"] = utc_now()
    metrics["last_status"] = "running"
    metrics["last_error"] = None
    metrics["last_quality_summary"] = None
    write_json(args.state_dir / "metrics.json", metrics)
    success = False
    stage = "staging"
    try:
        with tempfile.TemporaryDirectory(prefix=".feegate-stage-", dir=args.data_dir) as directory:
            staging = Path(directory)
            originals = {name: (args.data_dir / name).read_bytes() for name in DATA_FILES}
            for name, contents in originals.items():
                (staging / name).write_bytes(contents)
            stage = "harvest"
            run_step([sys.executable, str(SCRIPT_DIR / "sovereign_yield_harvester.py"),
                      "--output", str(staging / (SOVEREIGN_ID + ".json"))],
                     stage, logger, stop, args.step_timeout)
            stage = "catalog_sync"
            quality = synchronize_catalog(staging)
            stage = "validation"
            run_step([sys.executable, str(SCRIPT_DIR / "test_catalog_integrity.py"),
                      "--data-dir", str(staging)], stage, logger, stop, args.step_timeout)
            if stop.is_set():
                raise Stopped("shutdown requested before publication")
            stage = "publication"
            publish(staging, args.data_dir, originals)
            metrics["last_quality_summary"] = {key: quality.get(key, 0) for key in ("live", "cached", "mock")}
            if quality.get("cached", 0) or quality.get("mock", 0):
                metrics["degraded_runs_total"] += 1
                logger.warning("harvest used fallback records: %s", metrics["last_quality_summary"])
        success = True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RunFailure) as error:
        metrics["last_error"] = {"step": stage, "message": str(error)}
        metrics["step_failures"][stage] = metrics["step_failures"].get(stage, 0) + 1
        logger.exception("cycle failed step=%s", stage)
    finally:
        metrics["last_finished_at_utc"] = utc_now()
        metrics["last_duration_seconds"] = round(time.monotonic() - started, 3)
        metrics["last_status"] = "success" if success else "failed"
        if success:
            metrics["successes_total"] += 1
            metrics["consecutive_failures"] = 0
            metrics["last_success_at_utc"] = metrics["last_finished_at_utc"]
        else:
            metrics["failures_total"] += 1
            metrics["consecutive_failures"] += 1
        write_json(args.state_dir / "metrics.json", metrics)
        logger.info("cycle status=%s duration_seconds=%s runs_total=%s failures_total=%s",
                    metrics["last_status"], metrics["last_duration_seconds"],
                    metrics["runs_total"], metrics["failures_total"])
    return success


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError("must be finite, positive and within the platform timer limit")
    return number


def interval_hours(value):
    number = positive_seconds(value)
    positive_seconds(number * 3600)
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval-hours", type=interval_hours, default=6.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--step-timeout", type=positive_seconds, default=900.0,
                        help="timeout for each child process, in seconds (default: 900)")
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir.resolve()
    args.state_dir = args.state_dir.resolve()
    logger = setup_logging()
    stop = threading.Event()
    previous_signals = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_signals[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        if args.dry_run:
            logger.info("dry-run: would harvest sovereign yields, synchronize catalog, validate all 4 products and publish")
            run_step([sys.executable, str(SCRIPT_DIR / "test_catalog_integrity.py"),
                      "--data-dir", str(args.data_dir)], "dry_run_validation", logger, stop, args.step_timeout)
            return 0
        with exclusive_lock(args.data_dir):
            logger = setup_logging(args.state_dir)
            metrics = load_metrics(args.state_dir / "metrics.json")
            logger.info("daemon started interval_hours=%s once=%s data_dir=%s", args.interval_hours, args.once, args.data_dir)
            while not stop.is_set():
                success = run_cycle(args, logger, stop, metrics)
                if args.once:
                    return 0 if success else 1
                logger.info("next cycle in %g hours", args.interval_hours)
                stop.wait(args.interval_hours * 3600)
            logger.info("daemon stopped")
        return 0
    except (OSError, ValueError, RunFailure) as error:
        logger.error("daemon stopped: %s", error)
        return 1
    finally:
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)


if __name__ == "__main__":
    sys.exit(main())
