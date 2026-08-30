# Contributing

The useful contribution here is usually **a new detector**, because a detector
encodes a cost pattern somebody actually hit. If your machine wastes tokens in
a way this tool cannot see, that gap is the bug.

## Ground rules

1. **Measure, then advise.** A detector reads numbers and either fires with a
   figure attached or stays silent. "Consider being more concise" is not a
   detector; "your output is 13.3% of cost-weighted spend at 897 tokens per
   turn" is.
2. **Never mix exact and estimated numbers.** Billed figures come from the
   provider's usage records and are exact. Content sizes are estimated. A
   report that blurs the two is worse than no report — see the `confidence`
   labels in `advise.py`.
3. **Silence is a valid result.** A detector that fires on every machine is
   decoration. `bash-chatter` and `bash-bulk` are the model: mutually
   exclusive, and one of them tells you *not* to install something.
4. **Degrade, never crash.** A malformed transcript, an unreadable config, a
   missing tool: report and continue. `scripts/verify` checks this.

## Adding a detector

In `scripts/lib/advise.py`:

```python
def d_my_thing(fleet, mach):
    """One line on why this costs money."""
    ...
    if <not worth mentioning>:
        return None
    return Finding(
        id="my-thing",
        title="...",
        severity="medium",          # overwritten by severity_for(); see below
        confidence="measured",      # measured | estimated | heuristic
        saving_pct=to_spend(fleet, share_of_content),
        evidence=[...],             # the numbers that made it fire
        actions=[...],              # what a human should do
        fix=None,                   # id in fixes.py, if scriptable
    )
```

Then add it to `DETECTORS` and to `CATALOGUE` (the catalogue is what
`ts advise --all` lists as "did not fire", so an omission there makes the tool
look like it has fewer opinions than it does).

Notes:

- `saving_pct` is a share of **measured spend**. Use `to_spend()` to convert a
  share of content or of cache reads; do not hand-inline the factor.
- `severity` is recomputed from `saving_pct` by `severity_for()`. The field on
  the dataclass exists for readability while writing; the report never shows a
  severity that disagrees with the number beside it.
- Keep thresholds as literals in the function body. A reader should see why it
  fired without opening a config file.

## Adding a fix

`scripts/lib/fixes.py`. A fix must be all four of:

- **previewable** — `--dry-run` is the default and shows a diff,
- **backed up** — a timestamped copy beside the original,
- **idempotent** — applying twice changes nothing the second time,
- **reversible** — `ts fixes revert <id>` restores the prior state.

If it cannot be all four, it belongs in a Finding's `actions` list, where a
human does it deliberately.

## Tests

```sh
scripts/verify          # must be green before a PR
scripts/verify --keep   # keeps the generated fixtures for inspection
```

Expected values live in `fixtures/EXPECTATIONS.json` and are **derived by
hand**, with the arithmetic written into `_derivation`. Do not paste in
whatever the program printed — that only proves the program is deterministic.

If you add a detector, add a fixture profile that makes it fire and assert that
it stays silent on the profiles where it should not.

## Style

Match the surrounding code. Comments explain *why*, especially where a
threshold or a constant was chosen — the calibration note at the top of
`transcripts.py` is the standard to aim for.
