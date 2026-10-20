"""Forward-only SQL migration runner.

* Files db/migrations/NNN_name.sql, applied in order inside the files' own BEGIN/COMMIT.
* Applied versions are recorded in public.mis_schema_migrations with a checksum; an edited, already-applied file
  is refused (fix forward with a new migration).
* A migration whose header says `REQUIRES APPROVAL` is applied only when its number is in `approved`
  (env MIS_APPROVED_MIGRATIONS) - a person must opt in to additive changes beyond the approved schema v1.1.
* A session advisory lock serialises concurrent runners (e.g. several containers starting together).
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import psycopg

LOCK_KEY = 726_001
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"


class MigrationError(RuntimeError):
    pass


def discover(directory: Path = DEFAULT_DIR) -> list[tuple[str, str, Path, bool]]:
    out = []
    for p in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        m = re.match(r"(\d{3})_", p.name)
        head = p.read_text(encoding="utf-8")[:600]
        out.append((m.group(1), p.name, p, "REQUIRES APPROVAL" in head))
    return out


def applied_versions(conn) -> dict[str, str]:
    conn.execute("""CREATE TABLE IF NOT EXISTS public.mis_schema_migrations (
                      version text PRIMARY KEY, filename text NOT NULL, checksum text NOT NULL,
                      applied_at timestamptz NOT NULL DEFAULT now())""")
    conn.commit()
    return {v: c for v, c in conn.execute("SELECT version, checksum FROM public.mis_schema_migrations").fetchall()}


def run(database_url: str, *, approved: tuple[str, ...] = (), directory: Path = DEFAULT_DIR) -> dict:
    conn = psycopg.connect(database_url, autocommit=False)
    result = {"applied": [], "skipped_unapproved": [], "already_applied": []}
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        conn.commit()
        done = applied_versions(conn)
        for version, name, path, needs_approval in discover(directory):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if version in done:
                if done[version] != checksum:
                    raise MigrationError(f"{name} was edited after it was applied; add a new migration instead")
                result["already_applied"].append(name)
                continue
            if needs_approval and version not in approved:
                result["skipped_unapproved"].append(name)
                continue
            conn.commit()
            conn.autocommit = True                      # the file carries its own BEGIN/COMMIT
            try:
                conn.execute(sql)
            except Exception as e:
                try:
                    conn.execute("ROLLBACK")            # file failed inside its own BEGIN
                except Exception:
                    pass
                raise MigrationError(f"{name} failed: {type(e).__name__}: {str(e).splitlines()[0]}") from None
            finally:
                if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
                    conn.autocommit = False
            conn.execute("INSERT INTO public.mis_schema_migrations (version, filename, checksum) VALUES (%s,%s,%s)",
                         (version, name, checksum))
            conn.commit()
            result["applied"].append(name)
        return result
    finally:
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            conn.commit()
        finally:
            conn.close()


def schema_status(conn) -> dict:
    """Used by /health: which migrations are applied, and is the ingestion ledger (002) available?"""
    try:
        rows = conn.execute("SELECT version FROM public.mis_schema_migrations").fetchall()
        versions = {r[0] for r in rows}
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        versions = set()
    return {"applied": sorted(versions), "core_schema": "001" in versions, "ingestion_ledger": "002" in versions}


if __name__ == "__main__":                              # python -m processor.migrate
    import os
    url = os.environ["DATABASE_URL"]
    approved = tuple(x.strip() for x in os.environ.get("MIS_APPROVED_MIGRATIONS", "").split(",") if x.strip())
    print(run(url, approved=approved))
    sys.exit(0)
