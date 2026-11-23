#!/usr/bin/env bash
# Install dapp, the CLI for deploying apps on DataOS, on this machine (macOS or Linux).
#
#   From a clone:      ./install.sh
#   From anywhere:     git clone git@github.com:Akshayram11/ai_platform.git && ./ai_platform/install.sh
#
# What it does, and only this:
#   1. makes sure `uv` (a Python tool installer) is available; installs it if missing, without touching your shell files
#   2. gets the dapp source (this folder, or a clone in ~/.dapp/src)
#   3. installs `dapp` as an isolated tool: it never touches your system Python or other projects
#   4. checks dataos-ctl and your DataOS login with `dapp doctor`
# It does NOT edit your shell rc files, log you in, or deploy anything. Remove it with ./uninstall.sh.
#
# Options:   --ref <branch|tag>   --mcp (register dapp with Claude Code)   --no-doctor   -h
# Env:       DAPP_REPO (git url)  DAPP_REF  DAPP_HOME (default ~/.dapp)  DAPP_NO_UV_INSTALL=1
set -euo pipefail

ORIG_PATH="$PATH"   # what the user's terminals will have, before this script adds anything
REPO="${DAPP_REPO:-https://github.com/Akshayram11/ai_platform.git}"
REF="${DAPP_REF:-main}"
HOME_DIR="${DAPP_HOME:-$HOME/.dapp}"
PKG="dataos-apps"
PY="3.12"
WITH_MCP=0
RUN_DOCTOR=1

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF="$2"; shift 2 ;;
    --mcp) WITH_MCP=1; shift ;;
    --no-doctor) RUN_DOCTOR=0; shift ;;
    -h|--help) sed -n '2,19p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

case "$(uname -s)" in Darwin|Linux) ;; *) die "supported on macOS and Linux only (found $(uname -s))" ;; esac

# ---- 1. uv -------------------------------------------------------------------------------------------------------
say "Checking uv"
if ! command -v uv >/dev/null 2>&1; then
  [ -x "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  [ "${DAPP_NO_UV_INSTALL:-0}" = 1 ] && die "uv is not installed and DAPP_NO_UV_INSTALL=1. Install it: https://docs.astral.sh/uv/"
  command -v curl >/dev/null 2>&1 || die "curl is needed to install uv"
  warn "uv not found: installing it with its official installer (astral.sh), without editing your shell files"
  curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installed but not found on PATH; open a new terminal and run this again"
fi
ok "$(uv --version)"

# ---- 2. source ---------------------------------------------------------------------------------------------------
say "Getting the dapp source"
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [ -n "$HERE" ] && [ -f "$HERE/pyproject.toml" ] && grep -q "^name = \"$PKG\"" "$HERE/pyproject.toml"; then
  SRC="$HERE"
  ok "using this checkout: $SRC"
else
  command -v git >/dev/null 2>&1 || die "git is needed (on macOS: xcode-select --install)"
  SRC="$HOME_DIR/src"
  mkdir -p "$HOME_DIR"
  if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --quiet --depth 1 origin "$REF" && git -C "$SRC" checkout --quiet FETCH_HEAD \
      || die "could not update $SRC from $REPO"
    ok "updated $SRC ($REF)"
  else
    git clone --quiet --depth 1 --branch "$REF" "$REPO" "$SRC" 2>/dev/null || die "could not clone $REPO
  The repository is private. Make sure you have access and are signed in to GitHub (gh auth login),
  or use the SSH url:  DAPP_REPO=git@github.com:Akshayram11/ai_platform.git $0"
    ok "cloned to $SRC ($REF)"
  fi
fi

# ---- 3. install --------------------------------------------------------------------------------------------------
say "Installing dapp (isolated tool, Python $PY managed by uv)"
uv tool install --quiet --force --python "$PY" --from "$SRC" --with "mcp>=1.2" "$PKG" \
  || die "uv could not install dapp from $SRC"
BIN_DIR="$(uv tool dir --bin)"
export PATH="$BIN_DIR:$PATH"
command -v dapp >/dev/null 2>&1 || die "dapp was installed but is not on PATH ($BIN_DIR)"
ok "$(dapp version 2>/dev/null | tr -d '\n') at $(command -v dapp)"

# ---- 4. dataos-ctl and login -------------------------------------------------------------------------------------
say "Checking dataos-ctl and your DataOS login"
if [ "$RUN_DOCTOR" = 1 ]; then
  if dapp doctor; then
    ok "ready: try  dapp ls"
  else
    warn "dapp is installed, but it is not ready to deploy yet. Fix the item marked FAIL above, then run: dapp doctor"
    warn "usually: dataos-ctl login   (and pick your tenant with: dataos-ctl tenant select)"
  fi
fi

if [ "$WITH_MCP" = 1 ]; then
  say "Registering dapp with Claude Code"
  if command -v claude >/dev/null 2>&1; then
    claude mcp get dapp >/dev/null 2>&1 && claude mcp remove dapp >/dev/null 2>&1 || true
    claude mcp add dapp -- dapp mcp >/dev/null && ok "added the 'dapp' MCP server (restart Claude Code to see it)"
  else
    warn "claude is not installed; skipped"
  fi
fi

NEEDS_PATH=1
IFS=: read -r -a _dirs <<< "${ORIG_PATH:-}"
for d in "${_dirs[@]:-}"; do [ "$d" = "$BIN_DIR" ] && NEEDS_PATH=0; done
echo
say "Done"
echo "  Update later:  cd ${SRC} && git pull && ./install.sh"
echo "  Remove:        ${SRC}/uninstall.sh"
if [ "$NEEDS_PATH" = 1 ]; then
  echo
  warn "$BIN_DIR is not on your PATH in new terminals. Add this line to your shell profile:"
  echo "      export PATH=\"$BIN_DIR:\$PATH\""
fi
