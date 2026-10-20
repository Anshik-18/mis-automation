"""Reads CSV / XLSX bytes into a table of strings. Format is detected from the CONTENT, never the extension."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time

from .normalization import normalize_header


class FileFormatError(Exception):
    """File cannot be read as a table (corrupt, unsupported, ambiguous)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass
class ParsedTable:
    file_format: str                       # csv | xlsx
    headers: list[str]                     # as in the file (stripped)
    header_keys: list[str]                 # normalized
    rows: list[list[str]]
    row_numbers: list[int]                 # 1-based line / sheet row of each data row
    meta: dict = field(default_factory=dict)


def detect_format(data: bytes) -> str:
    """'empty' | 'xlsx' | 'xls' | 'csv'."""
    if not data or not data.strip(b"\x00 \t\r\n\xef\xbb\xbf"):
        return "empty"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile as exc:
            raise FileFormatError("corrupt_zip", "archive is damaged") from exc
        if "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names):
            return "xlsx"
        raise FileFormatError("unsupported_archive", "zip file is not an xlsx workbook")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    if b"\x00" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        raise FileFormatError("binary_file", "not a text table")
    return "csv"


def decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def sniff_delimiter(text: str) -> str:
    lines = [ln for ln in text.splitlines()[:20] if ln.strip()]
    best, best_score = ",", -1
    for d in [",", ";", "\t", "|"]:
        counts = [len(next(csv.reader([ln], delimiter=d))) for ln in lines]
        if not counts or counts[0] < 2:
            continue
        consistent = sum(1 for c in counts if c == counts[0])
        score = consistent * 100 + counts[0]
        if score > best_score:
            best, best_score = d, score
    return best


def _cell_to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if v.time() == time(0, 0) else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def _build(file_format, raw_rows, header_row: int, meta) -> ParsedTable:
    """raw_rows: iterable of (row_number, list[str]) in file order."""
    header, data = None, []
    for rn, cells in raw_rows:
        if header is None:
            if rn < header_row:
                continue
            header = [c.strip() for c in cells]
            header_rn = rn
            continue
        data.append((rn, cells))
    if header is None or not any(header):
        raise FileFormatError("no_header_row", f"no header found at row {header_row}")
    n = len(header)
    # trailing empty header cells (Excel/CSV artefacts) are dropped only if their column is empty everywhere
    while n and header[n - 1] == "" and all((len(c) <= n - 1 or c[n - 1].strip() == "") for _, c in data):
        n -= 1
    header = header[:n]
    if any(h == "" for h in header):
        header = [h if h else f"unnamed_{i + 1}" for i, h in enumerate(header)]
    keys = [normalize_header(h) for h in header]
    if len(set(keys)) != len(keys):
        dup = sorted({k for k in keys if keys.count(k) > 1})
        raise FileFormatError("duplicate_headers", f"{len(dup)} duplicate header name(s) after normalisation")
    rows, nums, ragged = [], [], 0
    for rn, cells in data:
        if len(cells) > n and any(c.strip() for c in cells[n:]):
            ragged += 1                      # data beyond the header width: keep row, flag it
            meta.setdefault("ragged_rows", []).append(rn)
        cells = (cells + [""] * n)[:n]
        rows.append([c.strip() for c in cells])
        nums.append(rn)
    meta["header_row"] = header_rn
    meta["ragged_row_count"] = ragged
    return ParsedTable(file_format, header, keys, rows, nums, meta)


def read_csv(data: bytes, header_row: int = 1) -> ParsedTable:
    text, enc = decode_text(data)
    delim = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    meta = {"encoding": enc, "delimiter": delim}
    try:
        raw = [(reader.line_num, row) for row in reader]
    except csv.Error as exc:
        raise FileFormatError("malformed_csv", str(exc)) from exc
    # reader.line_num after reading a row = last physical line of that record; good enough for diagnostics
    return _build("csv", raw, header_row, meta)


def read_xlsx(data: bytes, header_row: int = 1, sheet_name: str | None = None) -> ParsedTable:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:                                      # corrupt workbook
        raise FileFormatError("corrupt_xlsx", type(exc).__name__) from exc
    try:
        meta = {"sheets": [ws.title for ws in wb.worksheets]}
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                raise FileFormatError("sheet_not_found", f"configured sheet missing; available: {wb.sheetnames}")
            ws = wb[sheet_name]
            meta["sheet"] = ws.title
            raw = [(i, [_cell_to_str(c) for c in row]) for i, row in enumerate(ws.iter_rows(values_only=True), start=1)
                   if any(c is not None and str(c).strip() for c in row) or i == header_row]
        else:
            filled = []
            for ws in wb.worksheets:
                rows = [(i, [_cell_to_str(c) for c in row]) for i, row in enumerate(ws.iter_rows(values_only=True), start=1)
                        if any(c is not None and str(c).strip() for c in row)]
                if rows:
                    filled.append((ws.title, rows))
            if not filled:
                raise FileFormatError("empty_workbook", "no cells with data")
            if len(filled) > 1:
                raise FileFormatError("ambiguous_sheet", f"{len(filled)} non-empty sheets and none configured")
            meta["sheet"], raw = filled[0]
        return _build("xlsx", raw, header_row, meta)
    finally:
        wb.close()


def read_table(data: bytes, *, header_row: int = 1, sheet_name: str | None = None) -> ParsedTable:
    fmt = detect_format(data)
    if fmt == "empty":
        raise FileFormatError("empty_file", "file has no content")
    if fmt == "xls":
        raise FileFormatError("unsupported_xls", "legacy .xls is not supported; re-export as .xlsx or .csv")
    if fmt == "xlsx":
        return read_xlsx(data, header_row, sheet_name)
    return read_csv(data, header_row)
