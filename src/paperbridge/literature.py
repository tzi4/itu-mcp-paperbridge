"""Bounded DOI discovery through Crossref and OpenAlex metadata.

OpenAlex wire format: https://help.openalex.org/api/
Work fields: https://help.openalex.org/data/works/attributes/
No search here establishes complete coverage or reads a paper's full text.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from html import unescape
import json
import os
import re
import threading
import time
import unicodedata

import requests

from .discovery import DiscoveryError, _safe_link
from .identifiers import doi_url, normalize_doi


_OPENALEX_URL = "https://api.openalex.org/works"
_WINDOW = 20
_MAX_BYTES = 4_000_000
_CACHE_SIZE = 64  # Per source and per client, never shared between accounts.
_SUCCESS_TTL = 300
_FAILURE_TTL = 15
_STREAM_SECONDS = 20
_ORDERS = {"relevance", "recent", "citations"}
_STOP_WORDS = frozenset("a an and are as at be by for from in into is of on or the to using via with".split())
# Crossref has no precise equivalents for review or preprint. Do not broaden them.
_KINDS = {
    "article": "journal-article", "book": "book", "book-chapter": "book-chapter",
    "conference-paper": "proceedings-article", "dataset": "dataset",
    "dissertation": "dissertation", "review": None, "preprint": None,
}


def _text(value, maximum=4000):
    return value.strip()[:maximum] if isinstance(value, str) and value.strip() else None


def _year(value):
    return value if type(value) is int and 1 <= value <= 9999 else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _tokens(value):
    # Crossref abstracts can contain JATS markup. Match text, never tag names or
    # attributes. Hyphenated compounds split into the same words as plain spaces.
    text = re.sub(r"<[^>]*>", " ", value or "")
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", unescape(text)).casefold())


def _stem(word):
    """Conservative English inflections, without substring or synonym matching."""
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ied"):
        return word[:-3] + "y"
    for ending in ("ing", "ed"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            word = word[:-len(ending)]
            if len(word) > 2 and word[-1] == word[-2] and word[-1] in "bdglmnprt":
                word = word[:-1]
            break
    return word


def _query_terms(query):
    # Do not count repeated words or inflections as separate pieces of evidence.
    terms = {}
    for token in _tokens(query):
        if token not in _STOP_WORDS:
            terms.setdefault(_stem(token), token)
    return list(terms.values())


def _query_match(terms, paper):
    title = set(_tokens(paper["title"] if paper["title"] != paper["doi"] else ""))
    abstract = set(_tokens(paper.get("abstract")))
    title_stems, abstract_stems = {_stem(t) for t in title}, {_stem(t) for t in abstract}
    title_terms, abstract_terms, exact, stemmed = [], [], [], []
    for term in terms:
        stem = _stem(term)
        if stem in title_stems:
            title_terms.append(term)
        if stem in abstract_stems:
            abstract_terms.append(term)
        if term in title or term in abstract:
            exact.append(term)
        elif stem in title_stems or stem in abstract_stems:
            stemmed.append(term)
    matched = [term for term in terms if term in exact or term in stemmed]
    # Two words must both match. Longer queries need at least two thirds of
    # their distinct content terms, so one generic term cannot carry a result.
    minimum = (2 * len(terms) + 2) // 3
    return {"method": "title_abstract_term_coverage", "query_terms": terms,
            "matched_terms": matched, "title_terms": title_terms, "abstract_terms": abstract_terms,
            "exact_terms": exact, "stemmed_terms": stemmed,
            "coverage": len(matched) / len(terms) if terms else 0.0,
            "minimum_matched_terms": minimum}


def _abstract(index):
    """Reconstruct a bounded, unambiguous inverted index without sparse allocation."""
    if not isinstance(index, dict) or not index or len(index) > 5000:
        return None
    words = {}
    for word, positions in index.items():
        if (not isinstance(word, str) or not word or len(word) > 1000
                or not isinstance(positions, list) or len(positions) > 5000):
            return None
        for position in positions:
            if (type(position) is not int or not 0 <= position < 5000
                    or position in words):
                return None
            words[position] = word
    if not words or sorted(words) != list(range(len(words))):
        return None
    return " ".join(words[i] for i in range(len(words)))[:20000]


class LiteratureSearch:
    """Search metadata with independent source caches and explicit partial failures.

    ``limit`` is an integer in 1..20. Years are inclusive integers in 1..9999.
    Orders are relevance, recent and citations. Kinds are article, book,
    book-chapter, conference-paper, dataset, dissertation, review and preprint.
    Sources are combined, crossref and openalex. Unsupported Crossref kinds are
    reported as unavailable. A completed source means one bounded request, never
    an exhaustive search. Discovery always uses provider relevance ranking.
    Title/abstract token coverage gates the merged metadata, using conservative
    English inflections. ``query_match`` explains the evidence on each result.
    Citation/date ordering applies within coverage groups of those windows.
    This lexical heuristic can miss synonyms, translations or missing abstracts.

    Source metadata includes received, accepted, discarded_doi, discarded_record,
    filtered, duplicates, cached and window_limit. Accepted counts precede DOI
    deduplication, metadata coverage gating and the final result limit.
    Citation counts retain their source.
    The optional session is dedicated to OpenAlex and stripped of ambient state.
    """

    def __init__(self, metadata_client, session=None):
        self.metadata_client = metadata_client
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        # A supplied requests session must not leak cookies, default query keys,
        # authentication, proxy credentials or hooks from a different service.
        if isinstance(self.session, requests.Session):
            self.session.auth = None
            self.session.headers.clear()
            self.session.params.clear()
            self.session.proxies.clear()
            self.session.cookies.clear()
            self.session.hooks = {"response": []}
            self.session.cert = None
            self.session.verify = True
        self._openalex_key = os.environ.get("PAPERBRIDGE_OPENALEX_KEY", "").strip()
        self._cache = {name: OrderedDict() for name in ("crossref", "openalex")}
        # Holding the source lock through its request also coalesces identical
        # concurrent queries without blocking requests to the other source.
        self._locks = {name: threading.Lock() for name in self._cache}

    @staticmethod
    def _validate(query, limit, year_from, year_to, order, kind, source):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Use a nonempty query of at most 1000 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer in 1..20.")
        for value in (year_from, year_to):
            if value is not None and _year(value) is None:
                raise ValueError("Years must be integers in 1..9999.")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("year_from must not exceed year_to.")
        if not isinstance(order, str) or order not in _ORDERS:
            raise ValueError("order must be relevance, recent or citations.")
        if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
            raise ValueError("Unsupported literature kind.")
        if not isinstance(source, str) or source not in {"combined", "crossref", "openalex"}:
            raise ValueError("source must be combined, crossref or openalex.")

    def search(self, query, limit=10, *, year_from=None, year_to=None,
               order="relevance", kind=None, source="combined") -> dict:
        self._validate(query, limit, year_from, year_to, order, kind, source)
        # All presentation orders reuse the same relevance-first discovery window.
        key = (query.strip(), year_from, year_to, "relevance", kind)
        sources, rows = {}, {}
        warnings = ["Searches use bounded source windows and do not establish complete date coverage."]
        for name in self._cache:
            if source not in ("combined", name):
                sources[name] = {"status": "disabled"}
                rows[name] = []
                continue
            papers, info, notes = self._source(name, key)
            sources[name], rows[name] = info, papers
            warnings.extend(notes)

        # Interleaving avoids comparing unrelated provider relevance scores.
        merged = OrderedDict()
        for rank in range(max(map(len, rows.values()))):
            for name in ("crossref", "openalex"):
                if rank >= len(rows[name]):
                    continue
                paper = rows[name][rank]
                if paper["doi"] in merged:
                    merged[paper["doi"]] = self._merge(merged[paper["doi"]], paper)
                else:
                    merged[paper["doi"]] = paper
        terms = _query_terms(query)
        results = []
        for paper in merged.values():
            evidence = _query_match(terms, paper)
            if terms and len(evidence["matched_terms"]) < evidence["minimum_matched_terms"]:
                continue
            paper["query_match"] = evidence
            results.append(paper)
        removed = len(merged) - len(results)
        warnings.append("Metadata relevance uses title/abstract term coverage and English inflections. "
                        "It can miss synonyms, translations or missing metadata.")
        if removed:
            warnings.append(f"Metadata query coverage excluded {removed} retrieved DOI records.")
        if not terms:
            warnings.append("No content terms were available for metadata relevance checking.")
        # Exact DOI merging has already let either provider supply an abstract.
        # Prefer fuller query coverage before comparing citations or reported years.
        coverage = lambda p: len(p["query_match"]["matched_terms"])
        if order == "recent":
            results.sort(key=lambda p: (-coverage(p), p["year"] is None, -(p["year"] or 0)))
            warnings.append("Date ordering uses reported years within metadata coverage groups "
                            "from relevance-ranked windows.")
        elif order == "citations":
            results.sort(key=lambda p: (-coverage(p), -(p.get("citation_count", -1))))
            warnings.append("Citation ordering is local to metadata coverage groups from "
                            "relevance-ranked windows, not a global most-cited search.")
            warnings.append("Citation counts from different indexes are not directly comparable.")
        else:
            # Stable ties preserve the interleaved provider ranking.
            results.sort(key=lambda p: (-coverage(p), -len(p["query_match"]["title_terms"])))
        return {"results": results[:limit], "warnings": warnings, "sources": sources}

    def _source(self, name, key):
        with self._locks[name]:
            cache = self._cache[name]
            now = time.monotonic()
            expired = [k for k, (until, _) in cache.items() if until <= now]
            for k in expired:
                del cache[k]
            if key in cache:
                cache.move_to_end(key)
                result = deepcopy(cache[key][1])
                result[1]["cached"] = True
                return result
            info = {"status": "unavailable", "cached": False, "window_limit": _WINDOW,
                    "received": 0, "accepted": 0, "discarded_doi": 0,
                    "discarded_record": 0, "filtered": 0, "duplicates": 0}
            try:
                raw = self._crossref(key) if name == "crossref" else self._openalex(key)
                result = self._papers(name, raw, key, info)
                ttl = _SUCCESS_TTL
            except (DiscoveryError, requests.RequestException, ValueError, TypeError,
                    KeyError, AttributeError, OverflowError, RecursionError):
                # Do not echo upstream bodies, exceptions, request URLs or keys.
                note = f"{name}: source unavailable or returned malformed metadata."
                if name == "crossref" and key[4] in {"review", "preprint"}:
                    note = "crossref: no precise type filter is available for this kind."
                result, ttl = ([], info, [note]), _FAILURE_TTL
            cache[key] = (time.monotonic() + ttl, deepcopy(result))
            while len(cache) > _CACHE_SIZE:
                cache.popitem(last=False)
            return result

    def _crossref(self, key):
        query, lower, upper, _, kind = key
        if self.metadata_client is None or (kind is not None and _KINDS[kind] is None):
            raise DiscoveryError("Crossref is unavailable for this request.")
        params = {"query.bibliographic": query, "rows": _WINDOW,
                  "sort": "relevance", "order": "desc"}
        filters = []
        if lower is not None:
            filters.append(f"from-pub-date:{lower:04d}-01-01")
        if upper is not None:
            filters.append(f"until-pub-date:{upper:04d}-12-31")
        if kind is not None:
            filters.append("type:" + _KINDS[kind])
        if filters:
            params["filter"] = ",".join(filters)
        if self.metadata_client.email:
            params["mailto"] = self.metadata_client.email
        data = self.metadata_client._get("https://api.crossref.org/works", params)
        items = data["message"]["items"]
        if not isinstance(items, list):
            raise DiscoveryError("Invalid Crossref result list.")
        return items

    def _openalex(self, key):
        query, lower, upper, _, kind = key
        params = {"search": query, "per_page": _WINDOW,
                  "select": "id,doi,title,authorships,publication_year,primary_location,"
                            "abstract_inverted_index,cited_by_count,type"}
        filters = []
        if lower is not None:
            filters.append(f"from_publication_date:{lower:04d}-01-01")
        if upper is not None:
            filters.append(f"to_publication_date:{upper:04d}-12-31")
        if kind is not None:
            filters.append("type:" + kind)
        if filters:
            params["filter"] = ",".join(filters)
        params["sort"] = "relevance_score:desc"
        headers = {"User-Agent": "PaperBridge/0.1 (scholarly metadata)", "Accept": "application/json"}
        if self._openalex_key:
            # Invalid header bytes must fail without exposing the secret.
            if not re.fullmatch(r"[\x21-\x7e]{1,4096}", self._openalex_key):
                raise DiscoveryError("Invalid OpenAlex key configuration.")
            headers["Authorization"] = "Bearer " + self._openalex_key
        deadline = time.monotonic() + _STREAM_SECONDS
        with self.session.get(_OPENALEX_URL, params=params, headers=headers, auth=(),
                              proxies={}, timeout=(3.05, 10), allow_redirects=False,
                              stream=True) as response:
            if response.status_code != 200:
                raise DiscoveryError("OpenAlex request was unsuccessful.")
            length = response.headers.get("Content-Length")
            if length is not None and (not str(length).isdigit() or int(length) > _MAX_BYTES):
                raise DiscoveryError("Invalid or excessive response size.")
            body = bytearray()
            for part in response.iter_content(65536):
                if time.monotonic() > deadline:
                    raise DiscoveryError("OpenAlex response exceeded its time budget.")
                if len(body) + len(part) > _MAX_BYTES:
                    raise DiscoveryError("OpenAlex response exceeded 4 MB.")
                body.extend(part)
            data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise DiscoveryError("Invalid OpenAlex result list.")
        return data["results"]

    def _papers(self, name, raw, key, info):
        _, lower, upper, _, kind = key
        papers, seen = [], set()
        info["received"] = len(raw)
        for work in raw[:_WINDOW]:
            if not isinstance(work, dict):
                info["discarded_record"] += 1
                continue
            try:
                value = work.get("DOI" if name == "crossref" else "doi")
                if not isinstance(value, str):
                    raise ValueError("Missing DOI.")
                doi = normalize_doi(value)
            except (ValueError, TypeError, AttributeError):
                info["discarded_doi"] += 1
                continue
            try:
                paper = (self.metadata_client._paper(work) if name == "crossref"
                         else self._openalex_paper(work, doi))
                paper["year"] = _year(paper.get("year"))
                paper["title"] = _text(paper.get("title")) or doi
                paper["publisher"] = _text(paper.get("publisher"))
                paper["publisher_url"] = _safe_link(paper.get("publisher_url"))
                paper["authors"] = [text for a in paper.get("authors", [])[:20]
                                    if (text := _text(a, 500))]
                paper.update(id="doi:" + doi, doi=doi, url=doi_url(doi),
                             content_level="metadata_only", full_text_read=False)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                info["discarded_record"] += 1
                continue
            year = paper["year"]
            expected = _KINDS[kind] if name == "crossref" and kind is not None else kind
            if ((lower is not None and (year is None or year < lower))
                    or (upper is not None and (year is None or year > upper))
                    or (kind is not None and work.get("type") != expected)):
                info["filtered"] += 1
                continue
            if name == "crossref":
                citation_count = _count(work.get("is-referenced-by-count"))
                if citation_count is not None:
                    paper["citation_count"] = citation_count
                abstract = _text(work.get("abstract"), 20000)
                if abstract:
                    paper["abstract"] = abstract
            paper["sources"] = [name]
            paper["field_sources"] = {field: name for field, value in paper.items()
                                      if field not in {"sources", "content_level", "full_text_read"}
                                      and value is not None and value != []}
            info["accepted"] += 1
            if doi in seen:
                info["duplicates"] += 1
                continue
            seen.add(doi)
            papers.append(paper)
        info["status"] = "completed"
        notes = []
        if name == "crossref" and kind == "article":
            notes.append("crossref: article maps to journal-article, a broader provider classification.")
        if info["discarded_doi"] or info["discarded_record"]:
            notes.append(f"{name}: discarded records with missing/invalid DOIs or malformed metadata.")
        if len(raw) > _WINDOW:
            notes.append(f"{name}: response records beyond the source window were ignored.")
        return papers, info, notes

    @staticmethod
    def _openalex_paper(work, doi):
        location = work.get("primary_location")
        location = location if isinstance(location, dict) else {}
        source = location.get("source")
        source = source if isinstance(source, dict) else {}
        authorships = work.get("authorships")
        authors = []
        for entry in authorships[:20] if isinstance(authorships, list) else []:
            author = entry.get("author") if isinstance(entry, dict) else None
            if isinstance(author, dict) and (name := _text(author.get("display_name"), 500)):
                authors.append(name)
        # A repository's host organization is an institution, not a publisher.
        publisher_id = source.get("host_organization")
        publisher = (source.get("host_organization_name") if isinstance(publisher_id, str)
                     and re.fullmatch(r"https://openalex\.org/P[1-9][0-9]*", publisher_id) else None)
        paper = {"id": "doi:" + doi, "doi": doi, "title": work.get("title"),
                 "url": doi_url(doi), "authors": authors, "year": work.get("publication_year"),
                 "publisher": publisher, "publisher_url": _safe_link(location.get("landing_page_url")),
                 "content_level": "metadata_only", "full_text_read": False}
        identifier = work.get("id")
        if isinstance(identifier, str) and re.fullmatch(r"https://openalex\.org/W[1-9][0-9]*", identifier):
            paper["openalex_id"] = identifier
        abstract = _abstract(work.get("abstract_inverted_index"))
        if abstract:
            paper["abstract"] = abstract
        citation_count = _count(work.get("cited_by_count"))
        if citation_count is not None:
            paper["citation_count"] = citation_count
        return paper

    @staticmethod
    def _merge(first, second):
        # Source order in the rank interleave must not change field precedence.
        if "crossref" in second["sources"] and "crossref" not in first["sources"]:
            first, second = second, first
        for field, value in second.items():
            if field in {"sources", "field_sources", "licenses"}:
                continue
            missing = first.get(field) in (None, "", [])
            if field == "title" and first.get(field) == first["doi"]:
                missing = True
            # Keep Crossref publisher/license evidence intact even if missing.
            if field in {"publisher", "publisher_url"} and "crossref" in first["sources"]:
                continue
            if (missing or field in {"openalex_id", "abstract", "citation_count"}) and value is not None:
                first[field] = deepcopy(value)
                if field in second["field_sources"]:
                    first["field_sources"][field] = second["field_sources"][field]
        first["sources"] = [name for name in ("crossref", "openalex")
                            if name in first["sources"] or name in second["sources"]]
        return first
