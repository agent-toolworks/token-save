#!/usr/bin/env python3
"""`ts advise` — run every detector against this machine and rank what fires.

Usage: cmd_advise.py [--project GLOB] [--limit N] [--all] [--json] [--only ID]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import advise  # noqa: E402
import machine  # noqa: E402
import report as R  # noqa: E402
from transcripts import Fleet, tokenizer_name  # noqa: E402


def _render(findings, fleet, show_all: bool) -> None:
    if not findings:
        print("\n  " + R.green("nothing fired.")
              + " Every detector read your numbers and stayed quiet.")
        print("  " + R.dim("that is a real result, not an empty report — see "
                           "`ts advise --all` for the full catalogue"))
        return

    top = max(f.saving_pct for f in findings)
    print("\n  " + R.bold("%d of %d detectors fired" % (len(findings), len(advise.DETECTORS)))
          + R.dim("   largest single item: ~%.0f%% of spend" % top))
    print("  " + R.dim("findings overlap — do not add these percentages together"))

    for i, f in enumerate(findings, 1):
        print("\n" + R.rule())
        head = "  %s  %s  %s" % (R.severity(f.severity), R.bold(f.title),
                                 R.dim("[" + f.id + "]"))
        print(head)
        meta = "        %s   worth ~%.1f%% of spend" % (
            R.confidence(f.confidence), f.saving_pct)
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
    if show_all:
        fired = {f.id for f in findings}
        rest = [(k, v) for k, v in advise.CATALOGUE.items() if k not in fired]
        if rest:
            print("\n  " + R.bold("did not fire on this machine"))
            for k, v in rest:
                print("    " + R.dim("--   %-18s %s" % (k, v)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts advise", description=__doc__)
    ap.add_argument("--project", help="only this project directory (glob). Use the --project=NAME form: Claude Code project dirs begin with '-', which argparse reads as a flag")
    ap.add_argument("--limit", type=int, help="only the N most recent sessions")
    ap.add_argument("--root", help="transcript root")
    ap.add_argument("--all", action="store_true",
                    help="also list the detectors that stayed silent")
    ap.add_argument("--only", help="run a single detector by id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    fleet = Fleet.load(root=args.root, project=args.project, limit=args.limit)
    if not fleet.turns():
        sys.stderr.write(
            "no usable transcripts found — `ts audit` explains where it looked\n")
        return 1
    mach = machine.collect()

    findings = advise.run(fleet, mach)
    if args.only:
        findings = [f for f in findings if f.id == args.only]

    if args.json:
        print(json.dumps({
            "tokenizer": tokenizer_name(),
            "sessions": len(fleet.main_sessions()),
            "subagent_transcripts": len(fleet.subagents()),
            "turns": fleet.turns(),
            "amplification": round(fleet.amplification(), 1),
            "findings": [{
                "id": f.id, "title": f.title, "severity": f.severity,
                "confidence": f.confidence,
                "saving_pct": round(f.saving_pct, 2),
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
            "silent": [k for k in advise.CATALOGUE
                       if k not in {f.id for f in findings}],
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
    _render(findings, fleet, args.all)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
