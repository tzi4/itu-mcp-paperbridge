from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from paperbridge import DocumentStore, Rights


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install the optional MCP dependency")
class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_client_receives_exact_document_text(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "note.txt"
            path.write_text("Original rocket landing transport fixture.", encoding="utf-8")
            store = DocumentStore(root / "state")
            item = store.ingest(path, title="Transport fixture", url="https://example.org/demo",
                                rights=Rights(True, True, "Own original fixture"))
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "paperbridge.mcp_server"],
                env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                     "PAPERBRIDGE_DATA_DIR": str(root / "state")})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({t.name for t in tools.tools},
                                     {"search", "fetch", "papers_search", "papers_resources", "papers_library_query",
                                      "papers_resolve", "papers_status", "papers_get_page", "papers_view_page"})
                    self.assertTrue(all(t.annotations.readOnlyHint for t in tools.tools))
                    local = await session.call_tool("papers_search", {
                        "query": "rocket", "local_only": True, "discovery": "combined"})
                    local_body = json.loads(next(c.text for c in local.content if c.type == "text"))
                    self.assertEqual(local_body["scope"], "local_archive")
                    self.assertEqual(local_body["results"][0]["first_chunk_id"], item["first_chunk_id"])
                    result = await session.call_tool("fetch", {"id": item["first_chunk_id"]})
                    self.assertFalse(result.isError)
                    body = json.loads(next(c.text for c in result.content if c.type == "text"))
                    self.assertEqual(body["text"], path.read_text())
                    self.assertEqual(body["url"], "https://example.org/demo")
                    store.ingest(path, title="Transport fixture", url="https://example.org/demo",
                                 rights=Rights(True, False, "Revoked in fixture"))
                    denied = await session.call_tool("fetch", {"id": item["first_chunk_id"]})
                    self.assertTrue(denied.isError)
                    self.assertNotIn(path.read_text(), str(denied))
