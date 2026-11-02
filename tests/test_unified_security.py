"""Independent permission, reference and privacy checks for unified reads.

All data and transports are synthetic. These tests never contact an ITU service.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ninova_mcp import unified


BASE = "https://ninova.itu.edu.tr"
COURSE = "12345.678901"


def application(operations=None):
    app = SimpleNamespace(enabled_operations=operations)
    app.client = SimpleNamespace(base_url=BASE, ensure_logged_in=Mock(), get_html=Mock())
    app._load_tracking_state_document = Mock(return_value={"courses": {}})
    app.list_courses = Mock(return_value={"courses": []})
    app.mail_search = Mock(return_value={"messages": [], "matched_by_mailbox": {}})
    app.mail_get_message = Mock(return_value={"subject": "Synthetic message"})
    app.get_dashboard_announcements = Mock(return_value={"announcements": []})
    app.get_dashboard_assignments = Mock(return_value={"assignments": []})
    app.obs_get_schedule = Mock(return_value={
        "semester": {"akademikDonemId": 42},
        "schedule": {"kayitSinifResultList": []},
        "final_calendar": {"finalBilgiList": []},
    })
    app.obs = SimpleNamespace(list_semesters=Mock(return_value={
        "ogrenciDonemListesi": [{"akademikDonemId": 42}],
    }))
    app.obs_list_semesters = Mock(return_value={
        "ogrenciDonemListesi": [{"akademikDonemId": 42}],
    })
    app.obs_list_registered_courses = Mock(return_value={"kayitSinifResultList": []})
    app.obs_get_notifications = Mock(return_value={"notifications": []})
    app.obs_get_help_tickets = Mock(return_value={"tickets": []})
    app.get_help_ticket = Mock(return_value={"ticket": {"title": "Synthetic ticket"}})
    app.read_resource_text = Mock(return_value={"text": "Synthetic document"})
    app.get_assignment_upload_slots = Mock(return_value={"slots": []})
    app.coursehub_materials = Mock(return_value={
        "site": "mathematics", "authenticated": False,
        "source_url": "https://mathavuz.itu.edu.tr/", "coverage": "root_links_only",
        "materials": [{"title": "Mathematics safety guide", "url": "https://mathavuz.itu.edu.tr/files/guide.pdf",
                       "kind": "document", "collection": "guides", "course_codes": [],
                       "course_match": "general_or_unknown", "readable": True,
                       "text_extraction_supported": True}],
        "count": 1, "total_count": 1, "has_more": False,
        "omitted": {"links": 0}, "errors": [],
    })
    app.coursehub_page = Mock(return_value={"title": "Public mathematics guidance", "text": "Public page"})
    app.coursehub_text = Mock(return_value={"ok": True, "text": "Synthetic public document"})
    return app


def encoded(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class UnifiedReferenceSecurityTests(unittest.TestCase):
    def test_arbitrary_urls_and_malformed_references_never_dispatch(self):
        app = application()
        malformed = [
            "https://127.0.0.1/private", "file:///etc/passwd",
            "itu://mail/inbox/1?url=https://evil.example", "itu://mail/inbox/../2",
            "itu://mail/inbox/0", "itu://mail/sent/1#fragment",
            "itu://ninova/12345.678901/announcement/2%2f..",
            "itu://ninova/12345.678901/file/class/2?target=3",
            "itu://obs/course/42/12345/extra", "itu://portal/help/not-digits",
            "itu://portal/help/" + "1" * 21,
            "itu://portal/notification/%2e%2e", "itu://unknown/value",
        ]
        for ref in malformed:
            with self.subTest(ref=ref):
                result = unified.open_ref(app, ref)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_ref")
        app.client.ensure_logged_in.assert_not_called()
        app.client.get_html.assert_not_called()
        app.mail_get_message.assert_not_called()
        app.obs_get_schedule.assert_not_called()
        app.get_help_ticket.assert_not_called()

    def test_reference_input_is_validated_before_any_length_truncation(self):
        app = application()
        # Truncating the raw request before validation would silently turn this
        # oversized input into a usable mail reference.
        ref = "itu://mail/inbox/1".ljust(unified.MAX_REF_CHARS) + "unexpected-suffix"
        result = unified.open_ref(app, ref)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_ref")
        app.mail_get_message.assert_not_called()

    def test_primary_open_permissions_cover_every_reference_family(self):
        app = application(set())
        refs = [
            "itu://mail/inbox/1", "itu://mail/sent/1",
            f"itu://ninova/{COURSE}/announcement/1",
            f"itu://ninova/{COURSE}/assignment/1",
            f"itu://ninova/{COURSE}/file/class/1",
            f"itu://ninova/{COURSE}/file/lesson/1",
            "itu://obs/course/42/12345", "itu://obs/exam/42/12345",
            "itu://portal/help/1", "itu://portal/notification/notice-1",
        ]
        for ref in refs:
            with self.subTest(ref=ref):
                result = unified.open_ref(app, ref)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "disabled")
        app.client.ensure_logged_in.assert_not_called()
        app.list_courses.assert_not_called()
        app._load_tracking_state_document.assert_not_called()
        app.mail_get_message.assert_not_called()
        app.obs_get_schedule.assert_not_called()
        app.get_help_ticket.assert_not_called()

    def test_published_tools_and_operation_masks_are_both_required(self):
        for enabled, published in [({"mail_get_message"}, set()),
                                   (set(), {"mail_get_message"}), (None, set())]:
            with self.subTest(enabled=enabled, published=published):
                app = application(enabled)
                app.published_tool_names = published
                result = unified.open_ref(app, "itu://mail/inbox/1")
                self.assertEqual(result["error"]["code"], "disabled")
                app.mail_get_message.assert_not_called()

    def test_ninova_ownership_does_not_accept_a_foreign_host_suffix(self):
        app = application()
        app.list_courses.return_value = {"courses": [{
            "url": f"https://evil.example/Sinif/{COURSE}", "title": "Foreign link",
        }]}
        result = unified.open_ref(app, f"itu://ninova/{COURSE}/announcement/1")
        self.assertEqual(result["status"], "error")
        app.client.get_html.assert_not_called()

    def test_ninova_ownership_list_obeys_the_operation_mask(self):
        app = application({"get_course_announcements"})
        result = unified.open_ref(app, f"itu://ninova/{COURSE}/announcement/1")
        self.assertEqual(result["status"], "error")
        app.list_courses.assert_not_called()
        app.client.get_html.assert_not_called()

    def test_obs_optional_registered_course_lookup_obeys_the_operation_mask(self):
        app = application({"obs_get_schedule", "obs_list_semesters"})
        app.obs_get_schedule.return_value["schedule"]["kayitSinifResultList"] = [{
            "crn": "12345", "dersKodu": "MAT103E", "dersAdiEN": "Mathematics",
        }]
        result = unified.open_ref(app, "itu://obs/course/42/12345")
        app.obs_list_registered_courses.assert_not_called()
        self.assertIn(result["status"], {"ok", "error"})

    def test_ninova_url_reference_builder_rejects_foreign_or_credentialed_hosts(self):
        for host in ["ninova.itu.edu.tr.evil.example", "evil.example", "user@ninova.itu.edu.tr",
                     "ninova.itu.edu.tr:444"]:
            with self.subTest(host=host):
                self.assertIsNone(unified.ninova_ref(
                    f"https://{host}/Sinif/{COURSE}/Duyuru/1", BASE))


class UnifiedCursorSecurityTests(unittest.TestCase):
    def test_non_object_cursor_payloads_are_clean_validation_errors(self):
        for value in [None, [], [1], "value", 1, True]:
            with self.subTest(value=value), self.assertRaises(unified.UnifiedError):
                unified.decode_cursor(encoded(value), "query-key")

    def test_cursor_offsets_are_bounded_strict_integers(self):
        for offset in [-1, 10001, True, "1", 1.5, None]:
            with self.subTest(offset=offset), self.assertRaises(unified.UnifiedError):
                unified.decode_cursor(encoded({"o": offset, "k": "query-key"}), "query-key")
        self.assertEqual(unified.decode_cursor(encoded({"o": 10000, "k": "query-key"}), "query-key"), 10000)

    def test_cursor_is_bound_to_the_original_query(self):
        app = application(set())
        cursor = unified.encode_cursor(1, "unrelated-key")
        with self.assertRaises(unified.UnifiedError):
            unified.search(app, "mathematics", scopes=["mail"], cursor=cursor)
        app.mail_search.assert_not_called()
        app.client.ensure_logged_in.assert_not_called()


class UnifiedPrivacySecurityTests(unittest.TestCase):
    def test_disabled_search_scopes_never_read_account_data(self):
        app = application(set())
        result = unified.search(app, "mathematics", scopes=["mail", "ninova", "obs", "portal"])
        self.assertEqual(result["results"], [])
        self.assertTrue(all(value["status"] == "disabled" for value in result["sources"].values()))
        app.mail_search.assert_not_called()
        app.client.ensure_logged_in.assert_not_called()
        app._load_tracking_state_document.assert_not_called()
        app.obs_get_schedule.assert_not_called()
        app.obs_get_help_tickets.assert_not_called()

    def test_mail_search_does_not_request_body_content_by_default(self):
        app = application()
        unified.search(app, "mathematics", scopes=["mail"])
        self.assertIs(app.mail_search.call_args.kwargs["in_body"], False)

    def test_open_error_payload_does_not_echo_upstream_credentials_or_body(self):
        app = application()
        app.mail_get_message.side_effect = RuntimeError(
            "https://user:PRIVATE_PASSWORD@example.test/?token=PRIVATE_TOKEN "
            "Authorization: Bearer PRIVATE_BEARER PRIVATE_MESSAGE_BODY")
        result = unified.open_ref(app, "itu://mail/inbox/1")
        self.assertEqual(result["status"], "error")
        rendered = json.dumps(result)
        for secret in ["PRIVATE_PASSWORD", "PRIVATE_TOKEN", "PRIVATE_BEARER", "PRIVATE_MESSAGE_BODY"]:
            self.assertNotIn(secret, rendered)
        self.assertLess(len(result["error"]["message"]), 400)

    def test_search_error_payload_does_not_echo_upstream_secrets(self):
        app = application()
        app.mail_search.side_effect = RuntimeError("PRIVATE_TOKEN PRIVATE_MAIL_BODY")
        result = unified.search(app, "mathematics", scopes=["mail"])
        self.assertEqual(result["sources"]["mail"]["status"], "error")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertNotIn("PRIVATE_MAIL_BODY", json.dumps(result))


class UnifiedProviderRoundtripTests(unittest.TestCase):
    def test_mail_header_search_opens_the_exact_uid_and_mailbox(self):
        app = application()
        uid = "123456789012345678"
        app.mail_search.return_value = {
            "messages": [{"mailbox": mailbox, "uid": uid, "subject": "Mathematics notice",
                          "date_iso": "2026-11-02T10:00:00+03:00", "from": "staff@example.test",
                          "to": "student@example.test", "unread": True}
                         for mailbox in ("INBOX", "SENT")],
            "matched_by_mailbox": {"INBOX": 1, "SENT": 1}, "matched_count": 2,
        }

        def message(selected_uid, *, mailbox, max_chars):
            self.assertEqual(selected_uid, uid)
            return {"mailbox": mailbox, "uid": selected_uid, "read_only": True,
                    "subject": "Mathematics notice", "body_text": "Selected synthetic message.",
                    "attachments": [], "attachment_count": 0, "body_truncated": False}

        app.mail_get_message.side_effect = message
        result = unified.search(app, "mathematics", scopes=["mail"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["sources"]["mail"]["status"], "ok")
        app.mail_get_message.assert_not_called()
        for hit in result["results"]:
            mailbox = "SENT" if "/sent/" in hit["ref"] else "INBOX"
            with self.subTest(mailbox=mailbox):
                self.assertTrue(hit["ref"].endswith("/" + uid))
                opened = unified.open_ref(app, hit["ref"], max_chars=1000)
                self.assertEqual(opened["status"], "ok")
                self.assertEqual(opened["data"]["mailbox"], mailbox)
                self.assertTrue(opened["data"]["read_only"])
                self.assertEqual(opened["data"]["body_text"], "Selected synthetic message.")
                app.mail_get_message.assert_called_with(uid, mailbox=mailbox, max_chars=1000)

    def test_ninova_dashboard_results_open_only_the_owned_course(self):
        app = application()
        course = {"url": f"{BASE}/Sinif/{COURSE}", "code": "MAT103E", "title": "Mathematics I"}
        notice_url = course["url"] + "/Duyuru/7"
        assignment_url = course["url"] + "/Odev/8"
        notice = {"url": notice_url, "title": "Mathematics exam notice", "author": "Course staff",
                  "published_at": "02.11.2026 10:00", "summary": "Bring a calculator."}
        assignment = {"url": assignment_url, "title": "Mathematics assignment",
                      "description": "Solve the selected problems.", "submission_end": "05.11.2026 17:00"}
        app.get_dashboard_announcements.return_value = {"announcements": [notice]}
        app.get_dashboard_assignments.return_value = {"assignments": [assignment]}
        app.list_courses.return_value = {"courses": [course]}
        app._load_tracking_state_document.return_value = {"courses": {COURSE: {
            "course": course, "snapshot": {"overview": {"announcements": [notice], "assignments": [assignment]}},
        }}}
        pages = {
            notice_url: "<h1>Mathematics exam notice</h1><p>02.11.2026 10:00</p><p>Bring a calculator.</p>",
            assignment_url: "<h1>Mathematics assignment</h1><p>Teslim Başlangıcı</p><p>02.11.2026 09:00</p>"
                "<p>Teslim Bitişi</p><p>05.11.2026 17:00</p><p>Ödev Açıklaması</p>"
                "<p>Solve the selected problems.</p><p>Kaynak Dosyalar</p>",
        }
        app.client.get_html.side_effect = lambda url: (pages[url], SimpleNamespace(url=url))
        result = unified.search(app, "mathematics", scopes=["ninova"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["sources"]["ninova"]["status"], "ok")
        app.client.get_html.assert_not_called()
        for hit in result["results"]:
            with self.subTest(kind=hit["kind"]):
                opened = unified.open_ref(app, hit["ref"])
                self.assertEqual(opened["status"], "ok")
                self.assertFalse(opened["partial"])
                self.assertEqual(opened["data"]["course"]["url"], course["url"])
                self.assertEqual(opened["data"]["title"], hit["title"])
                if hit["kind"] == "announcement":
                    self.assertIn("Bring a calculator.", opened["data"]["body_text"])
                    self.assertEqual(opened["data"]["author"], "Course staff")
                else:
                    self.assertIn("Solve the selected problems.", opened["data"]["description"])
        self.assertEqual([call.args[0] for call in app.client.get_html.call_args_list],
                         [hit["url"] for hit in result["results"]])
        app.get_assignment_upload_slots.assert_not_called()
        app.read_resource_text.assert_not_called()

    def test_obs_schedule_result_opens_the_owned_semester_course_and_exam(self):
        app = application()
        app.obs_get_schedule.return_value = {
            "semester": {"akademikDonemId": 42, "akademikDonemAdiEN": "2026 Fall"},
            "schedule": {"kayitSinifResultList": [{
                "crn": "12345", "bransKodu": "MAT", "dersKodu": "103E", "dersAdiEN": "Mathematics I",
                "sinifYerZaman": [{"gunKodu": "T", "baslangicSaati": 540, "bitisSaati": 600,
                                   "binaKodu": "D", "mekanAdi": "101"}],
            }]},
            "final_calendar": {"finalBilgiList": [{
                "crn": "12345", "bransKodu": "MAT", "dersKodu": "103E", "dersAdiEN": "Mathematics I",
                "baslangicTarihi": "2026-12-20T10:00:00+03:00", "bitisTarihi": "2026-12-20T12:00:00+03:00",
                "finalMekanList": [{"binaAdi": "D", "derslikKodu": "101"}],
            }]},
        }
        app.obs_list_registered_courses.return_value = {"kayitSinifResultList": [
            {"crn": "12345", "sinifId": 1234}]}
        result = unified.search(app, "Mathematics", scopes=["obs"])
        self.assertEqual(result["count"], 2)
        for hit in result["results"]:
            with self.subTest(kind=hit["kind"]):
                self.assertEqual(hit["ref"], f"itu://obs/{hit['kind']}/42/12345")
                opened = unified.open_ref(app, hit["ref"])
                self.assertEqual(opened["status"], "ok")
                self.assertEqual(opened["data"]["semester"]["akademikDonemId"], 42)
                self.assertEqual(opened["data"]["crn"], "12345")
                self.assertEqual(opened["data"]["class_id"], 1234)
                self.assertEqual(opened["data"]["meetings"][0]["start"], "09:00")
                self.assertEqual(opened["data"]["final_exams"][0]["place"], "D 101")
                app.obs_get_schedule.assert_called_with(semester="42")

    def test_portal_result_opens_the_exact_own_ticket_and_notification(self):
        app = application()
        ticket_id = "123456789012345678"
        ticket = {"id": ticket_id, "title": "Registration question", "status": "Open",
                  "unit": "Student affairs", "category": "Registration", "description": "Synthetic request."}
        notification = {"id": "notice-42", "title": "Registration notice", "content": "Registration guidance.",
                        "created_at": "2026-11-02T10:00:00+03:00", "unread": True}
        app.obs_get_help_tickets.return_value = {"tickets": [ticket], "count": 1}
        app.obs_get_notifications.side_effect = lambda notification_id=None, limit=20: (
            {"notification": notification} if notification_id == notification["id"]
            else {"notifications": [notification], "count": 1})

        def own_ticket(identifier):
            self.assertEqual(identifier, ticket_id)
            return {"ticket": ticket, "untrusted_external_content": True}

        app.get_help_ticket.side_effect = own_ticket
        result = unified.search(app, "Registration", scopes=["portal"])
        self.assertEqual(result["count"], 2)
        app.get_help_ticket.assert_not_called()
        self.assertFalse(app.obs_get_help_tickets.call_args.kwargs["include_details"])
        for hit in result["results"]:
            with self.subTest(kind=hit["kind"]):
                opened = unified.open_ref(app, hit["ref"])
                self.assertEqual(opened["status"], "ok")
                self.assertEqual(opened["data"]["title"], hit["title"])
                if hit["kind"] == "help_ticket":
                    self.assertEqual(hit["ref"], "itu://portal/help/" + ticket_id)
                    self.assertEqual(opened["data"]["id"], ticket_id)
                    self.assertEqual(opened["data"]["description"], "Synthetic request.")
                else:
                    self.assertEqual(opened["data"]["content"], "Registration guidance.")
                    app.obs_get_notifications.assert_called_with(notification_id="notice-42")


class UnifiedExecutionSecurityTests(unittest.TestCase):
    def test_timed_out_work_remains_bounded_for_the_same_application(self):
        app = application()
        release = threading.Event()
        entered = []
        lock = threading.Lock()

        def blocked():
            with lock:
                entered.append(threading.get_ident())
            release.wait(2)
            return "finished"

        outcomes = []
        started = time.monotonic()
        try:
            for _ in range(unified.MAX_APP_PENDING + 3):
                outcomes.append(unified.run_concurrently({"source": (blocked, 0.01)}, app=app)["source"])
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(any(item.get("code") == "timeout" for item in outcomes))
            self.assertTrue(any(item.get("code") == "busy" for item in outcomes))
            with lock:
                self.assertLessEqual(len(entered), unified.MAX_APP_PENDING)
            self.assertTrue(all(not item["ok"] for item in outcomes))
        finally:
            release.set()


class UnifiedCourseHubSecurityTests(unittest.TestCase):
    def find(self, app):
        result = unified.search(app, "mathematics", scopes=["coursehub"], coursehub_sites=["MAT"])
        self.assertEqual(result["count"], 1)
        return result["results"][0]["ref"]

    def test_search_returns_only_metadata_and_uses_public_access_by_default(self):
        app = application()
        ref = self.find(app)
        self.assertTrue(ref.startswith("itu://coursehub/"))
        self.assertNotIn("mathavuz", ref)
        self.assertNotIn("guide.pdf", ref)
        self.assertIs(app.coursehub_materials.call_args.kwargs["authenticated"], False)
        app.coursehub_page.assert_not_called()
        app.coursehub_text.assert_not_called()
        app.client.ensure_logged_in.assert_not_called()
        app.mail_search.assert_not_called()

    def test_ref_cannot_be_opened_by_a_different_application(self):
        first, other = application(), application()
        ref = self.find(first)
        result = unified.open_ref(other, ref)
        self.assertEqual(result["status"], "error")
        other.coursehub_page.assert_not_called()
        other.coursehub_text.assert_not_called()
        other.coursehub_materials.assert_not_called()

    def test_source_url_is_never_accepted_as_a_document_reference(self):
        app = application()
        self.find(app)
        result = unified.open_ref(app, "https://mathavuz.itu.edu.tr/files/guide.pdf")
        self.assertEqual(result["status"], "error")
        app.coursehub_text.assert_not_called()

    def test_open_rechecks_the_current_document_permission(self):
        app = application()
        ref = self.find(app)
        app.enabled_operations = {"coursehub_materials"}
        result = unified.open_ref(app, ref)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "disabled")
        app.coursehub_text.assert_not_called()

    def test_open_preserves_the_reference_public_access_mode(self):
        app = application()
        ref = self.find(app)
        result = unified.open_ref(app, ref, max_chars=1000)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(app.coursehub_text.call_args.kwargs["url"],
                         "https://mathavuz.itu.edu.tr/files/guide.pdf")
        self.assertIs(app.coursehub_text.call_args.kwargs["authenticated"], False)
        self.assertEqual(app.coursehub_text.call_args.kwargs["max_chars"], 1000)
        app.coursehub_page.assert_not_called()

    def test_expired_ref_is_refused_before_any_reader_runs(self):
        app = application()
        ref = self.find(app)
        later = time.monotonic() + unified.REF_TTL_SECONDS + 1
        with patch.object(unified.time, "monotonic", return_value=later):
            result = unified.open_ref(app, ref)
        self.assertEqual(result["status"], "error")
        app.coursehub_page.assert_not_called()
        app.coursehub_text.assert_not_called()

    def test_unsafe_upstream_material_links_never_receive_usable_refs(self):
        for url in ["https://127.0.0.1/private.pdf", "https://mathavuz.itu.edu.tr.evil.example/file.pdf",
                    "https://user:password@mathavuz.itu.edu.tr/file.pdf", "http://mathavuz.itu.edu.tr/file.pdf",
                    "https://mathavuz.itu.edu.tr/logout.php"]:
            with self.subTest(url=url):
                app = application()
                app.coursehub_materials.return_value["materials"][0]["url"] = url
                result = unified.search(app, "mathematics", scopes=["coursehub"], coursehub_sites=["MAT"])
                self.assertEqual(result["count"], 0)
                app.coursehub_page.assert_not_called()
                app.coursehub_text.assert_not_called()

    def test_reference_storage_evicts_old_entries_at_its_capacity(self):
        app = application()
        refs = []
        with patch.object(unified, "MAX_STORED_REFS", 3):
            for index in range(5):
                app.coursehub_materials.return_value["materials"][0]["url"] = (
                    f"https://mathavuz.itu.edu.tr/files/guide-{index}.pdf")
                refs.append(self.find(app))
            old = unified.open_ref(app, refs[0])
            self.assertEqual(old["status"], "error")
            app.coursehub_text.assert_not_called()
            latest = unified.open_ref(app, refs[-1])
        self.assertEqual(latest["status"], "ok")
        self.assertEqual(app.coursehub_text.call_count, 1)

    def test_invalid_site_values_are_rejected_before_any_source_call(self):
        for value in [["https://127.0.0.1/"], ["unknown"], ["MAT", "foreign.example"]]:
            with self.subTest(value=value):
                app = application()
                with self.assertRaises(unified.UnifiedError):
                    unified.search(app, "mathematics", scopes=["coursehub"], coursehub_sites=value)
                app.coursehub_materials.assert_not_called()
                app.client.ensure_logged_in.assert_not_called()


class UnifiedOpenTimeoutSecurityTests(unittest.TestCase):
    def test_open_ref_has_a_bounded_wait_for_an_upstream_read(self):
        app = application()
        release = threading.Event()
        app.mail_get_message.side_effect = lambda *args, **kwargs: release.wait(2)
        started = time.monotonic()
        try:
            with patch.object(unified, "SOURCE_TIMEOUT_SECONDS", 0.02):
                result = unified.open_ref(app, "itu://mail/inbox/1")
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "timeout")
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
