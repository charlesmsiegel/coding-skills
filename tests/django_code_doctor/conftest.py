"""Makes `helpers.py` importable from the test modules in this directory.

The repo runs pytest with `--import-mode=importlib`, which deliberately does
not put a test file's own directory on sys.path — that is what keeps
same-named test modules under different skill directories from colliding. The
cost is that a plain sibling module is not importable either, so this puts just
this one directory on the path.
"""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
