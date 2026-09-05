#!/usr/bin/env python3
"""Shared transport, schema and reporting for the legal-survey scripts.

Three responsibilities live here because every script needs all three: the HTTP
layer (retries, throttling, robots), the records that carry the skill's
confidence discipline, and the reporter every CLI prints through.

The records are the important part, and they encode two rules specific to law
rather than to research generally:

  1. An authority that was not selected must say why, and one that was selected
     must say whether it *binds the forum* -- both enforced in __post_init__,
     because "this case is relevant" and "this case controls" are different
     claims and only the second one decides anything.
  2. A holding is a Claim and dicta is a Dictum, and the two are different types.
     Dicta cited as though it were a holding is the most common way a legal memo
     is confidently wrong, and a dataclass can refuse it where prose cannot.
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

# What kind of thing this is. The distinction is not cosmetic: it decides how the
# authority is read, whether it needs an as-of date, and whether it can be
# validated for treatment at all.
AUTHORITY_TYPES = (
    "case",
    "statute",
    "regulation",
    "constitution",
    "legislative_history",
    "secondary",
    "treaty",
)

# Authorities whose meaning depends on when you looked. A code section is a
# snapshot; the same citation resolved a year apart is two different texts.
DATED_TYPES = ("statute", "regulation", "constitution", "treaty")

# How much this authority weighs in the forum the question was asked about.
# "binding" is a claim about a specific court, which is why weight_reason has to
# name the basis: same jurisdiction, superior court, on point.
WEIGHTS = ("binding", "persuasive", "secondary")

MANIFEST_STATUSES = ("ok", "paywalled", "robots_blocked", "failed")
GAP_STATUSES = ("paywalled", "robots_blocked", "failed")

# The basis on which a claim rests. "dictum" is deliberately absent: dicta is a
# separate record type, so a reader cannot file it here by accident.
CLAIM_BASES = (
    "holding",
    "statutory_text",
    "regulatory_text",
    "constitutional_text",
    "secondary_analysis",
)

TREATMENT_STATUSES = ("good", "caution", "negative", "unchecked", "not_applicable")

USER_AGENT = "legal-survey/0.1 (+https://github.com/anthropics/coding-skills)"

# Retrying these is worth a wait; anything else is a definite answer.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass
class Authority:
    """One piece of law or commentary that discovery surfaced, selected or not.

    Two fields carry the discipline. `status` is why an authority is absent from
    the corpus; `weight` is what it is worth in the forum being researched. An
    authority selected without a weight is one nobody decided the strength of,
    and strength is most of what a legal answer consists of.
    """

    title: str
    authority_type: str = "case"
    external_ids: dict[str, str] = field(default_factory=dict)
    citation: str = ""
    court: str = ""
    jurisdiction: str = ""
    decided: str = ""
    precedential_status: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    citation_count: int | None = None
    summary: str = ""
    as_of: str = ""
    pdf_url: str = ""
    landing_url: str = ""
    sources: list[str] = field(default_factory=list)
    status: str = "new"
    drop_reason: str = ""
    weight: str = ""
    weight_reason: str = ""

    def __post_init__(self) -> None:
        if self.authority_type not in AUTHORITY_TYPES:
            raise ValueError(
                "authority_type must be one of " + repr(AUTHORITY_TYPES)
                + ", got " + repr(self.authority_type)
            )
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(
                "status must be one of " + repr(CANDIDATE_STATUSES) + ", got " + repr(self.status)
            )
        if self.status == "dropped" and not self.drop_reason:
            raise ValueError(
                "a dropped authority must carry a drop_reason: a survey that cannot say what "
                "it declined to read is indistinguishable from one that never looked"
            )
        if self.status != "dropped" and self.drop_reason:
            raise ValueError(
                "drop_reason is only meaningful on a dropped authority; got status "
                + repr(self.status)
            )
        if self.weight and self.weight not in WEIGHTS:
            raise ValueError(
                "weight must be one of " + repr(WEIGHTS) + ", got " + repr(self.weight)
            )
        if self.status == "selected" and not self.weight:
            raise ValueError(
                "a selected authority must carry a weight (" + "|".join(WEIGHTS) + "): whether "
                "it binds the forum is the difference between an argument and a reading list, "
                "and no later stage can infer it"
            )
        if self.weight and not self.weight_reason:
            raise ValueError(
                "a weight must carry its reason -- which forum, and on what basis (same "
                "jurisdiction, superior court, on point). 'binding' with no basis is an "
                "assertion about a court, not a finding"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Authority":
        return cls(**data)


@dataclass
class ManifestEntry:
    """One artifact's record: what it is, where it came from, and whether it arrived.

    A status other than "ok" is a *gap*, and the report has a tab for gaps because
    "the law says" and "the law we could reach says" are different claims. An
    entry may therefore never be both a failure and a file.

    `as_of` is required for codified law. A statute or regulation with no version
    date cannot support a claim about what the law said, because the citation
    alone does not identify the text -- and the reader who most needs the report
    is usually asking about conduct that happened at some particular time.
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
    citation: str = ""
    as_of: str = ""
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
            if self.kind in DATED_TYPES and not self.as_of:
                raise ValueError(
                    "an archived " + self.kind + " must carry an as_of date: codified law is a "
                    "snapshot, and a citation without a version does not identify the text a "
                    "claim rests on"
                )
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
    what silently turns "1985-2010" (with an en dash) into "19852010" -- two
    numbers fused into a meaningless one, in a filename nobody will re-read.
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


def normalize_citation(citation: str) -> str:
    """A reporter citation reduced to what two renderings of it share.

    "410 U.S. 113", "410 US 113" and "410 U. S. 113" are one citation; only the
    volume, the reporter's letters and the page carry identity. Periods and
    spaces do not, and treating them as though they did splits one case into
    three candidates that never merge -- so the same case is fetched three times,
    read three times, and counted three times in a corpus whose numbers are
    supposed to mean something.
    """
    return re.sub(r"[^a-z0-9]+", "", (citation or "").lower())


# Volume, reporter, page. The reporter needs digits as well as letters -- "F.3d",
# "F. Supp. 2d" and "N.Y.S.2d" are reporter names, and a class without digits
# silently matched none of the federal reporters carrying a series number, which
# is most of the modern corpus. It is bounded, and must start with a capital, so
# a sentence sitting between two numbers cannot be swallowed as a reporter name.
CITE_RE = re.compile(r"\b(\d+)\s+([A-Z][A-Za-z0-9.'&\- ]{0,28}?[A-Za-z0-9.])\s+(\d+)\b")


def parse_citation(text: str) -> tuple[str, str, str] | None:
    """(volume, reporter, page) from a reporter citation, or None.

    Used for two things: a stable artifact id, and the loose pattern
    verify_authorities.py looks for in a fetched document. The volume and page
    are stable across renderings; the reporter's punctuation is not.
    """
    match = CITE_RE.search(text or "")
    if not match:
        return None
    return match.group(1), match.group(2).strip(), match.group(3)


def citation_pattern(citation: str) -> "re.Pattern | None":
    """A regex matching this reporter citation however a document punctuated it.

    "410 U.S. 113", "410 U. S. 113" and "410 US 113" are one citation, and a
    literal search finds only the rendering the search API happened to return.
    Volume and page are exact; the reporter between them must start with a letter
    and may carry digits, because "F.3d" and "N.Y.S.2d" are reporter names -- a
    class without digits matched none of the federal reporters with a series
    number, so the treatment scan silently found nothing in the whole modern
    corpus and every case came back clean.

    The middle is short and must begin with a letter, so the pattern cannot leap
    across a sentence and join two unrelated numbers.

    One implementation, imported by both the treatment scan and the gate. Two
    copies of a regex this fiddly drift, and the two stages disagreeing about
    whether a document mentions a case is the worst possible place for it.
    """
    parts = parse_citation(citation)
    if not parts:
        return None
    volume, _reporter, page = parts
    return re.compile(r"\b" + re.escape(volume)
                      + r"[\s.,]+[A-Za-z][A-Za-z0-9. ]{0,20}?\s+"
                      + re.escape(page) + r"\b")


def citation_key(citation: str, authority_type: str = "case") -> str:
    """The one canonical form of a citation. Used for the artifact id *and* the
    dedupe key, which must agree or the corpus splits.

    A case is keyed on the parsed triple, so "901 F.3d 1099" and "901 F.3d 1099
    (9th Cir. 2019)" are one key: the parenthetical is bibliography, not identity,
    and two sources will disagree about whether to include it.

    Codified law is keyed on the whole flattened string, because a code citation
    is not a volume-reporter-page triple. Parsing "40 CFR 60.1" yields ("40",
    "CFR", "60") -- the subsection is dropped, so 40 CFR 60.1 and 40 CFR 60.2
    collapse to one id, one file on disk and one note covering two different
    regulations. Flattening keeps them apart and still collapses "40 CFR 60.1"
    and "40 CFR Sec. 60.1".
    """
    if not citation:
        return ""
    if authority_type == "case":
        parts = parse_citation(citation)
        if parts:
            volume, reporter, page = parts
            return volume + re.sub(r"[^a-z0-9]+", "", reporter.lower())[:16] + page
    return normalize_citation(citation)


def artifact_id(authority: Authority) -> str:
    """The stable handle a locator points at.

    The reporter citation comes first because it is what lawyers cite, grep for
    and argue about, and because it is the identity two databases are most likely
    to agree on. Then the source-specific ids, then a hash of the title, which is
    stable across runs so a resumed fetch resolves the same file.

    Every id is flattened to a filesystem-safe token: this names files, including
    `docs/notes/<id>.json`, and a slash in it silently becomes a directory that
    the non-recursive note glob cannot see -- so the authority reads as never
    read, and its claims never reach the gate.
    """
    ids = authority.external_ids
    key = citation_key(authority.citation, authority.authority_type)
    if key:
        return key[:64]
    for key in ("cl", "fr", "ecfr", "usc", "uk", "doi", "openalex"):
        if ids.get(key):
            return key + "-" + re.sub(r"[^A-Za-z0-9._-]+", "_", ids[key])
    digest = hashlib.sha256(authority.title.encode("utf-8")).hexdigest()[:10]
    return "t" + digest


def artifact_filename(authority: Authority, suffix: str) -> str:
    return artifact_id(authority) + "-" + slugify(authority.title) + suffix


@dataclass
class Locator:
    """Where in an authority a claim's evidence sits.

    An artifact id on its own points at a document, not at evidence, and "it's in
    there somewhere" is what an unread case looks like. So a locator needs a page,
    a section, or a quote -- something verify_authorities.py can actually resolve.

    `pincite` is the citation form a reader will see ("at 118", "para. 42",
    "sec. 552(b)(5)"). It deliberately does *not* satisfy the requirement: a
    pincite is a claim about where the page is, not evidence that the page was
    opened, and it is the single easiest thing in a legal document to invent.
    """

    artifact_id: str
    page: int | None = None
    section: str = ""
    quote: str = ""
    pincite: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("a locator must name an artifact_id")
        if self.page is None and not self.section and not self.quote:
            raise ValueError(
                "a locator needs a page, a section or a quote: an artifact id and a pincite "
                "point at a document rather than at evidence, and a pincite is the easiest "
                "part of a citation to fabricate"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Locator":
        return cls(**data)


@dataclass
class Claim:
    """A proposition of law the corpus establishes. Must carry its evidence.

    `basis` says what kind of authority it rests on, and the vocabulary excludes
    dicta on purpose -- see Dictum.

    `treatment_ack` is the escape hatch for citing an authority that is not in
    good standing. Citing an overruled case is sometimes exactly right (for the
    rule as it stood, or for a holding the overruling did not touch), but it has
    to be said out loud -- otherwise the report looks identical whether the
    author knew or not.
    """

    text: str
    basis: str = "holding"
    locators: list[Locator] = field(default_factory=list)
    treatment_ack: str = ""

    def __post_init__(self) -> None:
        if self.basis not in CLAIM_BASES:
            hint = ""
            if self.basis in ("dictum", "dicta"):
                hint = ("; dicta is not a basis for a claim -- record it as a Dictum, which is "
                        "the whole point of the two types being different")
            raise ValueError(
                "basis must be one of " + repr(CLAIM_BASES) + ", got " + repr(self.basis) + hint
            )
        if not self.locators:
            raise ValueError(
                "a claim must carry at least one locator: an unpincited proposition of law is "
                "the fabricated-citation failure this skill exists to prevent"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "basis": self.basis, "treatment_ack": self.treatment_ack,
                "locators": [locator.to_dict() for locator in self.locators]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(text=data.get("text", ""),
                   basis=data.get("basis", "holding"),
                   treatment_ack=data.get("treatment_ack", ""),
                   locators=[Locator.from_dict(item) for item in data.get("locators") or []])


@dataclass
class Dictum:
    """Something an authority says that does not decide anything.

    Dicta, a concurrence, a footnote's aside, a law review's proposal, an agency's
    non-binding guidance: language a later court is free to ignore. It carries
    `why_not_binding` instead of locators, because giving it locators would dress
    it as a holding -- the mistake the split exists to make impossible.

    If the proposition really is the court's holding, promote it to a Claim.
    """

    text: str
    why_not_binding: list[str] = field(default_factory=list)
    locators: list[Locator] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.why_not_binding:
            raise ValueError(
                "a dictum must carry why_not_binding: naming what makes this language "
                "non-binding -- unnecessary to the judgment, a concurrence, a lower court, "
                "another jurisdiction, a secondary source -- is the whole difference between "
                "a dictum and a holding"
            )
        if self.locators:
            raise ValueError(
                "a dictum must not carry locators -- that would present non-binding language "
                "as established law; promote it to a Claim if it really is the holding"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "why_not_binding": list(self.why_not_binding)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dictum":
        return cls(text=data.get("text", ""),
                   why_not_binding=list(data.get("why_not_binding") or []))


@dataclass
class Note:
    """One reader's extraction from one authority. The only thing synthesis sees."""

    artifact_id: str
    claims: list[Claim] = field(default_factory=list)
    dicta: list[Dictum] = field(default_factory=list)
    posture: str = ""
    disposition: str = ""
    rule: str = ""
    facts_material: str = ""
    reasoning: str = ""
    separate_opinions: str = ""
    subsequent_history: str = ""
    limits: str = ""
    related_authorities: list[str] = field(default_factory=list)
    prose: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Note":
        return cls(
            artifact_id=data.get("artifact_id", ""),
            claims=[Claim.from_dict(c) for c in data.get("claims") or []],
            dicta=[Dictum.from_dict(item) for item in data.get("dicta") or []],
            posture=data.get("posture", ""),
            disposition=data.get("disposition", ""),
            rule=data.get("rule", ""),
            facts_material=data.get("facts_material", ""),
            reasoning=data.get("reasoning", ""),
            separate_opinions=data.get("separate_opinions", ""),
            subsequent_history=data.get("subsequent_history", ""),
            limits=data.get("limits", ""),
            related_authorities=list(data.get("related_authorities") or []),
            prose=data.get("prose", ""),
        )


@dataclass
class Treatment:
    """Whether an authority is still good law, as far as an open citation graph can tell.

    **This is not a citator.** Shepard's and KeyCite are editorial products built
    by people reading citing opinions; this walks a public citation graph and
    matches language. "good" here means *no negative signal was found in what was
    checked*, which is a weaker statement, and every consumer of this record is
    required to say so.

    A `caution` or `negative` status must name the signals that produced it, and
    an `unchecked` one must say why -- including the common and important case of
    an authority nothing has cited yet, which is unvalidated rather than clean.
    """

    artifact_id: str
    status: str
    signals: list[dict[str, Any]] = field(default_factory=list)
    citing_checked: int = 0
    citing_total: int | None = None
    source: str = ""
    checked_at: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in TREATMENT_STATUSES:
            raise ValueError(
                "status must be one of " + repr(TREATMENT_STATUSES) + ", got " + repr(self.status)
            )
        if self.status in ("negative", "caution") and not self.signals:
            raise ValueError(
                "a " + self.status + " treatment must name the signals that produced it: an "
                "unexplained flag is one nobody can check or overrule"
            )
        if self.status == "unchecked" and not self.reason:
            raise ValueError(
                "an unchecked treatment must say why: 'nothing has cited this yet' and 'the "
                "citation graph was unreachable' are different facts, and only one of them is "
                "about the authority"
            )

    @property
    def is_clean(self) -> bool:
        return self.status in ("good", "not_applicable")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Treatment":
        return cls(**data)


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


def load_treatments(out) -> dict[str, Treatment]:
    """The treatment record per artifact, or {} if check_treatment.py has not run."""
    path = Path(out) / "treatment.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["artifact_id"]: Treatment.from_dict(row)
            for row in payload.get("treatments", [])}


# A page object is "/Type /Page" not followed by "s" -- the "/Pages" tree node uses
# the same prefix and must not be counted.
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![s])")

# The page tree declares its own total. Both key orders occur, and the match is
# anchored on /Type /Pages so an outline dictionary's /Count is not mistaken for
# a page total.
_PDF_COUNT_RES = (
    re.compile(rb"/Type\s*/Pages\b[^>]{0,400}?/Count\s+(\d+)", re.DOTALL),
    re.compile(rb"/Count\s+(\d+)[^>]{0,400}?/Type\s*/Pages\b", re.DOTALL),
)


def pdf_page_count(data: bytes) -> int | None:
    """How many pages a PDF has, or None when it cannot be determined.

    Deliberately crude: no stdlib module parses PDF, and the only thing this is
    used for is rejecting a pincite to page 40 of a 12-page slip opinion. None
    means unknown, and an unknown count must make a page check *unverifiable*
    rather than passing -- see verify_authorities.py.

    The declared /Count wins over the number of page objects visible in the
    bytes, and the larger of the two wins over either, because since PDF 1.5 a
    producer may pack page objects into compressed object streams. Erring high
    costs only the detection of a page number past the end; erring low breaks
    true claims against a blocking gate.
    """
    if not data.startswith(b"%PDF"):
        return None
    declared = [int(m.group(1)) for pattern in _PDF_COUNT_RES for m in pattern.finditer(data)]
    count = max([len(_PDF_PAGE_RE.findall(data))] + declared)
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
    second of wall-clock. Politeness is not optional here -- one connection at a
    time per host, a real interval between calls, and robots.txt honoured for
    open-web fetches. CourtListener, the Federal Register and eCFR are public
    goods run on small budgets.

    `token` is sent as an Authorization header when present. CourtListener
    answers anonymously but with a much lower rate limit and a capped result set,
    so a run without one covers less -- which the caller reports as a caveat
    rather than absorbing silently.
    """

    def __init__(self, opener=None, sleep=time.sleep, monotonic=time.monotonic,
                 max_attempts: int = 4, backoff_base: float = 1.0,
                 min_interval: float = 1.0, timeout: float = 60.0,
                 user_agent: str = USER_AGENT, token: str = ""):
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self.timeout = timeout
        self.user_agent = user_agent
        self.token = token
        self._last_call: dict[str, float] = {}
        self._robots: dict[str, Any] = {}

    def _headers(self, url: str, accept: str) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        # Scoped to the host the token belongs to. A blanket Authorization header
        # would hand a CourtListener token to every publisher the fetch stage
        # touches, which is a credential leak dressed as convenience.
        host = urlsplit(url).netloc.split(":")[0].lower()
        if self.token and (host == "courtlistener.com" or host.endswith(".courtlistener.com")):
            headers["Authorization"] = "Token " + self.token
        return headers

    def get(self, url: str, accept: str = "*/*") -> Response:
        host = urlsplit(url).netloc
        self._wait_for_host(host)
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(url, headers=self._headers(url, accept))
            # Stamped before the call, so a request that fails still holds the host's
            # place in the queue. Stamping only on success means a host answering 404
            # is never throttled at all -- thirty dead links go out back to back with
            # no interval between them, which is precisely the traffic pattern the
            # etiquette section promises not to generate, and it happens only when a
            # server is already unhappy.
            self._last_call[host] = self._monotonic()
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
                self._last_call[host] = self._monotonic()
                if exc.code not in RETRYABLE_STATUS:
                    raise FetchError("HTTP " + str(exc.code) + " for " + url) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self._last_call[host] = self._monotonic()
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
