"""An import whose ImportError the code handles is an optional dependency, not a missing one.

Found by running the doctor over anima-ui, whose detailer imports `ultralytics`
only inside `try: ... except ImportError:` and degrades with a message when it is
absent. The dependency detector charged that as a high `missing_dependency`.
Each test here pins the guarded shape beside the shape that must still fire, so a
fix cannot pass by switching the check off.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"

MANIFEST = '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'

# anima-ui's src/anima_ui/detectors.py, cut down to the two guarded imports.
DETECTORS = (
    "def library_available() -> bool:\n"
    "    try:\n"
    "        import ultralytics  # noqa: F401\n"
    "    except ImportError:\n"
    "        return False\n"
    "    return True\n"
    "\n"
    "\n"
    "def detect(detector, image, confidence):\n"
    "    try:\n"
    "        from ultralytics import YOLO\n"
    "    except ImportError as error:\n"
    "        raise RuntimeError(\n"
    "            f\"detector {detector!r} needs the `ultralytics` package, which is not installed\"\n"
    "        ) from error\n"
    "    return YOLO(str(detector)).predict(image, conf=confidence)\n"
)


def run_detector(script: str, target: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def missing(root: Path) -> list[str]:
    return sorted(f["description"].split("'")[1] for f in run_detector("find_dependency_issues.py", root)
                  if f["smell_type"] == "missing_dependency")


def test_an_import_guarded_by_except_importerror_is_not_a_missing_dependency(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/detectors.py", DETECTORS)
    write(tmp_path, "src/app/cli.py", "def serve():\n    import anyio\n    return anyio\n")

    assert missing(tmp_path) == ["anyio"], "the unguarded import still fires; the guarded one does not"


def test_every_spelling_of_the_guard_counts(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "speed.py",
          "try:\n"
          "    import ujson as json\n"
          "except ModuleNotFoundError:\n"
          "    import json\n"
          "try:\n"
          "    import yaml\n"
          "except (OSError, ImportError):\n"
          "    yaml = None\n"
          "try:\n"
          "    if True:\n"
          "        import orjson\n"
          "except builtins.ImportError:\n"
          "    orjson = None\n"
          "try:\n"
          "    import msgpack\n"
          "except* ImportError:\n"
          "    msgpack = None\n")

    assert missing(tmp_path) == []


def test_what_the_guard_does_not_cover_still_fires(tmp_path):
    """A broad handler says nothing about the import being optional; the handler,
    else and finally run outside the guard; a def in the guarded body imports when
    it is called, long after the try has finished."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "app.py",
          "try:\n"
          "    import requests\n"
          "except Exception:\n"
          "    requests = None\n"
          "try:\n"
          "    import fast_backend\n"
          "except:\n"
          "    fast_backend = None\n"
          "try:\n"
          "    import simdjson\n"
          "except ImportError:\n"
          "    import fallback_json\n"
          "else:\n"
          "    import else_dep\n"
          "finally:\n"
          "    import finally_dep\n"
          "try:\n"
          "    def later():\n"
          "        import deferred_dep\n"
          "        return deferred_dep\n"
          "except ImportError:\n"
          "    later = None\n")

    assert missing(tmp_path) == ["deferred_dep", "else_dep", "fallback_json", "fast_backend",
                                 "finally_dep", "requests"]


def test_one_unguarded_import_makes_the_dependency_required(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "detectors.py", DETECTORS)
    write(tmp_path, "train.py", "from ultralytics import YOLO\n\nMODEL = YOLO\n")

    records = [f for f in run_detector("find_dependency_issues.py", tmp_path)
               if f["smell_type"] == "missing_dependency"]
    assert len(records) == 1 and records[0]["severity"] == "high"
    assert "train.py" in records[0]["description"], "the example must be the import that requires it"


def test_a_declared_optional_dependency_imported_only_under_the_guard_is_used(tmp_path):
    write(tmp_path, "pyproject.toml",
          MANIFEST + '[project.optional-dependencies]\ndetect = ["ultralytics>=8"]\n')
    write(tmp_path, "detectors.py", DETECTORS)

    assert run_detector("find_dependency_issues.py", tmp_path) == []


def test_no_manifest_is_not_charged_for_optional_imports_alone(tmp_path):
    write(tmp_path, "detectors.py", DETECTORS)
    assert run_detector("find_dependency_issues.py", tmp_path) == []

    write(tmp_path, "client.py", "import requests\n")
    found = {f["smell_type"] for f in run_detector("find_dependency_issues.py", tmp_path)}
    assert found == {"no_dependency_manifest"}


def test_the_local_import_detector_exempts_the_guard_and_nothing_else(tmp_path):
    """A guarded optional import has to sit where its absence is handled, so it is
    not a deferred import to hoist. An unguarded one beside it still is."""
    write(tmp_path, "detectors.py",
          DETECTORS + "\n\ndef parse(text):\n    import shlex\n    return shlex.split(text)\n")

    local = [f for f in run_detector("find_local_imports.py", tmp_path) if f["smell_type"] == "local_import"]
    assert [(f["line"], f["kind"]) for f in local] == [(DETECTORS.count("\n") + 4, "finding")]
