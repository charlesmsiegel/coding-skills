"""An atlas assembled into code-overview's page shell must still draw.

code-overview passes its own template to code-visualization's assemble.py so
that its five documents read as one. The renderers used to live inside
code-visualization's template, so that shell produced an atlas whose
Dependencies and Hotspots boxes stayed empty and whose diagrams stayed raw
Mermaid source — and nothing failed. assemble.py now inlines the renderers
(assets/renderers.{css,js}) into whatever shell it is given; these tests pin
that from the assembled page's side, using the analyzers' own fragments.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills"
CV = SKILLS / "code-visualization"
SHELLS = {
    "code-overview": SKILLS / "code-overview" / "assets" / "template.html",
    "code-visualization": CV / "assets" / "template.html",
    "pr-visualization": SKILLS / "pr-visualization" / "assets" / "template.html",
}
FLOWS = ('<!-- tab: Flows -->\n<div class="mermaid-wrap"><div class="mviewport">'
         '<pre class="mermaid">\nflowchart LR\n  a --> b\n</pre></div></div>\n')


@pytest.fixture
def atlas_tabs(repo, tabs, run_script):
    """A real atlas's fragments: the analyzers' Dependencies and Hotspots tabs
    over a small repo, plus a judgment tab carrying a Mermaid diagram."""
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py", "from pkg import b\n\n\ndef f(x):\n    return b.g(x) if x else 0\n")
    repo.write("pkg/b.py", "def g(x):\n    return x + 1\n")
    repo.commit("init")
    repo.write("pkg/b.py", "def g(x):\n    if x > 1:\n        return x\n    return x + 1\n")
    repo.commit("branch")
    run_script(CV / "scripts" / "analyze_deps.py", repo.path, "--tabs-dir", tabs)
    run_script(CV / "scripts" / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs)
    (tabs / "06-flows.html").write_text(FLOWS, encoding="utf-8")
    return tabs


def assemble(run_script, tabs: Path, out: Path, shell: Path) -> str:
    run_script(CV / "scripts" / "assemble.py", "--tabs-dir", tabs, "--out", out,
               "--title", "pkg — Codebase Atlas", "--template", shell)
    return out.read_text(encoding="utf-8")


def renderers_in(page: str) -> tuple[str, str]:
    scripts = re.findall(r'<script id="atlas-renderers">(.*?)</script>', page, re.S)
    styles = re.findall(r'<style id="atlas-renderers-css">(.*?)</style>', page, re.S)
    assert len(scripts) == 1 and len(styles) == 1, (
        f"{len(scripts)} renderer scripts and {len(styles)} renderer stylesheets — "
        "none means empty graph boxes, two means every sort click cancels itself")
    return scripts[0], styles[0]


def test_an_atlas_in_code_overviews_shell_has_a_renderer_for_every_hook(
        atlas_tabs, run_script, tmp_path):
    page = assemble(run_script, atlas_tabs, tmp_path / "codemap.html", SHELLS["code-overview"])
    js, css = renderers_in(page)

    hooks = set(re.findall(r'data-render="([^"]+)"', page))
    assert hooks == {"forcegraph", "treemap"}, "the analyzers' own hooks, not hand-written ones"
    for hook in hooks:
        assert f"el.dataset.render==='{hook}'" in js, f"nothing in the page draws {hook}"
    assert '<pre class="mermaid">' in page and "mermaid.run(" in js
    assert '<table class="sortable">' in page and "querySelectorAll('table.sortable')" in js
    for selector in (".viz{", ".mermaid-wrap{", ".viz-tip{", "table.sortable thead th{"):
        assert selector in css, f"{selector} unstyled — the box collapses or the tip never shows"

    # The stylesheet belongs in the head; the script must run after the panels
    # it draws into exist.
    assert page.index('<style id="atlas-renderers-css">') < page.index("</head>")
    assert (page.index("<main>") < page.index('<script id="atlas-renderers">')
            < page.index("</body>"))


def test_code_overviews_shell_keeps_the_tab_contract_the_renderers_wait_for():
    """A hidden tab's graph is drawn when its panel is first shown, at its real
    size; a shell that never announces the switch leaves those boxes empty."""
    shell = SHELLS["code-overview"].read_text(encoding="utf-8")
    js = (CV / "assets" / "renderers.js").read_text(encoding="utf-8")

    assert "addEventListener('tab:shown'" in js and "classList.contains('active')" in js
    assert "new CustomEvent('tab:shown'" in shell
    assert "classList.toggle('active'" in shell


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_every_shell_gets_exactly_one_copy_of_the_renderers(
        shell, atlas_tabs, run_script, tmp_path):
    page = assemble(run_script, atlas_tabs, tmp_path / "out.html", SHELLS[shell])

    renderers_in(page)
    assert "function renderForceGraph" not in SHELLS[shell].read_text(encoding="utf-8"), (
        "the renderers live in assets/renderers.js only; a second inline copy is "
        "how the two drift")


def test_the_assembled_page_scripts_parse(atlas_tabs, run_script, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = assemble(run_script, atlas_tabs, tmp_path / "codemap.html", SHELLS["code-overview"])

    bodies = re.findall(r"<script(?![^>]*application/json)[^>]*>(.*?)</script>", page, re.S)
    assert len(bodies) >= 2, "the shell's tab script and the renderers"
    for i, body in enumerate(bodies):
        script = tmp_path / f"script{i}.js"
        script.write_text(body, encoding="utf-8")
        result = subprocess.run([node, "--check", str(script)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
