"""Tests for science-investigation's fail-soft scanner.

The distinction it has to keep is between an error that was *recorded* and one
that was absorbed: a handler that logs and re-raises is fine, a handler that
returns 0.0 makes a timeout and a bad answer the same number. Half of these tests
pin the "fine" side, because a scanner that flags every `except` is one nobody runs.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts" / "find_fail_soft.py"


def run(root, *args) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--format", "json", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout)


def kinds(payload, kind) -> list:
    return [row for row in payload["candidates"] if row["kind"] == kind]


# ---- error handling ---------------------------------------------------------- #

def test_a_handler_that_returns_zero_is_the_headline(tmp_path):
    (tmp_path / "score.py").write_text(
        "def score(row, client):\n"
        "    try:\n"
        "        return float(client.judge(row))\n"
        "    except Exception:\n"
        "        return 0.0\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "error_becomes_zero")
    assert "0/empty" in payload["headline"]


def test_a_handler_that_re_raises_is_not_flagged(tmp_path):
    (tmp_path / "score.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return compute(row)\n"
        "    except ValueError as exc:\n"
        "        log.warning('bad row')\n"
        "        raise RuntimeError('bad row') from exc\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "error_becomes_zero")
    assert not kinds(payload, "swallowed_error")


def test_a_handler_that_records_the_error_is_not_flagged(tmp_path):
    """Recording per-example error state is the good practice; do not punish it."""
    (tmp_path / "score.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return compute(row)\n"
        "    except TimeoutError as exc:\n"
        "        results.append({'score': None, 'error': str(exc)})\n",
        encoding="utf-8",
    )
    assert run(tmp_path)["candidates"] == []


def test_a_pass_only_handler_is_flagged_as_swallowed(tmp_path):
    (tmp_path / "run.py").write_text(
        "for row in rows:\n"
        "    try:\n"
        "        collect(row)\n"
        "    except Exception:\n"
        "        continue\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "swallowed_error")


def test_a_javascript_empty_catch_is_flagged(tmp_path):
    (tmp_path / "score.ts").write_text(
        "async function score(row) {\n"
        "  try {\n"
        "    return await judge(row);\n"
        "  } catch (e) {\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "swallowed_error")


def test_a_one_line_javascript_catch_returning_zero_is_flagged(tmp_path):
    (tmp_path / "score.ts").write_text(
        "const score = async (row) => {\n"
        "  try { return await judge(row); } catch (e) { return 0; }\n"
        "};\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "error_becomes_zero")


# ---- flags, caps, sampling, models ------------------------------------------- #

def test_a_component_defaulting_to_off_is_flagged_in_code_and_config(tmp_path):
    (tmp_path / "config.yaml").write_text("enable_reranker: false\nuse_adaptive_cutoff: false\n", encoding="utf-8")
    (tmp_path / "settings.py").write_text("USE_LEARNED_RERANKER = False\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "default_off_flag")
    assert len({row["file"] for row in rows}) == 2


def test_ordinary_zero_valued_settings_are_not_read_as_disabled_components(tmp_path):
    (tmp_path / "config.yaml").write_text("connection_timeout: 0\nretry_count: 0\n", encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag") == []


def test_a_cap_in_the_eval_loop_is_flagged(tmp_path):
    (tmp_path / "run.py").write_text("results = [score(r) for r in rows[:100]]\n", encoding="utf-8")
    assert kinds(run(tmp_path), "silent_cap")


def test_sampling_settings_are_flagged_but_zero_temperature_is_not(tmp_path):
    (tmp_path / "hot.py").write_text("resp = client.complete(prompt, temperature=0.7)\n", encoding="utf-8")
    (tmp_path / "cold.py").write_text("resp = client.complete(prompt, temperature=0)\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "nondeterminism")
    assert [row["file"] for row in rows] == ["hot.py"]


@pytest.mark.parametrize("model,flagged", [
    ("gpt-4o", True),
    ("claude-sonnet", True),
    ("gpt-4o-2024-08-06", False),
    ("claude-3-5-sonnet-20241022", False),
])
def test_only_unpinned_model_ids_are_flagged(tmp_path, model, flagged):
    (tmp_path / "call.py").write_text('resp = client.complete(model="' + model + '")\n', encoding="utf-8")
    assert bool(kinds(run(tmp_path), "unpinned_model")) is flagged


# ---- shape and hygiene -------------------------------------------------------- #

def test_every_row_is_a_candidate_with_a_confirm_step(tmp_path):
    (tmp_path / "run.py").write_text("rows = rows[:50]\n", encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"]
    for row in payload["candidates"]:
        assert row["status"] == "candidate"
        assert row["confirm"].strip()


def test_kind_filter_narrows_rows_without_rewriting_the_headline(tmp_path):
    (tmp_path / "run.py").write_text(
        "rows = rows[:50]\n"
        "def score(r):\n"
        "    try:\n"
        "        return judge(r)\n"
        "    except Exception:\n"
        "        return 0.0\n",
        encoding="utf-8",
    )
    payload = run(tmp_path, "--kind", "silent_cap")
    assert {row["kind"] for row in payload["candidates"]} == {"silent_cap"}
    assert "0/empty" in payload["headline"], "the headline reports the tree, not the filter"


def test_a_clean_tree_is_reported_without_claiming_it_is_clean(tmp_path):
    (tmp_path / "s.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"] == []
    assert "Read the caveat" in payload["headline"]


# ---- review fixes ------------------------------------------------------------ #

def test_a_handler_returning_a_real_low_score_is_not_a_zero(tmp_path):
    """`return 0.75` once matched the bare `0` alternative — a false headline."""
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except ValueError:\n"
        "        return 0.75\n",
        encoding="utf-8",
    )
    assert not kinds(run(tmp_path), "error_becomes_zero")


def test_a_handler_returning_an_empty_string_is_a_zero(tmp_path):
    """The documented empty-result case; a word boundary cannot follow a quote."""
    (tmp_path / "s.py").write_text(
        "def answer(row):\n"
        "    try:\n"
        "        return generate(row)\n"
        "    except TimeoutError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "error_becomes_zero")


def test_returning_none_is_a_swallowed_error_not_a_zero(tmp_path):
    """null-for-unmeasurable is the shape this skill recommends; don't call it a zero."""
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except TimeoutError:\n"
        "        return None\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "error_becomes_zero")
    assert kinds(payload, "swallowed_error")


def test_a_comment_mentioning_rethrow_does_not_excuse_a_swallowed_error(tmp_path):
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except Exception:\n"
        "        # cannot rethrow here, the batch would die\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "swallowed_error")


@pytest.mark.parametrize("line", [
    "ENABLE_RERANKER: bool = False",
    "const useReranker: boolean = false",
    "let featureAdaptiveCutoff: boolean = false",
])
def test_annotated_declarations_still_read_as_default_off(tmp_path, line):
    (tmp_path / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag")


@pytest.mark.parametrize("line", [
    "rows = random.sample(rows, 50)",
    "rows = random.choices(rows, k=50)",
    "rows = df.sample(n=50)",
])
def test_the_real_python_sampling_signatures_are_caught(tmp_path, line):
    (tmp_path / "run.py").write_text(line + "\n", encoding="utf-8")
    assert kinds(run(tmp_path), "silent_cap")


@pytest.mark.parametrize("model", ["gpt-4-0613", "gpt-4-1106-preview"])
def test_dated_provider_snapshots_count_as_pinned(tmp_path, model):
    (tmp_path / "call.py").write_text('client.complete(model="' + model + '")\n', encoding="utf-8")
    assert not kinds(run(tmp_path), "unpinned_model")


def test_dot_env_files_are_scanned(tmp_path):
    (tmp_path / ".env").write_text("ENABLE_RERANKER=false\n", encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag")


def test_a_file_too_large_to_read_is_reported_as_unread(tmp_path):
    (tmp_path / "huge.py").write_text("# padding\n" * 250_000, encoding="utf-8")
    payload = run(tmp_path)
    assert payload["counts"]["files_skipped_unread"] == 1
    assert "NOT read" in payload["headline"]
