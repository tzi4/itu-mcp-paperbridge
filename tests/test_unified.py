"""Offline behavior checks for bounded unified search and selected reads."""
from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ninova_mcp import unified


def app():
    application = SimpleNamespace(enabled_operations=None)
    application.client = SimpleNamespace(base_url="https://ninova.itu.edu.tr", ensure_logged_in=Mock())
    application.coursehub_materials = Mock(return_value={
        "site": "mathematics", "coverage": "root_links_only", "materials": [], "errors": [],
        "pages_read": 1, "has_more": False, "omitted": {"links": 0},
    })
    application.coursehub_text = Mock(return_value={"text": "Selected content"})
    application.coursehub_page = Mock(return_value={"title": "Selected page", "text": "Static guidance"})
    return application


def material(title="1 »", url="https://mathavuz.itu.edu.tr/show_worksheet.php?ws=MAT103Ews1", **values):
    return {"title": title, "url": url, "kind": "document", "collection": "practice",
            "course_codes": ["MAT103E"], "course_match": "exact_course",
            "text_extraction_supported": True, **values}


class CourseHubSearchTests(unittest.TestCase):
    def test_native_worksheet_matches_collection_alias_without_downloading(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material()]
        result = unified.search(application, "MAT103E worksheet", scopes=["coursehub"], coursehub_sites=["MAT"],
                                coursehub_course_code="MAT103E")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["sources"]["coursehub"]["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertTrue(result["untrusted_external_content"])
        self.assertNotIn("_target", json.dumps(result))
        application.coursehub_text.assert_not_called()
        application.coursehub_page.assert_not_called()
        opened = unified.open_ref(application, result["results"][0]["ref"], 4500)
        self.assertEqual(opened["data"]["text"], "Selected content")
        self.assertEqual(application.coursehub_text.call_args.kwargs["max_chars"], 4500)

    def test_observed_language_page_is_searchable_without_a_pdf(self):
        application = app()
        url = "https://ydy.itu.edu.tr/programlar/secmeli-diller/almanca"
        application.coursehub_materials.return_value.update({
            "site": "languages", "sources": [{"source_url": url, "source_title": "Almanca", "entry_point": url}],
            "entry_points": [{"url": url, "course_prefixes": ["ALM"]}], "coverage": "seed_pages_only",
        })
        result = unified.search(application, "ALM101", scopes=["coursehub"], coursehub_sites=["YDY"],
                                coursehub_course_code="ALM101")
        self.assertEqual(result["count"], 1)
        hit = result["results"][0]
        self.assertEqual(hit["kind"], "page")
        self.assertEqual(hit["title"], "Almanca")
        self.assertIsNone(hit["course"])
        opened = unified.open_ref(application, hit["ref"])
        self.assertEqual(opened["status"], "ok")
        self.assertEqual(application.coursehub_page.call_args.kwargs["site"], "languages")
        application.coursehub_text.assert_not_called()

    def test_chemistry_calendar_page_is_searchable_without_a_document(self):
        application = app()
        url = "https://kimya.itu.edu.tr/kimya-havuz/kim-101el"
        application.coursehub_materials.return_value.update({
            "sources": [{"source_url": url, "source_title": "KIM101EL", "entry_point": url}],
            "entry_points": [{"url": url, "course_codes": ["KIM101EL"]}],
        })
        result = unified.search(application, "KIM101EL calendar", scopes=["coursehub"],
                                coursehub_sites=["KIMLAB"], coursehub_course_code="KIM101EL")
        self.assertEqual(result["count"], 1)
        hit = result["results"][0]
        self.assertEqual(hit["title"], "KIM101EL")
        unified.open_ref(application, hit["ref"])
        self.assertEqual(application.coursehub_page.call_args.kwargs["url"], url)
        self.assertIs(application.coursehub_page.call_args.kwargs["authenticated"], False)

    def test_course_filter_selects_only_compatible_sites_and_preserves_suffix(self):
        application = app()
        unified.search(application, "ING103CO", scopes=["coursehub"], coursehub_course_code="ING103CO")
        self.assertEqual(application.coursehub_materials.call_count, 1)
        self.assertEqual(application.coursehub_materials.call_args.kwargs["site"], "languages")
        self.assertEqual(application.coursehub_materials.call_args.kwargs["course_code"], "ING103CO")

    def test_public_only_auth_and_incompatible_filters_fail_before_reads(self):
        for values in ({"coursehub_sites": ["YDY"], "coursehub_authenticated": True},
                       {"coursehub_sites": ["MAT"], "coursehub_course_code": "KIM101EL"},
                       {"coursehub_authenticated": "false"}, {"limit": True}, {"limit": 51},
                       {"include_body": "false"}, {"coursehub_course_code": True}):
            with self.subTest(values=values):
                application = app()
                with self.assertRaises(unified.UnifiedError):
                    unified.search(application, "test", scopes=["coursehub"], **values)
                application.coursehub_materials.assert_not_called()
                application.client.ensure_logged_in.assert_not_called()

    def test_authenticated_reference_keeps_original_mode_at_open(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material()]
        result = unified.search(application, "worksheet", scopes=["coursehub"], coursehub_sites=["MAT"],
                                coursehub_authenticated=True)
        unified.open_ref(application, result["results"][0]["ref"])
        self.assertIs(application.coursehub_text.call_args.kwargs["authenticated"], True)

    def test_unsupported_document_remains_metadata_and_never_downloads_on_open(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material(
            title="Worksheet archive", url="https://mathavuz.itu.edu.tr/archive.zip", text_extraction_supported=False)]
        result = unified.search(application, "worksheet", scopes=["coursehub"], coursehub_sites=["MAT"])
        opened = unified.open_ref(application, result["results"][0]["ref"])
        self.assertEqual(opened["error"]["code"], "unsupported")
        application.coursehub_text.assert_not_called()

    def test_merged_observed_labels_remain_searchable_without_claiming_one_collection(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material(
            title="Resource", collection=None, collections=["exams", "guides"],
            titles=["Resource", "Laboratory safety handbook"], course_codes=[])]
        result = unified.search(application, "safety handbook", scopes=["coursehub"], coursehub_sites=["MAT"])
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["results"][0]["collection"])
        self.assertIsNone(result["results"][0]["course"])

    def test_date_bounds_exclude_undated_metadata_with_explanation(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material()]
        result = unified.search(application, "worksheet", scopes=["coursehub"], coursehub_sites=["MAT"], since="2026-01-01")
        self.assertEqual(result["count"], 0)
        self.assertTrue(any("timestamp" in warning for warning in result["warnings"]))

    def test_cursor_is_bound_to_course_sites_auth_and_effective_permissions(self):
        application = app()
        application.coursehub_materials.return_value["materials"] = [material(), material(
            url="https://mathavuz.itu.edu.tr/show_worksheet.php?ws=MAT103Ews2")]
        params = {"scopes": ["coursehub"], "coursehub_sites": ["MAT"], "limit": 1}
        result = unified.search(application, "worksheet", **params)
        cursor = result["next_cursor"]
        self.assertIsNotNone(cursor)
        for change in ({"coursehub_sites": ["KIM"]}, {"coursehub_course_code": "MAT103E"},
                       {"coursehub_authenticated": True}):
            with self.subTest(change=change), self.assertRaises(unified.UnifiedError):
                unified.search(application, "worksheet", cursor=cursor, **{**params, **change})
        application.enabled_operations = {"coursehub_materials"}
        with self.assertRaises(unified.UnifiedError):
            unified.search(application, "worksheet", cursor=cursor, **params)

    def test_site_failure_keeps_other_site_results_without_leaking_exception(self):
        application = app()
        application.coursehub_materials.side_effect = [RuntimeError("secret-password"), {"materials": [material(
            url="https://kimyahavuz.itu.edu.tr/files/practice.pdf")]}]
        result = unified.search(application, "worksheet", scopes=["coursehub"], coursehub_sites=["MAT", "KIM"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["sources"]["coursehub"]["sites"]["mathematics"]["status"], "error")
        self.assertNotIn("secret-password", json.dumps(result))


class AccountSourceTests(unittest.TestCase):
    def test_ninova_dashboard_results_and_tracking_coverage(self):
        application = app()
        url = "https://ninova.itu.edu.tr/Sinif/123.456/Duyuru/7"
        application.get_dashboard_announcements = Mock(return_value={"announcements": [{
            "url": url, "title": "MAT103E exam notice", "summary": "Bring a calculator"}]})
        application.get_dashboard_assignments = Mock(return_value={"assignments": []})
        application._load_tracking_state_document = Mock(return_value={"courses": {}})
        result = unified.search(application, "exam", scopes=["ninova"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["ref"], "itu://ninova/123.456/announcement/7")
        self.assertEqual(result["sources"]["ninova"]["status"], "partial")
        application.client.ensure_logged_in.assert_called_once()

    def test_obs_schedule_matches_code_and_returns_structural_ref(self):
        application = app()
        application.obs_get_schedule = Mock(return_value={
            "semester": {"akademikDonemId": 42}, "schedule": {"kayitSinifResultList": [{
                "crn": "12345", "dersKodu": "MAT103E", "dersAdiEN": "Mathematics I"}]},
            "final_calendar": {"finalBilgiList": []}})
        result = unified.search(application, "MAT103E", scopes=["obs"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["ref"], "itu://obs/course/42/12345")
        self.assertIn("current term", result["sources"]["obs"]["coverage"])

    def test_portal_search_uses_summaries_and_open_checks_ticket_ownership_list_permission(self):
        application = app()
        application.obs_get_notifications = Mock(return_value={"notifications": []})
        application.obs_get_help_tickets = Mock(return_value={"tickets": [{"id": "12", "title": "Registration question"}]})
        result = unified.search(application, "Registration", scopes=["portal"])
        self.assertEqual(result["count"], 1)
        self.assertIs(application.obs_get_help_tickets.call_args.kwargs["include_details"], False)
        application.enabled_operations = {"get_help_ticket"}
        application.get_help_ticket = Mock()
        opened = unified.open_ref(application, result["results"][0]["ref"])
        self.assertEqual(opened["error"]["code"], "disabled")
        application.get_help_ticket.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def test_distinct_applications_have_independent_sso_lanes(self):
        first, second = app(), app()
        release, entered = threading.Event(), threading.Event()
        def held():
            entered.set()
            release.wait(1)
        thread = threading.Thread(target=lambda: unified._sso_read(first, held))
        thread.start()
        try:
            self.assertTrue(entered.wait(.5))
            started = time.monotonic()
            self.assertEqual(unified._sso_read(second, lambda: "ok"), "ok")
            self.assertLess(time.monotonic() - started, .25)
        finally:
            release.set()
            thread.join(1)

    def test_late_result_does_not_become_success_while_collecting_another_source(self):
        def read_after(delay):
            time.sleep(delay)
            return "late data"
        outcomes = unified.run_concurrently({
            "longer": (lambda: read_after(.035), .2),
            "shorter": (lambda: read_after(.02), .005),
        }, app=app())
        self.assertTrue(outcomes["longer"]["ok"])
        self.assertFalse(outcomes["shorter"]["ok"])
        self.assertEqual(outcomes["shorter"]["code"], "timeout")

    def test_assignment_open_does_not_follow_upload_action_links(self):
        application = app()
        application.client.get_html = Mock()
        parsed = {"title": "Assignment", "description": "Work", "upload_url": "https://ninova.itu.edu.tr/Sinif/999.999/OdevTeslim/3"}
        with patch.object(unified, "extract_assignment_detail", return_value=parsed):
            result = unified._assignment_detail(application, "html", "https://ninova.itu.edu.tr/Sinif/1.2/Odev/3", {})
        self.assertEqual(result["description"], "Work")
        application.client.get_html.assert_not_called()


if __name__ == "__main__":
    unittest.main()
