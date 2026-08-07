"""discover_packages.py proposes units and reports what it could not decide.

The contract that matters is not "it found the right packages" — there is no
right answer without the user — but that nothing disappears silently and every
undecidable thing comes back as a question.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
DISCOVER = SCRIPTS / "discover_packages.py"


def propose(run_script, repo, *args):
    result = run_script(DISCOVER, repo.path, "--format", "json", *args)
    return json.loads(result.stdout)


def names(proposal):
    return {package["name"] for package in proposal["packages"]}


@pytest.fixture
def monorepo(repo):
    repo.write("pyproject.toml", '[project]\nname = "platform"\n')
    for module in ("a", "b", "c"):
        repo.write(f"src/billing/{module}.py", "x = 1\n")
    repo.write("src/billing/__init__.py", "")
    for module in ("a", "b", "c"):
        repo.write(f"src/web/{module}.ts", "export const x = 1;\n")
    repo.write("src/web/package.json", '{"name": "@acme/web"}')
    repo.commit("init")
    return repo


def test_finds_manifest_and_package_candidates(run_script, monorepo):
    proposal = propose(run_script, monorepo)
    assert "platform" in names(proposal), "the root pyproject.toml names a candidate"
    assert "billing" in names(proposal), "an importable package is a candidate"
    assert "acme-web" in names(proposal), "a scoped npm name flattens to acme-web"


def test_language_and_doctor_are_inferred(run_script, monorepo):
    by_name = {p["name"]: p for p in propose(run_script, monorepo)["packages"]}
    assert by_name["billing"]["language"] == "python"
    assert by_name["billing"]["doctor"] == "python-code-doctor"
    web = next(p for p in by_name.values() if "web" in p["name"])
    assert web["language"] == "typescript"
    assert web["doctor"] == "typescript-code-doctor"


def test_typescript_wins_over_javascript_in_a_mixed_package(run_script, repo):
    repo.write("app/package.json", '{"name": "app"}')
    repo.write("app/legacy.js", "module.exports = 1;\n")
    repo.write("app/older.js", "module.exports = 2;\n")
    repo.write("app/new.ts", "export const x = 1;\n")
    repo.commit("init")
    app = next(p for p in propose(run_script, repo)["packages"] if p["name"] == "app")
    assert app["language"] == "typescript", (
        "a project with any .ts is a TS project with JS left in it, and the TS "
        "doctor reads both"
    )


def test_nesting_is_reported_as_a_question_not_resolved(run_script, monorepo):
    proposal = propose(run_script, monorepo)
    assert "nesting" in {q["id"] for q in proposal["questions"]}
    assert all(q["question"].endswith("?") for q in proposal["questions"])


def test_structural_directory_names_are_questioned(run_script, monorepo):
    ids = {q["id"] for q in propose(run_script, monorepo)["questions"]}
    assert "structural-names" in ids, "src/ is a layout convention, not a subsystem"


def test_a_language_with_no_doctor_raises_a_question(run_script, repo):
    for module in ("main", "server", "handler"):
        repo.write(f"cmd/api/{module}.go", "package main\n")
    repo.write("go.mod", "module example.com/api\n")
    repo.commit("init")
    proposal = propose(run_script, repo)
    assert "no-doctor" in {q["id"] for q in proposal["questions"]}
    assert all(p["doctor"] == "" for p in proposal["packages"] if p["language"] == "go")


def test_small_candidates_are_reported_not_dropped(run_script, repo):
    repo.write("tiny/__init__.py", "")
    repo.write("tiny/one.py", "x = 1\n")
    for module in ("a", "b", "c", "d"):
        repo.write(f"big/{module}.py", "x = 1\n")
    repo.write("big/__init__.py", "")
    repo.commit("init")
    proposal = propose(run_script, repo, "--min-files", "4")
    assert "tiny" not in names(proposal)
    assert "tiny" in {c["name"] for c in proposal["too_small"]}, (
        "a filtered candidate has to stay visible — a hole nobody can see is worse "
        "than a noisy proposal"
    )
    assert "too-small" in {q["id"] for q in proposal["questions"]}


def test_django_apps_become_packages_with_the_django_doctor(run_script, repo):
    repo.write("manage.py", "import django\n")
    repo.write("proj/settings.py",
               "INSTALLED_APPS = [\n"
               "    'django.contrib.admin',\n"
               "    'shop',\n"
               "    'orders.apps.OrdersConfig',\n"
               "]\n")
    for app in ("shop", "orders"):
        repo.write(f"{app}/__init__.py", "")
        repo.write(f"{app}/models.py", "from django.db import models\n")
        repo.write(f"{app}/views.py", "x = 1\n")
    repo.commit("init")
    proposal = propose(run_script, repo)
    by_name = {p["name"]: p for p in proposal["packages"]}
    assert {"shop", "orders"} <= set(by_name)
    assert by_name["shop"]["doctor"] == "django-code-doctor"
    assert by_name["orders"]["doctor"] == "django-code-doctor", (
        "'orders.apps.OrdersConfig' names the app at 'orders'"
    )


def test_npm_workspaces_are_expanded(run_script, repo):
    repo.write("package.json", '{"name": "root", "workspaces": ["packages/*"]}')
    for name in ("ui", "core"):
        repo.write(f"packages/{name}/package.json", f'{{"name": "@acme/{name}"}}')
        for module in ("a", "b", "c"):
            repo.write(f"packages/{name}/{module}.ts", "export const x = 1;\n")
    repo.commit("init")
    found = names(propose(run_script, repo))
    assert "acme-ui" in found and "acme-core" in found


def test_vendor_directories_are_never_proposed(run_script, repo):
    for module in ("a", "b", "c", "d"):
        repo.write(f"node_modules/left-pad/{module}.js", "module.exports = 1;\n")
        repo.write(f"app/{module}.py", "x = 1\n")
    repo.commit("init")
    proposal = propose(run_script, repo)
    assert "left-pad" not in names(proposal)
    assert not any("node_modules" in root
                   for package in proposal["packages"] for root in package["roots"])


def test_a_single_package_repo_is_questioned(run_script, repo):
    repo.write("pyproject.toml", '[project]\nname = "lonely"\n')
    for module in ("a", "b", "c"):
        repo.write(f"{module}.py", "x = 1\n")
    repo.commit("init")
    proposal = propose(run_script, repo)
    assert "single-package" in {q["id"] for q in proposal["questions"]}


def test_a_mixed_language_package_is_questioned(run_script, repo):
    """One doctor cannot speak for two languages, but both inflate the divisor."""
    for module in range(6):
        repo.write(f"svc/handler{module}.py", "x = 1\n")
    for module in range(4):
        repo.write(f"svc/widget{module}.ts", "export const x = 1;\n")
    repo.write("svc/__init__.py", "")
    repo.commit("init")
    proposal = propose(run_script, repo)
    svc = next(p for p in proposal["packages"] if p["name"] == "svc")
    assert svc["language"] == "python"
    assert svc["mixed_with"] == ["typescript"]
    assert "mixed-languages" in {q["id"] for q in proposal["questions"]}


def test_javascript_folded_into_typescript_is_not_called_mixed(run_script, repo):
    """The JS→TS fold is deliberate; it must not generate a spurious question."""
    for module in range(4):
        repo.write(f"web/legacy{module}.js", "module.exports = 1;\n")
        repo.write(f"web/new{module}.ts", "export const x = 1;\n")
    repo.write("web/package.json", '{"name": "web"}')
    repo.commit("init")
    proposal = propose(run_script, repo)
    web = next(p for p in proposal["packages"] if p["name"] == "web")
    assert web["language"] == "typescript"
    assert web["mixed_with"] == []
    assert "mixed-languages" not in {q["id"] for q in proposal["questions"]}


def test_an_incidental_second_language_is_not_called_mixed(run_script, repo):
    for module in range(20):
        repo.write(f"app/m{module}.py", "x = 1\n")
    repo.write("app/build.sh", "echo hi\n")
    repo.write("app/__init__.py", "")
    repo.commit("init")
    app = next(p for p in propose(run_script, repo)["packages"] if p["name"] == "app")
    assert app["mixed_with"] == [], "one shell script does not make a package bilingual"


def test_discovery_never_descends_into_ignored_trees(run_script, repo, tmp_path):
    """rglob-per-manifest-name would walk node_modules once per supported name."""
    for index in range(60):
        repo.write(f"node_modules/dep{index}/package.json", '{"name": "dep"}')
        repo.write(f"node_modules/dep{index}/index.js", "module.exports = 1;\n")
    repo.write("vendor/github.com/x/go.mod", "module x\n")
    for module in ("a", "b", "c"):
        repo.write(f"app/{module}.py", "x = 1\n")
    repo.write("app/__init__.py", "")
    repo.commit("init")
    proposal = propose(run_script, repo)
    assert "app" in names(proposal)
    for package in proposal["packages"] + proposal["too_small"]:
        for root in package["roots"]:
            assert not root.startswith(("node_modules", "vendor")), root


def test_sizes_exclude_generated_docs(run_script, repo):
    for module in ("a", "b", "c"):
        repo.write(f"app/{module}.py", "x = 1\n")
    repo.write("app/docs/codemap.html", "<html>" + "\n".join("x" * 40 for _ in range(500)) + "</html>")
    repo.write("app/docs/notes.sql", "select 1;\n" * 200)
    repo.commit("init")
    app = next(p for p in propose(run_script, repo)["packages"] if p["name"] == "app")
    assert app["size"]["files"] == 3, (
        "a package's own generated documentation must not count as its source — "
        "it is the divisor every density is computed against"
    )
