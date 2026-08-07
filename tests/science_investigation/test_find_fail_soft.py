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
    # Suppressing a more serious candidate because it was filtered out would be the
    # worse failure, so the headline still covers the tree — but it has to say so,
    # or it reads as describing rows that are not there.
    assert "0/empty" in payload["headline"]
    assert "--kind silent_cap" in payload["headline"]
    assert "covers the whole tree" in payload["headline"]


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


# ---- second review round ------------------------------------------------------ #

@pytest.mark.parametrize("name,body", [
    (".env", "MODEL=gpt-4\n"),
    ("config.yaml", "model: gpt-4\n"),
    ("call.py", 'client.complete(model="gpt-4")\n'),
])
def test_unpinned_models_are_caught_quoted_or_bare(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    assert kinds(run(tmp_path), "unpinned_model")


def test_a_pinned_model_stays_unflagged_in_config_too(tmp_path):
    (tmp_path / "config.yaml").write_text("model: gpt-4o-2024-08-06\n", encoding="utf-8")
    assert not kinds(run(tmp_path), "unpinned_model")


# ---- third review round ------------------------------------------------------- #

def test_the_word_rethrow_inside_a_string_does_not_excuse_a_zero(tmp_path):
    """Comments were stripped last round; string bodies reached the check too."""
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except Exception:\n"
        '        log("cannot rethrow here")\n'
        "        return 0.0\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "error_becomes_zero")


def test_a_genuine_reraise_is_still_recognized_after_string_stripping(tmp_path):
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except Exception:\n"
        '        log("giving up")\n'
        "        raise\n",
        encoding="utf-8",
    )
    assert run(tmp_path)["candidates"] == []


@pytest.mark.parametrize("body,kind", [
    ("const s = (r) => judge(r).catch(() => 0);\n", "error_becomes_zero"),
    ('const s = (r) => judge(r).catch(() => "");\n', "error_becomes_zero"),
    ("const s = (r) => judge(r).catch(() => null);\n", "swallowed_error"),
])
def test_promise_rejection_handlers_are_read_as_fail_soft(tmp_path, body, kind):
    (tmp_path / "m.ts").write_text(body, encoding="utf-8")
    assert kinds(run(tmp_path), kind)


def test_a_promise_catch_that_rethrows_is_not_flagged(tmp_path):
    (tmp_path / "m.ts").write_text(
        "const s = (r) => judge(r).catch((e) => { throw e; });\n", encoding="utf-8"
    )
    assert run(tmp_path)["candidates"] == []


@pytest.mark.parametrize("setting", ['{"temperature": 0.7}', '{"do_sample": true}'])
def test_json_quoted_sampling_keys_are_matched(tmp_path, setting):
    (tmp_path / "config.json").write_text(setting + "\n", encoding="utf-8")
    assert kinds(run(tmp_path), "nondeterminism")


@pytest.mark.parametrize("command", ["head -n 50 eval.jsonl > sub.jsonl", "head -50 eval.jsonl"])
def test_the_shell_head_cap_the_guide_documents_is_detected(tmp_path, command):
    (tmp_path / "run.sh").write_text(command + "\n", encoding="utf-8")
    assert kinds(run(tmp_path), "silent_cap")


# ---- fourth review round ------------------------------------------------------ #

def test_a_json_quoted_flag_key_still_reads_as_default_off(tmp_path):
    (tmp_path / "config.json").write_text('{"use_reranker": false}\n', encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag")


@pytest.mark.parametrize("line", ["disable_reranker: true", "skip_adaptive_cutoff: true"])
def test_a_negative_switch_defaulting_to_on_is_also_default_off(tmp_path, line):
    (tmp_path / "config.yaml").write_text(line + "\n", encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag")


def test_an_ordinary_true_setting_is_not_a_disabled_component(tmp_path):
    (tmp_path / "config.yaml").write_text("verbose: true\nstrict_mode: true\n", encoding="utf-8")
    assert kinds(run(tmp_path), "default_off_flag") == []


# ---- fifth review round ------------------------------------------------------- #

def test_a_conditional_reraise_does_not_hide_the_zero_path(tmp_path):
    """One class re-raised and every other failure scored is still fail-soft."""
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    try:\n"
        "        return judge(row)\n"
        "    except Exception as exc:\n"
        "        if isinstance(exc, FatalError):\n"
        "            raise\n"
        "        return 0.0\n",
        encoding="utf-8",
    )
    rows = kinds(run(tmp_path), "error_becomes_zero")
    assert rows and "another path re-raises" in rows[0]["detail"]


def test_a_multiline_promise_catch_body_is_followed(tmp_path):
    (tmp_path / "m.ts").write_text(
        "const s = (r) => judge(r).catch(() => {\n  return 0;\n});\n", encoding="utf-8"
    )
    assert len(kinds(run(tmp_path), "error_becomes_zero")) == 1


def test_a_multiline_promise_catch_that_rethrows_is_clean(tmp_path):
    (tmp_path / "m.ts").write_text(
        "const s = (r) => judge(r).catch((e) => {\n  throw e;\n});\n", encoding="utf-8"
    )
    assert run(tmp_path)["candidates"] == []


def test_sampling_explicitly_switched_off_is_not_nondeterminism(tmp_path):
    """`top_p: 1.0` is the neutral setting, and do_sample:false turns sampling off."""
    (tmp_path / "gen.yaml").write_text("top_p: 1.0\ndo_sample: false\n", encoding="utf-8")
    assert not kinds(run(tmp_path), "nondeterminism")


def test_a_real_sampling_setting_is_still_flagged(tmp_path):
    (tmp_path / "gen.yaml").write_text("top_p: 0.9\ntemperature: 0.7\n", encoding="utf-8")
    assert kinds(run(tmp_path), "nondeterminism")
