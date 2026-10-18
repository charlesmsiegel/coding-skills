"""Import deferral analysis reuses file evidence without caching away cycle context."""

import ast
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts'


def test_shared_import_graph_reads_each_module_once(load_module, tmp_path, monkeypatch):
    detector = load_module(SCRIPTS, 'find_local_imports')
    for name, source in {
        'entry': 'import left\nimport right\n',
        'left': 'import shared\n',
        'right': 'import shared\n',
        'shared': 'import leaf\n',
        'leaf': 'value = 1\n',
    }.items():
        (tmp_path / f'{name}.py').write_text(source, encoding='utf-8')
    counts = Counter()
    original = Path.read_text

    def counted(path, *args, **kwargs):
        if path.parent == tmp_path:
            counts[path.name] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', counted)
    assert detector._heavy_chain(ast.parse('import entry').body[0], tmp_path / 'caller.py') is None
    assert counts['shared.py'] == 1
    assert counts['leaf.py'] == 1


def test_cycle_context_does_not_poison_another_import_path(load_module, tmp_path):
    detector = load_module(SCRIPTS, 'find_local_imports')
    first = tmp_path / 'first.py'
    second = tmp_path / 'second.py'
    first.write_text('import second\n', encoding='utf-8')
    second.write_text('import first\nimport torch\n', encoding='utf-8')
    assert detector._load_cost(str(first.resolve()), frozenset({str(second.resolve())})) is None
    assert 'torch' in detector._load_cost(str(first.resolve()))


def test_heavy_import_chain_still_preserves_its_evidence(load_module, tmp_path):
    detector = load_module(SCRIPTS, 'find_local_imports')
    (tmp_path / 'shared.py').write_text('import torch\n', encoding='utf-8')
    chain = detector._heavy_chain(ast.parse('import shared').body[0], tmp_path / 'caller.py')
    assert chain.startswith('shared') and chain.endswith('torch')
