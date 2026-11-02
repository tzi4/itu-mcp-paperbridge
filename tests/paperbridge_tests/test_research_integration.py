"""Host integration of research discovery with archive and access reporting."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from paperbridge.service import PaperBridge
from paperbridge.discovery import MetadataClient
from paperbridge.storage import DocumentStore, Rights
from paperbridge.research_resources import ResearchResourcesError
from ninova_mcp.papers_client import PapersClient, PAPER_TOOLS


def paper(doi, year=2024, citations=2):
    return {"id": "doi:" + doi, "doi": doi, "url": "https://doi.org/" + doi,
            "title": "Synthetic spacecraft attitude control study", "year": year,
            "citation_count": citations, "content_level": "metadata_only", "full_text_read": False}


class ResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict("os.environ", {}, clear=True):
            self.bridge = PaperBridge(DocumentStore(Path(self.temp.name)), Mock())
        self.search = Mock()
        self.bridge._literature = self.search
        self.bridge._literature_metadata = self.bridge.discovery

    def report(self, results, sources, warnings=None):
        self.search.search.return_value = {"results": results, "sources": sources,
                                           "warnings": warnings or []}

    def ingest(self, doi, *, title="Operator archive title", text=None, allowed=True):
        path = Path(self.temp.name) / "synthetic.txt"
        path.write_text(text or f"Original synthetic spacecraft attitude control text for {doi}.", encoding="utf-8")
        return self.bridge.store.ingest(path, title=title, url="https://doi.org/" + doi,
                                        rights=Rights(True, allowed, "Original synthetic test text"))

    def metadata_records(self, records):
        """Use the real literature adapter and Crossref parser, with offline wire data."""
        self.bridge._literature = None
        self.bridge.discovery.email = None
        self.bridge.discovery._paper.side_effect = MetadataClient._paper
        self.bridge.discovery._get.return_value = {"message": {"items": records}}

    def test_bounded_coverage_note_does_not_misreport_a_completed_search_as_partial(self):
        self.report([paper("10.1234/research")], {"crossref": {"status": "completed"},
                    "openalex": {"status": "completed"}}, ["A bounded window is not exhaustive."])
        result = self.bridge.search("spacecraft control", discovery="combined", year_from=2022)
        self.assertEqual(result["metadata_discovery"], "completed")
        self.assertFalse(result["results"][0]["full_text_read"])
        self.assertEqual(result["results"][0]["access"]["entitlement"], "not_checked")
        self.assertIn("bounded", result["warnings"][0])

    def test_partial_and_total_source_failure_are_distinct(self):
        cases = [({"crossref": {"status": "completed"}, "openalex": {"status": "unavailable"}}, "partial"),
                 ({"crossref": {"status": "unavailable"}, "openalex": {"status": "unavailable"}}, "unavailable")]
        for sources, expected in cases:
            with self.subTest(expected=expected):
                self.report([], sources, ["Source unavailable."])
                self.assertEqual(self.bridge.search("spacecraft", discovery="combined")["metadata_discovery"], expected)

    def test_local_only_never_contacts_either_discovery_source(self):
        self.bridge.search("spacecraft", local_only=True, discovery="combined", year_from=2022)
        self.search.search.assert_not_called()
        self.bridge.discovery.search.assert_not_called()

    def test_invalid_date_range_fails_before_any_source_or_browser_call(self):
        with self.assertRaises(ValueError):
            self.bridge.search("spacecraft", discovery="combined", year_from=2025, year_to=2020)
        self.search.search.assert_not_called()

    def test_host_sorts_the_final_window_including_archive_entries(self):
        old = self.ingest("10.1234/old", title="Older archive label")
        new = self.ingest("10.1234/new", title="Newer archive label")
        extra = self.ingest("10.1234/local", title="Undated archive-only label")
        self.report([paper("10.1234/old", year=2022, citations=500),
                     paper("10.1234/new", year=2025, citations=5)],
                    {"openalex": {"status": "completed"}})
        for order, expected in (("recent", [new, old, extra]), ("citations", [old, new, extra])):
            with self.subTest(order=order):
                result = self.bridge.search("spacecraft", discovery="combined", order=order)
                self.assertEqual([r["id"] for r in result["results"]],
                                 [entry["first_chunk_id"] for entry in expected])
                self.assertTrue(all(r["content_level"] == "local_chunk_available" for r in result["results"]))
                self.assertEqual([r["year"] for r in result["results"][:2]],
                                 [2025, 2022] if order == "recent" else [2022, 2025])
                self.assertTrue(all(r["first_chunk_id"].startswith("pb:") for r in result["results"]))

    def test_filtered_metadata_joins_authorized_archive_and_preserves_title_evidence(self):
        doi = "10.1234/join"
        saved = self.ingest(doi, title="My shorter archive label")
        self.ingest("10.1234/not-discovered", title="Archive entry with no publication metadata")
        discovery_title = "Synthetic spacecraft attitude control study"
        self.metadata_records([{"DOI": doi, "title": [discovery_title], "type": "journal-article",
                                "published": {"date-parts": [[2024]]}, "publisher": "Fixture publisher"}])
        for filters in ({"year_from": 2022, "year_to": 2025}, {"kind": "article"},
                        {"year_from": 2022, "year_to": 2025, "kind": "article"}):
            with self.subTest(filters=filters):
                self.bridge.discovery._get.reset_mock()
                result = self.bridge.search("spacecraft attitude control", discovery="crossref", **filters)
                self.assertEqual(len(result["results"]), 1)
                hit = result["results"][0]
                self.assertEqual(hit["id"], saved["first_chunk_id"])
                self.assertEqual(hit["first_chunk_id"], saved["first_chunk_id"])
                self.assertEqual(hit["title"], discovery_title)
                self.assertTrue(hit["archive_title"].startswith("My shorter archive label"))
                self.assertEqual(hit["field_sources"]["title"], "crossref")
                self.assertEqual(hit["field_sources"]["id"], "local_archive")
                self.assertEqual(hit["field_sources"]["first_chunk_id"], "local_archive")
                self.assertEqual(hit["field_sources"]["archive_title"], "local_archive")
                self.assertEqual(hit["query_match"]["title_terms"], ["spacecraft", "attitude", "control"])
                self.assertEqual(hit["query_match"]["coverage"], 1)
                self.assertEqual(hit["sources"], ["crossref"])
                self.assertEqual(hit["publisher"], "Fixture publisher")
                self.assertEqual(hit["content_level"], "local_chunk_available")
                self.assertEqual(hit["access"]["retrieval"], "authorized_archive_available")
                self.assertIs(hit["full_text_read"], False)
                repeated = self.bridge.search("spacecraft attitude control", discovery="crossref", **filters)
                self.assertEqual(repeated["results"], result["results"])
                self.assertTrue(repeated["discovery_sources"]["crossref"]["cached"])
                self.bridge.discovery._get.assert_called_once()
        self.bridge.discovery.lookup.assert_not_called()
        self.bridge.discovery.open_access.assert_not_called()
        self.bridge.discovery.search.assert_not_called()
        self.search.search.assert_not_called()

    def test_local_only_unknown_filters_do_not_return_archive_or_query_metadata(self):
        self.ingest("10.1234/unknown")
        for filters in ({"year_from": 2022}, {"year_to": 2025}, {"kind": "article"}):
            with self.subTest(filters=filters):
                result = self.bridge.search("spacecraft", local_only=True, **filters)
                self.assertEqual(result["results"], [])
                self.assertEqual(result["metadata_discovery"], "not_requested")
        self.assertEqual(self.bridge.discovery.mock_calls, [])
        self.search.search.assert_not_called()

    def test_metadata_date_and_kind_mismatches_do_not_restore_archive_hits(self):
        self.ingest("10.1234/too-old")
        self.ingest("10.1234/wrong-kind")
        self.metadata_records([
            {"DOI": "10.1234/too-old", "title": ["Spacecraft attitude control"],
             "type": "journal-article", "published": {"date-parts": [[2019]]}},
            {"DOI": "10.1234/wrong-kind", "title": ["Spacecraft attitude control"],
             "type": "book-chapter", "published": {"date-parts": [[2024]]}},
        ])
        result = self.bridge.search("spacecraft attitude control", year_from=2022, kind="article")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["metadata_discovery"], "completed")
        self.assertEqual(result["discovery_sources"]["crossref"]["filtered"], 2)

    def test_unauthorized_archive_is_not_exposed_when_metadata_matches(self):
        doi = "10.1234/private"
        self.ingest(doi, title="Private archive title", allowed=False)
        self.report([paper(doi)], {"crossref": {"status": "completed"}})
        result = self.bridge.search("spacecraft", discovery="combined", year_from=2022)
        self.assertEqual(result["results"][0]["id"], "doi:" + doi)
        self.assertNotIn("archive_title", result["results"][0])
        self.assertNotIn("first_chunk_id", result["results"][0])
        self.assertEqual(result["results"][0]["content_level"], "metadata_only")

    def test_archive_duplicates_do_not_replace_later_discovery_titles_or_rank(self):
        first = paper("10.1234/first")
        second = paper("10.1234/second")
        second["title"] = "Second spacecraft attitude control study"
        saved = self.ingest("10.1234/second", title="Different operator title")
        self.report([first, second], {"crossref": {"status": "completed"}})
        result = self.bridge.search("spacecraft", discovery="combined")
        self.assertEqual([p["doi"] for p in result["results"]], ["10.1234/first", "10.1234/second"])
        self.assertEqual(result["results"][1]["title"], second["title"])
        self.assertEqual(result["results"][1]["id"], saved["first_chunk_id"])

    def test_cached_text_aliases_and_continuations_have_response_delivery_without_network(self):
        doi = "10.1234/cache"
        saved = self.ingest(doi, text="spacecraft attitude control " * 600)
        baseline = self.bridge.store.database.read_bytes()
        provider = Mock()
        self.bridge.providers = [provider]
        references = [doi, "doi:" + doi.upper(), "https://doi.org/" + doi, saved["first_chunk_id"]]
        for reference in references:
            with self.subTest(reference=reference):
                result = self.bridge.get_text(reference)
                self.assertEqual(result["metadata"]["delivery"],
                                 {"source": "authorized_archive", "network_retrieval": False})
        for reference in ("doi:" + doi, saved["first_chunk_id"], result["metadata"]["next_id"]):
            result = self.bridge.fetch(reference)
            self.assertEqual(result["metadata"]["delivery"],
                             {"source": "authorized_archive", "network_retrieval": False})
        self.assertEqual(self.bridge.discovery.mock_calls, [])
        self.assertEqual(provider.mock_calls, [])
        self.assertNotIn("delivery", self.bridge.store.fetch(saved["first_chunk_id"])["metadata"])
        self.assertEqual(self.bridge.store.database.read_bytes(), baseline)

    def test_provider_delivery_is_unknown_network_then_archive_without_repeated_retrieval(self):
        provider = Mock()
        provider.name = "synthetic_provider"
        provider.supports.return_value = True
        self.bridge.providers = [provider]
        self.bridge.discovery.open_access.return_value = {"status": "not_found"}
        returned = []

        def acquire(metadata):
            saved = self.ingest(metadata["doi"])
            response = self.bridge.store.fetch(saved["first_chunk_id"])
            response["metadata"]["retrieval"] = "synthetic_provider_provenance"
            returned.append(response)
            return response

        provider.retrieve.side_effect = acquire
        for method in ("get_text", "fetch"):
            with self.subTest(method=method):
                doi = "10.1234/provider-" + method
                self.bridge.discovery.lookup.return_value = paper(doi)
                provider.reset_mock()
                self.bridge.discovery.reset_mock()
                reference = "doi:" + doi
                fresh = getattr(self.bridge, method)(reference)
                self.assertEqual(fresh["metadata"]["delivery"],
                                 {"source": "performed_provider_retrieval", "network_retrieval": None})
                self.assertEqual(fresh["metadata"]["retrieval"], "synthetic_provider_provenance")
                self.assertNotIn("delivery", returned[-1]["metadata"])
                baseline = self.bridge.store.database.read_bytes()
                cached = self.bridge.get_text(reference)
                continued = self.bridge.fetch(fresh["id"])
                for response in (cached, continued):
                    self.assertEqual(response["text"], fresh["text"])
                    self.assertEqual(response["metadata"]["delivery"],
                                     {"source": "authorized_archive", "network_retrieval": False})
                self.assertEqual(fresh["metadata"]["delivery"]["source"], "performed_provider_retrieval")
                self.bridge.discovery.lookup.assert_called_once_with(doi)
                self.bridge.discovery.open_access.assert_called_once()
                provider.retrieve.assert_called_once()
                self.assertEqual(self.bridge.store.database.read_bytes(), baseline)

    def test_archive_delivery_still_enforces_current_export_permission(self):
        doi = "10.1234/revoked"
        saved = self.ingest(doi)
        self.assertEqual(self.bridge.get_text(doi)["metadata"]["delivery"]["source"], "authorized_archive")
        self.ingest(doi, allowed=False)
        for reference in (doi, saved["first_chunk_id"]):
            with self.subTest(reference=reference), self.assertRaises(PermissionError):
                self.bridge.get_text(reference)
        self.assertEqual(self.bridge.discovery.mock_calls, [])

    def test_directory_error_is_an_explicit_tool_result(self):
        self.bridge._resources = Mock()
        self.bridge._resources.search.side_effect = ResearchResourcesError("network_error", "Synthetic failure")
        client = PapersClient.__new__(PapersClient)
        client.bridge = self.bridge
        result = client.call("resources", query="aerospace")
        self.assertEqual(result["status"], "research_directory_unavailable")
        self.assertEqual(result["reason"], "network_error")
        self.assertFalse(result["full_text_read"])
        self.assertEqual(result["entitlement"], "not_checked")

    def test_new_tools_accept_neither_credentials_nor_arbitrary_fetch_destinations(self):
        for name in ("papers_resources", "papers_library_query"):
            schema = next(t["inputSchema"] for t in PAPER_TOOLS if t["name"] == name)
            self.assertFalse(schema["additionalProperties"])
            self.assertFalse({"password", "username", "url", "path", "permission", "confirm"} & set(schema["properties"]))
