import json

import httpx
import pytest

from dapp.client import DataOS
from dapp.config import Context
from dapp.ctl import ApiWriter
from dapp.pipelines import PipelineRequest
from dapp.platform import PipelineAccessError, Platform, PlatformError

BASE = "/poros/api/v2/tenants/ai"
PG = {"name": "pgrds", "type": "depot", "owner": "rajat", "spec": {"type": "postgresql", "owners": ["rajat"],
      "secrets": [{"id": "ai:pgsecret", "purpose": p} for p in ("scan", "rw")]}}
LH = {"name": "lake", "type": "depot", "owner": "rohit", "spec": {"type": "lakehouse",
      "secrets": [{"id": "ai:lakesecret", "purpose": "rw"}]}}
NOT_ALLOWED = ("failure validating tenant resource : could not resolve the DataOsAddress dataos://lake?purpose=rw, error: "
               "not-allowed, subject 'users:id:{me}', predicate 'read', object 'dataos:system:ds:lake', "
               "authorization_atom_id 'depot-access'")


class Fake:
    """Poros (resources), Nilus Manager (runs) and Heimdall (who am I) for one user."""

    def __init__(self, me: str, *, allowed: set[str] = frozenset({"pgrds", "lake"}), tags=()):
        self.me, self.allowed, self.tags = me, set(allowed), list(tags)
        self.store = {("depot", "pgrds"): PG, ("depot", "lake"): LH}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path.startswith("/heimdall/api/v1/users/"):
            return httpx.Response(200, json={"id": self.me, "tags": self.tags})
        if path.startswith("/nilus-manager/api/v1/tenants/ai/pipelines/workflow:v2alpha:"):
            if path.endswith("/runs"):
                return httpx.Response(200, json={"total_count": 1, "pipelines": [
                    {"started_at": "t", "duration_sec": 16.8, "is_success": True,
                     "records_count": {"orders": 31, "_nilus_pipeline_state": 1}}]})
            return httpx.Response(200, json={"stats": {"total_runs": 1, "success_rate": 100.0}})
        parts = path[len(BASE):].strip("/").split("/")
        if req.method == "GET" and len(parts) == 2:
            rows = [v for (t, _), v in self.store.items() if t == parts[1]]
            tags = req.url.params.get("tags")
            if tags:
                rows = [r for r in rows if set(tags.split(",")) <= set(r.get("tags") or [])]
            return httpx.Response(200, json={"resources": rows})
        if req.method == "GET" and len(parts) == 3:
            r = self.store.get((parts[1], parts[2]))
            return httpx.Response(200, json=r) if r else httpx.Response(404, json={})
        if req.method == "GET" and parts[-1] == "log":
            return httpx.Response(200, text="Successfully finished loading data from 'postgresql' to 'lakehouse'")
        if req.method == "GET" and parts[-1] == "runtime":
            return httpx.Response(200, json={"nodeDetails": [{"name": "x-batch-execute", "phase": "completed"}]})
        if req.method in ("POST", "PUT"):
            body = json.loads(req.content)
            if body["type"] == "nilus":
                for addr in (body["spec"].get("source", {}).get("address", ""), body["spec"].get("sink", {}).get("address", "")):
                    depot = addr.removeprefix("dataos://").split("?")[0]
                    if depot and depot not in self.allowed:
                        return httpx.Response(400, json={"message": NOT_ALLOWED.format(me=self.me).replace("lake", depot)})
            body.setdefault("owner", self.me)
            self.store[(body["type"], body["name"])] = body
            return httpx.Response(200, json=body)
        if req.method == "DELETE":
            return httpx.Response(200 if self.store.pop((parts[1], parts[2]), None) else 404, json={})
        return httpx.Response(400, json={"path": path})


def make(me="alice", **kw):
    fake = Fake(me, **kw)
    client = DataOS(Context("t", "x.dataos.cloud", "ai", me, "key", None), transport=httpx.MockTransport(fake))
    return Platform(client, writer=ApiWriter(client)), fake


def req(**kw):
    return PipelineRequest(**{"name": "orders", "how": "once", "source_depot": "pgrds", "source_object": "public.orders",
                              "dest_depot": "lake", **kw})


def test_deploy_list_runs_rerun_delete():
    p, fake = make()
    assert [d["name"] for d in p.depots()] == ["lake", "pgrds"]
    done = p.deploy_pipeline(req())
    assert done["describe"].startswith("Copy pgrds public.orders into lake raw.orders")
    m = fake.store[("nilus", "orders")]
    assert m["owner"] == "alice" and m["spec"]["source"]["address"] == "dataos://pgrds?purpose=rw"
    with pytest.raises(PlatformError, match="already exists"):
        p.deploy_pipeline(req())
    rows = p.pipelines()
    assert rows[0]["name"] == "orders" and rows[0]["source"] == "pgrds" and rows[0]["made_here"]
    assert p.pipeline_phase("orders") == "completed"
    runs = p.pipeline_runs("orders")
    assert runs[0]["rows"] == 31 and runs[0]["ok"]
    assert ("GET", "/nilus-manager/api/v1/tenants/ai/pipelines/workflow:v2alpha:orders/runs") in fake.calls
    assert p.pipeline_stats("orders")["total_runs"] == 1
    n = len(fake.calls)
    p.rerun_pipeline("orders", every=0)                       # delete, wait until gone, create again
    assert [c[0] for c in fake.calls[n:]][:2] == ["GET", "DELETE"] and ("nilus", "orders") in fake.store
    p.delete_pipeline("orders")
    assert ("nilus", "orders") not in fake.store


def test_missing_access_is_reported_not_granted():
    p, fake = make(allowed={"pgrds"})
    with pytest.raises(PipelineAccessError) as e:
        p.deploy_pipeline(req())
    assert e.value.missing == [{"kind": "depot", "name": "lake"}] and "Ask its owner" in str(e.value)
    assert not any(t == "grant" for t, _ in fake.store)      # nothing was granted on the way


def test_only_the_depot_owner_or_a_tenant_admin_grants_one_depot():
    p, fake = make("bob")
    with pytest.raises(PlatformError, match="only the owner"):
        p.grant_depot_access("lake", "carol")
    owner, fake = make("rohit")
    out = owner.grant_depot_access("lake", "carol", reason="orders pipeline")
    grants = [v for (t, _), v in fake.store.items() if t == "grant"]
    use = {g["spec"]["policy_use_case_id"]: g["spec"] for g in grants}
    assert set(use) == {"use-specific-depot", "use-specific-secret"}       # never use-any-*
    assert use["use-specific-depot"]["values"] == {"depot-access": [{"depot": "lake"}]}
    assert use["use-specific-depot"]["subjects"] == ["users:id:carol"] and use["use-specific-depot"]["collection"] == "ai"
    assert use["use-specific-secret"]["values"] == {"read-specific-secret-governance": [{"id": "ai:lakesecret"}]}
    assert use["use-specific-secret"]["collection"] == "system"
    assert "approved by rohit" in use["use-specific-depot"]["notes"] and out["secrets"] == ["ai:lakesecret"]
    listed = owner.depot_grants("lake")
    assert {g["name"] for g in listed} == set(out["grants"])
    owner.revoke_depot_grant("lake", out["grants"][0])
    with pytest.raises(PlatformError, match="not a dapp grant"):
        owner.revoke_depot_grant("lake", "someone-elses-grant")
    admin, _ = make("tina", tags=["roles:id:ai-tenant-admin"])
    assert admin.can_grant(PG)
