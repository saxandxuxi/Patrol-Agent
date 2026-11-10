import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.services.llm_rate_limit import SharedRateLimiter


class _FakeTime:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += max(0.0, float(seconds))


class SharedRateLimiterTests(unittest.TestCase):
    def test_three_rpm_is_shared_across_concurrent_callers(self):
        fake = _FakeTime()
        limiter = SharedRateLimiter(clock=fake.monotonic, sleep=fake.sleep)
        grants = []
        grants_lock = threading.Lock()

        def request(_):
            limiter.acquire("dashscope|https://example.test|kimi", rpm=3)
            with grants_lock:
                grants.append(fake.monotonic())

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(request, range(4)))

        grants.sort()
        self.assertEqual(len(grants), 4)
        self.assertGreaterEqual(grants[1] - grants[0], 20.0)
        self.assertGreaterEqual(grants[2] - grants[1], 20.0)
        self.assertGreaterEqual(grants[3] - grants[2], 20.0)

    def test_defer_delays_only_the_matching_model(self):
        fake = _FakeTime()
        limiter = SharedRateLimiter(clock=fake.monotonic, sleep=fake.sleep)

        limiter.acquire("provider|url|limited", rpm=3)
        limiter.defer("provider|url|limited", 60)
        limiter.acquire("provider|url|limited", rpm=3)
        limited_at = fake.monotonic()

        fake.now = 0.0
        limiter.acquire("provider|url|other", rpm=3)

        self.assertEqual(limited_at, 60.0)
        self.assertEqual(fake.monotonic(), 0.0)


if __name__ == "__main__":
    unittest.main()
