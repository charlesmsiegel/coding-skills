"""Installed skills are tooling, while their neighboring project config remains input."""
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "skills"


@pytest.mark.parametrize("owner", [".codex", ".claude", ".kiro", ".agents"])
@pytest.mark.parametrize("skill", ["python-code-doctor", "django-code-doctor", "science-investigation"])
def test_source_walk_omits_installed_skills_but_keeps_agent_config(load_module, tmp_path, owner, skill):
    payload = tmp_path / owner / "skills" / "python-code-doctor" / "scripts" / "doctor.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("def score(): return 0\n")
    config = tmp_path / owner / "hooks.py"
    config.write_text("def hook(): return 1\n")
    source = tmp_path / "main.py"
    source.write_text("def app(): return 2\n")
    common = load_module(SKILLS / skill / "scripts", "common")
    if skill == "science-investigation":
        found = set(common.iter_files(tmp_path, common.CODE_SUFFIXES))
    else:
        found = set(common.find_python_files(tmp_path))
    assert found == {config, source}
    # Explicitly asking to inspect a skill must still inspect it.
    if skill == "science-investigation":
        assert payload in common.iter_files(payload.parents[2], common.CODE_SUFFIXES)
    else:
        assert payload in common.find_python_files(payload.parents[2])


@pytest.mark.parametrize("owner", [".codex", ".claude", ".kiro", ".agents"])
def test_raw_source_walk_skips_payload_but_security_walk_keeps_it(load_module, tmp_path, owner):
    payload = tmp_path / owner / "skills" / "python-code-doctor" / "doctor.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("print('tooling')\n")
    config = tmp_path / owner / "hooks.py"
    config.write_text("print('config')\n")
    common = load_module(SKILLS / "code-doctor" / "scripts", "common")
    assert set(common.walk_files(tmp_path, source_only=True)) == {config}
    assert set(common.walk_files(tmp_path, source_only=False)) == {config, payload}


def test_installed_manifest_cannot_route_the_host_project(run_script, repo):
    repo.write("go.mod", "module example.com/host\n")
    repo.write(".codex/skills/doctor/pyproject.toml", '[project]\nname="tool"\ndependencies=["django"]\n')
    repo.write(".codex/skills/doctor/tsconfig.json", '{}\n')
    result = run_script(SKILLS / "code-doctor" / "scripts" / "route.py", repo.path, "--format", "json")
    import json
    assert json.loads(result.stdout)["routes"] == []
