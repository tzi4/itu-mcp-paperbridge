"""Read-only discovery of ITU's public research-resource directory.

Entry links are navigation metadata. They do not establish entitlement, grant
retrieval permission, or change any browser or publisher allowlist. Only the
fixed public directory API is contacted. An injected session must be dedicated
to this client because inherited credentials and transport settings are cleared.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import re
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
import requests


DIRECTORY_API_URL = "https://service.library.itu.edu.tr/web/api/Veritabanlari/FullList"
DIRECTORY_PAGE_URL = "https://kutuphane.itu.edu.tr/arastirma/veritabanlari"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 2048
CACHE_TTL_SECONDS = 300
_REQUEST_SECONDS = 30
_STOPWORDS = frozenset("a an and for in of on the to with ve ile icin bir".split())


class ResearchResourcesError(RuntimeError):
    """A directory failure with a stable code and no raw server or session data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid() -> ResearchResourcesError:
    return ResearchResourcesError("invalid_response", "The research directory returned malformed metadata.")


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold().replace("ı", "i"))
    return "".join(c for c in value if not unicodedata.combining(c))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for t in re.findall(r"[^\W_]+", _fold(value)) if t not in _STOPWORDS))


def _text(value, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 128_000:
        raise _invalid()
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = "".join(" " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in text)
    text = re.sub(r"\s+([.,!?;:%)\]])", r"\1", " ".join(text.split()))
    return text[:maximum]


def _names(item: dict, base: str, maximum: int) -> tuple[str, tuple[str, ...]]:
    english = _text(item.get(base + "_en"), maximum)
    original = _text(item.get(base), maximum)
    return english or original, tuple(dict.fromkeys(v for v in (english, original) if v))


def _entry_link(value) -> str | None:
    """Validate reported HTTP(S) navigation links without requesting their hosts."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    decoded = unquote(value)
    if (any(c.isspace() for c in value)
            or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in decoded)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443 if parsed.scheme == "https" else 80)):
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if host.endswith((".localhost", ".local", ".internal")) or host.endswith("."):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in host.split(".")
            ):
                return None
            # Reject legacy numeric IP spellings interpreted differently by browsers.
            if re.fullmatch(r"[0-9.]+", host) or host.split(".")[-1].isdigit():
                return None
        return value
    except (ValueError, UnicodeError):
        return None


def _validate_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "service.library.itu.edu.tr"
                 and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                 and parsed.path == "/web/api/Veritabanlari/FullList" and not parsed.query
                 and not parsed.fragment and "\\" not in value
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ResearchResourcesError("untrusted_endpoint", "The directory endpoint is not the official HTTPS API.")


@dataclass(frozen=True)
class _Record:
    catalog_record_id: int
    label: str
    subjects: tuple[str, ...]
    description: str
    publisher: str | None
    public_entry_url: str | None
    institutional_entry_url: str | None
    fields: tuple[tuple[str, frozenset[str], int], ...]
    subject_aliases: tuple[frozenset[str], ...]

    def public(self) -> dict:
        return {
            "catalog_record_id": self.catalog_record_id, "label": self.label,
            "subjects": list(self.subjects), "description": self.description,
            "publisher": self.publisher, "public_entry_url": self.public_entry_url,
            "institutional_entry_url": self.institutional_entry_url,
            "entitlement": "not_checked", "full_text_read": False,
            "retrieval_authorized": False,
        }


def _parse_directory(data) -> tuple[_Record, ...]:
    if (not isinstance(data, dict) or type(data.get("Toplam")) is not int
            or not isinstance(data.get("Liste"), list)):
        raise _invalid()
    rows = data["Liste"]
    if data["Toplam"] != len(rows) or len(rows) > MAX_RECORDS:
        raise _invalid()
    records = []
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("Id")) is not int
                or row["Id"] <= 0 or row["Id"] in ids):
            raise _invalid()
        ids.add(row["Id"])
        label, labels = _names(row, "Adi", 512)
        if not label:
            raise _invalid()
        description, descriptions = _names(row, "Aciklama", 16_000)
        filters = row.get("Filtreler")
        if not isinstance(filters, list) or len(filters) > 256:
            raise _invalid()
        subjects, publishers, aliases, publisher_aliases = [], [], [], []
        for item in filters:
            if not isinstance(item, dict) or type(item.get("FiltreTurId")) is not int:
                raise _invalid()
            if item["FiltreTurId"] not in {1, 3}:
                continue
            name, variants = _names(item, "Adi", 512)
            if not name:
                raise _invalid()
            if item["FiltreTurId"] == 1:
                subjects.append(name)
                aliases.extend(variants)
            else:
                publishers.append(name)
                publisher_aliases.extend(variants)
        fields = tuple((" ".join(_tokens(text)), frozenset(_tokens(text)), weight)
                       for values, weight in ((labels, 12), (publisher_aliases, 9),
                                              (aliases, 6), (descriptions, 2))
                       for text in values)
        records.append(_Record(
            row["Id"], label, tuple(dict.fromkeys(subjects)), description,
            ", ".join(dict.fromkeys(publishers)) or None,
            _entry_link(row.get("Link")), _entry_link(row.get("KampusDisiLink")),
            fields, tuple(frozenset(_tokens(alias)) for alias in aliases),
        ))
    return tuple(records)


def _match(term: str, words: frozenset[str]) -> int:
    if term in words:
        return 2
    return int(len(term) >= 3 and any(word.startswith(term) for word in words))


def _score(record: _Record, terms: tuple[str, ...]) -> int:
    if not terms:
        return 1
    score = 0
    for term in terms:
        best = max((_match(term, words) * weight for _, words, weight in record.fields), default=0)
        if not best:
            return 0
        score += best
    phrase = " ".join(terms)
    for text, _, weight in record.fields:
        if weight == 12 and phrase == text:
            score += 80
            break
        if weight == 12 and f" {phrase} " in f" {text} ":
            score += 40
            break
    return score


class ResearchResources:
    """Search one immutable directory snapshot cached per client for five minutes.

    The single snapshot has at most 2048 records and is loaded under a lock so
    concurrent callers share a refresh. Failed refreshes raise explicit errors
    rather than silently returning expired metadata. Public results are copies.
    Query terms must all match, with label and publisher matches ranked above
    subjects and descriptions. Turkish and English source labels are searchable.
    """

    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        self._lock = threading.Lock()
        self._snapshot: tuple[_Record, ...] | None = None
        self._expires_at = 0.0
        self._isolate_session()

    def _isolate_session(self) -> None:
        self.session.trust_env = False
        self.session.auth = None
        self.session.proxies = {}
        self.session.params = {}
        self.session.headers.clear()
        self.session.cookies.clear()
        self.session.hooks = {"response": []}
        self.session.cert = None
        self.session.verify = True

    def _load(self) -> tuple[_Record, ...]:
        """Use connect/read timeouts and a cooperative 30-second stream budget.

        The budget is checked between yielded chunks, so an ongoing slow chunk
        read may exceed it. It is not a hard total wall-clock deadline.
        """
        _validate_endpoint(DIRECTORY_API_URL)
        self._isolate_session()
        deadline = time.monotonic() + _REQUEST_SECONDS
        try:
            with self.session.get(
                DIRECTORY_API_URL, stream=True, timeout=(5, 20), allow_redirects=False,
                auth=(), proxies={}, cookies={}, verify=True,
                headers={"Accept": "application/json", "Accept-Encoding": "identity",
                         "User-Agent": "PaperBridge research-resource directory"},
            ) as response:
                if 300 <= response.status_code < 400 or response.history:
                    raise ResearchResourcesError("redirect_rejected", "The directory API returned a redirect.")
                if response.status_code != 200:
                    raise ResearchResourcesError("http_error", f"The directory API returned HTTP {response.status_code}.")
                _validate_endpoint(response.url)
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise _invalid()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ResearchResourcesError("invalid_response", "The directory API returned an unsupported encoding.")
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not re.fullmatch(r"[0-9]{1,20}", length):
                        raise _invalid()
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise ResearchResourcesError("response_too_large", "The directory response exceeded 8 MiB.")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if time.monotonic() > deadline:
                        raise ResearchResourcesError("request_timeout", "The directory request exceeded its time budget.")
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ResearchResourcesError("response_too_large", "The directory response exceeded 8 MiB.")
                    body.extend(chunk)
                if time.monotonic() > deadline:
                    raise ResearchResourcesError("request_timeout", "The directory request exceeded its time budget.")
                return _parse_directory(json.loads(body))
        except requests.RequestException:
            raise ResearchResourcesError("network_error", "The public research directory request failed.") from None
        except (ValueError, UnicodeError, RecursionError):
            raise _invalid() from None
        finally:
            self.session.cookies.clear()

    def _directory(self) -> tuple[_Record, ...]:
        with self._lock:
            if self._snapshot is None or time.monotonic() >= self._expires_at:
                snapshot = self._load()
                self._snapshot = snapshot
                self._expires_at = time.monotonic() + CACHE_TTL_SECONDS
            return self._snapshot

    def search(self, query: str = "", subject: str | None = None, limit: int = 10) -> dict:
        """Return directory metadata, counts before limiting, and all subject labels.

        ``total_resources`` counts the complete directory. ``matched_count`` is
        after query and subject filtering. ``subjects`` covers the complete
        directory, allowing callers to offer filters even when no records match.
        Limits are 1..100, queries at most 500 characters and 32 meaningful terms,
        and subjects at most 256 characters. Invalid arguments raise ValueError.
        """
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be a string of at most 500 characters.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100.")
        if subject is not None and (not isinstance(subject, str) or not subject.strip() or len(subject) > 256):
            raise ValueError("subject must be None or a nonempty string of at most 256 characters.")
        terms = _tokens(query)
        subject_terms = _tokens(subject) if subject is not None else ()
        if (query.strip() and not terms) or len(terms) > 32 or (subject is not None and not subject_terms):
            raise ValueError("Search inputs must contain meaningful terms, with at most 32 query terms.")
        records = self._directory()
        ranked = []
        for record in records:
            if subject_terms and not any(all(_match(t, words) for t in subject_terms)
                                         for words in record.subject_aliases):
                continue
            score = _score(record, terms)
            if score:
                ranked.append((score, record))
        ranked.sort(key=lambda item: (-item[0], _fold(item[1].label), item[1].catalog_record_id))
        return {
            "resources": [record.public() for _, record in ranked[:limit]],
            "total_resources": len(records), "matched_count": len(ranked),
            "truncated": len(ranked) > limit,
            "subjects": sorted({s for r in records for s in r.subjects}, key=_fold),
            "query": query, "subject": subject,
            "source_url": DIRECTORY_PAGE_URL, "metadata_url": DIRECTORY_API_URL,
            "content_level": "directory_metadata", "entitlement": "not_checked",
            "full_text_read": False, "retrieval_authorized": False,
        }
