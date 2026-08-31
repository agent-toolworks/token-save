#!/usr/bin/env python3
"""What is installed and configured on THIS machine.

`ts audit` reads history; this reads the present. Detectors in advise.py need
both, because the same measurement means different things depending on setup —
a 40K preamble is a finding when you have three MCP servers and tool search
off, and merely a fact when tool search is on and it is all project memory.

Everything here degrades: a missing or malformed file is reported as unknown,
never raised. A tool that reports "your config is broken" when it simply could
not read the file is worse than one that says nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transcripts import BUCKET_ASSISTANT, toks  # noqa: E402

_CONFIG_DIR_ENV = os.environ.get("CLAUDE_CONFIG_DIR")
CLAUDE_HOME = os.path.expanduser(_CONFIG_DIR_ENV or "~/.claude")

# .claude.json was the one path pinned to the real home while every other
# derived from CLAUDE_HOME, so global MCP configuration was read from the
# invoking user's actual home whatever CLAUDE_CONFIG_DIR said. Two costs: a
# machine that genuinely uses CLAUDE_CONFIG_DIR had its MCP configuration
# described from the wrong file, and no sandboxed test could reach this path
# at all -- which is why `verify` exports a throwaway CLAUDE_CONFIG_DIR and
# mcp-schema still fired on the fixture fleets, reading the real config.
#
# It lives INSIDE the config directory. The env-vars reference says of
# CLAUDE_CONFIG_DIR that "all settings, session history, and plugins are
# stored under this path", the documented use is running multiple accounts
# side by side, and .claude.json holds the sign-in session -- which that use
# requires to move with it. `projects/` is documented relocating inside it the
# same way. Note the DEFAULT is not inside: ~/.claude.json is a sibling of
# ~/.claude/, so the unset case is spelled out separately rather than derived,
# and today's behaviour is unchanged.
CLAUDE_JSON = (os.path.join(CLAUDE_HOME, ".claude.json") if _CONFIG_DIR_ENV
               else os.path.expanduser("~/.claude.json"))

# Third-party tools this project knows how to reason about. Presence alone is
# not a recommendation — advise.py decides from the measurements whether any of
# them would actually pay off here.
KNOWN_TOOLS = {
    "rtk": "Bash-output compressor (PreToolUse hook), rtk-ai/rtk",
    "headroom": "compression proxy, headroomlabs-ai/headroom",
    "repomix": "repo packer, reduces exploratory reads",
}


def _read_json(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return None


def _size_tokens(path) -> int:
    """Approximate token size of a text file.

    Sized as prose rather than with the global constant: these are CLAUDE.md
    and SKILL.md, which are markdown written in sentences, and the per-class
    divisor for prose is 4.06 against a global 3.6 — a 13% overstatement if the
    generic one is used. See the estimator note in transcripts.py.
    """
    try:
        return toks("x" * os.path.getsize(path), BUCKET_ASSISTANT)
    except OSError:
        return 0


def _walk_tokens(root, exts=(".md",)) -> tuple:
    total, files = 0, []
    if not os.path.isdir(root):
        return 0, []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if not n.endswith(exts):
                continue
            p = os.path.join(dirpath, n)
            t = _size_tokens(p)
            total += t
            files.append((p, t))
    files.sort(key=lambda x: -x[1])
    return total, files


def memory_footprint(cwd: str = None) -> dict:
    """Everything that gets injected into every session's preamble."""
    cwd = cwd or os.getcwd()
    out = {"files": [], "total": 0, "projects": {}, "always": 0,
           "project_median": 0, "per_session": 0}

    candidates = [
        os.path.join(CLAUDE_HOME, "CLAUDE.md"),
        os.path.join(cwd, "CLAUDE.md"),
        os.path.join(cwd, "CLAUDE.local.md"),
        os.path.join(cwd, "AGENTS.md"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            t = _size_tokens(p)
            out["files"].append((p, t))
            out["total"] += t

    # Everything above loads in EVERY session. Per-project auto-memory does
    # not: one project's directory loads, not all of them. Summing them into
    # one figure overstates what any single session pays -- 10,852 tokens here
    # against 1,084 for the project this was run from -- and that figure is
    # used to attribute the preamble, where an overstatement lands on the
    # advice. So they are kept apart, and `per_session` is what a session
    # actually carries.
    out["always"] = out["total"]
    mem_root = os.path.join(CLAUDE_HOME, "projects")
    if os.path.isdir(mem_root):
        for proj in sorted(os.listdir(mem_root)):
            d = os.path.join(mem_root, proj, "memory")
            if os.path.isdir(d):
                t, _files = _walk_tokens(d)
                if t:
                    out["files"].append((d + "/*.md", t))
                    out["projects"][proj] = t
                    # `total` stays the sum of everything on disk, which is what
                    # `ts doctor` and `ts share` have always reported.
                    out["total"] += t
    projs = sorted(out["projects"].values())
    # Median rather than the cwd's: a report covering many projects is
    # describing a typical session, not the one this shell happens to be in.
    out["project_median"] = projs[len(projs) // 2] if projs else 0
    out["per_session"] = out["always"] + out["project_median"]
    out["files"].sort(key=lambda x: -x[1])
    return out


def _description_tokens(path) -> int:
    """Tokens in a SKILL.md's frontmatter `description`, and nothing else.

    Only the description loads up front — the body is read when the skill
    fires. Pricing the whole file overstates the preamble by a large factor and
    would point a reader at the wrong fix, which is the specific mistake #2
    warns about.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            head = fh.read(16384)
    except OSError:
        return 0
    if not head.startswith("---"):
        return 0
    end = head.find("\n---", 3)
    front = head[3:end] if end > 0 else head[3:]
    grabbed, taking = [], False
    for line in front.splitlines():
        if re.match(r"^description\s*:", line):
            taking = True
            grabbed.append(line.split(":", 1)[1])
            continue
        if taking:
            # A folded YAML scalar continues until the next top-level key.
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", line):
                break
            grabbed.append(line)
    text = " ".join(x.strip() for x in grabbed).strip()
    return toks(text, BUCKET_ASSISTANT) if text else 0


def skills_footprint() -> dict:
    """Installed skills. Only descriptions load up front, but a skill with a
    bloated description is paid for in every session whether used or not.

    `total` is the whole-file size and is NOT what the preamble pays;
    `desc_total` is. Both are reported because the gap between them is the
    reason to look: a 40x ratio means the descriptions are fine and the bodies
    are large, which costs nothing until a skill fires.
    """
    roots = [os.path.join(CLAUDE_HOME, "skills"),
             os.path.join(CLAUDE_HOME, "plugins")]
    # A recursive **/SKILL.md walk finds the same skill more than once: a
    # plugin cache can hold <plugin-a>/skills/<name>/SKILL.md and, nested
    # under another plugin, <plugin-b>/plugins/<plugin-a>/skills/<name>/
    # SKILL.md. 40 files here are 30 distinct skills; the reporter's 155 are
    # 63. Counting the copies inflates the skill count, the on-disk total and
    # `skills_installed` in `ts share` -- and that last one is a field people
    # are asked to send in, used to argue that machines differ. Inflated by
    # packaging on one machine and not another, it compares the wrong thing.
    #
    # Deduplicated by skill name, shallowest path winning, so the canonical
    # copy is the one counted rather than whichever os.walk reached first.
    by_name = {}
    dupes = 0
    for r in roots:
        if not os.path.isdir(r):
            continue
        for dirpath, _d, names in os.walk(r):
            if "SKILL.md" not in names:
                continue
            p = os.path.join(dirpath, "SKILL.md")
            name = os.path.basename(dirpath)
            depth = p.count(os.sep)
            if name in by_name:
                dupes += 1
                if depth >= by_name[name][0]:
                    continue
            by_name[name] = (depth, p)
    total, desc_total, found = 0, 0, []
    for _depth, p in by_name.values():
        d = _description_tokens(p)
        total += _size_tokens(p)
        desc_total += d
        found.append((p, d))
    found.sort(key=lambda x: -x[1])
    return {"count": len(found), "total": total, "desc_total": desc_total,
            "duplicates": dupes, "files": found}


def mcp_servers() -> dict:
    """Configured MCP servers, global and per-project."""
    cfg = _read_json(CLAUDE_JSON)
    if cfg is None:
        return {"readable": False, "global": [], "projects": {}}
    g = list((cfg.get("mcpServers") or {}).keys())
    projects = {}
    for proj, v in (cfg.get("projects") or {}).items():
        names = list(((v or {}).get("mcpServers") or {}).keys())
        if names:
            projects[proj] = names
    return {"readable": True, "global": g, "projects": projects}


def settings() -> dict:
    out = {}
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(CLAUDE_HOME, name)
        d = _read_json(p)
        if d is not None:
            out[name] = d
    return out


def env_flags() -> dict:
    """Environment switches that change token behaviour."""
    keys = ("ENABLE_TOOL_SEARCH", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
            "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
            "DISABLE_PROMPT_CACHING")
    out = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    # settings.json can set env too, and that is the durable place.
    for _name, d in settings().items():
        for k, v in (d.get("env") or {}).items():
            out.setdefault(k, str(v))
    return out


def installed_tools() -> dict:
    out = {}
    for name, desc in KNOWN_TOOLS.items():
        path = shutil.which(name)
        ver = None
        if path:
            try:
                ver = subprocess.run(
                    [name, "--version"], capture_output=True, text=True,
                    timeout=5).stdout.strip().splitlines()[:1]
                ver = ver[0] if ver else None
            except Exception:
                ver = None
        out[name] = {"path": path, "version": ver, "what": desc}
    return out


def hooks() -> dict:
    """Hooks configured anywhere in settings, flattened to event -> commands."""
    out = {}
    for _name, d in settings().items():
        for event, entries in (d.get("hooks") or {}).items():
            for entry in entries or []:
                for h in (entry.get("hooks") or []):
                    cmd = h.get("command")
                    if cmd:
                        out.setdefault(event, []).append(cmd)
    return out


def collect(cwd: str = None) -> dict:
    """One call for every detector that needs to know about this machine."""
    return {
        "claude_home": CLAUDE_HOME,
        "memory": memory_footprint(cwd),
        "skills": skills_footprint(),
        "mcp": mcp_servers(),
        "env": env_flags(),
        "tools": installed_tools(),
        "hooks": hooks(),
        "settings_present": sorted(settings().keys()),
    }
