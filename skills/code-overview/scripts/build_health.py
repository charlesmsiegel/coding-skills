#!/usr/bin/env python3
"""Turn a code doctor's findings into a graded health document.

Consumes the JSON any of the doctors emit (`analyze_all.py --format json`,
`analyze_django.py --format json`, or a single detector's output), scores it
against the rubric in rubric.py, and writes an HTML page carrying a letter grade
plus a `code-health-meta` JSON block so the numbers can be extracted later
without parsing the page.

Two modes:

  package  one unit — findings for its roots, sized over its roots
  --root   the repo — the same arithmetic over the union of every package's
           findings plus any repo-wide ones, and a per-package grade table read
           back out of the package documents

The root grade is recomputed rather than averaged from package grades, so it is
the same kind of measurement as a package grade and comparable to one.

Usage:
  python build_health.py --out src/billing/docs/health.html \\
      --findings billing.json --name billing --root-dir src/billing \\
      --language python --doctor python-code-doctor --repo .

  python build_health.py --root --out docs/health.html --map docs/code-overview.json \\
      --findings billing.json --findings web.json --findings repo-wide.json --repo .
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import common
import rubric
from common import (HEALTH_SCHEMA, doc_path, esc, git_sha, json_block, listed_packages,
                    load_map, load_reports, measure, read_asset, read_meta, rel_href,
                    render, warn, within)
from health_render import (SEVERITY_ORDER, grade_class, headline_badges, line_number, panels,
                           render_by_type, render_candidates, render_category_rows,
                           render_caveats, render_coverage, render_findings_summary,
                           render_package_table, render_top_findings)

# Distinct from an empty set, which means "this analysis covered nothing".
GRADE_EVERYTHING = object()


def score_categories(findings: list[dict], loc: int, covered) -> dict:
    """Bucket findings by rubric category and score each one.

    `covered` names the categories this analysis was capable of reporting;
    `GRADE_EVERYTHING` means all of them. A category outside it comes back
    ungraded — a doctor with no duplication detector must not hand out a free
    100 for duplication, and neither must a detector that was skipped, crashed,
    or never ran at all.
    """
    buckets: dict[str, list[dict]] = {key: [] for key in rubric.CATEGORY_KEYS}
    unmapped: set[str] = set()
    for finding in findings:
        category, matched = rubric.categorize(finding)
        finding["_rubric_category"] = category
        buckets[category].append(finding)
        if not matched:
            unmapped.add(rubric.finding_type(finding))

    rows = []
    scores: dict[str, float | None] = {}
    for key, label, weight, half_life in rubric.CATEGORIES:
        bucket = buckets[key]
        counts = {sev: sum(1 for f in bucket if f.get("severity") == sev) for sev in SEVERITY_ORDER}
        counts["total"] = len(bucket)
        weighted = sum(rubric.severity_weight(f.get("severity", "medium")) for f in bucket)
        density = rubric.density(weighted, loc)

        graded = covered is GRADE_EVERYTHING or key in covered
        # Round before grading, not after. A score of 92.9535 is published as
        # 93.0, and the bands say 93.0 is an A — so grading the unrounded value
        # would print "93.0" beside "A-" and contradict the documented scale.
        score = (round(rubric.score_from_density(density, half_life), 1)
                 if graded else None)
        scores[key] = score
        rows.append({
            "key": key, "label": label, "weight": weight, "half_life": half_life,
            "graded": graded,
            "score": score,
            "grade": rubric.grade_for(score),
            "density": round(density, 2),
            "findings": counts,
        })

    overall = rubric.weighted_overall(scores)
    overall = None if overall is None else round(overall, 1)
    return {
        "categories": rows,
        "score": overall,
        "grade": rubric.grade_for(overall),
        "ungraded": [row["key"] for row in rows if not row["graded"]],
        "unmapped_types": sorted(unmapped),
    }


def relativize(value: str, repo: Path) -> str:
    """Cut the absolute repo prefix off a path.

    The doctors are run with an absolute path and echo it back, in the `file`
    field and inside description text alike. Left alone, the location column of
    every findings table is 80% sandbox path — and the documents are meant to be
    committed, where an absolute path from the machine that generated them is
    noise at best and misleading at worst.
    """
    prefix = str(repo)
    if not prefix.endswith("/"):
        prefix += "/"
    return str(value).replace(prefix, "").replace(str(repo), ".")


def top_findings(findings: list[dict], limit: int, repo: Path) -> list[dict]:
    ranked = sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.index(f.get("severity", "medium"))
                       if f.get("severity") in SEVERITY_ORDER else 1,
                       str(f.get("file", "")), line_number(f)),
    )
    out = []
    for finding in ranked[:limit]:
        category, _ = rubric.categorize(finding)
        out.append({
            "severity": finding.get("severity", "medium"),
            "type": rubric.finding_type(finding),
            "category": category,
            "file": relativize(finding.get("file", ""), repo),
            "line": line_number(finding),
            "description": relativize(str(finding.get("description", "")), repo)[:400],
            "suggestion": relativize(
                str(finding.get("suggestion") or finding.get("after") or ""), repo)[:300],
        })
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def scoring_roots(args, repo: Path, rolled_up: list[dict] | None = None) -> list[str]:
    """The code the grade is a claim about — repo-relative.

    Findings and the LOC they are divided by have to cover the same code. At the
    root that is the union of the mapped packages, not the whole checkout: when
    the user deliberately leaves a directory unassigned, measuring `.` puts its
    lines in the denominator while none of its findings are in the numerator,
    which quietly improves the repo's grade in proportion to how much code was
    left out.

    A package earns its place in the denominator by having been *analyzed*, and
    the only proof of that is its own graded health page. Naming a doctor in the
    map is an intention, not a result: a TypeScript package whose report never
    arrived, or arrived empty from a failed run, has a doctor in the map and no
    findings anywhere, so its lines are pure dilution. `rolled_up` carries the
    package rows already read back from those health pages.
    """
    if args.root_dir:
        return list(args.root_dir)
    if args.root and args.map:
        packages = load_map(args.map).get("packages", [])
        # Only packages some doctor actually reviews. A Go package beside a
        # Python one contributes no findings, so counting its lines dilutes the
        # Python density — far enough that an empty Python report could grade
        # A+ over the combined total.
        analyzed = [p for p in packages if p.get("doctor")]
        doctorless = [p["name"] for p in packages if not p.get("doctor")]
        if doctorless and analyzed:
            warn("packages with no doctor are excluded from the repo grade's size: "
                 f"{', '.join(doctorless)}. Their lines would dilute findings they cannot "
                 "contribute to; they still appear in the package table as ungraded.")

        # Now narrow further to those whose health page exists and is graded.
        # Before the package pages have been built there is nothing to narrow
        # by — that is the documented "run --root again afterwards" case — so
        # fall back to the doctored set rather than measuring nothing.
        if rolled_up:
            graded = {row["package"] for row in rolled_up if row.get("score") is not None}
            evidenced = [p for p in analyzed if p["name"] in graded]
            ungraded = [p["name"] for p in analyzed if p["name"] not in graded]
            if evidenced and ungraded:
                warn("packages with no graded health page are excluded from the repo "
                     f"grade's size: {', '.join(sorted(ungraded))}. A doctor named in the "
                     "map is an intention; only a graded health page is evidence the code "
                     "was examined, and unexamined lines dilute every finding beside them.")
            if evidenced:
                analyzed = evidenced

        roots = [root for package in (analyzed or packages)
                 for root in package.get("roots", [])]
        if roots and "." not in roots:
            return sorted(set(roots))
    return ["."]


def is_repo_wide(path: str, repo: Path, sized_extensions=frozenset()) -> bool:
    """Does this finding describe the repository rather than any one package?

    Two shapes qualify, and missing the second one was expensive:

    - **The repo root itself.** A finding with no single file to point at is
      reported *against the directory*: `find_untested_modules.py` emits
      `no_tests_in_repo` and `find_dependency_issues.py` emits
      `no_dependency_manifest` with `file=str(root)`. Requiring `is_file()` sent
      both — high severity, and about the repo as a whole — out of scope, so a
      project with no tests and no manifest rolled up to a clean **A+**.
    - **A non-source file directly in the repo root** — `tsconfig.json`, the
      root manifest, a CI config. It belongs to no package but describes the
      whole tree, and it contributes no lines to any denominator, so keeping it
      costs nothing.

    A *source* file directly in the repo root is deliberately excluded unless
    the repo itself is mapped. `loose.py` that the user left unassigned is code
    they chose not to grade: counting its findings while `scoring_roots` leaves
    its lines out is the same numerator/denominator asymmetry as everywhere
    else, pointed the other way — it penalizes the repo for code outside the
    scope it defined.
    """
    if not path:
        return False
    try:
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else repo / candidate).resolve()
    except (OSError, ValueError):
        return False
    if resolved == repo:
        return True
    if resolved.parent != repo or not resolved.is_file():
        return False
    return resolved.suffix.lower() not in sized_extensions


def resolve_coverage(args, reports: list[dict], trusted: set[str] | None = None):
    """Which rubric categories this analysis is entitled to grade.

    Coverage is **evidence**, not the doctor's advertised capability. A report
    from `analyze_all.py` names the analyzers that ran, so it can be believed
    per category; the other two shapes cannot, and are handled by their own
    rules rather than being credited with everything the doctor could have done.
    That distinction is what stops `--skip-duplicates`, a crashed detector, or a
    single-detector report from being read as a clean bill of health.

    `trusted` is who a report may speak for. Default (`None`) is the ordinary
    single-doctor run's `{"", args.doctor}`. A merged envelope passes every
    doctor it names running instead — each one is still resolved by its own
    per-detector evidence below, never by its bare presence in the envelope,
    which is what makes this the *only* coverage path rather than a second one
    beside it: a doctor whose own detector crashed does not get credited just
    because it is a doctor the envelope trusts.
    """
    # Remembered before the default fills `trusted` in, because it is the
    # actual basis for two things below: whether an unlabelled report is
    # attributable to anyone, and what a "different doctor than ..." warning
    # should name as the reason. Both have to reflect who this *call* trusts
    # — the merged envelope's `doctors_run`, or the single `--doctor` flag —
    # not `args.doctor`'s mere presence, which does not change what a
    # --merged caller trusts even when it is also set.
    merged_trust = trusted is not None
    if trusted is None:
        trusted = {"", args.doctor}
    if args.assume_full_coverage:
        return GRADE_EVERYTHING
    if args.covers:
        named = {c.strip() for part in args.covers for c in part.split(",") if c.strip()}
        unknown = named - set(rubric.CATEGORY_KEYS)
        if unknown:
            raise SystemExit(f"error: --covers names unknown categories: {', '.join(sorted(unknown))}")
        return named

    # A failed run is evidence of a *gap*, and it outranks the evidence beside
    # it. In the recommended Python+Django merge, a Django crash that leaves a
    # zero-byte file still leaves the Python report full and clean, and every
    # Python-profile category — which is all of them — would grade A+. But
    # Django is what sees N+1 queries, missing CSRF tokens and insecure
    # settings, so the categories Python "covered" were in fact half-measured.
    # Nothing in the files says which doctor was supposed to write the empty
    # one, so the gap cannot be subtracted from particular categories: the only
    # honest answer is to grade none of them until someone says what ran.
    if any(report.get("empty_artifact") for report in reports):
        warn("a findings file is empty, which means a doctor failed rather than found "
             "nothing — and nothing says which categories it was meant to cover, so none "
             "are graded. Re-run the failed doctor, or pass --covers a,b,c to declare what "
             "the surviving reports examined.")
        return set()

    # A report only speaks for the doctor that produced it. The workflow passes
    # every findings file to every package, so a package's page routinely sees
    # reports from doctors that never looked at its language — and a *successful*
    # foreign report is as wrong a source of coverage as a failed one. With only
    # a TypeScript report present, a Python package graded Security 100 off
    # `tsconfig`; the doctor-profile cap could not catch it, because both doctors
    # cover the same rubric categories. An unlabelled report is attributed to
    # --doctor, which is right for the ordinary single-doctor run.
    foreign = [r for r in reports
               if r["shape"] == common.SHAPE_FULL and r["doctor"] and r["doctor"] not in trusted]
    if foreign:
        # "than {args.doctor}" reads as nonsense once trust can come from a
        # merged envelope naming several doctors rather than one --doctor
        # flag — "different doctor than (none named)" beside a page that
        # trusted three of them. Worse, it was flatly wrong whenever --merged
        # and --doctor were both given: trust still came from the envelope's
        # `doctors_run`, not from `--doctor` (which is not even necessarily a
        # member of it), but the warning named `--doctor` anyway because it
        # happened to be set. `merged_trust` is what actually decides which
        # basis is real, not whether `args.doctor` is truthy.
        others = trusted - {""}
        trust_desc = ((f"the merged envelope's trusted doctors ({', '.join(sorted(others))})"
                       if others else "(none named)") if merged_trust
                      else (args.doctor if args.doctor else "(none named)"))
        warn(f"these reports were produced by a different doctor than {trust_desc} and "
             "contribute findings but no coverage: "
             f"{', '.join(sorted({r['doctor'] for r in foreign}))}.")

    evidenced = [report for report in reports
                 if report["shape"] == common.SHAPE_FULL
                 and report["doctor"] in trusted]
    if evidenced:
        # Resolve each report on its own, then union. Both halves matter.
        #
        # Per report, because a failure belongs to the run it happened in. The
        # workflow hands every findings file to every package, so a mixed repo
        # passes a Python report and a TypeScript one side by side — and a
        # crashed `tsconfig` analyzer, which maps to Security, was subtracted
        # from the *combined* set and ungraded Security on a Python package
        # whose own security detector had completed cleanly.
        #
        # Union, because a category one report skipped and another measured is
        # measured. That is what makes companion doctors add up.
        covered: set[str] = set()
        unattributed = 0
        for report in evidenced:
            ran = {rubric.DETECTOR_CATEGORIES[name] for name in report["ran"]
                   if name in rubric.DETECTOR_CATEGORIES}
            # A rubric category usually has several detectors behind it. If any
            # of *this report's* detectors for it was skipped or crashed, the
            # category was only partly measured — and a partial measurement can
            # only miss findings, never invent them, so grading it would
            # systematically flatter the code. Skipping exception_issues while
            # running mutation_hazards leaves Correctness unmeasured, not clean.
            absent = {rubric.DETECTOR_CATEGORIES[name]
                      for name in (report["skipped"] | set(report["errors"]))
                      if name in rubric.DETECTOR_CATEGORIES}
            resolved = ran - absent

            # Evidence still cannot exceed what the doctor that produced THIS
            # report is able to detect — capped per report, not once at the
            # end against a single --doctor. A single end-cap let a merged
            # envelope's other doctors evade their own profile entirely:
            # with --doctor typically unset on the --merged path,
            # `DOCTOR_COVERAGE.get(args.doctor)` resolved to None and applied
            # no cap at all, so code-doctor — whose raw layer can prove a
            # merge marker and essentially nothing else in Correctness —
            # could be credited Correctness off a `mutation_hazards` token it
            # has no business reporting.
            #
            # An unlabelled report (report["doctor"] == "") speaks for
            # --doctor, same as it always has, capped by --doctor's profile —
            # but only when --doctor is actually set. That is the existing
            # --findings-only behaviour (where --doctor is documented as
            # always paired with a bare --findings file) and must not change
            # here. When --doctor is *also* unset — the normal state of the
            # --merged path, which attributes every one of its own records by
            # their `doctor` field instead of a flag — an unlabelled report
            # has no established provenance at all. Leaving it uncapped
            # (`DOCTOR_COVERAGE.get("")` is None, same as an unrecognized
            # name, and used to be read as "no profile to cap against"
            # instead of "no one to credit") is exactly the hole being closed
            # here: a code-doctor-only merge plus one unlabelled --findings
            # report claiming `mutation_hazards` ran could grade Correctness
            # A+ — the one category code-doctor's own profile exists to
            # withhold. Nothing here says whose evidence this is, so the safe
            # reading is that the report contributes its findings but grants
            # no coverage at all: a category nothing established is ungraded,
            # not graded on an anonymous claim.
            #
            # A *named* report — every merged-envelope report, or a labelled
            # `--findings <doctor>:<path>` — is capped by its own doctor's
            # profile instead. A name absent from DOCTOR_COVERAGE is capped
            # to the empty set rather than left uncapped: a deliberate
            # decision, not a fallout of `.get(..., set())`. This rubric has
            # no coverage profile to check an unrecognized doctor's claims
            # against, and crediting its raw per-detector tokens with no
            # upper bound at all is exactly the same hole in another guise —
            # the empty set is the safe direction to be wrong in either way.
            if report["doctor"]:
                profile = rubric.DOCTOR_COVERAGE.get(report["doctor"], set())
                covered |= resolved & profile
            elif args.doctor:
                profile = rubric.DOCTOR_COVERAGE.get(args.doctor)
                covered |= resolved if profile is None else (resolved & profile)
            elif resolved:
                unattributed += 1
        if unattributed:
            warn(f"{unattributed} --findings report(s) have no '<doctor>:' label and no "
                 "--doctor to attribute them to, so they contribute findings but no "
                 "coverage. Label them '<doctor>:path', or pass --doctor, to credit them.")
        return covered

    if not reports:
        # The documented "codemap plus an ungraded health page" answer for a
        # language no doctor covers. There is no artifact to point at, and the
        # skill says not to invent one, so this is a normal input rather than a
        # mistake — it just cannot be graded.
        warn("no findings were supplied, so every category is ungraded. That is the "
             "documented answer for a language with no doctor; pass --covers a,b,c if "
             "something did examine this code.")
        return set()

    # Nothing here names what ran. A bare JSON list is what `analyze_django.py`
    # emits *and* what `find_duplicates.py --format json` emits, so the file
    # cannot distinguish a full Django run from one detector that found nothing;
    # crediting the doctor's profile graded the latter A+ in every category.
    warn("no findings file says which analyzers ran — a bare list or a single detector's "
         "output cannot, so every category is ungraded. Pass --covers a,b,c to declare what "
         "this analysis examined (for django-code-doctor alone that is "
         f"{','.join(sorted(rubric.DOCTOR_COVERAGE['django-code-doctor']))}), or "
         "--assume-full-coverage.")
    return set()


def analyzer_gaps(reports: list[dict],
                  default_doctor: str) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Skipped and crashed analyzers, filed under the doctor whose report named them.

    The merged envelope carries `analyzers_skipped`/`analyzer_errors` per doctor
    already — `load_merged` puts each one on its own report's `skipped`/`errors`
    — and `resolve_coverage` reads them per report for exactly that reason. What
    reached no reader was the name: a flat pool of analyzer names says a gap
    exists but not whose evidence it undermines, so a reader could not tell
    which doctor to re-run.

    Skipped stays filtered to *skipped everywhere*, the same set `load_reports`
    computes and the same one `resolve_coverage`'s per-report union can still
    leave graded: a detector one companion report skipped and another ran is
    covered, and reporting it as a gap here — even attributed correctly — would
    tell the reader a category is unmeasured when it is not. An unlabelled
    report speaks for `--doctor`, same as everywhere else this rubric attributes
    one, so its gap is filed there rather than under an empty name.
    """
    ran_anywhere = {name for report in reports for name in report.get("ran") or ()}
    skipped: dict[str, set[str]] = {}
    errors: dict[str, dict[str, str]] = {}
    for report in reports:
        doctor = report.get("doctor") or default_doctor or "(doctor unspecified)"
        gap = (report.get("skipped") or set()) - ran_anywhere
        if gap:
            skipped.setdefault(doctor, set()).update(gap)
        if report.get("errors"):
            errors.setdefault(doctor, {}).update(report["errors"])
    return ({doctor: sorted(names) for doctor, names in skipped.items()},
            {doctor: dict(messages) for doctor, messages in errors.items()})


def build(args, reports: list[dict], candidates: list[dict] | None = None,
          doctor_errors: dict[str, str] | None = None,
          completeness: dict | None = None,
          doctors: list[str] | None = None) -> tuple[str, dict]:
    candidates = candidates or []
    doctor_errors = doctor_errors or {}
    completeness = completeness or {}
    doctors = doctors or []
    repo = Path(args.repo).resolve()
    # Read the package roll-up first: at the root, which packages have a graded
    # health page decides which roots may enter the denominator.
    packages: list[dict] = []
    links: dict[str, str] = {}
    if args.root:
        packages, links = collect_packages(repo, args.map, Path(args.out))
    relative_roots = scoring_roots(args, repo, packages)
    roots = [repo / r for r in relative_roots]

    # Every doctor that contributed a report, for sizing. Not just --doctor:
    # that flag caps coverage, and the recommended Python+Django merge names
    # python-code-doctor while a Django report sits beside it carrying the
    # templates that belong in the denominator.
    report_doctors = {report["doctor"] for report in reports if report["doctor"]}
    report_doctors.add(args.doctor)

    # A root that vanished between runs measures zero lines, and a clean report
    # over nothing would grade A+ for a package that no longer exists. Map drift
    # is exactly what the re-run workflow anticipates, so it has to be caught.
    # Warning about it was not enough — the previous version said "nothing is
    # graded over a path that is not there" and then printed A+ (100.0). The
    # enforcement is on measured size below, because zero files is the honest
    # test: it catches a renamed root, an emptied one, and a filter that matched
    # nothing, all of which mean the same thing — nothing was examined.
    missing_roots = [r for r, path in zip(relative_roots, roots) if not path.exists()]
    if missing_roots:
        warn(f"these roots do not exist: {', '.join(missing_roots)} — the package map is "
             "stale, and the lines behind any findings about them are not being counted.")

    # Keep only findings about the code this document is a claim about. The
    # doctors are run from the repo root so they can see manifests, tests and
    # settings; without that context they invent findings (a package with no
    # manifest of its own reports a missing one) and miss real ones.
    #
    # Scoping happens per report and *before* deduplication, so the merged-
    # duplicate count describes this unit. Deduplicating first let a package
    # page announce duplicates that were merged in a different package.
    scope = [repo / s for s in (args.scope or relative_roots)]
    out_of_scope = 0
    if scope and [Path(s).resolve() for s in scope] != [repo]:
        # Which extensions this analysis would put in the denominator, before
        # the findings-derived part (that would be circular — it is computed
        # from the scoped findings below). Used only to tell a root-level
        # *source* file, whose lines are not being measured, from root-level
        # configuration, whose lines nothing measures anywhere.
        measured_here = ("." in relative_roots)
        root_file_exclusions = (frozenset() if measured_here
                                else common.sizing_extensions([], args.include_extension,
                                                              report_doctors))

        def in_scope(finding: dict) -> bool:
            path = str(finding.get("file", ""))
            if within(path, scope, repo):
                return True
            # The repo grade has to keep findings about the repository itself —
            # root-level configuration, and the whole-project findings reported
            # against the root directory. An unmapped *sub*directory is still
            # out, and so is unassigned root-level source; the exception is for
            # the repo, not for code the user chose to leave outside the map.
            return args.root and is_repo_wide(path, repo, root_file_exclusions)

        scoped = []
        for report in reports:
            kept = [f for f in report["findings"] if in_scope(f)]
            out_of_scope += len(report["findings"]) - len(kept)
            scoped.append({**report, "findings": kept})
        reports = scoped

        # Candidates are scoped with the same predicate as the findings, so a
        # candidate about code outside this document is dropped for the same
        # reason a finding about it would be.
        candidates = [record for record in candidates if in_scope(record)]

    findings, duplicates = common.dedupe(reports)

    # Per doctor, not the flat pool `load_reports` handed `main()` — that pool
    # is empty on the --merged path entirely (it is built only from
    # `--findings`), and even on the --findings path it cannot say which
    # doctor a gap belongs to. `reports` is the merged list by now (`main()`
    # extends it with `merged["reports"]` before calling `build`), and every
    # report on it already carries its own `doctor`/`skipped`/`errors` —
    # the same fields `resolve_coverage` above just used — so this is the
    # first point both paths have the same evidence in the same shape.
    skipped_by_doctor, errors_by_doctor = analyzer_gaps(reports, args.doctor)

    # Size over what was analyzed, which for a Django package includes its
    # templates. Each override replaces only its own field, so `--files` alone
    # and `--loc` alone both work; coupling them made one silently ignored and
    # the other zero out the count it did not set.
    extensions = common.sizing_extensions(findings, args.include_extension, report_doctors)
    size = measure(roots, extensions)
    if args.loc is not None:
        size["loc"] = args.loc
    if args.files is not None:
        size["files"] = args.files

    # A merged doctor is trusted the same way an unlabelled --findings report
    # is: resolved by its own per-detector evidence, never by its bare
    # presence in the envelope. There is one coverage path, not a coarse one
    # beside the fine one — unioning DOCTOR_COVERAGE for every doctor the
    # envelope names credited a category no detector had actually run,
    # including one whose detector for it had crashed.
    trusted = ({""} | set(doctors)) if doctors else None
    covered = resolve_coverage(args, reports, trusted)

    if doctor_errors or completeness:
        if covered is GRADE_EVERYTHING:
            # Direct evidence of a gap outranks a blanket human assertion: a
            # doctor the envelope says crashed, or a completeness gate the
            # producer itself called inadequate, cannot be waved off by
            # --assume-full-coverage into a clean bill of health for exactly
            # the categories that evidence names.
            warn("--assume-full-coverage is overridden for the categories the merged "
                 "envelope has direct evidence of a gap in (a failed doctor or an "
                 "inadequate completeness signal) — a demonstrated failure outranks a "
                 "blanket assertion that everything was covered.")
            covered = set(rubric.CATEGORY_KEYS)

        # A doctor that crashed measured nothing. Only the categories it
        # *alone* covered become unknown — where a surviving doctor covers the
        # same ground, that ground was still measured.
        #
        # This is USUALLY a no-op when `covered` came from resolve_coverage's
        # ordinary per-report evidenced loop, but "always a subset of
        # `surviving`" does not actually hold there — it is a per-report
        # claim, not a global one. A *named* report is capped by its own
        # doctor's profile, and that doctor has to be one of `doctors` to
        # have reached the loop as `evidenced` in the first place, so its
        # contribution is guaranteed inside `surviving`. An *unlabelled*
        # report capped by `--doctor` instead carries no such guarantee:
        # `--doctor` can name a doctor the envelope never ran (a merge
        # analyzed by code-doctor and python-code-doctor, graded with
        # `--doctor django-code-doctor` pointed at a hand-picked report), so
        # `covered` can exceed `surviving` and this subtraction genuinely
        # removes ground from it — the same way it does for the two paths
        # that are NOT built from per-report-capped evidence at all:
        # `--assume-full-coverage` (materialized to every category above) and
        # `--covers` (an arbitrary human-declared set), which can equally
        # name a category no running doctor's own profile actually reaches.
        # See test_assume_full_coverage_yields_to_a_doctor_
        # that_demonstrably_crashed and test_assume_full_coverage_does_not_
        # over_subtract_what_a_survivor_covers in test_merged_envelope.py.
        surviving: set[str] = set()
        for name in doctors:
            surviving |= rubric.DOCTOR_COVERAGE.get(name, set())
        for failed, message in doctor_errors.items():
            profile = rubric.DOCTOR_COVERAGE.get(failed)
            if profile is None:
                warn(f"{failed} failed ({message}) but is not a doctor this rubric knows "
                     "the coverage profile of, so nothing is subtracted for it.")
                continue
            covered -= profile - surviving

        for block in completeness.values():
            covered -= rubric.ungraded_from_completeness(block)

    # Nothing to divide by means nothing was examined, whatever the reports say.
    # The LOC floor turns an empty tree into a 1000-line denominator, so a clean
    # report over a root that no longer exists scored a confident A+ for code
    # that is not there. An explicit --loc/--files is the caller asserting a size
    # this script cannot see, and is left alone.
    if (missing_roots or size["files"] == 0) and args.loc is None and args.files is None:
        # Any missing root is enough, not only all of them. A two-root package
        # whose `shared/types` was deleted still measures its `src/app` files,
        # so a files-are-zero test left it graded — against a denominator
        # covering part of the package while the findings covered all of it.
        # Partial sizing is exactly the asymmetry the whole scoping design
        # exists to prevent.
        reason = (f"these roots do not exist: {', '.join(missing_roots)}" if missing_roots
                  else f"no source files under {', '.join(relative_roots)}")
        warn(f"{reason} — the findings and the lines they would be divided by no longer "
             "describe the same code, so every category is ungraded rather than scored "
             "against a partial tree. Check the roots in the package map.")
        covered = set()
    scored = score_categories(findings, size["loc"], covered)

    meta = {
        "schema": HEALTH_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": args.name,
        "roots": relative_roots,
        "language": args.language,
        "doctor": args.doctor,
        "generated": args.date or dt.date.today().isoformat(),
        "commit": args.commit or git_sha(repo),
        "size": size,
        "sized_extensions": sorted(extensions - common.CODE_EXTENSIONS),
        "missing_roots": missing_roots,
        "score": scored["score"],
        "grade": scored["grade"],
        "categories": scored["categories"],
        "ungraded": scored["ungraded"],
        "unmapped_types": scored["unmapped_types"],
        "analyzer_errors": errors_by_doctor,
        "analyzers_skipped": skipped_by_doctor,
        "findings_out_of_scope": out_of_scope,
        "duplicates_merged": duplicates,
        "findings_total": len(findings),
        "findings_by_severity": {sev: sum(1 for f in findings if f.get("severity") == sev)
                                 for sev in SEVERITY_ORDER},
        "top_findings": top_findings(findings, args.top, repo),
        "doctors": doctors,
        "doctor_errors": doctor_errors,
        "completeness": completeness,
        "candidates_total": len(candidates),
    }
    if packages:
        meta["packages"] = packages

    body = render(read_asset("health-body.html"), {
        "GRADE": esc(scored["grade"]),
        "GRADE_CLASS": grade_class(scored["grade"]),
        "SCORE": "—" if scored["score"] is None else f'{scored["score"]:.1f}',
        "SUBJECT": esc(args.name),
        "SUBJECT_DETAIL": esc("the whole repository" if relative_roots == ["."]
                              else ", ".join(relative_roots)),
        "HEADLINE_BADGES": headline_badges(meta),
        "UNGRADED_NOTE": ('<div class="callout warn">Nothing in this analysis could be graded — '
                          "the grade shown is a placeholder.</div>"
                          if scored["score"] is None else ""),
        "CATEGORY_ROWS": render_category_rows(scored["categories"]),
        "PACKAGE_TABLE": render_package_table(packages, links),
        "FINDINGS_SUMMARY": render_findings_summary(findings),
        "TOP_FINDINGS": render_top_findings(meta["top_findings"]),
        "BY_TYPE": render_by_type(findings),
        "CAVEATS": render_caveats(errors_by_doctor, skipped_by_doctor, scored["unmapped_types"],
                                  scored["ungraded"], args.note, out_of_scope,
                                  relative_roots, duplicates),
        "CANDIDATES": render_candidates(candidates),
        "COVERAGE": render_coverage(meta),
    })

    # [1:] drops everything before the first marker. The scaffold opens with an
    # explanatory comment, and treating that as element 0 of the split makes it a
    # fragment: it becomes tab #1, renders *active*, and hides the grade card
    # behind a panel with a mangled title. Everything before the first marker is
    # by definition not a tab.
    fragments = [f"<!-- tab:{part}" for part in body.split("<!-- tab:")[1:] if part.strip()]
    nav, sections = panels(fragments)
    sections += (f'\n<script type="application/json" id="code-health-meta">'
                 f"{json_block(meta)}</script>")

    scope = "repository" if args.root else "package"
    page = render(read_asset("template.html"), {
        "DOC_TITLE": esc(f"{args.name} — Code Health"),
        "DOC_LABEL": "CODE HEALTH",
        "DOC_SUBTITLE": esc(args.subtitle or f"Graded health of the {args.name} {scope}."),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}",
            meta["commit"],
            f"{size['files']} files, {size['loc']} lines",
            f"{len(findings)} findings, {len(candidates)} candidates",
            ", ".join(meta.get("doctors") or []) or args.doctor,
        ) if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_FOOTER": ("Generated by code-overview. The grade is a density of detectable "
                       "problems, not a verdict on the design — read the code map beside "
                       "it. Candidates are leads and are excluded from the score."),
    })
    return page, meta


def collect_packages(repo: Path, map_path: str | None, out: Path) -> tuple[list[dict], dict[str, str]]:
    """Read each package's health document back for the roll-up table."""
    if not map_path:
        return [], {}
    data = load_map(map_path)
    packages, links = [], {}
    for package in listed_packages(repo, data.get("packages", [])):
        health = doc_path(repo, package, "health")
        meta = read_meta(health)
        if meta is None:
            # Kept in the table rather than dropped. A package can legitimately
            # have no health page — the documented "codemap only" answer for a
            # language with no doctor — and a roll-up that silently omits it
            # looks complete while a whole package is missing from it.
            warn(f"no code-health metadata at {health} — {package['name']} is listed as "
                 "not generated (build its health page if that is not deliberate)")
            packages.append({
                "package": package["name"],
                "language": package.get("language", ""),
                "grade": rubric.UNGRADED,
                "score": None,
                "size": {},
                "findings_total": None,
                "docs": package.get("docs", ""),
                "generated": False,
            })
            continue
        packages.append({
            "package": meta.get("package", package["name"]),
            "language": meta.get("language", package.get("language", "")),
            "grade": meta.get("grade", rubric.UNGRADED),
            "score": meta.get("score"),
            "size": meta.get("size", {}),
            "findings_total": meta.get("findings_total", 0),
            "docs": package.get("docs", ""),
        })
        links[meta.get("package", package["name"])] = rel_href(out, health)
    return packages, links


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="path to write health.html to")
    parser.add_argument("--findings", action="append", default=[],
                        help="doctor JSON file, optionally '<doctor>:<path>' to say which "
                             "doctor produced it; repeat to merge, '-' for stdin. Label them "
                             "whenever more than one doctor's findings are passed — an "
                             "unlabelled report is attributed to --doctor, so a foreign one "
                             "would grant coverage it has no evidence for")
    parser.add_argument("--name", default="", help="package name (or repo name with --root)")
    parser.add_argument("--root-dir", action="append", default=[],
                        help="package root, repo-relative; repeat for a multi-root package. "
                             "With --root and --map, defaults to the union of the mapped "
                             "packages' roots so findings and LOC cover the same code")
    parser.add_argument("--scope", action="append", default=[],
                        help="keep only findings under this repo-relative path (default: the "
                             "root dirs). Lets the doctor run from the repo root — where it can "
                             "see manifests, tests and settings — while this document stays "
                             "about one package")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--language", default="")
    parser.add_argument("--doctor", default="", help="which doctor produced the findings")
    parser.add_argument("--root", action="store_true",
                        help="repo-level document: adds the per-package grade table")
    parser.add_argument("--map", default="", help="package map, for the --root grade table")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--note", action="append", default=[],
                        help="an extra caveat to record; repeatable")
    parser.add_argument("--top", type=int, default=25, help="findings to list (default: 25)")
    parser.add_argument("--loc", type=int, help="override the measured line count")
    parser.add_argument("--files", type=int, help="override the measured file count")
    parser.add_argument("--commit", default="", help="commit sha for the metadata")
    parser.add_argument("--date", default="", help="generation date (default: today)")
    parser.add_argument("--include-extension", action="append", default=[],
                        help="also count files with this extension when sizing "
                             "(template extensions are added automatically when the "
                             "findings show they were analyzed)")
    parser.add_argument("--covers", action="append", default=[],
                        help="comma-separated rubric categories this analysis actually examined "
                             f"({', '.join(rubric.CATEGORY_KEYS)}). Use when the findings come "
                             "from a single detector or an unrecognized tool, where nothing in "
                             "the file says what was looked at")
    parser.add_argument("--assume-full-coverage", action="store_true",
                        help="grade every category regardless of which doctor produced the "
                             "findings; without it, an unrecognized --doctor leaves everything "
                             "ungraded rather than scoring an unread language A+")
    parser.add_argument("--merged", type=Path, default=None,
                        help="code-doctor's merged envelope (code-doctor-merge/1); "
                             "supplies doctors, coverage, candidates and completeness at once")
    args = parser.parse_args(argv)

    # `--findings` is deliberately optional. The documented answer for a Go or
    # Rust package is "codemap plus an ungraded health page", and there is no
    # findings artifact to supply for it — requiring one would force the agent
    # to fabricate an empty JSON file, which this skill tells it not to do (and
    # which an empty file now correctly refuses to grade anyway).
    if not args.name:
        args.name = Path(args.repo).resolve().name if args.root else "package"

    # The per-report `errors`/`skipped` this returns (not the flat pool built
    # from them, which `build()` no longer takes) are what `resolve_coverage`
    # and `analyzer_gaps` read straight off each report in `reports` below —
    # this call's job here is reading the files and raising on a bad one.
    reports, _, _ = load_reports(args.findings)

    # `normalize_findings` already pulled candidates and a completeness block
    # out of any code-doctor report handed to `--findings` — that is what makes
    # `report["candidates"]`/`report["completeness"]` non-empty below. Captured
    # here, before `reports` gains --merged's own report records, so the two
    # sources are concatenated rather than counted twice: each report
    # contributes exactly once, whichever flag supplied it.
    candidates: list[dict] = [c for report in reports for c in report.get("candidates") or []]
    completeness: dict = {}
    for report in reports:
        block = report.get("completeness")
        if not block:
            continue
        # An unlabelled `--findings` report speaks for `--doctor`, the same
        # fallback `analyzer_gaps` uses for the same reason: a caveat with no
        # doctor beside it tells a reader something was incomplete but not
        # whose evidence to distrust.
        doctor = report.get("doctor") or args.doctor or "(doctor unspecified)"
        completeness[doctor] = {**completeness.get(doctor, {}), **block}

    merged = common.load_merged(args.merged) if args.merged else None
    doctor_errors: dict[str, str] = {}
    doctors: list[str] = []
    if merged:
        reports.extend(merged["reports"])
        candidates += merged["candidates"]
        doctor_errors = merged["doctor_errors"]
        completeness.update(merged["completeness"])
        doctors = merged["doctors"]

    page, meta = build(args, reports, candidates, doctor_errors, completeness, doctors)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"{out}: {meta['grade']} ({meta['score']}) — {meta['findings_total']} finding(s)"
          + (f", {meta['findings_out_of_scope']} outside {', '.join(meta['roots'])}"
             if meta["findings_out_of_scope"] else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
