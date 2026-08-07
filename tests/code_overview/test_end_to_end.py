"""The whole pipeline over a synthetic monorepo, in the order SKILL.md documents.

The unit tests each cover one script; this covers the seams between them — that
the map one script writes is the map the next one reads, that the root roll-up
finds the package pages, and that the finished set has no dangling link.

Deliberately does not run code-visualization: this repo's tests exercise one
skill at a time, and the atlas is stubbed the way inject_nav.py sees it anyway
(a file that exists, with a </header> to inject after).
"""

import json
import re
from pathlib import Path

import pytest

CO = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
DOCTOR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"

PACKAGES = ("billing", "shipping")
KINDS = ("summary", "codemap", "health")

SOURCE = '''\
import os

CACHE = {}


def handle(items=[]):
    total = 0
    for i in range(len(items)):
        total += items[i]
    CACHE["last"] = total
    handle_file = open("/tmp/out", "w")
    handle_file.write(str(total))
    os.system("echo " + str(total))
    return total
'''


@pytest.fixture
def monorepo(repo):
    # A manifest and a shared test tree at the root, owned by no single package —
    # exactly the context a doctor loses when it is pointed at one package dir.
    repo.write("pyproject.toml", '[project]\nname = "platform"\ndependencies = ["requests"]\n')
    for package in PACKAGES:
        repo.write(f"src/{package}/__init__.py", "")
        repo.write(f"src/{package}/core.py", SOURCE)
        repo.write(f"src/{package}/util.py", "\n".join(f"x{i} = {i}" for i in range(200)))
        repo.write(f"tests/test_{package}.py",
                   f"from src.{package} import core, util\n\n\n"
                   f"def test_handle():\n    assert core.handle([1]) == 1\n"
                   f"    assert util is not None\n")
    repo.commit("init")
    return repo


def stub_atlas(repo, docs_dir, title):
    repo.write(f"{docs_dir}/codemap.html",
               '<!DOCTYPE html><html><head><title>a</title></head><body>'
               f'<header class="doc"><h1 class="doc-title">{title}</h1></header>'
               '<nav class="tabs"><button>Inventory</button></nav><main></main></body></html>')


def read_meta(path: Path) -> dict:
    match = re.search(r'id="code-health-meta">(.*?)</script>', path.read_text(), re.DOTALL)
    assert match, f"{path} carries no metadata"
    return json.loads(match.group(1).replace("<\\/", "</"))


def test_the_documented_pipeline_produces_a_navigable_set(run_script, monorepo, tmp_path):
    repo = monorepo

    # 1. discover, then (standing in for the user) accept the proposal for the
    #    two importable packages and drop the structural `src` candidate.
    proposal = json.loads(
        run_script(CO / "discover_packages.py", repo.path, "--format", "json").stdout)
    chosen = [p for p in proposal["packages"] if p["name"] in PACKAGES]
    assert len(chosen) == len(PACKAGES), "discovery found the two real packages"
    assert "structural-names" in {q["id"] for q in proposal["questions"]}

    map_path = repo.path / "docs/code-overview.json"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps({"schema": "code-overview/1", "packages": chosen}))

    # 2. the doctor, ONCE, from the repo root — so it can see the manifest and
    #    the shared tests/ tree that no single package owns.
    report = tmp_path / "findings.json"
    report.write_text(run_script(DOCTOR / "analyze_all.py", repo.path, "--format", "json").stdout)

    # 3. per package: atlas (stubbed), health scoped to its roots, summary
    for package in chosen:
        docs = package["docs"]
        stub_atlas(repo, docs, f"{package['name']} — Codebase Atlas")
        run_script(CO / "build_health.py", "--out", repo.path / docs / "health.html",
                   "--findings", report, "--repo", repo.path, "--name", package["name"],
                   "--root-dir", package["roots"][0], "--language", package["language"],
                   "--doctor", package["doctor"])
        run_script(CO / "build_summary.py", "--out", repo.path / docs / "summary.html",
                   "--repo", repo.path, "--name", package["name"])

    # 4. repo level
    stub_atlas(repo, "docs", "whole-repo — Codebase Atlas")
    run_script(CO / "build_health.py", "--root", "--out", repo.path / "docs/health.html",
               "--map", map_path, "--repo", repo.path, "--name", "whole-repo",
               "--doctor", "python-code-doctor", "--findings", report)
    run_script(CO / "build_summary.py", "--root", "--out", repo.path / "docs/summary.html",
               "--repo", repo.path, "--name", "whole-repo", "--map", map_path)

    # 5. navigation, and the gate
    run_script(CO / "inject_nav.py", "--map", map_path, "--repo", repo.path)
    check = run_script(CO / "inject_nav.py", "--map", map_path, "--repo", repo.path, "--check")
    assert "BROKEN LINK" not in check.stderr
    assert "would update 0" in check.stderr

    # --- what the set has to look like when it is done ---
    expected = {repo.path / "docs" / f"{kind}.html" for kind in KINDS}
    expected |= {repo.path / p["docs"] / f"{kind}.html" for p in chosen for kind in KINDS}
    missing = [p for p in expected if not p.is_file()]
    assert not missing, f"missing documents: {missing}"

    for path in expected:
        text = path.read_text()
        assert text.count("<!-- code-overview:nav -->") == 1
        # Relative hrefs only — the shared webfont link is absolute, and is the
        # same one code-visualization's shell uses.
        for href in re.findall(r'href="([^"#][^"]*)"', text):
            if href.startswith(("http://", "https://", "//", "mailto:")):
                continue
            assert (path.parent / href).resolve().is_file(), f"{path}: {href}"

    # every package reachable from the portal, and every package reaching back up
    portal = (repo.path / "docs/summary.html").read_text()
    for package in chosen:
        assert f'{package["docs"]}/summary.html' in portal.replace("../", "")

    for package in chosen:
        for kind in KINDS:
            block = re.search(r"code-overview:nav -->(.*?)<!-- /",
                              (repo.path / package["docs"] / f"{kind}.html").read_text(),
                              re.DOTALL).group(1)
            assert f"docs/{kind}.html" in block, "up-links to the same document type"

    # the roll-up agrees with the pages it rolled up
    root = read_meta(repo.path / "docs/health.html")
    assert {p["package"] for p in root["packages"]} == set(PACKAGES)
    for row in root["packages"]:
        package = next(p for p in chosen if p["name"] == row["package"])
        assert row["grade"] == read_meta(repo.path / package["docs"] / "health.html")["grade"]
    # The root keeps everything the packages keep, plus findings about
    # repo-level configuration (the root pyproject.toml) that belongs to no
    # package — so it is a superset, not an equal.
    package_total = sum(
        read_meta(repo.path / p["docs"] / "health.html")["findings_total"] for p in chosen)
    assert root["findings_total"] >= package_total
    root_only = {(f["file"], f["line"], f["type"]) for f in root["top_findings"]}
    for package in chosen:
        root_only -= {(f["file"], f["line"], f["type"])
                      for f in read_meta(repo.path / package["docs"] / "health.html")["top_findings"]}
    assert all("/" not in path for path, _, _ in root_only), (
        f"the root's extra findings must be repo-level config, not package code: {root_only}"
    )

    # the fixture is deliberately awful, so the grade has to say so
    assert root["grade"] not in {"A+", "A", "A-"}
    assert any(row["key"] == "security" and row["findings"]["total"] > 0
               for row in root["categories"])


def test_repo_context_findings_beat_package_scoped_ones(run_script, monorepo, tmp_path):
    """Why SKILL.md runs the doctors from the repo root, pinned as a test.

    A doctor pointed at one package cannot see the root manifest or the shared
    tests/ tree, so it fabricates findings about both. The failure is silent —
    a plausible report, wrong in the Tests category, which carries 15% of the
    grade — so it is worth an explicit regression test rather than a comment.
    """
    repo = monorepo
    fabricated = {"no_tests_in_repo", "untested_module", "no_dependency_manifest"}

    def types(*args):
        findings = json.loads(run_script(DOCTOR / "analyze_all.py", *args,
                                         "--format", "json").stdout)
        return {issue.get("issue_type") or issue.get("smell_type") or issue.get("type")
                for payload in findings["categories"].values()
                for issue in payload["issues"]}

    scoped = types(repo.path / "src/billing")
    whole = types(repo.path)

    assert scoped & fabricated, (
        "the fixture has to actually reproduce the problem, or this test proves nothing"
    )
    assert not (whole & fabricated), (
        "run from the root, the doctor sees pyproject.toml and tests/ and stops "
        "inventing findings about their absence"
    )


def test_a_package_page_carries_only_its_own_findings(run_script, monorepo, tmp_path):
    repo = monorepo
    report = tmp_path / "findings.json"
    report.write_text(run_script(DOCTOR / "analyze_all.py", repo.path, "--format", "json").stdout)
    out = repo.path / "src/billing/docs/health.html"
    run_script(CO / "build_health.py", "--out", out, "--findings", report, "--repo", repo.path,
               "--name", "billing", "--root-dir", "src/billing", "--doctor", "python-code-doctor")

    meta = read_meta(out)
    assert meta["findings_out_of_scope"] > 0, "the shipping package's findings were in the report"
    for item in meta["top_findings"]:
        assert item["file"].startswith("src/billing/"), item["file"]
    # Every *location* on the page is inside the package. Descriptions may still
    # name another package — a duplicate block spanning the two is attributed to
    # this one and says where its twin is, which is the point of the finding.
    locations = re.findall(r'<code class="floc">([^<]+)</code>', out.read_text())
    assert locations
    assert all(loc.startswith("src/billing/") for loc in locations), locations


def test_rebuilding_the_root_before_the_packages_warns_and_recovers(run_script, monorepo,
                                                                    tmp_path):
    """Order matters, so getting it wrong has to be recoverable, not silent."""
    repo = monorepo
    map_path = repo.path / "docs/code-overview.json"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "billing", "roots": ["src/billing"], "docs": "src/billing/docs",
         "language": "python", "doctor": "python-code-doctor"},
    ]}))
    report = tmp_path / "billing.json"
    report.write_text(run_script(DOCTOR / "analyze_all.py", repo.path / "src/billing",
                                 "--format", "json").stdout)

    early = run_script(CO / "build_health.py", "--root", "--out", repo.path / "docs/health.html",
                       "--map", map_path, "--findings", report, "--repo", repo.path,
                       "--name", "whole-repo")
    assert "billing" in early.stderr
    # Listed as not-generated rather than dropped: a roll-up missing a whole
    # package silently looks complete.
    early_rows = read_meta(repo.path / "docs/health.html")["packages"]
    assert [row["package"] for row in early_rows] == ["billing"]
    assert early_rows[0]["score"] is None and early_rows[0]["generated"] is False
    assert "not generated" in (repo.path / "docs/health.html").read_text()

    run_script(CO / "build_health.py", "--out", repo.path / "src/billing/docs/health.html",
               "--findings", report, "--repo", repo.path, "--name", "billing",
               "--root-dir", "src/billing", "--doctor", "python-code-doctor")
    run_script(CO / "build_health.py", "--root", "--out", repo.path / "docs/health.html",
               "--map", map_path, "--findings", report, "--repo", repo.path,
               "--name", "whole-repo")
    assert [p["package"] for p in read_meta(repo.path / "docs/health.html")["packages"]] == ["billing"]
