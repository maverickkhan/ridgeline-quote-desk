"""Orchestration: read -> extract -> match -> units -> review flags -> outputs/."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .catalog import Catalog, load_catalog
from .config import OUT_DIR, REVIEW_FLAGS, REVIEW_THRESHOLD, RFQ_DIR
from .extract import extract, lookup_customer
from .match import match_line
from .models import ExtractedLine, OutLine
from .readers import read_file

UNIT_MAP = {"ea": "EA", "each": "EA", "pc": "EA", "pcs": "EA", "piece": "EA", "pieces": "EA",
            "box": "BX", "boxes": "BX", "bx": "BX", "case": "CS", "cases": "CS", "cs": "CS",
            "pair": "PR", "pairs": "PR", "pr": "PR", "pack": "PK", "pk": "PK", "pkg": "PK",
            "roll": "RL", "rolls": "RL", "rl": "RL", "ft": "FT", "feet": "FT", "foot": "FT"}


def resolve_unit(unit_raw: Optional[str], qty: Optional[int], product: Optional[dict]) -> tuple[Optional[str], list[str], Optional[str]]:
    """Return (unit, flags, note) per the unit rules."""
    pu = product["unit"] if product else None
    raw = (unit_raw or "").strip()
    if not raw:
        return pu, [], None
    canon = UNIT_MAP.get(raw.lower().rstrip("."))
    if canon:
        if pu and pu != canon:
            return canon, ["unit_mismatch"], f"customer asked {qty if qty is not None else ''} {canon}, product sold as {pu}".replace("  ", " ")
        return canon, [], None
    if pu:
        return pu, ["unit_mapped"], f"customer unit {raw} mapped to {pu}"
    return None, [], None


PACK_RE = re.compile(r"\b(box|pack|case|roll)\s+of\s+(\d+)", re.I)


def pack_check(ln: ExtractedLine, product: Optional[dict], unit: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Flag unitless quantities that look like piece counts for a pack-sold product."""
    if (ln.unit_raw or "").strip() or not product or ln.quantity is None:
        return [], None
    m = PACK_RE.search(product["description"])
    if m and ln.quantity >= int(m.group(2)):
        return ["pack_size_check"], (f"qty {ln.quantity} with no unit; product sold as {unit} of {m.group(2)} "
                                     "- confirm pieces vs boxes")
    return [], None


def build_line(i: int, ln: ExtractedLine, cat: Catalog) -> OutLine:
    """Match one extracted line and assemble the output record."""
    m = match_line(ln, cat)
    product = cat.products.get(m.part) if m.part else None
    unit, uflags, unote = resolve_unit(ln.unit_raw, ln.quantity, product)
    pflags, pnote = pack_check(ln, product, unit)
    flags = list(dict.fromkeys(ln.flags + m.flags + uflags + pflags))
    notes = [n for n in (ln.note, m.note, unote, pnote) if n]
    review = (m.conf < REVIEW_THRESHOLD or m.part is None or ln.quantity is None or bool(set(flags) & REVIEW_FLAGS))
    return OutLine(line=i, as_written=ln.as_written, quantity=ln.quantity, unit=unit, ridgeline_part_number=m.part,
                   match_method=m.method, confidence=round(m.conf, 4) if m.part else 0.0, needs_review=review,
                   flags=flags, note="; ".join(notes) or None)


def stub(rfq_id: str) -> dict:
    line = OutLine(line=1, as_written="entire RFQ", flags=["stub"], note="photo source: needs OCR or a vision model")
    return {"rfq_id": rfq_id, "customer_id": None, "customer_name": None,
            "lines": [line.model_dump()], "non_item_rows": []}


def failed(rfq_id: str, why: str) -> dict:
    line = OutLine(line=1, as_written="entire RFQ", flags=["extraction_failed"], note=why)
    return {"rfq_id": rfq_id, "customer_id": None, "customer_name": None,
            "lines": [line.model_dump()], "non_item_rows": []}


def process(path: Path, cat: Catalog) -> dict:
    """Process one RFQ file into the output dict."""
    rfq_id = path.stem
    text = read_file(path)
    if text is None:
        return stub(rfq_id)
    try:
        ex = extract(text)
    except Exception as e:  # noqa: BLE001 - never crash the run
        return failed(rfq_id, f"extraction failed: {type(e).__name__}")
    cid, cname = lookup_customer(ex.customer_name, ex.customer_email, cat)
    lines = [build_line(i, ln, cat) for i, ln in enumerate(ex.lines, 1)]
    return {"rfq_id": rfq_id, "customer_id": cid, "customer_name": cname or ex.customer_name,
            "lines": [l.model_dump(exclude_none=False) for l in lines], "non_item_rows": ex.non_item_rows}


def summary(out: dict) -> str:
    ls = out["lines"]
    return (f"{out['rfq_id']}: lines={len(ls)} matched={sum(bool(l['ridgeline_part_number']) for l in ls)} "
            f"needs_review={sum(l['needs_review'] for l in ls)}")


def run(only: Optional[str] = None) -> None:
    """Process all RFQs (or one) concurrently and write outputs/*.json."""
    cat = load_catalog()
    files = sorted(p for p in RFQ_DIR.iterdir() if p.is_file() and (not only or p.stem == only))
    OUT_DIR.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda p: process(p, cat), files))
    for out in results:
        (OUT_DIR / f"{out['rfq_id']}.json").write_text(json.dumps(out, indent=2) + "\n")
        print(summary(out))
