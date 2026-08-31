#!/usr/bin/env python3
"""`ts advise` — the catalogue of fixes, and which ones apply to THIS machine.

Every entry is a detector, not a tip. A detector reads the measurements and
either fires with a number attached or stays silent. That is the whole design:
the same catalogue tells one person "install a Bash-output compressor" and the
next person "a compressor is worthless here, your outputs are 115 tokens" —
because it read their transcripts before opening its mouth.

Savings are expressed in COST UNITS (tokens x price relative to one input
token) and as a share of measured spend, so a fix that removes a few tokens
from every one of a thousand turns ranks above one that removes many tokens
once. Each carries a confidence label:

The label qualifies the SAVING, not the evidence. Evidence is measured or it
is not reported; a saving is always a projection, so no saving is ever labelled
"measured" — an earlier cut did exactly that for three detectors whose numbers
multiplied exact billed figures by an invented constant, which is the failure
this tool was built to catch someone else committing.

    derived    — the projection follows from this machine's own measured shape
    estimated  — measured inputs, but a stated assumption about what you change
    heuristic  — the direction is right, the magnitude is not

Every non-derived Finding must carry an `assumption` naming what was assumed.

Adding a detector: write a function taking (fleet, mach) and returning a
Finding or None, then list it in DETECTORS. Keep the thresholds visible in the
function rather than in a config file — a reader should be able to see why it
fired without going somewhere else.

Every detector that can fire must also declare its `gate`: the conditions it
required, the literals it compared against, and how they combine. That is what
lets the report say "fires at 3.2x its threshold" rather than presenting a
machine sitting just over the line and one sitting far past it identically.
`verify` asserts that anything which fired has a margin of at least 1, so a
gate that drifts from the `if` above it fails the build rather than quietly
reporting a number nobody can check.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transcripts import (FAM_BOOKKEEPING, FAM_FILE, FAM_HOOK,  # noqa: E402
                         FAM_LISTING, FAM_OTHER, PRICE, attach_family,
                         cd_shape, pct, quantile, tokenizer_name)


def to_spend(fleet, share_pct: float) -> float:
    """Convert a share of CONTENT (or of cache reads) into a share of spend.

    Content does not cost anything by existing — it costs by being re-read.
    So a slice of content maps onto the bill through the cache-read line, and
    a slice of cache reads maps through the same factor. Both callers want the
    identical conversion, which is why there is one function rather than two
    hand-inlined constants that could drift apart.
    """
    cost = fleet.cost_units()
    if not cost:
        return 0.0
    read_cost = fleet.billed()["cache_read"] * PRICE["cache_read"]
    return share_pct * (read_cost / cost)


def _fmt(v, unit: str) -> str:
    if unit == "%":
        return "%.1f%%" % v
    if unit:
        return "{:,.0f} {}".format(v, unit)
    return "{:,.0f}".format(v)


@dataclass
class Cond:
    """One thing a detector required, with the literal it was compared against.

    A detector's gate is the most opinionated thing in it and was, until now,
    the least visible: an `if` returning None. Fire and no-fire were reported
    identically whether the machine sat at 1.05x the threshold or 3.2x it, so
    a reader could not tell "you are unusual here" from "everybody trips this".
    Both are useful; they are not the same fact.
    """
    name: str
    value: float
    bound: float                # None for a condition with no scale
    mode: str = "at_least"      # at_least | at_most | flag
    unit: str = ""

    @property
    def ratio(self):
        """How far past its own bound this machine sits, as a multiple.

        None when the condition has no scale to be past -- a boolean is either
        true or it is not, and reporting "1.0x" for one would invent a
        precision that is not there.
        """
        if self.bound is None or self.mode == "flag":
            return None
        if self.mode == "at_most":
            return (self.bound / self.value) if self.value else None
        return (self.value / self.bound) if self.bound else None

    def describe(self) -> str:
        if self.mode == "flag":
            return "%s: %s" % (self.name, "yes" if self.value else "no")
        r = self.ratio
        return "%s  %s %s %s%s" % (
            self.name, _fmt(self.value, self.unit),
            "<=" if self.mode == "at_most" else ">=", _fmt(self.bound, self.unit),
            "" if r is None else "  (%.1fx)" % r)


@dataclass
class Gate:
    """The conditions that let a detector fire, and how they combine."""
    mode: str                   # "any" -- one suffices; "all" -- every one
    conditions: list = field(default_factory=list)

    @property
    def margin(self):
        """Distance from the edge of the firing region.

        For an ANY gate the machine would have to fall below EVERY condition to
        go quiet, so the distance is set by the one it is furthest past. For an
        ALL gate losing any single condition silences it, so the distance is
        set by the one it is closest to losing. Taking max/min the other way
        round would report a margin the detector does not actually have.
        """
        rs = [c.ratio for c in self.conditions if c.ratio is not None]
        if not rs:
            return None
        return max(rs) if self.mode == "any" else min(rs)

    @property
    def binding(self):
        """The condition the margin came from."""
        scaled = [c for c in self.conditions if c.ratio is not None]
        if not scaled:
            return None
        return (max if self.mode == "any" else min)(scaled, key=lambda c: c.ratio)


@dataclass
class Unevaluated:
    """A detector that could not run, as distinct from one that ran and found
    nothing to report.

    Those are different facts and the output could not tell them apart: an
    unreadable config suppressed `mcp-schema` through the same path it would
    use to say "nothing here", so a machine with four MCP servers and an
    unreadable file looked exactly like a machine with none. That is not only
    a display problem -- it feeds a wrong datum into cross-machine
    calibration, where 0 and unknown must never be averaged together.
    """
    id: str
    reason: str


@dataclass
class Finding:
    id: str
    title: str
    severity: str            # high | medium | low
    confidence: str          # measured | estimated | heuristic
    saving_pct: float        # share of measured spend, 0 when unquantifiable
    evidence: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    fix: str = None          # id understood by fixes.py, when auto-appliable
    # What had to be assumed to turn measured facts into a saving. Printed
    # next to the number. A projection with an unstated assumption is the
    # thing this tool exists to complain about, so it may not be omitted.
    assumption: str = ""
    # The gate this machine cleared, and by how much. Every detector that can
    # fire must declare one; verify asserts the declared gate agrees with the
    # code path by checking that anything which fired has margin >= 1.
    gate: Gate = None
    # Where a finding can decompose its own subject, the decomposition travels
    # with it: {"total", "parts": [(tokens, label, how)], "unattributed"}. A
    # partial itemisation read as a complete one is the failure #2 describes,
    # so the residual is part of the structure rather than a rendering detail.
    attribution: dict = None

    @property
    def rank(self) -> tuple:
        return (-self.saving_pct, self.id)


def severity_for(saving_pct: float) -> str:
    """Severity IS the estimated impact.

    An earlier cut let each detector declare its own severity, which produced
    a report where a LOW worth 8% of spend sorted below a MEDIUM worth 2.4%.
    A reader cannot act on a ranking that disagrees with its own numbers, so
    the two are now the same thing and a detector cannot set them apart.
    """
    if saving_pct >= 8:
        return "high"
    if saving_pct >= 2.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def _preamble_actions(parts, residual, med, sk) -> list:
    """Advice aimed at whatever is actually largest, not at a fixed list.

    The old action list opened with "audit the files above" on every machine,
    including ones where those files were a fifth of the preamble. Which lever
    is worth pulling depends on the split, so the split chooses.
    """
    ranked = sorted(parts, key=lambda r: -r[0])
    top = ranked[0][1] if ranked else None
    out = []
    if top == "memory/instruction files":
        out.append("Memory files are the largest part of your preamble. Move "
                   "rarely-needed detail out of CLAUDE.md into a skill or a doc "
                   "the agent reads on demand — instructions that fire on 5% of "
                   "turns should not be in 100% of them. `ts doctor --memory` "
                   "lists them by size.")
    elif top == "skill descriptions":
        out.append("Skill descriptions are the largest part of your preamble: "
                   "{:,} tokens across {} installed skills, paid every session "
                   "whether a skill fires or not. Trim them to their trigger "
                   "phrases, or uninstall what you do not use.".format(
                       ranked[0][0], sk.get("count", 0)))
    elif top in ("deferred tool definitions", "MCP server instructions"):
        out.append("Tool and MCP definitions are the largest part of your "
                   "preamble. Removing an unused server removes its whole "
                   "block — see `mcp-schema`, which lists the ones you never "
                   "call.")
    elif top == "agent listings":
        out.append("Agent listings are the largest part of your preamble. "
                   "Every installed agent's description is loaded up front; "
                   "remove the ones you do not dispatch to.")
    if residual > 0 and pct(residual, med) >= 50:
        out.append("{:.0f}% of the preamble is unattributed — the system prompt "
                   "and built-in tool schemas, which you cannot edit. That is "
                   "the floor, and it means the editable part is the {:.0f}% "
                   "above it.".format(pct(residual, med),
                                      100 - pct(residual, med)))
    out.append("Re-run `ts audit` after a change: the preamble is measured, so "
               "a trim shows up as a smaller floor rather than as a hope.")
    return out


def d_preamble(fleet, mach):
    """The fixed prompt is re-read on every single turn, so it is the one place
    where a one-off edit is multiplied by every turn you will ever run."""
    subs = fleet.substantive()
    floors = sorted(s.floor for s in subs if s.floor)
    if not floors:
        return None
    med = quantile(floors, 0.5)
    reads = fleet.billed()["cache_read"]
    if not reads:
        return None
    cost = med * fleet.turns()
    share = pct(cost, reads)
    if share < 4 and med < 20000:
        return None
    gate = Gate("any", [
        Cond("preamble share of cache reads", share, 4, unit="%"),
        Cond("median preamble size", med, 20000, unit="tokens"),
    ])
    sev = "high" if share >= 12 or med >= 35000 else "medium" if share >= 6 else "low"

    ev = ["fixed preamble is {:,} tokens (median across {} sessions)".format(med, len(floors)),
          "re-read on every one of {:,} turns = {:,} cache-read tokens".format(
              fleet.turns(), cost),
          "that is {:.1f}% of all cache reads you were billed for".format(share)]

    # A finding whose premise is that the preamble is worth auditing has to say
    # what is in it. On the reporting machine it itemised 11,206 of 62,294
    # tokens and left 82% unattributed -- "audit these files" aimed at 18% of
    # the thing. The harness's own listings are the missing bulk, and they are
    # MEASURED from the transcript rather than estimated from disk.
    mem = mach["memory"]
    sk = mach["skills"]
    per_sess = fleet.attach_median_per_session()
    LISTINGS = [
        ("skill_listing", "skill descriptions"),
        ("deferred_tools_delta", "deferred tool definitions"),
        ("agent_listing_delta", "agent listings"),
        ("mcp_instructions_delta", "MCP server instructions"),
        ("nested_memory", "nested memory files"),
    ]
    parts = []
    if mem.get("per_session"):
        parts.append((mem["per_session"], "memory/instruction files",
                      "on disk, always-loaded + median project"))
    measured_skills = per_sess.get("skill_listing", 0)
    for key, label in LISTINGS:
        v = per_sess.get(key, 0)
        if v:
            parts.append((v, label, "measured, once per session"))
    # Fall back to the disk estimate only where the transcripts carry no
    # listing record -- an older Claude Code, or a version that names it
    # something this release does not know.
    if not measured_skills and sk.get("desc_total"):
        parts.append((sk["desc_total"], "skill descriptions",
                      "estimated from %d SKILL.md frontmatter(s)" % sk["count"]))

    attributed = sum(v for v, _l, _h in parts)
    residual = med - attributed
    ev.append("what is in that {:,}, per session:".format(med))
    for v, label, how in sorted(parts, key=lambda r: -r[0]):
        ev.append("    {:>7,}  {:>3.0f}%  {:<26} {}".format(
            v, pct(v, med), label, how))
    if residual > 0:
        ev.append("    {:>7,}  {:>3.0f}%  {:<26} {}".format(
            residual, pct(residual, med), "unattributed",
            "system prompt and built-in tool schemas"))
    elif attributed:
        # Say so rather than showing a negative or silently clamping. The
        # listings are medians over the sessions that carry them and the
        # preamble is a median over all of them; on a machine that changed
        # configuration mid-corpus the two can cross.
        ev.append("    the itemisation exceeds the measured preamble by {:,} "
                  "tokens — these are medians over different session sets, so "
                  "treat the split as indicative".format(-residual))

    for pth, t in mem["files"][:4]:
        ev.append("      {:>7,}  {}".format(
            t, pth.replace(os.path.expanduser("~"), "~")))
    if sk["count"]:
        ev.append("{} installed skills; only the frontmatter descriptions load "
                  "up front (~{:,} tokens on disk, against {:,} of whole "
                  "SKILL.md files)".format(sk["count"], sk.get("desc_total", 0),
                                           sk["total"]))

    # Only the read-multiplied share is recoverable, and only partly: a trim of
    # a third of the preamble is an aggressive but achievable target.
    return Finding(
        id="preamble",
        title="Fixed preamble is re-read every turn",
        severity=sev, confidence="estimated",
        assumption=(
            "a third of the %s tokens that could be attributed is removable; "
            "the %.0f%% that could not is not counted as recoverable"
            % ("{:,}".format(attributed), pct(residual, med))
            if attributed else
            "a third of the preamble is removable — nothing in it could be "
            "attributed to a source, so this is the old flat assumption"),
        # Only the part that can be NAMED is counted as recoverable. The old
        # number took a third of the whole preamble, including a residual that
        # is mostly system prompt and built-in tool schemas -- and on this
        # machine that residual is 77%, so the old figure was projecting a trim
        # of something the reader cannot edit. Where nothing can be attributed
        # the flat assumption is kept rather than silently reporting zero.
        saving_pct=(to_spend(fleet, share * attributed / float(med)) * 0.33
                    if attributed and med else to_spend(fleet, share) * 0.33),
        gate=gate,
        attribution={"total": med,
                     "parts": [(v, l, h) for v, l, h in parts],
                     # Exact, so parts + residual == total always. Positive is
                     # what could not be attributed; NEGATIVE means the
                     # itemisation overshoots, which happens when the listing
                     # medians and the preamble median come from different sets
                     # of sessions. Clamping it to zero would balance the
                     # display while breaking the arithmetic, which is the
                     # dishonesty this whole change is about.
                     "residual": residual,
                     "unattributed": max(0, residual)},
        evidence=ev,
        actions=_preamble_actions(parts, residual, med, sk))


def d_session_length(fleet, mach):
    """Cost grows with the square of session length: each new turn re-reads
    everything before it. This is the single largest lever on most machines."""
    subs = fleet.substantive()
    if len(subs) < 3:
        return None
    turns = sorted(s.turns for s in subs)
    peaks = sorted(s.peak for s in subs)
    med_t, p90_t = quantile(turns, 0.5), quantile(turns, 0.9)
    med_p = quantile(peaks, 0.5)
    if med_t < 80 and med_p < 120000:
        return None
    gate = Gate("any", [
        Cond("median turns per session", med_t, 80),
        Cond("median peak context", med_p, 120000, unit="tokens"),
    ])
    sev = "high" if (p90_t >= 400 or med_p >= 250000) else "medium"

    growth = []
    for s in sorted(subs, key=lambda s: -s.peak)[:3]:
        if s.floor:
            growth.append("    {:>5} turns   {:>7,} -> {:>8,}   ({:.0f}x)".format(
                s.turns, s.floor, s.peak, s.peak / s.floor))

    ev = ["median session: {:,} turns, peak context {:,} tokens".format(med_t, med_p),
          "p90 session: {:,} turns".format(p90_t),
          "worst sessions (turns, preamble -> peak):"] + growth
    ev.append("cost of a session grows ~quadratically in its turn count, "
              "because every turn re-reads all prior turns")

    # Derive the saving from each heavy session's OWN shape rather than a flat
    # multiplier. A session of N turns whose context runs from F to P costs
    # about N*(F+P)/2 in reads. Split it in two at the midpoint and each half
    # restarts at F and ends at M = (F+P)/2, for a total of N*(F+M)/2. So
    #
    #     saving fraction = (P - M) / (F + P) = (P - F) / (2 * (F + P))
    #
    # which is measured per session, not guessed. A comment here previously
    # claimed "roughly three quarters" while the code applied 0.35; neither
    # was derived from anything.
    # The fraction above describes how much CONTEXT RE-READING shrinks, so it
    # may only be applied to the cache-read component. It was applied to
    # cost_units -- reads plus cache writes plus output -- and neither of the
    # other two shrinks when a session is split: the same work produces the
    # same assistant output, just across two sessions, and output is billed at
    # 5x so it carried weight far out of proportion to its token count. Cache
    # writes go slightly the wrong way, since the second session writes one
    # more preamble. Measured over 77 heavy sessions on the reporting machine
    # that was a 1.44x over-statement: 21.1% claimed against 14.6% real.
    #
    # Routed through to_spend() rather than multiplying by PRICE["cache_read"]
    # here, because this detector was the one caller that bypassed the shared
    # helper -- which is exactly the drift its own docstring warns about.
    heavy = sorted(subs, key=lambda s: -s.cost_units)[:max(1, len(subs) // 4)]
    heavy_cost = sum(s.cost_units for s in heavy)
    heavy_share = pct(heavy_cost, fleet.cost_units())
    all_reads = fleet.billed()["cache_read"]
    saved_reads = 0.0
    for h in heavy:
        F, P = float(h.floor or 0), float(h.peak or 0)
        if F > 0 and P > F:
            saved_reads += h.billed["cache_read"] * ((P - F) / (2.0 * (F + P)))
    derived_pct = to_spend(fleet, pct(saved_reads, all_reads)) if all_reads else 0.0
    ev.append("splitting the heaviest {} session(s) in half would cut ~{:,} "
              "cache-read tokens = about {:.0f}% of total spend, from their own "
              "measured shape".format(len(heavy), int(saved_reads), derived_pct))
    ev.append("only re-reading shrinks: the same work produces the same output "
              "across two sessions, and the split adds one preamble write")
    return Finding(
        id="session-length",
        title="Long sessions dominate spend",
        severity=sev, confidence="derived",
        assumption="each heavy session split once at its midpoint; only "
                   "cache reads shrink, output is unchanged and the split "
                   "adds one preamble write, which is not subtracted",
        saving_pct=derived_pct,
        gate=gate,
        evidence=ev,
        actions=[
            "Clear between unrelated tasks. A fresh session restarts at the "
            "preamble; a continued one pays for everything before it, forever.",
            "Use `/compact` at natural boundaries rather than letting context "
            "run to the limit.",
            "Push exploratory reading into subagents — their transcript never "
            "enters the main thread's permanent history. Note this moves cost "
            "rather than removing it: the subagent's own turns are billed, and "
            "`subagent-cost` reports what they came to.",
        ])


def d_bash_chatter(fleet, mach):
    """Many small Bash calls. Compressors cannot help; batching can, and it
    cuts BOTH the command text and the turn count."""
    outs = sorted(fleet.bash_out())
    cmds = fleet.bash_cmds()
    if len(outs) < 100:
        return None
    med = quantile(outs, 0.5)
    p90 = quantile(outs, 0.9)
    cmd_tok = sum(c[0] for c in cmds)
    out_tok = sum(outs)
    combined = cmd_tok + out_tok
    share = pct(combined, fleet.content_total())
    if share < 15 or p90 > 5000:
        return None   # d_bash_bulk owns the fat-output case
    gate = Gate("all", [
        Cond("Bash text share of content", share, 15, unit="%"),
        Cond("Bash output p90, under the bulk cutoff", p90, 5000,
             mode="at_most", unit="tokens"),
    ])

    # How much of the command text is pure ceremony: cd prefixes and absolute
    # path repetition. Measured, not assumed.
    cd_tok = sum(n for n, c in cmds if c.strip().startswith("cd "))
    shapes = [cd_shape(c) for _n, c in cmds]
    n_chained = shapes.count("chained")
    n_standalone = shapes.count("standalone")
    # Both shapes present in quantity = a machine mid-migration from the
    # chained form to the standalone one, which is what a machine paying an
    # approval prompt per chained call does. Recommending it batch them back
    # is recommending the regression.
    converting = n_chained >= 20 and n_standalone >= 0.2 * n_chained
    ev = ["{:,} Bash calls: {:,} tokens of command text + {:,} of output "
          "= {:.1f}% of all content".format(len(outs), cmd_tok, out_tok, share),
          "output size: median {:,}, p90 {:,}, max {:,}".format(
              med, p90, outs[-1] if outs else 0),
          "the commands cost {:.0f}% of what their output costs — "
          "no compressor touches that half".format(pct(cmd_tok, out_tok))]
    if cd_tok:
        ev.append("{:,} tokens are in calls that begin with a `cd` prefix "
                  "({:,} chained, {:,} standalone)".format(
                      cd_tok, n_chained, n_standalone))
    return Finding(
        id="bash-chatter",
        title="Bash cost is call volume, not output size",
        severity="high" if share >= 35 else "medium",
        confidence="estimated",
        assumption="a quarter of Bash calls batchable, 30% off those",
        saving_pct=to_spend(fleet, share) * 0.25 * 0.30,
        gate=gate,
        evidence=ev,
        actions=[
            "Batch related commands into one call (`a && b && c`) — it halves "
            "the turn count as well as the ceremony.",
            # The measurement above stands either way; only the recommendation
            # changes. Silence would throw away a true number the reader wants.
            ("{:,} of those tokens are `cd` ceremony — but you already write "
             "{:,} standalone `cd` calls against {:,} chained ones, so you are "
             "converting that shape deliberately. On a machine that pays an "
             "approval prompt per chained call, standalone is the cheaper "
             "shape and batching it back would be a bad trade."
             .format(cd_tok, n_standalone, n_chained)) if converting else
            ("{:,} tokens are `cd` ceremony that a scoped flag (`git -C`, "
             "`make -C`) removes without a second call.".format(cd_tok)),
            "Do NOT install an output compressor for this profile: at a median "
            "of {:,} tokens there is nothing in the output to compress.".format(med),
            "Prefer Read/Grep over `cat`/`sed -n` where you only need content: "
            "same tokens, but they are excluded from any future compression "
            "and are cheaper to batch.",
        ])


def d_bash_bulk(fleet, mach):
    """The opposite profile: fat Bash outputs, where a compressor genuinely
    pays. Fires only when the distribution supports it."""
    outs = sorted(fleet.bash_out())
    if len(outs) < 30:
        return None
    p90 = quantile(outs, 0.9)
    if p90 < 5000:
        return None
    total = sum(outs)
    top = sorted(outs, reverse=True)
    n = max(1, len(top) // 10)
    conc = pct(sum(top[:n]), total)
    share = pct(total, fleet.content_total())
    rtk = mach["tools"].get("rtk", {}).get("path")
    ev = ["Bash output p90 is {:,} tokens (max {:,}) — there is real bulk here"
          .format(p90, outs[-1]),
          "the top 10% of calls carry {:.0f}% of Bash output".format(conc),
          "Bash output is {:.1f}% of all content".format(share)]
    ev.append("rtk is " + ("installed at " + rtk if rtk else "NOT installed"))
    return Finding(
        id="bash-bulk",
        title="Fat Bash output — a compressor would pay here",
        severity="high" if share >= 30 else "medium",
        confidence="estimated",
        assumption="half the output compressible, 30% off it",
        saving_pct=to_spend(fleet, share) * 0.5 * 0.30,
        gate=Gate("any", [Cond("Bash output p90", p90, 5000, unit="tokens")]),
        evidence=ev,
        actions=[
            "Install rtk (github.com/rtk-ai/rtk): a PreToolUse hook that "
            "compresses Bash output before it reaches context. It preserves "
            "errors, diffs and stack traces by design.",
            "Re-run `ts audit` a week later and compare the Bash block — this "
            "tool measures whether it actually helped.",
        ] if not rtk else [
            "rtk is already installed. Confirm the hook is registered: "
            "`ts doctor --hooks`.",
        ])


def d_output_verbosity(fleet, mach):
    """Output is billed at 5x input, so it punches far above its token count."""
    b = fleet.billed()
    cost = fleet.cost_units()
    if not cost:
        return None
    out_share = pct(b["output"] * 5.0, cost)
    if out_share < 10:
        return None
    turns = fleet.turns() or 1
    per_turn = b["output"] / turns
    return Finding(
        id="output-verbosity",
        title="Output tokens are a large share of spend",
        severity="high" if out_share >= 20 else "medium",
        confidence="estimated",
        assumption="a fifth off output length",
        saving_pct=out_share * 0.20,
        gate=Gate("any", [Cond("output share of cost-weighted spend",
                               out_share, 10, unit="%")]),
        evidence=[
            "{:,} output tokens = {:.1f}% of cost-weighted spend "
            "(output is billed at 5x input)".format(b["output"], out_share),
            "average {:,.0f} output tokens per turn".format(per_turn),
        ],
        actions=[
            "Add a terseness instruction to CLAUDE.md — `ts fixes apply "
            "terse-output` writes one (with a backup, and a revert).",
            "Ask for diffs rather than whole rewritten files.",
        ],
        fix="terse-output")


def d_mcp_schema(fleet, mach):
    """MCP tool definitions sit in the preamble of every turn unless deferred.

    Two different problems share this id and they need different numbers.

    With deferral OFF -- which a custom ANTHROPIC_BASE_URL does silently --
    every tool definition is in every preamble and the cost scales with how
    many tools exist. That is estimable from the tools actually called, which
    is a measured floor on how many are defined.

    With deferral ON the definitions are not in the preamble, so "you have four
    or more servers" costs approximately nothing. Firing on it was firing on an
    install property with no workload input at all, and reporting a hardcoded
    1.0 for it -- a constant, ranked in a list of computed percentages, on
    every machine with four servers, forever. What IS a workload fact is which
    configured servers you never call; that is what it reports now, and it is
    reported as unquantified rather than as a number, because under deferral
    the saving genuinely is close to nothing.
    """
    mcp = mach["mcp"]
    if not mcp.get("readable"):
        return Unevaluated(
            "mcp-schema",
            "MCP configuration could not be read, so this was not checked — "
            "`ts doctor --mcp` names the file")
    n_global = len(mcp["global"])
    n_proj = sum(len(v) for v in mcp["projects"].values())
    configured = set(mcp["global"])
    for names in mcp["projects"].values():
        configured.update(names)
    called = fleet.mcp_servers_called()
    tools = fleet.mcp_tools_called()
    # Configured is a subset: plugin-provided servers never appear there.
    total = max(n_global + n_proj, len(called))
    if total == 0:
        return None
    env = mach["env"]
    tool_search = env.get("ENABLE_TOOL_SEARCH")
    base_url = env.get("ANTHROPIC_BASE_URL")
    # Tool search is on by default in current Claude Code, but a custom base
    # URL turns it off, which is exactly the case a proxy user lands in.
    at_risk = bool(base_url) and tool_search in (None, "", "false", "0", "off")
    unused = sorted(n for n in configured if n not in called)
    if not at_risk and not unused:
        return None

    conds = [Cond("tool deferral off behind a custom base URL", at_risk, None,
                  mode="flag")]
    if unused:
        conds.append(Cond("configured MCP servers never called", len(unused), 1))
    gate = Gate("any", conds)

    ev = ["{} MCP server(s) configured ({} global, {} project-scoped); "
          "{} actually called, over {} distinct tools".format(
              n_global + n_proj, n_global, n_proj, len(called), len(tools))]
    if len(called) > n_global + n_proj:
        ev.append("{} more were called than are configured — plugin-provided "
                  "servers do not appear in the config".format(
                      len(called) - (n_global + n_proj)))
    for proj, names in list(mcp["projects"].items())[:4]:
        ev.append("    {}: {}".format(
            os.path.basename(proj.rstrip("/")), ", ".join(names)))

    if at_risk:
        # Estimate from this machine's own tool surface rather than a constant.
        # Tools CALLED is a floor on tools defined, so this is a floor on the
        # cost too -- said in the assumption rather than dressed up as exact.
        n_defs = max(len(tools), total)
        per_tool = 400
        reads = fleet.billed()["cache_read"]
        defs_cost = n_defs * per_tool * fleet.turns()
        share = pct(defs_cost, reads) if reads else 0.0
        ev.append("ANTHROPIC_BASE_URL is set and ENABLE_TOOL_SEARCH is not — "
                  "in that combination tool definitions are NOT deferred")
        ev.append("{} tool definition(s) at ~{} tokens, re-read on every one of "
                  "{:,} turns = {:,} cache-read tokens ({:.1f}% of all cache "
                  "reads)".format(n_defs, per_tool, fleet.turns(), defs_cost,
                                  share))
        return Finding(
            id="mcp-schema",
            title="Tool deferral is off behind a custom base URL",
            severity="high", confidence="estimated",
            assumption="~%d tokens per tool definition; tools called (%d) is a "
                       "floor on tools defined" % (per_tool, len(tools)),
            saving_pct=to_spend(fleet, share),
            gate=gate,
            evidence=ev,
            actions=[
                "Set ENABLE_TOOL_SEARCH=true — `ts fixes apply tool-search` "
                "writes it into settings.json.",
            ],
            fix="tool-search")

    ev.append("tool deferral is on, so these definitions are NOT in every "
              "preamble — the cost of an unused server is small, and no "
              "number is claimed for it here")
    ev.append("never called in the transcripts read: " + ", ".join(unused[:8])
              + ("" if len(unused) <= 8 else " (+%d more)" % (len(unused) - 8)))
    return Finding(
        id="mcp-schema",
        title="Configured MCP servers you never call",
        severity="low", confidence="derived",
        saving_pct=0.0,
        gate=gate,
        evidence=ev,
        actions=[
            "Remove the servers above from ~/.claude.json if you do not want "
            "them. Under deferral this is hygiene rather than a saving: their "
            "definitions are not loaded up front, only indexed.",
        ])


def d_repeat_reads(fleet, mach):
    """The same bytes read twice into one context. Not the same FILE twice.

    Two classification errors made this ~50x too large on the reporting
    machine, while calling its result "redundant copies".

    Reads were keyed on file_path with `offset` and `limit` dropped, so lines
    1-50 and lines 400-450 counted as two reads of the same thing and all but
    one were charged as duplicates. Measured here, 89% of the (session, file)
    groups it fired on had EVERY read at a different range -- nothing was
    duplicated at all. The detector's own first action already knew this
    ("read once with a wider range instead of several narrow ranges"); the
    measurement did not reflect it.

    Redundancy was also pooled across the whole fleet. A file read in session A
    and again in session B is not a duplicate: session B has no prior copy in
    its context and must read it. That is what produced evidence lines like
    34x for one file -- dozens of sessions each reading it once, correctly.

    Redundancy only means anything inside one context window, so it is counted
    inside one session and on identical ranges. Reading four narrow ranges
    instead of one wide one does waste something -- per-call overhead, re-sent
    headers -- but it is not `t - t//c` and it is not a copy, so it is not
    claimed here. The cross-session pattern is real too, and its advice ("put
    the stable part in memory") is good, but it has different arithmetic and
    wants its own name; it is deliberately not folded in.
    """
    wasted = 0
    worst = {}
    for sess in fleet.sessions:
        for (path, off, lim), (count, tok) in sess.reads.items():
            if count < 2 or tok <= 0:
                continue
            dup = tok - (tok // count)
            if not dup:
                continue
            wasted += dup
            prev = worst.get((path, off, lim)) or [0, 0, 0]
            worst[(path, off, lim)] = [prev[0] + dup, prev[1] + count - 1,
                                       max(prev[2], count)]
    if not wasted:
        return None
    share = pct(wasted, fleet.content_total())
    if share < 1:
        return None
    ranked = sorted(worst.items(), key=lambda kv: -kv[1][0])
    ev = ["{:,} tokens are re-reads of an IDENTICAL range inside a single "
          "session ({:.1f}% of content)".format(wasted, share),
          "counted within one session only, and only where the byte range "
          "matches: a different range is not a copy, and another session has "
          "no prior copy to duplicate"]
    for (path, off, lim), (dup, extra, mx) in ranked[:5]:
        rng = "whole file" if off is None and lim is None else "%s+%s" % (
            off if off is not None else 0, lim if lim is not None else "eof")
        ev.append("    {:>7,} tok  {:>3} extra read(s), worst session {}x  {} [{}]".format(
            dup, extra, mx, path.replace(os.path.expanduser("~"), "~"), rng))
    return Finding(
        id="repeat-reads",
        title="The same range of the same file is read twice in one session",
        severity="medium" if share >= 3 else "low",
        confidence="estimated",
        saving_pct=to_spend(fleet, share),
        gate=Gate("any", [Cond("identical re-read share of content",
                               share, 1, unit="%")]),
        evidence=ev,
        actions=[
            "Read once with a wider range instead of the same range twice. "
            "Reading DIFFERENT ranges is not counted here and is not the "
            "problem this reports.",
        ])


def d_images(fleet, mach):
    """Screenshots are cheap per image but permanent, and they are rarely
    needed after the turn that looked at them."""
    buckets = fleet.buckets()
    img = buckets.get("tool results (images)", 0)
    n = sum(s.images for s in fleet.sessions)
    if not img or n < 10:
        return None
    share = pct(img, fleet.content_total())
    if share < 2:
        return None
    return Finding(
        id="images",
        title="Screenshots accumulate in context",
        severity="low",
        confidence="estimated",
        saving_pct=to_spend(fleet, share) * 0.5,
        gate=Gate("all", [
            Cond("images captured", n, 10),
            Cond("image share of content", share, 2, unit="%"),
        ]),
        evidence=[
            "{} images totalling ~{:,} tokens ({:.1f}% of content)".format(
                n, img, share),
            "an image is billed at (width x height)/750 on every subsequent turn",
        ],
        actions=[
            "Resize before capture where you only need to confirm a layout.",
            "Clear after a browser-heavy stretch rather than carrying the "
            "screenshots into unrelated work.",
        ])


def d_reasoning_cost(fleet, mach):
    """Billed output that is not in the transcript. It is reasoning.

    Claude Code does not persist reasoning. A thinking block is written as
    ``{"type": "thinking", "thinking": "", "signature": "<opaque>"}`` -- the
    field is present and EMPTY. The old `thinking` detector gated on thinking
    reaching 8% of content, so it read a true zero on every real transcript and
    could not fire on any workload however the threshold was set. Its fixture
    wrote thinking blocks WITH text, which is why it looked covered: the
    fixture was richer than the product.

    The cost is real and is observable from the other side. Everything the
    assistant authored and that IS stored -- its text and its tool-call inputs
    -- can be compared against what was billed as output, and the difference is
    reasoning. Output is billed at 5x input, so it is not a rounding error.
    """
    b = fleet.billed()
    billed_out = b["output"]
    cost = fleet.cost_units()
    if not billed_out or not cost:
        return None
    bu = fleet.buckets()
    stored = (bu.get("assistant text", 0) + bu.get("assistant thinking", 0)
              + bu.get("tool call inputs", 0))
    gap = billed_out - stored
    if gap <= 0:
        return None
    gap_share = pct(gap, billed_out)
    # 40%, and the loose threshold is about the estimator rather than the
    # phenomenon. Billed output is exact; stored content is SIZED by the
    # tokenizer, so all estimator error lands on one side of this subtraction.
    # #4 puts the estimator at 28% spread across content classes, and a gap
    # that undercounting stored content by that much could produce is not
    # evidence of anything. 40% clears it in the worst direction.
    if gap_share < 40:
        return None
    share_of_spend = pct(gap * PRICE["output"], cost)
    return Finding(
        id="reasoning-cost",
        title="Most billed output is reasoning that is not in the transcript",
        severity="medium", confidence="estimated",
        assumption="the whole gap is reasoning, and a third of it goes away at "
                   "a lower effort level for routine work",
        saving_pct=share_of_spend * 0.33,
        gate=Gate("any", [Cond("billed output absent from the transcript",
                               gap_share, 40, unit="%")]),
        evidence=[
            "billed output {:,} tokens; stored assistant content {:,} "
            "(text + tool-call inputs)".format(billed_out, stored),
            "{:,} tokens ({:.0f}%) were billed as output and are in no "
            "transcript".format(gap, gap_share),
            "stored thinking is {:,} tokens: Claude Code writes the block with "
            "an empty `thinking` field and an opaque signature, so reasoning is "
            "not recoverable from a transcript at all".format(
                bu.get("assistant thinking", 0)),
            "at {:.0f}x input that is {:.1f}% of cost-weighted spend".format(
                PRICE["output"], share_of_spend),
            "stored side sized with {} — estimator error falls entirely on that "
            "side of the subtraction, which is why the gate is 40% and not "
            "tighter".format(tokenizer_name()),
        ],
        actions=[
            "Lower MAX_THINKING_TOKENS for routine work, or select a lower "
            "effort level when the task does not need deep reasoning.",
            "This counts the same tokens as `output-verbosity` from the other "
            "end. Do not add the two together.",
        ])


def d_attachments(fleet, mach):
    """Content the harness injects, rather than anything a tool returned.

    Four unrelated cost sources with four unrelated fixes, reported as one
    number under one description -- "@-mentions, IDE selections, directory
    listings" -- with one action: reference paths rather than @-mentioning
    whole files. On the machine that reported this, that action addressed 3.8%
    of the figure printed beside it, while 73.6% was the harness's own skill
    and tool listings, which no amount of not-@-mentioning will touch.

    This machine is close to the mirror image -- 36.6% real file content, 34.2%
    listings -- so the old advice was right here and wrong there, from the same
    detector at the same threshold. That is exactly the claim the tool makes
    about itself, and it could not make it while the bucket was one figure.
    """
    buckets = fleet.buckets()
    at = buckets.get("attachments", 0)
    if not at:
        return None
    share = pct(at, fleet.content_total())
    if share < 8:
        return None
    fams = fleet.attach_families()
    types = fleet.attach_types()

    ev = ["attachments/injections ~{:,} tokens ({:.1f}% of content) — injected "
          "by the harness, not returned by a tool call".format(at, share)]
    for fam, tok in sorted(fams.items(), key=lambda kv: -kv[1]):
        members = sorted(((t, n) for t, n in types.items()
                          if attach_family(t) == fam), key=lambda kv: -kv[1])
        detail = ", ".join("%s %.0f%%" % (t, pct(n, at)) for t, n in members[:3])
        ev.append("    {:>5.1f}%  {:<20}  {}".format(pct(tok, at), fam, detail))

    unknown = sorted(((t, n) for t, n in types.items()
                      if attach_family(t) == FAM_OTHER), key=lambda kv: -kv[1])
    advice = {
        FAM_LISTING:
            "{:.0f}% of this is the harness's own skill, tool and agent "
            "listings. They arrive once per session and are then re-read on "
            "every turn after, so the lever is how many skills and MCP servers "
            "are installed — not how you write prompts. `preamble` measures the "
            "same tokens from the other end; do not count them twice.",
        FAM_FILE:
            "{:.0f}% is actual file content. Reference paths rather than "
            "@-mentioning whole files when the agent can read the part it "
            "needs — this is the share that advice applies to.",
        FAM_HOOK:
            "{:.0f}% is hook output. See `hook-output`, which prices it and "
            "says what to do about it.",
        FAM_BOOKKEEPING:
            "{:.0f}% is fixed harness bookkeeping — token reminders, task "
            "reminders, date changes. Named here so nobody hunts it: there is "
            "no setting that removes it and it is not worth your time.",
        FAM_OTHER:
            "{:.0f}% is in attachment types this version does not recognise ("
            + ", ".join(t for t, _n in unknown[:4]) +
            "). Claude Code adds types between releases — if this share is "
            "large, the family map in transcripts.py is behind.",
    }
    actions = [advice[f].format(pct(fams[f], at))
               for f, _n in sorted(fams.items(), key=lambda kv: -kv[1])
               if f in advice and pct(fams[f], at) >= 10]
    if not actions:
        actions = ["No single source dominates this bucket; the family split "
                   "above is the finding."]
    return Finding(
        id="attachments",
        title="Harness-injected content is a large share of content",
        severity="low", confidence="estimated",
        saving_pct=to_spend(fleet, share),
        gate=Gate("any", [Cond("attachment share of content", share, 8, unit="%")]),
        evidence=ev,
        actions=actions)


def d_hook_output(fleet, mach):
    """Hooks that print on success pay for it on every turn after.

    Invisible until the attachments bucket was split by type: pooled with file
    injections, hook chatter read as @-mentions and got @-mention advice. On
    the reporting machine it is 24,389 records at ~51 per session and a median
    of 81 tokens -- roughly 4,100 tokens per session of output nobody reads,
    re-read for the rest of the session.

    This is the shape a detector is supposed to have and most of the catalogue
    does not: a machine with no hooks, or with hooks that stay quiet when they
    succeed, pays exactly zero and hears nothing. This machine is one of those
    -- 375 tokens in total -- so it stays silent here.
    """
    types = fleet.attach_types()
    counts = fleet.attach_counts()
    tok = sum(v for k, v in types.items() if attach_family(k) == FAM_HOOK)
    recs = sum(v for k, v in counts.items() if attach_family(k) == FAM_HOOK)
    if not tok or not recs:
        return None
    n_sess = len(fleet.sessions) or 1
    per_sess = tok / float(n_sess)
    gate = Gate("all", [
        Cond("hook output per session", per_sess, 1000, unit="tokens"),
        Cond("hook records", recs, 20),
    ])
    if per_sess < 1000 or recs < 20:
        return None
    share = pct(tok, fleet.content_total())
    # The removable part is measured, not assumed: hook_success is by name the
    # chatter a hook emits when nothing went wrong. hook_additional_context is
    # doing a job and is left out of the saving.
    removable = types.get("hook_success", 0)
    ev = ["{:,} hook records, ~{:,} tokens ({:.1f}% of content), "
          "{:,.0f} tokens per session".format(recs, tok, share, per_sess),
          "hook output lands in context and is re-read by every later turn in "
          "the session, like anything else there"]
    for t, n in sorted(types.items(), key=lambda kv: -kv[1]):
        if attach_family(t) == FAM_HOOK:
            ev.append("    {:>7,} tok  {:>6,} records  {}".format(
                n, counts.get(t, 0), t))
    if removable:
        ev.append("{:,} of it is `hook_success` — emitted when nothing went "
                  "wrong".format(removable))
    return Finding(
        id="hook-output",
        title="Hooks print on success, and it is re-read all session",
        severity="medium", confidence="estimated",
        assumption="a hook silent on success emits none of its hook_success "
                   "output; other hook output is doing a job and is not counted",
        saving_pct=to_spend(fleet, pct(removable, fleet.content_total())),
        gate=gate,
        evidence=ev,
        actions=[
            "Make hooks print nothing when they succeed. A hook that exits 0 "
            "with no output costs nothing; one that says \"ok\" costs that "
            "message on every turn for the rest of the session.",
            "Where a hook must report, send it to stderr or a log rather than "
            "into the transcript.",
        ])


def d_subagent_cost(fleet, mach):
    """Subagent turns are billed like any other, and are easy to treat as free.

    Delegation genuinely helps the MAIN thread: the subagent's transcript never
    enters it, so it does not inflate the context every later turn re-reads.
    But the subagent runs its own context with its own preamble, and that is
    billed. Until the recursive-discovery fix this tool could not see any of
    it, which meant `session-length` recommended delegating while being
    structurally blind to what delegating cost.
    """
    subs = fleet.subagents()
    if not subs:
        return None
    share = fleet.subagent_cost_share()
    if share < 5:
        return None
    turns = sum(s.turns for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)
    sub_turns = sorted(x.turns for x in subs)
    ev = ["{:,} subagent transcript(s), {:,} turns, {:.1f}% of cost-weighted "
          "spend".format(len(subs), turns, share)]
    if floors:
        ev.append("each carries its own preamble; median {:,} tokens, paid "
                  "again per subagent".format(quantile(floors, 0.5)))

    # Two machines can show the same inherited-preamble share for opposite
    # reasons, and they need opposite advice. Many tiny subagents means the
    # preamble is being paid too often; few long ones means the preamble is
    # simply large. Reported profile: median 39 turns per subagent, 2% at or
    # under 5 -- "batch your small questions" targeted a population that
    # barely existed there.
    short = [t for t in sub_turns if t <= 5]
    short_share = pct(len(short), len(sub_turns))
    inherited = sum((x.floor or 0) * x.turns for x in subs)
    sub_ctx = sum(sum(x.ctx_sizes) for x in subs)
    inherited_share = pct(inherited, sub_ctx)

    ev.append("median {:,} turns per subagent (p90 {:,}); {:.0f}% run 5 turns "
              "or fewer".format(quantile(sub_turns, 0.5),
                                quantile(sub_turns, 0.9), short_share))
    if inherited_share:
        ev.append("the inherited preamble is {:.0f}% of all subagent context"
                  .format(inherited_share))
    ev.append("delegation keeps work out of the MAIN thread's history, which "
              "is real — but the subagent's own turns are billed")

    actions = ["Delegate for context hygiene, not as a saving — measure both "
               "sides before assuming a subagent is cheaper than doing it "
               "inline."]
    if short_share >= 15:
        actions.append(
            "{:.0f}% of your subagents run 5 turns or fewer, and each pays a "
            "full preamble to answer briefly. Batch small questions into one "
            "agent, or ask inline.".format(short_share))
    elif inherited_share >= 20:
        actions.append(
            "Your subagents are not short (median {:,} turns), so batching "
            "them is not the lever. The preamble each one inherits is — it is "
            "{:.0f}% of all subagent context. See `preamble`."
            .format(quantile(sub_turns, 0.5), inherited_share))
    return Finding(
        id="subagent-cost",
        title="Subagent traffic is a large share of spend",
        severity="medium", confidence="derived",
        saving_pct=0.0,
        gate=Gate("any", [Cond("subagent share of cost-weighted spend",
                               share, 5, unit="%")]),
        evidence=ev,
        actions=actions)


DETECTORS = [
    d_preamble,
    d_session_length,
    d_subagent_cost,
    d_bash_chatter,
    d_bash_bulk,
    d_output_verbosity,
    d_mcp_schema,
    d_repeat_reads,
    d_images,
    d_hook_output,
    d_reasoning_cost,
    d_attachments,
]

CATALOGUE = {
    "preamble": "Fixed preamble re-read every turn",
    "session-length": "Long sessions dominate spend",
    "subagent-cost": "Subagent traffic is a large share of spend",
    "bash-chatter": "Bash cost is call volume, not output size",
    "bash-bulk": "Fat Bash output — a compressor would pay",
    "output-verbosity": "Output tokens are a large share of spend",
    "mcp-schema": "MCP tool schemas / deferral",
    "repeat-reads": "Same files read many times",
    "images": "Screenshots accumulate in context",
    "reasoning-cost": "Billed output that is not in the transcript",
    "attachments": "Harness-injected content is a large share of content",
    "hook-output": "Hooks print on success, and it is re-read all session",
}


def evaluate(fleet, mach):
    """(findings, unevaluated).

    A detector that could not run is reported as such rather than dropped. It
    used to vanish into the same silence as one that ran and found nothing --
    including when it raised, where the warning went to stderr and the report
    itself said nothing at all.
    """
    found, blocked = [], []
    for det in DETECTORS:
        try:
            f = det(fleet, mach)
        except Exception as exc:  # a broken detector must not kill the report
            sys.stderr.write("warning: detector %s failed: %s\n"
                             % (getattr(det, "__name__", "?"), exc))
            blocked.append(Unevaluated(getattr(det, "__name__", "?"),
                                       "detector raised %s: %s"
                                       % (type(exc).__name__, exc)))
            continue
        if isinstance(f, Unevaluated):
            blocked.append(f)
        elif f:
            # Detectors set a severity for readability while being written;
            # the report only ever shows the one derived from the number, so
            # the two can never drift apart in front of a reader.
            f.severity = severity_for(f.saving_pct)
            found.append(f)
    found.sort(key=lambda f: f.rank)
    return found, blocked


def run(fleet, mach) -> list:
    """Every detector that fires, ranked by severity then estimated saving.

    Findings only; see evaluate() for the ones that could not be checked.
    """
    return evaluate(fleet, mach)[0]
