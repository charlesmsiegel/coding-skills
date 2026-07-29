"""LLM call sites, model inventory, prompt lineage, and mechanical gaps.

In a codebase whose behavior is partly written in English, the model calls are
load-bearing and invisible to an import graph: which model, with which limits,
fed by which prompt file. This module extracts those facts textually.

What it reports is checkable — a call site with a file:line, a model id, which
parameters are present. What it does not do is judge prompt quality; the "gaps"
are mechanical absences (no max_tokens, no timeout or retry in scope, a model id
frozen at the call site, a system prompt built by interpolation), each one cited
so a reader can confirm or dismiss it.

Detection is textual: a call reached through a wrapper of your own, or a model id
read from the environment, will not appear. Callers must repeat CAVEAT.

Stdlib only; the caller supplies the file index so this module never walks a tree.
"""
import re
from pathlib import Path
from typing import NamedTuple

from common import CODE_LANGS, detect_lang

CAVEAT = ("LLM call sites are found textually by SDK method name: a call made "
          "through your own wrapper, by an unrecognized client, or with a model "
          "id read from config at runtime will be missed, and a detected "
          "parameter is evidence it is passed somewhere in the call, not that it "
          "holds on every path.")

INLINE_PROMPT_CHARS = 400
MAX_ARG_CHARS = 4000
RETRY_LOOKBACK_LINES = 20

# api name -> (regex, provider implied when the file gives no better answer).
# Only names specific enough to stand alone appear here: an ORM's .create() and
# a queue's .invoke() must never read as a model call.
APIS = [
    ("chat.completions.create", r"chat\.completions\.create", "openai"),
    ("completions.create", r"(?<!chat\.)completions\.create", "openai"),
    ("responses.create", r"responses\.create", "openai"),
    ("messages.create", r"messages\.create", "anthropic"),
    ("messages.stream", r"messages\.stream", "anthropic"),
    ("beta.messages.create", r"beta\.messages\.create", "anthropic"),
    ("generate_content", r"generate_content", "google"),
    ("generateContent", r"generateContent", "google"),
    ("converse", r"\bconverse", "bedrock"),
    ("invoke_model", r"invoke_model", "bedrock"),
    ("chat", r"\bollama\.(?:chat|generate)", "ollama"),
    ("completion", r"\blitellm\.completion|\bacompletion", "litellm"),
]
# The trailing paren is what separates calling the API from naming it: a pattern
# table, a doc example, or an allow-list of method names is not a call site.
API_RE = re.compile("|".join(f"(?P<a{i}>{pat}\\s*\\()" for i, (_, pat, _) in enumerate(APIS)))

# Provider signatures at file level — imports, clients, hostnames, CLI shells.
PROVIDER_SIGNATURES = [
    ("anthropic", r"\banthropic\b|api\.anthropic\.com|AnthropicBedrock|claude\s+-p\b"),
    ("openai", r"\bopenai\b|api\.openai\.com|AzureOpenAI"),
    ("google", r"google\.generativeai|generativelanguage\.googleapis\.com|\bvertexai\b|from\s+google\s+import\s+genai"),
    ("bedrock", r"bedrock-runtime|\bbedrock\b"),
    ("mistral", r"\bmistralai\b"),
    ("cohere", r"\bcohere\b"),
    ("ollama", r"\bollama\b"),
    ("litellm", r"\blitellm\b"),
    ("langchain", r"\blangchain\b"),
]

MODEL_ID_RE = re.compile(
    r"""['"]((?:claude|gpt|o[134]|gemini|llama|mistral|mixtral|deepseek|qwen|"""
    r"""command-r|sonar|grok|phi|gemma)[a-z0-9._-]*(?:-[a-z0-9._]+)*)['"]""",
    re.I)
# A bare family name is a topic, not a deployed model; require a version-ish tail.
MODEL_ID_TAIL_RE = re.compile(r"[-.][0-9a-z]", re.I)

PARAM_KEYS = {
    "model": r"model",
    "max_tokens": r"max_tokens|maxTokens|max_output_tokens|maxOutputTokens|max_completion_tokens",
    "temperature": r"temperature",
    "top_p": r"top_p|topP",
    "stream": r"stream",
    "timeout": r"timeout|deadline|request_timeout",
    "tools": r"tools",
    "tool_choice": r"tool_choice|toolChoice",
    "response_format": r"response_format|responseFormat|output_schema",
    "system": r"system|system_instruction|systemInstruction",
    "cache_control": r"cache_control|cacheControl",
}
PARAM_RES = {k: re.compile(rf"[\s,{{(]({v})\s*[:=]") for k, v in PARAM_KEYS.items()}

MODEL_ARG_RE = re.compile(r"""model\s*[:=]\s*['"]([^'"]+)['"]""")
RETRY_RE = re.compile(r"retry|retries|backoff|tenacity|max_attempts|attempt\b", re.I)
INTERPOLATED_SYSTEM_RE = re.compile(
    r"""(?:system|system_instruction|systemInstruction)\s*[:=]\s*"""
    r"""(?:f['"]|`[^`]*\$\{|['"][^'"]*['"]\s*\+|\w+\s*\+|['"][^'"]*['"]\s*\.format\()""")
LONG_STRING_RE = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|`([^`]*)`', re.S)


class CallSite(NamedTuple):
    path: str
    line: int
    provider: str
    api: str
    model: str | None
    params: dict
    snippet: str


class LlmScan(NamedTuple):
    sites: list
    models: dict
    inline_prompts: list
    prompt_assets: dict
    gaps: list


def _read(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:1024]:
        return ""
    return raw.decode("utf-8", errors="replace")


def _file_provider(text: str) -> str | None:
    """The provider a file points at, or None when it names more than one.

    A router that imports two SDKs cannot be labelled from its imports; the call
    site's own API and model id decide instead.
    """
    hits = [p for p, pattern in PROVIDER_SIGNATURES if re.search(pattern, text, re.I)]
    concrete = [p for p in hits if p not in ("langchain", "litellm")]
    return concrete[0] if len(concrete) == 1 else None


def _call_args(text: str, start: int) -> str:
    """The argument text of the call beginning at/after start, brackets balanced."""
    open_at = text.find("(", start)
    if open_at == -1:
        return ""
    depth, out = 0, []
    for ch in text[open_at:open_at + MAX_ARG_CHARS]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    return "".join(out)


def _model_of(args: str) -> str | None:
    """The model this call names — never one borrowed from elsewhere in the file.

    A call whose model comes from config reports None; the model inventory still
    carries the id, cited where it is actually written.
    """
    m = MODEL_ARG_RE.search(args)
    if m:
        return m.group(1)
    ids = model_literals(args)
    return ids[0][1] if ids else None


def _provider_of(file_provider, api_provider, model):
    if file_provider:
        return file_provider
    if model:
        low = model.lower()
        for prefix, provider in (("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"),
                                 ("o3", "openai"), ("gemini", "google"), ("llama", "meta"),
                                 ("mistral", "mistral"), ("mixtral", "mistral")):
            if low.startswith(prefix):
                return provider
    return file_provider or api_provider


def model_literals(text: str) -> list:
    """(line, model_id) for every string that looks like a deployed model id."""
    out = []
    for m in MODEL_ID_RE.finditer(text):
        ident = m.group(1)
        if MODEL_ID_TAIL_RE.search(ident):
            out.append((text.count("\n", 0, m.start()) + 1, ident))
    return out


def call_sites(rel: str, text: str) -> list:
    provider = _file_provider(text)
    out = []
    for m in API_RE.finditer(text):
        idx = int(m.lastgroup[1:])
        api, _, api_provider = APIS[idx]
        args = _call_args(text, m.start())
        model = _model_of(args)
        params = {k: bool(rx.search(args)) for k, rx in PARAM_RES.items()}
        line = text.count("\n", 0, m.start()) + 1
        out.append(CallSite(
            path=rel, line=line,
            provider=_provider_of(provider, api_provider, model),
            api=api, model=model, params=params,
            snippet=re.sub(r"\s+", " ", args[:160]).strip(),
        ))
    return out


def inline_prompts(rel: str, text: str) -> list:
    out = []
    for m in LONG_STRING_RE.finditer(text):
        body = next((g for g in m.groups() if g is not None), "")
        if len(body) >= INLINE_PROMPT_CHARS:
            out.append({"path": rel, "line": text.count("\n", 0, m.start()) + 1,
                        "chars": len(body)})
    return out


def _retry_in_scope(lines, line: int) -> bool:
    start = max(0, line - 1 - RETRY_LOOKBACK_LINES)
    return any(RETRY_RE.search(ln) for ln in lines[start:line])


def gaps(sites, texts) -> list:
    """Mechanical absences at each call site, each one cited."""
    out = []
    for site in sites:
        cite = f"{site.path}:{site.line}"
        lines = texts.get(site.path, "").splitlines()
        args = site.snippet
        if not site.params["max_tokens"]:
            anthropic_messages = site.provider == "anthropic" and site.api.endswith("messages.create")
            out.append({
                "kind": "no-max-tokens" if anthropic_messages else "unbounded-output",
                "cite": cite,
                "detail": ("the Anthropic messages API requires max_tokens; none is passed here"
                           if anthropic_messages else
                           "no max_tokens: output length is bounded only by the model default"),
            })
        if not site.params["timeout"] and not _retry_in_scope(lines, site.line):
            out.append({"kind": "no-timeout-or-retry", "cite": cite,
                        "detail": "no timeout argument and no retry/backoff in the enclosing lines"})
        if site.model and MODEL_ARG_RE.search(args or ""):
            out.append({"kind": "hardcoded-model", "cite": cite,
                        "detail": f"model id '{site.model}' is fixed at the call site, not configured"})
        if INTERPOLATED_SYSTEM_RE.search(args or ""):
            out.append({"kind": "interpolated-system-prompt", "cite": cite,
                        "detail": "the system prompt is built by interpolation — check what can reach it"})
    return out


def scan(files: dict, refs=()) -> LlmScan:
    """Find LLM call sites, models, inline prompts, prompt assets and gaps.

    ``files`` maps repo-relative POSIX path -> Path. ``refs`` is an iterable of
    (src, dst) pairs — resources.ResourceRef works directly — used to attribute
    prompt files to the call sites that load them.
    """
    sites, models, prompts, texts = [], {}, [], {}
    for rel, path in sorted(files.items()):
        if detect_lang(rel) not in CODE_LANGS:
            continue
        text = _read(path)
        if not text:
            continue
        texts[rel] = text
        found = call_sites(rel, text)
        sites += found
        for line, ident in model_literals(text):
            models.setdefault(ident, []).append(f"{rel}:{line}")
        if found:
            prompts += inline_prompts(rel, text)

    call_files = {s.path for s in sites}
    assets = {}
    for ref in refs:
        src, dst = ref[0], ref[1]
        if src in call_files:
            assets.setdefault(dst, [])
            if src not in assets[dst]:
                assets[dst].append(src)

    return LlmScan(sites=sites, models={k: sorted(v) for k, v in sorted(models.items())},
                   inline_prompts=prompts, prompt_assets=assets,
                   gaps=gaps(sites, texts))
