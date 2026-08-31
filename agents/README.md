# sha-pipelines/agents

The scoped LLM touchpoints from founding_doc.md section B.6, plus the
Synonyms and MCDA agents added on top. None of these is an autonomous
matching agent -- each one only ever picks from, adds detail to, or
explains candidates the deterministic stages already produced.

| File | Called from | Status |
|---|---|---|
| `llm_client.py` | all of the below | Working -- `get_chat_model()` reads `LLM_PROVIDER` (`anthropic`/`openai`/`azure`) from config.py, so switching models never touches code. `invoke_and_audit()` is the shared choke point every agent calls instead of hand-rolling `model.invoke()` + timing + audit_entry -- it also traces the call through `observability_sdk` (see docker/docker-compose.yml's `OBSERVABILITY_*` env vars). |
| `attribute_fallback.py` | `flow.py`'s `run_attributes` task | Working -- adapted from Himalaya-SKU-Mapping's `attribute_extractor.py`, rewritten through LangChain. |
| `synonyms.py` | `flow.py`'s `run_candidates` task | Working -- query-expansion agent, one call per unique source title (not per SKU). Generates alternate/synonym terms merged into the BM25 lexical search query, widening recall on the lexical channel only -- semantic search, scoring, and disposition are untouched. |
| `mcda_judge.py` | `flow.py`'s `run_disposition` task | Working -- multi-criteria decision analysis judge for thin-margin rows, replacing `thin_margin_judge.py` at this call site. Shows the LLM each candidate's actual per-criterion `ScoreBreakdown` (semantic/lexical/category/type/pack/overlap), not just candidate names, so its pick is grounded in the same criteria `stage_scoring.py` weighs. |
| `thin_margin_judge.py` | not called from `flow.py` anymore | Kept as reference/fallback -- same prompt contract as `mcda_judge.py`, superseded by it at the `run_disposition` call site. |
| `acronym_agent.py` | `stage_lexicon.py` (mining-time only, never at runtime) | Stub -- no Lexicon mining pipeline exists yet to call it from. |
| `recovery_assist.py` | `stage_recovery.py` | Stub -- no recovery loop exists yet to call it from. |

`sha-portal-api` also has its own on-demand agent, `match_reasoning.py` (not
in this directory -- the two services deploy independently and don't share
code, see `sha-portal-api/db.py`'s docstring). It backs the portal's
"Explain this match" button and runs only when a steward expands a row,
never from the pipeline -- so it costs nothing on a normal run, and works
for `pipeline_high` (fully deterministic) rows the pipeline itself never
called an LLM on. See `sha-portal-api/queries.py`'s `explain_match()`.

## Switching models

```bash
export LLM_PROVIDER=anthropic   # or openai, azure
export LLM_MODEL=claude-sonnet-5   # or gpt-5, etc.
```

That's the entire swap -- no other file changes. For `azure` specifically,
`AZURE_LLM_DEPLOYMENT` must match an exact deployment name provisioned on
the Azure OpenAI resource (currently `gpt-5-nano`, see config.py).

## Audit logging

Every agent function returns an `audit_entry` dict shaped for
`dbo.llm_audit`, built by `llm_client.invoke_and_audit()`. The agent
functions themselves never touch the database -- `flow.py` (or whoever
calls them) is responsible for writing it, which keeps these files easy to
unit-test in isolation, with no DB connection needed.

## Observability

`invoke_and_audit()` traces every call through `observability_sdk`
(vendored into `sha-pipelines/observability_sdk/` and
`sha-portal-api/observability_sdk/` -- copied source, not a path
dependency, since the upstream SDK has no packaging metadata). `flow.py`
and `flow_without_prefect.py` call `observability_sdk.init()` once at
process start using `config.py`'s `OBSERVABILITY_*` vars; `sha-portal-api`
does the same in `main.py`. All calls from this project trace into the
`himalaya-sku` project on the observability backend -- do not reuse that
token for other projects on the same backend.
