"""Type-only dependencies are absent from the runtime import graph."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_import_cycles.py'


def cycles(tmp_path, source):
    (tmp_path / 'a.py').write_text(source)
    (tmp_path / 'b.py').write_text('import a\n')
    result = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    return [record for record in json.loads(result.stdout) if record['smell_type'] == 'circular_import']


@pytest.mark.parametrize('source', [
    'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import b\n',
    'from typing import TYPE_CHECKING as TC\nif TC:\n    import b\n',
    'from typing_extensions import TYPE_CHECKING as TC\nif TC:\n    import b\n',
    'import typing\nif typing.TYPE_CHECKING:\n    import b\n',
    'import typing as t\nif t.TYPE_CHECKING:\n    import b\n',
    'from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    pass\nelse:\n    import b\n',
])
def test_type_only_imports_do_not_create_cycles(tmp_path, source):
    assert cycles(tmp_path, source) == []


@pytest.mark.parametrize('source', [
    'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import b\n',
    'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import b\nimport b\n',
    'from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    import b\n',
    'TYPE_CHECKING = True\nif TYPE_CHECKING:\n    import b\n',
    'from project.flags import TYPE_CHECKING\nif TYPE_CHECKING:\n    import b\n',
    'import typing\ntyping.TYPE_CHECKING = True\nif typing.TYPE_CHECKING:\n    import b\n',
    'from typing import TYPE_CHECKING\nfrom project.flags import *\nif TYPE_CHECKING:\n    import b\n',
    'from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    import b\n',
    'from typing import TYPE_CHECKING\ndef f(TYPE_CHECKING):\n    if TYPE_CHECKING:\n        import b\n',
    'import typing as t\nt = object()\nif t.TYPE_CHECKING:\n    import b\n',
])
def test_runtime_and_unresolved_guards_retain_cycles(tmp_path, source):
    assert cycles(tmp_path, source)
