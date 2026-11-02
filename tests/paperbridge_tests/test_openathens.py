from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paperbridge import DocumentStore
from paperbridge.browser import RetrievalUnavailable
from paperbridge.openathens import (
    OpenAthensCredentials, OpenAthensProvider, host_allowed, redirector_url, trusted_login, unproxied,
    PUBLISHERS,
)
from paperbridge_tests.test_paperbridge import sample_pdf

LOGIN = "https://login.openathens.net/auth/itu.edu.tr/o/82346286?t=%2Fsaml"
PAPER = {"doi": "10.1115/1.4056701", "title": "Synthetic labyrinth seal fixture",
         "publisher_url": "https://asmedigitalcollection.asme.org/turbomachinery/article/145/7/071007/1",
         "publisher": "ASME International"}
ARTICLE = "https://asmedigitalcollection-asme-org.ap2.proxy.openathens.net/turbomachinery/article/145/7/071007/1"
PDF_URL = "https://asmedigitalcollection-asme-org.ap2.proxy.openathens.net/turbomachinery/article-pdf/145/7/x.pdf"
PDF = sample_pdf(PAPER["title"] + " DOI: " + PAPER["doi"])


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def count(self):
        return 1 if self.page.url.startswith(LOGIN) or self.selector == "#ccc-notify-reject" else 0

    def wait_for(self, **_):
        return None

    def fill(self, value):
        self.page.filled[self.selector] = value

    def click(self, **_):
        if self.selector == 'button[name="logIn"]':
            self.page.submissions += 1
            self.page.url = self.page.after_login


class FakePage:
    def __init__(self, scenario):
        self.scenario = scenario
        self.url = "about:blank"
        self.after_login = scenario.get("after_login", ARTICLE)
        self.filled = {}
        self.submissions = 0
        self.closed = False

    def goto(self, url, **_):
        self.scenario["visited"].append(url)
        self.url = self.scenario.get("first", LOGIN)

    def wait_for_timeout(self, _):
        return None

    def wait_for_selector(self, *_, **__):
        if self.scenario.get("pdf", PDF_URL) is None:
            raise TimeoutError("no meta")

    def title(self):
        if self.url.startswith(LOGIN):
            return "OpenAthens / Sign in"
        return self.scenario.get("title", "Nonlinear Flutter Analysis | J. Turbomach.")

    def locator(self, selector):
        return Locator(self, selector)

    def evaluate(self, script, arg=None):
        if "password" in script:
            return self.scenario.get("form", [LOGIN, "post"])
        if "fetch(" in script:
            # The in-page fetch fails like a blocked cross-origin redirect; the context request follows.
            return {"failed": True}
        return self.scenario.get("pdf", PDF_URL)

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, scenario):
        self.scenario = scenario
        self.pages = []
        self.request = SimpleNamespace(get=self.get)

    def new_page(self):
        page = FakePage(self.scenario)
        self.pages.append(page)
        return page

    def get(self, url, **_):
        self.scenario["downloads"].append(url)
        final = self.scenario.get("final_url", url)
        return SimpleNamespace(url=final, status=self.scenario.get("status", 200),
                               body=lambda: self.scenario.get("body", PDF))


class OpenAthensTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY": "0",
                                              "PAPERBRIDGE_PUBLISHER_PROXY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DocumentStore(Path(self.temp.name))
        (self.store.root / "publisher-policy.json").write_text(json.dumps({"openathens": {
            "local_processing": True, "external_model": True, "basis": "Original synthetic fixture"}}))
        self.scenario = {"visited": [], "downloads": []}
        self.context = FakeContext(self.scenario)
        self.runner = Mock()
        self.runner.run.side_effect = lambda operation, **_: operation(self.context)
        self.credentials = Mock(return_value=OpenAthensCredentials("150200000", "library-secret"))
        clock = iter(range(0, 10_000))
        self.provider = OpenAthensProvider(self.store, runner=self.runner, credentials=self.credentials,
                                           clock=lambda: next(clock))

    def test_url_guards(self):
        self.assertTrue(trusted_login(LOGIN))
        for url in ("https://login.openathens.net/auth/other.edu/o/1", "http://login.openathens.net/auth/itu.edu.tr/",
                    "https://login.openathens.net.evil.test/auth/itu.edu.tr/", "https://evil.test/auth/itu.edu.tr/"):
            with self.subTest(url=url):
                self.assertFalse(trusted_login(url))
        self.assertEqual(unproxied("asmedigitalcollection-asme-org.ap2.proxy.openathens.net"),
                         "asmedigitalcollection.asme.org")
        self.assertEqual(unproxied("pubs.acs.org"), "pubs.acs.org")
        self.assertIsNone(unproxied("a.b.c.proxy.openathens.net"))
        asme = PUBLISHERS["asme"].hosts
        self.assertTrue(host_allowed(ARTICLE, asme))
        for url in ("https://evil-test.ap2.proxy.openathens.net/x", "http://asmedigitalcollection.asme.org/x",
                    "https://user:pw@asmedigitalcollection.asme.org/x", "https://asmedigitalcollection.asme.org.evil.test/"):
            with self.subTest(url=url):
                self.assertFalse(host_allowed(url, asme))
        self.assertEqual(redirector_url("10.1115/1.4056701"),
                         "https://go.openathens.net/redirector/itu.edu.tr?url=https%3A%2F%2Fdoi.org%2F10.1115%2F1.4056701")

    def test_supports_only_listed_publishers(self):
        self.assertTrue(self.provider.supports(PAPER))
        self.assertTrue(self.provider.supports({"doi": "10.1021/x", "publisher_url": "https://pubs.acs.org/doi/10.1021/x"}))
        for paper in ({"doi": "10.1016/x", "publisher_url": "https://www.sciencedirect.com/science/article/pii/X"},
                      {"doi": "10.1007/x", "publisher_url": "https://link.springer.com/10.1007/x"}):
            with self.subTest(paper=paper):
                self.assertFalse(self.provider.supports(paper))

    def test_signs_in_once_and_reads_the_named_pdf(self):
        result = self.provider.retrieve(PAPER)
        page = self.context.pages[0]
        self.assertEqual(self.scenario["visited"], [redirector_url(PAPER["doi"])])
        self.assertEqual(page.submissions, 1)
        self.assertEqual(page.filled, {'input[name="username"]:visible': "150200000",
                                       'input[name="password"]:visible': "library-secret"})
        self.assertEqual(self.scenario["downloads"], [PDF_URL])
        self.assertEqual(result["metadata"]["retrieval"], "openathens_browser_session")
        self.assertEqual(result["metadata"]["publisher"], "asme")
        self.assertTrue(page.closed)
        self.runner.run.assert_called_once()
        self.assertIs(self.runner.run.call_args.kwargs["use_publisher_proxy"], False)

    def test_explicit_proxy_routes_the_whole_openathens_session(self):
        with patch.dict(os.environ, {"PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY": "1",
                                    "PAPERBRIDGE_PUBLISHER_PROXY": "http://127.0.0.1:18888"}):
            self.provider.retrieve(PAPER)
        self.assertIs(self.runner.run.call_args.kwargs["use_publisher_proxy"], True)
        self.assertEqual(self.context.pages[0].submissions, 1)
        self.assertEqual(self.scenario["downloads"], [PDF_URL])

    def test_opted_in_proxy_misconfiguration_never_runs_direct(self):
        for flag, endpoint in (("1", ""), ("1", "http://remote.test:18888"),
                               ("true", "http://127.0.0.1:18888")):
            with self.subTest(flag=flag, endpoint=endpoint), patch.dict(os.environ, {
                    "PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY": flag, "PAPERBRIDGE_PUBLISHER_PROXY": endpoint}):
                with self.assertRaisesRegex(RetrievalUnavailable, "openathens_proxy_"):
                    self.provider.retrieve(PAPER)
                self.runner.run.assert_not_called()

    def test_proxy_failure_is_not_retried_on_the_default_route(self):
        self.runner.run.side_effect = RetrievalUnavailable("browser_interaction_required")
        with patch.dict(os.environ, {"PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY": "1",
                                    "PAPERBRIDGE_PUBLISHER_PROXY": "http://127.0.0.1:18888"}):
            with self.assertRaisesRegex(RetrievalUnavailable, "browser_interaction_required"):
                self.provider.retrieve(PAPER)
        self.runner.run.assert_called_once()
        self.assertIs(self.runner.run.call_args.kwargs["use_publisher_proxy"], True)

    def test_existing_session_skips_the_login(self):
        self.scenario["first"] = ARTICLE
        self.provider.retrieve(PAPER)
        self.assertEqual(self.context.pages[0].submissions, 0)
        self.credentials.assert_not_called()

    def test_untrusted_form_target_is_never_filled(self):
        self.scenario["form"] = ["https://evil.test/collect", "post"]
        with self.assertRaisesRegex(RetrievalUnavailable, "institution_login_destination_rejected"):
            self.provider.retrieve(PAPER)
        self.assertEqual(self.context.pages[0].filled, {})
        self.credentials.assert_not_called()

    def test_rejected_login_is_not_retried(self):
        self.scenario["after_login"] = LOGIN + "&error=1"
        with self.assertRaisesRegex(RetrievalUnavailable, "institution_login_rejected"):
            self.provider.retrieve(PAPER)
        self.assertEqual(self.context.pages[0].submissions, 1)

    def test_verification_page_is_reported_not_solved(self):
        self.scenario.update(first=ARTICLE, title="Just a moment...")
        with self.assertRaisesRegex(RetrievalUnavailable, "publisher_interaction_required"):
            self.provider.retrieve(PAPER)
        self.assertEqual(self.scenario["downloads"], [])

    def test_pdf_hosts_and_bodies_are_checked(self):
        cases = [
            ({"first": "https://evil.test/article"}, "publisher_redirect_not_allowed"),
            ({"first": ARTICLE, "pdf": "https://evil.test/x.pdf"}, "publisher_redirect_not_allowed"),
            ({"first": ARTICLE, "final_url": "https://evil.test/x.pdf"}, "publisher_redirect_not_allowed"),
            ({"first": ARTICLE, "pdf": None}, "publisher_pdf_link_unavailable"),
            ({"first": ARTICLE, "status": 403}, "publisher_access_unavailable"),
            ({"first": ARTICLE, "body": b"<html>cf challenge</html>"}, "publisher_interaction_required"),
            ({"first": ARTICLE, "body": b"<html>sign in</html>"}, "publisher_did_not_return_pdf"),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                self.scenario.update({"visited": [], "downloads": []}, **overrides)
                with self.assertRaisesRegex(RetrievalUnavailable, reason):
                    self.provider.retrieve(PAPER)
                for key in overrides:
                    self.scenario.pop(key)

    def test_permission_is_required(self):
        (self.store.root / "publisher-policy.json").unlink()
        with self.assertRaises(RetrievalUnavailable):
            self.provider.retrieve(PAPER)
        self.runner.run.assert_not_called()

    def test_credentials_from_env_with_library_fallback(self):
        with patch.dict(os.environ, {"PAPERBRIDGE_OPENATHENS_USERNAME": "150", "PAPERBRIDGE_OPENATHENS_PASSWORD": "pw"},
                        clear=True):
            self.assertEqual(OpenAthensCredentials.from_env(), OpenAthensCredentials("150", "pw"))
        with patch.dict(os.environ, {"NINOVA_LIBRARY_ID": "151", "NINOVA_LIBRARY_PIN": "pin"}, clear=True):
            self.assertEqual(OpenAthensCredentials.from_env(), OpenAthensCredentials("151", "pin"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RetrievalUnavailable, "openathens_credentials_unavailable"):
                OpenAthensCredentials.from_env()

    def test_service_registers_the_adapter_only_with_credentials(self):
        from paperbridge import PaperBridge
        with patch.dict(os.environ, {"PAPERBRIDGE_BROWSER_ENGINE": "chromium"}, clear=True):
            self.assertNotIn("openathens", [p.name for p in PaperBridge(self.store, Mock()).providers])
            self.assertNotIn("asme", PaperBridge.status()["configured_providers"])
        with patch.dict(os.environ, {"PAPERBRIDGE_BROWSER_ENGINE": "chromium", "NINOVA_LIBRARY_ID": "1",
                                     "NINOVA_LIBRARY_PIN": "2"}, clear=True):
            bridge = PaperBridge(self.store, Mock())
            self.assertEqual([p.name for p in bridge.providers], ["ieee", "springer", "openathens"])
            self.assertIn("asme", bridge.status()["configured_providers"])
            self.assertEqual(bridge.access_details(PAPER)["provider"], "asme")


if __name__ == "__main__":
    unittest.main()
