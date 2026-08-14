#!/usr/bin/env python3
"""Shared transport, schema and reporting for the literature-survey scripts.

Three responsibilities live here because every script needs all three: the HTTP
layer (retries, throttling, robots), the two records that carry the skill's
confidence discipline, and the reporter every CLI prints through.

The records are the important part. A candidate that was not selected must say
why, and an artifact that was not obtained must say why — both enforced in
__post_init__, so a script cannot report an absence as a fact.
"""

import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CANDIDATE_STATUSES = ("new", "selected", "dropped")

MANIFEST_STATUSES = ("ok", "paywalled", "robots_blocked", "failed")
GAP_STATUSES = ("paywalled", "robots_blocked", "failed")

USER_AGENT = "literature-survey/0.1 (+https://github.com/anthropics/coding-skills)"

# Retrying these is worth a wait; anything else is a definite answer.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass
class Candidate:
    """One piece of work that discovery surfaced, selected or not.

    `status` is the whole point. A dropped candidate carries the reason; a kept
    one carries none, so "why isn't this in the survey" always has an answer on
    disk.
    """

    title: str
    external_ids: dict[str, str] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    citation_count: int | None = None
    abstract: str = ""
    pdf_url: str = ""
    landing_url: str = ""
    sources: list[str] = field(default_factory=list)
    status: str = "new"
    drop_reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(
                "status must be one of " + repr(CANDIDATE_STATUSES) + ", got " + repr(self.status)
            )
        if self.status == "dropped" and not self.drop_reason:
            raise ValueError(
                "a dropped candidate must carry a drop_reason: a survey that cannot say what "
                "it declined to read is indistinguishable from one that never looked"
            )
        if self.status != "dropped" and self.drop_reason:
            raise ValueError(
                "drop_reason is only meaningful on a dropped candidate; got status "
                + repr(self.status)
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(**data)


@dataclass
class ManifestEntry:
    """One artifact's record: what it is, where it came from, and whether it arrived.

    A status other than "ok" is a *gap*, and the report has a tab for gaps because
    "the literature says" and "the reachable literature says" are different
    claims. An entry may therefore never be both a failure and a file.
    """

    artifact_id: str
    kind: str
    url: str
    status: str
    path: str = ""
    sha256: str = ""
    bytes_len: int = 0
    fetched_at: str = ""
    content_type: str = ""
    license: str = ""
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in MANIFEST_STATUSES:
            raise ValueError(
                "status must be one of " + repr(MANIFEST_STATUSES) + ", got " + repr(self.status)
            )
        if self.status == "ok":
            if not self.sha256:
                raise ValueError(
                    "an ok entry must carry a sha256; an unhashed file cannot pin a citation"
                )
            if not self.path:
                raise ValueError("an ok entry must carry a path")
            if self.failure_reason:
                raise ValueError("an ok entry must not carry a failure_reason")
        else:
            if not self.failure_reason:
                raise ValueError(
                    "a non-ok entry must carry a failure_reason: a gap with no reason reads as "
                    "an oversight rather than a limit"
                )
            if self.path or self.sha256:
                raise ValueError("a non-ok entry must not claim a path or sha256 on disk")

    @property
    def is_gap(self) -> bool:
        return self.status in GAP_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManifestEntry":
        return cls(**data)


def slugify(text: str, maxlen: int = 80) -> str:
    """A readable, filesystem-safe, deterministic slug.

    Truncation lands on a word boundary because a slug cut mid-word reads like a
    corrupted filename and sends people looking for a bug that is not there.

    Accents are stripped by dropping combining marks after NFKD, but every other
    non-ASCII character becomes a separator rather than being deleted. Deleting is
    what silently turns "1985-2010" (with an en dash) into "19852010" — two numbers
    fused into a meaningless one, in a filename nobody will re-read.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", unaccented).strip("-").lower()
    if not cleaned:
        return "untitled"
    if len(cleaned) <= maxlen:
        return cleaned
    cut = cleaned[:maxlen]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-") or "untitled"


def artifact_id(candidate: Candidate) -> str:
    """The stable handle a locator points at.

    arXiv id first because it is what people cite and grep for; then DOI with the
    slash made filesystem-safe; then the OpenAlex id, which `snowball.py` needs to
    turn a note's filename back into a graph query; then a hash of the title, which
    is stable across runs so a resumed fetch resolves the same file.
    """
    ids = candidate.external_ids
    if ids.get("arxiv"):
        return ids["arxiv"]
    if ids.get("doi"):
        return ids["doi"].replace("/", "_")
    if ids.get("openalex"):
        return ids["openalex"]
    digest = hashlib.sha256(candidate.title.encode("utf-8")).hexdigest()[:10]
    return "t" + digest


def artifact_filename(candidate: Candidate, suffix: str) -> str:
    return artifact_id(candidate) + "-" + slugify(candidate.title) + suffix


@dataclass
class Locator:
    """Where in an artifact a claim's evidence sits.

    An artifact id on its own points at a document, not at evidence, and "it's in
    there somewhere" is what an unread paper looks like. So a locator needs a page,
    a section, or a quote — something verify_locators.py can actually resolve.
    """

    artifact_id: str
    page: int | None = None
    section: str = ""
    quote: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("a locator must name an artifact_id")
        if self.page is None and not self.section and not self.quote:
            raise ValueError(
                "a locator needs a page, a section or a quote: an artifact id alone points at "
                "a document rather than at evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Locator":
        return cls(**data)


@dataclass
class Claim:
    """An assertion about the literature. Must carry its evidence."""

    text: str
    locators: list[Locator] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.locators:
            raise ValueError(
                "a claim must carry at least one locator: a claim with no locator is the "
                "fluent-synthesis-over-unread-abstracts failure this skill exists to prevent"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "locators": [locator.to_dict() for locator in self.locators]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(text=data.get("text", ""),
                   locators=[Locator.from_dict(item) for item in data.get("locators") or []])


@dataclass
class Lead:
    """Something the corpus hints at but does not establish.

    Carries the benign explanations instead of locators. Giving a lead locators
    would dress a guess as a finding, which is the mistake the split exists to
    make impossible.
    """

    text: str
    also_explained_by: list[str] = field(default_factory=list)
    locators: list[Locator] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.also_explained_by:
            raise ValueError(
                "a lead must carry also_explained_by: the benign readings are the whole "
                "difference between a lead and a claim"
            )
        if self.locators:
            raise ValueError(
                "a lead must not carry locators — that would present a guess as established; "
                "promote it to a Claim if the evidence is really there"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "also_explained_by": list(self.also_explained_by)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lead":
        return cls(text=data.get("text", ""),
                   also_explained_by=list(data.get("also_explained_by") or []))


@dataclass
class Note:
    """One reader's extraction from one artifact. The only thing synthesis sees."""

    artifact_id: str
    claims: list[Claim] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    method: str = ""
    data_and_n: str = ""
    baselines: str = ""
    limitations_stated: str = ""
    limitations_unstated: str = ""
    artifacts: list[str] = field(default_factory=list)
    prose: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Note":
        return cls(
            artifact_id=data.get("artifact_id", ""),
            claims=[Claim.from_dict(c) for c in data.get("claims") or []],
            leads=[Lead.from_dict(item) for item in data.get("leads") or []],
            method=data.get("method", ""),
            data_and_n=data.get("data_and_n", ""),
            baselines=data.get("baselines", ""),
            limitations_stated=data.get("limitations_stated", ""),
            limitations_unstated=data.get("limitations_unstated", ""),
            artifacts=list(data.get("artifacts") or []),
            prose=data.get("prose", ""),
        )


def load_notes(out) -> list[Note]:
    """Every note under <out>/docs/notes/, or [] if nothing has been read.

    A note that will not parse raises rather than being skipped: silently dropping
    it would understate how much was read, and "read in full" is the most honest
    number in the report.
    """
    notes_dir = Path(out) / "docs" / "notes"
    if not notes_dir.is_dir():
        return []
    notes = []
    for path in sorted(notes_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("note " + path.name + " did not parse: " + str(exc)) from exc
        notes.append(Note.from_dict(data))
    return notes


# A page object is "/Type /Page" not followed by "s" — the "/Pages" tree node uses
# the same prefix and must not be counted.
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![s])")


def pdf_page_count(data: bytes) -> int | None:
    """How many pages a PDF has, or None when it cannot be determined.

    Deliberately crude: no stdlib module parses PDF, and the only thing this is
    used for is rejecting a citation to page 40 of a 12-page paper. None means
    unknown, and an unknown count must make a page check *unverifiable* rather
    than passing — see verify_locators.py.
    """
    if not data.startswith(b"%PDF"):
        return None
    count = len(_PDF_PAGE_RE.findall(data))
    return count or None


class FetchError(Exception):
    """A fetch that will not succeed by trying again the same way."""


@dataclass
class Response:
    url: str
    body: bytes
    content_type: str = ""


class Http:
    """The single seam through which this skill touches the network.

    Everything injectable: `opener`, `sleep` and `monotonic` are parameters so the
    tests exercise retry, backoff and throttle logic without a socket or a real
    second of wall-clock. Politeness is not optional here — one connection at a
    time per host, a real interval between calls, and robots.txt honoured for
    open-web fetches.
    """

    def __init__(self, opener=None, sleep=time.sleep, monotonic=time.monotonic,
                 max_attempts: int = 4, backoff_base: float = 1.0,
                 min_interval: float = 1.0, timeout: float = 60.0,
                 user_agent: str = USER_AGENT):
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self.timeout = timeout
        self.user_agent = user_agent
        self._last_call: dict[str, float] = {}
        self._robots: dict[str, Any] = {}

    def get(self, url: str, accept: str = "*/*") -> Response:
        host = urlsplit(url).netloc
        self._wait_for_host(host)
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, "Accept": accept}
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    body = response.read()
                    headers = getattr(response, "headers", {}) or {}
                    content_type = ""
                    if hasattr(headers, "get"):
                        content_type = headers.get("Content-Type", "") or ""
                    self._last_call[host] = self._monotonic()
                    return Response(url=url, body=body, content_type=content_type)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS:
                    raise FetchError("HTTP " + str(exc.code) + " for " + url) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < self.max_attempts:
                self._sleep(self.backoff_base * (2 ** (attempt - 1)))
        raise FetchError("gave up on " + url + " after " + str(self.max_attempts)
                         + " attempts: " + repr(last_error))

    def get_json(self, url: str) -> Any:
        return json.loads(self.get(url, accept="application/json").body.decode("utf-8"))

    def _wait_for_host(self, host: str) -> None:
        previous = self._last_call.get(host)
        if previous is None:
            return
        elapsed = self._monotonic() - previous
        remaining = self.min_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def robots_allows(self, url: str) -> bool:
        """Whether robots.txt permits this fetch. Absent rules mean permission."""
        parts = urlsplit(url)
        host_key = parts.scheme + "://" + parts.netloc
        parser = self._robots.get(host_key)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                body = self.get(host_key + "/robots.txt").body.decode("utf-8", "replace")
                parser.parse(body.splitlines())
            except FetchError:
                parser.parse([])  # nothing published; nothing forbidden
            self._robots[host_key] = parser
        return parser.can_fetch(self.user_agent, url)


class Reporter:
    """The uniform output surface: one headline, any number of caveats, then rows.

    The headline is mandatory because the caller reads it first and sometimes
    only; a report that opens with rows makes the agent infer the summary, which
    is exactly the inference this skill exists to remove.
    """

    def __init__(self, tool: str):
        self.tool = tool
        self._headline: str | None = None
        self._caveats: list[str] = []
        self._rows: list[dict[str, Any]] = []

    def headline(self, text: str) -> None:
        self._headline = text

    def caveat(self, text: str) -> None:
        self._caveats.append(text)

    def row(self, row: dict[str, Any]) -> None:
        self._rows.append(row)

    def emit(self, fmt: str = "text") -> None:
        if self._headline is None:
            raise ValueError("emit() before headline(): every report must lead with its summary")
        if fmt == "json":
            print(json.dumps({
                "tool": self.tool,
                "headline": self._headline,
                "caveats": self._caveats,
                "rows": self._rows,
            }, indent=2))
            return
        print(self._headline)
        for caveat in self._caveats:
            print("  caveat: " + caveat)
        for row in self._rows:
            print("  " + "  ".join(str(k) + "=" + str(v) for k, v in row.items()))
