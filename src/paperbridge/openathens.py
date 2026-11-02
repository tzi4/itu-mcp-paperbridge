"""Subscription retrieval through ITU's OpenAthens, for publishers without their own adapter.

ITU reaches most subscribed publishers through OpenAthens: the redirector
``go.openathens.net/redirector/itu.edu.tr?url=...`` starts the publisher's
institutional login, and ITU's OpenAthens page asks for the student or staff
number and the library account password (not the central ITU password).

One browser session per call: open the article's DOI through the redirector,
sign in when OpenAthens asks, then read the PDF that the article page names in
``citation_pdf_url``. The form is filled only on ITU's OpenAthens login page,
and only when its POST target is that same host. A verification page from the
publisher (for example Cloudflare's "Just a moment...") ends the attempt with
``publisher_interaction_required``; it is never solved or bypassed.
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from .browser import RetrievalUnavailable
from .identifiers import doi_url, normalize_doi
from .network import publisher_proxy
from .pdf_identity import verify_pdf_identity
from .permissions import read_rights
from .publishers import identify_platform

REDIRECTOR = "https://go.openathens.net/redirector/itu.edu.tr?url="
LOGIN_HOST = "login.openathens.net"
LOGIN_PATH = "/auth/itu.edu.tr/"
PROXY_SUFFIX = ".proxy.openathens.net"
MAX_PDF_BYTES = 40_000_000
SETTLE_SECONDS = 30
CHALLENGE_MARKERS = ("just a moment", "are you a robot", "attention required", "verify you are human",
                     "client challenge", "security check")


@dataclass(frozen=True)
class Publisher:
    hosts: frozenset[str]
    pdf_hosts: frozenset[str]
    # Publishers whose pages often show a bot check to automated browsers.
    best_effort: bool = False
    # Atypon sites serve /doi/pdf/<doi> when the page names no citation_pdf_url.
    doi_pdf_path: bool = False


# Hosts are checked exactly; OpenAthens-proxied versions (``a-b-c.ap2.proxy.openathens.net``)
# of the same hosts are accepted too, because ITU routes some publishers through that proxy.
PUBLISHERS = {
    "asme": Publisher(frozenset({"asmedigitalcollection.asme.org"}),
                      frozenset({"asmedigitalcollection.asme.org", "watermark.silverchair.com",
                                 "watermark02.silverchair.com"})),
    "acs": Publisher(frozenset({"pubs.acs.org"}),
                     frozenset({"pubs.acs.org", "s3-eu-west-1.amazonaws.com"}), best_effort=True),
    "cambridge": Publisher(frozenset({"www.cambridge.org"}), frozenset({"www.cambridge.org"})),
    "oxford": Publisher(frozenset({"academic.oup.com"}),
                        frozenset({"academic.oup.com", "watermark.silverchair.com",
                                   "watermark02.silverchair.com"}), best_effort=True),
    "taylor_francis": Publisher(frozenset({"www.tandfonline.com"}), frozenset({"www.tandfonline.com"}),
                                best_effort=True, doi_pdf_path=True),
    "sage": Publisher(frozenset({"journals.sagepub.com"}), frozenset({"journals.sagepub.com"}),
                      best_effort=True, doi_pdf_path=True),
    "acm": Publisher(frozenset({"dl.acm.org"}), frozenset({"dl.acm.org"}), best_effort=True,
                     doi_pdf_path=True),
}


@dataclass(frozen=True)
class OpenAthensCredentials:
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "OpenAthensCredentials":
        """The library account; the catalog login is the same account at ITU."""
        username = (os.getenv("PAPERBRIDGE_OPENATHENS_USERNAME") or os.getenv("NINOVA_LIBRARY_ID") or "").strip()
        password = os.getenv("PAPERBRIDGE_OPENATHENS_PASSWORD") or os.getenv("NINOVA_LIBRARY_PIN") or ""
        if not username or not password:
            raise RetrievalUnavailable("openathens_credentials_unavailable")
        return cls(username, password)


def openathens_configured() -> bool:
    try:
        OpenAthensCredentials.from_env()
        return True
    except RetrievalUnavailable:
        return False


def use_publisher_proxy() -> bool:
    """Opt in only after the operator has extended the local CONNECT allowlist."""
    enabled = os.environ.get("PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY", "0")
    if enabled not in {"0", "1"}:
        raise RetrievalUnavailable("openathens_proxy_configuration_invalid")
    if enabled == "0":
        return False
    try:
        proxy = publisher_proxy()
    except ValueError:
        raise RetrievalUnavailable("openathens_proxy_configuration_invalid") from None
    if not proxy:
        raise RetrievalUnavailable("openathens_proxy_unavailable")
    return True


def _clean(url: str) -> bool:
    return isinstance(url, str) and "\\" not in url and not any(ord(c) < 33 for c in url)


def _https_host(url: str) -> str | None:
    try:
        part = urlsplit(url)
        if part.scheme != "https" or part.port not in (None, 443) or part.username or part.password:
            return None
        return part.hostname
    except ValueError:
        return None


def unproxied(host: str | None) -> str | None:
    """``asmedigitalcollection-asme-org.ap2.proxy.openathens.net`` -> ``asmedigitalcollection.asme.org``."""
    if not host or not host.endswith(PROXY_SUFFIX):
        return host
    label = host[: -len(PROXY_SUFFIX)].split(".")
    if len(label) != 2 or not label[0] or not label[1].isalnum():
        return None
    return label[0].replace("-", ".")


def host_allowed(url: str, hosts: frozenset[str]) -> bool:
    return _clean(url) and unproxied(_https_host(url)) in hosts


def trusted_login(url: str) -> bool:
    return _clean(url) and _https_host(url) == LOGIN_HOST and urlsplit(url).path.startswith(LOGIN_PATH)


def redirector_url(doi: str) -> str:
    return REDIRECTOR + quote(doi_url(normalize_doi(doi)), safe="")


def challenged(title: str) -> bool:
    title = (title or "").strip().casefold()
    return any(marker in title for marker in CHALLENGE_MARKERS)


def _title(page) -> str:
    """The page title, or "" while a hand-off page is still navigating."""
    try:
        return page.title()
    except Exception:
        return ""


_PAGE_FETCH = """async ({url, limit}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 50000);
    try {
        const response = await fetch(url, {credentials: 'include', redirect: 'follow', signal: controller.signal});
        const reader = response.body.getReader();
        const chunks = [];
        let length = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            length += value.length;
            if (length > limit) return {status: response.status, url: response.url, oversized: true};
            chunks.push(value);
        }
        const pieces = [];
        for (const chunk of chunks)
            for (let i = 0; i < chunk.length; i += 16384) pieces.push(String.fromCharCode(...chunk.subarray(i, i + 16384)));
        return {status: response.status, url: response.url, data: btoa(pieces.join(''))};
    } catch (_) {
        return {failed: true};
    } finally {
        clearTimeout(timer);
    }
}"""


def _download(page, context, url: str) -> tuple[int, str, bytes]:
    """Fetch inside the article page first, so the request carries that page's session.

    A cross-origin redirect (for example to a signed storage URL) can fail there;
    then the browser context's own request client follows it instead.
    """
    import base64
    result = page.evaluate(_PAGE_FETCH, {"url": url, "limit": MAX_PDF_BYTES})
    if result.get("oversized"):
        raise RetrievalUnavailable("pdf_size_limit_exceeded")
    if not result.get("failed"):
        return int(result["status"]), str(result["url"]), base64.b64decode(result["data"], validate=True)
    response = context.request.get(url, headers={"Referer": page.url}, max_redirects=5, timeout=60000)
    return response.status, response.url, response.body()


def fill_login(page, credential_source) -> None:
    """Fill ITU's OpenAthens form, after checking the page and the form target."""
    if not trusted_login(page.url):
        raise RetrievalUnavailable("institution_login_destination_rejected")
    target = page.evaluate("() => { const f = document.querySelector('input[type=password]')?.form;"
                           " return f ? [f.action, f.method] : null; }")
    if not target or str(target[1]).lower() != "post" or not trusted_login(target[0]):
        raise RetrievalUnavailable("institution_login_destination_rejected")
    username = page.locator('input[name="username"]:visible')
    password = page.locator('input[name="password"]:visible')
    submit = page.locator('button[name="logIn"]')
    if username.count() != 1 or password.count() != 1 or submit.count() != 1:
        raise RetrievalUnavailable("institution_login_form_unrecognized")
    banner = page.locator("#ccc-notify-reject")
    if banner.count() == 1:
        banner.click(timeout=5000)
    try:
        credentials = credential_source()
    except Exception:
        raise RetrievalUnavailable("openathens_credentials_unavailable") from None
    try:
        username.fill(credentials.username)
        password.fill(credentials.password)
        submit.click(timeout=10000)
    finally:
        credentials = None


class OpenAthensProvider:
    name = "openathens"

    def __init__(self, store, *, runner, credentials=OpenAthensCredentials.from_env, clock=time.monotonic):
        self.store = store
        self.runner = runner
        self.credentials = credentials
        self._clock = clock

    @staticmethod
    def publisher(paper: dict) -> str | None:
        name = identify_platform(paper)
        return name if name in PUBLISHERS else None

    def supports(self, paper: dict) -> bool:
        return self.publisher(paper) is not None

    def rights(self, doi):
        return read_rights(self.store, doi, self.name)

    def _settle(self, page) -> None:
        """Wait while OpenAthens and SAML hand-offs post forward to the publisher."""
        deadline = self._clock() + SETTLE_SECONDS
        submitted = False
        while self._clock() < deadline:
            page.wait_for_timeout(1500)
            if trusted_login(page.url):
                if submitted:
                    raise RetrievalUnavailable("institution_login_rejected")
                page.locator('input[type="password"]:visible').wait_for(timeout=10000)
                fill_login(page, self.credentials)
                submitted = True
                continue
            host = _https_host(page.url) or ""
            title = _title(page)
            if host and host != LOGIN_HOST and host != "go.openathens.net" and title \
                    and "saml" not in title.casefold() and "login" not in title.casefold():
                return
        raise RetrievalUnavailable("institution_login_interaction_required")

    def _read(self, context, paper: dict, publisher: Publisher) -> bytes:
        page = context.new_page()
        try:
            page.goto(redirector_url(paper["doi"]), wait_until="domcontentloaded", timeout=45000)
            self._settle(page)
            if challenged(page.title()):
                raise RetrievalUnavailable("publisher_interaction_required")
            if not host_allowed(page.url, publisher.hosts):
                raise RetrievalUnavailable("publisher_redirect_not_allowed")
            try:
                page.wait_for_selector('meta[name="citation_pdf_url"]', state="attached", timeout=15000)
            except Exception:
                # Some article pages fill in their metadata late or not at all; checked below.
                if challenged(_title(page)):
                    raise RetrievalUnavailable("publisher_interaction_required") from None
            pdf = page.evaluate("() => (document.querySelector('meta[name=citation_pdf_url]') || {}).content || null")
            if not pdf and publisher.doi_pdf_path:
                pdf = "https://" + _https_host(page.url) + "/doi/pdf/" + normalize_doi(paper["doi"])
            if not pdf:
                raise RetrievalUnavailable("publisher_pdf_link_unavailable")
            # Proxied article pages name the publisher's own PDF URL; stay on the proxy host.
            article_host, pdf_host = _https_host(page.url), _https_host(pdf)
            if article_host and article_host.endswith(PROXY_SUFFIX) and unproxied(article_host) == pdf_host:
                pdf = pdf.replace("https://" + pdf_host, "https://" + article_host, 1)
            if not host_allowed(pdf, publisher.pdf_hosts):
                raise RetrievalUnavailable("publisher_redirect_not_allowed")
            status, final_url, data = _download(page, context, pdf)
            if not host_allowed(final_url, publisher.pdf_hosts):
                raise RetrievalUnavailable("publisher_redirect_not_allowed")
            if status != 200:
                raise RetrievalUnavailable("publisher_access_unavailable")
            if len(data) > MAX_PDF_BYTES:
                raise RetrievalUnavailable("pdf_size_limit_exceeded")
            if not data.startswith(b"%PDF-"):
                # An HTML reply here is usually the publisher's verification page.
                raise RetrievalUnavailable("publisher_interaction_required"
                                           if b"challenge" in data[:20000].lower() else "publisher_did_not_return_pdf")
            return data
        finally:
            page.close()

    def retrieve(self, paper: dict) -> dict:
        from filelock import FileLock, Timeout
        name = self.publisher(paper)
        if name is None:
            raise RetrievalUnavailable("publisher_not_supported")
        rights = self.rights(paper["doi"])
        try:
            with FileLock(str(self.store.root / ".browser.lock"), timeout=0):
                data = self.runner.run(lambda context: self._read(context, paper, PUBLISHERS[name]),
                                       use_publisher_proxy=use_publisher_proxy())
                identity = verify_pdf_identity(data, paper)
                with tempfile.TemporaryDirectory(dir=self.store.root) as directory:
                    path = Path(directory) / "article.pdf"
                    with path.open("xb") as stream:
                        path.chmod(0o600)
                        stream.write(data)
                    document = self.store.ingest(path, title=paper["title"], url=doi_url(paper["doi"]),
                                                 rights=rights)
                result = self.store.fetch(document["first_chunk_id"])
                result["metadata"].update(retrieval="openathens_browser_session", publisher=name,
                                          institutional_entitlement="not_verified", pdf_identity=identity)
                return result
        except Timeout:
            raise RetrievalUnavailable("browser_busy") from None
