# Report structure

One self-contained `summary.html`: a persistent masthead over a tabbed body. The
masthead tells the reader what was consumed and how far it can be trusted; the
tabbed body is this repo's atlas convention.

## The masthead

**The stamp** — `LEGAL SURVEY`, via `assemble.py --label`.

**The `h1`** — the subject, with an `<em>` accent clause. "FOIA Exemption 5 after
*Sierra Club*: *what survives in the Ninth Circuit*". Serif, large, one line if it
will fit.

**The dek** — passed as `assemble.py --subtitle`, and the most valuable sentence
in the document.

> **A dek asserts. It does not describe.**

Bad, and the default an agent will reach for:

> This report surveys the law on FOIA Exemption 5, covering 22 cases and 3
> regulations.

That tells the reader nothing they cannot see from the masthead. Good:

> The two-part test is settled and does the least work of any element: in the
> Ninth Circuit the question is almost always whether the agency *adopted* the
> reasoning, and on that the circuits have split three ways since 2021.

State the answer. If the survey has no answer, the synthesis is not finished.

**The meta-strip** — never written by hand. `corpus_stats.py` computes it from
`manifest.json`, the notes, `treatment.json` and `snowball.json`;
`inject_masthead.py` puts it in at the `<!--META_STRIP-->` marker. Read in full,
cases, statutes and regulations, secondary sources, binding authority in the
forum, treatment, snowball rounds and how they stopped, unobtainable count.

A hand-written number here is an unverified claim in the one place whose job is to
establish that the report's numbers can be trusted. Do not do it. The treatment
cell in particular is the number a reader will act on, and it is computed so that
it always carries the qualifier — "open citation graph, not a citator" is part of
the figure, not a footnote to it.

## Tabs: fixed spine, derived middle

`assemble.py` takes any number of `NN-slug.html` fragments and reads each tab's
title from its first line: `<!-- tab: Human Readable Title -->`.

**Always present**, because these are what make the report checkable:

| order | tab | contents |
|---|---|---|
| 01 | Summary | the findings, as numbered `.finding` blocks |
| 02 | Question & method | the question presented, the forum, the as-of date, what was searched, selected, read and declined — with reasons — the claims ledger, and the citator disclaimer |
| .. | *issue tabs* | 2–4, derived from the corpus |
| 90 | Open questions | what the authorities themselves leave unresolved, and the splits |
| 91 | Practical guidance | what a reader should do, per situation |
| 92 | Authorities | every authority, linked to its local file, with weight and treatment |
| 93 | Gaps | what could not be obtained, and what was not checked |

**Issue tabs come from the corpus, not from a template.** Propose them from what
the notes actually cluster into — usually the elements of the test, or the stages
of the analysis — and confirm with the user. Titles should assert, like the dek:
"Adoption: the element that decides the case" beats "Adoption".

Number fragments so the spine sorts around the middle — `01`, `02`, then `10`–`40`
for issues, then `90`+.

**Cap the top-level tabs at about eight to ten.** A long memo has many sections;
they live *inside* a tab as numbered `.sec-head` blocks, not as more tabs.

**Question-shape runs** replace the issue tabs with two: **Answer** (the
authority-weighted conclusion, with a stated confidence) and **Counter-authority**
(the strongest case against it, at full strength — not a hedging paragraph at the
end of the answer). If a reader could brief the other side from your
counter-authority tab, it is doing its job.

The **claims ledger** lives inside *Question & method*, not in its own tab. It is
the audit trail for the summary, and a reader who wants it wants it next to the
method that produced it.

## The Authorities tab

This is the tab a lawyer will actually use, so it carries more than a list. One
row per authority, grouped by weight:

| column | contents |
|---|---|
| Authority | case name or code section, linked to the local file |
| Citation | the reporter or code citation, and the court and year |
| Weight | binding / persuasive / secondary, with the reason on hover or in a note |
| Treatment | good / caution / negative / unvalidated, from `treatment.json` |
| As of | for statutes and regulations, the version date. Non-negotiable. |

Use `.tag.t-yes|t-part|t-no` for the treatment column so a negative signal is
visible at a glance, and put the disclaimer directly under the table, not only in
the method tab.

## Citations, and what the verifier expects

Every claim in the report links to the local file and carries its pincite:

```html
<a href="docs/case/410us113-doe-v-agency.html">Doe v. Agency</a>, 410 U.S. 113,
118 (1973)
```

`verify_authorities.py` checks four things, and the first three fail the run:

- every locator in `docs/notes/*.json` resolves — authority in the manifest with
  status `ok`, file present, sha256 unchanged, page within the document, quote
  literally present in text artifacts;
- every archived case's own reporter citation appears in its text, so a real file
  cannot stand in for an invented cite;
- every `href="docs/..."` in the report resolves to a file on disk;
- and every claim resting on a case with negative treatment carries a
  `treatment_ack`, or the run fails; `caution` and `unchecked` are reported
  unverifiable, which is not clean.

So: **link to manifest paths, and pincite pages you actually opened.** A relative
link to a document that was never fetched fails the build, which is the intent.

## What the report must say about itself

Two statements, both non-optional, both in *Question & method* and repeated where
they bite:

**The citator disclaimer.** Treatment was checked against an open citation graph
by language matching. No commercial citator was consulted. See
`validating-authority.md` for the wording.

**Not legal advice.** Put it in `assemble.py --footer`, which takes raw HTML:

```
This report is legal research, not legal advice. It describes authorities
retrieved and read on <date>; it is not a substitute for a licensed
practitioner's judgment, and no attorney-client relationship arises from it.
```

The footer is injected as raw HTML and is not escaped, so it must never carry
text that came off the open web.

## The component kit

Styled by `assets/template.html`. Use them; they are what makes the report look
like one document.

| class | for |
|---|---|
| `.sec-head` + `.n` + `h2` | numbered section headings inside a tab |
| `.lead` | the opening paragraph of a section |
| `.finding` + `.fnum` + `.fbody` | a numbered finding with a title and body |
| `.kpis` / `.kpi.good\|warn\|bad\|neutral` | a row of numbers |
| `.callout` / `.callout.crim\|teal\|gold` + `.ct` | an aside that must not be missed — the citator disclaimer belongs in one |
| `.chart` + `.bar` / `.bar.warn\|bad` | simple horizontal bars |
| `.ve-card` + `.ve-card__label` | a verdict card — good for the answer's confidence |
| `.grid.g2\|g3` | side-by-side comparison, e.g. two sides of a split |
| `.tablewrap` + `table` | any table, so it scrolls on narrow screens |
| `.bibgroup` + `.bibtable` | the authorities tab |
| `.tag.t-yes\|t-part\|t-no` | a three-state marker — weight, treatment |
| `.pullquote` | a sentence of an opinion worth stopping on |
| `.term` / `.mono` | a term of art, or a code citation |

## Assembling

```bash
python "$SKILL/scripts/corpus_stats.py" --out "$OUT" \
  --json-out "$OUT/meta.json" --html-out "$OUT/strip.html"
python "$SKILL/scripts/assemble.py" --tabs-dir "$OUT/tabs" --out "$OUT/summary.html" \
  --title "FOIA Exemption 5 after Sierra Club" \
  --subtitle "The two-part test is settled and decides almost nothing." \
  --footer "This report is legal research, not legal advice."
python "$SKILL/scripts/inject_masthead.py" --report "$OUT/summary.html" \
  --strip "$OUT/strip.html" --meta "$OUT/meta.json"
python "$SKILL/scripts/verify_authorities.py" --out "$OUT" --report "$OUT/summary.html"
```

Order matters: stats before assembly (the strip has to exist), injection after
assembly (the marker has to be in the file), verification last (it checks the
finished report's links). Injection is idempotent, so re-running after a stats
change replaces the numbers rather than stacking them.
