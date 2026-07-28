#!/bin/bash
set -euo pipefail

# Only needed in Claude Code on the web; local environments manage their own tools.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# The detectors themselves are stdlib-only and need nothing. Dev tooling so the
# session can immediately run the smoke tests and linter:
python3 -m pip install --quiet --disable-pip-version-check pytest ruff
