"""Seeds portals and PROPOSED mapping profiles from config/profiles/*.yaml. Idempotent; never activates anything."""
from __future__ import annotations

from pathlib import Path

from .schema_registry import (ensure_portal, insert_spec, load_portals, load_seed_specs, validate_spec)


def canonical_field_names(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT field_name FROM mis.canonical_fields").fetchall()}


def status_codes(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT status_code FROM mis.lead_statuses").fetchall()}


def seed(conn, seed_dir: Path) -> dict:
    out = {"portals": 0, "profiles_added": [], "profiles_existing": [], "invalid": {}}
    for p in load_portals(seed_dir):
        ensure_portal(conn, p["code"], p.get("display_name"))
        out["portals"] += 1
    fields, statuses = canonical_field_names(conn), status_codes(conn)
    for spec in load_seed_specs(seed_dir):
        errs = validate_spec(spec, canonical_fields=fields, status_codes=statuses)
        if errs:
            out["invalid"][spec.profile_key] = errs
            continue
        exists = conn.execute("""SELECT 1 FROM mis.mapping_profiles p JOIN mis.reporting_portals rp USING (portal_id)
                                 WHERE rp.portal_code=%s AND p.profile_key=%s""",
                              (spec.portal_code, spec.profile_key)).fetchone()
        if exists:
            out["profiles_existing"].append(spec.profile_key)
            continue
        insert_spec(conn, spec, status="proposed", proposed_by="human")
        out["profiles_added"].append(spec.profile_key)
    return out
