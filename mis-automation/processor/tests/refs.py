"""Shared constants for tests."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from processor.filename_parser import ProfileRef

IST = ZoneInfo("Asia/Kolkata")
UPLOADED = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)           # 20:30 IST on 19 Oct
NOW = datetime(2026, 10, 20, 1, 0, tzinfo=IST)


def ref(i, portal, key, rtype, regex, fmt="YYYYMMDD"):
    return ProfileRef(i, i, portal, key, rtype, regex, fmt)


PROFILES = [
    ref(1, "credme", "credme_leads_daily", "lead_export", r"^CREDME_CLICKPE_ALL_LEADS_(?P<report_date>\d{8})_(?P<report_time>\d{6})$"),
    ref(2, "kreditbuddha", "kreditbuddha_leads_daily", "lead_export", r"^kreditbuddha_CLICKPE_leads_(?P<report_date>\d{8})_(?P<report_time>\d{6})$"),
    ref(3, "brightloans", "brightloans_leads_daily", "lead_export", r"^brightloans_leads_(?P<report_date>\d{8})-[0-9a-f]{8}$"),
    ref(4, "salarymantra", "salarymantra_leads_daily", "lead_export", r"^salarymantra_CLICKPE_leads_(?P<report_date>\d{8})-[0-9a-f]{8}$"),
    ref(5, "digicredit", "digicredit_leads", "lead_export", r"^digicredit_leads_(?P<period_start>\d{8})_(?P<period_end>\d{8})-[0-9a-f]{8}$"),
    ref(6, "creditsea", "creditsea_disbursed", "disbursed_cases", r"^creditsea_disbursed_cases_(?P<report_date>\d{4}-\d{2}-\d{2})-[0-9a-f]{8}$", "YYYY-MM-DD"),
    ref(7, "kreditbuddha", "kreditbuddha_leads_range", "lead_export", r"^leads_(?P<period_start>\d{8})_(?P<period_end>\d{8})$"),
    ref(8, "paytrust", "paytrust_leads", "lead_export", r"^paytrust_leads_(?P<report_date>\d{4}-\d{2}-\d{2})-[0-9a-f]{8}$", "YYYY-MM-DD"),
    ref(9, "marketing_tbd", "marketing_report", "lead_export", r"^Marketing[ _]Report_(?P<report_date>\d{4}-\d{2}-\d{2})(?:_(?P<report_time>\d{2}-\d{2}-\d{2}))?(?:_[0-9a-f]{8})?$", "YYYY-MM-DD"),
]
