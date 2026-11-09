"""File -> text. Returns None for images (stub)."""
from pathlib import Path
from typing import Optional

import openpyxl
import pdfplumber


def read_xlsx(path: Path) -> str:
    wb = openpyxl.load_workbook(path, data_only=True)
    rows = []
    for ws in wb.worksheets:
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            cells = ["" if c is None else str(c).strip() for c in row]
            if any(cells):
                rows.append(f"Row {i}: " + " | ".join(cells))
    return "\n".join(rows)


def read_pdf(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def read_file(path: Path) -> Optional[str]:
    """Read an RFQ file into plain text; None means unsupported (image) source."""
    ext = path.suffix.lower()
    if ext == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".xlsx":
        return read_xlsx(path)
    return None
