"""The one seam that touches other people's infrastructure.

Acquisition etiquette is a promise SKILL.md makes to CourtListener, the Federal
Register and eCFR, and a promise nothing checks is a comment. `Http` takes its
opener, sleep and clock as parameters precisely so the promise can be tested
without a socket or a real second of wall-clock, so these tests assert on what it
*waited* for, not only on what it returned.

The token tests are the ones with teeth: a credential is scoped to one host, and
"scoped" has to mean a suffix match on the host and not a substring match on the
URL, or a hostile URL collects it.
"""

import io
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


class FakeResponse(io.BytesIO):
    """What urlopen yields: a context manager over bytes, plus headers."""

    def __init__(self, body: bytes, content_type: str = ""):
        super().__init__(body)
        self.headers = {"Content-Type": content_type} if content_type else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """A scripted urlopen. Each element is bytes to return or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}
        self.seen_headers: list[dict[str, str]] = []

    def __call__(self, request, timeout=None):
        self.calls.append(request.full_url)
        self.timeout = timeout
        self.headers = dict(request.headers)
        self.seen_headers.append(dict(request.headers))
        item = self.script.pop(0) if self.script else b""
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class Clock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


def build(common, opener, clock=None, **kwargs):
    clock = clock or Clock()
    return common.Http(opener=opener, sleep=clock.sleep, monotonic=clock.monotonic,
                       **kwargs), clock


# --- retry and backoff --------------------------------------------------

def test_a_retryable_status_is_retried_with_growing_backoff(common):
    error = urllib.error.HTTPError("u", 503, "busy", {}, None)
    opener = Opener(error, error, b"ok")
    http, clock = build(common, opener, backoff_base=1.0)

    assert http.get("https://www.courtlistener.com/x").body == b"ok"
    assert clock.slept == [1.0, 2.0], "backoff doubles"


def test_a_definite_answer_is_not_retried(common):
    """A 404 will not become a 200 by asking again, and retrying is rude."""
    opener = Opener(urllib.error.HTTPError("u", 404, "gone", {}, None))
    http, clock = build(common, opener)

    with pytest.raises(common.FetchError, match="HTTP 404"):
        http.get("https://www.courtlistener.com/x")
    assert clock.slept == []


def test_a_403_is_reported_as_such_so_fetch_can_call_it_a_paywall(common):
    opener = Opener(urllib.error.HTTPError("u", 403, "no", {}, None))
    http, _ = build(common, opener)

    with pytest.raises(common.FetchError, match="HTTP 403"):
        http.get("https://example.com/x")


def test_it_gives_up_after_the_attempt_limit(common):
    opener = Opener(*[urllib.error.HTTPError("u", 503, "busy", {}, None)] * 4)
    http, _ = build(common, opener, max_attempts=4)

    with pytest.raises(common.FetchError, match="gave up"):
        http.get("https://www.courtlistener.com/x")
    assert len(opener.calls) == 4


# --- throttling ---------------------------------------------------------

def test_a_second_call_to_one_host_waits_out_the_interval(common):
    http, clock = build(common, Opener(b"a", b"b"), min_interval=1.0)

    http.get("https://www.courtlistener.com/1")
    http.get("https://www.courtlistener.com/2")

    assert clock.slept == [1.0]


def test_two_hosts_do_not_wait_on_each_other(common):
    http, clock = build(common, Opener(b"a", b"b"), min_interval=1.0)

    http.get("https://www.courtlistener.com/1")
    http.get("https://www.ecfr.gov/2")

    assert clock.slept == []


def test_a_failing_request_still_holds_the_hosts_place_in_the_queue(common):
    """Stamping only on success sends dead links out back to back, which is the
    traffic pattern the etiquette section promises not to generate -- and it would
    happen only when a server was already unhappy."""
    opener = Opener(urllib.error.HTTPError("u", 404, "gone", {}, None), b"ok")
    http, clock = build(common, opener, min_interval=1.0)

    with pytest.raises(common.FetchError):
        http.get("https://www.courtlistener.com/1")
    http.get("https://www.courtlistener.com/2")

    assert clock.slept == [1.0]


# --- the token ----------------------------------------------------------

def test_the_token_goes_to_courtlistener(common):
    opener = Opener(b"ok")
    http, _ = build(common, opener, token="secret")

    http.get("https://www.courtlistener.com/api/rest/v4/search/")

    assert opener.headers.get("Authorization") == "Token secret"


def test_the_token_never_leaves_courtlistener(common):
    """A blanket Authorization header is a credential leak dressed as convenience."""
    opener = Opener(b"ok")
    http, _ = build(common, opener, token="secret")

    http.get("https://www.federalregister.gov/api/v1/documents.json")

    assert "Authorization" not in opener.headers


def test_a_lookalike_host_does_not_collect_the_token(common):
    """Suffix-matching the host, not substring-matching the URL."""
    opener = Opener(b"ok", b"ok")
    http, _ = build(common, opener, token="secret")

    http.get("https://www.courtlistener.com.evil.example/x")
    assert "Authorization" not in opener.seen_headers[0]

    http.get("https://evil.example/?next=https://www.courtlistener.com/")
    assert "Authorization" not in opener.seen_headers[1]


def test_a_courtlistener_subdomain_still_gets_the_token(common):
    opener = Opener(b"ok")
    http, _ = build(common, opener, token="secret")

    http.get("https://api.courtlistener.com/x")

    assert opener.headers.get("Authorization") == "Token secret"


def test_no_token_means_no_header_at_all(common):
    opener = Opener(b"ok")
    http, _ = build(common, opener)

    http.get("https://www.courtlistener.com/x")

    assert "Authorization" not in opener.headers


# --- robots -------------------------------------------------------------

def test_robots_is_honoured(common):
    opener = Opener(b"User-agent: *\nDisallow: /private/\n")
    http, _ = build(common, opener)

    assert not http.robots_allows("https://example.com/private/x")
    assert http.robots_allows("https://example.com/public/x")


def test_an_absent_robots_file_forbids_nothing(common):
    opener = Opener(urllib.error.HTTPError("u", 404, "gone", {}, None))
    http, _ = build(common, opener)

    assert http.robots_allows("https://example.com/anything")


def test_robots_is_fetched_once_per_host(common):
    opener = Opener(b"User-agent: *\nDisallow: /private/\n")
    http, _ = build(common, opener)

    http.robots_allows("https://example.com/a")
    http.robots_allows("https://example.com/b")

    assert len(opener.calls) == 1


# --- the reporter -------------------------------------------------------

def test_a_report_must_lead_with_its_headline(common):
    reporter = common.Reporter("x")
    reporter.row({"a": 1})

    with pytest.raises(ValueError, match="every report must lead with its summary"):
        reporter.emit()
