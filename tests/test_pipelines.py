import pytest

from dapp.pipelines import (
    HOWS,
    PipelineRequest,
    build_manifest,
    check,
    connector_of,
    depot_summary,
    describe,
    hows_for,
    missing_access,
    phase_of,
    pick_purpose,
    resource_id,
    run_row,
    summary,
)

# shaped like the live tenant's Depots (2026-11-26)
PG = {"name": "pgrds", "owner": "rajat", "spec": {"type": "postgresql", "spec": {"database": "postgres"},
      "secrets": [{"id": "engineering:postgressecret", "purpose": p} for p in ("scan", "query", "rw")]}}
LH = {"name": "s3lhdepotrr", "owner": "rohit", "spec": {"type": "lakehouse", "spec": {"s3": {"bucket": "b"}},
      "secrets": [{"id": "engineering:s3_lh_sec_rr", "purpose": "rw"}]}}
SNOW = {"name": "snow", "spec": {"type": "snowflake", "secrets": [{"id": "engineering:sf", "purpose": "r"},
                                                                   {"id": "engineering:sf", "purpose": "rw"}]}}
KAFKA = {"name": "events", "spec": {"type": "kafka", "secrets": [{"id": "engineering:k", "purpose": "rw"}]}}
ODD = {"name": "weird", "spec": {"type": "elasticsearch"}}


def req(**kw):
    base = {"name": "orders-sync", "how": "once", "source_depot": "pgrds", "source_object": "public.orders",
            "dest_depot": "s3lhdepotrr"}
    return PipelineRequest(**{**base, **kw})


def test_purpose_comes_from_the_depot_never_assumed():
    assert pick_purpose(PG, "source") == "rw"          # pgrds has no ro: "?purpose=ro" failed live
    assert pick_purpose(SNOW, "source") == "r"          # the most limited one that reads
    assert pick_purpose(LH, "sink") == "rw"
    with pytest.raises(ValueError, match="no read-write purpose"):
        pick_purpose({"name": "x", "spec": {"secrets": [{"id": "a", "purpose": "r"}]}}, "sink")


def test_choices_follow_what_the_source_can_do():
    assert {h.key for h in hows_for(connector_of(PG))} == {"once", "refresh", "new_rows", "changes", "live",
                                                          "live_history", "catalog"}
    assert {h.key for h in hows_for(connector_of(SNOW))} == {"once", "refresh", "new_rows", "changes", "catalog"}
    assert {h.key for h in hows_for(connector_of(KAFKA))} == {"events"}
    assert hows_for(connector_of(ODD)) == []
    assert check(req(how="live"), SNOW, LH)[0].startswith("Snowflake can't do")
    assert "can't move yet" in check(req(), ODD, LH)[0]


def test_once_is_a_plain_batch_replace_without_schedule():
    m = build_manifest(req(row_limit=50), PG, LH, owner="me")
    assert (m["type"], m["version"], m["owner"]) == ("nilus", "v1alpha", "me")
    s = m["spec"]
    assert s["type"] == "batch" and "schedule" not in s
    assert s["source"] == {"address": "dataos://pgrds?purpose=rw",
                           "options": {"source_table": "public.orders", "sql_limit": 50}}
    assert s["sink"] == {"address": "dataos://s3lhdepotrr?purpose=rw",
                         "options": {"dest_table": "raw.orders", "incremental_strategy": "replace"}}
    assert "dapp-pipeline" in m["tags"] and m["description"].startswith("Copy pgrds public.orders into s3lhdepotrr raw.orders")


def test_changes_is_merge_with_both_keys_and_a_schedule():
    errs = check(req(how="changes"), PG, LH)
    assert any("identifies a row" in e for e in errs) and any("new or changed" in e for e in errs)
    m = build_manifest(req(how="changes", incremental_key="updated_at", primary_key="id", schedule="hourly",
                           mask={"email": "hash", "phone": "partial:3"}, exclude_columns=["raw_html"]), PG, LH)
    s = m["spec"]
    assert s["schedule"] == {"crons": ["0 * * * *"], "timezone": "UTC", "concurrencyPolicy": "Forbid"}
    assert s["source"]["options"] == {"source_table": "public.orders", "incremental_key": "updated_at", "primary_key": "id",
                                      "mask": {"email": "hash", "phone": "partial:3"}, "sql_exclude_columns": "raw_html"}
    assert s["sink"]["options"]["incremental_strategy"] == "merge"
    assert "every hour" in m["description"]


def test_new_rows_is_append_with_cursor():
    m = build_manifest(req(how="new_rows", incremental_key="created_at", schedule="30 1 * * *"), PG, LH)
    assert m["spec"]["sink"]["options"]["incremental_strategy"] == "append"
    assert m["spec"]["schedule"]["crons"] == ["30 1 * * *"]
    assert check(req(how="new_rows", incremental_key="id", schedule="sometimes"), PG, LH) == [
        "The schedule must be hourly, every-6h, daily, weekly or a cron line like `30 1 * * *`"]


def test_cdc_on_postgres_gets_its_own_slot_and_no_schedule():
    m = build_manifest(req(how="live"), PG, LH)
    s = m["spec"]
    assert s["type"] == "cdc" and "schedule" not in s
    assert s["source"]["options"] == {"strategy": "flatten", "max_table_nesting": "0"}
    assert s["source"]["cdc"] == {"table.include.list": "public.orders", "topic.prefix": "orders_sync",
                                  "slot.name": "dapp_orders_sync", "publication.name": "dapp_orders_sync"}
    assert s["sink"]["options"]["incremental_strategy"] == "merge"
    assert "running continuously" in m["description"]
    hist = build_manifest(req(how="live_history"), PG, LH)
    assert hist["spec"]["sink"]["options"]["incremental_strategy"] == "append"


def test_stream_runs_in_a_loop_and_metadata_has_no_sink():
    m = build_manifest(req(how="events", source_depot="events", source_object="orders.v1"), KAFKA, LH)
    assert m["spec"]["type"] == "stream" and "schedule" not in m["spec"]
    assert m["spec"]["source"]["options"] == {"source_table": "orders.v1", "run_in_loop": True}
    meta = build_manifest(req(how="catalog", source_object="", dest_depot="", metadata_mode="deep",
                              schedule="every-6h"), SNOW, None)
    s = meta["spec"]
    assert s["type"] == "metadata" and s["mode"] == "deep" and "sink" not in s
    assert s["source"] == {"address": "dataos://snow?purpose=r", "options": {"service_type": "snowflake"}}
    assert s["schedule"]["crons"] == ["0 */6 * * *"]


def test_bad_input_becomes_sentences():
    errs = check(req(source_object="orders; drop", dest_table="x", mask={"email": "rot13"}, cpu="lots"), PG, LH)
    assert "Write the table as schema.name, e.g. public.orders" in errs
    assert "Write the destination table as schema.table, e.g. raw.orders" in errs
    assert any(e.startswith("mask email=rot13") for e in errs) and any(e.startswith("CPU 'lots'") for e in errs)
    assert check(req(), PG, None) == ["Choose where the data goes"]
    assert "can't be written to" in check(req(), PG, KAFKA)[0]
    with pytest.raises(ValueError):
        build_manifest(req(source_object=""), PG, LH)
    with pytest.raises(ValueError, match="lower-case"):
        req(name="Bad Name")


def test_missing_access_reads_the_dataos_error():
    err = ("Invalid Parameter - failure validating tenant resource : could not resolve the DataOsAddress dataos://s3lhdepotrr"
           "?purpose=rw, error: 🔴 not-allowed, subject 'users:id:me', predicate 'read', object "
           "'dataos:system:ds:s3lhdepotrr', authorization_atom_id 'depot-access', collection 'engineering', could not resolve "
           "the DataOsAddress dataos://pgrds?purpose=ro, error: 🔴 not-allowed, subject 'users:id:me', predicate 'read', "
           "object 'dataos:system:ds:pgrds', authorization_atom_id 'depot-access'; predicate 'read', object "
           "'dataos:type:secret:engineering:postgressecret'")
    assert missing_access(err) == [{"kind": "depot", "name": "s3lhdepotrr"}, {"kind": "depot", "name": "pgrds"},
                                   {"kind": "secret", "name": "engineering:postgressecret"}]
    assert missing_access("purpose not found") == []


def test_reading_runs_and_runtime():
    assert resource_id("x") == "workflow:v2alpha:x"
    r = run_row({"started_at": "2026-11-26T06:20:01Z", "duration_sec": 16.8, "is_success": True,
                 "records_count": {"schemata": 31, "_nilus_pipeline_state": 1}, "destination_table": "dapp_test.schemata"})
    assert r["rows"] == 31 and r["tables"] == {"schemata": 31} and r["ok"]
    assert phase_of({"nodeDetails": [{"phase": "completed"}]}) == "completed"
    assert phase_of({"nodeDetails": [{"phase": "completed"}, {"phase": "running"}]}) == "running"
    assert phase_of({"nodeDetails": [{"phase": "Failed"}]}) == "failed"
    assert phase_of({}) == "waiting"


def test_summary_and_depot_summary():
    m = build_manifest(req(how="changes", incremental_key="u", primary_key="id"), PG, LH)
    row = summary({**m, "owner": "me", "status": {"aggregateStatus": "active"}})
    assert row["source"] == "pgrds" and row["dest"] == "s3lhdepotrr" and row["object"] == "public.orders"
    assert row["strategy"] == "merge" and row["cron"] == "0 2 * * *" and row["status"] == "active" and row["made_here"]
    d = depot_summary(PG)
    assert d["label"] == "PostgreSQL" and d["owner"] == "rajat" and d["secrets"] == ["engineering:postgressecret"]
    assert d["purposes"] == ["scan", "query", "rw"] and d["destination"] is True
    assert describe(req(), PG, LH) == "Copy pgrds public.orders into s3lhdepotrr raw.orders, once"
    assert set(HOWS) == {"once", "refresh", "new_rows", "changes", "live", "live_history", "events", "catalog"}
