---
name: token-save
description: Measure and reduce what an AI coding session costs — where the tokens actually went, which of them were re-read on every turn, and which fixes are worth making on THIS machine. Use when someone asks why their usage is high, how to cut token or API cost, whether a compression tool or proxy is worth installing, what is filling the context window, or why they hit a rate limit. Trigger phrases: "token usage", "reduce cost", "why is this so expensive", "context is full", "compress context", "save tokens", "hitting limits", "is <tool> worth it", "what is eating my context".
---

# Measuring and reducing session cost

Use the `ts` command. It reads the user's own transcripts and answers from
their numbers, not from general advice.

**Do not answer these questions from memory.** Token-saving advice that is not
grounded in a measurement is almost always wrong for the person asking: the
same symptom ("my usage is high") has opposite fixes depending on whether the
cost sits in long sessions, fat tool output, or a bloated preamble. Run the
tool first.

## Finding the command

```sh
TS="${CLAUDE_PLUGIN_ROOT:-$PWD}/scripts/ts"    # installed as a plugin, or a clone
"$TS" audit
```

`CLAUDE_PLUGIN_ROOT` is set when this is installed as a plugin. From a plain
clone, use the checkout's own path. Every example below writes `ts` for
readability; run `"$TS"`.

## The one idea

A token is not billed once. It is billed on **every turn that re-reads it**.

```
cost of a token  ~=  its size  x  the number of turns that re-read it
```

`ts audit` measures that multiplier and calls it **amplification**. On a real
machine it is commonly 100-700x. This is why the usual advice ("compress your
tool output") is often close to worthless: shrinking content matters far less
than shortening the window the content lives in.

Every recommendation `ts advise` makes is ranked by this, not by raw size.

## Commands

| Question | Command |
|---|---|
| What is this session costing right now? | `ts now` |
| ...as a statusline fragment | `ts now --statusline` |
| Where did my tokens go? | `ts audit` |
| ...for one project only | `ts audit --project '*myrepo*'` |
| ...the heaviest sessions | `ts audit --sessions` |
| What is installed and configured here? | `ts doctor` |
| Just the preamble sources | `ts doctor --memory` |
| What should I actually change? | `ts advise` |
| ...including what did NOT fire | `ts advise --all` |
| What can be changed mechanically? | `ts fixes list` |
| Share my numbers with someone safely | `ts share` |
| Preview one | `ts fixes show terse-output` |
| Apply it (backs up, reversible) | `ts fixes apply terse-output --yes` |
| Undo it | `ts fixes revert terse-output` |

Add `--json` to `audit`, `advise` and `doctor` when you need to compute on the
output rather than show it.

## When the user asks about the session they are in

Use `ts now`, not `ts audit`. It reports the current context, the growth rate,
and how many turns until clearing pays for itself.

Report the break-even plainly and then stop. It is arithmetic on size, and it
cannot know whether the context is still needed — say that, and leave the
decision with the user rather than telling them to clear.

## How to use it in a conversation

1. **Run `ts audit` first.** Report the amplification factor and the two or
   three largest content buckets. These frame everything else.
2. **Run `ts advise`.** Present findings in the order given — they are sorted
   by estimated share of spend, and the severity label is derived from that
   number, so the order is the recommendation.
3. **Quote the evidence lines.** Each finding carries the measurements that
   made it fire. A user is far more likely to act on "your median session is
   260 turns and peaks at 254K" than on "consider clearing more often".
4. **Respect the confidence labels, and repeat the assumption.** The label
   qualifies the *saving*, not the evidence. `derived` means the projection
   follows from the machine's own measured shape; `estimated` means measured
   inputs plus a stated assumption; `heuristic` means the direction is right
   and the magnitude is not. Every non-derived finding prints an `assuming:`
   line — quote it. A projection repeated without its assumption becomes a
   measurement in the reader's head, which is the failure this tool exists to
   catch.
5. **Offer `ts fixes` only where a finding names one.** Everything else is
   behavioural on purpose — no script can shorten someone's sessions.

## Reading the output honestly

- **Do not add the percentages together.** Findings overlap; the report says so
  and prints the largest single item rather than a sum.
- **The tokenizer is named in every report.** Without `tiktoken` installed the
  CONTENT column is `len/3.6`, which is within ~1.5% in aggregate but is still
  an estimate. The BILLED column is always exact — it is read from the usage
  records the provider wrote.
- **`ts advise` firing nothing is a real result**, not an empty report. Say so
  rather than inventing advice.

## When asked whether a third-party tool is worth installing

Run `ts advise` and look for `bash-bulk` versus `bash-chatter`. They are
mutually exclusive by construction and they answer exactly this question:

- **`bash-bulk` fired** — there is real bulk in the Bash output. A compressor
  such as [rtk](https://github.com/rtk-ai/rtk) will pay. Recommend it, then
  re-run `ts audit` a week later to check that it did.
- **`bash-chatter` fired** — the cost is call *volume*, and the outputs are too
  small to compress. Say plainly that a compressor will not help here, and
  point at batching instead.
- **Neither fired** — Bash is not where the money is. Look further up the list.

`ts doctor --tools` reports whether such tools are installed. Presence is not a
recommendation; only the measurement is.

## Setup

```sh
scripts/bootstrap --check     # report what is present, install nothing
scripts/bootstrap             # offer to install tiktoken (optional)
scripts/verify                # 12 checks against fixtures with known answers
```

`python3` (3.9+) is the only hard requirement. Everything else degrades: a
missing tokenizer becomes an estimate, a missing tool becomes an `absent` row.

## Where transcripts come from

`ts` reads `~/.claude/projects/*/*.jsonl`. Override with `--root` or
`TS_TRANSCRIPT_DIR`. It never sends anything anywhere — all analysis is local,
and the tool has no network code at all.
