"""Synthetic fixtures. Everything here is invented: ids are random hex, names are obviously fake, no real customer data."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import openpyxl

SEED_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"

STD21_HEADERS = ["Lead ID", "Partner Name", "Lead Source", "UTM Source", "UTM Medium", "UTM Campaign", "UTM Term",
                 "Lead Status", "Doable", "Disbursment Date", "Disbursment Amount", "Pricing Model", "Pricing Type",
                 "Pricing", "Payout", "Rejection Reason", "State", "City", "Pincode", "Date Of Birth", "Lead Entry Date"]
DC18_HEADERS = ["Lead ID", "Date", "Name", "Employment Type", "Loan Amt", "Monthly Income", "CIBIL", "Salary Mode",
                "City", "State", "Status", "Remark", "UTM Source", "Medium", "Campaign", "Approved Amt",
                "Disbursed Amt", "Disbursement Date"]
CS7_HEADERS = ["User Creation Date", "Application Number", "Phone Number", "Net Disbursed Amount (INR)",
               "Disbursal Date", "Medium", "Campaign"]
PARTIAL_HEADERS = ["Lead ID", "Disbursed Amount", "Disbursal Date", "Campaign"]
PARTIAL_SPEC = {
    "portal": "kreditbuddha", "profile_key": "kb_disbursed_only", "report_type": "disbursed_cases",
    "import_mode": "enrichment", "file_format": "csv", "report_date_format": "YYYY-MM-DD",
    "filename_regex": r"^kb_disbursed_(?P<report_date>\d{4}-\d{2}-\d{2})$", "identity_namespace": "lead_id",
    "on_unknown_lead": "create", "implied_status_code": "disbursed",
    "columns": [
        {"header": "Lead ID", "role": "canonical", "field": "external_lead_id", "transform": "text"},
        {"header": "Disbursed Amount", "role": "canonical", "field": "disbursed_amount", "transform": "number"},
        {"header": "Disbursal Date", "role": "canonical", "field": "disbursed_date", "transform": "date", "date_format": "DD Mon YYYY"},
        {"header": "Campaign", "role": "canonical", "field": "utm_campaign", "transform": "text"}],
    "attribution_rules": [{"priority": 1, "source_field": "utm_campaign", "strip_prefixes": [], "organic_values": []}],
    "validation_rules": [{"field": "external_lead_id", "rule": "required"}, {"field": "disbursed_amount", "rule": "required"}],
}


def partial_row(lead_id, amount="30000", code="ABC", n=1, **over):
    r = {"Lead ID": str(lead_id), "Disbursed Amount": amount, "Disbursal Date": "19 Oct 2026", "Campaign": hexid(n, code)}
    r.update(over)
    return r


MKT34_HEADERS = ["customerId", "leadId", "purpose", "loanAmount", "monthlyIncome", "salary", "city", "state", "status",
                 "utm_source", "lead_source", "ppc_campaign", "gclid", "clid", "third_party_id", "ppc_source",
                 "created_at", "updated_at", "medium", "aadhar_Details", "pan_Details", "facematch_Details",
                 "bankstatement", "approvalAmount", "breApproveAmount", "remarks", "disbursalAmount", "disbursalDate",
                 "SalaryMode", "AgentName", "cbs_risk_grade_v1", "e_sign", "emandate", "reference"]


def hexid(n: int, code: str = "ABC") -> str:
    """15 chars: 3-char attribution code + 12 hex digits (shape of real campaign values, invented content)."""
    return f"{code}{n:012X}"


def std21_row(n: int = 1, *, status="SYSTEM-REJECT", campaign=None, medium="WEB", code="ABC", prefix="CLICKPE",
              lead_id=None, **over) -> dict:
    r = {"Lead ID": str(lead_id or 900000 + n), "Partner Name": "TESTPARTNER", "Lead Source": "WEBSITE",
         "UTM Source": "TESTPARTNER", "UTM Medium": medium,
         "UTM Campaign": campaign if campaign is not None else f"{prefix}{hexid(n, code)}",
         "UTM Term": "ORGANIC", "Lead Status": status, "Doable": "No", "Disbursment Date": "", "Disbursment Amount": "",
         "Pricing Model": "Disbursal", "Pricing Type": "Flat", "Pricing": "800.00", "Payout": "0",
         "Rejection Reason": "ELIGIBILITY RULES NOT MET", "State": "TESTSTATE", "City": "Testville",
         "Pincode": "110001", "Date Of Birth": "1990-01-01", "Lead Entry Date": "2026-10-19"}
    r.update(over)
    return r


def dc18_row(n: int = 1, *, status="Fresh Lead", code="ABC", prefix="basic_referral", **over) -> dict:
    r = {"Lead ID": f"00000000-0000-4000-8000-{n:012x}", "Date": "19 Oct 2026", "Name": "Test Person",
         "Employment Type": "SALARIED", "Loan Amt": "50000", "Monthly Income": "40000", "CIBIL": "700",
         "Salary Mode": "SALARIED", "City": "Testville", "State": "TESTSTATE", "Status": status, "Remark": "",
         "UTM Source": "TESTPARTNER", "Medium": "referral", "Campaign": f"{prefix}{hexid(n, code)}",
         "Approved Amt": "", "Disbursed Amt": "", "Disbursement Date": ""}
    r.update(over)
    return r


def cs7_row(n: int = 1, *, code="ABC", amount="20000", appno=None, **over) -> dict:
    r = {"User Creation Date": "01 Sept 2026", "Application Number": appno or f"APP{n:07d}",
         "Phone Number": "XXXXXX0000", "Net Disbursed Amount (INR)": amount, "Disbursal Date": "19 Oct 2026",
         "Medium": "", "Campaign": hexid(n, code)}
    r.update(over)
    return r


def mkt34_row(n: int = 1, *, status="Fresh_Lead", code="abc", **over) -> dict:
    r = {h: "" for h in MKT34_HEADERS}
    r.update({"customerId": f"cust-{n}", "leadId": f"lead-{n:06d}", "purpose": "Personal", "loanAmount": "30000",
              "monthlyIncome": "25000", "salary": "Salaried", "city": "Testville", "state": "TESTSTATE",
              "status": status, "utm_source": "clickpe", "lead_source": "WEB",
              "ppc_campaign": f"clickpe{code}{n:012x}"[:15 + 7], "created_at": "2026-10-19 10:00:00",
              "medium": "ppc", "aadhar_Details": "true", "pan_Details": "true", "approvalAmount": "", "disbursalAmount": ""})
    r.update(over)
    return r


def make_csv(headers, rows, *, footer=None, delimiter=",", encoding="utf-8", bom=False, lineterminator="\r\n") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator=lineterminator)
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(h, "") for h in headers])
    if footer:
        w.writerow(footer)
    data = buf.getvalue().encode(encoding)
    return (b"\xef\xbb\xbf" + data) if bom else data


def std21_footer(disbursed_count=0, disbursed_amount=0, doable=0, payout=0) -> list[str]:
    f = [""] * len(STD21_HEADERS)
    f[STD21_HEADERS.index("Lead Status")] = f"Total Disbursed Count : {disbursed_count}"
    f[STD21_HEADERS.index("Doable")] = f"Total Doable Count : {doable}"
    f[STD21_HEADERS.index("Disbursment Amount")] = f"Total Disbursed Amount : {disbursed_amount}"
    f[STD21_HEADERS.index("Payout")] = f"Total Payout : {payout}"
    return f


def make_xlsx(headers, rows, *, sheet="Sheet1", extra_sheets=(), title_rows=0) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for i in range(title_rows):
        ws.append([f"banner line {i + 1}"])
    ws.append(list(headers))
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    for name, hs, rs in extra_sheets:
        w2 = wb.create_sheet(name)
        w2.append(list(hs))
        for r in rs:
            w2.append([r.get(h, "") for h in hs])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
