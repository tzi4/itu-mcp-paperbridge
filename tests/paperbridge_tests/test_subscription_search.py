"""Offline browser contract tests. No live browser, accounts, or sessions used.

Result HTML below documents the deliberately limited supported DOM contract.
It is synthetic. Public search-form and directory markup was inspected, while
authenticated ProQuest result markup still requires independent live validation.
"""
from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from paperbridge.browser import RetrievalUnavailable
from paperbridge.openathens import OpenAthensCredentials
from paperbridge.research_resources import DIRECTORY_API_URL
from paperbridge.subscription_search import (
    ACS, CACHE_ENTRIES, CACHE_SECONDS, CENTRAL, DAILY_REQUESTS, DIRECTORY,
    ENTITY, MAX_RESULTS, REQUEST_INTERVAL, SP_HOST, SP_TARGET,
    SubscriptionSearch, _Routes, _check_authn_request, _results, _url,
)


LOGIN = "https://login.openathens.net/auth/itu.edu.tr/o/82346286"
DISCOVERY = f"https://{SP_HOST}/Shibboleth.sso/DS?" + urlencode({"entityID": ENTITY, "target": SP_TARGET})
ENTRY = "https://go.openathens.net/redirector/itu.edu.tr?url=" + CENTRAL
ACTION = "https://www.proquest.com/indexol.sourcetypessearch.expandedbasicsearchbox.searchform"
SEARCH_FORM = f'''<form id="searchForm" action="{ACTION}" method="post">
<textarea id="searchTerm" name="searchTerm"></textarea>
<a id="expandedSearch" role="button" href="#">Search</a></form>'''
LOGIN_HTML = f'''<form action="{LOGIN}" method="post">
<input name="username"><input type="password" name="password">
<button name="logIn">Sign in</button></form>'''


def authn(acs=ACS, *, xml=None):
    destination = "https://login.openathens.net/saml/2/sso/itu.edu.tr/o/82346286/c/oafed"
    xml = xml or (f'<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
           f'AssertionConsumerServiceURL="{acs}" Destination="{destination}">'
           f'<saml:Issuer xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
           f'https://{SP_HOST}/shibboleth</saml:Issuer></samlp:AuthnRequest>')
    compressor = zlib.compressobj(wbits=-15)
    packed = compressor.compress(xml.encode()) + compressor.flush()
    return destination + "?" + urlencode({"SAMLRequest": base64.b64encode(packed).decode()})


def row(record="123456", *, date="2 Nov 2025", reviewed=True, doi="10.1234/actual.5"):
    return f'''<li class="resultItem"><h3><a href="/central/docview/{record}?accountid=11638">
    A research title</a></h3><span class="author">Ada Example</span>
    <span class="author">Bo Example</span><span class="pubDate">{date}</span>
    <span class="publicationTitle">Research Journal</span><div class="snippet">A short summary.</div>
    {('<span class="peerReviewed">Peer reviewed</span>' if reviewed else '')}
    {('<span class="doi">' + doi + '</span>' if doi else '')}
    <a href="/fulltextpdf/{record}">Full text PDF</a></li>'''


RESULTS = '<ul id="searchResults">' + row() + '</ul>'

# Synthetic text in the observed subscriber DOM shape, never captured content.
OBSERVED_RESULTS = '''<div id="lor_container"><div class="resultListContainer"><ul class="resultItems">
<li class="resultItem ltr"><a class="previewTitle" href="/docview/700001"></a>
<h3><a class="previewTitle Topicsresult" href="/docview/700001">Synthetic journal entry</a></h3>
<span class="scholUnivAuthors"><span class="truncatedAuthor">Example, Ada; Example, Bo</span></span>
<span class="jnlArticle"><span><strong>Example Journal</strong><strong>; Example City</strong>
Vol. 9, Iss. 3, (May 2023): article-42.</span></span>
<div class="source-type-label">Scholarly Journal</div><span class="snippet">Synthetic journal snippet.</span>
<span>Full Text</span><a href="/docview/700001">Full text - PDF</a></li>
<li class="resultItem ltr"><h3><a class="previewTitle" href="/docview/700002">Synthetic thesis entry</a></h3>
<span class="scholUnivAuthors"><span class="truncatedAuthor">Sample, Casey.</span></span>
<span class="dissertpub"><span>Example University ProQuest Dissertations &amp; Theses, 2022. 246810.</span></span>
<span class="snippet">Synthetic thesis snippet.</span></li></ul></div></div>'''


class Clock:
    def __init__(self, value=1_793_577_600):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds=REQUEST_INTERVAL):
        self.value += seconds


class FakeRoute:
    def __init__(self, page, url, method="GET", body=None, kind="document"):
        self.request = SimpleNamespace(url=url, method=method, post_data=body,
                                       resource_type=kind, frame=page.main_frame,
                                       frame_context=page.context)
        self.sent = False
        self.aborted = False
        self.passed_through = False

    def continue_(self):
        self.sent = True

    def fetch(self, **kwargs):
        raise AssertionError("Discovery must use native navigation, never API response buffering")

    def fulfill(self, **kwargs):
        pass

    def abort(self):
        self.aborted = True

    def fallback(self):
        self.passed_through = True


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def count(self):
        selector = self.selector.replace(":visible", "")
        return len(BeautifulSoup(self.page.html, "html.parser").select(selector))

    def fill(self, value):
        self.page.fills[self.selector] = value

    def click(self, **kwargs):
        p = self.page
        if 'button[name="logIn"]' == self.selector:
            body = urlencode({"username": p.fills['input[name="username"]:visible'],
                              "password": p.fills['input[name="password"]:visible']})
            p.request(p.context.login_target, "POST", body)
            for url, method, payload in p.context.after_login:
                p.request(url, method, payload)
            if p.context.repeat_login:
                p.url, p.html = LOGIN, LOGIN_HTML
            else:
                p.url, p.html = "https://www.proquest.com/central", SEARCH_FORM
        elif "expandedSearch" in self.selector:
            p.request(ACTION, "POST", urlencode({"searchTerm": p.fills[self.selector.replace(
                "a#expandedSearch", "textarea#searchTerm")]}))
            if p.context.duplicate_search:
                p.request(ACTION, "POST", "searchTerm=again")
            p.url = "https://www.proquest.com/results/fixture"
            p.html = p.context.results
        else:
            raise AssertionError(self.selector)


class FakePage:
    def __init__(self, context):
        self.context = context
        self.main_frame = SimpleNamespace(page=self)
        self.url, self.html = "about:blank", ""
        self.fills = {}
        self.closed = False

    def request(self, url, method="GET", body=None, kind="document"):
        route = FakeRoute(self, url, method, body, kind)
        route.request.frame_context = self.context
        self.context.handler(route)
        self.context.requests.append(route)
        if route.aborted and kind == "document":
            # Playwright reports a navigation error after a route abort.
            raise RuntimeError("Navigation failed at https://private.invalid/?secret=private")
        return route

    def goto(self, url, **kwargs):
        self.request(url.split("#")[0])
        self.url = url
        if url.startswith(DIRECTORY):
            raise AssertionError("The browser must not visit the paginated directory")
        elif url.startswith("https://go.openathens.net/"):
            self.request("https://search.proquest.com/central")
            self.request("https://www.proquest.com/central")
            self.url = "https://www.proquest.com/central"
            self.html = SEARCH_FORM if self.context.direct_form else (
                '<a href="https://www.proquest.com/athenslogin">OpenAthens</a>')
            if self.context.guest_form:
                self.html += SEARCH_FORM + '<a id="createLoginOverlay" class="ol-login-link">Log in through your library</a>'
        elif url == "https://www.proquest.com/athenslogin":
            self.html = f'<a href="{DISCOVERY}">Istanbul Technical University</a>'
        elif url == DISCOVERY:
            self.request(self.context.saml_request)
            self.request(self.context.login_url)
            self.url, self.html = self.context.login_url, LOGIN_HTML
        else:
            raise AssertionError(url)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def evaluate(self, script):
        # The real, unmodified fill_login calls this to inspect the form target.
        assert "input[type=password]" in script
        return [self.context.login_target, "post"]

    def wait_for_selector(self, selector, **kwargs):
        if not BeautifulSoup(self.html, "html.parser").select(selector):
            raise RuntimeError("No selector at private URL")

    def wait_for_timeout(self, delay):
        pass

    def wait_for_load_state(self, state, **kwargs):
        pass

    def content(self):
        return self.html

    def title(self):
        return self.context.title

    def close(self):
        self.closed = True
        self.context.handler = None

    def opener(self):
        return None


class FakeContext:
    def __init__(self):
        self.requests, self.pages = [], []
        self.handler = None
        self.responses = {}
        self.entry = ENTRY
        self.saml_request = authn()
        self.login_url = self.login_target = LOGIN
        self.after_login = [(ACS, "POST", "SAMLResponse=assertion&RelayState=relay")]
        self.results, self.title = RESULTS, "ProQuest"
        self.repeat_login = self.duplicate_search = False
        self.direct_form = False
        self.guest_form = False

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler

    def unroute(self, pattern, handler):
        assert self.handler == handler
        self.handler = None

    def new_cdp_session(self, page):
        self.session = FakeCDP(self, page)
        return self.session


class FakeCDP:
    def __init__(self, context, page):
        self.context, self.page = context, page
        self.callback = None
        self.requests = {}
        self.commands = []

    def on(self, event, callback):
        assert event == "Fetch.requestPaused"
        self.callback = callback

    def dispatch(self, route):
        request_id = str(len(self.requests))
        self.requests[request_id] = route
        request = route.request
        self.callback({"requestId": request_id, "frameId": "main-frame", "resourceType": request.resource_type.title(),
                       "request": {"url": request.url, "method": request.method,
                                   "hasPostData": request.post_data is not None,
                                   **({"postData": request.post_data} if request.post_data is not None else {})}})

    def send(self, method, arguments=None):
        self.commands.append((method, arguments))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main-frame"}}}
        if method == "Fetch.enable":
            self.context.handler = self.dispatch
        elif method == "Fetch.continueRequest":
            self.requests[arguments["requestId"]].continue_()
        elif method == "Fetch.failRequest":
            self.requests[arguments["requestId"]].abort()

    def detach(self):
        self.context.handler = None


class FakeRunner:
    def __init__(self, context=None):
        self.context = context or FakeContext()
        self.calls = 0

    def run(self, operation, *, use_publisher_proxy, browser_engine):
        assert use_publisher_proxy is False
        assert browser_engine == "chromium"
        self.calls += 1
        return operation(self.context)


class FakeResources:
    def __init__(self, context):
        self.context = context
        self.calls = []
        self.override = None

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        if self.override is not None:
            return self.override
        return {"source_url": DIRECTORY, "metadata_url": DIRECTORY_API_URL,
                "truncated": False, "resources": [
                    {"label": "ProQuest Central Premium", "public_entry_url": CENTRAL,
                     "institutional_entry_url": self.context.entry}]}


def reserve_in_process(directory, now, output):
    try:
        search = SubscriptionSearch(None, state_dir=Path(directory), clock=lambda: now)
        search._reserve()
        output.put("reserved")
    except RetrievalUnavailable as exc:
        output.put(exc.reason)
    except Timeout:
        output.put("library_search_busy")
    except Exception as exc:
        output.put(type(exc).__name__)


class SubscriptionSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"PAPERBRIDGE_LIBRARY_SEARCH": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = Clock()
        self.runner = FakeRunner()
        self.resources = FakeResources(self.runner.context)
        self.credential_calls = 0

    def credentials(self):
        self.credential_calls += 1
        return OpenAthensCredentials("test-user-17", "private+ pass!word")

    def searcher(self, runner=None):
        return SubscriptionSearch(runner or self.runner, state_dir=self.directory,
                                  credentials=self.credentials, clock=self.clock, resources=self.resources)

    def reason(self, expected, operation):
        with self.assertRaises(RetrievalUnavailable) as caught:
            operation()
        self.assertEqual(caught.exception.reason, expected)
        self.assertNotIn("https://", str(caught.exception))

    def state(self):
        return json.loads((self.directory / "requests.json").read_text())

    def test_metadata_truthful_no_downloads_or_record_visits(self):
        result = self.searcher().search("solar cells")
        paper = result["results"][0]
        self.assertEqual(paper["proquest_id"], "123456")
        self.assertEqual(paper["url"], "https://www.proquest.com/docview/123456")
        self.assertEqual(paper["doi"], "10.1234/actual.5")
        self.assertEqual(paper["authors"], ["Ada Example", "Bo Example"])
        self.assertEqual(paper["year"], 2025)
        self.assertEqual(paper["source"], "Research Journal")
        self.assertEqual(paper["snippet"], "A short summary.")
        for item in (result, paper):
            self.assertIs(item["full_text_read"], False)
            self.assertIs(item["metadata_only"], True)
            self.assertEqual(item["institutional_entitlement"], "not_verified")
        self.assertEqual(result["scope"], "first_results_page")
        self.assertEqual(self.state()["count"], 1)
        self.assertEqual(self.credential_calls, 1)
        self.assertTrue(self.runner.context.pages[0].closed)
        self.assertIsNone(self.runner.context.handler)
        urls = [r.request.url for r in self.runner.context.requests if r.sent]
        self.assertEqual(urls[0], ENTRY)
        self.assertFalse(any(url.startswith(DIRECTORY) for url in urls))
        self.assertEqual(self.resources.calls, [("ProQuest Central", 100)])
        self.assertEqual(result["entry_source"], DIRECTORY)
        self.assertFalse(any("docview/" in u or "fulltext" in u for u in urls))
        raw = (self.directory / "requests.json").read_text()
        for private in ("private+ pass!word", "test-user-17", "SAMLResponse", "assertion"):
            self.assertNotIn(private, raw)

    def test_observed_results_ancestry_completes_browser_operation(self):
        self.runner.context.results = OBSERVED_RESULTS
        result = self.searcher().search("synthetic observed result shape")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["proquest_id"], "700001")
        self.assertEqual(self.state()["count"], 1)
        self.assertFalse(result["full_text_read"])

    def test_reused_session_does_not_repeat_institution_selection(self):
        self.runner.context.direct_form = True
        result = self.searcher().search("already signed in")
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.credential_calls, 0)
        self.assertNotIn(DISCOVERY, [r.request.url for r in self.runner.context.requests])
        self.assertEqual(result["institutional_entitlement"], "not_verified")

    def test_public_guest_form_still_uses_publisher_institutional_login(self):
        self.runner.context.guest_form = True
        result = self.searcher().search("guest institutional route")
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.credential_calls, 1)
        self.assertIn(DISCOVERY, [r.request.url for r in self.runner.context.requests])

    def test_load_timeout_with_complete_search_and_result_dom_can_finish(self):
        self.runner.context.direct_form = True
        with patch.object(FakePage, "wait_for_load_state", side_effect=BrowserTimeout("private URL")):
            result = self.searcher().search("metadata during asset timeout")
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["full_text_read"])
        self.assertEqual(self.credential_calls, 0)
        self.assertEqual(self.state()["count"], 1)
        self.assertIsNone(self.runner.context.handler)

    def test_sso_dom_race_waits_for_search_form_without_repeating_credentials(self):
        original = FakePage.content
        reads = []
        def content(page):
            if 'input[name="password"]:visible' in page.fills and len(reads) < 2:
                reads.append(True)
                if len(reads) == 1:
                    raise BrowserError("Execution context was destroyed, most likely because of a navigation")
                return '<title>SSO handoff loading</title>'
            return original(page)
        with patch.object(FakePage, "content", content):
            result = self.searcher().search("wait for recognized search form")
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.credential_calls, 1)
        self.assertEqual(self.state()["count"], 1)

    def test_exact_opt_in_every_call_including_cache(self):
        searcher = self.searcher()
        searcher.search("first")
        for value in ("", "true", "0", "yes"):
            with patch.dict(os.environ, {"PAPERBRIDGE_LIBRARY_SEARCH": value}):
                self.reason("library_search_disabled", lambda: searcher.search("first"))
        self.assertEqual(self.runner.calls, 1)

    def test_absolute_state_path_required(self):
        for value in (None, "relative/state"):
            self.reason("library_search_configuration_required", lambda: SubscriptionSearch(
                self.runner, state_dir=value))

    def test_invalid_arguments_do_not_start_browser(self):
        bad = [{"query": ""}, {"query": "a\nb"}, {"query": "a" * 501},
               {"query": None}, {"query": "a", "limit": True},
               {"query": "a", "limit": MAX_RESULTS + 1}, {"query": "a", "limit": 0},
               {"query": "a", "year_from": "2020"}, {"query": "a", "year_to": True},
               {"query": "a", "year_from": 2025, "year_to": 2024},
               {"query": "a", "reviewed_only": 1}]
        for arguments in bad:
            with self.subTest(arguments=arguments):
                self.reason("library_search_invalid_arguments", lambda: self.searcher().search(**arguments))
        self.assertEqual(self.runner.calls, 0)

    def test_cache_survives_new_instance_and_is_detached(self):
        first = self.searcher().search("topic")
        first["results"][0]["title"] = "caller modified"
        other = FakeRunner()
        cached = self.searcher(other).search(" topic ")
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["results"][0]["title"], "A research title")
        self.assertEqual(other.calls, 0)
        self.assertEqual(self.state()["count"], 1)
        self.assertEqual(self.credential_calls, 1)

    def test_cache_key_includes_all_filters_and_expiry(self):
        for kwargs in ({}, {"year_from": 2025}, {"year_to": 2025},
                       {"reviewed_only": True}, {"limit": 1}):
            result = self.searcher().search("same", **kwargs)
            self.assertFalse(result["cached"])
            self.clock.advance()
        self.assertEqual(self.runner.calls, 5)
        self.clock.advance(CACHE_SECONDS)
        self.assertFalse(self.searcher().search("same")["cached"])

    def test_bounded_cache_eviction(self):
        for i in range(CACHE_ENTRIES + 1):
            self.searcher().search(f"topic {i}")
            self.clock.advance()
        self.assertEqual(len(self.state()["cache"]), CACHE_ENTRIES)
        self.assertFalse(self.searcher().search("topic 0")["cached"])

    def test_interval_and_daily_budget_survive_restart(self):
        self.searcher().search("one")
        self.reason("library_search_interval", lambda: self.searcher(FakeRunner()).search("two"))
        self.clock.advance()
        self.searcher(FakeRunner()).search("two")
        for _ in range(DAILY_REQUESTS - 2):
            self.clock.advance()
            self.searcher()._reserve()
        self.clock.advance()
        self.reason("library_search_daily_budget", lambda: self.searcher().search("three"))
        self.assertTrue(self.searcher().search("one")["cached"])
        self.clock.advance(86_400)
        self.searcher().search("three")
        self.assertEqual(self.state()["count"], 1)

    def test_actual_submission_failure_charged_but_login_failure_not_charged(self):
        self.runner.context.results = "<html>Unrecognized results layout</html>"
        self.reason("library_search_unsupported_page", lambda: self.searcher().search("topic"))
        self.assertEqual(self.state()["count"], 1)
        self.assertEqual(self.state()["cache"], {})
        self.clock.advance()
        self.runner.context.repeat_login = True
        self.reason("institution_login_rejected", lambda: self.searcher().search("other"))
        self.assertEqual(self.state()["count"], 1)

    def test_duplicate_search_request_cannot_escape_budget(self):
        self.runner.context.duplicate_search = True
        self.reason("library_search_request_limit", lambda: self.searcher().search("topic"))
        self.assertEqual(self.state()["count"], 1)
        self.assertTrue(self.runner.context.requests[-1].aborted)

    def test_clock_rollback_and_corrupt_state_fail_closed(self):
        self.searcher().search("topic")
        self.clock.advance(-1)
        self.reason("library_search_clock_invalid", lambda: self.searcher().search("topic"))
        (self.directory / "requests.json").write_text('{"version": 1}')
        self.reason("library_search_state_invalid", lambda: self.searcher().search("other"))

    def test_atomic_write_failure_preserves_budget_state(self):
        self.searcher().search("topic")
        before = (self.directory / "requests.json").read_bytes()
        self.clock.advance()
        with patch("paperbridge.subscription_search.os.replace", side_effect=OSError("private path")):
            self.reason("library_search_state_unavailable", lambda: self.searcher().search("different"))
        self.assertEqual((self.directory / "requests.json").read_bytes(), before)
        remaining = {p.name for p in self.directory.iterdir()}
        self.assertIn("requests.json", remaining)
        self.assertLessEqual(remaining, {"requests.json", "requests.lock", "operation.lock"})

    def test_filelock_blocks_other_process_reservation(self):
        self.searcher().search("topic")
        ctx = multiprocessing.get_context("spawn")
        output = ctx.Queue()
        process = ctx.Process(target=reserve_in_process, args=(str(self.directory),
                                  self.clock.value + REQUEST_INTERVAL, output))
        process.start()
        process.join(15)
        self.assertFalse(process.is_alive())
        self.assertEqual(output.get(timeout=2), "reserved")
        self.assertEqual(self.state()["count"], 2)
        with FileLock(str(self.directory / "requests.lock"), timeout=0):
            self.reason("library_search_busy", lambda: self.searcher().search("different"))
            process = ctx.Process(target=reserve_in_process, args=(str(self.directory),
                                      self.clock.value + REQUEST_INTERVAL, output))
            process.start()
            process.join(15)
            self.assertFalse(process.is_alive())
            self.assertEqual(output.get(timeout=2), "library_search_busy")
        self.assertEqual(self.state()["count"], 2)

    def test_human_checks_stop_before_credentials(self):
        for title in ("Just a moment...", "Verify you are human", "Security Check"):
            self.runner.context.title = title
            self.reason("library_search_human_interaction_required", lambda: self.searcher().search("topic"))
        self.assertEqual(self.credential_calls, 0)
        self.assertFalse((self.directory / "requests.json").exists())

    def test_unverified_official_entry_and_wrong_login_tenant(self):
        self.runner.context.entry = ENTRY.replace("search.proquest.com", "evil.invalid")
        self.reason("library_search_official_entry_unavailable", lambda: self.searcher().search("topic"))
        self.runner.context.entry = ENTRY
        self.runner.context.login_url = LOGIN.replace("itu.edu.tr", "other.edu.tr")
        self.reason("library_search_route_rejected", lambda: self.searcher().search("topic"))
        self.assertEqual(self.credential_calls, 0)

    def test_resolver_rejects_ambiguous_missing_or_unofficial_records_before_navigation(self):
        good = self.resources.search("ProQuest Central", limit=100)
        bad = [None, {}, {**good, "source_url": "https://evil.invalid/"},
               {**good, "metadata_url": DIRECTORY_API_URL + "?url=other"},
               {**good, "truncated": True}, {**good, "resources": []},
               {**good, "resources": good["resources"] * 2},
               {**good, "resources": [{**good["resources"][0], "public_entry_url": CENTRAL + "/other"}]}]
        for result in bad:
            self.resources.override = result if result is not None else {"resources": None}
            self.reason("library_search_official_entry_unavailable", lambda: self.searcher().search("topic"))
        self.assertEqual(self.runner.context.pages, [])
        self.assertEqual(self.credential_calls, 0)

    def test_resolver_returned_redirector_is_strictly_guarded_before_navigation(self):
        bad = [ENTRY.replace("https://", "http://", 1),
               ENTRY.replace("go.openathens.net", "user@go.openathens.net"),
               ENTRY.replace("go.openathens.net", "go.openathens.net:444"),
               ENTRY.replace("go.openathens.net", "go.openathens.net.evil.invalid"),
               ENTRY.replace("/itu.edu.tr?", "/other.edu.tr?"), ENTRY + "&extra=",
               ENTRY + "&url=" + CENTRAL, ENTRY + "#fragment",
               ENTRY.replace("/redirector/", "/redirector\\/"),
               ENTRY.replace("url=", "url=" + quote("https://evil.invalid/?original=", safe="")),
               ENTRY.replace(CENTRAL, quote(quote(CENTRAL, safe=""), safe=""))]
        for entry in bad:
            with self.subTest(entry=entry):
                self.runner.context.entry = entry
                self.reason("library_search_official_entry_unavailable", lambda: self.searcher().search("topic"))
        self.assertEqual(self.runner.context.pages, [])
        self.runner.context.entry = ENTRY.replace(CENTRAL, quote(CENTRAL, safe=""))
        self.assertEqual(self.searcher().search("topic")["count"], 1)

    def test_resolver_errors_are_sanitized(self):
        with patch.object(self.resources, "search", side_effect=RuntimeError("https://private.invalid/token")):
            self.reason("library_search_official_entry_unavailable", lambda: self.searcher().search("topic"))
        self.assertEqual(self.runner.context.pages, [])

    def test_login_post_redirect_blocked_before_transmission(self):
        for url in ("https://evil.invalid/steal", "https://www.proquest.com/steal",
                    LOGIN.replace("itu.edu.tr", "other.edu.tr")):
            self.runner.context.after_login = [(url, "POST", "username=test-user-17&password=private")]
            expected = ("institution_login_destination_rejected" if "test-user-17" in
                        self.runner.context.after_login[0][2] else "library_search_route_rejected")
            self.reason(expected, lambda: self.searcher().search("topic"))
            self.assertTrue(self.runner.context.requests[-1].aborted)
            self.assertFalse(self.runner.context.requests[-1].sent)

    def test_bad_saml_destination_stops_before_credential_source(self):
        for target in ("https://www.proquest.com/Shibboleth.sso/SAML2/POST",
                       ACS + "/extra", ACS.replace("https://", "http://"),
                       ACS.replace(SP_HOST, SP_HOST + ".evil.invalid")):
            self.runner.context.saml_request = authn(target)
            self.reason("library_search_route_rejected", lambda: self.searcher().search("topic"))
        self.assertEqual(self.credential_calls, 0)


class RouteAndDOMTests(unittest.TestCase):
    def guard(self):
        context = FakeContext()
        page = context.new_page()
        self.reservations = 0
        def reserve():
            self.reservations += 1
        guard = _Routes(page, reserve)
        guard.install(context)
        return page, guard

    def test_dom_retries_only_known_navigation_errors_and_is_bounded(self):
        for message in ("Unable to retrieve content because the page is navigating and changing the content",
                        "Execution context was destroyed, most likely because of a navigation"):
            page, guard = self.guard()
            page.url = "https://www.proquest.com/central"
            with patch.object(page, "content", side_effect=[BrowserError(message), SEARCH_FORM]) as read:
                soup = SubscriptionSearch._dom(page, guard)
            self.assertIsNotNone(soup.select_one("form#searchForm"))
            self.assertEqual(read.call_count, 2)
        page, guard = self.guard()
        page.url = "https://www.proquest.com/central"
        with patch.object(page, "content", side_effect=BrowserError("Execution context was destroyed")) as read:
            with self.assertRaises(RetrievalUnavailable) as caught:
                SubscriptionSearch._dom(page, guard)
        self.assertEqual(read.call_count, 8)
        self.assertEqual(caught.exception.reason, "library_search_navigation_timeout")
        with patch.object(page, "content", side_effect=BrowserError("Target page has been closed")) as read:
            with self.assertRaises(BrowserError):
                SubscriptionSearch._dom(page, guard)
        self.assertEqual(read.call_count, 1)

    def test_dom_retries_url_changes_and_title_context_loss_without_mixing_snapshots(self):
        page, guard = self.guard()
        page.url = "https://www.proquest.com/central"
        reads = []
        def content():
            reads.append(True)
            if len(reads) == 1:
                page.url = "https://www.proquest.com/index"
                return '<div id="stale">Old document</div>'
            return SEARCH_FORM
        page.content = content
        soup = SubscriptionSearch._dom(page, guard)
        self.assertIsNone(soup.select_one("#stale"))
        self.assertIsNotNone(soup.select_one("form#searchForm"))
        self.assertEqual(len(reads), 2)
        with patch.object(page, "title", side_effect=[BrowserError("Execution context was destroyed"), "ProQuest"]):
            self.assertIsNotNone(SubscriptionSearch._dom(page, guard).select_one("form#searchForm"))

    def test_dom_navigation_retry_preserves_guard_and_human_check_failures(self):
        page, guard = self.guard()
        page.url = "https://www.proquest.com/central"
        def rejected():
            guard.reason = "institution_login_destination_rejected"
            raise BrowserError("Execution context was destroyed")
        with patch.object(page, "content", side_effect=rejected) as read:
            with self.assertRaises(RetrievalUnavailable) as caught:
                SubscriptionSearch._dom(page, guard)
        self.assertEqual(caught.exception.reason, "institution_login_destination_rejected")
        self.assertEqual(read.call_count, 1)
        page, guard = self.guard()
        page.url = "https://www.proquest.com/central"
        page.context.title = "Verify you are human"
        with patch.object(page, "content", side_effect=[BrowserError("Execution context was destroyed"), SEARCH_FORM]):
            with self.assertRaises(RetrievalUnavailable) as caught:
                SubscriptionSearch._dom(page, guard)
        self.assertEqual(caught.exception.reason, "library_search_human_interaction_required")

    def test_strict_https_authorities_and_paths(self):
        bad = ["http://www.proquest.com/", "https://user@www.proquest.com/",
               "https://:pass@www.proquest.com/", "https://www.proquest.com:444/",
               "https://www.proquest.com:/", "https://www.proquest.com:0443/",
               "https://www.proquest.com\\@evil.invalid/", " https://www.proquest.com/",
               "https://www.proquest.com/\n", "https://www.proquest.com/%2e%2e/",
               "https://www.proquest.com/a/../b", "https://www.proquest.com//x",
               "https://[invalid/", "https:///www.proquest.com/"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(RetrievalUnavailable):
                _url(url)
        self.assertEqual(_url("https://www.proquest.com:443/central").port, 443)

    def test_route_host_suffix_and_popup_rejections(self):
        for url in ("https://www.proquest.com.evil.invalid/", "https://proquest.com/",
                    "https://login.openathens.net/auth/another.edu.tr/",
                    "https://login.openathens.net/auth/itu.edu.tr.evil/",
                    "https://shibboleth-sp.prod.proquest.com/unapproved"):
            page, guard = self.guard()
            route = FakeRoute(page, url)
            guard.handle(route)
            self.assertTrue(route.aborted)
            self.assertIsNotNone(guard.reason)
        page, guard = self.guard()
        route = FakeRoute(page, "https://www.proquest.com/")
        route.request.frame = object()
        guard.handle(route)
        self.assertTrue(route.aborted)

    def test_sp_return_accepts_only_exact_observed_slash_variants(self):
        for path in ("/ONE_SEARCH/PRODWWW", "/ONE_SEARCH/PRODWWW/"):
            with self.subTest(path=path):
                page, guard = self.guard()
                route = page.request(f"https://{SP_HOST}{path}", kind="fetch")
                self.assertTrue(route.sent)
                self.assertFalse(route.aborted)
                self.assertIsNone(guard.reason)
        for path in ("/ONE_SEARCH/PRODWWWevil", "/ONE_SEARCH/PRODWWW/evil",
                     "/ONE_SEARCH/PRODWWW//", "/ONE_SEARCH/PRODWWW%2Fevil",
                     "/ONE_SEARCH/PRODWWW;evil", "/ONE_SEARCH/PRODWWW/../evil"):
            with self.subTest(path=path):
                page, guard = self.guard()
                route = page.request(f"https://{SP_HOST}{path}", kind="fetch")
                self.assertTrue(route.aborted)
                self.assertFalse(route.sent)
                self.assertEqual(guard.reason, "library_search_route_rejected")

    def test_unrelated_managed_pages_pass_through_but_our_popups_do_not(self):
        page, guard = self.guard()
        other = page.context.new_page()
        route = FakeRoute(other, "https://an-unrelated-publisher.invalid/")
        guard.handle(route)
        self.assertTrue(route.passed_through)
        self.assertFalse(route.aborted)
        popup = page.context.new_page()
        popup.opener = lambda: page
        route = FakeRoute(popup, "https://evil.invalid/steal")
        guard.handle(route)
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)

    def test_images_fonts_and_media_are_aborted_without_fetch_or_quota(self):
        for kind in ("image", "font", "media"):
            for url in ("https://www.proquest.com/assets/unneeded", "https://unknown-cdn.invalid/unneeded"):
                page, guard = self.guard()
                route = FakeRoute(page, url, kind=kind)
                guard.handle(route)
                self.assertTrue(route.aborted)
                self.assertFalse(route.sent)
                self.assertIsNone(guard.reason)
                self.assertEqual(self.reservations, 0)

    def test_native_favicon_and_observed_housekeeping_stay_blocked_without_poisoning_guard(self):
        for url, method, kind in (
                ("https://login.openathens.net/favicon.ico", "GET", "other"),
                ("https://www.proquest.com/indexol.pagelayout.checksession:set-cleared-client-storage", "POST", "xhr"),
                ("https://www.proquest.com/indexol.pagelayout.captureproxyhost:capturehost", "POST", "xhr"),
                ("https://www.proquest.com/shibbolethlogin.pagelayout.captureproxyhost:capturehost;jsessionid=synthetic", "POST", "xhr")):
            page, guard = self.guard()
            route = page.request(url, method, kind=kind)
            self.assertTrue(route.aborted)
            self.assertFalse(route.sent)
            self.assertIsNone(guard.reason)
        page, guard = self.guard()
        guard.secrets = ("private",)
        route = page.request("https://www.proquest.com/indexol.pagelayout.captureproxyhost:capturehost",
                             "POST", "password=private", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertEqual(guard.reason, "institution_login_destination_rejected")

    def test_authenticated_index_housekeeping_is_blocked_without_authorizing_other_posts(self):
        path = "/index.homepagelayout.checksession:set-cleared-client-storage"
        page, guard = self.guard()
        route = page.request("https://www.proquest.com" + path, "POST", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertIsNone(guard.reason)
        self.assertEqual(self.reservations, 0)
        for other in (path + "/extra", path + "evil", "/index.homepagelayout.unknown:operation"):
            with self.subTest(path=other):
                page, guard = self.guard()
                route = page.request("https://www.proquest.com" + other, "POST", kind="xhr")
                self.assertTrue(route.aborted)
                self.assertFalse(route.sent)
                self.assertEqual(guard.reason, "library_search_route_rejected")
        page, guard = self.guard()
        guard.secrets = ("private",)
        route = page.request("https://www.proquest.com" + path, "POST", "password=private", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "institution_login_destination_rejected")

    def test_authenticated_capture_host_housekeeping_is_blocked_without_poison_or_secret_escape(self):
        path = "/index.homepagelayout.captureproxyhost:capturehost"
        for body in (None, "proxyHost=synthetic"):
            page, guard = self.guard()
            route = page.request("https://www.proquest.com" + path, "POST", body, kind="xhr")
            self.assertTrue(route.aborted)
            self.assertFalse(route.sent)
            self.assertIsNone(guard.reason)
            self.assertEqual(self.reservations, 0)
        page, guard = self.guard()
        route = page.request("https://www.proquest.com" + path + "/extra", "POST", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "library_search_route_rejected")
        page, guard = self.guard()
        guard.secrets = ("private",)
        route = page.request("https://www.proquest.com" + path, "POST", "password=private", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "institution_login_destination_rejected")

    def test_results_housekeeping_is_blocked_without_poisoning_or_broad_post_exception(self):
        path = "/results.pagelayout.checksession:set-cleared-client-storage"
        page, guard = self.guard()
        route = page.request("https://www.proquest.com" + path, "POST", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertIsNone(guard.reason)
        self.assertEqual(self.reservations, 0)
        for other in (path + "/extra", path + "evil", "/results.pagelayout.unknown:operation"):
            page, guard = self.guard()
            route = page.request("https://www.proquest.com" + other, "POST", kind="xhr")
            self.assertTrue(route.aborted)
            self.assertFalse(route.sent)
            self.assertEqual(guard.reason, "library_search_route_rejected")
        page, guard = self.guard()
        guard.secrets = ("private",)
        route = page.request("https://www.proquest.com" + path, "POST", "password=private", kind="xhr")
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "institution_login_destination_rejected")

    def test_timeout_recovery_requires_safe_recognized_search_form(self):
        page, guard = self.guard()
        page.url, page.html = "https://www.proquest.com/central", SEARCH_FORM
        SubscriptionSearch._timeout_dom(page, guard)
        for html in ("<title>ProQuest</title>", SEARCH_FORM.replace("searchTerm", "unknownField"),
                     SEARCH_FORM.replace(ACTION, "https://evil.invalid/steal"),
                     SEARCH_FORM.replace('method="post"', 'method="get"')):
            page.html = html
            with self.subTest(html=html[:60]), self.assertRaises(RetrievalUnavailable):
                SubscriptionSearch._timeout_dom(page, guard)
        page.html = SEARCH_FORM
        guard.reason = "library_search_route_rejected"
        with self.assertRaises(RetrievalUnavailable) as caught:
            SubscriptionSearch._timeout_dom(page, guard)
        self.assertEqual(caught.exception.reason, "library_search_route_rejected")

    def test_timeout_recovery_recognizes_guarded_itu_form_and_institution_choices(self):
        page, guard = self.guard()
        page.url, page.html = LOGIN, LOGIN_HTML
        with self.assertRaises(RetrievalUnavailable):
            SubscriptionSearch._timeout_dom(page, guard)
        guard.authn_checked = True
        SubscriptionSearch._timeout_dom(page, guard)
        page.html = LOGIN_HTML.replace(f'action="{LOGIN}"', 'action="https://evil.invalid/steal"')
        with self.assertRaises(RetrievalUnavailable):
            SubscriptionSearch._timeout_dom(page, guard)
        page.url = "https://www.proquest.com/athenslogin"
        page.html = ('<form id="wayfForm"><input id="institutionName"></form>'
                     f'<a href="{DISCOVERY}">Istanbul Technical University</a>')
        SubscriptionSearch._timeout_dom(page, guard)
        page.html = page.html.replace("idp.itu.edu.tr", "idp.other.edu.tr")
        with self.assertRaises(RetrievalUnavailable):
            SubscriptionSearch._timeout_dom(page, guard)

    def test_timeout_recovery_stops_human_checks_and_unsubmitted_results(self):
        page, guard = self.guard()
        page.url, page.html = "https://www.proquest.com/central", SEARCH_FORM
        page.context.title = "Verify you are human"
        with self.assertRaises(RetrievalUnavailable) as caught:
            SubscriptionSearch._timeout_dom(page, guard)
        self.assertEqual(caught.exception.reason, "library_search_human_interaction_required")
        page.context.title, page.html = "ProQuest", RESULTS
        with self.assertRaises(RetrievalUnavailable):
            SubscriptionSearch._timeout_dom(page, guard)
        guard.search_sent = True
        SubscriptionSearch._timeout_dom(page, guard)

    def test_commit_timeout_recovers_only_newly_reached_form(self):
        page, guard = self.guard()
        def reached(destination, **kwargs):
            self.assertEqual(kwargs["wait_until"], "commit")
            page.url, page.html = destination, SEARCH_FORM
            raise BrowserTimeout("private URL with query token")
        page.goto = reached
        SubscriptionSearch._commit(page, guard, "https://www.proquest.com/central", 1000)
        page.goto = lambda *args, **kwargs: (_ for _ in ()).throw(BrowserTimeout("private URL"))
        with self.assertRaises(RetrievalUnavailable) as caught:
            SubscriptionSearch._commit(page, guard, "https://www.proquest.com/athenslogin", 1000)
        self.assertEqual(caught.exception.reason, "library_search_navigation_timeout")

    def test_navigation_timeout_unknown_dom_and_other_exceptions_are_not_ignored(self):
        page, guard = self.guard()
        page.url, page.html = "https://www.proquest.com/central", "<div>Incomplete page</div>"
        page.wait_for_load_state = lambda *args, **kwargs: (_ for _ in ()).throw(BrowserTimeout("private"))
        with self.assertRaises(RetrievalUnavailable) as caught:
            SubscriptionSearch._follow(page, guard)
        self.assertEqual(caught.exception.reason, "library_search_navigation_timeout")
        page.wait_for_load_state = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("closed"))
        with self.assertRaises(RuntimeError):
            SubscriptionSearch._follow(page, guard)

    def test_saml_post_only_exact_verified_acs(self):
        for url, body, verified, allowed in (
                (ACS, "SAMLResponse=assertion", True, True),
                (ACS, "SAMLResponse=assertion", False, False),
                (ACS + "?other=1", "SAMLResponse=assertion", True, False),
                (ACS, "password=secret", True, False),
                (ACS, "SAMLResponse=assertion&password=secret", True, False),
                (ACS, "SAMLResponse=one&SAMLResponse=two", True, False)):
            with self.subTest(url=url, body=body):
                page, guard = self.guard()
                guard.authn_checked = verified
                route = page.request(url, "POST", body, kind="fetch")
                self.assertEqual(route.sent, allowed)

    def test_login_form_target_and_credentials_stay_on_tenant(self):
        for url, method, body, allowed in (
                (LOGIN, "POST", "password=private", True),
                (LOGIN + "?password=private", "GET", None, False),
                (LOGIN.replace("itu.edu.tr", "evil.edu.tr"), "POST", "password=private", False),
                ("https://www.proquest.com/?password=private", "GET", None, False)):
            page, guard = self.guard()
            guard.login_active = True
            guard.secrets = ("private",)
            route = page.request(url, method, body, kind="fetch")
            self.assertEqual(route.sent, allowed)

    def test_authn_decode_errors_and_decompression_bound(self):
        _check_authn_request(authn())
        for url in (authn().split("?", 1)[0], authn().split("?", 1)[0] + "?SAMLRequest=%%%",
                    authn().split("?", 1)[0] + "?SAMLRequest=" + quote(base64.b64encode(b"invalid").decode())):
            with self.assertRaises(RetrievalUnavailable):
                _check_authn_request(url)

    def test_authn_dtd_entities_and_external_references_rejected(self):
        packed = parse_qs(urlsplit(authn()).query)["SAMLRequest"][0]
        valid = zlib.decompress(base64.b64decode(packed), -15).decode()
        issuer = f"https://{SP_HOST}/shibboleth"
        for xml in ('<!DOCTYPE samlp:AuthnRequest>' + valid,
                    f'<!DOCTYPE samlp:AuthnRequest [<!ENTITY issuer "{issuer}">]>'
                    + valid.replace(issuer, '&issuer;'),
                    '<!DOCTYPE samlp:AuthnRequest SYSTEM "file:///etc/passwd">' + valid,
                    '<!DOCTYPE samlp:AuthnRequest [<!ENTITY issuer SYSTEM "https://evil.invalid/">]>'
                    + valid.replace(issuer, '&issuer;'), valid.replace(issuer, 'x' * 70_000)):
            with self.subTest(xml=xml[:60]), self.assertRaises(RetrievalUnavailable) as error:
                _check_authn_request(authn(xml=xml))
            self.assertEqual(error.exception.reason, "library_search_route_rejected")

    def test_route_cleanup_drains_pending_callbacks_before_close_and_unroute(self):
        page, guard = self.guard()
        guard.pending = 1
        guard.secrets = ("private",)
        def drain(delay):
            self.assertFalse(page.closed)
            self.assertEqual(guard.session.callback, guard.paused)
            self.assertTrue(guard.closing)
            route = FakeRoute(page, LOGIN, "POST", "password=private")
            guard.handle(route)
            self.assertTrue(route.aborted)
            self.assertFalse(route.sent)
            guard.pending = 0
        page.wait_for_timeout = drain
        guard.close(page.context)
        self.assertTrue(page.closed)
        self.assertIsNone(page.context.handler)
        self.assertEqual(guard.secrets, ())

    def test_cancelled_route_and_already_closed_abort_do_not_escape_cleanup(self):
        page, guard = self.guard()
        route = FakeRoute(page, "https://www.proquest.com/central")
        route.continue_ = lambda **kwargs: (_ for _ in ()).throw(asyncio.CancelledError())
        route.abort = lambda: (_ for _ in ()).throw(RuntimeError("Target closed: private URL"))
        guard.handle(route)
        self.assertEqual(guard.pending, 0)
        self.assertEqual(guard.reason, "library_search_unavailable")
        guard.close(page.context)
        self.assertIsNone(page.context.handler)

    def test_native_redirect_next_hop_is_guarded_before_send(self):
        page, guard = self.guard()
        allowed = "https://www.proquest.com/central"
        initial = page.request(allowed)
        self.assertTrue(initial.sent)
        with self.assertRaises(RuntimeError):
            page.request("https://evil.invalid/steal")
        self.assertTrue(page.context.requests[-1].aborted)
        self.assertFalse(page.context.requests[-1].sent)
        self.assertEqual(guard.reason, "library_search_route_rejected")

    def test_multihop_redirect_cannot_skip_validation(self):
        page, guard = self.guard()
        page.request("https://www.proquest.com/central")
        page.request("https://www.proquest.com/athenslogin")
        with self.assertRaises(RuntimeError):
            page.request("https://www.proquest.com.evil.invalid/steal")
        self.assertEqual([r.sent for r in page.context.requests], [True, True, False])

    def test_credential_post_307_never_replayed(self):
        page, guard = self.guard()
        guard.login_active = True
        guard.secrets = ("private+ pass",)
        body = urlencode({"password": "private+ pass"})
        initial = page.request(LOGIN, "POST", body)
        guard.session.callback({"requestId": "0", "frameId": "main-frame", "resourceType": "Document",
                                "request": {"url": LOGIN, "method": "POST", "postData": body},
                                "responseStatusCode": 307, "responseHeaders": [
                                    {"name": "Location", "value": ACS}]})
        target = page.request(ACS, "POST", body, kind="fetch")
        self.assertTrue(initial.sent)
        self.assertTrue(target.aborted)
        self.assertFalse(target.sent)
        self.assertEqual(guard.reason, "institution_login_destination_rejected")

    def test_response_sandbox_preserves_publisher_policy_and_blocks_download(self):
        page, guard = self.guard()
        page.request("https://www.proquest.com/central")
        event = {"requestId": "0", "frameId": "main-frame", "resourceType": "Document",
                 "request": {"url": "https://www.proquest.com/central", "method": "GET"},
                 "responseStatusCode": 200, "responseHeaders": [
                     {"name": "Content-Type", "value": "text/html"},
                     {"name": "Content-Security-Policy", "value": "default-src 'self'"}]}
        guard.session.callback(event)
        method, data = guard.session.commands[-1]
        self.assertEqual(method, "Fetch.continueResponse")
        self.assertNotIn("responsePhrase", data)
        policies = [h["value"] for h in data["responseHeaders"] if h["name"] == "Content-Security-Policy"]
        self.assertIn("default-src 'self'", policies)
        self.assertTrue(any("sandbox allow-same-origin allow-scripts allow-forms" in p for p in policies))
        self.assertTrue(any("connect-src https://www.proquest.com" in p for p in policies))
        event["responseHeaders"] = [{"name": "Content-Type", "value": "application/pdf"}]
        guard.session.callback(event)
        self.assertEqual(guard.reason, "library_search_download_rejected")
        self.assertEqual(guard.session.commands[-1][0], "Fetch.failRequest")

    def test_missing_post_body_is_rejected_before_native_send(self):
        page, guard = self.guard()
        guard.login_active = True
        route = FakeRoute(page, LOGIN, "POST")
        guard.session.requests["missing"] = route
        guard.session.callback({"requestId": "missing", "frameId": "main-frame", "resourceType": "Document",
                                "request": {"url": LOGIN, "method": "POST", "hasPostData": True}})
        self.assertTrue(route.aborted)
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "library_search_route_rejected")

    def test_observed_housekeeping_with_omitted_post_data_is_aborted_without_poison(self):
        for suffix in ("checksession:set-cleared-client-storage", "captureproxyhost:capturehost"):
            page, guard = self.guard()
            url = "https://www.proquest.com/results.pagelayout." + suffix
            route = FakeRoute(page, url, "POST", kind="xhr")
            guard.session.requests["omitted"] = route
            guard.session.callback({"requestId": "omitted", "frameId": "main-frame", "resourceType": "XHR",
                                    "request": {"url": url, "method": "POST", "hasPostData": True}})
            self.assertTrue(route.aborted)
            self.assertFalse(route.sent)
            self.assertIsNone(guard.reason)
            self.assertEqual(self.reservations, 0)
            self.assertEqual(guard.session.commands[-1][0], "Fetch.failRequest")

    def test_observed_dom_metadata_authors_source_dates_and_truthfulness(self):
        results = self.parse(OBSERVED_RESULTS)
        journal, thesis = results
        self.assertEqual(journal["title"], "Synthetic journal entry")
        self.assertEqual(journal["authors"], ["Example, Ada", "Example, Bo"])
        self.assertEqual(journal["source"], "Example Journal")
        self.assertEqual(journal["publication_date"], "May 2023")
        self.assertEqual(journal["year"], 2023)
        self.assertEqual(thesis["source"], "Example University ProQuest Dissertations & Theses")
        self.assertEqual(thesis["publication_date"], "2022")
        self.assertEqual(thesis["authors"], ["Sample, Casey."])
        for record in results:
            self.assertIsNone(record["doi"])
            self.assertIsNone(record["peer_reviewed"])
            self.assertFalse(record["full_text_read"])
            self.assertEqual(record["institutional_entitlement"], "not_verified")
        self.assertEqual(self.parse(OBSERVED_RESULTS, reviewed_only=True), [])
        self.assertEqual(len(self.parse(OBSERVED_RESULTS, year_from=2023)), 1)
        self.assertEqual(len(self.parse(OBSERVED_RESULTS, limit=1)), 1)
        with self.assertRaises(RetrievalUnavailable):
            self.parse(OBSERVED_RESULTS.replace('id="lor_container"', 'id="unknown-container"'))

    def test_raw_credential_get_and_post_encoding_cannot_escape(self):
        for method in ("GET", "POST"):
            page, guard = self.guard()
            guard.secrets = ("private+ pass",)
            url = "https://www.proquest.com/central"
            body = urlencode({"password": guard.secrets[0]})
            if method == "GET":
                url += "?" + body
                body = None
            route = page.request(url, method, body, kind="fetch")
            self.assertFalse(route.sent)
            self.assertTrue(route.aborted)

    def test_download_requests_and_responses_are_blocked(self):
        page, guard = self.guard()
        route = page.request("https://www.proquest.com/fulltextpdf/123", kind="fetch")
        self.assertFalse(route.sent)
        self.assertEqual(guard.reason, "library_search_download_rejected")

    def parse(self, html, **kwargs):
        arguments = dict(year_from=None, year_to=None, reviewed_only=False, limit=10)
        arguments.update(kwargs)
        return _results(BeautifulSoup(html, "html.parser"), **arguments)

    def test_no_results_and_unknown_or_malformed_dom_are_distinct(self):
        self.assertEqual(self.parse('<div id="noResults">No matches</div>'), [])
        for html in ("<div>New layout</div>", '<ul id="searchResults"></ul>',
                     '<ul id="searchResults"><li class="resultItem">Missing title</li></ul>',
                     RESULTS.replace("/central/docview/123456", "https://evil.invalid/docview/123456"),
                     RESULTS.replace("/central/docview/123456", "/download/123456")):
            with self.subTest(html=html[:70]), self.assertRaises(RetrievalUnavailable):
                self.parse(html)

    def test_missing_doi_and_peer_review_not_inferred_from_fulltext_label(self):
        html = '<ul id="searchResults">' + row(doi=None, reviewed=False) + '</ul>'
        result = self.parse(html)[0]
        self.assertIsNone(result["doi"])
        self.assertIsNone(result["peer_reviewed"])
        self.assertFalse(result["full_text_read"])
        self.assertEqual(self.parse(html, reviewed_only=True), [])
        self.assertIsNone(self.parse(html.replace("A short summary.", "DOI 10.1234/guess"))[0]["doi"])
        html = html.replace("A short summary.", '<a href="https://doi.org/10.1234/visible">DOI</a>')
        self.assertEqual(self.parse(html)[0]["doi"], "10.1234/visible")

    def test_filters_deduplication_and_limits(self):
        html = '<ul id="searchResults">' + row() + row() + row("77", date="2024", doi=None)
        html += row("88", date="unknown", reviewed=False) + '</ul>'
        self.assertEqual(len(self.parse(html)), 3)
        self.assertEqual(len(self.parse(html, limit=1)), 1)
        self.assertEqual([x["proquest_id"] for x in self.parse(html, year_from=2025,
                                                             year_to=2025, reviewed_only=True)], ["123456"])
        self.assertEqual(len(self.parse(html, year_to=2024)), 1)
        many = '<ul id="searchResults">' + ''.join(row(str(i)) for i in range(110)) + '</ul>'
        self.assertEqual(len(self.parse(many, limit=MAX_RESULTS)), MAX_RESULTS)


if __name__ == "__main__":
    unittest.main()
