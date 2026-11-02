"""Exercise PaperBridge through itu-mcp, without an extra MCP connection."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import paperbridge
from paperbridge import Rights
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ninova_mcp.papers_client import PAPER_TOOLS
from ninova_mcp.remote import _selected_remote_tool_names, _build_fastmcp
from ninova_mcp.server import NinovaMcpApp, SERVER_INSTRUCTIONS

# These contract tests cover every tool, so they serve the full tool profile.
os.environ.setdefault("ITU_MCP_TOOL_PROFILE", "full")


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"NINOVA_STATE_DIR": str(self.root / "itu"),
            "PAPERBRIDGE_DATA_DIR": str(self.root / "papers"),
            "PAPERBRIDGE_CONTACT_EMAIL": "", "NINOVA_USERNAME": "", "NINOVA_PASSWORD": ""})
        env.start()
        self.addCleanup(env.stop)
        self.app = NinovaMcpApp()

    def test_invalid_page_delay_cannot_initialize_retrieval(self):
        for automatic, delay in [(True, 61), (False, 30), (True, True)]:
            with self.subTest(automatic=automatic, delay=delay), self.assertRaises(ValueError):
                self.app.papers_view_page('10.1234/not-downloaded', 1, automatic=automatic,
                                         continuation_delay_seconds=delay)
        self.assertIsNone(self.app._papers)

    def test_invalid_page_transport_cannot_initialize_retrieval(self):
        with self.assertRaises(ValueError):
            self.app.papers_view_page('10.1234/not-downloaded', 1, continuation_transport='invalid')
        self.assertIsNone(self.app._papers)

    def test_invalid_batch_cannot_initialize_retrieval(self):
        with self.assertRaises(ValueError):
            self.app.papers_view_pages([{"reference": "10.1234/a", "page": 1},
                                       {"reference": "10.1234/b", "page": 0}], automatic=True)
        self.assertIsNone(self.app._papers)

    def test_batch_preserves_permission_failure_without_rendering(self):
        self.import_fixture(external=False)
        with patch("paperbridge.rendering.render_page") as render:
            result = self.app.papers_view_pages([{"reference": "10.1234/pb-demo", "page": 1}])
        self.assertEqual(result["status"], "page_images_unavailable")
        self.assertEqual(result["selection_coverage"][0]["status"], "external_model_permission_required")
        render.assert_not_called()

    def import_fixture(self, external=True):
        source = self.root / "paper.txt"
        source.write_text("Original autonomous landing integration fixture.", encoding="utf-8")
        return self.app.papers.bridge.store.ingest(source, title="Synthetic integration fixture",
            url="https://doi.org/10.1234/pb-demo", rights=Rights(True, external, "Own synthetic fixture"))

    def test_client_is_lazy_and_shared(self):
        self.assertIsNone(self.app._papers)
        first = self.app.papers
        self.assertIs(first, self.app.papers)
        self.assertIsNone(self.app._mail)
        self.assertIsNone(self.app._client)

    def test_doi_from_web_search_reads_local_paper(self):
        self.import_fixture()
        result = self.app.papers_get_text("https://doi.org/10.1234/pb-demo")
        self.assertEqual(result["text"], "Original autonomous landing integration fixture.")
        self.assertEqual(result["metadata"]["content_level"], "full_text_excerpt")
        self.assertTrue(result["untrusted_external_content"])

    def test_missing_text_does_not_claim_paywall_was_opened(self):
        result = self.app.papers_get_text("10.1234/not-imported")
        self.assertEqual(result["status"], "full_text_unavailable")
        self.assertIsNone(result["text"])
        self.assertFalse(result["full_text_read"])

    def test_permission_failure_is_actionable_without_content(self):
        self.import_fixture(external=False)
        result = self.app.papers_get_text("10.1234/pb-demo")
        self.assertEqual(result["status"], "external_model_permission_required")
        self.assertNotIn("Original autonomous", json.dumps(result))

    def test_instances_do_not_share_documents(self):
        self.import_fixture()
        with patch.dict(os.environ, {"NINOVA_STATE_DIR": str(self.root / "second"),
                                    "PAPERBRIDGE_DATA_DIR": str(self.root / "second-papers")}):
            other = NinovaMcpApp()
            self.assertEqual(other.papers_get_text("10.1234/pb-demo")["status"], "full_text_unavailable")

    def test_search_uses_library_in_process(self):
        discovery = Mock()
        discovery.search.return_value = [{"id": "doi:10.1234/demo", "title": "Fixture",
                                          "url": "https://doi.org/10.1234/demo"}]
        self.app.papers.bridge.discovery = discovery
        result = self.app.papers_search("rocket", limit=3)
        self.assertEqual(result["results"][0]["title"], "Fixture")
        discovery.search.assert_called_once_with("rocket", 3)

    def test_local_only_search_reuses_archive_without_discovery(self):
        self.import_fixture()
        discovery = Mock()
        self.app.papers.bridge.discovery = discovery
        result = self.app.papers_search("autonomous", local_only=True)
        self.assertEqual(result["scope"], "local_archive")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["storage"], "local_archive")
        self.assertEqual(self.app.papers_search("absent", local_only=True)["results"], [])
        self.import_fixture(external=False)
        self.assertEqual(self.app.papers_search("autonomous", local_only=True)["results"], [])
        discovery.search.assert_not_called()

    def test_visual_probe_disabled_without_operator_setting(self):
        with patch.dict(os.environ, {"PAPERBRIDGE_VISUAL_PROBE": ""}):
            self.assertEqual(self.app.papers_visual_probe()["status"], "visual_probe_disabled")
        self.assertIsNone(self.app._papers)

    def test_page_preview_preserves_permission_failure(self):
        self.import_fixture(external=False)
        with patch("paperbridge.rendering.render_page") as render:
            result = self.app.papers_view_page("10.1234/pb-demo", 1)
        self.assertEqual(result["status"], "external_model_permission_required")
        self.assertNotIn("Original autonomous", json.dumps(result))
        render.assert_not_called()

    def test_remote_registry_includes_paper_tools(self):
        with patch.dict(os.environ, {"NINOVA_REMOTE_ALLOWED_TOOLS": ""}):
            selected = set(_selected_remote_tool_names())
        self.assertTrue({t["name"] for t in PAPER_TOOLS} <= selected)
        for forbidden in ("ingest", "set_rights", "download_url"):
            self.assertNotIn(forbidden, selected)

    def test_existing_remote_allowlist_must_explicitly_include_new_tools(self):
        with patch.dict(os.environ, {"NINOVA_REMOTE_ALLOWED_TOOLS": "papers_status,papers_get_text"}):
            self.assertEqual(set(_selected_remote_tool_names()), {"papers_status", "papers_get_text"})

    def test_server_instructions_explain_capabilities_and_limits(self):
        self.assertIn("papers_get_text", SERVER_INSTRUCTIONS)
        self.assertIn("papers_status", SERVER_INSTRUCTIONS)
        self.assertIn("Never claim to have read text that was not returned", SERVER_INSTRUCTIONS)

    def test_operator_browser_setup_does_not_start_mcp_or_load_credentials(self):
        from ninova_mcp.server import main
        with patch("paperbridge.cli.main") as setup, patch("ninova_mcp.server.build_stdio_server") as server:
            main(["--paper-browser", "start", "--paper-browser-port", "9333", "--paper-browser-headed"])
        setup.assert_called_once_with(["browser-session", "--port", "9333", "--headed"])
        server.assert_not_called()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_itu_stdio_returns_paper_text_with_existing_mail_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            from paperbridge import DocumentStore
            source = root / "paper.txt"
            source.write_text("Original in-process MCP fixture.", encoding="utf-8")
            store = DocumentStore(root / "papers")
            store.ingest(source, title="Transport fixture", url="https://doi.org/10.1234/pb-demo",
                         rights=Rights(True, True, "Own synthetic fixture"))
            env = {"PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(Path(paperbridge.__file__).parents[1])]),
                   "PAPERBRIDGE_DATA_DIR": str(root / "papers"), "NINOVA_STATE_DIR": str(root / "itu"),
                   "NINOVA_USERNAME": "", "NINOVA_PASSWORD": "", "ITU_MCP_TOOL_PROFILE": "full"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "ninova_mcp"],
                                          cwd=temp, env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertEqual(init.serverInfo.name, "itu-mcp")
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertTrue({t["name"] for t in PAPER_TOOLS} <= names)
                    self.assertIn("mail_get_attachment", names)
                    local = await session.call_tool("papers_search", {
                        "query": "fixture", "local_only": True})
                    self.assertFalse(local.isError)
                    local_body = json.loads(next(c.text for c in local.content if c.type == "text"))
                    self.assertEqual(local_body["scope"], "local_archive")
                    self.assertEqual(len(local_body["results"]), 1)
                    result = await session.call_tool("papers_get_text", {"reference": "10.1234/pb-demo"})
                    self.assertFalse(result.isError)
                    body = json.loads(next(c.text for c in result.content if c.type == "text"))
                    self.assertEqual(body["text"], source.read_text())
                    self.assertEqual(body["url"], "https://doi.org/10.1234/pb-demo")
                    unavailable = await session.call_tool("papers_get_text", {"reference": "10.1234/missing"})
                    body = json.loads(next(c.text for c in unavailable.content if c.type == "text"))
                    self.assertEqual(body["status"], "full_text_unavailable")

    async def test_remote_builder_uses_the_same_paper_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"NINOVA_STATE_DIR": temp, "PAPERBRIDGE_DATA_DIR": temp + "/papers",
                                        "NINOVA_REMOTE_ALLOWED_TOOLS": "papers_status,papers_get_text"}):
                server = _build_fastmcp(NinovaMcpApp(), "/mcp")
                tools = await server.list_tools()
                self.assertEqual({t.name for t in tools}, {"papers_status", "papers_get_text"})
                self.assertTrue(all(t.annotations.readOnlyHint for t in tools))

    async def test_stdio_transmits_pdf_page_as_image_content(self):
        import base64
        import io
        from PIL import Image
        from paperbridge import DocumentStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "visual.pdf"
            Image.new("RGB", (120, 60), (255, 0, 0)).save(pdf, "PDF")
            store = DocumentStore(root / "papers")
            store.ingest(pdf, title="Synthetic red figure", url="https://doi.org/10.1234/visual",
                         rights=Rights(True, True, "Own synthetic figure"))
            env = {"PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(Path(paperbridge.__file__).parents[1])]),
                   "PAPERBRIDGE_DATA_DIR": str(root / "papers"), "NINOVA_STATE_DIR": str(root / "itu"),
                   "NINOVA_USERNAME": "", "NINOVA_PASSWORD": ""}
            env["PAPERBRIDGE_VISUAL_PROBE"] = "1"
            params = StdioServerParameters(command=sys.executable, args=["-m", "ninova_mcp"], env=env, cwd=temp)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    preview = await session.call_tool("papers_view_page", {
                        "reference": "10.1234/visual", "page": 1, "max_edge": 800, "automatic": True, "continuation_delay_seconds": 30, "continuation_transport": "mcp_apps"})
                    self.assertFalse(preview.isError)
                    self.assertEqual(preview.structuredContent["status"], "page_preview_ready")
                    self.assertEqual(preview.structuredContent["page"], 1)
                    self.assertTrue(preview.structuredContent["automatic"])
                    self.assertEqual(preview.structuredContent["continuation_delay_seconds"], 30)
                    self.assertEqual(preview.structuredContent["automatic_delivery"], "requested_not_verified")
                    self.assertTrue(all(c.type == "text" for c in preview.content))
                    self.assertNotIn("image_bytes", preview.structuredContent)
                    image = Image.open(io.BytesIO(base64.b64decode(preview.meta["paperbridge/image"]["data"])))
                    self.assertEqual(image.size, (800, 400))
                    batch = await session.call_tool("papers_view_pages", {
                        "pages": [{"reference": "10.1234/visual", "page": 1},
                                  {"reference": "10.1234/visual", "page": 99}],
                        "automatic": True, "max_edge": 800})
                    self.assertFalse(batch.isError)
                    self.assertEqual(batch.structuredContent["status"], "page_batch_preview_ready")
                    self.assertEqual(batch.structuredContent["requested_count"], 2)
                    self.assertEqual(batch.structuredContent["prepared_count"], 1)
                    self.assertEqual(batch.structuredContent["selection_coverage"][1]["status"],
                                     "invalid_or_unknown_reference")
                    self.assertTrue(batch.meta["paperbridge/page-fetch"])
                    self.assertNotIn("paperbridge/images", batch.meta)
                    self.assertEqual(len(batch.structuredContent["pages"]), 1)
                    self.assertEqual(batch.structuredContent["pages"][0]["widget_image_encoding"],
                                     "native_png_widget_fetch")
                    self.assertEqual(batch.structuredContent["continuation_transport"], "mcp_apps")
                    resource = await session.read_resource("ui://paperbridge/page-viewer-v13.html")
                    self.assertIn("Add page to chat", resource.contents[0].text)
                    probe = await session.call_tool("papers_visual_probe", {})
                    self.assertFalse(probe.isError)
                    self.assertTrue(probe.structuredContent["synthetic"])
                    self.assertTrue(all(c.type == "text" for c in probe.content))
                    listed = (await session.list_tools()).tools
                    descriptor = next(t for t in listed if t.name == "papers_visual_probe")
                    uri = descriptor.meta["ui"]["resourceUri"]
                    resource = await session.read_resource(uri)
                    self.assertIn("Add image to chat", resource.contents[0].text)
                    self.assertEqual(resource.contents[0].mimeType, "text/html;profile=mcp-app")
                    result = await session.call_tool("papers_get_page", {
                        "reference": "10.1234/visual", "page": 1, "max_edge": 800})
                    self.assertFalse(result.isError)
                    metadata = json.loads(next(c.text for c in result.content if c.type == "text"))
                    picture = next(c for c in result.content if c.type == "image")
                    self.assertEqual(picture.mimeType, "image/png")
                    self.assertEqual(result.structuredContent, metadata)
                    self.assertTrue(metadata["image_generated"])
                    self.assertEqual(metadata["model_visual_access"], "not_verified")
                    self.assertNotIn("image_bytes", metadata)
                    self.assertNotIn(picture.data, json.dumps(metadata))
                    self.assertEqual(metadata["page"], 1)
                    self.assertEqual(metadata["content_level"], "pdf_page_image")
                    image = Image.open(io.BytesIO(base64.b64decode(picture.data))).convert("RGB")
                    self.assertGreater(image.getpixel((400, 200))[0], 240)
                    self.assertLess(image.getpixel((400, 200))[1], 15)
