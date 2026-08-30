#!/usr/bin/env python3
"""`ts fixes` — the few changes safe to make mechanically.

Most findings in advise.py are behavioural: no script can shorten your sessions
for you. The ones here edit a file, and every one of them:

  * shows the exact diff before touching anything (`--dry-run` is the default),
  * writes a timestamped backup next to the original,
  * is reversible with `ts fixes revert <id>`,
  * is idempotent — applying twice changes nothing the second time.

A fix that cannot meet all four does not belong here; it belongs in the
`actions` list of a Finding, where a human does it deliberately.

Usage: fixes.py list
       fixes.py show   <id>
       fixes.py apply  <id> [--yes]
       fixes.py revert <id>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report as R  # noqa: E402
from machine import CLAUDE_HOME  # noqa: E402

MARKER_BEGIN = "<!-- ts:begin "
MARKER_END = "<!-- ts:end "

TERSE_BLOCK = """\
## Response style

- Answer at the length the question deserves; no preamble, no recap of what I
  just showed you, no summary of what you are about to do.
- Report a diff or the changed lines, not whole rewritten files.
- When a command's output already answers the question, do not restate it.
"""


def _backup(path: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.ts-backup-%s" % (path, stamp)
    shutil.copy2(path, dst)
    return dst


def _latest_backup(path: str):
    d = os.path.dirname(path) or "."
    base = os.path.basename(path) + ".ts-backup-"
    try:
        cands = sorted(n for n in os.listdir(d) if n.startswith(base))
    except OSError:
        return None
    return os.path.join(d, cands[-1]) if cands else None


def _read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# terse-output: append a response-style block to CLAUDE.md
# ---------------------------------------------------------------------------

def _terse_target() -> str:
    return os.path.join(CLAUDE_HOME, "CLAUDE.md")


def terse_show():
    path = _terse_target()
    cur = _read(path)
    applied = (MARKER_BEGIN + "terse-output") in cur
    block = "%sterse-output -->\n%s%sterse-output -->\n" % (
        MARKER_BEGIN, TERSE_BLOCK, MARKER_END)
    return {
        "id": "terse-output",
        "target": path,
        "applied": applied,
        "exists": os.path.isfile(path),
        "diff": "" if applied else "\n".join("+ " + l for l in block.splitlines()),
        "why": ("Output is billed at 5x input. A standing instruction costs a few "
                "dozen tokens in the preamble and trims every reply after it."),
    }


def terse_apply():
    path = _terse_target()
    cur = _read(path)
    if (MARKER_BEGIN + "terse-output") in cur:
        return False, "already applied"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = _backup(path) if os.path.isfile(path) else None
    block = "\n%sterse-output -->\n%s%sterse-output -->\n" % (
        MARKER_BEGIN, TERSE_BLOCK, MARKER_END)
    with open(path, "a") as fh:
        if cur and not cur.endswith("\n"):
            fh.write("\n")
        fh.write(block)
    return True, "appended to %s%s" % (
        path, " (backup: %s)" % backup if backup else "")


def terse_revert():
    path = _terse_target()
    cur = _read(path)
    begin = MARKER_BEGIN + "terse-output -->"
    end = MARKER_END + "terse-output -->"
    if begin not in cur:
        return False, "not applied"
    i = cur.index(begin)
    j = cur.index(end) + len(end)
    new = (cur[:i] + cur[j:]).rstrip() + "\n"
    _backup(path)
    with open(path, "w") as fh:
        fh.write(new)
    return True, "removed the block from %s" % path


# ---------------------------------------------------------------------------
# tool-search: keep MCP tool deferral on behind a custom base URL
# ---------------------------------------------------------------------------

def _settings_path() -> str:
    return os.path.join(CLAUDE_HOME, "settings.json")


def _load_settings(path: str):
    if not os.path.isfile(path):
        return {}, True
    raw = _read(path)
    if not raw.strip():
        return {}, True
    try:
        return json.loads(raw), True
    except Exception:
        return None, False


def toolsearch_show():
    path = _settings_path()
    data, ok = _load_settings(path)
    if not ok:
        return {"id": "tool-search", "target": path, "applied": False,
                "exists": True, "diff": "",
                "why": "settings.json is not valid JSON; refusing to touch it.",
                "blocked": "settings.json does not parse — fix it by hand first"}
    cur = (data.get("env") or {}).get("ENABLE_TOOL_SEARCH")
    applied = str(cur).lower() in ("true", "1", "on")
    return {
        "id": "tool-search",
        "target": path,
        "applied": applied,
        "exists": os.path.isfile(path),
        "diff": "" if applied else '  env.ENABLE_TOOL_SEARCH: %s -> "true"' % (
            json.dumps(cur) if cur is not None else "(unset)"),
        "why": ("A custom ANTHROPIC_BASE_URL turns off on-demand tool loading, "
                "which puts every MCP tool definition back into every preamble."),
    }


def toolsearch_apply():
    path = _settings_path()
    data, ok = _load_settings(path)
    if not ok:
        return False, "settings.json does not parse — refusing to overwrite it"
    env = data.setdefault("env", {})
    if str(env.get("ENABLE_TOOL_SEARCH")).lower() in ("true", "1", "on"):
        return False, "already applied"
    backup = _backup(path) if os.path.isfile(path) else None
    env["ENABLE_TOOL_SEARCH"] = "true"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return True, "set env.ENABLE_TOOL_SEARCH=true in %s%s" % (
        path, " (backup: %s)" % backup if backup else "")


def toolsearch_revert():
    path = _settings_path()
    data, ok = _load_settings(path)
    if not ok:
        return False, "settings.json does not parse"
    env = data.get("env") or {}
    if "ENABLE_TOOL_SEARCH" not in env:
        return False, "not applied"
    _backup(path)
    del env["ENABLE_TOOL_SEARCH"]
    if not env:
        data.pop("env", None)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return True, "removed env.ENABLE_TOOL_SEARCH from %s" % path


FIXES = {
    "terse-output": (terse_show, terse_apply, terse_revert),
    "tool-search": (toolsearch_show, toolsearch_apply, toolsearch_revert),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts fixes", description=__doc__)
    ap.add_argument("action", choices=["list", "show", "apply", "revert"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--yes", action="store_true", help="apply without confirming")
    args = ap.parse_args(argv)

    if args.action == "list":
        print(R.heading("MECHANICAL FIXES"))
        rows = []
        for fid, (show, _a, _r) in sorted(FIXES.items()):
            info = show()
            rows.append([fid,
                         R.green("applied") if info["applied"] else R.dim("not applied"),
                         info["target"].replace(os.path.expanduser("~"), "~")])
        print(R.table(rows, ["id", "state", "target"], "lll"))
        print("\n  " + R.dim("everything else in `ts advise` is behavioural and "
                             "deliberately not automated"))
        return 0

    if not args.id or args.id not in FIXES:
        sys.stderr.write("unknown fix %r; try: ts fixes list\n" % (args.id,))
        return 2

    show, apply_, revert = FIXES[args.id]

    if args.action == "show":
        info = show()
        print(R.heading(args.id))
        print(R.kv("target", info["target"]))
        print(R.kv("state", "applied" if info["applied"] else "not applied"))
        print("\n  " + info["why"])
        if info.get("blocked"):
            print("\n  " + R.red("blocked: ") + info["blocked"])
        elif info["diff"]:
            print("\n" + info["diff"])
        return 0

    if args.action == "apply":
        info = show()
        if info.get("blocked"):
            sys.stderr.write("blocked: %s\n" % info["blocked"])
            return 1
        if not args.yes:
            print(R.heading("would apply: " + args.id))
            print(R.kv("target", info["target"]))
            print("\n" + (info["diff"] or "  (nothing to change)"))
            print("\n  " + R.dim("re-run with --yes to write it"))
            return 0
        ok, msg = apply_()
        print(("  " + R.green("applied: ") if ok else "  " + R.dim("no change: ")) + msg)
        return 0 if ok else 1

    ok, msg = revert()
    print(("  " + R.green("reverted: ") if ok else "  " + R.dim("no change: ")) + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
