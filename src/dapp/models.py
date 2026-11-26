"""Small language models you can deploy on DataOS from the dashboard (Models tab).

A model runs as a DataOS app: llama.cpp's server (the image of `apps/llm-small`) downloads a GGUF file from Hugging Face on its
first start into its own Volume, and serves the OpenAI API on port 8080, private (only the gateway reaches it, with the app's own
key). The gateway registers it under a name, so callers use it with `model: <name>` like any other model, with the same keys and
metering. Everything here is pure data and validation; `Platform.deploy_model` does the DataOS calls.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .spec import AppSpec

LLAMA_IMAGE = os.environ.get("DAPP_MODEL_IMAGE", "docker.io/tmdcio/llm-small:b12679-m1")
MODEL_TAG = "dapp-model"
MAX_MODELS_PER_PERSON = int(os.environ.get("DAPP_MAX_MODELS", "5"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,38}[a-z0-9]$")
HF_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+(:[A-Za-z0-9_.-]+)?$")
RESERVED_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt", "text-", "claude", "davinci", "babbage", "whisper", "dall-e", "tts",
                     "omni", "gemini", "ft:", "ada", "curie", "computer-use", "codex")


KV_BUDGET_GB = 1.5      # the most KV cache a default context may need: bigger models get a shorter context instead of more memory
CTX_CAP = 8192          # a CPU server rarely needs more window than this; people can raise it in Customize


@dataclass(frozen=True)
class CatalogModel:
    id: str
    title: str
    params: str          # as the maker calls it
    repo: str            # Hugging Face repo with the GGUF files
    quant: str
    size_gb: float       # the GGUF file, checked against Hugging Face
    license: str
    note: str
    # from the model's own config.json (what its KV cache costs, how long a context it was trained for)
    layers: int = 0
    kv_heads: int = 0
    head_dim: int = 0
    native_ctx: int = 4096
    # what its makers recommend for sampling (generation_config.json / model card); None: llama.cpp's own default
    temperature: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    sampling_from: str = ""

    @property
    def hf(self) -> str:
        """What llama-server's `-hf` takes."""
        return f"{self.repo}:{self.quant}"

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}"

    @property
    def kv_bytes_per_token(self) -> int:
        """KV cache per token at f16: keys and values, every layer."""
        return 2 * self.layers * self.kv_heads * self.head_dim * 2


# Ungated GGUF repos, sizes read from Hugging Face. Q4_K_M unless the maker only publishes Q8_0 for that size. Architecture numbers and
# recommended sampling come from each model's own config.json / generation_config.json (ungated mirrors for Llama and Gemma).
CATALOG: tuple[CatalogModel, ...] = (
    CatalogModel("smollm2-360m", "SmolLM2 360M Instruct", "0.36B", "HuggingFaceTB/SmolLM2-360M-Instruct-GGUF", "Q8_0", 0.36,
                 "Apache-2.0", "The tiniest: simple rewriting and short answers, very fast on one CPU.",
                 32, 5, 64, 8192, temperature=0.2, top_p=0.9, sampling_from="the model card"),
    CatalogModel("qwen2.5-0.5b", "Qwen2.5 0.5B Instruct", "0.5B", "Qwen/Qwen2.5-0.5B-Instruct-GGUF", "Q4_K_M", 0.46,
                 "Apache-2.0", "What `small` is: chat, summaries, explaining numbers. Multilingual.",
                 24, 2, 64, 32768, temperature=0.7, top_k=20, top_p=0.8, repeat_penalty=1.1,
                 sampling_from="its generation_config.json"),
    CatalogModel("qwen3-0.6b", "Qwen3 0.6B", "0.6B", "Qwen/Qwen3-0.6B-GGUF", "Q8_0", 0.60, "Apache-2.0",
                 "Newer Qwen with a thinking mode; still tiny.",
                 28, 8, 128, 40960, temperature=0.6, top_k=20, top_p=0.95, min_p=0.0, sampling_from="its thinking-mode settings"),
    CatalogModel("llama3.2-1b", "Llama 3.2 1B Instruct", "1B", "bartowski/Llama-3.2-1B-Instruct-GGUF", "Q4_K_M", 0.75,
                 "Llama 3.2 community licence", "Meta's small chat model; good English.",
                 16, 8, 64, 131072, temperature=0.6, top_p=0.9, sampling_from="its generation_config.json"),
    CatalogModel("gemma3-1b", "Gemma 3 1B Instruct", "1B", "unsloth/gemma-3-1b-it-GGUF", "Q4_K_M", 0.75, "Gemma terms",
                 "Google's small model; tidy short answers.",
                 26, 1, 256, 32768, temperature=1.0, top_k=64, top_p=0.95, min_p=0.0, sampling_from="Google's recommendation"),
    CatalogModel("smollm2-1.7b", "SmolLM2 1.7B Instruct", "1.7B", "HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF", "Q4_K_M", 0.98,
                 "Apache-2.0", "A step up from 360M at the same low cost.",
                 24, 32, 64, 8192, temperature=0.2, top_p=0.9, sampling_from="the model card"),
    CatalogModel("qwen2.5-1.5b", "Qwen2.5 1.5B Instruct", "1.5B", "Qwen/Qwen2.5-1.5B-Instruct-GGUF", "Q4_K_M", 1.04,
                 "Apache-2.0", "Clearly better than 0.5B for instructions and structured output.",
                 28, 2, 128, 32768, temperature=0.7, top_k=20, top_p=0.8, repeat_penalty=1.1,
                 sampling_from="its generation_config.json"),
    CatalogModel("gemma2-2b", "Gemma 2 2B Instruct", "2B", "bartowski/gemma-2-2b-it-GGUF", "Q4_K_M", 1.59, "Gemma terms",
                 "Strong for its size; good summaries.", 26, 4, 256, 8192),
    CatalogModel("qwen3-1.7b", "Qwen3 1.7B", "1.7B", "Qwen/Qwen3-1.7B-GGUF", "Q8_0", 1.71, "Apache-2.0",
                 "Thinking mode, solid reasoning for the size.",
                 28, 8, 128, 40960, temperature=0.6, top_k=20, top_p=0.95, min_p=0.0, sampling_from="its thinking-mode settings"),
    CatalogModel("llama3.2-3b", "Llama 3.2 3B Instruct", "3B", "bartowski/Llama-3.2-3B-Instruct-GGUF", "Q4_K_M", 1.88,
                 "Llama 3.2 community licence", "Good all-rounder; needs about 4 GB memory.",
                 28, 8, 128, 131072, temperature=0.6, top_p=0.9, sampling_from="its generation_config.json"),
    CatalogModel("qwen2.5-3b", "Qwen2.5 3B Instruct", "3B", "Qwen/Qwen2.5-3B-Instruct-GGUF", "Q4_K_M", 1.96,
                 "Qwen research licence", "Better answers, slower: about 3 times the 0.5B's time per word.",
                 36, 2, 128, 32768, temperature=0.7, top_k=20, top_p=0.8, repeat_penalty=1.05,
                 sampling_from="its generation_config.json"),
    CatalogModel("phi3.5-mini", "Phi-3.5 mini Instruct", "3.8B", "bartowski/Phi-3.5-mini-instruct-GGUF", "Q4_K_M", 2.23, "MIT",
                 "Strong reasoning and code for its size. Its KV cache is large, so the default context is shorter.",
                 32, 32, 96, 131072),
    CatalogModel("gemma3-4b", "Gemma 3 4B Instruct", "4B", "unsloth/gemma-3-4b-it-GGUF", "Q4_K_M", 2.32, "Gemma terms",
                 "The most capable here; slow on CPU, give it 4+ cores.",
                 34, 4, 256, 131072, temperature=1.0, top_k=64, top_p=0.95, min_p=0.0, sampling_from="Google's recommendation"),
    CatalogModel("qwen3-4b", "Qwen3 4B", "4B", "Qwen/Qwen3-4B-GGUF", "Q4_K_M", 2.33, "Apache-2.0",
                 "Best reasoning here; slow on CPU, give it 4+ cores.",
                 36, 8, 128, 40960, temperature=0.6, top_k=20, top_p=0.95, min_p=0.0, sampling_from="its thinking-mode settings"),
)


# A rough guide for choosing, not a benchmark: quality (1-5) from size and generation, and what each is good at.
PROFILES: dict[str, tuple[int, tuple[str, ...], str]] = {
    "smollm2-360m": (1, ("fastest", "rewriting", "short answers"), "light"),
    "qwen2.5-0.5b": (2, ("fast", "summaries", "multilingual"), "light"),
    "qwen3-0.6b": (2, ("fast", "thinking mode", "multilingual"), "light"),
    "llama3.2-1b": (2, ("fast", "English chat", "long context"), "light"),
    "gemma3-1b": (2, ("fast", "tidy answers", "long context"), "light"),
    "smollm2-1.7b": (2, ("light", "general chat"), "balanced"),
    "qwen2.5-1.5b": (3, ("structured output", "multilingual", "instructions"), "balanced"),
    "gemma2-2b": (3, ("summaries", "general chat"), "balanced"),
    "qwen3-1.7b": (3, ("thinking mode", "reasoning", "multilingual"), "balanced"),
    "llama3.2-3b": (3, ("all-rounder", "English chat", "long context"), "balanced"),
    "qwen2.5-3b": (4, ("accurate", "structured output", "multilingual"), "capable"),
    "phi3.5-mini": (4, ("reasoning", "code", "long context"), "capable"),
    "gemma3-4b": (4, ("most capable", "multilingual", "long context"), "capable"),
    "qwen3-4b": (5, ("most capable", "thinking mode", "reasoning"), "capable"),
}
TOKENS_PER_GB_S = 14.0   # CPU generation is limited by memory bandwidth: tokens/s ≈ 14 / file size in GB (measured: 0.46 GB → 29.5/s,
                         # 2.23 GB → 6.4/s on 2 and 4 cores)


def quality_of(m: CatalogModel) -> int:
    return PROFILES.get(m.id, (3, (), "balanced"))[0]


def traits_of(m: CatalogModel) -> tuple[str, ...]:
    return PROFILES.get(m.id, (3, (), "balanced"))[1]


def tokens_per_second(size_gb: float | None) -> float:
    return min(45.0, TOKENS_PER_GB_S / size_gb) if size_gb else 10.0


def speed_of(size_gb: float | None) -> tuple[int, str]:
    """(1-5, a word) for how fast it writes, from the file size."""
    t = tokens_per_second(size_gb)
    for floor, stars, word in ((30, 5, "very fast"), (18, 4, "fast"), (10, 3, "moderate"), (6, 2, "slowish")):
        if t >= floor:
            return stars, word
    return 1, "slow"


def tier_of(m: CatalogModel) -> str:
    """light, balanced or capable: how much model (and memory) you are buying."""
    return PROFILES.get(m.id, (3, (), "balanced"))[2]


def rates() -> dict[str, Any]:
    """What a CPU-hour, a GB-hour and a GB of disk cost, for the estimate. DataOS doesn't bill apps here, so these are your own
    numbers (cloud on-demand list prices by default): set DAPP_COST_CPU_HOUR, DAPP_COST_GB_HOUR, DAPP_COST_DISK_GB_MONTH."""
    return {"cpu_hour": float(os.environ.get("DAPP_COST_CPU_HOUR", "0.04")), "gb_hour": float(os.environ.get("DAPP_COST_GB_HOUR", "0.005")),
            "disk_gb_month": float(os.environ.get("DAPP_COST_DISK_GB_MONTH", "0.10")),
            "currency": os.environ.get("DAPP_COST_CURRENCY", "$")}


def estimate(*, cpu: str, cpu_limit: str, memory: str, memory_limit: str, disk_gi: float, size_gb: float | None) -> dict[str, Any]:
    """What running it costs and how fast it answers. "Reserved" is what the pod asks for (what a node has to set aside); "most" is
    if it used its full limits all the time. Per-token cost assumes it is busy; an idle model still costs its reservation."""
    r = rates()
    res_hour = milli_cpu(cpu) / 1000 * r["cpu_hour"] + mebibytes(memory) / 1024 * r["gb_hour"]
    max_hour = milli_cpu(cpu_limit) / 1000 * r["cpu_hour"] + mebibytes(memory_limit) / 1024 * r["gb_hour"]
    disk_day = disk_gi * r["disk_gb_month"] / 30
    tps = tokens_per_second(size_gb)
    return {"hour": res_hour, "day": res_hour * 24 + disk_day, "month": res_hour * 24 * 30 + disk_gi * r["disk_gb_month"],
            "day_max": max_hour * 24 + disk_day, "disk_month": disk_gi * r["disk_gb_month"],
            "tokens_per_s": round(tps, 1), "words_per_s": round(tps * 0.75, 1),
            "per_million_tokens": res_hour / (tps * 3600) * 1e6, "currency": r["currency"], "rates": r}


_hf_cache: dict[str, tuple[float, dict[str, float] | None]] = {}
QUANT_RE = re.compile(r"[-.]((?:IQ|Q)\d\w*|F16|BF16|F32)\.gguf$", re.IGNORECASE)


def hf_quants(repo: str, *, ttl: float = 600) -> dict[str, float] | None:
    """Quantizations a Hugging Face GGUF repo offers, as {"Q4_K_M": size in GB}; None when Hugging Face can't be reached.
    Split files (…-00001-of-00003) and multimodal projectors are left out."""
    hit = _hf_cache.get(repo)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    out: dict[str, float] | None = None
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true",
                                     headers={"User-Agent": "dapp-models/0.1"})
        with urllib.request.urlopen(req, timeout=6) as r:
            files = json.load(r).get("siblings", [])
        out = {}
        for f in files:
            name = f.get("rfilename", "")
            m = QUANT_RE.search(name)
            if m and "mmproj" not in name.lower() and not re.search(r"-\d{5}-of-\d{5}", name):
                out[m.group(1).upper()] = round((f.get("size") or 0) / 2**30, 2)
    except Exception:
        out = None
    _hf_cache[repo] = (time.monotonic() + ttl, out)
    return out


def catalog_entry(model_id: str) -> CatalogModel | None:
    return next((m for m in CATALOG if m.id == model_id), None)


def defaults(size_gb: float | None, *, kv_gb: float = 0.4) -> dict[str, Any]:
    """CPU, memory, disk and context for a GGUF file of this size: weights + the KV cache for the context + headroom.
    Used as it is for a custom model (size and architecture unknown); a catalog model goes through defaults_for."""
    size = size_gb or 1.0
    cpu_limit = 2 if size < 0.8 else 4
    mem_limit = math.ceil((size * 1.1 + kv_gb + 0.7) * 2) / 2
    return {"cpu": "500m" if size < 0.8 else "1", "cpu_limit": str(cpu_limit),
            "memory": f"{max(1.0, mem_limit - 0.5):g}Gi", "memory_limit": f"{mem_limit:g}Gi",
            "disk_gi": max(2, math.ceil(size * 1.5 + 1)), "ctx": 4096, "parallel": 1, "threads": cpu_limit}


def default_ctx(m: CatalogModel) -> int:
    """The longest window that keeps the KV cache within budget, never more than the model was trained for."""
    ctx = min(m.native_ctx, CTX_CAP)
    while ctx > 2048 and m.kv_bytes_per_token * ctx / 2**30 > KV_BUDGET_GB:
        ctx //= 2
    return ctx


def defaults_for(m: CatalogModel) -> dict[str, Any]:
    """Everything the Deploy form starts with for a catalog model: its own context, KV cache, memory and recommended sampling."""
    ctx = default_ctx(m)
    out = defaults(m.size_gb, kv_gb=m.kv_bytes_per_token * ctx / 2**30)
    out["ctx"] = ctx
    for k in ("temperature", "top_k", "top_p", "min_p", "repeat_penalty"):
        if getattr(m, k) is not None:
            out[k] = getattr(m, k)
    return out


def explain_defaults(m: CatalogModel) -> list[str]:
    """Why the form starts with these numbers, in words."""
    d, ctx = defaults_for(m), default_ctx(m)
    kv = m.kv_bytes_per_token * ctx / 2**30
    lines = [f"Memory {d['memory_limit']}: the {m.size_gb} GB file plus about {kv:.1f} GB of KV cache for a {ctx}-token context, plus headroom.",
             f"CPU {d['cpu_limit']} cores and {d['disk_gi']} Gi disk for the file and a spare copy while it downloads."]
    if ctx < m.native_ctx:
        why = "to keep its cache small" if ctx < min(m.native_ctx, CTX_CAP) else "enough for a CPU server"
        lines.append(f"Context {ctx} tokens, {why} (the model supports {m.native_ctx}); raise it in Customize if you need to.")
    else:
        lines.append(f"Context {ctx} tokens, the most this model supports.")
    given = [f"{label} {getattr(m, k)}" for label, k in (("temperature", "temperature"), ("top-k", "top_k"), ("top-p", "top_p"),
                                                          ("min-p", "min_p"), ("repeat penalty", "repeat_penalty"))
             if getattr(m, k) is not None]
    lines.append(f"Sampling from {m.sampling_from}: {', '.join(given)}." if given else
                 "No sampling settings from its makers, so llama.cpp's own defaults apply.")
    return lines


def milli_cpu(value: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)", value.strip())
    if not m:
        raise ValueError(f"cpu {value!r}: use a number of cores (2, 1.5) or millicores (500m)")
    return round(float(m.group(1)) * (1 if m.group(2) else 1000))


def mebibytes(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi)", value.strip())
    if not m:
        raise ValueError(f"memory {value!r}: use Mi or Gi, for example 1536Mi or 2Gi")
    return float(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def app_name(alias: str) -> str:
    return ("model-" + re.sub(r"[^a-z0-9]+", "-", alias.lower()).strip("-"))[:44].rstrip("-")


Switch = Literal["on", "off"]


class ModelDeploy(BaseModel):
    """Everything on the Deploy form, including the Customize menu. All of it is checked here, so the dashboard can show the
    reason. The llama.cpp settings become LLAMA_ARG_* environment variables (DataOS rejects numeric-looking args)."""

    model_config = ConfigDict(extra="forbid")

    # model
    name: str = Field(description="What callers put in `model` (lower-case)")
    model_id: str = Field("", description="A catalog id, or empty for a custom Hugging Face model")
    hf: str = Field("", description="Hugging Face repo[:quant] for a custom model, e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M")
    quant: str = Field("", description="Quantization of the GGUF file, e.g. Q4_K_M (empty: the catalog's, or the repo's default)")
    size_gb_hint: float | None = Field(None, description="File size when known from Hugging Face (for the memory check)")
    # DataOS resources
    compute: str = ""
    cpu: str = "500m"
    cpu_limit: str = "2"
    memory: str = "1Gi"
    memory_limit: str = "1.5Gi"
    disk_gi: int = 2
    # llama.cpp server
    ctx: int = 4096
    parallel: int = 1
    threads: int = 2
    batch: int = 2048
    ubatch: int = 512
    flash_attn: Literal["auto", "on", "off"] = "auto"
    cache_type_k: Literal["f16", "q8_0", "q4_0", "bf16", "f32"] = "f16"
    cache_type_v: Literal["f16", "q8_0", "q4_0", "bf16", "f32"] = "f16"
    max_tokens: int = -1          # most tokens one reply may have when the caller doesn't say (-1: no limit)
    reasoning: Literal["auto", "on", "off"] = "auto"
    jinja: Switch = "on"
    context_shift: Switch = "off"
    cont_batching: Switch = "on"
    timeout_s: int = 3600
    # sampling defaults (used when a caller doesn't set them; empty: llama.cpp's own)
    temperature: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not NAME_RE.match(v) or v.startswith(RESERVED_PREFIXES):
            raise ValueError("name: 2-40 characters of lower-case letters, digits, '.' and '-', not starting with a provider's "
                             f"own prefix ({', '.join(RESERVED_PREFIXES[:6])}…)")
        return v

    def entry(self) -> CatalogModel | None:
        return catalog_entry(self.model_id) if self.model_id else None

    @property
    def repo_name(self) -> str:
        e = self.entry()
        return e.repo if e else self.hf.strip().split(":", 1)[0]

    @property
    def source(self) -> str:
        """The value of llama-server's -hf."""
        e = self.entry()
        quant = self.quant.strip() or (e.quant if e else (self.hf.strip().split(":", 1)[1] if ":" in self.hf else ""))
        return f"{self.repo_name}:{quant}" if quant else self.repo_name

    @property
    def size_gb(self) -> float | None:
        if self.size_gb_hint:
            return self.size_gb_hint
        e = self.entry()
        return e.size_gb if e and (not self.quant.strip() or self.quant.strip().upper() == e.quant.upper()) else None

    def check(self) -> ModelDeploy:
        """Raise ValueError with a sentence a person can act on."""
        if self.model_id and not self.entry():
            raise ValueError(f"unknown model {self.model_id!r}")
        if not self.model_id and not HF_RE.match(self.hf.strip()):
            raise ValueError("custom model: a Hugging Face repo with GGUF files, e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M")
        if self.quant.strip() and not re.fullmatch(r"(?i)(?:IQ|Q)\d\w*|F16|BF16|F32", self.quant.strip()):
            raise ValueError("quantization: a GGUF quantization such as Q4_K_M, Q5_K_M, Q8_0 or F16")
        cpu, cpu_limit = milli_cpu(self.cpu), milli_cpu(self.cpu_limit)
        mem, mem_limit = mebibytes(self.memory), mebibytes(self.memory_limit)
        if not 100 <= cpu <= cpu_limit <= 8000:
            raise ValueError("cpu: the request must be at least 100m and not above the limit, and the limit at most 8 cores")
        if not 256 <= mem <= mem_limit <= 16384:
            raise ValueError("memory: the request must be at least 256Mi and not above the limit, and the limit at most 16Gi")
        if self.size_gb and mem_limit < self.size_gb * 1024 * 1.15:
            need = math.ceil(self.size_gb * 1.15 * 2) / 2
            raise ValueError(f"memory limit {self.memory_limit} is too small to load this model ({self.size_gb} GB file): "
                             f"use at least {need:g}Gi")
        if not 1 <= self.disk_gi <= 50 or (self.size_gb and self.disk_gi < self.size_gb * 1.2 + 0.5):
            raise ValueError("disk: 1 to 50 Gi, and room for the model file (its size plus a little)")
        if not 512 <= self.ctx <= 131072 or not 1 <= self.parallel <= 8 or not 1 <= self.threads <= 16:
            raise ValueError("context 512-131072 tokens, parallel requests 1-8, threads 1-16")
        if self.ctx * self.parallel > 262144:
            raise ValueError("context × parallel requests is too large for a CPU server (at most 262144 tokens in all)")
        if not 32 <= self.ubatch <= self.batch <= 8192 or not -1 <= self.max_tokens <= 131072 or self.max_tokens == 0:
            raise ValueError("batch 32-8192 (micro-batch not above it), max tokens per reply -1 or 1-131072")
        if not 30 <= self.timeout_s <= 86400:
            raise ValueError("timeout: 30 to 86400 seconds")
        for label, v, lo, hi in (("temperature", self.temperature, 0, 5), ("top_p", self.top_p, 0, 1), ("min_p", self.min_p, 0, 1),
                                 ("repeat penalty", self.repeat_penalty, 0.5, 2), ("top_k", self.top_k, 0, 1000)):
            if v is not None and not lo <= v <= hi:
                raise ValueError(f"{label}: between {lo} and {hi}")
        if self.compute and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.compute):
            raise ValueError("compute: a compute resource name such as engineering-compute")
        return self

    def server_env(self) -> dict[str, str]:
        """The llama-server settings as LLAMA_ARG_* variables (all strings; DataOS quotes them)."""
        onoff = {"on": "1", "off": "0"}
        env = {"LLAMA_ARG_PORT": "8080", "LLAMA_ARG_CTX_SIZE": str(self.ctx), "LLAMA_ARG_N_PARALLEL": str(self.parallel),
               "LLAMA_ARG_THREADS": str(self.threads), "LLAMA_ARG_BATCH": str(self.batch), "LLAMA_ARG_UBATCH": str(self.ubatch),
               "LLAMA_ARG_FLASH_ATTN": self.flash_attn, "LLAMA_ARG_CACHE_TYPE_K": self.cache_type_k,
               "LLAMA_ARG_CACHE_TYPE_V": self.cache_type_v, "LLAMA_ARG_N_PREDICT": str(self.max_tokens),
               "LLAMA_ARG_REASONING": self.reasoning, "LLAMA_ARG_JINJA": onoff[self.jinja],
               "LLAMA_ARG_CONTEXT_SHIFT": onoff[self.context_shift], "LLAMA_ARG_CONT_BATCHING": onoff[self.cont_batching],
               "LLAMA_ARG_TIMEOUT": str(self.timeout_s)}
        for var, v in (("TEMPERATURE", self.temperature), ("TOP_K", self.top_k), ("TOP_P", self.top_p), ("MIN_P", self.min_p),
                       ("REPEAT_PENALTY", self.repeat_penalty)):
            if v is not None:
                env[f"LLAMA_ARG_{var}"] = f"{v:g}" if isinstance(v, float) else str(v)
        return env

    @property
    def app(self) -> str:
        return app_name(self.name)

    @property
    def volume(self) -> str:
        return f"{self.app}-data"[:60].rstrip("-")

    @property
    def secret(self) -> str:
        return f"dapp-{self.app}-key"


def with_hf_facts(d: ModelDeploy) -> ModelDeploy:
    """Check the quantization against Hugging Face and learn the real file size for the memory check (skipped when Hugging Face
    can't be reached, so a deploy is never blocked by it)."""
    quants = hf_quants(d.repo_name)
    if quants is None:
        return d
    if not quants:
        raise ValueError(f"{d.repo_name} has no GGUF files llama.cpp can load")
    quant = d.source.split(":", 1)[1].upper() if ":" in d.source else ""
    if quant and quant not in quants:
        raise ValueError(f"{d.repo_name} has no {quant} file; it offers: {', '.join(sorted(quants))}")
    if quants.get(quant):
        d = d.model_copy(update={"size_gb_hint": quants[quant]})
    return d


def build_deploy(*, model_id: str = "", hf: str = "", name: str = "", overrides: dict[str, Any] | None = None) -> ModelDeploy:
    """A checked ModelDeploy: the model's own defaults, then the settings you gave on top (None means "keep the default")."""
    entry = catalog_entry(model_id) if model_id else None
    if model_id and not entry:
        near = [m.id for m in CATALOG if model_id.lower() in m.id or m.id in model_id.lower()] or [m.id for m in CATALOG][:5]
        raise ValueError(f"unknown model {model_id!r}; try one of: {', '.join(near)} (see `dapp model catalog`)")
    if not entry and not hf:
        raise ValueError("choose a model: a catalog id (`dapp model catalog`) or --hf owner/repo[:Quant]")
    if not entry and not name:
        raise ValueError("a custom model needs a name: --name something (what callers put in `model`)")
    fields: dict[str, Any] = {**(defaults_for(entry) if entry else defaults(1.0)), "model_id": model_id, "hf": hf,
                              "name": name or (entry.id if entry else "")}
    given = {k: v for k, v in (overrides or {}).items() if v is not None}
    if "cpu_limit" in given and "threads" not in given:           # more cores asked for: generate with them
        try:
            given["threads"] = max(1, min(16, milli_cpu(str(given["cpu_limit"])) // 1000))
        except ValueError:
            pass                                                   # check() will say what is wrong with the cpu value
    fields.update(given)
    try:
        d = ModelDeploy(**fields)
    except ValidationError as e:
        raise ValueError("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from e
    return with_hf_facts(d).check()


def render_volume(d: ModelDeploy, *, tenant: str, dataplane: str) -> dict[str, Any]:
    return {"name": d.volume, "version": "v2alpha", "type": "volume", "tenant": tenant,
            "tags": ["dapp", MODEL_TAG], "description": f"Model files of {d.name} (downloaded on first start)",
            "volume": {"size": f"{d.disk_gi}Gi", "type": "disk", "dataplane": dataplane}}


def model_app_spec(d: ModelDeploy, *, compute: str) -> AppSpec:
    """The llama.cpp server as a private DataOS app that mounts the model's own Volume."""
    return AppSpec.model_validate({
        "name": d.app, "title": f"Model {d.name}", "description": f"{d.source} on CPU via llama.cpp, as model \"{d.name}\"",
        "image": LLAMA_IMAGE, "command": ["/opt/dapp/start.sh"],
        "args": ["-hf", d.source, "--alias", d.name, "--host", "0.0.0.0", "--metrics"],
        "port": 8080, "compute": compute, "path": f"/{d.app}", "hostname": d.app,
        "public": False, "login": False,           # DataOS' ingress refuses outside callers; the gateway holds the key
        "metrics": True, "secrets": [d.secret], "tags": [MODEL_TAG],
        "resources": {"cpu": d.cpu, "memory": d.memory, "cpuLimit": d.cpu_limit, "memoryLimit": d.memory_limit},
        "env": d.server_env(),   # numbers go in env: DataOS rejects numeric-looking args
        "volumes": [{"id": d.volume, "directory": "/models"}],
    })
