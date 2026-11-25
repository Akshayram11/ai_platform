#!/bin/bash
# What runs inside the tmux session: Claude, and a shell when it exits.
cd "$HOME/work"
printf '\n  Claude Code on DataOS, as %s (tenant %s).\n  Ask it to build and deploy an app; it uses `dapp`.\n  Refreshing the page reconnects to this same session.\n  Files here are saved to your immortal-obj; the Claude sign-in is lost when DataOS restarts this app, unless it is set up with a token (see dapp.yaml).\n\n' \
  "${DATAOS_RUN_AS_USER:-?}" "${DATAOS_TENANT_ID:-?}"
if ls "$HOME/.claude/projects"/* >/dev/null 2>&1; then
  claude --continue || claude
else
  claude
fi
echo
echo "Claude exited. This is a shell in $HOME/work; type 'claude' to start it again."
exec bash -l
