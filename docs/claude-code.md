# Claude Code on DataOS

`apps/claude-code`: a browser terminal (ttyd + tmux) running Claude Code with `dapp` inside. Ask it to build an app; it writes
`app/` and `dapp.yaml` in `~/work`, deploys with `dapp deploy` (see [run-from-source.md](run-from-source.md)), checks the app, and
gives you the URL. URL: `https://poliks.instance.dataos.cloud/claude-code/`.

## Who can open it, and as whom
Private (DataOS sign-in), and `dapp_proxy` additionally lets only the **owner** through. The terminal acts with the owner's
DataOS identity (DataOS gives the pod `DATAOS_RUN_AS_USER` / `DATAOS_RUN_AS_APIKEY`), so everything it deploys is yours and
DataOS permissions apply. It has a shell: treat the page like a terminal on your account.

## What survives
| Thing | Survives a browser refresh | Survives a pod restart or move |
|---|---|---|
| The running Claude session | yes (tmux reattaches) | no |
| `~/work` files | yes | yes, saved to `user:<you>/workspace` every 30 s and at shutdown (not caches, venvs, `.git`, `.env`/keys, databases, files over 700 KB) |
| Claude conversations | yes | yes, `claude-sessions` (`claude --continue`), when the save worked |
| Claude sign-in | yes | **only with a token secret** (below) |
| Deployed apps | n/a | yes, they have their own code |

## Staying signed in
`/login` is stored under `/tmp` in the pod. DataOS replaces pods now and then (node moves, rollouts), and every deploy of
claude-code replaces the pod, so you are asked to sign in again. Fix it once with a token:

1. `claude setup-token` (anywhere you have Claude Code; needs a Claude subscription). It prints a token valid for a year.
2. Store it as a DataOS secret without it touching a file you keep or a chat:
   ```bash
   T=$(pbpaste | tr -d ' \n\r'); printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$T" > /tmp/cc.env \
     && dapp secret set claude-code anthropic --from-env-file /tmp/cc.env --no-attach; rm -f /tmp/cc.env; unset T; printf '' | pbcopy
   ```
   (select the token, copy it, then run this; it clears the clipboard after.)
3. `secrets: [dapp-claude-code-anthropic]` in `apps/claude-code/dapp.yaml` (already there), `dapp deploy -f apps/claude-code/dapp.yaml`.

`start.sh` then skips the first-run screens. An `ANTHROPIC_API_KEY` in the same secret works too (Claude Code reads it through
`apiKeyHelper`, so `env` does not show it). A token is visible in the terminal's environment to anything running there: do not run untrusted code in it.
Never paste a token in chat or commit it. To replace it, run `claude setup-token` again and repeat step 2.

## Resources
`resources: {cpu: 500m, memory: 2Gi, cpuLimit: "4", memoryLimit: 4Gi}`. Real use is far lower (peak ~340 MiB, 0.1 core over a day);
the extra room is for installs and several tabs. Resources do not cause logouts.

## Files
`start.sh` (sets `HOME=/tmp/claude-home`, restores the workspace, starts ttyd and the proxy), `session.sh` (tmux attach),
`claude-loop.sh` (`claude --continue`, then a shell), `workspace-sync.py` (restore at start, save every `DAPP_SYNC_SECONDS`
and on SIGTERM; a save only starts after a successful restore, so a failed restore never wipes the saved copy),
`CLAUDE.md` (the guide the agent reads, copied to `~/work` on every start), `tmux.conf`.

## Troubleshooting
- **Asked to sign in after a refresh:** the pod was replaced. Check `dapp --json status claude-code` for a new pod name; set up the token above.
- **`[workspace-sync] could not save …` in `dapp logs claude-code`:** the gateway or immortal-obj was not reachable at that moment (timeout or
  connection refused). It retries every 30 s; a save that never succeeds means `~/work` is only in the pod.
- **`claude-sessions: 0 files` after a restart:** nothing had been saved from the earlier pod.
- **Build:** `docker buildx build --platform linux/amd64 -f apps/claude-code/Dockerfile -t docker.io/tmdcio/dapp-claude-code:0.1.4 --push .`
  then bump the tag in `dapp.yaml` and `src/dapp/cli.py` (`CLAUDE_CODE_IMAGE`).
