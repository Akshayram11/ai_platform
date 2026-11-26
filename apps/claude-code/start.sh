#!/bin/bash
# Start the terminal (ttyd), the workbench (one page: terminal + on-demand browser) and the DataOS sign-in proxy in front.
set -euo pipefail
# Home: on the machine's own disk (a DataOS Volume mounted at DAPP_HOME_DIR) when there is one and it is writable, so
# everything survives restarts: code, .git, virtualenvs, the browser's sign-ins. Otherwise under /tmp (lost on restart).
# Either way ~/work and Claude's conversations are also copied to the owner's immortal-obj objects (workspace-sync.py).
PERSISTENT=0
if [ -n "${DAPP_HOME_DIR:-}" ] && mkdir -p "$DAPP_HOME_DIR" 2>/dev/null && [ -w "$DAPP_HOME_DIR" ]; then
  export HOME="$DAPP_HOME_DIR"; PERSISTENT=1
else
  [ -n "${DAPP_HOME_DIR:-}" ] && echo "[start] $DAPP_HOME_DIR is not writable: using /tmp (nothing outside ~/work is kept)" >&2
  export HOME=/tmp/claude-home
fi
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
# The shared browser for Claude: Playwright MCP over CDP on 127.0.0.1:9222. Nothing runs until Claude uses it: the workbench
# starts Chromium on the first connection and stops it after BROWSER_IDLE_MINUTES (default 15) unused.
# The desktop for Claude: screenshots of the whole screen, and the real mouse and keyboard (desktop_mcp.py). The page's
# "Take over" pauses it. Both are registered again on every start (the home may already have them).
for mcp in browser desktop; do claude mcp remove --scope user "$mcp" >/dev/null 2>&1 || true; done
claude mcp add --scope user browser -- playwright-mcp --cdp-endpoint http://127.0.0.1:9222 >/dev/null 2>&1 \
  || echo "[start] could not register the browser MCP for Claude" >&2
claude mcp add --scope user desktop -- python /opt/claude/desktop_mcp.py >/dev/null 2>&1 \
  || echo "[start] could not register the desktop MCP for Claude" >&2
rm -f /tmp/desktop-paused /tmp/desktop-claude-active

# dapp inside the terminal: DataOS gives this pod its owner's identity (DATAOS_RUN_AS_USER/APIKEY); there's no
# dataos-ctl here, so dapp writes through the API.
export DAPP_WRITER=api DAPP_DEFAULT_COMPUTE="${DAPP_DEFAULT_COMPUTE:-engineering-compute}"
export DATAOS_FQDN="${DATAOS_FQDN:?DATAOS_FQDN must be set}"
# Only the owner gets through, whatever the app's access list says: the terminal acts as them.
export DAPP_PROXY_ONLY="${DATAOS_RUN_AS_USER:?DataOS did not set DATAOS_RUN_AS_USER}"
BASE="${ROOT_PATH:-/}"

# Bring back what was saved before the terminal opens; only if that worked, keep saving (a failed restore must not
# overwrite the saved copy with an empty folder).
# On the machine's own disk the restore happens once (the first start, to bring over what was saved before): after that
# the disk is newer than the saved copy, and pulling would overwrite recent work.
SYNC_PID=""
RESTORED="$HOME/.dapp-restored"
if [ "$PERSISTENT" = 1 ] && [ -f "$RESTORED" ]; then
  echo "[start] home is on the machine's disk: nothing to restore" >&2
  python /opt/claude/workspace-sync.py &
  SYNC_PID=$!
elif timeout 180 python /opt/claude/workspace-sync.py pull; then
  [ "$PERSISTENT" = 1 ] && touch "$RESTORED"
  python /opt/claude/workspace-sync.py &
  SYNC_PID=$!
else
  echo "[start] workspace not restored: changes in ~/work are NOT saved until the app restarts" >&2
fi
cp /opt/claude/CLAUDE.md "$WORK/CLAUDE.md"   # always the guide that matches this image
# DataOS stops the pod with SIGTERM: let the sync save one last time before we go.
trap '[ -n "$SYNC_PID" ] && kill -TERM "$SYNC_PID" && wait "$SYNC_PID"; exit 0' TERM INT

ttyd --interface lo --port 7681 --writable --base-path "${BASE%/}/term" \
     -t fontSize=14 -t "titleFixed=Claude Code on DataOS" -t disableLeaveAlert=true \
     /opt/claude/session.sh &
TTYD_PID=$!
python /opt/claude/workbench.py &
WORKBENCH_PID=$!
DAPP_UPSTREAM=http://127.0.0.1:7680 python -m uvicorn dapp_proxy:app --host 0.0.0.0 --port "$PORT" \
     --proxy-headers --log-level warning &
PROXY_PID=$!
# If the terminal, the workbench or the proxy stops, save and stop the container so DataOS restarts everything.
wait -n "$TTYD_PID" "$WORKBENCH_PID" "$PROXY_PID"
[ -n "$SYNC_PID" ] && kill -TERM "$SYNC_PID" && wait "$SYNC_PID"
exit 1
