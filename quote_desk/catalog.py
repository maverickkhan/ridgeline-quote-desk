"""Load products, cross-reference and customers."""
import csv
import re
from dataclasses import dataclass, field

from .config import DATA


def _rows(name: str) -> list[dict]:
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def xref_key(s: str) -> str:
    """Uppercase alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


@dataclass
class Catalog:
    products: dict[str, dict] = field(default_factory=dict)
    xref: dict[str, list[dict]] = field(default_factory=dict)
    customers: list[dict] = field(default_factory=list)


def load_catalog() -> Catalog:
    cat = Catalog(products={r["part_number"]: r for r in _rows("products.csv")},
                  customers=_rows("customers.csv"))
    for r in _rows("cross_reference.csv"):
        cat.xref.setdefault(xref_key(r["competitor_part_number"]), []).append(r)
    return cat
