"""Actual MCP schema and dispatch checks for the compact CourseHub material index."""
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


class MaterialProtocolTests(unittest.TestCase):
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
        self.source = Mock()
        self.source.materials.return_value = {"materials": [], "count": 0, "has_more": False}
        self.app._course_hub = self.source
        self.mcp = FastMCP("coursehub-protocol")
        register_tools(self.mcp, self.app, ["coursehub_materials"], profile="full")

    def call(self, arguments):
        return asyncio.run(self.mcp.call_tool("coursehub_materials", arguments))

    def test_schema_has_actual_collection_and_pagination_constraints(self):
        tool = asyncio.run(self.mcp.list_tools())[0]
        props = tool.inputSchema["properties"]
        self.assertEqual(set(props["collection"]["enum"]),
                         {"all", "slides", "practice", "exams", "syllabi", "guides"})
        self.assertEqual(props["collection"]["default"], "all")
        self.assertEqual((props["limit"]["minimum"], props["limit"]["maximum"]), (1, 100))
        self.assertEqual((props["offset"]["minimum"], props["offset"]["maximum"]), (0, 10000))
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.destructiveHint)

    def test_typed_arguments_reach_only_the_material_index(self):
        result = self.call({"site": "MAT", "course_code": "MAT103E", "authenticated": True,
                            "collection": "slides", "offset": 4, "limit": 3})
        blocks = result[0] if isinstance(result, tuple) else result
        body = json.loads(blocks[0].text)
        self.assertEqual(body["materials"], [])
        self.assertTrue(body["untrusted_external_content"])
        self.source.materials.assert_called_once_with("MAT", course_code="MAT103E", authenticated=True,
                                                      collection="slides", offset=4, limit=3)
        self.source.read_document.assert_not_called()
        self.source.submit_action.assert_not_called()

    def test_invalid_limits_and_collections_are_rejected_before_source_dispatch(self):
        invalid = [{"collection": "unrecognized"}, {"limit": 0}, {"limit": 101},
                   {"limit": True}, {"limit": "2"}, {"offset": -1},
                   {"offset": 10001}, {"offset": False}, {"offset": "0"}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(Exception):
                self.call({"site": "MAT", **values})
        self.source.materials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
