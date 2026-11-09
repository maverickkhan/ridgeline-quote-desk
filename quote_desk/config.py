"""Paths, thresholds and model names."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
RFQ_DIR = DATA / "rfqs"
OUT_DIR = ROOT / "outputs"
CACHE_DIR = ROOT / ".cache" / "llm"

REVIEW_THRESHOLD = 0.85  # see README > Evaluation: sits in the gap between risky (<=0.80) and safe (>=0.88) match types
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = "gemini-2.5-flash"

REVIEW_FLAGS = {
    "not_grounded", "unit_mismatch", "discontinued_replaced", "xref_conflict", "xref_note",
    "ambiguous_match", "llm_match", "refers_to_previous_order", "ambiguous_quantity",
    "ambiguous_item", "unparseable", "extraction_failed", "modified_in_thread",
    "no_catalog_match", "unknown_ridgeline_pn", "pack_size_check", "xref_pkg_qty_differs",
    "xref_close_substitute",
}
SKIP_MATCH_FLAGS = {"refers_to_previous_order", "ambiguous_item", "unparseable"}
