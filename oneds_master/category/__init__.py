"""V2 category-resolution layer, vendored into V1.

Decides which master (HGML) category / sub-category a source SKU belongs to,
using ordered per-category rules. Used to narrow the candidate pool before
V1's scoring runs, and to short-circuit rows that are confidently not
mappable at all. It never scores a product match -- product_code still comes
from V1's candidate generation and scoring, unchanged.

See v2_integration_prompt.md for the port's scope and constraints.
"""
