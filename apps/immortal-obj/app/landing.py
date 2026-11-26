"""immortal-obj's home page: what it is for and how to use it. Static text, no data, no secrets."""
from __future__ import annotations

CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--line:#e2e1dc;--acc:#2a5bd7;--code:#efeee9}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--mut:#9b9a93;--card:#1d1d1b;--line:#33332f;--acc:#7da2ff;--code:#26261f}}
*{box-sizing:border-box}body{font:15px/1.55 system-ui,-apple-system,sans-serif;color:var(--fg);background:var(--bg);margin:0}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 8px}
h3{font-size:15px;margin:18px 0 4px}p{margin:8px 0}.lead{font-size:17px;color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.grid h3{margin-top:0}
code,pre{font:13px ui-monospace,Menlo,monospace;background:var(--code);border-radius:6px}code{padding:1px 5px}
pre{padding:12px;overflow:auto;margin:8px 0}a{color:var(--acc)}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
"""

PAGE = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>immortal-obj</title><style>{CSS}</style><main>
<h1>immortal-obj</h1><p class=lead>Storage that survives restarts, for apps and agents in DataOS.</p>

<h2>What is it, and why do we have it?</h2>
<p>Apps in DataOS lose their local files every time they restart or are redeployed. That is fine for a web page and bad for a chatbot that
must remember conversations. <b>immortal-obj</b> keeps data safely on a DataOS Volume, outside every app, so it is still there after
any restart, update or delete of the app that wrote it.</p>
<div class=grid>
<div class=card><h3>🗄 Two kinds of object</h3><p><b>Database object:</b> key-value pairs plus a real SQLite database, for settings, history and records.<br>
<b>Volume object:</b> a folder of files (up to 700 KB each), for saved work.</p></div>
<div class=card><h3>🔒 Yours by default</h3><p>An object belongs to the app (or person) that created it. Nobody else can read it unless the owner shares it, as read or as write.</p></div>
<div class=card><h3>📦 Room and limits</h3><p>Each object holds up to 200 MiB. All objects together share one 5 Gi Volume. Delete what you no longer need.</p></div>
</div>

<h2>How to use it</h2>
<h3>1. From an app you deploy with dapp (the easy way)</h3>
<p>Add <code>objects: true</code> to <code>dapp.yaml</code>. The helper <code>dapp_objects</code> is already installed in the app:</p>
<pre>from dapp_objects import objects
db = objects.open("history")                       # this app's own object, created on first write
db.put("settings", {{"theme": "dark"}}); db.get("settings")
db.sql("create table if not exists msgs (who text, text text, at text default current_timestamp)")
db.sql("insert into msgs (who, text) values (?, ?)", [user, text])
rows = db.sql("select * from msgs where who = ? order by at desc limit 20", [user])
mine = objects.open("chat", for_user=user)         # one object per signed-in person</pre>

<h3>2. From an AI agent (MCP tools)</h3>
<p>Ask for the tool <code>immortal-obj</code> through the gateway. It offers these tools:</p>
<table>
<tr><th>Tool</th><th>What it does</th></tr>
<tr><td><code>put</code> / <code>get</code> / <code>keys</code> / <code>delete</code></td><td>Save, read, list and remove values by key in an object.</td></tr>
<tr><td><code>sql</code></td><td>Run one SQLite statement (create table, insert, select) on an object.</td></tr>
<tr><td><code>write_file</code> / <code>read_file</code> / <code>ls</code> / <code>remove</code></td><td>Work with files in a volume object.</td></tr>
<tr><td><code>objects</code></td><td>List your objects and what others shared with you.</td></tr>
<tr><td><code>share</code> / <code>unshare</code></td><td>Let someone read or write one of your objects, or stop.</td></tr>
<tr><td><code>drop</code></td><td>Delete an object and everything in it. Cannot be undone.</td></tr>
</table>
<p class=mut>Names: a plain name (<code>notes</code>) is your own object. <code>user:alice/notes</code> or <code>app:chatbot/history</code> is one somebody shared with you.</p>

<h3>3. See and manage your objects</h3>
<p>The dashboard's <b>Storage</b> page lists your objects, lets you browse them, run a query, upload and download files, share and delete.</p>

<h2>Good to know</h2>
<ul>
<li>Do not keep important data in local files or in SQLite on an app's own disk: it disappears on restart. Use an object.</li>
<li>Every call is checked by the dapp gateway, which knows who is calling and records it. This server itself answers only the gateway.</li>
<li>If a call fails with "connection refused" or 502 for about a minute, DataOS is moving a pod. The helper waits and retries by itself.</li>
<li>Claude Code on DataOS saves your work here too (objects <code>workspace</code> and <code>claude-sessions</code>).</li>
</ul>
<p class=mut>This page is only a guide: it shows no data. Calling the tools needs a key or a DataOS sign-in through the gateway.</p>
</main>"""
