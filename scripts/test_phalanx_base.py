#!/usr/bin/env python3
"""Comprehensive Unit Test Suite for Phalanx Microkernel Base (phalanx_base.py)."""

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import phalanx_base as pb


class TestPhalanxDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_kernel.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_in_memory_initialization(self):
        db = pb.PhalanxDB(":memory:")
        self.assertEqual(db.db_path, ":memory:")
        res = db.execute("SELECT 1 AS num").fetchone()
        self.assertEqual(res["num"], 1)
        db.close()

    def test_file_initialization_enforces_foreign_keys_and_wal(self):
        with pb.PhalanxDB(self.db_path) as db:
            fk = db.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk, 1)
            journal = db.execute("PRAGMA journal_mode").fetchone()[0].upper()
            self.assertEqual(journal, "WAL")

    def test_transaction_commit(self):
        with pb.PhalanxDB(self.db_path) as db:
            db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
            with db.transaction():
                db.execute("INSERT INTO users (name) VALUES (?)", ("Alice",))
                db.execute("INSERT INTO users (name) VALUES (?)", ("Bob",))

            rows = db.execute("SELECT name FROM users ORDER BY id").fetchall()
            self.assertEqual([r["name"] for r in rows], ["Alice", "Bob"])

    def test_transaction_rollback_on_error(self):
        with pb.PhalanxDB(self.db_path) as db:
            db.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER)")
            with db.transaction():
                db.execute("INSERT INTO accounts (id, balance) VALUES (1, 100)")

            with self.assertRaises(ValueError):
                with db.transaction():
                    db.execute("UPDATE accounts SET balance = 50 WHERE id = 1")
                    raise ValueError("Simulated transaction fault")

            row = db.execute("SELECT balance FROM accounts WHERE id = 1").fetchone()
            self.assertEqual(row["balance"], 100)

    def test_executemany_and_executescript(self):
        with pb.PhalanxDB(":memory:") as db:
            db.executescript("""
                CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT);
                CREATE TABLE log (msg TEXT);
            """)
            db.executemany("INSERT INTO items (sku) VALUES (?)", [("A1",), ("B2",), ("C3",)])
            cnt = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            self.assertEqual(cnt, 3)

    def test_versioned_migrations_idempotency(self):
        migrations_v1 = [
            (1, "init_schema", "CREATE TABLE orders (id INTEGER PRIMARY KEY, title TEXT);"),
            (2, "add_price", "ALTER TABLE orders ADD COLUMN price REAL DEFAULT 0.0;"),
        ]
        with pb.PhalanxDB(self.db_path) as db:
            applied = db.run_migrations(migrations_v1)
            self.assertEqual(applied, [1, 2])

            # Re-running the same migrations should apply nothing
            applied_again = db.run_migrations(migrations_v1)
            self.assertEqual(applied_again, [])

            # Appending a new migration
            migrations_v2 = migrations_v1 + [
                (3, "add_status", "ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'pending';")
            ]
            applied_v2 = db.run_migrations(migrations_v2)
            self.assertEqual(applied_v2, [3])

            # Verify table columns
            db.execute("INSERT INTO orders (title, price, status) VALUES ('Server', 99.9, 'paid')")
            row = db.execute("SELECT * FROM orders WHERE id = 1").fetchone()
            self.assertEqual(row["title"], "Server")
            self.assertEqual(row["price"], 99.9)
            self.assertEqual(row["status"], "paid")

    def test_thread_safety_concurrent_writes(self):
        with pb.PhalanxDB(self.db_path) as db:
            db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, val INTEGER)")
            db.execute("INSERT INTO counter (id, val) VALUES (1, 0)")

            def worker():
                for _ in range(50):
                    with db.transaction():
                        cur = db.execute("SELECT val FROM counter WHERE id = 1").fetchone()[0]
                        db.execute("UPDATE counter SET val = ? WHERE id = 1", (cur + 1,))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            final_val = db.execute("SELECT val FROM counter WHERE id = 1").fetchone()[0]
            self.assertEqual(final_val, 200)


    def test_migration_sorting_order(self):
        with pb.PhalanxDB(self.db_path) as db:
            out_of_order = [
                (2, "v2", "CREATE TABLE t2 (id INTEGER);"),
                (1, "v1", "CREATE TABLE t1 (id INTEGER);"),
            ]
            applied = db.run_migrations(out_of_order)
            self.assertEqual(applied, [1, 2])
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.assertIn("t1", tables)
            self.assertIn("t2", tables)


    def test_connection_property_and_busy_timeout(self):
        with pb.PhalanxDB(":memory:", busy_timeout_ms=3000) as db:
            self.assertIsInstance(db.connection, sqlite3.Connection)
            timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(timeout, 3000)

    def test_transaction_reentrant_error_prevention(self):
        with pb.PhalanxDB(":memory:") as db:
            db.execute("CREATE TABLE t (x INT)")
            with db.transaction():
                db.execute("INSERT INTO t VALUES (1)")
            rows = db.execute("SELECT * FROM t").fetchall()
            self.assertEqual(len(rows), 1)


class TestExponentialBackoff(unittest.TestCase):
    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            pb.ExponentialBackoff(base_delay=-1.0)
        with self.assertRaises(ValueError):
            pb.ExponentialBackoff(base_delay=10.0, max_delay=5.0)
        with self.assertRaises(ValueError):
            pb.ExponentialBackoff(factor=1.0)

    def test_deterministic_without_jitter(self):
        bo = pb.ExponentialBackoff(base_delay=1.0, max_delay=10.0, factor=2.0, jitter=False)
        self.assertEqual(bo.compute_delay(0), 1.0)
        self.assertEqual(bo.compute_delay(1), 2.0)
        self.assertEqual(bo.compute_delay(2), 4.0)
        self.assertEqual(bo.compute_delay(3), 8.0)
        self.assertEqual(bo.compute_delay(4), 10.0)  # Clamped to max_delay
        self.assertEqual(bo.compute_delay(-1), 1.0)

    def test_jitter_within_bounds(self):
        bo = pb.ExponentialBackoff(base_delay=2.0, max_delay=20.0, factor=2.0, jitter=True)
        for attempt in range(5):
            max_bound = min(20.0, 2.0 * (2.0 ** attempt))
            val = bo.compute_delay(attempt)
            self.assertTrue(0.0 <= val <= max_bound)


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.curr_time = 1000.0

    def fake_time(self) -> float:
        return self.curr_time

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            pb.CircuitBreaker(failure_threshold=0)
        with self.assertRaises(ValueError):
            pb.CircuitBreaker(recovery_timeout=-1.0)
        with self.assertRaises(ValueError):
            pb.CircuitBreaker(half_open_success_threshold=0)

    def test_circuit_state_lifecycle(self):
        cb = pb.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=10.0,
            half_open_success_threshold=2,
            time_func=self.fake_time,
        )
        self.assertEqual(cb.state, pb.CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

        # First failure -> still CLOSED
        cb.record_failure()
        self.assertEqual(cb.state, pb.CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

        # Second failure -> transitions to OPEN
        cb.record_failure()
        self.assertEqual(cb.state, pb.CircuitState.OPEN)
        self.assertFalse(cb.is_available())

        # Before recovery timeout -> remains unavailable
        self.curr_time += 5.0
        self.assertFalse(cb.is_available())

        # After recovery timeout -> transitions to HALF_OPEN on availability check
        self.curr_time += 6.0
        self.assertTrue(cb.is_available())
        self.assertEqual(cb.state, pb.CircuitState.HALF_OPEN)

        # In HALF_OPEN: 1 failure immediately reverts to OPEN
        cb.record_failure()
        self.assertEqual(cb.state, pb.CircuitState.OPEN)
        self.assertFalse(cb.is_available())

        # Recover again to HALF_OPEN
        self.curr_time += 11.0
        self.assertTrue(cb.is_available())
        self.assertEqual(cb.state, pb.CircuitState.HALF_OPEN)

        # In HALF_OPEN: 2 consecutive successes transition to CLOSED
        cb.record_success()
        self.assertEqual(cb.state, pb.CircuitState.HALF_OPEN)
        cb.record_success()
        self.assertEqual(cb.state, pb.CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

    def test_decorator_protect_normal_and_failure(self):
        cb = pb.CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, time_func=self.fake_time)

        @cb.protect()
        def risky_func(should_fail=False):
            if should_fail:
                raise ConnectionError("Network down")
            return "ok"

        self.assertEqual(risky_func(False), "ok")
        with self.assertRaises(ConnectionError):
            risky_func(True)
        with self.assertRaises(ConnectionError):
            risky_func(True)

        # Breaker is now OPEN; subsequent call throws CircuitOpenError without running function
        with self.assertRaises(pb.CircuitOpenError):
            risky_func(False)

    def test_decorator_fallback_and_ignored_exceptions(self):
        cb = pb.CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, time_func=self.fake_time)

        @cb.protect(fallback=lambda *a, **k: "fallback_value", ignored_exceptions=(ValueError,))
        def caller(val):
            if val == "ignore":
                raise ValueError("Ignored validation error")
            if val == "fail":
                raise RuntimeError("Service crash")
            return f"res_{val}"

        # ValueError is ignored and does not count as failure
        with self.assertRaises(ValueError):
            caller("ignore")
        self.assertEqual(cb.failure_count, 0)

        # Trigger failure threshold
        with self.assertRaises(RuntimeError):
            caller("fail")
        with self.assertRaises(RuntimeError):
            caller("fail")
        self.assertEqual(cb.state, pb.CircuitState.OPEN)

        # When OPEN, returns fallback instead of raising CircuitOpenError
        self.assertEqual(caller("any"), "fallback_value")

    def test_reset(self):
        cb = pb.CircuitBreaker(failure_threshold=1, time_func=self.fake_time)
        cb.record_failure()
        self.assertEqual(cb.state, pb.CircuitState.OPEN)
        cb.reset()
        self.assertEqual(cb.state, pb.CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

    def test_record_success_in_closed_clears_failures(self):
        cb = pb.CircuitBreaker(failure_threshold=3, time_func=self.fake_time)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.failure_count, 2)
        cb.record_success()
        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.state, pb.CircuitState.CLOSED)

    def test_circuit_state_enum_values(self):
        self.assertEqual(pb.CircuitState.CLOSED.value, "closed")
        self.assertEqual(pb.CircuitState.OPEN.value, "open")
        self.assertEqual(pb.CircuitState.HALF_OPEN.value, "half_open")


class TestGoldenFixtureLoader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fixtures_dir = Path(self.temp_dir.name) / "fixtures"
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)

        self.json_file = self.fixtures_dir / "sample_quote.json"
        self.json_file.write_text(json.dumps({"provider": "OpenPrice", "rate": 0.05}), encoding="utf-8")

        self.txt_file = self.fixtures_dir / "sample_page.html"
        self.txt_file.write_text("<html><body>Rate: $20</body></html>", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_json_fixture(self):
        data = pb.GoldenFixtureLoader.load_fixture("sample_quote.json", base_dir=self.fixtures_dir)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["provider"], "OpenPrice")
        self.assertEqual(data["rate"], 0.05)

    def test_load_text_fixture(self):
        content = pb.GoldenFixtureLoader.load_fixture("sample_page.html", base_dir=self.fixtures_dir)
        self.assertIn("<html><body>Rate: $20</body></html>", content)

    def test_fixture_not_found(self):
        with self.assertRaises(FileNotFoundError):
            pb.GoldenFixtureLoader.load_fixture("non_existent.json", base_dir=self.fixtures_dir)

    def test_decorator_preserves_metadata(self):
        cb = pb.CircuitBreaker()

        @cb.protect()
        def sample_documented_function(x: int) -> int:
            """Sample docstring."""
            return x * 2

        self.assertEqual(sample_documented_function.__name__, "sample_documented_function")
        self.assertEqual(sample_documented_function.__doc__, "Sample docstring.")

    def test_reproducible_backoff_with_custom_rng(self):
        import random
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        bo1 = pb.ExponentialBackoff(base_delay=1.0, max_delay=10.0, rng=rng1)
        bo2 = pb.ExponentialBackoff(base_delay=1.0, max_delay=10.0, rng=rng2)
        seq1 = [bo1.compute_delay(i) for i in range(5)]
        seq2 = [bo2.compute_delay(i) for i in range(5)]
        self.assertEqual(seq1, seq2)


if __name__ == "__main__":
    unittest.main()
