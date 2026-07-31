"""Regression tests for the code-visualization skill's analyzer scripts.

Each analyzer is driven as a subprocess over a throwaway repo, exactly as the
skill drives it, and asserted on twice: the JSON summary it prints (what the
agent reads) and the HTML fragment it writes (what the reader sees). The point
is to pin the signals the atlas is built on — cycles, churn ranking, citation
breakage, staleness verdicts — so a refactor that quietly stops reporting one
of them fails here instead of shipping a confidently wrong atlas.
"""

import ast
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "scripts"


def test_every_script_parses():
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert len(scripts) >= 8
    for script in scripts:
        ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def test_inventory_counts_languages_and_test_share(repo, tabs, run_script, fragment):
    repo.write("src/a.py", "def a():\n    return 1\n")
    repo.write("src/b.py", "def b(x):\n    if x:\n        return 2\n    return 3\n")
    repo.write("web/app.js", "export function c() { return 1; }\n")
    repo.write("tests/test_a.py", "def test_a():\n    assert True\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["languages"]["Python"] >= 3
    assert summary["languages"]["JavaScript"] == 1
    assert summary["total_loc"] >= 8
    # tests/test_a.py is 2 of the 9 lines — the share must be non-zero and sane.
    assert 0 < summary["test_loc_share_pct"] < 100
    assert fragment.title(tabs / "02-inventory.html") == "Inventory"


def test_inventory_ranks_largest_file_first(repo, tabs, run_script):
    repo.write("small.py", "x = 1\n")
    repo.write("big.py", "".join(f"line_{i} = {i}\n" for i in range(60)))

    summary = json.loads(run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["largest_files"][0]["path"] == "big.py"
    assert summary["largest_files"][0]["loc"] == 60


def test_inventory_escapes_html_in_paths(repo, tabs, run_script, fragment):
    """A path with HTML-significant characters must not inject into the fragment."""
    # Windows forbids <>:"|?* in filenames, so & is the portable payload here.
    repo.write("weird/a&b.py", "x = 1\n")

    run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs)

    body = fragment.body(tabs / "02-inventory.html")
    assert "weird/a&amp;b.py" in body
    assert "weird/a&b.py" not in body


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def test_deps_detects_import_cycle(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py", "from pkg import b\n")
    repo.write("pkg/b.py", "from pkg import a\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert ["pkg/a.py", "pkg/b.py"] in [sorted(c) for c in summary["file_cycles"]]


def test_deps_reports_no_cycle_for_a_chain(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py", "from pkg import b\n")
    repo.write("pkg/b.py", "from pkg import c\n")
    repo.write("pkg/c.py", "value = 1\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["file_cycles"] == []
    assert summary["import_edges"] >= 2


def test_deps_counts_fan_in_for_a_shared_module(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/util.py", "def helper():\n    return 1\n")
    for name in ("one", "two", "three"):
        repo.write(f"pkg/{name}.py", "from pkg import util\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in["pkg/util.py"] == 3


def test_deps_resolves_an_import_from_a_deeply_nested_package(repo, tabs, run_script):
    """sys.path can be rooted anywhere, so a module is importable under any
    suffix of its path — not only the first two or three segments."""
    repo.write("services/billing/src/pkg/util.py", "def helper():\n    return 1\n")
    repo.write("services/billing/src/pkg/__init__.py", "")
    repo.write("services/billing/src/app.py", "from pkg import util\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("services/billing/src/pkg/util.py") == 1


def test_deps_resolves_a_sibling_import_to_the_neighbour_not_a_twin(repo, tabs, run_script):
    """Two directories ship their own copy of a module — a script run from its
    own directory imports the one beside it, and nothing links the copies."""
    for skill in ("alpha", "beta"):
        repo.write(f"skills/{skill}/scripts/common.py", "VALUE = 1\n")
        repo.write(f"skills/{skill}/scripts/run.py", "import common\n\nprint(common.VALUE)\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("skills/alpha/scripts/common.py") == 1
    assert fan_in.get("skills/beta/scripts/common.py") == 1


def test_deps_leaves_an_ambiguous_import_unresolved(repo, tabs, run_script):
    """With two candidates and no sibling to prefer, drawing an edge to either
    would be a guess — and a confidently wrong edge is worse than none."""
    repo.write("one/helper.py", "VALUE = 1\n")
    repo.write("two/helper.py", "VALUE = 2\n")
    repo.write("app/main.py", "import helper\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["import_edges"] == 0


def test_deps_extracts_javascript_imports(repo, tabs, run_script):
    repo.write("src/index.js", "import { thing } from './lib';\n")
    repo.write("src/lib.js", "export const thing = 1;\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["import_edges"] >= 1
    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/lib.js") == 1


# --------------------------------------------------------------------------- #
# Runtime resources
#
# An import is not the only way one file depends on another. A rendered template
# or a prompt read off disk is a dependency by every meaning that matters, and a
# graph that shows none of it understates coupling exactly where behavior is
# assembled at runtime.
# --------------------------------------------------------------------------- #


def test_deps_counts_a_loaded_prompt_as_a_dependency(repo, tabs, run_script):
    repo.write("agent/run.py", "SYSTEM = open('prompts/system.md').read()\n")
    repo.write("prompts/system.md", "You are helpful.\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["resource_edges"] == 1
    assets = {a["path"]: a for a in summary["top_referenced_assets"]}
    assert assets["prompts/system.md"]["loaded_by"] == ["agent/run.py:1"]


def test_deps_makes_a_referenced_asset_a_node_but_not_an_unreferenced_one(repo, tabs, run_script):
    repo.write("agent/run.py", "T = open('templates/mail.html').read()\n")
    repo.write("templates/mail.html", "<p>hi</p>\n")
    repo.write("docs/design.md", "# notes\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)
    graph_files = summary["asset_nodes"]["paths"]

    assert "templates/mail.html" in graph_files
    assert "docs/design.md" not in graph_files


def test_deps_reports_an_unloaded_prompt_as_an_orphan(repo, tabs, run_script):
    repo.write("agent/run.py", "SYSTEM = open('prompts/system.md').read()\n")
    repo.write("prompts/system.md", "You are helpful.\n")
    repo.write("prompts/retired.md", "old instructions\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["orphan_assets"] == ["prompts/retired.md"]


def test_deps_does_not_call_a_ci_workflow_an_orphan(repo, tabs, run_script):
    """Files under a dot-directory are loaded by external tooling, not by this
    codebase; "nothing references it" would be a false alarm every time."""
    repo.write("tests/run.py", "CFG = open('.github/workflows/ci.yml').read()\n")
    repo.write(".github/workflows/ci.yml", "on: push\n")
    repo.write(".github/workflows/release.yml", "on: tag\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["orphan_assets"] == []


def test_deps_finds_a_cycle_between_templates(repo, tabs, run_script, fragment):
    repo.write("templates/a.html", '{% include "b.html" %}\n')
    repo.write("templates/b.html", '{% include "a.html" %}\n')

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert ["templates/a.html", "templates/b.html"] in [sorted(c) for c in summary["file_cycles"]]


def test_deps_fragment_shows_the_resource_section_and_distinguishes_the_edges(
        repo, tabs, run_script, fragment):
    repo.write("agent/run.py", "SYSTEM = open('prompts/system.md').read()\n")
    repo.write("prompts/system.md", "You are helpful.\n")

    run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs)
    body = fragment.body(tabs / "03-dependencies.html")

    assert "Runtime resources" in body
    assert "prompts/system.md" in body
    # The graph has to say which edges are loads rather than imports.
    assert '"kind":"resource"' in body


def test_deps_summary_carries_the_resource_caveat(repo, tabs, run_script):
    repo.write("agent/run.py", "SYSTEM = open('prompts/system.md').read()\n")
    repo.write("prompts/system.md", "hi\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert "textually" in summary["resource_caveat"]


# --------------------------------------------------------------------------- #
# Layout-independent resolution
#
# The failure these pin down: resolution used to guess a fixed list of source
# roots (src/main/kotlin/... at the repo root), so a Gradle multi-module repo —
# 433 Kotlin files across ten modules — produced 0 import edges and read as
# "no coupling". Resolution must key on what files DECLARE (package, namespace,
# crate) rather than where a build tool happens to put them.
# --------------------------------------------------------------------------- #


def _deps_summary(repo, tabs, run_script):
    return json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)


def test_deps_resolves_kotlin_imports_across_gradle_modules(repo, tabs, run_script):
    """Gradle layout: each module carries its own src/main/kotlin tree. The
    import must resolve via the declared package, not a path rooted at the repo."""
    repo.write("core/src/main/kotlin/dev/rpg/core/Util.kt",
               "package dev.rpg.core\n\nfun helper() = 1\n")
    repo.write("app/src/main/kotlin/dev/rpg/app/Main.kt",
               "package dev.rpg.app\n\nimport dev.rpg.core.Util\n\nfun main() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/kotlin/dev/rpg/core/Util.kt") == 1


def test_deps_kotlin_star_import_links_every_file_of_the_package(repo, tabs, run_script):
    repo.write("core/src/main/kotlin/dev/rpg/core/A.kt", "package dev.rpg.core\n")
    repo.write("core/src/main/kotlin/dev/rpg/core/B.kt", "package dev.rpg.core\n")
    repo.write("app/src/main/kotlin/dev/rpg/app/Main.kt",
               "package dev.rpg.app\n\nimport dev.rpg.core.*\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/kotlin/dev/rpg/core/A.kt") == 1
    assert fan_in.get("core/src/main/kotlin/dev/rpg/core/B.kt") == 1


def test_deps_ambiguous_jvm_class_is_dropped_not_guessed(repo, tabs, run_script):
    """Kotlin multiplatform: commonMain and jvmMain both declare the same
    package+class (expect/actual). Guessing one would be a wrong edge."""
    for src_set in ("commonMain", "jvmMain"):
        repo.write(f"lib/src/{src_set}/kotlin/dev/rpg/core/Clock.kt",
                   "package dev.rpg.core\n\nclass Clock\n")
    repo.write("app/src/main/kotlin/dev/rpg/app/Main.kt",
               "package dev.rpg.app\n\nimport dev.rpg.core.Clock\n")

    summary = _deps_summary(repo, tabs, run_script)

    assert summary["import_edges"] == 0


def test_deps_resolves_csharp_using_to_namespace_files(repo, tabs, run_script):
    repo.write("Core/Billing/Invoice.cs",
               "namespace Acme.Billing;\n\npublic class Invoice {}\n")
    repo.write("App/Program.cs",
               "using Acme.Billing;\n\nnamespace Acme.App;\n\nclass Program {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("Core/Billing/Invoice.cs") == 1


def test_deps_resolves_rust_workspace_crates_and_mod_decls(repo, tabs, run_script):
    """Cargo workspace: crates live under crates/<name>, not src/ at the root.
    `use other_crate::` must reach the other crate; `mod x;` must reach x.rs."""
    repo.write("crates/engine/Cargo.toml", '[package]\nname = "engine"\n')
    repo.write("crates/engine/src/lib.rs", "mod physics;\n\npub fn run() {}\n")
    repo.write("crates/engine/src/physics.rs", "pub fn step() {}\n")
    repo.write("crates/game/Cargo.toml",
               '[package]\nname = "game"\n\n[dependencies]\n'
               'engine = { path = "../engine" }\n')
    repo.write("crates/game/src/main.rs", "use engine::run;\n\nfn main() { run(); }\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("crates/engine/src/lib.rs") == 1      # from game/src/main.rs
    assert fan_in.get("crates/engine/src/physics.rs") == 1  # from lib.rs's mod decl


def test_deps_resolves_go_imports_in_a_nested_module(repo, tabs, run_script):
    """go.mod sits in backend/, not at the repo root — the old resolver only
    ever read the root go.mod."""
    repo.write("backend/go.mod", "module example.com/be\n\ngo 1.22\n")
    repo.write("backend/pkg/db/db.go", "package db\n\nfunc Open() {}\n")
    repo.write("backend/cmd/api/main.go",
               'package main\n\nimport "example.com/be/pkg/db"\n\nfunc main() { db.Open() }\n')

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("backend/pkg/db/db.go") == 1


def test_deps_resolves_js_root_alias_imports(repo, tabs, run_script):
    """'@/x' is the near-universal src-root alias (Vite/Next/tsconfig paths)."""
    repo.write("src/lib/util.ts", "export const x = 1;\n")
    repo.write("src/pages/home.ts", "import { x } from '@/lib/util';\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/lib/util.ts") == 1


def test_deps_summary_reports_per_language_resolution(repo, tabs, run_script):
    """Every language reports imports seen vs resolved, so a resolver gap shows
    up as a number instead of a silently sparse graph."""
    repo.write("app/src/main/kotlin/dev/rpg/app/Main.kt",
               "package dev.rpg.app\n\nimport dev.rpg.gone.Missing\n"
               "import kotlinx.coroutines.launch\n")
    repo.write("core/src/main/kotlin/dev/rpg/core/Real.kt", "package dev.rpg.core\n")

    summary = _deps_summary(repo, tabs, run_script)

    res = summary["resolution"]["Kotlin"]
    assert res["first_party"] == 1      # dev.rpg.gone.* — our dev.rpg root...
    assert res["resolved"] == 0         # ...but no file declares that package
    assert res["external"] == 1         # kotlinx.coroutines is not this repo
    assert any("dev.rpg.gone.Missing" in s for s in res["samples"])


def test_deps_rust_grouped_use_links_each_item(repo, tabs, run_script):
    """`use crate::{a, b};` names two modules; capturing only the prefix used
    to emit a bogus lib.rs self-reference and drop both real edges."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "use crate::{physics, audio};\n")
    repo.write("src/physics.rs", "pub fn step() {}\n")
    repo.write("src/audio.rs", "pub fn play() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/physics.rs") == 1
    assert fan_in.get("src/audio.rs") == 1


def test_deps_rust_super_stays_in_the_parent_module_dir(repo, tabs, run_script):
    """From src/a/child.rs, `super::` is module `a` — whose files live in
    src/a/, the importer's own directory, not one level further up."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "mod a;\n")
    repo.write("src/a/mod.rs", "mod child;\nmod sibling;\n")
    repo.write("src/a/child.rs", "use super::sibling::helper;\n")
    repo.write("src/a/sibling.rs", "pub fn helper() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    # one edge from mod.rs's `mod sibling;`, one from child.rs's super:: use
    assert fan_in.get("src/a/sibling.rs") == 2


def test_deps_kotlin_top_level_function_import_resolves_by_declaration(
        repo, tabs, run_script):
    """Kotlin imports name declarations, not files: `import pkg.helper` must
    find whichever file declares `fun helper`, even in a multi-file package."""
    repo.write("core/src/main/kotlin/dev/rpg/core/Utils.kt",
               "package dev.rpg.core\n\nfun helper() = 1\n")
    repo.write("core/src/main/kotlin/dev/rpg/core/Other.kt",
               "package dev.rpg.core\n\nclass Other\n")
    repo.write("app/src/main/kotlin/dev/rpg/app/Main.kt",
               "package dev.rpg.app\n\nimport dev.rpg.core.helper\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/kotlin/dev/rpg/core/Utils.kt") == 1


def test_deps_csharp_and_jvm_declarations_do_not_cross_link(repo, tabs, run_script):
    """A C# using and a Java package that happen to share a dotted name are
    different ecosystems — linking across them invents impossible edges."""
    repo.write("jvm/Shared.java", "package com.acme.shared;\n\nclass Shared {}\n")
    repo.write("cs/Program.cs", "using com.acme.shared;\n\nnamespace App;\nclass P {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    assert summary["import_edges"] == 0


def test_deps_scala_grouped_import_links_only_named_declarations(repo, tabs, run_script):
    """`import p.{A, B}` names two declarations — truncating at the brace used
    to star-link every file of the package, C included."""
    repo.write("core/src/main/scala/p/A.scala", "package p\n\nclass A\n")
    repo.write("core/src/main/scala/p/B.scala", "package p\n\nclass B\n")
    repo.write("core/src/main/scala/p/C.scala", "package p\n\nclass C\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.{A, B}\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/A.scala") == 1
    assert fan_in.get("core/src/main/scala/p/B.scala") == 1
    assert "core/src/main/scala/p/C.scala" not in fan_in


def test_deps_scala_package_object_declares_its_own_package(repo, tabs, run_script):
    """`package foo` + `package object bar` puts the file's members in foo.bar,
    not in a package literally named foo.object."""
    repo.write("src/main/scala/foo/package.scala",
               "package foo\n\npackage object bar {\n  def helper = 1\n}\n")
    repo.write("src/main/scala/foo/Other.scala", "package foo\n\nclass Other\n")
    repo.write("src/main/scala/app/Main.scala",
               "package app\n\nimport foo.bar.helper\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/main/scala/foo/package.scala") == 1


def test_deps_kotlin_extension_function_resolves_by_function_name(repo, tabs, run_script):
    """`fun String.helper()` is imported as `import p.helper` — the receiver
    type is not the declaration's name."""
    repo.write("core/src/main/kotlin/p/Ext.kt",
               "package p\n\nfun String.helper() = 1\n")
    repo.write("core/src/main/kotlin/p/Other.kt", "package p\n\nclass Other\n")
    repo.write("app/src/main/kotlin/app/Main.kt",
               "package app\n\nimport p.helper\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/kotlin/p/Ext.kt") == 1


def test_deps_csharp_type_resolves_even_when_file_stem_differs(repo, tabs, run_script):
    """C# type names owe nothing to file names: `using static Acme.Utility;`
    must find the file declaring class Utility, whatever it is called."""
    repo.write("lib/Helpers.cs",
               "namespace Acme;\n\npublic class Utility {\n    public static int X = 1;\n}\n")
    repo.write("lib/Extra.cs", "namespace Acme;\n\npublic class Extra {}\n")
    repo.write("app/Program.cs", "using static Acme.Utility;\n\nnamespace App;\nclass P {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/Helpers.cs") == 1


def test_deps_csharp_nested_namespace_blocks_compose(repo, tabs, run_script):
    """`namespace Acme { namespace Billing { ... } }` declares Acme.Billing;
    sibling blocks at the same depth must not compose with each other."""
    repo.write("lib/Nested.cs",
               "namespace Acme {\n  namespace Billing {\n    class Invoice {}\n  }\n"
               "  namespace Shipping {\n    class Label {}\n  }\n}\n")
    repo.write("lib/Other.cs", "namespace Acme;\n\nclass Other {}\n")
    repo.write("app/Program.cs", "using Acme.Billing;\n\nnamespace App;\nclass P {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/Nested.cs") == 1


def test_deps_rust_renamed_path_dependency_resolves(repo, tabs, run_script):
    """`foo = { package = "bar", ... }` makes code say `use foo::` for the
    workspace crate named bar — the alias must reach bar's sources."""
    repo.write("app/Cargo.toml",
               '[package]\nname = "app"\n\n[dependencies]\n'
               'foo = { package = "bar", path = "../bar" }\n')
    repo.write("app/src/main.rs", "use foo::engine::start;\n\nfn main() {}\n")
    repo.write("bar/Cargo.toml", '[package]\nname = "bar"\n')
    repo.write("bar/src/lib.rs", "pub mod engine;\n")
    repo.write("bar/src/engine.rs", "pub fn start() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("bar/src/engine.rs") == 2  # lib.rs `mod` + main.rs `use`


def test_deps_csharp_allman_namespaces_compose(repo, tabs, run_script):
    """The brace on its own line — the dominant C# style — must not pop the
    namespace before its block even opens."""
    repo.write("lib/Nested.cs",
               "namespace Acme\n{\n  namespace Billing\n  {\n    class Invoice {}\n  }\n}\n")
    repo.write("lib/Other.cs", "namespace Acme;\n\nclass Other {}\n")
    repo.write("app/Program.cs", "using Acme.Billing;\n\nnamespace App;\nclass P {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/Nested.cs") == 1


def test_deps_csharp_global_qualifier_is_accepted(repo, tabs, run_script):
    """`using static global::Acme.Utility;` is the ambiguity-proof spelling
    generated code favors — the global:: prefix must not hide the edge."""
    repo.write("lib/Helpers.cs", "namespace Acme;\n\npublic class Utility {}\n")
    repo.write("lib/Extra.cs", "namespace Acme;\n\npublic class Extra {}\n")
    repo.write("app/Program.cs",
               "using static global::Acme.Utility;\n\nnamespace App;\nclass P {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/Helpers.cs") == 1


def test_deps_jvm_type_qualified_wildcard_resolves_the_type(repo, tabs, run_script):
    """`import static com.acme.Utility.*` hangs the wildcard off a declared
    type, not a package — it must link the file declaring the type."""
    repo.write("lib/src/main/java/com/acme/Utility.java",
               "package com.acme;\n\npublic class Utility {}\n")
    repo.write("lib/src/main/java/com/acme/Extra.java",
               "package com.acme;\n\npublic class Extra {}\n")
    repo.write("app/src/main/java/app/Main.java",
               "package app;\n\nimport static com.acme.Utility.*;\n\nclass Main {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/src/main/java/com/acme/Utility.java") == 1
    assert "lib/src/main/java/com/acme/Extra.java" not in fan_in


def test_deps_scala_comma_separated_imports_all_count(repo, tabs, run_script):
    """`import p.A, q.B` is one statement naming two dependencies."""
    repo.write("core/src/main/scala/p/A.scala", "package p\n\nclass A\n")
    repo.write("core/src/main/scala/q/B.scala", "package q\n\nclass B\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.A, q.B\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/A.scala") == 1
    assert fan_in.get("core/src/main/scala/q/B.scala") == 1


def test_deps_scala3_extension_method_is_indexed(repo, tabs, run_script):
    """A Scala 3 `extension (s: String)` block declares its indented defs as
    importable members of the package."""
    repo.write("core/src/main/scala/p/Ext.scala",
               "package p\n\nextension (s: String)\n  def helper: Int = 1\n")
    repo.write("core/src/main/scala/p/Other.scala", "package p\n\nclass Other\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.helper\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/Ext.scala") == 1


def test_deps_rust_path_attribute_on_mod_is_honored(repo, tabs, run_script):
    """`#[path = \"platform/unix.rs\"] mod sys;` loads the attributed file,
    not a sys.rs that may not exist."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", '#[path = "platform/unix.rs"]\nmod sys;\n')
    repo.write("src/platform/unix.rs", "pub fn open() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/platform/unix.rs") == 1


def test_deps_rust_alias_scoping_is_per_manifest(repo, tabs, run_script):
    """Cargo dependency aliases are local to the manifest declaring them: two
    crates may both alias `util` to different targets."""
    repo.write("a/Cargo.toml",
               '[package]\nname = "a"\n\n[dependencies]\n'
               'util = { package = "helpers-one", path = "../one" }\n')
    repo.write("a/src/lib.rs", "use util::tools::run;\n")
    repo.write("b/Cargo.toml",
               '[package]\nname = "b"\n\n[dependencies]\n'
               'util = { package = "helpers-two", path = "../two" }\n')
    repo.write("b/src/lib.rs", "use util::tools::run;\n")
    repo.write("one/Cargo.toml", '[package]\nname = "helpers-one"\n')
    repo.write("one/src/lib.rs", "pub mod tools;\n")
    repo.write("one/src/tools.rs", "pub fn run() {}\n")
    repo.write("two/Cargo.toml", '[package]\nname = "helpers-two"\n')
    repo.write("two/src/lib.rs", "pub mod tools;\n")
    repo.write("two/src/tools.rs", "pub fn run() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    # each aliased use resolves within its own manifest: lib.rs mod + one use
    assert fan_in.get("one/src/tools.rs") == 2
    assert fan_in.get("two/src/tools.rs") == 2


def test_deps_rust_custom_lib_target_path_resolves(repo, tabs, run_script):
    """`[lib] path = "source/root.rs"` moves the crate root; crate:: and
    cross-crate uses must search the declared tree, not a nonexistent src/."""
    repo.write("bar/Cargo.toml",
               '[package]\nname = "bar"\n\n[lib]\npath = "source/root.rs"\n')
    repo.write("bar/source/root.rs", "pub mod engine;\n")
    repo.write("bar/source/engine.rs", "pub fn start() {}\n")
    repo.write("app/Cargo.toml",
               '[package]\nname = "app"\n\n[dependencies]\nbar = { path = "../bar" }\n')
    repo.write("app/src/main.rs", "use bar::engine::start;\n\nfn main() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("bar/source/engine.rs") == 2  # root.rs mod + main.rs use


def test_deps_rust_package_name_comes_from_the_package_table(repo, tabs, run_script):
    """A [[bin]] table with its own name may precede [package]; the crate is
    named by the package table, not the first `name =` in the file."""
    repo.write("app/Cargo.toml",
               '[[bin]]\nname = "cli"\npath = "src/main.rs"\n\n'
               '[package]\nname = "app"\n')
    repo.write("app/src/lib.rs", "pub mod engine;\n")
    repo.write("app/src/engine.rs", "pub fn start() {}\n")
    repo.write("tool/Cargo.toml", '[package]\nname = "tool"\n\n[dependencies]\napp = { path = "../app" }\n')
    repo.write("tool/src/main.rs", "use app::engine::start;\n\nfn main() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("app/src/engine.rs") == 2  # lib.rs mod + tool's use


def test_deps_rust_binary_beside_custom_lib_uses_its_own_root(repo, tabs, run_script):
    """A crate with a custom [lib] path may still have the default binary at
    src/main.rs — that binary's crate:: refers to the src tree, not the lib."""
    repo.write("bar/Cargo.toml",
               '[package]\nname = "bar"\n\n[lib]\npath = "source/root.rs"\n')
    repo.write("bar/source/root.rs", "pub fn lib_only() {}\n")
    repo.write("bar/src/main.rs", "mod binthing;\nuse crate::binthing::run;\n\nfn main() {}\n")
    repo.write("bar/src/binthing.rs", "pub fn run() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("bar/src/binthing.rs") == 1
    # root.rs's only fan-in is the manifest's runtime-load reference — the
    # binary's crate:: must not add an import edge to the library tree.
    assert fan_in.get("bar/source/root.rs", 0) == 1


def test_deps_scala_sibling_braced_packages_stay_separate(repo, tabs, run_script):
    """`package foo { .. }` followed by `package bar { .. }` declares two
    packages — composing them into foo.bar loses both."""
    repo.write("src/main/scala/Both.scala",
               "package foo {\n  class A\n}\n\npackage bar {\n  class B\n}\n")
    repo.write("src/main/scala/foo/Other.scala", "package foo\n\nclass Other\n")
    repo.write("src/main/scala/app/Main.scala",
               "package app\n\nimport foo.A\nimport bar.B\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/main/scala/Both.scala") == 1  # fan-in is per importer
    # both imports resolve — under foo.bar composition, foo.A would fail
    assert summary["resolution"]["Scala"]["resolved"] == 2


def test_deps_scala_multiline_grouped_import_expands(repo, tabs, run_script):
    """A formatter may wrap a grouped import across lines; the group must be
    accumulated to its closing brace, not truncated to a package star."""
    repo.write("core/src/main/scala/p/A.scala", "package p\n\nclass A\n")
    repo.write("core/src/main/scala/p/B.scala", "package p\n\nclass B\n")
    repo.write("core/src/main/scala/p/C.scala", "package p\n\nclass C\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.{\n  A,\n  B\n}\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/A.scala") == 1
    assert fan_in.get("core/src/main/scala/p/B.scala") == 1
    assert "core/src/main/scala/p/C.scala" not in fan_in


def test_deps_scala_opaque_type_is_indexed(repo, tabs, run_script):
    repo.write("core/src/main/scala/p/Ids.scala",
               "package p\n\nopaque type UserId = String\n")
    repo.write("core/src/main/scala/p/Other.scala", "package p\n\nclass Other\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.UserId\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/Ids.scala") == 1


def test_deps_go_duplicate_module_paths_prefer_the_importers_module(repo, tabs, run_script):
    """Example dirs often share a placeholder module path; an import must
    resolve within the importer's own module, not the first one found."""
    for ex in ("examples/one", "examples/two"):
        repo.write(f"{ex}/go.mod", "module example.com/demo\n")
        repo.write(f"{ex}/util/util.go", "package util\n\nfunc Do() {}\n")
        repo.write(f"{ex}/main.go",
                   'package main\n\nimport "example.com/demo/util"\n\nfunc main() { util.Do() }\n')

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("examples/one/util/util.go") == 1
    assert fan_in.get("examples/two/util/util.go") == 1


def test_deps_js_single_segment_baseurl_resolves_unless_a_package(repo, tabs, run_script):
    """`from "utils"` may be a baseUrl import of a unique repo file — but a
    name declared in package.json dependencies is an npm package, never a
    repo file, even when a same-named file exists."""
    repo.write("package.json", '{"dependencies": {"react": "^18.0.0"}}\n')
    repo.write("src/utils.ts", "export const x = 1;\n")
    repo.write("src/react.ts", "export const decoy = 1;\n")
    repo.write("src/app.ts", "import { x } from 'utils';\nimport React from 'react';\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/utils.ts") == 1
    assert "src/react.ts" not in fan_in


def test_deps_rust_undeclared_same_name_crate_does_not_link(repo, tabs, run_script):
    """An in-repo crate named like an external dependency (log) must not
    capture `use log::` from a crate that never declared it."""
    repo.write("app/Cargo.toml", '[package]\nname = "app"\n\n[dependencies]\nlog = "0.4"\n')
    repo.write("app/src/main.rs", "use log::info;\n\nfn main() {}\n")
    repo.write("unrelated/log/Cargo.toml", '[package]\nname = "log"\n')
    repo.write("unrelated/log/src/lib.rs", "pub fn info() {}\n")
    repo.write("declared/Cargo.toml",
               '[package]\nname = "declared"\n\n[dependencies]\n'
               'log = { path = "../unrelated/log" }\n')
    repo.write("declared/src/main.rs", "use log::info;\n\nfn main() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    # only the crate that declares the path dependency links to it
    assert fan_in.get("unrelated/log/src/lib.rs") == 1


def test_deps_rust_auto_discovered_binary_roots_at_itself(repo, tabs, run_script):
    """src/bin/cli.rs is its own crate: its crate:: and mod declarations live
    beside it, never in the library tree."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "pub fn lib_only() {}\n")
    repo.write("src/bin/cli.rs", "mod helper;\nuse crate::helper::run;\n\nfn main() {}\n")
    repo.write("src/bin/helper.rs", "pub fn run() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/bin/helper.rs") == 1
    assert "src/lib.rs" not in fan_in


def test_deps_rust_inline_module_scopes_child_declarations(repo, tabs, run_script):
    """`mod platform { pub mod imp; }` in lib.rs declares src/platform/imp.rs —
    not a same-named file at the crate root, even when one exists."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "mod platform {\n    pub mod imp;\n}\n")
    repo.write("src/platform/imp.rs", "pub fn real() {}\n")
    repo.write("src/imp.rs", "pub fn decoy() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/platform/imp.rs") == 1
    assert "src/imp.rs" not in fan_in


def test_deps_csharp_declarations_stay_in_their_namespace(repo, tabs, run_script):
    """A file with two namespace blocks declares each type only in its own
    block — A.Y must not exist just because the file also declares B.Y."""
    repo.write("lib/Mixed.cs",
               "namespace A {\n  class X {}\n}\nnamespace B {\n  class Y {}\n}\n")
    repo.write("lib/Other.cs", "namespace A;\n\nclass Z {}\n")
    repo.write("app/P1.cs", "using static A.X;\n\nnamespace App;\nclass P1 {}\n")
    repo.write("app/P2.cs", "using static A.Y;\n\nnamespace App;\nclass P2 {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/Mixed.cs") == 1        # P1 via A.X only
    assert summary["resolution"]["C#"]["resolved"] == 1


def test_deps_js_npm_deps_scope_to_the_importing_package(repo, tabs, run_script):
    """One monorepo package declaring an npm dep named utils must not disable
    another package's baseUrl import of its own utils.ts."""
    repo.write("packages/a/package.json", '{"dependencies": {"utils": "^1.0.0"}}\n')
    repo.write("packages/a/src/app.ts", "import { x } from 'utils';\n")
    repo.write("packages/a/src/utils.ts", "export const decoy = 1;\n")
    repo.write("packages/b/package.json", '{"dependencies": {}}\n')
    repo.write("packages/b/src/app.ts", "import { x } from 'utils';\n")
    repo.write("packages/b/src/utils.ts", "export const x = 1;\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("packages/b/src/utils.ts") == 1
    assert "packages/a/src/utils.ts" not in fan_in


def test_deps_scala3_direct_given_import_links_the_package(repo, tabs, run_script):
    """`import p.given` imports all givens from p — it is a wildcard, not a
    declaration named `given`."""
    repo.write("core/src/main/scala/p/Givens.scala",
               "package p\n\ngiven intOrd: Ordering[Int] = Ordering.Int\n")
    repo.write("core/src/main/scala/p/Other.scala", "package p\n\nclass Other\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.given\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/Givens.scala") == 1
    assert fan_in.get("core/src/main/scala/p/Other.scala") == 1


def test_deps_go_local_replace_translates_the_import_path(repo, tabs, run_script):
    """`replace example.com/old => ../lib` makes example.com/old/util resolve
    into the local lib module, whatever module path lib declares."""
    repo.write("mod-a/go.mod",
               "module example.com/a\n\nrequire example.com/old v0.0.0\n\n"
               "replace example.com/old => ../lib\n")
    repo.write("mod-a/main.go",
               'package main\n\nimport "example.com/old/util"\n\nfunc main() { util.Do() }\n')
    repo.write("lib/go.mod", "module example.com/lib\n")
    repo.write("lib/util/util.go", "package util\n\nfunc Do() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("lib/util/util.go") == 1


def test_deps_rust_plain_module_file_keeps_nested_children(repo, tabs, run_script):
    """src/foo.rs is an ordinary module, not a crate root: its `mod child;`
    lives at src/foo/child.rs in the standard nested layout."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "mod foo;\n")
    repo.write("src/foo.rs", "mod child;\n")
    repo.write("src/foo/child.rs", "pub fn f() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/foo/child.rs") == 1


def test_deps_rust_super_reaches_a_file_layout_parent(repo, tabs, run_script):
    """From src/foo/child.rs, `use super::Parent` lands on src/foo.rs when the
    parent uses the file (not mod.rs) layout."""
    repo.write("Cargo.toml", '[package]\nname = "app"\n')
    repo.write("src/lib.rs", "mod foo;\n")
    repo.write("src/foo.rs", "mod child;\n\npub struct Parent;\n")
    repo.write("src/foo/child.rs", "use super::Parent;\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/foo.rs") == 2  # lib.rs's mod + child.rs's super


def test_deps_rust_workspace_inherited_rename_resolves(repo, tabs, run_script):
    """A member declaring `foo.workspace = true` inherits the workspace's
    `foo = { package = "bar", path = "bar" }` rename."""
    repo.write("Cargo.toml",
               '[workspace]\nmembers = ["a", "bar"]\n\n'
               '[workspace.dependencies]\nfoo = { package = "bar", path = "bar" }\n')
    repo.write("a/Cargo.toml",
               '[package]\nname = "a"\n\n[dependencies]\nfoo.workspace = true\n')
    repo.write("a/src/lib.rs", "use foo::tools::run;\n")
    repo.write("bar/Cargo.toml", '[package]\nname = "bar"\n')
    repo.write("bar/src/lib.rs", "pub mod tools;\n")
    repo.write("bar/src/tools.rs", "pub fn run() {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("bar/src/tools.rs") == 2  # bar's mod + a's aliased use


def test_deps_scala3_colon_package_indexes_indented_declarations(repo, tabs, run_script):
    """Scala 3 `package p:` scopes the indented body — its defs are importable
    members of p even though they are not at column zero."""
    repo.write("core/src/main/scala/p/Ext.scala",
               "package p:\n  def helper: Int = 1\n")
    repo.write("core/src/main/scala/p/Other.scala", "package p\n\nclass Other\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport p.helper\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/Ext.scala") == 1


def test_deps_scala_root_qualifier_is_stripped(repo, tabs, run_script):
    repo.write("core/src/main/scala/p/A.scala", "package p\n\nclass A\n")
    repo.write("app/src/main/scala/app/Main.scala",
               "package app\n\nimport _root_.p.A\n\nclass Main\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("core/src/main/scala/p/A.scala") == 1


def test_deps_js_node_builtins_never_link_to_repo_files(repo, tabs, run_script):
    """`import path from "path"` is the Node built-in even when a repo file
    named path.ts exists and no package.json declares it."""
    repo.write("src/path.ts", "export const decoy = 1;\n")
    repo.write("src/helpers.ts", "export const x = 1;\n")
    repo.write("src/app.ts", "import path from 'path';\nimport { x } from 'helpers';\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/helpers.ts") == 1
    assert "src/path.ts" not in fan_in


def test_deps_jvm_duplicate_declarations_prefer_the_importers_module(repo, tabs, run_script):
    """Two Gradle modules may declare the same fully qualified class; an
    import inside one module resolves to its own declaration, not ambiguity."""
    for mod in ("app1", "app2"):
        repo.write(f"{mod}/build.gradle", "plugins { id 'java' }\n")
        repo.write(f"{mod}/src/main/java/p/Util.java",
                   "package p;\n\npublic class Util {}\n")
    repo.write("app1/src/main/java/q/Main.java",
               "package q;\n\nimport p.Util;\n\nclass Main {}\n")

    summary = _deps_summary(repo, tabs, run_script)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("app1/src/main/java/p/Util.java") == 1
    assert "app2/src/main/java/p/Util.java" not in fan_in


def test_deps_python_resolution_is_counted_too(repo, tabs, run_script):
    """Python goes through the same accounting as every other language: its own
    modules count as first-party (resolved or not), stdlib/pip as external."""
    repo.write("app/main.py", "import util\nimport missing_local\nimport os\n")
    repo.write("app/util.py", "X = 1\n")
    repo.write("missing_local/data.txt", "not python\n")  # dir exists, module doesn't

    summary = _deps_summary(repo, tabs, run_script)

    res = summary["resolution"]["Python"]
    assert res["first_party"] >= 1
    assert res["resolved"] >= 1          # app/util.py via the sibling rule
    assert res["external"] == 2          # os is stdlib; missing_local holds no python
    assert summary["import_edges"] == 1


def test_deps_refuses_a_nonexistent_repo_dir(tmp_path, tabs, run_script):
    """A mistyped or failed-clone path must error, not emit a confident
    empty graph (edges: 0, modules: 0) for a repo that isn't there."""
    result = run_script(SCRIPTS / "analyze_deps.py", tmp_path / "no-such-repo",
                        "--tabs-dir", tabs, expect_rc=2)
    assert "no-such-repo" in result.stderr


def test_deps_zero_edges_on_a_real_codebase_warns_in_the_fragment(
        repo, tabs, run_script, fragment):
    """The Gradle failure mode: many code files, no resolvable imports. The tab
    must say the graph is under-resolved, never present it as 'no coupling'."""
    for i in range(25):
        repo.write(f"m{i}/src/main/kotlin/com/x/m{i}/F.kt",
                   f"package com.x.m{i}\n\nimport com.x.other{i}.Gone\n\nclass F\n")

    run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs)

    body = fragment.body(tabs / "03-dependencies.html")
    assert "under-resolved" in body


# --------------------------------------------------------------------------- #
# Module grouping
#
# The failure these pin down: a repo whose code lives under two arms used to
# collapse to one node per arm, so a 12-module monorepo rendered as two boxes
# and one arrow. Grouping has to peel packaging directories (src/, app/) while
# keeping directories that merely sound structural but actually hold code.
# --------------------------------------------------------------------------- #


def _write_tree(repo, arms, ext="py", files_per_module=3):
    for arm, modules in arms.items():
        for module in modules:
            for i in range(files_per_module):
                repo.write(f"{arm}/{module}/f{i}.{ext}", "x = 1\n" * 40)


def test_deps_splits_a_two_arm_monorepo_into_submodules(repo, tabs, run_script):
    _write_tree(repo, {
        "frontend/src": ["components", "pages", "hooks", "api", "state", "utils"],
        "backend/app": ["routers", "services", "models", "db", "auth", "tasks"],
    })

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    displays = {m["display"] for m in summary["module_list"]}
    assert displays == {
        "frontend/components", "frontend/pages", "frontend/hooks",
        "frontend/api", "frontend/state", "frontend/utils",
        "backend/routers", "backend/services", "backend/models",
        "backend/db", "backend/auth", "backend/tasks",
    }
    assert summary["top_level_arms"] == ["backend", "frontend"]
    assert sorted(summary["structural_dirs_peeled"]) == ["app", "src"]


def test_deps_strips_src_and_repo_name_from_module_names(repo, tabs, run_script):
    name = Path(repo.path).name
    _write_tree(repo, {f"src/{name}": ["core", "api", "cli", "db", "render", "utils"]})

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    modules = {m["display"]: m["id"] for m in summary["module_list"]}
    assert set(modules) == {"core", "api", "cli", "db", "render", "utils"}
    # The id keeps the real path so citations elsewhere in the atlas resolve.
    assert modules["core"] == f"src/{name}/core"


def test_deps_keeps_a_structural_sounding_dir_that_holds_code(repo, tabs, run_script):
    # "app" is a structural hint, but this one is a module: the code is in it,
    # not merely under it. The pass-through check has to override the name.
    repo.write("backend/app/handlers.py", "x = 1\n" * 200)
    repo.write("backend/app/models.py", "x = 1\n" * 200)
    repo.write("backend/app/helpers/tiny.py", "x = 1\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    ids = {m["id"] for m in summary["module_list"]}
    assert "backend/app" in ids


def test_deps_does_not_over_split_a_flat_repo(repo, tabs, run_script):
    for i in range(8):
        repo.write(f"pkg/f{i}.py", "x = 1\n" * 40)

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert [m["display"] for m in summary["module_list"]] == ["pkg"]


def test_deps_assigns_loose_files_to_the_directory_holding_them(repo, tabs, run_script):
    repo.write("main.py", "x = 1\n" * 10)
    _write_tree(repo, {"frontend/src": ["a", "b"], "backend/app": ["c", "d"]})

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    by_display = {m["display"]: m for m in summary["module_list"]}
    assert by_display["(root)"]["files"] == 1
    assert sum(m["files"] for m in summary["module_list"]) == 13


def test_deps_depth_override_counts_only_non_structural_dirs(repo, tabs, run_script):
    _write_tree(repo, {"frontend/src": ["components", "pages"], "backend/app": ["routers", "db"]})

    summary = json.loads(
        run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs, "--depth", "1").stdout)

    assert {m["display"] for m in summary["module_list"]} == {"frontend", "backend"}


# --------------------------------------------------------------------------- #
# LLM Ops
#
# In a codebase whose behavior is partly written in English, the model calls are
# load-bearing and invisible to an import graph. The tab is facts only — where
# the model is called, which model, which parameters, fed by which prompt file.
# --------------------------------------------------------------------------- #


def test_llm_tab_reports_call_sites_models_and_prompt_lineage(repo, tabs, run_script, fragment):
    repo.write("agent/run.py",
               "import anthropic\n"
               "SYSTEM = open('prompts/system.md').read()\n"
               "client = anthropic.Anthropic()\n"
               "def ask(q):\n"
               "    return client.messages.create(model='claude-opus-5', max_tokens=512,\n"
               "                                  system=SYSTEM, messages=[{'role': 'user', 'content': q}])\n")
    repo.write("prompts/system.md", "You are helpful.\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_llm.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["call_sites"] == 1
    assert summary["providers"] == ["anthropic"]
    assert "claude-opus-5" in summary["models"]
    assert summary["prompt_assets"] == {"prompts/system.md": ["agent/run.py"]}
    assert fragment.title(tabs / "10-llm-ops.html") == "LLM Ops"
    body = fragment.body(tabs / "10-llm-ops.html")
    assert "agent/run.py:5" in body
    assert "prompts/system.md" in body


def test_llm_tab_lists_gaps_with_citations(repo, tabs, run_script, fragment):
    repo.write("agent/run.py",
               "import anthropic\n"
               "resp = client.messages.create(model='claude-opus-5', messages=msgs)\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_llm.py", repo.path, "--tabs-dir", tabs).stdout)

    kinds = {g["kind"] for g in summary["gaps"]}
    assert {"no-max-tokens", "no-timeout-or-retry"} <= kinds
    assert "agent/run.py:2" in fragment.body(tabs / "10-llm-ops.html")


def test_llm_tab_is_dropped_when_the_repo_calls_no_model(repo, tabs, run_script):
    repo.write("app/math.py", "def add(a, b):\n    return a + b\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_llm.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["note"] == "no LLM usage detected"
    assert not (tabs / "10-llm-ops.html").exists()


def test_llm_summary_carries_the_detection_caveat(repo, tabs, run_script):
    repo.write("agent/run.py", "resp = client.messages.create(model='claude-opus-5', max_tokens=8)\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_llm.py", repo.path, "--tabs-dir", tabs).stdout)

    assert "textually" in summary["caveat"]


def test_extract_tabs_gives_the_llm_tab_its_canonical_number(tmp_path, run_script, load_module):
    extract = load_module(SCRIPTS, "extract_tabs")

    assert extract.CANONICAL_PREFIX["llm-ops"] == 10


# --------------------------------------------------------------------------- #
# Hotspots (churn x complexity, so it needs real git history)
# --------------------------------------------------------------------------- #


def test_hotspots_ranks_churn_above_stable_code(repo, tabs, run_script):
    for i in range(5):
        repo.write("hot.py", f"def f(x):\n    if x:\n        return {i}\n    return 0\n")
        repo.commit(f"change {i}")
    repo.write("cold.py", "def g():\n    return 1\n")
    repo.commit("add cold")

    summary = json.loads(run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs).stdout)

    ranked = [h["path"] for h in summary["top_hotspots"]]
    assert ranked[0] == "hot.py"
    by_path = {h["path"]: h for h in summary["top_hotspots"]}
    assert by_path["hot.py"]["churn"] == 5
    assert by_path["cold.py"]["churn"] == 1
    assert by_path["hot.py"]["score"] > by_path["cold.py"]["score"]


def test_hotspots_since_window_excludes_older_commits(repo, tabs, run_script):
    repo.write("ancient.py", "x = 1\n")
    repo.commit("long ago", date="2015-01-01T00:00:00")
    repo.write("recent.py", "y = 2\n")
    repo.commit("just now")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs, "--since", "30 days ago").stdout
    )

    # Effort follows recent change, so the window must actually drop old commits.
    # Files outside it still appear, but with no churn credited to them.
    assert summary["commits_analyzed"] == 1
    churn = {h["path"]: h["churn"] for h in summary["top_hotspots"]}
    assert churn == {"recent.py": 1, "ancient.py": 0}


# --------------------------------------------------------------------------- #
# Codemap staleness verdicts
# --------------------------------------------------------------------------- #


def codemap_state(run_script, repo):
    result = run_script(SCRIPTS / "check_codemap_state.py", repo.path, expect_rc=None)
    return json.loads(result.stdout), result.returncode


def test_codemap_missing_reports_rc_2(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "missing"
    assert rc == 2


def test_codemap_current_when_meta_sha_is_head(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">generated from {sha}</div></html>\n')
    repo.commit("codemap")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "current"
    assert state["meta_sha"] == sha
    assert rc == 0


def test_codemap_stale_after_a_source_commit(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">generated from {sha}</div></html>\n')
    repo.commit("codemap")
    repo.write("m2.py", "y = 2\n")
    repo.commit("more source")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "stale"
    assert state["files_changed_since"] >= 1
    assert rc == 1


def test_codemap_generated_docs_do_not_count_as_source_change(repo, run_script):
    """A codemap that only ever changed itself is current, not stale."""
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div></html>\n')
    repo.commit("codemap")
    repo.write("docs/pr-7.html", "<html>report</html>\n")
    repo.commit("a generated pr report")

    state, rc = codemap_state(run_script, repo)

    assert state["files_changed_since"] == 0
    assert state["generated_docs_changed_since"] >= 1
    assert state["verdict"] == "current"
    assert rc == 0


def test_codemap_unknown_vintage_without_a_resolvable_sha(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")
    repo.write("docs/codemap.html", '<html><div class="doc-meta">no sha here</div></html>\n')
    repo.commit("codemap")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "unknown-vintage"
    assert rc == 1


def test_codemap_conflict_markers_beat_every_other_verdict(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")
    repo.write("docs/codemap.html", "<html>\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n</html>\n")
    repo.commit("committed a conflict")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "conflict-markers"
    assert "rebuild" in state["detail"]
    assert rc == 1


def test_codemap_merge_commit_is_flagged_as_a_suspect_splice(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div>main</html>\n')
    repo.commit("codemap on main")
    main = repo.git("rev-parse", "--abbrev-ref", "HEAD").strip()
    repo.git("checkout", "-q", "-b", "side", "HEAD~1")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div>side</html>\n')
    repo.commit("codemap on side")
    repo.git("checkout", "-q", main)
    repo.git("merge", "-q", "-X", "ours", "side", "-m", "merge side")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "merge-resolution-suspect"
    assert state["last_commit_touching_codemap"]["is_merge"] is True
    # Both parents are recorded so a reviewer can diff the two revisions.
    assert state["last_commit_touching_codemap"]["parent1"]
    assert state["last_commit_touching_codemap"]["parent2"]
    assert rc == 1


# --------------------------------------------------------------------------- #
# Citation verification
# --------------------------------------------------------------------------- #


def test_citations_resolve_and_report_the_cited_line(repo, tabs, run_script):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<p>See <code>src/core.py:2</code>.</p>\n", encoding="utf-8"
    )

    report = json.loads(run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs).stdout)

    assert report["citations_ok"] == 1
    assert report["citations_broken"] == 0
    (citation,) = report["citations"]
    assert citation["status"] == "ok"
    assert citation["resolved"] == "src/core.py"
    # The checker prints the cited line so a reader can judge whether it still
    # supports the claim — that content is the whole point of the tab.
    assert citation["cited_content"][0]["content"] == ["    return text.strip()"]


@pytest.mark.parametrize(
    "citation, expected",
    [
        ("src/core.py:99", "line-out-of-range"),
        ("src/nope.py:1", "missing"),
    ],
)
def test_citations_report_broken_references(repo, tabs, run_script, citation, expected):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        f"<!-- tab: Overview -->\n<p>See <code>{citation}</code>.</p>\n", encoding="utf-8"
    )

    result = run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs, expect_rc=1)

    assert expected in result.stdout


def test_citations_exit_zero_when_everything_resolves(repo, tabs, run_script):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<p>See <code>src/core.py:1</code>.</p>\n", encoding="utf-8"
    )

    run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs, expect_rc=0)


# --------------------------------------------------------------------------- #
# assemble <-> extract_tabs round trip
# --------------------------------------------------------------------------- #


def test_assembled_atlas_round_trips_back_to_fragments(tmp_path, tabs, run_script, fragment):
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<h2>Hi</h2><p>body one</p>\n", encoding="utf-8"
    )
    tabs.joinpath("05-risks.html").write_text(
        "<!-- tab: Risks &amp; Gaps -->\n<h2>Risks</h2><ul><li>x</li></ul>\n", encoding="utf-8"
    )
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas,
               "--title", "demo — Codebase Atlas", "--meta", "generated at deadbee")
    recovered = tmp_path / "recovered"
    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", recovered)

    extracted = json.loads(result.stdout)
    assert extracted["title"] == "demo — Codebase Atlas"
    assert extracted["meta"] == "generated at deadbee"
    # Known tabs keep their canonical prefix; a custom tab ("risks" is not a
    # canonical atlas tab) takes the first free slot at its display position.
    assert extracted["fragments"] == ["01-overview.html", "02-risks.html"]
    assert fragment.title(recovered / "01-overview.html") == "Overview"
    assert "body one" in fragment.body(recovered / "01-overview.html")
    assert "<li>x</li>" in fragment.body(recovered / "02-risks.html")


def test_assemble_skips_empty_fragments(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>real</p>\n", encoding="utf-8")
    tabs.joinpath("02-empty.html").write_text("<!-- tab: Empty -->\n\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"

    result = run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    assert 'data-tab="overview"' in atlas.read_text(encoding="utf-8")
    assert 'data-tab="empty"' not in atlas.read_text(encoding="utf-8")
    assert "1 tabs" in result.stdout or "1 tab" in result.stdout


def test_atlas_survives_the_mermaid_cdn_being_unreachable(tmp_path, tabs, run_script):
    """Diagrams render via a CDN, so the atlas must degrade, not break, offline.

    Mermaid is fetched at runtime rather than vendored. That is a real network
    dependency, and the only thing making it acceptable is the fallback that
    shows the diagram source as preformatted text when the fetch fails. This
    pins the fallback so it cannot be dropped silently.
    """
    tabs.joinpath("01-overview.html").write_text(
        '<!-- tab: Overview -->\n<pre class="mermaid">graph TD; a--&gt;b;</pre>\n', encoding="utf-8"
    )
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    html = atlas.read_text(encoding="utf-8")
    # Nothing is fetched while parsing: the loader is injected by script.
    assert "<script src=" not in html
    assert "cdn.jsdelivr.net/npm/mermaid" in html
    assert "onerror" in html, "no fallback if the mermaid fetch fails"
    # The diagram source survives in the document either way.
    assert "graph TD" in html


def test_assemble_defaults_to_the_atlas_label(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>x</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    assert "CODEBASE ATLAS" in atlas.read_text(encoding="utf-8")


def test_extraction_preserves_canonical_numbering_when_a_tab_was_dropped(tmp_path, tabs, run_script):
    # An atlas without git history legitimately has no Hotspots (04). Under
    # display-order renumbering, extraction used to shift flows/boundaries/...
    # down one slot; re-running the analyzers then wrote a NEW 04-hotspots
    # beside the shifted 04-flows and the documented
    # `--fragments 01,05,06,07,08` verified the wrong set, silently skipping
    # Critical Flows. Canonical prefixes must survive the round trip.
    for name, title in [
        ("01-overview.html", "Overview"), ("02-inventory.html", "Inventory"),
        ("03-dependencies.html", "Dependencies"),  # 04-hotspots deliberately absent
        ("05-flows.html", "Critical Flows"), ("06-boundaries.html", "Boundaries &amp; Ownership"),
        ("07-invariants.html", "Invariants &amp; Risks"), ("08-glossary.html", "Glossary"),
    ]:
        tabs.joinpath(name).write_text(f"<!-- tab: {title} -->\n<p>{name}</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"
    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    recovered = tmp_path / "recovered"
    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", recovered)

    extracted = json.loads(result.stdout)
    assert extracted["fragments"] == [
        "01-overview.html", "02-inventory.html", "03-dependencies.html",
        "05-flows.html", "06-boundaries.html", "07-invariants.html", "08-glossary.html",
    ]


def test_hotspots_correct_when_target_is_a_subdirectory_of_a_repo(repo, tabs, run_script):
    # git log paths are toplevel-relative; analyzing a subdirectory used to
    # match them against subdir-relative paths, giving every file churn 0 and
    # a confidently wrong tab.
    for i in range(3):
        repo.write("service/api.py", f"def handler(x):\n    if x:\n        return {i}\n    return 0\n")
        repo.commit(f"change {i}")
    repo.write("unrelated/other.py", "x = 1\n")
    repo.commit("outside the subtree")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_hotspots.py", repo.path / "service", "--tabs-dir", tabs).stdout
    )

    by_path = {h["path"]: h for h in summary["top_hotspots"]}
    assert by_path["api.py"]["churn"] == 3
    assert "unrelated/other.py" not in by_path
    assert summary["commits_analyzed"] == 3  # the outside-subtree commit is not counted


def test_hotspots_reports_shallow_history_flag(repo, tabs, run_script):
    repo.write("a.py", "x = 1\n")
    repo.commit("one")

    summary = json.loads(run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["shallow_history"] is False


# --------------------------------------------------------------------------- #
# Coverage tab (renders existing artifacts; never runs tests)
# --------------------------------------------------------------------------- #

COBERTURA = """<?xml version="1.0"?>
<coverage>
  <packages><package name="src"><classes>
    <class filename="src/core.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="1"/>
      <line number="3" hits="0"/><line number="4" hits="0"/>
    </lines></class>
    <class filename="src/util.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="1"/>
    </lines></class>
  </classes></package></packages>
</coverage>
"""


def test_coverage_tab_renders_cobertura(repo, tabs, run_script, fragment):
    repo.write("src/core.py", "def f(x):\n    if x:\n        return 1\n    return 0\n")
    repo.write("src/util.py", "def g():\n    return 2\n")
    repo.write("coverage.xml", COBERTURA)

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["format"] == "cobertura"
    assert summary["files_measured"] == 2
    assert summary["overall_line_coverage_pct"] == 66.7  # 4 of 6 lines
    assert fragment.title(tabs / "09-coverage.html") == "Coverage"
    assert "50%" in fragment.body(tabs / "09-coverage.html")  # core.py 2/4


def test_coverage_absent_asks_instead_of_fabricating(repo, tabs, run_script):
    repo.write("src/core.py", "def f():\n    return 1\n")

    result = run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs)

    summary = json.loads(result.stdout)
    assert "ask_user" in summary
    assert not (tabs / "09-coverage.html").exists()


def test_coverage_hints_at_convertible_artifacts(repo, tabs, run_script):
    repo.write("src/core.py", "def f():\n    return 1\n")
    repo.write(".coverage", "sqlite-ish blob")

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert any("coverage xml" in h for h in summary["hints"])


def test_coverage_lcov_and_unmeasured_files(repo, tabs, run_script):
    repo.write("src/a.js", "export const a = () => 1;\n")
    repo.write("src/b.js", "export const b = () => { if (x) { return 1; } return 2; };\n")
    repo.write("lcov.info", "SF:src/a.js\nDA:1,1\nDA:2,0\nend_of_record\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["format"] == "lcov"
    assert summary["files_measured"] == 1
    assert "src/b.js" in summary["unmeasured_complex"]


def test_extraction_gives_coverage_tab_its_canonical_prefix(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>o</p>\n", encoding="utf-8")
    tabs.joinpath("09-coverage.html").write_text("<!-- tab: Coverage -->\n<p>c</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"
    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", tmp_path / "rec")

    assert json.loads(result.stdout)["fragments"] == ["01-overview.html", "09-coverage.html"]
