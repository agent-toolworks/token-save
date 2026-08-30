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
    # A file with no trailing newline must not acquire one, or apply+revert is
    # not a no-op and shows up as a diff. Terminate its last line (otherwise
    # the marker lands mid-sentence) but leave the block itself unterminated,
    # so the file's trailing-newline state still mirrors the original and
    # revert can put it back exactly.
    if cur and not cur.endswith("\n"):
        block = "\n" + block[:-1]
    with open(path, "a") as fh:
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
    trailing = cur.endswith("\n")
    i = cur.index(begin)
    j = cur.index(end) + len(end)
    # Take back exactly what apply() wrote: the block's own terminator and the
    # blank-line separator in front of it. Anything else in the file -- runs of
    # blank lines the author put there -- is not ours to normalise.
    if j < len(cur) and cur[j] == "\n":
        j += 1
    if i and cur[i - 1] == "\n":
        i -= 1
    new = cur[:i] + cur[j:]
    if not trailing and not cur[j:]:
        new = new.rstrip("\n")     # apply() also had to terminate the last line
    _backup(path)
    with open(path, "w") as fh:
        fh.write(new)
    return True, "removed the block from %s" % path

# ---------------------------------------------------------------------------
# tool-search: keep MCP tool deferral on behind a custom base URL
# ---------------------------------------------------------------------------

# settings.json is the user's own file: hand-written, often version-controlled,
# and frequently full of prose -- `permissions` rules and policy blocks get
# written in sentences, with typographic punctuation. Round-tripping it through
# json.dump() rewrites all of that: ensure_ascii escapes every em-dash and
# indent= re-indents every line, so a one-key change lands as a diff of lines
# nobody touched. That is worse than cosmetic. `ts fixes` is offered as the
# reviewable option -- backup, diff, revert -- and a large unexplained diff
# from an automated editor of your config teaches you to stop reading them.
#
# So the key is spliced in as text. json still does the parsing, both to
# validate the file and to locate the edit; bytes outside the edit are never
# re-serialised.

_DECODER = json.JSONDecoder()


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _members(s: str, start: int):
    """Walk the object at s[start], yielding (key, key_start, val_start, val_end).

    Offsets into s, so a caller can rewrite one member and leave every other
    byte of the file alone.
    """
    i = _skip_ws(s, start)
    if i >= len(s) or s[i] != "{":
        return
    i += 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != '"':
            return
        key_start = i
        key, i = json.decoder.scanstring(s, i + 1)
        i = _skip_ws(s, _skip_ws(s, i) + 1)      # past the colon
        val_start = i
        _v, i = _DECODER.raw_decode(s, i)
        yield key, key_start, val_start, i
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1


def _find(s: str, start: int, key: str):
    for rec in _members(s, start):
        if rec[0] == key:
            return rec
    return None


def _line_indent(s: str, idx: int):
    """The whitespace before idx on its line, or None if idx is not the first
    thing on it -- i.e. the object is written inline and should stay inline."""
    bol = s.rfind("\n", 0, idx) + 1
    prefix = s[bol:idx]
    return prefix if prefix.strip() == "" else None


def _member_indent(s: str, obj_start: int):
    members = list(_members(s, obj_start))
    return _line_indent(s, members[0][1]) if members else None


def _indent_unit(s: str) -> str:
    """The file's own indent step, taken from its first indented line."""
    for line in s.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            return line[:len(line) - len(stripped)]
    return "  "


def _insert_member(s: str, obj_start: int, key: str, value_src: str) -> str:
    """Splice `"key": value_src` in as the last member of an object."""
    obj_start = _skip_ws(s, obj_start)
    _obj, obj_end = _DECODER.raw_decode(s, obj_start)
    close = s.rindex("}", obj_start, obj_end)
    members = list(_members(s, obj_start))
    entry = "%s: %s" % (json.dumps(key, ensure_ascii=False), value_src)
    if not members:
        return s[:obj_start + 1] + entry + s[close:]
    indent = _line_indent(s, members[0][1])
    end = members[-1][3]
    sep = ", " if indent is None else ",\n" + indent
    return s[:end] + sep + entry + s[end:]


def _remove_member(s: str, obj_start: int, key: str) -> str:
    """Cut one member out of an object, taking exactly one comma with it."""
    members = list(_members(s, obj_start))
    idx = next((n for n, m in enumerate(members) if m[0] == key), None)
    if idx is None:
        return s
    _k, key_start, _vs, val_end = members[idx]
    if len(members) == 1:
        obj_start = _skip_ws(s, obj_start)
        _obj, obj_end = _DECODER.raw_decode(s, obj_start)
        close = s.rindex("}", obj_start, obj_end)
        return s[:obj_start + 1] + s[close:]                 # -> {}
    if idx + 1 < len(members):
        return s[:key_start] + s[members[idx + 1][1]:]       # up to the next key
    return s[:members[idx - 1][3]] + s[val_end:]             # last: eat the comma before


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
    raw = _read(path) if os.path.isfile(path) else ""
    data, ok = _load_settings(path)
    if not ok:
        return False, "settings.json does not parse — refusing to overwrite it"
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        return False, '"env" is not an object — refusing to touch it'
    if str((env or {}).get("ENABLE_TOOL_SEARCH")).lower() in ("true", "1", "on"):
        return False, "already applied"

    backup = _backup(path) if os.path.isfile(path) else None
    unit = _indent_unit(raw) if raw.strip() else "  "

    if not raw.strip():
        new = '{\n%s"env": {\n%s%s"ENABLE_TOOL_SEARCH": "true"\n%s}\n}\n' % (
            unit, unit, unit, unit)
    else:
        top = _skip_ws(raw, 0)
        rec = _find(raw, top, "env")
        if rec is None:
            indent = _member_indent(raw, top)
            if indent is None:
                value_src = '{"ENABLE_TOOL_SEARCH": "true"}'
            else:
                value_src = '{\n%s%s"ENABLE_TOOL_SEARCH": "true"\n%s}' % (
                    indent, unit, indent)
            new = _insert_member(raw, top, "env", value_src)
        else:
            inner = _find(raw, rec[2], "ENABLE_TOOL_SEARCH")
            if inner is None:
                new = _insert_member(raw, rec[2], "ENABLE_TOOL_SEARCH", '"true"')
            else:
                new = raw[:inner[2]] + '"true"' + raw[inner[3]:]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(new)
    return True, "set env.ENABLE_TOOL_SEARCH=true in %s%s" % (
        path, " (backup: %s)" % backup if backup else "")


def toolsearch_revert():
    path = _settings_path()
    raw = _read(path) if os.path.isfile(path) else ""
    data, ok = _load_settings(path)
    if not ok:
        return False, "settings.json does not parse"
    env = data.get("env")
    if not isinstance(env, dict) or "ENABLE_TOOL_SEARCH" not in env:
        return False, "not applied"
    top = _skip_ws(raw, 0)
    if len(env) == 1:
        new = _remove_member(raw, top, "env")   # the key was all env held
    else:
        new = _remove_member(raw, _find(raw, top, "env")[2], "ENABLE_TOOL_SEARCH")
    _backup(path)
    with open(path, "w") as fh:
        fh.write(new)
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
