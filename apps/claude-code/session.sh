#!/bin/bash
# One browser tab = one terminal session (ttyd starts this for each connection).
cd "$HOME/work"
printf '\n  Claude Code on DataOS, as %s (tenant %s).\n  Ask it to build and deploy an app; it uses `dapp`.\n  Files here are lost when this app restarts: deployed apps keep their own copy.\n  First time (or after a restart): Claude asks you to sign in with your Claude account, or type /login.\n\n' \
  "${DATAOS_RUN_AS_USER:-?}" "${DATAOS_TENANT_ID:-?}"
if ls "$HOME/.claude/projects"/* >/dev/null 2>&1; then
  claude --continue || claude
else
  claude
fi
echo
echo "Claude exited. This is a shell in $HOME/work; type 'claude' to start it again."
exec bash -l
