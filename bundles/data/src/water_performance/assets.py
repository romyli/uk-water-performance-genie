"""Read archived assets without trusting their layout: shape profiles and licence statements.

Profiles record each sheet's size and its first rows so that a parser contract can cite the
evidence it matched, and so an operator can see why an unsupported layout was rejected.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import zipfile

OGL = re.compile(r"open\s+government\s+licen[cs]e(?:\s+v(?:ersion)?\s*([0-9.]+))?", re.IGNORECASE)
CROWN = re.compile(r"crown\s+copyright", re.IGNORECASE)


def kind(file_name: str) -> str:
    name = file_name.lower()
    for ext, k in ((".xlsx", "xlsx"), (".xlsm", "xlsx"), (".csv", "csv"), (".pdf", "pdf"), (".jsonl.gz", "jsonl_gz")):
        if name.endswith(ext):
            return k
    return "other"


def open_workbook(path: str):
    import openpyxl

    # data_only: cached formula results, never formulas. A missing cached value stays None.
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


def _cell_text(v) -> str:
    return "" if v is None else str(v).strip()


def profile(path: str, file_name: str, sample_rows: int = 15, sample_width: int = 25) -> list[dict]:
    k = kind(file_name)
    if k == "xlsx":
        if not zipfile.is_zipfile(path):
            return [{"part": "(file)", "max_row": None, "max_col": None, "sample": "not a valid Excel (zip) file"}]
        wb = open_workbook(path)
        out = []
        try:
            for ws in wb.worksheets:
                rows = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= sample_rows:
                        break
                    rows.append([_cell_text(v)[:60] for v in row[:sample_width]])
                out.append({"part": ws.title, "max_row": ws.max_row, "max_col": ws.max_column, "sample": rows})
        finally:
            wb.close()
        return out
    if k == "csv":
        with open(path, "rb") as f:
            head = f.read(200_000).decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(head)))[:sample_rows]
        return [{"part": "(csv)", "max_row": None, "max_col": max((len(r) for r in rows), default=0), "sample": rows}]
    if k == "jsonl_gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            first = [next(f, "") for _ in range(3)]
        return [{"part": "(records)", "max_row": None, "max_col": None, "sample": [line[:500] for line in first]}]
    if k == "pdf":
        text, pages = pdf_text(path, max_pages=3)
        return [{"part": "(pdf)", "max_row": pages, "max_col": None, "sample": [text[:1500]]}]
    return [{"part": "(file)", "max_row": None, "max_col": None, "sample": "unsupported file type"}]


def pdf_text(path: str, max_pages: int | None = None) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    n = len(reader.pages)
    idx = range(n) if max_pages is None else sorted(set(list(range(min(max_pages, n))) + list(range(max(0, n - 2), n))))
    return "\n".join((reader.pages[i].extract_text() or "") for i in idx), n


def find_licence_statement(path: str, file_name: str) -> tuple[str | None, str | None, str | None]:
    """Search an asset for an Open Government Licence statement.

    Returns (licence_id, statement, locator), or (None, None, None) when no statement is present.
    Only an explicit OGL statement counts; 'Crown copyright' alone does not establish reuse terms."""
    k = kind(file_name)
    if k == "xlsx" and zipfile.is_zipfile(path):
        found = []
        wb = open_workbook(path)
        try:
            for ws in wb.worksheets:
                for r, row in enumerate(ws.iter_rows(values_only=True, max_row=300), start=1):
                    for c, v in enumerate(row, start=1):
                        text = _cell_text(v)
                        if text and OGL.search(text):
                            found.append((_ogl_id(text), text[:1000], f"sheet={ws.title}!R{r}C{c}"))
        finally:
            wb.close()
        # A versioned statement (for example on a Licence sheet) beats a passing mention elsewhere.
        versioned = [f for f in found if f[0] != "OGL-UK-unversioned"]
        if versioned or found:
            return (versioned or found)[0]
    elif k == "pdf":
        text, _ = pdf_text(path)
        m = OGL.search(text)
        if m:
            start = max(0, m.start() - 300)
            return _ogl_id(m.group(0)), " ".join(text[start : m.end() + 300].split()), "pdf text"
    return None, None, None


def _ogl_id(text: str) -> str:
    m = OGL.search(text)
    version = ((m.group(1) if m else None) or "").rstrip(".")
    if not version:
        return "OGL-UK-unversioned"  # not a recognised licence id, so the asset stays unverified
    return "OGL-UK-3.0" if version in ("3", "3.0") else f"OGL-UK-{version}"
