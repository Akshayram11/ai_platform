"""Data movement with DataOS Nilus, asked for in plain words.

A person says where the data is (a Depot and a table), where it should go (usually the Lakehouse) and how it changes
("copy it once", "rows change", "I need changes fast"...). This module turns that into the right Nilus pipeline: the type
(batch, cdc, stream or metadata), the write strategy, the schedule, the Depot purposes, and the manifest. It holds no
power of its own: the dashboard and the CLI apply the manifest as the signed-in person, and access that person lacks is
reported (see ``missing_access``) so they can ask the owner, never granted here.

Facts tested live on 2026-11-26 are in docs/nilus-notes.md (the purpose rule, re-apply does not re-run, the run API).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PIPELINE_TAG = "dapp-pipeline"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,46}[a-z0-9]$")
TABLE_RE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){1,2}$")   # schema.table (or db.schema.table)
TOPIC_RE = re.compile(r"^[\w.\-]{1,200}$")
COLUMN_RE = re.compile(r"^[A-Za-z_][\w$]*$")
CRON_RE = re.compile(r"^(\S+\s+){4}\S+$")
CPU_RE = re.compile(r"^\d+(\.\d+)?m?$")
MEM_RE = re.compile(r"^\d+(Mi|Gi)$")
MASKS = ("hash", "redact")                      # and partial:N


# ------------------------------------------------------------------ what each Depot type can do
@dataclass(frozen=True)
class Connector:
    kind: str                 # the Depot's spec.type
    label: str
    modes: frozenset[str]     # as a source: batch, cdc, stream, metadata
    destination: bool         # can a pipeline write into it
    object_word: str = "table"
    metadata_service: str | None = None     # source.options.service_type for a metadata pipeline
    cdc_include_key: str = "table.include.list"


def _c(kind, label, modes, destination, **kw) -> Connector:
    return Connector(kind, label, frozenset(modes), destination, **kw)


# From DataOS's "Supported connectors" table (docs, 2026-11). Keys are Depot spec.type values.
CONNECTORS: dict[str, Connector] = {c.kind: c for c in (
    _c("postgresql", "PostgreSQL", {"batch", "cdc", "metadata"}, True, metadata_service="postgres"),
    _c("mysql", "MySQL", {"batch", "cdc"}, False),
    _c("mongodb", "MongoDB", {"batch", "cdc", "metadata"}, True, object_word="collection",
       metadata_service="mongodb", cdc_include_key="collection.include.list"),
    _c("sqlserver", "SQL Server", {"batch", "cdc"}, True),
    _c("mssql", "SQL Server", {"batch", "cdc"}, True),
    _c("snowflake", "Snowflake", {"batch", "metadata"}, True, metadata_service="snowflake"),
    _c("bigquery", "BigQuery", {"batch"}, True),
    _c("redshift", "Amazon Redshift", {"batch"}, True),
    _c("lakehouse", "DataOS Lakehouse", {"metadata"}, True, metadata_service="lakehouse"),
    _c("kafka", "Kafka", {"stream"}, False, object_word="topic"),
)}


def connector_of(depot: dict[str, Any]) -> Connector | None:
    return CONNECTORS.get(depot_kind(depot))


def depot_kind(depot: dict[str, Any]) -> str:
    spec = depot.get("spec") or {}
    return str(spec.get("type") or depot.get("depot_type") or "").lower()


def depot_purposes(depot: dict[str, Any]) -> list[str]:
    """The purposes a Depot's Secrets declare. A `dataos://` address may only use one of these (tested: `?purpose=ro`
    on a Depot whose secrets say scan/query/rw fails with "purpose not found")."""
    seen: list[str] = []
    for s in ((depot.get("spec") or {}).get("secrets") or []):
        p = str(s.get("purpose") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def depot_secrets(depot: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in ((depot.get("spec") or {}).get("secrets") or []):
        i = str(s.get("id") or "")
        if i and i not in out:
            out.append(i)
    return out


def pick_purpose(depot: dict[str, Any], side: Literal["source", "sink"]) -> str:
    """Which purpose to put in the address: the most limited one that still does the job."""
    have = depot_purposes(depot)
    wants = ("rw",) if side == "sink" else ("ro", "r", "read", "rw")
    for w in wants:
        if w in have:
            return w
    name = depot.get("name", "?")
    if not have:
        return "rw"          # no secrets listed (e.g. not readable): DataOS will say so on apply
    raise ValueError(f"Depot {name} has no {'read-write' if side == 'sink' else 'read'} purpose "
                     f"(it has {', '.join(have)}); ask its owner to add one")


def depot_summary(depot: dict[str, Any]) -> dict[str, Any]:
    c = connector_of(depot)
    spec = depot.get("spec") or {}
    inner = spec.get("spec") or {}
    where = inner.get("database") or (inner.get("s3") or {}).get("bucket") or inner.get("host") or ""
    return {"name": depot.get("name"), "kind": depot_kind(depot), "label": c.label if c else depot_kind(depot) or "?",
            "owner": depot.get("owner") or ((spec.get("owners") or [None])[0]),
            "description": depot.get("description") or spec.get("description") or "",
            "modes": sorted(c.modes) if c else [], "destination": bool(c and c.destination),
            "object_word": c.object_word if c else "table", "where": where,
            "purposes": depot_purposes(depot), "secrets": depot_secrets(depot), "supported": c is not None}


# ------------------------------------------------------------------ "how does the data change?"
@dataclass(frozen=True)
class How:
    key: str
    title: str
    detail: str
    type: str                       # batch | cdc | stream | metadata
    strategy: str | None            # replace | append | merge (None for metadata)
    scheduled: bool                 # runs again on a schedule
    needs: tuple[str, ...] = ()     # incremental_key, primary_key


HOWS: dict[str, How] = {h.key: h for h in (
    How("once", "Copy it once", "A one-time copy, e.g. a lookup table or a backfill. Run again whenever you want.",
        "batch", "replace", False),
    How("refresh", "Refresh the whole table on a schedule", "Small tables that change a little: copied in full each time.",
        "batch", "replace", True),
    How("new_rows", "New rows only get added", "Logs, events, orders that never change. Each run adds only the new rows.",
        "batch", "append", True, ("incremental_key",)),
    How("changes", "Rows change, a schedule is fine", "Customers, accounts: each run brings new and changed rows and "
        "updates them in place.", "batch", "merge", True, ("incremental_key", "primary_key")),
    How("live", "I need changes within seconds", "Reads the database's change log and keeps the copy current. The source "
        "admin must switch change capture on first.", "cdc", "merge", False),
    How("live_history", "Every change, as history", "Like the above, but keeps every insert, update and delete as a row.",
        "cdc", "append", False),
    How("events", "It's a stream of events (Kafka)", "Stays connected and writes events as they arrive.", "stream",
        "append", False),
    How("catalog", "Just show it in the catalog", "Copies no rows: tables, columns and lineage appear in the DataOS "
        "catalog.", "metadata", None, True),
)}

SCHEDULES = {"hourly": "0 * * * *", "every-6h": "0 */6 * * *", "daily": "0 2 * * *", "weekly": "0 2 * * 1"}
SCHEDULE_WORDS = {"hourly": "every hour", "every-6h": "every 6 hours", "daily": "every day at 02:00 UTC",
                  "weekly": "every Monday at 02:00 UTC"}


def hows_for(conn: Connector | None) -> list[How]:
    """The answers that make sense for this source (CDC only where the source supports it, and so on)."""
    if conn is None:
        return []
    return [h for h in HOWS.values() if h.type in conn.modes]


# ------------------------------------------------------------------ the request
class PipelineRequest(BaseModel):
    name: str
    how: str
    source_depot: str
    source_object: str = ""                 # schema.table, a collection or a topic; empty for metadata
    dest_depot: str = ""                    # empty for metadata
    dest_table: str = ""                    # default: raw.<object name>
    schedule: str = "daily"                 # a key of SCHEDULES or a cron line
    incremental_key: str = ""
    primary_key: str = ""
    mask: dict[str, str] = Field(default_factory=dict)        # column -> hash | redact | partial:N
    exclude_columns: list[str] = Field(default_factory=list)
    row_limit: int | None = None            # sql_limit, for a first trial
    metadata_mode: Literal["shallow", "deep"] = "shallow"
    compute: str = "engineering-compute"
    cpu: str = "200m"
    memory: str = "512Mi"
    cpu_limit: str = "1"
    memory_limit: str = "2Gi"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not NAME_RE.match(v):
            raise ValueError("use 3-48 lower-case letters, digits and '-', starting with a letter")
        return v

    @property
    def kind(self) -> How:
        return HOWS[self.how]

    def cron(self) -> str:
        s = self.schedule.strip()
        return SCHEDULES.get(s, s)

    def schedule_words(self) -> str:
        s = self.schedule.strip()
        return SCHEDULE_WORDS.get(s, f"on cron `{s}` (UTC)")

    def default_dest_table(self) -> str:
        leaf = re.sub(r"[^a-z0-9_]", "_", (self.source_object.split(".")[-1] or self.name).lower()).strip("_")
        return f"raw.{leaf or self.name.replace('-', '_')}"


def check(req: PipelineRequest, source: dict[str, Any], dest: dict[str, Any] | None) -> list[str]:
    """Everything wrong with the request, as sentences a person can act on. Empty means it can be deployed."""
    errs: list[str] = []
    if req.how not in HOWS:
        return [f"unknown choice {req.how!r}"]
    h = HOWS[req.how]
    conn = connector_of(source)
    if conn is None:
        errs.append(f"Depot {source.get('name')} is a {depot_kind(source) or 'kind of'} source this page can't move yet")
        return errs
    if h.type not in conn.modes:
        can = ", ".join(x.title.lower() for x in hows_for(conn)) or "nothing"
        errs.append(f"{conn.label} can't do “{h.title}”. It can: {can}")
    if h.type != "metadata":
        if not req.source_object.strip():
            errs.append(f"Say which {conn.object_word} to move")
        elif h.type == "stream":
            if not TOPIC_RE.match(req.source_object.strip()):
                errs.append("The topic name has characters a topic can't have")
        elif not TABLE_RE.match(req.source_object.strip()):
            errs.append(f"Write the {conn.object_word} as schema.name, e.g. public.orders")
        if dest is None:
            errs.append("Choose where the data goes")
        else:
            dc = connector_of(dest)
            if not (dc and dc.destination):
                errs.append(f"Depot {dest.get('name')} can't be written to by a pipeline")
        if req.dest_table and not TABLE_RE.match(req.dest_table.strip()):
            errs.append("Write the destination table as schema.table, e.g. raw.orders")
    for col in ("incremental_key", "primary_key"):
        v = getattr(req, col).strip()
        if col in h.needs and not v:
            errs.append({"incremental_key": "Say which column tells new or changed rows apart (e.g. updated_at, id)",
                         "primary_key": "Say which column identifies a row (e.g. id); without it rows get duplicated"}[col])
        if v and not all(COLUMN_RE.match(x.strip()) for x in v.split(",")):
            errs.append(f"{col.replace('_', ' ')}: {v!r} is not a column name")
    if h.scheduled and not CRON_RE.match(req.cron()):
        errs.append("The schedule must be hourly, every-6h, daily, weekly or a cron line like `30 1 * * *`")
    for colname, how in req.mask.items():
        if not COLUMN_RE.match(colname) or not (how in MASKS or re.fullmatch(r"partial:\d{1,2}", how)):
            errs.append(f"mask {colname}={how}: use hash, redact or partial:N")
    if any(not COLUMN_RE.match(c) for c in req.exclude_columns):
        errs.append("Columns to skip must be plain column names")
    if req.row_limit is not None and not (1 <= req.row_limit <= 10_000_000):
        errs.append("The row limit must be between 1 and 10,000,000")
    for v, rx, what in ((req.cpu, CPU_RE, "CPU"), (req.cpu_limit, CPU_RE, "CPU limit"),
                        (req.memory, MEM_RE, "memory"), (req.memory_limit, MEM_RE, "memory limit")):
        if not rx.match(v):
            errs.append(f"{what} {v!r} is not like 200m / 1 / 512Mi / 2Gi")
    if h.type == "metadata" and not conn.metadata_service:
        errs.append(f"{conn.label} can't be catalogued yet")
    return errs


def build_manifest(req: PipelineRequest, source: dict[str, Any], dest: dict[str, Any] | None, *,
                   owner: str | None = None) -> dict[str, Any]:
    """The Nilus resource for this request. Call check() first; this raises ValueError on the same problems."""
    errs = check(req, source, dest)
    if errs:
        raise ValueError("; ".join(errs))
    h, conn = HOWS[req.how], connector_of(source)
    assert conn is not None
    spec: dict[str, Any] = {
        "type": h.type, "compute": req.compute, "logLevel": "INFO",
        "resources": {"requests": {"cpu": req.cpu, "memory": req.memory},
                      "limits": {"cpu": req.cpu_limit, "memory": req.memory_limit}},
    }
    if h.scheduled:
        spec["schedule"] = {"crons": [req.cron()], "timezone": "UTC", "concurrencyPolicy": "Forbid"}
    src_addr = f"dataos://{source['name']}?purpose={pick_purpose(source, 'source')}"
    opts: dict[str, Any] = {}
    if h.type == "metadata":
        opts["service_type"] = conn.metadata_service
        spec["mode"] = req.metadata_mode
        spec["source"] = {"address": src_addr, "options": opts}
    else:
        obj = req.source_object.strip()
        if h.type == "cdc":
            opts.update({"strategy": "flatten", "max_table_nesting": "0"})
            cdc = {conn.cdc_include_key: obj, "topic.prefix": req.name.replace("-", "_")}
            if conn.kind == "postgresql":     # one replication slot and publication per pipeline
                ident = re.sub(r"[^a-z0-9_]", "_", f"dapp_{req.name}")[:60]
                cdc.update({"slot.name": ident, "publication.name": ident})
            spec["source"] = {"address": src_addr, "options": opts, "cdc": cdc}
        else:
            opts["source_table"] = obj
            if h.type == "stream":
                opts["run_in_loop"] = True
            if req.row_limit and h.type == "batch":
                opts["sql_limit"] = req.row_limit
            spec["source"] = {"address": src_addr, "options": opts}
        if req.incremental_key.strip() and h.type == "batch":
            opts["incremental_key"] = req.incremental_key.strip()
        if req.primary_key.strip() and h.type in ("batch", "stream"):    # CDC takes the table's own key
            opts["primary_key"] = req.primary_key.strip()
        if req.mask:
            opts["mask"] = dict(req.mask)
        if req.exclude_columns and h.type == "batch":
            opts["sql_exclude_columns"] = ",".join(req.exclude_columns)
        assert dest is not None
        sink_opts: dict[str, Any] = {"dest_table": (req.dest_table.strip() or req.default_dest_table()),
                                     "incremental_strategy": h.strategy}
        if h.type == "stream" and h.strategy == "merge" and req.primary_key:
            sink_opts["primary_key"] = req.primary_key.strip()
        spec["sink"] = {"address": f"dataos://{dest['name']}?purpose={pick_purpose(dest, 'sink')}", "options": sink_opts}
    m: dict[str, Any] = {
        "version": "v1alpha", "name": req.name, "type": "nilus",
        "tags": ["dapp", PIPELINE_TAG, f"dapp-pipeline-{h.type}"],
        "description": (req.description.strip() or describe(req, source, dest))[:240],
        "spec": spec,
    }
    if owner:
        m["owner"] = owner
    return m


def describe(req: PipelineRequest, source: dict[str, Any], dest: dict[str, Any] | None) -> str:
    """One sentence a person reads before deploying."""
    h = HOWS[req.how]
    when = f", {req.schedule_words()}" if h.scheduled else (", running continuously" if h.type in ("cdc", "stream")
                                                            else ", once")
    if h.type == "metadata":
        return f"Catalogue {source.get('name')} ({req.metadata_mode}){when}"
    to = f"{dest.get('name') if dest else '?'} {req.dest_table.strip() or req.default_dest_table()}"
    verb = {"replace": "copy", "append": "add new rows of", "merge": "keep up to date"}[h.strategy or "replace"]
    return f"{verb.capitalize()} {source.get('name')} {req.source_object.strip()} into {to}{when}"


# ------------------------------------------------------------------ reading DataOS answers
_NOT_ALLOWED = re.compile(r"object '([^']+)'")


def missing_access(error: str) -> list[dict[str, str]]:
    """The Depots and Secrets a DataOS 'not-allowed' error names, e.g.
    "not-allowed, subject 'users:id:x', predicate 'read', object 'dataos:system:ds:pgrds', authorization_atom_id 'depot-access'"."""
    out: list[dict[str, str]] = []
    for obj in _NOT_ALLOWED.findall(error or ""):
        if obj.startswith("dataos:system:ds:"):
            item = {"kind": "depot", "name": obj.removeprefix("dataos:system:ds:")}
        elif obj.startswith("dataos:type:secret:"):
            item = {"kind": "secret", "name": obj.removeprefix("dataos:type:secret:")}
        elif obj.startswith("dataos:type:compute:") or ":compute:" in obj:
            item = {"kind": "compute", "name": obj.rsplit(":", 1)[-1]}
        else:
            continue
        if item not in out:
            out.append(item)
    return out


def phase_of(runtime: dict[str, Any] | None) -> str:
    """The pipeline's latest run state from Poros' runtime answer: pending, initializing, running, completed, failed."""
    nodes = (runtime or {}).get("nodeDetails") or (runtime or {}).get("containerGroups") or []
    phases = [str(n.get("phase") or n.get("status") or "").lower() for n in nodes if isinstance(n, dict)]
    phases = [p for p in phases if p]
    for p in ("failed", "error", "running", "initializing", "pending"):
        if p in phases:
            return "failed" if p == "error" else p
    if phases and all(p in ("completed", "succeeded") for p in phases):
        return "completed"
    return phases[0] if phases else "waiting"


def run_row(r: dict[str, Any]) -> dict[str, Any]:
    """One Nilus Manager run, as the pages show it."""
    counts = {k: v for k, v in (r.get("records_count") or {}).items() if not str(k).startswith("_")}
    return {"started": r.get("started_at"), "finished": r.get("finished_at"), "seconds": r.get("duration_sec"),
            "ok": bool(r.get("is_success")), "error": r.get("error"), "rows": sum(int(v or 0) for v in counts.values()),
            "tables": counts, "dest": r.get("destination_table"), "source": r.get("source_table"),
            "mb": r.get("files_size_mb"), "memory_mb": r.get("memory_mb"), "by": r.get("run_as_user")}


def resource_id(name: str) -> str:
    """How Nilus Manager names a pipeline's runs (tested: workflow:v2alpha:<name>, not nilus:...)."""
    return f"workflow:v2alpha:{name}"


def summary(res: dict[str, Any]) -> dict[str, Any]:
    """A Nilus resource from Poros, as the list shows it."""
    spec = res.get("spec") or {}
    src, sink = spec.get("source") or {}, spec.get("sink") or {}
    status = res.get("status")
    agg = status.get("aggregateStatus") if isinstance(status, dict) else status
    cron = ((spec.get("schedule") or {}).get("crons") or [None])[0]
    return {"name": res.get("name"), "owner": res.get("owner"), "type": spec.get("type"),
            "source": _addr_depot(src.get("address")), "object": (src.get("options") or {}).get("source_table")
            or next(iter((src.get("cdc") or {}).values()), None),
            "dest": _addr_depot(sink.get("address")), "dest_table": (sink.get("options") or {}).get("dest_table"),
            "strategy": (sink.get("options") or {}).get("incremental_strategy"), "cron": cron,
            "description": res.get("description") or "", "status": agg, "created": res.get("createdAt") or res.get("created"),
            "made_here": PIPELINE_TAG in (res.get("tags") or [])}


def _addr_depot(addr: str | None) -> str | None:
    if not addr:
        return None
    m = re.match(r"dataos://([^?/]+)", addr)
    return m.group(1) if m else addr.split("://")[0]
