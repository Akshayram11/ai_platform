# Pipelines: move data with DataOS Nilus, in plain words

The dashboard's **Pipelines** tab (`https://<host>/dapp/pipelines`) and `dapp pipeline` move data between systems with DataOS Nilus.
A person answers four questions and the right Nilus pipeline is made and started:

1. **Where is the data?** A source Depot and a table (`schema.table`), collection or Kafka topic.
2. **How does it change?** One plain answer picks the pipeline kind and the write strategy (table below).
3. **Where should it go?** A destination Depot (usually a Lakehouse) and a table there (default `raw.<table>`).
4. **When, and what to call it.** A schedule for scheduled kinds; a name.

**Preview** shows a one-line summary and the Nilus resource; **Deploy and start** applies it as the signed-in person and it starts
running at once. Each pipeline has a page with its state, its runs (rows, seconds, MB written, errors), 7-day stats, the latest log,
**Run again now** and **Delete**.

Code: `src/dapp/pipelines.py` (the rules and the manifest), `Platform.*pipeline*` in `src/dapp/platform.py`, the routes in
`src/dapp_dashboard/app.py` and `templates/pipeline*.html`, the CLI in `src/dapp/cli.py`. Live findings that shaped it:
[nilus-notes.md](nilus-notes.md).

## The answers and what they make

| Answer (`--how`) | Nilus type | Strategy | Runs | Needs |
|---|---|---|---|---|
| Copy it once (`once`) | batch | replace | once, right after deploy | |
| Refresh the whole table on a schedule (`refresh`) | batch | replace | schedule | |
| New rows only get added (`new_rows`) | batch | append | schedule | a column that grows (`updated_at`, `id`) |
| Rows change, a schedule is fine (`changes`) | batch | merge | schedule | that column, and the row key (`id`) |
| I need changes within seconds (`live`) | cdc | merge | continuously | change capture on the source |
| Every change, as history (`live_history`) | cdc | append | continuously | change capture on the source |
| It's a stream of events (`events`) | stream | append | continuously | a Kafka Depot |
| Just show it in the catalog (`catalog`) | metadata | (no rows copied) | schedule | `shallow` or `deep` |

Only the answers the source can do are offered (from DataOS's connector table): PostgreSQL batch, CDC, catalog; MySQL and SQL Server
batch and CDC; MongoDB batch, CDC, catalog; Snowflake batch and catalog; BigQuery and Redshift batch; Kafka events; Lakehouse catalog
(and destination). Schedules: hourly, every 6 hours, daily 02:00 UTC, weekly, or any cron line (UTC).

Rules the code follows so a pipeline doesn't go wrong:
- The Depot purpose in `dataos://depot?purpose=` is taken from the Depot's own Secrets (the most limited one that works). Never `ro`
  blindly: a Depot without it refuses the pipeline.
- `merge` always comes with a row key; without one Nilus silently appends and duplicates rows.
- CDC on PostgreSQL gets its own replication slot and publication (`dapp_<name>`); `topic.prefix` is the pipeline name.
- Streams get `run_in_loop: true` and no schedule; catalog pipelines get no sink.
- **Run again** deletes and re-creates the pipeline: DataOS does not run an unchanged pipeline again when it is re-applied (tested).
- **Delete** stops and removes the pipeline. The delete box lists what else could go, each unticked by default, and says why
  something must stay:
  - **Its table** in the Lakehouse (shows the rows and size; `dapp pipeline rm NAME --drop-table`). Dropped through the Lakehouse's
    Iceberg catalog (`DELETE .../tables/<t>?purgeRequested=true`) as you, so DataOS decides whether you may; it can't be undone.
    It is offered only when you own the pipeline, one of its successful runs wrote exactly that table, and no other pipeline writes it
    (so a table that `dest_table` merely points at, or one shared with another pipeline, is never dropped). The page and the platform
    both check this, not just the page. Only Lakehouse Depots served by the DataOS catalog (`/lh/...`) can be checked or dropped from here.
  - **The source you connected** for it (`--remove-source`): its Depot, Secret and grants, when you connected it with dapp and no other
    pipeline uses it.
  - Always stays: Nilus's record of past runs, the empty schema in the Lakehouse, and any Depot access that people were given.
  `dapp pipeline leftovers NAME` shows the same list.

Customize: mask columns (`email=hash, phone=partial:3, ssn=redact`), skip columns, a row limit for a first try, catalog depth, compute,
CPU and memory.

## Sources: connect a system that has no Depot yet

A pipeline starts from a Depot. For a system that isn't one yet (Supabase, your own PostgreSQL or MySQL, Snowflake, MongoDB...),
open **Pipelines → Connect a source** (`/dapp/sources`, or `dapp source add`). Pick what it is (Supabase and a few others are one
click; or choose any Depot type DataOS has) and paste a connection string or fill the boxes. The fields come from DataOS's own
definition of each Depot type (`GET /ds/api/v2/types`), so every type works, not a hand-written list.

What it makes, in this order, all as you and all owned by you:
1. a **Secret** `dapp-src-<name>` with the credentials;
2. **Can Use** on that Secret for you only (DataOS requires it, even for the creator, and audits it);
3. a **Depot** `<name>` of that type, pointing at the Secret;
4. **Can Use** on the Depot for you only.
If a step fails, what was already made is removed. Then the pipeline form opens with that Depot chosen.

Credentials: typed into password boxes (or `dapp source add --url-env VAR`, `--secret-env FIELD=VAR`, `--secret-file FIELD=PATH`;
never a command-line argument), sent straight to DataOS, and never stored by the dashboard, written to a log, or shown on a page
again, not even after an error (the boxes come back empty, and messages are scrubbed). **Replace credentials** after a password
change; **Remove source** deletes the Depot, the Secret (unless you keep it) and the grants.

Governance: connecting grants access only to you. Later, another person asks to use your Depot with **Request access** and you (or a
tenant admin) approve it; approving gives them the Depot and the Secret behind it. So **connect with a separate read-only user** on
the source, not your own account.

Supabase: use the **Session pooler** connection string (port 5432, host `aws-0-<region>.pooler.supabase.com`, user
`postgres.<project>`) with the database password, not the direct `db.<project>.supabase.co` host (IPv6 only) or the transaction
pooler (port 6543). The preset adds `sslmode=require`.

## Access is governed

A pipeline runs as the person who deploys it. They need:
- a tenant role that may create pipelines: Tenant Admin, Data Admin or Data Developer;
- **Can Use** on the Compute, on each Depot it reads or writes, and on the Secrets behind those Depots (DataOS checks each one; a
  tenant role alone is not enough).

The dashboard and the CLI **never grant access by themselves**. If DataOS refuses because of a Depot, nothing is created and the page
names the Depot with a **Request access** button. The request goes to the Depot's owner, who sees it at the top of the Pipelines page
(so does any tenant admin). **Approve** makes two DataOS grants for that one person: `use-specific-depot` on that one Depot and
`use-specific-secret` on its Secrets, tagged `dapp-depot-<depot>` and noting who approved it. It never uses `use-any-depots` or
`use-any-secret`. Every grant is audited by DataOS. Decline grants nothing.

The Depot's own database user also needs rights on the source system: read for batch, change-log rights for CDC (PostgreSQL
`wal_level = logical` and `REPLICATION`), catalog/system tables for a catalog scan. Those are set by that system's admin.

## From a terminal (or an agent)

```
dapp pipeline depots                     # what each Depot can be moved from, and to
dapp pipeline kinds                      # the --how answers
dapp pipeline new orders-sync --from pgrds:public.orders --to s3lhdepotrr:raw.orders \
     --how changes --incremental-key updated_at --primary-key id --schedule hourly --dry-run
dapp pipeline new orders-sync ...        # same without --dry-run: creates and starts it
dapp pipeline ls | dapp pipeline status orders-sync | dapp pipeline logs orders-sync
dapp pipeline run orders-sync            # batch or catalog: run again now
dapp pipeline rm orders-sync --yes
```

A refused Depot gives the same message as the dashboard, with the link to request access there (requests are kept by the dashboard).

## Where the run data comes from

State: Poros runtime of the `nilus` resource (a workflow container group `<name>-batch-execute`: pending, initializing, running,
completed, failed). Runs and stats: Nilus Manager `GET /nilus-manager/api/v1/tenants/<tenant>/pipelines/workflow:v2alpha:<name>/runs`
and `/stats?period=7d`, with the person's own token. Log: Poros `/resources/nilus/<name>/log`.

## Not yet
- An MCP tool so AI apps and agents can preview and request pipelines (never approve).
- Direct-URI sources without a Depot (Databricks, IBM Db2, NATS) and custom API sources: see [nilus-custom-sources.md](nilus-custom-sources.md) (parked).
- Picking tables from a list instead of typing them.
