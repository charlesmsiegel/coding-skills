# Abstraction, reuse, and tests that attack the theory

## Contents

- [Why generated code repeats](#why-generated-code-repeats)
- [Factorization vs. abstraction](#factorization-vs-abstraction)
- [The reuse search](#the-reuse-search)
- [Tests that attack the theory](#tests-that-attack-the-theory)

## Why generated code repeats

Producing the next plausible token given surrounding context is an operation that
*naturally* yields near-copies with local variation. It is very good at "like that, but
for orders." Finding the concept under three cases is a different operation, and nothing
in next-token prediction performs it by default.

So duplication in generated code should be read as diagnostic rather than sloppy: **a
missing concept, showing up three times.** The fix is not to delete the repetition. It's
to find what the repeated things are instances *of*.

## Factorization vs. abstraction

Both reduce line count. Only one reduces the number of things a reader must hold in mind.

**Factorization** — the three blocks become one function with switches:

```python
def process(items, validate=False, notify=False, dry_run=False, legacy_format=False):
    ...
```

Fewer lines, same five concepts, now interleaved, plus 16 reachable combinations of which
maybe 3 are meant to exist. Every call site is a puzzle. This is what "DRY" degenerates
into when applied as a rule about text.

**Abstraction** — the concept gets found and named:

```python
class ImportPolicy:       # what an importer is allowed to do and must check
    ...

def import_batch(items, policy: ImportPolicy):
    ...
```

Now the variation lives in a thing with a name, the illegal combinations aren't
expressible, and a fourth case is written by adding a policy rather than a parameter.

### The four checks

1. **Domain name.** A noun or verb from the problem, that a domain expert would recognize
   without a glossary. Struggling to name it means the concept isn't found yet — naming
   difficulty is a signal, not an obstacle to push past.
2. **Expressible unwritten cases.** A real abstraction handles cases nobody implemented,
   because it captures the rule. If it only supports exactly the three call sites that
   motivated it, it's factorization wearing a name.
3. **Fewer things in mind.** Ask: does a reader need to know *less* after this change?
   Boolean parameters almost always mean more.
4. **Illegal states unrepresentable, ideally.** The strongest form: the type system
   refuses the combinations that shouldn't exist.

### When the concept won't come

Say so. "These three branches clearly share something I can't name yet; leaving them
explicit" is a better handoff than a wrapper that pretends the work is done. Honest
duplication is cheap to fix once the concept appears. A false abstraction has to be
dismantled first, and by then it has callers.

## The reuse search

The `isEven` case is a *retrieval* failure, not a knowledge failure. Generating plausible
code is the path of least resistance; checking whether the thing exists requires
deliberately doing something else first. Before writing any helper:

**1. Standard library.** Most "utility" functions are a stdlib call. Date arithmetic,
path manipulation, string casing, set operations, binary search, LRU caching, batching,
retry-with-backoff, temp files, atomic writes, comparison keys, priority queues — all
routinely rewritten badly.

**2. Dependencies already present.** Read the manifest — don't recall it:

```bash
cat package.json pyproject.toml go.mod Cargo.toml pom.xml build.gradle 2>/dev/null
```

Then read the installed package's actual source or `--help`. Recollection of an API
surface is the exact faculty that generates confident, plausible, non-existent methods.
Anything asserted about a library's behavior should come from something read in this
session.

**3. The repository.** Grep the concept *and its synonyms* before adding a function:

```bash
grep -rn "def .*retry\|def .*backoff" --include=*.py .
rg "fn (parse|read|load)_config" -t rust
```

Search the vocabulary, not just the name being introduced: `duration`/`interval`/
`timeout`/`ttl` are the same concept under four names, and a codebase with all four has a
naming problem worth fixing while you're here.

**4. The change itself.** Before finishing, reread the diff for a helper that duplicates
one added ten minutes ago in another file. Long generations lose track of their own
earlier decisions.

When the search finds an existing implementation, **delete the new one and use the
original.** That deletion is usually the highest-value edit in the change — it removes
code, removes a second thing to maintain, and removes a future divergence where two
implementations of one concept drift apart.

## Tests that attack the theory

Green tests mean no anticipated failure occurred. When one model wrote both the code and
the tests, the anticipated set is the same in both — the tests encode the same
misunderstanding they're supposed to catch. This correlated failure is why "tests pass"
is weaker evidence for generated code than for hand-written code, not stronger.

Two questions, and they are different:

- *What input makes this line throw?* → finds typos. Pattern-completion is good at these
  and generates them by default.
- *What input makes the model of the problem wrong?* → finds design errors. Requires
  going back to the theory statement and asking where it stops being true.

The second is the one worth spending time on. Prompts:

- **Empty and one.** Empty collection, single element, all-identical elements.
- **Absent vs. null.** Different in most domains, conflated in most generated code.
- **Two at once.** Same operation twice concurrently; same message delivered twice.
  At-least-once delivery is the norm, so idempotence is usually part of the theory
  whether or not anyone wrote it down.
- **Boundaries in the assumptions, not the arguments.** Timezone changes mid-operation,
  clock moves backward, encoding isn't UTF-8, locale changes casing rules, input is
  larger than memory.
- **Ordering.** Does the theory depend on order? Is that guaranteed, or observed once?
- **The adversarial input.** If someone controls this field, what do they choose?

And state what's deliberately untested. "Retries under partial network failure are
untested; the theory assumes the transport classifies errors correctly" tells a reviewer
more than another passing assertion — it points at the boundary of the model, which is
where the next bug is going to come from.
