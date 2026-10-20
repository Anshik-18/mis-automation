"""Attribution partners: register a 3-character code and resolve leads that were waiting for it."""
from __future__ import annotations

import re

CODE_RE = re.compile(r"^[0-9A-Z]{3}$")


def register_partner(conn, code: str, name: str, notes: str | None = None) -> dict:
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        raise ValueError("attribution code must be exactly 3 characters, digits or A-Z")
    if not name or not name.strip():
        raise ValueError("partner name is required")
    row = conn.execute("""INSERT INTO mis.attribution_partners (attribution_code, partner_name, notes)
                          VALUES (%s,%s,%s)
                          ON CONFLICT (attribution_code) DO NOTHING RETURNING partner_id""",
                       (code, name.strip(), notes)).fetchone()
    created = row is not None
    pid = row[0] if row else conn.execute("SELECT partner_id FROM mis.attribution_partners WHERE attribution_code=%s", (code,)).fetchone()[0]
    n = conn.execute("""UPDATE mis.leads SET attribution_status='resolved', attribution_partner_id=%s
                        WHERE attribution_code=%s AND attribution_status='unregistered_code'""", (pid, code)).rowcount
    return {"partner_id": pid, "code": code, "created": created, "leads_resolved": n}


def unregistered_codes(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute("""SELECT attribution_code, count(*) FROM mis.leads WHERE attribution_status='unregistered_code'
                           GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s""", (limit,)).fetchall()
    return [{"code": c, "leads": n} for c, n in rows]
