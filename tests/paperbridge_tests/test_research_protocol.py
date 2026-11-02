"""Validate the actual SDK registrations, including coercion boundaries."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ninova_mcp.server import NinovaMcpApp, register_tools as register_itu
from paperbridge.mcp_server import register_tools as register_papers
from paperbridge.research_resources import ResearchResourcesError


class ResearchProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Mock()
        self.facade = Mock()
        self.app = NinovaMcpApp.__new__(NinovaMcpApp)
        self.app.state_dir = Path(self.temp.name)
        self.app._papers = self.facade

    def servers(self):
        itu = FastMCP("research-itu")
        register_itu(itu, self.app, ["papers_search", "papers_resources", "papers_library_query"], profile="full")
        standalone = FastMCP("research-standalone")
        register_papers(standalone, self.bridge)
        return [itu, standalone]

    def test_actual_schemas_publish_bounds_and_source_options_in_both_hosts(self):
        for server in self.servers():
            with self.subTest(host=server.name):
                tools = {t.name: t for t in asyncio.run(server.list_tools())}
                search = tools["papers_search"].inputSchema["properties"]
                self.assertEqual(search["limit"]["minimum"], 1)
                self.assertEqual(search["limit"]["maximum"], 20)
                self.assertEqual(search["year_from"]["anyOf"][0]["maximum"], 2100)
                self.assertEqual(set(search["discovery"]["enum"]), {"crossref", "openalex", "combined"})
                self.assertEqual(tools["papers_resources"].inputSchema["properties"]["limit"]["maximum"], 50)
                self.assertEqual(tools["papers_library_query"].inputSchema["properties"]["query"]["maxLength"], 500)
                self.assertTrue(tools["papers_library_query"].annotations.readOnlyHint)
                self.assertFalse(tools["papers_library_query"].annotations.idempotentHint)

    def test_sdk_rejects_boolean_numeric_limits_and_invalid_options_before_dispatch(self):
        cases = [
            ("papers_search", {"query": "spacecraft", "limit": True}),
            ("papers_search", {"query": "spacecraft", "year_from": "2022"}),
            ("papers_search", {"query": "spacecraft", "discovery": "unknown"}),
            ("papers_search", {"query": "spacecraft", "local_only": "true"}),
            ("papers_resources", {"limit": 51}),
            ("papers_library_query", {"query": "x"}),
            ("papers_library_query", {"query": "spacecraft", "reviewed_only": 1}),
        ]
        for server in self.servers():
            for name, arguments in cases:
                with self.subTest(host=server.name, name=name, arguments=arguments):
                    with self.assertRaises(ToolError):
                        asyncio.run(server.call_tool(name, arguments))
        self.facade.call.assert_not_called()
        self.bridge.search.assert_not_called()
        self.bridge.resources.assert_not_called()
        self.bridge.library_query.assert_not_called()

    def test_standalone_directory_failure_keeps_explicit_status_and_reason(self):
        self.bridge.resources.side_effect = ResearchResourcesError("network_error", "Synthetic failure")
        server = self.servers()[1]
        content = asyncio.run(server.call_tool("papers_resources", {"query": "physics"}))
        result = json.loads(content[0].text)
        self.assertEqual(result["status"], "research_directory_unavailable")
        self.assertEqual(result["reason"], "network_error")
        self.assertFalse(result["full_text_read"])
        self.assertEqual(result["entitlement"], "not_checked")
