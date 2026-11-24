#!/bin/bash
# Start the terminal (ttyd, localhost only) and the DataOS sign-in proxy in front of it.
set -euo pipefail
# DataOS may run us as a user without a writable home: keep everything under /tmp (lost on restart).
export HOME=/tmp/claude-home
WORK="$HOME/work"
mkdir -p "$WORK" "$HOME/.claude"
chmod 700 "$HOME"

# Two ways to reach Claude:
#  - ANTHROPIC_API_KEY in the app's secret: Claude Code uses it (via apiKeyHelper, so the terminal's `env` doesn't
#    show it);
#  - no key: sign in with your Claude account inside the terminal (`/login`). The login is kept under $HOME, which
#    lives in /tmp: after the app restarts you sign in again.
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
# Trust the workspace up front. With an API key, skip the first-run screens too; without one, keep them: they
# end with the Claude sign-in. An existing file is kept (it holds the sign-in).
ONBOARDED=false
[ -s "$HOME/.anthropic-key" ] && ONBOARDED=true
[ -f "$HOME/.claude.json" ] || cat > "$HOME/.claude.json" <<JSON
{"hasCompletedOnboarding": $ONBOARDED, "projects": {"$WORK": {"hasTrustDialogAccepted": true}}}
JSON
[ -f "$WORK/CLAUDE.md" ] || cp /opt/claude/CLAUDE.md "$WORK/CLAUDE.md"
umask 022

# dapp inside the terminal: DataOS gives this pod its owner's identity (DATAOS_RUN_AS_USER/APIKEY); there's no
# dataos-ctl here, so dapp writes through the API.
export DAPP_WRITER=api DAPP_DEFAULT_COMPUTE="${DAPP_DEFAULT_COMPUTE:-engineering-compute}"
export DATAOS_FQDN="${DATAOS_FQDN:?DATAOS_FQDN must be set}"
# Only the owner gets through, whatever the app's access list says: the terminal acts as them.
export DAPP_PROXY_ONLY="${DATAOS_RUN_AS_USER:?DataOS did not set DATAOS_RUN_AS_USER}"
BASE="${ROOT_PATH:-/}"

ttyd --interface lo --port 7681 --writable --base-path "$BASE" \
     -t fontSize=14 -t "titleFixed=Claude Code on DataOS" -t disableLeaveAlert=true \
     /opt/claude/session.sh &
DAPP_UPSTREAM=http://127.0.0.1:7681 python -m uvicorn dapp_proxy:app --host 0.0.0.0 --port "$PORT" \
     --proxy-headers --log-level warning &
# If either stops, stop the container so DataOS restarts both.
wait -n
exit 1
