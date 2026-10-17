"""Deferred package imports inherit the cost of initializer-relative imports."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("level", [1, 2, 3])
def test_initializer_relative_import_preserves_heavy_dependency(load_module, tmp_path, level):
    scripts = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"
    detector = load_module(scripts, "find_local_imports")
    package = tmp_path / "pkg"
    nested = package / "sub" / "deep"
    nested.mkdir(parents=True)
    for directory in (package, package / "sub", nested):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    heavy_dir = (nested, nested.parent, package)[level - 1]
    (heavy_dir / "heavy.py").write_text("import torch\n", encoding="utf-8")
    (nested / "__init__.py").write_text(f"from {'.' * level}heavy import Model\n", encoding="utf-8")
    source = tmp_path / "app.py"
    source.write_text("def run():\n    import pkg.sub.deep\n", encoding="utf-8")

    records = detector.analyze_file(source, set())

    assert len(records) == 1
    assert records[0].smell_type == "local_import"
    assert records[0].kind == "candidate"
    assert "torch" in records[0].description
