# Models tab: deploy a small language model in a few clicks

Dashboard → **Models**. Pick a model, optionally change its settings, **Deploy**; a minute later it answers on the gateway as
`model: <name>`. Create a key on the model's page and call it with the OpenAI API (curl, Python, or any app). Prompts stay inside DataOS.

## What a deploy creates
One click makes four things, in this order (a failure removes what was made, so nothing is left behind):
1. a **Volume** `model-<name>-data` that keeps the downloaded model (so restarts don't download again);
2. a **secret** `dapp-model-<name>-key` with the model server's own `LLAMA_API_KEY`;
3. the **app** `model-<name>`: llama.cpp's server (image of `apps/llm-small`) that downloads the GGUF file from Hugging Face on its first start,
   private (DataOS' ingress refuses outside callers; only the gateway has the key), with CPU/memory metrics;
4. the model's **name in the gateway** (`POST /admin/models`): callers put `"model": "<name>"` and the gateway sends the call to the app with its key,
   with the same keys, metering and streaming as any other model (provider `local`).

Takes about 3 s to create and 15 s to answer for a 0.5B model (measured: deploy, first answer, delete). The gateway registers it only if you own the
app, only at `http://<app>:8080`, and never under a provider's own name (`gpt…`, `claude…`, `o1…`, …).

## Choosing the right model
The Models page is built to pick from, not just list: a **"Which model should I pick?"** row with three starting points (simple tasks, everyday chat,
best answers), and the catalog grouped into **Fast and light** (under 1 GB), **Balanced** (1-2 GB) and **Most capable** (2-3 GB). Each card shows:
- **Quality** and **Speed** as five dots. Quality is a rough guide from size and generation, not a benchmark. Speed comes from the file size: CPU
  generation is memory-bound, tokens/s ≈ 14 ÷ file GB (measured: 0.46 GB gave 29.5/s, 2.23 GB gave 6.4/s), shown as about N words per second.
- what it is good at (chips such as *multilingual*, *thinking mode*, *code*, *long context*), what it **needs** (memory, CPU, download size, licence);
- the **estimated cost** of keeping it running: per day and per month.

**Cost estimate.** DataOS doesn't bill these apps here, so the number is a comparison between models, not a bill. It is what the pod *reserves*
(CPU request × price per CPU-hour + memory request × price per GB-hour, plus disk per GB-month), with an "at most" figure if it used its full limits and a
"per 1M tokens when busy" figure. Prices default to cloud list prices ($0.04 per CPU-hour, $0.005 per GB-hour, $0.10 per GB-month of disk); set your own with
`DAPP_COST_CPU_HOUR`, `DAPP_COST_GB_HOUR`, `DAPP_COST_DISK_GB_MONTH`, `DAPP_COST_CURRENCY` on the dashboard. On the Deploy form the cost panel **updates as you
change CPU, memory, disk or quantization**. An idle model costs the same, so delete what you don't use. Running models show their cost per day too.

## The catalog
14 ungated GGUF models, file sizes read from Hugging Face (`src/dapp/models.py`): SmolLM2 360M and 1.7B, Qwen2.5 0.5B / 1.5B / 3B, Qwen3 0.6B / 1.7B / 4B,
Llama 3.2 1B / 3B, Gemma 3 1B / 4B, Gemma 2 2B, Phi-3.5 mini. Each card shows size, quantization and licence (some are not Apache: Llama, Gemma, Qwen 3B).
**Your own GGUF model** takes any `owner/repo[:Quant]` from Hugging Face. The page offers only quantizations the repo really has (looked up live) and refuses one it hasn't.

## Defaults depend on the model
The form starts with numbers that fit the model you picked, and says why under "Defaults for …". Each catalog entry carries facts from the model's own
`config.json` and `generation_config.json` (ungated mirrors for Llama and Gemma):

| Default | How it is chosen | Examples |
|---|---|---|
| Memory | file × 1.1 + the KV cache for the context + 0.7 GB headroom, rounded up to 0.5 Gi. The KV cache per token = 2 × layers × KV heads × head size × 2 bytes | Qwen2.5 0.5B 1.5Gi (12 KB/token); Qwen3 0.6B 2.5Gi (114 KB/token); SmolLM2 1.7B 3.5Gi (196 KB/token); Phi-3.5 mini 5Gi (393 KB/token) |
| Context | the model's trained length, capped at 8192 for a CPU server, and halved until its KV cache is at most 1.5 GB | most 8192; Phi-3.5 mini 4096 (no grouped-query attention) |
| Sampling | what its makers publish: temperature, top-k, top-p, min-p, repeat penalty. Models that publish nothing stay on llama.cpp's defaults | Qwen2.5: 0.7 / 20 / 0.8 / rep 1.1; Qwen3: 0.6 / 20 / 0.95 / min-p 0; Llama 3.2: 0.6 / top-p 0.9; Gemma 3: 1.0 / 64 / 0.95; SmolLM2: 0.2 / 0.9; Phi-3.5, Gemma 2: none |
| CPU, disk | 2 cores under a 0.8 GB file, else 4; disk = file × 1.5 + 1 GB | |
| Custom model | size and architecture are unknown, so general defaults (4096 context, KV cache assumed 0.4 GB) | |

Checked live: Phi-3.5 mini with its defaults (4 CPU, 5Gi, 4096 context) loaded, answered a 1,370-token prompt without running out of memory
(about 42 prompt tokens/s and 6 tokens/s generating on 4 cores), and deleted cleanly. The pod's reported memory sits close to its limit because the
model file is memory-mapped; if a model is killed for memory, raise the limit in Customize.

## Customize: every setting
All of these are in the **Customize** menu (the values above are what it starts with):

| Group | Setting | Becomes |
|---|---|---|
| Model | Quantization | the `:Quant` of `-hf` |
| DataOS resources | Compute, CPU request/limit, memory request/limit, Disk (Gi) | the app's resources, its Volume size |
| Server | Context size, Parallel requests, Threads | `LLAMA_ARG_CTX_SIZE`, `_N_PARALLEL`, `_THREADS` |
| | Batch size, Micro-batch size | `_BATCH`, `_UBATCH` |
| | Flash attention, KV cache type (keys, values) | `_FLASH_ATTN`, `_CACHE_TYPE_K`, `_CACHE_TYPE_V` |
| | Max tokens per reply, Reasoning/thinking | `_N_PREDICT`, `_REASONING` |
| | Jinja chat template, Context shift, Continuous batching | `_JINJA`, `_CONTEXT_SHIFT`, `_CONT_BATCHING` |
| | Request timeout | `_TIMEOUT` |
| Sampling defaults | Temperature, Top-k, Top-p, Min-p, Repeat penalty | `_TEMPERATURE`, `_TOP_K`, `_TOP_P`, `_MIN_P`, `_REPEAT_PENALTY` (only when set) |

The env names were read from the llama-server `--help` of the image we run (build b12679). Numbers go in env because DataOS rejects numeric-looking `args`.
Checks with a sentence you can act on: CPU ≤ 8 cores, memory ≤ 16 Gi and big enough for the file (+15 %), disk ≥ file + 20 %, context × parallel ≤ 262144,
batch ranges, sampling ranges, at most **5 models per person** (`DAPP_MAX_MODELS`).

## Try it in the chatbot
A model shows **starting…** (downloading and loading) and then **ready**: the gateway asks the model server's `/health`, which answers 200 only once the model is loaded,
and the model page refreshes itself until then. Every model page has **Try it in the DataOS chatbot**, which opens the chatbot with that model selected
(`…/chatbot/?model=<name>`; needs access to the chatbot app, and the chatbot must be deployed: `DAPP_CHATBOT_APP` / `DAPP_CHATBOT_PATH` change where the link points).

The chatbot's model drop-down is **live**: it asks the gateway (`GET /local-models`, with the chatbot's own key) every 10 seconds, so a model you deploy appears within
seconds, marked "(in DataOS, starting…)" until it is ready, and disappears when you delete it. Picking a model that is still loading gives a plain message instead of an error.

## Use it
The model page shows the base URL, a key button (a **dapp key**, a personal gateway key shown once; usage counts as `user:<you>`, LLM calls only), and ready examples with `$DAPP_KEY` and `$DAPP_BASE_URL` (the same names `dapp llm-key personal --save` writes; it is the gateway's key, not an OpenAI one):
curl, streaming, the OpenAI Python SDK, and from an app (`llm: true`, `model="<name>"`). Anyone with a gateway key in the tenant can call a model
(like `small`). Small models don't do tool calling. The page also shows usage by app (tokens, requests, errors) and the live deployment (resources, settings).

## Delete
On the model's page, type its name. The gateway stops routing first, then the app, its key secret and gateway keys go (`dapp rm --purge`), and the Volume is
removed once its pod has stopped (done in the background; about 15 s). Only the owner can delete. `small` comes from the gateway's configuration
(`DAPP_LOCAL_MODELS`) and shows as "gateway config": change it where it was deployed.

## From a terminal: `dapp model`
The same thing without the dashboard (for you and for agents such as the Claude Code app):

```bash
dapp model catalog                                       # table: id, size, quality, words/s, memory, CPU, download, $/day  (--json has more)
dapp model deploy qwen2.5-1.5b --name my-qwen --dry-run  # the settings and the estimated cost; deploys nothing
dapp model deploy qwen2.5-1.5b --name my-qwen            # deploys, waits until it answers (--no-wait, --timeout 900)
dapp model deploy --hf Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q8_0 --name mine --cpu-limit 4 --memory-limit 3Gi --set ctx=8192 --set temperature=0.3
dapp model ls                                            # name, ready or starting, weights, app, owner
dapp model rm my-qwen --yes                              # gateway first, then the app and key, then the Volume (--keep-volume)
```
Flags: `--name --quant --compute --cpu --cpu-limit --memory --memory-limit --disk`; every other Customize setting is `--set key=value` (`ctx`, `parallel`, `threads`,
`batch`, `ubatch`, `flash_attn`, `cache_type_k`, `cache_type_v`, `max_tokens`, `reasoning`, `jinja`, `context_shift`, `cont_batching`, `timeout_s`, `temperature`,
`top_k`, `top_p`, `min_p`, `repeat_penalty`). The defaults are the model's own, and `threads` follows `--cpu-limit` unless set. The checks and messages are the dashboard's.
Compute comes from `--compute`, else `DAPP_DEFAULT_COMPUTE`, else `engineering-compute`; the Volume's dataplane from `DAPP_MODELS_DATAPLANE` (default `poldp01`).
A key to call it: `dapp llm-key personal --save ~/.dapp/llm.env`, then `$DAPP_KEY` and `$DAPP_BASE_URL`.

## Operating it
- Dashboard env: `DAPP_MODELS_COMPUTE` (default `engineering-compute`), `DAPP_MODELS_DATAPLANE` (default `poldp01`), `DAPP_MODEL_IMAGE` (the llama.cpp image),
  `DAPP_MAX_MODELS`. The dashboard acts as the signed-in person, so they need the usual create-resource and Can Use permissions.
- Gateway: table `local_models` (the model server's key is encrypted with `DAPP_GATEWAY_SECRET_KEY`), endpoints `GET/POST /admin/models`,
  `DELETE /admin/models/<tenant>/<name>`. A name in `DAPP_LOCAL_MODELS` cannot be registered again.
- CPU only: about 30 tokens/s for the 0.5B on 2 cores; 3-4B models are several times slower, give them 4+ cores.
- Not there yet: per-model allow lists (today any key in the tenant can call a model), GPU.
