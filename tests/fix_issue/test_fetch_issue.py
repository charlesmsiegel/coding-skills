"""Tests for fix-issue's issue fetcher.

The fetch layer shells out to `gh`; the normalize and extraction layers hold every
decision, so those are what these pin. Payload shapes are trimmed from real
`gh issue view --json` and cross-reference timeline output captured off cli/cli.
"""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "fix-issue" / "scripts"


@pytest.fixture
def fetcher(load_module):
    return load_module(SCRIPTS_DIR, "fetch_issue")


ISSUE = {
    "number": 13972,
    "title": "`gh run list` reports a run as queued while its jobs are running",
    "body": "Repro:\n```bash\ngh run list --limit 1\n```\nBroken since `pkg/cmd/run/list.go` changed.",
    "state": "OPEN", "stateReason": None,
    "url": "https://github.com/cli/cli/issues/13972",
    "author": {"login": "reporter"}, "createdAt": "2026-07-20T00:00:00Z",
    "labels": [{"name": "bug"}, {"name": "needs-triage"}],
    "assignees": [{"login": "maintainer"}],
    "milestone": {"title": "v2.76"},
    "comments": [
        {"author": {"login": "helper"}, "authorAssociation": "MEMBER",
         "body": "Same here:\n```\nTraceback (most recent call last):\n  File \"api/client.py\", line 9\nValueError: nope\n```",
         "createdAt": "2026-07-21T00:00:00Z", "url": "https://example/c1"},
    ],
}


# ---- reproduction leads ---------------------------------------------------- #

def test_code_blocks_are_extracted_with_their_language(fetcher):
    leads = fetcher.extract_leads(["```python\nprint(1)\n```\ntext\n```\nbare\n```"])
    assert [(b["language"], b["content"]) for b in leads["code_blocks"]] == [
        ("python", "print(1)\n"), (None, "bare\n"),
    ]


@pytest.mark.parametrize("snippet", [
    "Traceback (most recent call last):\n  File \"x.py\", line 1\n",
    "java.lang.NullPointerException\n\tat com.example.Main.run(Main.java:42)\n",
    "panic: runtime error: index out of range\n",
    "stack backtrace:\n   0: rust_begin_unwind\n",
])
def test_stack_traces_are_flagged_across_languages(fetcher, snippet):
    leads = fetcher.extract_leads([f"```\n{snippet}```"])
    assert len(leads["tracebacks"]) == 1
    assert leads["tracebacks"][0]["looks_like_traceback"] is True


def test_ordinary_code_is_not_mistaken_for_a_stack_trace(fetcher):
    leads = fetcher.extract_leads(["```bash\ngh run list --limit 1\n```"])
    assert leads["tracebacks"] == []
    assert len(leads["code_blocks"]) == 1


def test_file_paths_are_collected_deduped_and_sorted(fetcher):
    leads = fetcher.extract_leads([
        "see pkg/cmd/run/list.go and src/api/client.py",
        "also pkg/cmd/run/list.go again",
    ])
    assert leads["mentioned_paths"] == ["pkg/cmd/run/list.go", "src/api/client.py"]


def test_path_extraction_stays_narrow_enough_to_trust(fetcher):
    # A loose pattern turns every URL, version, and prose sentence into a "lead".
    # A bare filename is also excluded: without a directory it is not locatable.
    leads = fetcher.extract_leads([
        "on v2.75.1 at https://github.com/cli/cli/issues/1 see docs/readme.md "
        "and photo assets/logo.png and main.go by itself",
    ])
    assert leads["mentioned_paths"] == []


def test_leads_survive_empty_and_missing_text(fetcher):
    leads = fetcher.extract_leads([None, "", "no code here"])
    assert leads == {"code_blocks": [], "tracebacks": [], "mentioned_paths": []}


# ---- related work ----------------------------------------------------------- #

def test_a_pr_that_closes_the_issue_sorts_first(fetcher):
    timeline = [
        {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
         "source": {"__typename": "Issue", "number": 100, "title": "dupe",
                    "state": "OPEN", "url": "https://example/i100"}},
        {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
         "source": {"__typename": "PullRequest", "number": 200, "title": "mention",
                    "state": "CLOSED", "url": "https://example/p200"}},
        {"__typename": "ConnectedEvent",
         "subject": {"__typename": "PullRequest", "number": 300, "title": "the fix",
                     "state": "OPEN", "url": "https://example/p300"}},
    ]
    related = fetcher.normalize_related(timeline)
    assert [(r["number"], r["closes"]) for r in related] == [
        (300, True),    # explicitly closes this issue — read it first
        (200, False),   # a PR that merely mentions it
        (100, False),   # another issue
    ]


def test_the_same_reference_mentioned_twice_appears_once(fetcher):
    ref = {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
           "source": {"__typename": "PullRequest", "number": 7, "title": "t",
                      "state": "OPEN", "url": "u"}}
    assert len(fetcher.normalize_related([ref, dict(ref)])) == 1


def test_unresolvable_timeline_entries_are_skipped(fetcher):
    # A cross-reference from a private repo comes back with an empty source.
    assert fetcher.normalize_related([
        {"__typename": "CrossReferencedEvent", "source": {}},
        {"__typename": "CrossReferencedEvent", "source": None},
        {"__typename": "SomethingElse"},
    ]) == []


# ---- whole report ----------------------------------------------------------- #

def test_report_carries_the_issue_metadata_a_fixer_needs(fetcher):
    issue = fetcher.normalize(ISSUE, [])["issue"]
    assert issue["labels"] == ["bug", "needs-triage"]
    assert issue["assignees"] == ["maintainer"]
    assert issue["milestone"] == "v2.76"
    assert issue["state"] == "OPEN"


def test_leads_are_gathered_from_comments_as_well_as_the_body(fetcher):
    report = fetcher.normalize(ISSUE, [])
    # The repro command is in the body; the stack trace is in a comment.
    assert len(report["leads"]["code_blocks"]) == 2
    assert len(report["leads"]["tracebacks"]) == 1
    assert report["leads"]["mentioned_paths"] == ["api/client.py", "pkg/cmd/run/list.go"]


def test_a_missing_author_never_breaks_the_report(fetcher):
    ghost = {**ISSUE, "author": None,
             "comments": [{"author": None, "body": "x", "createdAt": "z", "url": "u"}]}
    report = fetcher.normalize(ghost, [])
    assert report["issue"]["author"] == "(unknown)"
    assert report["comments"][0]["author"] == "(unknown)"


def test_report_renders_and_is_json_serializable(fetcher):
    report = fetcher.normalize(ISSUE, [])
    json.dumps(report)
    text = fetcher.render(report)
    assert "#13972" in text
    assert "untrusted user input" in text  # the prompt-injection warning always ships


def test_a_closed_issue_is_called_out_before_anyone_starts_work(fetcher):
    closed = {**ISSUE, "state": "CLOSED", "stateReason": "COMPLETED"}
    text = fetcher.render(fetcher.normalize(closed, []))
    assert "already closed" in text
