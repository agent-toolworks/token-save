---
name: A detector is wrong for my machine
about: The measurement may be right, but the advice is wrong here
labels: detector
---

<!-- This is the most valuable report this project takes, and it has its own
     template because it is not quite a bug.

     The design claim is that the same catalogue should give different machines
     different — sometimes opposite — advice. Every time that fails, it fails
     on an axis the tool cannot see from a transcript. Naming that axis is the
     whole contribution. -->

## Which detector

<!-- e.g. bash-chatter, subagent-cost -->

## What it told you

```

```

## Why it is wrong here

<!-- What is true about your machine that the tool has no view of? Past
     examples: approval-prompt friction made batching a bad trade; long-running
     subagents made "batch small questions" target a population that did not
     exist. -->

## Is the measurement wrong too, or only the advice?

<!-- These get fixed differently. A wrong number is a bug; a right number with
     wrong advice needs a gate, and a gate needs a signal. -->

## Can you suggest a signal?

<!-- Something visible in the transcripts that separates your machine from one
     where the advice is correct. If you have a candidate, please measure it
     against your own data before proposing it — two candidates have already
     died that way, and both would have shipped as gates that fired everywhere
     while appearing to discriminate. -->

## Version and profile

```
ts version
```

<details>
<summary><code>ts share --show</code></summary>

```json

```
</details>
