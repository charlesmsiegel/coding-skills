"""The one seam that touches other people's infrastructure.

Acquisition etiquette is a promise the SKILL.md makes to arXiv, OpenAlex and
whoever else the corpus is pulled from, and a promise nothing checks is a
comment. `Http` takes its opener, sleep and clock as parameters precisely so the
promise can be tested without a socket or a real second of wall-clock, so these
tests assert on what it *waited* for, not only on what it returned.
"""

import io
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


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

    def __call__(self, request, timeout=None):
        self.calls.append(request.full_url)
        self.timeout = timeout
        self.headers = dict(request.headers)
        item = self.script.pop(0) if self.script else b""
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return FakeResponse(*item)
        return FakeResponse(item)


class Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def http(common, opener, clock=None, **kwargs):
    clock = clock or Clock()
    return common.Http(opener=opener, sleep=clock.sleep, monotonic=clock.monotonic, **kwargs), clock


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.org/x", code, "boom", {}, None)


# --- retries -------------------------------------------------------------

def test_a_retryable_status_is_retried_with_exponential_backoff(common):
    client, clock = http(common, Opener(http_error(429), http_error(503), b"ok"), backoff_base=1.0)

    assert client.get("https://example.org/x").body == b"ok"
    assert clock.slept == [1.0, 2.0], "backoff must grow; a fixed retry is what gets a client blocked"


def test_a_definite_answer_is_not_retried(common):
    """A 404 will not become a 200 by asking four times, and asking is rude."""
    opener = Opener(http_error(404), b"ok")
    client, clock = http(common, opener)

    with pytest.raises(common.FetchError, match="HTTP 404"):
        client.get("https://example.org/x")
    assert len(opener.calls) == 1
    assert clock.slept == []


def test_a_transport_error_is_retried_then_given_up_on(common):
    opener = Opener(*[urllib.error.URLError("dns")] * 4)
    client, _ = http(common, opener, max_attempts=4)

    with pytest.raises(common.FetchError, match="gave up"):
        client.get("https://example.org/x")
    assert len(opener.calls) == 4, "max_attempts is the number of attempts, not of retries"


def test_the_last_attempt_is_not_followed_by_a_pointless_sleep(common):
    client, clock = http(common, Opener(*[http_error(503)] * 3), max_attempts=3, backoff_base=1.0)

    with pytest.raises(common.FetchError):
        client.get("https://example.org/x")
    assert clock.slept == [1.0, 2.0]


# --- throttling ----------------------------------------------------------

def test_a_second_call_to_one_host_waits_for_the_interval(common):
    client, clock = http(common, Opener(b"a", b"b"), min_interval=1.5)

    client.get("https://export.arxiv.org/a")
    client.get("https://export.arxiv.org/b")

    assert clock.slept == [1.5]


def test_a_different_host_is_not_made_to_wait(common):
    """Politeness is per-host; one slow source must not serialize the whole run."""
    client, clock = http(common, Opener(b"a", b"b"), min_interval=1.5)

    client.get("https://export.arxiv.org/a")
    client.get("https://api.openalex.org/b")

    assert clock.slept == []


def test_time_already_spent_counts_against_the_interval(common):
    client, clock = http(common, Opener(b"a", b"b"), min_interval=2.0)

    client.get("https://export.arxiv.org/a")
    clock.now += 1.25
    client.get("https://export.arxiv.org/b")

    assert clock.slept == [pytest.approx(0.75)]


def test_a_failed_call_still_holds_the_hosts_place_in_the_queue(common):
    """A dead link is a request the server saw, so it counts against the interval."""
    client, clock = http(common, Opener(http_error(404), http_error(404)), min_interval=1.0)

    for path in ("a", "b"):
        with pytest.raises(common.FetchError):
            client.get("https://export.arxiv.org/" + path)

    assert clock.slept == [1.0]


def test_a_host_answering_nothing_but_errors_is_still_throttled(common):
    """Stamping the clock only on success meant thirty dead links went out back to
    back with no interval at all — the exact traffic pattern that gets a client
    banned, generated only when a server was already unhappy."""
    client, clock = http(common, Opener(*[http_error(404)] * 30), min_interval=1.0)

    for index in range(30):
        with pytest.raises(common.FetchError):
            client.get("https://dead-mirror.example.org/paper%d.pdf" % index)

    assert clock.now == pytest.approx(29.0), "29 gaps between 30 requests to one host"


# --- request shape -------------------------------------------------------

def test_the_user_agent_identifies_the_tool(common):
    """An anonymous scraper is what rate-limit bans are written for."""
    opener = Opener(b"ok")
    client, _ = http(common, opener)

    client.get("https://example.org/x")

    assert "literature-survey" in opener.headers.get("User-agent", "")


def test_the_content_type_is_carried_through_to_the_manifest(common):
    client, _ = http(common, Opener((b"%PDF-1.4", "application/pdf")))

    assert client.get("https://example.org/x").content_type == "application/pdf"


def test_a_response_without_headers_does_not_crash_the_fetch(common):
    class Bare(io.BytesIO):
        headers = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    client, _ = http(common, lambda request, timeout=None: Bare(b"body"))

    assert client.get("https://example.org/x").content_type == ""


def test_get_json_decodes_the_body(common):
    client, _ = http(common, Opener(b'{"results": [1, 2]}'))

    assert client.get_json("https://api.openalex.org/works") == {"results": [1, 2]}


# --- robots --------------------------------------------------------------

def test_a_disallowed_path_is_refused(common):
    client, _ = http(common, Opener(b"User-agent: *\nDisallow: /private/\n"))

    assert client.robots_allows("https://example.org/private/thread") is False


def test_an_allowed_path_passes(common):
    client, _ = http(common, Opener(b"User-agent: *\nDisallow: /private/\n"))

    assert client.robots_allows("https://example.org/public/thread") is True


def test_absent_robots_rules_mean_permission(common):
    """A 404 on robots.txt is not a prohibition, and must not silently empty the corpus."""
    client, _ = http(common, Opener(http_error(404)))

    assert client.robots_allows("https://example.org/anything") is True


def test_robots_is_fetched_once_per_host(common):
    opener = Opener(b"User-agent: *\nDisallow: /private/\n")
    client, _ = http(common, opener)

    client.robots_allows("https://example.org/a")
    client.robots_allows("https://example.org/b")

    assert opener.calls == ["https://example.org/robots.txt"]
