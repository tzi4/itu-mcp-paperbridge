"""Exercise byte-free batch manifests and existing-tool reads with a synthetic host."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import paperbridge.page_viewer as viewer
from paperbridge_tests.test_page_batch import fixture, PNG


class DeferredBatchResultTests(unittest.TestCase):
    def test_manual_batch_instructions_require_continue_after_add(self):
        result = viewer.pages_view_result(dict(pages=[fixture(1), fixture(2)], requested_count=2))
        instructions = result.structuredContent['next_action']
        self.assertIn('Add pages to chat, then Continue with these pages once', instructions)
        self.assertIn('Continue sends the selected image blocks with the message', instructions)
        self.assertIn('Add alone does not send that message', instructions)

    def test_initial_response_has_no_image_bytes_and_retains_exact_order_and_edge(self):
        pages = [dict(fixture(2), page=4), dict(fixture(1), page=2)]
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="0"):
            result = viewer.pages_view_result(dict(pages=pages, requested_count=2), automatic=True, max_edge=1600)
        serialized = result.model_dump_json(by_alias=True)
        self.assertNotIn(base64.b64encode(PNG).decode(), serialized)
        self.assertNotIn('"image_bytes"', serialized)
        self.assertNotIn('"paperbridge/image"', serialized)
        self.assertNotIn('"paperbridge/images"', serialized)
        self.assertTrue(result.meta["paperbridge/page-fetch"])
        manifest = result.structuredContent["pages"]
        self.assertEqual([(item["id"], item["page"]) for item in manifest],
                         [(item["id"], item["page"]) for item in pages])
        self.assertEqual([item["widget_image_fetch_max_edge"] for item in manifest], [1600, 1600])
        self.assertEqual([item["widget_image_byte_count"] for item in manifest], [len(PNG), len(PNG)])
        self.assertEqual(result.structuredContent["widget_image_byte_count_total"], 2 * len(PNG))
        self.assertEqual(result.structuredContent["model_visual_access"], "not_verified")
        self.assertEqual(pages[0]["image_bytes"], PNG)

    def test_invalid_edge_or_aggregate_budget_fails_before_wrapping(self):
        for edge in (True, 799, 2401):
            with self.subTest(edge=edge), patch.object(viewer, "page_view_result") as wrap:
                with self.assertRaises(ValueError):
                    viewer.pages_view_result(dict(pages=[fixture(1)]), max_edge=edge)
                wrap.assert_not_called()
        pages = [dict(fixture(i), image_bytes=PNG + b"x" * 4_000_000) for i in (1, 2)]
        with patch.object(viewer, "page_view_result") as wrap, self.assertRaises(ValueError):
            viewer.pages_view_result(dict(pages=pages))
        wrap.assert_not_called()

    def test_native_fetch_rechecks_permission_after_manifest_preparation(self):
        from paperbridge import DocumentStore, PaperBridge, Rights
        from paperbridge_tests.test_page_images import visual_pdf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original-fixture.pdf"
            source.write_bytes(visual_pdf())
            store = DocumentStore(root / "archive")
            store.ingest(source, title="Own drawing", url="https://doi.org/10.1234/visual",
                         rights=Rights(True, True, "Own drawing"))
            bridge = PaperBridge(store, Mock(), provider=Mock())
            initial = bridge.get_page("10.1234/visual", 1, 800)
            result = viewer.pages_view_result(dict(pages=[initial], requested_count=1), max_edge=800)
            selected = result.structuredContent["pages"][0]
            store.ingest(source, title="Own drawing", url="https://doi.org/10.1234/visual",
                         rights=Rights(True, False, "Own drawing"))
            with patch("paperbridge.rendering.render_page") as render, self.assertRaises(PermissionError):
                bridge.get_page(selected["id"], selected["page"], selected["widget_image_fetch_max_edge"])
            render.assert_not_called()


@unittest.skipUnless(os.environ.get("PAPERBRIDGE_BROWSER_TESTS") == "1", "Opt-in UI browser test")
class WidgetPageFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        try:
            cls.browser = cls.driver.firefox.launch(headless=True)
        except BaseException:
            cls.driver.stop()
            raise
        cls.html = Path(viewer.__file__).with_name("page_viewer.html").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def widget(self, scenario="success", *, invalid_manifest=None):
        pages = [dict(fixture(2), page=4), dict(fixture(1), page=2)]
        coverage = [{"selection_index": 0, "reference": pages[0]["id"], "page": 4,
                     "status": "image_prepared", "image_index": 0},
                    {"selection_index": 1, "reference": "10.1234/unavailable", "page": 1,
                     "status": "external_model_permission_required", "reason": "permission_required"},
                    {"selection_index": 2, "reference": pages[1]["id"], "page": 2,
                     "status": "image_prepared", "image_index": 1}]
        result = viewer.pages_view_result(dict(pages=pages, requested_count=3, selection_coverage=coverage),
                                          automatic=True, max_edge=1600)
        if invalid_manifest == "doi":
            result.structuredContent["pages"][0]["id"] = "10.1234/must-not-acquire"
        elif invalid_manifest == "budget":
            for item in result.structuredContent["pages"]:
                item["widget_image_byte_count"] = 4_000_001
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route("**/*", lambda route: route.abort())
        page.set_content('<iframe id="card"></iframe>')
        page.evaluate("""() => {
          window.messages = [];
          window.addEventListener('message', event => {
            const child = document.getElementById('card').contentWindow;
            if (event.source !== child || event.data?.jsonrpc !== '2.0') return;
            const message = event.data;
            const reply = result => child.postMessage({jsonrpc:'2.0', id:message.id, result}, '*');
            if (message.method === 'ui/initialize') reply({protocolVersion:'2026-01-26',
              hostInfo:{name:'synthetic',version:'1'},hostCapabilities:{message:{text:{},image:{}}}});
            if (message.method === 'ui/message') { window.messages.push(message); reply({}); }
          });
        }""")
        frame = page.frames[1]
        frame.evaluate("""({meta,png,scenario}) => {
          window.reads = []; window.uploads = []; window.states = []; window.order = [];
          window.activeReads = 0; window.maximumActiveReads = 0;
          const originalTimeout = window.setTimeout.bind(window);
          window.setTimeout = (fn,ms,...args) => originalTimeout(fn,ms === 45000 ? 500 : ms,...args);
          window.openai = {toolResponseMetadata:meta,
            callTool: async (name,args) => {
              window.reads.push({name,args}); window.order.push('read:' + args.page);
              window.maximumActiveReads = Math.max(window.maximumActiveReads, ++window.activeReads);
              try {
                const number = window.reads.length;
                const source = meta['paperbridge/page'].pages.find(item => item.id === args.reference && item.page === args.page);
                if (name !== 'papers_get_page' || !source || args.max_edge !== 1600)
                  throw new Error('Unexpected page request');
                if (number === 2 && ['hold','timeout'].includes(scenario))
                  await new Promise(resolve => { window.releaseRead = resolve; });
                if (number === 2 && scenario === 'reject') throw new Error('PRIVATE_SENTINEL');
                if (number === 2 && scenario === 'permission') return {content:[{type:'text',text:JSON.stringify({status:'external_model_permission_required'})}]};
                const info = {...source};
                const image = {type:'image',mimeType:'image/png',data:png};
                if (number === 2 && scenario === 'wrong_identity') info.id = 'pb:' + '3'.repeat(64) + ':0';
                if (number === 2 && scenario === 'wrong_size') image.data = btoa(atob(png) + 'x');
                if (number === 2 && scenario === 'wrong_mime') image.mimeType = 'image/webp';
                return {content:[{type:'text',text:JSON.stringify(info)}, image]};
              } finally { window.activeReads--; }
            },
            uploadFile: async file => {
              window.order.push('upload');
              window.uploads.push({name:file.name,type:file.type,bytes:Array.from(new Uint8Array(await file.arrayBuffer()))});
              return {fileId:'file_' + window.uploads.length};
            },
            setWidgetState: state => { window.openai.widgetState = state; window.states.push(state); }
          };
        }""", {"meta": result.meta, "png": base64.b64encode(PNG).decode(), "scenario": scenario})
        frame.set_content(self.html)
        return page, frame

    def test_ordered_native_reads_precede_exact_uploads_and_completed_remount_needs_no_read(self):
        page, frame = self.widget()
        page.wait_for_function("window.messages.length === 1")
        self.assertEqual(frame.evaluate("window.order"), ["read:4", "read:2", "upload", "upload"])
        self.assertEqual(frame.evaluate("window.maximumActiveReads"), 1)
        uploads = frame.evaluate("window.uploads")
        self.assertEqual([item["name"] for item in uploads],
                         ["paperbridge-selection-1-page-4.png", "paperbridge-selection-2-page-2.png"])
        self.assertTrue(all(item["type"] == "image/png" and bytes(item["bytes"]) == PNG for item in uploads))
        state = frame.evaluate("window.openai.widgetState")
        self.assertEqual(state["imageIds"], ["file_1", "file_2"])
        self.assertEqual(state["modelContent"].splitlines()[1:3],
                         ["1. https://example.org/paper/2; PDF page 4.", "2. https://example.org/paper/1; PDF page 2."])
        self.assertIn("2 of 3 requested pages prepared", state["modelContent"])
        self.assertIn("permission_required", state["modelContent"])
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(state))
        frame.evaluate("window.dispatchEvent(new Event('pagehide'))")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertEqual(frame.evaluate("window.reads.length"), 2)
        self.assertEqual(frame.evaluate("window.uploads"), uploads)
        self.assertEqual(page.evaluate("window.messages.length"), 1)

    def test_fetch_failure_never_uploads_or_retries_automatically(self):
        for scenario in ("reject", "permission", "wrong_identity", "wrong_size", "wrong_mime", "timeout"):
            with self.subTest(scenario=scenario):
                page, frame = self.widget(scenario)
                frame.wait_for_function("document.getElementById('status').textContent.includes('could not be loaded')")
                text = frame.locator("#status").inner_text()
                self.assertIn("https://example.org/paper/1 · PDF page 2", text)
                self.assertNotIn("PRIVATE_SENTINEL", text)
                self.assertEqual(frame.evaluate("window.uploads.length"), 0)
                self.assertEqual(page.evaluate("window.messages.length"), 0)
                frame.evaluate("window.dispatchEvent(new Event('openai:set_globals')); window.dispatchEvent(new Event('pagehide'))")
                frame.set_content(self.html)
                frame.get_by_role("button", name="Add pages to chat").wait_for()
                self.assertEqual(frame.evaluate("window.reads.length"), 2)
                self.assertEqual(frame.evaluate("window.uploads.length"), 0)
                self.assertEqual(page.evaluate("window.messages.length"), 0)

    def test_manual_continuation_after_remount_reloads_pixels_without_uploading_again(self):
        page, frame = self.widget()
        page.wait_for_function("window.messages.length === 1")
        first = page.evaluate("window.messages[0].params.content")
        self.assertEqual(first[1:], [dict(type="image", mimeType="image/png",
            data=base64.b64encode(PNG).decode())] * 2)
        self.assertIn("1. https://example.org/paper/2; PDF page 4.", first[0]["text"])
        self.assertIn("2. https://example.org/paper/1; PDF page 2.", first[0]["text"])
        uploads = frame.evaluate("window.uploads")
        frame.evaluate("window.dispatchEvent(new Event('pagehide'))")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertEqual(frame.evaluate("window.reads.length"), 2)
        self.assertEqual(page.evaluate("window.messages.length"), 1)
        frame.get_by_role("button", name="Continue with these pages").click()
        page.wait_for_function("window.messages.length === 2")
        self.assertEqual(page.evaluate("window.messages[1].params.content"), first)
        self.assertEqual(frame.evaluate("window.reads.map(item => item.args.page)"), [4, 2, 4, 2])
        self.assertEqual(frame.evaluate("window.uploads"), uploads)
        frame.evaluate("document.getElementById('continue').dispatchEvent(new Event('click')); "
                       "window.dispatchEvent(new Event('pagehide'))")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertTrue(frame.get_by_role("button", name="Continue with these pages").is_disabled())
        self.assertEqual(page.evaluate("window.messages.length"), 2)
        self.assertEqual(frame.evaluate("window.reads.length"), 4)

    def test_revoked_permission_on_remount_refuses_image_message_without_reupload(self):
        page, frame = self.widget()
        page.wait_for_function("window.messages.length === 1")
        frame.evaluate("""() => {
          const read = window.openai.callTool;
          window.openai.callTool = async (name,args) => {
            await read(name,args);
            return {content:[{type:'text',text:JSON.stringify({status:'external_model_permission_required'})}]};
          };
          window.dispatchEvent(new Event('pagehide'));
        }""")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        frame.get_by_role("button", name="Continue with these pages").click()
        frame.wait_for_function("document.getElementById('continuation-result').textContent.includes("
                                "'After-button: Selected image bytes could not be validated or loaded')")
        self.assertEqual(page.evaluate("window.messages.length"), 1)
        self.assertEqual(frame.evaluate("window.uploads.length"), 2)
        self.assertEqual(frame.evaluate("window.reads.length"), 3)
        frame.evaluate("window.dispatchEvent(new Event('openai:set_globals')); window.dispatchEvent(new Event('pagehide'))")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertTrue(frame.get_by_role("button", name="Continue with these pages").is_disabled())
        self.assertEqual(page.evaluate("window.messages.length"), 1)
        self.assertEqual(frame.evaluate("window.reads.length"), 3)

    def test_teardown_during_manual_pixel_reload_prevents_late_image_message(self):
        page, frame = self.widget()
        page.wait_for_function("window.messages.length === 1")
        frame.evaluate("""() => {
          const read = window.openai.callTool;
          window.openai.callTool = async (name,args) => {
            const result = await read(name,args);
            await new Promise(resolve => { window.releaseContinuationRead = resolve; });
            return result;
          };
          window.dispatchEvent(new Event('pagehide'));
        }""")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        frame.get_by_role("button", name="Continue with these pages").click()
        frame.wait_for_function("typeof window.releaseContinuationRead === 'function'")
        frame.evaluate("window.dispatchEvent(new Event('pagehide')); window.releaseContinuationRead()")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertEqual(page.evaluate("window.messages.length"), 1)
        self.assertEqual(frame.evaluate("window.uploads.length"), 2)
        self.assertEqual(frame.evaluate("window.reads.length"), 3)
        self.assertTrue(frame.get_by_role("button", name="Continue with these pages").is_disabled())

    def test_manual_retry_reuses_successful_fetch_and_does_not_start_a_followup(self):
        page, frame = self.widget("reject")
        frame.wait_for_function("document.getElementById('status').textContent.includes('could not be loaded')")
        frame.get_by_role("button", name="Add pages to chat").click()
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertEqual(frame.evaluate("window.reads.map(item => item.args.page)"), [4, 2, 2])
        self.assertEqual(frame.evaluate("window.uploads.length"), 2)
        self.assertEqual(page.evaluate("window.messages.length"), 0)

    def test_late_page_result_after_teardown_never_uploads_or_restarts(self):
        page, frame = self.widget("hold")
        frame.wait_for_function("typeof window.releaseRead === 'function'")
        frame.evaluate("window.dispatchEvent(new Event('pagehide')); window.releaseRead()")
        frame.wait_for_function("window.activeReads === 0")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Add pages to chat").wait_for()
        self.assertEqual(frame.evaluate("window.reads.length"), 2)
        self.assertEqual(frame.evaluate("window.uploads.length"), 0)
        self.assertEqual(page.evaluate("window.messages.length"), 0)

    def test_nonlocal_reference_and_overbudget_manifest_never_call_a_tool(self):
        for invalid in ("doi", "budget"):
            with self.subTest(invalid=invalid):
                page, frame = self.widget(invalid_manifest=invalid)
                self.assertTrue(frame.get_by_role("button", name="Add page to chat").is_disabled())
                self.assertEqual(frame.evaluate("window.reads.length"), 0)
                self.assertEqual(frame.evaluate("window.uploads.length"), 0)
                self.assertEqual(page.evaluate("window.messages.length"), 0)
