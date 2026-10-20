"""Human review of a mapping proposal: a plain-language change summary, a digest that binds an approval to exactly
what was reviewed, and signed links for the email path.

Contains column NAMES, counts and ids only - never row values.
  * digest   : sha256 over the proposal's content. An approval that quotes a digest succeeds only if the proposal is
               still exactly what the reviewer saw (a re-proposal or edit changes the digest -> 409).
  * token    : HMAC-signed, expiring, bound to (profile_id, digest). It lets a person open the confirmation page from
               an email without the API key. Opening it changes nothing; approving needs a button press (POST), so
               link scanners and chat bots that merely fetch the URL cannot approve anything.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import time

from .guards import blockers, required_acceptances, semantic_guards
from .schema_registry import ProfileSpec, load_spec_from_db, reject_available

# fields whose meaning changes numbers or identity on the dashboard
CRITICAL_FIELDS = {"external_lead_id", "lead_status_raw", "lead_entry_date", "disbursed_date", "requested_loan_amount",
                   "approved_amount", "disbursed_amount", "payout_amount"}


def _col(c) -> dict:
    return {"header": c.header, "role": c.role, "field": c.canonical_field, "extra_key": c.extra_key,
            "transform": c.transform, "date_format": c.date_format, "only_if_disbursed": c.only_if_disbursed}


def _content(spec: ProfileSpec) -> dict:
    return {"profile_id": spec.profile_id, "portal": spec.portal_code, "key": spec.profile_key,
            "columns": [_col(c) for c in spec.columns], "status_map": dict(sorted(spec.status_map.items())),
            "attribution": [{"f": r.source_field, "p": sorted(r.strip_prefixes), "o": sorted(r.organic_values)}
                            for r in spec.attribution_rules],
            "identity_namespace": spec.identity_namespace, "on_unknown_lead": spec.on_unknown_lead,
            "open_questions": spec.open_questions, "flags": sorted(spec.review_flags)}


def digest_of(spec: ProfileSpec) -> str:
    return hashlib.sha256(json.dumps(_content(spec), sort_keys=True, default=str).encode()).hexdigest()


def _previous(conn, spec: ProfileSpec) -> ProfileSpec | None:
    """The layout this proposal replaces: the active version of the same profile, else the one it was derived from,
    else (a renamed or different-looking file) the portal's active profile that shares the most columns."""
    row = conn.execute("""SELECT profile_id FROM mis.mapping_profiles
                          WHERE portal_id=%s AND profile_key=%s AND status='active' AND profile_id<>%s
                          ORDER BY version DESC LIMIT 1""", (spec.portal_id, spec.profile_key, spec.profile_id)).fetchone()
    if not row:
        row = conn.execute("""SELECT derived_from_profile_id FROM mis.mapping_profiles
                              WHERE profile_id=%s AND derived_from_profile_id IS NOT NULL""", (spec.profile_id,)).fetchone()
    if row:
        return load_spec_from_db(conn, row[0])
    mine = {c.key for c in spec.columns}
    best, best_n = None, -1
    for (pid,) in conn.execute("""SELECT profile_id FROM mis.mapping_profiles
                                  WHERE portal_id=%s AND status='active' AND profile_id<>%s""", (spec.portal_id, spec.profile_id)).fetchall():
        other = load_spec_from_db(conn, pid)
        n = len(mine & {c.key for c in other.columns})
        if n > best_n:
            best, best_n = other, n
    return best


def _target(c: dict) -> str:
    if c["role"] == "canonical":
        return c["field"] or "?"
    if c["role"] == "extra":
        return f"extra:{c['extra_key']}"
    return "excluded"


def build_review(conn, profile_id: int) -> dict:
    spec = load_spec_from_db(conn, profile_id)
    prev = _previous(conn, spec)
    new = {c.key: _col(c) for c in spec.columns}
    old = {c.key: _col(c) for c in prev.columns} if prev else {}
    added = [{"header": new[k]["header"], "maps_to": _target(new[k])} for k in new if k not in old]
    removed = [{"header": old[k]["header"], "was": _target(old[k])} for k in old if k not in new]
    changed = [{"header": new[k]["header"], "was": _target(old[k]), "now": _target(new[k])}
               for k in new if k in old and (_target(new[k]), new[k]["transform"], new[k]["date_format"]) !=
               (_target(old[k]), old[k]["transform"], old[k]["date_format"])]
    # a "rename" is a removed + an added header that carry the same canonical field
    renamed = []
    for r in list(removed):
        for a in list(added):
            if r["was"] == a["maps_to"] and not r["was"].startswith(("extra:", "excluded")):
                renamed.append({"from": r["header"], "to": a["header"], "field": a["maps_to"]})
                removed.remove(r)
                added.remove(a)
                break
    touched = {x["maps_to"] for x in added} | {x["was"] for x in removed} | {x["was"] for x in changed} | \
        {x["now"] for x in changed}
    critical_changed = sorted(t for t in touched if t in CRITICAL_FIELDS)
    smap_new, smap_old = spec.status_map, (prev.status_map if prev else {})
    status_added = sorted(k for k in smap_new if k not in smap_old)
    status_changed = sorted(k for k in smap_new if k in smap_old and smap_new[k] != smap_old[k])
    held = conn.execute("SELECT count(*) FROM mis.drive_file_events WHERE profile_id=%s AND status='needs_mapping'",
                        (profile_id,)).fetchone()[0]
    hard = blockers(spec)
    need = sorted(required_acceptances(spec))
    return {
        "profile_id": profile_id, "portal": spec.portal_code, "profile_key": spec.profile_key, "version": spec.version,
        "status": spec.status, "proposed_by": spec.proposed_by, "used_llm": spec.proposed_by == "ai",
        "previous_profile_id": prev.profile_id if prev else None, "is_first_profile": prev is None,
        "digest": digest_of(spec),
        "columns_total": len(spec.columns),
        "shared_columns": len(set(new) & set(old)) if prev else None, "previous_columns": len(old) if prev else None, "added": added, "removed": removed, "renamed": renamed,
        "changed": changed, "critical_changed": critical_changed,
        "status_values_added": status_added, "status_values_changed": status_changed,
        "held_files": held, "open_questions": spec.open_questions, "must_accept": need,
        "blockers": [{"code": b.code, "header": b.header, "message": b.message} for b in hard],
        "findings": [{"level": f.level, "code": f.code, "header": f.header} for f in semantic_guards(spec)],
        "can_reject": spec.status in ("draft", "proposed") and reject_available(conn),
        "can_approve": spec.status in ("draft", "proposed") and not hard and not spec.open_questions,
    }


def render_markdown(r: dict) -> str:
    L = [f"## Mapping proposal #{r['profile_id']} - {r['portal']} ({r['profile_key']} v{r['version']})"]
    if r["status"] not in ("draft", "proposed"):
        L.append(f"**Already decided: status is `{r['status']}`.**")
    L.append(f"{r['held_files']} file(s) are held waiting for this. Nothing has been loaded from them yet.")
    L.append("Built with AI assistance for unresolved columns." if r["used_llm"] else "Built from rules and the alias dictionary (no AI call).")
    if r["is_first_profile"]:
        L.append("**First mapping for this layout** - there is no earlier version to compare with; review every column.")
    if not r["is_first_profile"] and r["previous_columns"]:
        if r["shared_columns"] / r["previous_columns"] < 0.5:
            L.append(f"**\u26a0 This file looks completely different from the usual report:** only {r['shared_columns']} of the usual "
                     f"{r['previous_columns']} columns are present.")
        else:
            L.append(f"{r['shared_columns']} of the usual {r['previous_columns']} columns are present.")
    if r["critical_changed"]:
        L.append("**⚠ Money / date / status / identity columns affected:** " + ", ".join(r["critical_changed"]))
    elif not r["is_first_profile"]:
        L.append("✔ No money, date, status or identity column changed.")
    for title, rows, fmt in (
        ("Renamed columns", r["renamed"], lambda x: f"- {x['from']} → {x['to']}  (still {x['field']})"),
        ("New columns", r["added"], lambda x: f"- {x['header']} → {x['maps_to']}"),
        ("Removed columns", r["removed"], lambda x: f"- {x['header']} (was {x['was']})"),
        ("Re-mapped columns", r["changed"], lambda x: f"- {x['header']}: {x['was']} → {x['now']}")):
        if rows:
            L.append(f"**{title}**")
            L += [fmt(x) for x in rows[:40]]
            if len(rows) > 40:
                L.append(f"- ... and {len(rows) - 40} more")
    if r["status_values_added"] or r["status_values_changed"]:
        L.append("**Status values:** new " + (", ".join(r["status_values_added"]) or "none") +
                 "; changed " + (", ".join(r["status_values_changed"]) or "none"))
    if r["blockers"]:
        L.append("**Cannot be approved (unsafe):** " + "; ".join(f"{b['message']} [{b['header']}]" for b in r["blockers"]))
    if r["open_questions"]:
        L.append("**Open questions (resolve by submitting a corrected mapping):** " + "; ".join(
            f"[{q.get('header') or 'general'}] {q['question']}" for q in r["open_questions"][:10]))
    if r["must_accept"]:
        L.append("**Approval must explicitly accept:** " + ", ".join(r["must_accept"]))
    L.append("To refuse this mapping, press **Reject** on the review page (or ask your assistant to reject it). "
             "The files stay held until a corrected mapping is approved.")
    L.append(f"Review code (digest): `{r['digest'][:12]}`")
    return "\n\n".join(L)


def render_html(r: dict) -> str:
    """Minimal, self-contained HTML (inline CSS only, no scripts) for email bodies and the confirmation page."""
    out = []
    for block in render_markdown(r).split("\n\n"):
        if block.startswith("## "):
            out.append(f"<h2>{html.escape(block[3:])}</h2>")
            continue
        lines = block.split("\n")
        if all(x.startswith("- ") for x in lines[1:]) and len(lines) > 1 and lines[0].startswith("**"):
            out.append(f"<p><b>{html.escape(lines[0].strip('*'))}</b></p><ul>" +
                       "".join(f"<li>{html.escape(x[2:])}</li>" for x in lines[1:]) + "</ul>")
        else:
            esc = html.escape(block)
            import re
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)
            out.append(f"<p>{esc}</p>")
    return "".join(out)


# ------------------------------------------------------------------------------------------------ tokens
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign_token(secret: str, profile_id: int, digest: str, ttl_seconds: int, now: float | None = None) -> str:
    if not secret:
        raise ValueError("review secret is not configured")
    exp = int((time.time() if now is None else now) + ttl_seconds)
    payload = f"{profile_id}.{digest[:32]}.{exp}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64(payload.encode()) + "." + _b64(sig)


def verify_token(secret: str, token: str, now: float | None = None) -> tuple[int, str]:
    """Returns (profile_id, digest_prefix) or raises ValueError('invalid'|'expired')."""
    try:
        p64, s64 = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        payload = base64.urlsafe_b64decode(pad(p64))
        sig = base64.urlsafe_b64decode(pad(s64))
        good = hmac.new(secret.encode(), payload, hashlib.sha256).digest() if secret else b""
        if not secret or not hmac.compare_digest(sig, good):
            raise ValueError("invalid")
        pid, dg, exp = payload.decode().split(".")
        if (time.time() if now is None else now) > int(exp):
            raise ValueError("expired")
        return int(pid), dg
    except ValueError as e:
        raise ValueError(str(e) if str(e) in ("invalid", "expired") else "invalid") from None
    except Exception:
        raise ValueError("invalid") from None
