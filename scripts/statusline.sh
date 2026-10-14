#!/usr/bin/env bash
# The whole Claude Code statusline, and a local history of what it showed (#41).
# Copy this file to ~/.claude/ once and point the statusLine setting at it:
#
#     "statusLine": { "type": "command",
#                     "command": "~/.claude/statusline.sh" }
#
# It prints, for example:
#
#     ~/git/…/repo main | Opus 5.5 (high) | ctx 42k/200k 21% | 423.0K +530/t clear>1t | $1.23 | 5h 23% used(2h14m) · 7d 9% used(3d11h)
#
# and records each change in ~/.claude/token-save/history.db. The guide is
# docs/statusline.md: what each segment means, what a row holds, how to query
# it, and how to turn recording off.
#
# The copy does not go stale on an update: all it does is find the newest
# installed `ts` and hand it the session JSON, so the rendering and recording
# come from whatever version is installed. Like statusline-snippet.sh it is
# bounded, never writes to stderr and exits 0 no matter what, which means a
# setup problem shows as an empty line. When it does, run
#
#     echo '{"transcript_path":"/path/to/session.jsonl"}' \
#       | ~/.claude/statusline.sh --explain
#
# which says what it found, what it would record and where, and writes nothing.
#
# Override: TS_BIN=/path/to/scripts/ts skips the plugin-cache lookup.

# The newest installed `ts`, under ANY marketplace name -- the same lookup as
# statusline-snippet.sh, which explains it (#38). Copied rather than sourced:
# this file lives outside the plugin, and must keep working on its own.
ts_resolve_bin() {
  if [[ -n ${TS_BIN:-} ]]; then printf '%s' "$TS_BIN"; return 0; fi
  local cand v
  for cand in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/token-save/*/scripts/ts; do
    [[ -x $cand ]] || continue
    v=${cand%/scripts/ts}; v=${v##*/}
    printf '%s\t%s\n' "$v" "$cand"
  done 2>/dev/null | sort -t "$(printf '\t')" -k1,1V 2>/dev/null | tail -1 | cut -f2
}

ts_bin=$(ts_resolve_bin)

if [[ ${1:-} == --explain ]]; then
  if [[ -n ${TS_BIN:-} ]]; then
    echo "override:   TS_BIN"
  else
    echo "searched:   ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/*/token-save/*/scripts/ts"
  fi
  if [[ -z $ts_bin ]]; then
    echo "ts:         none found -- install the plugin, or set TS_BIN"
  elif [[ ! -x $ts_bin ]]; then
    echo "ts:         $ts_bin is not executable"
  else
    echo "ts:         $ts_bin ($("$ts_bin" version 2>/dev/null))"
    "$ts_bin" statusline --explain 2>&1
  fi
  exit 0
fi

[[ -n $ts_bin && -x $ts_bin ]] || exit 0

# timeout(1) is GNU coreutils and absent from a stock macOS; perl's alarm is
# everywhere. `ts statusline` bounds each of its own steps well inside this,
# so the outer bound only fires if something is badly wrong.
if command -v timeout >/dev/null 2>&1; then runner=(timeout 3)
elif command -v gtimeout >/dev/null 2>&1; then runner=(gtimeout 3)
else runner=(perl -e 'my $s = shift; alarm $s; exec @ARGV or exit 127' 3)
fi

"${runner[@]}" "$ts_bin" statusline 2>/dev/null
exit 0
