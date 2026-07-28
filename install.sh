#!/usr/bin/env bash
# Install the skills into one or more agent skill directories.
#
# Usage:
#   ./install.sh --claude [--codex] [--kiro] [--dest DIR]
#
#   --claude    install into .claude/skills/
#   --codex     install into .codex/skills/
#   --kiro      install into .kiro/skills/
#   --dest DIR  base directory holding the .claude/.codex/.kiro dirs
#               (default: $HOME; pass a project root to install per-project)
#
# Flags combine: `./install.sh --claude --codex` installs into both.
# Each skill is replaced wholesale (stale files from an older version are
# removed), mirroring the release artifact: the skill directory plus LICENSE,
# minus caches.
set -euo pipefail

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/skills"
BASE="${HOME}"
targets=()

while [ $# -gt 0 ]; do
    case "$1" in
        --claude) targets+=(".claude/skills") ;;
        --codex)  targets+=(".codex/skills") ;;
        --kiro)   targets+=(".kiro/skills") ;;
        --dest)
            [ $# -ge 2 ] || { echo "error: --dest needs a directory" >&2; exit 2; }
            BASE="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "${#targets[@]}" -eq 0 ]; then
    echo "error: pick at least one of --claude, --codex, --kiro" >&2
    usage >&2
    exit 2
fi

[ -d "$SRC" ] || { echo "error: $SRC not found — run from a checkout of coding-skills" >&2; exit 1; }

for target in "${targets[@]}"; do
    dest="$BASE/$target"
    mkdir -p "$dest"
    for skill_dir in "$SRC"/*/; do
        name="$(basename "$skill_dir")"
        if [ ! -f "$skill_dir/SKILL.md" ]; then
            echo "skip: $name (no SKILL.md)" >&2
            continue
        fi
        rm -rf "${dest:?}/$name"
        cp -R "$skill_dir" "$dest/$name"
        [ -f "$REPO_DIR/LICENSE" ] && cp "$REPO_DIR/LICENSE" "$dest/$name/LICENSE"
        find "$dest/$name" -type d -name "__pycache__" -prune -exec rm -rf {} +
        echo "installed $name -> $dest/$name"
    done
done
