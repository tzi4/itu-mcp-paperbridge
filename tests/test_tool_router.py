"""Router tool profile: catalog coverage, ranking and the itu_call dispatcher."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("NINOVA_USERNAME", "dummy")
os.environ.setdefault("NINOVA_PASSWORD", "dummy")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ninova_mcp import server  # noqa: E402
from ninova_mcp.tool_catalog import (  # noqa: E402
    DOMAINS, KEYWORDS, REQUIRED_ONE_OF, TOOL_DOMAIN, WRITE_OPERATIONS, effect,
)
from ninova_mcp.tool_router import (  # noqa: E402
    CALL_TOOL_NAME, FAST_PATH_TOOL_NAMES, FIND_TOOL_NAME, PROFILE_ENV, fold,
    has_write_intent, selected_profile, tokenize,
)


def _build(profile: str, tool_names: list[str] | None = None) -> FastMCP:
    mcp = FastMCP("test")
    server.register_tools(mcp, server.NinovaMcpApp(),
                          tool_names or server.LOCAL_TOOL_NAMES, profile=profile)
    return mcp


def _call(mcp: FastMCP, name: str, arguments: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


class CatalogTests(unittest.TestCase):
    def test_every_tool_has_exactly_one_domain(self) -> None:
        listed = [name for _, names in DOMAINS.values() for name in names]
        self.assertEqual(len(listed), len(set(listed)), "a tool is listed in two domains")
        self.assertEqual(set(listed), set(server.LOCAL_TOOL_NAMES))

    def test_keywords_name_known_tools(self) -> None:
        self.assertLessEqual(set(KEYWORDS), set(TOOL_DOMAIN))

    def test_write_effects_match_the_server_safety_sets(self) -> None:
        self.assertLessEqual(WRITE_OPERATIONS, set(server.LOCAL_TOOL_NAMES))
        # Every write stays published for per-call confirmation, never behind itu_call.
        self.assertLessEqual(WRITE_OPERATIONS, server.STATEFUL_TOOL_NAMES)
        self.assertLessEqual(set(server.DESTRUCTIVE_TOOL_NAMES), WRITE_OPERATIONS)
        for name in (server.MAIL_SEND_TOOL_NAMES | server.MAIL_DRAFT_TOOL_NAMES
                     | server.HELP_TICKET_WRITE_TOOL_NAMES | server.OBS_REGISTRATION_WRITE_TOOL_NAMES):
            self.assertEqual(effect(name), "write", name)
        for name in ("mail_get_thread", "itu_health", "download_resource", "refresh_session"):
            self.assertEqual(effect(name), "read", name)

    def test_required_choices_name_optional_parameters(self) -> None:
        mcp = FastMCP("test")
        server.register_tools(mcp, server.NinovaMcpApp(), server.LOCAL_TOOL_NAMES, profile="full")
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        for name, groups in REQUIRED_ONE_OF.items():
            schema = tools[name].inputSchema
            for param in {param for group in groups for param in group}:
                with self.subTest(operation=name, param=param):
                    self.assertIn(param, schema["properties"])
                    self.assertNotIn(param, schema.get("required", []))


class TextTests(unittest.TestCase):
    def test_turkish_letters_fold_to_ascii(self) -> None:
        self.assertEqual(fold("İTÜ Ödev Şubesi ığç"), "itu odev subesi igc")

    def test_stacked_suffixes_meet_the_stem(self) -> None:
        self.assertIn("not", tokenize("notlarım"))
        self.assertIn("ders", tokenize("derslerim"))
        self.assertIn("sinif", tokenize("sınıfta"))


class ProfileTests(unittest.TestCase):
    def test_default_profile_is_router(self) -> None:
        self.assertEqual(selected_profile({}), "router")

    def test_unknown_profile_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            selected_profile({PROFILE_ENV: "tiny"})

    def test_full_profile_publishes_every_tool(self) -> None:
        names = _names(_build("full"))
        self.assertEqual(names, set(server.LOCAL_TOOL_NAMES))

    def test_router_keeps_writes_state_mail_and_fast_paths_published(self) -> None:
        names = _names(_build("router"))
        self.assertIn(FIND_TOOL_NAME, names)
        self.assertIn(CALL_TOOL_NAME, names)
        must_publish = (
            set(server.STATEFUL_TOOL_NAMES) | set(server.DESTRUCTIVE_TOOL_NAMES)
            | set(server.MAIL_TOOL_NAMES) | set(FAST_PATH_TOOL_NAMES) | {"search", "fetch"}
        )
        self.assertLessEqual(must_publish, names)
        self.assertNotIn("get_cafeteria_menu", names)
        self.assertLess(len(names), len(server.LOCAL_TOOL_NAMES) // 2)

    def test_router_loses_no_operation(self) -> None:
        mcp = _build("router")
        listed = _call(mcp, FIND_TOOL_NAME, {"domain": "obs"})
        self.assertIn("obs_get_advisor", [op["operation"] for op in listed["operations"]])
        published = _names(mcp) - {FIND_TOOL_NAME, CALL_TOOL_NAME}
        reachable = set(published)
        for domain in DOMAINS:
            reachable.update(op["operation"] for op in _call(mcp, FIND_TOOL_NAME, {"domain": domain})["operations"])
        self.assertEqual(reachable, set(server.LOCAL_TOOL_NAMES))

    def test_router_tools_are_read_only(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(_build("router").list_tools())}
        for name in (FIND_TOOL_NAME, CALL_TOOL_NAME):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)

    def test_router_respects_the_selected_tool_subset(self) -> None:
        mcp = _build("router", ["auth_status", "get_cafeteria_menu", "submit_assignment"])
        found = _call(mcp, FIND_TOOL_NAME, {"query": "obs danışman"})
        self.assertNotIn("obs_get_advisor", [m["operation"] for m in found["matches"]])
        refused = _call(mcp, CALL_TOOL_NAME, {"operation": "obs_get_advisor", "arguments": {}})
        self.assertEqual(refused["error"], "unknown_operation")


class RankingTests(unittest.TestCase):
    CASES = {
        "Ninova'da hocaların girdiği notlarım": "get_course_grades",
        "OBS harf notlarım açıklandı mı": "obs_get_course_grades",
        "Bugün yemekhanede ne var": "get_cafeteria_menu",
        "Şu an boş derslik var mı": "find_empty_classrooms",
        "Danışmanım kim": "obs_get_advisor",
        "devamsızlık sınırına yakın mıyım": "obs_get_attendance",
        "Mezun olmak için kaç dersim kaldı": "obs_get_graduation_remaining",
        "BLG 336E dersini geçmişte kim verdi": "archive_who_taught",
        "MAT 201E için hangi hocadan almalıyım": "hocametre_course_sections",
        "Ayşe Tosun hocası nasıl": "hocametre_instructor",
        "3.00 ortalama için kaç almam gerekir": "calculate_target_gpa",
        "kampüs kartı bakiyesi": "obs_get_campus_card",
        "When are my exams": "get_personal_exam_calendar",
        "obs_get_profile": "obs_get_profile",
        "ProQuest üzerinden kurumsal makale ve tez ara": "papers_library_query",
        "ProQuest üzerinden tez ara": "papers_library_query",
        "Find ProQuest journal articles": "papers_library_query",
        "İTÜ araştırma veritabanlarını konuya göre bul": "papers_resources",
        "Find research databases by subject": "papers_resources",
        "2022 sonrası uzay aracı tutum kontrolü makalelerini bul": "papers_search",
        "Search OpenAlex for recent spacecraft papers": "papers_search",
        "makale ara": "papers_search",
        "Find papers": "papers_search",
        "10.1109/TAC.2022.3143082 DOI erişim bağlantısını bul": "papers_resolve",
        "makale tam metnini oku": "papers_get_text",
        "Kütüphane kataloğunda kitap ara": "library_search",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build("router")

    def test_expected_operation_ranks_first(self) -> None:
        for query, expected in self.CASES.items():
            with self.subTest(query=query):
                found = _call(self.mcp, FIND_TOOL_NAME, {"query": query, "limit": 3})
                self.assertEqual(found["matches"][0]["operation"], expected)

    def test_matches_carry_compact_schemas(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "hedef ortalama", "limit": 1})
        schema = found["matches"][0]["input_schema"]
        self.assertIn("target_gpa", schema["properties"])
        self.assertNotIn("title", json.dumps(schema))

    def test_paper_intent_returns_canonical_tool_with_discovery_options(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "makale ara", "limit": 1})
        match = found["matches"][0]
        self.assertEqual(match["operation"], "papers_search")
        self.assertEqual(match["call_with"], CALL_TOOL_NAME)
        properties = match["input_schema"]["properties"]
        self.assertIn("combined", properties["discovery"]["enum"])
        self.assertIn("year_from", properties)
        self.assertIn("kind", properties)

    def test_research_compatibility_alias_remains_explicitly_discoverable(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "search", "limit": 1})
        match = found["matches"][0]
        self.assertEqual(match["operation"], "search")
        self.assertEqual(match["call_with"], "search")

    def test_only_leading_matches_carry_schemas(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "sınav takvimi", "limit": 5})
        with_schema = ["input_schema" in match for match in found["matches"]]
        self.assertEqual(with_schema, [True, True] + [False] * (len(with_schema) - 2))

    def test_call_description_indexes_every_routed_operation(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_tools())}
        description = tools[CALL_TOOL_NAME].description
        self.assertIn("calculate_target_gpa(current_gpa, current_credits, target_gpa, "
                      "future_credits)", description)
        self.assertIn("get_cafeteria_menu(date?, meal?, vegan?)", description)
        routed = set(server.LOCAL_TOOL_NAMES) - set(tools)
        for name in routed:
            self.assertIn(f" {name}(", description.replace("\n", " "))
        for name in ("submit_assignment(", "mail_list_inbox(", "get_dashboard("):
            self.assertNotIn(name, description)

    def test_index_marks_required_choices(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_tools())}
        description = tools[CALL_TOOL_NAME].description
        self.assertIn("obs_get_course_grades(class_id | course, semester?, include_midterms?, "
                      "include_letter?)", description)
        self.assertIn("get_assignment_upload_slots(upload_url | course+assignment)", description)

    def test_published_tools_point_to_themselves(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "ödev teslim et yükle"})
        submit = next(m for m in found["matches"] if m["operation"] == "submit_assignment")
        self.assertEqual(submit["call_with"], "submit_assignment")

    def test_empty_request_lists_domains(self) -> None:
        listed = _call(self.mcp, FIND_TOOL_NAME, {})
        self.assertIn("obs", [entry["domain"] for entry in listed["domains"]])


class IntentTests(unittest.TestCase):
    def test_write_intent(self) -> None:
        for query in ("send an email to my instructor", "hocama mail at", "ödev teslim et",
                      "bu dosyayı yükle", "maili gönder", "open a help ticket",
                      "kitabı ayırt", "renew my library book"):
            with self.subTest(query=query):
                self.assertTrue(has_write_intent(query))
        for query in ("gönderdiğim mailler", "gönderilen mailler", "ödevin teslim tarihi",
                      "did my instructor reply", "hocam cevap yazdı mı", "iletişim bilgilerim",
                      "show the write capability of all services", "read-only global search",
                      "dosya yüklendi mi", "bildirimlerim"):
            with self.subTest(query=query):
                self.assertFalse(has_write_intent(query))


class EffectRankingTests(unittest.TestCase):
    HEALTH = "show health, auth, latency, rate-limit and write capability of all services"
    GLOBAL_SEARCH = "read-only global search over every service"

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build("router")

    def find(self, **arguments) -> dict:
        return _call(self.mcp, FIND_TOOL_NAME, arguments)

    def test_health_query_finds_the_health_check_not_fill_rate(self) -> None:
        # "rate" once ranked archive_fill_rate first.
        found = self.find(query=self.HEALTH)
        names = [match["operation"] for match in found["matches"]]
        self.assertEqual(names[0], "itu_health")
        self.assertTrue(found["exact_match"])
        self.assertFalse(found["write_intent"])
        self.assertFalse(WRITE_OPERATIONS & set(names))

    def test_read_only_global_search_lists_no_write_tools(self) -> None:
        # This query once surfaced mail_prepare_message and mail_send_message.
        found = self.find(query=self.GLOBAL_SEARCH, limit=10)
        names = [match["operation"] for match in found["matches"]]
        self.assertGreaterEqual(len(names), 6)
        self.assertFalse(WRITE_OPERATIONS & set(names[:6]))
        effects = [match["effect"] for match in found["matches"]]
        self.assertEqual(effects, sorted(effects))  # any write comes after every read
        self.assertIn("global", found["unmatched_terms"])

    def test_reads_rank_above_writes_without_write_intent(self) -> None:
        for query in ("did my instructor reply", "search all messages and attachments from my instructor",
                      "find everything mentioning the lab report in mail and Ninova"):
            with self.subTest(query=query):
                effects = [match["effect"] for match in self.find(query=query, limit=10)["matches"]]
                self.assertEqual(effects, sorted(effects))  # "read" < "write"
        found = self.find(query="did my instructor reply")
        self.assertEqual(found["matches"][0]["operation"], "mail_get_thread")

    def test_write_intent_keeps_write_tools_first(self) -> None:
        found = self.find(query="send an email to my instructor")
        self.assertTrue(found["write_intent"])
        self.assertEqual(found["matches"][0]["effect"], "write")
        self.assertTrue(found["exact_match"])

    def test_effect_filter(self) -> None:
        for wanted in ("read", "write"):
            with self.subTest(effect=wanted):
                found = self.find(query="mail", effect=wanted, limit=10)
                self.assertTrue(found["matches"])
                self.assertEqual({match["effect"] for match in found["matches"]}, {wanted})
        listed = self.find(domain="mail", effect="write")
        self.assertEqual({op["operation"] for op in listed["operations"]},
                         {"mail_prepare_message", "mail_send_message", "mail_save_draft"})
        router = server.ToolRouter(mock.Mock(list_tools=lambda: []), {})
        self.assertEqual(router.find("mail", effect_filter="delete")["error"], "unknown_effect")

    def test_weak_match_reports_a_missing_capability(self) -> None:
        for query in ("what is the weather in Istanbul", "rezervasyon yap spor salonu",
                      "renew my library book"):
            with self.subTest(query=query):
                found = self.find(query=query)
                self.assertFalse(found["exact_match"])
                self.assertIn("missing_capability", found)
        for query in RankingTests.CASES:
            with self.subTest(query=query):
                found = self.find(query=query)
                self.assertTrue(found["exact_match"])
                self.assertNotIn("missing_capability", found)

    def test_ranking_is_deterministic(self) -> None:
        for query in (self.HEALTH, self.GLOBAL_SEARCH, "hocama mail at"):
            self.assertEqual(self.find(query=query), self.find(query=query))


class CallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build("router")

    def test_runs_a_routed_operation(self) -> None:
        arguments = {"current_gpa": 2.8, "current_credits": 60, "target_gpa": 3.0, "future_credits": 20}
        direct = server.NinovaMcpApp().calculate_target_gpa(**arguments)
        routed = _call(self.mcp, CALL_TOOL_NAME,
                       {"operation": "calculate_target_gpa", "arguments": arguments})
        self.assertTrue(routed["untrusted_external_content"])
        routed.pop("untrusted_external_content")
        routed.pop("content_notice")
        self.assertEqual(routed, direct)

    def test_invalid_arguments_return_the_schema(self) -> None:
        result = _call(self.mcp, CALL_TOOL_NAME,
                       {"operation": "calculate_target_gpa", "arguments": {"current_gpa": 2.8}})
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertIn("target_gpa", result["input_schema"]["properties"])

    def test_write_tools_are_never_dispatched(self) -> None:
        for operation in ("submit_assignment", "obs_save_registration_draft", "help_submit_ticket"):
            with self.subTest(operation=operation):
                with mock.patch.object(server.NinovaMcpApp, operation) as write:
                    result = _call(self.mcp, CALL_TOOL_NAME, {"operation": operation, "arguments": {}})
                self.assertEqual(result["error"], "not_routed")
                write.assert_not_called()

    def test_missing_required_choice_does_not_run(self) -> None:
        with mock.patch.object(server.NinovaMcpApp, "obs_get_course_grades") as read:
            result = _call(self.mcp, CALL_TOOL_NAME,
                           {"operation": "obs_get_course_grades", "arguments": {"semester": "202610"}})
        read.assert_not_called()
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertEqual(result["message"], "Pass one of: class_id; course.")
        self.assertIn("class_id", result["input_schema"]["properties"])

    def test_failed_operation_returns_its_schema(self) -> None:
        # A one-letter query fails before any network access.
        result = _call(self.mcp, CALL_TOOL_NAME,
                       {"operation": "obs_search_courses", "arguments": {"query": "a"}})
        self.assertEqual(result["error"], "operation_failed")
        self.assertIn("at least two characters", result["message"])
        self.assertIn("query", result["input_schema"]["properties"])
        self.assertTrue(result["untrusted_external_content"])

    def test_unknown_operation_suggests_names(self) -> None:
        result = _call(self.mcp, CALL_TOOL_NAME, {"operation": "cafeteria menu", "arguments": {}})
        self.assertEqual(result["error"], "unknown_operation")
        self.assertIn("get_cafeteria_menu", result["suggestions"])


class CallLogTests(unittest.TestCase):
    def test_log_records_metadata_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "calls.jsonl")
            with mock.patch.dict(os.environ, {"ITU_MCP_CALL_LOG": path}):
                mcp = _build("router")
                _call(mcp, FIND_TOOL_NAME, {"query": "hedef ortalama"})
                _call(mcp, CALL_TOOL_NAME, {"operation": "calculate_target_gpa", "arguments": {
                    "current_gpa": 2.5, "current_credits": 40, "target_gpa": 2.9, "future_credits": 20}})
            with open(path, encoding="utf-8") as stream:
                entries = [json.loads(line) for line in stream]
        self.assertEqual([entry["tool"] for entry in entries],
                         [FIND_TOOL_NAME, "calculate_target_gpa", CALL_TOOL_NAME])
        self.assertEqual(entries[2]["operation"], "calculate_target_gpa")
        for entry in entries:
            self.assertEqual(set(entry) - {"operation"}, {"ts", "tool", "ms", "result_chars", "error"})


if __name__ == "__main__":
    unittest.main()
