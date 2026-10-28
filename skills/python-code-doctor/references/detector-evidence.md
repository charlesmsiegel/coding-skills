# Bounded detector evidence

## Local SQL identifier quoting

`find_security_issues.py` uses the bundled `sql_evidence.py` helper to keep
dynamically interpolated SQL visible as a
`sql_injection` candidate, with an empty suggestion and an `also_caused_by`
explanation, when **every executed interpolation** meets both conditions below.
Candidates are unscored under the existing reporting contract.

1. A unique lexical binding resolves to an undecorated, synchronous local
   function with one positional parameter, no defaults, keyword-only parameters,
   or variadic parameters. After an optional leading literal expression (normally a docstring), its body is one
   return expression structurally equal to
   `'"' + parameter.replace('"', '""') + '"'`. The function's name is irrelevant.
   Simple aliases and exact tuple/list unpacking of uniquely bound values are
   followed. Definitions and assignments must be unconditional statements in
   the resolved scope and precede the use on an earlier source line. Parameters,
   reassignment, deletion, imports, global/nonlocal declarations, conditional
   assignments and unsupported binding forms cannot supply this evidence.
   The existing framework `connection.ops.quote_name(source_constant)` evidence
   remains available, subject to the same position and formatting checks.
2. A bounded SQL lexer places the entire quoted result immediately after
   `FROM`, `JOIN`, `UPDATE`, `INTO`, `TABLE`, `VIEW`, `INDEX`, `TRIGGER`, or `SET`,
   or after a comma in a `SET` clause before a subsequent clause. It rejects
   holes inside single/double/backtick/bracket quotes or comments, adjacent
   identifier suffixes, data/expression positions, and mixed identifier/data
   interpolations. Any dollar sign or backslash, more than one block-comment opener
   (including nested comments), unterminated comments, and unterminated quoted
   strings remain unproven.

Supported assembly is an f-string without conversions or format specifications,
`+` concatenation, exact `%s` substitution (with literal `%%`), or a literal
`.format()` template with direct positional/named fields and no conversion,
attribute/index access, or format specification. Truncation, replacement-count
arguments, wrong delimiters, missing escaping, transformations of the quoted
result, and arbitrary imported quoting helpers cannot establish the classification.

This is local source evidence, not a database parser, taint analysis, or proof
of arbitrary runtime argument types or dialect behavior. Unsupported safe
implementations can still be findings. Existing literal-derived SQL and the
separate DDL/PRAGMA/test-context candidate policies retain their own contracts;
this recognition does not expand them. Mutation, synchronization and best-effort
exception handling are not exempted by these changes.

## Ordinary exception-handler reachability

`find_exception_issues.py` reports an entire ordinary `except` handler as
`unreachable_except` only if every member is provably covered by the union of
earlier handlers, or an earlier bare handler catches everything. Builtin
inheritance uses the explicit known hierarchy and only lexically unshadowed
builtin names. Parameters, local/module assignments, definitions, imports and
explicit global/nonlocal declarations invalidate builtin-name evidence in the
relevant scope. Custom inheritance, qualified names and aliases are unproven.

`Exception` is below `BaseException`; it does not cover `BaseException`,
`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, or `asyncio.CancelledError`.
An earlier `Exception` still covers an unshadowed builtin `ValueError`.
Tuples retain unsupported members as unknown: a subscript, call or starred
expression prevents a complete-coverage claim. Covering only one tuple member
is insufficient. `except*` traversal remains separate and makes no ordinary
ordering claim; exception-group splitting requires different analysis.

Other exception observations, including broad catches and swallowed errors,
continue to receive their existing independent semantic review.
