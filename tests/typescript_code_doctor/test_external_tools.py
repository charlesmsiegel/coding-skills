"""run_external_tools.py resolves the package manager it reports.

The audit used to resolve `npm` and then label the output with whatever the
lockfile implied: on a yarn project it ran `npm npm audit`, which cannot
succeed, and on a pnpm project it ran npm's audit and reported it as pnpm's.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def external(load_module):
    return load_module(SCRIPTS_DIR, "run_external_tools")


@pytest.fixture
def which(monkeypatch, external):
    """Every manager is 'installed' at /bin/<name>; the test asserts which one is chosen."""
    monkeypatch.setattr(external.shutil, "which", lambda name: f"/bin/{name}")


@pytest.mark.parametrize("files, manager, tail", [
    ({"package-lock.json": "{}"}, "npm", ["audit", "--json"]),
    ({"pnpm-lock.yaml": ""}, "pnpm", ["audit", "--json"]),
    ({"yarn.lock": ""}, "yarn", ["audit", "--json"]),
    ({"yarn.lock": "", ".yarnrc.yml": "nodeLinker: node-modules\n"}, "yarn", ["npm", "audit", "--json"]),
])
def test_audit_argv_names_and_runs_the_same_manager(external, which, tmp_path, files, manager, tail):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    got_manager, argv = external._audit_argv(tmp_path)
    assert got_manager == manager
    assert argv == [f"/bin/{manager}", *tail]


def test_audit_prefers_the_projects_own_binary(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    local = tmp_path / "node_modules" / ".bin"
    local.mkdir(parents=True)
    (local / "pnpm").write_text("")
    monkeypatch.setattr(external.shutil, "which", lambda name: None)
    manager, argv = external._audit_argv(tmp_path)
    assert manager == "pnpm" and argv[0] == str(local / "pnpm")


def test_a_missing_manager_is_reported_not_substituted(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    monkeypatch.setattr(external.shutil, "which", lambda name: None)
    assert external._audit_argv(tmp_path) is None


def test_run_audit_executes_the_resolved_argv_and_labels_it(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(external.shutil, "which", lambda name: f"/bin/{name}")
    seen = []

    def fake_run(argv, cwd, timeout=900):
        seen.append(argv)
        return 0, '{"vulnerabilities": {"left-pad": {"severity": "high", "via": [{"title": "t", "url": "u"}]}}}', ""

    monkeypatch.setattr(external, "_run", fake_run)
    findings = external.run_audit(None, tmp_path)
    assert seen == [["/bin/pnpm", "audit", "--json"]]
    assert findings[0]["smell_type"] == "pnpm-audit:high"
    assert "pnpm audit fix" in findings[0]["suggestion"]


def test_the_tool_table_has_an_audit_entry_and_no_npm_entry(external):
    assert "audit" in external.TOOLS and "npm" not in external.TOOLS


def test_an_unknown_tool_name_is_named_rather_than_silently_dropped(
        external, tmp_path, monkeypatch, capsys):
    """`--tools npm` asks for a tool this script does not have. Filtering the
    name out in silence runs *nothing* and reports it as a clean pass."""
    (tmp_path / "package.json").write_text('{"name": "p"}', encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["run_external_tools.py", str(tmp_path), "--tools", "npm,tsc", "--format", "json"])

    external.main()
    captured = capsys.readouterr()
    assert "unknown tool(s) ignored" in captured.err
    assert "npm" in captured.err
    assert "tsc" not in captured.err, "a tool this script does have was called unknown"


@pytest.mark.parametrize("package_manager, tail", [
    ("yarn@4.1.0", ["npm", "audit", "--json"]),      # Berry
    ("yarn@2.4.3", ["npm", "audit", "--json"]),      # Berry
    ("yarn@1.22.19", ["audit", "--json"]),           # classic
])
def test_yarn_berry_is_recognised_from_package_manager(
        external, which, tmp_path, package_manager, tail):
    """A Berry repo need not ship `.yarnrc.yml`; Corepack's `packageManager`
    field pins the major on its own. Reading only the file ran `yarn audit` on
    Berry, where that subcommand does not exist."""
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        f'{{"name": "p", "packageManager": "{package_manager}"}}', encoding="utf-8")

    manager, argv = external._audit_argv(tmp_path)
    assert manager == "yarn"
    assert argv == ["/bin/yarn", *tail]
