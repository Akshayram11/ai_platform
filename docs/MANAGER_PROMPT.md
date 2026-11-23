# One prompt: have an agent deploy a chatbot with dapp

Paste the prompt below into Claude Code (or any agent that can run shell commands). It installs `dapp` if needed,
checks that the machine is ready, deploys a ready-made OpenAI chatbot under a unique name, and verifies it.

## Before you paste it (once, 2 minutes, you do these, an agent cannot)

1. You need access to the repo `Akshayram11/ai_platform` (it is private) and `git` signed in to GitHub (`gh auth login`).
2. You need `dataos-ctl` logged in to our instance: `dataos-ctl login`, then `dataos-ctl tenant select` and pick the
   `engineering` tenant of `poliks.instance.dataos.cloud`. The login lasts 24 hours.
3. A tenant admin must have given you the app-developer permissions
   (`deploy/grants/app-developer.template.yaml`: create resources, use `engineering-compute`, read your `dapp-*` secrets).

If one is missing, the agent will stop at that step and tell you exactly which.

## The prompt

```text
You are deploying an LLM chatbot to our DataOS platform with the `dapp` CLI. Run the commands yourself, one step at a
time, and check each result before moving on. If a step fails, stop and report the exact error text; do not improvise
workarounds. Never print, log, or ask me to paste any password, API key or token.

Facts: instance poliks.instance.dataos.cloud, tenant engineering, compute engineering-compute. dapp deploys a container
as a DataOS app. A ready chatbot image already exists, so nothing has to be built and Docker is not needed.

1. Install dapp if `dapp version` does not work:
     git clone git@github.com:Akshayram11/ai_platform.git ~/ai_platform   (or: gh repo clone Akshayram11/ai_platform ~/ai_platform)
     ~/ai_platform/install.sh
   If the clone is refused, tell me I need access to the Akshayram11/ai_platform repo, and stop.
   If `dapp` is still not found afterwards, run: export PATH="$(uv tool dir --bin):$PATH"

2. Run `dapp doctor`. Required: no line starting with FAIL.
   - The "context" line must show poliks.instance.dataos.cloud and tenant engineering. If it shows anything else, tell me to
     run `dataos-ctl tenant select`, and stop.
   - If "credentials" fails, tell me to run `dataos-ctl login` myself (it opens a browser, you cannot), then run doctor again.
   - If "deploy access" fails, tell me to ask Akshay for the app-developer permissions, and stop.

3. Choose a unique app name: chatbot-<my username or first name>, lowercase letters, digits and dashes only, at most
   30 characters. Never use the plain name `chatbot` (it belongs to someone else). Run `dapp --json ls --all` and make
   sure the name is not already in use.

4. In a new empty folder run:  dapp init <name> --template chatbot
   Do not edit the generated dapp.yaml.

5. Run `dapp deploy --dry-run`, then `dapp deploy --lint`. Show me the app name, URL path and compute from the dry run.
   Lint must print "valid".

6. Run `dapp deploy`. Then poll `dapp --json status <name>` every 10 seconds until "state" is "running" (give up after
   5 minutes). If "state" becomes "failed": run `dapp logs <name> -n 100`, report the error text verbatim, and stop (you may
   retry `dapp deploy` once if the error looks transient).

7. Verify: `curl -s https://poliks.instance.dataos.cloud/<name>/healthz` must return JSON with "ok": true and
   "via_gateway": true. Also run
     curl -s -o /dev/null -w '%{http_code}' -X POST https://poliks.instance.dataos.cloud/<name>/api/chat
   It must print 401 (the app is private: it needs a DataOS sign-in).

8. Reply with a short summary only:
   - the URL: https://poliks.instance.dataos.cloud/<name>/  (private: opens with my DataOS login)
   - to let a colleague in:   dapp access allow <name> <their-dataos-username>
   - to see who uses it and what it costs:   dapp usage <name> --by end_user   (or the dashboard at
     https://poliks.instance.dataos.cloud/dapp/)
   - to remove it completely:   dapp rm <name> --purge --yes

Rules: only touch the app you created. Never run `dapp rm` on any other app. Do not change other apps' access, keys or
secrets. Do not deploy more than one app.
```

## What the agent will see

`dapp doctor` prints one line per check (`ok` / `FAIL` / `warn`) with a hint on every failure, and exits 1 if a required
check fails, so an agent can act on it without guessing. Everything also has `--json` (put it first: `dapp --json status ...`).

## Removing what the prompt created

`dapp rm <name> --purge --yes` deletes the app, its secrets and revokes its LLM key. To remove `dapp` itself from the
machine: `~/ai_platform/uninstall.sh --yes`.
