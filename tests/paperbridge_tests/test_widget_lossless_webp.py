"""Original pixel fixtures and an isolated host exercise optional WebP attachments."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
import paperbridge.page_viewer as viewer


def drawing(mode="RGB", size=(96, 64)):
    image = Image.new(mode, size)
    image.putdata([((x * 17) % 256, (y * 23) % 256, ((x + y) * 13) % 256)
                   + ((0 if x % 3 == 0 else 128 if x % 3 == 1 else 255,) if mode == "RGBA" else ())
                   for y in range(size[1]) for x in range(size[0])])
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=0)
    image.close()
    return output.getvalue()


def prepared(data, index=1):
    return dict(image_bytes=data, id="pb:" + str(index) * 64 + ":0", title="Original color fixture",
                url="https://example.org/original-fixture", page=index, page_count=3,
                width=96, height=64)


class LosslessEncodingTests(unittest.TestCase):
    def test_opt_in_preserves_rgb_and_transparent_rgba_pixels_exactly(self):
        for mode in ("RGB", "RGBA"):
            with self.subTest(mode=mode), patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"):
                png = drawing(mode)
                encoded, mime, reason = viewer._widget_image(png)
                self.assertEqual((mime, reason), ("image/webp", "lossless_webp"))
                self.assertLess(len(encoded), len(png))
                with Image.open(io.BytesIO(png)) as original, Image.open(io.BytesIO(encoded)) as restored:
                    self.assertEqual(restored.format, "WEBP")
                    self.assertEqual(restored.size, original.size)
                    original_pixels = original.convert("RGBA").tobytes()
                    restored_pixels = restored.convert("RGBA").tobytes()
                    self.assertEqual(restored_pixels, original_pixels)
                    self.assertEqual(hashlib.sha256(restored_pixels).digest(), hashlib.sha256(original_pixels).digest())

    def test_default_and_nonliteral_opt_in_keep_original_png_without_decoder(self):
        png = drawing()
        for setting in (None, "", "0", "true", "yes", "01"):
            with self.subTest(setting=setting), patch.dict(os.environ), patch.object(viewer, "_encode_lossless_webp") as encode:
                os.environ.pop("PAPERBRIDGE_WIDGET_LOSSLESS_WEBP", None)
                if setting is not None:
                    os.environ["PAPERBRIDGE_WIDGET_LOSSLESS_WEBP"] = setting
                self.assertEqual(viewer._widget_image(png), (png, "image/png", "disabled"))
                encode.assert_not_called()

    def test_unavailable_encoder_and_larger_result_keep_original_bytes(self):
        png = drawing()
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"):
            with patch("PIL.features.check", return_value=False):
                self.assertEqual(viewer._widget_image(png), (png, "image/png", "encoder_unavailable"))
            for output in (png, png + b"larger"):
                with patch.object(viewer, "_encode_lossless_webp", return_value=output):
                    self.assertEqual(viewer._widget_image(png), (png, "image/png", "not_smaller"))

    def test_invalid_and_oversized_input_rejected_before_encoding(self):
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"), patch.object(viewer, "_encode_lossless_webp") as encode:
            for invalid in (b"RIFFxxxxWEBP", "PNG", b"\x89PNG\r\n\x1a\n" + b"x" * 8_000_000):
                with self.subTest(kind=type(invalid).__name__), self.assertRaises(ValueError):
                    viewer._widget_image(invalid)
            encode.assert_not_called()

    def test_pixel_limit_and_corrupt_png_rejected(self):
        output = io.BytesIO()
        Image.new("RGB", (3001, 2000)).save(output, format="PNG")
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"):
            for invalid in (output.getvalue(), b"\x89PNG\r\n\x1a\n"):
                with self.assertRaises(ValueError):
                    viewer._widget_image(invalid)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Requires the optional MCP dependency")
class WidgetWebpResultTests(unittest.TestCase):
    def test_native_png_unchanged_and_widget_binary_stays_out_of_model_text(self):
        from paperbridge.mcp_server import page_tool_result
        png = drawing("RGBA")
        page = prepared(png)
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"):
            native = page_tool_result(page)
            wrapped = viewer.page_view_result(page, automatic=True)
        self.assertEqual(native.content[1].mimeType, "image/png")
        self.assertEqual(base64.b64decode(native.content[1].data), png)
        self.assertEqual(page["image_bytes"], png)
        self.assertEqual(wrapped.structuredContent["widget_image_mime_type"], "image/webp")
        self.assertEqual(wrapped.structuredContent["model_visual_access"], "not_verified")
        image = wrapped.meta["paperbridge/image"]
        self.assertEqual(image["mimeType"], "image/webp")
        self.assertEqual(wrapped.structuredContent["widget_image_byte_count"], len(base64.b64decode(image["data"])))
        for model_text in (wrapped.content[0].text, json.dumps(wrapped.structuredContent)):
            self.assertNotIn(image["data"], model_text)
            self.assertNotIn(base64.b64encode(png).decode(), model_text)
            self.assertNotIn("image_bytes", model_text.replace("widget_image_byte_count", "byte_count"))

    def test_deferred_batch_reports_native_png_budget_even_when_inline_webp_is_enabled(self):
        png = drawing()
        actual = viewer._encode_lossless_webp(png)
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1"), patch.object(viewer, "_encode_lossless_webp", side_effect=[actual, None]):
            wrapped = viewer.pages_view_result(dict(pages=[prepared(png), prepared(png, 2)], requested_count=2))
        self.assertEqual([page["widget_image_mime_type"] for page in wrapped.structuredContent["pages"]], ["image/png", "image/png"])
        self.assertTrue(wrapped.meta["paperbridge/page-fetch"])
        self.assertNotIn("paperbridge/images", wrapped.meta)
        self.assertNotIn("paperbridge/image", wrapped.meta)
        self.assertEqual(wrapped.structuredContent["widget_image_byte_count_total"], 2 * len(png))
        self.assertNotIn("widget_image_mime_type", wrapped.structuredContent)
        with patch.object(viewer, "_encode_lossless_webp") as encode, self.assertRaises(ValueError):
            viewer.pages_view_result(dict(pages=[prepared(png + b"x" * 4_100_000)] * 2))
        encode.assert_not_called()


@unittest.skipUnless(os.environ.get("PAPERBRIDGE_BROWSER_TESTS") == "1", "Opt-in UI browser test")
class WidgetWebpBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        engine = os.environ.get("PAPERBRIDGE_TEST_BROWSER_ENGINE", "firefox")
        cls.browser = getattr(cls.driver, engine).launch(headless=True)
        cls.html = Path(viewer.__file__).with_name("page_viewer.html").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def widget(self, images, *, reject=False, fail_second=False, mutate=None):
        info = prepared(b"")
        info.pop("image_bytes")
        info.update(view_request_id="a" * 32, automatic=True, continuation_delay_seconds=0, continuation_transport="mcp_apps")
        meta = {"paperbridge/image": images[0], "paperbridge/page": info}
        if len(images) > 1:
            info.update(pages=[dict(info, page=i + 1) for i in range(len(images))], requested_count=len(images), selection_coverage=[])
            meta = {"paperbridge/images": images, "paperbridge/page": info}
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route("**/*", lambda route: route.abort())
        page.set_content('<iframe id="card"></iframe>')
        page.evaluate("""() => {
          window.messages = [];
          window.addEventListener('message', event => {
            const child = document.getElementById('card').contentWindow;
            if (event.source !== child || event.data?.jsonrpc !== '2.0') return;
            const msg = event.data;
            const reply = result => child.postMessage({jsonrpc:'2.0', id:msg.id, result}, '*');
            if (msg.method === 'ui/initialize') reply({protocolVersion:'2026-01-26', hostInfo:{name:'synthetic',version:'1'}, hostCapabilities:{message:{text:{},image:{}}}});
            if (msg.method === 'ui/message') { window.messages.push(msg); reply({}); }
          });
        }""")
        frame = page.frames[1]
        frame.evaluate("""({meta,reject,failSecond,mutate}) => {
          window.uploads = []; window.states = [];
          window.openai = {toolResponseMetadata:meta,
            uploadFile: async file => {
              const data = Array.from(new Uint8Array(await file.arrayBuffer()));
              window.uploads.push({name:file.name, type:file.type, data});
              if (reject || (failSecond && window.uploads.length === 2)) throw new Error('Synthetic unsupported file');
              const images = meta['paperbridge/images'] || [meta['paperbridge/image']];
              if (window.uploads.length === images.length) {
                if (mutate === 'invalid') images[0].data = 'not-base64';
                if (mutate === 'budget') for (const image of images)
                  image.data = btoa(atob(image.data) + 'x'.repeat(4000000));
              }
              return {fileId:'file_fixture_' + window.uploads.length};
            },
            setWidgetState: state => { window.openai.widgetState = state; window.states.push(state); }
          };
        }""", {"meta": meta, "reject": reject, "failSecond": fail_second, "mutate": mutate})
        frame.set_content(self.html)
        return page, frame

    def image(self, enabled):
        with patch.dict(os.environ, PAPERBRIDGE_WIDGET_LOSSLESS_WEBP="1" if enabled else "0"):
            data, mime, _ = viewer._widget_image(drawing("RGBA"))
        return {"mimeType": mime, "data": base64.b64encode(data).decode()}

    def test_mixed_formats_preview_file_bytes_and_remount_are_consistent(self):
        images = [self.image(True), self.image(False)]
        page, frame = self.widget(images)
        page.wait_for_function("window.messages.length === 1")
        frame.wait_for_function("Array.from(document.images).every(img => img.complete && img.naturalWidth === 96 && img.naturalHeight === 64)")
        uploads = frame.evaluate("window.uploads")
        self.assertEqual([row["name"] for row in uploads], ["paperbridge-selection-1-page-1.webp", "paperbridge-selection-2-page-2.png"])
        for row, image in zip(uploads, images):
            self.assertEqual(row["type"], image["mimeType"])
            self.assertEqual(bytes(row["data"]), base64.b64decode(image["data"]))
        state = frame.evaluate("window.openai.widgetState")
        self.assertIsInstance(state["modelContent"], str)
        self.assertEqual(state["privateContent"]["paperbridge"]["fileIds"], state["imageIds"])
        self.assertNotIn("file_fixture_", state["modelContent"])
        self.assertNotIn("file_mime_type", state["modelContent"])
        content = page.evaluate("window.messages[0].params.content")
        self.assertEqual(content[1:], [dict(type="image", **image) for image in images])
        for image in images:
            self.assertNotIn(image["data"], json.dumps(state))
            self.assertNotIn(image["data"], content[0]["text"])
        frame.set_content(self.html)
        frame.wait_for_timeout(100)
        self.assertEqual(frame.evaluate("window.uploads"), uploads)
        self.assertEqual(page.evaluate("window.messages.length"), 1)

    def test_partial_recovery_keeps_original_file_format_and_avoids_reupload(self):
        page, frame = self.widget([self.image(True), self.image(False)], fail_second=True)
        frame.wait_for_function("window.uploads.length === 2 && document.getElementById('status').textContent.includes('could not')")
        frame.set_content(self.html)
        frame.get_by_role("button", name="Add pages to chat").click()
        frame.get_by_role("button", name="Pages uploaded").wait_for()
        self.assertEqual([row["name"] for row in frame.evaluate("window.uploads")], ["paperbridge-selection-1-page-1.webp", "paperbridge-selection-2-page-2.png", "paperbridge-selection-2-page-2.png"])
        self.assertEqual(page.evaluate("window.messages.length"), 0)

    def test_unsupported_webp_does_not_retry_automatically_as_png(self):
        page, frame = self.widget([self.image(True)], reject=True)
        frame.wait_for_function("document.getElementById('status').textContent.includes('could not')")
        frame.evaluate("window.dispatchEvent(new Event('openai:set_globals'))")
        frame.wait_for_timeout(100)
        self.assertEqual([row["name"] for row in frame.evaluate("window.uploads")], ["paperbridge-page-1.webp"])
        self.assertEqual(page.evaluate("window.messages.length"), 0)
        self.assertEqual(frame.evaluate("window.openai.widgetState.imageIds"), [])

    def test_explicit_message_revalidates_image_bytes_and_aggregate_budget(self):
        for mode, count in (("invalid", 1), ("budget", 2)):
            with self.subTest(mode=mode):
                page, frame = self.widget([self.image(False) for _ in range(count)], mutate=mode)
                frame.wait_for_function("document.getElementById('continuation-result').textContent.includes("
                                        "'Automatic: Selected image bytes could not be validated or loaded')")
                self.assertEqual(page.evaluate("window.messages.length"), 0)
                self.assertEqual(frame.evaluate("window.uploads.length"), count)
                frame.evaluate("window.dispatchEvent(new Event('pagehide'))")
                frame.set_content(self.html)
                frame.wait_for_timeout(100)
                self.assertEqual(page.evaluate("window.messages.length"), 0)
                self.assertEqual(frame.evaluate("window.uploads.length"), count)

    def test_mismatched_mime_and_invalid_signatures_never_upload(self):
        webp, png = self.image(True), self.image(False)
        bad_size = bytearray(base64.b64decode(webp["data"]))
        bad_size[4] ^= 1
        invalid = [dict(webp, mimeType="image/png"), dict(png, mimeType="image/webp"),
                   dict(png, mimeType="image/gif"), dict(webp, data=base64.b64encode(bad_size).decode()),
                   dict(webp, data=base64.b64encode(b"not an image").decode())]
        for image in invalid:
            with self.subTest(mime=image["mimeType"]):
                page, frame = self.widget([image])
                frame.wait_for_timeout(75)
                self.assertTrue(frame.locator("#attach").is_disabled())
                self.assertEqual(frame.evaluate("window.uploads"), [])
                self.assertEqual(page.evaluate("window.messages.length"), 0)
