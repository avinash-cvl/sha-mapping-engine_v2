"""DB-first SQLAlchemy Core schema access -- the schema itself lives in
database/schema/006_channel_schema.sql + 007_channel_crosswalk_enhancements.sql;
this module only ever *reads* what the database already has via reflection,
never declares columns in Python. A new column on staging.zepto_products
shows up here automatically on next reflect, no code change.

Core `Table`/`Column` objects only -- no declarative ORM classes, no
`Session`/relationship() machinery. Matches the house style (rule 1: no
behavior classes; these are typed data descriptions, not behavior holders).

Known limitation: SQL Server 2025's VECTOR type isn't recognized by
SQLAlchemy's mssql+pyodbc dialect -- it reflects as VARBINARY. That's fine
for column *presence* (this module doesn't touch embedding values), but any
code that actually reads/writes the embedding column uses raw parameterized
SQL with the CAST(...AS VECTOR(n)) workaround (see db.py), not a Core
insert()/select() through this reflected column.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import sqlalchemy as sa

import common.config as C

_engine: sa.Engine | None = None
_metadata: sa.MetaData | None = None

CHANNEL_SCHEMAS = ("raw", "staging", "app", "config", "audit")


def get_engine() -> sa.Engine:
    global _engine
    if _engine is None:
        odbc_connect = urllib.parse.quote_plus(C.SQL_SERVER_DSN)
        _engine = sa.create_engine(f"mssql+pyodbc:///?odbc_connect={odbc_connect}")
    return _engine


def reflect_metadata(engine: sa.Engine) -> sa.MetaData:
    """Reflects every table in raw/staging/app/config/audit once per process.
    Memoized -- reflection round-trips to the DB for every table's column
    list, not worth repeating per call."""
    global _metadata
    if _metadata is None:
        metadata = sa.MetaData()
        for schema in CHANNEL_SCHEMAS:
            metadata.reflect(bind=engine, schema=schema)
        _metadata = metadata
    return _metadata


def get_table(engine: sa.Engine, schema: str, name: str) -> sa.Table:
    metadata = reflect_metadata(engine)
    #print(metadata.tables)
    return metadata.tables[f"{schema}.{name}"]


@dataclass(frozen=True)
class ChannelTables:
    channel_id: int
    channel_key: str
    products: sa.Table
    mapping: sa.Table
    crosswalk: sa.Table
    crosswalk_competitor: sa.Table

    @property
    def product_id_column(self) -> str:
        """The FK column name every mapping/crosswalk table uses to point
        back at `products` -- e.g. staging.amazon_products -> amazon_product_id.
        Derived from the table name rather than hardcoded per channel, so a
        new channel's tables (following the same naming convention) need no
        code change here."""
        return self.products.name.removesuffix("s") + "_id"


class UnknownChannelError(Exception):
    def __init__(self, channel_key: str) -> None:
        super().__init__(f"no active config.channels row for channel_key={channel_key!r}")


def _table_from_qualified_name(metadata: sa.MetaData, qualified_name: str) -> sa.Table:
    schema, name = qualified_name.split(".", 1)
    return metadata.tables[f"{schema}.{name}"]


def resolve_channel_tables(engine: sa.Engine, channel_key: str) -> ChannelTables:
    """Reads config.channels to find the products/mapping/crosswalk tables
    for one channel -- this is what lets the pipeline stay channel-agnostic
    instead of branching on channel name in code."""
    metadata = reflect_metadata(engine)
    channels = metadata.tables["config.channels"]
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(
                channels.c.channel_id,
                channels.c.staging_table_name,
                channels.c.staging_mapping_table_name,
                channels.c.crosswalk_table_name,
                channels.c.crosswalk_competitor_table_name,
            ).where(channels.c.channel_key == channel_key, channels.c.is_active == True)  # noqa: E712
        ).first()
    if row is None:
        raise UnknownChannelError(channel_key)
    return ChannelTables(
        channel_id=row.channel_id,
        channel_key=channel_key,
        products=_table_from_qualified_name(metadata, row.staging_table_name),
        mapping=_table_from_qualified_name(metadata, row.staging_mapping_table_name),
        crosswalk=_table_from_qualified_name(metadata, row.crosswalk_table_name),
        crosswalk_competitor=_table_from_qualified_name(metadata, row.crosswalk_competitor_table_name),
    )


def resolve_channel_tables_by_id(engine: sa.Engine, channel_id: int) -> ChannelTables:
    """Same as resolve_channel_tables() but keyed by config.channels.channel_id
    -- used to decode the portal's composite mapping ids (see
    sha-portal-api/queries.py) back into a channel."""
    metadata = reflect_metadata(engine)
    channels = metadata.tables["config.channels"]
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(channels.c.channel_key).where(channels.c.channel_id == channel_id)
        ).first()
    if row is None:
        raise UnknownChannelError(f"channel_id={channel_id}")
    return resolve_channel_tables(engine, row.channel_key)


def list_active_channels(engine: sa.Engine) -> list[str]:
    metadata = reflect_metadata(engine)
    channels = metadata.tables["config.channels"]
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(channels.c.channel_key).where(channels.c.is_active == True)  # noqa: E712
        ).all()
    return [row.channel_key for row in rows]


def resolve_channel_key_by_name(engine: sa.Engine, channel_name: str) -> str | None:
    """Case-insensitive match of a raw.oneds_products.channel_name value
    (e.g. 'Amazon', 'amazon.in') against config.channels.channel_name /
    channel_key. Returns None -- not raised -- for an unmapped channel value,
    since this is a per-row lookup during ingest and an unknown channel is
    an expected, handled case (routed to a labelled disposition), not a
    programming error."""
    metadata = reflect_metadata(engine)
    channels = metadata.tables["config.channels"]
    normalized = channel_name.strip().lower()
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(channels.c.channel_key).where(
                sa.or_(
                    sa.func.lower(channels.c.channel_key) == normalized,
                    sa.func.lower(channels.c.channel_name) == normalized,
                )
            )
        ).first()
    return row.channel_key if row is not None else None
