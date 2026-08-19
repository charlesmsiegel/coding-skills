#!/usr/bin/env bash
# Install this repo's skills and output styles into one or more agent directories.
# Run ./install.sh --help for usage.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$REPO_DIR/skills"
STYLES_SRC="$REPO_DIR/styles"
BASE="${HOME}"

targets=""          # space-separated: claude codex kiro
want_skills=0
want_styles=0
skills_all=0        # a bare --skills anywhere means all of them
styles_all=0
skills_req=""       # comma-separated names, empty when none were named
styles_req=""

usage() {
    cat <<'USAGE'
Usage: ./install.sh (--claude|--codex|--kiro)... (--skills[=LIST]|--styles[=LIST])... [--dest DIR]

Targets (at least one):
  --claude           install into .claude/
  --codex            install into .codex/
  --kiro             install into .kiro/

Content (at least one):
  --skills [LIST]    install skills. Bare installs all; a comma-separated
                     LIST installs only those.
  --styles [LIST]    install output styles. Bare installs all; a comma-separated
                     LIST installs only those.

  --dest DIR         base directory holding .claude/.codex/.kiro
                     (default: $HOME; pass a project root to install per-project)
  -h, --help         show this message

Where things land:
  skills    .claude/skills/   .codex/skills/   .kiro/skills/
  styles    .claude/output-styles/    .kiro/steering/

  Kiro has no output styles; a style installs as a steering file named
  style-<name>.md with its frontmatter rewritten to `inclusion: manual`,
  so it is referenced as #style-<name> rather than being always on.

  Codex has no output-style equivalent. --codex --styles prints a warning
  and skips them; skills still install.

Each item is replaced wholesale on re-install (stale files from an older
version are removed). A skill is copied with LICENSE alongside it, mirroring
the release artifact.

Examples:
  ./install.sh --claude --skills --styles
  ./install.sh --claude --skills 'fix-pr,brutal-review'
  ./install.sh --kiro --styles 'blunt,peer' --dest ~/proj
USAGE
}

die() { echo "error: $*" >&2; usage >&2; exit 2; }

# --- argument parsing ---------------------------------------------------------
#
# --skills and --styles take an OPTIONAL value, which bash gives no help with:
# `--skills --claude` is a bare flag followed by a target, while `--skills a,b`
# is a flag with a list. The next token is a value only when it does not start
# with `-`. Getting this wrong installs everything when two things were asked
# for, so tests/test_install.py pins both forms.
while [ $# -gt 0 ]; do
    case "$1" in
        --claude|--codex|--kiro) targets="$targets ${1#--}" ;;
        --skills|--styles|--skills=*|--styles=*)
            kind="${1%%=*}"; kind="${kind#--}"
            value=""
            case "$1" in
                *=*)
                    value="${1#*=}"
                    [ -n "$value" ] || die "--$kind= needs a name (drop the '=' to install all)"
                    ;;
                *)
                    if [ $# -ge 2 ]; then
                        case "$2" in
                            -*) : ;;     # the next flag, not a value
                            *)  value="$2"; shift ;;
                        esac
                    fi
                    ;;
            esac
            if [ "$kind" = "skills" ]; then
                want_skills=1
                if [ -n "$value" ]; then skills_req="$skills_req,$value"; else skills_all=1; fi
            else
                want_styles=1
                if [ -n "$value" ]; then styles_req="$styles_req,$value"; else styles_all=1; fi
            fi
            ;;
        --dest)
            [ $# -ge 2 ] || die "--dest needs a directory"
            BASE="$2"; shift ;;
        --dest=*)
            BASE="${1#--dest=}"
            [ -n "$BASE" ] || die "--dest needs a directory" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

[ -n "$targets" ] || die "pick at least one target: --claude, --codex, --kiro"
[ "$want_skills" -eq 1 ] || [ "$want_styles" -eq 1 ] \
    || die "pick at least one of --skills, --styles"

if [ "$want_skills" -eq 1 ] && [ ! -d "$SKILLS_SRC" ]; then
    echo "error: $SKILLS_SRC not found — run from a checkout of coding-skills" >&2
    exit 1
fi
if [ "$want_styles" -eq 1 ] && [ ! -d "$STYLES_SRC" ]; then
    echo "error: $STYLES_SRC not found — run from a checkout of coding-skills" >&2
    exit 1
fi

# --- name resolution ----------------------------------------------------------

available_skills() {
    for dir in "$SKILLS_SRC"/*/; do
        if [ -f "$dir/SKILL.md" ]; then
            basename "$dir"
        fi
    done
}

available_styles() {
    for file in "$STYLES_SRC"/*.md; do
        name="$(basename "$file" .md)"
        [ "$name" = "README" ] || echo "$name"
    done
}

# Resolve a requested list against what exists, leaving the answer in RESOLVED.
# Every name is checked before anything is copied: a typo in a list of five must
# not leave four installed and the fifth reported as missing.
#
# RESOLVED rather than stdout on purpose. `exit` inside a command substitution
# ends the subshell, not the script, so a `$(resolve ...)` form would abort only
# by way of set -e propagating the substitution's status — true today, and
# exactly the kind of thing a later edit breaks without noticing.
RESOLVED=""
resolve() {
    kind="$1"; requested="$2"; available="$3"
    selected=""
    invalid=""
    # Unquoted on purpose: commas become spaces, and word splitting then handles
    # both `a,b` and `a, b`, dropping empty entries from `a,,b` for free.
    for name in $(echo "$requested" | tr ',' ' '); do
        if echo "$available" | grep -qxF -- "$name"; then
            case " $selected " in
                *" $name "*) : ;;                       # named twice
                *) selected="$selected $name" ;;
            esac
        else
            invalid="$invalid $name"
        fi
    done
    if [ -n "$invalid" ]; then
        echo "error: no such $kind:$invalid" >&2
        echo "available $kind:" >&2
        echo "$available" | sed 's/^/  /' >&2
        exit 2
    fi
    RESOLVED="$selected"
}

skills_selected=""
styles_selected=""
if [ "$want_skills" -eq 1 ]; then
    if [ "$skills_all" -eq 1 ]; then
        skills_selected="$(available_skills | tr '\n' ' ')"
    else
        resolve skills "$skills_req" "$(available_skills)"
        skills_selected="$RESOLVED"
    fi
fi
if [ "$want_styles" -eq 1 ]; then
    if [ "$styles_all" -eq 1 ]; then
        styles_selected="$(available_styles | tr '\n' ' ')"
    else
        resolve styles "$styles_req" "$(available_styles)"
        styles_selected="$RESOLVED"
    fi
fi

# --- install ------------------------------------------------------------------

install_skills() {
    dest="$1"
    mkdir -p "$dest"
    for name in $skills_selected; do
        rm -rf "${dest:?}/$name"
        cp -R "$SKILLS_SRC/$name" "$dest/$name"
        if [ -f "$REPO_DIR/LICENSE" ]; then
            cp "$REPO_DIR/LICENSE" "$dest/$name/LICENSE"
        fi
        find "$dest/$name" -type d -name "__pycache__" -prune -exec rm -rf {} +
        echo "installed skill $name -> $dest/$name"
    done
}

install_claude_styles() {
    dest="$1"
    mkdir -p "$dest"
    for name in $styles_selected; do
        cp "$STYLES_SRC/$name.md" "$dest/$name.md"
        echo "installed style $name -> $dest/$name.md"
    done
}

# Kiro steering files use a different frontmatter vocabulary than Claude Code
# output styles, so the body ports and the header is rewritten. `inclusion:
# manual` makes the style opt-in per conversation (#style-<name>); edit the
# installed file to `inclusion: always` to make it permanent. The style- prefix
# keeps these clear of Kiro's own product.md / tech.md / structure.md.
install_kiro_styles() {
    dest="$1"
    mkdir -p "$dest"
    for name in $styles_selected; do
        awk '
            BEGIN { print "---"; print "inclusion: manual"; print "---" }
            NR == 1 && $0 == "---" { in_fm = 1; next }
            in_fm && $0 == "---"   { in_fm = 0; next }
            in_fm                  { next }
                                   { print }
        ' "$STYLES_SRC/$name.md" > "$dest/style-$name.md"
        echo "installed style $name -> $dest/style-$name.md (inclusion: manual)"
    done
}

for target in $targets; do
    if [ "$want_skills" -eq 1 ]; then
        install_skills "$BASE/.$target/skills"
    fi
    if [ "$want_styles" -eq 1 ]; then
        case "$target" in
            claude) install_claude_styles "$BASE/.claude/output-styles" ;;
            kiro)   install_kiro_styles "$BASE/.kiro/steering" ;;
            codex)
                echo "warning: Codex does not support styles — skipping them for --codex" >&2
                echo "         (see styles/README.md for the manual workarounds)" >&2
                ;;
        esac
    fi
done
