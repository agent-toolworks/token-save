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
from transcripts import (Fleet, quantile, tokenizer_name,  # noqa: E402
                         tool_version, transcript_dir)

# Bump on ANY change to a field name, a field type, or the MEANING of a value.
# Schema 3: `saving_pct` became nullable and gained `locates_pct` and
# `claims_saving` beside it (#30/#32). That is the same class of change as
# `mcp_servers_configured` becoming nullable under schema 1, listed below --
# and it is not merely additive: the SAME finding that reported 0.0 under
# schema 2 reports null under 3, so a collection aggregating both would
# average a measured zero together with an absent one. That is the #14
# distinction, arriving in the profile.
# Schema 1 covered three mutually incompatible shapes across v0.6.0-v0.11.0:
# `mcp_servers` split into called/configured, `skills_tokens_on_disk` was
# replaced by `skills_description_tokens` -- a different quantity, 484,371
# against 8,681 on one machine -- and `mcp_servers_configured` became nullable.
# Worse, `amplification` under schema 1 is not one quantity: the nested-
# transcript, per-message-usage and per-class-estimator fixes moved it 387 ->
# 134 on an unchanged workload. A collection that cannot recover which of
# those a profile is cannot calibrate anything, which is what profiles are
# being gathered for.
SCHEMA = 3


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
        # The literal string "ts" was stamped here in every profile ever
        # generated -- the wrong token out of `ts version`'s "ts 0.11.0".
        # With a real version, a profile identifies itself even across a
        # schema bump that someone forgets.
        "tool_version": tool_version(),
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
            # null, never 0, when the source could not be read. A machine
            # with four MCP servers and an unreadable config reported 0 and
            # was indistinguishable from one that genuinely has none -- so it
            # did not merely lose a datum, it contributed a WRONG one to
            # cross-machine calibration. null cannot be silently averaged;
            # config_readable says why it is null.
            "config_readable": bool(mach["mcp"].get("readable")),
            "mcp_servers_configured": (
                (len(mach["mcp"]["global"])
                 + sum(len(v) for v in mach["mcp"]["projects"].values()))
                if mach["mcp"].get("readable") else None),
            # Counts only. Server NAMES stay out: they can identify an employer
            # or an internal service.
            "mcp_servers_called": len(fleet.mcp_servers_called()),
            # Distinct skills, not SKILL.md files: nested plugin copies were
            # inflating this by 25% here and 145% on the reporting machine.
            # It is one of the fields used to argue that machines differ, so
            # counting packaging instead of surface compared the wrong thing.
            "skills_installed": mach["skills"]["count"],
            "skills_duplicate_files": mach["skills"].get("duplicates", 0),
            # What actually loads. The on-disk total this replaces was 27x
            # larger and described nothing anyone pays for.
            "skills_description_tokens": mach["skills"].get("desc_total", 0),
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
             # Mirrors `advise --json` exactly, so the two machine-readable
             # surfaces share one vocabulary and a consumer diffing profiles
             # gets the same discriminator either way. round(None) crashed
             # `ts share` outright from v0.19.0 through v0.21.0 -- the command
             # for sending a profile upstream, so the failure landed on the
             # person about to report something.
             "claims_saving": f.claims_saving,
             "saving_pct": (None if f.saving_pct is None
                            else round(f.saving_pct, 2)),
             "locates_pct": (None if f.locates_pct is None
                             else round(f.locates_pct, 2))}
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
        sys.stderr.write("no usable transcripts found under %s\n"
                         % (args.root or transcript_dir()))
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
