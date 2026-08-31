#!/usr/bin/env python3
"""`ts advise` — run every detector against this machine and rank what fires.

Usage: cmd_advise.py [--project GLOB] [--limit N] [--all] [--json] [--only ID]
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import advise  # noqa: E402
import machine  # noqa: E402
import report as R  # noqa: E402
from transcripts import (Fleet, positive_int, project_filter,  # noqa: E402
                         tokenizer_name, transcript_dir,
                         unmatched_project_note)


def _render(findings, fleet, show_all: bool, blocked=(), only=None) -> None:
    if not findings and not blocked:
        if only:
            # Do not claim every detector read the numbers when only some ran.
            print("\n  " + R.green("nothing fired")
                  + " among the %d detector(s) you selected: %s"
                  % (len(only), ", ".join(only)))
            print("  " + R.dim("the rest of the catalogue was not reported — "
                               "drop --only to see it"))
        else:
            print("\n  " + R.green("nothing fired.")
                  + " Every detector read your numbers and stayed quiet.")
            print("  " + R.dim("that is a real result, not an empty report — "
                               "see `ts advise --all` for the full catalogue"))
        return

    # "largest single item" described the top of the list, and stopped being
    # true once a located-cost finding could outrank every saving. It names
    # the quantity now, so it is not read as a description of what follows.
    claimed = [f.saving_pct for f in findings if f.saving_pct is not None]
    head_note = ("   largest single saving: ~%.0f%% of spend" % max(claimed)
                 if claimed else "   no saving claimed by any finding")
    print("\n  " + R.bold("%d of %d detectors fired" % (len(findings), len(advise.DETECTORS)))
          + R.dim(head_note))
    excluded = [f for f in findings if f.id in advise.UNION_EXCLUDED]
    union, groups = advise.union_lower_bound(findings)

    merged = [g for g in groups if len(g) > 1]
    note = ("; %s counted once" % ", ".join("+".join(g) for g in merged)
            if merged else "")

    # Tied to the grouping, not to a count of findings. Guarding on
    # `len(findings) > 1` said "findings overlap" for any two findings, so a
    # report of two ADDITIVE findings told the reader not to add them and then
    # printed their sum on the next line. The predicate is whether anything
    # actually overlaps: a group with more than one member, or an excluded
    # finding, which overlaps everything by definition.
    # An excluded finding overlaps the others -- but only if there ARE others.
    # `merged or excluded` alone reintroduced #28: a lone session-length was
    # told not to add its single percentage to itself.
    if merged or (excluded and len(findings) > 1):
        print("  " + R.dim("findings overlap — do not add these percentages "
                           "together"))

    # Saying only what NOT to compute left the reader with eight numbers, a
    # warning, and no total. The floor counts each overlapping group once, so
    # it is what the findings are worth together AT LEAST.
    addable = [f for f in findings if f.id not in advise.UNION_EXCLUDED]
    if union is not None and excluded:
        # The exclusion is named in the SAME sentence as the number, not on the
        # line below it. "taken together they are worth at least 1.3%" sat
        # directly above a 15.3% finding and reads as the total opportunity --
        # and the summary is the part written to be read alone.
        print("  " + R.dim("the addable finding%s %s worth "
                           % ("" if len(addable) == 1 else "s",
                              "is" if len(addable) == 1 else "are"))
              + R.bold("at least %.1f%%" % union)
              + R.dim(" of spend, excluding %s, which multiplies %s rather "
                      "than adding%s"
                      % (", ".join("`%s` (%.1f%%)" % (f.id, f.saving_pct)
                                   for f in excluded),
                         "it" if len(addable) == 1 else "them", note)))
    elif union is not None and len(addable) > 1:
        # With a single addable finding and nothing excluded, this said "taken
        # together they are worth at least 1.3%" with nothing to take it
        # together with -- and the number restates `largest single item` on the
        # line directly above.
        print("  " + R.dim("taken together they are worth ")
              + R.bold("at least %.1f%%" % union)
              + R.dim(" of spend%s" % note))

    # Only when the union sentence above did not already name it. Left out of
    # the sum rather than folded in: it overlaps every other finding, so
    # grouping it honestly would collapse the union to this one number and
    # report the largest finding as the total.
    if union is None:
        for f in excluded:
            print("  " + R.dim("`%s` (%.1f%%) is the only finding — it "
                               "multiplies whatever else you fix rather than "
                               "adding to it" % (f.id, f.ranked_pct)))

    # Two blocks, because a located share and a saveable share are not the
    # same quantity and ordering them against each other put a MED above two
    # HIGHs. Severity is comparable within a block and is not compared across.
    shown_noclaim_header = False
    for i, f in enumerate(findings, 1):
        if not f.claims_saving and not shown_noclaim_header:
            shown_noclaim_header = True
            print("\n" + R.rule())
            print("  " + R.bold("cost located, no saving claimed")
                  + R.dim("   these are not ranked against the savings above"))
        print("\n" + R.rule())
        head = "  %s  %s  %s" % (R.severity(f.severity), R.bold(f.title),
                                 R.dim("[" + f.id + "]"))
        print(head)
        if f.saving_pct is not None:
            meta = "        %s   worth ~%.1f%% of spend" % (
                R.confidence(f.confidence), f.saving_pct)
        elif f.locates_pct:
            # The number shown is the number that ranked it.
            meta = "        %s   locates ~%.1f%% of spend; no saving claimed" % (
                R.confidence(f.confidence), f.locates_pct)
        else:
            meta = "        %s   no saving claimed" % R.confidence(f.confidence)
        margin = f.gate.margin if f.gate else None
        if margin is not None:
            meta += R.dim("   fires at %.1fx its threshold" % margin)
        print(meta)
        if f.assumption:
            print("        " + R.dim("assuming: " + f.assumption))
        # The gate is the most opinionated part of a detector. Showing what it
        # required, and by how much this machine cleared it, is what separates
        # "you are unusual here" from "everybody trips this".
        if f.gate:
            for c in f.gate.conditions:
                print("        " + R.dim("gate: " + c.describe()))
        print()
        for line in f.evidence:
            print("    " + R.dim("·") + " " + line)
        print()
        for a in f.actions:
            # wrap actions at a readable width without a dependency
            words, cur = a.split(), ""
            out = []
            for w in words:
                if len(cur) + len(w) + 1 > 70:
                    out.append(cur)
                    cur = w
                else:
                    cur = (cur + " " + w).strip()
            out.append(cur)
            print("    " + R.cyan("->") + " " + out[0])
            for extra in out[1:]:
                print("       " + extra)
        if f.fix:
            print("\n    " + R.magenta("automatable: ")
                  + "ts fixes show %s" % f.fix)

    print("\n" + R.rule())
    # Printed whether or not --all was asked for: "could not check" is news,
    # and burying it behind a flag is how an unreadable config looked exactly
    # like a clean bill of health.
    if blocked:
        print("\n  " + R.bold("could not be evaluated"))
        for u in blocked:
            print("    " + R.yellow("??") + "   %-18s %s" % (u.id, u.reason))
    if show_all:
        fired = {f.id for f in findings} | {u.id for u in blocked}
        rest = [(k, v) for k, v in advise.CATALOGUE.items() if k not in fired]
        if rest:
            print("\n  " + R.bold("did not fire on this machine"))
            for k, v in rest:
                print("    " + R.dim("--   %-18s %s" % (k, v)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts advise", description=__doc__)
    ap.add_argument("--project", type=project_filter,
                    help="only this project directory (glob). Use the --project=NAME form: Claude Code project dirs begin with '-', which argparse reads as a flag")
    ap.add_argument("--limit", type=positive_int,
                    help="only the N most recent sessions")
    ap.add_argument("--root", help="transcript root")
    ap.add_argument("--all", action="store_true",
                    help="also list the detectors that stayed silent")
    ap.add_argument("--only", help="restrict the report to these detector ids "
                                   "(comma-separated). Unknown ids are an error")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    fleet = Fleet.load(root=args.root, project=args.project, limit=args.limit)
    if not fleet.turns():
        # Was: "`ts audit` explains where it looked" -- which sent the
        # reader to a message that named the wrong directory. Two hops, both
        # confident, wrong destination. Say it here instead.
        where = args.root or transcript_dir()
        sys.stderr.write("no usable transcripts found under %s\n" % where)
        sys.stderr.write(unmatched_project_note(where, args.project))
        return 1
    mach = machine.collect()

    findings, blocked = advise.evaluate(fleet, mach)

    # `silent` is computed BEFORE the filter, or a detector that fired and was
    # filtered out would be reported as having stayed quiet -- the same
    # not-measured-versus-measured-zero confusion this flag already had.
    fired_ids = {f.id for f in findings}
    blocked_ids = {u.id for u in blocked}
    silent_ids = [k for k in advise.CATALOGUE
                  if k not in fired_ids and k not in blocked_ids]

    only = None
    if args.only:
        only = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in only if x not in advise.CATALOGUE]
        if unknown:
            # An unmatched filter meant nothing ran and the report said
            # "nothing fired ... that is a real result, not an empty report" --
            # a sentence written to prevent exactly this misreading, printed
            # unconditionally, turning a typo into a confident all-clear. And
            # --json listed every detector as silent, so a CI check or a
            # collected profile recorded negative results never measured.
            for bad_id in unknown:
                near = difflib.get_close_matches(bad_id, advise.CATALOGUE, 1, 0.6)
                sys.stderr.write("ts advise: unknown detector %r%s\n" % (
                    bad_id, "; did you mean %r?" % near[0] if near else ""))
            sys.stderr.write("valid ids: %s\n" % ", ".join(sorted(advise.CATALOGUE)))
            return 2
        findings = [f for f in findings if f.id in only]
        blocked = [u for u in blocked if u.id in only]
        silent_ids = [k for k in silent_ids if k in only]

    union, groups = advise.union_lower_bound(findings)
    if args.json:
        print(json.dumps({
            "tokenizer": tokenizer_name(),
            "sessions": len(fleet.main_sessions()),
            "subagent_transcripts": len(fleet.subagents()),
            "turns": fleet.turns(),
            "amplification": round(fleet.amplification(), 1),
            # A floor, not an estimate: each overlapping group counted once,
            # so the real union is at least this. `excluded` names what was
            # left out and why, so a consumer is not silently handed a total
            # that quietly omits the largest lever.
            "union_lower_bound_pct": (None if union is None
                                      else round(union, 2)),
            "union_groups": groups,
            "union_excluded": [f.id for f in findings
                               if f.id in advise.UNION_EXCLUDED],
            "findings": [{
                "id": f.id, "title": f.title, "severity": f.severity,
                "confidence": f.confidence,
                # Which block the finding is rendered in. Derivable from
                # `saving_pct is null`, but the array is flat and carries both
                # blocks in order, so a consumer rendering it as given gets a
                # non-monotone severity column with nothing saying why. The
                # human reader is told in a printed sentence; this is the same
                # sentence for the machine.
                #
                # Per-finding rather than a top-level id list beside
                # `union_excluded`: those two are orthogonal, and adjacency
                # would imply otherwise. `session-length` is union_excluded
                # AND claims a saving, so it sits in the first block.
                "claims_saving": f.claims_saving,
                "saving_pct": (None if f.saving_pct is None
                               else round(f.saving_pct, 2)),
                "locates_pct": (None if f.locates_pct is None
                                else round(f.locates_pct, 2)),
                "assumption": f.assumption,
                "attribution": None if not f.attribution else {
                    "total": f.attribution["total"],
                    "residual": f.attribution["residual"],
                    "unattributed": f.attribution["unattributed"],
                    "parts": [{"tokens": v, "label": l, "source": h}
                              for v, l, h in f.attribution["parts"]],
                },
                "gate": None if not f.gate else {
                    "mode": f.gate.mode,
                    "margin": (None if f.gate.margin is None
                               else round(f.gate.margin, 2)),
                    "conditions": [{
                        "name": c.name,
                        "value": (c.value if isinstance(c.value, bool)
                                  else round(float(c.value), 2)),
                        "bound": c.bound,
                        "mode": c.mode,
                        "unit": c.unit,
                        "ratio": None if c.ratio is None else round(c.ratio, 2),
                    } for c in f.gate.conditions],
                },
                "evidence": f.evidence, "actions": f.actions, "fix": f.fix,
            } for f in findings],
            # `silent` means checked and quiet. A detector that could not run
            # is listed apart, with why -- being unable to check is not the
            # same as having checked and found nothing.
            "silent": silent_ids,
            "unevaluated": [{"id": u.id, "reason": u.reason} for u in blocked],
        }, indent=1))
        return 0

    skipped = fleet.skipped()
    if skipped:
        sys.stderr.write("note: skipped %d unreadable record(s); everything "
                         "else was accounted for\n" % skipped)
    subn = len(fleet.subagents())
    print(R.bold("\n  %d sessions%s  |  %s turns  |  %.0fx amplification  |  %s"
                 % (len(fleet.main_sessions()),
                    " + %d subagent transcripts" % subn if subn else "",
                    "{:,}".format(fleet.turns()),
                    fleet.amplification(), tokenizer_name())))
    _render(findings, fleet, args.all, blocked, only)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
