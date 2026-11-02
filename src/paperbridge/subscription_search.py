"""Bounded ProQuest discovery from ITU's public database directory.

Operator configuration: PAPERBRIDGE_LIBRARY_SEARCH=1 and an explicit absolute
state_dir. Credentials come from the server's OpenAthens credential source.
No document downloads, record-page visits, or entitlement grants occur here.
The existing runner selects Chromium for target-scoped native interception.

Public evidence checked on 2026-11-02 (without credentials):
* https://kutuphane.itu.edu.tr/arastirma/veritabanlari
* ResearchResources resolves Central through the directory's fixed public API.
  No browser traversal of the paginated directory or its widgets is needed.
* The listed Central link leads to a form#searchForm, textarea#searchTerm,
  and a#expandedSearch. Its /athenslogin link lists ITU's entity below.
* That institution link's SAML AuthnRequest names the exact ACS below.

Result rows use #lor_container .resultListContainer ul.resultItems > li.resultItem,
h3 a.previewTitle, .scholUnivAuthors .truncatedAuthor, .jnlArticle or .dissertpub,
and .snippet. Journal labels alone do not establish peer review. Optional DOI
fields are returned only when present. The older #searchResults container is
also supported. #noResults explicitly means no matches. Unrecognized markup
raises library_search_unsupported_page. Tests use synthetic text exclusively.
The first results HTML response is read through the guarded browser stream and
parsed before its scripts execute. Filters apply conservatively to that page.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlsplit
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from .browser import RetrievalUnavailable
from .openathens import OpenAthensCredentials, challenged, fill_login
from .research_resources import DIRECTORY_API_URL, ResearchResources


DIRECTORY = "https://kutuphane.itu.edu.tr/arastirma/veritabanlari"
CENTRAL = "https://search.proquest.com/central"
SP_HOST = "shibboleth-sp.prod.proquest.com"
ACS = f"https://{SP_HOST}/Shibboleth.sso/SAML2/POST"
ENTITY = "https://idp.itu.edu.tr/entity"
SP_TARGET = f"https://{SP_HOST}/ONE_SEARCH/PRODWWW"
LOGIN_HOST = "login.openathens.net"
PROQUEST_HOSTS = {"www.proquest.com", "search.proquest.com"}
MAX_RESULTS = 20
DAILY_REQUESTS = 30
REQUEST_INTERVAL = 15
CACHE_SECONDS = 600
CACHE_ENTRIES = 24
STATE_BYTES = 2_000_000
RESPONSE_BYTES = 2_000_000
STREAM_CHUNK_BYTES = 65_536
RESULT_ROOT = "#lor_container .resultListContainer ul.resultItems"
RESULT_WAIT = RESULT_ROOT + " > li.resultItem, #searchResults li.resultItem, #noResults"
_HOUSEKEEPING = frozenset({
    "/indexol.pagelayout.checksession:set-cleared-client-storage",
    "/index.homepagelayout.checksession:set-cleared-client-storage",
    "/results.pagelayout.checksession:set-cleared-client-storage",
    "/shibbolethlogin.pagelayout.checksession:set-cleared-client-storage",
    "/indexol.pagelayout.captureproxyhost:capturehost",
    "/index.homepagelayout.captureproxyhost:capturehost",
    "/results.pagelayout.captureproxyhost:capturehost",
    "/shibbolethlogin.pagelayout.captureproxyhost:capturehost",
})
_BROWSER_POLICY = ("sandbox allow-same-origin allow-scripts allow-forms; frame-src 'none'; "
                   "object-src 'none'; base-uri 'self'; connect-src "
                   "https://www.proquest.com https://search.proquest.com "
                   "https://login.openathens.net https://shibboleth-sp.prod.proquest.com "
                   "https://go.openathens.net")


def _reject(reason="library_search_route_rejected"):
    raise RetrievalUnavailable(reason)


def _url(value):
    """Reject browser URL repairs, ambiguous authorities, and escaped paths."""
    if not isinstance(value, str) or not value or len(value) > 32_768:
        _reject()
    if "\\" in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        _reject()
    try:
        p = urlsplit(value)
        host = p.hostname
        if (p.scheme != "https" or not host or not host.isascii()
                or p.netloc.lower() not in (host, host + ":443")
                or p.port not in (None, 443) or p.username is not None
                or "%" in p.path or "//" in p.path
                or any(x in (".", "..") for x in p.path.split("/"))):
            _reject()
        return p
    except ValueError:
        _reject()


def _tenant(url):
    p = _url(url)
    return p.hostname == LOGIN_HOST and p.path.startswith("/auth/itu.edu.tr/")


def _official_entry(url):
    p = _url(url)
    return (p.hostname == "go.openathens.net" and p.path == "/redirector/itu.edu.tr"
            and not p.fragment and parse_qs(p.query, keep_blank_values=True, strict_parsing=True)
            == {"url": [CENTRAL]})


def _institution_link(url):
    p = _url(url)
    q = parse_qs(p.query)
    return (p.hostname == SP_HOST and p.path == "/Shibboleth.sso/DS"
            and q == {"entityID": [ENTITY], "target": [SP_TARGET]} and not p.fragment)


def _check_authn_request(url):
    """Check the advertised assertion destination before any credential fill."""
    values = parse_qs(_url(url).query).get("SAMLRequest", [])
    if len(values) != 1:
        _reject()
    try:
        packed = base64.b64decode(values[0], validate=True)
        inflater = zlib.decompressobj(-15)
        xml = inflater.decompress(packed, 65_537)
        if len(xml) > 65_536 or not inflater.eof:
            _reject()
        root = ElementTree.fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        issuer = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer")
        if (root.tag != "{urn:oasis:names:tc:SAML:2.0:protocol}AuthnRequest"
                or root.get("AssertionConsumerServiceURL") != ACS
                or root.get("Destination") != url.split("?", 1)[0]
                or issuer is None or issuer.text != f"https://{SP_HOST}/shibboleth"):
            _reject()
    except (ValueError, zlib.error, ElementTree.ParseError, DefusedXmlException):
        _reject()


class _PausedRequest:
    """Adapt one Chromium request pause to the shared pre-send policy."""

    def __init__(self, session, event, page, main_frame_id):
        self.session, self.request_id = session, event["requestId"]
        raw = event["request"]
        frame = page.main_frame if event.get("frameId") == main_frame_id else SimpleNamespace(page=page)
        self.request = SimpleNamespace(url=raw["url"], method=raw["method"],
                                       post_data=raw.get("postData", ""), frame=frame,
                                       resource_type=event.get("resourceType", "other").lower())

    def continue_(self):
        self.session.send("Fetch.continueRequest", {"requestId": self.request_id})

    def abort(self):
        self.session.send("Fetch.failRequest", {"requestId": self.request_id,
                                               "errorReason": "BlockedByClient"})


class _Routes:
    """A pre-send guard, including redirects and automatic SAML submissions."""

    def __init__(self, page, reserve):
        self.page, self.reserve = page, reserve
        self.reason = None
        self.authn_checked = False
        self.login_active = False
        self.secrets = ()
        self.search_target = None
        self.search_sent = False
        self.pending = 0
        self.closing = False
        self.session = None
        self.main_frame_id = None
        self.navigation_requests = 0
        self.result_html = None
        self.capture_started = False
        self._stream_clock = time.monotonic

    def install(self, context):
        # Target-scoped CDP pauses every native redirect hop. Playwright route
        # continuation alone does not re-run its handler on every redirect.
        # No context/default-page routes or browser-wide target handlers change.
        try:
            self.session = context.new_cdp_session(self.page)
            self.main_frame_id = self.session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            self.session.on("Fetch.requestPaused", self.paused)
            self.session.send("Fetch.enable", {"patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
                {"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"},
            ]})
        except Exception:
            _reject("library_search_chromium_required")

    def paused(self, event):
        route = _PausedRequest(self.session, event, self.page, self.main_frame_id)
        if "responseStatusCode" not in event:
            raw = event["request"]
            try:
                p = _url(raw["url"])
                housekeeping = (p.hostname in PROQUEST_HOSTS and raw["method"] == "POST"
                                and event.get("resourceType") == "XHR"
                                and p.path.split(";jsessionid=", 1)[0] in _HOUSEKEEPING)
            except RetrievalUnavailable:
                housekeeping = False
            if housekeeping:
                # Never send these requests, including when CDP omits POST data.
                # A missing body cannot poison navigation for an aborted request.
                visible = unquote_plus(raw["url"] + "\n" + raw.get("postData", ""))
                if any(value and value in visible for value in self.secrets):
                    self.reason = "institution_login_destination_rejected"
                self._abort(route)
                return
            if raw.get("hasPostData") and "postData" not in raw:
                self.reason = "library_search_route_rejected"
                self._abort(route)
                return
            self.handle(route)
            return
        self.pending += 1
        try:
            if self.closing:
                self._abort(route)
                return
            self.check()
            headers = event.get("responseHeaders", [])
            content = {h["name"].lower(): h["value"].lower() for h in headers}
            if ("application/pdf" in content.get("content-type", "")
                    or "attachment" in content.get("content-disposition", "")):
                _reject("library_search_download_rejected")
            part = _url(event["request"]["url"])
            if (self.search_sent and event.get("resourceType") == "Document"
                    and event.get("frameId") == self.main_frame_id
                    and part.hostname in PROQUEST_HOSTS
                    and re.fullmatch(r"/results/[^/]+/1", part.path)
                    and event["responseStatusCode"] not in (301, 302, 303, 307, 308)):
                if self.capture_started:
                    self._abort(route)
                    return
                self.capture_started = True
                self.result_html = self._capture_response(event, content)
                return
            # An additional policy preserves all publisher policies. Sandboxing
            # blocks popups/downloads before page scripts can run. WebSockets
            # cannot provide a route around the HTTP credential checks.
            secured = [*headers, {"name": "Content-Security-Policy", "value": _BROWSER_POLICY}]
            self.session.send("Fetch.continueResponse", {
                "requestId": event["requestId"], "responseCode": event["responseStatusCode"],
                # HTTP/2 has no reason phrase. Sending an empty one is rejected
                # by Chromium when continuing an otherwise valid response.
                **({"responsePhrase": event["responseStatusText"]} if event.get("responseStatusText") else {}),
                "responseHeaders": secured,
            })
        except RetrievalUnavailable as exc:
            self.reason = self.reason or exc.reason
            self._abort(route)
        except (Exception, asyncio.CancelledError):
            self.reason = self.reason or "library_search_route_rejected"
            self._abort(route)
        finally:
            self.pending -= 1

    def _capture_response(self, event, headers):
        """Read one response with a cooperative deadline, then stop rendering."""
        handle = None
        try:
            try:
                if event["responseStatusCode"] != 200:
                    _reject("library_search_response_status_rejected")
                if headers.get("content-type", "").split(";", 1)[0].strip() != "text/html":
                    _reject("library_search_response_type_rejected")
                length = headers.get("content-length")
                if length and length.isdecimal() and int(length) > RESPONSE_BYTES:
                    _reject("library_search_response_size_limit")
                deadline = self._stream_clock() + 25
                handle = self.session.send("Fetch.takeResponseBodyAsStream", {
                    "requestId": event["requestId"]})["stream"]
                body = bytearray()
                while True:
                    self.check()
                    if self._stream_clock() >= deadline:
                        _reject("library_search_response_timeout")
                    chunk = self.session.send("IO.read", {"handle": handle, "size": STREAM_CHUNK_BYTES})
                    if self._stream_clock() >= deadline:
                        _reject("library_search_response_timeout")
                    raw = chunk["data"]
                    if not isinstance(raw, str) or len(raw) > STREAM_CHUNK_BYTES * 2:
                        _reject("library_search_response_size_limit")
                    data = base64.b64decode(raw, validate=True) if chunk.get("base64Encoded") else raw.encode("utf-8")
                    if len(data) > STREAM_CHUNK_BYTES or len(body) + len(data) > RESPONSE_BYTES:
                        _reject("library_search_response_size_limit")
                    body.extend(data)
                    if chunk.get("eof") is True:
                        if not body:
                            _reject("library_search_response_empty")
                        self.check()
                        return body.decode("utf-8", errors="replace")
                    if not data:
                        self.page.wait_for_timeout(25)
            finally:
                try:
                    if handle is not None:
                        self.session.send("IO.close", {"handle": handle})
                finally:
                    self.session.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "Aborted"})
        except RetrievalUnavailable:
            raise
        except (Exception, asyncio.CancelledError):
            _reject("library_search_response_capture_unavailable")

    def check(self):
        if self.reason:
            _reject(self.reason)

    def handle(self, route):
        self.pending += 1
        try:
            self._handle(route)
        except asyncio.CancelledError:
            self.reason = self.reason or "library_search_unavailable"
            self._abort(route)
        finally:
            self.pending -= 1

    @staticmethod
    def _abort(route):
        try:
            route.abort()
        except (Exception, asyncio.CancelledError):
            # A cancelled/closed browser request must not escape the callback.
            return

    def _handle(self, route):
        request = route.request
        try:
            request_page = request.frame.page
            if request_page != self.page:
                if request_page.opener() == self.page:
                    self._abort(route)
                else:
                    # Preserve other operations and their existing route handlers.
                    route.fallback()
                return
            if self.closing:
                self._abort(route)
                return
            if request.resource_type in {"image", "font", "media"}:
                # Discovery needs text and form controls, never these payloads.
                self._abort(route)
                return
            self.check()
            p = _url(request.url)
            if request.resource_type == "other" and p.path == "/favicon.ico":
                self._abort(route)
                return
            method = request.method.upper()
            body = request.post_data or ""
            if request.frame != self.page.main_frame:
                # Directory maps and publisher widgets are not part of discovery.
                # This also prevents popups from sending an initial request.
                self._abort(route)
                return
            if request.resource_type == "document":
                self.navigation_requests += 1
                if self.navigation_requests > 24:
                    _reject("library_search_redirect_limit")
            decoded = unquote_plus(request.url + "\n" + body)
            secret = any(value and value in decoded for value in self.secrets)
            if secret and not _tenant(request.url):
                _reject("institution_login_destination_rejected")
            if p.hostname in PROQUEST_HOSTS:
                pass
            elif p.hostname == "go.openathens.net":
                if not _official_entry(request.url):
                    _reject()
            elif p.hostname == LOGIN_HOST:
                if p.path.startswith("/saml/2/sso/itu.edu.tr/"):
                    if "SAMLRequest" in parse_qs(p.query):
                        _check_authn_request(request.url)
                        self.authn_checked = True
                elif not _tenant(request.url):
                    _reject()
            elif p.hostname == SP_HOST:
                if p.path not in ("/Shibboleth.sso/DS", "/Shibboleth.sso/SAML2/POST",
                                  "/ONE_SEARCH/PRODWWW", "/ONE_SEARCH/PRODWWW/"):
                    _reject()
                if p.path == "/Shibboleth.sso/DS" and not _institution_link(request.url):
                    _reject()
            else:
                _reject()
            # Never transmit raw credentials in URLs, even on the login host.
            if any(value and value in unquote_plus(request.url) for value in self.secrets):
                _reject("institution_login_destination_rejected")
            if (p.hostname in PROQUEST_HOSTS and method == "POST" and request.resource_type == "xhr"
                    and p.path.split(";jsessionid=", 1)[0] in _HOUSEKEEPING
                    and not secret):
                # Observed public-page housekeeping. It is unnecessary for
                # discovery, so leave it blocked without poisoning navigation.
                self._abort(route)
                return
            if method == "POST":
                fields = parse_qs(body, keep_blank_values=True)
                if _tenant(request.url) and self.login_active:
                    pass
                elif request.url == ACS and self.authn_checked and not secret:
                    if (not fields.get("SAMLResponse") or len(fields["SAMLResponse"]) != 1
                            or set(fields) - {"SAMLResponse", "RelayState"}):
                        _reject()
                elif self.search_target == (request.url, method) and not secret:
                    if self.search_sent:
                        _reject("library_search_request_limit")
                    self.reserve()
                    self.search_sent = True
                else:
                    _reject()
            elif method != "GET":
                _reject()
            if re.search(r"/(?:fulltextpdf|fulltext|download)(?:/|$)", p.path, re.I):
                _reject("library_search_download_rejected")
            # Native response streaming retains the browser's TLS/session stack.
            # CDP will pause the next redirect request independently, before send.
            route.continue_()
        except RetrievalUnavailable as exc:
            if request.resource_type not in {"image", "font", "stylesheet", "script"}:
                self.reason = self.reason or exc.reason
            self._abort(route)
        except Timeout:
            self.reason = "library_search_busy"
            self._abort(route)
        except Exception:
            self.reason = "library_search_route_rejected"
            self._abort(route)

    def close(self, context):
        """Drain guarded callbacks before closing their page and removing the guard."""
        self.closing = True
        deadline = time.monotonic() + 5
        try:
            while self.pending and time.monotonic() < deadline:
                self.page.wait_for_timeout(25)
        finally:
            try:
                self.page.close()
            finally:
                try:
                    if self.session is not None:
                        try:
                            self.session.detach()
                        except Exception:
                            # Closing the guarded target also detaches CDP.
                            self.session = None
                finally:
                    self.secrets = ()
                    self.result_html = None


def _text(node, maximum=1000):
    return " ".join(node.get_text(" ", strip=True).split())[:maximum] if node else None


def _results(soup, *, year_from, year_to, reviewed_only, limit):
    root = soup.select_one(RESULT_ROOT) or soup.select_one("#searchResults")
    if root is None:
        if soup.select_one("#noResults") is not None:
            return []
        _reject("library_search_unsupported_page")
    rows = root.select("li.resultItem")
    if not rows:
        _reject("library_search_unsupported_page")
    output, seen = [], set()
    for row in rows[:100]:
        link = row.select_one("h3 a.previewTitle[href]") or row.select_one("h2 a[href], h3 a[href]")
        if link is None or not _text(link):
            _reject("library_search_malformed_results")
        p = _url(urljoin("https://www.proquest.com/", link["href"]))
        record = re.search(r"(?:^|/)docview/([0-9]{1,24})(?:/|$)", p.path)
        if p.hostname not in PROQUEST_HOSTS or not record:
            _reject("library_search_malformed_results")
        record_id = record[1]
        if record_id in seen:
            continue
        seen.add(record_id)
        date = _text(row.select_one(".pubDate"), 120)
        citation = row.select_one(".jnlArticle, .dissertpub")
        citation_text = _text(citation, 1200) or ""
        if not date:
            citation_years = re.findall(r"(?<!\d)([12][0-9]{3})(?!\d)", citation_text)
            if len(set(citation_years)) == 1:
                dated = re.search(r"\(([^()]*" + citation_years[0] + r"[^()]*)\)", citation_text)
                date = dated[1][:120] if dated else citation_years[0]
        years = re.findall(r"(?<!\d)([12][0-9]{3})(?!\d)", date or "")
        year = int(years[0]) if len(set(years)) == 1 else None
        reviewed = _text(row.select_one(".peerReviewed"), 80)
        peer_reviewed = True if reviewed and reviewed.casefold() == "peer reviewed" else None
        if ((year_from is not None and (year is None or year < year_from))
                or (year_to is not None and (year is None or year > year_to))
                or (reviewed_only and peer_reviewed is not True)):
            continue
        doi_node = row.select_one('.doi, a[href^="https://doi.org/"], a[href^="https://dx.doi.org/"]')
        doi = doi_node.get("href") if doi_node and doi_node.name == "a" else _text(doi_node, 300)
        if doi:
            doi = re.sub(r"^(?:https://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
            if not re.fullmatch(r"10\.[0-9]{4,9}/[^\s<>]+", doi, flags=re.I):
                doi = None
        author_nodes = row.select(".scholUnivAuthors .truncatedAuthor") or row.select(".author")
        authors = [part.strip()[:200] for node in author_nodes[:30]
                   for part in (_text(node, 6000) or "").split(";") if part.strip()][:30]
        source = _text(row.select_one(".publicationTitle"), 500)
        if source is None and citation is not None:
            source = _text(citation.select_one("strong"), 500)
            if source is None:
                prefix = re.split(r"(?<!\d)[12][0-9]{3}(?!\d)", citation_text, maxsplit=1)[0]
                source = prefix.rstrip(" ,.")[:500] or None
        output.append({
            "title": _text(link, 1000),
            "authors": authors,
            "publication_date": date, "year": year,
            "source": source,
            "snippet": _text(row.select_one(".snippet"), 1500),
            "proquest_id": record_id,
            "url": f"https://www.proquest.com/docview/{record_id}",
            "doi": doi, "peer_reviewed": peer_reviewed,
            "metadata_only": True, "full_text_read": False,
            "institutional_entitlement": "not_verified",
        })
        if len(output) == limit:
            break
    return output


class SubscriptionSearch:
    """Server-owned discovery for papers_library_query, using the existing runner.

    state_dir is mandatory and must be absolute. Share it across processes so
    the UTC daily budget, 15-second request interval, and bounded ten-minute
    cache survive restarts. Failed submitted searches still consume budget.
    """

    def __init__(self, runner, *, state_dir: Path,
                 credentials=OpenAthensCredentials.from_env, clock=time.time, resources=None):
        self.runner, self.credentials, self.clock = runner, credentials, clock
        self.resources = resources
        try:
            self.state_dir = Path(state_dir)
            if not self.state_dir.is_absolute():
                _reject("library_search_configuration_required")
        except (TypeError, ValueError):
            _reject("library_search_configuration_required")

    def _resolve_entry(self):
        """Resolve a fixed resource query. Returned links never extend the allowlist."""
        try:
            if self.resources is None:
                self.resources = ResearchResources()
            result = self.resources.search("ProQuest Central", limit=100)
            if (result["source_url"] != DIRECTORY or result["metadata_url"] != DIRECTORY_API_URL
                    or result.get("truncated") is not False or not isinstance(result["resources"], list)):
                _reject()
            matches = [row for row in result["resources"] if isinstance(row, dict)
                       and row.get("label") in {"ProQuest Central", "ProQuest Central Premium"}]
            if len(matches) != 1 or matches[0].get("public_entry_url") != CENTRAL:
                _reject()
            entry = matches[0]["institutional_entry_url"]
            if not _official_entry(entry):
                _reject()
            return entry
        except Exception:
            _reject("library_search_official_entry_unavailable")

    def _now(self):
        value = float(self.clock())
        if not math.isfinite(value) or value < 0:
            _reject("library_search_clock_invalid")
        return value

    def _read_state(self):
        path = self.state_dir / "requests.json"
        if not path.exists():
            return {"version": 1, "day": "", "count": 0, "last": 0, "cache": {}}
        try:
            with path.open("rb") as stream:
                raw = stream.read(STATE_BYTES + 1)
            if len(raw) > STATE_BYTES:
                raise ValueError()
            state = json.loads(raw)
            if (state["version"] != 1 or type(state["count"]) is not int
                    or not 0 <= state["count"] <= DAILY_REQUESTS
                    or not isinstance(state["day"], str)
                    or type(state["last"]) not in (int, float)
                    or not math.isfinite(state["last"]) or state["last"] < 0
                    or not isinstance(state["cache"], dict)
                    or len(state["cache"]) > CACHE_ENTRIES):
                raise ValueError()
            return state
        except (OSError, ValueError, TypeError, KeyError):
            _reject("library_search_state_invalid")

    def _write_state(self, state):
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > STATE_BYTES:
            _reject("library_search_state_invalid")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.state_dir, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_dir / "requests.json")
        except OSError:
            _reject("library_search_state_unavailable")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _state_lock(self):
        return FileLock(str(self.state_dir / "requests.lock"), timeout=0)

    def _reserve(self):
        with self._state_lock():
            state, now = self._read_state(), self._now()
            if now < state["last"]:
                _reject("library_search_clock_invalid")
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            if state["day"] != day:
                state.update(day=day, count=0)
            if state["count"] >= DAILY_REQUESTS:
                _reject("library_search_daily_budget")
            if state["last"] and now - state["last"] < REQUEST_INTERVAL:
                _reject("library_search_interval")
            state.update(count=state["count"] + 1, last=now)
            self._write_state(state)

    def _cached(self, key):
        with self._state_lock():
            state, now = self._read_state(), self._now()
            if now < state["last"]:
                _reject("library_search_clock_invalid")
            item = state["cache"].get(key)
            if item and 0 <= now - item["at"] < CACHE_SECONDS:
                result = item["result"]
                if (not isinstance(result, dict) or result.get("metadata_only") is not True
                        or result.get("full_text_read") is not False
                        or result.get("institutional_entitlement") != "not_verified"
                        or not isinstance(result.get("results"), list)
                        or len(result["results"]) > MAX_RESULTS
                        or result.get("count") != len(result["results"])):
                    _reject("library_search_state_invalid")
                record_keys = {"title", "authors", "publication_date", "year", "source", "snippet",
                               "proquest_id", "url", "doi", "peer_reviewed", "metadata_only",
                               "full_text_read", "institutional_entitlement"}
                for record in result["results"]:
                    if (not isinstance(record, dict) or set(record) != record_keys
                            or record["metadata_only"] is not True or record["full_text_read"] is not False
                            or record["institutional_entitlement"] != "not_verified"
                            or not isinstance(record["proquest_id"], str)
                            or not re.fullmatch(r"[0-9]{1,24}", record["proquest_id"])
                            or record["url"] != "https://www.proquest.com/docview/" + record["proquest_id"]):
                        _reject("library_search_state_invalid")
                return {**result, "cached": True}

    def _save_cache(self, key, result):
        with self._state_lock():
            state, now = self._read_state(), self._now()
            cache = {k: v for k, v in state["cache"].items()
                     if 0 <= now - v["at"] < CACHE_SECONDS}
            cache[key] = {"at": now, "result": result}
            state["cache"] = dict(sorted(cache.items(), key=lambda kv: kv[1]["at"])[-CACHE_ENTRIES:])
            while len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > STATE_BYTES:
                state["cache"].pop(next(iter(state["cache"])))
            self._write_state(state)

    @staticmethod
    def _navigation_race(error):
        # Inspect locally, never return or log Playwright's URL-bearing message.
        text = str(error).casefold()
        return any(marker in text for marker in (
            "execution context was destroyed", "cannot find context with specified id",
            "unable to retrieve content because the page is navigating"))

    @staticmethod
    def _dom(page, guard):
        if guard.result_html is not None:
            return SubscriptionSearch._html(guard.result_html, guard)
        deadline = time.monotonic() + 3
        for attempt in range(8):
            guard.check()
            before = page.url
            _url(before)
            try:
                html = page.content()
                title = page.title()
            except BrowserError as error:
                guard.check()
                if not SubscriptionSearch._navigation_race(error):
                    raise
            else:
                guard.check()
                if before == page.url:
                    break
            if attempt == 7 or time.monotonic() >= deadline:
                _reject("library_search_navigation_timeout")
            page.wait_for_timeout(200)
        else:
            _reject("library_search_navigation_timeout")
        return SubscriptionSearch._html(html, guard, title=title)

    @staticmethod
    def _html(html, guard, *, title=None):
        guard.check()
        if not isinstance(html, str) or len(html.encode("utf-8")) > RESPONSE_BYTES:
            _reject("library_search_unsupported_page")
        soup = BeautifulSoup(html, "html.parser")
        if (challenged(title or _text(soup.title, 500))
                or soup.select_one('iframe[src*="captcha"], .g-recaptcha, input[name="otp"], '
                                   'input[autocomplete="one-time-code"]') is not None
                or re.search(r"verify you are human|multi.factor authentication|two.factor authentication",
                             _text(soup, 10_000) or "", re.I)):
            _reject("library_search_human_interaction_required")
        guard.check()
        return soup

    @staticmethod
    def _timeout_dom(page, guard):
        """Recover only a fully recognized, guarded DOM after a load timeout."""
        guard.check()
        try:
            soup = SubscriptionSearch._dom(page, guard)
        except RetrievalUnavailable:
            raise
        except Exception:
            guard.check()
            _reject("library_search_navigation_timeout")
        host = _url(page.url).hostname
        recognized = False
        if host in PROQUEST_HOSTS:
            forms = soup.select("form#searchForm")
            if len(forms) == 1:
                form = forms[0]
                recognized = (form.get("method", "").lower() == "post"
                              and form.select_one("textarea#searchTerm[name=searchTerm]") is not None
                              and form.select_one("a#expandedSearch") is not None
                              and bool(form.get("action"))
                              and _url(urljoin(page.url, form["action"])).hostname in PROQUEST_HOSTS)
            if not recognized and soup.select_one("form#wayfForm input#institutionName") is not None:
                links = [a["href"] for a in soup.select("a[href]")
                         if "idp.itu.edu.tr" in unquote(a["href"]) and _institution_link(a["href"])]
                recognized = len(set(links)) == 1
            if not recognized and guard.search_sent:
                # Use the same parser as successful searches, never a generic
                # container or title as evidence that results have arrived.
                _results(soup, year_from=None, year_to=None, reviewed_only=False, limit=1)
                recognized = True
        elif _tenant(page.url) and guard.authn_checked:
            passwords = soup.select('input[type="password"]')
            if len(passwords) == 1:
                form = passwords[0].find_parent("form")
                recognized = (form is not None and form.get("method", "").lower() == "post"
                              and _tenant(urljoin(page.url, form.get("action", "")))
                              and len(form.select('input[name="username"]')) == 1
                              and len(form.select('button[name="logIn"]')) == 1)
        guard.check()
        if not recognized:
            _reject("library_search_navigation_timeout")

    @staticmethod
    def _commit(page, guard, destination, timeout):
        previous = page.url
        try:
            page.goto(destination, wait_until="commit", timeout=timeout)
        except BrowserTimeout:
            guard.check()
            if page.url == previous and page.url != destination:
                _reject("library_search_navigation_timeout")
            SubscriptionSearch._timeout_dom(page, guard)

    @staticmethod
    def _follow(page, guard, *, deadline=None):
        guard.check()
        if guard.result_html is not None:
            return
        deadline = min(deadline, time.monotonic() + 8) if deadline is not None else time.monotonic() + 8
        for attempt in range(8):
            guard.check()
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                _reject("library_search_navigation_timeout")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=remaining)
            except BrowserTimeout:
                SubscriptionSearch._timeout_dom(page, guard)
            except BrowserError as error:
                guard.check()
                if guard.result_html is not None:
                    return
                if not SubscriptionSearch._navigation_race(error):
                    raise
                if attempt == 7:
                    _reject("library_search_navigation_timeout")
                page.wait_for_timeout(200)
                continue
            guard.check()
            return

    def _goto(self, page, guard, destination):
        deadline = time.monotonic() + 45
        self._commit(page, guard, destination, 20_000)
        self._follow(page, guard, deadline=deadline)

    @staticmethod
    def _wait(page, guard, selector):
        try:
            page.wait_for_selector(selector, timeout=10_000)
        except Exception:
            guard.check()
            _reject("library_search_unsupported_page")

    @staticmethod
    def _poll_results(page, guard, filters, *, clock=time.monotonic):
        """Pump native response callbacks without accessing the results renderer."""
        deadline = clock() + 45
        for _ in range(451):
            guard.check()
            if clock() >= deadline:
                break
            if guard.search_sent and guard.result_html is not None:
                soup = SubscriptionSearch._html(guard.result_html, guard)
                rows = _results(soup, **filters)
                guard.check()
                return rows
            guard.check()
            remaining = int((deadline - clock()) * 1000)
            if remaining <= 0:
                break
            page.wait_for_timeout(min(100, remaining))
        _reject("library_search_results_timeout")

    def _browse(self, context, query, filters):
        # Lock lifetime follows the browser operation even if runner.run times out.
        with FileLock(str(self.state_dir / "operation.lock"), timeout=0):
            key = filters.pop("cache_key")
            cached = self._cached(key)
            if cached is not None:
                return cached
            entry = self._resolve_entry()
            page = context.new_page()
            guard = _Routes(page, self._reserve)
            try:
                guard.install(context)
                self._goto(page, guard, entry)
                soup = self._dom(page, guard)
                # Use only links actually exposed by the reached publisher page.
                # A reused session may already expose the search form. This is
                # sufficient for metadata discovery, never proof of entitlement.
                guest_login = soup.select_one("a#createLoginOverlay.ol-login-link") is not None
                if soup.select_one("form#searchForm") is None or guest_login:
                    athens = [a["href"] for a in soup.select("a[href]")
                              if a["href"] == "https://www.proquest.com/athenslogin"]
                    if athens:
                        self._goto(page, guard, athens[0])
                        soup = self._dom(page, guard)
                    choices = []
                    for a in soup.select("a[href]"):
                        if "idp.itu.edu.tr" in unquote(a["href"]) and _institution_link(a["href"]):
                            choices.append(a["href"])
                    if len(set(choices)) != 1:
                        _reject("library_search_unsupported_page")
                    self._goto(page, guard, choices[0])
                submitted = False
                for _ in range(20):
                    soup = self._dom(page, guard)
                    if _tenant(page.url):
                        if submitted:
                            page.wait_for_timeout(500)
                            continue
                        if not guard.authn_checked:
                            _reject("institution_login_destination_rejected")
                        def credential_source():
                            guard.check()
                            value = self.credentials()
                            guard.secrets = (value.username, value.password)
                            guard.login_active = True
                            return value
                        submitted = True
                        fill_login(page, credential_source)
                        self._follow(page, guard)
                    elif _url(page.url).hostname in PROQUEST_HOSTS:
                        form = soup.select_one("form#searchForm")
                        if (form is not None and form.get("method", "").lower() == "post"
                                and form.select_one("textarea#searchTerm[name=searchTerm]") is not None
                                and form.select_one("a#expandedSearch") is not None):
                            break
                        if not submitted:
                            _reject("library_search_unsupported_page")
                    page.wait_for_timeout(500)
                else:
                    _reject("institution_login_rejected" if _tenant(page.url)
                            else "library_search_unsupported_page")
                guard.login_active = False
                soup = self._dom(page, guard)
                form = soup.select_one("form#searchForm")
                if (form is None or form.select_one("textarea#searchTerm[name=searchTerm]") is None
                        or form.select_one("a#expandedSearch") is None
                        or form.get("method", "").lower() != "post"):
                    _reject("library_search_unsupported_page")
                action = urljoin(page.url, form.get("action", ""))
                if _url(action).hostname not in PROQUEST_HOSTS or not form.get("action"):
                    _reject()
                guard.search_target = (action, "POST")
                page.locator("form#searchForm textarea#searchTerm").fill(query)
                page.locator("form#searchForm a#expandedSearch").click(timeout=15_000, no_wait_after=True)
                rows = self._poll_results(page, guard, filters)
                result = {"query": query, "results": rows,
                          "source": "proquest_browser_response", "entry_source": DIRECTORY,
                          "metadata_only": True, "full_text_read": False,
                          "institutional_entitlement": "not_verified", "cached": False,
                          "scope": "first_results_page", "filters": dict(filters)}
                result["count"] = len(result["results"])
                self._save_cache(key, result)
                return result
            except RetrievalUnavailable:
                raise
            except Exception:
                guard.check()
                _reject("library_search_unavailable")
            finally:
                guard.close(context)

    def search(self, query, *, year_from=None, year_to=None, reviewed_only=False, limit=10):
        """Return metadata from one results page, with no full-text assertion."""
        try:
            if os.getenv("PAPERBRIDGE_LIBRARY_SEARCH") != "1":
                _reject("library_search_disabled")
            if (not isinstance(query, str) or not query.strip() or len(query) > 500
                    or any(ord(c) < 32 or ord(c) == 127 for c in query)
                    or type(limit) is not int or not 1 <= limit <= MAX_RESULTS
                    or type(reviewed_only) is not bool):
                _reject("library_search_invalid_arguments")
            for year in (year_from, year_to):
                if year is not None and (type(year) is not int or not 1000 <= year <= 9999):
                    _reject("library_search_invalid_arguments")
            if year_from is not None and year_to is not None and year_from > year_to:
                _reject("library_search_invalid_arguments")
            query = query.strip()
            filters = dict(year_from=year_from, year_to=year_to, reviewed_only=reviewed_only, limit=limit)
            key = hashlib.sha256(json.dumps([query, filters], sort_keys=True).encode()).hexdigest()
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            cached = self._cached(key)
            if cached is not None:
                return cached
            return self.runner.run(lambda context: self._browse(context, query, {**filters, "cache_key": key}),
                                   use_publisher_proxy=False, browser_engine="chromium")
        except RetrievalUnavailable:
            raise
        except Timeout:
            _reject("library_search_busy")
        except Exception:
            # Browser and state errors can include URLs, CSRF tokens, or form data.
            _reject("library_search_unavailable")
