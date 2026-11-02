"""Router tool profile: find an operation, then call it.

By default (``ITU_MCP_TOOL_PROFILE=router``) the server publishes a small set
of tools instead of every operation; ``full`` publishes every operation. Read-only operations stay registered on a
private FastMCP ``ToolManager`` and are reached through two public tools:

- ``itu_find_tool`` lists a domain or ranks operations for a query and
  returns the input schema of each match, so the model sees exactly the
  schemas it needs.
- ``itu_call`` validates the arguments against the same schema and runs the
  operation through the same wrapper and validation as a directly published
  tool. Its description carries a compact index of operation signatures, so a
  clear request can skip the lookup.

Ranking is deterministic BM25 over each operation's name, title, description
and Turkish/English keywords, with Turkish characters folded to ASCII and
common Turkish suffixes stripped. Each operation has an effect, read or write;
unless the query asks for a change, reads rank above writes. When the best match
covers too little of the query, the result says so (``exact_match: false`` and
``missing_capability``) rather than offering only loosely related tools. The
model makes the final choice from the returned candidates.

Tools that change data or local state, mail tools, tools that render their
own UI and a few frequent fast paths are never routed: they stay published
as ordinary tools so clients keep showing and confirming them individually.
"""

from __future__ import annotations

import copy
import math
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Any

from .tool_catalog import DOMAINS, EFFECTS, KEYWORDS, REQUIRED_ONE_OF, TOOL_DOMAIN, effect

PROFILE_ENV = "ITU_MCP_TOOL_PROFILE"
FULL_PROFILE = "full"
ROUTER_PROFILE = "router"
PROFILES = (FULL_PROFILE, ROUTER_PROFILE)

FIND_TOOL_NAME = "itu_find_tool"
CALL_TOOL_NAME = "itu_call"

# Frequent entry points that stay published so a common question needs no
# lookup first.
FAST_PATH_TOOL_NAMES = frozenset({
    "get_dashboard",
    "list_courses",
    "get_course_overview",
    "obs_get_schedule",
})
# Mail stays published so clients show and confirm each mailbox read.
NEVER_ROUTED_DOMAINS = frozenset({"mail"})
# ChatGPT deep research expects these two names.
NEVER_ROUTED_TOOL_NAMES = frozenset({"search", "fetch", "itu_search", "itu_open"})

MAX_ERROR_MESSAGE = 500
DEFAULT_FIND_LIMIT = 5
MAX_FIND_LIMIT = 10
# Only the leading matches carry a full input schema; the rest are summaries.
SCHEMA_MATCHES = 2
AMBIGUITY_RATIO = 0.85

_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
    "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
})
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORD_TEXT = (
    "a an and are be by do for from get how i in is it list me my of on or show "
    "the this to what when where which who with "
    # Scope words say how much to search, not what.
    "about across all any anything can every everything over please you your "
    "did does has have was were "
    "butun her hepsi tum "
    "acaba bana beni bu bunu bir da de en gibi hangi icin ile kac mi mu mı ne neler "
    "nedir nerede o olan şey şu var ve veya ya "
    # Question particles carry no topic.
    "mi mı mu mü miyim mıyım muyum müyüm misin mısın musun müsün midir mıdır mudur"
)
# Longest first; applied once to folded tokens so "notlarim" and "notu"
# both meet "not". Both the full token and its stem are indexed.
_SUFFIXES = tuple(sorted({
    "lerimizin", "larimizin", "lerimiz", "larimiz", "lerinin", "larinin",
    "lerini", "larini", "lerin", "larin", "leri", "lari", "ler", "lar",
    "imizin", "umuzun", "imiz", "umuz", "inin", "unun", "ini", "unu",
    "nin", "nun", "dan", "den", "tan", "ten", "da", "de", "ta", "te",
    "im", "um", "in", "un", "si", "su", "yi", "yu", "i", "u",
    "ina", "ine", "una", "une", "na", "ne", "ya", "ye", "a", "e",
}, key=len, reverse=True))
_MIN_STEM = 3
_MAX_SUFFIX_PASSES = 3
# Below this IDF-weighted share of the query's catalog words that the best
# match covers, the request is treated as one no operation offers. Words the
# catalog never uses (names, course codes, topics) do not count either way.
MIN_COVERAGE = 0.4
MAX_MISSING_WORDS = 8

# Requests to change something, matched on folded text. Past and passive forms
# ("gönderilen", "gönderdiğim", "yüklendi mi") and questions about a reply are
# reads, so only imperative and future forms count.
_WRITE_INTENT = re.compile(
    r"\b(?:send|submit|upload|save|forward|compose|create|apply|cancel|delete|renew|reserve"
    r"|register|enroll)\b|\brezerv(?:e|asyon)\s+(?:et|yap)\w*|\bayirt\w*"
    r"|\b(?:reply|respond|write)\s+(?:to|an?|back)\b|\bopen\s+an?\s+(?:help\s+)?ticket"
    r"|\bgonder(?!il|d|mis)\w*|\byolla(?!n|d)\w*|\byaz(?:\b|in\b|ar\s+mi|alim)"
    r"|\b(?:yanitla|cevapla)(?!n|d)\w*|\bilet(?:\b|ir\b|elim|in\b)|\byonlendir(?!il|d)\w*"
    r"|\byukle(?!n|d|mis)\w*|\bkaydet(?!ti|il|mis)\w*|\bolustur(?!ul|d)\w*|\bkaydol\w*"
    r"|\bteslim\s+e[dt](?!il|ti)\w*|\b(?:mail|e-?posta|mesaj)\w*\s+at\b"
    r"|\b(?:bilet|talep)\w*\s+ac\b|\bsikayet\s+et\w*"
)
# Talk about write permissions, or an explicit read-only request, is not a write.
_READ_ONLY_TALK = re.compile(
    r"\bread[- ]only\b|\bsalt\s+okunur\b"
    r"|\b(?:write|writing|yazma)\s+(?:capabilit|permission|access|action|izin|yetki)\w*"
)


def selected_profile(env: dict[str, str] | None = None) -> str:
    """Return the configured tool profile; unknown values fail at startup."""
    value = ((env if env is not None else os.environ).get(PROFILE_ENV) or ROUTER_PROFILE)
    value = value.strip().lower()
    if value not in PROFILES:
        raise RuntimeError(
            f"{PROFILE_ENV}={value!r} is not a known tool profile. "
            f"Valid profiles: {', '.join(PROFILES)}."
        )
    return value


def is_routable(name: str, stateful: Iterable[str], unstructured: Iterable[str],
                has_ui: Iterable[str]) -> bool:
    """Whether ``name`` may be hidden behind ``itu_call``."""
    return not (
        name in FAST_PATH_TOOL_NAMES
        or name in NEVER_ROUTED_TOOL_NAMES
        or TOOL_DOMAIN.get(name) in NEVER_ROUTED_DOMAINS
        or name in set(stateful)
        or name in set(unstructured)
        or name in set(has_ui)
    )


def fold(text: str) -> str:
    """Lowercase and fold Turkish letters and accents to ASCII."""
    text = unicodedata.normalize("NFKD", text.translate(_FOLD).lower())
    return "".join(char for char in text if not unicodedata.combining(char))


_STOPWORDS = frozenset(fold(word) for word in _STOPWORD_TEXT.split())


def _stem(token: str) -> str:
    # A few passes peel stacked suffixes: "notlarim" -> "notlar" -> "not".
    for _ in range(_MAX_SUFFIX_PASSES):
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
                token = token[: -len(suffix)]
                break
        else:
            break
    return token


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _TOKEN.findall(fold(text)):
        if token in _STOPWORDS:
            continue
        # English plurals index as singular on both sides: "exams" == "exam".
        if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        tokens.append(token)
        stem = _stem(token)
        if stem != token:
            tokens.append(stem)
    return tokens


def has_write_intent(query: str) -> bool:
    """Whether the request asks to send, submit, save or otherwise change data."""
    text = _READ_ONLY_TALK.sub(" ", fold(query))
    return bool(_WRITE_INTENT.search(text))


def query_words(query: str) -> list[tuple[str, frozenset[str]]]:
    """The query's content words, each with the index terms it can match."""
    words: dict[str, frozenset[str]] = {}
    for word in _TOKEN.findall(fold(query)):
        terms = tokenize(word)
        if terms and word not in words:
            words[word] = frozenset(terms)
    return list(words.items())


def _summary(description: str, limit: int = 160) -> str:
    text = " ".join(description.split())
    end = text.find(". ")
    sentence = text if end < 0 else text[: end + 1]
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def compact_schema(schema: Any) -> Any:
    """Drop titles that FastMCP adds to every property; they only cost tokens."""
    if isinstance(schema, dict):
        return {key: compact_schema(value) for key, value in schema.items() if key != "title"}
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    return schema


class OperationIndex:
    """BM25 ranking over operation metadata. Same input, same output."""

    K1 = 1.2
    B = 0.75
    FIELD_WEIGHTS = {"name": 3, "keywords": 2, "title": 2, "domain": 1, "description": 1}

    def __init__(self, operations: dict[str, dict[str, Any]]) -> None:
        self.operations = operations
        self._terms: dict[str, Counter[str]] = {}
        for name, meta in operations.items():
            counts: Counter[str] = Counter()
            fields = {
                "name": name.replace("_", " "),
                "keywords": KEYWORDS.get(name, ""),
                "title": meta.get("title") or "",
                "domain": TOOL_DOMAIN.get(name, ""),
                "description": meta.get("description") or "",
            }
            for field, text in fields.items():
                for token in tokenize(text):
                    counts[token] += self.FIELD_WEIGHTS[field]
            self._terms[name] = counts
        lengths = [sum(counts.values()) for counts in self._terms.values()]
        self._average_length = (sum(lengths) / len(lengths)) if lengths else 1.0
        document_frequency: Counter[str] = Counter()
        for counts in self._terms.values():
            document_frequency.update(counts.keys())
        total = len(self._terms)
        self._idf = {
            term: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def score(self, query: str) -> list[tuple[str, float]]:
        query_terms = set(tokenize(query))
        exact = fold(query).strip().replace(" ", "_")
        results = []
        for name, counts in self._terms.items():
            length = sum(counts.values())
            total = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                norm = frequency + self.K1 * (1 - self.B + self.B * length / self._average_length)
                total += self._idf.get(term, 0.0) * frequency * (self.K1 + 1) / norm
            if name == exact:
                total += 100.0
            if total > 0:
                results.append((name, total))
        results.sort(key=lambda item: (-item[1], item[0]))
        return results

    def uncovered(self, name: str, words: list[tuple[str, frozenset[str]]]) -> list[str]:
        """The query words that operation ``name`` does not match at all."""
        counts = self._terms.get(name, Counter())
        return [word for word, terms in words if not any(counts.get(term) for term in terms)]

    def coverage(self, name: str, words: list[tuple[str, frozenset[str]]]) -> float:
        """IDF-weighted share of the query's catalog words that ``name`` matches."""
        counts = self._terms.get(name, Counter())
        known = [terms for word, terms in words
                 if any(term in self._idf for term in terms) and not any(char.isdigit() for char in word)]
        weights = [(max(self._idf.get(term, 0.0) for term in terms),
                    any(counts.get(term) for term in terms)) for terms in known]
        total = sum(weight for weight, _ in weights)
        return sum(weight for weight, hit in weights if hit) / total if total else 1.0


class ToolRouter:
    """Backs ``itu_find_tool`` and ``itu_call`` for one server instance."""

    def __init__(self, manager: Any, published: dict[str, dict[str, Any]]) -> None:
        # ``manager`` holds the routed operations; ``published`` describes the
        # tools that remain ordinary MCP tools, so a search can point to them.
        self.manager = manager
        self.published = published
        routed = {
            tool.name: {"title": tool.title, "description": tool.description,
                        "input_schema": tool.parameters}
            for tool in manager.list_tools()
        }
        self.routed_names = frozenset(routed)
        self.operations = {**published, **routed}
        self.index = OperationIndex(self.operations)

    def _describe(self, name: str, with_schema: bool) -> dict[str, Any]:
        meta = self.operations[name]
        entry: dict[str, Any] = {
            "operation": name,
            "summary": _summary(meta.get("description") or meta.get("title") or ""),
            # A published tool is called by its own name, never through itu_call.
            "call_with": CALL_TOOL_NAME if name in self.routed_names else name,
            "effect": effect(name),
        }
        if with_schema:
            entry["input_schema"] = compact_schema(meta.get("input_schema") or {})
        return entry

    def signature(self, name: str) -> str:
        """``name(required, optional?)``; ``a | b+c`` marks a required choice."""
        schema = self.operations[name].get("input_schema") or {}
        required = set(schema.get("required") or ())
        groups = REQUIRED_ONE_OF.get(name, ())
        grouped = {param for group in groups for param in group}
        params = [" | ".join("+".join(group) for group in groups)] if groups else []
        params += [param if param in required else param + "?"
                   for param in schema.get("properties") or {} if param not in grouped]
        return f"{name}({', '.join(params)})"

    def operation_index(self) -> str:
        """One line per domain listing the routed operations' signatures."""
        lines = []
        for domain, (_, names) in DOMAINS.items():
            signatures = [self.signature(name) for name in names if name in self.routed_names]
            if signatures:
                lines.append(f"{domain}: {', '.join(signatures)}")
        return "\n".join(lines)

    def domains(self) -> list[dict[str, Any]]:
        available = set(self.operations)
        return [
            {"domain": domain, "description": description,
             "operations": sum(name in available for name in names)}
            for domain, (description, names) in DOMAINS.items()
            if any(name in available for name in names)
        ]

    def find(self, query: str = "", domain: str = "", limit: int = DEFAULT_FIND_LIMIT,
             effect_filter: str = "") -> dict[str, Any]:
        query = (query or "").strip()
        domain = (domain or "").strip().lower()
        effect_filter = (effect_filter or "").strip().lower()
        limit = max(1, min(int(limit or DEFAULT_FIND_LIMIT), MAX_FIND_LIMIT))
        if domain and domain not in DOMAINS:
            return {"error": "unknown_domain", "domains": self.domains()}
        if effect_filter and effect_filter not in EFFECTS:
            return {"error": "unknown_effect", "effects": list(EFFECTS)}
        if not query and not domain:
            return {"domains": self.domains(),
                    "hint": "Pass a domain to list its operations, or a query to rank them."}
        if not query:
            names = [name for name in DOMAINS[domain][1] if name in self.operations
                     and (not effect_filter or effect(name) == effect_filter)]
            return {"domain": domain,
                    "operations": [self._describe(name, with_schema=False) for name in names],
                    "hint": "Search with the chosen operation name to get its input_schema."}
        ranked = self.index.score(query)
        if domain:
            ranked = [item for item in ranked if TOOL_DOMAIN.get(item[0]) == domain]
        if effect_filter:
            ranked = [item for item in ranked if effect(item[0]) == effect_filter]
        write_intent = has_write_intent(query)
        if not write_intent:
            # Without a request to change something, reads come first; the sort is stable.
            ranked.sort(key=lambda item: effect(item[0]) == "write")
        top = ranked[:limit]
        result: dict[str, Any] = {
            "query": query,
            "write_intent": write_intent,
            "matches": [self._describe(name, with_schema=rank < SCHEMA_MATCHES)
                        for rank, (name, _) in enumerate(top)],
        }
        if len(top) >= 2 and top[1][1] >= top[0][1] * AMBIGUITY_RATIO:
            result["ambiguous"] = True
            result["hint"] = ("Top matches score closely; compare their summaries. Search an "
                              "operation's exact name to get its input_schema.")
        # "read-only" or "write permissions" say which effect is wanted, not what to do.
        words = query_words(_READ_ONLY_TALK.sub(" ", fold(query)))
        exact_name = bool(top) and fold(query).strip().replace(" ", "_") == top[0][0]
        missing: list[str] = []
        if not top:
            missing = [word for word, _ in words]
            result["exact_match"] = False
        elif exact_name:
            result["exact_match"] = True
        else:
            missing = self.index.uncovered(top[0][0], words)
            result["exact_match"] = self.index.coverage(top[0][0], words) >= MIN_COVERAGE
        shown = " ".join(missing[:MAX_MISSING_WORDS]) or query
        if top and missing:
            result["unmatched_terms"] = missing[:MAX_MISSING_WORDS]
        if write_intent and top and not any(effect(name) == "write" for name, _ in top):
            result["exact_match"] = False
            result["missing_capability"] = (
                "The request asks to change something, but no matching operation does; the "
                "matches only read. Tell the user this action is not available here."
            )
        elif not result["exact_match"]:
            result["missing_capability"] = (
                f"No operation covers '{shown}'. Any matches are only loosely related: tell the "
                "user this is not available rather than presenting them as the answer."
            )
        if not top:
            result["domains"] = self.domains()
            result["hint"] = "No match. Rephrase in English or Turkish, or browse a domain."
        return result

    def validation_error(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Return a structured error when the arguments do not fit the schema."""
        import jsonschema

        schema = self.operations[operation].get("input_schema") or {}
        validator_class = jsonschema.validators.validator_for(schema)
        error = jsonschema.exceptions.best_match(validator_class(schema).iter_errors(arguments))
        if error is not None:
            message, path = error.message, [str(part) for part in error.absolute_path]
        else:
            groups = REQUIRED_ONE_OF.get(operation, ())
            given = {key for key, value in arguments.items() if value not in (None, "")}
            if not groups or any(set(group) <= given for group in groups):
                return None
            message = "Pass one of: " + "; ".join(" and ".join(group) for group in groups) + "."
            path = []
        return {
            "error": "invalid_arguments",
            "operation": operation,
            "message": message,
            "path": path,
            "input_schema": compact_schema(schema),
        }

    def unknown_operation(self, operation: str) -> dict[str, Any]:
        if operation in self.operations:
            return {"error": "not_routed", "operation": operation,
                    "message": f"'{operation}' is published as its own tool; call it directly."}
        suggestions = [name for name, _ in self.index.score(operation)[:3]]
        return {"error": "unknown_operation", "operation": operation,
                "suggestions": suggestions,
                "message": "Use itu_find_tool to discover operation names."}

    async def call(self, operation: str, arguments: dict[str, Any] | None, context: Any = None) -> Any:
        operation = (operation or "").strip()
        arguments = copy.deepcopy(arguments or {})
        if operation not in self.routed_names:
            return self.unknown_operation(operation)
        problem = self.validation_error(operation, arguments)
        if problem is not None:
            return problem
        try:
            return await self.manager.call_tool(operation, arguments, context=context,
                                                convert_result=False)
        except Exception as error:  # noqa: BLE001 - reported to the model, not swallowed
            # Show the schema with the failure so a guessed argument can be fixed in
            # one retry. The message may quote ITU pages, so it is marked untrusted.
            return {
                "error": "operation_failed",
                "operation": operation,
                "message": str(error)[:MAX_ERROR_MESSAGE],
                "input_schema": compact_schema(self.operations[operation].get("input_schema") or {}),
                "hint": "If an argument caused this, correct it against input_schema and retry; "
                        "otherwise report the failure.",
                "untrusted_external_content": True,
            }


FIND_TOOL_DESCRIPTION = (
    "Find the ITU operation for a request and get its input schema. Use it when no published "
    "tool fits and the itu_call operation index does not make the choice and arguments clear. "
    "Pass `query` with the user's intent (Turkish or English, e.g. 'OBS official letter grades' "
    "or 'yemekhane menüsü'); optionally `domain` to narrow it and `effect` ('read' or 'write'). "
    "With only `domain`, lists that domain's operations. Each match carries its effect; reads "
    "rank above writes unless the request asks to change something. When exact_match is false, "
    "missing_capability says what no operation covers: tell the user instead of using a loosely "
    "related match. Then run the chosen operation with itu_call. Domains: "
    + ", ".join(DOMAINS)
    + "."
)


def call_tool_description(router: ToolRouter) -> str:
    """itu_call's description, ending with the routed operations' signatures."""
    return (
        "Run a read-only ITU operation. `operation` is the exact name, `arguments` an object "
        "matching its input schema. When an operation below clearly fits, call it directly; "
        "otherwise use itu_find_tool first. Invalid arguments return the full schema instead of "
        "running. This tool never changes data: writes, mail and file operations are separate "
        "tools. Operations by domain, `name(required, optional?)`:\n"
        + router.operation_index()
    )


ROUTER_INSTRUCTIONS = (
    "\n\nTool routing: most read-only ITU operations are reached through itu_call rather than "
    "listed individually. Call a published tool directly when one fits (for example "
    "get_dashboard, list_courses, get_course_overview, obs_get_schedule). Otherwise, if the "
    "operation index in itu_call's description clearly fits, call itu_call directly; if not, "
    "call itu_find_tool with the user's intent and pick from its summaries. Ninova grades are "
    "instructor-entered LMS grades; OBS grades are official. Do not answer from memory when an "
    "operation can fetch the data."
)
