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
import shutil
import subprocess

CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
CLAUDE_JSON = os.path.expanduser("~/.claude.json")

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
    """Approximate token size of a text file (len/3.6, see transcripts.py)."""
    try:
        return int(os.path.getsize(path) / 3.6)
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
    out = {"files": [], "total": 0}

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

    # Per-project auto-memory directories (this harness writes one per project).
    mem_root = os.path.join(CLAUDE_HOME, "projects")
    if os.path.isdir(mem_root):
        for proj in os.listdir(mem_root):
            d = os.path.join(mem_root, proj, "memory")
            if os.path.isdir(d):
                t, files = _walk_tokens(d)
                if t:
                    out["files"].append((d + "/*.md", t))
                    out["total"] += t
    out["files"].sort(key=lambda x: -x[1])
    return out


def skills_footprint() -> dict:
    """Installed skills. Only descriptions load up front, but a skill with a
    bloated description is paid for in every session whether used or not."""
    roots = [os.path.join(CLAUDE_HOME, "skills"),
             os.path.join(CLAUDE_HOME, "plugins")]
    total, found = 0, []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for dirpath, _d, names in os.walk(r):
            if "SKILL.md" in names:
                p = os.path.join(dirpath, "SKILL.md")
                t = _size_tokens(p)
                total += t
                found.append((p, t))
    found.sort(key=lambda x: -x[1])
    return {"count": len(found), "total": total, "files": found}


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
