"""health_render.py is the layer under the page builders, and stays under them.

build_health.py was split because it had grown past the size the ratchet
allows, and the split is only worth anything if it holds: the moment
health_render.py imports a page builder back, the two files are one module
again wearing two names, and the next reader has to hold both in their head
to follow either.

The bug-class ratchet in CI catches the outright cycle. These tests catch
the step before it — a renderer reaching for the builder's data, or a
builder keeping its own copy of a rendering helper so the two can drift.
"""

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
HEALTH_RENDER = SCRIPTS / "health_render.py"
BUILD_HEALTH = SCRIPTS / "build_health.py"
BUILD_SUMMARY = SCRIPTS / "build_summary.py"

PAGE_BUILDERS = {"build_health", "build_summary", "build_theory"}


def imported_modules(path: Path) -> set[str]:
    """Every module name `path` imports, by either spelling."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def test_the_renderer_does_not_import_a_page_builder():
    offenders = imported_modules(HEALTH_RENDER) & PAGE_BUILDERS
    assert not offenders, (
        f"health_render.py imports {sorted(offenders)}. The dependency runs one way — page "
        "builders onto renderers — so that a rendering change cannot reach back into the "
        "arithmetic that produced the numbers it is displaying."
    )


def test_both_page_builders_take_their_renderers_from_the_renderer():
    for builder in (BUILD_HEALTH, BUILD_SUMMARY):
        assert "health_render" in imported_modules(builder), (
            f"{builder.name} no longer imports health_render. Its rendering helpers live "
            "in one module precisely so the health page and the summary cannot disagree "
            "about how a grade is drawn."
        )


def test_build_summary_does_not_route_its_renderers_through_build_health():
    """The import moved with the code; it must not route back through the builder.

    `from build_health import render_top_findings` would still work — build_health
    imports the name, so it is an attribute of that module — which is exactly why
    this is worth pinning. It would make build_summary depend on build_health for
    something build_health does not own.
    """
    assert "build_health" not in imported_modules(BUILD_SUMMARY), (
        "build_summary.py imports build_health. It needs rendering helpers, which "
        "health_render.py owns; importing them via build_health re-couples the two "
        "page builders through a module that is only passing them along."
    )


def imported_from_health_render(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "health_render"
            for alias in node.names}


def test_no_builder_shadows_a_renderer_it_imports():
    """A local definition of an imported name is how a split quietly comes undone.

    Deliberately not "no shared names at all": build_summary.py has its own
    `render_caveats`, taking a meta dict and worded for the page readers land
    on, which is a different function from the health page's of the same name.
    Two same-named renderers are fine. One module holding both an import and a
    definition of the name is not — the import silently loses, and the call
    site reads as if it were using the shared one.
    """
    for builder in (BUILD_HEALTH, BUILD_SUMMARY):
        shadowed = imported_from_health_render(builder) & top_level_names(builder)
        assert not shadowed, (
            f"{builder.name} defines {sorted(shadowed)} and also imports it from "
            "health_render.py. Whichever comes second wins, so the page is drawn by a "
            "function other than the one the import says it uses."
        )
