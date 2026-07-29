"""Tests for fix-pr's feedback fetcher.

The fetch layer shells out to `gh` and is a dozen lines; the normalize layer is
where every decision lives, so that is what these pin. The fixtures are trimmed
from real payloads captured off cli/cli#11451 and #13084 — the same shapes the
script was written against.
"""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "fix-pr" / "scripts"


@pytest.fixture
def fetcher(load_module):
    return load_module(SCRIPTS_DIR, "fetch_pr_feedback")


def thread(path="a.go", line=10, resolved=False, outdated=False, comments=None, original=None):
    return {
        "isResolved": resolved, "isOutdated": outdated, "path": path,
        "line": line, "originalLine": original if original is not None else line,
        "diffSide": "RIGHT",
        "comments": {"nodes": comments if comments is not None else [
            {"author": {"login": "reviewer"}, "body": "please fix",
             "createdAt": "2025-08-08T18:51:38Z", "url": "https://example/1"},
        ]},
    }


PR_VIEW = {
    "number": 11451, "title": "fix error for ErrReleaseNotFound", "state": "OPEN",
    "url": "https://github.com/cli/cli/pull/11451", "author": {"login": "ejahnGithub"},
    "headRefName": "eugene/fix", "baseRefName": "trunk", "isDraft": False,
    "reviewDecision": "CHANGES_REQUESTED",
    "reviews": [
        {"author": {"login": "bot"}, "state": "COMMENTED", "body": "## Overview\nlooks fine",
         "submittedAt": "2025-08-08T17:29:32Z"},
        {"author": {"login": "tingx2wang"}, "state": "APPROVED", "body": "",
         "submittedAt": "2025-08-08T18:00:00Z"},
        {"author": {"login": "drive-by"}, "state": "COMMENTED", "body": "",
         "submittedAt": "2025-08-08T18:10:00Z"},
    ],
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "govulncheck", "status": "COMPLETED",
         "conclusion": "FAILURE", "detailsUrl": "https://example/ci/1"},
        {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
         "conclusion": "SUCCESS", "detailsUrl": "https://example/ci/2"},
        {"__typename": "CheckRun", "name": "lint", "status": "IN_PROGRESS",
         "conclusion": None, "detailsUrl": "https://example/ci/3"},
    ],
}


# ---- threads ---------------------------------------------------------------- #

def test_suggestion_blocks_are_extracted_verbatim(fetcher):
    body = ("This masks all decoding errors.\n"
            '```suggestion\n\t\treturn "", fmt.Errorf("failed: %w", err)\n```')
    t = fetcher.normalize_thread(thread(comments=[
        {"author": {"login": "copilot"}, "body": body, "createdAt": "x", "url": "u"},
    ]))
    # A reviewer-authored patch is applyable as-is; keeping the exact text (tabs
    # included) is the whole point of surfacing it separately.
    assert t["suggestions"] == ['\t\treturn "", fmt.Errorf("failed: %w", err)\n']


def test_a_deleted_or_bot_author_does_not_crash_the_thread(fetcher):
    t = fetcher.normalize_thread(thread(comments=[
        {"author": None, "body": "ghost comment", "createdAt": "x", "url": "u"},
    ]))
    assert t["comments"][0]["author"] == "(unknown)"
    assert t["participants"] == ["(unknown)"]


def test_a_stale_thread_falls_back_to_the_line_the_reviewer_saw(fetcher):
    # GitHub nulls `line` once the diff moves; originalLine still locates the code.
    t = fetcher.normalize_thread(thread(line=None, original=168, outdated=True))
    assert t["line"] == 168
    assert t["outdated"] is True


def test_open_current_threads_sort_ahead_of_outdated_then_resolved(fetcher):
    raw = [
        thread(path="z.go", line=1, resolved=True),
        thread(path="b.go", line=5, outdated=True),
        thread(path="a.go", line=9),
        thread(path="a.go", line=2),
    ]
    report = fetcher.normalize(PR_VIEW, raw, include_all=True)
    assert [(t["path"], t["line"]) for t in report["threads"]] == [
        ("a.go", 2), ("a.go", 9),   # open + current, by file then line
        ("b.go", 5),                # open but on moved code
        ("z.go", 1),                # resolved, last
    ]


def test_resolved_threads_are_hidden_by_default_and_counted(fetcher):
    raw = [thread(path="a.go", resolved=True), thread(path="b.go")]

    default = fetcher.normalize(PR_VIEW, raw)
    assert [t["path"] for t in default["threads"]] == ["b.go"]
    assert default["summary"]["hidden_threads"] == 1
    assert default["summary"]["open_threads"] == 1
    assert default["summary"]["resolved_threads"] == 1

    everything = fetcher.normalize(PR_VIEW, raw, include_all=True)
    assert len(everything["threads"]) == 2
    assert everything["summary"]["hidden_threads"] == 0


def test_summary_counts_only_actionable_suggestions_and_stale_threads(fetcher):
    suggestion = [{"author": {"login": "r"}, "body": "```suggestion\nx\n```",
                   "createdAt": "x", "url": "u"}]
    raw = [
        thread(path="a.go", comments=suggestion),                    # open, suggestion
        thread(path="b.go", resolved=True, comments=suggestion),     # settled, ignore
        thread(path="c.go", outdated=True),                          # open on moved code
        thread(path="d.go", resolved=True, outdated=True),           # settled, ignore
    ]
    summary = fetcher.normalize(PR_VIEW, raw)["summary"]
    assert summary["threads_with_suggestions"] == 1
    assert summary["outdated_threads"] == 1
    assert summary["open_threads"] == 2


# ---- checks ----------------------------------------------------------------- #

def test_checks_split_into_failing_pending_and_passing(fetcher):
    checks = fetcher.normalize_checks(PR_VIEW["statusCheckRollup"])
    assert [c["name"] for c in checks["failing"]] == ["govulncheck"]
    assert [c["name"] for c in checks["pending"]] == ["lint"]
    assert checks["passing_count"] == 1


def test_legacy_status_contexts_are_classified_too(fetcher):
    # The older StatusContext type has `state`/`context` where CheckRun has
    # `conclusion`/`name`; a PR can carry both at once.
    checks = fetcher.normalize_checks([
        {"__typename": "StatusContext", "context": "ci/travis", "state": "FAILURE",
         "targetUrl": "https://example/t"},
        {"__typename": "StatusContext", "context": "ci/coverage", "state": "SUCCESS"},
        {"__typename": "StatusContext", "context": "ci/deploy", "state": "PENDING"},
    ])
    assert [c["name"] for c in checks["failing"]] == ["ci/travis"]
    assert checks["failing"][0]["url"] == "https://example/t"
    assert [c["name"] for c in checks["pending"]] == ["ci/deploy"]
    assert checks["passing_count"] == 1


def test_no_checks_at_all_is_not_a_failure(fetcher):
    assert fetcher.normalize_checks(None) == {"failing": [], "pending": [], "passing_count": 0}


# ---- whole report ----------------------------------------------------------- #

def test_reviews_keep_verdicts_and_bodies_but_drop_empty_drive_bys(fetcher):
    reviews = fetcher.normalize(PR_VIEW, [])["reviews"]
    # An APPROVED with no body is a verdict worth showing; a COMMENTED with no
    # body is noise from a reaction or a resolved thread.
    assert [(r["author"], r["state"]) for r in reviews] == [
        ("bot", "COMMENTED"), ("tingx2wang", "APPROVED"),
    ]


def test_pr_header_carries_what_a_reviewer_needs_to_orient(fetcher):
    pr = fetcher.normalize(PR_VIEW, [])["pr"]
    assert pr["number"] == 11451
    assert pr["branch"] == "eugene/fix"
    assert pr["base"] == "trunk"
    assert pr["decision"] == "CHANGES_REQUESTED"
    assert pr["author"] == "ejahnGithub"
    assert pr["draft"] is False


def test_report_renders_and_is_json_serializable(fetcher):
    report = fetcher.normalize(PR_VIEW, [thread(), thread(path="b.go", resolved=True)])
    json.dumps(report)  # must survive --format json
    text = fetcher.render(report)
    assert "PR #11451" in text
    assert "govulncheck" in text        # failing checks are surfaced
    assert "1 resolved thread(s) hidden" in text
