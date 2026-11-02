"""Independent directory discovery tests using the official public JSON shape."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from paperbridge import research_resources as directory
from paperbridge.research_resources import ResearchResources, ResearchResourcesError


# Field names and filter categories mirror the public FullList response.
RESOURCE = {
    "Id": 6747,
    "Adi": "ABI/INFORM Collection",
    "Link": "https://search.proquest.com/abicomplete",
    "KampusDisiLink": "https://go.openathens.net/redirector/itu.edu.tr?url=https%3A%2F%2Fsearch.proquest.com",
    "KampusDisiLinkDokumanURL": None,
    "MobilLink": None,
    "Aciklama": "<p>İşletme ve ekonomi araştırmaları.</p>",
    "Aciklama_en": "<p>Business and economics <b>research</b>.</p>",
    "Icerik": "Tam Metin",
    "EklenmeTarihi": "18 Ocak 2022",
    "ErisimTuru": None,
    "YeniMi": False,
    "AcikErisim": False,
    "KurumsalErisim": False,
    "Filtreler": [
        {"Id": 60, "Adi": "ProQuest", "Adi_en": "ProQuest", "Secili": False,
         "Tip": 0, "FiltreTurId": 3, "FiltreTurAdi": "Yayıncı", "FiltreTurAdi_en": "Publisher"},
        {"Id": 1, "Adi": "Dergi", "Adi_en": "Journal", "Secili": False,
         "Tip": 0, "FiltreTurId": 2, "FiltreTurAdi": "İçerik Türü", "FiltreTurAdi_en": "Content Type"},
        {"Id": 137, "Adi": "Tam Metin", "Adi_en": "Fulltext", "Secili": False,
         "Tip": 0, "FiltreTurId": 4, "FiltreTurAdi": "Haklar / Kısıtlar", "FiltreTurAdi_en": "Rights / Limitations"},
        {"Id": 44, "Adi": "İşletme, Ekonomi ve Yönetim", "Adi_en": "Business, Economics, and Management",
         "Secili": False, "Tip": 0, "FiltreTurId": 1, "FiltreTurAdi": "Konu", "FiltreTurAdi_en": "Subject"},
    ],
    "Dosyalar": [],
}


def resource(record_id=6747, **values):
    row = deepcopy(RESOURCE)
    row.update(Id=record_id, **values)
    return row


def payload(*rows):
    return {"Toplam": len(rows), "Liste": list(rows)}


class Response:
    def __init__(self, data=None, *, body=None, chunks=None, status=200, headers=None, url=None):
        self.status_code = status
        self.url = url or directory.DIRECTORY_API_URL
        self.history = []
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self.headers.update(headers or {})
        self.body = body if body is not None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.chunks = chunks
        self.read_chunks = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def iter_content(self, chunk_size):
        chunks = self.chunks if self.chunks is not None else (
            self.body[i:i + chunk_size] for i in range(0, len(self.body), chunk_size)
        )
        for chunk in chunks:
            self.read_chunks += 1
            yield chunk


def client_for(data=None, **response_options):
    session = requests.Session()
    response = Response(payload(resource()) if data is None else data, **response_options)
    session.get = Mock(return_value=response)
    return ResearchResources(session=session), session, response


class ResearchResourcesTests(unittest.TestCase):
    def assert_failure(self, client, code):
        with self.assertRaises(ResearchResourcesError) as caught:
            client.search()
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def test_public_schema_from_real_shaped_response(self):
        client, session, response = client_for()
        result = client.search()
        self.assertEqual(result["total_resources"], 1)
        self.assertEqual(result["matched_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["subjects"], ["Business, Economics, and Management"])
        self.assertEqual(result["source_url"], directory.DIRECTORY_PAGE_URL)
        self.assertEqual(result["metadata_url"], directory.DIRECTORY_API_URL)
        self.assertEqual(result["content_level"], "directory_metadata")
        row = result["resources"][0]
        self.assertEqual(row, {
            "catalog_record_id": 6747, "label": "ABI/INFORM Collection",
            "subjects": ["Business, Economics, and Management"],
            "description": "Business and economics research.", "publisher": "ProQuest",
            "public_entry_url": RESOURCE["Link"], "institutional_entry_url": RESOURCE["KampusDisiLink"],
            "entitlement": "not_checked", "full_text_read": False, "retrieval_authorized": False,
        })
        for item in (result, row):
            self.assertEqual(item["entitlement"], "not_checked")
            self.assertIs(item["full_text_read"], False)
            self.assertIs(item["retrieval_authorized"], False)
        session.get.assert_called_once()
        self.assertTrue(response.closed)

    def test_english_names_win_and_both_languages_remain_searchable(self):
        row = resource(Adi="Güç ve Işık", Adi_en="Power and Light")
        client, _, _ = client_for(payload(row))
        for query in ("power light", "GÜÇ IŞIK", "guc isik", "İŞLETME", "isletme", "ekonomi", "economics"):
            with self.subTest(query=query):
                result = client.search(query)
                self.assertEqual(result["matched_count"], 1)
                self.assertEqual(result["resources"][0]["label"], "Power and Light")

    def test_missing_english_falls_back_to_exact_turkish_source(self):
        row = resource(Adi="Türkçe Kaynak", Adi_en=None, Aciklama_en="")
        row["Filtreler"][0]["Adi_en"] = None
        row["Filtreler"][-1]["Adi_en"] = None
        client, _, _ = client_for(payload(row))
        result = client.search("turkce kaynak")["resources"][0]
        self.assertEqual(result["label"], "Türkçe Kaynak")
        self.assertEqual(result["description"], "İşletme ve ekonomi araştırmaları.")
        self.assertEqual(result["subjects"], ["İşletme, Ekonomi ve Yönetim"])

    def test_multiple_publishers_are_retained_without_confusing_filter_types(self):
        row = resource()
        row["Filtreler"].extend([
            {"Adi": "İkinci Yayıncı", "Adi_en": "Second Publisher", "FiltreTurId": 3},
            {"Adi": "ProQuest", "Adi_en": "ProQuest", "FiltreTurId": 3},
            {"Adi": "Sağlayıcı", "Adi_en": "Provider", "FiltreTurId": 27},
        ])
        client, _, _ = client_for(payload(row))
        self.assertEqual(client.search()["resources"][0]["publisher"], "ProQuest, Second Publisher")
        self.assertEqual(client.search("second publisher")["matched_count"], 1)
        self.assertEqual(client.search("provider")["matched_count"], 0)
        self.assertEqual(client.search(subject="fulltext")["matched_count"], 0)

    def test_all_query_tokens_required_and_title_beats_long_description(self):
        title = resource(1, Adi="Energy Systems", Aciklama="", Aciklama_en="")
        prose = resource(2, Adi="Broad Collection", Aciklama="", Aciklama_en="energy systems " * 500)
        partial = resource(3, Adi="Energy Only", Aciklama="", Aciklama_en="")
        client, _, _ = client_for(payload(prose, partial, title))
        result = client.search("energy systems")
        self.assertEqual([r["catalog_record_id"] for r in result["resources"]], [1, 2])
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(client.search("energy missing")["matched_count"], 0)

    def test_publisher_matches_outrank_description_and_short_terms_are_whole_words(self):
        prose = resource(1, Adi="Broad Collection", Aciklama_en="ProQuest research and said", Aciklama="")
        publisher = resource(2, Adi="Focused Collection", Aciklama_en="", Aciklama="")
        prose["Filtreler"] = []
        client, _, _ = client_for(payload(prose, publisher))
        self.assertEqual([r["catalog_record_id"] for r in client.search("proquest")["resources"]], [2, 1])
        self.assertEqual(client.search("ai")["matched_count"], 0)

    def test_prefix_matching_stopwords_and_deterministic_ties(self):
        rows = [resource(3, Adi="Zeta Engineering"), resource(2, Adi="Alpha Engineering"),
                resource(1, Adi="Alpha Engineering")]
        client, _, _ = client_for(payload(*rows))
        self.assertEqual([r["catalog_record_id"] for r in client.search("the engineer")["resources"]], [1, 2, 3])

    def test_subject_filter_searches_both_languages_and_preserves_global_facets(self):
        other = resource(2, Adi="Engineering Research")
        other["Filtreler"] = [{"FiltreTurId": 1, "Adi": "Elektrik ve Elektronik Mühendisliği",
                               "Adi_en": "Electrical-Electronics Engineering"}]
        client, _, _ = client_for(payload(resource(), other))
        for subject in ("Electrical", "ELEKTRİK elektronik", "elektrik elektronik muhendisligi"):
            result = client.search("research", subject=subject)
            self.assertEqual([r["catalog_record_id"] for r in result["resources"]], [2])
            self.assertEqual(result["total_resources"], 2)
        none = client.search(subject="unknown")
        self.assertEqual(none["matched_count"], 0)
        self.assertEqual(len(none["subjects"]), 2)
        self.assertFalse(none["truncated"])

    def test_subject_terms_must_match_one_subject_rather_than_several(self):
        row = resource()
        row["Filtreler"] = [{"FiltreTurId": 1, "Adi": "Electrical Engineering"},
                            {"FiltreTurId": 1, "Adi": "Business Management"}]
        client, _, _ = client_for(payload(row))
        self.assertEqual(client.search(subject="electrical management")["matched_count"], 0)

    def test_counts_and_limit_apply_after_filtering(self):
        client, _, _ = client_for(payload(resource(1), resource(2), resource(3, Adi="Other")))
        result = client.search("abi", limit=1)
        self.assertEqual(len(result["resources"]), 1)
        self.assertEqual(result["total_resources"], 3)
        self.assertEqual(result["matched_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(client.search("abi", limit=2)["truncated"])

    def test_valid_empty_directory_is_cached_and_distinct_from_invalid_response(self):
        client, session, _ = client_for(payload())
        for _ in range(2):
            result = client.search()
            self.assertEqual(result["resources"], [])
            self.assertEqual(result["subjects"], [])
            self.assertEqual(result["total_resources"], 0)
            self.assertEqual(result["matched_count"], 0)
        self.assertEqual(session.get.call_count, 1)

    def test_cache_expires_at_ttl_and_returned_values_cannot_mutate_snapshot(self):
        client, session, _ = client_for()
        with patch.object(directory.time, "monotonic", return_value=100) as clock:
            result = client.search()
            result["resources"][0]["label"] = "Changed"
            result["resources"][0]["subjects"].clear()
            result["subjects"].clear()
            clock.return_value = 100 + directory.CACHE_TTL_SECONDS - 1
            again = client.search()
            self.assertEqual(again["resources"][0]["label"], RESOURCE["Adi"])
            self.assertTrue(again["resources"][0]["subjects"])
            self.assertTrue(again["subjects"])
            self.assertEqual(session.get.call_count, 1)
            session.get.return_value = Response(payload(resource(2, Adi="New Collection")))
            clock.return_value += 1
            self.assertEqual(client.search()["resources"][0]["label"], "New Collection")
            self.assertEqual(session.get.call_count, 2)

    def test_cache_is_per_client_and_does_not_grow_with_search_variants(self):
        one, session, _ = client_for()
        two, _, _ = client_for(payload(resource(2, Adi="Second Client")))
        for query in ("abi", "proquest", "business", "missing", "economics"):
            one.search(query)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(len(one._snapshot), 1)
        self.assertEqual(two.search()["resources"][0]["label"], "Second Client")

    def test_concurrent_callers_share_one_refresh(self):
        client, session, response = client_for()
        entered, release = threading.Event(), threading.Event()
        start = threading.Barrier(8)

        def get(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Concurrent refresh was not released")
            return response

        def search():
            start.wait(timeout=3)
            return client.search()

        session.get.side_effect = get
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(search) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(3))
            finally:
                release.set()
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(all(result["matched_count"] == 1 for result in results))

    def test_failed_refresh_does_not_serve_stale_or_cache_the_error(self):
        client, session, _ = client_for()
        with patch.object(directory.time, "monotonic", return_value=100) as clock:
            client.search()
            clock.return_value = 100 + directory.CACHE_TTL_SECONDS
            session.get.return_value = Response(status=503)
            self.assert_failure(client, "http_error")
            session.get.return_value = Response(payload())
            self.assertEqual(client.search()["total_resources"], 0)
            self.assertEqual(session.get.call_count, 3)

    def test_credentials_proxies_hooks_and_environment_are_isolated_before_requests(self):
        session = requests.Session()
        session.get = Mock(return_value=Response(payload(resource())))
        session.auth = ("private-user", "private-password")
        session.proxies = {"https": "http://private-proxy.test"}
        session.headers.update({"Authorization": "private-token", "Cookie": "private-cookie", "Host": "evil.test"})
        session.cookies.set("private", "cookie")
        session.params = {"private": "query"}
        session.hooks = {"response": [lambda r: r]}
        session.cert = "private-client-certificate"
        session.verify = False
        client = ResearchResources(session)
        # Reintroducing caller state cannot leak it during a later refresh.
        session.auth = ("later-user", "later-password")
        session.headers["Proxy-Authorization"] = "later-token"
        session.cookies.set("later", "cookie")
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://ambient-proxy.test", "NETRC": "/nonexistent"}), \
                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc must not be consulted")):
            client.search()
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        self.assertEqual(session.proxies, {})
        self.assertEqual(session.params, {})
        self.assertEqual(dict(session.headers), {})
        self.assertEqual(dict(session.cookies), {})
        self.assertEqual(session.hooks, {"response": []})
        self.assertIsNone(session.cert)
        self.assertTrue(session.verify)
        args, kwargs = session.get.call_args
        self.assertEqual(args, (directory.DIRECTORY_API_URL,))
        self.assertEqual(kwargs["auth"], ())
        self.assertEqual(kwargs["proxies"], {})
        self.assertEqual(kwargs["cookies"], {})
        self.assertTrue(kwargs["verify"])
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")

    def test_real_request_preparation_has_no_auth_cookies_or_proxy(self):
        session = requests.Session()
        session.auth = ("private-user", "private-password")
        session.headers["Authorization"] = "private-token"
        session.cookies.set("private", "cookie", domain="service.library.itu.edu.tr")
        raw_response = requests.Response()
        raw_response.status_code = 200
        raw_response.url = directory.DIRECTORY_API_URL
        raw_response.headers["Content-Type"] = "application/json"
        raw_response._content = json.dumps(payload(resource())).encode()
        raw_response._content_consumed = True
        client = ResearchResources(session)
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://ambient-proxy.test", "NETRC": "/nonexistent"}), \
                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("No netrc")), \
                patch.object(session, "send", return_value=raw_response) as send:
            client.search()
        request = send.call_args.args[0]
        for key in ("Authorization", "Proxy-Authorization", "Cookie"):
            self.assertNotIn(key, request.headers)
        self.assertEqual(request.url, directory.DIRECTORY_API_URL)
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertFalse(send.call_args.kwargs["allow_redirects"])

    def test_only_exact_official_https_endpoint_can_be_requested(self):
        bad_urls = ["http://service.library.itu.edu.tr/web/api/Veritabanlari/FullList",
                    "https://service.library.itu.edu.tr.evil.test/web/api/Veritabanlari/FullList",
                    "https://user:secret@service.library.itu.edu.tr/web/api/Veritabanlari/FullList",
                    "https://service.library.itu.edu.tr:8443/web/api/Veritabanlari/FullList",
                    "https://service.library.itu.edu.tr/other", directory.DIRECTORY_API_URL + "?url=elsewhere",
                    directory.DIRECTORY_API_URL + "#fragment"]
        for url in bad_urls:
            with self.subTest(url=url), patch.object(directory, "DIRECTORY_API_URL", url):
                client, session, _ = client_for()
                self.assert_failure(client, "untrusted_endpoint")
                session.get.assert_not_called()

    def test_redirects_and_unexpected_response_origins_are_rejected(self):
        for status in (301, 302, 307, 308):
            client, session, response = client_for(status=status, headers={"Location": "https://elsewhere.test"})
            self.assert_failure(client, "redirect_rejected")
            session.get.assert_called_once()
            self.assertEqual(response.read_chunks, 0)
            self.assertTrue(response.closed)
        client, _, _ = client_for(url="https://elsewhere.test/web/api/Veritabanlari/FullList")
        self.assert_failure(client, "untrusted_endpoint")
        client, _, response = client_for()
        response.history = [object()]
        self.assert_failure(client, "redirect_rejected")

    def test_unsafe_links_are_removed_without_dropping_valid_records(self):
        unsafe = ["javascript:alert(1)", "data:text/html,unsafe", "file:///etc/passwd", "//evil.test/path",
                  "https://user:secret@example.org", "https://@example.org", "https://example.org:8443/",
                  "http://example.org:443/", "https://example.org:bad/", "https://example.org:99999/",
                  "https://127.0.0.1/", "https://[::1]/", "https://10.0.0.1/", "https://169.254.169.254/",
                  "https://localhost/", "https://host.local/", "https://host.internal/", "https://singlelabel/",
                  "https://example.org\\@evil.test/", "https://example.org/%0d%0aCookie:secret",
                  "https://example.org/%5cpath", "https://evil\n.example.org/", "https://-invalid.test/",
                  "https://2130706433/", "https://127.1/", "https://0177.0.0.1/", "https://example.org./",
                  None, {}, 13, "x" * 4097]
        for link in unsafe:
            with self.subTest(link=link):
                client, session, _ = client_for(payload(resource(Link=link, KampusDisiLink=link)))
                row = client.search()["resources"][0]
                self.assertIsNone(row["public_entry_url"])
                self.assertIsNone(row["institutional_entry_url"])
                session.get.assert_called_once()

    def test_reported_http_default_port_and_https_links_are_metadata_only(self):
        for link in ("http://search.ebscohost.com/login.aspx?authtype=ip,uid", "http://example.org:80/entry",
                     "https://example.org:443/entry", "https://example.org/search?q=power%20systems",
                     RESOURCE["KampusDisiLink"]):
            with self.subTest(link=link):
                client, session, _ = client_for(payload(resource(Link=link)))
                result = client.search()
                self.assertEqual(result["resources"][0]["public_entry_url"], link)
                self.assertFalse(result["retrieval_authorized"])
                self.assertEqual(session.get.call_args.args[0], directory.DIRECTORY_API_URL)

    def test_html_is_plain_text_and_embedded_urls_are_never_followed(self):
        html = ('<p>Useful <b>research</b> &amp; references.</p><script>private-script</script>'
                '<style>private-style</style><iframe src="https://elsewhere.test">private-frame</iframe>'
                '<a href="javascript:alert(1)">Guide</a><img src="https://elsewhere.test/pixel">')
        client, session, _ = client_for(payload(resource(Aciklama_en=html, Adi_en="<b>English Label</b>")))
        row = client.search()["resources"][0]
        self.assertEqual(row["label"], "English Label")
        self.assertEqual(row["description"], "Useful research & references. Guide")
        session.get.assert_called_once()

    def test_unrecognized_source_keys_and_access_claims_are_not_exposed(self):
        row = resource(AcikErisim=True, KurumsalErisim=True, private_key="must-not-escape",
                       token="must-not-escape", Dosyalar=[{"secret": "must-not-escape"}])
        row["Filtreler"][0]["private_key"] = "must-not-escape"
        client, _, _ = client_for(payload(row))
        result = client.search()
        serialized = json.dumps(result)
        self.assertNotIn("must-not-escape", serialized)
        for key in ("Adi", "Filtreler", "AcikErisim", "KurumsalErisim", "Dosyalar", "token"):
            self.assertNotIn('"' + key + '"', serialized)
        self.assertEqual(result["entitlement"], "not_checked")
        self.assertFalse(result["retrieval_authorized"])

    def test_malformed_envelopes_and_records_raise_instead_of_appearing_empty(self):
        malformed = [[], {}, {"Toplam": 0}, {"Liste": []}, {"Toplam": "0", "Liste": []},
                     {"Toplam": True, "Liste": []}, {"Toplam": -1, "Liste": []},
                     {"Toplam": 1, "Liste": []}, {"Toplam": 0, "Liste": {}}, payload(None),
                     payload(resource(), resource()), payload(resource(IdOverride="ignored"), False),
                     payload(resource(True)), payload(resource(0)), payload(resource(Adi="")),
                     payload(resource(Adi=None)), payload(resource(Adi={})), payload(resource(Aciklama_en=[])),
                     payload(resource(Filtreler=None)), payload(resource(Filtreler="not a list")),
                     payload(resource(Filtreler=[None])), payload(resource(Filtreler=[{}])),
                     payload(resource(Filtreler=[{"FiltreTurId": True, "Adi": "bad"}])),
                     payload(resource(Filtreler=[{"FiltreTurId": 1, "Adi": None}]))]
        for data in malformed:
            with self.subTest(data=str(data)[:180]):
                client, _, response = client_for(data)
                self.assert_failure(client, "invalid_response")
                self.assertTrue(response.closed)

    def test_directory_filter_and_text_bounds_prevent_unbounded_cache(self):
        for data in (payload(*(resource(i + 1) for i in range(directory.MAX_RECORDS + 1))),
                     payload(resource(Filtreler=[{"FiltreTurId": 2}] * 257)),
                     payload(resource(Aciklama_en="x" * 128_001))):
            client, _, _ = client_for(data)
            self.assert_failure(client, "invalid_response")
            self.assertIsNone(client._snapshot)

    def test_invalid_json_content_type_encoding_and_length(self):
        for body in (b"not-json private-secret", b"", b"\xff", b'{"Toplam":0,"Liste":', b"[" * 2000):
            client, _, _ = client_for(body=body)
            message = self.assert_failure(client, "invalid_response")
            self.assertNotIn("private-secret", message)
        for headers in ({"Content-Type": "text/html"}, {"Content-Encoding": "gzip"},
                        {"Content-Length": "-1"}, {"Content-Length": "unknown"}):
            client, _, response = client_for(headers=headers)
            self.assert_failure(client, "invalid_response")
            self.assertEqual(response.read_chunks, 0)

    def test_advertised_oversize_is_rejected_before_streaming(self):
        self.assertEqual(directory.MAX_RESPONSE_BYTES, 8 * 1024 * 1024)
        client, _, response = client_for(headers={"Content-Length": str(directory.MAX_RESPONSE_BYTES + 1)})
        self.assert_failure(client, "response_too_large")
        self.assertEqual(response.read_chunks, 0)
        self.assertTrue(response.closed)

    def test_stream_limit_catches_missing_and_false_small_content_lengths(self):
        chunk = b"x" * 65536
        for headers in ({}, {"Content-Length": "1"}):
            with self.subTest(headers=headers):
                client, _, response = client_for(chunks=(chunk for _ in range(200)), headers=headers)
                self.assert_failure(client, "response_too_large")
                self.assertEqual(response.read_chunks, directory.MAX_RESPONSE_BYTES // 65536 + 1)
                self.assertTrue(response.closed)
                self.assertIsNone(client._snapshot)

    def test_exact_response_limit_is_accepted_and_next_byte_is_rejected(self):
        body = json.dumps(payload()).encode()
        with patch.object(directory, "MAX_RESPONSE_BYTES", len(body)):
            client, _, _ = client_for(body=body)
            self.assertEqual(client.search()["total_resources"], 0)
            client, _, _ = client_for(body=body + b" ")
            self.assert_failure(client, "response_too_large")

    def test_stream_time_budget_and_network_errors_are_explicit_and_redacted(self):
        client, _, response = client_for()
        with patch.object(directory.time, "monotonic", side_effect=[0, directory._REQUEST_SECONDS + 1]):
            self.assert_failure(client, "request_timeout")
        self.assertTrue(response.closed)
        for error in (requests.Timeout("private-url"), requests.ConnectionError("private-token")):
            client, session, _ = client_for()
            session.get.side_effect = error
            message = self.assert_failure(client, "network_error")
            self.assertNotIn("private", message)

    def test_stream_transport_failure_closes_response_and_does_not_cache(self):
        client, _, response = client_for()

        def broken_stream(_):
            yield b'{"Toplam":'
            raise requests.exceptions.ChunkedEncodingError("private-response-data")

        response.iter_content = broken_stream
        message = self.assert_failure(client, "network_error")
        self.assertNotIn("private-response-data", message)
        self.assertTrue(response.closed)
        self.assertIsNone(client._snapshot)

    def test_argument_validation_happens_before_any_request(self):
        cases = [{"query": None}, {"query": 1}, {"query": "x" * 501}, {"query": "!!!"},
                 {"query": "the and ve"}, {"query": " ".join("word" + str(i) for i in range(33))},
                 {"subject": ""}, {"subject": " "}, {"subject": []}, {"subject": "x" * 257},
                 {"subject": "and ve"}, {"limit": True}, {"limit": 0}, {"limit": 101},
                 {"limit": 1.5}, {"limit": "10"}]
        client, session, _ = client_for()
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                client.search(**values)
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
