"""File-name tolerance: a renamed file is recognised by the START of its name, its date is read from the name when that
is unambiguous, and the file is judged by its CONTENT. Anything unclear is flagged (and announced), never silently
guessed. Real PostgreSQL + the real HTTP app. Synthetic data."""
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from processor import filename_parser as fp
from processor.tests import fixtures as fx
from processor.tests import test_e2e as e2e
from processor.tests.httpharness import KEY
from processor.tests.test_stage6 import NEW_HEADERS, new_rows

H = fx.STD21_HEADERS
IST = ZoneInfo("Asia/Kolkata")
PREFIXES = {"credme": ["credme"], "kreditbuddha": ["kreditbuddha"], "creditsea": ["creditsea"], "brightloans": ["brightloans"]}


class Unit(unittest.TestCase):
    def dates(self, stem, up=date(2026, 10, 19), today=date(2026, 10, 20)):
        return fp.lenient_dates(stem, upload_day=up, today=today)

    def test_prefix_ignores_case_and_separators(self):
        for n in ("CREDME_x_1.csv", "cred_me-x.csv", "Cred Me report.csv", "credMe.xlsx", "CRED.ME_leads.csv"):
            self.assertEqual(fp.portal_by_prefix(n, PREFIXES), ("credme", None), n)
        self.assertEqual(fp.portal_by_prefix("leads_20261019.csv", PREFIXES), (None, None))
        self.assertEqual(fp.portal_by_prefix("my_credme_file.csv", PREFIXES), (None, None))          # only the START counts
        self.assertEqual(fp.portal_by_prefix("credit_sea_x.csv", PREFIXES), ("creditsea", None))
        self.assertEqual(fp.portal_by_prefix("ab.csv", {"p": ["ab"], "q": ["ab"]}), (None, "ambiguous_portal_prefix"))

    def test_unambiguous_date_formats(self):
        for stem, want in (("x_20261019_060000", date(2026, 10, 19)), ("x_2026-10-19", date(2026, 10, 19)),
                           ("x 2026_10_19", date(2026, 10, 19)), ("x_19-10-2026", date(2026, 10, 19)),
                           ("x_10-19-2026", date(2026, 10, 19)), ("x_19102026", date(2026, 10, 19)),
                           ("x_19-Oct-2026", date(2026, 10, 19)), ("x_19Oct2026", date(2026, 10, 19)),
                           ("x_October 19 2026", date(2026, 10, 19)), ("x_Oct-19-2026", date(2026, 10, 19)),
                           ("x_05-05-2026", date(2026, 5, 5))):
            d, _ = self.dates(stem, up=date(2026, 10, 19) if want.month == 10 else date(2026, 5, 6), today=date(2026, 10, 20))
            self.assertEqual(d, [want], stem)

    def test_ambiguous_implausible_and_multiple(self):
        d, notes = self.dates("x_05-06-2026", up=date(2026, 6, 5), today=date(2026, 10, 20))
        self.assertEqual(d, [])
        self.assertIn("date_in_name_ambiguous_day_month", notes)
        self.assertEqual(self.dates("x_20300101")[0], [])                             # future
        self.assertEqual(self.dates("x_20200105")[0], [])                             # far in the past
        self.assertEqual(self.dates("x_20261001_20261019")[0], [date(2026, 10, 1), date(2026, 10, 19)])
        self.assertEqual(self.dates("x_abcdef12_99999999")[0], [])                    # hex / junk digits
        self.assertEqual(self.dates("x_20261340")[0], [])                             # invalid month

    def test_resolution_flags_and_fallbacks(self):
        up = datetime(2026, 10, 19, 14, 0, tzinfo=ZoneInfo("UTC"))
        now = datetime(2026, 10, 20, 9, 0, tzinfo=IST)
        r = fp.resolve_lenient("credme_19-10-2026.csv", "credme", tz=IST, uploaded_at=up, now=now)
        self.assertEqual((r.status, r.report_date, r.flag), ("ok", date(2026, 10, 19), "filename_pattern_changed:read_as_dd-mm-yyyy"))
        self.assertEqual(r.observed_at.date(), date(2026, 10, 19))
        r = fp.resolve_lenient("credme_nodate.csv", "credme", tz=IST, uploaded_at=up, now=now)
        self.assertEqual(r.report_date, date(2026, 10, 19))                           # upload day in IST
        self.assertIn("no_usable_date_in_name", r.flag)
        r = fp.resolve_lenient("credme_20261001_20261019.csv", "credme", tz=IST, uploaded_at=up, now=now)
        self.assertEqual((r.status, r.reason), ("review", "filename_multiple_dates"))


class Flow(e2e.EndToEnd):
    SETTINGS = {"api_key": KEY}

    def put_std(self, fid, name, n=3, created="2026-10-19T14:00:00Z", headers=H):
        rows = [fx.std21_row(i, medium="000864CED981774") for i in range(1, n + 1)]
        self.drive.put(fid, name, fx.make_csv(headers, rows, footer=fx.std21_footer()), created)

    def event(self, name):
        return self.q("SELECT status, review_reason, report_date, portal_id IS NOT NULL FROM mis.drive_file_events WHERE original_filename=%s", (name,))[0]

    def test_renamed_file_is_processed_by_content_and_flagged(self):
        name = "Cred_Me_ClickPe_All_Leads_19-10-2026.csv"
        self.put_std("c1", name)
        out = self.discover_and_process()
        self.assertEqual((out[0]["outcome"], out[0]["rows"]["inserted"]), ("processed", 3))
        st, why, rd, has_portal = self.event(name)
        self.assertEqual((st, why, str(rd), has_portal), ("processed", "filename_pattern_changed:read_as_dd-mm-yyyy", "2026-10-19", True))
        self.assertEqual(out[0]["filename_flag"], why)
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 3)
        code, att = self.srv.call("POST", "/v1/events/attention")
        self.assertEqual(att["count"], 1)
        self.assertIn("loaded, but the file name did not match the usual pattern", att["text"])
        self.assertIn(name, att["text"])
        self.assertIn("<li>", att["email_html"])
        self.assertEqual(att["email_subject"], "[MIS] 1 file(s) need attention")
        self.assertEqual(self.srv.call("POST", "/v1/events/attention")[1]["count"], 0)       # announced once

    def test_correctly_named_file_is_not_flagged(self):
        self.put_std("c1", "CREDME_CLICKPE_ALL_LEADS_20261019_060000.csv")
        out = self.discover_and_process()
        self.assertEqual(out[0]["outcome"], "processed")
        self.assertIsNone(out[0]["filename_flag"])
        self.assertEqual(self.srv.call("POST", "/v1/events/attention")[1]["count"], 0)

    def test_no_or_ambiguous_date_falls_back_to_upload_day_and_says_so(self):
        for fid, name, n in (("c1", "credme_leads_05-06-2026.csv", 3), ("c2", "credme-leads.csv", 4)):
            self.put_std(fid, name, n=n, created="2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual({o["outcome"] for o in out}, {"processed"})
        for name, note in (("credme_leads_05-06-2026.csv", "date_in_name_ambiguous_day_month"), ("credme-leads.csv", "no_usable_date_in_name")):
            st, why, rd, _ = self.event(name)
            self.assertEqual((st, str(rd)), ("processed", "2026-10-19"))
            self.assertIn(note, why)

    def test_several_dates_in_the_name_are_held_not_guessed(self):
        name = "credme_leads_20261001_20261019.csv"
        self.put_std("c1", name)
        self.assertEqual(self.discover_and_process(), [])
        st, why, _, has_portal = self.event(name)
        self.assertEqual((st, why, has_portal), ("needs_review", "filename_multiple_dates", True))
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        self.assertIn("several dates", self.srv.call("POST", "/v1/events/attention")[1]["text"])

    def test_completely_unknown_name_gets_a_proposal_and_a_new_file_type_email(self):
        name = "weekly_report_20261019.csv"
        self.drive.put("u1", name, fx.make_csv(NEW_HEADERS, new_rows(3)), "2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual((out[0]["outcome"], out[0]["reason"]), ("needs_mapping", "unknown_file"))
        code, prop = out[0]["proposal"]
        self.assertEqual(code, 200, prop)
        self.assertIn("A file nobody recognised", prop["review_markdown"])
        self.assertIn("weekly", prop["review_markdown"])
        self.assertIn("NEW FILE TYPE", prop["email_subject"])
        self.assertEqual(self.q("SELECT is_active FROM mis.reporting_portals WHERE portal_code='weekly'"), [(False,)])
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        # nothing becomes live until a person approves: an inactive portal is invisible to name matching
        self.drive.put("u2", "weekly_report_20261020.csv", fx.make_csv(NEW_HEADERS, new_rows(2, start=9)), "2026-10-20T01:00:00Z")
        out2 = self.discover_and_process()
        self.assertEqual(out2[0]["outcome"], "needs_mapping")
        self.assertEqual(out2[0]["proposal"][1]["profile_id"], prop["profile_id"])          # same pending decision, not a second one
        self.assertEqual(self.q("SELECT count(*) FROM mis.drive_file_events WHERE profile_id=%s AND status='needs_mapping'", (prop["profile_id"],))[0][0], 2)
        code, appr = self.srv.call("POST", f"/v1/mapping/{prop['profile_id']}/approve", {"approved_by": "owner"})
        self.assertEqual(len(appr["requeued_events"]), 2)                                  # both waiting files are released
        out3 = self.discover_and_process()
        self.assertEqual(sorted(o["outcome"] for o in out3), ["processed", "processed"])
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 5)

    def with_ai_read(self, reply):
        """Make the LLM answer the advisory 'what is this file' call with `reply`; column mapping still gets GOOD."""
        good = self.llm.complete_json

        def route(system, user):
            if system.startswith("You look at the NAME"):
                if isinstance(reply, Exception):
                    raise reply
                self.llm.calls += 1
                self.last_ai_prompt = user
                return reply
            return good(system, user)
        self.llm.complete_json = route

    def test_ai_read_can_name_the_portal_and_is_shown_to_the_reviewer(self):
        self.with_ai_read({"looks_like_lead_report": True, "portal_code": "acmefinance", "confidence": "high",
                           "reason": "name and columns match a lender lead export"})
        self.drive.put("u1", "export_20261019.csv", fx.make_csv(NEW_HEADERS, new_rows(2)), "2026-10-19T14:00:00Z")
        prop = self.discover_and_process()[0]["proposal"][1]
        self.assertIn("acmefinance", prop["review_markdown"])
        self.assertIn("AI read", prop["review_markdown"])
        self.assertIn("looks like a lead report", prop["review_markdown"])
        self.assertEqual(self.q("SELECT is_active FROM mis.reporting_portals WHERE portal_code='acmefinance'"), [(False,)])
        self.assertNotIn("REF-ZQX", self.last_ai_prompt)                                  # names only, never values
        self.assertIn("Lead Reference", self.last_ai_prompt)

    def test_ai_says_not_a_lead_report_blocks_approval_but_can_be_rejected(self):
        self.with_ai_read({"looks_like_lead_report": False, "portal_code": "unknown", "confidence": "high",
                           "reason": "looks like an invoice export"})
        self.drive.put("u1", "invoices_20261019.csv", fx.make_csv(NEW_HEADERS, new_rows(2)), "2026-10-19T14:00:00Z")
        prop = self.discover_and_process()[0]["proposal"][1]
        self.assertIn("does NOT look like a lead report", prop["review_markdown"])
        self.assertFalse(prop["can_approve"])
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{prop['profile_id']}/approve", {"approved_by": "o"})[0], 400)
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{prop['profile_id']}/reject", {"rejected_by": "o"})[0], 200)
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)

    def fallback_case(self, reply):
        self.with_ai_read(reply)
        self.drive.put("u1", "zeta_leads_20261019.csv", fx.make_csv(NEW_HEADERS, new_rows(2)), "2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual(out[0]["proposal"][0], 200)
        self.assertEqual(self.q("SELECT portal_code FROM mis.reporting_portals WHERE portal_code IN ('zeta','guesswork')"), [("zeta",)])

    def test_low_confidence_ai_read_falls_back_to_the_file_name(self):
        self.fallback_case({"looks_like_lead_report": True, "portal_code": "guesswork", "confidence": "low", "reason": "?"})

    def test_failed_ai_read_falls_back_to_the_file_name(self):
        from processor.llm import LLMError
        self.fallback_case(LLMError("down"))

    def test_malformed_ai_read_falls_back_to_the_file_name(self):
        self.fallback_case({"nonsense": 1})

    def test_no_usable_name_cannot_be_proposed(self):
        self.drive.put("u1", "2026101912.csv", fx.make_csv(NEW_HEADERS, new_rows(2)), "2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual(out[0]["outcome"], "needs_mapping")
        self.assertEqual(out[0]["proposal"][0], 400)                                       # no portal derivable -> a person decides
        code, att = self.srv.call("POST", "/v1/events/attention")
        self.assertIn("a file nobody recognises", att["text"])

    def test_switch_off_keeps_the_old_hold_and_announce_behaviour(self):
        import dataclasses
        from processor.tests.httpharness import Server
        off = dataclasses.replace(self.settings, propose_unknown_files=False)
        with Server(off, self.db, self.llm) as srv:
            self.drive.put("u1", "weekly_report_20261019.csv", fx.make_csv(H, [fx.std21_row(1)]), "2026-10-19T14:00:00Z")
            w = srv.call("GET", "/v1/discovery/window")[1]
            for page in self.drive.list(w["created_after"]):
                srv.call("POST", "/v1/discovery/register", {"files": page})
            self.assertEqual(self.event("weekly_report_20261019.csv")[:2], ("needs_review", "no_profile_matches_filename"))
            self.assertIn("may be a different kind of file", srv.call("POST", "/v1/events/attention")[1]["text"])

    def test_known_prefix_but_different_columns_is_held_proposed_and_called_completely_different(self):
        name = "credme leads 2026-10-19.csv"
        self.drive.put("c1", name, fx.make_csv(NEW_HEADERS, new_rows(3)), "2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual(out[0]["outcome"], "needs_mapping")                         # judged by content, not rejected for its name
        prop = out[0]["proposal"][1]
        self.assertIn("looks completely different", prop["review_markdown"])
        self.assertNotIn("First mapping", prop["review_markdown"])
        self.assertIn("completely different", prop["email_html"])
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)

    def test_range_file_policy_is_unchanged(self):
        # a deliberately ambiguous range stays with a person (the strict path owns it)
        self.drive.put("r1", "leads_20260930_20261030.csv", fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(1)]), "2026-10-19T14:00:00Z")
        self.discover_and_process()
        self.assertEqual(self.event("leads_20260930_20261030.csv")[:2], ("needs_review", "filename_date_issue:range_end_after_upload_day"))


for _n in [n for n in dir(e2e.EndToEnd) if n.startswith("test_")]:
    setattr(Flow, _n, None)

if __name__ == "__main__":
    unittest.main()
