#!/usr/bin/env bash
# kew sandboxed-agent entrypoint for the OpenCode runner image.
#
# Implements the container entrypoint contract in
# docs/sandbox-entrypoint-contract.md (mounted checkout, prompt-file->stdin,
# "$@" extra args, one JSON event per line on stdout, reviewer sidecar path).
# The page is canonical; the reference implementation is
# docker/agent-entrypoint.sh — this file is the opencode variant and must stay
# behaviourally parallel to it.
#
# "$@" arrives from OpenCodeRunner.container_args(): typically
#   run --format json -m <already-routed model>
# so the exec line below becomes
#   opencode run --format json -m <model>   < /work/.kew-prompt.txt
# `opencode run` reads the prompt from stdin when no positional message is
# given — the prompt never rides in argv (contract element 2).
#
# Auth (contract: environment variables only, declared on the runner):
# OPENROUTER_API_KEY is forwarded by name by the host per
# OpenCodeRunner.required_credentials (#1146). No credential file, no CLI
# login state, nothing baked into a layer.
set -euo pipefail

PROMPT_FILE="${KEW_PROMPT_FILE:-/work/.kew-prompt.txt}"

if [[ ! -s "$PROMPT_FILE" ]]; then
  echo "ERROR: kew agent prompt file missing or empty at $PROMPT_FILE" >&2
  echo "  The host must write the prompt to <checkout>/.kew-prompt.txt before launch." >&2
  exit 64
fi

# Refuse missing prerequisites before emitting the startup acknowledgement.
if ! AGENT_PROGRAM="$(command -v opencode)" || [[ ! -f "$AGENT_PROGRAM" || ! -x "$AGENT_PROGRAM" ]]; then
  echo "ERROR: kew agent executable missing or not executable" >&2
  exit 78
fi

# The sandbox mounts a fresh HOME over the image's home directory. Seed the
# OpenCode config there from the image asset so a model added after the pinned
# CLI's catalog remains selectable on every run.
OPENCODE_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
mkdir -p "$OPENCODE_CONFIG_DIR"
cp /usr/local/share/kew/opencode.json "$OPENCODE_CONFIG_DIR/opencode.json"

# OpenCode can fail with a generic server error when a model is absent from its
# local catalog. Check the catalog before handing it a prompt, so a stale image
# fails closed with an actionable model-specific diagnostic and no model call.
OPENCODE_MODEL=""
for ((arg_index = 1; arg_index <= $#; arg_index++)); do
  current_arg="${!arg_index}"
  case "$current_arg" in
    -m|--model)
      next_index=$((arg_index + 1))
      if (( next_index <= $# )); then
        OPENCODE_MODEL="${!next_index}"
      fi
      ;;
    --model=*)
      OPENCODE_MODEL="${current_arg#*=}"
      ;;
  esac
done

if [[ "$OPENCODE_MODEL" == openrouter/* ]]; then
  if ! OPENCODE_MODELS="$("$AGENT_PROGRAM" models openrouter --pure 2>/dev/null)"; then
    echo "ERROR: OpenCode catalog probe failed for '$OPENCODE_MODEL'; refusing dispatch before model traffic." >&2
    exit 78
  fi
  # `--pure` prints one fully-qualified "openrouter/<model-id>" per line, so the
  # probe matches whole lines: a substring match would accept a truncated id
  # (openrouter/deepseek/deepseek-v4-flash-073) that OpenCode cannot resolve.
  if ! grep -Fxq -- "$OPENCODE_MODEL" <<<"$OPENCODE_MODELS"; then
    echo "ERROR: OpenCode catalog gap: '$OPENCODE_MODEL' is not resolvable in this image; update docker/opencode.json." >&2
    exit 78
  fi
fi

# Git bootstrap (#742): HOME is a fresh per-container tmpfs (no ~/.gitconfig),
# and /work is a host bind mount owned by a different uid than the agent user.
# Identity is container-local; the host broker owns push/PR.
git config --global --add safe.directory /work
git config --global user.name  "kew-agent"
git config --global user.email "agent@kew.local"

# Runner flags (subcommand, output format, model) are passed by the host as
# the container command ("$@"); the prompt itself is fed on stdin.
# The host consumes this attempt acknowledgement before parsing agent events.
printf 'KEW_AGENT_START %s\n' "${KEW_AGENT_START_NONCE:?missing startup nonce}"
exec "$AGENT_PROGRAM" "$@" <"$PROMPT_FILE"
