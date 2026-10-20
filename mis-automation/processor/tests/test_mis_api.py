"""/v1/mis/* read API: real PostgreSQL, real HTTP, synthetic data. Expected values are recomputed independently
from the raw mis.leads rows in Python, never through the code under test."""
import csv
import io
import json
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import urlencode

from processor import partners
from processor.database import connect
from processor.tests import fixtures as fx
from processor.tests.httpharness import KEY, Server
from processor.tests.pgharness import PgCase

H = fx.STD21_HEADERS
STATUSES = ["SYSTEM-REJECT", "SANCTION", "DISBURSED", "LEAD-INPROCESS", "DISBURSE-PENDING", "LEAD-REGISTRATION"]


def kb(date, time="060000"):
    return f"kreditbuddha_CLICKPE_leads_{date}_{time}.csv"


class MisApi(PgCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import dataclasses
        cls.settings = dataclasses.replace(cls.settings, api_key=KEY)
        t = cls("run")
        t.reset_data()
        with cls.db.connection() as c:
            partners.register_partner(c, "027", "Synthetic Partner 027")
            partners.register_partner(c, "27P", "Synthetic Partner 27P")
        rows = []
        for i in range(1, 121):
            code = ["027", "27P", "ZZ9", "027"][i % 4]
            status = STATUSES[i % len(STATUSES)]
            extra = {}
            if status == "DISBURSED":
                extra = {"Disbursment Date": "2026-10-18", "Disbursment Amount": "" if i % 12 == 2 else str(10000 + i), "Payout": str(100 + i)}
            rows.append(fx.std21_row(i, status=status, code=code, **{"Lead Entry Date": "2026-10-%02d" % (10 + i % 9), **extra}))
        rows[0]["Lead ID"] = "=SUM(1,1)"                                    # spreadsheet-formula-looking id
        rows[1]["City"] = 'Mum,bai "East"\nZone'
        out = t.ingest(kb("20261019"), fx.make_csv(H, rows))
        assert out["outcome"] == "processed", out
        out = t.ingest("brightloans_leads_20261018-aaaaaaaa.csv", fx.make_csv(H, [fx.std21_row(900 + i, status="DISBURSED", code="027", **{"Disbursment Amount": "5000", "Disbursment Date": "2026-10-17", "Lead Entry Date": "2026-10-16"}) for i in range(5)]), created="2026-10-18T14:00:00+00:00")
        cls.srv = Server(cls.settings, cls.db).__enter__()
        with connect(cls.url) as c:
            cls.raw = c.execute("""SELECT l.lead_id, l.external_lead_id, l.attribution_code, l.attribution_status, l.lead_status_code,
                 l.lead_entry_date, l.disbursed_date, sf.report_date, l.disbursed_amount, l.payout_amount, p.portal_code, l.state
                 FROM mis.leads l JOIN mis.reporting_portals p USING (portal_id)
                 LEFT JOIN mis.source_files sf ON sf.source_file_id = l.last_seen_file_id""").fetchall()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__(None, None, None)
        super().tearDownClass()

    def get(self, path, params=None, key=KEY):
        status, body = self.srv.call("GET", path + ("?" + urlencode(params, doseq=False) if params else ""), key=key)
        return status, body

    def raw_call(self, path, params):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}{path}?{urlencode(params)}", headers={"X-API-Key": KEY})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()

    def expected(self, **f):
        sel = []
        idx = {"entry": 5, "disbursement": 6, "report": 7}[f.get("dateField", "entry")]
        for r in self.raw:
            if f.get("code") and r[2] != f["code"]: continue
            if f.get("from") and (r[idx] is None or not (f["from"] <= r[idx].isoformat() <= f["to"])): continue
            if f.get("status") and r[4] not in f["status"].split(","): continue
            if f.get("portal") and r[10] not in f["portal"].split(","): continue
            if f.get("state") and (r[11] or "").lower() not in [x.lower() for x in f["state"].split(",")]: continue
            if f.get("attribution") and r[3] not in f["attribution"].split(","): continue
            sel.append(r)
        return sel

    # ---- tests ------------------------------------------------------------------------------------------
    def test_auth_required_and_contract_error_shape(self):
        self.assertEqual(self.get("/v1/mis/meta", key=None)[0], 401)
        self.assertEqual(self.get("/v1/mis/summary", key="wrong")[0], 401)
        code, body = self.get("/v1/mis/leads", {"code": "12", "page": "0", "sort": "x;drop table leads", "dir": "up", "pageSize": "7"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertGreaterEqual(len(body["error"]["details"]), 5)
        self.assertEqual(self.get("/v1/mis/summary", {"from": "2026-10-05", "to": "2026-10-01"})[0], 400)
        self.assertEqual(self.get("/v1/mis/summary", {"from": "2026-10-05"})[0], 400)
        self.assertEqual(self.get("/v1/mis/summary", {"dateField": "created"})[0], 400)
        self.assertEqual(self.get("/v1/mis/export", {"format": "pdf"})[0], 400)
        self.assertEqual(self.get("/v1/mis/partners/12")[0], 400)

    def test_meta(self):
        code, m = self.get("/v1/mis/meta")
        self.assertEqual(code, 200)
        self.assertEqual(m["mode"], "live")
        self.assertIn("disbursed", [s["code"] for s in m["statuses"]])
        self.assertIn("kreditbuddha", [p["code"] for p in m["portals"]])
        self.assertEqual(m["definitions"], {"approvedStatuses": ["approved", "disbursement_pending"],
                                            "disbursedStatuses": ["disbursed"], "rejectedStatuses": ["rejected"]})
        self.assertEqual(m["sync"]["dataThroughReportDate"], "2026-10-19")
        self.assertTrue(m["sync"]["lastLoadAt"].endswith("Z"))

    def test_summary_matches_independent_computation_for_many_filters(self):
        cases = [{}, {"code": "027"}, {"code": "27P", "from": "2026-10-10", "to": "2026-10-14"},
                 {"dateField": "disbursement", "from": "2026-10-01", "to": "2026-10-31"},
                 {"dateField": "report", "from": "2026-10-19", "to": "2026-10-19"},
                 {"status": "disbursed,rejected", "code": "027"}, {"portal": "brightloans"},
                 {"attribution": "unregistered_code"}, {"code": "QQQ"}]
        for c in cases:
            with self.subTest(c):
                exp = self.expected(**c)
                code, s = self.get("/v1/mis/summary", c)
                self.assertEqual(code, 200)
                disb = [r for r in exp if r[4] == "disbursed"]
                self.assertEqual(s["totalLeads"], len(exp))
                self.assertEqual(s["disbursedCount"], len(disb))
                amts = [r[8] for r in disb if r[8] is not None]
                self.assertEqual(s["disbursedAmount"], float(sum(amts)) if amts else None)
                self.assertEqual(s["disbursedMissingAmountCount"], len([r for r in disb if r[8] is None]))
                pays = [r[9] for r in exp if r[9] is not None]
                self.assertEqual(s["payout"], float(sum(pays)) if pays else None)
                self.assertEqual(s["approvedCount"], len([r for r in exp if r[4] in ("approved", "disbursement_pending")]))
                self.assertEqual(s["rejectedCount"], len([r for r in exp if r[4] == "rejected"]))
                self.assertEqual(s["conversionRatePct"], round(len(disb) / len(exp) * 100, 2) if exp else None)
        self.assertTrue(self.get("/v1/mis/summary", {})[1]["totalLeads"] > 100)

    def test_leads_pagination_sorting_filters_and_row_shape(self):
        exp = self.expected(code="027", dateField="entry", **{"from": "2026-10-01", "to": "2026-10-31"})
        q = {"code": "027", "from": "2026-10-01", "to": "2026-10-31", "pageSize": "10", "sort": "externalLeadId", "dir": "asc"}
        ids, page = [], 1
        while True:
            code, body = self.get("/v1/mis/leads", {**q, "page": str(page)})
            self.assertEqual(code, 200)
            self.assertEqual(body["total"], len(exp))
            if not body["items"]: break
            ids += [i["leadId"] for i in body["items"]]
            page += 1
        self.assertEqual(sorted(ids), sorted(str(r[0]) for r in exp))              # every lead exactly once
        self.assertEqual(len(set(ids)), len(ids))
        row = self.get("/v1/mis/leads", {**q, "page": "1", "sort": "leadEntryDate", "dir": "desc"})[1]["items"][0]
        for k in ["leadId", "externalLeadId", "idNamespace", "attributionCode", "attributionStatus", "partnerName", "portalCode",
                  "portalName", "status", "statusRaw", "statusReason", "leadEntryDate", "disbursedDate", "reportDate",
                  "requestedLoanAmount", "approvedAmount", "disbursedAmount", "payoutAmount", "state", "city",
                  "employmentType", "leadSource", "lastObservedAt"]:
            self.assertIn(k, row)
        self.assertEqual((row["attributionCode"], row["partnerName"], row["attributionStatus"]), ("027", "Synthetic Partner 027", "resolved"))
        self.assertRegex(row["leadEntryDate"], r"^2026-10-\d\d$")
        self.assertTrue(row["lastObservedAt"].endswith("Z"))
        # leading zero is text: '27' must not find '027'
        self.assertEqual(self.get("/v1/mis/leads", {"code": "27"})[0], 400)
        unreg = self.get("/v1/mis/leads", {"code": "ZZ9", "pageSize": "100"})[1]
        self.assertTrue(unreg["total"] > 0 and all(i["attributionStatus"] == "unregistered_code" and i["partnerName"] is None for i in unreg["items"]))
        # sorting: amounts ascending, NULLs last, deterministic tie-break
        for d in ("asc", "desc"):
            items = self.get("/v1/mis/leads", {"sort": "disbursedAmount", "dir": d, "pageSize": "100"})[1]["items"]
            vals = [i["disbursedAmount"] for i in items]
            known = [v for v in vals if v is not None]
            self.assertEqual(known, sorted(known, reverse=(d == "desc")))
            first_null = vals.index(None) if None in vals else len(vals)
            self.assertTrue(all(v is None for v in vals[first_null:]))
        a = self.get("/v1/mis/leads", {"sort": "status", "pageSize": "50"})[1]["items"]
        b = self.get("/v1/mis/leads", {"sort": "status", "pageSize": "50"})[1]["items"]
        self.assertEqual([i["leadId"] for i in a], [i["leadId"] for i in b])

    def test_date_basis_is_never_mixed(self):
        win = {"from": "2026-10-17", "to": "2026-10-18"}
        e = self.get("/v1/mis/summary", {**win, "dateField": "entry"})[1]["totalLeads"]
        d = self.get("/v1/mis/summary", {**win, "dateField": "disbursement"})[1]["totalLeads"]
        r = self.get("/v1/mis/summary", {**win, "dateField": "report"})[1]["totalLeads"]
        self.assertEqual(e, len(self.expected(dateField="entry", **win)))
        self.assertEqual(d, len(self.expected(dateField="disbursement", **win)))
        self.assertEqual(r, len(self.expected(dateField="report", **win)))
        items = self.get("/v1/mis/leads", {**win, "dateField": "disbursement", "pageSize": "100"})[1]["items"]
        self.assertTrue(items and all(i["disbursedDate"] in ("2026-10-17", "2026-10-18") for i in items))

    def test_state_filter_and_meta_states(self):
        with connect(self.url) as c:
            c.execute("UPDATE mis.leads SET state = CASE WHEN lead_id % 3 = 0 THEN 'Tamil Nadu' WHEN lead_id % 3 = 1 THEN 'Kerala' ELSE state END")
            c.commit()
        with connect(self.url) as c:
            type(self).raw = c.execute("""SELECT l.lead_id, l.external_lead_id, l.attribution_code, l.attribution_status, l.lead_status_code,
                 l.lead_entry_date, l.disbursed_date, sf.report_date, l.disbursed_amount, l.payout_amount, p.portal_code, l.state
                 FROM mis.leads l JOIN mis.reporting_portals p USING (portal_id)
                 LEFT JOIN mis.source_files sf ON sf.source_file_id = l.last_seen_file_id""").fetchall()
        m = self.get("/v1/mis/meta")[1]
        self.assertEqual(m["states"], sorted(m["states"]))
        self.assertIn("Tamil Nadu", m["states"])
        self.assertIn("Kerala", m["states"])
        for st in ("Tamil Nadu", "Kerala,Tamil Nadu", "tamil nadu", "Atlantis"):
            with self.subTest(st):
                exp = self.expected(state=st)
                code, body = self.get("/v1/mis/leads", {"state": st, "pageSize": "100"})
                self.assertEqual((code, body["total"]), (200, len(exp)))
                self.assertTrue(all(i["state"].lower() in [x.lower() for x in st.split(",")] for i in body["items"]))
                self.assertEqual(self.get("/v1/mis/summary", {"state": st})[1]["totalLeads"], len(exp))
        combo = self.get("/v1/mis/summary", {"state": "Kerala", "code": "027", "status": "disbursed"})[1]
        self.assertEqual(combo["totalLeads"], len(self.expected(state="Kerala", code="027", status="disbursed")))
        self.assertEqual(self.get("/v1/mis/leads", {"state": "<script>"})[0], 400)
        self.assertEqual(self.get("/v1/mis/leads", {"state": "x'; DROP TABLE mis.leads;--"})[0], 400)
        exp = self.expected(state="Tamil Nadu")
        _, headers, body = self.raw_call("/v1/mis/export", {"state": "Tamil Nadu", "format": "csv"})
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
        self.assertEqual((int(headers["x-export-row-count"]), len(rows) - 1), (len(exp), len(exp)))
        self.assertEqual({r[16] for r in rows[1:]}, {"Tamil Nadu"})
        import openpyxl
        _, _, xb = self.raw_call("/v1/mis/export", {"state": "Tamil Nadu,Kerala", "format": "xlsx"})
        wb = openpyxl.load_workbook(io.BytesIO(xb))
        kv = {str(r[0].value): r[1].value for r in wb["Summary"].iter_rows() if r[0].value and len(r) > 1}
        self.assertEqual(kv["State"], "Tamil Nadu, Kerala")

    def test_partner_lookup(self):
        self.assertEqual(self.get("/v1/mis/partners/027")[1], {"code": "027", "state": "resolved", "partnerName": "Synthetic Partner 027"})
        self.assertEqual(self.get("/v1/mis/partners/27p")[1]["state"], "resolved")
        self.assertEqual(self.get("/v1/mis/partners/zz9")[1], {"code": "ZZ9", "state": "unregistered", "partnerName": None})
        self.assertEqual(self.get("/v1/mis/partners/QQQ")[1]["state"], "unknown")

    def test_csv_export_streams_all_rows_with_filters_and_safe_cells(self):
        f = {"code": "027", "from": "2026-10-01", "to": "2026-10-31", "sort": "externalLeadId", "dir": "asc", "pageSize": "10"}
        exp = self.expected(code="027", **{"from": "2026-10-01", "to": "2026-10-31"})
        status, headers, body = self.raw_call("/v1/mis/export", {**f, "format": "csv"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-export-row-count"], str(len(exp)))
        self.assertIn("lead-mis_027_entry_2026-10-01_2026-10-31.csv", headers["content-disposition"])
        self.assertEqual(body[:3], b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
        self.assertEqual(len(rows) - 1, len(exp))                                  # all rows, not one page (pageSize was 10)
        self.assertEqual({len(r) for r in rows}, {21})
        self.assertEqual({r[2] for r in rows[1:]}, {"027"})
        self.assertEqual(rows[0][:3], ["Lead ID", "ID Namespace", "Attribution Code"])
        # injection guard + quoting round trip across ALL leads
        _, _, allbody = self.raw_call("/v1/mis/export", {"format": "csv", "sort": "externalLeadId", "dir": "asc"})
        allrows = list(csv.reader(io.StringIO(allbody.decode("utf-8-sig"), newline="")))
        self.assertIn("'=SUM(1,1)", [r[0] for r in allrows[1:]])
        self.assertIn('Mum,Bai "East" Zone', [r[17] for r in allrows[1:]])      # (ingest normalises whitespace/case); comma + quote round-trip
        self.assertEqual(len(allrows) - 1, len(self.raw))

    def test_xlsx_export_summary_and_leads_sheets(self):
        import openpyxl
        f = {"code": "027", "dateField": "entry", "from": "2026-10-01", "to": "2026-10-31", "status": "disbursed,rejected", "sort": "externalLeadId", "dir": "asc"}
        exp = self.expected(code="027", status="disbursed,rejected", **{"from": "2026-10-01", "to": "2026-10-31"})
        status, headers, body = self.raw_call("/v1/mis/export", {**f, "format": "xlsx"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-export-row-count"], str(len(exp)))
        wb = openpyxl.load_workbook(io.BytesIO(body))
        self.assertEqual(wb.sheetnames, ["Summary", "Leads"])
        kv = {str(r[0].value): r[1].value for r in wb["Summary"].iter_rows() if r[0].value and len(r) > 1}
        self.assertEqual((kv["Attribution code"], kv["Partner"], kv["Date basis"]), ("027", "Synthetic Partner 027", "Lead Entry Date"))
        self.assertEqual((kv["Date from"], kv["Date to"], kv["Lead status"]), ("2026-10-01", "2026-10-31", "disbursed, rejected"))
        self.assertEqual(kv["Rows in Leads sheet"], len(exp))
        s = self.get("/v1/mis/summary", {k: v for k, v in f.items() if k not in ("sort", "dir")})[1]
        self.assertEqual((kv["Total leads"], kv["Total disbursed count"]), (s["totalLeads"], s["disbursedCount"]))
        leads = list(wb["Leads"].iter_rows(values_only=True))
        self.assertEqual(len(leads) - 1, len(exp))
        self.assertEqual({r[2] for r in leads[1:]}, {"027"})
        import datetime
        self.assertTrue(all(isinstance(r[9], datetime.datetime) for r in leads[1:]))      # real dates
        # all leads: formula-looking id stays TEXT
        _, _, allb = self.raw_call("/v1/mis/export", {"format": "xlsx"})
        wb2 = openpyxl.load_workbook(io.BytesIO(allb))
        cells = [c for c in wb2["Leads"]["A"] if c.value == "=SUM(1,1)"]
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].data_type, "s")
        self.assertEqual(wb2["Leads"].max_row - 1, len(self.raw))

    def test_no_customer_free_text_in_any_response(self):
        blob = json.dumps(self.get("/v1/mis/leads", {"pageSize": "100"})[1]) + self.raw_call("/v1/mis/export", {"format": "csv"})[2].decode("utf-8-sig")
        for needle in ("Date Of Birth", "1990-01-01", "TESTPARTNER", "Pincode", "110001"):
            self.assertNotIn(needle, blob)

    def test_database_errors_do_not_leak(self):
        import dataclasses
        from processor.database import Database
        bad = Database(self.url.replace("dbname=", "dbname=nonexistent_"), use_pool=False)
        with Server(dataclasses.replace(self.settings, database_url=bad.url), bad) as srv:
            code, body = srv.call("GET", "/v1/mis/meta")
        self.assertEqual(code, 500)
        self.assertNotIn("nonexistent_", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
