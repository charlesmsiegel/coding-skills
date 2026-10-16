"""False positives removed from the correctness and security detectors.

Each rule here was written against a real library's report, where the detector
charged a correctness or security defect to code that had none: an explicit
`return None`, a pydantic field default, an `example.com` fixture, a DDL
statement whose identifier cannot be bound. Every test that pins a removed false
positive sits next to one that pins the true positive the same rule must keep
catching — a fix that quietened both would pass the first test alone.

Fixtures are written to tmp_path at runtime, as in test_detectors.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def records(script: str, tmp_path: Path, source: str, smell: str, name: str = "sample.py") -> list[dict]:
    """The records of one smell type for one fixture file."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return [r for r in run_detector(script, path) if r["smell_type"] == smell]


def findings(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind", "finding") == "finding"]


def candidates(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind") == "candidate"]


def assert_candidate_shape(record: dict) -> None:
    """A candidate names its benign readings and carries no fix."""
    assert record["kind"] == "candidate"
    assert record["also_caused_by"], "a candidate names how healthy code produces it"
    assert not record["suggestion"], "a candidate carries what to confirm, not a fix"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


# ---- common.reasoned_noqa / looks_like_test ------------------------------- #


@pytest.mark.parametrize("line", [
    "except Exception:  # noqa: BLE001 -- best-effort cache warm",
    "except Exception:  # noqa: BLE001 - bookkeeping never fails a call",
    "except Exception:  # noqa: BLE001 \u2014 a disconnected client can't be told",
    "except Exception:  # noqa: BLE001: see the docstring",
    "except Exception:  # noqa: S110, BLE001 -- both, for one reason",
])
def test_a_noqa_with_a_reason_is_reasoned(common, line):
    assert common.reasoned_noqa([line], 1, "BLE001")


@pytest.mark.parametrize("line", [
    "except Exception:  # noqa: BLE001",
    "except Exception:  # noqa: BLE001 --",
    "except Exception:  # noqa: E722 -- a different rule's reason",
    "except Exception:  # best-effort, BLE001 is not even named in a noqa",
])
def test_a_bare_or_foreign_noqa_is_not_reasoned(common, line):
    assert not common.reasoned_noqa([line], 1, "BLE001")


def test_a_reasoned_noqa_may_sit_on_the_comment_line_below(common):
    lines = ["except Exception:", "    # noqa: BLE001 -- the caller reads None as unreadable", "    pass"]
    assert common.reasoned_noqa(lines, 1, "BLE001")
    # ...but not on a code line below: that is a different statement's suppression.
    lines = ["except Exception:", "    pass  # noqa: BLE001 -- a later line", ""]
    assert not common.reasoned_noqa(lines, 1, "BLE001")


@pytest.mark.parametrize("path, expected", [
    ("pkg/tests/helpers.py", True),
    ("pkg/test_thing.py", True),
    ("pkg/thing_test.py", True),
    ("pkg/conftest.py", True),
    ("pkg/testing.py", False),
    ("pkg/contest/entry.py", False),
])
def test_looks_like_test(common, path, expected):
    assert common.looks_like_test(path) is expected


# ---- inconsistent_returns -------------------------------------------------- #


def test_explicit_return_none_beside_a_value_is_consistent(tmp_path):
    """PEP 8 asks for exactly this spelling: no finding, at any severity."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x) -> int | None:\n"
                    "    if x:\n"
                    "        return None\n"
                    "    return 1\n",
                    "inconsistent_returns")
    assert found == []


def test_a_nested_functions_returns_are_not_charged_to_its_parent(tmp_path):
    """A `def` directly in the body is its own scope; its `return 1` is not the outer's."""
    found = records("find_return_issues.py", tmp_path,
                    "def outer(c):\n"
                    "    def inner():\n"
                    "        return 1\n"
                    "    if c:\n"
                    "        return\n"
                    "    inner()\n",
                    "inconsistent_returns")
    assert found == []


def test_a_bare_return_beside_a_value_is_still_flagged(tmp_path):
    """RET502: the true positive the explicit-None rule must not swallow."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x):\n"
                    "    if x:\n"
                    "        return\n"
                    "    return 1\n",
                    "inconsistent_returns")
    assert len(findings(found)) == 1
    assert "bare 'return'" in found[0]["description"]


def test_falling_off_the_end_after_a_value_is_flagged(tmp_path):
    """RET503: one path returns 1, the other runs off the end and returns None."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x):\n"
                    "    if x:\n"
                    "        return 1\n",
                    "inconsistent_returns")
    assert len(findings(found)) == 1
    assert "end of its body" in found[0]["description"]
    assert found[0]["severity"] == "medium"


@pytest.mark.parametrize("body", [
    # every branch returns
    "    if x:\n        return 1\n    else:\n        return 2\n",
    # a with block that returns
    "    with x:\n        return 1\n",
    # a handler that raises
    "    try:\n        return int(x)\n    except ValueError:\n        raise\n",
    # an infinite loop left only by returning
    "    while True:\n        if x:\n            return 1\n",
    # a never-returning call
    "    if x:\n        return 1\n    sys.exit(2)\n",
    # a finally that returns
    "    try:\n        pass\n    finally:\n        return 1\n",
    # a method annotated NoReturn, called through self
    "    if x:\n        return 1\n    self._abort()\n",
])
def test_bodies_that_cannot_fall_off_are_not_flagged(tmp_path, body):
    source = ("import sys\n"
              "from typing import NoReturn\n"
              "class C:\n"
              "    def _abort(self) -> NoReturn:\n"
              "        raise SystemExit\n"
              "def f(x, self=None):\n" + body)
    assert records("find_return_issues.py", tmp_path, source, "inconsistent_returns") == []


@pytest.mark.parametrize("body", [
    # a handler that logs and carries on
    "    try:\n        return int(x)\n    except ValueError:\n        print('bad')\n",
    # a while True with its own break
    "    while True:\n        if x:\n            return 1\n        break\n",
    # an elif chain with no else
    "    if x > 0:\n        return 1\n    elif x < 0:\n        return -1\n",
    # a for loop over a possibly empty iterable
    "    for y in x:\n        return y\n",
])
def test_bodies_that_can_fall_off_are_flagged(tmp_path, body):
    found = records("find_return_issues.py", tmp_path, "def f(x):\n" + body, "inconsistent_returns")
    assert len(findings(found)) == 1
