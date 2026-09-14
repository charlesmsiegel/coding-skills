"""run_external_tools.py resolves the package manager it reports.

The audit used to resolve `npm` and then label the output with whatever the
lockfile implied: on a yarn project it ran `npm npm audit`, which cannot
succeed, and on a pnpm project it ran npm's audit and reported it as pnpm's.
"""

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
