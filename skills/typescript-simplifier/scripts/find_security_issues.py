#!/usr/bin/env python3
"""
Find the security mistakes that are visible in the source: dynamic evaluation,
HTML injection sinks, shell interpolation, weak crypto and hardcoded secrets.

Everything here is a *lead*, not a verdict — whether a sink is reachable from
untrusted input is a question about the whole system. Each finding names the
sink so a reviewer can answer that question quickly.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, argument_spans, iter_calls

# sink -> (severity, why it matters)
EVAL_SINKS = {
    "eval": ("high", "executes a string as code with the current scope"),
    "Function": ("high", "`new Function(str)` is `eval` with a different name"),
    "setTimeout": ("medium", "a string first argument to setTimeout is evaluated as code"),
    "setInterval": ("medium", "a string first argument to setInterval is evaluated as code"),
    "execScript": ("high", "executes a string as code"),
}

# Node child_process APIs that go through a shell.
SHELL_CALLS = {"exec", "execSync", "spawnSync", "execFile"}

DOM_SINKS = {
    "innerHTML": ("high", "parses and inserts HTML, running any script attributes it contains"),
    "outerHTML": ("high", "parses and inserts HTML, running any script attributes it contains"),
    "insertAdjacentHTML": ("high", "parses and inserts HTML"),
    "dangerouslySetInnerHTML": ("high", "bypasses React's escaping, which is the only thing protecting the page"),
}

WEAK_HASHES = {"md5", "sha1", "MD5", "SHA1", "sha-1", "md4", "rc4", "des"}

# Names whose *value* being a literal is the finding.
SECRET_NAMES = re.compile(
    r"(?:^|[._-])(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|auth[_-]?token|credential)s?$",
    re.IGNORECASE,
)
# Values that are obviously placeholders rather than live credentials.
PLACEHOLDER = re.compile(r"^(?:|x+|\*+|\.{3}|changeme|placeholder|your[_-].*|<.*>|\$\{.*\}|"
                         r"process\.env.*|todo|none|null|test|dummy|example|fake|secret)$", re.IGNORECASE)


def _check_eval(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        name = callee.rsplit(".", 1)[-1]
        if name not in EVAL_SINKS:
            continue
        severity, why = EVAL_SINKS[name]
        spans = argument_spans(file, paren)
        if name in ("setTimeout", "setInterval"):
            if not spans or file.tokens[spans[0][0]].kind not in ("str", "template"):
                continue
        if name == "Function" and not (paren >= 2 and file.tokens[paren - 2].is_name("new")):
            continue
        report.add(file.tokens[paren].line, "dynamic_code_execution",
                   f"`{callee}(…)` — {why}",
                   "Replace it with the concrete operation. If the input is a value, parse it "
                   "(`JSON.parse`); if it is a choice, use a lookup table of functions.", severity)


def _check_shell(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        method = callee.rsplit(".", 1)[-1]
        if method not in SHELL_CALLS:
            continue
        spans = argument_spans(file, paren)
        if not spans:
            continue
        first = file.tokens[spans[0][0]]
        interpolated = first.kind == "template" and "${" in first.value
        concatenated = any(file.tokens[i].is_op("+") for i in range(*spans[0]))
        if not interpolated and not concatenated:
            continue
        report.add(first.line, "shell_injection_risk",
                   f"`{callee}(…)` builds a shell command by interpolation",
                   "Use `execFile`/`spawn` with an argument array so the shell never parses the "
                   "values. String-built commands are one quote away from arbitrary execution.",
                   "high")


def _check_dom_sinks(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in DOM_SINKS:
            continue
        severity, why = DOM_SINKS[token.value]
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        previous = file.tokens[index - 1] if index else None
        is_write = following is not None and following.is_op("=", "+=", "{", "(")
        if not is_write or (previous is not None and previous.is_op("'", '"')):
            continue
        report.add(token.line, "html_injection_sink",
                   f"Writes to `{token.value}` — {why}",
                   "Set `textContent`, or render through the framework. If HTML really is required, "
                   "sanitize with a maintained library (DOMPurify) immediately before the write.",
                   severity)


def _check_weak_crypto(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] not in ("createHash", "createHmac", "digest"):
            continue
        for start, end in argument_spans(file, paren):
            token = file.tokens[start]
            if token.kind == "str" and token.value.strip("'\"").lower() in WEAK_HASHES:
                report.add(token.line, "weak_hash_algorithm",
                           f"`{callee}({token.value})` — {token.value.strip(chr(39) + chr(34))} is broken for security use",
                           "Use SHA-256 or better for integrity, and a password KDF (bcrypt, scrypt, "
                           "argon2) for passwords — a fast hash is the wrong tool for a password.",
                           "high")
    for index, token in enumerate(file.tokens):
        if not token.is_name("random") or index < 2 or not file.tokens[index - 1].is_op("."):
            continue
        if not file.tokens[index - 2].is_name("Math"):
            continue
        context = file.snippet(token.line).lower()
        if any(word in context for word in ("token", "secret", "password", "nonce", "session", "uuid", "id")):
            report.add(token.line, "insecure_randomness",
                       "`Math.random()` used where the value looks security-relevant",
                       "`Math.random` is predictable. Use `crypto.randomUUID()` or "
                       "`crypto.getRandomValues()` for anything an attacker must not guess.", "high")


def _check_hardcoded_secrets(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or not SECRET_NAMES.search(token.value):
            continue
        assign = index + 1
        if assign >= len(file) or not file.tokens[assign].is_op("=", ":"):
            continue
        # `apiKey: string = "…"` — step over the annotation to reach the value.
        if file.tokens[assign].is_op(":"):
            probe = assign + 1
            while probe < len(file) and not file.tokens[probe].is_op("=", ",", ";", ")", "}"):
                probe += 1
            if probe < len(file) and file.tokens[probe].is_op("="):
                assign = probe
        value = file.tokens[assign + 1] if assign + 1 < len(file) else None
        if value is None or value.kind != "str":
            continue
        literal = value.value.strip("'\"")
        if len(literal) < 8 or PLACEHOLDER.match(literal):
            continue
        report.add(token.line, "hardcoded_secret",
                   f"`{token.value}` is assigned a string literal in source",
                   "Read it from the environment or a secret manager. A committed credential is "
                   "public the moment the repository is cloned, and rotating it means a code change.",
                   "high")


def _check_transport(file: TsFile, report: Reporter) -> None:
    for token in file.tokens:
        if token.kind not in ("str", "template"):
            continue
        literal = token.value.strip("'\"`")
        if literal.startswith("http://") and not literal.startswith(
                ("http://localhost", "http://127.0.0.1", "http://0.0.0.0", "http://[::1]")):
            report.add(token.line, "insecure_transport",
                       f"Plain-HTTP URL `{literal[:60]}`",
                       "Use https. Anything sent over http is readable and rewritable in transit.",
                       "medium")
    for index, token in enumerate(file.tokens):
        if token.is_name("NODE_TLS_REJECT_UNAUTHORIZED"):
            report.add(token.line, "tls_verification_disabled",
                       "`NODE_TLS_REJECT_UNAUTHORIZED` is being set — this disables certificate checking process-wide",
                       "Fix the certificate chain instead (pass a `ca` for a private CA). Turning "
                       "off verification makes TLS decorative.", "high")
        if token.is_name("rejectUnauthorized") and index + 2 < len(file) \
                and file.tokens[index + 1].is_op(":") and file.tokens[index + 2].is_name("false"):
            report.add(token.line, "tls_verification_disabled",
                       "`rejectUnauthorized: false` accepts any certificate, including an attacker's",
                       "Supply the correct CA rather than switching off verification.", "high")


def _check_postmessage(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] != "postMessage":
            continue
        spans = argument_spans(file, paren)
        if len(spans) >= 2:
            target = file.tokens[spans[1][0]]
            if target.kind == "str" and target.value.strip("'\"") == "*":
                report.add(target.line, "postmessage_wildcard_origin",
                           "`postMessage(…, '*')` sends the payload to any origin that can receive it",
                           "Name the exact target origin, and check `event.origin` on the receiving "
                           "side.", "high")


def _check_target_blank(file: TsFile, report: Reporter) -> None:
    if not file.is_tsx:
        return
    for index, token in enumerate(file.tokens):
        if token.kind != "str" or token.value.strip("'\"") != "_blank":
            continue
        window = file.slice(max(0, index - 40), min(len(file), index + 40))
        if "noopener" in window or "noreferrer" in window:
            continue
        report.add(token.line, "target_blank_without_noopener",
                   "`target=\"_blank\"` without `rel=\"noopener noreferrer\"`",
                   "The opened page gets a handle to yours via `window.opener` and can navigate it. "
                   "Add `rel=\"noopener noreferrer\"`.", "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_eval(file, report)
    _check_shell(file, report)
    _check_dom_sinks(file, report)
    _check_weak_crypto(file, report)
    _check_postmessage(file, report)
    _check_target_blank(file, report)
    if not is_test_file(file.path):
        _check_hardcoded_secrets(file, report)
        _check_transport(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find security risks: eval, HTML injection, shell interpolation, weak crypto, secrets",
        "No security issues found!",
        analyze,
    )
