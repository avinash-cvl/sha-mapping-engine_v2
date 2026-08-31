# Implementation prompt — integrate V2 category rules + prompts into the V1 engine

Copy everything below the line into your coding agent, with both repos available.

---

## Context

You are working in the **V1** SKU-harmonization engine (`sha-pipelines`). It maps a
marketplace SKU to a specific Himalaya master `product_code` via: ingest → deterministic
crosswalk → attributes → candidate generation (BM25 + SQL Server vector search) →
weighted ensemble scoring → confidence-tier disposition → persist.

A separate prototype, **V2** (`SKUIntelligenceMaster/mapping_code`), decides one thing
V1 currently decides crudely: which HGML **category / sub-category** a SKU belongs to.
It does that with ordered per-category rules plus a per-category LLM prompt, built
against the same two inputs V1 already reads — the Himalaya Material Master and the
OneDS sheet.

**Only that layer is being ported.** `product_code` still comes from V1's master tables
through V1's existing candidate generation and scoring, exactly as it does today. You
are adding two things and nothing else:

1. category / sub-category resolution rules, used to narrow which master products a row
   is scored against, and
2. per-category prompt selection, so the LLM judge is given the prompt for that SKU's
   category instead of one generic prompt.

The rest of the flow is unchanged.

**"HGML category / sub-category" is the same thing V1 calls master category /
sub-category.** V2's `HGML_CATEGORY_NAME` / `HGML_SUB_CATEGORY_NAME` are exactly the
`master_category` / `master_subcategory` columns of
`config.oneds_master_category_mapping`, and exactly the `(category, subcategory)` key of
the `master_lookup` dict built in `step_3_build_master_lookup`. The two names differ only
because the codebases were written separately — they index the same nodes in the same
Material Master. Read "HGML" as "master" throughout this document. The one practical
difference is casing: V2 emits the master's own casing (`"FACE WASH"`), V1 lowercases at
every lookup.

Your task is to bring V2's **category-resolution rules** and **per-category prompts**
into V1 as a gating and prompt-selection layer.

## Hard constraint — do not touch scoring

V1's scoring logic is final and must not change. Specifically, do not modify:

- `oneds_master/stages/stage_scoring.py` — `score_candidate()`, `_blend()`,
  `rank_candidates()`, `type_alignment_score()`, `category_score()`, `overlap_score()`
- the `W_*` weights, `DOMAIN_MISMATCH_PENALTY`, `TYPE_HARD_INCOMPAT_PENALTY`,
  `TIER_HIGH`, `TIER_REVIEW`, `TIER_NOEQ` in `common/config.py`
- `oneds_master/batch_flow_steps.py::step_12_score_candidates`

**V2's confidence numbers must never enter the ensemble arithmetic.** They are
hand-assigned routing constants in `Rule(...)` declarations, not calibrated
probabilities. They may only be used to (a) admit or exclude candidates, (b)
short-circuit a row before candidate generation, and (c) record evidence. Every
candidate that survives the gate is scored by V1's existing code, unchanged.

---

## Part 1 — Vendor the V2 rules engine

Copy these two files from V2 into a new package `oneds_master/category/`:

- `mapping_core.py` → `oneds_master/category/core.py`
- `category_rules.py` → `oneds_master/category/rules.py`

Required edits when vendoring:

1. Fix the import in `rules.py`: `from mapping_core import ...` becomes
   `from oneds_master.category.core import ...`
2. Strip the pandas-dependent parts of `core.py` that V1 does not need:
   `run_category()`, `summarise()`, `audit_sample()`, and the `import pandas as pd`.
   Keep `normalise()`, `pack_count()`, `parse_size()`, `SIZE_RE`, `NOT_PRODUCT`,
   `Rule`, `CategoryPack`, `classify()`, `validate()`, and the terminal-state
   constants `NO_EQ`, `UNRES`, `UNCLS`, `TERMINALS`.
3. Do **not** vendor `categories/*.py`. Those scripts do `sys.path.insert(0, "..")`
   and call `load_master()` at module import time, so they are not importable as a
   library. `rules.py::PACKS` is the importable form. Note in a comment that the four
   hand-audited categories (`lip_makeup`, `pet_care`, `oral_healthcare`,
   `vitamins_supplements`) carry logic the declarative model cannot express and are a
   later port, not part of this change.
4. Delete V2's module-level `THRESHOLD = 0.80`. Add a single new constant to
   `common/config.py` instead:

   ```python
   # Minimum V2 category-resolver confidence at which the resolved HGML
   # category/sub-category is trusted enough to gate the candidate pool.
   # Below this, the row falls back to the config.oneds_master_category_mapping
   # table so recall is never reduced by a weak category decision.
   CATEGORY_GATE_MIN_CONF = float(os.environ.get("CATEGORY_GATE_MIN_CONF", "0.80"))
   ```

   Do not reuse `TIER_HIGH`/`TIER_REVIEW` for this — they are product-match
   thresholds and mean something different.

## Part 2 — Adapter from `SourceProduct` to `classify()`

`classify(row, pack)` reads `row.title`, `row.subcategory`, and `row.brand`. V1's
`SourceProduct` (`common/models.py`) already carries all three, so no shim object is
needed — pass the `SourceProduct` directly.

Create `oneds_master/category/resolve.py`:

```python
"""Row-level HGML category resolution, used to gate the candidate pool.

Returns a decision per SourceProduct. This never scores a product match --
it only narrows which master products a row is allowed to be scored against,
and short-circuits rows that are confidently not mappable at all.
"""
from __future__ import annotations

from dataclasses import dataclass

import common.config as C
from oneds_master.category.core import TERMINALS, UNRES, classify
from oneds_master.category.rules import PACKS
from common.models import SourceProduct


@dataclass(frozen=True)
class CategoryDecision:
    resolved: bool          # True -> hgml_category/hgml_subcategory are usable as a gate
    terminal: str | None    # NO HGML EQUIVALENT | UNRESOLVED | UNCLASSIFIED, else None
    hgml_category: str
    hgml_subcategory: str
    confidence: float
    evidence: str


def resolve(source: SourceProduct) -> CategoryDecision:
    pack = PACKS.get((source.category or "").strip().lower())
    if pack is None:
        # No V2 pack for this 1DS category -- fall through to the existing
        # config.oneds_master_category_mapping behaviour untouched.
        return CategoryDecision(False, None, "", "", 0.0, "no category pack")

    d = classify(source, pack)
    cat, sub = d["cat"], d["sub"]
    conf = float(d["sub_conf"])

    if cat in TERMINALS:
        return CategoryDecision(False, cat, "", "", conf, str(d["sub_evidence"]))

    resolved = conf >= C.CATEGORY_GATE_MIN_CONF
    return CategoryDecision(
        resolved=resolved,
        terminal=None if resolved else UNRES,
        hgml_category=cat,
        hgml_subcategory=sub,
        confidence=conf,
        evidence=str(d["sub_evidence"]),
    )
```

Note the case convention: V2 emits HGML names verbatim in the master's casing
(`"FACE WASH"`), while V1's `master_lookup` in `step_3_build_master_lookup` is keyed on
`.strip().lower()`. Normalize with `.strip().lower()` at every lookup site.

## Part 3 — Widen the eligible master pool (step 4/5)

`step_5_build_eligible_master_groups` builds `eligible_master` per
`(oneds_category, oneds_subcategory)` group from the DB table alone. The BM25 index in
`step_7_build_bm25_index` and the vector search in `step_10_vector_search` both operate
over that pool, so a target V2 can resolve to but the DB table does not list would never
be retrievable.

In `step_5_build_eligible_master_groups`, after the existing loop that fills
`eligible_by_code` from `category_mapping`, add the union of every target the V2 pack for
that 1DS category can emit:

```python
pack = PACKS.get(oneds_category)
if pack is not None:
    for cat, sub in pack_targets(pack):          # helper you add to resolve.py
        for master in master_lookup.get(
            (cat.strip().lower(), sub.strip().lower()), []
        ):
            eligible_by_code[master.product_code] = master
```

Add `pack_targets(pack)` to `resolve.py`: iterate `pack.rules` yielding `(r.cat, r.sub)`,
`pack.him_lookup.values()`, `pack.subcat_map.values()` (first two elements), and
`pack.default[:2]` — skipping any pair whose category is in `TERMINALS`.

This only ever *widens* the pool, so it cannot reduce recall. Log the before/after count
per group.

## Part 4 — Terminal-state short-circuit

Rows V2 confidently classifies as terminal should not be embedded, retrieved, scored, or
sent to the LLM judge.

In `batch_flow.py::main()`, immediately after `step_6b_apply_crosswalk_shortcircuit` and
before workers are spawned, add a second short-circuit that partitions the batch on
`resolve()`:

| V2 terminal | V1 `confidence_tier` | V1 `resolution_method` |
|---|---|---|
| `NO HGML EQUIVALENT` | `No Himalaya Equivalent` | `category_no_equivalent` |
| `UNCLASSIFIED` | `No Himalaya Equivalent` | `category_unclassified` |
| `UNRESOLVED` | *(not short-circuited — continues to the normal pipeline)* | — |

Build these as `MatchResult(source=source, candidate=None, rank=1, scores=None,
confidence_tier=..., resolution_method=..., llm_confidence=float("nan"))` and persist
them through the same `step_15_add_mapping_data` path as everything else. Put the
resolver's `evidence` string into the mapping row's reason/notes column so a steward can
see *why* a row was closed without a candidate.

`UNRESOLVED` must keep flowing through the normal pipeline. It means "the rules could not
decide", not "there is nothing here" — dropping it would silently lose recall.

Add the two new `resolution_method` values to `determine_mapping_status` (and
`flow.py::_mapping_status`) so they map to `StewardReview`, not `AutoMatch`. A
category-level finding is not a product-level auto-match.

## Part 5 — Row-level candidate gate

This is where the per-row decision does its real work. `eligible_master` is group-level,
so a face-care row and a men's-face-wash row in the same group currently see the same
candidate pool.

In `process_one_sku` (`batch_flow.py`), between `step_11_merge_candidates` and
`step_12_score_candidates`, filter the merged candidate dict to master products whose
`(category, subcategory)` matches the row's resolved target:

```python
decision = category_decisions.get(source_id)
if decision is not None and decision.resolved:
    target = (decision.hgml_category.strip().lower(),
              decision.hgml_subcategory.strip().lower())
    gated = {
        code: scores
        for code, scores in merged_candidates.items()
        if (
            (master_by_code[code].category or "").strip().lower(),
            (master_by_code[code].subcategory or "").strip().lower(),
        ) == target
    }
    # Never gate a row down to nothing -- a category decision that
    # eliminates every retrieved candidate is more likely to be a bad
    # rule than a genuinely empty target, so fall back to ungated.
    if gated:
        merged_candidates = gated
    else:
        logger.warning(
            "SKU=%r category gate eliminated all %d candidates "
            "(target=%r, evidence=%r) -- falling back to ungated",
            sku, len(merged_candidates), target, decision.evidence,
        )
```

`step_12` then runs on the gated dict with V1's scoring completely unchanged.

Thread `category_decisions` (a `dict[source_id, CategoryDecision]`, computed once per
group in `main()` alongside `step_7b_prepare_source_products`) into `process_one_sku` as
a new keyword argument, following the existing pattern of `source_products` and
`synonym_terms`.

Persist `hgml_category`, `hgml_subcategory`, `confidence`, and `evidence` onto the
mapping row in `step_15_add_mapping_data`. Add the columns if they do not exist. Without
this the gate is invisible to stewards and untraceable when a mapping is disputed.

## Part 6 — Per-category prompts for the judge

Copy V2's `prompts/*.md` (25 files) to `agents/prompts/`.

Each file is a markdown document, not a raw prompt: a `# Prompt — ...` heading and
explanatory prose, then `---`, then the actual system prompt, then a trailing `---` and a
`**Note.**` paragraph. The loader must extract the body **between the first and last
`---` fences**, not the whole file.

In `agents/mcda_judge.py`, replace the single module-level `_SYSTEM_PROMPT` constant with
a loader:

```python
import functools
import pathlib

_PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"
_GENERIC_SYSTEM_PROMPT = """..."""   # the existing prompt, kept verbatim as fallback


@functools.lru_cache(maxsize=None)
def _system_prompt_for(category: str) -> str:
    """Per-category judge prompt, falling back to the generic one.

    Category names are 1DS names ('oral healthcare'); prompt files are
    slugged ('oral_healthcare.md').
    """
    slug = (category or "").strip().lower().replace(" & ", "_and_").replace(" ", "_")
    path = _PROMPT_DIR / f"{slug}.md"
    if not path.exists():
        return _GENERIC_SYSTEM_PROMPT
    parts = path.read_text(encoding="utf-8").split("\n---\n")
    return parts[1].strip() if len(parts) >= 3 else _GENERIC_SYSTEM_PROMPT
```

Then in `judge()`, use `_system_prompt_for(source.category)` in place of
`_SYSTEM_PROMPT`. Everything else in `judge()` stays: the same
`get_chat_model()`/`invoke_and_audit(...)` call, the same `call_type="mcda_judge"`, the
same `(product_code, confidence, reason, audit_entry)` return contract, the same
`needs_judge()` trigger in `stage_disposition.py`.

**One critical adaptation.** The V2 prompts were written for a *classification* task and
specify this output contract:

```
row, evidence, hgml_category, hgml_subcategory, confidence
```

V1's judge is a *candidate-selection* task and parses:

```json
{"pick": int|null, "confidence": float, "reason": string}
```

Do not let the file's output contract reach the model unmodified — it will return the
wrong shape and every judged row will fail parsing and return `NaN`. Strip the
`## Output` section from the loaded text and append V1's existing output contract
paragraph in its place. Keep everything above it: the allowed-target list, the decision
order, the hard rules (ingredient mentions are not product identity; never cross the
audience line; multipacks quote combined weight), and the calibration table. That
material is the whole point of the change.

Verify this with a real call before wiring it into a full run.

Also set `temperature=0` in `agents/llm_client.py::get_chat_model()` for all three
providers. Classification gains nothing from sampling variety, and a non-zero temperature
makes runs irreproducible, which undermines the audit trail in `dbo.llm_audit`.

## Part 7 — Startup validation

At the top of `batch_flow.py::main()`, after `step_3_build_master_lookup` gives you
`master_lookup`, assert that every target every pack can emit exists in the master:

```python
allowed = set(master_lookup)      # already (lower, lower) tuples
missing = {
    (c, s) for pack in PACKS.values() for c, s in pack_targets(pack)
    if (c.strip().lower(), s.strip().lower()) not in allowed
}
if missing:
    logger.error("V2 targets not present in master: %s", sorted(missing))
```

V2's own `run_all.py` does this and it is the check that catches a stale rule after a
master refresh. Log it loudly; make it fatal only behind a `--strict-categories` flag, so
a single stale rule cannot block a production run.

---

## Scope boundaries

Out of scope for this change — do not attempt them here:

- `him_lookup.py` / `him_match.py` (Himalaya own-SKU deterministic matcher). Worth
  porting into `stage_deterministic.py` later, but it is a separate change and needs
  `build_him_lookup.py` to emit `product_code` first.
- V2's `llm_audit.py` and the `llm.*` schema.
- V2's `llm_mapper.py` client — V1 uses LangChain via Azure with observability tracing.
  Port the prompts, not the client.
- The four bespoke `categories/*.py` scripts.

## Verification before this is considered done

1. **Scoring is untouched.** `git diff` shows no change to `stage_scoring.py`, the `W_*`
   weights, or `step_12_score_candidates`.
2. **Regression on a known-good group.** Run one category end to end with the gate
   disabled (`CATEGORY_GATE_MIN_CONF=1.1`) and again enabled. Rank-1 `product_code` must
   be unchanged for every row where the gate did not fire. Any difference on a
   non-gated row is a bug in the wiring, not an improvement.
3. **Gate effect is measured, not assumed.** For the gated rows report: count gated,
   mean candidate-pool size before vs after, count of rank-1 changes, and count of
   ungated fallbacks (the `logger.warning` path). A high fallback rate means the pool
   widening in Part 3 is incomplete.
4. **Terminal short-circuit.** Confirm `UNCLASSIFIED` rows (N95 masks, saplings,
   flossers, water irrigators) close with no candidates and never reach step 8.
5. **Prompt loader.** Assert all 25 files parse, that the body starts with `You are a
   product classifier`, that the V2 output contract is gone and V1's is present, and
   that an unknown category returns the generic fallback.
6. **Judge still parses.** Run one thin-margin group with the per-category prompt and
   confirm `llm_confidence` is not `NaN` across the batch.

## Known caveats to carry forward

- Only 4 of V2's 25 categories are hand-audited (lip makeup, pet care, oral healthcare,
  vitamins & supplements). The other 21 cover ~156k of 187k rows and are **unverified**.
  The rules will run; do not quote accuracy for them until each is sampled at 100 rows.
- Several V2 categories currently auto-commit 100% of rows. Across all four audited
  pilots that pattern always meant confidence was optimistic, never that the mapping was
  flawless.
- V2's thresholds are inconsistent across its own files (0.80 in `mapping_core.py`, 0.86
  in `config.py`). This integration deliberately introduces one new V1 constant,
  `CATEGORY_GATE_MIN_CONF`, rather than importing either.
