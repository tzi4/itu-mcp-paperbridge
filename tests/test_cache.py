from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from ninova_mcp.cache import TtlCache, parse_ttl_seconds


class TtlCacheTests(unittest.TestCase):
    def test_age_reports_live_entry_without_extending_expiry(self) -> None:
        cache: TtlCache[str] = TtlCache(default_ttl_seconds=60)
        with patch("ninova_mcp.cache.time.monotonic", return_value=100):
            cache.set("k", "v")
        with patch("ninova_mcp.cache.time.monotonic", return_value=112):
            self.assertEqual(cache.age_seconds("k"), 12)
        with patch("ninova_mcp.cache.time.monotonic", return_value=161):
            self.assertIsNone(cache.age_seconds("k"))
            self.assertIsNone(cache.get("k"))

    def test_set_get_and_expire(self) -> None:
        cache: TtlCache[str] = TtlCache(default_ttl_seconds=0.05)
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")
        time.sleep(0.07)
        self.assertIsNone(cache.get("k"))

    def test_invalidate_and_clear(self) -> None:
        cache: TtlCache[int] = TtlCache(default_ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.invalidate("a")
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        cache.clear()
        self.assertIsNone(cache.get("b"))

    def test_zero_ttl_disables_cache(self) -> None:
        cache: TtlCache[str] = TtlCache(default_ttl_seconds=0)
        cache.set("k", "v")
        self.assertIsNone(cache.get("k"))

    def test_parse_ttl_seconds(self) -> None:
        self.assertEqual(parse_ttl_seconds(None, 60), 60)
        self.assertEqual(parse_ttl_seconds("30", 60), 30.0)
        self.assertEqual(parse_ttl_seconds("bad", 60), 60)
        self.assertEqual(parse_ttl_seconds("-5", 60), 0.0)


if __name__ == "__main__":
    unittest.main()
