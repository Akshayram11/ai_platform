# Nilus (data movement): notes for the Pipelines feature

Working notes for building a high-level "Pipelines" feature on top of DataOS Nilus (dashboard tab, `dapp pipeline` CLI, an MCP tool,
a CLAUDE.md recipe). Source: DataOS docs (v2.dataos.info, read 2026-11-26). Items marked **(to verify live)** are not yet tested in
our tenant.

## What Nilus is
One resource kind, `type: nilus` (`version: v1alpha`), for every kind of data movement. Each pipeline has a **source address** and
(except metadata) a **sink address**. The runtime handles credentials, schema inference, state, retries and loading
(Extract → Normalize → Load).

Addresses are either a Depot (`dataos://<depot>?purpose=ro|rw`, credentials come from the Depot) or a direct URI
(`postgresql://{PG_USER}:{PG_PASSWORD}@host:5432/db`) with credentials projected from a Secret under `spec.use.projection`.
Databricks, IBM Db2, NATS and Microsoft Fabric Lakehouse are direct-URI only.

## The four types: when to use which (the rule our UI applies)

| `spec.type` | Use it when | Do not use it when | Runs as |
|---|---|---|---|
| `batch` | A table, view, file, SaaS object or warehouse table is loaded once or on a schedule, in full or incrementally (`incremental_key`) | The user needs changes within seconds or minutes, or the source is Kafka/NATS | A job: with `spec.schedule.crons` it repeats; without a schedule it runs once when applied |
| `cdc` | A database's row changes (insert/update/delete) must arrive in near real time, and the source supports CDC (PostgreSQL, MySQL, MongoDB, SQL Server, IBM Db2) | The source has no change log (warehouses, SaaS), or a daily copy is enough (use batch: far cheaper and fewer grants) | A long-running service; no schedule |
| `stream` | The data is events on Kafka or NATS+JetStream | The source is a database or a file | A long-running service; never add `spec.schedule`; `source.options.run_in_loop: true` is required, else it stops after one pull |
| `metadata` | The user wants tables, columns, lineage, profiles, PII tags or usage in the DataOS catalog **without copying rows** | The user wants the data itself | A scheduled workflow (DAG); no `sink` block; `mode: shallow` (metadata + lineage) or `deep` (adds profiler, classification, usage) is required |

How the UI picks the type from one plain question ("How does this data change?"):

| User's answer | Type | Sink `incremental_strategy` | Notes |
|---|---|---|---|
| "Copy it once" / small lookup table / Google Sheet | `batch`, no schedule | `replace` | Re-run = delete and re-create the resource (see below) |
| "Refresh it every day/hour" | `batch` + `schedule.crons` | `replace` (small) or `merge` (large, with `incremental_key` + `primary_key`) | `merge` without a usable `primary_key` silently falls back to append and duplicates rows: require a key |
| "New rows only get added" (logs, events in a table) | `batch` + schedule | `append` + `incremental_key` | |
| "Rows change and I need them fast" | `cdc` (only if the source supports it) | `merge` for current state, `append` for full change history | Needs extra grants on the source (below) |
| "It's a Kafka/NATS topic" | `stream` | `append` (default), `merge` with a stable key | |
| "Just show it in the catalog" | `metadata` | (no sink) | `shallow` by default; `deep` only when asked, it is heavy |

## Permissions (two separate layers; both are needed)

### 1. Inside DataOS (who may create and run the pipeline)
- The creator needs a Tenant role: **Tenant Admin**, **Data Admin** or **Data Developer**.
- Plus **`Can Use`** on each dependency:
  - the **Compute** (e.g. `engineering-compute`; to verify live which compute Nilus accepts),
  - the **source Depot** and the **sink Depot**,
  - the **Secret behind each Depot** (using a Depot does not include its Secret),
  - for direct URIs, the Secret projected in `spec.use.projection`.
- Owning a Compute, Depot or Secret gives `Can Edit` + `Can Manage Access`, **not** `Can Use`. The owner grants `Can Use` to themself
  (audited). Anyone with `Can Manage Access` (or a Tenant Admin) can grant it to others.
- After creation the pipeline's owner has `Can Edit` and `Can Manage Access` on it. There is no `Can Use` on a Nilus resource.
- `spec.runAsUser` is optional; without it the pipeline runs as its owner.
- What happens when one is missing: apply fails fast for a missing **resource** (`invalid resource dependency, not found: depot:<name>`).
  A missing **grant** on the source or destination system is not caught at apply: the pipeline is created and then fails when it runs
  ("Authentication or permission denied").

### 2. On the source and destination systems (what the Depot's credential may do)
| Pipeline | Needs on the source/destination |
|---|---|
| batch read | Connect + `SELECT` on the schema/table/collection |
| any sink | Connect + DML for the strategy (`append`: insert; `merge`: insert/update; `replace`: drop/create) + create table/schema if Nilus creates it |
| cdc | Server-level change log, usually enabled by an admin: PostgreSQL `wal_level = logical` + `REPLICATION` attribute, a unique `slot.name` and a publication per pipeline; SQL Server native CDC on the tables; MongoDB a replica set with change streams |
| metadata | Read on catalog objects + system tables for lineage/usage (PostgreSQL: `pg_stat_statements` + `pg_read_all_stats`; Snowflake: account-usage views) |
| stream | Kafka ACLs to read the topic and commit the consumer group |
Grant only what the type needs: a batch source needs no write grants and no replication.

### 3. What our feature will need (to verify live)
- The dashboard acts as the signed-in person, so the **person** needs the Tenant role and the `Can Use` grants; the app holds no extra power.
- Before showing "Deploy", check those grants and say exactly which one is missing and who owns it (the Depot/Secret owner can grant it).
- Reading run history: Nilus Manager API `GET /nilus-manager/api/v1/tenants/<tenant>/pipelines?resource_id=…`, `/pipelines/{id}/runs`,
  `/pipelines/{id}/stats?period=7d` (Bearer token). Which token and role it accepts is to verify live.

## Behaviour that the UI must hide or explain
- **Re-applying an unchanged resource does not run it again.** "Run again" = delete and re-create (or a new name).
- Changing the extract style on the same `dest_table` (table read ↔ `query:`) breaks Iceberg decimals: use a new `dest_table`.
- `crons` is a list; `concurrencyPolicy: Forbid` avoids overlapping runs; `timezone` and `endOn` are optional.
- CDC: `topic.prefix` is the connector identity and the destination table prefix; never change it in production. PostgreSQL `slot.name` unique per pipeline.
- Stream: `group_id` (Kafka) / `durable=` (NATS) must stay stable for offsets to continue.
- Large Lakehouse loads: attach a Volume and set `DATAOS_PERSISTENT_DIR` to its directory, else the pod can run out of disk or memory.
- Masking: `source.options.mask: {email: hash, phone: partial:3}`; skip columns with `sql_exclude_columns`.
- New nullable source columns are added automatically; type changes are not.

## Minimal manifests (shape only; names to fill)
```yaml
# batch, daily, merge
version: v1alpha
name: orders-to-lakehouse
type: nilus
spec:
  type: batch
  compute: <compute>
  schedule: {crons: ["0 2 * * *"], concurrencyPolicy: Forbid}
  source: {address: "dataos://<src-depot>?purpose=ro", options: {source_table: public.orders, incremental_key: updated_at, primary_key: id}}
  sink:   {address: "dataos://<lakehouse-depot>?purpose=rw", options: {dest_table: raw.orders, incremental_strategy: merge}}
---
# metadata, shallow, every 6 hours
version: v1alpha
name: pg-catalog
type: nilus
spec:
  type: metadata
  mode: shallow
  compute: <compute>
  schedule: {crons: ["0 */6 * * *"], concurrencyPolicy: Forbid}
  source: {address: "dataos://<src-depot>?purpose=rw", options: {service_type: <snowflake|databricks|lakehouse|...>}}
```

## Phase 1 checklist (live, in tenant `engineering`)
- [x] DataOS login renewed. Note: `~/.local/bin/dataos-ctl` is a wrapper that needs the dp-toolbox container; the real binary
      `~/.dataos/v2/bin/dataos-ctl login` works without it (2026-11-26).
- [x] My Tenant role: `engineering-tenant-admin` (+ operator, user). Tenant Admin may create `type: nilus`.
- [x] Depots in the tenant (all owned by other people):
      Postgres `pgrds` (rajatshardatmdcio) and `postgresdepotss` (shreyanegitmdcio), same Azure Postgres server, db `postgres`,
      secret `engineering:postgressecret`;
      Lakehouse `s3lhdepotrr` (rohitrajtmdcio, Iceberg REST + S3, secret `engineering:s3_lh_sec_rr`) and
      `rustfscryos` (divyamgargtmdcio, Iceberg REST + RustFS, secret `engineering:cryos-rustfs-credentials`);
      Snowflake `snowflakedepots`, `snowflaketestdepot`, `snowflaketestdepotsn`; `ordersksdepot`.
- [x] Computes: `engineering-compute`, `mi-lakehouse-compute`.
- [x] No Nilus pipelines exist in the tenant yet.
- [x] **Lint checks Depot access.** `apply --lint` of a batch manifest `pgrds` → `s3lhdepotrr` failed with
      `not-allowed, subject users:id:<me>, predicate 'read', object 'dataos:system:ds:<depot>', authorization_atom_id 'depot-access'`
      for both Depots. So: Tenant Admin alone does **not** let you use a Depot; each Depot needs its own grant (`depot-access`).
      Good for the UI: lint is a free, side-effect-free "can this person run this pipeline?" check, and the error names the Depot.
      Lint created nothing.
- [x] Grants (Tenant Admin self-grant, audited, user approved on 2026-11-26): `deploy/grants/nilus-test-akshay.yaml`
      (`use-specific-depot` on `pgrds` + `s3lhdepotrr`, `use-specific-secret` on `engineering:postgressecret` + `engineering:s3_lh_sec_rr`).
      Found the use cases with `GET /heimdall/api/v1/use-cases` and `/use-cases/<id>` (atoms and variables):
      Depot → `use-specific-depot`, atom `depot-access`, variable `depot`, collection = tenant;
      Secret → `use-specific-secret`, atom `read-specific-secret-governance`, variable `id` (`<tenant>:<secret>`), collection `system`.
      A Compute grant was not needed (already allowed to use `engineering-compute`).
- [x] Lint passes after the grants. Next error was `purpose not found` for `dataos://pgrds?purpose=ro`: a Depot only has the purposes
      its Secrets declare (`pgrds`: `scan`, `query`, `rw`). **The UI must take the purpose from the Depot's `spec.secrets[].purpose`**
      (use `rw`, or the read one it has); never assume `ro`.
- [x] Applied a tiny batch pipeline with dataos-ctl (`information_schema.schemata`, `replace`): Nilus renders it as a workflow
      container group `<name>-batch-execute` (phases pending → initializing → running → completed). It ran ~1 minute end to end;
      31 rows extracted and loaded into `dapp_test.schemata` on `s3lhdepotrr` in 19 s. Nilus also **published lineage to the catalog**
      by itself (`postgres…information_schema.schemata → lakehouse_s3lhdepotrr.dapp_test.schemata`).
- [x] Applied the same through our REST ApiWriter (`POST /poros/api/v2/tenants/<t>/resources`, the dashboard's path): works,
      completed in ~45 s. Poros GET/DELETE `/resources/nilus/<name>` work too. (The API has no lint; use dataos-ctl lint or apply errors.)
- [x] Nilus Manager API with the person's own token (as the dashboard has it): `/health` 200;
      `/tenants/<t>/pipelines?limit=` lists runs with `resource_id`, `run_as_user`, `source_depot`, `source_table`, `destination_depot`,
      `destination_table`, `started_at`, `finished_at`, `duration_sec`, `records_count` (per table), `extracted_records_count`,
      `files_loaded`, `files_size_mb`, `memory_mb`, `cpu_percent`, `is_success`, `error`. Differences from the docs:
      `resource_id` is **`workflow:v2alpha:<name>`** (not `nilus:…`), and the list total is **`total_count`** (not `total`).
      `/pipelines/<resource_id>/runs` and `/stats?period=7d` (total, successful, failed, success_rate, avg/max duration) work.
- [x] **Re-applying an unchanged pipeline does not run it again**: apply says `updated`, runs stayed at 1 after 2 minutes. So
      "Run again" in our UI = delete + create (same name), or schedule it.
- [x] Deleted both test pipelines (`dapp-nilus-test`, `dapp-nilus-test-api`). **Left behind:** Lakehouse tables `dapp_test.schemata`
      and `dapp_test.schemata_api` (31 rows each, Postgres schema names only) in `s3lhdepotrr`: Nilus does not drop tables and we have
      no drop tool; a Lakehouse admin (or Minerva/Spark `DROP TABLE`) can remove them. The UI's Delete must say the data stays.
- [ ] Remove the test grant when phase 1 is done: `dataos-ctl resource delete -f deploy/grants/nilus-test-akshay.yaml`

## Governance: how the Pipelines feature handles access (decided 2026-11-26)
The self-grant above was a one-off for building the feature. The product must keep access **governed**:
- The feature **never grants access by itself**, even when the signed-in person is a Tenant Admin. It acts as that person and
  holds no extra power.
- Before Deploy it lints the pipeline as the person. If a grant is missing it says plainly which one (Depot, its Secret, the
  Compute), who owns it (the resource's `owner`), and why it is needed ("to read `public.orders` from `pgrds`").
- The person can press **Request access**: the request goes to the owner (and Tenant Admins), like the app access requests we
  already have; the owner approves or denies in the dashboard; the approval creates a `type: grant` scoped to **that person and
  that one Depot/Secret** (never `use-any-depots` / `use-any-secret`), tagged `dapp` + the pipeline name, with a note of who asked
  and why. Every grant is audited by DataOS.
- Grants are listed on the pipeline's page and can be revoked by the owner; deleting a pipeline offers to remove grants that only
  it needed.
- Agents (Claude Code, AI apps through the MCP tool) may preview and request, never approve their own requests.
