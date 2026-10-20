"""Human review of mapping proposals: change summary, digest-bound approval, and the signed email-link path.
Real PostgreSQL + the real HTTP app (same harness as the end-to-end tests). Everything is synthetic."""
import dataclasses
import re
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from processor import review
from processor.tests import fixtures as fx
from processor.tests.httpharness import KEY
from processor.tests import test_e2e as e2e
from processor.tests.test_stage6 import NEW_HEADERS, new_rows

SECRET = "test-review-secret-0123456789"
BASE = "https://mis.example.test"
H = fx.STD21_HEADERS


class TokenUnit(unittest.TestCase):
    def test_roundtrip_expiry_and_tamper(self):
        t = review.sign_token(SECRET, 7, "ab" * 32, 60, now=1000)
        self.assertEqual(review.verify_token(SECRET, t, now=1010), (7, "ab" * 16))
        with self.assertRaisesRegex(ValueError, "expired"):
            review.verify_token(SECRET, t, now=1061)
        with self.assertRaisesRegex(ValueError, "invalid"):
            review.verify_token("another-secret", t, now=1010)
        p, s = t.split(".")
        for bad in (p + "." + s[:-2] + "AA", "x." + s, p + "x." + s, "garbage", "", "a.b.c"):
            with self.assertRaisesRegex(ValueError, "invalid"):
                review.verify_token(SECRET, bad, now=1010)
        with self.assertRaises(ValueError):
            review.sign_token("", 1, "x", 60)
        with self.assertRaisesRegex(ValueError, "invalid"):
            review.verify_token("", t, now=1010)                      # no secret configured -> nothing verifies


class ReviewFlow(e2e.EndToEnd):
    SETTINGS = {"api_key": KEY, "review_secret": SECRET, "public_base_url": BASE, "review_ttl_hours": 1}

    def web(self, method, path, form=None):
        """Raw HTTP without the API key; returns (status, headers, text)."""
        data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()

    def new_bank_proposal(self):
        self.drive.put("n1", "newbank_leads_20261019_080000.csv", fx.make_csv(NEW_HEADERS, new_rows(3)), "2026-10-19T03:00:00Z")
        self.discover_and_process()
        eid = self.q("SELECT event_id FROM mis.drive_file_events")[0][0]
        code, prop = self.propose(eid, self.drive.download("n1"), portal="newbank", create_portal="true", display_name="NewBank")
        self.assertEqual(code, 200, prop)
        return eid, prop

    # ---- summary content -------------------------------------------------------------------------
    def test_summary_for_a_first_mapping_names_columns_but_never_values(self):
        eid, p = self.new_bank_proposal()
        md = p["review_markdown"]
        self.assertIn("First mapping for this layout", md)
        self.assertIn("Requested Amount", md)                          # column names are shown
        self.assertIn("1 file(s) are held", md)
        self.assertIn("AI assistance", md)
        for secret in ("REF-ZQX", "Pune", "Sentinel"):
            self.assertNotIn(secret, md + p["email_html"] + p["email_text"])
        self.assertEqual(p["review_url"].split("/review/")[0], BASE)
        self.assertIn(p["review_url"], p["email_text"])
        self.assertIn("newbank", p["email_subject"])
        self.assertTrue(p["email_html"].startswith("<h2>"))
        self.assertNotIn("<script", p["email_html"])

    def test_summary_for_a_changed_layout_shows_the_difference_and_whether_money_columns_moved(self):
        # a known portal's file arrives with one extra column
        hdr = H + ["Branch Code"]
        rows = [{**fx.std21_row(i), "Branch Code": "B1"} for i in range(1, 4)]
        self.drive.put("k1", "kreditbuddha_CLICKPE_leads_20261019_060000.csv", fx.make_csv(hdr, rows), "2026-10-19T01:00:00Z")
        out = self.discover_and_process()
        self.assertEqual(out[0]["outcome"], "needs_mapping")
        r = out[0]["proposal"][1]
        md = r["review_markdown"]
        self.assertEqual([a["header"] for a in self.review(r["profile_id"])["added"]], ["Branch Code"])
        self.assertIn("Branch Code", md)
        self.assertIn("No money, date, status or identity column changed", md)
        self.assertEqual(r["critical_changed"], [])
        self.assertNotIn("First mapping", md)

    def review(self, pid):
        code, r = self.srv.call("GET", f"/v1/mapping/{pid}/review")
        self.assertEqual(code, 200, r)
        return r

    def test_review_endpoint_needs_the_api_key(self):
        _, p = self.new_bank_proposal()
        self.assertEqual(self.srv.call("GET", f"/v1/mapping/{p['profile_id']}/review", key=None)[0], 401)
        self.assertEqual(self.review(p["profile_id"])["digest"], p["digest"])

    # ---- approval bound to what was reviewed (the path an assistant uses) -------------------------
    def test_approve_with_digest_succeeds_only_for_the_reviewed_content(self):
        eid, p = self.new_bank_proposal()
        pid = p["profile_id"]
        code, r = self.srv.call("POST", f"/v1/mapping/{pid}/approve", {"approved_by": "owner", "expected_digest": "0" * 64})
        self.assertEqual((code, r["error"]), (409, "proposal_changed"))
        self.assertEqual(self.q("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s", (pid,)), [("proposed",)])
        code, r = self.srv.call("POST", f"/v1/mapping/{pid}/approve", {"approved_by": "owner", "expected_digest": p["digest"]})
        self.assertEqual((code, r["requeued_events"]), (200, [eid]))
        self.assertEqual(self.q("SELECT status, approved_by FROM mis.mapping_profiles WHERE profile_id=%s", (pid,)), [("active", "owner")])
        code, r = self.srv.call("POST", f"/v1/mapping/{pid}/approve", {"approved_by": "owner", "expected_digest": p["digest"]})
        self.assertEqual(code, 400)                                    # already decided: cannot be approved twice

    def test_digest_changes_when_the_proposal_content_changes(self):
        _, p = self.new_bank_proposal()
        pid = p["profile_id"]
        self.execute("UPDATE mis.mapping_profile_columns SET canonical_field='payout_amount' WHERE profile_id=%s AND canonical_field='disbursed_amount'", (pid,))
        self.assertNotEqual(self.review(pid)["digest"], p["digest"])

    # ---- email link path ------------------------------------------------------------------------
    def link_path(self, p):
        return "/review/" + p["review_url"].split("/review/")[1]

    def test_opening_the_link_shows_the_page_and_changes_nothing(self):
        eid, p = self.new_bank_proposal()
        path = self.link_path(p)
        code, hdr, html = self.web("GET", path)
        self.assertEqual(code, 200)
        self.assertIn("Approve this mapping", html)
        self.assertIn("Requested Amount", html)
        self.assertEqual(hdr["cache-control"], "no-store")
        self.assertIn("default-src 'none'", hdr["content-security-policy"])
        self.assertNotIn("<script", html)
        for _ in range(3):                                             # a scanner or a bot fetching it repeatedly
            self.web("GET", path)
        self.assertEqual(self.q("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s", (p["profile_id"],)), [("proposed",)])
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events WHERE event_id=%s", (eid,)), [("needs_mapping",)])

    def test_pressing_approve_activates_requeues_and_records_who(self):
        eid, p = self.new_bank_proposal()
        path = self.link_path(p)
        self.assertEqual(self.web("POST", path + "/approve", {"approved_by": ""})[0], 400)       # a name is required
        code, _, html = self.web("POST", path + "/approve", {"approved_by": "Asha Rao"})
        self.assertEqual(code, 200, html)
        self.assertIn("Approved", html)
        self.assertEqual(self.q("SELECT status, approved_by FROM mis.mapping_profiles WHERE profile_id=%s", (p["profile_id"],)),
                         [("active", "email-link:Asha Rao")])
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events WHERE event_id=%s", (eid,)), [("discovered",)])
        out = self.discover_and_process()                               # the held file now flows
        self.assertEqual((out[0]["outcome"], out[0]["rows"]["inserted"]), ("processed", 3))
        code, _, html = self.web("POST", path + "/approve", {"approved_by": "Asha Rao"})          # second press
        self.assertEqual(code, 400)

    def test_bad_expired_and_stale_links_are_refused(self):
        _, p = self.new_bank_proposal()
        path = self.link_path(p)
        self.assertEqual(self.web("GET", path[:-3] + "xyz")[0], 403)
        self.assertEqual(self.web("POST", path[:-3] + "xyz/approve", {"approved_by": "x"})[0], 403)
        pid = p["profile_id"]
        old = review.sign_token(SECRET, pid, p["digest"], -5)
        self.assertEqual(self.web("GET", "/review/" + old)[0], 410)
        self.assertEqual(self.web("POST", f"/review/{old}/approve", {"approved_by": "x"})[0], 410)
        # content changed after the email was sent -> the link no longer approves what the reader saw
        self.execute("UPDATE mis.mapping_profile_columns SET canonical_field='payout_amount' WHERE profile_id=%s AND canonical_field='disbursed_amount'", (pid,))
        self.assertEqual(self.web("GET", path)[0], 409)
        self.assertEqual(self.web("POST", path + "/approve", {"approved_by": "x"})[0], 409)
        self.assertEqual(self.q("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s", (pid,)), [("proposed",)])


    # ---- rejection ------------------------------------------------------------------------------
    def test_reject_via_api_holds_the_files_and_allows_a_fresh_proposal(self):
        eid, p = self.new_bank_proposal()
        pid = p["profile_id"]
        self.assertTrue(self.review(pid)["can_reject"])
        code, r = self.srv.call("POST", f"/v1/mapping/{pid}/reject", {"rejected_by": "owner", "expected_digest": "0" * 64})
        self.assertEqual((code, r["error"]), (409, "proposal_changed"))
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{pid}/reject", {"expected_digest": p["digest"]})[0], 400)   # name required
        code, r = self.srv.call("POST", f"/v1/mapping/{pid}/reject", {"rejected_by": "owner", "reason": "wrong amount column",
                                                                      "expected_digest": p["digest"]})
        self.assertEqual((code, r["status"], r["held_events"]), (200, "rejected", [eid]))
        self.assertEqual(self.q("SELECT status, rejected_by, rejected_reason FROM mis.mapping_profiles WHERE profile_id=%s", (pid,)),
                         [("rejected", "owner", "wrong amount column")])
        self.assertEqual(self.q("SELECT status, review_reason FROM mis.drive_file_events WHERE event_id=%s", (eid,)),
                         [("needs_review", f"mapping_rejected:{pid}")])
        # a rejected proposal can never be approved, rejected twice, or listed as pending
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{pid}/approve", {"approved_by": "owner"})[0], 400)
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{pid}/reject", {"rejected_by": "owner"})[0], 400)
        self.assertNotIn(pid, [d["profile_id"] for d in self.srv.call("GET", "/v1/mapping/drafts")[1]["drafts"]])
        self.assertFalse(self.review(pid)["can_reject"])
        self.assertIn("Already decided", self.review(pid)["review_markdown"])
        # the held file does not flow, and a new proposal is a NEW profile, not the rejected one
        self.assertEqual(self.discover_and_process(), [])
        code, p2 = self.propose(eid, self.drive.download("n1"), portal="newbank")
        self.assertEqual(code, 200, p2)
        self.assertNotEqual(p2["profile_id"], pid)
        self.assertEqual(self.q("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s", (p2["profile_id"],)), [("proposed",)])

    def test_reject_from_the_email_page(self):
        eid, p = self.new_bank_proposal()
        path = self.link_path(p)
        code, _, html = self.web("GET", path)
        self.assertIn("Reject this mapping", html)
        self.assertEqual(self.web("POST", path + "/reject", {"rejected_by": ""})[0], 400)
        code, _, html = self.web("POST", path + "/reject", {"rejected_by": "Asha Rao", "reason": "not sure about city"})
        self.assertEqual(code, 200, html)
        self.assertIn("Rejected", html)
        self.assertEqual(self.q("SELECT status, rejected_by FROM mis.mapping_profiles WHERE profile_id=%s", (p["profile_id"],)),
                         [("rejected", "email-link:Asha Rao")])
        self.assertEqual(self.web("POST", path + "/approve", {"approved_by": "Asha Rao"})[0], 400)       # the same mail cannot approve now
        self.assertEqual(self.web("POST", path + "/reject", {"rejected_by": "Asha Rao"})[0], 400)
        self.assertNotIn("Reject this mapping", self.web("GET", path)[2])
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events WHERE event_id=%s", (eid,)), [("needs_review",)])

    def test_reject_is_refused_clearly_until_migration_004_is_applied(self):
        _, p = self.new_bank_proposal()
        pid = p["profile_id"]
        with mock.patch("processor.schema_registry.reject_available", return_value=False), \
                mock.patch("processor.review.reject_available", return_value=False):
            self.assertFalse(self.review(pid)["can_reject"])
            code, r = self.srv.call("POST", f"/v1/mapping/{pid}/reject", {"rejected_by": "owner"})
            self.assertEqual(code, 400)
            self.assertIn("migration 004", r["message"])
            self.assertNotIn("Reject this mapping", self.web("GET", self.link_path(p))[2])
        self.assertEqual(self.q("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s", (pid,)), [("proposed",)])

    def test_links_are_disabled_without_a_secret(self):
        _, p = self.new_bank_proposal()
        s2 = dataclasses.replace(self.settings, review_secret="", public_base_url="")
        from processor.tests.httpharness import Server
        with Server(s2, self.db, self.llm) as srv:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.port}" + self.link_path(p))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 503)
            code, r = srv.call("GET", f"/v1/mapping/{p['profile_id']}/review")
            self.assertEqual((code, r["review_url"]), (200, None))


# keep the inherited end-to-end tests out of this module's run (they have their own module)
for _n in [n for n in dir(e2e.EndToEnd) if n.startswith("test_")]:
    setattr(ReviewFlow, _n, None)


if __name__ == "__main__":
    unittest.main()
