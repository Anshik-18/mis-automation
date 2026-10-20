import unittest
from datetime import datetime, timedelta, timezone

from processor import discovery
from processor.tests import fixtures as fx
from processor.tests.httpharness import KEY, Server
from processor.tests.pgharness import PgCase

H = fx.STD21_HEADERS


def drive_json(fid, name, data, created="2026-10-19T14:00:00Z"):
    import hashlib
    return {"id": fid, "name": name, "mimeType": "text/csv", "createdTime": created, "modifiedTime": created,
            "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(), "parents": ["folderA"]}


class WindowMath(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)

    def test_initial_lookback(self):
        w = discovery.compute_window(self.NOW, None, 3, 6)
        self.assertEqual((w["mode"], w["start"]), ("initial_lookback", self.NOW - timedelta(days=3)))

    def test_recent_cursor_uses_rolling_lookback(self):
        w = discovery.compute_window(self.NOW, self.NOW - timedelta(hours=2), 3, 6)
        self.assertEqual((w["mode"], w["start"]), ("rolling_lookback", self.NOW - timedelta(days=3)))

    def test_outage_longer_than_lookback_reaches_back_to_cursor_minus_overlap(self):
        cur = self.NOW - timedelta(days=9)
        w = discovery.compute_window(self.NOW, cur, 3, 6)
        self.assertEqual((w["mode"], w["start"]), ("recovery", cur - timedelta(hours=6)))


class Api(PgCase):
    SETTINGS = {"api_key": KEY, "propose_unknown_files": False}       # this class exercises the manual review/resolve path

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = Server(cls.settings, cls.db).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()
        super().tearDownClass()

    def setUp(self):
        self.reset_data()

    def test_auth_required_everywhere_but_health(self):
        s = self.srv
        self.assertEqual(s.call("GET", "/health", key=None)[0], 200)
        for method, path in (("GET", "/v1/status"), ("GET", "/v1/discovery/window"), ("GET", "/v1/events/pending"),
                             ("POST", "/v1/discovery/register"), ("POST", "/v1/events/1/claim"),
                             ("POST", "/v1/partners"), ("POST", "/v1/events/replay")):
            self.assertEqual(s.call(method, path, {} if method == "POST" else None, key=None)[0], 401, path)
            self.assertEqual(s.call(method, path, {} if method == "POST" else None, key="wrong")[0], 401, path)
        code, _ = s.call("GET", "/v1/status", key=None, headers={"Authorization": "Bearer " + KEY})
        self.assertEqual(code, 200)

    def test_full_http_flow_register_pending_claim_process(self):
        s = self.srv
        data = fx.make_csv(H, [fx.std21_row(1, code="000"), fx.std21_row(2)])
        name = "kreditbuddha_CLICKPE_leads_20261019_101500.csv"
        code, w = s.call("GET", "/v1/discovery/window")
        self.assertEqual((code, w["mode"]), (200, "initial_lookback"))
        code, reg = s.call("POST", "/v1/discovery/register", {"files": [drive_json("d1", name, data)]})
        self.assertEqual((code, reg["summary"]), (200, {"discovered": 1}))
        code, again = s.call("POST", "/v1/discovery/register", {"files": [drive_json("d1", name, data)]})
        self.assertFalse(again["results"][0]["is_new"])
        code, pend = s.call("GET", "/v1/events/pending")
        eid = pend["events"][0]["event_id"]
        code, cl = s.call("POST", f"/v1/events/{eid}/claim")
        self.assertEqual(code, 200)
        self.assertEqual(s.call("POST", f"/v1/events/{eid}/claim")[0], 409)                  # exclusive
        self.assertEqual(s.upload(f"/v1/events/{eid}/process", "00000000-0000-0000-0000-000000000000", name, data)[0], 409)
        code, out = s.upload(f"/v1/events/{eid}/process", cl["claim_token"], name, data)
        self.assertEqual((code, out["outcome"], out["rows"]["inserted"]), (200, "processed", 2))
        self.assertEqual(s.call("GET", "/v1/events/pending")[1]["count"], 0)
        code, done = s.call("POST", "/v1/discovery/complete", {"scan_started_at": "2026-10-20T06:00:00+00:00"})
        self.assertEqual(code, 200)
        later = s.call("POST", "/v1/discovery/complete", {"scan_started_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})[1]
        older = s.call("POST", "/v1/discovery/complete", {"scan_started_at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()})[1]
        self.assertEqual(older["cursor"], later["cursor"])                                  # never moves backwards
        self.assertEqual(s.call("GET", "/v1/discovery/window")[1]["mode"], "rolling_lookback")
        self.reset_data()
        s.call("POST", "/v1/discovery/complete", {"scan_started_at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()})
        self.assertEqual(s.call("GET", "/v1/discovery/window")[1]["mode"], "recovery")      # long outage => reach back

    def test_failed_download_marks_failed_then_dead(self):
        s = self.srv
        data = fx.make_csv(H, [fx.std21_row(1)])
        s.call("POST", "/v1/discovery/register", {"files": [drive_json("d2", "kreditbuddha_CLICKPE_leads_20261019_101500.csv", data)]})
        eid = s.call("GET", "/v1/events/pending")[1]["events"][0]["event_id"]
        for i in range(3):
            tok = s.call("POST", f"/v1/events/{eid}/claim")[1]["claim_token"]
            code, st = s.call("POST", f"/v1/events/{eid}/fail", {"claim_token": tok, "error": "HTTP 503 from Drive"})
        self.assertEqual(st["status"], "dead")
        self.assertEqual(s.call("POST", f"/v1/events/{eid}/claim")[0], 409)

    def test_review_resolve_and_replay_endpoints(self):
        s = self.srv
        data = fx.make_csv(H, [fx.std21_row(1)])
        reg = s.call("POST", "/v1/discovery/register", {"files": [drive_json("d3", "weird_name.csv", data)]})[1]["results"][0]
        self.assertEqual((reg["status"], reg["reason"]), ("needs_review", "no_profile_matches_filename"))
        code, r = s.call("POST", f"/v1/events/{reg['event_id']}/resolve",
                         {"portal": "kreditbuddha", "profile_key": "kreditbuddha_leads_daily", "report_date": "2026-10-19", "resolved_by": "ops@example.com"})
        self.assertEqual((code, r["status"]), (200, "discovered"))
        tok = s.call("POST", f"/v1/events/{reg['event_id']}/claim")[1]["claim_token"]
        out = s.upload(f"/v1/events/{reg['event_id']}/process", tok, "weird_name.csv", data)[1]
        self.assertEqual(out["outcome"], "processed")
        self.assertEqual(s.call("POST", "/v1/events/replay", {"event_ids": [reg["event_id"]]})[0], 400)     # reason required

    def test_partner_endpoints(self):
        s = self.srv
        code, r = s.call("POST", "/v1/partners", {"code": "27p", "name": "Partner 27P"})
        self.assertEqual((code, r["code"]), (200, "27P"))
        self.assertEqual(s.call("POST", "/v1/partners", {"code": "TOOLONG", "name": "x"})[0], 400)

    def test_bad_inputs_are_400_not_500_and_do_not_echo_data(self):
        s = self.srv
        self.assertEqual(s.call("POST", "/v1/discovery/register", {"files": "nope"})[0], 400)
        self.assertEqual(s.call("POST", "/v1/discovery/register", raw=b"not json")[0], 400)
        code, r = s.call("POST", "/v1/discovery/register", {"files": [{"id": "x"}]})
        self.assertEqual(r["results"][0]["status"], "invalid_metadata")

    def test_upload_over_limit_rejected(self):
        s = self.srv
        s.app.state.settings  # noqa
        big = b"a" * (2 * 1024 * 1024)
        import dataclasses
        object.__setattr__(s.app.state.settings, "max_upload_mb", 1)
        try:
            code, r = s.upload("/v1/events/1/process", "x", "f.csv", big)
            self.assertEqual(code, 413)
        finally:
            object.__setattr__(s.app.state.settings, "max_upload_mb", 100)


if __name__ == "__main__":
    unittest.main()


class Attention(PgCase):
    def setUp(self):
        self.reset_data()

    def test_announced_once_per_state(self):
        from processor import events
        with self.db.connection() as c:
            r = events.register(c, self.drive_file("mystery.csv", b"x"), tz=self.settings.tz, range_policy="review")
            first = events.attention(c)
            second = events.attention(c)
        self.assertEqual([e["filename"] for e in first], ["mystery.csv"])
        self.assertEqual(second, [])
        self.execute("UPDATE mis.drive_file_events SET status='dead' WHERE event_id=%s", (r["event_id"],))
        with self.db.connection() as c:
            self.assertEqual([e["status"] for e in events.attention(c)], ["dead"])      # new state => news

    def test_mapping_proposals_are_not_double_announced(self):
        from processor import events
        with self.db.connection() as c:
            r = events.register(c, self.drive_file("kreditbuddha_CLICKPE_leads_20261019_060000.csv", b"x"), tz=self.settings.tz, range_policy="review")
        self.execute("UPDATE mis.drive_file_events SET status='needs_mapping', review_reason='awaiting_mapping_approval' WHERE event_id=%s", (r["event_id"],))
        with self.db.connection() as c:
            self.assertEqual(events.attention(c), [])
