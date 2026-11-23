#!/usr/bin/env bash
# Remove dapp from this machine.
#
#   ./uninstall.sh            asks first
#   ./uninstall.sh --yes      no questions (for scripts and agents)
#
# Removes:  the `dapp` tool, the source clone this installer made (~/.dapp/src), dapp's saved login
#           (~/.dapp/config.yaml, only exists if you used `dapp setup`), and the 'dapp' entry in Claude Code if present.
# Keeps:    your dataos-ctl login, your own git clone of the repo, uv, and everything you deployed:
#           apps keep running on DataOS. Delete those with `dapp rm <app>` BEFORE uninstalling.
#
# Options:  --yes   --keep-config (keep ~/.dapp/config.yaml)   -h
# Env:      DAPP_HOME (default ~/.dapp)
set -euo pipefail

HOME_DIR="${DAPP_HOME:-$HOME/.dapp}"
PKG="dataos-apps"
YES=0
KEEP_CONFIG=0

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1; shift ;;
    --keep-config) KEEP_CONFIG=1; shift ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[ -x "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
HAVE_TOOL=0
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^$PKG "; then HAVE_TOOL=1; fi
HAVE_SRC=0; [ -d "$HOME_DIR/src" ] && HAVE_SRC=1
HAVE_CFG=0; [ -f "$HOME_DIR/config.yaml" ] && [ "$KEEP_CONFIG" = 0 ] && HAVE_CFG=1
HAVE_MCP=0
if command -v claude >/dev/null 2>&1 && claude mcp get dapp >/dev/null 2>&1; then HAVE_MCP=1; fi

if [ "$HAVE_TOOL$HAVE_SRC$HAVE_CFG$HAVE_MCP" = "0000" ]; then
  say "Nothing to remove: dapp is not installed here."
  exit 0
fi

say "This will remove:"
[ "$HAVE_TOOL" = 1 ] && echo "  - the dapp tool ($(command -v dapp 2>/dev/null || echo 'uv tool'))"
[ "$HAVE_SRC" = 1 ]  && echo "  - $HOME_DIR/src (the clone made by install.sh)"
[ "$HAVE_CFG" = 1 ]  && echo "  - $HOME_DIR/config.yaml (saved DataOS API key)"
[ "$HAVE_MCP" = 1 ]  && echo "  - the 'dapp' MCP server in Claude Code"
echo "  Apps you deployed keep running on DataOS (remove them first with: dapp rm <app>)."

if [ "$YES" != 1 ]; then
  [ -t 0 ] || die "not a terminal: pass --yes to confirm"
  printf 'Continue? [y/N] '; read -r answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 1 ;; esac
fi

if [ "$HAVE_MCP" = 1 ]; then claude mcp remove dapp >/dev/null 2>&1 && ok "removed the Claude Code entry" || warn "could not remove the Claude Code entry"; fi
if [ "$HAVE_TOOL" = 1 ]; then uv tool uninstall "$PKG" >/dev/null && ok "removed the dapp tool"; fi
if [ "$HAVE_SRC" = 1 ]; then rm -rf "$HOME_DIR/src" && ok "removed $HOME_DIR/src"; fi
if [ "$HAVE_CFG" = 1 ]; then rm -f "$HOME_DIR/config.yaml" && ok "removed the saved login"; fi
rmdir "$HOME_DIR" 2>/dev/null || true
echo
say "dapp is removed."
