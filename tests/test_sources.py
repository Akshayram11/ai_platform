import json

import pytest
from pydantic import SecretStr

from dapp.sources import (
    SourceRequest,
    check,
    depot_manifest,
    from_url,
    parse_types,
    redact,
    secret_manifest,
)

# as GET /ds/api/v2/types returns them (2026-11-26, trimmed)
TYPES = parse_types([
    {"name": "postgresql",
     "specSchema": json.dumps({"type": "object", "required": ["subprotocol", "host", "port", "database"],
                               "properties": {"host": {"type": "string"}, "port": {"type": "integer"},
                                              "params": {"type": "object"}, "database": {"type": "string"},
                                              "subprotocol": {"type": "string"}}}),
     "secretSchema": json.dumps({"required": ["username", "password"],
                                 "properties": {"password": {"type": "string"}, "username": {"type": "string"}}})},
    {"name": "snowflake",
     "specSchema": json.dumps({"required": ["url", "database"], "properties": {"url": {"type": "string"},
                               "database": {"type": "string"}, "warehouse": {"type": "string"}}}),
     "secretSchema": json.dumps({"properties": {"username": {"type": "string"}, "password": {"type": "string"},
                                                "role": {"type": "string"}}})},
])
PW = "s3cr3t/p@ss:w0rd"
URL = "postgresql://postgres.abcd:s3cr3t%2Fp%40ss%3Aw0rd@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"


def test_fields_come_from_the_dataos_schema():
    pg = TYPES["postgresql"]
    assert [(f.key, f.side, f.required, f.kind) for f in pg.fields] == [
        ("database", "spec", True, "text"), ("host", "spec", True, "text"), ("port", "spec", True, "int"),
        ("subprotocol", "spec", True, "text"), ("params", "spec", False, "params"),
        ("password", "secret", True, "password"), ("username", "secret", True, "text")]
    assert pg.field("subprotocol").default == "postgresql"
    assert TYPES["snowflake"].field("role").kind == "text" and TYPES["snowflake"].field("password").kind == "password"


def test_a_supabase_connection_string_becomes_a_depot_and_a_secret():
    kind, spec, creds = from_url(URL)
    assert kind == "postgresql" and creds == {"username": "postgres.abcd", "password": PW}
    assert spec == {"subprotocol": "postgresql", "host": "aws-0-ap-south-1.pooler.supabase.com", "port": 5432,
                    "database": "postgres"}
    req = SourceRequest(name="supabase", type=kind, spec=spec, secret={k: SecretStr(v) for k, v in creds.items()},
                        preset="supabase")
    assert check(req, TYPES["postgresql"]) == []
    d = depot_manifest(req, TYPES["postgresql"], "engineering")
    assert d["spec"]["type"] == "postgresql" and d["spec"]["spec"]["params"] == {"sslmode": "require"}   # the preset
    assert d["spec"]["secrets"] == [{"id": "engineering:dapp-src-supabase", "purpose": p} for p in ("rw", "query", "scan")]
    assert "dapp-source" in d["tags"] and PW not in json.dumps(d)
    s = secret_manifest(req, "engineering")
    assert s["name"] == "dapp-src-supabase" and s["spec"] == {"type": "key-value", "data": creds}


def test_credentials_never_show():
    req = SourceRequest(name="supabase", type="postgresql", spec={}, secret={"password": SecretStr(PW)})
    for text in (repr(req), str(req), str(req.model_dump()), " ".join(check(req, TYPES["postgresql"]))):
        assert PW not in text
    msg = f"connect failed for postgresql://u:{PW}@h/db password={PW} token: abc123"
    out = redact(msg, req)
    assert PW not in out and "abc123" not in out and "•••" in out


def test_mistakes_become_sentences():
    with pytest.raises(ValueError, match="lower-case letters and digits"):
        SourceRequest(name="my-db", type="postgresql")
    req = SourceRequest(name="mydb", type="postgresql", spec={"host": "https://x.com/db", "port": "99999", "colour": "red"},
                        secret={"username": SecretStr("u")})
    errs = check(req, TYPES["postgresql"])
    assert "database is required" in errs and "password is required" in errs and "port must be between 1 and 65535" in errs
    assert "postgresql has no field colour" in errs and "host must be a host name, without http:// or a path" in errs
    assert check(req, None) == ["DataOS has no Depot type 'postgresql' in this tenant"]
    with pytest.raises(ValueError, match="starts with postgresql://"):
        from_url("jdbc:postgresql://h/db")
