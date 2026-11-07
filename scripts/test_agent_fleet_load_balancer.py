#!/usr/bin/env python3
"""Comprehensive unit test suite for agent_fleet_load_balancer.py."""

import unittest
from agent_fleet_load_balancer import (
    CircuitBreaker,
    CircuitState,
    ExponentialBackoff,
    TokenBucket,
    TokenBurndownWatchdog,
    AgentFleetLoadBalancer,
    RoutingStrategy,
)


class MockClock:
    def __init__(self, start: float = 1000.0):
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class TestTokenBucket(unittest.TestCase):
    def test_token_bucket_initial_and_acquire(self):
        clock = MockClock()
        bucket = TokenBucket(capacity=10.0, refill_rate=2.0, time_func=clock)
        self.assertTrue(bucket.try_acquire(5.0))
        self.assertEqual(bucket.tokens, 5.0)
        self.assertTrue(bucket.try_acquire(5.0))
        self.assertFalse(bucket.try_acquire(1.0))

    def test_token_bucket_refill_and_wait_time(self):
        clock = MockClock()
        bucket = TokenBucket(capacity=10.0, refill_rate=2.0, time_func=clock)
        bucket.try_acquire(10.0)
        self.assertAlmostEqual(bucket.wait_time(4.0), 2.0)
        clock.advance(2.0)
        self.assertTrue(bucket.try_acquire(4.0))

    def test_token_bucket_validation(self):
        with self.assertRaises(ValueError):
            TokenBucket(capacity=0, refill_rate=1.0)
        bucket = TokenBucket(capacity=5.0, refill_rate=1.0)
        with self.assertRaises(ValueError):
            bucket.try_acquire(-1.0)


class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_closed_to_open_transitions(self):
        clock = MockClock()
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0, time_func=clock)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)

        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.is_available())

    def test_circuit_breaker_open_to_half_open_to_closed(self):
        clock = MockClock()
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, half_open_success_threshold=2, time_func=clock)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        clock.advance(6.0)
        self.assertTrue(cb.is_available())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # 1st success keeps in half-open
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # 2nd success closes circuit
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.is_available())

    def test_circuit_breaker_half_open_failure_reopens(self):
        clock = MockClock()
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0, time_func=clock)
        cb.record_failure()
        clock.advance(6.0)
        self.assertTrue(cb.is_available())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.is_available())


class TestExponentialBackoff(unittest.TestCase):
    def test_exponential_backoff_delay_calculation(self):
        eb = ExponentialBackoff(base_delay=1.0, max_delay=10.0, factor=2.0, jitter=False)
        self.assertEqual(eb.compute_delay(0), 1.0)
        self.assertEqual(eb.compute_delay(1), 2.0)
        self.assertEqual(eb.compute_delay(2), 4.0)
        self.assertEqual(eb.compute_delay(3), 8.0)
        self.assertEqual(eb.compute_delay(4), 10.0)  # capped at max_delay

    def test_exponential_backoff_with_jitter(self):
        eb = ExponentialBackoff(base_delay=1.0, max_delay=10.0, factor=2.0, jitter=True)
        for attempt in range(5):
            delay = eb.compute_delay(attempt)
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, 10.0)


class TestTokenBurndownWatchdog(unittest.TestCase):
    def test_burndown_watchdog_consumption_and_budget(self):
        clock = MockClock()
        dog = TokenBurndownWatchdog(budget_tokens=100_000, cost_per_million_usd=10.0, time_func=clock)
        self.assertEqual(dog.remaining_tokens, 100_000)
        self.assertFalse(dog.is_exhausted)

        dog.record_consumption(40_000)
        self.assertEqual(dog.remaining_tokens, 60_000)
        self.assertAlmostEqual(dog.total_cost_usd, 0.40)

        dog.record_consumption(60_000)
        self.assertEqual(dog.remaining_tokens, 0)
        self.assertTrue(dog.is_exhausted)

    def test_burndown_velocity_and_exhaustion_projection(self):
        clock = MockClock()
        dog = TokenBurndownWatchdog(budget_tokens=100_000, time_func=clock)
        dog.record_consumption(10_000)
        clock.advance(10.0)
        dog.record_consumption(10_000)

        # Consumed 20,000 tokens over 10 seconds -> velocity = 2,000 tps
        velocity = dog.current_velocity()
        self.assertAlmostEqual(velocity, 2000.0, delta=10.0)

        # Remaining: 80,000 tokens -> time to exhaustion = 80,000 / 2,000 = 40.0s
        tte = dog.time_to_exhaustion_seconds()
        self.assertIsNotNone(tte)
        self.assertAlmostEqual(tte, 40.0, delta=1.0)


class TestAgentFleetLoadBalancer(unittest.TestCase):
    def test_load_balancer_round_robin_selection(self):
        clock = MockClock()
        lb = AgentFleetLoadBalancer(strategy=RoutingStrategy.ROUND_ROBIN, time_func=clock)
        lb.add_worker("node-1", capacity_tps=100.0)
        lb.add_worker("node-2", capacity_tps=100.0)

        w1 = lb.select_worker()
        w2 = lb.select_worker()
        w3 = lb.select_worker()

        self.assertEqual(w1.worker_id, "node-1")
        self.assertEqual(w2.worker_id, "node-2")
        self.assertEqual(w3.worker_id, "node-1")

    def test_load_balancer_least_connections_strategy(self):
        clock = MockClock()
        lb = AgentFleetLoadBalancer(strategy=RoutingStrategy.LEAST_CONNECTIONS, time_func=clock)
        w1 = lb.add_worker("node-1")
        w2 = lb.add_worker("node-2")

        w1.active_connections = 5
        w2.active_connections = 1

        selected = lb.select_worker()
        self.assertEqual(selected.worker_id, "node-2")

    def test_load_balancer_lowest_latency_strategy(self):
        clock = MockClock()
        lb = AgentFleetLoadBalancer(strategy=RoutingStrategy.LOWEST_LATENCY, time_func=clock)
        w1 = lb.add_worker("node-1")
        w2 = lb.add_worker("node-2")

        lb.record_transaction("node-1", latency_ms=120.0, success=True)
        lb.record_transaction("node-2", latency_ms=45.0, success=True)

        selected = lb.select_worker()
        self.assertEqual(selected.worker_id, "node-2")

    def test_load_balancer_circuit_breaker_ejection(self):
        clock = MockClock()
        lb = AgentFleetLoadBalancer(strategy=RoutingStrategy.ROUND_ROBIN, time_func=clock)
        lb.add_worker("node-1", failure_threshold=2)
        lb.add_worker("node-2", failure_threshold=2)

        # Trigger failure on node-1 until circuit breaker opens
        lb.record_transaction("node-1", latency_ms=500.0, success=False)
        lb.record_transaction("node-1", latency_ms=500.0, success=False)

        # node-1 is now OPEN; all traffic routes to node-2
        selected = lb.select_worker()
        self.assertEqual(selected.worker_id, "node-2")
        selected_again = lb.select_worker()
        self.assertEqual(selected_again.worker_id, "node-2")

    def test_load_balancer_snapshot(self):
        lb = AgentFleetLoadBalancer()
        lb.add_worker("node-alpha")
        snap = lb.snapshot()
        self.assertEqual(snap["total_workers"], 1)
        self.assertIn("node-alpha", snap["workers"])


if __name__ == "__main__":
    unittest.main()
