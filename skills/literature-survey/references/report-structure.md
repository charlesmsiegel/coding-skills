# Report structure

One self-contained `summary.html`: a persistent masthead over a tabbed body. The
masthead form comes from a real run of this kind of report; the tabbed body is this
repo's atlas convention.

## The masthead

It exists so the reader knows, in one screen and before any navigation, what was
consumed and what the report concludes.

**The stamp** — `LITERATURE SURVEY`, via `assemble.py --label`.

**The `h1`** — the subject, with an `<em>` accent clause. "Knowledge extraction and
knowledge graphs: *the state of the field*". Serif, large, one line if it will fit.

**The dek** — passed as `assemble.py --subtitle`, and the most valuable sentence in
the document.

> **A dek asserts. It does not describe.**

Bad, and the default an agent will reach for:

> This report surveys the current literature on knowledge extraction and knowledge
> graphs, covering 166 papers and 37 repositories.

That tells the reader nothing they cannot see from the masthead. Good:

> Extraction stopped being the hard part. Three years after LLMs made text→triples
> trivial, the literature has converged on a less comfortable conclusion: the
> bottlenecks are **identity**, **schema**, and above all **measurement** — and a
> significant fraction of the reported wins from graph structure do not survive a
> controlled baseline.

State the punchline. If the survey has no punchline, the synthesis is not finished.

**The meta-strip** — never written by hand. `corpus_stats.py` computes it from
`manifest.json`, the notes and `snowball.json`; `inject_masthead.py` puts it in at
the `<!--META_STRIP-->` marker. Read in full, papers archived, repositories cloned,
web pages and threads, corpus recency, snowball rounds and how they stopped,
unobtainable count.

A hand-written number here is an unverified claim in the one place whose job is to
establish that the report's numbers can be trusted. Do not do it.

## Tabs: fixed spine, derived middle

`assemble.py` takes any number of `NN-slug.html` fragments and reads each tab's
title from its first line: `<!-- tab: Human Readable Title -->`.

**Always present**, because these are what make the report checkable:

| order | tab | contents |
|---|---|---|
| 01 | Summary | the findings, as numbered `.finding` blocks |
| 02 | Corpus & method | what was searched, selected, read and declined — with reasons — plus the claims ledger |
| .. | *theme tabs* | 2–4, derived from the corpus |
| 90 | Open problems | what the literature itself says is unsettled |
| 91 | Decision guide | what a reader should do, per situation |
| 92 | Library | every artifact, linked to its local file |
| 93 | Gaps | what could not be obtained, and why |

**Theme tabs come from the corpus, not from a template.** Propose them from what
the notes actually cluster into, and confirm with the user. Titles should assert,
like the dek: "Identity: the layer where mistakes cannot be undone" beats "Entity
resolution".

Number fragments so the spine sorts around the middle — `01`, `02`, then `10`–`40`
for themes, then `90`+.

**Cap the top-level tabs at about eight to ten.** A long report has many sections;
they live *inside* a tab as numbered `.sec-head` blocks with the tab's own running
numbers, not as more tabs.

**Question-shape runs** replace the theme tabs with two: **Answer** (the
evidence-weighted verdict) and **Counter-evidence** (the strongest case against it,
at full strength — not a hedging paragraph at the end of the answer).

The **claims ledger** lives inside *Corpus & method*, not in its own tab. It is the
audit trail for the summary, and a reader who wants it wants it next to the method
that produced it.

## Citations, and what the verifier expects

Every claim in the report links to the local file:

```html
<a href="docs/papers/2510.20345-llm-empowered-knowledge-graph-construction-a-survey.pdf">the survey</a> (p.&nbsp;7)
```

`verify_locators.py` checks two separate things, and both fail the run:

- every locator in `docs/notes/*.json` resolves — artifact in the manifest with
  status `ok`, file present, sha256 unchanged, page within the document, quote
  literally present in text artifacts;
- every `href="docs/..."` in the report resolves to a file on disk.

So: **link to manifest paths, and cite pages you actually opened.** A relative link
to a file that was never fetched fails the build, which is the intent.

## The component kit

Styled by `assets/template.html`. Use them; they are what makes the report look
like one document.

| class | for |
|---|---|
| `.sec-head` + `.n` + `h2` | numbered section headings inside a tab |
| `.lead` | the opening paragraph of a section |
| `.finding` + `.fnum` + `.fbody` | a numbered finding with a title and body |
| `.kpis` / `.kpi.good\|warn\|bad\|neutral` | a row of numbers |
| `.callout` / `.callout.crim\|teal\|gold` + `.ct` | an aside that must not be missed |
| `.chart` + `.bar` / `.bar.warn\|bad` | simple horizontal bars |
| `.ve-card` + `.ve-card__label` | a verdict card |
| `.grid.g2\|g3` | side-by-side comparison |
| `.tablewrap` + `table` | any table, so it scrolls on narrow screens |
| `.bibgroup` + `.bibtable` | the library tab |
| `.tag.t-yes\|t-part\|t-no` | a three-state marker in a table |
| `.pullquote` | a sentence worth stopping on |
| `.term` / `.mono` | a term of art, or code |

## Assembling

```bash
python "$SKILL/scripts/corpus_stats.py" --out "$OUT" \
  --json-out "$OUT/meta.json" --html-out "$OUT/strip.html"
python "$SKILL/scripts/assemble.py" --tabs-dir "$OUT/tabs" --out "$OUT/summary.html" \
  --title "Team knowledge — the state of the field" \
  --subtitle "The bottleneck is measurement, not extraction."
python "$SKILL/scripts/inject_masthead.py" --report "$OUT/summary.html" \
  --strip "$OUT/strip.html" --meta "$OUT/meta.json"
python "$SKILL/scripts/verify_locators.py" --out "$OUT" --report "$OUT/summary.html"
```

Order matters: stats before assembly (the strip has to exist), injection after
assembly (the marker has to be in the file), verification last (it checks the
finished report's links). Injection is idempotent, so re-running after a stats
change replaces the numbers rather than stacking them.
