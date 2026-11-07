#!/usr/bin/env python3
"""Standard-library browser pacing, cursor paths, quotas, and challenge stops.

Reading delays follow a discrete Poisson distribution conditioned on the inclusive
45..120 second interval, rather than clipping samples or using an exponential
distribution. Cursor paths are timed cubic Bezier samples with eased endpoints.

Call ``before_action(domain)`` immediately before each browser/outreach action and
``observe_response(...)`` after each response, including rendered challenge HTML.
The first action also waits a reading interval. A challenge latches a global stop
until ``reset_after_review()`` is explicitly called. Only timing/path data are
generated; browser fingerprint identifiers are neither stored nor modified.
Quotas and stops are in memory: share one engine across threads; separate
processes/restarts require an external shared state store.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
import html
import ipaddress
import math
import random
import re
import threading
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit


DAY_SECONDS = 86_400.0


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class PoissonReadingDelay:
    """Sample X | min_seconds <= X <= max_seconds for X ~ Poisson(mean).

    The parameter is the *unconditioned* Poisson mean; truncation can shift the
    resulting mean slightly. A precomputed CDF avoids unbounded rejection loops
    and artificial probability spikes at the interval's endpoints.
    """

    def __init__(
        self,
        mean_seconds: float = 75.0,
        min_seconds: int = 45,
        max_seconds: int = 120,
        rng: random.Random | None = None,
    ) -> None:
        _positive_integer(min_seconds, "min_seconds")
        _positive_integer(max_seconds, "max_seconds")
        mean = _finite_number(mean_seconds, "mean_seconds")
        if not min_seconds <= mean <= max_seconds:
            raise ValueError("mean_seconds must lie between min_seconds and max_seconds")
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.mean_seconds = mean
        self._rng = rng if rng is not None else random.Random()
        values = range(min_seconds, max_seconds + 1)
        # The common exp(-mean) factor cancels during normalization.
        log_weights = [value * math.log(mean) - math.lgamma(value + 1) for value in values]
        peak = max(log_weights)
        weights = [math.exp(weight - peak) for weight in log_weights]
        total = math.fsum(weights)
        cumulative = 0.0
        self._cdf: list[float] = []
        for weight in weights:
            cumulative += weight / total
            self._cdf.append(cumulative)
        self._cdf[-1] = 1.0

    def sample(self) -> int:
        """Return a reading delay in whole seconds without sleeping."""
        return self.min_seconds + bisect_right(self._cdf, self._rng.random())


Point = tuple[float, float]


def _point(value: Sequence[float], name: str) -> Point:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise ValueError(f"{name} must contain two finite coordinates")
    return (_finite_number(value[0], name), _finite_number(value[1], name))


def _bezier(start: Point, control1: Point, control2: Point, end: Point, t: float) -> Point:
    u = 1.0 - t
    weights = (u ** 3, 3.0 * u * u * t, 3.0 * u * t * t, t ** 3)
    return tuple(math.fsum(weight * point[axis] for weight, point in
                           zip(weights, (start, control1, control2, end)))
                 for axis in (0, 1))


def cubic_bezier_point(
    start: Sequence[float], control1: Sequence[float], control2: Sequence[float],
    end: Sequence[float], t: float,
) -> Point:
    """Evaluate a cubic Bezier curve at a parameter in the inclusive range 0..1."""
    parameter = _finite_number(t, "t")
    if not 0.0 <= parameter <= 1.0:
        raise ValueError("t must be between zero and one")
    return _bezier(_point(start, "start"), _point(control1, "control1"),
                   _point(control2, "control2"), _point(end, "end"), parameter)


@dataclass(frozen=True)
class CursorSample:
    x: float
    y: float
    time_seconds: float


def generate_cursor_trajectory(
    start: Sequence[float], end: Sequence[float], *, steps: int = 30,
    duration_seconds: float = 1.0, curvature: float = 0.2,
    rng: random.Random | None = None,
) -> tuple[CursorSample, ...]:
    """Return steps+1 timed samples, including both exact endpoints.

    Control points vary along and perpendicular to the direct path. Smoothstep
    timing slows departure and arrival. Coordinates use the caller's coordinate
    system; the caller must clip a path to its browser viewport if necessary.
    """
    first, last = _point(start, "start"), _point(end, "end")
    _positive_integer(steps, "steps")
    duration = _finite_number(duration_seconds, "duration_seconds")
    bend = _finite_number(curvature, "curvature")
    if duration <= 0 or not 0.0 <= bend <= 1.0:
        raise ValueError("duration_seconds must be positive and curvature must be in 0..1")
    source = rng if rng is not None else random.Random()
    dx, dy = last[0] - first[0], last[1] - first[1]
    if not math.isfinite(dx) or not math.isfinite(dy):
        raise ValueError("coordinate difference is too large")
    controls = []
    for low, high in ((0.2, 0.4), (0.6, 0.8)):
        along, across = source.uniform(low, high), source.uniform(-bend, bend)
        controls.append((first[0] + dx * along - dy * across,
                         first[1] + dy * along + dx * across))
    if any(not math.isfinite(coordinate) for control in controls for coordinate in control):
        raise ValueError("control point is too large")
    samples = []
    for index in range(steps + 1):
        fraction = index / steps
        parameter = fraction * fraction * (3.0 - 2.0 * fraction)
        x, y = _bezier(first, controls[0], controls[1], last, parameter)
        samples.append(CursorSample(x, y, duration * fraction))
    return tuple(samples)


def normalize_domain(domain: str) -> str:
    """Key HTTP(S) URLs/hostnames by lower-case IDNA host, ignoring port/path.

    Subdomains remain separate. Registrable-domain grouping needs a public suffix
    list; it cannot be inferred reliably using only the standard library.
    """
    if not isinstance(domain, str) or not domain or any(char.isspace() or ord(char) < 32
                                                       for char in domain):
        raise ValueError("domain must be a nonempty hostname or HTTP(S) URL")
    # Also accept our own canonical, unbracketed IPv6 keys on subsequent calls.
    try:
        return ipaddress.ip_address(domain).compressed
    except ValueError:
        pass
    try:
        parsed = urlsplit(domain if "://" in domain else "//" + domain)
        if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("only HTTP(S) URLs are supported")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are not allowed in domain URLs")
        host = parsed.hostname
        parsed.port  # Validate a supplied port, even though it is not part of the key.
        if not host:
            raise ValueError("missing hostname")
        host = host.removesuffix(".").encode("idna").decode("ascii").lower()
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        labels = host.split(".")
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in labels):
            raise ValueError("invalid hostname")
        return host
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid domain: " + str(error)) from error


@dataclass(frozen=True)
class RateLimitStatus:
    domain: str
    limit: int
    used: int
    remaining: int
    retry_after_seconds: float

    @property
    def allowed(self) -> bool:
        return self.remaining > 0


class SlidingWindowRateLimiter:
    """Thread-safe per-host quotas; defaults to 25 actions per rolling 24 hours.

    Events at exactly now-window_seconds have expired. Denied attempts and status
    queries never consume capacity. The clock must be monotonic and finite.
    """

    def __init__(
        self, default_limit: int = 25, window_seconds: float = DAY_SECONDS,
        domain_limits: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._default_limit = _positive_integer(default_limit, "default_limit")
        self._window_seconds = _finite_number(window_seconds, "window_seconds")
        if self._window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limits: dict[str, int] = {}
        for domain, limit in (domain_limits or {}).items():
            host = normalize_domain(domain)
            if host in self._limits:
                raise ValueError("duplicate normalized domain limit: " + host)
            self._limits[host] = _positive_integer(limit, "domain limit")
        self._clock = clock
        self._last_time: float | None = None
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _snapshot(self, host: str) -> tuple[RateLimitStatus, float]:
        # The caller holds _lock throughout the read/check/update transaction.
        now = _finite_number(self._clock(), "clock result")
        if self._last_time is not None and now < self._last_time:
            raise ValueError("clock must not move backwards")
        self._last_time = now
        events = self._events.get(host)
        if events is not None:
            while events and events[0] <= now - self._window_seconds:
                events.popleft()
            if not events:
                del self._events[host]
        used = len(events) if events else 0
        limit = self._limits.get(host, self._default_limit)
        retry = max(0.0, events[0] + self._window_seconds - now) if used >= limit else 0.0
        return RateLimitStatus(host, limit, used, max(0, limit - used), retry), now

    def status(self, domain: str) -> RateLimitStatus:
        host = normalize_domain(domain)
        with self._lock:
            return self._snapshot(host)[0]

    def try_acquire(self, domain: str) -> bool:
        host = normalize_domain(domain)
        with self._lock:
            status, now = self._snapshot(host)
            if not status.allowed:
                return False
            self._events.setdefault(host, deque()).append(now)
            return True


class CircuitOpenError(RuntimeError):
    """Automation is suspended pending explicit operator review."""


class RateLimitExceeded(RuntimeError):
    def __init__(self, status: RateLimitStatus) -> None:
        self.domain = status.domain
        self.retry_after_seconds = status.retry_after_seconds
        super().__init__(f"quota exhausted for {status.domain}; retry after "
                         f"{status.retry_after_seconds:.3f}s")


_CHALLENGE_PATTERNS = (
    ("captcha", re.compile(r"\b(?:captcha|recaptcha|hcaptcha|g-recaptcha)\b", re.I)),
    ("browser_challenge", re.compile(
        r"cf-turnstile|challenges\.cloudflare\.com|/cdn-cgi/challenge-platform/|"
        r"(?:_{1,2}cf_chl_|cf-chl-)[\w-]*", re.I)),
    ("human_verification", re.compile(
        r"\b(?:(?:verify|confirm) (?:that )?you(?: are|'re|’re) (?:not (?:a )?robot|(?:a )?human)|"
        r"are you (?:a )?human|checking (?:your )?browser|"
        r"security (?:check|verification|challenge)|challenge required|"
        r"complete (?:the|this) (?:security )?challenge|"
        r"attention required|unusual traffic|automated (?:queries|requests)|access denied)\b", re.I)),
)


class ChallengeCircuitBreaker:
    """Latched, global stop for challenge markers and HTTP 403/429 responses.

    Clean responses and elapsed time never reset a trip. Only a short reason is
    retained; response bodies, cookies, and other browser identity data are not.
    """

    def __init__(self) -> None:
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def inspect(
        self, content: str | bytes = "", *, status_code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bool:
        """Inspect a response and return whether the circuit is now open."""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            raise ValueError("content must be text or bytes")
        if status_code is not None and (isinstance(status_code, bool)
                                       or not isinstance(status_code, int)
                                       or not 100 <= status_code <= 599):
            raise ValueError("status_code must be an HTTP status integer")
        reason = f"http_{status_code}" if status_code in (403, 429) else None
        for name, value in (headers or {}).items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("header names and values must be strings")
            if name.casefold() == "cf-mitigated" and value.strip().casefold() == "challenge":
                reason = reason or "challenge_header"
        raw = html.unescape(content)
        visible = " ".join(re.sub(r"<[^>]*>", " ", raw).split())
        for label, pattern in _CHALLENGE_PATTERNS:
            if pattern.search(raw) or pattern.search(visible):
                reason = reason or label
                break
        with self._lock:
            if self._reason is None and reason is not None:
                self._reason = reason
            return self._reason is not None

    def raise_if_open(self) -> None:
        with self._lock:
            if self._reason is not None:
                raise CircuitOpenError("challenge stop requires review: " + self._reason)

    def reset_after_review(self) -> None:
        """Explicit operator acknowledgement; no automatic retry or CAPTCHA bypass."""
        with self._lock:
            self._reason = None


class AntiBanHumanBehaviorEngine:
    """Coordinate reading waits, action admission, cursor paths, and global stops.

    Admission waits are serialized across domains for one browser/session. Sleeping does
    not hold the response-inspection lock, so another thread can trip the circuit
    during a wait. Admission is rechecked afterwards and counts only a successful
    authorization. An interrupted wait consumes no quota. Callers must execute an
    authorized action immediately and submit its response for inspection.
    """

    def __init__(
        self, default_daily_limit: int = 25, domain_limits: Mapping[str, int] | None = None,
        rng: random.Random | None = None, clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rng = rng if rng is not None else random.Random()
        self.reading_delay = PoissonReadingDelay(rng=self._rng)
        self.rate_limiter = SlidingWindowRateLimiter(
            default_limit=default_daily_limit, domain_limits=domain_limits, clock=clock)
        self.circuit_breaker = ChallengeCircuitBreaker()
        self._sleep = sleep_func
        self._action_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def _ensure_ready(self, host: str) -> None:
        self.circuit_breaker.raise_if_open()
        status = self.rate_limiter.status(host)
        if not status.allowed:
            raise RateLimitExceeded(status)

    def before_action(self, domain: str) -> int:
        """Wait a reading interval, reserve one quota slot, and return seconds slept.

        Raises CircuitOpenError or RateLimitExceeded when activity must stop. This
        method does not wait out daily quotas or retry challenged requests.
        """
        host = normalize_domain(domain)
        with self._action_lock:
            with self._state_lock:
                self._ensure_ready(host)
                delay = self.reading_delay.sample()
            self._sleep(delay)
            with self._state_lock:
                self._ensure_ready(host)
                if not self.rate_limiter.try_acquire(host):
                    raise RateLimitExceeded(self.rate_limiter.status(host))
            return delay

    def observe_response(
        self, content: str | bytes = "", *, status_code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bool:
        """Return True if a response has left the global circuit open."""
        with self._state_lock:
            return self.circuit_breaker.inspect(content, status_code=status_code, headers=headers)

    def reset_after_review(self) -> None:
        with self._state_lock:
            self.circuit_breaker.reset_after_review()

    def cursor_trajectory(
        self, start: Sequence[float], end: Sequence[float], *, steps: int = 30,
        duration_seconds: float = 1.0, curvature: float = 0.2,
    ) -> tuple[CursorSample, ...]:
        return generate_cursor_trajectory(start, end, steps=steps,
                                          duration_seconds=duration_seconds,
                                          curvature=curvature, rng=self._rng)
