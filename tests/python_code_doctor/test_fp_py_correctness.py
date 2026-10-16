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
