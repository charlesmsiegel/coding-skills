# Refactoring Catalog — Code Smells

The full classic smell catalog (Fowler / refactoring.guru), organized into its five
families, adapted to Python and to this skill's two-pronged workflow.

**The two-pass protocol.** The deterministic scripts flag what a parser can see;
each entry below names its detector when one exists. Pass 1: triage those
candidates — confirm or reject each with the diagnostic question. Pass 2: read the
hot files (high churn × high complexity) for what no parser can see — the
judgment-only smells (change patterns, duplicated *intent*, wrong hierarchies) and
the subtler instances next to the ones the detector caught. A detector hit is
evidence, not a verdict; a quiet detector is not a clean bill.

For every confirmed smell: **name the technique (see `refactoring-techniques.md`)
→ pin behavior with a characterization test → make the change small.** Behavior
must not change.

## Bloaters

Code, methods and classes that have grown too large to work with. They accumulate
gradually — nobody writes a 400-line method on day one — so look for them where
the churn is, not in cold corners.

### Long method
**Spot it:** a function you can't take in at a glance; sections separated by blank
lines and "now do X" comments; multiple levels of abstraction interleaved. The
catalog's rule of thumb: past ~10 lines, start asking questions.
**Diagnostic:** "Does this do one thing at one level of abstraction?" If a fragment
needs a comment, it wants to be a function named after that comment.
**Fix:** Extract Method per section. Decompose Conditional for gnarly boolean
logic; Replace Conditional with Polymorphism or a dispatch dict when the length is
a big type-switch. If tangled locals block extraction, first Replace Temp with
Query or Introduce Parameter Object; Replace Method with Method Object is the last
resort. The performance objection is almost always a red herring — measure first.
**Detector:** `analyze_complexity.py`.

### Large class / God object
**Spot it:** a class with many fields and methods spanning several concerns; the
name is vague (`Manager`, `Engine`, `System`); you scroll to understand it.
**Diagnostic:** "Could I split this into two or three things with clearer names?"
**Fix:** Extract Class along the seams — groups of fields used together mark a
sub-object. Extract Subclass when part of the behavior applies only in some cases;
Extract Interface (a `Protocol` in Python) only when a client genuinely needs the
narrowed view. (`analyze_complexity.py` and `find_code_smells.py` flag the size;
deciding the seams is judgment.)

### Primitive obsession
**Spot it:** money as a bare `float`; a phone number as a `str` validated in twelve
places; information coded into constants (`USER_ADMIN_ROLE = 1`); status as a magic
string `"active"`; string keys into dicts standing in for fields; coordinates as a
loose `(x, y)` tuple passed everywhere.
**Diagnostic:** "Is this primitive carrying meaning and rules that live nowhere?"
**Fix:** Replace Data Value with Object — in Python a `dataclass` (often
`frozen=True`). Replace Type Code with Class → `enum.Enum` for fixed sets. Replace
Array with Object → a dataclass instead of a positional tuple or stringly-keyed
dict. Validation and behavior move onto the type; the rest of the code stops
re-checking it.
**Detector:** partial — `find_code_smells.py` (magic numbers),
`find_parameter_objects.py` (primitives travelling together). The single
meaning-laden primitive is found by reading.

### Long parameter list
**Spot it:** more than three or four parameters; callers that pass a parade of
positional values; boolean flags steering which algorithm runs.
**Diagnostic:** "Do these values travel together, or come from one object the
function could just take?"
**Fix:** Preserve Whole Object when the values are pulled off one object; Replace
Parameter with Method Call when the callee could fetch the value itself; Introduce
Parameter Object when the group recurs. Boolean steering flags: split the function
(see `find_boolean_params.py`).
**Detector:** `find_code_smells.py`, `analyze_complexity.py`.
**Ignore when:** bundling would couple modules that are deliberately independent —
explicit parameters are sometimes the honest dependency injection.

### Data clumps
**Spot it:** the same group of variables travelling together through signatures and
locals (`host, port, timeout` …). The catalog's test: delete one value — do the
others still make sense? If not, it's a clump.
**Diagnostic:** "Does this group have a name?"
**Fix:** Extract Class / Introduce Parameter Object → a (frozen) dataclass; then
Preserve Whole Object at the call sites. Watch the behavior that follows the data —
it often wants to move onto the new class.
**Detector:** `find_parameter_objects.py`.

## Object-Orientation Abusers

Incomplete or incorrect application of OO ideas — half-used hierarchies, dispatch
done by hand, state smeared across time.

### Switch statements (type-switch ladders)
**Spot it:** a complex `if/elif` ladder (or `match`) dispatching on a type tag,
kind string, or `isinstance` — especially the *same* switch repeated in several
places.
**Diagnostic:** "When I add the next case, how many ladders do I edit?" When you
see switch, think polymorphism — adding a case should mean adding an entry, not
editing every ladder.
**Fix:** a dispatch dict (key → function) for data-shaped branching; Replace
Conditional with Polymorphism when the variants already are (or should be)
classes — via Replace Type Code with Subclasses first if needed. If branches just
call one method with different arguments, Replace Parameter with Explicit Methods.
If one branch handles None, consider Introduce Null Object (sparingly — see
`overengineering-and-abstraction.md`).
**Detector:** `find_design_smells.py` (`type_switch`).
**Ignore when:** one simple switch in one place is often the clearest form —
factory functions legitimately switch on what to build. The smell is complexity
and repetition, not the keyword.

### Temporary field
**Spot it:** instance fields that are `None` except while one algorithm runs;
`__init__` full of `self.x = None` placeholders that only one method touches.
**Diagnostic:** "Would a reader expect this field to hold something meaningful at
any time?" If it's really a local that hitched a ride on `self`, it's a smell.
**Fix:** pass the value through parameters, or Extract Class: move the algorithm
and its scratch state into its own small object (the method-object move).
**Detector:** `find_design_smells.py` (`temporary_field`).
**Ignore when:** a lazy-init cache behind a `@property`/`@cached_property` is
idiomatic — better yet, use `functools.cached_property` and delete the field.

### Refused bequest
**Spot it:** a subclass that inherits methods it doesn't want and overrides them
with no-ops or `raise NotImplementedError`; subclasses using only a sliver of the
parent.
**Diagnostic:** "Is this really an is-a, or just code reuse via inheritance?"
**Fix:** Replace Inheritance with Delegation (compose, forward what's needed); or,
if the relationship is real but the parent is too fat, Extract Superclass for the
truly shared part and inherit from that.
**Detector:** `find_design_smells.py` (`refused_bequest`, same-file hierarchies);
cross-file refusals are found by reading.

### Alternative classes with different interfaces
**Spot it:** two classes doing the same job with different method names — usually
written by authors who didn't know the other existed.
**Diagnostic:** "If I renamed the methods to match, would these classes be
duplicates?"
**Fix:** Rename Method / Move Method / Add Parameter until the interfaces converge,
then delete one (or Extract Superclass if only part of the behavior is shared).
**Detector:** none — judgment, though `find_duplicates.py` sometimes surfaces the
shared bodies.
**Ignore when:** the duplicates live in different third-party libraries you don't
control.

## Change Preventers

One change should mean one edit in one place. These smells are about how change
*flows*, so they're invisible to a parser — find them in the git history (workflow
step 2: churn analysis) and in your own friction while editing.

### Divergent change
**Spot it:** one module/class that you edit for *unrelated* reasons — a change to
the tax rules and a change to the PDF layout both touch the same file.
**Diagnostic:** "Does this thing have more than one reason to change?" (SRP)
**Fix:** Extract Class — split the responsibilities so each has a single axis of
change.

### Shotgun surgery
**Spot it:** the opposite — one conceptual change forces tiny edits across many
files (add a field → touch the model, the form, the serializer, three templates,
two validators).
**Diagnostic:** "Does one change scatter into many places?"
**Fix:** Move Method / Move Field to gather the scattered behavior into one home;
if no class fits, create the one that owns the concept. If gathering leaves shells
behind, Inline Class finishes the job.

### Parallel inheritance hierarchies
**Spot it:** every new subclass in one hierarchy forces a sibling in another —
`FooHandler` needs `FooValidator` needs `FooSerializer`; matching name prefixes are
the tell.
**Diagnostic:** "When I subclass here, am I obligated to subclass there?"
**Fix:** make instances of one hierarchy refer to instances of the other, then
Move Method / Move Field until the duplicate hierarchy collapses.
**Ignore when:** de-duplication produces something uglier than the parallelism —
the catalog itself says so: step back and revert.

## Dispensables

Things whose absence would make the code cleaner. The default action here is
**delete**.

### Comments (as deodorant)
**Spot it:** a method filled with explanatory comments; comments narrating *what*
the next lines do.
**Diagnostic:** "Could a better name or shape make this comment unnecessary?"
**Fix:** Extract Variable for a complex expression; Extract Method for a commented
section (the comment text is the method name); Rename Method when the comment
compensates for a bad name; Introduce Assertion when the comment states an
invariant.
**Detector:** `find_redundant_comments.py`, `find_comment_smells.py`.
**Ignore when:** the comment explains *why* — rationale, constraints, links to the
bug it works around. Those are the comments worth keeping (see
`naming-comments-readability.md`).

### Duplicate code
**Spot it:** two fragments that look almost identical — or *do* the same thing
while looking different (the subtle kind a parser can't match).
**Diagnostic:** "If this logic changes, how many places must remember to change?"
**Fix:** same class → Extract Method. Sibling subclasses → Extract Method, then
Pull Up Method/Field (Pull Up Constructor Body for `__init__` overlap); similar
but not identical → Form Template Method; same job, different algorithm → pick the
better one and Substitute Algorithm. Unrelated classes → Extract Superclass, or
Extract Class and share the component. Same code in every branch of a conditional
→ Consolidate Duplicate Conditional Fragments; many conditions running the same
code → Consolidate Conditional Expression.
**Detector:** `find_duplicates.py` (AST-normalized), `find_design_smells.py`
(`duplicate_conditional_fragment`).
**Ignore when:** the two fragments merely *coincide* today and serve different
masters — forcing them together creates the wrong abstraction, which is costlier
than the duplication (see `overengineering-and-abstraction.md`).

### Lazy class
**Spot it:** a class that doesn't do enough to earn its keep — an empty subclass, a
class with one trivial method, the residue of past refactoring or future plans
that never came.
**Diagnostic:** "What would break if I inlined this into its caller or base?"
**Fix:** Inline Class; for near-empty subclasses, Collapse Hierarchy.
**Detector:** `find_design_smells.py` (`lazy_class`), `find_overengineering.py`.
**Ignore when:** it's a deliberate marker/sentinel type — then say so in its
docstring so the next reviewer doesn't ask.

### Data class with no behavior
**Spot it:** a class that is only fields plus getters/setters, while the logic that
operates on those fields lives in other classes that constantly reach in.
**Diagnostic:** "Should the behavior that envies this data live *on* it?"
**Fix:** Move Method / Extract Method the envious client logic onto the data; then
tighten access (in Python: drop setters, prefer `frozen=True`).
**Detector:** `find_code_smells.py` (`data_class`).
**Caveat:** pure DTOs / boundary objects (API payloads, rows, messages) are
legitimately behavior-free — don't force behavior onto a deliberate data carrier.
Judge by whether other code keeps manipulating its internals.

### Dead code
**Spot it:** unused variables, parameters, functions, classes, imports; branches
that can no longer execute.
**Diagnostic:** "Who calls this?" If the answer needs archaeology, let git
remember it instead.
**Fix:** delete. Remove Parameter for unused parameters; Inline Class / Collapse
Hierarchy for unused hierarchy levels.
**Detector:** `find_dead_code.py`; unreachable `except` via
`find_exception_issues.py`.
**Ignore when:** it's the public API of a library — unused *here* isn't unused.

### Speculative generality
**Spot it:** abstract hooks with one implementation, parameters no caller sets,
"future-proof" indirection, `if False`-style switches.
**Diagnostic:** "Is anything actually using this generality, today?"
**Fix:** delete it (YAGNI): Collapse Hierarchy for unused abstract layers, Inline
Class for needless delegation, Inline Method, Remove Parameter.
**Detector:** `find_overengineering.py`, `find_dead_code.py`,
`find_boolean_params.py`.
**Ignore when:** you're building a framework whose *users* need the hooks — but
make sure they exist, and check the "unused" element isn't used by tests alone.

## Couplers

Excessive coupling between classes — or what happens when coupling is replaced by
excessive delegation.

### Feature envy
**Spot it:** a method that reaches into another object far more than its own —
pulling five fields off `obj` to compute something `obj` should compute itself.
**Diagnostic:** "Does this logic use another object's data more than its own?"
Things that change together should live together.
**Fix:** Move Method to the data it envies; if only part of the method envies,
Extract Method first and move that piece. Using several classes' data? Move it to
the one with the most.
**Detector:** `find_coupling_issues.py`.
**Ignore when:** behavior is deliberately kept separate from data — Strategy,
Visitor and friends do this on purpose to allow swapping behavior.

### Inappropriate intimacy
**Spot it:** one class using another's `_private` fields and methods; classes that
spend too much time together; mutual dependence.
**Diagnostic:** "Should this class know that much about that one's insides?"
**Fix:** Move Method / Move Field to put the parts where they're used; Hide
Delegate to make the relationship official; mutual dependence → Change
Bidirectional Association to Unidirectional. Subclass-parent intimacy: either
accept it explicitly (Replace Delegation with Inheritance) or break it.
**Detector:** `find_design_smells.py` (`inappropriate_intimacy`).
**Python note:** touching `_private` names within the *same module* is
conventional and fine; the smell is cross-module reaching.

### Message chains / train wrecks
**Spot it:** `a.b().c().d().e` — the caller is navigating someone else's structure
and is now coupled to every link.
**Diagnostic:** "Am I reaching through objects to get to the one I want?"
**Fix:** Hide Delegate — add a method on the first object that returns what the
caller actually needs (Law of Demeter); or Extract Method the chain's purpose and
Move Method it up the chain.
**Detector:** `find_coupling_issues.py`.
**Ignore when:** over-hiding creates the Middle Man smell — these two trade off.

### Middle man
**Spot it:** a class where most methods only forward to another class — often the
residue of over-zealous Hide Delegate, or a class whose useful work migrated away.
**Diagnostic:** "If this class vanished and callers talked to the delegate, what
would be lost?"
**Fix:** Remove Middle Man — let callers use the delegate directly.
**Detector:** `find_overengineering.py` (`thin_wrapper`).
**Ignore when:** the indirection is the point — Proxy/Decorator/adapters isolating
a dependency boundary exist deliberately.

### Incomplete library class
**Spot it:** you need behavior the library almost provides, and the library is
read-only.
**Diagnostic:** "Am I about to scatter workarounds for this gap across the
codebase?"
**Fix:** Introduce Foreign Method — in Python, a plain module function taking the
library object as its first argument, kept in one place; for a broad gap,
Introduce Local Extension — a subclass or wrapper that callers use instead.
Resist monkey-patching: it's invisible at the call site and leaks everywhere.
**Detector:** none — judgment.

## House addition (not in the classic catalog)

### Temporal / sequential coupling
**Spot it:** methods that must be called in a specific order — `obj.init();
obj.configure(); obj.run()` — where calling them wrong fails silently or corrupts
state.
**Diagnostic:** "Does correctness depend on call order the type doesn't enforce?"
**Fix:** encapsulate the sequence behind one method, or use a builder / context
manager so the order is structurally guaranteed and the bad states can't be
expressed.
