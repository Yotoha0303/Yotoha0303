#!/usr/bin/env python3
"""Offline tests; clocks, sleepers, and random sources are deterministic.

Run: python3 -m unittest discover -s scripts -p 'test_anti_ban_human_behavior_engine.py' -v
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import math
from pathlib import Path
import random
import statistics
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anti_ban_human_behavior_engine as engine


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start
        self.sleeps = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class ReadingDelayTests(unittest.TestCase):
    def test_default_samples_stay_in_inclusive_interval(self):
        delay = engine.PoissonReadingDelay(rng=random.Random(12))
        samples = [delay.sample() for _ in range(10_000)]
        self.assertTrue(all(isinstance(value, int) and 45 <= value <= 120 for value in samples))
        self.assertGreater(len(set(samples)), 40)

    def test_seeded_sampling_is_repeatable(self):
        left = engine.PoissonReadingDelay(rng=random.Random(7))
        right = engine.PoissonReadingDelay(rng=random.Random(7))
        self.assertEqual([left.sample() for _ in range(100)], [right.sample() for _ in range(100)])

    def test_mean_and_variance_match_poisson_not_uniform_or_exponential(self):
        delay = engine.PoissonReadingDelay(rng=random.Random(96))
        samples = [delay.sample() for _ in range(30_000)]
        self.assertAlmostEqual(statistics.mean(samples), 75.0, delta=0.2)
        self.assertAlmostEqual(statistics.pvariance(samples), 75.0, delta=2.5)

    def test_conditional_poisson_probabilities_on_small_interval(self):
        # For Poisson(2), relative masses at 1,2,3 are 2:2:4/3 = 3:3:2.
        delay = engine.PoissonReadingDelay(mean_seconds=2, min_seconds=1, max_seconds=3,
                                           rng=Mock(random=Mock(side_effect=[0.0, 0.374, 0.376,
                                                                             0.749, 0.751, 0.999])))
        self.assertEqual([delay.sample() for _ in range(6)], [1, 1, 2, 2, 3, 3])

    def test_default_both_endpoints_are_reachable(self):
        delay = engine.PoissonReadingDelay(rng=Mock(random=Mock(side_effect=[0.0, math.nextafter(1.0, 0.0)])))
        self.assertEqual(delay.sample(), 45)
        self.assertEqual(delay.sample(), 120)

    def test_singleton_interval(self):
        delay = engine.PoissonReadingDelay(mean_seconds=60, min_seconds=60, max_seconds=60)
        self.assertEqual([delay.sample() for _ in range(5)], [60] * 5)

    def test_invalid_reading_configuration(self):
        for changes in ({"mean_seconds": 0}, {"mean_seconds": float("nan")},
                        {"mean_seconds": float("inf")}, {"mean_seconds": True},
                        {"mean_seconds": "75"}, {"mean_seconds": 121},
                        {"min_seconds": 0}, {"min_seconds": -1}, {"min_seconds": 45.5},
                        {"min_seconds": True}, {"max_seconds": 44}, {"max_seconds": 120.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.PoissonReadingDelay(**changes)

    def test_private_random_source_does_not_change_global_random(self):
        state = random.getstate()
        delay = engine.PoissonReadingDelay()
        delay.sample()
        engine.generate_cursor_trajectory((0, 0), (100, 100))
        self.assertEqual(random.getstate(), state)


class BezierTests(unittest.TestCase):
    def test_exact_cubic_endpoints_and_known_midpoint(self):
        controls = ((0, 0), (0, 10), (10, 10), (10, 0))
        self.assertEqual(engine.cubic_bezier_point(*controls, 0), (0.0, 0.0))
        self.assertEqual(engine.cubic_bezier_point(*controls, 1), (10.0, 0.0))
        self.assertEqual(engine.cubic_bezier_point(*controls, 0.5), (5.0, 7.5))

    def test_curve_stays_in_control_point_convex_hull(self):
        for index in range(101):
            x, y = engine.cubic_bezier_point((-5, 2), (-5, 30), (20, 30), (20, 2), index / 100)
            self.assertGreaterEqual(x, -5)
            self.assertLessEqual(x, 20)
            self.assertGreaterEqual(y, 2)
            self.assertLessEqual(y, 30)

    def test_timed_path_includes_exact_endpoints_and_duration(self):
        path = engine.generate_cursor_trajectory((12, 34), (500, 700), steps=40,
                                                  duration_seconds=1.75, rng=random.Random(4))
        self.assertEqual(len(path), 41)
        self.assertEqual(path[0], engine.CursorSample(12, 34, 0))
        self.assertEqual(path[-1], engine.CursorSample(500, 700, 1.75))
        self.assertTrue(all(a.time_seconds < b.time_seconds for a, b in zip(path, path[1:])))
        self.assertTrue(all(math.isfinite(point.x) and math.isfinite(point.y) for point in path))

    def test_path_is_seeded_and_varies_between_seeds(self):
        args = ((10, 20), (700, 400))
        first = engine.generate_cursor_trajectory(*args, rng=random.Random(1))
        self.assertEqual(first, engine.generate_cursor_trajectory(*args, rng=random.Random(1)))
        self.assertNotEqual(first, engine.generate_cursor_trajectory(*args, rng=random.Random(2)))

    def test_zero_curvature_is_straight_and_monotonic(self):
        path = engine.generate_cursor_trajectory((0, 0), (300, 600), curvature=0, rng=random.Random(5))
        for point in path:
            self.assertAlmostEqual(point.y, point.x * 2)
        self.assertTrue(all(a.x < b.x for a, b in zip(path, path[1:])))

    def test_easing_slows_departure_and_arrival(self):
        path = engine.generate_cursor_trajectory((0, 0), (1000, 0), steps=100,
                                                  curvature=0, rng=random.Random(6))
        distance = lambda left, right: math.hypot(right.x - left.x, right.y - left.y)
        middle = distance(path[49], path[50])
        self.assertLess(distance(path[0], path[1]), middle / 10)
        self.assertLess(distance(path[-2], path[-1]), middle / 10)

    def test_stationary_cursor_and_one_step_path(self):
        path = engine.generate_cursor_trajectory((25, -10), (25, -10), steps=4)
        for point in path:
            self.assertAlmostEqual(point.x, 25)
            self.assertAlmostEqual(point.y, -10)
        self.assertEqual(len(engine.generate_cursor_trajectory((0, 0), (1, 1), steps=1)), 2)

    def test_samples_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            engine.CursorSample(1, 2, 0).x = 9

    def test_invalid_curve_coordinates_and_parameters(self):
        for point in (None, "12", (1,), (1, 2, 3), (True, 2), (float("inf"), 1), ("1", 2)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                engine.cubic_bezier_point(point, (1, 2), (2, 3), (3, 4), 0.5)
        for parameter in (-0.1, 1.1, float("nan"), True):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                engine.cubic_bezier_point((0, 0), (1, 2), (2, 3), (3, 4), parameter)

    def test_invalid_trajectory_configuration(self):
        for changes in ({"steps": 0}, {"steps": -1}, {"steps": 1.5}, {"steps": True},
                        {"duration_seconds": 0}, {"duration_seconds": float("nan")},
                        {"duration_seconds": float("inf")}, {"curvature": -0.1},
                        {"curvature": 1.1}, {"curvature": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.generate_cursor_trajectory((0, 0), (10, 10), **changes)


class DomainTests(unittest.TestCase):
    def test_url_case_port_path_and_trailing_dot_share_host(self):
        for domain in ("example.com", "EXAMPLE.COM", "example.com.", "example.com:443",
                       "https://Example.COM.:8443/path?query=1#fragment", "http://example.com/chat"):
            with self.subTest(domain=domain):
                self.assertEqual(engine.normalize_domain(domain), "example.com")

    def test_unicode_and_ip_normalization(self):
        self.assertEqual(engine.normalize_domain("https://bücher.example"), "xn--bcher-kva.example")
        self.assertEqual(engine.normalize_domain("https://[2001:0db8::1]:443/a"), "2001:db8::1")
        self.assertEqual(engine.normalize_domain("http://127.0.0.1:8080"), "127.0.0.1")

    def test_normalized_keys_can_be_normalized_again(self):
        for domain in ("https://[2001:0db8::1]/chat", "https://bücher.example", "EXAMPLE.COM."):
            with self.subTest(domain=domain):
                normalized = engine.normalize_domain(domain)
                self.assertEqual(engine.normalize_domain(normalized), normalized)

    def test_subdomains_remain_distinct(self):
        self.assertNotEqual(engine.normalize_domain("mail.example.com"), engine.normalize_domain("example.com"))

    def test_invalid_domains(self):
        for domain in (None, "", " ", "exa mple.com", "https:///path", "ftp://example.com",
                       "https://user:secret@example.com", "example.com:bad", "example.com:99999",
                       "https://example.com\n/", "-bad.example", "bad_.example", "example..com", "."):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                engine.normalize_domain(domain)


class SlidingWindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = engine.SlidingWindowRateLimiter(clock=self.clock)

    def test_default_daily_ceiling_is_25(self):
        self.assertEqual(self.limiter.status("example.com").remaining, 25)
        self.assertTrue(all(self.limiter.try_acquire("example.com") for _ in range(25)))
        self.assertFalse(self.limiter.try_acquire("example.com"))
        status = self.limiter.status("example.com")
        self.assertEqual((status.limit, status.used, status.remaining), (25, 25, 0))
        self.assertFalse(status.allowed)
        self.assertEqual(status.retry_after_seconds, engine.DAY_SECONDS)

    def test_domain_quotas_are_independent(self):
        limiter = engine.SlidingWindowRateLimiter(default_limit=1, clock=self.clock)
        self.assertTrue(limiter.try_acquire("example.com"))
        self.assertFalse(limiter.try_acquire("example.com"))
        self.assertTrue(limiter.try_acquire("other.example"))

    def test_normalized_aliases_cannot_bypass_quota(self):
        limiter = engine.SlidingWindowRateLimiter(default_limit=1, clock=self.clock)
        self.assertTrue(limiter.try_acquire("https://EXAMPLE.com.:443/chat"))
        self.assertFalse(limiter.try_acquire("http://example.com/other"))

    def test_normalized_domain_overrides_support_20_and_30_daily_limits(self):
        limiter = engine.SlidingWindowRateLimiter(domain_limits={"HTTPS://EXAMPLE.COM/a": 20,
                                                                 "other.example": 30}, clock=self.clock)
        for domain, expected in (("example.com", 20), ("other.example", 30), ("third.example", 25)):
            with self.subTest(domain=domain):
                self.assertTrue(all(limiter.try_acquire(domain) for _ in range(expected)))
                self.assertFalse(limiter.try_acquire(domain))

    def test_exact_window_boundary_expires_oldest_only(self):
        limiter = engine.SlidingWindowRateLimiter(default_limit=2, clock=self.clock)
        limiter.try_acquire("example.com")
        self.clock.advance(10)
        limiter.try_acquire("example.com")
        self.clock.advance(engine.DAY_SECONDS - 10 - 0.5)
        self.assertFalse(limiter.try_acquire("example.com"))
        self.assertEqual(limiter.status("example.com").retry_after_seconds, 0.5)
        self.clock.advance(0.5)
        self.assertEqual(limiter.status("example.com").remaining, 1)
        self.assertTrue(limiter.try_acquire("example.com"))
        self.assertFalse(limiter.try_acquire("example.com"))
        self.assertEqual(limiter.status("example.com").retry_after_seconds, 10)

    def test_midnight_does_not_reset_a_rolling_window(self):
        clock = FakeClock(engine.DAY_SECONDS - 1)
        limiter = engine.SlidingWindowRateLimiter(default_limit=1, clock=clock)
        limiter.try_acquire("example.com")
        clock.advance(2)
        self.assertFalse(limiter.try_acquire("example.com"))
        self.assertEqual(limiter.status("example.com").retry_after_seconds, engine.DAY_SECONDS - 2)

    def test_denials_and_status_reads_do_not_extend_window_or_consume_slots(self):
        limiter = engine.SlidingWindowRateLimiter(default_limit=1, clock=self.clock)
        self.assertEqual(limiter.status("example.com").used, 0)
        limiter.try_acquire("example.com")
        self.clock.advance(100)
        for _ in range(5):
            self.assertFalse(limiter.try_acquire("example.com"))
            self.assertEqual(limiter.status("example.com").used, 1)
        self.clock.advance(engine.DAY_SECONDS - 100)
        self.assertTrue(limiter.try_acquire("example.com"))

    def test_long_idle_period_clears_all_old_events(self):
        self.limiter.try_acquire("example.com")
        self.clock.advance(engine.DAY_SECONDS * 3)
        self.assertEqual(self.limiter.status("example.com").remaining, 25)

    def test_concurrent_acquisition_does_not_exceed_ceiling(self):
        gate = threading.Barrier(32)

        def acquire(_):
            gate.wait(timeout=5)
            return self.limiter.try_acquire("example.com")

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(acquire, range(32)))
        self.assertEqual(sum(results), 25)
        self.assertEqual(self.limiter.status("example.com").used, 25)

    def test_bad_clock_values_fail_closed(self):
        self.limiter.try_acquire("example.com")
        self.clock.advance(-1)
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.limiter.try_acquire("example.com")
        self.clock.advance(1)
        self.assertEqual(self.limiter.status("example.com").used, 1)
        for invalid in (float("inf"), float("nan"), True, "1000"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                engine.SlidingWindowRateLimiter(clock=lambda: invalid).try_acquire("example.com")

    def test_invalid_limiter_configuration(self):
        for changes in ({"default_limit": 0}, {"default_limit": -1}, {"default_limit": True},
                        {"default_limit": 2.5}, {"window_seconds": 0}, {"window_seconds": -1},
                        {"window_seconds": float("inf")}, {"window_seconds": float("nan")},
                        {"domain_limits": {"example.com": 0}},
                        {"domain_limits": {"example.com": 20, "https://EXAMPLE.COM": 30}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.SlidingWindowRateLimiter(**changes)


class ChallengeTests(unittest.TestCase):
    def test_clean_page_starts_and_stays_closed(self):
        breaker = engine.ChallengeCircuitBreaker()
        self.assertFalse(breaker.is_open)
        self.assertIsNone(breaker.reason)
        self.assertFalse(breaker.inspect("<h1>Welcome to the API documentation</h1>", status_code=200))
        breaker.raise_if_open()

    def test_captcha_variants_trip_on_first_response(self):
        for content in ("CAPTCHA required", '<div class="g-recaptcha">',
                        '<script src="https://www.google.com/recaptcha/api.js"></script>',
                        '<div class="hcaptcha">', b"\xff Please complete the captcha"):
            with self.subTest(content=content):
                breaker = engine.ChallengeCircuitBreaker()
                self.assertTrue(breaker.inspect(content, status_code=200))
                self.assertEqual(breaker.reason, "captcha")
                with self.assertRaises(engine.CircuitOpenError):
                    breaker.raise_if_open()

    def test_cloudflare_and_turnstile_markers(self):
        for content in ('<div class="cf-turnstile">', "https://challenges.cloudflare.com/turnstile/v0/api.js",
                        "/cdn-cgi/challenge-platform/h/g/orchestrate", "window._cf_chl_opt = {}",
                        '<div id="cf-chl-widget">'):
            with self.subTest(content=content):
                breaker = engine.ChallengeCircuitBreaker()
                self.assertTrue(breaker.inspect(content))
                self.assertEqual(breaker.reason, "browser_challenge")

    def test_human_verification_and_challenge_phrases(self):
        for content in ("Verify that you are human", "Verify you're a human", "Are you a human?",
                        "Please verify that you are not a robot", "Confirm you are human",
                        "Please complete the challenge", "Complete this security challenge",
                        "Checking your browser", "Security challenge", "Challenge required",
                        "Attention required", "Unusual traffic", "Automated queries", "Access denied"):
            with self.subTest(content=content):
                self.assertTrue(engine.ChallengeCircuitBreaker().inspect(content))

    def test_html_entities_tags_and_whitespace_in_challenge_phrase(self):
        breaker = engine.ChallengeCircuitBreaker()
        self.assertTrue(breaker.inspect("<p>Verify&nbsp;<b>that you</b>\n are\t human</p>"))
        self.assertEqual(breaker.reason, "human_verification")

    def test_challenge_header_is_case_insensitive(self):
        breaker = engine.ChallengeCircuitBreaker()
        self.assertTrue(breaker.inspect(headers={"CF-Mitigated": " Challenge "}))
        self.assertEqual(breaker.reason, "challenge_header")

    def test_http_denials_and_rate_limits_trip_without_html(self):
        for status in (403, 429):
            with self.subTest(status=status):
                breaker = engine.ChallengeCircuitBreaker()
                self.assertTrue(breaker.inspect(status_code=status))
                self.assertEqual(breaker.reason, f"http_{status}")

    def test_generic_server_error_or_challenge_discussion_does_not_trip(self):
        for content, status in (("Server maintenance", 503), ("Temporary server error", 500),
                                ("Our team enjoys an engineering challenge", 200),
                                ("The recaptchable variable is a test fixture", 200)):
            with self.subTest(content=content):
                self.assertFalse(engine.ChallengeCircuitBreaker().inspect(content, status_code=status))

    def test_trip_is_latched_and_keeps_original_reason(self):
        breaker = engine.ChallengeCircuitBreaker()
        breaker.inspect("CAPTCHA")
        self.assertTrue(breaker.inspect("Welcome back", status_code=200))
        self.assertTrue(breaker.inspect(status_code=429))
        self.assertEqual(breaker.reason, "captcha")

    def test_explicit_reset_and_retrip(self):
        breaker = engine.ChallengeCircuitBreaker()
        breaker.inspect("CAPTCHA")
        breaker.reset_after_review()
        self.assertFalse(breaker.is_open)
        self.assertIsNone(breaker.reason)
        breaker.raise_if_open()
        self.assertTrue(breaker.inspect("Security verification"))

    def test_invalid_response_inputs(self):
        for content in (None, 123, {}):
            with self.subTest(content=content), self.assertRaises(ValueError):
                engine.ChallengeCircuitBreaker().inspect(content)
        for status in (True, 99, 600, 403.0, "403"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                engine.ChallengeCircuitBreaker().inspect(status_code=status)
        with self.assertRaises(ValueError):
            engine.ChallengeCircuitBreaker().inspect(headers={"CF-Mitigated": 1})


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.behavior = engine.AntiBanHumanBehaviorEngine(
            rng=random.Random(42), clock=self.clock, sleep_func=self.clock.sleep)

    def test_before_action_waits_then_consumes_exactly_one_slot(self):
        def sleep(seconds):
            self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 0)
            self.clock.sleep(seconds)

        self.behavior._sleep = sleep
        delay = self.behavior.before_action("https://EXAMPLE.COM/chat")
        self.assertTrue(45 <= delay <= 120)
        self.assertEqual(self.clock.sleeps, [delay])
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 1)

    def test_each_action_gets_its_own_reading_wait(self):
        delays = [self.behavior.before_action("example.com") for _ in range(3)]
        self.assertEqual(self.clock.sleeps, delays)
        self.assertEqual(self.clock(), 1_000 + sum(delays))
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 3)

    def test_quota_exhaustion_stops_without_another_wait(self):
        for _ in range(25):
            self.behavior.before_action("example.com")
        with self.assertRaises(engine.RateLimitExceeded) as raised:
            self.behavior.before_action("example.com")
        self.assertEqual(raised.exception.domain, "example.com")
        self.assertGreater(raised.exception.retry_after_seconds, 0)
        self.assertEqual(len(self.clock.sleeps), 25)
        self.behavior.before_action("other.example")

    def test_captcha_stops_all_domains_without_sleep_or_quota_use(self):
        self.assertTrue(self.behavior.observe_response("CAPTCHA required"))
        for domain in ("example.com", "other.example"):
            with self.subTest(domain=domain), self.assertRaises(engine.CircuitOpenError):
                self.behavior.before_action(domain)
            self.assertEqual(self.behavior.rate_limiter.status(domain).used, 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_time_and_clean_responses_cannot_automatically_resume(self):
        self.behavior.observe_response("Checking your browser")
        self.clock.advance(engine.DAY_SECONDS * 7)
        self.assertTrue(self.behavior.observe_response("Welcome", status_code=200))
        with self.assertRaises(engine.CircuitOpenError):
            self.behavior.before_action("example.com")

    def test_reset_resumes_and_preserves_existing_quota(self):
        self.behavior.before_action("example.com")
        self.behavior.observe_response(status_code=429)
        self.behavior.reset_after_review()
        self.behavior.before_action("example.com")
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 2)

    def test_interrupted_sleep_consumes_no_quota(self):
        self.behavior._sleep = Mock(side_effect=InterruptedError("cancelled"))
        with self.assertRaises(InterruptedError):
            self.behavior.before_action("example.com")
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 0)

    def test_challenge_observed_by_another_thread_during_wait_blocks_dispatch(self):
        waiting, resume = threading.Event(), threading.Event()

        def sleep(seconds):
            waiting.set()
            if not resume.wait(timeout=5):
                raise TimeoutError("test did not resume sleeping action")
            self.clock.sleep(seconds)

        self.behavior._sleep = sleep
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.behavior.before_action, "example.com")
            try:
                self.assertTrue(waiting.wait(timeout=5))
                self.assertTrue(self.behavior.observe_response("Verify you are human"))
            finally:
                resume.set()
            with self.assertRaises(engine.CircuitOpenError):
                future.result(timeout=5)
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 0)

    def test_quota_is_rechecked_if_consumed_during_wait(self):
        behavior = engine.AntiBanHumanBehaviorEngine(default_daily_limit=1, clock=self.clock,
                                                     sleep_func=self.clock.sleep)

        def sleep(seconds):
            self.clock.sleep(seconds)
            self.assertTrue(behavior.rate_limiter.try_acquire("example.com"))

        behavior._sleep = sleep
        with self.assertRaises(engine.RateLimitExceeded):
            behavior.before_action("example.com")
        self.assertEqual(behavior.rate_limiter.status("example.com").used, 1)

    def test_concurrent_engine_actions_respect_domain_ceiling(self):
        def attempt(_):
            try:
                self.behavior.before_action("example.com")
                return True
            except engine.RateLimitExceeded:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            admitted = list(pool.map(attempt, range(40)))
        self.assertEqual(sum(admitted), 25)
        self.assertEqual(len(self.clock.sleeps), 25)

    def test_cursor_facade_returns_a_timed_path_without_sleep_or_quota_use(self):
        path = self.behavior.cursor_trajectory((0, 0), (100, 200), steps=5, duration_seconds=0.5)
        self.assertEqual(len(path), 6)
        self.assertEqual(path[-1], engine.CursorSample(100, 200, 0.5))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.behavior.rate_limiter.status("example.com").used, 0)

    def test_invalid_domain_rejected_before_wait(self):
        with self.assertRaises(ValueError):
            self.behavior.before_action("https://")
        self.assertEqual(self.clock.sleeps, [])

    def test_ipv6_url_uses_same_quota_as_its_canonical_key(self):
        self.behavior.before_action("https://[2001:db8::1]:8443/chat")
        self.assertEqual(self.behavior.rate_limiter.status("2001:db8::1").used, 1)


if __name__ == "__main__":
    unittest.main()
