#!/bin/bash
# ttyd runs this for every browser connection. The work itself runs inside one tmux session, so a page refresh,
# a closed tab or a network blip reattaches to the same running Claude instead of starting a new one.
exec tmux -f /opt/claude/tmux.conf new-session -A -s claude -c "$HOME/work" /opt/claude/claude-loop.sh
