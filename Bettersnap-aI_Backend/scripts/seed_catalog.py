"""Seed the catalog_* tables from the generated catalog_data.py (single source of truth,
same data shared/catalog.py + the inference container use). Idempotent: each run REPLACES
the catalog rows (delete-all + insert) inside one transaction, so re-running always converges
the DB to exactly what catalog_data.py holds — no drift, no duplicates.

Prereq: migration 029_catalog_tables.sql must be applied first (run scripts/run_migrations.py).

Run from the backend dir (needs DB access — SQL_* env + the Db-Password Key Vault secret):
    python scripts/gen_catalog.py      # regenerate catalog_data.py from the seed (if edited)
    python scripts/run_migrations.py   # apply pending migrations (027..029)
    python scripts/seed_catalog.py     # populate the catalog_* tables
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared"))  # allow top-level `import catalog_data`

from shared.db import new_connection  # noqa: E402
try:
    from shared.catalog_data import CATEGORY_CATALOG  # noqa: E402
except ImportError:
    from catalog_data import CATEGORY_CATALOG  # noqa: E402

GENDERS = ("male", "female", "other")


def seed(conn):
    cur = conn.cursor()
    # Replace-all in one transaction (children first for FK order).
    cur.execute("DELETE FROM dbo.catalog_attires")
    cur.execute("DELETE FROM dbo.catalog_backgrounds")
    cur.execute("DELETE FROM dbo.catalog_categories")

    n_cat = n_att = n_bg = 0
    for ckey, c in CATEGORY_CATALOG.items():
        import json as _json
        cur.execute(
            "INSERT INTO dbo.catalog_categories "
            "(category_key, label, category_type, lead_phrase, lighting_json, is_custom, sort_order, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            ckey, c["label"], c["type"], c["lead"],
            (_json.dumps(c["lighting"]) if c.get("lighting") else None),
            1 if c.get("custom") else 0, int(c.get("sort_order", 0)),
        )
        n_cat += 1
        for g in GENDERS:
            for a in sorted(c.get("attires", {}).get(g, {}).values(),
                            key=lambda o: o.get("sort_order", 0)):
                akey = a["ref"].split(".", 1)[1]
                cur.execute(
                    "INSERT INTO dbo.catalog_attires "
                    "(category_key, gender, attire_key, ref, label, prompt_phrase, image_key, sort_order, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    ckey, g, akey, a["ref"], a["label"], a["phrase"],
                    a.get("image_key"), int(a.get("sort_order", 0)),
                )
                n_att += 1
        for b in sorted(c.get("backgrounds", {}).values(), key=lambda o: o.get("sort_order", 0)):
            bkey = b["ref"].split(".", 1)[1]
            cur.execute(
                "INSERT INTO dbo.catalog_backgrounds "
                "(category_key, background_key, ref, label, prompt_phrase, image_key, sort_order, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                ckey, bkey, b["ref"], b["label"], b["phrase"],
                b.get("image_key"), int(b.get("sort_order", 0)),
            )
            n_bg += 1
    conn.commit()
    return n_cat, n_att, n_bg


def main():
    conn = new_connection()
    try:
        n_cat, n_att, n_bg = seed(conn)
        print(f"Seeded catalog: categories={n_cat}  attires={n_att}  backgrounds={n_bg}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
