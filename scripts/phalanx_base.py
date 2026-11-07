#!/usr/bin/env python3
"""Phalanx Standard Microkernel Base (phalanx_base.py).

Unified zero-dependency foundational infrastructure for autonomous agent pipelines.
Eliminates code duplication and isolated implementations across all task workers:
- Thread-safe SQLite WAL connection pool, autocommit isolation, and versioned migrations
- Resilient exponential backoff with jitter
- Fault-tolerant 3-state circuit breaker with decorator protection
- Immutable Golden Fixture snapshot loader for zero-network testing
"""

from __future__ import annotations

import contextlib
import enum
import functools
import json
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, TypeVar

T = TypeVar("T")


# ==============================================================================
# 1. Thread-Safe SQLite WAL Connection & Migration Manager
# ==============================================================================

class PhalanxDB:
    """Standardized SQLite engine with WAL mode, busy timeout, and versioned migrations."""

    def __init__(self, path: str | Path = ":memory:", busy_timeout_ms: int = 5000):
        self.raw_path = str(path)
        if self.raw_path != ":memory:":
            resolved = Path(self.raw_path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)
        else:
            self.db_path = ":memory:"

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=busy_timeout_ms / 1000.0,
            isolation_level=None,  # Autocommit mode for explicit transaction control
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        # Hardware-level reliability pragmas
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON;")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
            if self.db_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL;")
                self._connection.execute("PRAGMA synchronous = NORMAL;")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Provides an atomic, thread-safe transaction block."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.executemany(sql, seq_of_params)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.executescript(sql_script)

    def run_migrations(self, migrations: Sequence[tuple[int, str, str]]) -> list[int]:
        """Applies versioned schema migrations idempotently.
        
        Args:
            migrations: List of (version_int, name_str, ddl_sql_str) ordered by version.
            
        Returns:
            List of newly applied migration versions.
        """
        applied_versions = []
        with self._lock:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS _schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
            """)

            existing = {
                row["version"]
                for row in self._connection.execute("SELECT version FROM _schema_migrations")
            }

            # Sort migrations by version
            sorted_migrations = sorted(migrations, key=lambda m: m[0])

            for version, name, sql in sorted_migrations:
                if version not in existing:
                    # executescript manages script execution and statements
                    self._connection.executescript(sql)
                    now_iso = datetime.now(timezone.utc).isoformat()
                    self._connection.execute(
                        "INSERT INTO _schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, now_iso),
                    )
                    applied_versions.append(version)
        return applied_versions

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> PhalanxDB:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# ==============================================================================
# 2. Resilient Exponential Backoff with Jitter
# ==============================================================================

class ExponentialBackoff:
    """Calculates exponential backoff delay with decorrelated full jitter."""

    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        factor: float = 2.0,
        jitter: bool = True,
        rng: Optional[random.Random] = None,
    ):
        if base_delay <= 0 or max_delay < base_delay or factor <= 1.0:
            raise ValueError("Invalid backoff parameters: base_delay > 0, max_delay >= base_delay, factor > 1.0")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter = jitter
        self.rng = rng or random.Random()

    def compute_delay(self, attempt: int) -> float:
        """Computes delay in seconds for the given zero-indexed attempt."""
        if attempt < 0:
            attempt = 0
        raw_delay = min(self.max_delay, self.base_delay * (self.factor ** attempt))
        if self.jitter:
            return self.rng.uniform(0.0, raw_delay)
        return raw_delay


# ==============================================================================
# 3. Fault-Tolerant Circuit Breaker with Decorator Protection
# ==============================================================================

class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""
    pass


class CircuitBreaker:
    """Production-grade circuit breaker (Closed -> Open -> Half-Open)."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 10.0,
        half_open_success_threshold: int = 2,
        time_func: Callable[[], float] = time.time,
    ):
        if failure_threshold <= 0 or recovery_timeout <= 0 or half_open_success_threshold <= 0:
            raise ValueError("Failure threshold, recovery timeout, and half-open threshold must be > 0")

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold
        self.time_func = time_func

        self._lock = threading.Lock()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0

    def is_available(self) -> bool:
        """Checks if requests are allowed through."""
        with self._lock:
            now = self.time_func()
            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    return True
                return False
            return True

    def record_success(self) -> None:
        """Records a successful operation."""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    def record_failure(self) -> None:
        """Records an execution failure."""
        with self._lock:
            now = self.time_func()
            self.last_failure_time = now
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.failure_count = self.failure_threshold
            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN

    def reset(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = 0.0

    def protect(
        self,
        fallback: Optional[Callable[..., Any]] = None,
        ignored_exceptions: Sequence[type[BaseException]] = (),
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator wrapping a callable with circuit breaker checks and automatic state updates."""
        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if not self.is_available():
                    if fallback is not None:
                        return fallback(*args, **kwargs)
                    raise CircuitOpenError(
                        f"CircuitBreaker is OPEN. Calls blocked until recovery period ({self.recovery_timeout}s) elapses."
                    )
                try:
                    res = func(*args, **kwargs)
                    self.record_success()
                    return res
                except BaseException as exc:
                    if not any(isinstance(exc, ign) for ign in ignored_exceptions):
                        self.record_failure()
                    raise
            return wrapper
        return decorator


# ==============================================================================
# 4. Golden Fixture Contract Loader
# ==============================================================================

class GoldenFixtureLoader:
    """Safely loads immutable read-only real-world fixture snapshots."""

    DEFAULT_FIXTURES_DIR = Path("fixtures")

    @classmethod
    def load_fixture(cls, filename: str, base_dir: Optional[Path | str] = None) -> Any:
        """Loads a fixture file by relative name.
        
        If the file has a .json extension, it is automatically parsed as JSON.
        Otherwise, raw text is returned.
        """
        search_dirs = []
        if base_dir:
            search_dirs.append(Path(base_dir))
        search_dirs.extend([
            Path.cwd() / "fixtures",
            Path(__file__).resolve().parent.parent / "fixtures",
            Path(__file__).resolve().parent / "fixtures",
        ])

        target_file = None
        for d in search_dirs:
            candidate = d / filename
            if candidate.exists() and candidate.is_file():
                target_file = candidate
                break

        if not target_file:
            raise FileNotFoundError(f"Golden fixture '{filename}' not found in search paths: {search_dirs}")

        content = target_file.read_text(encoding="utf-8")
        if target_file.suffix.lower() == ".json":
            return json.loads(content)
        return content
