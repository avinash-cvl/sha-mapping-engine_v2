"""Re-embed staging.himalaya_products using identity-only text.

Why this exists
---------------
The master embeddings were generated from `search_text`, a de-duplicated bag
of words spanning eleven columns. Two defects, both measured on the live
master (3662 rows):

  * Word order is destroyed by the de-dup, so the embedded text is not the
    natural phrase an embedding model expects. AMLA DRINK LEMON FLAVOURED
    embeds as "... amla drink lemon flvred 125ml health 125 ml flavoured
    (india) amalaki ...".
  * Rows average 20.4 tokens and TWELVE tokens appear in 30-100% of all rows
    ('sales' 100%, 'himalaya' 100%, 'india' 75%, 'pack' 68%, 'regular' 60%,
    'offer' 38%, 'wellness' 36%, '(himalaya company)' 35%, 'care' 33%).
    Roughly half of every vector is catalogue boilerplate, which pulls all
    master vectors toward a common centroid and COMPRESSES the distance
    between the products the engine most needs to tell apart.

The source side embeds clean_title (+/- ingredient/benefit), so this puts
both sides of the cosine comparison into the same shape: brand + natural
title + product group, nothing else.

This mirrors build_embedding_text() in sha-pipelines'
himalaya_product_processor.py -- that is where the fix belongs permanently;
this script applies it to rows that are already loaded, without re-running
the whole ingestion pipeline.

search_text is NOT modified: it is the right shape for BM25 and is still
used for lexical matching.

Safety: staging.himalaya_products_embed_backup holds the previous vectors.
Restore with --restore.
"""
from __future__ import annotations

import argparse

from common import db, embedder

BATCH = 256


def build_embedding_text(title: str | None, group: str | None) -> str:
    """Brand + natural title + product group, order preserved.

    Whole repeated words are dropped (the group usually repeats the title's
    words) but ORDER is kept -- the goal is a readable phrase, not a bag.
    """
    seen: set[str] = set()
    words: list[str] = []
    for part in ("Himalaya", title or "", group or ""):
        for word in str(part).strip().lower().split():
            if word and word not in seen:
                seen.add(word)
                words.append(word)
    return " ".join(words)


def restore(conn) -> None:
    updated = conn.exec_driver_sql(
        f"""
        UPDATE t
        SET embedding = CAST(b.embedding_text AS VECTOR({db.C.EMBED_DIM}, FLOAT32))
        FROM staging.himalaya_products t
        JOIN staging.himalaya_products_embed_backup b
          ON b.product_code = t.product_code
        """
    ).rowcount
    conn.commit()
    print(f"restored {updated} rows from staging.himalaya_products_embed_backup")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true",
                        help="Put the backed-up embeddings back and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the new text for a few rows, embed nothing")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        if args.restore:
            restore(conn)
            return

        rows = conn.exec_driver_sql(
            """
            SELECT product_code, normalized_title, normalized_product_group
            FROM staging.himalaya_products
            WHERE product_code IS NOT NULL
            ORDER BY product_code
            """
        ).fetchall()

        pairs = [
            (str(code), build_embedding_text(title, group))
            for code, title, group in rows
        ]

        if args.dry_run:
            for code, text in pairs[:15]:
                print(f"{code}  {text}")
            print(f"\n{len(pairs)} rows would be re-embedded")
            return

        print(f"re-embedding {len(pairs)} master rows...")

        done = 0
        for start in range(0, len(pairs), BATCH):
            chunk = pairs[start:start + BATCH]
            # tag differs from the old "master" tag so this never reads a
            # cached vector built from the old search_text.
            vectors = embedder.embed_many(
                [text for _, text in chunk], tag="master_identity_v2"
            )
            for (code, _), vector in zip(chunk, vectors):
                conn.exec_driver_sql(
                    f"""
                    UPDATE staging.himalaya_products
                    SET embedding = CAST(CAST(? AS NVARCHAR(MAX))
                                    AS VECTOR({db.C.EMBED_DIM}, FLOAT32))
                    WHERE product_code = ?
                    """,
                    (db.vector_literal(vector), code),
                )
            conn.commit()
            done += len(chunk)
            print(f"  {done}/{len(pairs)}")

        print("done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
