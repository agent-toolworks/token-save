#!/usr/bin/env python3
"""`ts doctor` — what is installed and configured here, and what it costs.

`ts audit` reads history; this reads the present. Run it when advice surprises
you: it shows the raw facts a detector fired on.

Usage: cmd_doctor.py [--memory] [--mcp] [--hooks] [--tools] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import machine  # noqa: E402
import report as R  # noqa: E402
from transcripts import (Fleet, find_sessions,  # noqa: E402
                         find_transcripts, quantile, tokenizer_name,
                         transcript_dir)

HOME = os.path.expanduser("~")


def _short(p: str) -> str:
    return p.replace(HOME, "~") if p else "-"


def _environment() -> str:
    out = [R.heading("ENVIRONMENT")]
    out.append(R.kv("python", "%d.%d.%d" % sys.version_info[:3]))
    out.append(R.kv("tokenizer", tokenizer_name(),
                    "" if "tiktoken" in tokenizer_name()
                    else "pip install tiktoken for exact counts"))
    # Not CLAUDE_HOME + "/projects": the transcript root is TS_TRANSCRIPT_DIR,
    # which moves independently of CLAUDE_CONFIG_DIR. `doctor` exists to say
    # which paths are in effect, so naming one that is not is the worst place
    # for this mistake.
    out.append(R.kv("transcripts", _short(transcript_dir())))
    # Both populations, named. "transcripts found" was one number covering
    # two things, which is the conflation that produced #8, #16 and #22.
    all_paths = find_transcripts()
    n = len(all_paths)
    n_sess = len(find_sessions())
    out.append(R.kv("transcripts found", "{:,}".format(n),
                    ("%d session(s) + %d subagent transcript(s)"
                     % (n_sess, n - n_sess)) if n else _nothing_found_hint()))
    return "\n".join(out)


def _nothing_found_hint() -> str:
    """The last line carrying the #20 defect: it advised setting the variable
    that was already set and already used. Same shape as the original bug, in
    the one place that did not get updated with the rest."""
    if os.environ.get("TS_TRANSCRIPT_DIR"):
        return "nothing to audit — that is TS_TRANSCRIPT_DIR, which is set"
    return "nothing to audit — set TS_TRANSCRIPT_DIR if yours live elsewhere"


def _memory(mach) -> str:
    mem = mach["memory"]
    sk = mach["skills"]
    out = [R.heading("PREAMBLE SOURCES  (paid on every turn)")]
    if mem["files"]:
        rows = [["{:,}".format(t), _short(p)] for p, t in mem["files"]]
        rows.append(["{:,}".format(mem["total"]), R.bold("total instruction files")])
        out.append(R.table(rows, ["~tokens", "file"], "rl"))
    else:
        out.append("  " + R.dim("no CLAUDE.md / memory files found"))
    out.append("")
    # The on-disk size of every SKILL.md used to be printed here, 27x the
    # number it was explaining and 7.6x the whole preamble it sat under, with
    # a one-line caveat asking the reader to discount it. Needing that caveat
    # was the tell. What loads is the frontmatter description, so that is what
    # is reported; the caveat is gone because it is no longer needed.
    dup = sk.get("duplicates", 0)
    out.append(R.kv("installed skills", "{:,}".format(sk["count"]),
                    "~{:,} tokens of descriptions, loaded every session".format(
                        sk.get("desc_total", 0))))
    if dup:
        out.append("  " + R.dim("%d further SKILL.md file(s) are duplicate "
                                "copies of these, nested in the plugin cache, "
                                "and are not counted" % dup))
    if sk["files"]:
        for p, t in sk["files"][:5]:
            out.append("      %7s  %s" % ("{:,}".format(t), _short(p)))
    return "\n".join(out)


def _mcp(mach) -> str:
    m = mach["mcp"]
    out = [R.heading("MCP SERVERS")]
    if not m.get("readable"):
        out.append("  " + R.dim("~/.claude.json not readable"))
        return "\n".join(out)
    out.append(R.kv("global", ", ".join(m["global"]) or R.dim("(none)")))
    if m["projects"]:
        for proj, names in sorted(m["projects"].items()):
            out.append(R.kv("  " + os.path.basename(proj.rstrip("/"))[:26],
                            ", ".join(names)))
    else:
        out.append(R.kv("project-scoped", R.dim("(none)")))
    ts = mach["env"].get("ENABLE_TOOL_SEARCH")
    base = mach["env"].get("ANTHROPIC_BASE_URL")
    out.append("")
    out.append(R.kv("ENABLE_TOOL_SEARCH", ts or R.dim("(unset — default on)")))
    out.append(R.kv("ANTHROPIC_BASE_URL", base or R.dim("(unset — direct)"),
                    "a custom base URL disables tool deferral" if base else ""))
    return "\n".join(out)


def _hooks(mach) -> str:
    h = mach["hooks"]
    out = [R.heading("HOOKS")]
    if not h:
        out.append("  " + R.dim("none configured"))
        return "\n".join(out)
    for event, cmds in sorted(h.items()):
        for c in cmds:
            out.append(R.kv(event, c[:60] + ("..." if len(c) > 60 else "")))
    return "\n".join(out)


def _tools(mach) -> str:
    out = [R.heading("KNOWN THIRD-PARTY TOOLS")]
    rows = []
    for name, info in sorted(mach["tools"].items()):
        rows.append([
            name,
            R.green("installed") if info["path"] else R.dim("absent"),
            info["version"] or "-",
            info["what"],
        ])
    out.append(R.table(rows, ["tool", "state", "version", "what it does"], "llll"))
    out.append("\n  " + R.dim("presence is not a recommendation — `ts advise` "
                              "decides from your measurements whether any of "
                              "these would pay off here"))
    return "\n".join(out)


def _shape() -> str:
    fleet = Fleet.load()
    if not fleet.turns():
        return ""
    subs = fleet.substantive()
    if not subs:
        return ""
    turns = sorted(s.turns for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)
    peaks = sorted(s.peak for s in subs)
    out = [R.heading("MEASURED SHAPE  (from your transcripts)")]
    out.append(R.kv("sessions", "{:,}".format(len(fleet.sessions))))
    out.append(R.kv("assistant turns", "{:,}".format(fleet.turns())))
    out.append(R.kv("amplification", "%.0fx" % fleet.amplification(),
                    "every content token billed this many times"))
    if floors:
        out.append(R.kv("fixed preamble (median)", "{:,} tokens".format(
            quantile(floors, 0.5))))
    out.append(R.kv("session turns (median/p90)", "%s / %s" % (
        "{:,}".format(quantile(turns, 0.5)), "{:,}".format(quantile(turns, 0.9)))))
    out.append(R.kv("peak context (median/max)", "%s / %s" % (
        "{:,}".format(quantile(peaks, 0.5)), "{:,}".format(peaks[-1]))))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts doctor", description=__doc__)
    for flag in ("memory", "mcp", "hooks", "tools"):
        ap.add_argument("--" + flag, action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    mach = machine.collect()

    if args.json:
        print(json.dumps(mach, indent=1, default=str))
        return 0

    selective = any([args.memory, args.mcp, args.hooks, args.tools])
    if not selective:
        print(_environment())
        shape = _shape()
        if shape:
            print(shape)
    if args.memory or not selective:
        print(_memory(mach))
    if args.mcp or not selective:
        print(_mcp(mach))
    if args.hooks or not selective:
        print(_hooks(mach))
    if args.tools or not selective:
        print(_tools(mach))
    if not selective:
        print("\n  " + R.dim("next: ") + "ts advise" + R.dim("  — ranked fixes for these numbers"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
