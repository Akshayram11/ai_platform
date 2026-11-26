#!/bin/bash
# Start the terminal (ttyd, localhost only) and the DataOS sign-in proxy in front of it.
set -euo pipefail
# DataOS may run us as a user without a writable home: keep everything under /tmp. ~/work and Claude's
# conversations are saved to the owner's immortal-obj objects and restored on start (workspace-sync.py).
export HOME=/tmp/claude-home
WORK="$HOME/work"
mkdir -p "$WORK" "$HOME/.claude"
chmod 700 "$HOME"

# Ways to reach Claude:
#  - CLAUDE_CODE_OAUTH_TOKEN in the app's secret (from `claude setup-token`, valid for a year): you stay signed in
#    through restarts and pod moves;
#  - ANTHROPIC_API_KEY in the app's secret: Claude Code uses it (via apiKeyHelper, so the terminal's `env` doesn't
#    show it);
#  - neither: sign in with your Claude account inside the terminal (`/login`). That login is kept under $HOME, which
#    lives in /tmp: whenever DataOS restarts or moves the pod you sign in again.
umask 077
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  printf '%s' "$ANTHROPIC_API_KEY" > "$HOME/.anthropic-key"
  cat > "$HOME/.claude/settings.json" <<JSON
{"apiKeyHelper": "cat $HOME/.anthropic-key", "env": {"DISABLE_AUTOUPDATER": "1"}}
JSON
else
  [ -f "$HOME/.claude/settings.json" ] || echo '{"env": {"DISABLE_AUTOUPDATER": "1"}}' > "$HOME/.claude/settings.json"
fi
unset ANTHROPIC_API_KEY
# Trust the workspace up front. With an API key or token, skip the first-run screens too; without one, keep them: they
# end with the Claude sign-in. An existing file is kept (it holds the sign-in).
ONBOARDED=false
{ [ -s "$HOME/.anthropic-key" ] || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; } && ONBOARDED=true
[ -f "$HOME/.claude.json" ] || cat > "$HOME/.claude.json" <<JSON
{"hasCompletedOnboarding": $ONBOARDED, "projects": {"$WORK": {"hasTrustDialogAccepted": true}}}
JSON
umask 022

# dapp inside the terminal: DataOS gives this pod its owner's identity (DATAOS_RUN_AS_USER/APIKEY); there's no
# dataos-ctl here, so dapp writes through the API.
export DAPP_WRITER=api DAPP_DEFAULT_COMPUTE="${DAPP_DEFAULT_COMPUTE:-engineering-compute}"
export DATAOS_FQDN="${DATAOS_FQDN:?DATAOS_FQDN must be set}"
# Only the owner gets through, whatever the app's access list says: the terminal acts as them.
export DAPP_PROXY_ONLY="${DATAOS_RUN_AS_USER:?DataOS did not set DATAOS_RUN_AS_USER}"
BASE="${ROOT_PATH:-/}"

# Bring back what was saved before the terminal opens; only if that worked, keep saving (a failed restore must not
# overwrite the saved copy with an empty folder).
SYNC_PID=""
if timeout 180 python /opt/claude/workspace-sync.py pull; then
  python /opt/claude/workspace-sync.py &
  SYNC_PID=$!
else
  echo "[start] workspace not restored: changes in ~/work are NOT saved until the app restarts" >&2
fi
cp /opt/claude/CLAUDE.md "$WORK/CLAUDE.md"   # always the guide that matches this image
# DataOS stops the pod with SIGTERM: let the sync save one last time before we go.
trap '[ -n "$SYNC_PID" ] && kill -TERM "$SYNC_PID" && wait "$SYNC_PID"; exit 0' TERM INT

ttyd --interface lo --port 7681 --writable --base-path "$BASE" \
     -t fontSize=14 -t "titleFixed=Claude Code on DataOS" -t disableLeaveAlert=true \
     /opt/claude/session.sh &
TTYD_PID=$!
DAPP_UPSTREAM=http://127.0.0.1:7681 python -m uvicorn dapp_proxy:app --host 0.0.0.0 --port "$PORT" \
     --proxy-headers --log-level warning &
PROXY_PID=$!
# If the terminal or the proxy stops, save and stop the container so DataOS restarts everything.
wait -n "$TTYD_PID" "$PROXY_PID"
[ -n "$SYNC_PID" ] && kill -TERM "$SYNC_PID" && wait "$SYNC_PID"
exit 1
