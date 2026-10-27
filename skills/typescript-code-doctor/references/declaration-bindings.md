# Duplicate declaration evidence

`find_ai_scaffolding.py` delegates binding analysis to the bundled,
stdlib-only `declaration_bindings.py`. The output schema, finding type
`duplicate_definition`, and scoring weights are unchanged.

Declarations are compared in their actual brace scope (callbacks, function
bodies and blocks have independent identities) and TypeScript binding namespace.
Functions contribute value bindings; aliases and interfaces contribute type
bindings; classes and enums contribute both. A function and a type alias can
therefore share a name. Same-scope aliases and duplicate function implementations
remain findings. Diagnostics describe a binding conflict, never runtime
replacement by an erased type alias. Overload signatures without bodies are
not duplicate implementations.

Private declarations in reopened namespace bodies stay local to each body.
Explicit exports share the namespace's qualified binding path, retaining its
enclosing lexical identity; dotted and nested exported namespace spellings resolve
to the same path. Declarations in ambient (`declare` or `.d.ts`)
namespace bodies are implicitly exported. Ambient context propagates to nested
namespace bodies. A private nested namespace retains the identity of its
particular enclosing body, even if its own members are exported. This is
single-file analysis; cross-file merges and export-list aliases require compiler
review and are not resolved here.

Interface merges are proven compatible only for empty bodies or simple named
properties with matching optional/readonly modifiers and primitive types
(`string`, `number`, `boolean`, `unknown`, `never`, `any`). Disjoint properties
merge; conflicting contracts are findings. Generic headers, heritage, methods
and other complex contracts remain visible merge candidates. Class/interface
merges also require review.

Enum compatibility checks require consistent `const` modifiers, distinct member
names, and valid implicit initializers. Quoted member names decode standard
single-character, hexadecimal and Unicode escapes; identifier Unicode escapes
are also decoded before comparison. Unknown escape forms prevent a compatibility
proof. Across merged declarations at most one first member may omit an
initializer. Implicit members after string literals or known call expressions
are invalid; numeric literal predecessors permit auto-increment. Explicit signed
decimal/hex/binary/octal numeric literals and string literals support bounded
compatibility checks. Other expressions and constant references need compiler
review: they produce an unscored candidate with a reason and no fix instead of
silently assuming a legal merge. Definite conflicts take priority over uncertainty.
Single enum bodies are checked for definite member conflicts; uncertain single
bodies do not constitute duplicate declarations.

This token-based analysis is deliberately bounded. It does not evaluate arbitrary
constant expressions or replace the TypeScript compiler. Unsupported merge
contracts stay candidates. No repository path, declaration name allowlist,
post-processing suppression or grading adjustment participates in these rules.
