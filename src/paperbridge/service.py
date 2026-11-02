from __future__ import annotations

import json
import os
from itertools import zip_longest

from .discovery import DiscoveryError, MetadataClient, public_access
from .openathens import PUBLISHERS as OPENATHENS_PUBLISHERS, openathens_configured
from .identifiers import doi_url, normalize_doi
from .publishers import identify_platform, platform_coverage, unavailable_next_action
from .storage import DocumentStore

# Publishers with a retrieval adapter: their own (IEEE, Springer) or ITU's OpenAthens.
ADAPTED = frozenset({"ieee", "springer"}) | frozenset(OPENATHENS_PUBLISHERS)


def _browser_configuration():
    """Keep environment-based provider selection and status consistent."""
    from .managed_browser import browser_headless
    requested = os.environ.get("PAPERBRIDGE_BROWSER_ENGINE")
    engine = "chromium" if requested is None else requested
    if engine not in {"chromium", "firefox"}:
        raise ValueError("PAPERBRIDGE_BROWSER_ENGINE must be chromium or firefox.")
    port = os.environ.get("PAPERBRIDGE_BROWSER_PORT")
    enabled = requested is not None or bool(port)
    managed = enabled and (engine == "firefox" or not port)
    springer_override = os.environ.get("PAPERBRIDGE_SPRINGER_BROWSER_ENGINE")
    if springer_override not in (None, "firefox", "chromium"):
        raise ValueError("PAPERBRIDGE_SPRINGER_BROWSER_ENGINE must be chromium or firefox.")
    return {"engine": engine, "port": port, "enabled": enabled, "managed": managed,
            "headless": browser_headless(), "springer_override": springer_override,
            "springer_engine": (springer_override or engine) if managed else None}


class PaperBridge:
    def __init__(self, store: DocumentStore, discovery: MetadataClient | None = None, *, institution=None,
                 provider=None, credentials=None):
        self.store = store
        self.discovery = discovery or MetadataClient()
        self.institution = institution
        self.provider = provider
        self.providers = [provider] if provider is not None else []
        self._literature = None
        self._literature_metadata = None
        self._resources = None
        self._library_search = None
        self._library_runner = None
        config = _browser_configuration()
        if self.provider is None and config["enabled"]:
            from .browser import browser_port
            from .ieee import IeeeBrowserProvider
            login = ((lambda session, url: institution.authenticate_ieee(session, url, credentials))
                     if institution and credentials else None)
            if config["managed"]:
                from .managed_browser import ManagedBrowser
                from .springer import SpringerBrowserProvider
                runner = ManagedBrowser(browser_engine=config["engine"], headless=config["headless"])
                self._library_runner = runner
                self.provider = IeeeBrowserProvider(store, None, login=login, runner=runner)
                springer_login = ((lambda session, url: institution.authenticate_springer(session, url, credentials))
                                 if institution and credentials else None)
                self.providers = [self.provider,
                    SpringerBrowserProvider(store, runner=runner, login=springer_login,
                        browser_engine=config["springer_override"])]
                if openathens_configured():
                    from .openathens import OpenAthensProvider
                    self.providers.append(OpenAthensProvider(store, runner=runner))
            else:
                self.provider = IeeeBrowserProvider(store, browser_port(config["port"]), login=login)
                self.providers = [self.provider]

    def search(self, query: str, limit: int = 10, *, local_only: bool = False,
               year_from: int | None = None, year_to: int | None = None,
               order: str = "relevance", kind: str | None = None,
               discovery: str = "crossref") -> dict:
        if type(local_only) is not bool:
            raise ValueError("local_only must be a boolean.")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Use a nonempty query of at most 1000 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 to 20.")
        if not isinstance(discovery, str) or discovery not in {"crossref", "openalex", "combined"}:
            raise ValueError("Select crossref, openalex or combined discovery.")
        if not isinstance(order, str) or order not in {"relevance", "recent", "citations"}:
            raise ValueError("Select relevance, recent or citations ordering.")
        for year in (year_from, year_to):
            if year is not None and (type(year) is not int or not 1000 <= year <= 2100):
                raise ValueError("Years must be integers from 1000 to 2100.")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("The first year cannot exceed the last year.")
        if kind is not None and (not isinstance(kind, str) or kind not in {
                "article", "review", "book-chapter", "dissertation", "preprint"}):
            raise ValueError("Unsupported publication kind.")
        authorized = self.store.search(query, limit)
        # Keep authorized matches for joining to filtered discovery metadata.
        # Their archive rows alone cannot establish a publication year or kind.
        cached = {entry["url"]: entry for entry in authorized}
        local = authorized
        if year_from is not None or year_to is not None or kind is not None:
            # Undated archive entries cannot certify a requested year range.
            local = [entry for entry in local if
                     (year_from is None or type(entry.get("year")) is int and entry["year"] >= year_from)
                     and (year_to is None or type(entry.get("year")) is int and entry["year"] <= year_to)
                     and (kind is None or entry.get("kind") == kind)]
        results = local
        warnings = []
        source_details = {}
        discovery_status = "not_requested"
        if not local_only:
            try:
                enhanced = discovery != "crossref" or year_from is not None or year_to is not None or order != "relevance" or kind is not None
                if enhanced:
                    from .literature import LiteratureSearch
                    if self._literature is None or self._literature_metadata is not self.discovery:
                        self._literature = LiteratureSearch(self.discovery)
                        self._literature_metadata = self.discovery
                    report = self._literature.search(query, limit, year_from=year_from, year_to=year_to,
                                                     order=order, kind=kind, source=discovery)
                    metadata = report["results"]
                    warnings.extend(report.get("warnings", []))
                    source_details = report.get("sources", {})
                    statuses = [info.get("status") for info in source_details.values()
                                if info.get("status") != "disabled"]
                    discovery_status = ("unavailable" if "completed" not in statuses else
                                        "partial" if "unavailable" in statuses else "completed")
                else:
                    metadata = self.discovery.search(query, limit)
                    discovery_status = "completed"
                # Metadata and full-text rankings are not comparable scores.
                # Interleave their distinct sources, beginning with discovery so
                # even limit=1 is not permanently confined to the old archive.
                metadata = [self._archive_search_hit(entry, cached[entry["url"]])
                            if entry["url"] in cached else entry for entry in metadata]
                discovered_urls = {entry["url"] for entry in metadata}
                # A duplicate archive hit must not hide discovery metadata or
                # move its result ahead of the provider's relevance ranking.
                remaining_local = [entry for entry in local if entry["url"] not in discovered_urls]
                results, seen = [], set()
                for pair in zip_longest(metadata, remaining_local):
                    for entry in pair:
                        if entry is not None and entry["url"] not in seen:
                            seen.add(entry["url"])
                            results.append(entry)
            except DiscoveryError as exc:
                discovery_status = "unavailable"
                warnings.append(str(exc))
        def coverage_rank(entry):
            terms = entry.get("query_match", {}).get("matched_terms", [])
            return len(terms) if isinstance(terms, list) else 0

        if order == "recent":
            results.sort(key=lambda entry: (-coverage_rank(entry),
                         -(entry.get("year") if type(entry.get("year")) is int else 0)))
        elif order == "citations":
            results.sort(key=lambda entry: (-coverage_rank(entry),
                         -(entry.get("citation_count") if type(entry.get("citation_count")) is int else -1)))
        results = [dict(entry, access=self.access_details(entry)) for entry in results[:limit]]
        return {"results": results, "warnings": warnings, "discovery_sources": source_details,
                "metadata_discovery": discovery_status,
                "scope": "local_archive" if local_only else "local_archive_and_metadata",
                "notice": "Search results do not mean full text has been read. For a complete local reading, "
                          "start at first_chunk_id and follow next_id until null."}

    @staticmethod
    def _archive_search_hit(paper: dict, archived: dict) -> dict:
        """Keep discovery evidence intact while exposing authorized reading IDs."""
        result = dict(paper)
        provenance = dict(paper.get("field_sources", {}))
        # The displayed discovery title still describes query_match.title_terms.
        # The archive's operator-supplied title and chunk label stay separate.
        if "title" in archived:
            result["archive_title"] = archived["title"]
            provenance["archive_title"] = "local_archive"
        for field in ("id", "first_chunk_id", "content_level", "storage", "full_text_read"):
            if field in archived:
                result[field] = archived[field]
                provenance[field] = "local_archive"
        result["field_sources"] = provenance
        return result

    @staticmethod
    def _text_delivery(result: dict, source: str) -> dict:
        """Describe this response's path, without persisting document provenance."""
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            # Preserve provider error envelopes that contain no document metadata.
            return result
        return {**result, "metadata": {**metadata, "delivery": {
            "source": source,
            "network_retrieval": False if source == "authorized_archive" else None,
        }}}

    def resources(self, query: str = "", subject: str | None = None, limit: int = 10) -> dict:
        """Find official research-resource entries without asserting entitlement."""
        if self._resources is None:
            from .research_resources import ResearchResources
            self._resources = ResearchResources()
        return self._resources.search(query=query, subject=subject, limit=limit)

    def library_query(self, query: str, year_from: int | None = None, year_to: int | None = None,
                      reviewed_only: bool = False, limit: int = 10) -> dict:
        """Search the operator-enabled subscription discovery route."""
        from .browser import RetrievalUnavailable
        from .subscription_search import SubscriptionSearch
        if self._library_runner is None:
            raise RetrievalUnavailable("browser_session_not_configured")
        if self._library_search is None:
            self._library_search = SubscriptionSearch(self._library_runner, state_dir=self.store.root / "library-discovery")
        return self._library_search.search(query, year_from=year_from, year_to=year_to,
                                          reviewed_only=reviewed_only, limit=limit)

    def access_details(self, paper: dict) -> dict:
        cached = paper.get('content_level') == 'local_chunk_available' and str(paper.get('id', '')).startswith('pb:')
        selected = next((candidate for candidate in self.providers if candidate.supports(paper)), None)
        # The OpenAthens adapter serves several publishers; report the publisher itself.
        provider = (identify_platform(paper) if selected is None or selected.name == 'openathens'
                    else selected.name)
        result = {'provider': provider,
                'retrieval': 'authorized_archive_available' if cached else
                    'configured_not_attempted' if selected else
                    'adapter_not_configured_or_article_route_unsupported' if provider in ADAPTED else
                    'adapter_not_implemented',
                'entitlement': 'not_checked', 'full_text_read_by_discovery': False,
                'figures_read_by_discovery': False}
        public = public_access(paper)
        if not cached and public:
            result.update(retrieval='public_direct_link', open_access=public,
                          next_action=self._public_next_action())
        return result

    @staticmethod
    def _public_next_action() -> str:
        return ("Metadata reports a publicly accessible full-text location for this paper. Open the returned link "
                "with your ordinary web tools; PaperBridge's institutional access is not needed "
                "for that location. Do not retry this paper through PaperBridge. Use PaperBridge "
                "for papers that require institutional access. No paper text or figures were read here.")

    def _open_access(self, paper: dict) -> dict:
        public = public_access(paper)
        if public:
            return public
        try:
            return self.discovery.open_access(paper['doi'])
        except DiscoveryError:
            # Missing OA evidence does not establish that a paper is paywalled.
            return {'status': 'unavailable', 'downloaded': False}

    def _public_response(self, paper: dict, public: dict) -> dict:
        return {'status': 'public_access_available', 'reason': 'open_access_use_direct_link',
                'id': paper['id'], 'url': paper['url'], 'title': paper['title'],
                'text': None, 'content_level': 'metadata_only', 'full_text_read': False,
                'figures_read': False, 'downloaded': False, 'open_access': public,
                'access': dict(self.access_details(paper), retrieval='public_direct_link'),
                'next_action': self._public_next_action()}

    def resolve(self, doi: str) -> dict:
        doi = normalize_doi(doi)
        paper = self.discovery.lookup(doi)
        if paper.get('doi') != doi:
            raise DiscoveryError('Metadata identity did not match the requested DOI.')
        access = self.access_details(paper)
        provider = access['provider']
        configured = access['retrieval'] == 'configured_not_attempted'
        public = self._open_access(paper)
        if isinstance(public, dict) and public.get('status') == 'found' and public.get('links'):
            return dict(self._public_response(paper, public), paper=paper, provider=provider,
                        institution=None, subscription_retrieval='not_needed_for_public_link',
                        external_model_permission='not_evaluated')
        return {"paper": paper, "open_access": public,
                "institution": self.institution.entry_points(provider) if self.institution else None,
                "provider": provider, "subscription_retrieval": (
                    "experimental_browser_session" if configured
                    else "not_configured" if provider in ADAPTED else "not_implemented"),
                "external_model_permission": "not_evaluated", "access": access}

    def fetch(self, paper_id: str) -> dict:
        if paper_id.startswith("pb:"):
            return self._text_delivery(self.store.fetch(paper_id), "authorized_archive")
        if not paper_id.startswith("doi:"):
            raise ValueError("Use an identifier returned by search.")
        cached = self.store.first_chunk_for_url(doi_url(normalize_doi(paper_id)))
        if cached:
            return self._text_delivery(self.store.fetch(cached), "authorized_archive")
        if self.providers:
            return self.get_text(paper_id)
        paper = self.discovery.lookup(normalize_doi(paper_id))
        if paper.get('doi') != normalize_doi(paper_id):
            raise DiscoveryError('Metadata identity did not match the requested DOI.')
        public = self._open_access(paper)
        if isinstance(public, dict) and public.get('status') == 'found' and public.get('links'):
            return self._public_response(paper, public)
        # Metadata-only fallback is deliberately explicit, not an empty 'full text'.
        return {"id": paper["id"], "title": paper["title"], "url": paper["url"],
                "text": json.dumps({"title": paper["title"], "authors": paper["authors"],
                                    "year": paper["year"], "doi": paper["doi"]}, ensure_ascii=False),
                "metadata": {"content_level": "metadata_only", "full_text_read": False,
                    "retrieval_status": "subscription_retrieval_not_implemented",
                    "untrusted_external_content": True}}

    def get_text(self, reference: str) -> dict:
        """Read by raw DOI, DOI URL, search ID or continuation ID; never fake full text."""
        if reference.startswith("pb:"):
            return self._text_delivery(self.store.fetch(reference), "authorized_archive")
        doi = normalize_doi(reference)
        cached = self.store.first_chunk_for_url(doi_url(doi))
        if cached:
            return self._text_delivery(self.store.fetch(cached), "authorized_archive")
        if self.providers:
            from .browser import RetrievalUnavailable
            paper = None
            attempted_provider = None
            try:
                # Operator permission is checked before any publisher request.
                permitted = []
                denial = "document_permission_required"
                for candidate in self.providers:
                    try:
                        candidate.rights(doi)
                        permitted.append(candidate)
                    except RetrievalUnavailable as exc:
                        denial = exc.reason
                if not permitted:
                    raise RetrievalUnavailable(denial)
                paper = self.discovery.lookup(doi)
                if paper.get("doi") != doi:
                    raise RetrievalUnavailable("metadata_identity_not_verified")
                public = self._open_access(paper)
                if isinstance(public, dict) and public.get('status') == 'found' and public.get('links'):
                    return self._public_response(paper, public)
                selected = next((candidate for candidate in self.providers if candidate.supports(paper)), None)
                if selected:
                    # A different publisher's policy never authorizes this source.
                    selected.rights(doi)
                    attempted_provider = selected.name
                    return self._text_delivery(selected.retrieve(paper), "performed_provider_retrieval")
                reason = "publisher_not_supported"
            except RetrievalUnavailable as exc:
                reason = exc.reason
            except DiscoveryError:
                reason = "metadata_lookup_failed"
            access = self.access_details(paper) if paper else {"provider": "not_resolved", "retrieval": "not_attempted"}
            if attempted_provider is not None:
                # Provider entry does not establish network access or authentication.
                access.update(provider=attempted_provider, retrieval="provider_attempt_failed")
            return {"status": "full_text_unavailable", "id": "doi:" + doi, "url": doi_url(doi),
                    "text": None, "content_level": "none", "full_text_read": False,
                    "reason": reason,
                    "access": access,
                    "next_action": unavailable_next_action(reason)}
        # Public links remain useful even when no institutional browser is configured.
        try:
            paper = self.discovery.lookup(doi)
            if paper.get('doi') == doi:
                public = self._open_access(paper)
                if isinstance(public, dict) and public.get('status') == 'found' and public.get('links'):
                    return self._public_response(paper, public)
        except DiscoveryError:
            pass
        return {"status": "full_text_unavailable", "id": "doi:" + doi, "url": doi_url(doi),
                "text": None, "content_level": "none", "full_text_read": False,
                "reason": ("browser_session_not_configured" if doi.startswith("10.1109/")
                           else "publisher_retrieval_not_implemented"),
                "message": "No authorized local text or configured retrieval provider is available.",
                "next_action": "Use papers_resolve to discover access links; do not claim the paper was read."}

    def get_page(self, reference: str, page: int, max_edge: int = 2000) -> dict:
        if type(page) is not int or not 1 <= page <= 200 or type(max_edge) is not int or not 800 <= max_edge <= 2400:
            raise ValueError("Use page 1..200 and image edge 800..2400 pixels.")
        if reference.startswith("pb:"):
            return self.store.get_page(reference, page, max_edge)
        # Reuse the same acquisition and permission checks as text retrieval.
        result = self.get_text(reference)
        if not result.get("id", "").startswith("pb:"):
            return result
        return self.store.get_page(result["id"], page, max_edge)

    def get_pages(self, pages: list[dict], max_edge: int = 2000) -> dict:
        """Collect a bounded selection with per-page access checks and coverage."""
        from .rendering import PageRenderUnavailable
        validate_page_selection(pages, max_edge)
        rendered, coverage, seen = [], [], set()
        acquisition_failures = {}
        total_bytes = 0
        byte_budget_exhausted = False
        for index, selection in enumerate(pages):
            entry = {"selection_index": index, **selection}
            if byte_budget_exhausted or total_bytes >= 8_000_000:
                entry.update(status="not_processed", reason="batch_byte_limit")
                coverage.append(entry)
                continue
            try:
                try:
                    source = normalize_doi(selection["reference"])
                except ValueError:
                    source = None  # Archive IDs and invalid references retain normal page checks.
                if source in acquisition_failures:
                    result = acquisition_failures[source]
                else:
                    result = self.get_page(selection["reference"], selection["page"], max_edge)
                    if source is not None and result.get("status") in {"full_text_unavailable", "public_access_available"} and "image_bytes" not in result:
                        # Reuse source failures only in this batch; page/render failures may differ.
                        acquisition_failures[source] = result
                if "image_bytes" not in result:
                    entry.update(status=result.get("status", "page_image_unavailable"),
                                 reason=result.get("reason", "page_not_available"))
                    if result.get('status') == 'public_access_available':
                        entry.update(open_access=result['open_access'], next_action=result['next_action'])
                else:
                    key = (result["id"].rsplit(":", 1)[0], result["page"])
                    if key in seen:
                        entry.update(status="duplicate_page", reason="already_selected")
                    elif total_bytes + len(result["image_bytes"]) > 8_000_000:
                        entry.update(status="page_image_unavailable", reason="batch_byte_limit")
                        byte_budget_exhausted = True
                    else:
                        seen.add(key)
                        total_bytes += len(result["image_bytes"])
                        entry.update(status="image_prepared", image_index=len(rendered),
                                     id=result["id"], url=result["url"])
                        rendered.append(result)
            except PermissionError:
                entry.update(status="external_model_permission_required", reason="permission_required")
            except (ValueError, LookupError):
                entry.update(status="invalid_or_unknown_reference", reason="invalid_reference_or_page")
            except PageRenderUnavailable:
                entry.update(status="page_image_unavailable", reason="render_failed_or_limit_exceeded")
            except DiscoveryError:
                entry.update(status="discovery_unavailable", reason="metadata_lookup_failed")
            coverage.append(entry)
        return {"status": "pages_prepared" if rendered else "page_images_unavailable",
                "pages": rendered, "selection_coverage": coverage,
                "requested_count": len(pages), "prepared_count": len(rendered),
                "unprocessed_count": sum(entry["status"] == "not_processed" for entry in coverage),
                "image_byte_limit": 8_000_000,
                "image_bytes_total": total_bytes, "model_visual_access": "not_verified",
                "text_read_by_this_call": False, "entire_papers_read": False}

    @staticmethod
    def status() -> dict:
        from .managed_browser import browser_idle_seconds
        config = _browser_configuration()
        configured = (["ieee", "springer"] if config["managed"]
                      else ["ieee"] if config["enabled"] else [])
        if config["managed"] and openathens_configured():
            configured += sorted(OPENATHENS_PUBLISHERS)
        return {"version": "0.1.0.dev20", "deployment": "single_user_prototype",
                "execution": "importing_process", "llm_api_calls": False,
                "ready": ["crossref_metadata", "openalex_metadata", "combined_doi_discovery",
                          "research_resource_directory", "local_pdf_text_ingest",
                          "permission_gated_chunk_fetch", "pdf_page_images", "bounded_page_batches"],
                "optional": ["unpaywall_discovery_with_contact_email", "subscription_literature_discovery"],
                "research_discovery": {"sources": ["crossref", "openalex"],
                                       "directory": "official_itu_library",
                                       "subscription_search_enabled": os.getenv("PAPERBRIDGE_LIBRARY_SEARCH") == "1",
                                       "subscription_access_verified": False,
                                       "discovery_reads_full_text": False},
                "experimental": ["ieee_browser_session_retrieval", "springer_browser_session_retrieval",
                                 "openathens_browser_session_retrieval", "operator_browser_login",
                                 "itu_yetkim_credential_login", "chatgpt_page_file_attachment", "chatgpt_automatic_page_followup"],
                "browser_provider_configured": config["enabled"],
                "browser_engine": config["engine"],
                "springer_browser_engine": config["springer_engine"],
                "managed_browser_headless": config["headless"] if config["managed"] else None,
                "managed_browser_idle_seconds": browser_idle_seconds(),
                "browser_session_policy": "one_browser_at_a_time_restart_on_network_or_engine_change",
                "configured_providers": configured,
                "document_storage": "persistent_local_archive",
                "repeat_retrieval": "local_first",
                "open_access_policy": "return_reported_public_links_without_new_pdf_acquisition",
                "page_image_model_visibility": "client_dependent_not_verified",
                "publisher_proxy_configured": bool(os.environ.get("PAPERBRIDGE_PUBLISHER_PROXY")),
                "browser_session_authenticated": "not_checked",
                "publisher_coverage": platform_coverage(configured),
                "status_scope": "configuration_only_no_network_requests",
                "institutional_retrieval_verified": None,
                "institutional_retrieval_verification": "not_checked_by_status",
                "openathens_login_configured": openathens_configured(),
                "not_implemented": ["elsevier_wiley_other_publisher_downloads",
                                    "ocr", "multi_user_isolation"]}


def validate_page_selection(pages: list[dict], max_edge: int = 2000):
    """Validate the complete selection before retrieval or browser startup."""
    if not isinstance(pages, list) or not pages:
        raise ValueError("Select at least one PDF page.")
    if type(max_edge) is not int or not 800 <= max_edge <= 2400:
        raise ValueError("Use an image edge of 800..2400 pixels.")
    for item in pages:
        if not isinstance(item, dict) or set(item) != {"reference", "page"}:
            raise ValueError("Each selection requires only reference and page.")
        if not isinstance(item["reference"], str) or not 1 <= len(item["reference"]) <= 2048:
            raise ValueError("A bounded paper reference is required.")
        if type(item["page"]) is not int or not 1 <= item["page"] <= 200:
            raise ValueError("Use page numbers from 1 to 200.")
