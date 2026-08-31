#!/usr/bin/env python3
"""`ts share` — a profile of this machine safe to send to someone else.

Every threshold in this tool was calibrated against one machine. That is its
biggest weakness and the only cure is other people's numbers, so there has to
be a way to hand those over without handing over anything private.

The other outputs cannot be shared. `advise --json` puts real file paths in its
evidence lines; `doctor --json` is paths end to end. This emits shape only:
distributions, counts, ratios, and which detectors fired. No paths, no project
or session names, no commands, no file or tool contents.

The redaction is a WHITELIST — every field is named explicitly below and
anything not named cannot appear, which is the only construction that stays
safe when someone later adds a field elsewhere. `--show` prints exactly what
would be sent so nobody has to take that on trust.

Usage: share.py [--show] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import advise as A  # noqa: E402
import machine as M  # noqa: E402
import report as R  # noqa: E402
from transcripts import Fleet, quantile, tokenizer_name  # noqa: E402

SCHEMA = 1


def _dist(values: list) -> dict:
    v = sorted(values)
    if not v:
        return {"n": 0}
    return {
        "n": len(v),
        "median": quantile(v, 0.5),
        "p75": quantile(v, 0.75),
        "p90": quantile(v, 0.9),
        "p99": quantile(v, 0.99),
        "max": v[-1],
        "total": sum(v),
    }


def build(fleet: Fleet, mach: dict) -> dict:
    """Assemble the shareable profile. Whitelist only — see module docstring."""
    subs = fleet.substantive()
    turns = sorted(s.turns for s in subs)
    peaks = sorted(s.peak for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)

    # Tool NAMES are shape, not content, and are what makes a profile
    # interpretable (an MCP-heavy machine looks nothing like a Bash-heavy one).
    # Tool ARGUMENTS and results never appear. MCP server names are folded to a
    # count, because those can name an employer or an internal service.
    out_by_tool = fleet.merged("tool_out")
    in_by_tool = fleet.merged("tool_in")
    calls_by_tool = fleet.merged("tool_calls")

    def fold(d):
        folded = {}
        mcp = 0
        for k, v in d.items():
            if k.startswith("mcp__") or k.startswith("mcp_"):
                mcp += v
            else:
                folded[k] = v
        if mcp:
            folded["<mcp servers, folded>"] = mcp
        return folded

    findings = A.run(fleet, mach)

    return {
        "schema": SCHEMA,
        "tool_version": "ts",
        "generated_by": "ts share",
        "platform": {
            "os": platform.system(),
            "python": "%d.%d" % sys.version_info[:2],
            "tokenizer": tokenizer_name(),
        },
        "scale": {
            "sessions": len(fleet.main_sessions()),
            "subagent_transcripts": len(fleet.subagents()),
            "sessions_substantive": len(subs),
            "turns": fleet.turns(),
        },
        "billed": fleet.billed(),
        "cost_units": round(fleet.cost_units(), 1),
        "content": fleet.buckets(),
        "content_total": fleet.content_total(),
        "amplification": round(fleet.amplification(), 1),
        "session_shape": {
            "turns": _dist(turns),
            "peak_context": _dist(peaks),
            "preamble": _dist(floors),
        },
        "bash": {
            "output": _dist(fleet.bash_out()),
            "command_tokens": sum(c[0] for c in fleet.bash_cmds()),
        },
        "by_tool": {
            "results": fold(out_by_tool),
            "call_inputs": fold(in_by_tool),
            "calls": fold(calls_by_tool),
        },
        "setup": {
            # Counts and booleans only. Never the server names, never the paths.
            "mcp_servers_configured": (len(mach["mcp"]["global"])
                                       + sum(len(v) for v in mach["mcp"]["projects"].values())),
            # Counts only. Server NAMES stay out: they can identify an employer
            # or an internal service.
            "mcp_servers_called": len(fleet.mcp_servers_called()),
            "skills_installed": mach["skills"]["count"],
            "skills_tokens_on_disk": mach["skills"]["total"],
            "memory_tokens": mach["memory"]["total"],
            "memory_files": len(mach["memory"]["files"]),
            "custom_base_url": bool(mach["env"].get("ANTHROPIC_BASE_URL")),
            "tool_search": mach["env"].get("ENABLE_TOOL_SEARCH") or "unset",
            "known_tools_installed": sorted(
                k for k, v in mach["tools"].items() if v["path"]),
            "hook_events": sorted(mach["hooks"].keys()),
        },
        # Detector OUTCOMES, without the evidence lines — those quote paths.
        "findings": [
            {"id": f.id, "severity": f.severity, "confidence": f.confidence,
             "saving_pct": round(f.saving_pct, 2)}
            for f in findings
        ],
        "silent": sorted(k for k in A.CATALOGUE
                         if k not in {f.id for f in findings}),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts share", description=__doc__)
    ap.add_argument("--show", action="store_true",
                    help="print it to the terminal instead of writing a file")
    ap.add_argument("--out", help="write here (default ./ts-profile.json)")
    ap.add_argument("--root", help="transcript root")
    args = ap.parse_args(argv)

    fleet = Fleet.load(root=args.root)
    if not fleet.turns():
        sys.stderr.write("no usable transcripts found\n")
        return 1
    profile = build(fleet, M.collect())
    text = json.dumps(profile, indent=1, sort_keys=True)

    if args.show:
        print(text)
        return 0

    path = args.out or "ts-profile.json"
    with open(path, "w") as fh:
        fh.write(text + "\n")

    print(R.heading("SHAREABLE PROFILE"))
    print(R.kv("written to", path, "%d bytes" % len(text)))
    print(R.kv("sessions", "{:,}".format(len(fleet.main_sessions()))))
    print(R.kv("amplification", "%.0fx" % fleet.amplification()))
    print(R.kv("detectors fired", "%d of %d"
               % (len(profile["findings"]), len(A.CATALOGUE))))
    print()
    print("  " + R.bold("what is NOT in it: ")
          + "file paths, project or session names, commands,")
    print("  tool arguments or results, MCP server names, memory or code contents.")
    print()
    print("  " + R.dim("read it before you send it: ") + "ts share --show")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
