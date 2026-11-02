"""Actual MCP schemas, permissions and publication for cross-service reads."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp.server.fastmcp import FastMCP
from ninova_mcp.server import NinovaMcpApp, register_tools


class UnifiedProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "NINOVA_USERNAME": "dummy", "NINOVA_PASSWORD": "dummy",
            "NINOVA_STATE_DIR": self.temp.name,
            "NINOVA_ENV_FILE": str(Path(self.temp.name) / "no-env-file"),
            "ITU_MCP_UPDATE_CHECK": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        self.app = NinovaMcpApp()
        self.mcp = FastMCP("unified-protocol")
        register_tools(self.mcp, self.app, ["itu_search", "itu_open"], profile="router")

    def call(self, name, arguments):
        result = asyncio.run(self.mcp.call_tool(name, arguments))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_cross_service_reads_stay_directly_published_in_router(self):
        tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_tools())}
        for name in ("itu_search", "itu_open"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
        scope = tools["itu_search"].inputSchema["properties"]["scopes"]
        scopes = next(p for p in scope["anyOf"] if p.get("type") == "array")
        self.assertEqual(set(scopes["items"]["enum"]), {"mail", "ninova", "obs", "portal", "coursehub"})

    def test_valid_search_filters_reach_the_engine(self):
        with patch("ninova_mcp.unified.search", return_value={"results": []}) as run:
            self.call("itu_search", {"query": "MAT103E", "scopes": ["coursehub"],
                "coursehub_sites": ["MAT"], "coursehub_course_code": "MAT103E",
                "coursehub_authenticated": True, "include_body": False, "limit": 3})
        self.assertEqual(run.call_args.kwargs["coursehub_authenticated"], True)
        self.assertEqual(run.call_args.kwargs["coursehub_sites"], ["MAT"])
        self.assertEqual(run.call_args.kwargs["scopes"], ["coursehub"])

    def test_invalid_filters_do_not_reach_any_source(self):
        cases = ({"limit": True}, {"limit": "3"}, {"limit": 51},
                 {"scopes": []}, {"scopes": ["unknown"]},
                 {"coursehub_sites": []}, {"coursehub_authenticated": "false"},
                 {"include_body": "true"})
        with patch("ninova_mcp.unified.search") as run:
            for values in cases:
                with self.subTest(values=values), self.assertRaises(Exception):
                    self.call("itu_search", {"query": "MAT103E", **values})
            run.assert_not_called()

    def test_open_forwards_only_exact_ref_and_validated_text_bound(self):
        with patch("ninova_mcp.unified.open_ref", return_value={"status": "ok"}) as run:
            self.call("itu_open", {"ref": "itu://coursehub/synthetic", "max_chars": 1000})
            run.assert_called_once_with(self.app, "itu://coursehub/synthetic", max_chars=1000)
        with patch("ninova_mcp.unified.open_ref") as run:
            for value in (True, "1000", 999, 100001):
                with self.subTest(value=value), self.assertRaises(Exception):
                    self.call("itu_open", {"ref": "itu://coursehub/synthetic", "max_chars": value})
            run.assert_not_called()

    def test_unified_only_publication_does_not_enable_underlying_sources(self):
        self.app._client = Mock(side_effect=AssertionError("Unexpected account access"))
        self.app._course_hub = Mock()
        result = self.call("itu_search", {"query": "MAT103E"})
        self.assertEqual(set(result["sources"]), {"mail", "ninova", "obs", "portal", "coursehub"})
        self.assertTrue(all(item["status"] == "disabled" for item in result["sources"].values()))
        self.assertFalse(result["complete"])
        self.app._course_hub.materials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
