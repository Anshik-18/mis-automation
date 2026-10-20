import json
import unittest
from decimal import Decimal

from processor import events, ingest, proposer
from processor.database import connect
from processor.guards import blockers, required_acceptances, semantic_guards
from processor.llm import LLMError, NullLLM
from processor.normalization import looks_sensitive
from processor.profiler import infer_attribution, llm_view, map_status_values, profile_table, suggest_filename_regex
from processor.file_parser import read_table
from processor.filename_parser import parse_with_pattern
from processor.schema_registry import (ProfileSpecError, approve_and_activate, load_aliases, load_seed_specs,
                                       load_spec_from_db)
from processor.tests import fixtures as fx
from processor.tests.pgharness import PgCase
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

SENT_NAME, SENT_PHONE, SENT_ID = "ZQXSENTINELNAME", "9876501234", "REF-ZQX-0001"
NEW_HEADERS = ["Lead Reference", "Customer Name", "Mobile", "Requested Amount", "Status", "Funds Released",
               "Release Date", "utm_campaign", "City"]


def new_rows(n=3, start=1, status="Fresh Lead"):
    return [{"Lead Reference": f"REF-ZQX-{start + i:04d}", "Customer Name": SENT_NAME, "Mobile": SENT_PHONE,
             "Requested Amount": "50000", "Status": status if i else "Disbursed", "Funds Released": "45000" if not i else "",
             "Release Date": "2026-10-20" if not i else "", "utm_campaign": "clickpe" + fx.hexid(start + i, "27P"), "City": "Pune"}
            for i in range(n)]


class FakeLLM:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls, self.prompts = response, error, 0, []

    def complete_json(self, system, user):
        self.calls += 1
        self.prompts.append(user)
        if self.error:
            raise self.error
        return self.response() if callable(self.response) else self.response


GOOD = {"columns": [
    {"header": "Lead Reference", "role": "canonical", "field": "external_lead_id", "transform": "text"},
    {"header": "Requested Amount", "role": "canonical", "field": "requested_loan_amount", "transform": "number"},
    {"header": "Funds Released", "role": "canonical", "field": "disbursed_amount", "transform": "number"},
    {"header": "Release Date", "role": "canonical", "field": "disbursed_date", "transform": "date", "date_format": "YYYY-MM-DD"}],
    "open_questions": []}


class Guards(unittest.TestCase):
    def spec_with(self, header, field, **kw):
        from dataclasses import replace
        base = {s.profile_key: s for s in load_seed_specs(fx.SEED_DIR)}["digicredit_leads"]
        cols = [c for c in base.columns if c.canonical_field not in (field,)]
        cols.append(proposer.ColumnSpec(header, "canonical", canonical_field=field, transform="number"))
        return replace(base, columns=cols, **kw)

    def test_loan_amt_cannot_be_disbursed_amount(self):
        codes = {f.code for f in blockers(self.spec_with("Loan Amt", "disbursed_amount"))}
        self.assertIn("loan_amount_not_disbursed", codes)

    def test_approved_amt_cannot_be_disbursed_amount(self):
        self.assertIn("approved_not_disbursed", {f.code for f in blockers(self.spec_with("Approved Amt", "disbursed_amount"))})

    def test_net_disbursed_needs_explicit_acceptance(self):
        sp = self.spec_with("Net Disbursed Amount (INR)", "disbursed_amount")
        self.assertEqual(blockers(sp), [])
        self.assertIn("net_disbursed_amount_semantics", required_acceptances(sp))

    def test_application_number_in_lead_id_namespace_blocked(self):
        from dataclasses import replace
        cs = {s.profile_key: s for s in load_seed_specs(fx.SEED_DIR)}["creditsea_disbursed"]
        self.assertIn("identity_namespace_mismatch", {f.code for f in blockers(replace(cs, identity_namespace="lead_id"))})

    def test_sensitive_header_cannot_be_stored(self):
        self.assertIn("sensitive_column_stored", {f.code for f in blockers(self.spec_with("Customer Phone", "city"))})

    def test_every_seed_profile_has_no_blockers(self):
        for s in load_seed_specs(fx.SEED_DIR):
            self.assertEqual(blockers(s), [], s.profile_key)


class Profiler(unittest.TestCase):
    def test_llm_view_has_no_raw_sensitive_values(self):
        t = read_table(fx.make_csv(NEW_HEADERS, new_rows(6)))
        view = json.dumps(llm_view(profile_table(t), t.headers))
        for secret in (SENT_NAME, SENT_PHONE, SENT_ID, "REF-ZQX"):
            self.assertNotIn(secret, view)
        self.assertIn("looks_sensitive", view)                         # the Mobile column is flagged, not shown

    def test_enum_values_shown_only_for_safe_low_cardinality_columns(self):
        t = read_table(fx.make_csv(NEW_HEADERS, new_rows(6)))
        cols = {c["header"]: c for c in llm_view(profile_table(t), t.headers)["columns"]}
        self.assertEqual(cols["City"]["distinct_values"], ["Pune"])
        self.assertNotIn("distinct_values", cols["Lead Reference"])
        self.assertNotIn("distinct_values", cols["utm_campaign"])          # identifier-like

    def test_phone_like_value_in_innocent_column_suppresses_values(self):
        rows = [{"Lead ID": "1", "Notes": "call 9876501234 now"}]
        t = read_table(fx.make_csv(["Lead ID", "Notes"], rows))
        view = json.dumps(llm_view(profile_table(t), ["Notes"]))
        self.assertNotIn("9876501234", view)
        self.assertTrue(looks_sensitive("call 9876501234 now"))

    def test_attribution_inference_prefix_and_coverage(self):
        vals = ["clickpe" + fx.hexid(i, "27P") for i in range(20)]
        inf = infer_attribution(vals)
        self.assertEqual((inf["dominant_prefix"], inf["coverage"]), ("clickpe", 1.0))
        self.assertIsNone(infer_attribution(["app", "APP", "KF_12"]))

    def test_status_keyword_mapping(self):
        m, unknown = map_status_values(["SYSTEM-REJECT", "DISBURSE-PENDING", "DISBURSED", "Fresh Lead", "APPLICATION-HOLD",
                                        "Auto Withdrawal", "Whatever"])
        self.assertEqual(m["DISBURSE-PENDING"], "disbursement_pending")
        self.assertEqual((m["DISBURSED"], m["SYSTEM-REJECT"], m["Auto Withdrawal"]), ("disbursed", "rejected", "withdrawn"))
        self.assertEqual(unknown, ["Whatever"])

    def test_filename_regex_suggestion_round_trips(self):
        tz = ZoneInfo("Asia/Kolkata")
        up = datetime(2026, 10, 20, 5, tzinfo=timezone.utc)
        for name, key in (("acme_leads_20261019_101500.csv", "report_date"), ("acme_leads_2026-10-19-abcdef12.csv", "report_date"),
                          ("acme_leads_20260930_20261019-abcdef12.csv", "period_end")):
            rx, _ = suggest_filename_regex(name)
            info = parse_with_pattern(name, rx, "YYYY-MM-DD" if "-" in name.split("leads_")[1][:5] else "YYYYMMDD", tz=tz, uploaded_at=up)
            self.assertTrue(info.ok, (name, rx, info.issues))
            self.assertEqual(info.report_date.isoformat(), "2026-10-19")


class Propose(PgCase):
    ISOLATED = True
    ACTIVATE = ("credme_leads_daily", "kreditbuddha_leads_daily", "brightloans_leads_daily", "salarymantra_leads_daily",
                "digicredit_leads", "kreditbuddha_leads_range", "paytrust_leads")
    SETTINGS = {"alias_dictionary_path": str(fx.SEED_DIR.parent / "column_aliases.yaml")}

    def register_unknown(self, name, data):
        f = self.drive_file(name, data)
        with self.db.connection() as c:
            return events.register(c, f, tz=self.settings.tz, range_policy="review")

    def propose(self, reg, data, llm, **kw):
        return proposer.propose(self.db, self.settings, llm, reg["event_id"], data, **kw)

    def activate(self, p, flags=()):
        with self.db.connection() as c:
            approve_and_activate(c, p.profile_id, "ops@example.com", list(flags))
            return events.requeue_needs_mapping(c, p.profile_id)

    def run_event(self, eid, data):
        with self.db.connection() as c:
            cl = events.claim(c, eid, lease_minutes=30, max_attempts=3)
        return ingest.process_event(self.db, self.settings, eid, cl["claim_token"], data)

    def test_unknown_portal_full_lifecycle_with_profile_reuse(self):
        data = fx.make_csv(NEW_HEADERS, new_rows(4))
        name = "newbank_leads_20261020_083000.csv"
        reg = self.register_unknown(name, data)
        self.assertEqual((reg["status"], reg["reason"]), ("needs_review", "no_profile_matches_filename"))
        llm = FakeLLM(GOOD)
        p = self.propose(reg, data, llm, portal_code="newbank", create_portal=True, display_name="NewBank")
        self.assertEqual((llm.calls, p.used_llm, p.open_questions), (1, True, []))
        for secret in (SENT_NAME, SENT_PHONE, "REF-ZQX"):
            self.assertNotIn(secret, llm.prompts[0])                          # nothing personal reached the model
        self.assertNotIn(SENT_NAME, p.notification)
        self.assertEqual(self.q("SELECT status, review_reason FROM mis.drive_file_events")[0], ("needs_mapping", "awaiting_mapping_approval"))
        sp = load_spec_from_db(connect(self.url), p.profile_id)
        self.assertEqual((sp.status, sp.proposed_by, sp.identity_namespace), ("proposed", "ai", "lead_id"))
        by = {c.header: c for c in sp.columns}
        self.assertEqual(by["Customer Name"].role, "exclude")
        self.assertEqual(by["Mobile"].role, "exclude")
        self.assertEqual(sp.attribution_rules[0].strip_prefixes, ("clickpe",))
        # held until approved: nothing loaded, event not claimable
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        requeued = self.activate(p)
        self.assertEqual(requeued, [reg["event_id"]])
        out = self.run_event(reg["event_id"], data)
        self.assertEqual((out["outcome"], out["rows"]["inserted"]), ("processed", 4), out)
        l = self.lead("newbank", "REF-ZQX-0001")
        self.assertEqual((l["status"], l["disbursed"], l["code"]), ("disbursed", Decimal("45000.00"), "27P"))
        # next day, same layout: deterministic path, no further LLM call
        data2 = fx.make_csv(NEW_HEADERS, new_rows(2, start=10))
        r2 = self.ingest("newbank_leads_20261021_083000.csv", data2)
        self.assertEqual(r2["outcome"], "processed")
        self.assertEqual(llm.calls, 1)

    def test_llm_never_called_for_known_compatible_formats(self):
        llm = FakeLLM(GOOD)
        self.ingest("kreditbuddha_CLICKPE_leads_20261019_101500.csv", fx.make_csv(fx.STD21_HEADERS, [fx.std21_row(1)]))
        self.ingest("digicredit_leads_20261019_20261019-aaaaaaaa.csv", fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(1)]))
        self.assertEqual(llm.calls, 0)

    def test_unsafe_llm_mapping_is_demoted_and_blocks_approval(self):
        bad = {"columns": [dict(GOOD["columns"][0]),
                           {"header": "Requested Amount", "role": "canonical", "field": "disbursed_amount", "transform": "number"}],
               "open_questions": [{"header": None, "question": "Is Funds Released gross?"}]}
        data = fx.make_csv(NEW_HEADERS, new_rows(3))
        reg = self.register_unknown("newbank_leads_20261020_083000.csv", data)
        p = self.propose(reg, data, FakeLLM(bad), portal_code="newbank", create_portal=True)
        sp = load_spec_from_db(connect(self.url), p.profile_id)
        self.assertEqual({c.header: c.role for c in sp.columns}["Requested Amount"], "extra")      # demoted, not stored as disbursed
        self.assertTrue(any("loan_amount_not_disbursed" in q["question"] for q in p.open_questions))
        with self.assertRaises(ProfileSpecError) as cm, connect(self.url) as c:
            approve_and_activate(c, p.profile_id, "ops@example.com")
        self.assertIn("open question", str(cm.exception))

    def test_ambiguous_identifier_requires_review(self):
        both = {"columns": [dict(GOOD["columns"][0]),
                            {"header": "Requested Amount", "role": "canonical", "field": "external_lead_id", "transform": "text"}],
                "open_questions": []}
        data = fx.make_csv(NEW_HEADERS, new_rows(3))
        reg = self.register_unknown("newbank_leads_20261020_083000.csv", data)
        p = self.propose(reg, data, FakeLLM(both), portal_code="newbank", create_portal=True)
        self.assertTrue(any(q["blocking"] for q in p.open_questions))

    def test_invalid_llm_output_falls_back_safely(self):
        data = fx.make_csv(NEW_HEADERS, new_rows(3))
        for i, llm in enumerate((FakeLLM({"columns": "nope"}), FakeLLM(error=LLMError("HTTP 500")), NullLLM())):
            self.reset_data()
            self.execute("DELETE FROM mis.mapping_profiles WHERE profile_key ~ '^newbank'") if i else None
            reg = self.register_unknown("newbank_leads_20261020_083000.csv", data)
            p = self.propose(reg, data, llm, portal_code="newbank", create_portal=True)
            sp = load_spec_from_db(connect(self.url), p.profile_id)
            self.assertFalse(p.used_llm)
            self.assertEqual(sp.proposed_by, "human")
            self.assertTrue(any(q["blocking"] for q in p.open_questions))      # no identity column found -> person must decide
            roles = {c.header: c.role for c in sp.columns}
            self.assertEqual((roles["Customer Name"], roles["Mobile"]), ("exclude", "exclude"))
            self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)

    def test_extra_column_in_known_layout_creates_new_version_only_after_approval(self):
        rows = [fx.std21_row(1), fx.std21_row(2, status="DISBURSED", **{"Disbursment Amount": "9000", "Disbursment Date": "2026-10-19"})]
        for r in rows:
            r["Utm Content"] = "banner-a"
        data = fx.make_csv(fx.STD21_HEADERS + ["Utm Content"], rows)
        name = "kreditbuddha_CLICKPE_leads_20261020_101500.csv"
        first = self.ingest(name, data)
        self.assertEqual(first["outcome"], "needs_mapping")
        reg = {"event_id": first["event_id"]}
        p = self.propose(reg, data, NullLLM())
        sp = load_spec_from_db(connect(self.url), p.profile_id)
        self.assertEqual((sp.profile_key, sp.version, sp.open_questions), ("kreditbuddha_leads_daily", 2, []))
        self.assertEqual({c.header: c.role for c in sp.columns}["Utm Content"], "extra")
        self.assertEqual(sp.attribution_rules[0].source_field, "utm_campaign")
        # old version still active until a person approves the new one
        self.assertEqual(self.q("SELECT version FROM mis.mapping_profiles WHERE profile_key='kreditbuddha_leads_daily' AND status='active'"), [(1,)])
        self.activate(p)
        self.assertEqual(self.q("SELECT version, status FROM mis.mapping_profiles WHERE profile_key='kreditbuddha_leads_daily' ORDER BY version"),
                         [(1, "retired"), (2, "active")])
        out = self.run_event(reg["event_id"], data)
        self.assertEqual(out["outcome"], "processed", out)
        self.assertEqual(out["profile_version"], 2)
        self.assertEqual(self.lead("kreditbuddha", "900001")["attrs"].get("utm_content"), "banner-a")

    def test_propose_twice_reuses_draft(self):
        data = fx.make_csv(NEW_HEADERS, new_rows(3))
        reg = self.register_unknown("newbank_leads_20261020_083000.csv", data)
        llm = FakeLLM(GOOD)
        a = self.propose(reg, data, llm, portal_code="newbank", create_portal=True)
        b = self.propose(reg, data, llm, portal_code="newbank")
        self.assertEqual((a.profile_id, b.reused, llm.calls), (b.profile_id, True, 1))

    def test_disbursed_only_layout_is_inferred_and_needs_acceptance(self):
        hdr = ["Lead ID", "Disbursed Amount", "Disbursal Date", "Campaign"]
        rows = [fx.partial_row(i, n=i) for i in range(1, 6)]
        data = fx.make_csv(hdr, rows)
        reg = self.register_unknown("partnerx_disbursed_20261020.csv", data)
        p = self.propose(reg, data, NullLLM(), portal_code="partnerx", create_portal=True)
        sp = load_spec_from_db(connect(self.url), p.profile_id)
        self.assertEqual((sp.report_type, sp.import_mode, sp.implied_status_code, sp.on_unknown_lead),
                         ("disbursed_cases", "enrichment", "disbursed", "quarantine"))
        self.assertIn("report_type_inferred_disbursed_only", p.flags)
        with self.assertRaises(ProfileSpecError), connect(self.url) as c:
            approve_and_activate(c, p.profile_id, "ops@example.com")
        with connect(self.url) as c:
            approve_and_activate(c, p.profile_id, "ops@example.com", ["report_type_inferred_disbursed_only"])
            c.commit()

    def test_seeded_net_disbursed_profile_cannot_be_activated_without_acceptance(self):
        with connect(self.url) as c:
            pid = c.execute("SELECT profile_id FROM mis.mapping_profiles WHERE profile_key='creditsea_disbursed'").fetchone()[0]
            with self.assertRaises(ProfileSpecError) as cm:
                approve_and_activate(c, pid, "ops@example.com")
            self.assertIn("net_disbursed_amount_semantics", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
