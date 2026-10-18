"""An import served by a directory the module itself puts on sys.path is a lead, not a defect.

Found by running the doctor over anima-ui, whose tests/real_weights/comfy_side.py
runs under ComfyUI's own interpreter: it puts the checkout named on its command
line on sys.path and then imports `comfy` and `nodes` from it. Those are
ComfyUI's own modules, not a distribution this project could declare, and the
dependency detector charged each as a high `missing_dependency`.

The source cannot say what that directory holds, so the record stays, as a
candidate. A path the source does resolve inside the repository explains
nothing (every module there is already first-party), and an import in a module
that never touches sys.path is exactly as missing as before.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"

MANIFEST = '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'

# anima-ui's tests/real_weights/comfy_side.py, cut down to how it reaches ComfyUI.
COMFY_SIDE = (
    '"""ComfyUI\'s side of the real-weight measurements, run under ComfyUI\'s own interpreter."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "import argparse\n"
    "import json\n"
    "import shlex\n"
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def boot(comfyui: Path, comfy_args: str) -> None:\n"
    "    sys.path.insert(0, str(comfyui))\n"
    "    sys.argv = ['comfy_side', *shlex.split(comfy_args)]\n"
    "    import comfy.options\n"
    "\n"
    "    comfy.options.enable_args_parsing()\n"
    "    import comfy.model_management  # reads the arguments, so it must come first\n"
    "\n"
    "\n"
    "def render(arguments: argparse.Namespace) -> dict:\n"
    "    import comfy.sample\n"
    "    import nodes\n"
    "    from comfy.ldm.cosmos import predict2\n"
    "\n"
    "    return {'latent': nodes.EmptyLatentImage, 'sample': comfy.sample, 'dit': predict2}\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    parser = argparse.ArgumentParser()\n"
    "    parser.add_argument('--comfyui', type=Path, required=True)\n"
    "    arguments = parser.parse_args()\n"
    "    boot(arguments.comfyui, '--cpu')\n"
    "    print(json.dumps(render(arguments)))\n"
)


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def line_of(source: str, text: str) -> int:
    return next(n for n, line in enumerate(source.splitlines(), 1) if text in line)


def missing(root: Path) -> dict[str, dict]:
    return {f["description"].split("'")[1]: f for f in run_detector("find_dependency_issues.py", root)
            if f["smell_type"] == "missing_dependency"}


def test_a_harness_importing_from_a_checkout_it_puts_on_sys_path_is_a_lead(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "tests/real_weights/comfy_side.py", COMFY_SIDE)
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/cli.py", "def serve():\n    import anyio\n    return anyio\n")

    records = missing(tmp_path)

    assert sorted(records) == ["anyio", "comfy", "nodes"]
    assert records["anyio"]["kind"] == "finding", "a module that leaves sys.path alone is unchanged"
    for name, first in (("comfy", "import comfy.options"), ("nodes", "import nodes")):
        lead = records[name]
        assert lead["kind"] == "candidate"
        assert lead["severity"] == "high", "a candidate's severity is what it would be if confirmed"
        assert lead["also_caused_by"] and not lead["suggestion"]
        assert lead["file"].replace("\\", "/").endswith("tests/real_weights/comfy_side.py")
        assert lead["line"] == line_of(COMFY_SIDE, first)
        assert f"line {line_of(COMFY_SIDE, 'sys.path.insert')}" in lead["description"]


def test_the_same_name_imported_where_sys_path_is_untouched_is_still_missing(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "tests/real_weights/comfy_side.py", COMFY_SIDE)
    write(tmp_path, "tools/graph.py", "import nodes\n\nGRAPH = nodes\n")

    lead = missing(tmp_path)["nodes"]
    assert lead["kind"] == "finding" and "graph.py" in lead["description"]


def test_an_import_the_added_path_cannot_have_served_still_fires(tmp_path):
    """Above the addition in the file; or at module level below a def that adds
    the path, which runs when the module loads, before anything can call the def."""
    source = (
        "import sys\n"
        "import requests\n"
        "\n"
        "\n"
        "def boot(checkout):\n"
        "    sys.path.insert(0, checkout)\n"
        "\n"
        "\n"
        "import leftpad\n"
        "\n"
        "\n"
        "def run():\n"
        "    import comfy\n"
        "    return comfy, leftpad, requests\n"
    )
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "harness.py", source)

    kinds = {name: record["kind"] for name, record in missing(tmp_path).items()}
    assert kinds == {"requests": "finding", "leftpad": "finding", "comfy": "candidate"}


def test_a_path_the_source_resolves_inside_the_repository_explains_nothing(tmp_path):
    """Every module under the repository is already first-party, so adding one of
    its directories cannot be where an undeclared name comes from."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "tests/real_weights/conftest.py",
          "import os\n"
          "import sys\n"
          "from pathlib import Path\n"
          "\n"
          "HERE = Path(__file__).resolve().parent\n"
          "ROOT = Path(__file__).resolve().parents[2]\n"
          "sys.path.insert(0, str(HERE.parent / 'models'))\n"
          "sys.path.insert(0, str(ROOT))\n"
          "sys.path.append(os.path.dirname(os.path.abspath(__file__)))\n"
          "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))\n"
          "sys.path.extend(['tests/models', ROOT / 'src'])\n"
          "\n"
          "\n"
          "def setup():\n"
          "    root = Path(__file__).parent.parent.parent\n"
          "    sys.path.insert(0, str(root.joinpath('vendor')))\n"
          "    import requests\n"
          "    return requests\n")

    assert {name: r["kind"] for name, r in missing(tmp_path).items()} == {"requests": "finding"}


@pytest.mark.parametrize("addition", [
    "sys.path.insert(0, '/opt/ComfyUI')",
    "sys.path.insert(0, '../ComfyUI')",
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))",
    "sys.path.append(os.environ['COMFYUI_DIR'])",
    "sys.path.extend(['src', os.getenv('COMFYUI_DIR', '')])",
    "sys.path += [sys.argv[1]]",
    "site.addsitedir(sys.argv[1])",
])
def test_every_way_of_reaching_past_what_the_source_shows_is_a_lead(tmp_path, addition):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "plugin.py",
          "import os\nimport site\nimport sys\nfrom pathlib import Path\n\n"
          + addition + "\n\nimport comfy\n")

    assert {name: r["kind"] for name, r in missing(tmp_path).items()} == {"comfy": "candidate"}


def test_a_name_bound_more_than_once_or_passed_in_is_not_read_as_a_path(tmp_path):
    """A parameter or a rebound name could hold anything by the time it is added."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "plugin.py",
          "import sys\nfrom pathlib import Path\n\n"
          "HERE = Path(__file__).parent\n\n\n"
          "def boot(HERE):\n"
          "    sys.path.insert(0, str(HERE))\n"
          "    import comfy\n"
          "    return comfy\n")
    write(tmp_path, "other.py",
          "import sys\nfrom pathlib import Path\n\n"
          "where = Path(__file__).parent\n"
          "where = Path(sys.argv[1])\n"
          "sys.path.insert(0, str(where))\n"
          "import nodes\n")

    assert {name: r["kind"] for name, r in missing(tmp_path).items()} == \
        {"comfy": "candidate", "nodes": "candidate"}


def test_the_lead_reads_as_a_lead_in_text_and_in_the_aggregate_report(tmp_path):
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "comfy_side.py", COMFY_SIDE)

    output = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_dependency_issues.py"), str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=True).stdout
    assert "[CANDIDATE]" in output and "? also caused by:" in output
    assert not [line for line in output.splitlines() if line.strip() == "→"], \
        "a candidate rendered a fix arrow with nothing after it"

    report = json.loads(subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_all.py"), str(tmp_path),
         "--format", "json", "--skip-duplicates"],
        capture_output=True, text=True, timeout=300, check=True).stdout)
    assert "records_rejected" not in report["meta"], report["meta"].get("records_rejected")
    leads = [(r["smell_type"], r["kind"]) for r in report["categories"]["dependency_issues"]["issues"]]
    assert leads == [("missing_dependency", "candidate")] * 2


@pytest.mark.parametrize("addition", [
    "from os.path import abspath, dirname, join\n"
    "sys.path.insert(0, abspath(join(dirname(__file__), '..', 'lib')))",
    "import os.path as osp\n"
    "sys.path.insert(0, osp.join(osp.dirname(__file__), '..'))",
    "from os import path\n"
    "sys.path.insert(0, path.join(path.dirname(__file__), 'vendor'))",
    "from pathlib import Path as P\n"
    "sys.path.insert(0, str(P(__file__).parent))",
    "sys.path.insert(0, str(Path(__file__).with_name('vendor')))",
    "sys.path.insert(0, str(Path(__file__).with_suffix('')))",
])
def test_in_repo_spellings_through_imported_names_explain_nothing(tmp_path, addition):
    """The same in-repo directory, spelled through `from os.path import join`, an
    `osp` alias, or Path.with_name, is still inside the repository."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "scripts/run.py",
          "import sys\nfrom pathlib import Path\n\n" + addition + "\n\nimport requests\n")

    assert {name: r["kind"] for name, r in missing(tmp_path).items()} == {"requests": "finding"}


def test_an_unread_path_is_opaque_only_when_the_run_decides_it(tmp_path):
    """A path the reader cannot follow demotes the imports after it only when it
    reads a parameter, the environment, sys.argv, or a name bound more than once.
    One that is merely spelled in a way the reader does not know stays a finding."""
    cases = {
        "def vendor_dir():\n    return 'vendor'\n\nsys.path.insert(0, vendor_dir())": "finding",
        "from settings import PLUGIN_DIR\nsys.path.insert(0, PLUGIN_DIR)": "finding",
        "for entry in ('lib', 'vendor'):\n    sys.path.insert(0, entry)": "finding",
        "from os import environ\nsys.path.insert(0, environ['PLUGIN_DIR'])": "candidate",
        "from os import getenv\nsys.path.append(getenv('PLUGIN_DIR'))": "candidate",
        "from sys import argv\nsys.path.append(argv[1])": "candidate",
        "sys.path.insert(0, os.environ.get('PLUGIN_DIR', 'plugins'))": "candidate",
        "def boot(arguments):\n    sys.path.insert(0, str(arguments.case_dir))": "candidate",
        "base = Path(__file__).parent\nbase = base / 'plugins'\nsys.path.insert(0, str(base))": "candidate",
    }
    wrong = {}
    for number, (addition, expected) in enumerate(cases.items()):
        root = tmp_path / f"case{number}"
        write(root, "pyproject.toml", MANIFEST)
        write(root, "settings.py", "PLUGIN_DIR = 'plugins'\n")
        write(root, "plugin.py",
              "import os\nimport sys\nfrom pathlib import Path\n\n" + addition + "\n\n\n"
              "def fetch():\n    import requests\n    return requests\n")
        got = missing(root)["requests"]["kind"]
        if got != expected:
            wrong[addition] = got
    assert not wrong, wrong


_FAN_OUT = ", ".join(["ROOT"] * 30)
_CHAIN = "".join(f"{name} = os.path.join({', '.join([below] * 30)})\n"
                 for name, below in zip("ABCDEF", "BCDEFG")) + "G = os.path.dirname(__file__)\n"


@pytest.mark.parametrize("bindings,added", [
    ("from cfg import *\n\nROOT = os.path.join(" + _FAN_OUT + ")\n", "ROOT"),
    (_CHAIN, "A"),
], ids=["self-referential", "fan-out-chain"])
def test_a_path_built_from_itself_or_a_wide_chain_is_read_quickly(tmp_path, bindings, added):
    """`ROOT = os.path.join(ROOT, ROOT, ...)` names itself; a chain of names each
    joining the next thirty times is a tree of 30**6 reads if nothing is remembered."""
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "plugin.py",
          "import os\nimport sys\n\n" + bindings + f"sys.path.insert(0, {added})\n\nimport requests\n")

    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "find_dependency_issues.py"), str(tmp_path), "--format", "json"],
            capture_output=True, text=True, timeout=20, check=True)
    except subprocess.TimeoutExpired:
        pytest.fail("the path reader did not finish in 20 s")
    assert time.monotonic() - started < 20
    kinds = {r["description"].split("'")[1]: r["kind"] for r in json.loads(result.stdout)
             if r["smell_type"] == "missing_dependency"}
    assert kinds["requests"] == "finding", "a path that reads nothing the run decides explains nothing"


def test_a_relative_entry_counts_from_the_root_and_from_its_own_directory(tmp_path):
    """A relative sys.path entry is relative to the working directory, and the two
    that occur are the project root and the file's own directory: Sphinx runs
    docs/conf.py from docs/, so its `abspath('..')` is the repository root."""
    cases = {
        ("docs/conf.py", "sys.path.insert(0, os.path.abspath('..'))"): "finding",
        ("docs/conf.py", "sys.path.insert(0, os.path.abspath('../src'))"): "finding",
        ("tests/conftest.py", "sys.path.append('..')"): "finding",
        ("scripts/x.py", "sys.path.insert(0, str(Path('..').resolve()))"): "finding",
        ("scripts/x.py", "sys.path.insert(0, os.path.dirname(os.path.abspath('.')))"): "finding",
        ("docs/conf.py", "sys.path.insert(0, os.path.abspath('../../ComfyUI'))"): "candidate",
    }
    wrong = {}
    for number, ((rel, addition), expected) in enumerate(cases.items()):
        root = tmp_path / f"case{number}"
        write(root, "pyproject.toml", MANIFEST)
        write(root, rel, "import os\nimport sys\nfrom pathlib import Path\n\n" + addition
              + "\n\nimport sphinx_rtd_theme\n")
        got = missing(root)["sphinx_rtd_theme"]["kind"]
        if got != expected:
            wrong[(rel, addition)] = got
    assert not wrong, wrong


def test_a_checkout_named_on_the_command_line_through_argparse_is_run_time(tmp_path):
    """`args.comfy` from parse_args() is the command line, as sys.argv is. The cwd,
    `~` and input() stay findings: they are not what this rule is about."""
    cases = {
        "parser = argparse.ArgumentParser()\nparser.add_argument('--comfy')\n"
        "args = parser.parse_args()\nsys.path.insert(0, args.comfy)": "candidate",
        "parser = argparse.ArgumentParser()\nargs, _ = parser.parse_known_args()\n"
        "sys.path.insert(0, str(Path(args.comfy)))": "candidate",
        "def main():\n    parser = argparse.ArgumentParser()\n    options = parser.parse_args()\n"
        "    sys.path.insert(0, options.comfy)": "candidate",
        "sys.path.insert(0, input('ComfyUI checkout? '))": "finding",
        "sys.path.insert(0, str(Path.cwd() / 'ComfyUI'))": "finding",
        "sys.path.insert(0, os.path.expanduser('~/ComfyUI'))": "finding",
    }
    wrong = {}
    for number, (addition, expected) in enumerate(cases.items()):
        root = tmp_path / f"case{number}"
        write(root, "pyproject.toml", MANIFEST)
        write(root, "harness.py",
              "import argparse\nimport os\nimport sys\nfrom pathlib import Path\n\n" + addition + "\n\n\n"
              "def render():\n    import comfy\n    return comfy\n")
        got = missing(root)["comfy"]["kind"]
        if got != expected:
            wrong[addition] = got
    assert not wrong, wrong


def test_a_long_chain_of_names_is_cut_off_rather_than_crashing(tmp_path):
    """Hundreds of names each bound to the next used to exhaust the interpreter's
    recursion limit in the run-time check, and the whole tree's report with it."""
    chain = "".join(f"N{i} = N{i + 1}\n" for i in range(400)) + "N400 = sys.argv[1]\n"
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "plugin.py", "import sys\n\n" + chain + "sys.path.insert(0, N0)\n\nimport requests\n")
    write(tmp_path, "other.py", "import yaml\n")

    kinds = {name: r["kind"] for name, r in missing(tmp_path).items()}
    assert kinds == {"requests": "finding", "yaml": "finding"}, "past the reader's depth, the finding stays"


def test_one_file_that_breaks_the_reader_does_not_take_down_the_report(tmp_path, load_module, monkeypatch, capsys):
    detector = load_module(SCRIPTS_DIR, "find_dependency_issues")
    write(tmp_path, "pyproject.toml", MANIFEST)
    write(tmp_path, "bad.py", "import yaml\n")
    write(tmp_path, "good.py", "import requests\n")
    real = detector.import_sites

    def breaking(tree, filepath, root):
        if filepath.name == "bad.py":
            raise RecursionError("maximum recursion depth exceeded")
        return real(tree, filepath, root)

    monkeypatch.setattr(detector, "import_sites", breaking)
    names = {i.description.split("'")[1] for i in detector.analyze(tmp_path, set())
             if i.smell_type == "missing_dependency"}

    assert names == {"requests"}
    assert "bad.py" in capsys.readouterr().err, "the skipped file is named, not reported clean"
