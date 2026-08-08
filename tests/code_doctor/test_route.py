"""Routing: which language specialists this repository's manifests justify.

The rule under test is the evidence rule. A route must come from a manifest the
repository wrote about itself, because the alternative — counting file
extensions — routes a Go service with one Python script to the Python doctor,
which then reports the missing `pyproject.toml` it was never supposed to have.
"""

import json
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "route.py"


def routes_for(repo, run_script) -> dict:
    result = run_script(SCRIPT, repo.path, "--format", "json")
    return json.loads(result.stdout)


def skills_in(payload) -> list[str]:
    return [route["skill"] for route in payload["routes"]]


def test_a_python_manifest_routes_to_the_python_doctor(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx"]\n')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor"]
    assert payload["routes"][0]["evidence"] == ["pyproject.toml"]
    assert payload["raw_only"] is False


def test_a_declared_django_dependency_adds_the_django_doctor(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["Django>=5.0"]\n')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor", "django-code-doctor"]


def test_manage_py_beside_installed_apps_routes_to_both_without_any_manifest(repo, run_script):
    repo.write("manage.py", "import os\n")
    repo.write("site/settings.py", "INSTALLED_APPS = ['django.contrib.auth']\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor", "django-code-doctor"]
    assert "site/settings.py" in payload["routes"][1]["evidence"]


def test_python_files_with_no_manifest_route_nowhere_and_say_so(repo, run_script):
    repo.write("tool.py", "x = 1\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["routes"] == []
    assert payload["raw_only"] is True
    assert any("no manifest" in note for note in payload["notes"])


def test_a_go_repo_with_a_stray_python_script_is_not_a_python_project(repo, run_script):
    repo.write("go.mod", "module example.com/m\n")
    repo.write("scripts/gen.py", "print(1)\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True, "a filename census would have routed this to Python"


def test_a_tsconfig_routes_to_the_typescript_doctor(repo, run_script):
    repo.write("tsconfig.json", '{"compilerOptions": {"strict": true}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["typescript-code-doctor"]


def test_a_typescript_dev_dependency_routes_without_a_tsconfig(repo, run_script):
    repo.write("package.json", '{"name": "x", "devDependencies": {"typescript": "^5.4.0"}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["typescript-code-doctor"]
    assert payload["routes"][0]["evidence"] == ["package.json"]


def test_manifests_inside_excluded_directories_are_not_evidence(repo, run_script):
    repo.write("go.mod", "module example.com/m\n")
    repo.write("node_modules/left-pad/package.json",
               '{"name": "left-pad", "devDependencies": {"typescript": "^5.0.0"}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True, "a dependency's own manifest is not this repo's declaration"


def test_two_ecosystems_both_route_and_the_overlap_is_noted(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx"]\n')
    repo.write("tsconfig.json", '{"compilerOptions": {}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert set(skills_in(payload)) == {"python-code-doctor", "typescript-code-doctor"}
    assert any("more than one ecosystem" in note.lower() for note in payload["notes"])


def test_no_declaration_at_all_says_correctness_will_be_ungraded(repo, run_script):
    repo.write("main.rs", "fn main() {}\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True
    assert any("ungraded" in note for note in payload["notes"])


def test_text_output_names_each_route_and_its_evidence(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["Django"]\n')
    repo.commit()

    result = run_script(SCRIPT, repo.path)

    assert "django-code-doctor" in result.stdout
    assert "pyproject.toml" in result.stdout


def test_a_missing_path_exits_two(repo, run_script):
    result = run_script(SCRIPT, repo.path / "nope", expect_rc=2)

    assert "no such file" in result.stderr.lower()


@pytest.fixture
def route(load_module):
    return load_module(SKILL / "scripts", "route")


@pytest.mark.parametrize("spec, expected", [
    ("Django>=5.0", "django"),
    ("django", "django"),
    ("Django[argon2]==5.0.1", "django"),
    ("django ; python_version >= '3.11'", "django"),
    ("  DJANGO  ", "django"),
    ("-e .", ""),
    ("-r other.txt", ""),
    ("", ""),
])
def test_requirement_name_takes_the_head_of_the_spec(route, spec, expected):
    assert route.requirement_name(spec) == expected


def test_a_js_dependency_named_django_does_not_route_a_python_doctor(repo, run_script):
    """npm and PyPI are different namespaces, and a name is only meaningful in one.

    npm really does publish a package called `django`. Merged into a single
    dependency table, declaring it routed this pure-JavaScript repository to
    python-code-doctor *and* django-code-doctor, citing `package.json` as the
    evidence — and a Python doctor pointed at a repo with no Python in it
    reports the missing `pyproject.toml` and the absent `tests/` as findings
    about a project that was never Python. Over-routing is the direction this
    router must not be wrong in.
    """
    repo.write("package.json", json.dumps({
        "name": "web", "dependencies": {"django": "^0.1.0", "react": "^18.0.0"}}))
    repo.write("src/app.js", "export const go = () => 1;\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == [], (
        "a package.json declares JavaScript dependencies; it is not evidence of a "
        "Python project, whatever the dependencies happen to be called"
    )
    assert payload["raw_only"] is True
    evidence = [item for route in payload["routes"] for item in route["evidence"]]
    assert "package.json" not in evidence


def test_a_python_dependency_named_typescript_does_not_route_the_ts_doctor(repo, run_script):
    """The mirror image: PyPI carries a `typescript` distribution too."""
    repo.write("pyproject.toml",
               '[project]\nname = "x"\ndependencies = ["typescript"]\n')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor"]


def test_django_declared_in_a_python_manifest_still_routes(repo, run_script):
    """Splitting the namespaces must not cost the real route its evidence."""
    repo.write("requirements.txt", "Django>=5.0\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor", "django-code-doctor"]
    assert payload["routes"][1]["evidence"] == ["requirements.txt"]
