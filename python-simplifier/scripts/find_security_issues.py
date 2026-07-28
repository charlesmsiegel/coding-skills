#!/usr/bin/env python3
"""
Detect risky security constructs in Python code via AST analysis.

These are patterns a cleanup pass should never leave in production code.
Conservative — only flags clear, unambiguous cases.

Finds:
  - eval_exec              : calls to builtin eval() or exec()
  - shell_injection        : subprocess.* with shell=True, os.system, os.popen
  - unsafe_yaml            : yaml.load() with no Loader= keyword argument
  - unsafe_deserialization : pickle.load/loads, marshal.load/loads, __import__
  - weak_hash              : hashlib.md5() or hashlib.sha1()
  - tls_verify_disabled    : call with verify=False, or ssl._create_unverified_context
  - hardcoded_secret       : assignment of a string literal to a name that looks
                             like a credential/secret
  - sql_injection          : DB execute/executemany/etc. called with a dynamically
                             built SQL string (f-string, %-format, +concat, .format)
  - command_injection      : subprocess/os.system/os.popen called with a dynamically
                             built command string (shell=True makes it worse)
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator
from collections import defaultdict


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


# Names whose assignment to a plain string literal suggests a hardcoded secret
_SECRET_NAMES = {
    "password", "passwd", "pwd", "secret", "api_key", "apikey",
    "access_key", "secret_key", "token", "auth_token", "private_key",
}

# Values that are obviously placeholders — skip these
_PLACEHOLDER_VALUES = {
    "", "changeme", "your_password_here", "xxx", "todo", "none", "null", "example",
}


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _has_keyword(call_node, kwname):
    """Return True if the call has a keyword argument with the given name."""
    return any(kw.arg == kwname for kw in call_node.keywords)


def _keyword_value(call_node, kwname):
    """Return the AST node for keyword argument kwname, or None."""
    for kw in call_node.keywords:
        if kw.arg == kwname:
            return kw.value
    return None


def _is_false_literal(node):
    return isinstance(node, ast.Constant) and node.value is False


def _is_placeholder_secret(value: str) -> bool:
    """Return True if the string looks like a placeholder rather than a real secret."""
    low = value.strip().lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    # All-uppercase env-var style names like "MY_SECRET_KEY" — likely a reference/default
    stripped = value.strip()
    if stripped.isupper() and "_" in stripped:
        return True
    return False


def _target_looks_like_secret(node) -> bool:
    """Return True if an assignment target Name/Attribute looks like a credential."""
    if isinstance(node, ast.Name):
        return node.id.lower() in _SECRET_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr.lower() in _SECRET_NAMES
    return False


def _is_dynamic_string(node) -> bool:
    """Return True if `node` is a string that is built dynamically at runtime.

    Covers:
      - f-strings (ast.JoinedStr with at least one FormattedValue)
      - %-format BinOp whose left side is a str Constant (or whose operator is Mod)
      - +-concatenation BinOp that involves a str Constant on either side
      - .format() call on a str literal or an arbitrary expression
    """
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):
            # "..." % x  or  x % "..."  — treat left-str case as SQL-like interpolation
            return (isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str))
        if isinstance(node.op, ast.Add):
            return (
                (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str))
                or (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str))
            )

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            # "...".format(x)  or  expr.format(x)
            return True

    return False


_SQL_EXECUTE_ATTRS = {"execute", "executemany", "executescript", "raw", "mogrify"}

_SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    for node in ast.walk(tree):

        # ------------------------------------------------------------------ #
        # eval_exec — calls to builtin eval() or exec()
        # ------------------------------------------------------------------ #
        if isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                add(node.lineno, "eval_exec",
                    f"Call to builtin '{func.id}()' executes arbitrary Python code",
                    "Avoid dynamic code execution. Use ast.literal_eval() for safe literal "
                    "parsing, or replace with explicit dispatch logic.",
                    "high")

            # ---------------------------------------------------------------- #
            # sql_injection — execute/executemany/etc. with dynamic SQL string
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in _SQL_EXECUTE_ATTRS
                  and node.args
                  and _is_dynamic_string(node.args[0])):
                add(node.lineno, "sql_injection",
                    f"Call to .{func.attr}() passes a dynamically built SQL string",
                    "Use parameterized queries — pass parameters as the second argument "
                    "(e.g. execute(sql, params)); never interpolate values into SQL.",
                    "high")

            # ---------------------------------------------------------------- #
            # command_injection — subprocess/os.* with a dynamic command string
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("system", "popen")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "os"
                  and node.args
                  and _is_dynamic_string(node.args[0])):
                add(node.lineno, "command_injection",
                    f"os.{func.attr}() is called with a dynamically built command string",
                    "Pass an argument list (e.g. [\"ls\", d]) and avoid shell=True; "
                    "never interpolate values into a shell string.",
                    "high")

            elif (isinstance(func, ast.Attribute)
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "subprocess"
                  and func.attr in _SUBPROCESS_FUNCS
                  and node.args
                  and _is_dynamic_string(node.args[0])
                  # Without shell=True the string is an executable path, not a
                  # shell command — metacharacters aren't interpreted, so this
                  # is only injection when a shell is actually invoked.
                  and _keyword_value(node, "shell") is not None
                  and not _is_false_literal(_keyword_value(node, "shell"))):
                add(node.lineno, "command_injection",
                    f"subprocess.{func.attr}() runs a dynamically built command string through the shell (shell=True)",
                    "Pass an argument list (e.g. [\"ls\", d]) with shell=False; "
                    "never interpolate values into a shell string.",
                    "high")

            # ---------------------------------------------------------------- #
            # shell_injection — subprocess.* shell=True, os.system, os.popen
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("system", "popen")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "os"):
                add(node.lineno, "shell_injection",
                    f"os.{func.attr}() passes a command string to the shell",
                    "Use subprocess.run() with an argument list and shell=False to avoid "
                    "shell injection vulnerabilities.",
                    "high")

            elif (isinstance(func, ast.Attribute)
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "subprocess"):
                shell_val = _keyword_value(node, "shell")
                if shell_val is not None and not _is_false_literal(shell_val):
                    add(node.lineno, "shell_injection",
                        f"subprocess.{func.attr}() called with shell=True",
                        "Pass the command as a list (e.g., ['ls', '-l']) and use shell=False "
                        "to prevent shell injection.",
                        "high")

            # ---------------------------------------------------------------- #
            # unsafe_yaml — yaml.load() with no explicit loader
            # Safe when: Loader= keyword is present OR a second positional arg
            # is supplied (the caller has deliberately chosen a loader).
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr == "load"
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "yaml"
                  and not _has_keyword(node, "Loader")
                  and len(node.args) < 2):
                add(node.lineno, "unsafe_yaml",
                    "yaml.load() called without a Loader= keyword can execute arbitrary code",
                    "Use yaml.safe_load() instead, or pass Loader=yaml.SafeLoader explicitly.",
                    "high")

            # ---------------------------------------------------------------- #
            # unsafe_deserialization — pickle/marshal load/loads, __import__
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("load", "loads")
                  and isinstance(func.value, ast.Name)
                  and func.value.id in ("pickle", "marshal")):
                add(node.lineno, "unsafe_deserialization",
                    f"{func.value.id}.{func.attr}() deserializes untrusted data unsafely",
                    "Avoid deserializing data from untrusted sources with pickle/marshal. "
                    "Use JSON, msgpack, or another safe format instead.",
                    "medium")

            elif isinstance(func, ast.Name) and func.id == "__import__":
                add(node.lineno, "unsafe_deserialization",
                    "__import__() with dynamic arguments can load arbitrary modules",
                    "Use importlib.import_module() with a validated module name instead.",
                    "medium")

            # ---------------------------------------------------------------- #
            # weak_hash — hashlib.md5() or hashlib.sha1()
            # Skip when usedforsecurity=False is passed explicitly (Constant False).
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("md5", "sha1")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "hashlib"):
                ufs = _keyword_value(node, "usedforsecurity")
                if ufs is None or not _is_false_literal(ufs):
                    add(node.lineno, "weak_hash",
                        f"hashlib.{func.attr}() is a cryptographically weak hash algorithm",
                        "Use hashlib.sha256() or stronger for security-sensitive uses. "
                        "If only used for checksums, add usedforsecurity=False (Python 3.9+) "
                        "to suppress this warning.",
                        "medium")

            # ---------------------------------------------------------------- #
            # tls_verify_disabled — call with verify=False or ssl._create_unverified_context
            # Only flag verify=False when the callee is a known HTTP API method.
            # ---------------------------------------------------------------- #
            else:
                verify_val = _keyword_value(node, "verify")
                if verify_val is not None and _is_false_literal(verify_val):
                    _HTTP_ATTR_NAMES = {
                        "get", "post", "put", "delete", "patch",
                        "head", "options", "request", "send",
                    }
                    _HTTP_NAME_NAMES = {
                        "get", "post", "put", "delete", "patch",
                        "head", "options", "request",
                    }
                    _is_http_call = (
                        (isinstance(func, ast.Attribute) and func.attr in _HTTP_ATTR_NAMES)
                        or (isinstance(func, ast.Name) and func.id in _HTTP_NAME_NAMES)
                    )
                    if _is_http_call:
                        add(node.lineno, "tls_verify_disabled",
                            "TLS certificate verification disabled via verify=False",
                            "Keep certificate verification enabled. If testing against a local "
                            "server use a proper CA bundle or a self-signed cert trusted by the "
                            "test environment.",
                            "medium")
                elif (isinstance(func, ast.Attribute)
                      and func.attr == "_create_unverified_context"
                      and isinstance(func.value, ast.Name)
                      and func.value.id == "ssl"):
                    add(node.lineno, "tls_verify_disabled",
                        "ssl._create_unverified_context() disables TLS certificate verification",
                        "Use ssl.create_default_context() to keep certificate verification on.",
                        "medium")

        # ------------------------------------------------------------------ #
        # hardcoded_secret — assignment of a string literal to a secret-named var
        # ------------------------------------------------------------------ #
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node = node.value
            else:
                targets = [node.target]
                value_node = node.value

            if value_node is None:
                continue

            if not (isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, str)):
                continue

            secret_value = value_node.value
            if len(secret_value) < 6:
                continue
            if _is_placeholder_secret(secret_value):
                continue

            for target in targets:
                if _target_looks_like_secret(target):
                    tname = target.id if isinstance(target, ast.Name) else target.attr
                    add(node.lineno, "hardcoded_secret",
                        f"Hardcoded string literal assigned to '{tname}' looks like a secret",
                        "Load credentials from environment variables (os.environ) or a secrets "
                        "manager (e.g., AWS Secrets Manager, HashiCorp Vault) rather than "
                        "embedding them in source code.",
                        "medium")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, Exception):
        return []


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(description="Detect security issues in Python code")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No security issues found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} security issue(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
