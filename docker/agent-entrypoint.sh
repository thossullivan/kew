#!/usr/bin/env bash
# kew sandboxed-agent entrypoint — the REFERENCE IMPLEMENTATION of the
# container entrypoint contract.
#
# The contract is specified in docs/sandbox-entrypoint-contract.md (mounted
# checkout, prompt-file->stdin, "$@" extra args, one JSON event per line on
# stdout, reviewer sidecar path). That page is canonical; this script and
# kew.sandbox are the two halves that implement it. Change the page first.
#
# In short: the host writes the prompt to `<checkout>/.kew-prompt.txt` inside
# the already-mounted checkout dir, which appears at
# ${KEW_PROMPT_FILE:-/work/.kew-prompt.txt} in the container. This entrypoint
# reads that file and hands it to `claude --print` via stdin, so the prompt
# never rides in argv (no length limit, not visible in `ps`) — the layer where
# claude previously exited "Input must be provided either through stdin or as a
# prompt argument when using --print" (#695).
set -euo pipefail

PROMPT_FILE="${KEW_PROMPT_FILE:-/work/.kew-prompt.txt}"

if [[ ! -s "$PROMPT_FILE" ]]; then
  echo "ERROR: kew agent prompt file missing or empty at $PROMPT_FILE" >&2
  echo "  The host must write the prompt to <checkout>/.kew-prompt.txt before launch." >&2
  exit 64
fi

# Refuse missing prerequisites before emitting the startup acknowledgement.
if ! AGENT_PROGRAM="$(command -v claude)" || [[ ! -f "$AGENT_PROGRAM" || ! -x "$AGENT_PROGRAM" ]]; then
  echo "ERROR: kew agent executable missing or not executable" >&2
  exit 78
fi

# Git bootstrap (#742): HOME is a fresh per-container tmpfs (no ~/.gitconfig),
# and /work is a host bind mount owned by a different uid than the agent user.
# Without these, the agent's `git commit` dies on "dubious ownership" and
# "Author identity unknown" — work completes but never lands (the kew-721
# incident). Identity is container-local; the host broker owns push/PR.
git config --global --add safe.directory /work
git config --global user.name  "kew-agent"
git config --global user.email "agent@kew.local"

# Extra claude flags (model, --dangerously-skip-permissions, output format) are
# passed by the host as the container command ("$@"); the prompt itself is fed
# on stdin so `--print` reads it.
# The host consumes this attempt acknowledgement before parsing agent events.
printf 'KEW_AGENT_START %s\n' "${KEW_AGENT_START_NONCE:?missing startup nonce}"
exec "$AGENT_PROGRAM" --print "$@" <"$PROMPT_FILE"
