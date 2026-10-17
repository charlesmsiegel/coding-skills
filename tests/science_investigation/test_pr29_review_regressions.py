"""Review controls for suppression and long-line coverage boundaries."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts"


def run(script, root, *prefix, timeout=30):
    result = subprocess.run([sys.executable, str(SCRIPTS / script), *prefix, str(root),
                             "--format", "json"], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_indexed_collection_slice_stays_a_cap_candidate(tmp_path):
    (tmp_path / "eval.py").write_text("rows = [[1, 2, 3, 4]]\nexamples = rows[0][:2]\n", encoding="utf-8")
    assert any(r["kind"] == "silent_cap" for r in run("find_fail_soft.py", tmp_path)["candidates"])


def test_printing_a_binary_metric_does_not_hide_its_zero_fallback(tmp_path):
    (tmp_path / "score.py").write_text(
        "def accuracy(rows):\n    print(len(rows))\n    if not rows:\n        return 0\n    return 1\n",
        encoding="utf-8")
    assert any(r["kind"] == "zero_default" for r in run("find_metrics.py", tmp_path)["candidates"])


def test_long_handwritten_trace_lines_have_bounded_honest_coverage(tmp_path):
    (tmp_path / "score.py").write_text(
        "answer = " + " + ".join(["e"] * 16000) + "\n" + "# pad\n" * 256, encoding="utf-8")
    payload = run("trace_value.py", tmp_path, "e", timeout=10)
    assert payload["counts"]["files_skipped_generated"] == 0
    assert payload["counts"]["lines_truncated"] == 1
    assert "10000" in payload["caveat"] and "not covered" in payload["caveat"]
