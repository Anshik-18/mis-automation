import unittest
from datetime import date, datetime, time, timezone

from processor import filename_parser as fp
from processor.tests.refs import IST, NOW, PROFILES, UPLOADED


def resolve(name, **kw):
    return fp.resolve(name, PROFILES, tz=IST, uploaded_at=UPLOADED, now=NOW, **kw)


class SplitExtension(unittest.TestCase):
    def test_double_and_single_extensions(self):
        self.assertEqual(fp.split_extension("a.csv.txt"), ("a", ".csv.txt"))
        self.assertEqual(fp.split_extension("A.CSV"), ("A", ".CSV"))
        self.assertEqual(fp.split_extension("m.xlsx"), ("m", ".xlsx"))
        self.assertEqual(fp.split_extension("noext"), ("noext", ""))


class ObservedPatterns(unittest.TestCase):
    def check(self, name, portal, rdate, rtime=None, start=None, end=None):
        r = resolve(name)
        self.assertEqual(r.status, "ok", (name, r.reason))
        self.assertEqual(r.profile.portal_code, portal)
        self.assertEqual(r.info.report_date, rdate)
        self.assertEqual(r.info.report_time, rtime)
        self.assertEqual(r.info.period_start, start)
        self.assertEqual(r.info.period_end, end)
        return r

    def test_credme_with_timestamp(self):
        r = self.check("CREDME_CLICKPE_ALL_LEADS_20261019_200107.csv", "credme", date(2026, 10, 19), time(20, 1, 7))
        self.assertEqual(r.info.filename_ts, datetime(2026, 10, 19, 20, 1, 7, tzinfo=IST))
        self.assertEqual(r.info.observed_at, r.info.filename_ts)

    def test_kreditbuddha_with_timestamp(self):
        self.check("kreditbuddha_CLICKPE_leads_20261019_211948.csv", "kreditbuddha", date(2026, 10, 19), time(21, 19, 48))

    def test_creditsea_iso_date_random_suffix_double_extension(self):
        r = self.check("creditsea_disbursed_cases_2026-10-19-2cab89d5.csv.txt", "creditsea", date(2026, 10, 19))
        self.assertEqual(r.profile.report_type, "disbursed_cases")
        # no timestamp in name => freshness stamp is report date 00:00 business tz
        self.assertEqual(r.info.observed_at, datetime(2026, 10, 19, 0, 0, tzinfo=IST))

    def test_hash_suffix_files(self):
        self.check("brightloans_leads_20261019-fed78a20.csv.txt", "brightloans", date(2026, 10, 19))
        self.check("salarymantra_CLICKPE_leads_20261019-bdedde32.csv.txt", "salarymantra", date(2026, 10, 19))
        self.check("paytrust_leads_2026-10-19-4f36720e.xlsx", "paytrust", date(2026, 10, 19))

    def test_single_day_range_keeps_period(self):
        self.check("digicredit_leads_20261019_20261019-b7e04cee.csv.txt", "digicredit", date(2026, 10, 19),
                   start=date(2026, 10, 19), end=date(2026, 10, 19))

    def test_marketing_with_and_without_time(self):
        r = self.check("Marketing_Report_2026-10-20_00-16-15_47d6c1f3.xlsx", "marketing_tbd", date(2026, 10, 20), time(0, 16, 15))
        self.assertEqual(r.info.observed_at, datetime(2026, 10, 20, 0, 16, 15, tzinfo=IST))
        self.check("Marketing Report_2026-10-19.xlsx", "marketing_tbd", date(2026, 10, 19))


class RangeAndAmbiguity(unittest.TestCase):
    def test_future_range_end_goes_to_review_by_default(self):
        r = resolve("leads_20260930_20261030.csv")
        self.assertEqual(r.status, "review")
        self.assertIn("range_end_after_upload_day", r.reason)
        self.assertEqual(r.info.period_start, date(2026, 9, 30))
        self.assertEqual(r.info.period_end, date(2026, 10, 30))

    def test_future_range_end_clamp_policy(self):
        r = resolve("leads_20260930_20261030.csv", range_end_future_policy="clamp")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.info.report_date, date(2026, 10, 19))      # upload day in IST
        self.assertIn("report_date_clamped_to_upload_day", r.info.notes)

    def test_past_range_uses_period_end(self):
        r = fp.parse_with_pattern("leads_20260901_20260930.csv", PROFILES[6].filename_regex, "YYYYMMDD",
                                  tz=IST, uploaded_at=UPLOADED, now=NOW)
        self.assertEqual(r.report_date, date(2026, 9, 30))

    def test_unknown_filename_is_review_not_guess(self):
        r = resolve("something_20261019.csv")
        self.assertEqual((r.status, r.reason), ("review", "no_profile_matches_filename"))

    def test_invalid_calendar_date_is_review(self):
        r = resolve("brightloans_leads_20261345-fed78a20.csv")
        self.assertEqual(r.status, "review")
        self.assertIn("invalid_report_date", r.reason)

    def test_future_report_date_is_review(self):
        r = resolve("brightloans_leads_20270101-fed78a20.csv")
        self.assertEqual(r.status, "review")
        self.assertIn("report_date_in_future", r.reason)

    def test_invalid_time_is_review(self):
        r = resolve("CREDME_CLICKPE_ALL_LEADS_20261019_256107.csv")
        self.assertEqual(r.status, "review")
        self.assertIn("invalid_report_time", r.reason)

    def test_ambiguous_profile_and_folder_hint(self):
        from processor.tests.refs import ref
        dup = PROFILES + [ref(20, "otherportal", "other_range", "lead_export", r"^leads_(?P<period_start>\d{8})_(?P<period_end>\d{8})$")]
        r = fp.resolve("leads_20260901_20260930.csv", dup, tz=IST, uploaded_at=UPLOADED, now=NOW)
        self.assertEqual(r.reason, "ambiguous_profile")
        r2 = fp.resolve("leads_20260901_20260930.csv", dup, tz=IST, uploaded_at=UPLOADED, now=NOW,
                        folder_portal_hints={"otherportal"})
        self.assertEqual((r2.status, r2.profile.portal_code), ("ok", "otherportal"))

    def test_bad_profile_regex_does_not_crash(self):
        info = fp.parse_with_pattern("x.csv", "([", "YYYYMMDD", tz=IST, uploaded_at=UPLOADED, now=NOW)
        self.assertIn("invalid_profile_regex", info.issues)


class DriveMetadata(unittest.TestCase):
    def test_timestamps_are_timezone_aware_utc(self):
        from processor.drive_metadata import from_api, parse_rfc3339
        d = from_api({"id": "f1", "name": "a.csv", "mimeType": "text/csv", "size": "12",
                      "createdTime": "2026-10-19T14:31:07.123456789Z", "modifiedTime": "2026-10-19T20:01:07+05:30",
                      "md5Checksum": "ABC", "parents": ["p1"]})
        self.assertEqual(d.created_at.utcoffset().total_seconds(), 0)
        self.assertEqual(d.modified_at, parse_rfc3339("2026-10-19T14:31:07Z"))
        self.assertEqual(d.size_bytes, 12)
        self.assertEqual(d.version_key, "md5:abc")
        self.assertEqual(d.eligibility(), (True, None))

    def test_eligibility_and_errors(self):
        from processor.drive_metadata import DriveMetadataError, from_api
        base = {"id": "1", "createdTime": "2026-10-19T00:00:00Z"}
        self.assertEqual(from_api({**base, "name": "x.pdf"}).eligibility(), (False, "unsupported_extension"))
        self.assertEqual(from_api({**base, "name": "x", "mimeType": "application/vnd.google-apps.folder"}).eligibility()[1], "folder")
        self.assertEqual(from_api({**base, "name": "s", "mimeType": "application/vnd.google-apps.spreadsheet"}).eligibility()[1],
                         "google_native_document_not_exported")
        self.assertEqual(from_api({**base, "name": "a.csv.txt"}).eligibility(), (True, None))
        with self.assertRaises(DriveMetadataError):
            from_api({"id": "1", "name": "a.csv"})
        with self.assertRaises(DriveMetadataError):
            from_api({**base, "name": "a.csv", "createdTime": "2026-10-19T00:00:00"})


if __name__ == "__main__":
    unittest.main()
