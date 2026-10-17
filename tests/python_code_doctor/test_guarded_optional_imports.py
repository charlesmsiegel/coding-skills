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
import textwrap
from pathlib import Path

import pytest

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


# library_available() alone: the guard that degrades (it returns False).
AVAILABLE = DETECTORS[:DETECTORS.index("def detect")]
# The line of detect()'s import, whose handler re-raises as an error of its own.
DETECT_LINE = DETECTORS.splitlines().index("        from ultralytics import YOLO") + 1


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


def kinds(root: Path) -> dict[str, str]:
    return {f["description"].split("'")[1]: f["kind"] for f in run_detector("find_dependency_issues.py", root)
            if f["smell_type"] == "missing_dependency"}


def test_an_import_guarded_by_except_importerror_is_not_a_missing_dependency(tmp_path):
    """anima-ui's detectors.py: library_available()'s guard degrades and is not
    reported; detect()'s re-raises, so that one site is an unscored lead."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/detectors.py", DETECTORS)
    write(tmp_path, "src/app/cli.py", "def serve():\n    import anyio\n    return anyio\n")

    records = {f["description"].split("'")[1]: f for f in run_detector("find_dependency_issues.py", tmp_path)
               if f["smell_type"] == "missing_dependency"}
    assert {name: r["kind"] for name, r in records.items()} == {"anyio": "finding", "ultralytics": "candidate"}
    assert records["ultralytics"]["line"] == DETECT_LINE


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
          "    import fallback_json as simdjson\n"
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
    write(tmp_path, "detectors.py", AVAILABLE)
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


@pytest.mark.parametrize("stop", [
    'sys.exit("PyYAML is required: pip install pyyaml")',
    'raise ImportError("PyYAML is required; pip install pyyaml")',
    "raise",
    "raise SystemExit(1)",
    'exit("pip install pyyaml")',
    "quit()",
    "os._exit(1)",
])
def test_a_handler_that_stops_the_program_leaves_a_lead(tmp_path, stop):
    """`except ImportError: sys.exit("X is required")` is how a script says it cannot
    run without X. The guard does not make that optional, and silence would hide it."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "tool.py",
          "import os\nimport sys\n\ntry:\n    import yaml\nexcept ImportError:\n"
          "    print('missing', file=sys.stderr)\n    " + stop + "\n")

    lead = next(f for f in run_detector("find_dependency_issues.py", tmp_path)
                if f["smell_type"] == "missing_dependency")
    assert "'yaml'" in lead["description"]
    assert lead["kind"] == "candidate" and lead["severity"] == "high"
    assert lead["also_caused_by"] and not lead["suggestion"]
    assert lead["file"].replace("\\", "/").endswith("tool.py") and lead["line"] == 5


def test_a_shared_guard_spares_only_the_imports_its_handler_rebinds(tmp_path):
    """Of several third-party imports under one guard, only one whose name the
    degrading handler rebinds is optional: `x = None`, `x = fallback`, or a
    fallback import. `requests` beside `ujson as json` is a NameError waiting to
    happen when it is absent. Stdlib and relative imports in the same try do not
    make it a group."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "client.py",
          "try:\n    import requests\n    import ujson as json\nexcept ImportError:\n    import json\n"
          "try:\n    import numpy, scipy\nexcept ImportError:\n    numpy = scipy = None\n"
          "try:\n    from PIL import Image, ImageDraw\nexcept ImportError:\n    Image = ImageDraw = None\n"
          "try:\n    import os\n    import yaml\nexcept ImportError:\n    yaml = None\n"
          "try:\n    from . import _speedups\n    import cffi\nexcept ImportError:\n    _speedups = None\n"
          "try:\n    import torch\n    import xformers\nexcept ImportError:\n    HAVE_GPU = False\n")

    assert kinds(tmp_path) == {"requests": "candidate", "torch": "candidate", "xformers": "candidate"}


def test_a_guard_that_stops_stays_a_lead_however_the_module_is_guarded_elsewhere(tmp_path):
    """One optional guard does not speak for every site: a test that sets
    `requests = None` to skip network tests says nothing about the entry point
    that exits without it. A plain import anywhere is still a finding."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "app/main.py",
          "import sys\n\ntry:\n    import requests\nexcept ImportError:\n"
          "    sys.exit('requests is required: pip install requests')\n")
    write(tmp_path, "tests/test_net.py", "try:\n    import requests\nexcept ImportError:\n    requests = None\n")
    lead = next(f for f in run_detector("find_dependency_issues.py", tmp_path) if f["smell_type"] == "missing_dependency")
    assert lead["kind"] == "candidate" and lead["file"].replace("\\", "/").endswith("app/main.py") and lead["line"] == 4

    write(tmp_path, "app/available.py", AVAILABLE)
    write(tmp_path, "app/detect.py", DETECTORS[len(AVAILABLE):])
    assert kinds(tmp_path) == {"requests": "candidate", "ultralytics": "candidate"}

    write(tmp_path, "app/fetch.py", "import requests\n")
    assert kinds(tmp_path) == {"requests": "finding", "ultralytics": "candidate"}


@pytest.mark.parametrize("broad", ["Exception", "BaseException", "(OSError, Exception)", "builtins.Exception"])
def test_an_except_importerror_behind_a_catch_all_is_not_a_guard(tmp_path, broad):
    """Only the first matching handler runs. Behind `except Exception:` the
    `except ImportError:` never does, and the broad handler is no guard."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "app.py",
          "try:\n    import requests\nexcept " + broad + ":\n    requests = None\n"
          "except ImportError:\n    pass\n")
    assert kinds(tmp_path) == {"requests": "finding"}

    write(tmp_path, "app.py",
          "try:\n    import requests\nexcept (ImportError, " + broad.strip("()") + "):\n    requests = None\n"
          "try:\n    import yaml\nexcept ImportError:\n    yaml = None\nexcept " + broad + ":\n    raise\n")
    assert kinds(tmp_path) == {}, "a handler that names ImportError ahead of the catch-all still guards"


_PRELUDE = (
    "import argparse\nimport logging\nimport sys\nimport sys as _sys\nimport warnings\n"
    "from sys import exit as bye\n\nlogger = logging.getLogger(__name__)\n"
    "parser = argparse.ArgumentParser()\n\n\ndef die(message):\n    sys.exit(message)\n\n\n"
)


def _guarded(handler: str) -> str:
    return "try:\n    import yaml\nexcept ImportError:\n" + textwrap.indent(handler, "    ") + "\n"


def _in_main(handler: str) -> str:
    return ("def main():\n    try:\n        import yaml\n    except ImportError:\n"
            + textwrap.indent(handler, "        ") + "\n    return yaml\n")


# Source after the prelude -> what the guarded `yaml` becomes: a lead, or nothing.
_HANDLER_CASES = {
    _guarded("die('pip install pyyaml')"): "candidate",
    _guarded("_sys.exit('pip install pyyaml')"): "candidate",
    _guarded("bye('pip install pyyaml')"): "candidate",
    _guarded("if sys.platform == 'win32':\n    sys.exit('pip install pyyaml')\nelse:\n    raise"): "candidate",
    _guarded("parser.error('pip install pyyaml')"): "candidate",
    _guarded("print('yaml is missing')"): "candidate",
    _in_main("print('pip install pyyaml')\nreturn 1"): "candidate",
    _guarded("pass"): None,
    _guarded("yaml = None"): None,
    _guarded("warnings.warn('yaml missing; reading JSON instead')"): None,
    _guarded("logging.warning('yaml missing')"): None,
    _guarded("logger.info('yaml missing')"): None,
    _in_main("return None"): None,
    _in_main("return False"): None,
    _in_main("return"): None,
    "for attempt in range(2):\n" + textwrap.indent(_guarded("continue"), "    "): None,
}


def test_a_handler_carries_on_only_when_its_last_statement_visibly_degrades(tmp_path):
    """Anything a handler might do besides degrading — a helper that exits, an
    aliased sys.exit, an if/else that stops both ways, `return 1` from main(),
    argparse's error() — is a lead, never silence."""
    wrong = {}
    for number, (source, expected) in enumerate(_HANDLER_CASES.items()):
        root = tmp_path / f"case{number}"
        write(root, "pyproject.toml", MANIFEST)
        write(root, "tool.py", _PRELUDE + source)
        got = kinds(root)
        if got != ({"yaml": expected} if expected else {}):
            wrong[source] = got
    assert not wrong, wrong
