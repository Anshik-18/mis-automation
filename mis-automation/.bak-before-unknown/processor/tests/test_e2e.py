"""End-to-end through the real HTTP API against a real PostgreSQL, playing the part of n8n and Google Drive.

A fake 'Drive' (a dict of files) is scanned in pages, registered, claimed, 'downloaded' and posted exactly as the
workflows do. Everything is synthetic data.
"""
import dataclasses
import hashlib
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import yaml

from processor.tests import fixtures as fx
from processor.tests.httpharness import KEY, Server
from processor.tests.pgharness import PgCase
from processor.tests.test_stage6 import GOOD, NEW_HEADERS, FakeLLM, new_rows

H = fx.STD21_HEADERS


def dj(fid, name, data, created):
    return {"id": fid, "name": name, "mimeType": "text/csv", "createdTime": created, "modifiedTime": created,
            "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(), "parents": ["F"]}


class FakeDrive:
    def __init__(self):
        self.files = {}          # id -> (meta, bytes)

    def put(self, fid, name, data, created="2026-10-19T14:00:00Z"):
        self.files[fid] = (dj(fid, name, data, created), data)

    def list(self, created_after, page_size=2):
        metas = sorted((m for m, _ in self.files.values() if m["createdTime"] > created_after), key=lambda m: m["createdTime"] + m["id"])
        return [metas[i:i + page_size] for i in range(0, len(metas), page_size)] or [[]]

    def download(self, fid):
        return self.files[fid][1]


class EndToEnd(PgCase):
    ISOLATED = True
    ACTIVATE = ("credme_leads_daily", "kreditbuddha_leads_daily", "brightloans_leads_daily", "salarymantra_leads_daily",
                "digicredit_leads", "kreditbuddha_leads_range", "paytrust_leads")
    SETTINGS = {"api_key": KEY}

    def setUp(self):
        mf = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"timezone": "Asia/Kolkata", "reports": [
            {"id": r, "portal": r.split("_")[0], "profile_key": r, "expect_by": "09:00"}
            for r in ("credme_leads_daily", "kreditbuddha_leads_daily", "brightloans_leads_daily", "salarymantra_leads_daily")]
            + [{"id": "digicredit", "portal": "digicredit", "profile_key": "digicredit_leads", "required": False},
               {"id": "newbank", "portal": "newbank", "profile_key": "newbank_leads", "required": False}]}, mf)
        mf.close()
        self.settings = dataclasses.replace(self.settings, manifest_path=mf.name, alias_dictionary_path=str(fx.SEED_DIR.parent / "column_aliases.yaml"))
        self.llm = FakeLLM(GOOD)
        self.srv = Server(self.settings, self.db, self.llm).__enter__()
        self.drive = FakeDrive()

    def tearDown(self):
        self.srv.__exit__()

    # -- what n8n's Discover + Process workflows do, step for step --------------------------------------
    def discover_and_process(self):
        s = self.srv
        w = s.call("GET", "/v1/discovery/window")[1]
        started = w["scan_started_at"]
        for page in self.drive.list(w["created_after"]):
            code, reg = s.call("POST", "/v1/discovery/register", {"files": page})
            self.assertEqual(code, 200, reg)
        s.call("POST", "/v1/discovery/complete", {"scan_started_at": started})
        outcomes = []
        for ev in s.call("GET", "/v1/events/pending")[1]["events"]:
            outcomes.append(self.process_event(ev))
        return outcomes

    def process_event(self, ev):
        s = self.srv
        code, cl = s.call("POST", f"/v1/events/{ev['event_id']}/claim")
        if code != 200:
            return {"outcome": "skipped_not_claimable"}
        data = self.drive.download(ev["drive_file_id"])
        code, out = s.upload(f"/v1/events/{ev['event_id']}/process", cl["claim_token"], ev["filename"], data)
        self.assertEqual(code, 200, out)
        out["filename"] = ev["filename"]
        if out["outcome"] == "needs_mapping":
            out["proposal"] = self.propose(ev["event_id"], data)
        return out

    def propose(self, eid, data, **fields):
        b = "----p"
        parts = []
        for k, v in {"event_id": eid, **fields}.items():
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts += [f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="f.csv"\r\n\r\n'.encode(), data, f"\r\n--{b}--\r\n".encode()]
        return self.srv.call("POST", "/v1/mapping/propose", raw=b"".join(parts), ctype=f"multipart/form-data; boundary={b}")

    def test_a_full_business_day(self):
        d = self.drive
        d.put("c1", "CREDME_CLICKPE_ALL_LEADS_20261019_060000.csv", fx.make_csv(H, [fx.std21_row(i, medium="000864CED981774") for i in range(1, 6)], footer=fx.std21_footer()), "2026-10-19T01:00:00Z")
        d.put("k1", "kreditbuddha_CLICKPE_leads_20261019_060000.csv", fx.make_csv(H, [fx.std21_row(i) for i in range(1, 8)]), "2026-10-19T01:00:00Z")
        d.put("b1", "brightloans_leads_20261019-aaaaaaaa.csv", fx.make_csv(H, [fx.std21_row(1, status="DISBURSED", **{"Disbursment Amount": "9000", "Disbursment Date": "2026-10-19"})]), "2026-10-19T01:30:00Z")
        d.put("d1", "digicredit_leads_20261019_20261019-bbbbbbbb.csv", fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(1)]), "2026-10-19T02:00:00Z")
        d.put("x1", "photo.png", b"\x89PNG", "2026-10-19T02:00:00Z")
        out = self.discover_and_process()
        by = {o.get("filename"): o["outcome"] for o in out}
        self.assertEqual(by["CREDME_CLICKPE_ALL_LEADS_20261019_060000.csv"], "processed")
        self.assertEqual(by["digicredit_leads_20261019_20261019-bbbbbbbb.csv"], "processed")
        self.assertNotIn("photo.png", by)                                       # unsupported extension is recorded as skipped
        self.assertEqual(self.q("SELECT status FROM mis.drive_file_events WHERE original_filename='photo.png'"), [("skipped",)])
        # reconcile: salarymantra never arrived
        code, rec = self.srv.call("POST", "/v1/reconcile/run", {"date": "2026-10-19", "lookback_days": 1})
        b = rec["batches"][0]
        self.assertEqual(b["status"] in ("incomplete", "open"), True)
        states = {i["id"]: i["state"] for i in b["items"]}
        self.assertEqual((states["credme_leads_daily"], states["salarymantra_leads_daily"]), ("success", states["salarymantra_leads_daily"]))
        self.assertIn(states["salarymantra_leads_daily"], ("missing", "not_due"))
        # the late file arrives; a second scan (overlapping window) finds nothing new and processes only it
        d.put("s1", "salarymantra_CLICKPE_leads_20261019-cccccccc.csv", fx.make_csv(H, [fx.std21_row(i) for i in range(1, 4)]), "2026-10-19T04:00:00Z")
        out2 = self.discover_and_process()
        self.assertEqual([o["outcome"] for o in out2], ["processed"])
        # re-running the whole scan again is a no-op (exact retries)
        self.assertEqual(self.discover_and_process(), [])
        total_leads = self.q("SELECT count(*) FROM mis.leads")[0][0]
        self.assertEqual(total_leads, 5 + 7 + 1 + 1 + 3)
        code, rec = self.srv.call("POST", "/v1/reconcile/run", {"date": "2026-10-19", "lookback_days": 1})
        self.assertEqual(rec["batches"][0]["status"], "complete")
        self.assertEqual(self.llm.calls, 0)                                     # known formats never touch the LLM

    def test_new_layout_is_held_then_approved_then_flows_without_llm(self):
        d = self.drive
        rows = new_rows(3)
        d.put("n1", "newbank_leads_20261019_080000.csv", fx.make_csv(NEW_HEADERS, rows), "2026-10-19T03:00:00Z")
        self.assertEqual(self.discover_and_process(), [])                       # registered as needs_review: nothing to claim
        eid, st, why = self.q("SELECT event_id, status, review_reason FROM mis.drive_file_events")[0]
        self.assertEqual((st, why), ("needs_review", "no_profile_matches_filename"))     # unknown name: nobody guesses a portal
        code, att = self.srv.call("POST", "/v1/events/attention")
        self.assertEqual((att["count"], "newbank_leads_20261019_080000.csv" in att["text"]), (1, True))
        self.assertEqual(self.srv.call("POST", "/v1/events/attention")[1]["count"], 0)    # announced once
        data = d.download("n1")
        code, prop = self.propose(eid, data, portal="newbank", create_portal="true", display_name="NewBank")
        self.assertEqual(code, 200, prop)
        self.assertEqual((prop["used_llm"], prop["open_questions"]), (True, []))
        self.assertEqual(self.q("SELECT count(*) FROM mis.leads")[0][0], 0)
        self.assertEqual(self.srv.call("POST", f"/v1/mapping/{prop['profile_id']}/approve", {})[0], 400)    # approver is mandatory
        code, appr = self.srv.call("POST", f"/v1/mapping/{prop['profile_id']}/approve", {"approved_by": "ops@example.com"})
        self.assertEqual((code, appr["requeued_events"]), (200, [eid]))
        out = self.discover_and_process()
        self.assertEqual((out[0]["outcome"], out[0]["rows"]["inserted"]), ("processed", 3))
        d.put("n2", "newbank_leads_20261020_080000.csv", fx.make_csv(NEW_HEADERS, new_rows(2, start=20)), "2026-10-20T03:00:00Z")
        self.assertEqual(self.discover_and_process()[0]["outcome"], "processed")
        self.assertEqual(self.llm.calls, 1)
        code, g = self.srv.call("GET", f"/v1/mapping/{prop['profile_id']}")
        self.assertEqual((g["status"], g["proposed_by"]), ("active", "ai"))
        self.assertNotIn("REF-ZQX", repr(g))

    def test_kreditbuddha_range_file_needs_a_person_then_loads(self):
        rng = fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(i) for i in range(1, 6)])
        self.drive.put("r1", "leads_20260930_20261030.csv", rng, "2026-10-19T14:00:00Z")
        out = self.discover_and_process()
        self.assertEqual(self.q("SELECT status, review_reason FROM mis.drive_file_events")[0], ("needs_review", "filename_date_issue:range_end_after_upload_day"))
        eid = self.q("SELECT event_id FROM mis.drive_file_events")[0][0]
        code, r = self.srv.call("POST", f"/v1/events/{eid}/resolve", {"portal": "kreditbuddha", "profile_key": "kreditbuddha_leads_range",
                                                                       "report_date": "2026-10-19", "period_start": "2026-09-30", "period_end": "2026-10-30",
                                                                       "resolved_by": "ops@example.com"})
        self.assertEqual((code, r["status"]), (200, "discovered"))
        out = self.discover_and_process()
        self.assertEqual((out[0]["outcome"], out[0]["rows"]["inserted"]), ("processed", 5))
        row = self.q("SELECT report_date, period_start, period_end FROM mis.source_files")[0]
        self.assertEqual([str(x) for x in row], ["2026-10-19", "2026-09-30", "2026-10-30"])

    def test_same_portal_two_report_types_in_one_day(self):
        self.drive.put("k1", "kreditbuddha_CLICKPE_leads_20261019_060000.csv", fx.make_csv(H, [fx.std21_row(1)]))
        self.drive.put("k2", "leads_20261019_20261019.csv", fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(7)]))
        out = self.discover_and_process()
        self.assertEqual(sorted(o["outcome"] for o in out), ["processed", "processed"])
        self.assertEqual(self.q("SELECT count(DISTINCT profile_id) FROM mis.source_files WHERE portal_id=(SELECT portal_id FROM mis.reporting_portals WHERE portal_code='kreditbuddha')")[0][0], 2)

    def test_downtime_recovery_window_reaches_back_to_cursor(self):
        s = self.srv
        s.call("POST", "/v1/discovery/complete", {"scan_started_at": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()})
        w = s.call("GET", "/v1/discovery/window")[1]
        self.assertEqual(w["mode"], "recovery")
        start = datetime.fromisoformat(w["created_after"].replace("Z", "+00:00"))
        self.assertLess(start, datetime.now(timezone.utc) - timedelta(days=8))

    def test_logs_and_responses_do_not_contain_row_values(self):
        import logging
        records = []

        class H_(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())
        h = H_()
        logging.getLogger().addHandler(h)
        try:
            self.drive.put("k1", "kreditbuddha_CLICKPE_leads_20261019_060000.csv",
                           fx.make_csv(H, [fx.std21_row(1, **{"Date Of Birth": "1977-07-07", "Pincode": "zzzzzz"})]))
            out = self.discover_and_process()
        finally:
            logging.getLogger().removeHandler(h)
        blob = repr(out) + " ".join(records)
        for secret in ("1977-07-07", "TESTPARTNER"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
