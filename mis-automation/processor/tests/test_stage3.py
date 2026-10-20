import os
import re
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from processor.attribution import AttributionRule, extract
from processor.file_parser import FileFormatError, read_table
from processor.normalization import (apply_transform, header_signature, parse_date, parse_decimal, parse_yes_no,
                                     redact_digit_runs)
from processor.pipeline import transform_table
from processor.schema_registry import (check_compat, load_seed_specs, select_profile, validate_spec)
from processor.tests import fixtures as fx

IMPLIES = {"disbursed"}
SPECS = {s.profile_key: s for s in load_seed_specs(fx.SEED_DIR)}
D = date(2026, 10, 20)


def run(key, data, **kw):
    t = read_table(data)
    return transform_table(t, SPECS[key], report_date=D, implies_disbursement=IMPLIES, **kw)


class SeedProfiles(unittest.TestCase):
    def test_all_seed_specs_valid(self):
        for k, s in SPECS.items():
            self.assertEqual(validate_spec(s), [], k)

    def test_layouts_share_signature_across_portals(self):
        self.assertEqual(SPECS["credme_leads_daily"].signature, SPECS["salarymantra_leads_daily"].signature)
        self.assertEqual(SPECS["digicredit_leads"].signature, SPECS["kreditbuddha_leads_range"].signature)
        self.assertNotEqual(SPECS["credme_leads_daily"].signature, SPECS["digicredit_leads"].signature)

    def test_filename_regexes_compile_python_syntax(self):
        for s in SPECS.values():
            re.compile(s.filename_regex)


class Normalization(unittest.TestCase):
    def test_money(self):
        self.assertEqual(parse_decimal("₹ 1,23,456.5"), Decimal("123456.50"))
        self.assertEqual(parse_decimal("(500)"), Decimal("-500.00"))
        self.assertIsNone(parse_decimal(""))
        with self.assertRaises(ValueError):
            parse_decimal("12abc")

    def test_dates(self):
        self.assertEqual(parse_date("19 Oct 2026", "DD Mon YYYY"), date(2026, 10, 19))
        self.assertEqual(parse_date("1 Sept 2026", "DD Mon YYYY"), date(2026, 9, 1))
        self.assertEqual(parse_date("2026-10-19 10:15:00", "YYYY-MM-DD HH24:MI:SS"), date(2026, 10, 19))
        self.assertEqual(parse_date("2026-10-19", "YYYY-MM-DD"), date(2026, 10, 19))
        with self.assertRaises(ValueError):
            parse_date("31 Feb 2026", "DD Mon YYYY")

    def test_bool_and_redaction(self):
        self.assertIs(parse_yes_no("Yes"), True)
        self.assertIs(parse_yes_no("no"), False)
        self.assertIn("[redacted]", redact_digit_runs("paid ref 123456789012 ok"))
        self.assertEqual(redact_digit_runs("code 12345"), "code 12345")

    def test_header_signature_order_and_case_insensitive(self):
        a = header_signature(["Lead ID", "Status"])
        b = header_signature([" status ", "LEAD  id"])
        self.assertEqual(a, b)


class Attribution(unittest.TestCase):
    R_CAMPAIGN = [AttributionRule(1, "utm_campaign", ("CLICKPE",), organic_values=("ORGANIC",))]

    def test_leading_zeros_preserved(self):
        r = extract({"utm_campaign": "CLICKPE000097DB170ED9A"}, self.R_CAMPAIGN)
        self.assertEqual((r.status, r.code), ("code", "000"))

    def test_alphanumeric_code(self):
        r = extract({"utm_campaign": "CLICKPE27P97DB170ED9A1"}, self.R_CAMPAIGN)
        self.assertEqual(r.code, "27P")

    def test_case_insensitive_prefix_lower_case_code(self):
        rules = [AttributionRule(1, "utm_campaign", ("clickpe",), organic_values=("existing",))]
        r = extract({"utm_campaign": "clickpe27p97db170ed9a1"}, rules)
        self.assertEqual(r.code, "27P")

    def test_medium_vs_campaign_selected_by_rule(self):
        utm = {"utm_medium": "000864CED981774", "utm_campaign": "ORGANIC"}
        by_medium = extract(utm, [AttributionRule(1, "utm_medium")])
        by_campaign = extract(utm, [AttributionRule(1, "utm_campaign", organic_values=("ORGANIC",))])
        self.assertEqual(by_medium.code, "000")
        self.assertEqual(by_campaign.status, "organic")

    def test_unknown_shapes_are_unresolved_never_guessed(self):
        for v in ("CLICKPEKAMSAL12345", "17KCRsomething", "app", "APP", "KF_123", "CLICKPE12", ""):
            r = extract({"utm_campaign": v}, self.R_CAMPAIGN)
            self.assertEqual(r.status, "unresolved", v)
            self.assertIsNone(r.code)

    def test_prefix_required_when_configured(self):
        r = extract({"utm_campaign": "ABC864CED981774"}, self.R_CAMPAIGN)
        self.assertEqual(r.status, "unresolved")
        self.assertEqual(r.reason, "prefix_missing")

    def test_empty_prefix_allows_none(self):
        rules = [AttributionRule(1, "utm_campaign", ("CLICKPE", ""))]
        self.assertEqual(extract({"utm_campaign": "ABC864CED981774"}, rules).code, "ABC")

    def test_priority_fallback(self):
        rules = [AttributionRule(1, "utm_campaign"), AttributionRule(2, "utm_medium")]
        r = extract({"utm_campaign": "", "utm_medium": "27P864CED981774"}, rules)
        self.assertEqual(r.code, "27P")

    def test_non_hex_tail_rejected(self):
        self.assertEqual(extract({"utm_campaign": "CLICKPEABC86ZZED981774"}, self.R_CAMPAIGN).status, "unresolved")


class Pipeline(unittest.TestCase):
    def test_std21_basic_footer_and_reconcile(self):
        rows = [std21_ok(1), std21_ok(2, status="DISBURSED", **{"Disbursment Amount": "25000",
                                                                 "Disbursment Date": "2026-10-19", "Payout": "1250"})]
        data = fx.make_csv(fx.STD21_HEADERS, rows, footer=fx.std21_footer(1, 25000, 0, 1250))
        r = run("kreditbuddha_leads_daily", data)
        self.assertEqual((len(r.records), r.rows_footer, r.rows_quarantined), (2, 1, 0))
        self.assertEqual(r.footer_mismatches, [])
        self.assertEqual(r.records[1].fields["disbursed_amount"], Decimal("25000.00"))

    def test_footer_mismatch_detected(self):
        rows = [std21_ok(1, status="DISBURSED", **{"Disbursment Amount": "25000", "Disbursment Date": "2026-10-19"})]
        data = fx.make_csv(fx.STD21_HEADERS, rows, footer=fx.std21_footer(1, 99999, 0, 0))
        self.assertIn("disbursed_amount", run("kreditbuddha_leads_daily", data).footer_mismatches)

    def test_disbursement_values_on_non_disbursed_row_are_parked_not_counted(self):
        rows = [std21_ok(1, status="SANCTION", **{"Disbursment Amount": "5000", "Disbursment Date": "2026-10-19"})]
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, rows))
        rec = r.records[0]
        self.assertIsNone(rec.fields["disbursed_amount"])
        self.assertIsNone(rec.fields["disbursed_date"])
        self.assertEqual(rec.source_attributes["unconfirmed_disbursement_amount"], "5000.00")

    def test_column_reordering_and_aliases_by_header(self):
        headers = list(reversed(fx.STD21_HEADERS))
        data = fx.make_csv(headers, [std21_ok(1)])
        t = read_table(data)
        self.assertEqual(check_compat(SPECS["kreditbuddha_leads_daily"], t.header_keys).level, "exact")
        r = run("kreditbuddha_leads_daily", data)
        self.assertEqual(r.records[0].attribution_code, "ABC")

    def test_delimiter_and_encoding_variants(self):
        rows = [std21_ok(1)]
        for kw in ({"delimiter": ";"}, {"delimiter": "\t"}, {"bom": True}, {"encoding": "utf-16"}):
            r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, rows, **kw))
            self.assertEqual(len(r.records), 1, kw)

    def test_utm_medium_profile_vs_campaign_profile(self):
        row = std21_ok(1, medium="27P864CED981774", campaign="ORGANIC")
        data = fx.make_csv(fx.STD21_HEADERS, [row])
        self.assertEqual(run("credme_leads_daily", data).records[0].attribution_code, "27P")
        self.assertEqual(run("kreditbuddha_leads_daily", data).records[0].attribution_status, "organic")

    def test_unresolved_and_original_utm_kept(self):
        row = std21_ok(1, campaign="CLICKPEKAMSAL555")
        rec = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, [row])).records[0]
        self.assertEqual(rec.attribution_status, "unresolved")
        self.assertEqual(rec.utm["utm_campaign"], "CLICKPEKAMSAL555")

    def test_blank_rows_and_missing_identity(self):
        rows = [std21_ok(1), {h: "" for h in fx.STD21_HEADERS}, std21_ok(2, lead_id="")]
        rows[2]["Lead ID"] = ""
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, rows))
        self.assertEqual((len(r.records), r.rows_blank, r.rows_quarantined), (1, 1, 1))
        self.assertEqual(r.issues[0].issue_type, "missing_external_id")

    def test_bad_money_quarantines_row_without_echoing_value(self):
        rows = [std21_ok(1, status="DISBURSED", **{"Disbursment Amount": "12x4", "Disbursment Date": "2026-10-19"})]
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, rows))
        self.assertEqual((len(r.records), r.rows_quarantined), (0, 1))
        self.assertNotIn("12x4", repr(r.issues))

    def test_duplicate_rows_identical_collapsed_conflicting_quarantined(self):
        a = std21_ok(1)
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, [a, dict(a)]))
        self.assertEqual((len(r.records), r.rows_duplicate_collapsed), (1, 1))
        b = dict(a, **{"Lead Status": "SANCTION"})
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, [a, b]))
        self.assertEqual((len(r.records), r.rows_quarantined), (0, 2))

    def test_unmapped_status_flagged_not_guessed(self):
        r = run("kreditbuddha_leads_daily", fx.make_csv(fx.STD21_HEADERS, [std21_ok(1, status="WEIRD-NEW")]))
        self.assertEqual(r.records[0].lead_status_code, "unmapped")
        self.assertEqual(r.unmapped_statuses, {"WEIRD-NEW": 1})

    def test_dc18_dates_and_amounts_and_loan_amt_not_disbursed(self):
        rows = [fx.dc18_row(1, status="Approved", **{"Loan Amt": "70000", "Approved Amt": "60000"}),
                fx.dc18_row(2, status="Disbursed", **{"Disbursed Amt": "55000", "Disbursement Date": "19 Oct 2026"})]
        r = run("digicredit_leads", fx.make_csv(fx.DC18_HEADERS, rows))
        a, b = r.records
        self.assertEqual(a.fields["requested_loan_amount"], Decimal("70000.00"))
        self.assertEqual(a.fields["approved_amount"], Decimal("60000.00"))
        self.assertIsNone(a.fields["disbursed_amount"])
        self.assertEqual(b.fields["disbursed_date"], date(2026, 10, 19))
        self.assertEqual(a.attribution_code, "ABC")
        self.assertEqual(a.fields["lead_entry_date"], date(2026, 10, 19))
        self.assertNotIn("name", {k for k in a.fields})

    def test_cs7_implied_disbursed_and_net_amount(self):
        r = run("creditsea_disbursed", fx.make_csv(fx.CS7_HEADERS, [fx.cs7_row(1), fx.cs7_row(2, code="000")]))
        self.assertTrue(all(x.lead_status_code == "disbursed" for x in r.records))
        self.assertEqual(r.records[1].attribution_code, "000")
        self.assertEqual(r.records[0].fields["disbursed_amount"], Decimal("20000.00"))
        self.assertIn("user_creation_date", r.records[0].source_attributes)
        self.assertIsNone(r.records[0].fields["lead_entry_date"])      # User Creation Date is NOT lead entry

    def test_mkt_xlsx_booleans_excluded_and_ppc_prefix(self):
        row = fx.mkt34_row(1, status="Documents_Received", ppc_campaign="clickpe27p0123456789ab")
        r = run("paytrust_leads", fx.make_xlsx(fx.MKT34_HEADERS, [row]))
        rec = r.records[0]
        self.assertEqual(rec.attribution_code, "27P")
        self.assertEqual(rec.lead_status_code, "in_process")
        self.assertIn("aadhar_Details", r.ignored_columns)

    def test_mkt_existing_is_organic(self):
        row = fx.mkt34_row(1, ppc_campaign="existing")
        self.assertEqual(run("paytrust_leads", fx.make_xlsx(fx.MKT34_HEADERS, [row])).records[0].attribution_status, "organic")


def std21_ok(n, **kw):
    return fx.std21_row(n, **kw)


class FileFormats(unittest.TestCase):
    def test_empty_and_header_only_and_garbage(self):
        with self.assertRaises(FileFormatError) as c:
            read_table(b"")
        self.assertEqual(c.exception.code, "empty_file")
        t = read_table(fx.make_csv(fx.STD21_HEADERS, []))
        self.assertEqual(t.rows, [])
        with self.assertRaises(FileFormatError):
            read_table(b"\x00\x01\x02\x03binary\xff\xfe")

    def test_xlsx_content_detected_regardless_of_extension_and_empty_workbook(self):
        data = fx.make_xlsx(fx.DC18_HEADERS, [fx.dc18_row(1)])
        self.assertEqual(read_table(data).file_format, "xlsx")
        with self.assertRaises(FileFormatError) as c:
            read_table(fx.make_xlsx([], []))
        self.assertIn(c.exception.code, ("empty_workbook", "no_header_row"))

    def test_csv_bytes_starting_like_text_not_mistaken_for_xlsx(self):
        self.assertEqual(read_table(fx.make_csv(fx.DC18_HEADERS, [fx.dc18_row(1)])).file_format, "csv")

    def test_xlsx_configurable_header_row_and_ambiguous_sheets(self):
        data = fx.make_xlsx(fx.DC18_HEADERS, [fx.dc18_row(1)], title_rows=2)
        self.assertEqual(read_table(data, header_row=3).headers[0], "Lead ID")
        data = fx.make_xlsx(fx.DC18_HEADERS, [fx.dc18_row(1)], extra_sheets=[("Other", fx.DC18_HEADERS, [fx.dc18_row(2)])])
        with self.assertRaises(FileFormatError) as c:
            read_table(data)
        self.assertEqual(c.exception.code, "ambiguous_sheet")
        self.assertEqual(read_table(data, sheet_name="Other").rows[0][0].endswith("2"), True)

    def test_duplicate_headers_rejected(self):
        with self.assertRaises(FileFormatError) as c:
            read_table(b"Lead ID,lead id\r\n1,2\r\n")
        self.assertEqual(c.exception.code, "duplicate_headers")


class Compat(unittest.TestCase):
    def test_new_column_makes_incompatible(self):
        data = fx.make_csv(fx.STD21_HEADERS + ["Brand New"], [std21_ok(1)])
        t = read_table(data)
        c = check_compat(SPECS["kreditbuddha_leads_daily"], t.header_keys)
        self.assertEqual(c.level, "incompatible")
        self.assertEqual(c.unknown_headers, ("brand new",))

    def test_missing_canonical_column_incompatible_missing_excluded_compatible(self):
        h = [x for x in fx.STD21_HEADERS if x != "Disbursment Amount"]
        self.assertEqual(check_compat(SPECS["kreditbuddha_leads_daily"], read_table(fx.make_csv(h, [])).header_keys).level, "incompatible")
        h = [x for x in fx.STD21_HEADERS if x != "Date Of Birth"]
        self.assertEqual(check_compat(SPECS["kreditbuddha_leads_daily"], read_table(fx.make_csv(h, [])).header_keys).level, "compatible")

    def test_select_profile_by_headers(self):
        t = read_table(fx.make_csv(fx.DC18_HEADERS, []))
        cands = [SPECS["digicredit_leads"], SPECS["creditsea_disbursed"]]
        s, c, why = select_profile(cands, t.header_keys)
        self.assertEqual(s.profile_key, "digicredit_leads")
        s, c, why = select_profile([SPECS["creditsea_disbursed"]], t.header_keys)
        self.assertIsNone(s)
        self.assertEqual(why, "headers_incompatible")


@unittest.skipUnless(os.environ.get("MIS_REFERENCE_DIR"), "set MIS_REFERENCE_DIR to run against the local reference files")
class ReferenceFiles(unittest.TestCase):
    """Runs every real reference file through its profile. Reports aggregates only - never row content."""

    EXPECT = {  # filename prefix -> (profile, records)
        "CREDME": ("credme_leads_daily", 943), "kreditbuddha_CLICKPE": ("kreditbuddha_leads_daily", 949),
        "brightloans": ("brightloans_leads_daily", 80), "salarymantra": ("salarymantra_leads_daily", 1589),
        "digicredit": ("digicredit_leads", 23), "leads_2026": ("kreditbuddha_leads_range", 3458),
        "creditsea": ("creditsea_disbursed", 29), "paytrust": ("paytrust_leads", 9),
        "Marketing Report_2026-10-19": ("marketing_report", 12)}

    def test_every_reference_file(self):
        d = Path(os.environ["MIS_REFERENCE_DIR"])
        seen = 0
        for f in sorted(d.iterdir()):
            data = f.read_bytes()
            exp = next((v for k, v in self.EXPECT.items() if f.name.startswith(k)), None)
            if exp is None:
                with self.assertRaises(FileFormatError):
                    read_table(data)
                continue
            r = run(exp[0], data)
            self.assertEqual(len(r.records), exp[1], f.name)
            self.assertEqual(r.rows_quarantined, 0, f.name)
            self.assertEqual(r.footer_mismatches, [], f.name)
            seen += 1
        self.assertGreaterEqual(seen, 9)


if __name__ == "__main__":
    unittest.main()
