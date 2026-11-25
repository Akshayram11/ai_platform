# Data Chat (`examples/data-chat`)

Ask questions about DataOS data products in plain words. The small model that runs inside DataOS (`small`, see
[llm-gateway.md](llm-gateway.md)) explains the answer. Deployed from source ([run-from-source.md](run-from-source.md)):
`dapp deploy -f examples/data-chat/dapp.yaml`, URL `https://poliks.instance.dataos.cloud/data-chat/` (private; owner only until
`dapp access allow data-chat USER`).

`dapp.yaml` needs `llm: true`, `objects: true` (history) and `tools: [dataproduct-mcp]`; the gateway tool must be registered
(`dapp tool ls`), and the app is allowed on it at deploy.

## Why the app does the tool work
`small` is a 0.5B model: no function calling, 4096-token context. So the app calls the Data Product MCP itself (through the gateway,
with the person's id in `x-dapp-user`) and only asks the model to put the numbers into words.

## What happens to a question
1. `search(index=data_product, tenant=…)` finds up to 5 data products (`dp:<name>` in the question skips this and picks one).
2. For up to three of them, in order: `vulcan_semantic_schema` → parse the measures and dimensions → match the question by words:
   what comes before "by" / "per" picks a **measure**, what follows picks a **dimension**. A match must account for at least 60 % of the
   question's content words, otherwise the next data product is tried (a weak match would answer another question).
3. `vulcan_query_submit` (limit 20 rows), then `vulcan_query_result` every 3 s (up to ~75 s).
4. The first successful result (trimmed to ~5000 characters, about 1700 tokens) goes to `small` with an instruction to use only those facts.
5. **No number, no model.** If nothing matched, or every query failed, the app says so itself and lists the measures and dimensions the
   first data product has. A 0.5B model asked to answer without facts invents numbers (we saw it), so it is not asked.

Every step is shown in the chat as a muted `›` line (which MCP function ran). If the MCP cannot be reached the chat says so.
Chat history is kept per signed-in person in immortal-obj and shared back to them (see [immortal-obj.md](immortal-obj.md)).

## Limits
- Word matching, not understanding: use the data product's own words ("customer", "revenue", "region"). Questions that need filters,
  dates or several measures are not planned; ask for the measure and one breakdown.
- The query runs as the app's gateway identity plus the person's `x-dapp-user`; what the data product returns is governed by the MCP.
- Some data products cannot answer a pair (for example "Can't find join path"): the chat shows the MCP's error text.
- The model can still phrase a real number badly; the number itself comes from the result shown in the step.
- `app/mcp_tools.py` is a copy of `examples/chatbot/mcp_tools.py` (the runtime packs only `code:`); keep the two in step.

## Try
"how many customers are there?" → picks the best fitting product, runs `customer_count`, answers with the number.
