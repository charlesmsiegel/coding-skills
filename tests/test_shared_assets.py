"""What must stay identical across skills for the document set to read as one.

Each skill directory is zipped and installed on its own, so shared material is
copied rather than imported. The repo's answer to that is not trust, it is this
kind of test — the same one CI already applies to code-visualization and
pr-visualization's shared scripts.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

TEMPLATES = (
    SKILLS / "code-overview" / "assets" / "template.html",
    SKILLS / "code-visualization" / "assets" / "template.html",
    SKILLS / "science-investigation" / "assets" / "template.html",
)


def token_blocks(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    root = re.search(r":root\{.*?\n\}", text, re.S)
    light = re.search(r"@media \(prefers-color-scheme: light\)\{.*?\n\s*\}\n\}", text, re.S)
    assert root and light, f"{path.name} is missing its design-token blocks"
    return root.group(0) + "\n" + light.group(0)


def test_every_template_shares_one_palette():
    canonical = token_blocks(TEMPLATES[0])

    for path in TEMPLATES[1:]:
        assert token_blocks(path) == canonical, (
            f"{path.relative_to(ROOT)} has drifted from code-overview's tokens. The four "
            "documents in an overview sit in one nav bar; a divergent palette is visible "
            "the moment two of them are opened side by side."
        )


def test_the_shell_carries_both_slot_families():
    text = TEMPLATES[0].read_text(encoding="utf-8")

    for slot in ("<!--DOC_TITLE-->", "<!--DOC_LABEL-->", "<!--DOC_SUBTITLE-->",
                 "<!--DOC_META-->", "<!--DOC_NAV-->", "<!--DOC_BODY-->",
                 "<!--DOC_FOOTER-->", "<!--TABS_NAV-->", "<!--TABS_PANELS-->"):
        assert slot in text, f"{slot} missing — one shell must serve tabbed and untabbed pages"


def test_an_untabbed_page_hides_the_empty_tab_bar():
    text = TEMPLATES[0].read_text(encoding="utf-8")

    assert "nav.tabs:empty" in text, (
        "summary.html leaves TABS_NAV empty; without this rule the bar renders as a "
        "stray line under the header"
    )


def test_the_measurement_body_scaffold_is_identical_in_both_skills():
    ours = SKILLS / "code-overview" / "assets" / "measurement-body.html"
    theirs = SKILLS / "science-investigation" / "assets" / "measurement-body.html"

    assert ours.read_bytes() == theirs.read_bytes(), (
        "code-overview forces this scaffold on build_measurement.py; if the copies differ, "
        "the page it forces is not the page the skill was tested against"
    )


def test_the_letter_bands_are_identical_in_both_rubrics():
    def bands(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"GRADE_BANDS[^=]*=\s*\((.*?)\n\)", text, re.S)
        assert match, f"{path} has no GRADE_BANDS block"
        return re.sub(r"\s+", "", match.group(1))

    overview = bands(SKILLS / "code-overview" / "scripts" / "rubric.py")
    science = bands(SKILLS / "science-investigation" / "scripts" / "rubric.py")

    assert overview == science, (
        "a B- must mean one score range. The two pages sit side by side in the nav and a "
        "reader compares the letters directly."
    )
