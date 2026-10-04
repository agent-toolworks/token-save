#!/usr/bin/env bash
# A drop-in statusline segment: context size, growth rate, and the clear
# break-even. Paste the marked block into your own statusline script, or copy
# this file to ~/.claude/ once and use it directly as a minimal statusline:
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
# anything alarming. The price is that a setup problem looks exactly like
# "nothing to report" -- so when the segment stays empty, run
#
#     echo '{"transcript_path":"/path/to/session.jsonl"}' \
#       | ~/.claude/statusline-snippet.sh --explain
#
# which says which step came up empty, on stdout, and changes nothing.

# ---- BEGIN token-save segment ---------------------------------------------
# Requires: bash and the session JSON on stdin (Claude Code supplies it).
# Override: TS_BIN=/path/to/scripts/ts skips the plugin-cache lookup.

# The newest installed `ts`, under ANY marketplace name. The plugin cache is
# <config>/plugins/cache/<marketplace>/token-save/<version>/, and the
# marketplace is whatever the user added: `agent-toolworks` through the org
# catalogue, `token-save-fleet` through this repo directly (#38). Versions are
# compared on the version directory alone -- sorting whole paths would rank by
# marketplace name first -- and old versions stay in the cache after an
# update, so the highest one wins rather than the first one found.
ts_resolve_bin() {
  if [[ -n ${TS_BIN:-} ]]; then printf '%s' "$TS_BIN"; return 0; fi
  local cand v
  for cand in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/token-save/*/scripts/ts; do
    [[ -x $cand ]] || continue
    v=${cand%/scripts/ts}; v=${v##*/}
    printf '%s\t%s\n' "$v" "$cand"
  done 2>/dev/null | sort -t "$(printf '\t')" -k1,1V 2>/dev/null | tail -1 | cut -f2
}

# transcript_path tells us which session to read. Pulled out with bash alone:
# this used jq, which nothing ever declared, and without jq the segment was
# silently empty for good (#38). A path JSON would have to escape (a quote or
# a backslash) does not match, and fails the -f test below -- empty, not wrong.
ts_transcript_path() {
  local re='"transcript_path"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
  [[ $1 =~ $re ]] && printf '%s' "${BASH_REMATCH[1]}"
}

ts_segment() {
  local input=$1

  # Guessing the newest file is wrong the moment two sessions are open, so no
  # transcript means no segment.
  local transcript
  transcript=$(ts_transcript_path "$input")
  [[ -n $transcript && -f $transcript ]] || return 0

  local ts_bin
  ts_bin=$(ts_resolve_bin)
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

# --explain: walk the same steps ts_segment takes and say which came up empty.
# Outside the marked block on purpose -- it is for a person debugging once,
# not for a prompt.
ts_explain() {
  local input=$1 cfg=${CLAUDE_CONFIG_DIR:-$HOME/.claude} transcript ts_bin
  transcript=$(ts_transcript_path "$input")
  if [[ -z $input ]]; then
    echo "stdin:      empty -- pipe in the session JSON (it must carry transcript_path)"
  elif [[ -z $transcript ]]; then
    echo "stdin:      no transcript_path found in the JSON given"
  elif [[ ! -f $transcript ]]; then
    echo "transcript: $transcript does not exist"
  else
    echo "transcript: $transcript"
  fi
  if [[ -n ${TS_BIN:-} ]]; then
    echo "ts:         TS_BIN=$TS_BIN"
  else
    echo "searched:   $cfg/plugins/cache/*/token-save/*/scripts/ts"
  fi
  ts_bin=$(ts_resolve_bin)
  if [[ -z $ts_bin ]]; then
    echo "ts:         none found -- install the plugin, or set TS_BIN"
  elif [[ ! -x $ts_bin ]]; then
    echo "ts:         $ts_bin is not executable"
  else
    echo "ts:         $ts_bin"
  fi
  if [[ -n $transcript && -f $transcript && -n $ts_bin && -x $ts_bin ]]; then
    local out
    out=$(ts_segment "$input")
    if [[ -n $out ]]; then
      echo "segment:    $out"
    else
      echo "segment:    empty -- try: $ts_bin now --session $transcript"
    fi
  fi
}

# Standalone use: read stdin once and print just this segment.
if [[ ${BASH_SOURCE[0]} == "${0}" ]]; then
  if [[ ${1:-} == --explain ]]; then
    ts_explain "$(cat)"
  else
    ts_segment "$(cat)"
  fi
  exit 0
fi
