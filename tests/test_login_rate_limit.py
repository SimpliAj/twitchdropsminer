import unittest

from fastapi import HTTPException

from src.web import app as app_module


class TestLoginRateLimit(unittest.TestCase):
    """
    Regression test for the gap noted while porting upstream TwitchDropsMiner
    v1.3.0's dashboard password protection (#104): /__auth_login had no
    brute-force protection at all -- unlimited password guesses against
    whatever the user set at /__setup. _check_login_rate_limit is the shared
    two-tier limiter (global ceiling + per-IP ceiling) now called from that
    endpoint before comparing the submitted password.
    """

    def setUp(self):
        app_module._LOGIN_ATTEMPTS.clear()

    def test_allows_attempts_under_the_per_ip_limit(self):
        for _ in range(app_module._LOGIN_ATTEMPTS_PER_IP_MAX):
            app_module._check_login_rate_limit("1.2.3.4")  # must not raise

    def test_blocks_the_same_ip_past_its_limit(self):
        for _ in range(app_module._LOGIN_ATTEMPTS_PER_IP_MAX):
            app_module._check_login_rate_limit("1.2.3.4")
        with self.assertRaises(HTTPException) as ctx:
            app_module._check_login_rate_limit("1.2.3.4")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers["Retry-After"], "60")

    def test_a_different_ip_is_not_blocked_by_another_ips_attempts(self):
        for _ in range(app_module._LOGIN_ATTEMPTS_PER_IP_MAX):
            app_module._check_login_rate_limit("1.2.3.4")
        app_module._check_login_rate_limit("5.6.7.8")  # must not raise

    def test_blocks_once_the_global_ceiling_is_reached_even_across_many_ips(self):
        for i in range(app_module._LOGIN_ATTEMPTS_GLOBAL_MAX):
            # Spread across distinct IPs so the per-IP ceiling is never hit
            # first -- this must isolate the GLOBAL ceiling specifically.
            app_module._check_login_rate_limit(f"10.0.0.{i}")
        with self.assertRaises(HTTPException) as ctx:
            app_module._check_login_rate_limit("10.0.0.999")
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
