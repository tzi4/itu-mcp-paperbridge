"""A PDF preview with explicit MCP Apps image messages and widget compatibility."""
import base64
import io
import json
import os
from pathlib import Path
from uuid import uuid4

PAGE_VIEW_URI = "ui://paperbridge/page-viewer-v13.html"


def _encode_lossless_webp(data: bytes):
    """Encode one bounded, generated PNG without resizing or changing its pixels."""
    from PIL import Image, features
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != "PNG" or image.width * image.height > 6_000_000
                    or getattr(image, "n_frames", 1) != 1):
                raise ValueError("A single PNG page of at most six million pixels is required.")
            image.load()
            if not features.check("webp"):
                return None
            # RGBA preserves color values beneath fully transparent pixels too.
            raster = image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
            try:
                output = io.BytesIO()
                try:
                    raster.save(output, format="WEBP", lossless=True, exact=True,
                                quality=100, method=6)
                except OSError:
                    return None
                return output.getvalue()
            finally:
                if raster is not image:
                    raster.close()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("The PNG page could not be decoded within its limits.") from exc


def _widget_image(data: bytes):
    """Keep PNG by default; the opt-in affects only the widget attachment."""
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 8_000_000:
        raise ValueError("A bounded PNG page is required.")
    if os.environ.get("PAPERBRIDGE_WIDGET_LOSSLESS_WEBP") != "1":
        return data, "image/png", "disabled"
    encoded = _encode_lossless_webp(data)
    if encoded is None:
        return data, "image/png", "encoder_unavailable"
    if len(encoded) >= len(data):
        return data, "image/png", "not_smaller"
    return encoded, "image/webp", "lossless_webp"


def validate_page_view_options(automatic: bool, continuation_delay_seconds: int,
                               continuation_transport: str = "mcp_apps"):
    """Reject invalid continuation options before any document retrieval."""
    if type(automatic) is not bool:
        raise ValueError("automatic must be a boolean.")
    if type(continuation_delay_seconds) is not int or not 0 <= continuation_delay_seconds <= 60:
        raise ValueError("continuation_delay_seconds must be an integer from 0 to 60.")
    if continuation_delay_seconds and not automatic:
        raise ValueError("A continuation delay requires automatic=true.")
    if continuation_transport not in ("compatibility", "mcp_apps"):
        raise ValueError("continuation_transport must be compatibility or mcp_apps.")


def page_view_result(result: dict, automatic: bool = False, continuation_delay_seconds: int = 0,
                     continuation_transport: str = "mcp_apps"):
    """Wrap an already-authorized rendered page; never grant access in the UI."""
    from mcp.types import CallToolResult, TextContent
    validate_page_view_options(automatic, continuation_delay_seconds, continuation_transport)
    if "image_bytes" not in result:
        return result
    data, mime_type, encoding = _widget_image(result["image_bytes"])
    metadata = {key: value for key, value in result.items() if key != "image_bytes"}
    metadata.update(status="page_preview_ready", requires_user_action=None if automatic else True,
                    widget_image_mime_type=mime_type, widget_image_encoding=encoding,
                    widget_image_byte_count=len(data),
                    automatic=automatic, view_request_id=uuid4().hex,
                    automatic_delivery="requested_not_verified" if automatic else "not_requested",
                    attachment_method=("mcp_apps_image_content" if continuation_transport == "mcp_apps"
                                       else "chatgpt_widget_file"), model_visual_access="not_verified",
                    status_scope="initial_server_response_not_live_client_state",
                    continuation_check="manual_after_reply_available",
                    continuation_transport=continuation_transport,
                    continuation_acknowledgement="not_checked",
                    continuation_delay_seconds=continuation_delay_seconds,
                    continuation_delay_purpose="timing_diagnostic_not_a_reply_finished_signal",
                    next_action=("The preview will attempt attachment and one follow-up message after the configured delay. "
                                 "End the initial reply briefly so the later continuation can be tested. "
                                 "do not call this tool again to poll. If it cannot continue, use the preview's manual fallback."
                                 if automatic else
                                 "Use Add page to chat in the preview, then Continue with this page once after "
                                 "the current reply finishes. With MCP Apps, Continue sends the selected image "
                                 "block with the message. Add alone does not send that message. "
                                 "Actual visual access still depends on the client."))
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))],
        structuredContent=metadata,
        _meta={"paperbridge/image": {"mimeType": mime_type, "data": base64.b64encode(data).decode()},
               "paperbridge/page": metadata})


def pages_view_result(result: dict, automatic: bool = False, continuation_delay_seconds: int = 0,
                      *, max_edge: int = 2000):
    """Return a validated selection for sequential, permission-checked widget reads."""
    validate_page_view_options(automatic, continuation_delay_seconds, "mcp_apps")
    if type(max_edge) is not int or not 800 <= max_edge <= 2400:
        raise ValueError("Image edge must be an integer from 800 to 2400 pixels.")
    pages = result.get("pages", [])
    if not pages:
        return result
    if not isinstance(pages, list):
        raise ValueError("A list of prepared PDF pages is required.")
    if any(not isinstance(page, dict) or not isinstance(page.get("image_bytes"), bytes) or
           not page["image_bytes"].startswith(b"\x89PNG\r\n\x1a\n") for page in pages):
        raise ValueError("Every prepared page must contain a PNG image.")
    if sum(len(page["image_bytes"]) for page in pages) > 8_000_000:
        raise ValueError("The page selection exceeds the image byte limit.")
    # Reuse the established continuation metadata; no encoded image survives in this result.
    response = page_view_result(pages[0], automatic, continuation_delay_seconds, "mcp_apps")
    manifest = [dict({key: value for key, value in page.items() if key != "image_bytes"},
                     widget_image_mime_type="image/png", widget_image_encoding="native_png_widget_fetch",
                     widget_image_byte_count=len(page["image_bytes"]),
                     widget_image_fetch_max_edge=max_edge) for page in pages]
    metadata = response.structuredContent
    metadata.update({key: value for key, value in result.items() if key != "pages"})
    metadata.update(status="page_batch_preview_ready", pages=manifest,
                    prepared_count=len(pages),
                    widget_image_byte_count_total=sum(page["widget_image_byte_count"] for page in manifest),
                    next_action="Read the selection coverage. The preview will load the prepared local pages "
                    "through papers_get_page before attempting their attachment together. "
                    "With automatic=true end the initial reply briefly; the preview attempts one continuation. "
                    "For manual delivery, use Add pages to chat, then Continue with these pages once after "
                    "the current reply finishes. Continue sends the selected image blocks with the message. "
                    "Add alone does not send that message. "
                    "Do not poll or repeat this pending batch. After its delivery, request still-needed selections "
                    "deferred by the image-byte budget in a later batch. Do not automatically retry permission, "
                    "rendering or acquisition failures. Choose only pages needed for the answer. "
                    "Synthesize the original question only after actual visual access, "
                    "and report missing text or pages explicitly.")
    for key in ("widget_image_mime_type", "widget_image_encoding", "widget_image_byte_count"):
        metadata.pop(key, None)
    response.meta.clear()
    response.meta.update({"paperbridge/page-fetch": True, "paperbridge/page": metadata})
    response.content[0].text = json.dumps(metadata, ensure_ascii=False)
    return response


def register_page_view_resource(mcp):
    @mcp.resource(PAGE_VIEW_URI, mime_type="text/html;profile=mcp-app",
                  meta={"ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
                        "openai/widgetDescription": "An authorized PDF page preview with an Add page to chat button.",
                        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}})
    def page_view_ui():
        return Path(__file__).with_name("page_viewer.html").read_text(encoding="utf-8")
