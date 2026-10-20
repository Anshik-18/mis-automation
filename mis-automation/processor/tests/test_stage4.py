import threading
import unittest
from decimal import Decimal
from unittest import mock

from processor import events, ingest
from processor.database import connect
from processor.tests import fixtures as fx
from processor.tests.pgharness import PgCase

H = fx.STD21_HEADERS


def kb_name(date="20261019", time="101500"):
    return f"kreditbuddha_CLICKPE_leads_{date}_{time}.csv"


def kb_csv(rows, footer=None):
    return fx.make_csv(H, rows, footer=footer)


class Ingest(PgCase):
    def setUp(self):
        self.reset_data()

    def test_basic_load_counts_attribution_evidence_history(self):
        rows = [fx.std21_row(1, code="000"), fx.std21_row(2, code="27P"), fx.std21_row(3, campaign="ORGANIC"),
                fx.std21_row(4, campaign="CLICKPEKAMSAL9")]
        out = self.ingest(kb_name(), kb_csv(rows))
        self.assertEqual(out["outcome"], "processed", out)
        self.assertEqual(out["rows"]["inserted"], 4)
        self.assertEqual(self.lead("kreditbuddha", "900001")["code"], "000")        # leading zeros survive
        self.assertEqual(self.lead("kreditbuddha", "900001")["attr"], "unregistered_code")
        self.assertEqual(self.lead("kreditbuddha", "900003")["attr"], "organic")
        self.assertEqual(self.lead("kreditbuddha", "900004")["attr"], "unresolved")
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_attribution_evidence")[0][0], 4)
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_history")[0][0], 4)
        self.assertEqual(self.q("SELECT processing_status, rows_inserted FROM mis.source_files")[0], ("processed", 4))
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events")[0][0], "processed")

    def test_registered_partner_resolves_and_original_utm_kept(self):
        with connect(self.url) as c:
            c.execute("INSERT INTO mis.attribution_partners (attribution_code, partner_name) VALUES ('000','Partner Zero'),('27P','Partner 27P')")
            c.commit()
        self.ingest(kb_name(), kb_csv([fx.std21_row(1, code="000"), fx.std21_row(2, code="27P")]))
        self.assertEqual(self.lead("kreditbuddha", "900001")["attr"], "resolved")
        self.assertEqual(self.q("SELECT utm_campaign FROM mis.lead_attribution_evidence e JOIN mis.leads l USING (lead_id) "
                                "WHERE l.external_lead_id='900001'")[0][0], fx.hexid(1, "000").join(["CLICKPE", ""]))

    def test_exact_retry_other_name_is_duplicate_content_and_changes_nothing(self):
        data = kb_csv([fx.std21_row(1), fx.std21_row(2)])
        a = self.ingest(kb_name(), data)
        before = self.q("SELECT count(*) FROM mis.lead_history")[0][0]
        b = self.ingest("kreditbuddha_CLICKPE_leads_20261019_101500_copy.csv", data)       # no profile: name differs
        self.assertEqual(b["outcome"], "not_claimed")
        c = self.ingest(kb_name(), data)                    # same name, new Drive id, same bytes
        self.assertEqual(c["outcome"], "duplicate_content")
        self.assertEqual(self.q("SELECT count(*) FROM mis.source_files")[0][0], 1)
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_history")[0][0], before)
        self.assertEqual(self.q("SELECT count(*) FROM mis.drive_file_events")[0][0], 3)

    def test_same_drive_file_registered_twice_is_one_event(self):
        data = kb_csv([fx.std21_row(1)])
        f = self.drive_file(kb_name(), data, file_id="same")
        with self.db.connection() as c:
            r1 = events.register(c, f, tz=self.settings.tz, range_policy="review")
            r2 = events.register(c, f, tz=self.settings.tz, range_policy="review")
        self.assertTrue(r1["is_new"])
        self.assertFalse(r2["is_new"])
        self.assertEqual(r1["event_id"], r2["event_id"])

    def test_replay_of_processed_file_is_idempotent(self):
        data = kb_csv([fx.std21_row(1), fx.std21_row(2, status="SANCTION")])
        out = self.ingest(kb_name(), data)
        hist = self.q("SELECT count(*) FROM mis.lead_history")[0][0]
        with self.db.connection() as c:
            events.replay(c, [out["event_id"]], reason="test", reprocess_processed=True)
            cl = events.claim(c, out["event_id"], lease_minutes=30, max_attempts=3)
        again = ingest.process_event(self.db, self.settings, cl["event_id"], cl["claim_token"], data)
        self.assertEqual(again["outcome"], "processed")
        self.assertEqual((again["rows"]["inserted"], again["rows"]["updated"], again["rows"]["unchanged"]), (0, 0, 2))
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_history")[0][0], hist)

    def test_status_update_newer_file_and_history(self):
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION")]))
        out = self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, status="DISBURSED", **{
            "Disbursment Amount": "25000", "Disbursment Date": "2026-10-19", "Payout": "1250"})]))
        self.assertEqual(out["rows"]["updated"], 1)
        l = self.lead("kreditbuddha", "900001")
        self.assertEqual((l["status"], l["disbursed"], l["payout"]), ("disbursed", Decimal("25000.00"), Decimal("1250.00")))
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_history")[0][0], 2)

    def test_stale_older_file_cannot_overwrite_newer_data(self):
        self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, status="DISBURSED", **{
            "Disbursment Amount": "25000", "Disbursment Date": "2026-10-19"})]))
        out = self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION", State="OLDSTATE")]))
        self.assertEqual(out["outcome"], "processed")
        l = self.lead("kreditbuddha", "900001")
        self.assertEqual((l["status"], l["disbursed"]), ("disbursed", Decimal("25000.00")))
        self.assertEqual(l["state"], "Teststate")                  # older file may not change an existing descriptor
        self.assertEqual(out["rows"]["stale_skipped"], 1)

    def test_blank_fields_in_newer_file_do_not_erase_values(self):
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION", State="Delhi", City="Delhi")]))
        self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, status="SANCTION", State="", City="")]))
        l = self.lead("kreditbuddha", "900001")
        self.assertEqual(l["state"], "Delhi")

    def test_same_lead_in_two_portals_stays_two_rows(self):
        a = fx.make_csv(H, [fx.std21_row(1, lead_id=777)])
        self.ingest(kb_name(), a)
        data = fx.make_csv(H, [fx.std21_row(1, lead_id=777, medium="27P864CED981774")])
        out = self.ingest("CREDME_CLICKPE_ALL_LEADS_20261019_101500.csv", data)
        self.assertEqual(out["outcome"], "processed")
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads WHERE external_lead_id='777'")[0][0], 2)

    def test_creditsea_application_numbers_are_quarantined_not_matched(self):
        self.ingest(kb_name(), kb_csv([fx.std21_row(1)]))
        data = fx.make_csv(fx.CS7_HEADERS, [fx.cs7_row(1, appno="900001"), fx.cs7_row(2)])
        out = self.ingest("creditsea_disbursed_cases_2026-10-19-abcdef12.csv", data)
        self.assertEqual(out["outcome"], "processed", out)
        self.assertEqual(out["rows"]["quarantined"], 2)
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads WHERE portal_id=(SELECT portal_id FROM mis.reporting_portals WHERE portal_code='creditsea')")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM mis.ingestion_issues WHERE issue_type='unknown_lead_skipped'")[0][0], 2)
        self.assertEqual(self.lead("kreditbuddha", "900001")["status"], "rejected")        # same digits, other namespace: untouched

    def test_empty_files_and_header_only(self):
        out = self.ingest(kb_name(), b"")
        self.assertEqual(out["outcome"], "empty")
        out = self.ingest(kb_name("20261020"), kb_csv([]))
        self.assertEqual(out["outcome"], "empty")
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)

    def test_malformed_file_goes_to_review_not_retried(self):
        out = self.ingest(kb_name(), b"\x00\x01\x02garbage\xff")
        self.assertEqual(out["outcome"], "needs_review")
        self.assertTrue(out["reason"].startswith("file_unreadable"))

    def test_changed_headers_hold_file_as_needs_mapping(self):
        data = fx.make_csv(H + ["Brand New Column"], [fx.std21_row(1)])
        out = self.ingest(kb_name(), data)
        self.assertEqual(out["outcome"], "needs_mapping")
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events")[0][0], "needs_mapping")
        self.assertIn("brand new column", repr(out["detail"]))

    def test_unknown_filename_needs_review(self):
        out = self.ingest("mystery_export.csv", kb_csv([fx.std21_row(1)]))
        self.assertEqual(out["outcome"], "not_claimed")
        self.assertEqual(out["status"], "needs_review")
        self.assertEqual(out["reason"], "no_profile_matches_filename")

    def test_failed_transaction_leaves_no_partial_data_and_retry_succeeds(self):
        data = kb_csv([fx.std21_row(i) for i in range(1, 6)])
        real = ingest._insert_issues
        with mock.patch.object(ingest, "_insert_issues", side_effect=RuntimeError("boom 123456789")):
            out = self.ingest(kb_name(), data)
        self.assertEqual(out["outcome"], "failed")
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM mis.lead_history")[0][0], 0)
        st = self.q("SELECT status, attempt_count, last_error FROM mis.drive_file_events")[0]
        self.assertEqual(st[0], "failed")
        self.assertNotIn("123456789", st[2])
        self.assertEqual(self.q("SELECT processing_status FROM mis.source_files")[0][0], "failed")
        eid = self.q("SELECT event_id FROM mis.drive_file_events")[0][0]
        with self.db.connection() as c:
            cl = events.claim(c, eid, lease_minutes=30, max_attempts=3)
        ok = ingest.process_event(self.db, self.settings, eid, cl["claim_token"], data)
        self.assertEqual(ok["outcome"], "processed")
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 5)

    def test_event_dies_after_max_attempts(self):
        data = kb_csv([fx.std21_row(1)])
        f = self.drive_file(kb_name(), data)
        with self.db.connection() as c:
            eid = events.register(c, f, tz=self.settings.tz, range_policy="review")["event_id"]
        with mock.patch.object(ingest, "_load", side_effect=RuntimeError("x")):
            for _ in range(3):
                with self.db.connection() as c:
                    cl = events.claim(c, eid, lease_minutes=30, max_attempts=3)
                self.assertIsNotNone(cl)
                ingest.process_event(self.db, self.settings, eid, cl["claim_token"], data)
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events")[0][0], "dead")
        with self.db.connection() as c:
            self.assertIsNone(events.claim(c, eid, lease_minutes=30, max_attempts=3))

    def test_claim_is_exclusive_under_concurrency(self):
        f = self.drive_file(kb_name(), kb_csv([fx.std21_row(1)]))
        with self.db.connection() as c:
            eid = events.register(c, f, tz=self.settings.tz, range_policy="review")["event_id"]
        wins, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            with self.db.connection() as c:
                r = events.claim(c, eid, lease_minutes=30, max_attempts=3)
            if r:
                wins.append(r)
        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1)

    def test_expired_lease_can_be_reclaimed_and_old_token_is_rejected(self):
        data = kb_csv([fx.std21_row(1)])
        f = self.drive_file(kb_name(), data)
        with self.db.connection() as c:
            eid = events.register(c, f, tz=self.settings.tz, range_policy="review")["event_id"]
            first = events.claim(c, eid, lease_minutes=30, max_attempts=3)
            c.execute("UPDATE mis.drive_file_events SET lease_expires_at = now() - interval '1 minute' WHERE event_id=%s", (eid,))
            second = events.claim(c, eid, lease_minutes=30, max_attempts=3)
        self.assertIsNotNone(second)
        with self.assertRaises(events.LostClaim):
            ingest.process_event(self.db, self.settings, eid, first["claim_token"], data)
        self.assertEqual(ingest.process_event(self.db, self.settings, eid, second["claim_token"], data)["outcome"], "processed")

    def test_concurrent_identical_content_two_events_processed_once(self):
        data = kb_csv([fx.std21_row(i) for i in range(1, 30)])
        evs = []
        for _ in range(2):
            f = self.drive_file(kb_name(), data)
            with self.db.connection() as c:
                reg = events.register(c, f, tz=self.settings.tz, range_policy="review")
                evs.append(events.claim(c, reg["event_id"], lease_minutes=30, max_attempts=3))
        results, barrier = [], threading.Barrier(2)

        def run(cl):
            barrier.wait()
            results.append(ingest.process_event(self.db, self.settings, cl["event_id"], cl["claim_token"], data)["outcome"])
        ts = [threading.Thread(target=run, args=(c,)) for c in evs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["duplicate_content", "processed"])
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 29)

    def test_concurrent_different_files_same_leads_no_deadlock(self):
        a = kb_csv([fx.std21_row(i, status="SANCTION") for i in range(1, 40)])
        b = kb_csv([fx.std21_row(i, status="DISBURSED", **{"Disbursment Amount": "1000", "Disbursment Date": "2026-10-19"}) for i in reversed(range(1, 40))])
        evs = []
        for name, data in ((kb_name("20261018", "090000"), a), (kb_name("20261019", "090000"), b)):
            f = self.drive_file(name, data)
            with self.db.connection() as c:
                reg = events.register(c, f, tz=self.settings.tz, range_policy="review")
                evs.append((events.claim(c, reg["event_id"], lease_minutes=30, max_attempts=3), data))
        out, barrier = [], threading.Barrier(2)

        def run(cl, data):
            barrier.wait()
            out.append(ingest.process_event(self.db, self.settings, cl["event_id"], cl["claim_token"], data)["outcome"])
        ts = [threading.Thread(target=run, args=e) for e in evs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out, ["processed", "processed"])
        self.assertEqual(self.lead("kreditbuddha", "900001")["status"], "disbursed")      # newest wins whatever the order

    def test_equal_timestamp_conflict_is_quarantined_not_guessed(self):
        self.ingest("brightloans_leads_20261019-aaaaaaaa.csv", kb_csv([fx.std21_row(1, status="SANCTION")]))
        out = self.ingest("brightloans_leads_20261019-bbbbbbbb.csv", kb_csv([fx.std21_row(1, status="REJECT")]))
        self.assertEqual(out["outcome"], "processed")
        self.assertEqual(self.lead("brightloans", "900001")["status"], "approved")        # first stays; conflict not applied
        self.assertEqual(self.q("SELECT count(*) FROM mis.ingestion_issues WHERE issue_type='stale_update_skipped' AND details->>'reason'='ambiguous_order_tie'")[0][0], 1)

    def test_date_comes_from_filename_not_upload_time_ist_boundary(self):
        out = self.ingest("CREDME_CLICKPE_ALL_LEADS_20261019_235900.csv", fx.make_csv(H, [fx.std21_row(1, medium="27P864CED981774")]),
                          created="2026-10-19T19:30:00+00:00")         # = 20 Oct 01:00 IST upload
        rd, obs = self.q("SELECT report_date, observed_at AT TIME ZONE 'Asia/Kolkata' FROM mis.source_files")[0]
        self.assertEqual(str(rd), "2026-10-19")
        self.assertEqual(obs.strftime("%H:%M:%S"), "23:59:00")

    def test_footer_row_is_not_a_lead_and_totals_reconcile(self):
        rows = [fx.std21_row(1, status="DISBURSED", **{"Disbursment Amount": "25000", "Disbursment Date": "2026-10-19", "Payout": "100"})]
        out = self.ingest(kb_name(), kb_csv(rows, footer=fx.std21_footer(1, 25000, 0, 100)))
        self.assertEqual((out["rows"]["footer"], out["footer_mismatches"]), (1, []))
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 1)

    def test_no_customer_values_in_issues_or_errors(self):
        rows = [fx.std21_row(1, status="DISBURSED", **{"Disbursment Amount": "bad-amount-77", "Disbursment Date": "2026-10-19"})]
        self.ingest(kb_name(), kb_csv(rows))
        dump = repr(self.q("SELECT * FROM mis.ingestion_issues"))
        self.assertNotIn("bad-amount-77", dump)


class PartialReports(PgCase):
    """Disbursed-only report in the SAME identity namespace (spec submitted as data): enrich, never delete/downgrade."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from processor.guards import required_acceptances
        from processor.proposer import spec_from_dict
        from processor.schema_registry import approve_and_activate, insert_spec, load_spec_from_db
        c = connect(cls.url)
        pid = insert_spec(c, spec_from_dict(fx.PARTIAL_SPEC))
        approve_and_activate(c, pid, "test-approver", sorted(required_acceptances(load_spec_from_db(c, pid))))
        c.commit()
        c.close()

    def setUp(self):
        self.reset_data()

    def feed(self, rows, date="2026-10-19"):
        return self.ingest(f"kb_disbursed_{date}.csv", fx.make_csv(fx.PARTIAL_HEADERS, rows))

    def test_disbursed_only_enriches_listed_leads_and_never_touches_absent_ones(self):
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION", **{"Rejection Reason": ""}),
                                                          fx.std21_row(2, status="SANCTION")]))
        out = self.feed([fx.partial_row(900001, "30000")])
        self.assertEqual(out["outcome"], "processed", out)
        l1, l2 = self.lead("kreditbuddha", "900001"), self.lead("kreditbuddha", "900002")
        self.assertEqual((l1["status"], l1["disbursed"]), ("disbursed", Decimal("30000.00")))
        self.assertEqual(l2["status"], "approved")                       # absent from the report => unchanged
        self.assertIsNone(l2["disbursed"])

    def test_older_lead_export_cannot_downgrade_a_disbursed_lead(self):
        self.feed([fx.partial_row(900001, "30000")])
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION")]))
        l = self.lead("kreditbuddha", "900001")
        self.assertEqual((l["status"], l["disbursed"]), ("disbursed", Decimal("30000.00")))

    def test_partial_report_does_not_blank_existing_values(self):
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, status="SANCTION", State="Delhi")]))
        self.feed([fx.partial_row(900001)])
        l = self.lead("kreditbuddha", "900001")
        self.assertEqual((l["state"], l["raw"] is not None), ("Delhi", True))
        self.assertEqual(l["approved"], None)

    def test_newer_lead_export_can_still_move_a_lead_forward(self):
        self.feed([fx.partial_row(900001)], "2026-10-18")
        self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, status="DISBURSED", **{
            "Disbursment Amount": "31000", "Disbursment Date": "2026-10-19"})]))
        self.assertEqual(self.lead("kreditbuddha", "900001")["disbursed"], Decimal("31000.00"))

    def test_disbursed_only_for_unknown_lead_follows_profile_policy(self):
        out = self.feed([fx.partial_row(555)])
        self.assertEqual(out["rows"]["inserted"], 1)                     # this profile says on_unknown_lead=create


class Partners(PgCase):
    def setUp(self):
        self.reset_data()

    def test_register_partner_resolves_waiting_leads_and_keeps_zeros(self):
        from processor import partners
        self.ingest(kb_name(), kb_csv([fx.std21_row(1, code="000"), fx.std21_row(2, code="27P"), fx.std21_row(3, code="000")]))
        with self.db.connection() as c:
            self.assertEqual({d["code"]: d["leads"] for d in partners.unregistered_codes(c)}, {"000": 2, "27P": 1})
            r = partners.register_partner(c, "000", "Partner Zero")
        self.assertEqual((r["created"], r["leads_resolved"]), (True, 2))
        self.assertEqual(self.lead("kreditbuddha", "900001")["attr"], "resolved")
        self.assertEqual(self.lead("kreditbuddha", "900002")["attr"], "unregistered_code")
        with self.db.connection() as c:
            with self.assertRaises(ValueError):
                partners.register_partner(c, "00", "bad")

    def test_new_file_resolves_unregistered_lead_after_partner_registered(self):
        from processor import partners
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, code="27P")]))
        with self.db.connection() as c:
            partners.register_partner(c, "27P", "P27")
        self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, code="27P", status="SANCTION")]))
        self.assertEqual(self.lead("kreditbuddha", "900001")["attr"], "resolved")

    def test_attribution_is_set_once(self):
        with connect(self.url) as c:
            c.execute("INSERT INTO mis.attribution_partners (attribution_code, partner_name) VALUES ('AAA','A'),('BBB','B')")
            c.commit()
        self.ingest(kb_name("20261018", "100000"), kb_csv([fx.std21_row(1, code="AAA")]))
        self.ingest(kb_name("20261019", "100000"), kb_csv([fx.std21_row(1, code="BBB")]))
        self.assertEqual(self.lead("kreditbuddha", "900001")["code"], "AAA")
