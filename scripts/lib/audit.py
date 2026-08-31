#!/usr/bin/env python3
"""`ts audit` — where the tokens went.

Answers one question the built-in cost display cannot: of everything you were
billed for, how much was NEW work and how much was re-reading what was already
there? That ratio is the amplification factor, and it is the number every
recommendation in `ts advise` is ranked by.

Usage: audit.py [--project GLOB] [--limit N] [--sessions] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report as R  # noqa: E402
from transcripts import (  # noqa: E402
    PRICE, Fleet, command_program, human, pct, quantile, tokenizer_name,
    transcript_dir,
)


def _billed_block(fleet: Fleet) -> str:
    b = fleet.billed()
    total = sum(b.values())
    cost = fleet.cost_units()
    out = [R.heading("BILLED  (exact — read from the provider's usage records)")]
    rows = []
    for k in ("cache_read", "cache_write", "input", "output"):
        rows.append([
            k.replace("_", " "),
            "{:,}".format(b[k]),
            "%5.1f%%" % pct(b[k], total),
            "%.2fx" % PRICE[k],
            "{:,.0f}".format(b[k] * PRICE[k]),
            "%5.1f%%" % pct(b[k] * PRICE[k], cost),
        ])
    rows.append(["TOTAL", "{:,}".format(total), "", "", "{:,.0f}".format(cost), ""])
    out.append(R.table(
        rows,
        ["kind", "tokens", "share", "price", "cost units", "of spend"],
        "lrrrrr"))
    out.append("")
    out.append("  " + R.dim(
        "cost units = tokens x price relative to one input token. This is the "
        "column\n  that tracks money; the raw token column does not."))
    return "\n".join(out)


def _content_block(fleet: Fleet) -> str:
    buckets = fleet.buckets()
    total = sum(buckets.values())
    out = [R.heading(
        "CONTENT  (%s — the text that accumulated)" % tokenizer_name())]
    rows = []
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        if v <= 0:
            continue
        rows.append([k, "{:,}".format(v), "%5.1f%%" % pct(v, total),
                     R.bar(v / total if total else 0, 18)])
    rows.append(["TOTAL DISTINCT CONTENT", "{:,}".format(total), "", ""])
    out.append(R.table(rows, ["component", "tokens", "share", ""], "lrrl"))
    return "\n".join(out)


def _amplification_block(fleet: Fleet) -> str:
    b = fleet.billed()
    content = fleet.content_total()
    amp = fleet.amplification()
    turns = fleet.turns()
    out = [R.heading("AMPLIFICATION  (the number that matters)")]
    out.append(R.kv("distinct content created", "{:,} tokens".format(content)))
    out.append(R.kv("billed as cache reads", "{:,} tokens".format(b["cache_read"])))
    out.append("")
    verdict = (R.red if amp >= 200 else R.yellow if amp >= 60 else R.green)
    out.append("  " + R.bold("every token of content was billed ")
               + verdict("%.0f times" % amp) + R.bold(" on average"))
    out.append("")
    out.append("  " + R.dim(
        "A token costs (its size) x (the turns that re-read it). At %.0fx, "
        "shrinking\n  content matters far less than shortening the window it "
        "lives in." % amp))
    if turns:
        out.append("")
        out.append(R.kv("assistant turns", "{:,}".format(turns)))
        out.append(R.kv("avg context per turn",
                        "{:,.0f} tokens".format(b["cache_read"] / turns)
                        if turns else "-"))
        out.append(R.kv("avg output per turn",
                        "{:,.0f} tokens".format(b["output"] / turns)))
    return "\n".join(out)


def _shape_block(fleet: Fleet) -> str:
    subs = fleet.substantive()
    if not subs:
        return ""
    turns = sorted(s.turns for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)
    peaks = sorted(s.peak for s in subs)
    out = [R.heading("SESSION SHAPE")]
    out.append(R.table([
        ["turns per session", "{:,}".format(quantile(turns, 0.5)),
         "{:,}".format(quantile(turns, 0.9)), "{:,}".format(turns[-1])],
        ["peak context", "{:,}".format(quantile(peaks, 0.5)),
         "{:,}".format(quantile(peaks, 0.9)), "{:,}".format(peaks[-1])],
        ["fixed preamble", "{:,}".format(quantile(floors, 0.5)) if floors else "-",
         "{:,}".format(quantile(floors, 0.9)) if floors else "-",
         "{:,}".format(floors[-1]) if floors else "-"],
    ], ["", "median", "p90", "max"], "lrrr"))
    subs = fleet.subagents()
    if subs:
        out.append("")
        out.append(R.kv("subagent transcripts", "{:,}".format(len(subs)),
                        "%.1f%% of spend, %s turns"
                        % (fleet.subagent_cost_share(),
                           "{:,}".format(sum(x.turns for x in subs)))))
        out.append("  " + R.dim("counted in every total above; excluded from "
                                "the session-shape rows, which are about the "
                                "main thread"))
    if floors:
        med_floor = quantile(floors, 0.5)
        total_reads = fleet.billed()["cache_read"]
        preamble_cost = med_floor * fleet.turns()
        out.append("")
        out.append(R.kv(
            "preamble re-read every turn",
            "{:,} tokens".format(preamble_cost),
            "= %.1f%% of all your cache reads" % pct(preamble_cost, total_reads)))
    return "\n".join(out)


def _tools_block(fleet: Fleet) -> str:
    out_tok = fleet.merged("tool_out")
    in_tok = fleet.merged("tool_in")
    calls = fleet.merged("tool_calls")
    names = set(out_tok) | set(in_tok)
    grand = sum(out_tok.values()) + sum(in_tok.values())
    rows = []
    for n in sorted(names, key=lambda k: -(out_tok.get(k, 0) + in_tok.get(k, 0))):
        tot = out_tok.get(n, 0) + in_tok.get(n, 0)
        if tot < max(500, grand * 0.002):
            continue
        c = calls.get(n, 0)
        rows.append([
            n[:38], "{:,}".format(c),
            "{:,}".format(in_tok.get(n, 0)), "{:,}".format(out_tok.get(n, 0)),
            "{:,}".format(tot), "%5.1f%%" % pct(tot, grand),
            "{:,.0f}".format(tot / c) if c else "-",
        ])
    out = [R.heading("BY TOOL  (call inputs and results are both billed)")]
    out.append(R.table(
        rows[:14],
        ["tool", "calls", "call in", "results", "total", "share", "per call"],
        "lrrrrrr"))
    return "\n".join(out)


def _bash_block(fleet: Fleet) -> str:
    outs = sorted(fleet.bash_out())
    cmds = fleet.bash_cmds()
    if not outs:
        return ""
    out = [R.heading("BASH DETAIL  (usually the largest single surface)")]
    cmd_tok = sum(c[0] for c in cmds)
    out_tok = sum(outs)
    out.append(R.kv("calls", "{:,}".format(len(outs))))
    out.append(R.kv("command text billed", "{:,} tokens".format(cmd_tok),
                    "the commands themselves"))
    out.append(R.kv("output billed", "{:,} tokens".format(out_tok)))
    out.append(R.kv("combined", "{:,} tokens".format(cmd_tok + out_tok),
                    "%.1f%% of all content" % pct(cmd_tok + out_tok,
                                                  fleet.content_total())))
    out.append("")
    out.append(R.table([[
        "output size",
        "{:,}".format(quantile(outs, 0.5)),
        "{:,}".format(quantile(outs, 0.75)),
        "{:,}".format(quantile(outs, 0.9)),
        "{:,}".format(quantile(outs, 0.99)),
        "{:,}".format(outs[-1]),
    ]], ["", "median", "p75", "p90", "p99", "max"], "lrrrrr"))

    # Concentration decides whether a compressor could ever pay off: a few fat
    # outputs are compressible, a long flat tail of small ones is not.
    total = sum(outs) or 1
    top = sorted(outs, reverse=True)
    conc = []
    for n in (10, 50, 100):
        if n < len(top):
            conc.append([
                "top %d calls" % n,
                "%.1f%%" % pct(n, len(top)),
                "%.1f%% of Bash output" % pct(sum(top[:n]), total)])
    if conc:
        out.append("")
        out.append(R.table(conc, ["concentration", "of calls", ""], "lrl"))

    fam = {}
    for n, c in cmds:
        p = command_program(c)
        cur = fam.get(p) or [0, 0]
        fam[p] = [cur[0] + 1, cur[1] + n]
    rows = [[p, "{:,}".format(v[0]), "{:,}".format(v[1]), "{:,.0f}".format(v[1] / v[0])]
            for p, v in sorted(fam.items(), key=lambda kv: -kv[1][1])[:8]]
    out.append("")
    out.append(R.table(rows, ["command", "calls", "cmd tokens", "avg"], "lrrr"))
    return "\n".join(out)


def _sessions_block(fleet: Fleet, limit: int = 12) -> str:
    subs = sorted(fleet.substantive(), key=lambda s: -s.cost_units)[:limit]
    rows = []
    for s in subs:
        rows.append([
            s.project[:26], s.session_id[:8], "{:,}".format(s.turns),
            "{:,}".format(s.floor), "{:,}".format(s.peak),
            "%.0fx" % (s.peak / s.floor) if s.floor else "-",
            human(s.cost_units),
        ])
    out = [R.heading("HEAVIEST SESSIONS  (by cost units)")]
    out.append(R.table(
        rows,
        ["project", "id", "turns", "preamble", "peak ctx", "growth", "cost"],
        "llrrrrr"))
    return "\n".join(out)


def _as_json(fleet: Fleet) -> str:
    subs = fleet.substantive()
    turns = sorted(s.turns for s in subs)
    peaks = sorted(s.peak for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)
    outs = sorted(fleet.bash_out())
    return json.dumps({
        "tokenizer": tokenizer_name(),
        "sessions": len(fleet.main_sessions()),
        "subagent_transcripts": len(fleet.subagents()),
        "skipped_records": fleet.skipped(),
        "turns": fleet.turns(),
        "billed": fleet.billed(),
        "cost_units": round(fleet.cost_units(), 1),
        "content": fleet.buckets(),
        "content_total": fleet.content_total(),
        "amplification": round(fleet.amplification(), 1),
        "session_shape": {
            "turns_median": quantile(turns, 0.5),
            "turns_p90": quantile(turns, 0.9),
            "peak_median": quantile(peaks, 0.5),
            "peak_max": peaks[-1] if peaks else 0,
            "preamble_median": quantile(floors, 0.5) if floors else 0,
        },
        "bash": {
            "calls": len(outs),
            "out_median": quantile(outs, 0.5),
            "out_p90": quantile(outs, 0.9),
            "out_max": outs[-1] if outs else 0,
            "cmd_tokens": sum(c[0] for c in fleet.bash_cmds()),
            "out_tokens": sum(outs),
        },
        "by_tool": {
            "results": fleet.merged("tool_out"),
            "call_inputs": fleet.merged("tool_in"),
            "calls": fleet.merged("tool_calls"),
        },
    }, indent=1, sort_keys=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts audit", description=__doc__)
    ap.add_argument("--project", help="only this project directory (glob). Use the --project=NAME form: Claude Code project dirs begin with '-', which argparse reads as a flag")
    ap.add_argument("--limit", type=int, help="only the N most recent sessions")
    ap.add_argument("--sessions", action="store_true", help="per-session table")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", help="transcript root (default ~/.claude/projects)")
    args = ap.parse_args(argv)

    fleet = Fleet.load(root=args.root, project=args.project, limit=args.limit)
    if not fleet.sessions:
        # transcript_dir() resolves TS_TRANSCRIPT_DIR; re-deriving the default
        # here meant the one message whose entire job is to say where we looked
        # named somewhere we did not look, and then advised setting the variable
        # that was already set and already honoured.
        where = args.root or transcript_dir()
        sys.stderr.write("no transcripts found under %s\n" % where)
        if not args.root and not os.environ.get("TS_TRANSCRIPT_DIR"):
            sys.stderr.write(
                "  set TS_TRANSCRIPT_DIR or pass --root if yours live "
                "elsewhere\n")
        elif not args.root:
            sys.stderr.write(
                "  that is TS_TRANSCRIPT_DIR, which is set and was used\n")
        return 1
    if not fleet.turns():
        sys.stderr.write(
            "found %d transcript(s) but none carry usage records — nothing to "
            "account for\n" % len(fleet.sessions))
        return 1

    if args.json:
        print(_as_json(fleet))
        return 0

    skipped = fleet.skipped()
    if skipped:
        sys.stderr.write("note: skipped %d unreadable record(s); everything "
                         "else was accounted for\n" % skipped)
    subn = len(fleet.subagents())
    print(R.bold("\n  %d sessions%s  |  %s  |  %s"
                 % (len(fleet.main_sessions()),
                    " + %d subagent transcripts" % subn if subn else "",
                    "{:,} turns".format(fleet.turns()), tokenizer_name())))
    print(_billed_block(fleet))
    print(_content_block(fleet))
    print(_amplification_block(fleet))
    shape = _shape_block(fleet)
    if shape:
        print(shape)
    print(_tools_block(fleet))
    bash = _bash_block(fleet)
    if bash:
        print(bash)
    if args.sessions:
        print(_sessions_block(fleet))
    print("\n  " + R.dim("next: ") + "ts advise" + R.dim("  — what to do about it"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
