# Resource dependencies and an LLM Ops tab

**Date:** 2026-07-28
**Skills touched:** code-visualization, pr-visualization

## Problem

`analyze_deps.py` only records *import* edges between *code* files. Its first pass
(`analyze_deps.py:284`) drops every file whose language is `Markdown`, `JSON`, `YAML`,
`TOML`, `HTML`, `CSS`, or `Other`, so a Jinja template, a `prompts/*.md` file, a `.sql`
query, or a JSON schema cannot even be a node — let alone the target of an edge.

In a codebase where behavior is assembled at runtime from files (templates rendered,
prompts concatenated, schemas loaded), the dependency graph therefore understates
coupling in exactly the places that matter. A file that loads `prompts/router.md` depends
on it as surely as one that imports a module: change the file, change the behavior.

The same blindness costs pr-visualization: a PR that rewrites a prompt shows as an
isolated docs-ish change with no blast radius and no contract delta.

Secondarily, LLM call sites themselves are invisible in the atlas — models, parameters,
tool definitions, and the prompt assets feeding each call are facts a reader needs and
that can be extracted deterministically.

## Design

Two new **shared** modules (byte-identical in both skills, joining the set CI enforces at
`.github/workflows/ci.yml:38`) do pure detection and return data. Each skill's own
analyzers decide what to render.

### 1. `scripts/resources.py` — runtime resource references

Detects edges from a code or template file to a repo file it *loads* rather than imports.

```python
ResourceRef = NamedTuple("ResourceRef", src: str, dst: str, line: int,
                         kind: str, token: str)

def loader_roots(repo: Path, files: set[str]) -> list[str]
def resource_edges(repo: Path, files: dict[str, Path], roots: list[str]) -> list[ResourceRef]
```

`files` is the **full** file index (code and non-code alike), keyed by repo-relative
POSIX path — the caller supplies it, so the module never walks the tree itself.

**The invariant that keeps this honest: an edge is emitted only when the target file
exists on disk.** No edge is ever inferred from a string that does not resolve. This is
what makes a language-agnostic literal scan safe.

Five detection layers, each tagged as a `kind`:

| kind | what it matches |
|---|---|
| `literal` | any quoted string in a source file that resolves to a repo file |
| `pattern` | a literal containing `{…}`, `%s`, `${…}`, or `*`, converted to a glob |
| `loader-root` | roots contributed by loader constructions (see below), used during resolution |
| `template-include` | `{% extends %}` / `{% include %}` / `{% import %}` / `{% from %}` / `{{> partial }}` inside a template file |
| `embed` | `//go:embed PATTERN`, `include_str!("…")`, `include_bytes!("…")`, `new URL('./x', import.meta.url)` |

**Resolution order** for a candidate string, first hit wins:

1. relative to the citing file's directory
2. relative to the repo root
3. relative to each loader root (see below), longest root first
4. unique basename match across the whole index (only when exactly one file in the repo
   bears that basename)

Resolution of a string that resolves to the citing file itself is dropped.

**Loader roots** are directories that a template/prompt loader searches, so that
`render_template("index.html")` resolves against `templates/`. Two sources:

- **Detected**: `FileSystemLoader(...)` / `searchpath=`, `PackageLoader(pkg, "dir")`,
  `template_folder=`, Django `TEMPLATES[...]["DIRS"]`, `importlib.resources.files("pkg")`,
  and `Path(__file__).parent / "name"` — each string argument that names an existing
  directory becomes a root.
- **Conventional**: any existing directory in the repo named `templates`, `template`,
  `prompts`, `prompt`, `assets`, `static`, `sql`, `queries`, `schemas`, `fixtures`.

Roots are collected in a first pass over source text, before edge extraction.

**Guards against noise** (all required):

- A candidate literal must be 2–200 characters and must contain either a `/` or a file
  extension — a bare word is never a path candidate.
- Strings containing `://` are skipped (URLs).
- `pattern` globs are rejected unless at least one literal path segment precedes the
  first wildcard, and each pattern contributes at most **25** target edges (excess is
  counted and reported, never silently dropped).
- Files over `walk_source`'s size cap are not read at all (the caller's index already
  excludes them); files whose read yields a NUL byte in the first 1 KB are treated as
  binary and skipped.
- At most **400** resolved refs per source file, so a generated data file full of paths
  cannot dominate the graph. The overflow count is reported.

Every layer records a 1-based `line`, so the atlas can cite `path/file.py:LINE` for
"this is where the template is loaded" and `verify_citations.py` can check it.

### 2. `scripts/llmops.py` — LLM call sites and prompt lineage

Also pure detection, no HTML:

```python
CallSite = NamedTuple(path, line, provider, api, model, params: dict, snippet)

def call_sites(files: dict[str, Path], texts) -> list[CallSite]
def model_literals(text: str) -> list[tuple[int, str]]
def inline_prompts(path, text) -> list[tuple[int, int]]   # (line, chars)
def gaps(sites, refs) -> list[dict]
```

- **Providers/APIs**: `anthropic`, `openai`, `google.generativeai` / `genai`, `mistralai`,
  `cohere`, `ollama`, `litellm`, `langchain*`, `boto3` `bedrock-runtime`, `vertexai`;
  raw HTTP to `api.anthropic.com` / `api.openai.com` / `generativelanguage.googleapis.com`;
  CLI shells (`claude -p`, `codex exec`, `ollama run`).
- **Call methods**: `messages.create`, `messages.stream`, `chat.completions.create`,
  `responses.create`, `generate_content`, `.invoke(`, `.complete(`, `converse(`.
- **Model ids**: literals matching `claude-*`, `gpt-*`, `o[134]-*`, `gemini-*`, `llama*`,
  `mistral*`, `deepseek*`, plus any string assigned to `model=` / `"model":`.
- **Params observed near a call site** (same statement / next 15 lines): `temperature`,
  `max_tokens`, `top_p`, `stream`, `timeout`, `tools`, `tool_choice`, `response_format`,
  `system`, `cache_control`.
- **Prompt assets**: resource refs (from `resources.py`) whose `src` is a call-site file,
  plus inline triple-quoted / template-literal strings over 400 characters in such files.
- **Gaps** (mechanical, each cited): call site with no `timeout` and no retry wrapper in
  the enclosing function; no `max_tokens` on an API that requires or honors one;
  no `max_tokens` on an Anthropic messages call (where the API requires it — for other
  providers a missing `max_tokens` is reported as unbounded output, not an error);
  model id hardcoded at the call site rather than read from config; prompt built by
  concatenating a non-literal into a system prompt (injection surface).

Detection is textual and says so: both consumers repeat a `caveat` field, in the same
spirit as `analyze_blast_radius.py`'s existing caveat.

### 3. code-visualization: `analyze_deps.py`

- Build **two** indexes from one walk: `code_files` (as today) and `all_files`.
- Call `resources.py` with `all_files`; keep refs whose `dst` is in `all_files`.
- **Nodes**: code files as today, **plus** every non-code file that is the target of at
  least one ref. Unreferenced non-code files are not nodes. Referenced assets are counted
  into `locs`, so they participate in module partitioning and node sizing.
- **Edges**: resource refs are folded into module-level links alongside imports. A
  module-pair link carries `weight` (import edges) and `resWeight` (resource edges); its
  `kind` is `"import"`, `"resource"`, or `"mixed"`.
- **Cycles**: Tarjan runs on the union graph (imports ∪ resources), at both module and
  file level, so a `{% extends %}` cycle is caught. A cycle whose edges are wholly or
  partly resource edges is labeled as such in the callout text.
- **New section, "Runtime resources"**, below the fan-in/fan-out grid:
  - table: asset → number of loaders → loader `file:line` list (top 25 assets by loader
    count);
  - **orphan assets**: files with the same extension, living in a directory that already
    contains at least one referenced asset, that nothing references (cap 30). A prompt
    file nothing loads is a finding; this definition avoids listing every README.
- **Summary JSON** gains: `resource_edges` (count), `resource_kinds` (count per kind),
  `top_referenced_assets`, `orphan_assets`, `asset_nodes` (count and LOC contributed),
  `resource_caveat`, and truncation counters for the caps above.

### 4. code-visualization: `analyze_llm.py` (new)

Writes `10-llm-ops.html` (tab id `llm-ops`, title "LLM Ops"). Integer slot 10 is chosen
so no existing fragment renumbers; it displays after Coverage. `extract_tabs.py`'s
`CANONICAL_PREFIX` gains `"llm-ops": 10`.

Content: KPIs (call sites · providers · distinct models · prompt assets); a call-site
table (`file:line`, provider, api, model, params present); a model inventory (model id →
call sites); a prompt-asset → call-site map built from resource refs; and a gaps list.

When the repo has no detected LLM usage the script writes **no fragment** and prints a
summary with `"note": "no LLM usage detected"` — the assembler already drops absent tabs,
so honest degradation needs no extra machinery.

### 5. pr-visualization

- `analyze_blast_radius.py`: new **Runtime resources** section. For each changed file that
  is the target of resource refs, list its loaders — flagging the ones the PR did *not*
  touch, which is the blast radius of a prompt/template edit. Summary gains
  `changed_resources` with the existing caveat repeated.
- `analyze_diff.py` (Contracts & Tests tab): changed prompt/template assets get contract
  rows ("prompt/template contract"), as do added/removed model ids and changed LLM params
  detected by `llmops.py` on the diff's added/removed lines.

### 6. Renderer

Both `assets/template.html` force-graph renderers (code-visualization ~281-289,
pr-visualization ~296-304) get the identical edit: a link with `kind === "resource"` draws
with `stroke-dasharray` and a slightly lower opacity; `"mixed"` draws solid (imports
dominate). Legend gains "dashed = runtime resource load". No other renderer behavior
changes; the two files stay in lockstep as the README claims.

### 7. Plumbing

- `.github/workflows/ci.yml:38` sync list gains `resources.py` and `llmops.py`.
- `skills/code-visualization/SKILL.md`: step 2 gains the `analyze_llm.py` command; the
  step-4 tab table gains the LLM Ops row; the dependency-extraction paragraph gains the
  resource-edge description and its caveat; step 5's `--fragments` lists are unchanged
  (10 is script-generated, not verified as judgment).
- `skills/pr-visualization/SKILL.md`: step 2/3 text notes the runtime-resource section
  and its caveat.
- `README.md`: the shared-file paragraph lists the two new shared modules.

## Testing

New: `tests/code_visualization/test_resources.py` — each of the five kinds fires on a
fixture repo; a string that does not resolve produces no edge; a URL produces no edge; the
per-pattern and per-file caps truncate and report; the unique-basename rule does not fire
on an ambiguous basename.

New: `tests/code_visualization/test_llmops.py` — a call site is detected per provider
family; params are read; a repo with no LLM usage writes no fragment.

Extended: the existing deps test gains a referenced-asset case (asset becomes a node,
unreferenced asset does not) and a template-include cycle case. The pr-visualization
blast-radius test gains a changed-prompt case with an untouched loader.

## Accepted consequences

- Counting referenced assets into `locs` shifts module partitioning on repos with large
  referenced fixtures. This is correct (a big prompts directory *is* mass) but it will
  visibly change existing atlases.
- Union-graph cycle detection may surface template cycles that read as new regressions in
  a re-generated atlas. The callout distinguishes them by edge kind so a reader is not
  misled into thinking an import cycle appeared.
- Detection is textual: a dynamically computed path with no literal segment is invisible,
  and both new tabs say so rather than implying completeness.
