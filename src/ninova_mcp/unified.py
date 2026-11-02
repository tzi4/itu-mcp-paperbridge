"""Bounded read-only search and reference opening across ITU services.

CourseHub searches page/link metadata without downloading linked documents.
Returned CourseHub references are short-lived, application-owned capabilities.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections import OrderedDict
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from .client import NinovaAuthError
from .mail_client import ItuMailAuthError
from .parsing import (
    TURKIYE_TZ, clean_text, extract_announcement_detail, extract_assignment_detail,
    ninova_datetime_iso,
)
from .tool_router import fold, tokenize
from . import course_hub as hub

SCOPES = ("mail", "ninova", "obs", "portal", "coursehub")
MAX_QUERY_CHARS = 200
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
SOURCE_TIMEOUT_SECONDS = 25.0
MAIL_BODY_TIMEOUT_SECONDS = 90.0
MAIL_RESULTS_PER_MAILBOX = 25
SNIPPET_CHARS = 240
MAX_REF_CHARS = 120

# A deployment can hide individual tools (remote mail switch, file tools,
# NINOVA_REMOTE_ALLOWED_TOOLS). Cross-service tools never reach data that the
# matching single-service tool would not expose.
SCOPE_OPERATIONS = {
    "mail": ("mail_search",),
    "ninova": ("get_dashboard_announcements", "get_dashboard_assignments"),
    "obs": ("obs_get_schedule", "obs_list_semesters"),
    "portal": ("obs_get_notifications", "obs_get_help_tickets"),
    "coursehub": ("coursehub_materials",),
}
OPEN_OPERATIONS = {
    ("mail", "email"): "mail_get_message",
    ("ninova", "announcement"): "get_course_announcements",
    ("ninova", "assignment"): "get_course_assignments",
    ("ninova", "file"): "read_resource_text",
    ("obs", "course"): "obs_get_schedule",
    ("obs", "exam"): "obs_get_schedule",
    ("portal", "help_ticket"): "get_help_ticket",
    ("portal", "notification"): "obs_get_notifications",
}
SOURCE_ORDER = {name: index for index, name in enumerate(SCOPES)}

_COURSE_ID = r"[0-9]{1,10}\.[0-9]{1,10}"
_REF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mail", re.compile(r"itu://mail/(inbox|sent)/([1-9][0-9]{0,19})")),
    ("ninova", re.compile(rf"itu://ninova/({_COURSE_ID})/(announcement|assignment)/([0-9]{{1,12}})")),
    ("ninova_file", re.compile(rf"itu://ninova/({_COURSE_ID})/file/(class|lesson)/([0-9]{{1,12}})")),
    ("obs", re.compile(r"itu://obs/(course|exam)/([0-9]{1,8})/([0-9]{4,6})")),
    ("portal", re.compile(r"itu://portal/(help|notification)/([A-Za-z0-9_-]{1,64})")),
)
_NINOVA_ITEM_PATH = re.compile(rf"/Sinif/({_COURSE_ID})/(Duyuru|Odev)/([0-9]{{1,12}})/?")
_NINOVA_FILE_PATH = re.compile(rf"/Sinif/({_COURSE_ID})/(SinifDosyalari|DersDosyalari)/?")
_NINOVA_FILE_QUERY = re.compile(r"g=?([0-9]{1,12})")
_NINOVA_COURSE_PATH = re.compile(rf"/Sinif/({_COURSE_ID})(?:/.*)?")
_NINOVA_KINDS = {"Duyuru": "announcement", "Odev": "assignment"}
_NINOVA_SEGMENTS = {value: key for key, value in _NINOVA_KINDS.items()}
_FILE_SECTIONS = {"SinifDosyalari": "class", "DersDosyalari": "lesson"}
_FILE_SEGMENTS = {value: key for key, value in _FILE_SECTIONS.items()}
_DOTNET_DATE = re.compile(r"/Date\((-?[0-9]{1,15})(?:[+-][0-9]{4})?\)/")
_LOCAL_DATE_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y")
_TOKEN = re.compile(r"[a-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# OBS meeting day codes: Monday to Friday as M T W R F, weekends as S and U.
DAY_CODES = {"M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

MAX_GLOBAL_PENDING = 12
MAX_APP_PENDING = 5
MAX_WORKERS = 6
MAX_STORED_REFS = 256
REF_TTL_SECONDS = 600.0
MAX_COURSEHUB_MATERIALS = 100
_STATE_LOCK = threading.Lock()
_PENDING = threading.BoundedSemaphore(MAX_GLOBAL_PENDING)
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="itu-unified")
_LOCAL = threading.local()


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sso = threading.Lock()
        self.pending = set()
        self.refs = OrderedDict()


def _state(app):
    with _STATE_LOCK:
        state = getattr(app, "_itu_unified_state", None)
        if state is None:
            state = _State()
            setattr(app, "_itu_unified_state", state)
        return state



class UnifiedError(ValueError):
    """Invalid input to a cross-service tool; reported before any network call."""


@dataclass(frozen=True)
class Ref:
    source: str
    kind: str
    parts: tuple[str, ...]
    text: str


# ---------------------------------------------------------------------------
# References


def parse_ref(value: Any) -> Ref:
    """Validate an ``itu://`` reference; anything else, including URLs, is refused."""
    text = value if isinstance(value, str) else ""
    if not text or len(text) > MAX_REF_CHARS:
        raise UnifiedError("ref must be an itu:// reference returned by itu_search.")
    if re.fullmatch(r"itu://coursehub/[A-Za-z0-9_-]{32}", text):
        return Ref("coursehub", "material", (text.rsplit("/", 1)[1],), text)
    for family, pattern in _REF_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        groups = match.groups()
        if family == "mail":
            return Ref("mail", "email", (groups[0], groups[1]), text)
        if family == "ninova":
            return Ref("ninova", groups[1], (groups[0], groups[2]), text)
        if family == "ninova_file":
            return Ref("ninova", "file", (groups[0], groups[1], groups[2]), text)
        if family == "obs":
            return Ref("obs", groups[0], (groups[1], groups[2]), text)
        kind = "help_ticket" if groups[0] == "help" else "notification"
        if kind == "help_ticket" and (not groups[1].isdigit() or len(groups[1]) > 20):
            break
        return Ref("portal", kind, (groups[1],), text)
    raise UnifiedError(
        "Unknown or malformed reference. Use a ref exactly as itu_search returned it, "
        "for example itu://mail/inbox/123 or itu://ninova/12345.678901/announcement/42."
    )


def mail_ref(mailbox: str, uid: Any) -> str | None:
    uid = str(uid or "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
        return None
    return f"itu://mail/{'sent' if str(mailbox).upper() == 'SENT' else 'inbox'}/{uid}"


def _ninova_path(url: Any, base_url: str) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() != urlparse(base_url).netloc.lower():
        return None
    return parsed.path, parsed.query


def ninova_ref(url: Any, base_url: str) -> str | None:
    """Reference for a Ninova announcement, assignment or file URL on the configured host."""
    located = _ninova_path(url, base_url)
    if located is None:
        return None
    path, query = located
    match = _NINOVA_ITEM_PATH.fullmatch(path)
    if match:
        return f"itu://ninova/{match.group(1)}/{_NINOVA_KINDS[match.group(2)]}/{match.group(3)}"
    match = _NINOVA_FILE_PATH.fullmatch(path)
    file_id = _NINOVA_FILE_QUERY.fullmatch(query)
    if match and file_id:
        return f"itu://ninova/{match.group(1)}/file/{_FILE_SECTIONS[match.group(2)]}/{file_id.group(1)}"
    return None


def ninova_course_id(url: Any, base_url: str) -> str | None:
    located = _ninova_path(url, base_url)
    if located is None:
        return None
    match = _NINOVA_COURSE_PATH.fullmatch(located[0])
    return match.group(1) if match else None


def ninova_url(ref: Ref, base_url: str) -> str:
    """The Ninova URL for a validated reference, built only from its digits."""
    base = base_url.rstrip("/")
    if ref.kind == "file":
        course_id, section, file_id = ref.parts
        return f"{base}/Sinif/{course_id}/{_FILE_SEGMENTS[section]}?g{file_id}"
    course_id, item_id = ref.parts
    return f"{base}/Sinif/{course_id}/{_NINOVA_SEGMENTS[ref.kind]}/{item_id}"


def obs_ref(kind: str, semester_id: Any, crn: Any) -> str | None:
    semester_id, crn = str(semester_id or ""), str(crn or "")
    if not re.fullmatch(r"[0-9]{1,8}", semester_id) or not re.fullmatch(r"[0-9]{4,6}", crn):
        return None
    return f"itu://obs/{kind}/{semester_id}/{crn}"


def portal_ref(kind: str, identifier: Any) -> str | None:
    identifier = str(identifier or "").strip()
    pattern = r"[0-9]{1,20}" if kind == "help" else r"[A-Za-z0-9_-]{1,64}"
    return f"itu://portal/{kind}/{identifier}" if re.fullmatch(pattern, identifier) else None


# ---------------------------------------------------------------------------
# Time


def now_local() -> datetime:
    return datetime.now(TURKIYE_TZ)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse the date formats the services use. Naive times are İTÜ local time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        dotnet = _DOTNET_DATE.fullmatch(text)
        if dotnet:
            try:
                return datetime.fromtimestamp(int(dotnet.group(1)) / 1000, TURKIYE_TZ)
            except (OverflowError, OSError, ValueError):
                return None
        parsed = None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _LOCAL_DATE_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            iso = ninova_datetime_iso(text)
            parsed = datetime.fromisoformat(iso) if iso else None
        if parsed is None:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TURKIYE_TZ)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def parse_bound(value: str | None, name: str) -> datetime | None:
    """A ``since``/``before`` argument: YYYY-MM-DD or an ISO 8601 date and time."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+\-Z]{2,32})?", text):
        raise UnifiedError(f"{name} must be a date such as 2026-10-01 or 2026-10-01T09:00.")
    parsed = parse_timestamp(text)
    if parsed is None:
        raise UnifiedError(f"{name} must be a date such as 2026-10-01 or 2026-10-01T09:00.")
    return parsed


# ---------------------------------------------------------------------------
# Matching and ranking


def _norm(text: Any) -> str:
    return fold(clean_text(str(text))) if text else ""


class Matcher:
    """Every query word must match; title beats course/person, which beats body.

    Words are folded to ASCII (``ödevi`` meets ``Ödev``) and also matched by
    their stem, so Turkish suffixes do not hide a match.
    """

    TITLE, META, BODY = 3.0, 2.0, 1.0
    STEM_WEIGHT = 0.8
    PHRASE_BONUS = 1.0

    def __init__(self, query: str) -> None:
        self.groups: list[tuple[str, ...]] = []
        for token in _TOKEN.findall(fold(query)):
            variants = tuple(dict.fromkeys(tokenize(token)))
            if variants:
                self.groups.append(variants)
        words = _TOKEN.findall(fold(query))
        self.phrase = " ".join(words) if len(words) > 1 else ""

    def score(self, title: str, meta: str = "", body: str = "") -> float | None:
        fields = []
        for weight, text in ((self.TITLE, title), (self.META, meta), (self.BODY, body)):
            folded = _norm(text)
            fields.append((weight, folded, _NON_ALNUM.sub("", folded)))
        total = 0.0
        for variants in self.groups:
            best = 0.0
            for index, variant in enumerate(variants):
                factor = 1.0 if index == 0 else self.STEM_WEIGHT
                for weight, folded, compact in fields:
                    if folded and (variant in folded or variant in compact):
                        best = max(best, weight * factor)
                        break
            if not best:
                return None
            total += best
        result = total / len(self.groups)
        if self.phrase and self.phrase in fields[0][1]:
            result += self.PHRASE_BONUS
        return result


def recency(timestamp: datetime | None, now: datetime) -> float:
    """1.0 for now, falling towards 0 over weeks; past and future alike."""
    if timestamp is None:
        return 0.0
    days = abs((now - timestamp).total_seconds()) / 86400
    return 1.0 / (1.0 + days / 14.0)


def make_hit(*, ref: str, source: str, kind: str, title: Any, snippet: Any = None,
             timestamp: datetime | None = None, course: Any = None, person: Any = None,
             unread: bool | None = None, url: Any = None) -> dict[str, Any]:
    snippet_text = clean_text(str(snippet)) if snippet else None
    if snippet_text and len(snippet_text) > SNIPPET_CHARS:
        snippet_text = snippet_text[: SNIPPET_CHARS - 1].rstrip() + "…"
    return {
        "ref": ref, "source": source, "kind": kind,
        "title": clean_text(str(title)) if title else None,
        "snippet": snippet_text or None,
        "timestamp": iso(timestamp),
        "course": clean_text(str(course)) if course else None,
        "person": clean_text(str(person)) if person else None,
        "unread": unread,
        "url": url if isinstance(url, str) and url.startswith("https://") else None,
        "score": 0.0,
    }


@dataclass
class Candidate:
    hit: dict[str, Any]
    timestamp: datetime | None
    title: str
    meta: str
    body: str
    server_matched: bool = False


def rank(candidates: list[Candidate], matcher: Matcher, *, include_body: bool, now: datetime,
         since: datetime | None, before: datetime | None) -> list[dict[str, Any]]:
    """Filter and order hits; same input and clock, same order."""
    ranked: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        stamp = candidate.timestamp
        if (since or before) and stamp is None:
            continue
        if (since and stamp < since) or (before and stamp >= before):
            continue
        match = matcher.score(candidate.title, candidate.meta,
                              candidate.body if include_body else "")
        if match is None:
            if not candidate.server_matched:
                continue
            # The mail server matched a field we do not hold locally (an address).
            match = 0.5
        hit = dict(candidate.hit)
        hit["score"] = round(match + 0.5 * recency(stamp, now), 4)
        previous = ranked.get(hit["ref"])
        if previous is None or hit["score"] > previous["score"]:
            ranked[hit["ref"]] = hit
    return sorted(ranked.values(),
                  key=lambda item: (-item["score"], SOURCE_ORDER.get(item["source"], 9), item["ref"]))


# ---------------------------------------------------------------------------
# Execution


def operation_allowed(app: Any, name: str) -> bool:
    published = getattr(app, "published_tool_names", None)
    enabled = getattr(app, "enabled_operations", None)
    return (published is None or name in published) and (enabled is None or name in enabled)


def scope_enabled(app: Any, scope: str) -> bool:
    return all(operation_allowed(app, name) for name in SCOPE_OPERATIONS[scope])


def ensure_sso(app: Any) -> None:
    app.client.ensure_logged_in()


def _sso_read(app, read):
    # Hold the lane throughout the read: OBS/Portal can also refresh shared SSO.
    lane = _state(app).sso
    deadline = getattr(_LOCAL, "deadline", time.monotonic() + SOURCE_TIMEOUT_SECONDS)
    if not lane.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError("SSO reader busy")
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("Read deadline elapsed")
        return read()
    finally:
        lane.release()


def error_message(exc: BaseException) -> str:
    # Upstream exception messages may contain passwords, URLs or session tokens.
    if isinstance(exc, (NinovaAuthError, ItuMailAuthError)):
        return "Authentication failed for this source."
    if isinstance(exc, TimeoutError):
        return "The source did not answer within the read deadline."
    return "The source could not complete this read."


def error_code(exc: BaseException) -> str:
    if isinstance(exc, (NinovaAuthError, ItuMailAuthError)):
        return "auth"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, UnifiedError):
        return "invalid_input"
    return "upstream_error"


def run_concurrently(tasks: dict[str, tuple[Callable[[], Any], float]], *, app=None) -> dict[str, dict[str, Any]]:
    """Bound waiting and outstanding work, including threads surviving a timeout."""
    if not tasks:
        return {}
    state = _state(app) if app is not None else _State()
    started = time.monotonic()
    outcomes, futures = {}, {}
    for name, (fn, timeout) in tasks.items():
        with state.lock:
            admitted = len(state.pending) < MAX_APP_PENDING and name not in state.pending
            if admitted:
                admitted = _PENDING.acquire(blocking=False)
            if admitted:
                state.pending.add(name)
        if not admitted:
            outcomes[name] = {"ok": False, "code": "busy", "error": "A previous read is still running.", "elapsed_ms": 0}
            continue
        def release(_future, name=name):
            with state.lock:
                state.pending.discard(name)
            _PENDING.release()
        def execute(fn=fn, timeout=timeout):
            _LOCAL.deadline = started + timeout
            try:
                if time.monotonic() >= _LOCAL.deadline:
                    raise TimeoutError("Read deadline elapsed")
                value = fn()
                if time.monotonic() > _LOCAL.deadline:
                    raise TimeoutError("Read completed after its deadline")
                return value
            finally:
                del _LOCAL.deadline
        try:
            future = _EXECUTOR.submit(execute)
        except Exception:
            release(None)
            raise
        future.add_done_callback(release)
        futures[name] = future
    for name, future in futures.items():
        remaining = max(0.0, started + tasks[name][1] - time.monotonic())
        try:
            outcomes[name] = {"ok": True, "value": future.result(timeout=remaining)}
        except FutureTimeout:
            future.cancel()
            outcomes[name] = {"ok": False, "code": "timeout", "error": "The source did not answer within the read deadline."}
        except Exception as exc:  # noqa: BLE001 - safe per-source failure
            outcomes[name] = {"ok": False, "code": error_code(exc), "error": error_message(exc)}
        outcomes[name]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return outcomes


def _report(status: str, *, count: int = 0, warnings: list[str] | None = None,
            **details: Any) -> dict[str, Any]:
    return {"status": status, "count": count, "warnings": list(warnings or []), **details}


def _failed(outcome: dict[str, Any]) -> dict[str, Any]:
    return _report("error", warnings=[outcome["error"]], error_code=outcome["code"],
                   elapsed_ms=outcome.get("elapsed_ms"))


def _disabled(scope: str) -> dict[str, Any]:
    return _report("disabled", warnings=[f"{scope} is not enabled on this server."])


# ---------------------------------------------------------------------------
# Source readers


def _course_label(course: dict[str, Any] | None) -> str | None:
    if not course:
        return None
    text = " ".join(part for part in (course.get("code"), course.get("title")) if part)
    return text or course.get("context") or None


def tracked_ninova(app: Any) -> dict[str, Any]:
    """The tracking state saved by sync_all_courses, read without changing it."""
    return app._load_tracking_state_document()


def _tracked_items(state: dict[str, Any], base_url: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(kind, item, course) for each tracked announcement, assignment and file."""
    items = []
    for entry in (state.get("courses") or {}).values():
        course = entry.get("course") or {}
        overview = (entry.get("snapshot") or {}).get("overview") or {}
        for kind, key in (("announcement", "announcements"), ("assignment", "assignments")):
            for item in overview.get(key) or []:
                items.append((kind, item, course))
        for key in ("class_files", "lesson_files"):
            for item in overview.get(key) or []:
                if item.get("entry_type") == "file":
                    items.append(("file", item, course))
    return items


def _ninova_candidate(kind: str, item: dict[str, Any], course: dict[str, Any] | None,
                      base_url: str) -> Candidate | None:
    ref = ninova_ref(item.get("url"), base_url)
    if ref is None:
        return None
    label = _course_label(course) or item.get("course_title") or item.get("class_name")
    if kind == "announcement":
        stamp = parse_timestamp(item.get("published_at_iso") or item.get("published_at"))
        title, body, person = item.get("title"), item.get("summary") or item.get("body_text"), item.get("author")
    elif kind == "assignment":
        stamp = parse_timestamp(item.get("submission_end_iso") or item.get("submission_end"))
        title, body, person = item.get("title"), item.get("description"), None
    else:
        stamp = parse_timestamp(item.get("date"))
        title, body, person = item.get("name"), item.get("path"), None
    hit = make_hit(ref=ref, source="ninova", kind=kind, title=title, snippet=body, timestamp=stamp,
                   course=label, person=person, url=item.get("url"))
    return Candidate(hit, stamp, title or "", f"{label or ''} {person or ''}", body or "")


def read_ninova(app: Any) -> dict[str, Any]:
    """Live dashboard announcements and open assignments plus tracked items."""
    ensure_sso(app)
    base_url = app.client.base_url
    warnings: list[str] = []
    live: dict[str, list[tuple[str, dict[str, Any], None]]] = {}
    failed: set[str] = set()
    for kind, read in (("announcement", lambda: app.get_dashboard_announcements(limit=200)),
                       ("assignment", lambda: app.get_dashboard_assignments(limit=200, include_details=False))):
        try:
            payload = read()
        except Exception as exc:  # noqa: BLE001 - one list failing leaves the other
            failed.add(kind)
            warnings.append(f"Ninova dashboard {kind}s could not be read: {error_message(exc)}")
            continue
        if payload.get("parse_warning"):
            warnings.append("Ninova reported incomplete parsing of this dashboard listing.")
        key = "announcements" if kind == "announcement" else "assignments"
        live[kind] = [(kind, item, None) for item in payload.get(key) or []]
    state = tracked_ninova(app)
    return {
        "base_url": base_url,
        "live": [entry for entries in live.values() for entry in entries],
        "tracked": [entry for entry in _tracked_items(state, base_url)
                    if operation_allowed(app, OPEN_OPERATIONS[("ninova", entry[0])])],
        "state": state,
        "live_failed": failed,
        "warnings": warnings,
        "tracking_warning": None if state.get("courses") else (
            "No tracked Ninova data yet: files, older announcements and change history are not "
            "covered. Run sync_all_courses once to include them."),
    }


def _ninova_status(data: dict[str, Any], count: int, *, kinds: tuple[str, ...] = ("announcement", "assignment"),
                   needs_tracking: bool = True) -> str:
    """error when nothing was read, partial when a list or the tracked data is missing."""
    failed = data["live_failed"] & set(kinds)
    if len(failed) == len(kinds) and not data["tracked"]:
        return "error"
    if failed or (needs_tracking and data["tracking_warning"]):
        return "partial"
    return "ok" if count else "empty"


def _ninova_warnings(data: dict[str, Any], *, needs_tracking: bool = True) -> list[str]:
    extra = [data["tracking_warning"]] if needs_tracking and data["tracking_warning"] else []
    return list(data["warnings"]) + extra


def _ninova_details(data: dict[str, Any]) -> dict[str, Any]:
    state = data["state"]
    return {"tracked_last_sync_at": state.get("last_sync_at"),
            "tracked_course_count": len(state.get("courses") or {})}


def _session_summary(row: dict[str, Any]) -> str:
    parts = []
    for session in row.get("sinifYerZaman") or []:
        if not isinstance(session, dict):
            continue
        day = DAY_CODES.get(str(session.get("gunKodu") or ""))
        start, end = _clock(session.get("baslangicSaati")), _clock(session.get("bitisSaati"))
        place = " ".join(str(session.get(key)) for key in ("binaKodu", "mekanAdi") if session.get(key))
        text = f"{DAY_NAMES[day] if day is not None else '?'} {start or '?'}-{end or '?'}"
        parts.append(f"{text} {place}".strip())
    return "; ".join(parts)


def _clock(value: Any) -> str | None:
    # OBS meeting times are minutes after midnight.
    if type(value) is not int or not 0 <= value < 1440:
        return None
    return f"{value // 60:02d}:{value % 60:02d}"


def _obs_code(row: dict[str, Any]) -> str:
    code = str(row.get("dersKodu") or "").strip()
    branch = str(row.get("bransKodu") or "").strip()
    if branch and not code.upper().startswith(branch.upper()):
        code = f"{branch} {code}".strip()
    return code


def _obs_name(row: dict[str, Any]) -> str:
    return str(row.get("dersAdiTR") or row.get("dersAdiEN") or "").strip()


def read_obs(app: Any) -> dict[str, Any]:
    ensure_sso(app)
    payload = app.obs_get_schedule()
    semester = payload.get("semester") or {}
    schedule = payload.get("schedule") or {}
    finals = payload.get("final_calendar") or {}
    return {
        "semester": semester,
        "semester_id": semester.get("akademikDonemId"),
        "rows": [row for row in schedule.get("kayitSinifResultList") or [] if isinstance(row, dict)],
        "exams": [row for row in finals.get("finalBilgiList") or [] if isinstance(row, dict)],
    }


def _exam_place(exam: dict[str, Any]) -> str:
    places = []
    for place in exam.get("finalMekanList") or []:
        if isinstance(place, dict):
            places.append(" ".join(str(place.get(key)) for key in ("binaAdi", "derslikKodu") if place.get(key)))
    return ", ".join(item for item in places if item)


def _obs_candidates(data: dict[str, Any]) -> list[Candidate]:
    candidates = []
    semester_id = data["semester_id"]
    for row in data["rows"]:
        ref = obs_ref("course", semester_id, row.get("crn"))
        if ref is None:
            continue
        code, name = _obs_code(row), _obs_name(row)
        sessions = _session_summary(row)
        hit = make_hit(ref=ref, source="obs", kind="course", title=f"{code} {name}".strip(),
                       snippet=f"CRN {row.get('crn')}. {sessions}".strip(), course=code)
        candidates.append(Candidate(hit, None, f"{code} {name} {row.get('dersAdiEN') or ''}",
                                    f"{row.get('crn')}", sessions))
    for exam in data["exams"]:
        ref = obs_ref("exam", semester_id, exam.get("crn"))
        if ref is None:
            continue
        code, name = _obs_code(exam), _obs_name(exam)
        start = parse_timestamp(exam.get("baslangicTarihi"))
        place = _exam_place(exam)
        hit = make_hit(ref=ref, source="obs", kind="exam", title=f"Final exam: {code} {name}".strip(),
                       snippet=place or None, timestamp=start, course=code)
        candidates.append(Candidate(hit, start, f"{code} {name} {exam.get('dersAdiEN') or ''} final sinav exam",
                                    f"{exam.get('crn')}", place))
    return candidates


def read_portal(app: Any, *, ticket_details: bool, ticket_limit: int) -> dict[str, Any]:
    ensure_sso(app)
    warnings: list[str] = []
    notifications = tickets = None
    try:
        notifications = app.obs_get_notifications(limit=100)
    except Exception as exc:  # noqa: BLE001 - one widget failing leaves the other
        warnings.append(f"Portal notifications could not be read: {error_message(exc)}")
    try:
        tickets = app.obs_get_help_tickets(limit=ticket_limit, include_details=ticket_details)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Help tickets could not be read: {error_message(exc)}")
    if notifications and notifications.get("api_fallback"):
        warnings.append("Portal notifications came from the page widget: only titles and relative ages.")
    return {
        "notifications": (notifications or {}).get("notifications") or [],
        "tickets": (tickets or {}).get("tickets") or [],
        "failures": (notifications is None) + (tickets is None),
        "warnings": warnings,
    }


def _notification_id(item: dict[str, Any]) -> Any:
    return item.get("id") or item.get("notification_id")


def _portal_candidates(data: dict[str, Any]) -> tuple[list[Candidate], int]:
    candidates, skipped = [], 0
    for item in data["notifications"]:
        ref = portal_ref("notification", _notification_id(item))
        if ref is None:
            skipped += 1
            continue
        stamp = parse_timestamp(item.get("created_at"))
        hit = make_hit(ref=ref, source="portal", kind="notification", title=item.get("title"),
                       snippet=item.get("content") or item.get("time_ago") or item.get("age"),
                       timestamp=stamp, unread=item.get("unread"))
        candidates.append(Candidate(hit, stamp, item.get("title") or "", "", item.get("content") or ""))
    for item in data["tickets"]:
        ref = portal_ref("help", item.get("id"))
        if ref is None:
            skipped += 1
            continue
        stamp = parse_timestamp(item.get("updated_at") or item.get("created_at"))
        status = item.get("status")
        snippet = " · ".join(str(part) for part in (status and f"Status: {status}", item.get("unit"),
                                                     item.get("category")) if part)
        hit = make_hit(ref=ref, source="portal", kind="help_ticket", title=item.get("title"),
                       snippet=snippet, timestamp=stamp, url=item.get("url"))
        candidates.append(Candidate(hit, stamp, item.get("title") or "",
                                    f"{item.get('id')} {item.get('unit') or ''} {item.get('category') or ''}",
                                    str(status or "")))
    return candidates, skipped


def _portal_status(data: dict[str, Any], count: int) -> str:
    if data["failures"] == 2:
        return "error"
    if data["failures"]:
        return "partial"
    return "ok" if count else "empty"


# ---------------------------------------------------------------------------
# itu_search


def _cursor_key(query: str, scopes: list[str], since: datetime | None, before: datetime | None,
                include_body: bool, dataset: Any = None) -> str:
    material = json.dumps([query, scopes, iso(since), iso(before), include_body, dataset], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def encode_cursor(offset: int, key: str) -> str:
    raw = json.dumps({"o": offset, "k": key}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, key: str) -> int:
    if cursor is None:
        return 0
    text = cursor if isinstance(cursor, str) else ""
    if len(text) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise UnifiedError("cursor is not a next_cursor value from itu_search.")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, TypeError):
        raise UnifiedError("cursor is not a next_cursor value from itu_search.") from None
    offset = data.get("o") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("k") != key or type(offset) is not int or not 0 <= offset <= 10_000:
        raise UnifiedError("cursor belongs to a different query; repeat the search without it.")
    return offset


def _normalize_scopes(scopes: Any) -> list[str]:
    if scopes is None:
        return list(SCOPES)
    if isinstance(scopes, str):
        scopes = [scopes]
    chosen = []
    if not isinstance(scopes, (list, tuple)):
        raise UnifiedError("scopes must be a list of source names.")
    for scope in scopes:
        value = str(scope).strip().lower()
        if value not in SCOPES:
            raise UnifiedError(f"Unknown scope. Use any of: {', '.join(SCOPES)}.")
        if value not in chosen:
            chosen.append(value)
    if not chosen:
        raise UnifiedError(f"Give at least one scope: {', '.join(SCOPES)}.")
    return [scope for scope in SCOPES if scope in chosen]


def _search_mail(app: Any, query: str, since: datetime | None, before: datetime | None,
                 include_body: bool) -> dict[str, Any]:
    since_day = since.astimezone(TURKIYE_TZ).date() if since else None
    before_day = None
    if before:
        local = before.astimezone(TURKIYE_TZ)
        before_day = local.date() + (timedelta(days=1) if local.time() != datetime.min.time() else timedelta())
    result = app.mail_search(query=query, mailbox="ALL",
                             since=since_day.isoformat() if since_day else None,
                             before=before_day.isoformat() if before_day else None,
                             in_body=include_body, limit=MAIL_RESULTS_PER_MAILBOX)
    candidates = []
    for message in result.get("messages") or []:
        ref = mail_ref(message.get("mailbox"), message.get("uid"))
        if ref is None:
            continue
        sent = str(message.get("mailbox")).upper() == "SENT"
        stamp = parse_timestamp(message.get("date_iso"))
        person = message.get("to") if sent else message.get("from")
        hit = make_hit(ref=ref, source="mail", kind="email", title=message.get("subject") or "(no subject)",
                       snippet=f"To: {message.get('to')}" if sent else None, timestamp=stamp,
                       person=person, unread=None if sent else message.get("unread"))
        meta = " ".join(str(message.get(key) or "") for key in ("from", "to", "cc"))
        candidates.append(Candidate(hit, stamp, message.get("subject") or "", meta, "", server_matched=True))
    matched = result.get("matched_by_mailbox") or {}
    truncated = any(count > MAIL_RESULTS_PER_MAILBOX for count in matched.values())
    warnings = []
    if truncated:
        warnings.append(f"Mail matched {result.get('matched_count')} messages; only the newest "
                        f"{MAIL_RESULTS_PER_MAILBOX} per mailbox are ranked. Narrow with since/before.")
    return {"candidates": candidates, "warnings": warnings,
            "details": {"matched_count": result.get("matched_count"), "truncated": truncated,
                        "searched": result.get("searched")}}


def _search_ninova(app: Any) -> dict[str, Any]:
    data = read_ninova(app)
    base_url = data["base_url"]
    candidates = []
    # Live entries first so their fresher fields win the duplicate check.
    for kind, item, course in data["live"] + data["tracked"]:
        candidate = _ninova_candidate(kind, item, course, base_url)
        if candidate is not None:
            candidates.append(candidate)
    return {"candidates": candidates, "data": data, "warnings": _ninova_warnings(data),
            "details": _ninova_details(data)}


def _search_obs(app: Any) -> dict[str, Any]:
    data = read_obs(app)
    return {"candidates": _obs_candidates(data), "warnings": [],
            "details": {"semester": data["semester"].get("akademikDonemAdiEN")
                        or data["semester"].get("akademikDonemAdi")},
            "note": "OBS covers the current term's registered courses, meeting times and final exams."}


def _search_portal(app: Any) -> dict[str, Any]:
    data = read_portal(app, ticket_details=False, ticket_limit=100)
    candidates, skipped = _portal_candidates(data)
    warnings = list(data["warnings"])
    if skipped:
        warnings.append(f"{skipped} Portal items had no usable ID and were left out.")
    return {"candidates": candidates, "data": data, "warnings": warnings, "details": {}}


# CourseHub refs never encode a URL or permit arbitrary caller-selected targets.
@dataclass(frozen=True)
class _MaterialTarget:
    site: str
    authenticated: bool
    reader: str
    url: str


def _material_target(site: str, authenticated: bool, item: dict) -> _MaterialTarget:
    url = item.get("url")
    if item.get("kind") == "document":
        reader = "document" if item.get("text_extraction_supported") is not False else "unsupported"
        hub._checked_source_document(url, site)
    else:
        reader = "page"
        hub._checked_source_page(url, site)
    return _MaterialTarget(site, authenticated, reader, url)


def _remember(app, target):
    state = _state(app)
    now = time.monotonic()
    with state.lock:
        for key, (expires, _) in list(state.refs.items()):
            if expires <= now:
                del state.refs[key]
        while len(state.refs) >= MAX_STORED_REFS:
            state.refs.popitem(last=False)
        token = secrets.token_urlsafe(24)
        state.refs[token] = (now + REF_TTL_SECONDS, target)
    return "itu://coursehub/" + token


def _lookup(app, token):
    state = _state(app)
    with state.lock:
        stored = state.refs.get(token)
        if stored is None or stored[0] <= time.monotonic():
            state.refs.pop(token, None)
            raise NotFound("The CourseHub reference is unknown or expired; repeat the search.")
        return stored[1]


def _coursehub_filters(sites, course_code, authenticated):
    if type(authenticated) is not bool:
        raise UnifiedError("coursehub_authenticated must be a boolean.")
    if course_code is not None and (not isinstance(course_code, str) or not course_code.strip() or len(course_code) > 30):
        raise UnifiedError("coursehub_course_code must be a supported course code.")
    if sites is not None and (not isinstance(sites, (list, tuple)) or not sites or len(sites) > len(hub.SITES)):
        raise UnifiedError("coursehub_sites must be a nonempty list of supported sites.")
    explicit = sites is not None
    try:
        selected = list(dict.fromkeys(hub.site_key(value) for value in sites)) if explicit else list(hub.SITES)
    except (hub.CourseHubError, ValueError, TypeError, AttributeError):
        raise UnifiedError("coursehub_sites contains an unsupported site.") from None
    if authenticated:
        if explicit and any(site not in hub._ACCOUNT_SITES for site in selected):
            raise UnifiedError("Authenticated CourseHub reads are supported only by MAT, FIZ and KIM.")
        selected = [site for site in selected if site in hub._ACCOUNT_SITES]
    canonical = None
    if course_code is not None:
        compatible = []
        for site in selected:
            try:
                canonical = hub._requested_material_course(site, course_code)
                compatible.append(site)
            except hub.CourseHubError:
                if explicit:
                    raise UnifiedError("The course code does not belong to a selected CourseHub site.") from None
        selected = compatible
        if not selected:
            raise UnifiedError("No selected CourseHub site supports this course code.")
    return selected, canonical


_COLLECTION_TERMS = {
    "slides": "slides slide slayt sunu presentation", "practice": "practice worksheet çalışma föy alıştırma",
    "exams": "exams exam sınav", "syllabi": "syllabi syllabus izlence katalog catalog catalogue",
    "guides": "guides guide safety güvenlik handbook booklet kitapçık form",
}


def _search_coursehub(app, sites, course_code, authenticated):
    candidates, reports, warnings = [], {}, []
    warnings.append("CourseHub searches bounded page/link metadata, not document contents or a complete site archive.")
    for site in sites:
        if time.monotonic() >= getattr(_LOCAL, "deadline", float("inf")):
            warnings.append("The CourseHub deadline left some selected sites unread.")
            break
        try:
            payload = app.coursehub_materials(site=site, course_code=course_code, authenticated=authenticated,
                                               collection="all", offset=0, limit=MAX_COURSEHUB_MATERIALS)
            reports[site] = {key: payload.get(key) for key in (
                "coverage", "source_url", "source_urls", "pages_read", "pages_attempted", "page_limit",
                "total_count", "count", "has_more", "remaining_seeds", "omitted")}
            reports[site]["status"] = "partial"
            reports[site]["error_count"] = len(payload.get("errors") or [])
            suppressed = 0
            items = list((payload.get("materials") or [])[:MAX_COURSEHUB_MATERIALS])
            # Indexes have already read these pages; expose their observed metadata
            # without adding another request or claiming their document bodies match.
            observed_pages = payload.get("sources") or []
            entry_points = {entry.get("url"): entry for entry in payload.get("entry_points") or []}
            for source in observed_pages[:hub.MAX_SEED_PAGES]:
                entry = entry_points.get(source.get("entry_point") or source.get("source_url"), {})
                items.append({"url": source.get("source_url"), "title": source.get("source_title"),
                              "kind": "page", "course_codes": entry.get("course_codes") or [],
                              "course_match": hub._course_match(entry.get("course_codes") or [], course_code, shared=True),
                              "source_title": source.get("source_title"),
                              "collection": None,
                              "_search_terms": ("calendar takvim experiments deney laboratory laboratuvar"
                                  if site == "chemistry_lab" else "preparatory preparation prep hazırlık guidance rehber"
                                  if "/lisans-hazirlik-programi/" in str(source.get("source_url")) else "")})
            for item in items:
                try:
                    target = _material_target(site, authenticated, item)
                except (hub.CourseHubError, ValueError, TypeError, AttributeError):
                    suppressed += 1
                    continue
                identifier = hashlib.sha256(repr(target).encode()).hexdigest()
                title = str(item.get("title") or item.get("name") or "CourseHub material")
                codes = item.get("course_codes") or []
                if not isinstance(codes, list):
                    codes = []
                meta = " ".join(str(part) for part in (
                    site, course_code or "", " ".join(str(code) for code in codes), target.url,
                    item.get("source_title") or "", " ".join(item.get("titles") or []),
                    " ".join(str(observation.get("source_title") or "") for observation in item.get("provenance") or []),
                    " ".join(_COLLECTION_TERMS.get(collection, "") for collection in
                             item.get("collections") or [item.get("collection")]), item.get("_search_terms") or ""))
                stamp = parse_timestamp(item.get("published_at") or item.get("timestamp"))
                hit = make_hit(ref="pending:coursehub:" + identifier, source="coursehub", kind="page" if target.reader == "page" else "material",
                               title=title, snippet=item.get("source_title") or item.get("collection"),
                               timestamp=stamp, course=" ".join(str(code) for code in codes) or None,
                               url=target.url)
                hit.update({"site": site, "collection": item.get("collection"),
                            "course_match": item.get("course_match"), "authenticated": authenticated,
                            "reader": target.reader, "_target": target})
                candidates.append(Candidate(hit, stamp, title, meta, ""))
            reports[site]["suppressed_links"] = suppressed
        except Exception as exc:  # noqa: BLE001 - independent site failures remain explicit
            reports[site] = {"status": "error", "error_code": error_code(exc), "error": error_message(exc)}
    return {"candidates": candidates, "warnings": warnings,
            "details": {"sites": reports, "selected_sites": sites, "course_code": course_code,
                        "authenticated": authenticated, "material_limit_per_site": MAX_COURSEHUB_MATERIALS, "page_metadata_limit_per_site": hub.MAX_SEED_PAGES},
            "status": "error" if reports and all(r["status"] == "error" for r in reports.values()) else "partial"}


def search(app: Any, query: str, *, scopes: Any = None, since: str | None = None,
           before: str | None = None, include_body: bool = False, limit: int = DEFAULT_LIMIT,
           cursor: str | None = None, now: datetime | None = None,
           coursehub_sites=None, coursehub_course_code=None,
           coursehub_authenticated: bool = False) -> dict[str, Any]:
    if not isinstance(query, str):
        raise UnifiedError("query must be a string.")
    if type(include_body) is not bool:
        raise UnifiedError("include_body must be a boolean.")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise UnifiedError("limit must be an integer between 1 and 50.")
    sites, code = _coursehub_filters(coursehub_sites, coursehub_course_code, coursehub_authenticated)
    query = " ".join(query.split())
    if not query:
        raise UnifiedError("query must not be empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise UnifiedError(f"query must be at most {MAX_QUERY_CHARS} characters.")
    matcher = Matcher(query)
    if not matcher.groups:
        raise UnifiedError("query has no searchable words; add a course, name or topic.")
    chosen = _normalize_scopes(scopes)
    since_at, before_at = parse_bound(since, "since"), parse_bound(before, "before")
    if since_at and before_at and since_at >= before_at:
        raise UnifiedError("since must be earlier than before.")
    permissions = sorted(name for name in set(OPEN_OPERATIONS.values()) | {op for ops in SCOPE_OPERATIONS.values() for op in ops} | {"list_courses", "obs_list_semesters", "coursehub_materials", "coursehub_page", "coursehub_text"}
                         if operation_allowed(app, name))
    key = _cursor_key(query, chosen, since_at, before_at, include_body,
                      [sites, code, coursehub_authenticated, permissions])
    offset = decode_cursor(cursor, key)
    now = now or now_local()

    readers = {
        "mail": (lambda: _search_mail(app, query, since_at, before_at, bool(include_body)),
                 MAIL_BODY_TIMEOUT_SECONDS if include_body else SOURCE_TIMEOUT_SECONDS),
        "ninova": (lambda: _sso_read(app, lambda: _search_ninova(app)), SOURCE_TIMEOUT_SECONDS),
        "obs": (lambda: _sso_read(app, lambda: _search_obs(app)), SOURCE_TIMEOUT_SECONDS),
        "portal": (lambda: _sso_read(app, lambda: _search_portal(app)), SOURCE_TIMEOUT_SECONDS),
        "coursehub": (lambda: _search_coursehub(app, sites, code, coursehub_authenticated), SOURCE_TIMEOUT_SECONDS),
    }
    sources: dict[str, dict[str, Any]] = {}
    tasks = {}
    for scope in chosen:
        if scope_enabled(app, scope):
            tasks[scope] = readers[scope]
        else:
            sources[scope] = _disabled(scope)
    outcomes = run_concurrently(tasks, app=app)

    candidates: list[Candidate] = []
    for scope, outcome in outcomes.items():
        if not outcome["ok"]:
            sources[scope] = _failed(outcome)
            continue
        value = outcome["value"]
        scope_hits = rank(value["candidates"], matcher, include_body=bool(include_body), now=now,
                          since=since_at, before=before_at)
        count = len(scope_hits)
        if scope == "coursehub":
            status = value["status"]
        elif scope == "ninova":
            status = _ninova_status(value["data"], count)
        elif scope == "portal":
            status = _portal_status(value["data"], count)
        else:
            status = "ok" if count else "empty"
        report = _report(status, count=count, warnings=value["warnings"],
                         elapsed_ms=outcome["elapsed_ms"], **value["details"])
        if value.get("note"):
            report["coverage"] = value["note"]
        sources[scope] = report
        candidates.extend(value["candidates"])

    ranked = rank(candidates, matcher, include_body=bool(include_body), now=now,
                  since=since_at, before=before_at)
    page = ranked[offset: offset + limit]
    for hit in page:
        if hit["source"] == "coursehub":
            hit["ref"] = _remember(app, hit.pop("_target"))
            hit["ref_expires_in_seconds"] = REF_TTL_SECONDS
    next_offset = offset + len(page)
    statuses = {report["status"] for report in sources.values()}
    warnings = [f"{scope}: {warning}" for scope in SCOPES if scope in sources
                for warning in sources[scope]["warnings"]]
    result: dict[str, Any] = {
        "untrusted_external_content": True,
        "query": query,
        "scopes": chosen,
        "since": iso(since_at),
        "before": iso(before_at),
        "include_body": include_body,
        "coursehub_sites": sites if "coursehub" in chosen else [],
        "coursehub_course_code": code,
        "coursehub_authenticated": coursehub_authenticated,
        "count": len(page),
        "total_ranked": len(ranked),
        "results": page,
        "next_cursor": encode_cursor(next_offset, key) if next_offset < len(ranked) else None,
        "sources": {scope: sources[scope] for scope in chosen},
        "complete": statuses <= {"ok", "empty"},
        "warnings": warnings,
        "hint": "Open a result with itu_open(ref). A source with status error, partial or disabled "
                "may hold more matches; say so instead of reporting that nothing exists.",
    }
    if (since_at or before_at) and "coursehub" in sources:
        result["warnings"].append("CourseHub links without an observed timestamp are excluded by date bounds.")
    if include_body:
        result["body_search"] = ("Mail bodies were searched on the server; Ninova bodies only where "
                                 "listings or tracked data hold text.")
    return result


# ---------------------------------------------------------------------------
# itu_open


def envelope(ref: str, source: str | None, *, status: str = "ok", data: Any = None,
             warnings: list[str] | None = None, partial: bool = False, cache_hit: bool = False,
             age_seconds: float | None = None, error: dict[str, str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "source": source,
        "ref": ref,
        "retrieved_at": iso(now_local()),
        "cache": {"hit": cache_hit, "age_seconds": None if age_seconds is None else round(age_seconds)},
        "partial": partial,
        "warnings": list(warnings or []),
        "data": data,
        "untrusted_external_content": True,
    }
    if error:
        result["error"] = error
    return result


class NotFound(LookupError):
    """The reference is valid but names nothing the user can see."""


def _own_ninova_course(app: Any, course_id: str) -> dict[str, Any]:
    """The user's own course with this id, from the course list or tracked state."""
    if not operation_allowed(app, "list_courses"):
        raise NotFound("Course ownership cannot be checked with the enabled operations.")
    try:
        courses = app.list_courses().get("courses") or []
    except Exception:  # noqa: BLE001 - tracked state can still prove enrollment
        courses = []
    tracked = [entry.get("course") or {} for entry in (tracked_ninova(app).get("courses") or {}).values()]
    for course in courses + tracked:
        if ninova_course_id(course.get("url"), app.client.base_url) == course_id:
            return course
    raise NotFound("This course is not in your Ninova course list.")


def _tracked_entry(app: Any, ref: Ref) -> tuple[dict[str, Any], dict[str, Any], str | None] | None:
    state = tracked_ninova(app)
    base_url = app.client.base_url
    for entry in (state.get("courses") or {}).values():
        overview = (entry.get("snapshot") or {}).get("overview") or {}
        keys = {"announcement": ("announcements",), "assignment": ("assignments",),
                "file": ("class_files", "lesson_files")}[ref.kind]
        for key in keys:
            for item in overview.get(key) or []:
                if ninova_ref(item.get("url"), base_url) == ref.text:
                    return item, entry.get("course") or {}, entry.get("synced_at") or state.get("last_sync_at")
    return None


def _age(timestamp: Any) -> float | None:
    parsed = parse_timestamp(timestamp)
    return max(0.0, (now_local() - parsed).total_seconds()) if parsed else None


def _open_ninova(app: Any, ref: Ref, max_chars: int = 30000) -> dict[str, Any]:
    ensure_sso(app)
    base_url = app.client.base_url
    course = _own_ninova_course(app, ref.parts[0])
    tracked = _tracked_entry(app, ref)
    listed = tracked[0] if tracked else {}
    # A tracked file keeps its exact URL; it was matched to this ref above.
    url = listed["url"] if ref.kind == "file" and listed.get("url") else ninova_url(ref, base_url)
    meta = {"course": {"code": course.get("code"), "title": course.get("title"), "url": course.get("url")},
            "url": url}
    try:
        if ref.kind == "file":
            extracted = app.read_resource_text(url=url, max_chars=max_chars)
            data = {**meta, "kind": "file", "name": listed.get("name"), "path": listed.get("path"),
                    "size": listed.get("size"), "date": listed.get("date"), **extracted}
            return envelope(ref.text, "ninova", data=data)
        html, response = app.client.get_html(url)
        if ninova_ref(response.url, base_url) != ref.text:
            raise NotFound(f"Ninova did not show this {ref.kind}; it may have been removed.")
        if ref.kind == "announcement":
            detail = extract_announcement_detail(html, response.url, base_url=base_url)
            published = detail.get("published_at") or listed.get("published_at")
            data = {**meta, "kind": "announcement", "title": detail.get("title") or listed.get("title"),
                    "published_at": published, "published_at_iso": ninova_datetime_iso(published),
                    "author": listed.get("author"), "body_text": detail.get("body_text")}
        else:
            data = {**meta, "kind": "assignment", **_assignment_detail(app, html, response.url, listed)}
        return envelope(ref.text, "ninova", data=data)
    except NotFound:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back to tracked data when present
        if not tracked or ref.kind == "file":
            raise
        warning = f"Live read failed ({error_message(exc)}); showing the tracked copy."
        return envelope(ref.text, "ninova", data={**meta, "kind": ref.kind, **listed}, partial=True,
                        warnings=[warning], cache_hit=True, age_seconds=_age(tracked[2]))


def _assignment_detail(app: Any, html: str, page_url: str, listed: dict[str, Any]) -> dict[str, Any]:
    base_url = app.client.base_url
    detail = extract_assignment_detail(html, page_url, base_url=base_url)
    start = detail.get("submission_start") or listed.get("submission_start")
    end = detail.get("submission_end") or listed.get("submission_end")
    result = {
        "title": detail.get("title") or listed.get("title"),
        "description": detail.get("description"),
        "submission_start": start, "submission_start_iso": ninova_datetime_iso(start),
        "submission_end": end, "submission_end_iso": ninova_datetime_iso(end),
        "source_files": detail.get("source_files"),
        "required_files": detail.get("required_files"),
        "requested_file_count": listed.get("requested_file_count"),
        "uploaded_file_count": listed.get("uploaded_file_count"),
    }
    return result


def _open_obs(app: Any, ref: Ref) -> dict[str, Any]:
    ensure_sso(app)
    semester_id, crn = ref.parts
    semesters = (app.obs_list_semesters().get("ogrenciDonemListesi") or [])
    if not any(str(item.get("akademikDonemId")) == semester_id for item in semesters):
        raise NotFound("This semester is not in your OBS record.")
    payload = app.obs_get_schedule(semester=semester_id)
    rows = [row for row in (payload.get("schedule") or {}).get("kayitSinifResultList") or []
            if isinstance(row, dict) and str(row.get("crn")) == crn]
    exams = [row for row in (payload.get("final_calendar") or {}).get("finalBilgiList") or []
             if isinstance(row, dict) and str(row.get("crn")) == crn]
    if not rows and not exams:
        raise NotFound("This CRN is not among your registered courses for that semester.")
    warnings = []
    class_id, class_id_failed = None, False
    try:
        if not operation_allowed(app, "obs_list_registered_courses"):
            raise PermissionError("Registered course details disabled")
        registered = app.obs_list_registered_courses(semester=semester_id)
        class_id = next((row.get("sinifId") for row in registered.get("kayitSinifResultList") or []
                         if str(row.get("crn")) == crn), None)
    except Exception as exc:  # noqa: BLE001 - the class id only helps follow-up calls
        class_id_failed = True
        warnings.append(f"OBS class id unavailable: {error_message(exc)}")
    row = rows[0] if rows else exams[0]
    data = {
        "kind": ref.kind,
        "semester": payload.get("semester"),
        "crn": crn,
        "code": _obs_code(row),
        "name": _obs_name(row),
        "name_en": row.get("dersAdiEN"),
        "class_id": class_id,
        "term_start": rows[0].get("donemBaslangicTarihi") if rows else None,
        "term_end": rows[0].get("donemBitisTarihi") if rows else None,
        "meetings": [_meeting(session) for item in rows for session in item.get("sinifYerZaman") or []
                     if isinstance(session, dict)],
        "final_exams": [{"start": iso(parse_timestamp(exam.get("baslangicTarihi"))),
                         "end": iso(parse_timestamp(exam.get("bitisTarihi"))),
                         "place": _exam_place(exam) or None} for exam in exams],
        "follow_up": "For grades use obs_get_course_grades(class_id); for attendance obs_get_attendance(class_id).",
    }
    if ref.kind == "exam" and not exams:
        warnings.append("OBS lists no final exam for this course yet.")
    return envelope(ref.text, "obs", data=data, warnings=warnings, partial=class_id_failed)


def _meeting(session: dict[str, Any]) -> dict[str, Any]:
    day = DAY_CODES.get(str(session.get("gunKodu") or ""))
    return {
        "day": DAY_NAMES[day] if day is not None else session.get("gunAdiEN"),
        "start": _clock(session.get("baslangicSaati")),
        "end": _clock(session.get("bitisSaati")),
        "building": session.get("binaAdi") or session.get("binaKodu"),
        "room": session.get("mekanAdi"),
        "campus": session.get("kampusAdi"),
    }


def _open_portal(app: Any, ref: Ref) -> dict[str, Any]:
    ensure_sso(app)
    identifier = ref.parts[0]
    if ref.kind == "help_ticket":
        cache_key = f"help_ticket:{identifier}"
        cache = getattr(app, "_course_cache", None)
        age = cache.age_seconds(cache_key) if cache is not None else None
        result = app.get_help_ticket(identifier)
        return envelope(ref.text, "portal", data=result.get("ticket"), cache_hit=age is not None,
                        age_seconds=age)
    result = app.obs_get_notifications(notification_id=identifier)
    warnings = []
    if result.get("detail_available") is False:
        warnings.append("Only the notification title and relative age are available from the Portal widget.")
    return envelope(ref.text, "portal", data=result.get("notification"), warnings=warnings,
                    partial=bool(warnings))


def _open_coursehub(app, parsed, target, max_chars):
    # Recheck the selected-site boundary at open, independently of index metadata.
    if target.reader == "page":
        hub._checked_source_page(target.url, target.site)
        data = app.coursehub_page(site=target.site, url=target.url,
                                  authenticated=target.authenticated, max_chars=max_chars)
    elif target.reader == "document":
        hub._checked_source_document(target.url, target.site)
        data = app.coursehub_text(url=target.url, authenticated=target.authenticated, max_chars=max_chars)
    else:
        return envelope(parsed.text, "coursehub", status="error", error={
            "code": "unsupported", "message": "Text extraction is not supported for this material format."})
    return envelope(parsed.text, "coursehub", data=data)


def open_ref(app: Any, ref: str, max_chars: int = 30000) -> dict[str, Any]:
    try:
        parsed = parse_ref(ref)
        if type(max_chars) is not int or not 1000 <= max_chars <= 100000:
            raise UnifiedError("max_chars must be an integer between 1000 and 100000.")
    except UnifiedError as exc:
        return envelope("", None, status="error", error={"code": "invalid_ref", "message": str(exc)})
    target = None
    if parsed.source == "coursehub":
        try:
            target = _lookup(app, parsed.parts[0])
        except NotFound as exc:
            return envelope(parsed.text, parsed.source, status="error",
                            error={"code": "not_found", "message": str(exc)})
        operations = ["coursehub_page" if target.reader == "page" else "coursehub_text"]
    else:
        operations = [OPEN_OPERATIONS[(parsed.source, parsed.kind)]]
        if parsed.source == "ninova":
            operations.append("list_courses")
        if parsed.source == "obs":
            operations.append("obs_list_semesters")
        if parsed.source == "portal" and parsed.kind == "help_ticket":
            operations.append("obs_get_help_tickets")
    if any(not operation_allowed(app, operation) for operation in operations):
        return envelope(parsed.text, parsed.source, status="error",
                        error={"code": "disabled", "message": "A required read operation is not enabled on this server."})
    def read():
        try:
            if parsed.source == "coursehub":
                return _open_coursehub(app, parsed, target, max_chars)
            if parsed.source == "mail":
                mailbox = "SENT" if parsed.parts[0] == "sent" else "INBOX"
                return envelope(parsed.text, "mail", data=app.mail_get_message(
                    parsed.parts[1], mailbox=mailbox, max_chars=max_chars))
            if parsed.source == "ninova":
                return _sso_read(app, lambda: _open_ninova(app, parsed, max_chars))
            if parsed.source == "obs":
                return _sso_read(app, lambda: _open_obs(app, parsed))
            return _sso_read(app, lambda: _open_portal(app, parsed))
        except NotFound as exc:
            return envelope(parsed.text, parsed.source, status="error",
                            error={"code": "not_found", "message": str(exc)})
    outcome = run_concurrently({"open:" + parsed.source: (read, SOURCE_TIMEOUT_SECONDS)}, app=app)["open:" + parsed.source]
    if outcome["ok"]:
        return outcome["value"]
    return envelope(parsed.text, parsed.source, status="error",
                    error={"code": outcome["code"], "message": outcome["error"]})
