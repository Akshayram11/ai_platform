"""Small models deployed from the dashboard: catalog, settings, the DataOS steps, and what a failure leaves behind."""

import base64
import io
import json

import pytest
from test_platform import CTX, FakePoros, make

from dapp import models
from dapp.models import (
    CATALOG,
    ModelDeploy,
    catalog_entry,
    default_ctx,
    defaults,
    defaults_for,
    explain_defaults,
    model_app_spec,
    render_volume,
)
from dapp.platform import Platform, PlatformError


def test_catalog_entries_are_usable():
    ids = [c.id for c in CATALOG]
    assert len(ids) == len(set(ids)) >= 10
    for c in CATALOG:
        assert models.NAME_RE.match(c.id) and not c.id.startswith(models.RESERVED_PREFIXES), c.id
        assert models.HF_RE.match(c.hf) and c.size_gb <= 2.5 and c.license, c.id       # small, and says what it is
        assert c.layers and c.kv_heads and c.head_dim and c.native_ctx >= 4096, c.id    # its own architecture numbers are filled in
        d = ModelDeploy(name=c.id, model_id=c.id, **defaults_for(c)).check()               # its defaults pass their own checks
        assert d.source == c.hf and d.app.startswith("model-") and d.volume.endswith("-data")
        assert 2048 <= defaults_for(c)["ctx"] <= min(c.native_ctx, models.CTX_CAP)
        assert explain_defaults(c)


def test_defaults_depend_on_the_model():
    """Same code path, different model: context, memory (from the KV cache) and sampling follow what the model needs."""
    by = {c.id: defaults_for(c) for c in CATALOG}
    assert (by["qwen2.5-0.5b"]["cpu_limit"], by["qwen2.5-0.5b"]["memory_limit"], by["qwen2.5-0.5b"]["disk_gi"]) == ("2", "1.5Gi", 2)
    assert by["qwen3-0.6b"]["memory_limit"] == "2.5Gi"                       # a tiny file, but 114 KB of KV cache per token (8 KV heads)
    assert by["qwen2.5-1.5b"]["memory_limit"] == "2.5Gi" and by["smollm2-1.7b"]["memory_limit"] == "3.5Gi"   # 28 KB vs 196 KB per token
    assert by["phi3.5-mini"]["ctx"] == 4096 and by["phi3.5-mini"]["memory_limit"] == "5Gi"     # no grouped-query attention: shorter window
    assert by["gemma2-2b"]["ctx"] == 8192 and default_ctx(catalog_entry("smollm2-360m")) == 8192   # capped by what it was trained for
    assert all(float(by[i]["memory_limit"][:-2]) * 1024 >= catalog_entry(i).size_gb * 1024 * 1.15 for i in by)
    # recommended sampling comes from each model's own settings; models without any leave it to llama.cpp
    assert {k: by["qwen2.5-0.5b"].get(k) for k in ("temperature", "top_k", "top_p", "repeat_penalty")} == {
        "temperature": 0.7, "top_k": 20, "top_p": 0.8, "repeat_penalty": 1.1}
    assert {k: by["qwen3-4b"].get(k) for k in ("temperature", "top_k", "top_p", "min_p")} == {
        "temperature": 0.6, "top_k": 20, "top_p": 0.95, "min_p": 0.0}
    assert (by["llama3.2-3b"]["temperature"], by["llama3.2-3b"]["top_p"], "top_k" in by["llama3.2-3b"]) == (0.6, 0.9, False)
    assert (by["gemma3-4b"]["temperature"], by["gemma3-4b"]["top_k"]) == (1.0, 64)
    assert not any(k in by["phi3.5-mini"] for k in ("temperature", "top_k", "top_p", "min_p", "repeat_penalty"))
    text = " ".join(explain_defaults(catalog_entry("phi3.5-mini")))
    assert "KV cache" in text and "4096-token context" in text and "131072" in text and "llama.cpp's own defaults" in text
    assert "generation_config.json" in " ".join(explain_defaults(catalog_entry("qwen2.5-3b")))
    env = ModelDeploy(name="qq", model_id="qwen3-4b", **by["qwen3-4b"]).check().server_env()
    assert env["LLAMA_ARG_TEMPERATURE"] == "0.6" and env["LLAMA_ARG_MIN_P"] == "0" and env["LLAMA_ARG_CTX_SIZE"] == "8192"


def test_defaults_grow_with_the_model():
    small, big = defaults(0.46), defaults(2.33)
    assert (small["cpu_limit"], small["memory_limit"], small["disk_gi"]) == ("2", "2Gi", 2)   # a custom model: size known, KV cache assumed
    assert big["cpu_limit"] == "4" and float(big["memory_limit"][:-2]) >= 3.5 and big["disk_gi"] >= 5


def test_every_llama_setting_reaches_the_server_env():
    d = ModelDeploy(name="qq", model_id="qwen2.5-0.5b", ctx=8192, parallel=2, threads=4, batch=1024, ubatch=256, flash_attn="on",
                    cache_type_k="q8_0", cache_type_v="q8_0", max_tokens=512, reasoning="off", jinja="off", context_shift="on",
                    cont_batching="off", timeout_s=120, temperature=0.3, top_k=20, top_p=0.9, min_p=0.1, repeat_penalty=1.1,
                    **{k: v for k, v in defaults(0.46).items() if k in ("cpu", "cpu_limit", "memory", "memory_limit", "disk_gi")})
    env = d.check().server_env()
    assert env == {"LLAMA_ARG_PORT": "8080", "LLAMA_ARG_CTX_SIZE": "8192", "LLAMA_ARG_N_PARALLEL": "2", "LLAMA_ARG_THREADS": "4",
                   "LLAMA_ARG_BATCH": "1024", "LLAMA_ARG_UBATCH": "256", "LLAMA_ARG_FLASH_ATTN": "on",
                   "LLAMA_ARG_CACHE_TYPE_K": "q8_0", "LLAMA_ARG_CACHE_TYPE_V": "q8_0", "LLAMA_ARG_N_PREDICT": "512",
                   "LLAMA_ARG_REASONING": "off", "LLAMA_ARG_JINJA": "0", "LLAMA_ARG_CONTEXT_SHIFT": "1",
                   "LLAMA_ARG_CONT_BATCHING": "0", "LLAMA_ARG_TIMEOUT": "120", "LLAMA_ARG_TEMPERATURE": "0.3",
                   "LLAMA_ARG_TOP_K": "20", "LLAMA_ARG_TOP_P": "0.9", "LLAMA_ARG_MIN_P": "0.1", "LLAMA_ARG_REPEAT_PENALTY": "1.1"}
    plain = ModelDeploy(name="qq", model_id="qwen2.5-0.5b", **defaults(0.46)).check().server_env()
    assert not any(k.endswith(("TEMPERATURE", "TOP_K", "TOP_P", "MIN_P", "REPEAT_PENALTY")) for k in plain)   # unset = llama.cpp's own


@pytest.mark.parametrize("change, message", [
    ({"name": "gpt-4o"}, "provider's own prefix"), ({"name": "X"}, "name:"),
    ({"memory_limit": "512Mi", "memory": "256Mi"}, "too small to load"), ({"cpu": "4", "cpu_limit": "2"}, "cpu"),
    ({"cpu_limit": "16"}, "8 cores"), ({"memory_limit": "32Gi"}, "16Gi"), ({"disk_gi": 1}, "disk"), ({"ctx": 100}, "context"),
    ({"ctx": 131072, "parallel": 4}, "too large"), ({"ubatch": 4096}, "batch"), ({"temperature": 9.0}, "temperature"),
    ({"timeout_s": 5}, "timeout"), ({"quant": "banana"}, "quantization"), ({"compute": "Bad Name"}, "compute"),
    ({"model_id": "nope"}, "unknown model"), ({"model_id": "", "hf": "not a repo"}, "custom model")])
def test_a_wrong_setting_says_what_to_change(change, message):
    base = {"name": "qq", "model_id": "qwen2.5-0.5b", **defaults(0.46)}
    with pytest.raises(ValueError, match=message):
        ModelDeploy(**{**base, **change}).check()


def test_custom_models_and_quantizations():
    d = ModelDeploy(name="mine", model_id="", hf="Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q8_0", **defaults(0.63)).check()
    assert d.source == "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q8_0" and d.size_gb is None
    pick = ModelDeploy(name="mine", model_id="qwen2.5-0.5b", quant="q5_k_m", size_gb_hint=0.49, **defaults(0.49)).check()
    assert pick.source == "Qwen/Qwen2.5-0.5B-Instruct-GGUF:q5_k_m" and pick.size_gb == 0.49    # the choice, with its real size
    assert models.app_name("Qwen2.5-1.5B") == "model-qwen2-5-1-5b" and len(models.app_name("a" * 80)) <= 44


def test_hf_quants_reads_the_repo_listing(monkeypatch):
    listing = {"siblings": [{"rfilename": "m-q4_k_m.gguf", "size": 2**29}, {"rfilename": "m-Q8_0.gguf", "size": 2**30},
                            {"rfilename": "m-Q8_0-00001-of-00002.gguf", "size": 5}, {"rfilename": "mmproj-F16.gguf", "size": 5},
                            {"rfilename": "README.md", "size": 1}]}
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda req, timeout: io.StringIO(json.dumps(listing)))
    models._hf_cache.clear()
    assert models.hf_quants("a/b") == {"Q4_K_M": 0.5, "Q8_0": 1.0}
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("offline")))
    models._hf_cache.clear()
    assert models.hf_quants("c/d") is None                                                     # offline: don't block a deploy


def test_the_app_is_a_private_llama_server_on_its_own_volume():
    d = ModelDeploy(name="qwen-test", model_id="qwen2.5-0.5b", **defaults(0.46)).check()
    spec = model_app_spec(d, compute="engineering-compute")
    assert spec.image == models.LLAMA_IMAGE and spec.command == ["/opt/dapp/start.sh"] and spec.public is False and not spec.uses_login
    assert spec.args == ["-hf", "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M", "--alias", "qwen-test", "--host", "0.0.0.0", "--metrics"]
    assert spec.hostname == "model-qwen-test" and spec.secrets == ["dapp-model-qwen-test-key"] and spec.metrics
    assert [(v.id, v.directory) for v in spec.volumes] == [("model-qwen-test-data", "/models")]
    vol = render_volume(d, tenant="ai", dataplane="poldp01")
    assert vol["type"] == "volume" and vol["volume"] == {"size": "2Gi", "type": "disk", "dataplane": "poldp01"}


class FakeGateway:
    def __init__(self, known=(), fail_register=False):
        self.known, self.fail_register, self.registered, self.removed = list(known), fail_register, [], []

    def list_models(self, tenant=None):
        return self.known

    def register_model(self, body):
        if self.fail_register:
            raise PlatformError("gateway: name taken (HTTP 409)")
        self.registered.append(body)
        return body

    def unregister_model(self, tenant, name):
        self.removed.append(name)
        return {"deleted": 1}


def deploy(p, gw, **over):
    p._gateway = lambda: gw
    p._wait_for = lambda *a, **k: None
    d = ModelDeploy(name="qwen-test", model_id="qwen2.5-0.5b", **{**defaults(0.46), **over})
    return p.deploy_model(d, compute="engineering-compute", dataplane="poldp01")


def test_deploy_creates_volume_key_app_and_registers_it():
    p, fake = make()
    gw = FakeGateway()
    out = deploy(p, gw)
    assert out["app"] == "model-qwen-test" and out["url"].endswith("/model-qwen-test/")
    assert {k for k in fake.store} == {("volume", "model-qwen-test-data"), ("secret", "dapp-model-qwen-test-key"),
                                       ("app", "model-qwen-test")}
    vol = fake.store[("volume", "model-qwen-test-data")]
    assert vol["owner"] == "alice" and vol["spec"]["size"] == "2Gi" and vol["spec"]["dataplane"] == "poldp01"
    app = fake.store[("app", "model-qwen-test")]["spec"]
    assert app["service"]["image"] == models.LLAMA_IMAGE and app["service"]["volumes"][0]["id"] == "model-qwen-test-data"
    assert app["service"]["stateful"] is True and app["service"]["envs"]["LLAMA_ARG_CTX_SIZE"].strip('"') == "4096"
    token = base64.b64decode(fake.store[("secret", "dapp-model-qwen-test-key")]["spec"]["data"]["LLAMA_API_KEY"]).decode()
    reg = gw.registered[0]
    assert reg["key"] == token and len(token) >= 32                          # the gateway gets the very key the server holds
    assert (reg["name"], reg["app"], reg["url"]) == ("qwen-test", "model-qwen-test", "http://model-qwen-test:8080")


def test_a_failed_deploy_leaves_nothing_behind():
    p, fake = make()
    with pytest.raises(PlatformError, match="name taken"):
        deploy(p, FakeGateway(fail_register=True))
    assert fake.store == {}                                                   # volume, key and app all removed again


def test_deploy_refuses_duplicates_and_too_many():
    p, fake = make()
    with pytest.raises(PlatformError, match="already exists"):
        deploy(p, FakeGateway(known=[{"name": "qwen-test", "source": "config"}]))
    mine = [{"name": f"m{i}", "source": "dashboard", "owner": "alice"} for i in range(models.MAX_MODELS_PER_PERSON)]
    with pytest.raises(PlatformError, match="delete one first"):
        deploy(p, FakeGateway(known=mine))
    with pytest.raises(ValueError, match="too small to load"):
        deploy(p, FakeGateway(), memory_limit="512Mi", memory="256Mi")
    assert fake.store == {}


def test_delete_stops_routing_then_removes_the_app_and_frees_the_volume(monkeypatch):
    p, fake = make()
    gw = FakeGateway()
    deploy(p, gw)
    gw.known = [{"name": "qwen-test", "source": "dashboard", "owner": "alice", "app": "model-qwen-test"}]
    removed = []
    monkeypatch.setattr(p, "delete", lambda app, purge=False: removed.append((app, purge)) or {"output": "ok"})
    out = p.delete_model("qwen-test")
    assert gw.removed == ["qwen-test"] and removed == [("model-qwen-test", True)] and out["volume"] == "model-qwen-test-data"
    with pytest.raises(PlatformError, match="no model"):
        p.delete_model("missing")
    gw.known = [{"name": "small", "source": "config", "app": "llm-small"}]
    with pytest.raises(PlatformError, match="gateway's own configuration"):
        p.delete_model("small")
    assert p.delete_model_volume("model-qwen-test-data", timeout=1, every=0.01) is True
    assert ("volume", "model-qwen-test-data") not in fake.store


def test_ctx_and_poros_fixture_are_the_platform_tests():
    assert CTX.user_id == "alice" and isinstance(FakePoros(), FakePoros) and Platform
