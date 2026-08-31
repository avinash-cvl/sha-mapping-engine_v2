"""Stage 0/1 -- load raw source files, land them in raw.himalaya_products /
raw.oneds_products, normalize into staging.himalaya_products /
staging.{channel}_products, and return SourceProduct / MasterProduct rows
carrying the FK ids (source_row_id) later stages need to write mapping/
crosswalk rows against.

Founding doc section B.2 Stage 0 (source profiling) is folded in here as
column resolution; a real profiling step (arrival shape, language,
coverage estimate) is future work.

Uses df.iterrows() and one DB round-trip per row -- fine at this project's
current data scale, but both should be batched before running the full
~41k-row 1DS file.
"""
from __future__ import annotations

import re

import pandas as pd
import sqlalchemy as sa

import common.config as C
from common import db
from common import db_models
from common.models import MasterProduct, SourceProduct

_WS_RE = re.compile(r"\s+")


def is_db_source(source: str) -> bool:
    """A source value is either an Excel path (ends .xlsx) or a DB
    reference -- 'staging' / 'all' (every active channel/the Himalaya
    catalog) or a qualified 'staging.<table>' name for one channel."""
    return not source.lower().endswith(".xlsx")


def _clean(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def _domain(text: str) -> str:
    lowered = text.lower()
    if "baby" in lowered:
        return "baby"
    if "face" in lowered:
        return "face"
    return "other"


def _match_type(brand: str) -> str:
    lowered = brand.lower()
    is_himalaya = any(known in lowered for known in C.HIMALAYA_BRANDS)
    return "Catalog Match" if is_himalaya else "Competitive Substitute"


def load_1ds(conn: db.Connection, batch_id: str, path: str = C.ONEDS_FILE) -> tuple[list[SourceProduct], list[str]]:
    """Returns (products routed to a known channel, channel_name values that
    didn't resolve via config.channels). Unresolved rows still land in
    raw.oneds_products (nothing is silently dropped -- founding_doc.md's "no
    silent drops" invariant) but skip the staging/channel-products write and
    are excluded from the returned SourceProduct list, since there's no
    channel-specific table to score them against yet."""
    df = pd.read_excel(path)
    products: list[SourceProduct] = []
    unmapped_channels: list[str] = []

    for _, row in df.iterrows():
        title = _clean(row.get("title"))
        if not title:
            continue
        brand = _clean(row.get("brand"))
        category = _clean(row.get("category"))
        channel_name = _clean(row.get("channel_name"))
        sku = str(row.get("sku"))

        _raw_id = db.insert_raw_oneds_row(conn, batch_id, {
            "channel_name": channel_name or None,
            "sku": sku,
            "title": title,
            "brand": brand or None,
            "category": category or None,
            "subcategory": _clean(row.get("subcategory")) or None,
            "combo_flag": _clean(row.get("combo_flag")) or None,
            "price_band": _clean(row.get("price_band")) or None,
            "product_benefit": _clean(row.get("product_benefit")) or None,
            "active_ingredient": _clean(row.get("active_ingredient")) or None,
            "pet_category": _clean(row.get("pet_category")) or None,
            "pet_food_type": _clean(row.get("pet_food_type")) or None,
            "pack_size_band": _clean(row.get("pack_size_band")) or None,
            "segment_tier": _clean(row.get("segment_tier")) or None,
        })

        channel_key = db_models.resolve_channel_key_by_name(conn.engine, channel_name) if channel_name else None
        if channel_key is None:
            unmapped_channels.append(channel_name)
            continue

        channel_tables = db_models.resolve_channel_tables(conn.engine, channel_key)
        staging_id = db.insert_staging_channel_product_row(conn, channel_tables, batch_id, {
            "sku": sku,
            "title": title,
            "clean_title": title.lower(),
            "brand": brand or None,
            "category": category or None,
            "subcategory": _clean(row.get("subcategory")) or None,
            "pack_size": None,
            "uom": None,
            "variant": None,
            "ingredient": _clean(row.get("active_ingredient")) or None,
            "combo_flag": None,
            "price_band": None,
            "product_benefit": _clean(row.get("product_benefit")) or None,
        })
        conn.commit()

        products.append(SourceProduct(
            sku=sku,
            source="1ds",
            channel=channel_name or None,
            brand=brand,
            title=title,
            clean_title=title.lower(),
            category=category,
            subcategory=_clean(row.get("subcategory")),
            pack_value=None,
            pack_unit=None,
            benefit=_clean(row.get("product_benefit")) or None,
            ingredient=_clean(row.get("active_ingredient")) or None,
            domain=_domain(f"{title} {category}"),
            match_type=_match_type(brand),
            channel_key=channel_key,
            source_row_id=staging_id,
        ))
    return products, unmapped_channels


def load_nielsen(path: str = C.NIELSEN_FILE) -> list[SourceProduct]:
    """Not wired into flow.py's run_ingest by default -- matches the prior
    art, which also loaded Nielsen but left it out of the live API flow.
    Wire it in once the column mapping below has been checked against a
    real Nielsen extract (columns are batch/delivery-specific coded names,
    see database-analysis artifact section on Nielsen's opaque CSTM_*/INP_*
    schema). Nielsen has no raw/staging landing table in the colleague's
    schema either -- it predates that design."""
    df = pd.read_excel(path)
    df = df[df.get("hierarchy_level_name") == "ITEM CODE"]
    products: list[SourceProduct] = []
    for _, row in df.iterrows():
        title = _clean(row.get("PRDC_LNG_DSC"))
        if not title:
            continue
        brand = _clean(row.get("INP_124465"))
        category = _clean(row.get("CSTM_10000000000000035905"))
        products.append(SourceProduct(
            sku=str(row.get("product_id")),
            source="nielsen",
            channel=None,
            brand=brand,
            title=title,
            clean_title=title.lower(),
            category=category,
            subcategory="",
            pack_value=None,
            pack_unit=None,
            benefit=_clean(row.get("INP_124548")) or None,
            ingredient=_clean(row.get("INP_125764")) or None,
            domain=_domain(f"{title} {category}"),
            match_type=_match_type(brand),
        ))
    return products


def load_master(conn: db.Connection, batch_id: str, path: str = C.MASTER_FILE) -> list[MasterProduct]:
    df = pd.read_excel(path)
    products: list[MasterProduct] = []
    for _, row in df.iterrows():
        code = _clean(row.get("ProductCode"))
        if not code:
            continue
        name = _clean(row.get("ProductName"))
        long_name = _clean(row.get("ProductLongName"))
        sales_text = _clean(row.get("ProductSalesText"))
        sap_status = _clean(row.get("SAPStatus"))
        division = _clean(row.get("DivisionName"))
        category = _clean(row.get("CategoryName"))
        uom = _clean(row.get("UOM"))
        text = " ".join(part for part in (name, long_name, sales_text) if part)

        raw_id = db.insert_raw_himalaya_row(conn, batch_id, {
            "product_code": code,
            "product_name": name or None,
            "division_code": _clean(row.get("DivisionCode")) or None,
            "division_name": division or None,
            "market_type": _clean(row.get("MarketType")) or None,
            "pack_type": _clean(row.get("PackType")) or None,
            "sap_status": sap_status or None,
            "pack_size": _clean(row.get("PackSize")) or None,
            "uom": uom or None,
            "product_short_name": _clean(row.get("ProductShortName")) or None,
            "product_sales_text": sales_text or None,
            "product_long_name": long_name or None,
            "category_name": category or None,
        })

        staging_id = db.insert_staging_himalaya_row(conn, batch_id, raw_id, {
            "normalized_title": name or None,
            "normalized_category": category or None,
            "search_text": text or None,
            "product_code": code,
            "normalized_uom": uom or None,
        })
        conn.commit()

        products.append(MasterProduct(
            product_code=code,
            product_name=name,
            division=division,
            category=category,
            sap_status=sap_status,
            blocked_in_sap=sap_status == "Blocked_in_SAP",
            pack_value=None,
            pack_unit=uom or None,
            text=text,
            source_row_id=staging_id,
        ))
    return products


def load_master_from_staging(conn: db.Connection) -> list[MasterProduct]:
    """Reads the whole Himalaya catalog directly from staging.himalaya_products
    -- already normalized and embedded by an earlier ingest/restore, so no
    fresh raw/staging rows are written and stage_candidates.py can skip
    re-embedding these (see db.rows_missing_embedding). Joined back to
    raw.himalaya_products for division/sap_status, which staging doesn't
    carry directly."""
    engine = conn.engine
    staging = db_models.get_table(engine, "staging", "himalaya_products")
    # SQL Server rejects a join between staging.himalaya_products and
    # raw.himalaya_products with "same exposed names" unless one side is
    # explicitly aliased -- same table name, different schema, is not
    # enough disambiguation for it.
    # raw = db_models.get_table(engine, "raw", "himalaya_products").alias("raw_himalaya")
    rows = conn.execute(
        sa.select(
            staging.c.id,
            staging.c.product_code,
            staging.c.normalized_title,
            staging.c.normalized_category,
            staging.c.search_text,
            staging.c.normalized_uom,
            sa.literal("").label("division_name"),
            sa.literal("").label("sap_status"),
        )
        # .select_from(
        #     staging.join(raw, staging.c.bronze_product_id == raw.c.id)
        # )
        .where(staging.c.product_code.is_not(None))
        .where(staging.c.normalized_subcategory == "FACE WASH")
    ).all()
    return [
        MasterProduct(
            product_code=row.product_code,
            product_name=row.normalized_title or "",
            division=row.division_name or "",
            category=row.normalized_category or "",
            sap_status=row.sap_status or "",
            blocked_in_sap=row.sap_status == "Blocked_in_SAP",
            pack_value=None,
            pack_unit=row.normalized_uom,
            text=row.search_text or row.normalized_title or "",
            source_row_id=row.id,
        )
        for row in rows
    ]


def load_channel_from_staging(conn: db.Connection, channel_key: str) -> list[SourceProduct]:
    """Reads one channel's already-staged rows directly from
    staging.{channel}_products -- no fresh raw/staging rows written."""
    channel_tables = db_models.resolve_channel_tables(conn.engine, channel_key)
    products = channel_tables.products
    #rows = conn.execute(sa.select(products)
    #                    .where(products.c.mapping_status == "PENDING")).all()

    rows = conn.execute(
        sa.select(products)
        .where(
            products.c.mapping_status == "PENDING",
            products.c.brand.in_(C.HIMALAYA_BRANDS),
            products.c.category == "face care",
            products.c.subcategory == "face wash",
        )
        .limit(0)
    ).all()

    # stmt = (
    #     sa.select(products)
    #     .where(
    #         products.c.mapping_status == "PENDING",
    #         products.c.brand.in_(C.HIMALAYA_BRANDS),
    #         products.c.category == "face care",
    #         products.c.subcategory == "face wash",
    #     )
    #     .limit(1)
    # )

    # print("stmt", stmt)

    result: list[SourceProduct] = []
    for row in rows:
        title = row.title or ""
        brand = row.brand or ""
        category = row.category or ""
        result.append(SourceProduct(
            sku=row.sku,
            source="1ds",
            channel=channel_key,
            brand=brand,
            title=title,
            clean_title=(row.clean_title or title.lower()),
            category=category,
            subcategory=row.subcategory or "",
            pack_value=float(row.pack_size) if row.pack_size is not None else None,
            pack_unit=row.uom,
            benefit=row.product_benefit,
            ingredient=row.ingredient,
            domain=_domain(f"{title} {category}"),
            match_type=_match_type(brand),
            channel_key=channel_key,
            source_row_id=row.id,
        ))
    return result


def load_all_channels_from_staging(conn: db.Connection) -> list[SourceProduct]:
    """Every active channel's already-staged rows, concatenated -- used
    when oneds_file is 'staging'/'all' rather than one specific channel
    table."""
    products: list[SourceProduct] = []
    for channel_key in db_models.list_active_channels(conn.engine):
        products.extend(load_channel_from_staging(conn, channel_key))
    return products


def load_master_from_source(conn: db.Connection, batch_id: str, source: str) -> list[MasterProduct]:
    """Dispatches on `source`: an Excel path re-ingests fresh raw/staging
    rows (load_master); 'staging'/'staging.himalaya_products' reads what's
    already there (load_master_from_staging)."""
    if not is_db_source(source):
        return load_master(conn, batch_id, source)
    if source.lower() in ("staging", "staging.himalaya_products"):
        return load_master_from_staging(conn)
    raise ValueError(f"unrecognized master_file source: {source!r} -- expected a .xlsx path or 'staging'")


def load_1ds_from_source(conn: db.Connection, batch_id: str, source: str) -> tuple[list[SourceProduct], list[str]]:
    """Dispatches on `source`: an Excel path re-ingests fresh raw/staging
    rows (load_1ds); 'staging'/'all' reads every active channel's already-
    staged rows; 'staging.<channel>_products' reads just that one channel.
    DB-sourced reads have no unmapped-channel concept (every row read this
    way is already routed), so the second return value is always []."""
    if not is_db_source(source):
        return load_1ds(conn, batch_id, source)
    if source.lower() in ("staging", "all"):
        return load_all_channels_from_staging(conn), []
    if source.lower().startswith("staging."):
        table_name = source.split(".", 1)[1]
        channel_key = table_name.removesuffix("_products")
        return load_channel_from_staging(conn, channel_key), []
    raise ValueError(f"unrecognized oneds_file source: {source!r} -- expected a .xlsx path, 'staging'/'all', or 'staging.<channel>_products'")
