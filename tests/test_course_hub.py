"""Synthetic transport, parsing, account and confirmed-write checks for pool sites."""
from __future__ import annotations
import io
import json
import os
import unittest
import zipfile
from unittest.mock import Mock, patch

import requests

from ninova_mcp.course_hub import CourseHubError, CourseHubClient, checked_url, parse_page, readable_url
from ninova_mcp import course_hub as source
from ninova_mcp.text_extract import extract_text_from_bytes, guess_extension

MATH = "https://mathavuz.itu.edu.tr"
CHEM = "https://kimyahavuz.itu.edu.tr"
PHYSICS = "https://fizikhavuz.itu.edu.tr"
PHYSICS_WWW = "https://www.fizikhavuz.itu.edu.tr"
HOME = '''<title>Pool courses</title><h2>Announcements</h2><p>Exam on 28 November.</p>
<table><tr><th>Course</th><th>Room</th></tr><tr><td>MAT103E</td><td>D101</td></tr></table>
<a href="/files/general.pdf">General guide</a><a href="/files/mat103e.pdf">MAT 103E notes</a>
<a href="/files/mat104.pdf">MAT104</a><a href="/mat/catalog.php?code=MAT103">MAT103/E</a>
<a href="/logout.php">Logout</a><a href="https://pearson.com/">Pearson</a>
<form action="syllabus.php" method="get"><button type="submit" name="ders" value="MAT103E">MAT103E</button></form>'''
LOGIN = '''<form method="post"><input name="itu_username"><input name="student_number"><button>Login</button></form>'''
RULE = {"site": "chemistry", "page_path": "/student/review.php", "form_id": "objection",
        "action_path": "/student/objection.php", "fields": ["reason", "exam"],
        "purpose": "Submit an exam objection", "submit_name": "send", "submit_value": "yes",
        "csrf_fields": ["csrf"], "context_fields": ["exam_id"]}
FORM = '''<a href="/student/logout.php">Logout</a><h2>Exam objection</h2>
<form id="objection" method="post" action="/student/objection.php">
<input type="hidden" name="csrf" value="secret-token"><input type="hidden" name="exam_id" value="exam-1">
<textarea name="reason" required></textarea><select name="exam"><option value="midterm">Midterm</option></select>
<button type="submit" name="send" value="yes">Submit</button></form>'''


class Transport(requests.adapters.BaseAdapter):
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        if not self.replies:
            raise AssertionError("Unexpected request: " + request.method + " " + request.url)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = reply.get("status", 200)
        response.headers.update(reply.get("headers", {"Content-Type": "text/html; charset=utf-8"}))
        response._content = reply.get("body", HOME).encode() if isinstance(reply.get("body", HOME), str) else reply["body"]
        response._content_consumed = True
        response.encoding = "utf-8"
        return response

    def close(self):
        pass


def transport(replies):
    session = requests.Session()
    session.trust_env = False
    adapter = Transport(replies)
    session.mount("https://", adapter)
    return session, adapter


def client(replies, **kwargs):
    session, adapter = transport(replies)
    c = CourseHubClient(public_session=session, **kwargs)
    c._delay = 0
    return c, adapter


class UrlTests(unittest.TestCase):
    def test_encoded_spaces_in_paths_and_queries_remain_readable(self):
        for url in [MATH + "/files/Lecture%20notes.pdf", MATH + "/mat/catalog.php?name=MAT%20103",
                    MATH + "/files/Lecture%2520notes.pdf?title=Study%20guide"]:
            with self.subTest(url=url):
                self.assertEqual(checked_url(url), url)
                self.assertEqual(readable_url(url), url)

    def test_literal_whitespace_and_decoded_controls_stay_rejected(self):
        for suffix in ["/files/Lecture notes.pdf", "/files/Lecture\tnotes.pdf", "/files/Lecture\nnotes.pdf",
                       "/files/Lecture\u00a0notes.pdf", "/files/Lecture%09notes.pdf", "/files/Lecture%0anotes.pdf",
                       "/files/Lecture%250dnotes.pdf", "/files/Lecture%00notes.pdf", "/files/Lecture%7fnotes.pdf",
                       "/files/Lecture%5cnotes.pdf", "/files/Lecture%255cnotes.pdf", "/files/%2e%2e/notes.pdf"]:
            with self.subTest(suffix=suffix), self.assertRaises(CourseHubError):
                checked_url(MATH + suffix)

    def test_only_exact_https_pool_hosts(self):
        self.assertEqual(checked_url(MATH + "/files/test.pdf#page=2"), MATH + "/files/test.pdf")
        for url in ["http://mathavuz.itu.edu.tr/", MATH + ":444/", "https://mathavuz.itu.edu.tr.evil.test/",
                    "https://pw@mathavuz.itu.edu.tr/", "https://127.0.0.1/", MATH + "/%252e%252e/admin",
                    MATH + "/x%0d%0aHeader:private", MATH + "/x\\foo"]:
            with self.subTest(url=url), self.assertRaises(CourseHubError):
                checked_url(url)

    def test_get_readers_refuse_actions(self):
        for path in ["/logout.php", "/student/delete.php?id=3", "/page.php?action=save", "/login.php"]:
            with self.subTest(path=path), self.assertRaises(CourseHubError):
                readable_url(MATH + path)


class ParsingTests(unittest.TestCase):
    def test_verified_native_math_document_routes_keep_their_course_and_collection(self):
        examples = [
            ("/show_slide.php?s=MAT103Tch01", "slides", "MAT103", "MAT103Tch01"),
            ("/show_slide.php?s=MAT103Ech01", "slides", "MAT103E", "MAT103Ech01"),
            ("/show_worksheet.php?ws=MAT103Tws1", "practice", "MAT103", "MAT103Tws1"),
            ("/show_worksheet.php?ws=MAT103Ews1", "practice", "MAT103E", "MAT103Ews1"),
            ("/show_exam.php?exam=MAT103E26BF", "exams", "MAT103E", "MAT103E26BF"),
            ("/show_exam.php?exam=MAT103E26BV", "exams", "MAT103E", "MAT103E26BV"),
            ("/show_exam.php?exam=MAT103E26YF", "exams", "MAT103E", "MAT103E26YF"),
            ("/show_exam.php?exam=MAT103E26YV", "exams", "MAT103E", "MAT103E26YV"),
            ("/show_exam.php?exam=MAT104E26BF", "exams", "MAT104E", "MAT104E26BF"),
        ]
        for path, collection, code, identifier in examples:
            with self.subTest(path=path):
                result = parse_page('<a href="' + path + '">Material</a>', MATH + "/")
                link = result["links"][0]
                self.assertEqual(link["kind"], "document")
                self.assertEqual(link["collection"], collection)
                self.assertEqual(link["course_codes"], [code])
                self.assertEqual(link["native_identifier"], identifier)
                self.assertNotIn("MAT103T", link["course_codes"])

    def test_native_course_identifiers_are_parameter_path_and_host_specific(self):
        for path in ["/other.php?s=MAT103Ech01", "/show_slide.php?exam=MAT103Ech01",
                     "/show_slide.php?s=MAT1034Ech01", "/show_slide.php?s=private-token",
                     "/show_slide.php?s=MAT103Ech01&s=MAT104Ech01",
                     "https://kimyahavuz.itu.edu.tr/show_slide.php?s=MAT103Ech01"]:
            with self.subTest(path=path):
                link = parse_page('<a href="' + path + '">Material</a>', MATH + "/")["links"][0]
                self.assertEqual(link["course_codes"], [])
                self.assertNotIn("native_identifier", link)

    def test_discovery_indexes_native_documents_without_reading_them_as_html(self):
        home = '<a href="/show_slide.php?s=MAT103Ech01">Chapter 1</a>'
        c, adapter = client([{"body": home}])
        result = c.discover("MAT", course_code="MAT103E", max_pages=5)
        self.assertEqual(result["pages_read"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["resources"][0]["course_match"], "exact_course")
        self.assertEqual(len(adapter.sent), 1)

    def test_exploration_prioritizes_content_before_home_and_locale_duplicates(self):
        home = ('<a href="/?lang=en">English</a><a href="/index.php">Home</a>'
                '<a href="/en/">English home</a><a href="/student/results.php">Results</a>')
        c, adapter = client([{"body": home}, {"body": "<h1>Student results</h1>"}])
        result = c.discover("MAT", max_pages=2)
        self.assertEqual(adapter.sent[1].url, MATH + "/student/results.php")
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["remaining_pages"], 3)
        self.assertEqual(result["pages_attempted"], 2)

    def test_exploration_does_not_read_a_redirected_alias_home_again(self):
        c, adapter = client([
            {"status": 302, "headers": {"Location": PHYSICS_WWW + "/"}},
            {"body": '<a href="' + PHYSICS_WWW + '/">Home</a><a href="/course.php">Course</a>'},
            {"body": "<h1>Physics course</h1>"},
        ])
        result = c.discover("FIZ", max_pages=2)
        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(len(adapter.sent), 3)
        self.assertTrue(adapter.sent[-1].url.endswith("/course.php"))
        self.assertEqual(result["remaining_pages"], 0)

    def test_physics_discovery_follows_links_on_its_exact_alias(self):
        c, adapter = client([{"body": '<a href="' + PHYSICS_WWW + '/catalog.php?code=FIZ101">FIZ101</a>'},
                             {"body": "<h1>Physics course content</h1>"}])
        result = c.discover("FIZ", max_pages=5)
        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(len(adapter.sent), 2)
        self.assertTrue(adapter.sent[1].url.startswith(PHYSICS_WWW))

    def test_structured_page_includes_get_syllabi_tables_and_external_links(self):
        result = parse_page(HOME, MATH + "/")
        self.assertIn("Exam on 28 November.", result["text"])
        self.assertEqual(result["tables"][0]["rows"][1], ["MAT103E", "D101"])
        self.assertIn(MATH + "/syllabus.php?ders=MAT103E", [l["url"] for l in result["links"]])
        self.assertFalse(next(l for l in result["links"] if "pearson" in l["url"])["readable"])
        self.assertFalse(next(l for l in result["links"] if "logout" in l["url"])["readable"])
        self.assertTrue(result["untrusted_external_content"])

    def test_login_secrets_are_never_part_of_form_output(self):
        result = parse_page(FORM + '<form><input type="password" value="private-password"></form>', CHEM + "/")
        encoded = json.dumps(result)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("private-password", encoded)
        self.assertNotIn("csrf", [field["name"] for field in result["forms"][0]["fields"]])
        self.assertEqual(result["forms"][0]["hidden_fields"], [
            {"name": "csrf", "type": "hidden"}, {"name": "exam_id", "type": "hidden"}])

    def test_verified_get_selector_is_distinct_from_unverified_forms(self):
        html = ('<form action="syllabus.php" method="get"><button name="ders" value="MAT103">MAT103</button></form>'
                '<form action="/unknown.php" method="get"><input name="year"></form>'
                '<form action="/student/objection.php" method="post"><textarea name="reason"></textarea></form>')
        result = parse_page(html, MATH + "/")
        selector, unknown_get, unknown_post = result["forms"]
        self.assertEqual(selector["kind"], "read_selector")
        self.assertIs(selector["requires_reviewed_write_rule"], False)
        self.assertEqual(unknown_get["kind"], "unverified_form")
        self.assertIsNone(unknown_get["requires_reviewed_write_rule"])
        self.assertEqual(unknown_post["kind"], "unverified_form")
        self.assertIs(unknown_post["requires_reviewed_write_rule"], True)

    def test_read_selector_excludes_hidden_disabled_and_non_submit_controls(self):
        html = '''<form action="syllabus.php" method="get">
        <input type="hidden" name="ders" value="MAT999">
        <button name="ders" value="MAT103" disabled>Disabled</button>
        <button type="button" name="ders" value="MAT104">Script button</button>
        <fieldset disabled><button name="ders" value="MAT105">Inherited disabled</button></fieldset>
        <input type="submit" name="ders" value="MAT103E"></form>'''
        result = parse_page(html, MATH + "/")
        syllabi = [link for link in result["links"] if link["kind"] == "syllabus"]
        self.assertEqual([link["url"] for link in syllabi], [MATH + "/syllabus.php?ders=MAT103E"])
        self.assertNotIn("MAT999", json.dumps(result))

    def test_disabled_fieldsets_and_optgroups_are_visible_in_metadata(self):
        html = '''<form method="post"><fieldset disabled>
        <legend><input name="first_legend"></legend><legend><input name="second_legend"></legend>
        <input name="outside_legend"></fieldset><select name="exam">
        <optgroup label="Unavailable" disabled><option value="midterm">Midterm</option></optgroup>
        <option value="final">Final</option></select></form>'''
        fields = {field["name"]: field for field in parse_page(html, CHEM + "/")["forms"][0]["fields"]}
        self.assertFalse(fields["first_legend"]["disabled"])
        self.assertTrue(fields["second_legend"]["disabled"])
        self.assertTrue(fields["outside_legend"]["disabled"])
        self.assertTrue(fields["exam"]["options"][0]["disabled"])
        self.assertFalse(fields["exam"]["options"][1]["disabled"])

    def test_course_filter_preserves_general_material_and_prioritizes_exact_course(self):
        c, _ = client([{}])
        result = c.discover("MAT", course_code="MAT 103E", max_pages=1)
        self.assertEqual(result["resources"][0]["course_match"], "exact_course")
        general = next(r for r in result["resources"] if "general.pdf" in r["url"])
        self.assertEqual(general["course_match"], "general_or_unknown")
        self.assertEqual(result["coverage"], "partial")
        self.assertGreater(result["remaining_pages"], 0)

    def test_unfiltered_discovery_crawls_identified_course_pages(self):
        home = '<a href="/syllabus.php?ders=MAT103">MAT103</a>'
        c, adapter = client([{"body": home}, {"body": "<h1>Course syllabus</h1>"}])
        result = c.discover("MAT", max_pages=5)
        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(len(adapter.sent), 2)
        self.assertEqual(result["resources"][0]["course_match"], "identified_course")
        self.assertEqual(result["coverage"], "discovered_links_exhausted")

    def test_shared_course_label_matches_english_and_turkish_courses(self):
        for code in ("MAT103", "MAT103E"):
            with self.subTest(code=code):
                home = '<a href="/mat/catalog.php?code=MAT103">MAT 103/E</a>'
                c, adapter = client([{"body": home}, {"body": "<h1>Shared course content</h1>"}])
                result = c.discover("MAT", course_code=code, max_pages=5)
                self.assertEqual(result["pages_read"], 2)
                self.assertEqual(result["resources"][0]["course_codes"], ["MAT103", "MAT103E"])
                self.assertEqual(result["resources"][0]["course_match"], "exact_course")

    def test_course_matching_does_not_truncate_a_longer_course_number(self):
        c, _ = client([{"body": '<a href="/mat/catalog.php?code=MAT1034">MAT1034</a>'}])
        result = c.discover("MAT", course_code="MAT103", max_pages=1)
        self.assertEqual(result["resources"][0]["course_codes"], [])

    def test_uppercase_hidden_and_password_types_are_not_editable_fields(self):
        result = parse_page(FORM.replace('type="hidden"', 'type="HIDDEN"') +
                            '<form><input type="PASSWORD" name="password" value="private-password"></form>', CHEM + "/")
        fields = [field for form in result["forms"] for field in form["fields"]]
        self.assertNotIn("csrf", [field["name"] for field in fields])
        self.assertNotIn("exam_id", [field["name"] for field in fields])
        self.assertNotIn("password", [field["name"] for field in fields])

    def test_partial_source_failure_is_explicit(self):
        c, adapter = client([{"status": 503}])
        result = c.discover("physics")
        self.assertEqual(result["pages_read"], 0)
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("503", result["errors"][0]["error"])
        self.assertEqual(len(adapter.sent), 1)


class TransportTests(unittest.TestCase):
    def test_public_physics_documents_accept_only_their_exact_alias_redirects(self):
        for initial, final in [(PHYSICS_WWW, PHYSICS), (PHYSICS, PHYSICS_WWW)]:
            with self.subTest(initial=initial):
                c, adapter = client([
                    {"status": 302, "headers": {"Location": final + "/files/lecture.txt"}},
                    {"body": b"Physics material", "headers": {"Content-Type": "text/plain"}},
                ])
                with patch("ninova_mcp.course_hub.subprocess.run") as run:
                    run.return_value.stdout = b'{"ok":true,"text":"Physics material"}'
                    result = c.read_document(initial + "/files/lecture.txt")
                self.assertTrue(result["ok"])
                self.assertEqual(result["url"], final + "/files/lecture.txt")
                self.assertEqual(len(adapter.sent), 2)

    def test_physics_document_redirect_cannot_switch_departments(self):
        c, adapter = client([{"status": 302, "headers": {"Location": CHEM + "/files/lecture.txt"}}])
        with self.assertRaises(CourseHubError):
            c.read_document(PHYSICS_WWW + "/files/lecture.txt")
        self.assertEqual(len(adapter.sent), 1)

    def test_streamed_body_failure_is_wrapped_and_response_is_closed(self):
        response = Mock(status_code=200, url=MATH + "/", headers={"Content-Type": "text/html"})
        response.iter_content.side_effect = requests.ReadTimeout("private request data")
        session = Mock()
        session.request.return_value = response
        c = CourseHubClient(public_session=session)
        c._delay = 0
        with self.assertRaises(CourseHubError) as raised:
            c.read_page("MAT")
        self.assertNotIn("private request", str(raised.exception))
        response.close.assert_called_once()

    def test_redirect_is_checked_before_second_request(self):
        c, adapter = client([{"status": 302, "headers": {"Location": "https://evil.test/private"}}])
        with self.assertRaises(CourseHubError):
            c.read_page("MAT")
        self.assertEqual(len(adapter.sent), 1)

    def test_public_requests_do_not_forward_cookies(self):
        c, adapter = client([{}])
        c.public.cookies.set("session", "private", domain="mathavuz.itu.edu.tr")
        c.read_page("MAT")
        self.assertNotIn("Cookie", adapter.sent[0].headers)

    def test_login_redirect_on_read_is_refused(self):
        c, adapter = client([{"status": 302, "headers": {"Location": "/login.php?ReturnUrl=/"}}])
        with self.assertRaises(CourseHubError):
            c.read_page("MAT")
        self.assertEqual(len(adapter.sent), 1)

    def test_html_body_cannot_be_extracted_as_pdf(self):
        c, _ = client([{"body": LOGIN}])
        with self.assertRaisesRegex(CourseHubError, "HTML"):
            c.read_document(CHEM + "/docs/syllabus.pdf")

    def test_document_download_endpoint_uses_pdf_mime_and_signature(self):
        c, _ = client([{"body": b"%PDF-1.4 synthetic", "headers": {"Content-Type": "application/pdf"}}])
        with patch("ninova_mcp.course_hub.subprocess.run") as run:
            run.return_value.stdout = b'{"ok":true,"text":"material"}'
            result = c.read_document(MATH + "/files/download.php?id=1")
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0][-2], ".pdf")

    def test_large_responses_are_closed_and_rejected(self):
        c, _ = client([{"body": b"x" * 20}])
        with self.assertRaisesRegex(CourseHubError, "size limit"):
            c._request(c.public, "GET", MATH, hosts={"mathavuz.itu.edu.tr"}, limit=10)

    def test_document_worker_runs_without_credential_environment(self):
        c, _ = client([{"body": b"plain material", "headers": {"Content-Type": "text/plain"}}])
        with patch.dict(os.environ, {"NINOVA_PASSWORD": "private", "PAPERBRIDGE_OPENATHENS_PASSWORD": "secret"}), patch(
                "ninova_mcp.course_hub.subprocess.run") as run:
            run.return_value.stdout = b'{"ok":true,"text":"plain material"}'
            result = c.read_document(MATH + "/files/material.txt")
        self.assertEqual(result["text"], "plain material")
        self.assertNotIn("NINOVA_PASSWORD", run.call_args.kwargs["env"])
        self.assertNotIn("PAPERBRIDGE_OPENATHENS_PASSWORD", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)


class AccountTests(unittest.TestCase):
    def test_cached_recovery_is_attempted_only_once_when_the_fresh_session_expires(self):
        stale, stale_adapter = transport([{"status": 401}])
        fresh, fresh_adapter = transport([
            {"body": LOGIN}, {"body": "OK"}, {"body": FORM},
            {"status": 302, "headers": {"Location": "/student/login.php"}},
        ])
        factory = Mock(return_value=fresh)
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=factory, credentials=credentials)
        c._accounts["chemistry"] = stale
        with self.assertRaises(CourseHubError):
            c.read_page("KIM", authenticated=True)
        self.assertNotIn("chemistry", c._accounts)
        self.assertEqual(len(stale_adapter.sent), 1)
        self.assertEqual(len(fresh_adapter.sent), 4)
        factory.assert_called_once()
        credentials.assert_called_once_with("chemistry")

    def test_fresh_session_expiry_does_not_start_another_login(self):
        fresh, adapter = transport([
            {"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"status": 401},
        ])
        factory = Mock(return_value=fresh)
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=factory, credentials=credentials)
        with self.assertRaises(CourseHubError):
            c.read_page("KIM", authenticated=True)
        self.assertEqual(len(adapter.sent), 4)
        self.assertNotIn("chemistry", c._accounts)
        factory.assert_called_once()
        credentials.assert_called_once_with("chemistry")

    def test_failed_reauthentication_is_not_retried(self):
        stale, stale_adapter = transport([{"status": 401}])
        fresh, fresh_adapter = transport([{"body": LOGIN}, {"status": 403}])
        factory = Mock(return_value=fresh)
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=factory, credentials=credentials)
        c._accounts["chemistry"] = stale
        with self.assertRaisesRegex(CourseHubError, "could not be refreshed"):
            c.read_page("KIM", authenticated=True)
        self.assertEqual(len(stale_adapter.sent), 1)
        self.assertEqual([request.method for request in fresh_adapter.sent], ["GET", "POST"])
        factory.assert_called_once()
        credentials.assert_called_once_with("chemistry")

    def test_403_unknown_redirect_and_network_failure_do_not_reauthenticate(self):
        replies = [{"status": 403}, {"status": 302, "headers": {"Location": "https://evil.test/Login.aspx"}},
                   requests.ReadTimeout("private request data")]
        for reply in replies:
            with self.subTest(reply=reply):
                stale, adapter = transport([reply])
                factory, credentials = Mock(), Mock()
                c, _ = client([], session_factory=factory, credentials=credentials)
                c._accounts["chemistry"] = stale
                with self.assertRaises(CourseHubError):
                    c.read_page("KIM", authenticated=True)
                self.assertEqual(len(adapter.sent), 1)
                factory.assert_not_called()
                credentials.assert_not_called()

    def test_streamed_body_failure_does_not_reauthenticate(self):
        response = Mock(status_code=200, url=CHEM + "/student/", headers={"Content-Type": "text/html"})
        response.iter_content.side_effect = requests.ReadTimeout("private body data")
        stale = Mock()
        stale.request.return_value = response
        factory, credentials = Mock(), Mock()
        c, _ = client([], session_factory=factory, credentials=credentials)
        c._accounts["chemistry"] = stale
        with self.assertRaises(CourseHubError):
            c.read_page("KIM", authenticated=True)
        stale.request.assert_called_once()
        factory.assert_not_called()
        credentials.assert_not_called()

    def test_readonly_student_identity_with_own_logout_is_an_account_page(self):
        html = FORM.replace('<textarea', '<input name="student_number" readonly value="native-id"><textarea')
        stale, adapter = transport([{"body": html}])
        factory, credentials = Mock(), Mock()
        c, _ = client([], session_factory=factory, credentials=credentials)
        c._accounts["chemistry"] = stale
        result = c.read_page("KIM", authenticated=True)
        self.assertFalse(result["session_refreshed"])
        fields = {field["name"]: field for field in result["forms"][0]["fields"]}
        self.assertTrue(fields["student_number"]["readonly"])
        self.assertEqual(len(adapter.sent), 1)
        factory.assert_not_called()
        credentials.assert_not_called()

    def test_external_logout_link_does_not_prove_a_fresh_login(self):
        account, adapter = transport([
            {"body": LOGIN}, {"body": "OK"},
            {"body": '<a href="https://evil.test/logout.php">Logout</a>'},
        ])
        factory = Mock(return_value=account)
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=factory, credentials=credentials)
        with self.assertRaisesRegex(CourseHubError, "could not be verified"):
            c.read_page("KIM", authenticated=True)
        self.assertEqual(len(adapter.sent), 3)
        factory.assert_called_once()
        self.assertNotIn("chemistry", c._accounts)

    def test_account_reads_cannot_follow_another_departments_redirect(self):
        for source, target, site in [(PHYSICS_WWW, CHEM, "physics"), (CHEM, PHYSICS, "chemistry")]:
            with self.subTest(site=site):
                account, adapter = transport([{"status": 302, "headers": {"Location": target + "/student/"}}])
                credentials = Mock()
                c, _ = client([], credentials=credentials)
                c._accounts[site] = account
                with self.assertRaises(CourseHubError):
                    c.read_page(site, url=source + "/student/results.php", authenticated=True)
                self.assertEqual(len(adapter.sent), 1)
                credentials.assert_not_called()

    def test_source_status_describes_cached_sessions_without_claiming_current_verification(self):
        c, _ = client([])
        c._accounts["mathematics"] = Mock()
        sites = {site["site"]: site for site in c.sites()["sites"]}
        self.assertTrue(sites["mathematics"]["session_cached"])
        self.assertEqual(sites["mathematics"]["session_status"], "cached_not_revalidated")
        self.assertFalse(sites["chemistry"]["session_cached"])
        self.assertEqual(sites["chemistry"]["session_status"], "not_cached")
        self.assertNotIn("account_verified", json.dumps(sites))

    def test_cached_expired_session_is_reauthenticated_once_for_the_same_read(self):
        stale, stale_adapter = transport([{"status": 302, "headers": {"Location": "/student/login.php"}}])
        fresh, fresh_adapter = transport([{"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"body": FORM}])
        factory = Mock(return_value=fresh)
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=factory, credentials=credentials)
        c._accounts["chemistry"] = stale
        result = c.read_page("KIM", authenticated=True)
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["session_refreshed"])
        self.assertEqual(len(stale_adapter.sent), 1)
        self.assertEqual(stale_adapter.sent[0].url, fresh_adapter.sent[-1].url)
        factory.assert_called_once()
        credentials.assert_called_once_with("chemistry")
        self.assertEqual(len(fresh_adapter.sent), 4)

    def test_cached_sso_session_is_invalidated_before_following_login_redirect(self):
        stale, adapter = transport([{"status": 302, "headers": {
            "Location": "https://girisv3.itu.edu.tr/Login.aspx?currentURL=synthetic"}}])
        fresh, fresh_adapter = transport([
            {"status": 302, "headers": {"Location": NativeLoginFlowTests.SSO}},
            {"body": NativeLoginFlowTests.LOGIN},
            {"status": 302, "headers": {"Location": MATH + "/"}},
            {"body": HOME}, {"body": HOME}, {"body": HOME},
        ])
        credentials = Mock(return_value={"username": "synthetic", "password": "private-password"})
        c, _ = client([], session_factory=lambda: fresh, credentials=credentials)
        c._accounts["mathematics"] = stale
        result = c.read_page("MAT", authenticated=True)
        self.assertTrue(result["session_refreshed"])
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(fresh_adapter.sent[0].url, MATH + "/login.php?ReturnUrl=/")
        credentials.assert_called_once_with("mathematics")

    def test_cached_account_401_allows_one_verified_reauthentication(self):
        stale, adapter = transport([{"status": 401}])
        fresh, _ = transport([{"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"body": FORM}])
        credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
        c, _ = client([], session_factory=lambda: fresh, credentials=credentials)
        c._accounts["chemistry"] = stale
        result = c.read_page("KIM", authenticated=True)
        self.assertTrue(result["session_refreshed"])
        self.assertEqual(len(adapter.sent), 1)
        credentials.assert_called_once_with("chemistry")

    def test_cached_document_recovers_from_login_redirect_and_html(self):
        for reply in [{"status": 302, "headers": {"Location": "/student/login.php"}}, {"body": LOGIN}]:
            with self.subTest(reply=reply):
                stale, adapter = transport([reply])
                fresh, fresh_adapter = transport([
                    {"body": LOGIN}, {"body": "OK"}, {"body": FORM},
                    {"body": b"%PDF-1.4 synthetic", "headers": {"Content-Type": "application/pdf"}},
                ])
                credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
                c, _ = client([], session_factory=lambda: fresh, credentials=credentials)
                c._accounts["chemistry"] = stale
                with patch("ninova_mcp.course_hub.subprocess.run") as run:
                    run.return_value.stdout = b'{"ok":true,"text":"Material"}'
                    result = c.read_document(CHEM + "/docs/syllabus.pdf", authenticated=True)
                self.assertTrue(result["session_refreshed"])
                self.assertEqual(len(adapter.sent), 1)
                self.assertEqual(adapter.sent[0].url, fresh_adapter.sent[-1].url)
                credentials.assert_called_once_with("chemistry")

    def test_crawl_has_one_recovery_budget_then_stops_on_later_expiry(self):
        home = ('<a href="/student/first.php">First</a><a href="/student/next.php">Next</a>'
                '<a href="/student/last.php">Last</a>')
        for reply in [{"status": 302, "headers": {"Location": "/student/login.php"}}, {"body": LOGIN}]:
            with self.subTest(reply=reply):
                stale, adapter = transport([{"body": home}, reply])
                fresh, fresh_adapter = transport([
                    {"body": LOGIN}, {"body": "OK"}, {"body": FORM},
                    {"body": "<h1>First result</h1>"}, {"status": 401},
                ])
                factory = Mock(return_value=fresh)
                credentials = Mock(return_value={"username": "synthetic", "student_number": "150000000"})
                c, _ = client([], session_factory=factory, credentials=credentials)
                c._accounts["chemistry"] = stale
                result = c.discover("KIM", authenticated=True, max_pages=5)
                self.assertEqual(result["coverage"], "partial")
                self.assertEqual(result["pages_read"], 2)
                self.assertTrue(result["session_refreshed"])
                self.assertEqual([page["session_refreshed"] for page in result["pages"]], [False, True])
                self.assertEqual(result["remaining_pages"], 1)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(len(adapter.sent), 2)
                self.assertNotIn("chemistry", c._accounts)
                factory.assert_called_once()
                credentials.assert_called_once_with("chemistry")
                self.assertEqual(len(fresh_adapter.sent), 5)

    def test_chemistry_has_separate_session_and_requires_verified_account_home(self):
        account, adapter = transport([{"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"body": FORM}])
        c, public = client([], session_factory=lambda: account,
                           credentials=lambda _: {"username": "synthetic", "student_number": "150000000"})
        result = c.read_page("KIM", authenticated=True)
        self.assertTrue(result["authenticated"])
        self.assertEqual(len(public.sent), 0)
        self.assertEqual([r.method for r in adapter.sent], ["GET", "POST", "GET", "GET"])
        self.assertIn("student_number=150000000", adapter.sent[1].body)

    def test_untrusted_login_target_never_reads_credentials(self):
        source = Mock()
        account, adapter = transport([{"body": LOGIN.replace('<form method="post">', '<form method="post" action="https://evil.test/">')}])
        c, _ = client([], session_factory=lambda: account, credentials=source)
        with self.assertRaises(CourseHubError):
            c.read_page("KIM", authenticated=True)
        source.assert_not_called()
        self.assertEqual(len(adapter.sent), 1)

    def test_mathematics_sso_uses_verified_standard_itu_form(self):
        sso = "https://girisv3.itu.edu.tr/Login.aspx?currentURL=synthetic"
        login = """<form method="post"><input type="hidden" name="__VIEWSTATE" value="private-state">
        <input name="ctl00$ContentPlaceHolder1$tbUserName">
        <input type="password" name="ctl00$ContentPlaceHolder1$tbPassword">
        <input type="submit" name="ctl00$ContentPlaceHolder1$btnLogin" value="Login"></form>"""
        account, adapter = transport([
            {"status": 302, "headers": {"Location": sso}}, {"body": login},
            {"status": 302, "headers": {"Location": MATH + "/"}}, {"body": HOME},
            {"body": HOME}, {"body": HOME},
        ])
        c, public = client([], session_factory=lambda: account,
                           credentials=lambda _: {"username": "synthetic", "password": "private-password"})
        result = c.read_page("MAT", authenticated=True)
        self.assertTrue(result["authenticated"])
        self.assertEqual(public.sent, [])
        posts = [r for r in adapter.sent if r.method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].url.startswith("https://girisv3.itu.edu.tr/Login.aspx"))
        self.assertNotIn("private-password", json.dumps(result))
        self.assertNotIn("private-state", json.dumps(result))

    def test_http_success_without_account_evidence_is_not_login_success(self):
        account, _ = transport([{"body": LOGIN}, {"body": "OK"}, {"body": "Welcome"}])
        c, _ = client([], session_factory=lambda: account,
                      credentials=lambda _: {"username": "synthetic", "student_number": "150000000"})
        with self.assertRaisesRegex(CourseHubError, "could not be verified"):
            c.read_page("KIM", authenticated=True)
        self.assertEqual(c._accounts, {})


class WriteTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.dict(os.environ, {"NINOVA_COURSEHUB_WRITES": "1"})
        enabled.start()
        self.addCleanup(enabled.stop)

    def make(self, forms=(FORM, FORM), outcome="Recorded"):
        session, adapter = transport([*({"body": f} for f in forms), {"body": outcome}])
        c, _ = client([], write_rules=[RULE])
        c._accounts["chemistry"] = session
        return c, adapter

    def prepare(self, c):
        return c.prepare_action("KIM", CHEM + "/student/review.php", "objection", {"reason": "Synthetic request", "exam": "midterm"})

    def test_requires_explicit_operator_switch(self):
        c, adapter = self.make()
        with patch.dict(os.environ, {"NINOVA_COURSEHUB_WRITES": "0"}), self.assertRaises(CourseHubError):
            self.prepare(c)
        self.assertEqual(len(adapter.sent), 0)

    def test_hidden_context_remains_bound_when_also_named_as_csrf(self):
        c, adapter = self.make(forms=(FORM, FORM.replace("exam-1", "exam-2")))
        c._rules_override = [{**RULE, "csrf_fields": ["csrf", "exam_id"]}]
        prepared = self.prepare(c)
        self.assertEqual(prepared["preview"]["context"], {"exam_id": "exam-1"})
        with self.assertRaisesRegex(CourseHubError, "form changed"):
            c.submit_action(prepared["action_id"], confirm=True)
        self.assertEqual([request.method for request in adapter.sent], ["GET", "GET"])

    def test_write_preflight_expiry_never_reauthenticates_or_posts(self):
        for phase in ("prepare", "submit"):
            with self.subTest(phase=phase):
                c, adapter = self.make()
                factory, credentials = Mock(), Mock()
                c._session_factory, c._credentials = factory, credentials
                if phase == "prepare":
                    adapter.replies[0] = {"status": 401}
                    with self.assertRaises(CourseHubError):
                        self.prepare(c)
                else:
                    token = self.prepare(c)["action_id"]
                    adapter.replies[0] = {"status": 401}
                    with self.assertRaises(CourseHubError):
                        c.submit_action(token, confirm=True)
                self.assertNotIn("POST", [request.method for request in adapter.sent])
                factory.assert_not_called()
                credentials.assert_not_called()

    def test_post_auth_error_is_uncertain_and_never_reauthenticates(self):
        c, adapter = self.make()
        factory, credentials = Mock(), Mock()
        c._session_factory, c._credentials = factory, credentials
        adapter.replies[-1] = {"status": 401}
        token = self.prepare(c)["action_id"]
        result = c.submit_action(token, confirm=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["retry_allowed"])
        self.assertEqual([request.method for request in adapter.sent], ["GET", "GET", "POST"])
        factory.assert_not_called()
        credentials.assert_not_called()


    def test_effectively_disabled_fields_and_options_cannot_be_posted(self):
        variants = [
            FORM.replace('<option value="midterm">', '<optgroup disabled><option value="midterm">').replace('</option>', '</option></optgroup>'),
            FORM.replace('<textarea', '<fieldset disabled><textarea').replace('</select>', '</select></fieldset>'),
            FORM.replace('<button type="submit"', '<button disabled type="submit"'),
            FORM.replace('<input type="hidden" name="exam_id"', '<input disabled type="hidden" name="exam_id"'),
        ]
        for html in variants:
            with self.subTest(html=html):
                c, adapter = self.make(forms=(html,))
                with self.assertRaises(CourseHubError):
                    self.prepare(c)
                self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_disabled_ancestor_changes_invalidate_the_approved_form(self):
        changes = [
            FORM.replace('<option value="midterm">', '<optgroup disabled><option value="midterm">').replace('</option>', '</option></optgroup>'),
            FORM.replace('<textarea', '<fieldset disabled><textarea').replace('</select>', '</select></fieldset>'),
        ]
        for changed in changes:
            with self.subTest(changed=changed):
                c, adapter = self.make(forms=(FORM, changed))
                token = self.prepare(c)["action_id"]
                with self.assertRaisesRegex(CourseHubError, "form changed"):
                    c.submit_action(token, confirm=True)
                self.assertEqual([request.method for request in adapter.sent], ["GET", "GET"])

    def test_first_legend_controls_remain_enabled_and_other_fieldset_controls_are_omitted(self):
        form = FORM.replace('<textarea name="reason" required></textarea>',
            '<fieldset disabled><legend><textarea name="reason" required></textarea></legend>'
            '<input name="ignored" value="Disabled value"></fieldset>')
        c, adapter = self.make(forms=(form, form))
        token = self.prepare(c)["action_id"]
        c.submit_action(token, confirm=True)
        self.assertIn("reason=Synthetic+request", adapter.sent[-1].body)
        self.assertNotIn("ignored", adapter.sent[-1].body)

    def test_submit_control_cannot_disagree_with_the_editable_field_preview(self):
        c, adapter = self.make()
        c._rules_override = [{**RULE, "fields": ["reason", "exam", "send"]}]
        with self.assertRaisesRegex(CourseHubError, "cannot be supplied as editable"):
            c.prepare_action("KIM", CHEM + "/student/review.php", "objection",
                             {"reason": "Synthetic", "exam": "midterm", "send": "no"})
        self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_reviewed_non_submit_button_is_rejected(self):
        form = FORM.replace('<button type="submit"', '<button type="button"')
        c, adapter = self.make(forms=(form,))
        with self.assertRaisesRegex(CourseHubError, "not a native submit"):
            self.prepare(c)
        self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_unreviewed_form_is_never_posted(self):
        c, adapter = self.make()
        c._rules_override = []
        with self.assertRaisesRegex(CourseHubError, "no reviewed POST adapter"):
            self.prepare(c)
        self.assertEqual([r.method for r in adapter.sent], ["GET"])

    def test_preview_and_confirmation_are_bound_and_single_use(self):
        c, adapter = self.make()
        prepared = self.prepare(c)
        self.assertNotIn("secret-token", json.dumps(prepared))
        prepared["preview"]["fields"]["reason"] = "Tampered client preview"
        with self.assertRaises(CourseHubError):
            c.submit_action(prepared["action_id"], confirm="true")
        result = c.submit_action(prepared["action_id"], confirm=True)
        self.assertEqual(result["status"], "submitted_unverified")
        self.assertFalse(result["verified"])
        self.assertIn("Synthetic+request", adapter.sent[-1].body)
        self.assertNotIn("Tampered", adapter.sent[-1].body)
        with self.assertRaises(CourseHubError):
            c.submit_action(prepared["action_id"], confirm=True)
        self.assertEqual([r.method for r in adapter.sent].count("POST"), 1)

    def test_csrf_refresh_is_allowed_but_changed_record_identity_is_rejected(self):
        c, adapter = self.make(forms=(FORM, FORM.replace("secret-token", "fresh-token")))
        token = self.prepare(c)["action_id"]
        c.submit_action(token, confirm=True)
        self.assertIn("fresh-token", adapter.sent[-1].body)
        c, adapter = self.make(forms=(FORM, FORM.replace("exam-1", "exam-2")))
        token = self.prepare(c)["action_id"]
        with self.assertRaisesRegex(CourseHubError, "form changed"):
            c.submit_action(token, confirm=True)
        self.assertEqual([r.method for r in adapter.sent], ["GET", "GET"])

    def test_uppercase_hidden_context_changes_are_rejected_before_post(self):
        form = FORM.replace('type="hidden"', 'type="HIDDEN"')
        c, adapter = self.make(forms=(form, form.replace("exam-1", "exam-2")))
        prepared = self.prepare(c)
        self.assertEqual(prepared["preview"]["context"], {"exam_id": "exam-1"})
        with self.assertRaisesRegex(CourseHubError, "form changed"):
            c.submit_action(prepared["action_id"], confirm=True)
        self.assertEqual([r.method for r in adapter.sent], ["GET", "GET"])

    def test_streamed_post_response_failure_is_uncertain_and_single_use(self):
        c, adapter = self.make()
        original_send = adapter.send

        def send(request, **kwargs):
            response = original_send(request, **kwargs)
            if request.method == "POST":
                response.iter_content = Mock(side_effect=requests.ReadTimeout("private request data"))
            return response

        adapter.send = send
        token = self.prepare(c)["action_id"]
        result = c.submit_action(token, confirm=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertIsNone(result["submitted"])
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn("private request", json.dumps(result))
        with self.assertRaises(CourseHubError):
            c.submit_action(token, confirm=True)
        self.assertEqual([r.method for r in adapter.sent].count("POST"), 1)

    def test_transport_timeout_is_uncertain_and_cannot_be_retried(self):
        c, adapter = self.make()
        adapter.replies[-1] = requests.Timeout("private request data")
        token = self.prepare(c)["action_id"]
        result = c.submit_action(token, confirm=True)
        self.assertIsNone(result["submitted"])
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn("private request", json.dumps(result))
        with self.assertRaises(CourseHubError):
            c.submit_action(token, confirm=True)

    def test_target_context_is_in_the_preview_and_tokens_are_not(self):
        c, _ = self.make()
        prepared = self.prepare(c)
        self.assertEqual(prepared["preview"]["context"], {"exam_id": "exam-1"})
        self.assertNotIn("secret-token", json.dumps(prepared))

    def test_unsafe_or_unsupported_controls_require_a_dedicated_adapter(self):
        variants = [
            FORM.replace('action="/student/objection.php"', 'action="https://evil.test/collect"'),
            FORM.replace('method="post"', 'method="get"'),
            FORM.replace('<textarea', '<input type="file" name="attachment"><textarea'),
            FORM.replace('<textarea', '<input type="hidden" name="unclassified" value="private"><textarea'),
            FORM.replace('<textarea', '<input name="exam" value="duplicate"><textarea'),
            FORM.replace('<select name="exam">', '<select name="exam" multiple>'),
            FORM.replace('<select name="exam">', '<select name="exam" disabled>'),
            FORM.replace('<option value="midterm">', '<option value="midterm" disabled>'),
            FORM.replace('name="send" value="yes"', 'name="send" value="yes" formaction="/other.php"'),
        ]
        for html in variants:
            with self.subTest(html=html):
                c, adapter = self.make(forms=(html,))
                with self.assertRaises(CourseHubError):
                    self.prepare(c)
                self.assertEqual([r.method for r in adapter.sent], ["GET"])

    def test_post_redirect_never_replays_body_to_another_endpoint(self):
        c, adapter = self.make()
        adapter.replies[-1] = {"status": 307, "headers": {"Location": "/student/different.php"}}
        token = self.prepare(c)["action_id"]
        result = c.submit_action(token, confirm=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual([r.method for r in adapter.sent], ["GET", "GET", "POST"])

    def test_changed_rule_or_expired_preview_is_refused(self):
        c, adapter = self.make()
        token = self.prepare(c)["action_id"]
        c._rules_override = []
        with self.assertRaisesRegex(CourseHubError, "rule changed"):
            c.submit_action(token, confirm=True)
        self.assertEqual(len(adapter.sent), 1)
        c, adapter = self.make()
        token = self.prepare(c)["action_id"]
        c._pending[token].expires = 0
        with self.assertRaisesRegex(CourseHubError, "expired"):
            c.submit_action(token, confirm=True)
        self.assertEqual(len(adapter.sent), 1)


def pptx():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("ppt/presentation.xml", '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="slideB"/><p:sldId id="2" r:id="slideA"/></p:sldIdLst></p:presentation>''')
        z.writestr("ppt/_rels/presentation.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="slideB" Type="x/slide" Target="slides/slide9.xml"/><Relationship Id="slideA" Type="x/slide" Target="slides/slide1.xml"/></Relationships>''')
        template = '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:sld>'
        z.writestr("ppt/slides/slide9.xml", template.format("First visible slide"))
        z.writestr("ppt/slides/slide1.xml", template.format("Second visible slide"))
        z.writestr("ppt/slides/_rels/slide9.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="note" Type="x/notesSlide" Target="../notesSlides/notesSlide3.xml"/></Relationships>''')
        z.writestr("ppt/notesSlides/notesSlide3.xml", '''<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><a:p><a:r><a:t>Speaker explanation</a:t></a:r></a:p></p:sp></p:notes>''')
    return out.getvalue()


class ToolContractTests(unittest.TestCase):
    def test_remote_writes_need_both_switches(self):
        from ninova_mcp.remote import _selected_remote_tool_names
        for remote, pool, expected in [("0", "0", False), ("0", "1", False),
                                       ("1", "0", False), ("1", "1", True)]:
            with self.subTest(remote=remote, pool=pool), patch.dict(os.environ, {
                "NINOVA_REMOTE_ENABLE_WRITES": remote, "NINOVA_COURSEHUB_WRITES": pool,
                "NINOVA_REMOTE_ALLOWED_TOOLS": "", "NINOVA_REMOTE_ENABLE_FILES": "0",
            }):
                names = _selected_remote_tool_names()
                self.assertIn("coursehub_page", names)
                self.assertEqual("coursehub_commit_change" in names, expected)
                self.assertEqual("coursehub_review_change" in names, expected)

    def test_full_mcp_rejects_truthy_string_confirmation_before_client_access(self):
        import asyncio
        from mcp.server.fastmcp import FastMCP
        from ninova_mcp.server import NinovaMcpApp, register_tools
        app = NinovaMcpApp()
        app._course_hub = Mock()
        server = FastMCP("pool-test")
        register_tools(server, app, ["coursehub_commit_change"], profile="full")
        try:
            asyncio.run(server.call_tool("coursehub_commit_change", {"action_id": "synthetic", "confirm": "true"}))
        except Exception as exc:
            self.assertIn("bool", str(exc).lower())
        else:
            self.fail("String confirmation was accepted")
        app._course_hub.submit_action.assert_not_called()


class ReadonlyContextTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.dict(os.environ, {"NINOVA_COURSEHUB_WRITES": "1"})
        enabled.start()
        self.addCleanup(enabled.stop)

    @staticmethod
    def form(control):
        return FORM.replace('<textarea name="reason"', control + '<textarea name="reason"')

    @staticmethod
    def make(first, second=None, rule=None):
        session, adapter = transport([{"body": first}, {"body": second or first}, {"body": "Recorded"}])
        selected_rule = rule or {**RULE, "context_fields": ["exam_id", "identity"]}
        c, _ = client([], write_rules=[selected_rule])
        c._accounts["chemistry"] = session
        return c, adapter

    @staticmethod
    def prepare(c, fields=None):
        return c.prepare_action("KIM", CHEM + "/student/review.php", "objection",
                                fields or {"reason": "Synthetic request", "exam": "midterm"})

    def test_supported_readonly_inputs_are_previewed_and_posted_unchanged(self):
        from urllib.parse import parse_qsl
        for kind, value in [(None, "native-id"), ("text", "native-id"), ("search", "native-id"),
                            ("email", "synthetic@example.test"), ("url", "https://example.test/identity"),
                            ("tel", "+0000000"), ("number", "150000000")]:
            with self.subTest(kind=kind):
                type_attribute = 'type="' + kind + '" ' if kind else ""
                form = self.form('<input ' + type_attribute + 'name="identity" readonly value="' + value + '">')
                c, adapter = self.make(form)
                prepared = self.prepare(c)
                self.assertEqual(prepared["preview"]["context"], {"exam_id": "exam-1", "identity": value})
                prepared["preview"]["context"]["identity"] = "tampered-preview"
                result = c.submit_action(prepared["action_id"], confirm=True)
                self.assertEqual(result["status"], "submitted_unverified")
                self.assertEqual(dict(parse_qsl(adapter.sent[-1].body))["identity"], value)
                self.assertEqual([request.method for request in adapter.sent], ["GET", "GET", "POST"])

    def test_readonly_metadata_exposes_the_flag_without_its_value(self):
        html = self.form('<input name="identity" readonly value="native-private-identity">')
        result = parse_page(html, CHEM + "/student/review.php")
        fields = {field["name"]: field for field in result["forms"][0]["fields"]}
        self.assertTrue(fields["identity"]["readonly"])
        self.assertFalse(fields["reason"]["readonly"])
        self.assertNotIn("value", fields["identity"])
        self.assertNotIn("native-private-identity", json.dumps(result))

    def test_readonly_input_without_context_classification_is_rejected(self):
        form = self.form('<input name="identity" readonly value="native-id">')
        c, adapter = self.make(form, rule=RULE)
        with self.assertRaisesRegex(CourseHubError, "explicit reviewed context"):
            self.prepare(c)
        self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_caller_cannot_supply_a_readonly_value_even_if_declared_editable(self):
        form = self.form('<input name="identity" readonly value="native-id">')
        rule = {**RULE, "context_fields": ["exam_id", "identity"], "fields": ["reason", "exam", "identity"]}
        for value in ("native-id", "different-id"):
            with self.subTest(value=value):
                c, adapter = self.make(form, rule=rule)
                with self.assertRaisesRegex(CourseHubError, "cannot be supplied by the caller"):
                    self.prepare(c, {"reason": "Synthetic request", "exam": "midterm", "identity": value})
                self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_changed_readonly_target_is_bound_even_if_also_named_as_csrf(self):
        first = self.form('<input name="identity" readonly value="first-id">')
        second = self.form('<input name="identity" readonly value="different-id">')
        for csrf in (["csrf"], ["csrf", "identity"]):
            with self.subTest(csrf=csrf):
                rule = {**RULE, "context_fields": ["exam_id", "identity"], "csrf_fields": csrf}
                c, adapter = self.make(first, second, rule)
                token = self.prepare(c)["action_id"]
                with self.assertRaisesRegex(CourseHubError, "form changed"):
                    c.submit_action(token, confirm=True)
                self.assertEqual([request.method for request in adapter.sent], ["GET", "GET"])

    def test_disabled_readonly_context_is_rejected_before_post(self):
        for control in ['<input name="identity" readonly disabled value="native-id">',
                        '<fieldset disabled><input name="identity" readonly value="native-id"></fieldset>']:
            with self.subTest(control=control):
                c, adapter = self.make(self.form(control))
                with self.assertRaisesRegex(CourseHubError, "context control is disabled"):
                    self.prepare(c)
                self.assertEqual([request.method for request in adapter.sent], ["GET"])

    def test_unsupported_readonly_controls_are_rejected_before_post(self):
        controls = [
            '<textarea name="identity" readonly>native-id</textarea>',
            '<select name="identity" readonly><option value="native-id">Identity</option></select>',
            '<input name="identity" type="password" readonly value="native-id">',
            '<input name="identity" type="file" readonly>',
            '<input name="identity" type="hidden" readonly value="native-id">',
            '<input name="identity" type="date" readonly value="2026-11-02">',
            '<input name="identity" type="checkbox" readonly value="native-id">',
        ]
        for control in controls:
            with self.subTest(control=control):
                c, adapter = self.make(self.form(control))
                with self.assertRaises(CourseHubError):
                    self.prepare(c)
                self.assertEqual([request.method for request in adapter.sent], ["GET"])


class MaterialIndexTests(unittest.TestCase):
    HOME = '''<a href="/show_slide.php?s=MAT103Ech01">Chapter 1</a>
    <a href="/show_slide.php?s=MAT103Tch01">Chapter 1 Turkish</a>
    <a href="/show_slide.php?s=MAT104Ech01">Chapter 1 other course</a>
    <a href="/show_worksheet.php?ws=MAT103Ews1">Worksheet 1</a>
    <a href="/show_exam.php?exam=MAT103E26BF">Exam document</a>
    <a href="/mat/catalog.php?code=MAT103">MAT103/E</a>
    <a href="/files/quiz_guide.pdf">General quiz guide</a>
    <a href="/files/misc.pdf">Additional material</a>
    <a href="/student/results.php">Results page</a>
    <a href="/show_slide.php?s=MAT103Ech01">Duplicate chapter</a>'''

    def test_index_reads_one_root_and_never_downloads_materials(self):
        c, adapter = client([{"body": self.HOME}])
        result = c.materials("MAT")
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(result["pages_read"], 1)
        self.assertEqual(result["source_url"], MATH + "/")
        self.assertEqual(result["coverage"], "root_links_only")
        self.assertTrue(result["untrusted_external_content"])
        self.assertFalse(result["session_refreshed"])
        self.assertEqual(result["total_count"], 8)
        self.assertEqual(result["count"], 8)
        self.assertEqual(result["collection_counts"], {"slides": 3, "practice": 1, "exams": 1,
                                                     "syllabi": 1, "guides": 1, "unclassified": 1})

    def test_material_index_reports_a_cached_session_refresh(self):
        stale, adapter = transport([{"status": 401}])
        fresh, fresh_adapter = transport([
            {"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"body": self.HOME},
        ])
        c, _ = client([], session_factory=lambda: fresh,
                      credentials=lambda _: {"username": "synthetic", "student_number": "150000000"})
        c._accounts["chemistry"] = stale
        result = c.materials("KIM", authenticated=True)
        self.assertTrue(result["session_refreshed"])
        self.assertEqual(result["coverage"], "root_links_only")
        self.assertEqual(result["pages_read"], 1)
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(len(fresh_adapter.sent), 4)

    def test_filtered_index_keeps_exact_shared_and_general_material(self):
        c, adapter = client([{"body": self.HOME}])
        result = c.materials("MAT", course_code="MAT 103E")
        self.assertEqual(result["total_count"], 6)
        self.assertEqual(result["other_courses_excluded"], 2)
        by_url = {item["url"]: item for item in result["materials"]}
        self.assertEqual(by_url[MATH + "/show_slide.php?s=MAT103Ech01"]["course_match"], "exact_course")
        self.assertEqual(by_url[MATH + "/mat/catalog.php?code=MAT103"]["course_match"], "shared_course")
        self.assertEqual(by_url[MATH + "/files/quiz_guide.pdf"]["course_match"], "general_or_unknown")
        self.assertEqual(by_url[MATH + "/files/misc.pdf"]["course_match"], "general_or_unknown")
        self.assertIsNone(by_url[MATH + "/files/misc.pdf"]["collection"])
        self.assertEqual(len(adapter.sent), 1)

    def test_collection_filter_and_pagination_have_observed_counts(self):
        c, _ = client([{"body": self.HOME}])
        first = c.materials("MAT", collection="slides", limit=2)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["total_count"], 3)
        self.assertEqual(first["next_offset"], 2)
        self.assertTrue(first["has_more"])
        c, _ = client([{"body": self.HOME}])
        second = c.materials("MAT", collection="slides", offset=2, limit=2)
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["total_count"], 3)
        self.assertIsNone(second["next_offset"])
        self.assertFalse(second["has_more"])
        self.assertFalse({item["url"] for item in first["materials"]} & {item["url"] for item in second["materials"]})

    def test_authenticated_index_uses_the_existing_selected_account(self):
        account, adapter = transport([{"body": self.HOME}])
        credentials = Mock()
        c, public = client([], credentials=credentials)
        c._accounts["mathematics"] = account
        result = c.materials("MAT", authenticated=True, collection="exams")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["materials"][0]["native_identifier"], "MAT103E26BF")
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(public.sent, [])
        credentials.assert_not_called()

    def test_root_link_omissions_remain_explicit(self):
        html = "".join('<a href="/files/material' + str(i) + '.pdf">Material</a>' for i in range(405))
        c, adapter = client([{"body": html}])
        result = c.materials("MAT", limit=100)
        self.assertEqual(result["total_count"], 400)
        self.assertEqual(result["omitted"]["links"], 5)
        self.assertEqual(result["coverage"], "root_links_only")
        self.assertEqual(result["next_offset"], 100)
        self.assertEqual(len(adapter.sent), 1)

    def test_bad_inputs_are_rejected_before_source_access(self):
        for arguments in [{"offset": True}, {"offset": 0.0}, {"offset": -1}, {"offset": 10001},
                          {"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": 1.0},
                          {"collection": "other"}, {"course_code": "FIZ101"}, {"course_code": "MAT103T"}]:
            with self.subTest(arguments=arguments):
                c, adapter = client([])
                with self.assertRaises(CourseHubError):
                    c.materials("MAT", **arguments)
                self.assertEqual(adapter.sent, [])


class PublicExpansionTests(unittest.TestCase):
    YDY = "https://ydy.itu.edu.tr"
    ING100 = YDY + "/programlar/i-leri-i-ngilizce-programi/ing-100"
    ARDEK = "https://ardek.itu.edu.tr/docs/librariesprovider95/default-document-library/ing-103es.pdf?sfvrsn=202bdb92_0"
    KIMLAB = "https://kimya.itu.edu.tr/kimya-havuz/kim-101el"

    def test_new_source_inventory_is_public_only_and_explicitly_seeded(self):
        c, adapter = client([], write_rules=[])
        sites = {entry["site"]: entry for entry in c.sites()["sites"]}
        self.assertEqual(source.site_key("FIZLAB"), "physics_lab")
        self.assertEqual(source.site_key("KIMLAB"), "chemistry_lab")
        self.assertEqual(source.site_key("YDY"), "languages")
        self.assertEqual(source.site_key("LANG"), "languages")
        with self.assertRaises(CourseHubError):
            source.site_key("ALM")
        for key in ("physics_lab", "chemistry_lab", "languages"):
            self.assertEqual(sites[key]["authentication"], "public_only")
            self.assertFalse(sites[key]["capabilities"]["account_read"])
            self.assertFalse(sites[key]["capabilities"]["reviewed_write"])
        self.assertEqual(len(sites["physics_lab"]["entry_points"]), 4)
        self.assertEqual(len(sites["languages"]["entry_points"]), 20)
        self.assertEqual(adapter.sent, [])

    def test_public_only_sources_reject_account_and_write_requests_before_network(self):
        with patch.dict(os.environ, {"NINOVA_COURSEHUB_WRITES": "1"}):
            for key in ("physics_lab", "chemistry_lab", "languages"):
                c, adapter = client([])
                c._credentials = Mock(side_effect=AssertionError("Read credentials"))
                c._session_factory = Mock(side_effect=AssertionError("Created account session"))
                c._accounts[key] = Mock()
                for action in (
                    lambda: c.read_page(key, authenticated=True),
                    lambda: c.discover(key, authenticated=True),
                    lambda: c.materials(key, authenticated=True),
                    lambda: c.read_document("https://" + source.SITES[key] + "/files/example.pdf", authenticated=True),
                    lambda: c.prepare_action(key, source._source_root(key), "form", {"reason": "Synthetic"}),
                ):
                    with self.subTest(key=key, action=action), self.assertRaises(CourseHubError):
                        action()
                self.assertEqual(adapter.sent, [])
                c._credentials.assert_not_called()
                c._session_factory.assert_not_called()

    def test_sitefinity_static_content_survives_without_control_or_script_values(self):
        html = '''<title>Public laboratory</title><form method="post" id="aspnetForm">
        <h1>Laboratory calendar</h1><p>ALM101 and KIM101EL public guidance</p>
        <table><tr><th>Experiment</th><th>Calendar</th></tr><tr><td>Safety</td><td>November</td></tr></table>
        <div><textarea name="draft">UNIQUE_TEXTAREA_SECRET</textarea>
        <input type="hidden" name="__VIEWSTATE" value="UNIQUE_HIDDEN_SECRET">
        <input type="password" name="password" value="UNIQUE_PASSWORD_SECRET">
        <input name="student_number" readonly value="UNIQUE_INPUT_SECRET">
        <select name="choice"><option value="allowed-option">Allowed option</option></select>
        <script>window.value='UNIQUE_SCRIPT_SECRET'</script></div>
        <a href="/docs/safety.pdf">Safety booklet</a></form>'''
        page = parse_page(html, self.KIMLAB)
        self.assertIn("ALM101 and KIM101EL public guidance", page["text"])
        self.assertEqual(page["tables"][0]["rows"][1], ["Safety", "November"])
        self.assertIn("Laboratory calendar", page["sections"][0]["heading"])
        encoded = json.dumps(page)
        for sentinel in ("UNIQUE_TEXTAREA_SECRET", "UNIQUE_HIDDEN_SECRET", "UNIQUE_PASSWORD_SECRET", "UNIQUE_INPUT_SECRET", "UNIQUE_SCRIPT_SECRET"):
            self.assertNotIn(sentinel, encoded)
        self.assertIn("allowed-option", encoded)
        self.assertNotIn("Allowed option", page["text"])

    def test_full_lab_and_language_suffixes_never_truncate(self):
        codes = ["FIZ101L", "FIZ101EL", "KIM101L", "KIM101EL", "MAT103E", "ING100", "ING112A", "ING201A",
                 "ING103CO", "ING103ES", "ING103H", "ING103I", "ING103N", "ING103SC", "ALM101", "ARB102", "KOR101"]
        for code in codes:
            with self.subTest(code=code):
                html = '<a href="/files/example.pdf">' + code + '</a>'
                self.assertEqual(parse_page(html, MATH)["links"][0]["course_codes"], [code])
        html = '<a href="/docs/ing-103es.pdf">Ders Katalog Formu</a>'
        self.assertEqual(parse_page(html, self.ING100)["links"][0]["course_codes"], ["ING103ES"])
        for invalid in ("FIZ101ELE", "KIM10123EL", "ING103COX", "ALM101A", "MAT103EL"):
            self.assertEqual(parse_page('<a href="/files/example.pdf">' + invalid + '</a>', MATH)["links"][0]["course_codes"], [])

    def test_seed_selection_uses_real_family_and_retains_general_guidance(self):
        alm = source._selected_seeds("languages", "ALM101")
        self.assertEqual(alm[0].url, self.YDY + "/programlar/secmeli-diller/almanca")
        self.assertTrue(all(seed.general for seed in alm[1:]))
        english = source._selected_seeds("languages", "ING103ES")
        self.assertEqual(english[0].url, self.YDY + "/programlar/i-leri-i-ngilizce-programi/ing-103")
        self.assertFalse(any(seed.url.endswith("ing-100") for seed in english))
        turkish_lab = source._selected_seeds("physics_lab", "FIZ101L")
        self.assertTrue(all(seed.general for seed in turkish_lab))
        self.assertFalse(any("2020" in seed.url for seed in turkish_lab))

    def test_configured_unlinked_seeds_do_not_break_exploration_sorting(self):
        c, adapter = client([{"body": "<h1>Public content</h1>"}])
        result = c.discover("FIZLAB", max_pages=1)
        self.assertEqual(result["pages_read"], 1)
        self.assertEqual(result["remaining_pages"], 3)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(len(adapter.sent), 1)

    def test_new_sources_do_not_crawl_unrelated_same_host_navigation(self):
        c, adapter = client([{"status": 302, "headers": {"Location": self.YDY + "/hakkimizda/unrelated"}}])
        with self.assertRaises(CourseHubError):
            c.read_page("YDY", url=self.ING100)
        self.assertEqual(len(adapter.sent), 1)
        c, adapter = client([])
        with self.assertRaises(CourseHubError):
            c.read_page("YDY", url=self.YDY + "/administration")
        self.assertEqual(adapter.sent, [])

    def test_language_home_announcements_are_discoverable_and_readable(self):
        path = "/haber-detay/2026/10/23/program-announcement"
        c, adapter = client([
            {"body": '<form><h1>Announcements</h1><a href="' + path + '">Program announcement</a></form>'},
            {"body": '<form><h1>Program announcement</h1><p>Public class guidance.</p></form>'},
        ])
        result = c.discover("YDY", max_pages=1)
        self.assertEqual(result["pages"][0]["url"], self.YDY + "/")
        self.assertTrue(result["resources"][0]["readable"])
        self.assertEqual(result["coverage"], "partial")
        page = c.read_page("YDY", url=result["resources"][0]["url"])
        self.assertIn("Public class guidance.", page["text"])
        self.assertEqual([request.url for request in adapter.sent], [self.YDY + "/", self.YDY + path])

    def test_ardek_is_document_only_and_exact_paths_are_checked_at_every_redirect(self):
        c, adapter = client([{"body": b"%PDF-1.4 synthetic", "headers": {"Content-Type": "application/pdf"}}])
        with patch("ninova_mcp.course_hub.subprocess.run") as run:
            run.return_value.stdout = b'{"ok":true,"text":"Course syllabus"}'
            result = c.read_document(self.ARDEK)
        self.assertTrue(result["ok"])
        self.assertEqual(len(adapter.sent), 1)
        c, adapter = client([])
        with self.assertRaises(CourseHubError):
            c.read_page("YDY", url=self.ARDEK)
        with self.assertRaises(CourseHubError):
            c.read_document("https://ardek.itu.edu.tr/news/unverified.pdf")
        self.assertEqual(adapter.sent, [])
        c, adapter = client([{"status": 302, "headers": {"Location": "https://ardek.itu.edu.tr/docs/unverified.pdf"}}])
        with self.assertRaises(CourseHubError):
            c.read_document(self.ARDEK)
        self.assertEqual(len(adapter.sent), 1)

    def test_new_material_index_preserves_archival_provenance_and_link_titles(self):
        archive_pdf = "https://fizik.itu.edu.tr/docs/librariesprovider20/default-document-library/fiz101el-aciklama2020.pdf?sfvrsn=e79713a5_0"
        c, adapter = client([
            {"body": "<title>Laboratory announcement 2026</title><p>Current instructions</p>"},
            {"body": '<title>FIZ101EL 2020 Güz Dönemi</title><a href="' + archive_pdf + '">dokümanı</a>'},
            {"body": "<title>Laboratory course page</title>"},
            {"body": "<title>Student laboratories</title>"},
        ])
        result = c.materials("FIZLAB", course_code="FIZ101EL")
        self.assertEqual(result["coverage"], "seed_pages_only")
        self.assertEqual(result["pages_read"], 4)
        self.assertEqual(len(adapter.sent), 4)
        item = result["materials"][0]
        self.assertEqual(item["title"], "dokümanı")
        self.assertIn("2020", item["source_title"])
        self.assertTrue(item["provenance"][0]["archival"])
        self.assertNotIn("is_current", item)

    def test_lab_general_safety_stays_unassigned_and_legacy_doc_is_not_extracted(self):
        html = '''<form><h1>KIM101EL</h1><table><tr><td>Takvim</td><td>Experiment</td></tr></table>
        <a href="/files/safety.pdf">Güvenlik Kitapçığı</a><a href="/files/control.doc">Deney Kontrol Formu</a></form>'''
        c, adapter = client([{"body": html}])
        result = c.materials("KIMLAB", course_code="KIM101L", collection="guides")
        self.assertEqual(result["total_count"], 2)
        self.assertTrue(all(item["course_match"] == "general_or_unknown" for item in result["materials"]))
        legacy = next(item for item in result["materials"] if item["url"].endswith(".doc"))
        self.assertFalse(legacy["text_extraction_supported"])
        with self.assertRaisesRegex(CourseHubError, "browser"):
            c.read_document(legacy["url"])
        self.assertEqual(len(adapter.sent), 1)

    def test_multi_seed_dedup_preserves_all_course_evidence_and_promotes_known_categories(self):
        seeds = source._SOURCE_SEEDS["languages"][:2]
        shared = self.YDY + "/files/shared.pdf"
        with patch.dict(source._SOURCE_SEEDS, {"languages": seeds}):
            c, _ = client([
                {"body": '<title>ING100 source</title><a href="' + shared + '">Resource</a>'},
                {"body": '<title>ING112A source</title><a href="' + shared + '">ING112A Exam guide</a>'},
            ])
            result = c.materials("YDY", collection="guides")
        self.assertEqual(result["total_count"], 1)
        item = result["materials"][0]
        self.assertEqual(item["collection"], "guides")
        self.assertEqual(item["course_codes"], ["ING112A"])
        self.assertEqual(len(item["source_urls"]), 2)
        self.assertEqual(len(item["provenance"]), 2)
        self.assertEqual(item["titles"], ["Resource", "ING112A Exam guide"])

    def test_multi_seed_conflicting_labels_remain_explicit_in_collection_filters(self):
        seeds = source._SOURCE_SEEDS["languages"][:2]
        with patch.dict(source._SOURCE_SEEDS, {"languages": seeds}):
            c, _ = client([
                {"body": '<a href="/files/shared.pdf">Exam document</a>'},
                {"body": '<a href="/files/shared.pdf">Study guide</a>'},
            ])
            result = c.materials("YDY", collection="guides")
        item = result["materials"][0]
        self.assertIsNone(item["collection"])
        self.assertTrue(item["collection_ambiguous"])
        self.assertEqual(item["collections"], ["exams", "guides"])
        self.assertEqual(item["collection_basis"], "conflicting_labels")

    def test_multi_seed_limits_partial_failures_and_complete_failure_are_explicit(self):
        with patch.object(source, "MAX_SEED_PAGES", 1):
            c, _ = client([{"body": "<h1>Announcement</h1>"}])
            result = c.materials("FIZLAB")
        self.assertEqual(result["page_limit"], 1)
        self.assertEqual(result["remaining_seeds"], 3)
        self.assertTrue(result["partial"])
        seeds = source._SOURCE_SEEDS["languages"][:2]
        with patch.dict(source._SOURCE_SEEDS, {"languages": seeds}):
            c, _ = client([{"status": 503}, {"body": "<title>Available source</title>"}])
            result = c.materials("YDY")
            self.assertEqual(result["pages_attempted"], 2)
            self.assertEqual(result["pages_read"], 1)
            self.assertEqual(len(result["errors"]), 1)
            self.assertTrue(result["partial"])
            c, _ = client([{"status": 503}, {"status": 503}])
            with self.assertRaisesRegex(CourseHubError, "No verified source seed pages"):
                c.materials("YDY")

    def test_native_catalog_and_safety_labels_have_label_based_collections(self):
        for label, expected in [("Ders Katalog Formu", "syllabi"), ("Course catalogue", "syllabi"),
                                ("Güvenlik Kitapçığı", "guides"), ("Deney Kontrol Formu", "guides")]:
            page = parse_page('<a href="/files/example.pdf">' + label + '</a>', self.KIMLAB)
            self.assertEqual(page["links"][0]["collection"], expected)
            self.assertEqual(page["links"][0]["collection_basis"], "label_or_extension")


class PresentationTests(unittest.TestCase):
    def test_slide_order_and_speaker_notes_follow_package_relationships(self):
        result = extract_text_from_bytes(pptx(), extension=".pptx")
        self.assertTrue(result["ok"], result)
        text = result["text"]
        self.assertLess(text.index("First visible"), text.index("Second visible"))
        self.assertIn("Speaker notes:\nSpeaker explanation", text)
        self.assertEqual(guess_extension(content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation"), ".pptx")

    def test_real_document_worker_extracts_presentations(self):
        import subprocess
        import sys
        worker = subprocess.run([sys.executable, "-m", "ninova_mcp.file_text_worker", ".pptx", "30000"],
                                input=pptx(), capture_output=True, timeout=30, check=True)
        result = json.loads(worker.stdout)
        self.assertTrue(result["ok"])
        self.assertIn("Speaker explanation", result["text"])

    def test_invalid_presentation_is_reported_as_failure(self):
        self.assertFalse(extract_text_from_bytes(b"not a zip", extension=".pptx")["ok"])


class NativeLoginFlowTests(unittest.TestCase):
    SSO = "https://girisv3.itu.edu.tr/Login.aspx?currentURL=synthetic"
    LOGIN = '''<form method="post"><input type="hidden" name="__VIEWSTATE" value="login-state">
        <input name="ctl00$ContentPlaceHolder1$tbUserName"><input type="password" name="ctl00$ContentPlaceHolder1$tbPassword">
        <input type="submit" name="ctl00$ContentPlaceHolder1$btnLogin" value="Login"></form>'''
    NOTICE = '''<form method="post"><input type="hidden" name="__VIEWSTATE" value="notice-state">
        <input type="submit" name="Button1" value="Uyarıyı Geç »"><input type="checkbox" name="CheckBox1">
        <input type="submit" name="btSkip" value="Uyarıyı Geç »"><input type="checkbox" name="cbSkip"></form>'''

    @classmethod
    def popup(cls, callback=MATH + "/login.php?ReturnUrl=/"):
        from urllib.parse import urlencode
        return "https://girisv3.itu.edu.tr/Popup.aspx?" + urlencode({"currentUrl": callback, "subSessionId": "synthetic"})

    def test_standard_warning_is_skipped_without_changing_any_checkbox(self):
        account, adapter = transport([
            {"status": 302, "headers": {"Location": self.SSO}}, {"body": self.LOGIN},
            {"status": 302, "headers": {"Location": self.popup()}}, {"body": self.NOTICE},
            {"status": 302, "headers": {"Location": MATH + "/"}}, {"body": HOME},
            {"body": HOME}, {"body": HOME},
        ])
        c, _ = client([], session_factory=lambda: account,
                      credentials=lambda _: {"username": "synthetic", "password": "private-password"})
        result = c.read_page("MAT", authenticated=True)
        self.assertTrue(result["authenticated"])
        posts = [request for request in adapter.sent if request.method == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].headers["Origin"], "https://girisv3.itu.edu.tr")
        self.assertEqual(posts[1].headers["Referer"], self.popup())
        self.assertIn("btSkip=", posts[1].body)
        self.assertIn("notice-state", posts[1].body)
        self.assertNotIn("CheckBox1", posts[1].body)
        self.assertNotIn("cbSkip", posts[1].body)
        self.assertNotIn("private-password", posts[1].body)
        for request in adapter.sent:
            if request.url.startswith(MATH):
                self.assertNotIn("Origin", request.headers)
                self.assertNotIn("Referer", request.headers)

    def test_physics_login_and_account_verification_accept_the_selected_aliases(self):
        account, adapter = transport([
            {"status": 302, "headers": {"Location": PHYSICS_WWW + "/login.php?ReturnUrl=/"}},
            {"status": 302, "headers": {"Location": self.SSO}}, {"body": self.LOGIN},
            {"status": 302, "headers": {"Location": PHYSICS_WWW + "/"}}, {"body": HOME},
            {"status": 302, "headers": {"Location": PHYSICS_WWW + "/"}}, {"body": HOME},
            {"body": HOME},
        ])
        credentials = Mock(return_value={"username": "synthetic", "password": "private-password"})
        c, _ = client([], session_factory=lambda: account, credentials=credentials)
        result = c.read_page("FIZ", url=PHYSICS_WWW + "/", authenticated=True)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["url"], PHYSICS_WWW + "/")
        self.assertEqual(len(adapter.sent), 8)
        credentials.assert_called_once_with("physics")
        posts = [request for request in adapter.sent if request.method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].url.startswith("https://girisv3.itu.edu.tr/Login.aspx"))

    def test_warning_callback_accepts_only_the_selected_physics_aliases(self):
        hosts = {"fizikhavuz.itu.edu.tr", "www.fizikhavuz.itu.edu.tr", "girisv3.itu.edu.tr"}
        session, adapter = transport([
            {"status": 302, "headers": {"Location": PHYSICS + "/login.php?ReturnUrl=/"}}, {"body": HOME}])
        c, _ = client([])
        c._finish_sso_notice(session, self.popup(PHYSICS_WWW + "/login.php?ReturnUrl=/"),
                             self.NOTICE.encode(), "fizikhavuz.itu.edu.tr", hosts)
        self.assertEqual(len(adapter.sent), 2)
        self.assertNotIn("CheckBox1", adapter.sent[0].body)
        self.assertNotIn("cbSkip", adapter.sent[0].body)
        session, adapter = transport([])
        with self.assertRaises(CourseHubError):
            c._finish_sso_notice(session, self.popup(CHEM + "/login.php?ReturnUrl=/"),
                                 self.NOTICE.encode(), "fizikhavuz.itu.edu.tr", hosts)
        self.assertEqual(adapter.sent, [])

    def test_unknown_warning_or_untrusted_return_never_posts(self):
        variants = [
            (self.popup("https://evil.test/login.php"), self.NOTICE),
            (self.popup(), self.NOTICE.replace("Uyarıyı Geç »", "Accept agreement")),
            (self.popup(), self.NOTICE.replace('name="cbSkip"', 'name="cbSkip" checked')),
            (self.popup(), self.NOTICE.replace('<form method="post">', '<form method="post" action="/Other.aspx">')),
            (self.popup(), self.NOTICE.replace('<form method="post">', '<form method="post"><input type="password" name="extra">')),
        ]
        for url, html in variants:
            with self.subTest(url=url, html=html):
                session, adapter = transport([])
                c, _ = client([])
                with self.assertRaises(CourseHubError):
                    c._finish_sso_notice(session, url, html.encode(), "mathavuz.itu.edu.tr",
                                         {"mathavuz.itu.edu.tr", "girisv3.itu.edu.tr"})
                self.assertEqual(adapter.sent, [])

    def test_nested_encoded_warning_return_is_validated_without_rewriting_action(self):
        from urllib.parse import quote
        url = self.popup(quote(MATH + "/login.php?ReturnUrl=/", safe=""))
        session, adapter = transport([{ "status": 302, "headers": {"Location": MATH + "/"}}, {"body": HOME}])
        c, _ = client([])
        c._finish_sso_notice(session, url, self.NOTICE.encode(), "mathavuz.itu.edu.tr",
                             {"mathavuz.itu.edu.tr", "girisv3.itu.edu.tr"})
        self.assertEqual(adapter.sent[0].url, url)
        self.assertEqual(adapter.sent[0].method, "POST")
        self.assertEqual(len(adapter.sent), 2)
        self.assertNotIn("cbSkip", adapter.sent[0].body)
        self.assertNotIn("CheckBox1", adapter.sent[0].body)

    def test_nested_encoded_foreign_warning_return_never_posts(self):
        from urllib.parse import quote
        for destination in ("https://evil.test/login.php", "https://user:pw@mathavuz.itu.edu.tr/login.php",
                            "http://mathavuz.itu.edu.tr/login.php"):
            with self.subTest(destination=destination):
                session, adapter = transport([])
                c, _ = client([])
                with self.assertRaises(CourseHubError):
                    c._finish_sso_notice(session, self.popup(quote(destination, safe="")), self.NOTICE.encode(),
                                         "mathavuz.itu.edu.tr", {"mathavuz.itu.edu.tr", "girisv3.itu.edu.tr"})
                self.assertEqual(adapter.sent, [])

    def test_warning_hop_is_attempted_at_most_once(self):
        session, adapter = transport([{"body": self.NOTICE}])
        c, _ = client([])
        with self.assertRaisesRegex(CourseHubError, "still requires browser interaction"):
            c._finish_sso_notice(session, self.popup(), self.NOTICE.encode(), "mathavuz.itu.edu.tr",
                                 {"mathavuz.itu.edu.tr", "girisv3.itu.edu.tr"})
        self.assertEqual(len(adapter.sent), 1)

    def test_chemistry_login_preserves_native_origin_and_referer(self):
        account, adapter = transport([{"body": LOGIN}, {"body": "OK"}, {"body": FORM}, {"body": FORM}])
        c, _ = client([], session_factory=lambda: account,
                      credentials=lambda _: {"username": "synthetic", "student_number": "150000000"})
        c.read_page("KIM", authenticated=True)
        post=next(request for request in adapter.sent if request.method=="POST")
        self.assertEqual(post.headers["Origin"], CHEM)
        self.assertEqual(post.headers["Referer"], CHEM + "/student/login.php")


class FacultyHubFilterTests(unittest.TestCase):
    def test_faculty_hub_accepts_department_course_filters(self):
        for code in ("MAT103E", "FIZ101E", "KIM101"):
            with self.subTest(code=code):
                c, _ = client([{"body": "<a href='/files/guide.pdf'>General guide</a>"}])
                result = c.discover("FEF", course_code=code, max_pages=1)
                self.assertEqual(result["course_code"], code)
                self.assertEqual(result["resources"][0]["course_match"], "general_or_unknown")


class MaterialFormatTests(unittest.TestCase):
    def test_index_distinguishes_text_formats_from_video_and_legacy_files(self):
        html = '<a href="/files/guide.pdf">Guide</a><a href="/files/guide.mp4">Video guide</a><a href="/files/old.ppt">Legacy slides</a><a href="/show_slide.php?s=MAT103Ech01">Slide</a>'
        page = parse_page(html, MATH + "/")
        support = {link["url"]: link["text_extraction_supported"] for link in page["links"]}
        self.assertTrue(support[MATH + "/files/guide.pdf"])
        self.assertTrue(support[MATH + "/show_slide.php?s=MAT103Ech01"])
        self.assertFalse(support[MATH + "/files/guide.mp4"])
        self.assertFalse(support[MATH + "/files/old.ppt"])

    def test_unsupported_file_types_are_rejected_before_network_or_login(self):
        c, adapter = client([])
        c._credentials = Mock(side_effect=AssertionError("Read credentials"))
        for extension in (".mp4", ".webm", ".zip", ".doc", ".ppt", ".xls", ".xlsx"):
            with self.subTest(extension=extension), self.assertRaisesRegex(CourseHubError, "browser"):
                c.read_document(MATH + "/files/material" + extension, authenticated=True)
        self.assertEqual(adapter.sent, [])
        c._credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
