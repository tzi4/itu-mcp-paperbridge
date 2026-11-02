"""Offline contract and transport tests for scholarly metadata discovery."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

import requests

from paperbridge.discovery import MetadataClient
from paperbridge.literature import LiteratureSearch


def crossref(doi="10.1234/shared", **changes):
    work = {"DOI": doi, "title": ["Crossref query title"], "author": [{"given": "Ada", "family": "Lovelace"}],
            "published": {"date-parts": [[2024, 3, 1]]}, "type": "journal-article",
            "publisher": "Crossref publisher", "is-referenced-by-count": 7,
            "resource": {"primary": {"URL": "https://publisher.example/paper"}},
            "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/",
                         "content-version": "vor"}]}
    work.update(changes)
    return work


def openalex(doi="https://doi.org/10.1234/shared", **changes):
    work = {"doi": doi, "id": "https://openalex.org/W1234", "title": "OpenAlex query title",
            "type": "article", "publication_year": 2023, "cited_by_count": 42,
            "authorships": [{"author": {"display_name": "Ada L."}}],
            "abstract_inverted_index": {"Metadata": [0], "helps": [1], "discovery.": [2]},
            "primary_location": {"landing_page_url": "https://other.example/paper",
                                 "source": {"host_organization": "https://openalex.org/P123",
                                            "host_organization_name": "Other publisher"}}}
    work.update(changes)
    return work


class CrossrefClient(MetadataClient):
    def __init__(self, records=None, payload=None, error=None):
        # Use the real existing parser without creating an HTTP session.
        self.email = None
        self.calls = []
        self.payload = payload if payload is not None else {"message": {"items": records or []}}
        self.error = error

    def _get(self, url, params):
        self.calls.append((url, deepcopy(params)))
        if self.error:
            raise self.error
        return deepcopy(self.payload)


class Response:
    def __init__(self, payload=None, *, body=None, status=200, headers=None, chunks=None):
        self.body = body if body is not None else json.dumps(payload or {"results": []}).encode()
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False
        self.read = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        self.read = True
        if self.chunks is not None:
            yield from self.chunks
        else:
            for start in range(0, len(self.body), size):
                yield self.body[start:start + size]


class Session:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        if self.error:
            raise self.error
        return self.response


class LiteratureTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"PAPERBRIDGE_OPENALEX_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def engine(self, crossref_records=None, openalex_records=None):
        metadata = CrossrefClient(crossref_records)
        session = Session(Response({"results": openalex_records or []}))
        return LiteratureSearch(metadata, session), metadata, session

    def test_combined_identity_provenance_and_crossref_reading_evidence(self):
        engine, _, _ = self.engine([crossref("10.1234/SHARED")], [openalex()])
        result = engine.search("query")
        self.assertEqual(len(result["results"]), 1)
        paper = result["results"][0]
        self.assertEqual((paper["id"], paper["doi"], paper["url"]),
                         ("doi:10.1234/shared", "10.1234/shared", "https://doi.org/10.1234/shared"))
        self.assertEqual(paper["title"], "Crossref query title")
        self.assertEqual(paper["authors"], ["Ada Lovelace"])
        self.assertEqual(paper["year"], 2024)
        self.assertEqual(paper["publisher"], "Crossref publisher")
        self.assertEqual(paper["publisher_url"], "https://publisher.example/paper")
        self.assertEqual(paper["licenses"], [{"url": "https://creativecommons.org/licenses/by/4.0/",
                                              "content_version": "vor", "effective_from": None}])
        self.assertEqual(paper["abstract"], "Metadata helps discovery.")
        self.assertEqual(paper["citation_count"], 42)
        self.assertEqual(paper["openalex_id"], "https://openalex.org/W1234")
        self.assertEqual(paper["sources"], ["crossref", "openalex"])
        self.assertEqual(paper["field_sources"]["licenses"], "crossref")
        self.assertEqual(paper["field_sources"]["publisher_url"], "crossref")
        self.assertEqual(paper["field_sources"]["citation_count"], "openalex")
        self.assertEqual(paper["field_sources"]["abstract"], "openalex")
        self.assertEqual(paper["content_level"], "metadata_only")
        self.assertIs(paper["full_text_read"], False)

    def test_crossref_precedence_when_openalex_rank_is_earlier(self):
        engine, _, _ = self.engine([crossref("10.1234/other"), crossref()], [openalex()])
        paper = next(p for p in engine.search("query")["results"] if p["doi"] == "10.1234/shared")
        self.assertEqual(paper["title"], "Crossref query title")
        self.assertEqual(paper["field_sources"]["title"], "crossref")
        self.assertEqual(paper["field_sources"]["abstract"], "openalex")

    def test_missing_crossref_evidence_is_not_manufactured_from_openalex(self):
        engine, _, _ = self.engine([crossref(publisher=None, resource={}, license=[])], [openalex()])
        paper = engine.search("query")["results"][0]
        self.assertIsNone(paper["publisher"])
        self.assertIsNone(paper["publisher_url"])
        self.assertEqual(paper["licenses"], [])

    def test_missing_bibliographic_fields_are_enriched_with_field_provenance(self):
        engine, _, _ = self.engine([crossref(title=[], author=[], published={})], [openalex()])
        paper = engine.search("query")["results"][0]
        for field, value in (("title", "OpenAlex query title"), ("authors", ["Ada L."]), ("year", 2023)):
            self.assertEqual(paper[field], value)
            self.assertEqual(paper["field_sources"][field], "openalex")

    def test_openalex_does_not_treat_repository_host_as_publisher(self):
        record = openalex(primary_location={"source": {"host_organization": "https://openalex.org/I123",
                                                       "host_organization_name": "University"}})
        engine, metadata, _ = self.engine(openalex_records=[record])
        result = engine.search("query", source="openalex")
        self.assertIsNone(result["results"][0]["publisher"])
        self.assertEqual(metadata.calls, [])
        self.assertEqual(result["sources"]["crossref"]["status"], "disabled")

    def test_doi_discard_counts_duplicates_and_malformed_records(self):
        engine, _, _ = self.engine(
            [crossref(), crossref("10.1234/SHARED"), crossref(None), crossref("bad"),
             crossref(author=[None]), None],
            [openalex(), openalex(None), openalex("bad"), 42])
        result = engine.search("query")
        self.assertEqual(len(result["results"]), 1)
        info = result["sources"]["crossref"]
        self.assertEqual((info["received"], info["accepted"], info["discarded_doi"],
                          info["discarded_record"], info["duplicates"]), (6, 2, 2, 2, 1))
        info = result["sources"]["openalex"]
        self.assertEqual((info["received"], info["discarded_doi"], info["discarded_record"]), (4, 2, 1))
        self.assertTrue(any("discarded" in warning for warning in result["warnings"]))

    def test_exact_validation_before_any_source_request(self):
        engine, metadata, session = self.engine()
        cases = [{"query": None}, {"query": 1}, {"query": " "}, {"query": "a" * 1001},
                 {"limit": True}, {"limit": "10"}, {"limit": 1.5}, {"limit": 0}, {"limit": 21},
                 {"year_from": True}, {"year_from": "2024"}, {"year_to": 0}, {"year_to": 10000},
                 {"year_from": 2025, "year_to": 2024}, {"kind": "ARTICLE"}, {"kind": []},
                 {"order": None}, {"order": []}, {"order": "newest"}, {"order": "oldest"},
                 {"source": []}, {"source": "Combined"}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.search(**({"query": "test"} | changes))
        self.assertEqual(metadata.calls, [])
        self.assertEqual(session.calls, [])

    def test_filters_sent_and_enforced_against_source_records(self):
        engine, metadata, session = self.engine(
            [crossref("10.1234/ok"), crossref("10.1234/old", published={"date-parts": [[2022]]}),
             crossref("10.1234/missing", published={}), crossref("10.1234/type", type="book")],
            [openalex("10.1234/oa", publication_year=2024), openalex("10.1234/old", publication_year=2022),
             openalex("10.1234/future", publication_year=2025), openalex("10.1234/type", type="book")])
        result = engine.search("query", year_from=2024, year_to=2024, order="recent", kind="article")
        self.assertEqual({p["doi"] for p in result["results"]}, {"10.1234/ok", "10.1234/oa"})
        self.assertEqual(result["sources"]["crossref"]["filtered"], 3)
        self.assertEqual(result["sources"]["openalex"]["filtered"], 3)
        self.assertEqual(metadata.calls[0][1]["filter"],
                         "from-pub-date:2024-01-01,until-pub-date:2024-12-31,type:journal-article")
        params = session.calls[0][1]["params"]
        self.assertEqual(params["filter"],
                         "from_publication_date:2024-01-01,to_publication_date:2024-12-31,type:article")
        self.assertEqual(params["sort"], "relevance_score:desc")
        self.assertTrue(any("do not establish complete date coverage" in w for w in result["warnings"]))

    def test_one_sided_year_filters_and_unknown_year(self):
        for bounds, expected in [({"year_from": 2024}, [2025]), ({"year_to": 2024}, [2023])]:
            with self.subTest(bounds=bounds):
                engine, _, _ = self.engine(openalex_records=[openalex("10.1234/a", publication_year=2023),
                                                            openalex("10.1234/b", publication_year=2025),
                                                            openalex("10.1234/c", publication_year=None)])
                result = engine.search("query", source="openalex", **bounds)
                self.assertEqual([p["year"] for p in result["results"]], expected)

    def test_supported_kind_mappings_and_crossref_unsupported_kind(self):
        mapping = {"article": "journal-article", "book": "book", "book-chapter": "book-chapter",
                   "conference-paper": "proceedings-article", "dataset": "dataset",
                   "dissertation": "dissertation", "review": None, "preprint": None}
        for kind, crossref_type in mapping.items():
            with self.subTest(kind=kind):
                engine, metadata, _ = self.engine([crossref(type=crossref_type)], [openalex(type=kind)])
                result = engine.search("query", kind=kind)
                if crossref_type is None:
                    self.assertEqual(metadata.calls, [])
                    self.assertEqual(result["sources"]["crossref"]["status"], "unavailable")
                    self.assertTrue(any("precise type filter" in w for w in result["warnings"]))
                else:
                    self.assertIn("type:" + crossref_type, metadata.calls[0][1]["filter"])
                    self.assertEqual(result["sources"]["crossref"]["status"], "completed")
                self.assertEqual(result["sources"]["openalex"]["status"], "completed")

    def test_ordering_and_limit_apply_after_merge(self):
        records = [openalex("10.1234/a", publication_year=2024, cited_by_count=2),
                   openalex("10.1234/b", publication_year=None, cited_by_count=5),
                   openalex("10.1234/c", publication_year=2020, cited_by_count=20)]
        for order, expected in [("recent", ["a", "c", "b"]),
                                ("citations", ["c", "b", "a"]), ("relevance", ["a", "b", "c"])]:
            with self.subTest(order=order):
                engine, _, session = self.engine(openalex_records=records)
                result = engine.search("query", limit=2, order=order, source="openalex")
                self.assertEqual([p["doi"].split("/")[-1] for p in result["results"]], expected[:2])
                self.assertEqual(session.calls[0][1]["params"]["per_page"], 20)

    def test_relevance_interleaves_without_comparing_provider_scores(self):
        engine, _, _ = self.engine([crossref("10.1234/c1"), crossref("10.1234/c2")],
                                  [openalex("10.1234/o1"), openalex("10.1234/o2")])
        result = engine.search("query", limit=3)
        self.assertEqual([p["doi"] for p in result["results"]],
                         ["10.1234/c1", "10.1234/o1", "10.1234/c2"])

    def test_spacecraft_query_rejects_unrelated_high_citations_before_local_ordering(self):
        engine, metadata, session = self.engine([
            crossref("10.1234/diffusion", title=["Adding Conditional Control to Text-to-Image Diffusion Models"],
                     **{"is-referenced-by-count": 500000}),
            crossref("10.1234/fastp", title=["fastp: FASTQ preprocessing"],
                     **{"is-referenced-by-count": 200000}),
            crossref("10.1234/biofilms", title=["Biofilms"],
                     abstract="<jats:p>Control of biofilm formation.</jats:p>",
                     **{"is-referenced-by-count": 100000}),
            crossref("10.1234/partial", title=["Attitude control for robots"],
                     **{"is-referenced-by-count": 90000}),
            crossref("10.1234/shared", title=["Spacecraft attitude control with reaction wheels"],
                     **{"is-referenced-by-count": 130}),
            crossref("10.1234/observer", title=["Observer-based spacecraft attitude control"],
                     **{"is-referenced-by-count": 80}),
        ], [
            openalex("10.1234/shared", title="Spacecraft attitude control with reaction wheels",
                     publication_year=2024, cited_by_count=150),
            openalex("10.1234/robust", title="Robust attitude controls for spacecraft",
                     publication_year=2023, cited_by_count=1000),
            openalex("10.1234/nonlinear", title="Nonlinear stabilization with reaction wheels",
                     abstract_inverted_index={"Spacecraft": [0], "attitude": [1], "control": [2]},
                     publication_year=2023, cited_by_count=300),
            openalex("10.1234/adaptive", title="Adaptive control of spacecraft attitude",
                     publication_year=2024, cited_by_count=200),
        ])
        result = engine.search("spacecraft attitude control", limit=5, year_from=2022,
                               order="citations", source="combined")
        self.assertEqual([p["doi"] for p in result["results"]], [
            "10.1234/robust", "10.1234/nonlinear", "10.1234/adaptive",
            "10.1234/shared", "10.1234/observer"])
        self.assertTrue(all(p["query_match"]["coverage"] == 1 for p in result["results"]))
        self.assertEqual(result["results"][0]["query_match"]["stemmed_terms"], ["control"])
        self.assertEqual(result["results"][1]["query_match"]["title_terms"], [])
        shared = result["results"][3]
        self.assertEqual(shared["sources"], ["crossref", "openalex"])
        self.assertEqual(shared["field_sources"]["citation_count"], "openalex")
        self.assertEqual(shared["publisher"], "Crossref publisher")
        self.assertTrue(shared["licenses"])
        self.assertEqual(metadata.calls[0][1]["sort"], "relevance")
        self.assertEqual(session.calls[0][1]["params"]["sort"], "relevance_score:desc")
        self.assertTrue(all(s["status"] == "completed" for s in result["sources"].values()))
        self.assertTrue(any("excluded 3" in w for w in result["warnings"]))
        self.assertTrue(any("not a global most-cited search" in w for w in result["warnings"]))

    def test_query_match_handles_exact_tokens_inflections_hyphens_and_stop_words(self):
        engine, _, _ = self.engine([
            crossref("10.1234/exact", title=["Spacecraft attitude control"]),
            crossref("10.1234/stem", title=["Controlling spacecraft attitudes"]),
            crossref("10.1234/substring", title=["Spacecraft latitudes and uncontrollable motion"]),
        ])
        result = engine.search("Control of SPACECRAFT attitude-control", source="crossref")
        self.assertEqual([p["doi"] for p in result["results"]], ["10.1234/exact", "10.1234/stem"])
        exact, stemmed = [p["query_match"] for p in result["results"]]
        self.assertEqual(exact["query_terms"], ["control", "spacecraft", "attitude"])
        self.assertEqual(exact["exact_terms"], ["control", "spacecraft", "attitude"])
        self.assertEqual(stemmed["stemmed_terms"], ["control", "attitude"])
        self.assertEqual(stemmed["coverage"], 1)

    def test_abstract_enrichment_can_establish_coverage_after_doi_merge(self):
        engine, _, _ = self.engine([
            crossref(title=["Reaction wheel stabilization"],
                     abstract="<jats:p>Spacecraft dynamics.</jats:p>"),
        ], [openalex(title="Reaction wheel stabilization", abstract_inverted_index={
            "Attitude": [0], "control": [1], "of": [2], "spacecraft": [3]})])
        result = engine.search("spacecraft attitude control")
        self.assertEqual(len(result["results"]), 1)
        paper = result["results"][0]
        self.assertEqual(paper["query_match"]["title_terms"], [])
        self.assertEqual(paper["query_match"]["coverage"], 1)
        self.assertEqual(paper["field_sources"]["abstract"], "openalex")
        self.assertEqual(paper["sources"], ["crossref", "openalex"])
        self.assertIs(paper["full_text_read"], False)

    def test_relevance_uses_visible_title_abstract_not_authors_publishers_or_jats_attributes(self):
        engine, _, _ = self.engine([
            crossref(title=["Biofilms"], abstract='<jats:p label="spacecraft attitude">Control.</jats:p>',
                     author=[{"family": "Spacecraft attitude"}], publisher="Spacecraft attitude control"),
            crossref("10.1234/abstract", title=["Reaction wheels"],
                     abstract="<jats:p>Spacecraft attitude &amp; control.</jats:p>"),
        ])
        result = engine.search("spacecraft attitude control", source="crossref")
        self.assertEqual([p["doi"] for p in result["results"]], ["10.1234/abstract"])
        self.assertEqual(result["results"][0]["query_match"]["abstract_terms"],
                         ["spacecraft", "attitude", "control"])

    def test_empty_relevance_subset_keeps_source_states_and_explains_lexical_limits(self):
        engine, _, _ = self.engine([crossref(title=["Unrelated control"])], [
            openalex("10.1234/other", title="Other topic", abstract_inverted_index=None)])
        result = engine.search("spacecraft attitude control")
        self.assertEqual(result["results"], [])
        self.assertTrue(all(s["status"] == "completed" for s in result["sources"].values()))
        self.assertTrue(all(s["accepted"] == 1 for s in result["sources"].values()))
        self.assertTrue(any("excluded 2" in w for w in result["warnings"]))
        self.assertTrue(any("miss synonyms" in w for w in result["warnings"]))

    def test_all_orders_reuse_relevance_first_windows_and_recompute_local_order(self):
        engine, metadata, session = self.engine([
            crossref("10.1234/new", title=["Spacecraft attitude control"],
                     published={"date-parts": [[2025]]}, **{"is-referenced-by-count": 5})], [
            openalex("10.1234/cited", title="Spacecraft attitude control", publication_year=2023,
                     cited_by_count=500)])
        cited = engine.search("spacecraft attitude control", order="citations")
        recent = engine.search("spacecraft attitude control", order="recent")
        engine.search("spacecraft attitude control", order="relevance")
        self.assertEqual(cited["results"][0]["doi"], "10.1234/cited")
        self.assertEqual(recent["results"][0]["doi"], "10.1234/new")
        self.assertEqual((len(metadata.calls), len(session.calls)), (1, 1))
        self.assertTrue(all(s["cached"] for s in recent["sources"].values()))
        self.assertEqual(metadata.calls[0][1]["sort"], "relevance")
        self.assertEqual(session.calls[0][1]["params"]["sort"], "relevance_score:desc")

    def test_malformed_envelopes_are_source_failures(self):
        for payload in [[], {"results": None}, {"results": {}}, {"message": "bad"}]:
            with self.subTest(payload=payload):
                session = Session(Response(body=json.dumps(payload).encode()))
                result = LiteratureSearch(CrossrefClient([crossref()]), session).search("query")
                self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
                self.assertEqual(result["sources"]["crossref"]["status"], "completed")
                self.assertEqual(len(result["results"]), 1)
        for payload in [{"message": None}, {"message": {"items": "bad"}}, {"message": []}]:
            with self.subTest(payload=payload):
                result = LiteratureSearch(CrossrefClient(payload=payload), Session()).search("query")
                self.assertEqual(result["sources"]["crossref"]["status"], "unavailable")
                self.assertEqual(result["sources"]["openalex"]["status"], "completed")

    def test_empty_completed_and_explicit_disabled_sources(self):
        engine, metadata, session = self.engine()
        result = engine.search("query", source="crossref")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["sources"]["crossref"]["status"], "completed")
        self.assertEqual(result["sources"]["openalex"]["status"], "disabled")
        self.assertEqual(session.calls, [])
        self.assertEqual(len(metadata.calls), 1)

    def test_partial_and_total_outages_hide_secret_errors(self):
        secret = "https://api.openalex.org/works?api_key=private-key"
        engine = LiteratureSearch(CrossrefClient([crossref()]),
                                  Session(error=requests.Timeout(secret)))
        partial = engine.search("query")
        self.assertEqual(len(partial["results"]), 1)
        self.assertEqual(partial["sources"]["openalex"]["status"], "unavailable")
        self.assertNotIn(secret, repr(partial))
        engine = LiteratureSearch(CrossrefClient(error=requests.ConnectionError(secret)),
                                  Session(error=requests.Timeout(secret)))
        total = engine.search("query")
        self.assertEqual(total["results"], [])
        self.assertTrue(all(v["status"] == "unavailable" for v in total["sources"].values()))
        self.assertNotIn("private-key", repr(total))

    def test_crossref_outage_keeps_openalex_results_and_caches_sources_independently(self):
        metadata = CrossrefClient(error=requests.Timeout("private URL"))
        session = Session(Response({"results": [openalex()]}))
        engine = LiteratureSearch(metadata, session)
        first = engine.search("query")
        self.assertEqual(first["results"][0]["sources"], ["openalex"])
        self.assertEqual(first["sources"]["crossref"]["status"], "unavailable")
        second = engine.search("query")
        self.assertEqual(second["results"], first["results"])
        self.assertEqual((len(metadata.calls), len(session.calls)), (1, 1))

    def test_provider_credentials_and_contact_details_are_not_shared(self):
        with patch.dict("os.environ", {"PAPERBRIDGE_OPENALEX_KEY": "openalex-secret"}):
            engine, metadata, session = self.engine()
        metadata.email = "contact@example.org"
        engine.search("query")
        self.assertEqual(metadata.calls[0][0], "https://api.crossref.org/works")
        self.assertEqual(metadata.calls[0][1]["mailto"], "contact@example.org")
        self.assertNotIn("openalex-secret", repr(metadata.calls))
        self.assertNotIn("contact@example.org", repr(session.calls))

    def test_openalex_transport_has_exact_origin_no_redirects_and_header_key(self):
        with patch.dict("os.environ", {"PAPERBRIDGE_OPENALEX_KEY": "test-secret"}):
            engine, metadata, session = self.engine(openalex_records=[openalex()])
        result = engine.search("words & symbols?", source="openalex")
        url, options = session.calls[0]
        self.assertEqual(url, "https://api.openalex.org/works")
        self.assertFalse(session.trust_env)
        self.assertIs(options["allow_redirects"], False)
        self.assertIs(options["stream"], True)
        self.assertEqual(options["auth"], ())
        self.assertEqual(options["proxies"], {})
        self.assertEqual(options["timeout"], (3.05, 10))
        self.assertEqual(options["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(options["headers"]["Accept"], "application/json")
        self.assertNotIn("test-secret", repr(options["params"]))
        self.assertNotIn("test-secret", repr(result))
        self.assertEqual(metadata.calls, [])

    def test_requests_session_is_cleared_of_ambient_state(self):
        session = requests.Session()
        self.addCleanup(session.close)
        session.auth = ("user", "secret")
        session.headers["Authorization"] = "Bearer inherited"
        session.headers["Proxy-Authorization"] = "Basic inherited"
        session.params["api_key"] = "inherited"
        session.proxies = {"https": "https://proxy.example"}
        session.cookies.set("private", "cookie")
        session.hooks["response"].append(lambda response, *a, **kw: response)
        engine = LiteratureSearch(None, session)
        observed = []

        def send(request, **kwargs):
            observed.append((request, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"results": []}'
            response._content_consumed = True
            return response

        with patch.dict("os.environ", {"HTTPS_PROXY": "https://ambient.example", "NETRC": "/missing"}), \
                patch.object(session, "send", side_effect=send):
            engine.search("query", source="openalex")
        request, options = observed[0]
        self.assertTrue(request.url.startswith("https://api.openalex.org/works?"))
        self.assertNotIn("inherited", request.url)
        for header in ("Authorization", "Proxy-Authorization", "Cookie"):
            self.assertNotIn(header, request.headers)
        self.assertEqual(options["proxies"], {})
        self.assertTrue(options["verify"])
        self.assertIsNone(session.auth)
        self.assertEqual(session.hooks, {"response": []})

    def test_redirects_and_http_errors_do_not_read_or_follow_response(self):
        for status in (301, 302, 307, 401, 403, 429, 500):
            with self.subTest(status=status):
                response = Response(status=status, headers={"Location": "https://evil.example/secret"})
                session = Session(response)
                result = LiteratureSearch(None, session).search("query", source="openalex")
                self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
                self.assertEqual(len(session.calls), 1)
                self.assertFalse(response.read)
                self.assertTrue(response.closed)

    def test_stream_bounds_even_when_content_length_lies(self):
        responses = [Response(headers={"Content-Length": "4000001"}),
                     Response(headers={"Content-Length": "invalid"}),
                     Response(headers={"Content-Length": "1"}, chunks=[b" " * 65536] * 62),
                     Response(chunks=[b"x" * 4_000_001]), Response(body=b"not json"),
                     Response(body=b"\xff"), Response(body=b"[" * 2000 + b"]" * 2000)]
        for response in responses:
            with self.subTest(length=len(response.body)):
                result = LiteratureSearch(None, Session(response)).search("query", source="openalex")
                self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
                self.assertTrue(response.closed)
        body = b'{"results": []}'
        response = Response(body=body + b" " * (4_000_000 - len(body)))
        result = LiteratureSearch(None, Session(response)).search("query", source="openalex")
        self.assertEqual(result["sources"]["openalex"]["status"], "completed")

    def test_stream_deadline_limits_slow_trickles(self):
        clock = [0]

        def slow_chunks():
            yield b'{"results": '
            clock[0] = 21
            yield b"[]}"

        response = Response(chunks=slow_chunks())
        with patch("paperbridge.literature.time.monotonic", side_effect=lambda: clock[0]):
            result = LiteratureSearch(None, Session(response)).search("query", source="openalex")
        self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
        self.assertTrue(response.closed)

    def test_midstream_transport_failure_closes_response_without_leaking_error(self):
        def broken_chunks():
            yield b'{"results": '
            raise requests.exceptions.ChunkedEncodingError("private response and URL")

        response = Response(chunks=broken_chunks())
        session = Session(response)
        engine = LiteratureSearch(None, session)
        result = engine.search("query", source="openalex")
        self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
        self.assertEqual(result["results"], [])
        self.assertNotIn("private response", repr(result))
        self.assertTrue(response.closed)
        cached = engine.search("query", source="openalex")
        self.assertTrue(cached["sources"]["openalex"]["cached"])
        self.assertEqual(len(session.calls), 1)

    def test_invalid_key_is_unavailable_without_request_or_secret_echo(self):
        with patch.dict("os.environ", {"PAPERBRIDGE_OPENALEX_KEY": "secret\r\ninjected"}):
            engine, _, session = self.engine()
        result = engine.search("query", source="openalex")
        self.assertEqual(session.calls, [])
        self.assertEqual(result["sources"]["openalex"]["status"], "unavailable")
        self.assertNotIn("secret", repr(result))

    def test_malformed_optional_fields_do_not_invent_metadata(self):
        malformed = [None, [], {"word": [True]}, {"word": [-1]}, {"word": [5000]},
                     {"a": [0], "b": [0]}, {"word": [1]}, {"word": "0"}]
        for index in malformed:
            with self.subTest(index=index):
                record = openalex(abstract_inverted_index=index, cited_by_count=True,
                                  publication_year="2024", id="https://evil.example/W1",
                                  primary_location={"landing_page_url": "https://127.0.0.1/private"},
                                  authorships=[None, {"author": None}, {"author": {"display_name": 42}}])
                engine, _, session = self.engine(openalex_records=[record])
                paper = engine.search("query", source="openalex")["results"][0]
                for field in ("abstract", "citation_count", "openalex_id"):
                    self.assertNotIn(field, paper)
                self.assertIsNone(paper["year"])
                self.assertIsNone(paper["publisher_url"])
                self.assertEqual(paper["authors"], [])
                self.assertEqual(len(session.calls), 1)

    def test_repeated_abstract_words_preserve_positions_and_false_claims_stay_metadata(self):
        engine, _, _ = self.engine(openalex_records=[openalex(
            abstract_inverted_index={"a": [0, 2], "study": [1], "result": [3]},
            open_access={"is_oa": True}, has_fulltext=True,
            content_urls={"pdf": "https://content.openalex.org/private"})])
        paper = engine.search("query", source="openalex")["results"][0]
        self.assertEqual(paper["abstract"], "a study a result")
        self.assertEqual(paper["content_level"], "metadata_only")
        self.assertIs(paper["full_text_read"], False)
        self.assertNotIn("content_urls", paper)

    def test_cache_avoids_network_across_modes_and_limits_and_returns_copies(self):
        engine, metadata, session = self.engine([crossref()], [openalex()])
        first = engine.search(" query ", limit=1)
        first["results"][0]["title"] = "tampered"
        first["results"][0]["licenses"].clear()
        first["sources"]["crossref"]["status"] = "tampered"
        metadata.error = requests.ConnectionError("network disabled")
        session.error = requests.ConnectionError("network disabled")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")):
            cached = engine.search("query", limit=20)
            engine.search("query", source="openalex")
            engine.search("query", source="crossref")
        self.assertEqual(cached["results"][0]["title"], "Crossref query title")
        self.assertEqual(len(cached["results"][0]["licenses"]), 1)
        self.assertTrue(all(s["cached"] for s in cached["sources"].values()))
        self.assertEqual((len(metadata.calls), len(session.calls)), (1, 1))

    def test_cache_filter_isolation_expiry_failure_ttl_and_size_bound(self):
        engine, metadata, session = self.engine()
        clock = [0]
        with patch("paperbridge.literature.time.monotonic", side_effect=lambda: clock[0]):
            engine.search("query", source="crossref")
            engine.search("query", source="crossref", year_from=2024)
            engine.search("query", source="crossref", order="recent")
            engine.search("query", source="crossref", kind="book")
            self.assertEqual(len(metadata.calls), 3)
            clock[0] = 301
            engine.search("query", source="crossref")
            self.assertEqual(len(metadata.calls), 4)
            session.error = requests.Timeout("secret")
            engine.search("query", source="openalex")
            engine.search("query", source="openalex")
            self.assertEqual(len(session.calls), 1)
            clock[0] = 317
            session.error = None
            recovered = engine.search("query", source="openalex")
            self.assertEqual(recovered["sources"]["openalex"]["status"], "completed")
            self.assertEqual(len(session.calls), 2)
            for number in range(70):
                engine.search(str(number), source="crossref")
            self.assertLessEqual(len(engine._cache["crossref"]), 64)
            self.assertEqual(len(engine._cache["openalex"]), 1)

    def test_source_windows_bound_processing_and_global_output(self):
        engine, _, _ = self.engine([crossref(f"10.1234/c{i}") for i in range(30)],
                                  [openalex(f"10.1234/o{i}") for i in range(30)])
        result = engine.search("query", limit=20)
        self.assertEqual(len(result["results"]), 20)
        for info in result["sources"].values():
            self.assertEqual(info["received"], 30)
            self.assertEqual(info["accepted"], 20)
        self.assertTrue(any("beyond the source window" in w for w in result["warnings"]))

    def test_concurrent_identical_queries_make_one_request(self):
        engine, metadata, session = self.engine([crossref()], [openalex()])
        barrier = threading.Barrier(8)

        def search():
            barrier.wait(timeout=5)
            return engine.search("query")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: search(), range(8)))
        self.assertEqual((len(metadata.calls), len(session.calls)), (1, 1))
        self.assertTrue(all(len(result["results"]) == 1 for result in results))

    def test_blocked_crossref_does_not_block_openalex_source(self):
        engine, metadata, _ = self.engine(openalex_records=[openalex()])
        entered, release = threading.Event(), threading.Event()

        def blocked_get(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Source isolation test timed out")
            return {"message": {"items": []}}

        metadata._get = blocked_get
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(engine.search, "query", source="crossref")
            try:
                self.assertTrue(entered.wait(timeout=2))
                independent = pool.submit(engine.search, "query", source="openalex").result(timeout=2)
                self.assertEqual(independent["sources"]["openalex"]["status"], "completed")
            finally:
                release.set()
            waiting.result(timeout=2)


if __name__ == "__main__":
    unittest.main()
