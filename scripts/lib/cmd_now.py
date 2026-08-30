#!/usr/bin/env python3
"""`ts now` — what the session you are in is costing, while you can still act.

`ts audit` is a post-mortem. By the time it tells you a session ran 1,126 turns
to a 716K context, the money is spent. This answers the same question early
enough to matter, and the one question a post-mortem cannot: *would clearing
right now pay for itself?*

The break-even is arithmetic, not a rule of thumb. Carrying a context of C
tokens costs C*r on every future turn. Clearing means paying the preamble F
once as a cache write (F*w) and then only F*r per turn. Over N further turns:

    continue  =  r * (N*C + g*N*(N+1)/2)
    clear     =  w*F  +  r * (N*F + g*N*(N+1)/2)

The growth term g is identical on both sides — new work costs the same either
way — so it cancels, and the whole comparison collapses to:

    N* = (w * F) / (r * (C - F))

With Anthropic's multipliers (w=1.25, r=0.10) that is N* = 12.5F / (C-F). A
session carrying ten times its preamble breaks even in under two turns, which
is a far more aggressive answer than most people's instinct — and it is why
this command exists as a live signal rather than a chapter in a report.

The one thing the arithmetic cannot know is whether the carried context is
still *needed*. That judgement is the user's, and the output says so every
time rather than pretending to a certainty it does not have.

Usage: cmd_now.py [--session PATH] [--statusline] [--window N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report as R  # noqa: E402
from transcripts import PRICE, Session, find_transcripts, human, parse, pct  # noqa: E402

# Recent turns used for the growth rate. Short enough to react to a change of
# activity, long enough that one big file read does not dominate.
DEFAULT_WINDOW = 20


def newest_transcript() -> str:
    paths = find_transcripts()
    if not paths:
        return ""
    return max(paths, key=lambda p: os.path.getmtime(p))


def growth_per_turn(sess: Session, window: int = DEFAULT_WINDOW) -> float:
    """Median context added per turn over the recent window.

    Median rather than mean: a single 30K file read inside the window would
    otherwise triple the reported rate and produce a projection nobody should
    believe.
    """
    sizes = sess.ctx_sizes
    if len(sizes) < 3:
        return 0.0
    recent = sizes[-(window + 1):]
    deltas = [b - a for a, b in zip(recent, recent[1:]) if b >= a]
    if not deltas:
        return 0.0
    return float(statistics.median(deltas))


def breakeven_turns(context: int, floor: int) -> float:
    """Turns after which clearing now costs less than carrying on.

    Returns 0.0 when the context is at or below the preamble (nothing to gain)
    and float('inf') when the arithmetic does not resolve.
    """
    if floor <= 0 or context <= floor:
        return float("inf")
    return (PRICE["cache_write"] * floor) / (PRICE["cache_read"] * (context - floor))


def analyse(sess: Session, window: int = DEFAULT_WINDOW) -> dict:
    ctx = sess.ctx_sizes[-1] if sess.ctx_sizes else 0
    floor = sess.floor
    g = growth_per_turn(sess, window)
    be = breakeven_turns(ctx, floor)
    reads = sess.billed["cache_read"]
    return {
        "project": sess.project,
        "session": sess.session_id,
        "turns": sess.turns,
        "context": ctx,
        "floor": floor,
        "ratio": (ctx / floor) if floor else 0.0,
        "growth_per_turn": g,
        "cost_units": sess.cost_units,
        "cache_read": reads,
        "reread_share": pct(reads * PRICE["cache_read"], sess.cost_units),
        "breakeven_turns": be,
        # What the next ten turns cost if nothing changes. Concrete beats a rate.
        "next_10_cost": PRICE["cache_read"] * (10 * ctx + g * 55),
    }


def _band(a: dict):
    """Colour by how far past its preamble the session has drifted."""
    r = a["ratio"]
    if r >= 8:
        return R.red, "carrying %.0fx its preamble" % r
    if r >= 4:
        return R.yellow, "carrying %.0fx its preamble" % r
    return R.green, "close to its preamble"


def render(a: dict) -> str:
    colour, note = _band(a)
    out = [R.heading("THIS SESSION  %s / %s" % (a["project"], a["session"][:8]))]
    out.append(R.kv("turn", "{:,}".format(a["turns"])))
    out.append(R.kv("context now", colour("{:,} tokens".format(a["context"])), note))
    out.append(R.kv("preamble", "{:,} tokens".format(a["floor"])))
    out.append(R.kv("growth", "+{:,.0f} tokens/turn".format(a["growth_per_turn"]),
                    "median of the last %d turns" % DEFAULT_WINDOW))
    out.append("")
    out.append(R.kv("billed so far", "{:,} cache reads".format(a["cache_read"]),
                    "{:s} cost units".format(human(a["cost_units"]))))
    out.append(R.kv("of that", "%.0f%% was re-reading" % a["reread_share"],
                    "context already sent on an earlier turn"))
    out.append(R.kv("next 10 turns", "~%s cost units" % human(a["next_10_cost"]),
                    "if nothing changes"))
    out.append("")

    be = a["breakeven_turns"]
    if be == float("inf"):
        out.append("  " + R.green("nothing to gain by clearing")
                   + R.dim(" — this session is still at its preamble"))
    else:
        verdict = R.red if be <= 3 else R.yellow if be <= 15 else R.dim
        out.append("  " + R.bold("clearing now breaks even after ")
                   + verdict("%.1f turns" % be))
        out.append("  " + R.dim(
            "carrying %s costs %.1fx more per turn than a fresh %s preamble"
            % (human(a["context"]), a["context"] / a["floor"] if a["floor"] else 0,
               human(a["floor"]))))
    out.append("")
    out.append("  " + R.dim(
        "This is arithmetic on size, not relevance. If the context you are\n"
        "  carrying is still needed for what comes next, keep it — that call\n"
        "  is yours and the numbers above cannot make it."))
    return "\n".join(out)


def render_statusline(a: dict) -> str:
    """One compact line for statusline.sh. No newline, minimal width."""
    ctx = human(a["context"])
    g = "+%s/t" % human(a["growth_per_turn"])
    be = a["breakeven_turns"]
    colour, _ = _band(a)
    if be == float("inf"):
        tail = ""
    elif be <= 3:
        tail = " " + R.red("clear>%.0ft" % max(1, round(be)))
    elif be <= 15:
        tail = " " + R.yellow("clear>%.0ft" % be)
    else:
        tail = ""
    return "%s %s%s" % (colour(ctx), R.dim(g), tail)


def _session_from_stdin_json():
    """Claude Code hands a statusline command its session JSON on stdin, which
    carries `transcript_path`. Using it beats guessing the newest file, which
    is wrong the moment two sessions run at once.

    Only ever called in --statusline mode. An earlier version consulted stdin
    on every invocation and hung forever the first time it ran under a shell
    that left stdin open as an idle pipe: isatty() is False there, and a bare
    read() on a pipe nobody writes to never returns. A tool that can hang is
    worse than one that guesses, so this now waits a beat and gives up.
    """
    try:
        if sys.stdin.isatty():
            return None
        import select
        # A caller that pipes us JSON has already written it. Half a second is
        # generous for that and imperceptible in a statusline.
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        if not ready:
            return None
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        data = json.loads(raw)
    except Exception:
        return None
    path = data.get("transcript_path") if isinstance(data, dict) else None
    return path if path and os.path.isfile(path) else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts now", description=__doc__)
    ap.add_argument("--session", help="transcript path (default: newest, or "
                                      "the one named on stdin)")
    ap.add_argument("--statusline", action="store_true",
                    help="one compact line; reads session JSON on stdin")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="turns used for the growth rate")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = (args.session
            or (_session_from_stdin_json() if args.statusline else None)
            or newest_transcript())
    if not path or not os.path.isfile(path):
        if args.statusline:
            return 0          # a statusline must never print an error
        sys.stderr.write("no transcript found; pass --session PATH\n")
        return 1

    # Content accounting is not used by anything below, and this runs in a
    # statusline. Read the usage records only.
    sess = parse(path, usage_only=True)
    if not sess.turns:
        if args.statusline:
            return 0
        sys.stderr.write("%s carries no usage records yet\n" % path)
        return 1

    a = analyse(sess, args.window)
    if args.json:
        out = dict(a)
        if out["breakeven_turns"] == float("inf"):
            out["breakeven_turns"] = None
        print(json.dumps(out, indent=1))
    elif args.statusline:
        sys.stdout.write(render_statusline(a))
    else:
        print(render(a))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
