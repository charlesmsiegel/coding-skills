#!/usr/bin/env python3
"""
Find security leads: shell interpolation, SQL built by concatenation, disabled
certificate verification, weak hashes, and credentials committed as literals.

These are leads, not verdicts. A detector that reads syntax cannot know whether
the string reaching `Command::new("sh")` is attacker-controlled — only that the
construction admits it. Each finding names the specific thing to check.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, argument_spans, iter_calls, iter_method_calls

# Hashes that are broken for authentication (fine for a checksum or a cache key).
_WEAK_HASHES = {"md5": "MD5", "Md5": "MD5", "sha1": "SHA-1", "Sha1": "SHA-1"}

# Crate paths whose presence is itself the finding.
_DANGEROUS_SETTINGS = {
    "danger_accept_invalid_certs": (
        "TLS certificate validation is turned off — every connection this client makes is "
        "trivially interceptable",
        "Trust a specific certificate instead (`Certificate::from_pem` + `add_root_certificate`). "
        "If this is for a local test server, gate it behind `#[cfg(test)]` so it cannot reach "
        "production.", "high"),
    "danger_accept_invalid_hostnames": (
        "Hostname verification is turned off, so any valid certificate for any host is accepted",
        "Remove it, or pin the expected certificate.", "high"),
    "set_verify": (
        "`set_verify` may be disabling peer verification",
        "Check the mode: `SslVerifyMode::NONE` accepts anything.", "medium"),
}

_SECRET_NAMES = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token"
    r"|private[_-]?key|client[_-]?secret|aws[_-]?secret)"
)

# Literal shapes that are credentials wherever they appear.
_SECRET_LITERALS = (
    (re.compile(r'"(sk-[A-Za-z0-9][A-Za-z0-9_-]{14,})"'), "an OpenAI-style secret key"),
    (re.compile(r'"(gh[pousr]_[A-Za-z0-9]{20,})"'), "a GitHub token"),
    (re.compile(r'"(AKIA[0-9A-Z]{12,})"'), "an AWS access key id"),
    (re.compile(r'"(xox[baprs]-[A-Za-z0-9-]{10,})"'), "a Slack token"),
    (re.compile(r'"(-----BEGIN [A-Z ]*PRIVATE KEY-----)'), "a private key"),
)

_SQL_START = re.compile(r"(?i)^\s*(select|insert|update|delete|drop|create|alter)\s")

# Rust string literals come in several spellings, and SQL is very often written
# as a raw string so the query can contain quotes: `r#"SELECT … '{}'"#`. Testing
# the prefixed form against a plain-quote pattern skipped exactly those.
_STRING_PREFIX = re.compile(r'^(?:[bcr]{0,2})(#*)"')


def _literal_body(token_value: str) -> str:
    """The text inside a Rust string literal, whatever its prefix or hashes."""
    match = _STRING_PREFIX.match(token_value)
    if not match:
        return token_value
    opening = match.end()
    closing = len(token_value) - (1 + len(match.group(1)))
    return token_value[opening:closing] if closing > opening else ""


def _check_shell_execution(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        if callee.rsplit("::", 1)[-1] != "new" or "Command" not in callee:
            continue
        spans = argument_spans(file, index)
        program = file.slice(*spans[0]).strip() if spans else ""
        if program.strip('"') not in ("sh", "bash", "cmd", "/bin/sh", "/bin/bash", "powershell"):
            continue
        window = file.slice(index, min(index + 80, len(file.tokens)))
        if '"-c"' not in window and "'-c'" not in window and '"/C"' not in window:
            continue
        interpolated = "format!" in window or re.search(r'`[^`]*\{', window) or "{}" in window
        report.add(file.line_of(index), "shell_command_construction",
                   f"`Command::new({program})` with `-c` and "
                   + ("an interpolated string" if interpolated else "a shell string"),
                   "Run the program directly: `Command::new(\"git\").args([\"clone\", url])`. The "
                   "argument vector is passed to `execve` without a shell, so quoting, `$(…)`, "
                   "`;` and `&&` in the data have no meaning at all.",
                   "high" if interpolated else "medium")

    for name_index, paren, method in iter_method_calls(file):
        if method not in ("arg", "args"):
            continue
        spans = argument_spans(file, paren)
        if not spans:
            continue
        text = file.slice(*spans[0]).strip()
        if not text.startswith("format!"):
            continue
        # `.arg(format!("/tmp/{name}.txt"))` is one argument, passed to execve
        # without a shell: it cannot be split on whitespace and cannot become a
        # flag. The real hazard is an argument whose *first character* comes
        # from the interpolated value, which can then start with `-`.
        template = re.search(r'format!\s*\(\s*(r?#*"(?:[^"\\]|\\.)*")', text)
        if template is None:
            continue
        head = template.group(1).lstrip("r#").strip('"')
        if not head.startswith(("{", "-")):
            continue
        report.add(file.line_of(name_index), "option_injectable_argument",
                   f"`.arg(format!({template.group(1)[:32]}…))` — the argument begins with the "
                   "interpolated value, so a value starting with `-` becomes a flag",
                   "Pass the flag and the value as separate arguments "
                   "(`.arg(\"--name\").arg(value)`), or use `--` to end option parsing if the "
                   "program supports it. Note this is option injection, not shell injection: "
                   "there is no shell here and no word splitting.", "medium")


def _check_sql_construction(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        if callee != "format!":
            continue
        spans = argument_spans(file, index)
        if not spans:
            continue
        template = _literal_body(file.slice(*spans[0]).strip())
        if not _SQL_START.match(template) or "{" not in template:
            continue
        report.add(file.line_of(index), "sql_built_by_interpolation",
                   "SQL assembled with `format!` and an interpolated value",
                   "Use bound parameters: `sqlx::query(\"… WHERE id = $1\").bind(id)`, or "
                   "`rusqlite`'s `params![]`. Interpolation is how the value becomes syntax. "
                   "Where the *identifier* must vary (a table name), validate it against an "
                   "allowlist — placeholders cannot bind identifiers.", "high")

    for index, token in enumerate(file.tokens):
        if token.kind != "str" or not _SQL_START.match(_literal_body(token.value)):
            continue
        following = file.tok(index + 1)
        if following is not None and following.is_op("+"):
            report.add(token.line, "sql_built_by_concatenation",
                       "SQL string concatenated with `+`",
                       "Bind the value as a parameter instead of splicing it into the statement.",
                       "high")


def _check_transport_security(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        entry = _DANGEROUS_SETTINGS.get(method)
        if entry is None:
            continue
        description, suggestion, severity = entry
        spans = argument_spans(file, paren)
        argument = file.slice(*spans[0]).strip() if spans else ""
        if method.startswith("danger_") and argument == "false":
            continue
        if is_test_file(file.path) or file.in_test_code(name_index):
            severity = "low"
        report.add(file.line_of(name_index), "insecure_transport_setting", description,
                   suggestion, severity)

    for index, token in enumerate(file.tokens):
        if token.kind != "str" or not token.value.startswith('"http://'):
            continue
        if re.match(r'"http://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])', token.value):
            continue
        report.add(token.line, "plaintext_http_url",
                   f"hardcoded plaintext URL {token.value[:50]}",
                   "Use `https://`. Anything sent over this connection — including the "
                   "`Authorization` header — is readable and modifiable in transit.", "medium")


def _check_weak_crypto(file: RsFile, report: Reporter) -> None:
    for use in file.uses:
        for symbol, label in _WEAK_HASHES.items():
            if re.search(r"\b" + re.escape(symbol) + r"\b", use.path):
                report.add(use.line, "weak_hash_algorithm",
                           f"{label} is imported here",
                           f"{label} is broken for anything that needs collision or preimage "
                           "resistance — signatures, integrity checks, password storage. Use "
                           "SHA-256/BLAKE3 for digests, and Argon2/bcrypt/scrypt for passwords "
                           "(a fast hash is the wrong primitive for a password whatever its "
                           "strength).", "high")
                break

    # NOT `thread_rng`/`random`: rand's thread-local generator is a CSPRNG
    # (ChaCha12), seeded and periodically reseeded from OS entropy. Reporting it
    # as recoverable was simply wrong, and a security finding that misstates the
    # primitive is worse than no finding — it teaches people to distrust the
    # tool. These are the generators that genuinely are not cryptographic.
    for index, callee in iter_calls(file):
        leaf = callee.rsplit("::", 1)[-1]
        if leaf in ("seed_from_u64", "from_seed") and _crypto_context(file, index):
            report.add(file.line_of(index), "deterministic_rng_for_secret",
                       f"`{callee}(…)` seeds the generator deterministically, where the value "
                       "looks like a secret",
                       "A fixed or low-entropy seed makes every output reproducible. Use "
                       "`OsRng`, or `thread_rng()` — which is a CSPRNG seeded from the OS — and "
                       "keep the seeded generator for tests.", "high")
        elif leaf in ("SmallRng", "StepRng", "XorShiftRng", "Pcg32", "Pcg64") \
                and _crypto_context(file, index):
            report.add(file.line_of(index), "non_cryptographic_rng_for_secret",
                       f"`{callee}` is a fast non-cryptographic generator, used where the value "
                       "looks like a secret",
                       "`SmallRng` and friends trade unpredictability for speed and are "
                       "documented as unsuitable for security. Use `OsRng` or `thread_rng()`.",
                       "high")


def _crypto_context(file: RsFile, index: int) -> bool:
    window = file.slice(max(0, index - 12), min(index + 12, len(file.tokens)))
    return bool(re.search(r"(?i)(token|secret|key|nonce|salt|password|session)", window))


def _check_hardcoded_secrets(file: RsFile, report: Reporter) -> None:
    testish = is_test_file(file.path)

    # `const API_KEY: &str = "…";` — the declaration carries both halves.
    for binding in file.bindings:
        if not _SECRET_NAMES.search(binding.name):
            continue
        # A fixture inside `#[cfg(test)] mod tests` is not compiled into a
        # release build, and a `tests/` file with the same content is already
        # downgraded — the two should not disagree because of where they live.
        in_test = testish or file.in_test_code(binding.start)
        value = binding.value_text.strip()
        if not value.startswith('"') or len(value) < 10:
            continue
        if value.startswith(('"$', '"{', '"<')):
            continue
        if any(pattern.search(value) for pattern, _ in _SECRET_LITERALS):
            continue  # the literal scan below names the specific credential type
        report.add(binding.line, "credential_named_literal",
                   f"`{binding.kind} {binding.name}` is a string literal in the source",
                   "Load it from the environment (`std::env::var`) or a secret store. A default "
                   "credential in source is a credential in every build — and rotate the value "
                   "that was committed, because deleting the line does not remove it from git.",
                   "low" if in_test else "high")

    for index, token in enumerate(file.tokens):
        if token.kind != "str":
            continue
        in_test = testish or file.in_test_code(index)
        for pattern, label in _SECRET_LITERALS:
            if pattern.search(token.value):
                report.add(token.line, "hardcoded_credential",
                           f"string literal looks like {label}",
                           "Read it from the environment or a secret store, rotate the value that "
                           "was committed, and check whether it reached the published history — "
                           "deleting the line does not remove it from git.",
                           "low" if in_test else "high")
                break
        else:
            # `PASSWORD = "…"`, `password: "…"` — the name says what the literal is.
            assignment = file.tok(index - 1)
            name = file.tok(index - 2)
            if assignment is None or not assignment.is_op("=", ":"):
                continue
            if name is None or name.kind != "name" or not _SECRET_NAMES.search(name.value):
                continue
            if len(token.value) > 8 and not token.value.startswith(('"$', '"{', '"<')):
                report.add(token.line, "credential_named_literal",
                           f"`{name.value}` is assigned a string literal",
                           "Load it from the environment (`std::env::var`) or a secrets manager. "
                           "A default credential in source is a credential in every build.",
                           "low" if in_test else "high")


def _check_path_and_deserialization(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method != "join":
            continue
        spans = argument_spans(file, paren)
        argument = file.slice(*spans[0]).strip() if spans else ""
        if not argument or argument.startswith('"'):
            continue
        window = file.slice(max(0, name_index - 40), name_index)
        if not re.search(r"(?i)(request|param|input|untrusted|query|header|user_)", window):
            continue
        report.add(file.line_of(name_index), "path_join_with_external_input",
                   f"`Path::join({argument})` where the component comes from outside",
                   "A component of `../../etc/passwd` escapes the base directory, and an absolute "
                   "path replaces it entirely — `join` is documented to do that. Canonicalize the "
                   "result and check it still starts with the base, or reject any component that "
                   "is not a plain file name.", "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_shell_execution(file, report)
    _check_sql_construction(file, report)
    _check_transport_security(file, report)
    _check_weak_crypto(file, report)
    _check_hardcoded_secrets(file, report)
    _check_path_and_deserialization(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find security leads in Rust source",
        "No security leads found!",
        analyze,
    )
