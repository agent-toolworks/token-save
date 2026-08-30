#!/usr/bin/env bash
# A drop-in statusline segment: context size, growth rate, and the clear
# break-even. Paste the marked block into your own statusline script, or use
# this file directly as a minimal statusline:
#
#     "statusLine": { "type": "command",
#                     "command": "~/.claude/statusline-snippet.sh" }
#
# It prints, for example:
#
#     423K +530/t clear>1t
#
# meaning: 423K of context, growing 530 tokens a turn, and clearing now pays
# for itself within about one turn. See `ts now` for the full reading and the
# arithmetic behind the break-even.
#
# Everything below is written to be safe inside a prompt: it is bounded, it
# never writes to stderr, and it exits 0 no matter what. A statusline that can
# fail breaks the shell it lives in, so this prints nothing rather than
# anything alarming.

# ---- BEGIN token-save segment ---------------------------------------------
# Requires: the session JSON on stdin (Claude Code supplies it).

ts_segment() {
  local input=$1

  # transcript_path tells us which session to read. Guessing the newest file is
  # wrong the moment two sessions are open, so no transcript means no segment.
  local transcript
  transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)
  [[ -n $transcript && -f $transcript ]] || return 0

  local ts_bin
  ts_bin=${TS_BIN:-$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/agent-toolworks/token-save/*/scripts/ts 2>/dev/null | sort -V | tail -1)}
  [[ -n $ts_bin && -x $ts_bin ]] || return 0

  # timeout(1) is GNU coreutils and absent from a stock macOS; perl's alarm is
  # everywhere. Without a bound, one wedged call stalls every prompt render.
  local runner
  if command -v timeout >/dev/null 2>&1; then runner=(timeout 2)
  elif command -v gtimeout >/dev/null 2>&1; then runner=(gtimeout 2)
  else runner=(perl -e 'my $s = shift; alarm $s; exec @ARGV or exit 127') runner+=(2)
  fi

  local out
  out=$("${runner[@]}" "$ts_bin" now --statusline --session "$transcript" 2>/dev/null) || return 0
  [[ -n $out ]] && printf '%s' "$out"
}
# ---- END token-save segment -----------------------------------------------

# Standalone use: read stdin once and print just this segment.
if [[ ${BASH_SOURCE[0]} == "${0}" ]]; then
  ts_segment "$(cat)"
fi
