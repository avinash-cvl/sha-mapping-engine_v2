"""Stage 8 -- relationship discovery (Engine 2 / Part 2 trigger).

STATUS: no precedent anywhere in the Himalaya-SKU-Mapping prior art -- that
codebase only ever built Engine 1 (identity resolution). Founding doc
section B.8 has the full design: hierarchy-first, SKU-matching last,
competitors get synthetic IDs and are NEVER written into the MDM -- only
linked via judgment relationships (DIRECT / INDIRECT / SUBSTITUTE).

Not called from flow.py yet -- this is a separate concern from Engine 1
matching and should get its own flow once scoped.
"""
from __future__ import annotations

from common.models import MatchResult


def discover_relationships(resolved: list[MatchResult]) -> list[dict[str, object]]:
    """For each settled Himalaya SKU, rank a competitive landscape within
    its classification cell. dbo.relationships (this stage's original
    target table) was dropped along with the rest of the legacy dbo schema
    (see 008_drop_legacy_dbo_pipeline_tables.sql) -- the raw/staging/app
    schema has no relationship-tracking table yet; add one under a new
    schema (e.g. relate.*) when this stage is actually built.

    TODO: implement founding_doc.md section B.8's H1-H6 flow. Chained
    comparability (A~B, B~C => A~C) is explicitly prohibited there.
    """
    raise NotImplementedError("Relationship discovery not built yet -- see founding_doc.md section B.8")
