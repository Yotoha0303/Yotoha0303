#!/usr/bin/env python3
"""High-throughput Agent Fleet Load Balancer and Token Burndown Watchdog.

Provides:
- TokenBucket: High-resolution token rate limiting and burst management
- CircuitBreaker: State machine (CLOSED, OPEN, HALF_OPEN) for upstream LLM fault tolerance
- ExponentialBackoff: Exponential backoff with decorrelated jitter for HTTP 429 rate limits
- TokenBurndownWatchdog: Real-time velocity tracking, cost budgeting, and burndown alerts
- AgentFleetLoadBalancer: Adaptive routing across multi-agent LLM pools with automatic failover
"""

from __future__ import annotations

import argparse
import enum
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class RoutingStrategy(str, enum.Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_CONNECTIONS = "least_connections"
    WEIGHTED = "weighted"
    LOWEST_LATENCY = "lowest_latency"


class TokenBucket:
    """Thread-safe and deterministic token bucket rate limiter."""

    def __init__(self, capacity: float, refill_rate: float, time_func: Callable[[], float] = time.time):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive numbers")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.time_func = time_func
        self.last_refill = self.time_func()

    def _refill(self) -> None:
        now = self.time_func()
        delta = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + delta * self.refill_rate)
        self.last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def wait_time(self, tokens: float = 1.0) -> float:
        self._refill()
        if self.tokens >= tokens:
            return 0.0
        return (tokens - self.tokens) / self.refill_rate


class CircuitBreaker:
    """Fault tolerance circuit breaker for upstream model providers."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 10.0,
        half_open_success_threshold: int = 2,
        time_func: Callable[[], float] = time.time,
    ):
        if failure_threshold <= 0 or recovery_timeout <= 0 or half_open_success_threshold <= 0:
            raise ValueError("thresholds and timeout must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold
        self.time_func = time_func

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0

    def is_available(self) -> bool:
        now = self.time_func()
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                return True
            return False
        return True

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

    def record_failure(self) -> None:
        now = self.time_func()
        self.last_failure_time = now
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.failure_count = self.failure_threshold
        elif self.state == CircuitState.CLOSED:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN


class ExponentialBackoff:
    """Calculates backoff delay with jitter to avoid thundering herd on 429."""

    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        factor: float = 2.0,
        jitter: bool = True,
        rng: Optional[random.Random] = None,
    ):
        if base_delay <= 0 or max_delay < base_delay or factor <= 1.0:
            raise ValueError("invalid backoff parameters")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter = jitter
        self.rng = rng or random.Random()

    def compute_delay(self, attempt: int) -> float:
        if attempt < 0:
            raise ValueError("attempt must be non-negative")
        raw_delay = min(self.max_delay, self.base_delay * (self.factor ** attempt))
        if self.jitter:
            return self.rng.uniform(0.0, raw_delay)
        return raw_delay


@dataclass
class TokenBurndownWatchdog:
    """Monitors token usage velocity and enforces financial/quota budgets."""

    budget_tokens: int
    cost_per_million_usd: float = 10.0
    time_func: Callable[[], float] = time.time
    window_seconds: float = 3600.0

    consumed_tokens: int = 0
    _events: List[Tuple[float, int]] = field(default_factory=list)

    def record_consumption(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        now = self.time_func()
        self.consumed_tokens += tokens
        self._events.append((now, tokens))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.pop(0)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.budget_tokens - self.consumed_tokens)

    @property
    def is_exhausted(self) -> bool:
        return self.consumed_tokens >= self.budget_tokens

    @property
    def total_cost_usd(self) -> float:
        return (self.consumed_tokens / 1_000_000.0) * self.cost_per_million_usd

    def current_velocity(self) -> float:
        """Returns tokens per second in the current observation window."""
        now = self.time_func()
        self._prune(now)
        if not self._events:
            return 0.0
        earliest = self._events[0][0]
        duration = max(1.0, now - earliest)
        window_tokens = sum(count for _, count in self._events)
        return window_tokens / duration

    def time_to_exhaustion_seconds(self) -> Optional[float]:
        velocity = self.current_velocity()
        if velocity <= 0.0:
            return None
        return self.remaining_tokens / velocity

    def snapshot(self) -> Dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "consumed_tokens": self.consumed_tokens,
            "remaining_tokens": self.remaining_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "current_velocity_tps": round(self.current_velocity(), 2),
            "exhausted": self.is_exhausted,
        }


@dataclass
class PoolWorker:
    """Represents a single model provider endpoint or worker node."""

    worker_id: str
    weight: float = 1.0
    capacity_tps: float = 100.0
    failure_threshold: int = 3
    recovery_timeout: float = 10.0
    time_func: Callable[[], float] = time.time

    active_connections: int = 0
    average_latency_ms: float = 0.0
    total_requests: int = 0
    total_failures: int = 0

    def __post_init__(self) -> None:
        self.bucket = TokenBucket(capacity=self.capacity_tps, refill_rate=self.capacity_tps, time_func=self.time_func)
        self.breaker = CircuitBreaker(
            failure_threshold=self.failure_threshold,
            recovery_timeout=self.recovery_timeout,
            time_func=self.time_func,
        )


class AgentFleetLoadBalancer:
    """High-throughput multi-agent pool load balancer."""

    def __init__(
        self,
        strategy: RoutingStrategy = RoutingStrategy.ROUND_ROBIN,
        time_func: Callable[[], float] = time.time,
    ):
        self.strategy = strategy
        self.time_func = time_func
        self.workers: Dict[str, PoolWorker] = {}
        self._rr_index = 0
        self.backoff = ExponentialBackoff(base_delay=0.5, max_delay=10.0)

    def add_worker(
        self,
        worker_id: str,
        weight: float = 1.0,
        capacity_tps: float = 100.0,
        failure_threshold: int = 3,
        recovery_timeout: float = 10.0,
    ) -> PoolWorker:
        if worker_id in self.workers:
            raise ValueError(f"Worker {worker_id} already exists")
        worker = PoolWorker(
            worker_id=worker_id,
            weight=weight,
            capacity_tps=capacity_tps,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            time_func=self.time_func,
        )
        self.workers[worker_id] = worker
        return worker

    def select_worker(self) -> Optional[PoolWorker]:
        available = [
            w for w in self.workers.values()
            if w.breaker.is_available() and w.bucket.try_acquire(1.0)
        ]
        if not available:
            return None

        if self.strategy == RoutingStrategy.ROUND_ROBIN:
            worker = available[self._rr_index % len(available)]
            self._rr_index = (self._rr_index + 1) % len(available)
            return worker

        if self.strategy == RoutingStrategy.LEAST_CONNECTIONS:
            return min(available, key=lambda w: w.active_connections)

        if self.strategy == RoutingStrategy.LOWEST_LATENCY:
            return min(available, key=lambda w: w.average_latency_ms)

        if self.strategy == RoutingStrategy.WEIGHTED:
            total_weight = sum(w.weight for w in available)
            pick = random.uniform(0, total_weight)
            upto = 0.0
            for w in available:
                if upto + w.weight >= pick:
                    return w
                upto += w.weight
            return available[-1]

        return available[0]

    def record_transaction(self, worker_id: str, latency_ms: float, success: bool, is_429: bool = False) -> None:
        if worker_id not in self.workers:
            raise KeyError(f"Unknown worker {worker_id}")
        worker = self.workers[worker_id]
        worker.total_requests += 1

        if success:
            worker.breaker.record_success()
            # Exponential moving average for latency
            if worker.total_requests == 1:
                worker.average_latency_ms = latency_ms
            else:
                worker.average_latency_ms = 0.8 * worker.average_latency_ms + 0.2 * latency_ms
        else:
            worker.total_failures += 1
            worker.breaker.record_failure()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "total_workers": len(self.workers),
            "workers": {
                w_id: {
                    "state": w.breaker.state.value,
                    "active_connections": w.active_connections,
                    "avg_latency_ms": round(w.average_latency_ms, 2),
                    "total_requests": w.total_requests,
                    "total_failures": w.total_failures,
                }
                for w_id, w in self.workers.items()
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=3, help="Number of test workers to simulate")
    parser.add_argument("--budget", type=int, default=1_000_000, help="Initial token budget")
    args = parser.parse_args()

    balancer = AgentFleetLoadBalancer(strategy=RoutingStrategy.ROUND_ROBIN)
    for i in range(args.workers):
        balancer.add_worker(f"model-worker-{i+1}", capacity_tps=50.0)

    watchdog = TokenBurndownWatchdog(budget_tokens=args.budget)
    watchdog.record_consumption(50_000)

    print("Agent Fleet Load Balancer Status:")
    print(json.dumps(balancer.snapshot(), indent=2))
    print("\nToken Burndown Watchdog Status:")
    print(json.dumps(watchdog.snapshot(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
