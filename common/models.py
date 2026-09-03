"""Typed data shapes shared by every stage. Frozen dataclasses only -- these
hold data, not behavior. No stage mutates one of these in place; a stage
that needs to change a field returns a new instance via
`dataclasses.replace(...)`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceProduct:
    sku: str
    source: str            # "1ds" | "nielsen"
    channel: str | None
    brand: str
    title: str
    clean_title: str
    category: str
    subcategory: str
    pack_value: float | None
    pack_unit: str | None
    benefit: str | None
    ingredient: str | None
    domain: str             # "baby" | "face" | "other"
    match_type: str          # "Catalog Match" | "Competitive Substitute"
    sub_brand: str | None = None
    variant: str | None = None
    # Units in the pack ("Pack of 2"), from the staging pack_no column or
    # parsed out of the title. None means the listing says nothing, which is
    # deliberately distinct from 1 -- see stage_attributes.pack_count_score.
    pack_count: int | None = None
    # channel_key/source_row_id are populated by stage_ingest.py once a row
    # has been written to raw.oneds_products + resolved against
    # config.channels -- "" / 0 (unset) for rows that predate that (e.g.
    # unit tests constructing a SourceProduct directly, or a source with no
    # channel concept like Nielsen).
    channel_key: str = ""
    source_row_id: int = 0


@dataclass(frozen=True)
class MasterProduct:
    product_code: str
    product_name: str
    division: str
    category: str
    subcategory: str
    sap_status: str
    blocked_in_sap: bool
    pack_value: float | None
    pack_unit: str | None
    text: str               # concatenated searchable text (name + long name + sales text)
    source_row_id: int = 0
    # The master's own marketing grouping (raw.himalaya_products.product_group,
    # projected to staging.normalized_product_group) -- e.g. every size and
    # pack of "STRAWBERRY SHINE LIP BALM" shares one product_group. It is the
    # signal that separates flavour/variant lines whose titles differ by a
    # single word. Defaulted so existing construction sites are unaffected.
    product_group: str = ""
    # raw.himalaya_products.pack_type: REGULAR SALES PACK / OFFER SALES
    # PK-MULTI / OFFER SALES PK-SINGL / GIFT SALES PACK(KIT).
    pack_type: str = ""
    # Units in the pack, parsed from the master title -- the master states
    # this nowhere else. "(PACK OF 3)", "24x10g", "6N(5N+FREE 1N)" and
    # "54'S" are all real notations here. None = the title says nothing.
    pack_count: int | None = None  # raw.himalaya_products.id this came from; 0 = unset


@dataclass(frozen=True)
class ScoreBreakdown:
    semantic: float
    lexical: float
    category: float
    type_align: float
    pack: float
    overlap: float
    ensemble: float
    penalty_applied: str | None = None
    clean_title_score: float | None = None
    clean_title_ing_score: float | None = None
    clean_title_benefit_score: float | None = None
    all_score: float | None = None


@dataclass(frozen=True)
class MatchResult:
    source: SourceProduct
    candidate: MasterProduct | None
    rank: int
    scores: ScoreBreakdown | None
    confidence_tier: str
    resolution_method: str
    llm_pick: str | None = None
    llm_confidence: float = float("nan")
    llm_reason: str | None = None
    # The category resolver's decision for this row (oneds_master/category).
    # These are master_category/master_subcategory in the vocabulary of
    # config.oneds_master_category_mapping -- the same values held by
    # staging.himalaya_products.normalized_category/_subcategory.
    #
    # Recorded for traceability only -- these never enter the ensemble
    # arithmetic; they gate which candidates were scored, and close terminal
    # rows. All default to None so every existing construction site (including
    # oneds_competitor, which never sets them) is unaffected.
    master_category: str | None = None
    master_subcategory: str | None = None
    category_confidence: float | None = None
    category_evidence: str | None = None
