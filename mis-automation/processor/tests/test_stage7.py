import unittest
from datetime import date, datetime, timezone

from processor import events, reconcile
from processor.database import connect
from processor.manifest import ManifestError, parse_manifest
from processor.tests import fixtures as fx
from processor.tests.httpharness import KEY, Server
from processor.tests.pgharness import PgCase

H = fx.STD21_HEADERS
IST = timezone.utc  # placeholder, real offsets built explicitly below


def at(iso):                      # an aware instant from an ISO string with offset
    return datetime.fromisoformat(iso)


MF = parse_manifest({"timezone": "Asia/Kolkata", "reports": [
    {"id": "credme_leads_daily", "portal": "credme", "profile_key": "credme_leads_daily", "expect_by": "09:00"},
    {"id": "kreditbuddha_leads_daily", "portal": "kreditbuddha", "profile_key": "kreditbuddha_leads_daily", "expect_by": "09:00"},
    {"id": "brightloans_leads_daily", "portal": "brightloans", "profile_key": "brightloans_leads_daily",
     "expect_by": "09:00", "allow_empty": True},
    {"id": "optional_digicredit", "portal": "digicredit", "profile_key": "digicredit_leads", "required": False},
]})
D = date(2026, 10, 19)                                           # a Monday
BEFORE = at("2026-10-19T08:00:00+05:30")
AFTER = at("2026-10-19T10:00:00+05:30")


def cm(n=1, d="20261019"):
    return (f"CREDME_CLICKPE_ALL_LEADS_{d}_060000.csv", fx.make_csv(H, [fx.std21_row(n, medium="27P864CED981774")]))


def kb(n=1, d="20261019"):
    return (f"kreditbuddha_CLICKPE_leads_{d}_060000.csv", fx.make_csv(H, [fx.std21_row(n)]))


def bl(rows=1, d="20261019"):
    return (f"brightloans_leads_{d}-aaaaaaaa.csv", fx.make_csv(H, [fx.std21_row(i) for i in range(1, rows + 1)]))


class ManifestParsing(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(len(MF.reports), 4)
        for bad in ({}, {"reports": [{"portal": "x"}]}, {"reports": [{"id": "A B", "portal": "x"}]},
                    {"reports": [{"id": "a", "portal": "x"}, {"id": "a", "portal": "y"}]},
                    {"reports": [{"id": "a", "portal": "x", "expect_by": "9am"}]},
                    {"reports": [{"id": "a", "portal": "x", "days": ["funday"]}]}):
            with self.assertRaises(ManifestError, msg=str(bad)):
                parse_manifest(bad)

    def test_example_manifest_loads(self):
        from pathlib import Path
        from processor.manifest import load_manifest
        mf = load_manifest(fx.SEED_DIR.parent / "expected_reports.example.yaml")
        keys = {(r.portal, r.profile_key) for r in mf.reports}
        from processor.schema_registry import load_seed_specs
        seeded = {(s.portal_code, s.profile_key) for s in load_seed_specs(fx.SEED_DIR)}
        self.assertTrue(keys <= seeded, keys - seeded)


class Reconcile(PgCase):
    def setUp(self):
        self.reset_data()

    def run_rec(self, now, **kw):
        with self.db.connection() as c:
            return reconcile.run(c, MF, now=now, date_=D, lookback_days=kw.pop("lookback_days", 1), max_attempts=3, **kw)

    def states(self, out, d=0):
        return {i["id"]: i["state"] for i in out["batches"][d]["items"]}

    def test_before_cutoff_nothing_is_missing_and_no_notification(self):
        out = self.run_rec(BEFORE)
        b = out["batches"][0]
        self.assertEqual((b["status"], b["notify"]), ("open", False))
        self.assertEqual(set(self.states(out).values()), {"not_due"})

    def test_after_cutoff_missing_reports_flagged_then_deduplicated_then_completed(self):
        self.ingest(*cm())
        out = self.run_rec(AFTER)
        b = out["batches"][0]
        self.assertEqual(b["status"], "incomplete")
        self.assertEqual(self.states(out)["credme_leads_daily"], "success")
        self.assertEqual(self.states(out)["kreditbuddha_leads_daily"], "missing")
        self.assertTrue(b["notify"])
        self.assertIn("Missing: kreditbuddha_leads_daily, brightloans_leads_daily", b["message"])
        with self.db.connection() as c:
            reconcile.acknowledge(c, D, b["summary_hash"])
        again = self.run_rec(AFTER)["batches"][0]
        self.assertFalse(again["notify"])                                   # same facts -> no repeat notification
        self.ingest(*kb())
        third = self.run_rec(AFTER)["batches"][0]
        self.assertTrue(third["notify"])                                    # progress is news
        self.ingest(*bl())
        done = self.run_rec(AFTER)["batches"][0]
        self.assertEqual((done["status"], done["notify"]), ("complete", True))
        self.assertEqual(self.q("SELECT status, completed_at IS NOT NULL FROM mis.daily_batches")[0], ("complete", True))
        self.assertIn("3/3 required", done["message"])

    def test_optional_report_does_not_block_completion(self):
        for f in (cm(), kb(), bl()):
            self.ingest(*f)
        out = self.run_rec(AFTER)
        self.assertEqual(out["batches"][0]["status"], "complete")
        self.assertEqual(self.states(out)["optional_digicredit"], "missing")

    def test_failed_file_is_retried_then_reported_failed_when_dead(self):
        from unittest import mock
        from processor import ingest
        name, data = cm()
        f = self.drive_file(name, data)
        with self.db.connection() as c:
            eid = events.register(c, f, tz=self.settings.tz, range_policy="review")["event_id"]
        with mock.patch.object(ingest, "_load", side_effect=RuntimeError("x")):
            with self.db.connection() as c:
                cl = events.claim(c, eid, lease_minutes=30, max_attempts=3)
            ingest.process_event(self.db, self.settings, eid, cl["claim_token"], data)
        out = self.run_rec(AFTER)
        self.assertEqual(self.states(out)["credme_leads_daily"], "in_progress")
        self.assertEqual(out["retry_event_ids"], [eid])
        with mock.patch.object(ingest, "_load", side_effect=RuntimeError("x")):
            for _ in range(2):
                with self.db.connection() as c:
                    cl = events.claim(c, eid, lease_minutes=30, max_attempts=3)
                ingest.process_event(self.db, self.settings, eid, cl["claim_token"], data)
        out = self.run_rec(AFTER)
        self.assertEqual(self.states(out)["credme_leads_daily"], "failed")
        self.assertEqual(out["retry_event_ids"], [])

    def test_abandoned_claim_is_offered_for_retry_after_lease_expiry(self):
        name, data = cm()
        with self.db.connection() as c:
            eid = events.register(c, self.drive_file(name, data), tz=self.settings.tz, range_policy="review")["event_id"]
            events.claim(c, eid, lease_minutes=30, max_attempts=3)
        self.assertEqual(self.run_rec(AFTER)["retry_event_ids"], [])        # live lease: leave it alone
        self.execute("UPDATE mis.drive_file_events SET lease_expires_at = now() - interval '1 hour'")
        self.assertEqual(self.run_rec(datetime.now(timezone.utc))["retry_event_ids"], [eid])

    def test_waiting_for_mapping_and_review_are_reported_distinctly(self):
        self.ingest("kreditbuddha_CLICKPE_leads_20261019_060000.csv", fx.make_csv(H + ["New Col"], [fx.std21_row(1)]))
        self.ingest("brightloans_mystery.csv", b"x")
        out = self.run_rec(AFTER)
        self.assertEqual(self.states(out)["kreditbuddha_leads_daily"], "waiting_mapping")
        self.assertEqual([u["filename"] for u in out["batches"][0]["unexpected"]], ["brightloans_mystery.csv"])
        self.assertIn("Waiting for mapping approval: kreditbuddha_leads_daily", out["batches"][0]["message"])

    def test_empty_file_allowed_vs_not(self):
        self.ingest("brightloans_leads_20261019-aaaaaaaa.csv", fx.make_csv(H, []))
        self.ingest("CREDME_CLICKPE_ALL_LEADS_20261019_060000.csv", fx.make_csv(H, []))
        out = self.run_rec(AFTER)
        self.assertEqual(self.states(out)["brightloans_leads_daily"], "success")        # allow_empty
        self.assertEqual(self.states(out)["credme_leads_daily"], "failed")             # empty not allowed

    def test_weekday_schedule_and_report_date_offset(self):
        mf = parse_manifest({"reports": [
            {"id": "weekday_only", "portal": "credme", "profile_key": "credme_leads_daily", "days": ["mon", "tue"]},
            {"id": "yesterdays", "portal": "kreditbuddha", "profile_key": "kreditbuddha_leads_daily", "report_date_offset_days": -1}]})
        self.ingest(*kb(d="20261018"))
        with self.db.connection() as c:
            sunday = reconcile.run(c, mf, now=at("2026-10-18T12:00:00+05:30"), date_=date(2026, 10, 18), max_attempts=3)
            monday = reconcile.run(c, mf, now=AFTER, date_=D, max_attempts=3)
        self.assertEqual(self.states(sunday)["weekday_only"], "not_expected")
        self.assertEqual(self.states(monday)["yesterdays"], "success")                 # Oct-18 file satisfies Monday's batch
        self.assertEqual(self.states(monday)["weekday_only"], "missing")

    def test_business_date_follows_ist_not_utc(self):
        now_utc = at("2026-10-19T19:00:00+00:00")                                       # = 20 Oct 00:30 IST
        with self.db.connection() as c:
            out = reconcile.run(c, MF, now=now_utc, max_attempts=3)
        self.assertEqual(out["batches"][0]["date"], "2026-10-20")

    def test_complete_batches_are_not_reevaluated_but_open_ones_are(self):
        for f in (cm(), kb(), bl()):
            self.ingest(*f)
        self.run_rec(AFTER)                                                              # D complete
        with self.db.connection() as c:
            out = reconcile.run(c, MF, now=at("2026-10-20T10:00:00+05:30"), date_=date(2026, 10, 20), lookback_days=2, max_attempts=3)
        self.assertEqual([b["date"] for b in out["batches"]], ["2026-10-20"])

    def test_rows_summary_has_counts_and_no_customer_data(self):
        self.ingest(*cm())
        out = self.run_rec(AFTER)
        self.assertEqual(out["batches"][0]["items"][0]["rows"]["inserted"], 1)
        msg = out["batches"][0]["message"]
        self.assertNotIn("900001", msg)
        self.assertIn("inserted 1", msg)


class ReconcileHttp(PgCase):
    SETTINGS = {"api_key": KEY}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import tempfile, yaml
        cls.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"reports": [{"id": "credme_leads_daily", "portal": "credme", "profile_key": "credme_leads_daily"}]}, cls.tmp)
        cls.tmp.close()
        from dataclasses import replace
        cls.settings = replace(cls.settings, manifest_path=cls.tmp.name)
        cls.srv = Server(cls.settings, cls.db).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()
        super().tearDownClass()

    def test_run_and_ack_over_http(self):
        self.reset_data()
        code, out = self.srv.call("POST", "/v1/reconcile/run", {"date": "2026-10-19", "lookback_days": 1})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["batches"][0]["date"], "2026-10-19")
        self.assertEqual(self.srv.call("POST", "/v1/reconcile/run", {}, key=None)[0], 401)
        code, _ = self.srv.call("POST", "/v1/reconcile/ack", {"date": "2026-10-19", "summary_hash": out["batches"][0]["summary_hash"]})
        self.assertEqual(code, 200)

    def test_missing_manifest_file_is_a_clean_400(self):
        from dataclasses import replace
        object.__setattr__(self.srv.app.state.settings, "manifest_path", "/nonexistent.yaml")
        try:
            code, out = self.srv.call("POST", "/v1/reconcile/run", {})
            self.assertEqual((code, out["error"]), (400, "bad_request"))
        finally:
            object.__setattr__(self.srv.app.state.settings, "manifest_path", self.tmp.name)


if __name__ == "__main__":
    unittest.main()
