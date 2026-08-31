#!/usr/bin/env python3
"""Read Claude Code transcripts and account for what they cost.

Two kinds of number come out of here and they must never be confused:

  BILLED    — read verbatim from the ``usage`` block the provider wrote into
              every assistant record. Exact. Nothing is estimated.
  CONTENT   — the size of the text that accumulated in the conversation.
              Estimated, because the transcript stores text, not tokens.

The whole point of this tool is the RATIO between them, so the honest thing is
to keep the exact side exact and label the estimated side as estimated. Every
report prints which tokenizer produced the CONTENT column.

Estimator: ``len(text) / divisor``, with the divisor chosen per content class.
When ``tiktoken`` is importable it is used instead and the report says so.

A single constant of 3.6 was accurate in AGGREGATE — +1.0% against tiktoken
here — while describing no actual class of content. Measured over 14.3M
characters of real transcript, the classes run from 2.48 chars/token (system
records, dense JSON) to 4.72 (user prose): a spread of 91%. So the aggregate
error was a function of workload MIX, not of the estimator, and a machine
dominated by one class inherited that class's error in full and was never
told. It landed on the denominator of amplification, which is the number the
README leads with.

Per class, end to end against tiktoken on this machine's own transcripts:

    bucket                    old       new
    tool call inputs        -3.1%     -0.3%
    tool results (text)     +3.8%     +2.4%
    attachments             +1.9%     -0.8%
    assistant text         +14.1%     +1.1%
    system records         -31.4%     -0.3%
    user prompts           +28.9%     +4.6%
    CONTENT TOTAL           +1.0%     +0.8%

The aggregate barely moves. The worst single bucket improves 6.8x, and that is
the whole point: the total was never what was wrong. Blends of several signals
are still not used — they scored worse than a constant because they overfit
whitespace — and a table of per-class constants stays as auditable as one.

The divisors average two independent measurements where two exist and use the
single available one otherwise; they are a proxy, since Anthropic's tokenizer
is neither cl100k_base nor o200k_base.

Do NOT expect the ``assistant text`` and ``assistant thinking`` buckets to sum
to billed ``output``. They cannot: Claude Code stores the thinking field empty,
and stored assistant content accounts for roughly 42% of billed output here.
Reading one as a check on the other will suggest the estimator is badly broken
when it is not. `reasoning-cost` in advise.py measures that gap deliberately.

Images are counted at Anthropic's documented (w*h)/750, capped at 1600, read
from the actual image header rather than assumed — a base64 payload is ~4x its
token cost, and counting the base64 as text overstates a screenshot by ~40x.
That mistake is the single easiest way to draw the wrong conclusion from a
transcript, so the sniffer is here rather than in a caller.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# tokenizing
# --------------------------------------------------------------------------

_CHARS_PER_TOKEN = 3.6

# Per-class divisors. 3.6 is accurate in AGGREGATE because a mix of content
# averages out to it, not because it describes any actual class: measured
# against tiktoken over 14.3M characters of real transcript here, the classes
# run from 2.48 (system records, dense JSON) to 4.72 (user prose) -- a spread
# of 91%, against an aggregate error of +1.2%.
#
# That makes the error a function of WORKLOAD MIX rather than of the estimator,
# and it lands on the denominator of amplification, which is the number the
# README leads with. A machine dominated by one class inherits that class's
# error wholesale and is never told.
#
# The numbers below average two independent measurements (this machine's, over
# cl100k_base and o200k_base, and the reporter's, over o200k_base) for the four
# classes both measured, and use the single available measurement for the three
# only one did. They are a proxy: Anthropic's tokenizer is neither of these.
#
# What this buys, measured against local ground truth rather than assumed:
# aggregate error is unchanged (+1.2% -> +1.1%) and WORST-CLASS error falls
# from 31.2% to 5.5%. The aggregate was never the problem.
#
# Note that the reporter's four constants alone score +0.9% aggregate but leave
# the worst class at -31.2%, because system records -- which neither their
# table nor the README considered -- fall through to 3.6. Covering every class
# matters more than the precision of any one constant.
CPT_BASH = "bash result"        # a toks() key only; the bucket stays TOOL_TEXT

_CPT = {}                       # populated below, once the buckets are named

_ENC = None
_ENC_NAME = "estimate(len/3.6)"

try:  # pragma: no cover - depends on the host having tiktoken
    import tiktoken as _tiktoken

    _ENC = _tiktoken.get_encoding("cl100k_base")
    _ENC_NAME = "tiktoken(cl100k_base)"
except Exception:
    _ENC = None


def tokenizer_name() -> str:
    """Which counter produced the CONTENT numbers. Printed in every report."""
    return _ENC_NAME


def toks(text, cls: str = None) -> int:
    """Token count for a string. Estimated unless tiktoken is installed.

    ``cls`` is the content class -- a bucket name, or CPT_BASH -- and selects a
    per-class divisor. It defaults to None, which uses the single global
    constant and reproduces the previous behaviour exactly, because toks() has
    callers outside parse() that have no class to give it.
    """
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, default=str)
    if _ENC is not None:
        try:
            return len(_ENC.encode(text, disallowed_special=()))
        except Exception:
            pass
    return int(len(text) / _CPT.get(cls, _CHARS_PER_TOKEN))


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

_IMAGE_TOKEN_CAP = 1600
_IMAGE_TOKEN_FALLBACK = 800  # a screenshot-sized image when the header won't parse


def image_dimensions(raw: bytes):
    """(width, height) from a PNG/JPEG/WebP/GIF header, or None.

    Deliberately header-only: decoding the pixels would need a dependency, and
    every format below puts the dimensions in the first few dozen bytes.
    """
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", raw[16:24])
            return w, h
        if raw[:3] == b"\xff\xd8\xff":  # JPEG: walk the segment chain to a SOFn
            i = 2
            while i < len(raw) - 9:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                              0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            fmt = raw[12:16]
            if fmt == b"VP8X":
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
            if fmt == b"VP8L":
                n = int.from_bytes(raw[21:25], "little")
                return (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
            if fmt == b"VP8 ":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", raw[6:10])
            return w, h
    except Exception:
        pass
    return None


def image_tokens(b64_data: str) -> int:
    """Token cost of an inline image block, from its real pixel dimensions."""
    if not b64_data:
        return _IMAGE_TOKEN_FALLBACK
    head = b64_data[:4096]
    head += "=" * (-len(head) % 4)
    try:
        raw = base64.b64decode(head, validate=False)
    except Exception:
        return _IMAGE_TOKEN_FALLBACK
    wh = image_dimensions(raw)
    if not wh or not wh[0] or not wh[1]:
        return _IMAGE_TOKEN_FALLBACK
    return min(int(wh[0] * wh[1] / 750), _IMAGE_TOKEN_CAP)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

# Anthropic price multipliers relative to one base input token. Cache reads are
# a tenth of an input token and cache writes a quarter more than one, so a token
# that merely SITS in context is ~12x cheaper per turn than one being written —
# which is exactly why persistence, not size, is what these reports rank by.
PRICE = {"cache_read": 0.10, "cache_write": 1.25, "input": 1.00, "output": 5.00}

# Bucket names are stable public API: fixes/ and the report both key off them.
BUCKET_TOOL_IN = "tool call inputs"
BUCKET_TOOL_TEXT = "tool results (text)"
BUCKET_TOOL_IMG = "tool results (images)"
BUCKET_ASSISTANT = "assistant text"
BUCKET_THINKING = "assistant thinking"
BUCKET_USER = "user prompts"
BUCKET_ATTACH = "attachments"
BUCKET_SYSTEM = "system records"

# The attachments bucket is four unrelated cost sources with four unrelated
# fixes, and reporting it as one number meant the advice beside it could only
# be right for whichever source happened to dominate. These families are what
# the advice keys off.
#
# The type strings are Claude Code's, not ours, and they move between versions
# -- deferred_tools_delta and agent_listing_delta both look recent. So this map
# is explicitly OPEN: anything unrecognised lands in its own family and is
# reported under its real name rather than being folded into a neighbour. A
# closed vocabulary meeting a moving target is a bug this repo has already had.
FAM_LISTING = "harness listings"
FAM_HOOK = "hook output"
FAM_FILE = "file content"
FAM_BOOKKEEPING = "harness bookkeeping"
FAM_OTHER = "unrecognised"

ATTACH_FAMILY = {
    "skill_listing": FAM_LISTING,
    "deferred_tools_delta": FAM_LISTING,
    "agent_listing_delta": FAM_LISTING,
    "mcp_instructions_delta": FAM_LISTING,
    "nested_memory": FAM_LISTING,
    "hook_success": FAM_HOOK,
    "hook_additional_context": FAM_HOOK,
    "hook_system_message": FAM_HOOK,
    "file": FAM_FILE,
    "edited_text_file": FAM_FILE,
    "opened_file_in_ide": FAM_FILE,
    "directory": FAM_FILE,
    "selected_lines_in_ide": FAM_FILE,
    "total_tokens_reminder": FAM_BOOKKEEPING,
    "task_reminder": FAM_BOOKKEEPING,
    "date_change": FAM_BOOKKEEPING,
    "queued_command": FAM_BOOKKEEPING,
    "auto_mode": FAM_BOOKKEEPING,
    "plan_mode_exit": FAM_BOOKKEEPING,
    "command_permissions": FAM_BOOKKEEPING,
    "diagnostics": FAM_BOOKKEEPING,
}


# Filled in here rather than above because it keys off the bucket names.
_CPT.update({
    BUCKET_ASSISTANT: 4.06,
    BUCKET_USER: 4.49,
    BUCKET_TOOL_IN: 3.50,
    BUCKET_TOOL_TEXT: 3.53,     # tool results that are NOT Bash
    CPT_BASH: 3.69,
    BUCKET_ATTACH: 3.68,
    BUCKET_SYSTEM: 2.48,
    # BUCKET_THINKING is absent deliberately: Claude Code stores the field
    # empty, so there is nothing to size and nothing to calibrate against.
    # See `reasoning-cost`, which measures that cost from the billed side.
})


def attach_family(t: str) -> str:
    """Which cost source an attachment type belongs to.

    Unknown types are NOT guessed into a family. A new Claude Code release
    inventing a type should surface as unrecognised in the report, where
    someone will notice it, rather than silently inflating whichever family a
    substring match happened to suggest.
    """
    return ATTACH_FAMILY.get(t, FAM_OTHER)


@dataclass
class Session:
    path: str
    project: str
    session_id: str
    turns: int = 0
    billed: dict = field(default_factory=lambda: dict(
        cache_read=0, cache_write=0, input=0, output=0))
    ctx_sizes: list = field(default_factory=list)   # per-turn context size, exact
    buckets: dict = field(default_factory=dict)     # bucket name -> tokens
    tool_out: dict = field(default_factory=dict)    # tool name -> result tokens
    tool_in: dict = field(default_factory=dict)     # tool name -> call-input tokens
    tool_calls: dict = field(default_factory=dict)  # tool name -> call count
    bash_out: list = field(default_factory=list)    # every Bash result size
    bash_cmds: list = field(default_factory=list)   # (tokens, command) per call
    reads: dict = field(default_factory=dict)       # (path, off, lim) -> [count, tokens]
    # Records that could not be accounted for. Surfaced rather than
    # swallowed: a tool that silently drops input is worse than one that
    # says how much it dropped.
    skipped: int = 0
    images: int = 0
    mtime: float = 0.0
    is_subagent: bool = False
    # attachment tokens and record counts keyed by Claude Code's own
    # `attachment.type`, so the report can say which cost source it is looking
    # at instead of averaging four of them together.
    attach_types: dict = field(default_factory=dict)
    attach_counts: dict = field(default_factory=dict)

    @property
    def content_total(self) -> int:
        return sum(self.buckets.values())

    @property
    def billed_total(self) -> int:
        return sum(self.billed.values())

    @property
    def cost_units(self) -> float:
        """Billed tokens re-weighted by what each kind actually costs."""
        return sum(self.billed[k] * PRICE[k] for k in self.billed)

    @property
    def floor(self) -> int:
        """The fixed preamble: system prompt + tool schemas + memory.

        Taken as the smallest context seen in the opening turns, which is the
        floor before any work has accumulated. Exact (it is a billed number),
        but only meaningful for sessions with a few turns to look at.
        """
        head = self.ctx_sizes[:5]
        return min(head) if head else 0

    @property
    def peak(self) -> int:
        return max(self.ctx_sizes) if self.ctx_sizes else 0

    @property
    def amplification(self) -> float:
        """How many times the average token of content was billed as a read."""
        c = self.content_total
        return (self.billed["cache_read"] / c) if c else 0.0


def _bump(d: dict, k, n) -> None:
    d[k] = d.get(k, 0) + n


def transcript_dir() -> str:
    return os.path.expanduser(
        os.environ.get("TS_TRANSCRIPT_DIR", "~/.claude/projects"))


def find_transcripts(root: str = None, project: str = None) -> list:
    """Every transcript under ``root``, at any depth.

    Subagent transcripts are written BELOW the session directory --
    ``<project>/<session-id>/subagents/<agent-id>.jsonl`` in one layout,
    ``<project>/<session-id>/<agent-id>.jsonl`` in another -- and they carry
    ordinary ``usage`` records, so they are billed exactly like main-thread
    turns. A one-level glob never opened them: on the machine that reported
    this, 21.9% of billed spend and 34% of turns were invisible.

    That omission was self-serving in a way worth naming: ``session-length``
    recommends pushing exploratory work into subagents, and the tool could not
    see the cost of taking its own advice.

    Recursive, then deduplicated against the flat glob, so a flat layout
    behaves exactly as before.
    """
    root = root or transcript_dir()
    scope = project or "*"
    flat = glob.glob(os.path.join(root, scope, "*.jsonl"))
    deep = glob.glob(os.path.join(root, scope, "**", "*.jsonl"), recursive=True)
    return sorted(set(flat) | set(deep))


def find_sessions(root: str = None, project: str = None) -> list:
    """Main-chain transcripts only -- one per session a human actually had.

    `find_transcripts()` changed meaning when discovery became recursive: it
    returns TRANSCRIPTS, and a subagent has one of its own. Every caller that
    kept reading it as "sessions" has since been found separately, by accident,
    one at a time:

        #8   `--limit N` counted files, so 14 of "25 most recent sessions"
             were subagent transcripts
        #16  a crash, when a subagent-era path met a shape no fixture had
        #22  `ts now` reported on a subagent whenever one had written more
             recently -- which is the state of the world WHILE a subagent runs,
             so the statusline understated context 8.2x and dropped its clear
             warning exactly when it mattered

    So the distinction is now something a caller has to state. Use this when
    you mean sessions; use find_transcripts() when you mean everything that
    was billed, which is what cost accounting wants.
    """
    root = root or transcript_dir()
    return [p for p in find_transcripts(root, project)
            if not is_subagent_path(p, root)]


def _project_of(path: str, root: str) -> str:
    """The project directory a transcript belongs to, however deep it sits."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return os.path.basename(os.path.dirname(path))
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else os.path.basename(os.path.dirname(path))


def is_subagent_path(path: str, root: str) -> bool:
    """True when a transcript is a subagent's rather than a main thread's.

    Depth is the signal, not the directory name: the two layouts seen in the
    wild differ by one level and only one of them uses a ``subagents/``
    directory, but both put the agent deeper than ``<project>/<file>``.
    """
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False
    return len(rel.split(os.sep)) > 2



def parent_session_of(path: str, root: str):
    """The session id a subagent transcript belongs to; None for a main one.

    Structural, like is_subagent_path(): both layouts nest the agent under the
    session directory -- ``<project>/<session-id>/subagents/<agent>.jsonl`` and
    ``<project>/<session-id>/<agent>.jsonl`` -- so the second path component
    names the parent either way.
    """
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return None
    parts = rel.split(os.sep)
    return parts[1] if len(parts) > 2 else None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def limit_to_sessions(paths: list, root: str, limit: int) -> list:
    """The N most recent SESSIONS, each with the subagents it spawned.

    ``--limit N`` is documented as the N most recent sessions, and until
    recursive discovery it was one: every file found was a main transcript.
    Now a subagent's file is written when it runs and sorts by mtime among
    real sessions, so taking the N most recent FILES returns fewer and fewer
    sessions the more a machine delegates. On the machine that reported this,
    ``--limit 25`` was 11 sessions and 14 subagent transcripts, and the report
    still said ``sessions=25``.

    Two things are wrong with that and only one is the label. Because the cut
    happens before classification, a mixed population is fed to every
    downstream statistic for the run -- and the bias grows with exactly the
    behaviour v0.7.0 was added to measure.

    So classify first, then cut, then pull each kept session's subagents back
    in. Dropping them instead would be cheaper and would reintroduce, inside
    the window, the very under-count recursive discovery exists to fix. An
    orphan subagent -- parent not among the N, or no longer on disk -- belongs
    to no session in this window and is left out.
    """
    mains, subs = [], []
    for p in paths:
        (subs if is_subagent_path(p, root) else mains).append(p)
    mains = sorted(mains, key=lambda p: -_mtime(p))[:limit]
    keep = set(os.path.basename(p)[:-len(".jsonl")] for p in mains)
    return sorted(mains + [p for p in subs
                           if parent_session_of(p, root) in keep])


def _flush_usage(sess: "Session", usage: dict) -> None:
    """Apply one MESSAGE's usage to a session. See the note in parse()."""
    cr = usage.get("cache_read_input_tokens") or 0
    cw = usage.get("cache_creation_input_tokens") or 0
    ip = usage.get("input_tokens") or 0
    sess.turns += 1
    sess.billed["cache_read"] += cr
    sess.billed["cache_write"] += cw
    sess.billed["input"] += ip
    sess.billed["output"] += usage.get("output_tokens") or 0
    ctx = cr + cw + ip
    if ctx:
        sess.ctx_sizes.append(ctx)


def parse(path: str, usage_only: bool = False, root: str = None) -> Session:
    """Account for one transcript. Never raises on a malformed line.

    ``usage_only`` skips all content accounting and keeps only the billed
    usage records. `ts now` needs nothing else, and it runs in a statusline
    where a tokenizer pass over a multi-megabyte transcript on every prompt
    render would be felt. With tiktoken installed the difference is roughly
    two orders of magnitude; with the estimator it is still worth having.
    """
    root = root or transcript_dir()
    s = Session(path=path,
                project=_project_of(path, root),
                session_id=os.path.basename(path)[:-6],
                is_subagent=is_subagent_path(path, root))
    try:
        s.mtime = os.path.getmtime(path)
    except OSError:
        pass
    id2name = {}
    id2input = {}
    pending = None      # (message id, usage) for the message being read
    line_no = 0

    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return s

    with fh:
        for line in fh:
            line_no += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("type")

            if kind == "attachment":
                if not usage_only:
                    att = rec.get("attachment")
                    n = toks(json.dumps(att, default=str), BUCKET_ATTACH)
                    _bump(s.buckets, BUCKET_ATTACH, n)
                    kind_ = (att.get("type") if isinstance(att, dict) else None)
                    kind_ = str(kind_) if kind_ else "unlabelled"
                    _bump(s.attach_types, kind_, n)
                    _bump(s.attach_counts, kind_, 1)
                continue
            if kind == "system":
                if not usage_only:
                    _bump(s.buckets, BUCKET_SYSTEM,
                          toks(json.dumps(rec.get("message") or rec, default=str),
                               BUCKET_SYSTEM))
                continue
            if kind not in ("assistant", "user"):
                continue

            msg = rec.get("message") or {}
            content = msg.get("content")

            if kind == "assistant":
                usage = msg.get("usage") or {}
                if usage:
                    # One assistant MESSAGE is written as several transcript
                    # LINES -- roughly one per content block -- and every line
                    # repeats the same usage object. Accumulating per line
                    # counted each message once per block: measured at 1.86
                    # lines per message on one machine and 2.19 on another,
                    # so every billed figure was ~2x too high and every
                    # per-turn figure with it.
                    #
                    # The two halves need different rules, which is why this
                    # is not simply "take the first":
                    #   inputs  -- byte-identical on every line of a message
                    #              (100% of multi-line messages, both corpora),
                    #              so count them ONCE.
                    #   output  -- non-decreasing across lines, the last line
                    #              carrying the total, so take the MAX. Taking
                    #              the first truncates output to a partial.
                    #
                    # Content blocks are NOT duplicated (3,240 of 3,243
                    # multi-line messages carry a distinct slice per line), so
                    # content accounting below stays per line and is correct.
                    mid = msg.get("id")
                    if not mid:
                        # No id: keep the line as its own message rather than
                        # collapsing unrelated lines together. Over-counting a
                        # rare unkeyed line is the safer direction.
                        mid = "\x00line-%d" % line_no
                    if pending is not None and pending[0] != mid:
                        _flush_usage(s, pending[1])
                        pending = None
                    if pending is None:
                        pending = (mid, dict(usage))
                    else:
                        # Same message continued: inputs already held, output
                        # advances.
                        prev = pending[1]
                        if (usage.get("output_tokens") or 0) > (prev.get("output_tokens") or 0):
                            prev["output_tokens"] = usage.get("output_tokens") or 0
                if isinstance(content, list) and not usage_only:
                    try:
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            t = b.get("type")
                            if t == "text":
                                _bump(s.buckets, BUCKET_ASSISTANT,
                                      toks(b.get("text"), BUCKET_ASSISTANT))
                            elif t == "thinking":
                                _bump(s.buckets, BUCKET_THINKING,
                                      toks(b.get("thinking"), BUCKET_THINKING))
                            elif t == "tool_use":
                                name = b.get("name") or "?"
                                inp = b.get("input") or {}
                                id2name[b.get("id")] = name
                                id2input[b.get("id")] = inp
                                n = toks(json.dumps(inp, default=str), BUCKET_TOOL_IN)
                                _bump(s.buckets, BUCKET_TOOL_IN, n)
                                _bump(s.tool_in, name, n)
                                _bump(s.tool_calls, name, 1)
                                if name == "Bash":
                                    s.bash_cmds.append((n, str(inp.get("command", ""))))
                    except Exception:
                        # A per-line try/except around json.loads() makes this
                        # loop robust against malformed JSON and nothing else.
                        # The other kind exists: well-formed JSON of a shape
                        # nobody would think to write into a fixture -- a Read
                        # whose `offset` is a list. Two such records in 5,248
                        # took down four of five commands with no partial result
                        # and no diagnostic. A transcript is a log, not an API
                        # response: skip the record, count it, and report on the
                        # other 99.96%.
                        s.skipped += 1
                continue

            # user record
            if usage_only:
                continue
            if isinstance(content, str):
                _bump(s.buckets, BUCKET_USER, toks(content, BUCKET_USER))
                continue
            if not isinstance(content, list):
                continue
            try:
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text":
                        _bump(s.buckets, BUCKET_USER,
                              toks(b.get("text"), BUCKET_USER))
                    elif t == "image":
                        n = image_tokens(((b.get("source") or {}).get("data")) or "")
                        _bump(s.buckets, BUCKET_USER, n)
                        s.images += 1
                    elif t == "tool_result":
                        tuid = b.get("tool_use_id")
                        name = id2name.get(tuid, "?unknown")
                        cc = b.get("content")
                        text_n = 0
                        img_n = 0
                        # Bash output and other tool results are one bucket in the
                        # report but two content classes in the estimator: on the
                        # reporting machine they sit 9% apart, the single largest
                        # contributor to the spread.
                        rcls = CPT_BASH if name == "Bash" else BUCKET_TOOL_TEXT
                        if isinstance(cc, str):
                            text_n = toks(cc, rcls)
                        elif isinstance(cc, list):
                            for blk in cc:
                                if not isinstance(blk, dict):
                                    continue
                                if blk.get("type") == "text":
                                    text_n += toks(blk.get("text"), rcls)
                                elif blk.get("type") == "image":
                                    img_n += image_tokens(((blk.get("source") or {}).get("data")) or "")
                                    s.images += 1
                        elif cc is not None:
                            text_n = toks(json.dumps(cc, default=str), rcls)
                        if text_n:
                            _bump(s.buckets, BUCKET_TOOL_TEXT, text_n)
                        if img_n:
                            _bump(s.buckets, BUCKET_TOOL_IMG, img_n)
                        _bump(s.tool_out, name, text_n + img_n)
                        if name == "Bash":
                            s.bash_out.append(text_n)
                        elif name in ("Read", "read"):
                            # Keyed on the RANGE as well as the path. Dropping
                            # offset/limit made lines 1-50 and lines 400-450 look
                            # like two reads of the same thing, and everything
                            # downstream then charged all but one of them as a
                            # duplicate. 89% of the groups this fired on had every
                            # read at a different range -- nothing duplicated.
                            # None/None is the whole file, and is correctly a
                            # different key from any ranged read of it.
                            inp = id2input.get(tuid) or {}
                            # Every component coerced, not just the path.
                            # `offset` arrives as a LIST in real transcripts
                            # (`offset = [389, 415]`) -- a malformed tool input
                            # the harness recorded verbatim. str(None) is
                            # "None", distinct from any real range, so a whole
                            # file stays a different key from a ranged read.
                            rkey = (str(inp.get("file_path", "?")),
                                    str(inp.get("offset")),
                                    str(inp.get("limit")))
                            cur = s.reads.get(rkey) or [0, 0]
                            s.reads[rkey] = [cur[0] + 1, cur[1] + text_n + img_n]
            except Exception:
                # A per-line try/except around json.loads() makes this
                # loop robust against malformed JSON and nothing else.
                # The other kind exists: well-formed JSON of a shape
                # nobody would think to write into a fixture -- a Read
                # whose `offset` is a list. Two such records in 5,248
                # took down four of five commands with no partial result
                # and no diagnostic. A transcript is a log, not an API
                # response: skip the record, count it, and report on the
                # other 99.96%.
                s.skipped += 1
    if pending is not None:
        _flush_usage(s, pending[1])
    return s


@dataclass
class Fleet:
    """Every session found, plus the totals the reports and detectors read."""
    sessions: list = field(default_factory=list)

    @classmethod
    def load(cls, root: str = None, project: str = None, limit: int = None) -> "Fleet":
        root = root or transcript_dir()
        paths = find_transcripts(root, project)
        if limit:
            paths = limit_to_sessions(paths, root, limit)
        return cls(sessions=[parse(p, root=root) for p in paths])

    # -- aggregates ---------------------------------------------------------
    def billed(self) -> dict:
        out = dict(cache_read=0, cache_write=0, input=0, output=0)
        for s in self.sessions:
            for k in out:
                out[k] += s.billed[k]
        return out

    def cost_units(self) -> float:
        b = self.billed()
        return sum(b[k] * PRICE[k] for k in b)

    def buckets(self) -> dict:
        out = {}
        for s in self.sessions:
            for k, v in s.buckets.items():
                _bump(out, k, v)
        return out

    def content_total(self) -> int:
        return sum(self.buckets().values())

    def amplification(self) -> float:
        c = self.content_total()
        return (self.billed()["cache_read"] / c) if c else 0.0

    def turns(self) -> int:
        return sum(s.turns for s in self.sessions)

    def substantive(self, min_turns: int = 5) -> list:
        """Main-thread sessions long enough for turn-shape stats to mean
        anything. Subagents are excluded deliberately: their length is not a
        habit anyone can change by clearing, and folding them in halves the
        median turn count while telling you nothing actionable. Cost
        aggregates below count them like any other traffic."""
        return [s for s in self.sessions
                if s.turns >= min_turns and not s.is_subagent]

    def mcp_servers_called(self) -> set:
        """Distinct MCP servers actually invoked, from the tool names on the
        wire (``mcp__<server>__<tool>``).

        The configured count misses plugin-provided servers entirely -- on the
        machine that reported this, 3 were configured and 10 were called, six
        of them arriving via plugins. What was called is ground truth; what is
        configured is a subset of it.
        """
        out = set()
        for sess in self.sessions:
            for name in sess.tool_calls:
                if not isinstance(name, str):
                    continue
                if name.startswith("mcp__"):
                    parts = name.split("__", 2)
                    if len(parts) == 3 and parts[1]:
                        out.add(parts[1])
                elif name.startswith("mcp_"):
                    parts = name.split("_", 2)
                    if len(parts) == 3 and parts[1]:
                        out.add(parts[1])
        return out

    def skipped(self) -> int:
        """Records that could not be accounted for, across every transcript.

        Reported rather than swallowed. A tool that silently drops input is
        worse than one that says how much it dropped -- and the drop being
        invisible is what let a two-record type confusion read as a total
        failure of the tool.
        """
        return sum(x.skipped for x in self.sessions)

    def main_sessions(self) -> list:
        """Sessions proper. With subagents() this partitions ``sessions``,
        which is every transcript found and what the cost totals sum. Report
        a count of one or the other -- never of ``sessions`` under the name
        "sessions", which is what made --limit misreport itself."""
        return [s for s in self.sessions if not s.is_subagent]
    def attach_types(self) -> dict:
        """Attachment tokens by Claude Code's own type string."""
        return self.merged("attach_types")

    def attach_counts(self) -> dict:
        """Attachment record counts by type."""
        return self.merged("attach_counts")

    def attach_median_per_session(self) -> dict:
        """Median tokens per session for each attachment type.

        Over the sessions that carry the type at all, not over every session:
        the once-per-session listings arrive at ~1.0 per session, and averaging
        them across sessions that predate the feature would understate what a
        session actually pays today.
        """
        per = {}
        for sess in self.sessions:
            for t, n in sess.attach_types.items():
                if n:
                    per.setdefault(t, []).append(n)
        return {t: quantile(sorted(v), 0.5) for t, v in per.items()}

    def attach_families(self) -> dict:
        """Attachment tokens grouped into the four cost sources plus unknowns."""
        out = {}
        for t, n in self.attach_types().items():
            _bump(out, attach_family(t), n)
        return out

    def mcp_tools_called(self) -> set:
        """Distinct MCP TOOLS invoked, as ``mcp__<server>__<tool>``.

        A floor on how many definitions are loaded, not the count: a tool that
        exists and was never called is invisible here. It is a floor measured
        from this machine's own traffic, which is the point -- the alternative
        was a constant.
        """
        out = set()
        for sess in self.sessions:
            for name in sess.tool_calls:
                if name.startswith("mcp__"):
                    out.add(name)
        return out

    def subagents(self) -> list:
        return [s for s in self.sessions if s.is_subagent]

    def subagent_cost_share(self) -> float:
        total = self.cost_units()
        if not total:
            return 0.0
        return 100.0 * sum(s.cost_units for s in self.subagents()) / total

    def merged(self, attr: str) -> dict:
        out = {}
        for s in self.sessions:
            for k, v in getattr(s, attr).items():
                if isinstance(v, list):
                    cur = out.get(k) or [0, 0]
                    out[k] = [cur[0] + v[0], cur[1] + v[1]]
                else:
                    _bump(out, k, v)
        return out

    def bash_out(self) -> list:
        out = []
        for s in self.sessions:
            out.extend(s.bash_out)
        return out

    def bash_cmds(self) -> list:
        out = []
        for s in self.sessions:
            out.extend(s.bash_cmds)
        return out


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

def pct(n, d) -> float:
    return (100.0 * n / d) if d else 0.0


def quantile(sorted_values: list, q: float):
    if not sorted_values:
        return 0
    i = int(q * (len(sorted_values) - 1))
    return sorted_values[i]


def human(n) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % n


# Words that precede the real program without being it.
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "sudo",
                       "builtin", "then", "do", "!"})
# Shell constructs that ARE the thing being run; reporting them is correct.
_KEYWORDS = frozenset({"for", "while", "until", "if", "case", "select",
                       "function"})
_CONNECTORS = frozenset({"&&", "||", ";", "|", "(", ")", "{", "}"})
# Commands that only prepare the environment. A line consisting solely of one
# of these is scenery: the program a multi-line script "runs" is the first line
# that actually does something.
_PREFIX_CMDS = frozenset({"cd", "export", "set", "unset", "source", ".",
                          "shopt", "umask"})
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Non-greedy up to the first separator, so a quoted path with spaces is
# consumed without this pattern carrying quote literals of its own.
_CD = re.compile(r"^cd\s+.*?(?:&&|\|\||;|\n)\s*", re.S)
_WORD = re.compile(r"^(\S+)\s*")


def _program_of_segment(rest):
    """Classify one command segment, consuming prefixes in a loop.

    A loop rather than a wider regex, because the obvious widening leaks the
    other way: a bare redirect digit or a ``for`` keyword becomes the
    "program". Keywords are returned deliberately -- a command that really is a
    ``for`` loop is honestly reported as one.
    """
    for _ in range(24):          # bounded: no pathological input can spin here
        if not rest:
            return "?"
        m = _CD.match(rest)
        if m:
            rest = rest[m.end():]
            continue
        m = _WORD.match(rest)
        if not m:
            return "?"
        word = m.group(1)
        if word in _CONNECTORS or _ASSIGN.match(word) or word in _WRAPPERS:
            rest = rest[m.end():]
            continue
        if word in _KEYWORDS:
            return word
        prog = word.rsplit("/", 1)[-1].strip("\"'`;|&(){}")
        return prog or "?"
    return "?"


def command_program(cmd):
    """The program a shell command actually runs.

    A single leading-token regex used to be enough until it wasn't: it took the
    first word-ish run of characters, so ``SP=/some/path cmd`` reported ``SP``.
    On the machine that reported this, 42.4% of Bash calls were attributed to a
    variable that does not exist, pushing the real leaders off the table.

    Newlines separate commands as surely as ``&&`` does. A call that opens with
    a ``cd`` line and continues on the next is extremely common, and treating
    only ``&&``/``;`` as separators left ``cd`` and ``export`` at the top of
    the leaderboard describing nothing.
    """
    text = (cmd or "").strip()
    if not text:
        return "?"
    fallback = None
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        prog = _program_of_segment(line)
        if prog == "?":
            continue
        if prog in _PREFIX_CMDS:
            # Scenery. Remember it in case the whole call is scenery, but keep
            # looking for something that does work.
            fallback = fallback or prog
            continue
        return prog
    return fallback or "?"

_CD_CHAINED = re.compile(r"^cd\s+\S.*?(?:&&|\|\||;|\n)", re.S)
_CD_STANDALONE = re.compile(r"^cd\s+\S+\s*$")


def cd_shape(cmd):
    """Classify how a command uses ``cd``: chained, standalone, or neither.

    The two shapes are not interchangeable on every machine. ``cd X && cmd``
    draws an approval prompt where a standalone ``cd`` followed by a scoped
    command does not, and prefix-matched allowlists cover the second and not
    the first. A machine carrying BOTH shapes in quantity is one actively
    converting the first into the second -- a behavioural signal already
    sitting in the transcripts, and the only discriminator proposed for this
    that survived being measured. Revealed preference on compound commands
    generally does not work: the reporting machine writes 37% compound calls
    and still wants the cd advice suppressed.
    """
    text = (cmd or "").strip()
    if not text:
        return None
    # Standalone means the WHOLE command is a cd -- not merely that it opens
    # with one. Matching only the first line classified every multi-line script
    # beginning with `cd` as standalone (622 of 622 on the first machine
    # tested), which is the same newline blind spot as the original
    # command_program bug, one level up.
    if _CD_STANDALONE.match(text):
        return "standalone"
    if _CD_CHAINED.match(text):
        return "chained"
    return None
