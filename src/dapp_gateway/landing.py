"""The gateway's home page: what it is for, and how to use it. Pure HTML, no secrets, no sign-in needed."""
from __future__ import annotations

import html

CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--line:#e2e1dc;--acc:#2a5bd7;--code:#efeee9;--ok:#1a7f4b;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--mut:#9b9a93;--card:#1d1d1b;--line:#33332f;--acc:#7da2ff;--code:#26261f;--ok:#4cc38a;--bad:#ff8a80}}
*{box-sizing:border-box}body{font:15px/1.55 system-ui,-apple-system,sans-serif;color:var(--fg);background:var(--bg);margin:0}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 8px}
h3{font-size:15px;margin:18px 0 4px}p{margin:8px 0}.lead{font-size:17px;color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.grid .card{margin:0}
.grid h3{margin-top:0}code,pre{font:13px ui-monospace,Menlo,monospace;background:var(--code);border-radius:6px}
code{padding:1px 5px}pre{padding:12px;overflow:auto;margin:8px 0;white-space:pre}a{color:var(--acc)}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
ol{padding-left:20px}li{margin:4px 0}
"""


def e(x: object) -> str:
    return html.escape(str(x))


def page(*, base: str, host: str, dashboard: str, providers: dict[str, bool], local_models: dict[str, str],
         endpoints: dict[str, str]) -> str:
    """`base` is this gateway's public path (like /dapp-gateway), `host` the DataOS host name, e.g. poliks.instance.dataos.cloud."""
    pub = f"https://{host}{base}" if host else base
    prov = " ".join(f"<span class=pill>{e(k)}: <b class={'ok' if v else 'bad'}>{'ready' if v else 'not set up'}</b></span>"
                    for k, v in providers.items())
    models = "".join(f"<tr><td><code>{e(m)}</code></td><td class=mut>{e(v)}</td></tr>" for m, v in local_models.items()) \
        or "<tr><td colspan=2 class=mut>None yet. Deploy one from the dashboard's <b>Models</b> tab (or <code>dapp model deploy</code>); it shows up here in seconds.</td></tr>"
    ep = "".join(f"<tr><td><code>{e(k)}</code></td><td>{e(v)}</td></tr>" for k, v in endpoints.items())
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>dapp LLM gateway</title><style>{CSS}</style><main>
<div class=top><div><h1>dapp LLM gateway</h1><p class=lead>One door to every AI model, for every app in DataOS.</p></div>
<div><a href="{e(dashboard)}">Open the dashboard →</a></div></div>
<p>Status: running. {prov}</p>

<h2>What is it, and why do we have it?</h2>
<p>Apps and people in DataOS need to call language models (OpenAI, Anthropic, or small models running inside DataOS) and data tools
(like the Data Product MCP). Without a gateway, every app would carry its own provider key, nobody would know who spent what, and a leaked
key would be a disaster. The gateway sits in the middle and fixes that.</p>
<div class=grid>
<div class=card><h3>🔑 Provider keys stay here</h3><p>The real OpenAI / Anthropic keys live only in the gateway. Apps get their own small key that works only here, so a leak costs nothing and can be revoked in one click.</p></div>
<div class=card><h3>📊 Every call is metered</h3><p>Tokens, cost and latency are recorded per app, per person and per model. The dashboard shows who used what.</p></div>
<div class=card><h3>🧠 Models inside DataOS</h3><p>Small models you deploy from the <b>Models</b> tab are served through the same address, so prompts stay in DataOS and cost no per-token bill.</p></div>
<div class=card><h3>🛠 Data tools (MCP)</h3><p>Apps can call tools such as the Data Product MCP through the gateway. It checks who is calling and which tools they may use.</p></div>
</div>

<h2>How to use it</h2>
<h3>1. From an app you deploy with dapp</h3>
<p>Add <code>llm: true</code> to the app's <code>dapp.yaml</code>. The app then gets two variables by itself: <code>DAPP_LLM_KEY</code> (its own key) and
<code>OPENAI_BASE_URL</code> (this gateway). Nothing else to set up.</p>
<pre>from openai import OpenAI
import os
client = OpenAI(api_key=os.environ["DAPP_LLM_KEY"])   # base URL comes from OPENAI_BASE_URL
r = client.chat.completions.create(model="gpt-6-luna",
      messages=[{{"role": "user", "content": "Say hi"}}])
print(r.choices[0].message.content)</pre>
<p>To use a model you deployed inside DataOS, use its name: <code>model="my-model"</code>. Anthropic works too, through <code>{e(pub)}/anthropic/v1</code>.</p>

<h3>2. From your laptop or a notebook</h3>
<p>Get your own personal key once. It is for LLM calls only and its usage is counted as you.</p>
<pre>dapp llm-key personal --save ~/.dapp/llm.env       # writes DAPP_KEY and DAPP_BASE_URL (never print a key)
set -a; . ~/.dapp/llm.env; set +a

curl -s "$DAPP_BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $DAPP_KEY" -H "Content-Type: application/json" \\
  -d '{{"model": "gpt-6-luna", "messages": [{{"role": "user", "content": "Say hi"}}]}}'</pre>
<p>Or create and revoke keys on the dashboard's <b>Keys</b> page.</p>

<h3>3. Data tools (MCP)</h3>
<p>Add <code>tools: [dataproduct-mcp]</code> (with <code>llm: true</code>) to <code>dapp.yaml</code>; the app may then call the tool at
<code>$DAPP_TOOLS_URL/dataproduct-mcp</code> with <code>Authorization: Bearer $DAPP_LLM_KEY</code>. See what exists with <code>dapp tool ls</code>.
Personal keys are refused for tools: use your DataOS key there.</p>

<h3>4. See what was used</h3>
<pre>dapp usage                 # tokens and cost, per app and model
dapp usage my-app          # one app</pre>
<p>On the dashboard the same numbers are on each app's page and on the Apps home.</p>

<h2>Models you can call right now</h2>
<table><tr><th>Model</th><th>Where it runs</th></tr>
<tr><td><code>gpt-6-luna</code>, <code>gpt-4o-mini</code></td><td class=mut>OpenAI, through the gateway (needs the OpenAI key to be <b>ready</b> above)</td></tr>
{models}</table>

<h2>Good to know</h2>
<ul>
<li>A <b>local</b> model costs its reservation (CPU and memory) even when idle, not tokens. Delete the ones nobody uses.</li>
<li>Small models cannot call tools and have a 4,096 to 8,192 token context. Give them plain text.</li>
<li>If a call fails with "connection refused" or 502 for about a minute, DataOS is moving the gateway. Wait and retry; apps and the CLI retry by themselves.</li>
<li>Keys are shown once. Lost one? Make a new one; the old one stops working.</li>
</ul>

<h2>Endpoints</h2>
<table>{ep}</table>
<p class=mut>This page is only a guide. Calling these addresses needs a key or a DataOS sign-in.</p>
</main>"""
