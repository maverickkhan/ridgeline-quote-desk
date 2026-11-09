#!/usr/bin/env python3
"""Score pipeline outputs against the answer keys and write evaluation/results.json.

Metrics (pooled over all lines of all keyed RFQs unless noted):
- extraction_accuracy: expected lines whose paired prediction has the right quantity and unit.
- match_precision: predicted part numbers that equal the expected part number.
- match_recall: expected part numbers the pipeline returned correctly.
- quantity_accuracy / unit_accuracy: per-field accuracy over expected lines (unit case-insensitive).
- as_written_similarity_mean: mean fuzzy ratio of as_written text over paired lines.
- line_count_match: share of RFQs with the same number of lines as the key.
- customer_id_accuracy: share of RFQs whose customer_id matches the key.
- correct_abstention_rate: no-match expected lines left unmatched or flagged for review.
- review_rate: share of paired predicted lines flagged needs_review.
- auto_accept_precision: correct part numbers among predicted lines that skip review.
- auto_accepted_errors: wrong part numbers among predicted lines that skip review.
- threshold_sweep: what each confidence threshold would auto-accept (confidence only).
- per_rfq: extraction, precision, recall and line counts for each RFQ.
- errors: every quantity, unit or part-number mismatch, plus missing or extra lines.
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    from quote_desk.config import REVIEW_THRESHOLD
except ImportError as exc:
    print(f"WARNING: cannot import REVIEW_THRESHOLD from quote_desk.config ({exc}); using 0.8")
    REVIEW_THRESHOLD = 0.8

SWEEP = [round(0.50 + 0.05 * i, 2) for i in range(10)]


def _frac(num, den):
    return round(num / den, 3) if den else 0.0


def _unit(value):
    return str(value or "").strip().upper()


def _exp_pn(line):
    return (line or {}).get("expected_part_number")


def _pred_pn(line):
    return (line or {}).get("ridgeline_part_number")


def load_pairs(key_path, pred_path):
    """Pair expected and predicted lines by line number; return one record per RFQ."""
    key = json.loads(Path(key_path).read_text())
    pred = json.loads(Path(pred_path).read_text())
    pred_lines = pred.get("lines", [])
    by_line = {}
    for p in pred_lines:
        by_line.setdefault(p["line"], p)
    pairs = [(e, by_line.get(e["line"])) for e in key.get("lines", [])]
    used = {id(p) for _, p in pairs if p is not None}
    return {
        "rfq_id": key["rfq_id"],
        "customer_match": key.get("customer_id") == pred.get("customer_id"),
        "lines_expected": len(key.get("lines", [])),
        "lines_predicted": len(pred_lines),
        "pairs": pairs,
        "extras": [p for p in pred_lines if id(p) not in used],
    }


def _scored(recs):
    """Every predicted line with its paired expected line (None when unpaired)."""
    out = []
    for r in recs:
        out += [(e, p) for e, p in r["pairs"] if p is not None]
        out += [(None, p) for p in r["extras"]]
    return out


def _headline(recs):
    pairs = [pr for r in recs for pr in r["pairs"]]
    scored = _scored(recs)
    ext_ok = sum(1 for e, p in pairs if p and p.get("quantity") == e.get("quantity")
                 and _unit(p.get("unit")) == _unit(e.get("unit")))
    expected_pn = [(e, p) for e, p in pairs if _exp_pn(e)]
    recall_ok = sum(1 for e, p in expected_pn if _pred_pn(p) == _exp_pn(e))
    with_pn = [(e, p) for e, p in scored if _pred_pn(p)]
    prec_ok = sum(1 for e, p in with_pn if _pred_pn(p) == _exp_pn(e))
    return {
        "extraction_accuracy": _frac(ext_ok, len(pairs)),
        "match_precision": _frac(prec_ok, len(with_pn)),
        "match_recall": _frac(recall_ok, len(expected_pn)),
        "lines_expected": len(pairs),
        "lines_predicted": len(scored),
    }


def threshold_sweep(recs):
    paired = [(e, p) for r in recs for e, p in r["pairs"] if p is not None]
    rows = []
    for t in SWEEP:
        sel = [(e, p) for e, p in paired if _pred_pn(p) and (p.get("confidence") or 0.0) >= t]
        ok = sum(1 for e, p in sel if _pred_pn(p) == _exp_pn(e))
        rows.append({"threshold": t, "auto_accept_rate": _frac(len(sel), len(paired)),
                     "auto_accept_precision": _frac(ok, len(sel)), "auto_accepted_errors": len(sel) - ok})
    return rows


def collect_errors(recs):
    errs = []
    for r in recs:
        def add(line, field, exp, pred, conf):
            errs.append({"rfq_id": r["rfq_id"], "line": line, "field": field, "expected": exp,
                         "predicted": pred, "confidence": None if conf is None else round(conf, 3)})
        for e, p in r["pairs"]:
            if p is None:
                add(e["line"], "missing_line", e.get("as_written"), None, None)
                continue
            for field, ex, pr in [("quantity", e.get("quantity"), p.get("quantity")),
                                  ("unit", _unit(e.get("unit")), _unit(p.get("unit"))),
                                  ("part_number", _exp_pn(e), _pred_pn(p))]:
                if ex != pr:
                    add(e["line"], field, ex, pr, p.get("confidence"))
        for p in r["extras"]:
            add(p["line"], "extra_line", None, p.get("as_written"), p.get("confidence"))
    return errs


def compute_metrics(recs, threshold):
    pairs = [pr for r in recs for pr in r["pairs"]]
    paired = [(e, p) for e, p in pairs if p is not None]
    head = _headline(recs)
    abstain = [(e, p) for e, p in pairs if not _exp_pn(e)]
    abstained = sum(1 for _, p in abstain
                    if p is not None and (not _pred_pn(p) or p.get("needs_review")))
    auto = [(e, p) for e, p in _scored(recs) if not p.get("needs_review")]
    auto_ok = sum(1 for e, p in auto if _pred_pn(p) == _exp_pn(e))
    sims = [fuzz.ratio(str(e.get("as_written") or "").lower(),
                       str(p.get("as_written") or "").lower()) / 100 for e, p in paired]
    return {
        "threshold": round(float(threshold), 3),
        "extraction_accuracy": head["extraction_accuracy"],
        "match_precision": head["match_precision"],
        "match_recall": head["match_recall"],
        "additional_metrics": {
            "quantity_accuracy": _frac(sum(p.get("quantity") == e.get("quantity") for e, p in paired), len(pairs)),
            "unit_accuracy": _frac(sum(_unit(p.get("unit")) == _unit(e.get("unit")) for e, p in paired), len(pairs)),
            "as_written_similarity_mean": round(sum(sims) / len(sims), 3) if sims else 0.0,
            "line_count_match": _frac(sum(r["lines_expected"] == r["lines_predicted"] for r in recs), len(recs)),
            "customer_id_accuracy": _frac(sum(r["customer_match"] for r in recs), len(recs)),
            "correct_abstention_rate": _frac(abstained, len(abstain)),
            "review_rate": _frac(sum(bool(p.get("needs_review")) for _, p in paired), len(paired)),
            "auto_accept_precision": _frac(auto_ok, len(auto)),
            "auto_accepted_errors": len(auto) - auto_ok,
            "threshold_sweep": threshold_sweep(recs),
            "per_rfq": {r["rfq_id"]: _headline([r]) for r in recs},
            "errors": collect_errors(recs),
        },
    }


def print_report(res):
    am = res["additional_metrics"]
    print(f"Review threshold {res['threshold']} | extraction {res['extraction_accuracy']:.3f} | "
          f"precision {res['match_precision']:.3f} | recall {res['match_recall']:.3f}")
    print("  ".join(f"{k} {am[k]:.3f}" for k in ("quantity_accuracy", "unit_accuracy", "as_written_similarity_mean",
                                                  "line_count_match", "customer_id_accuracy",
                                                  "correct_abstention_rate", "review_rate",
                                                  "auto_accept_precision")))
    print(f"auto_accepted_errors {am['auto_accepted_errors']}")
    print(f"\n{'rfq_id':<18}{'extract':>8}{'prec':>8}{'recall':>8}{'exp':>5}{'pred':>6}")
    for rid, m in am["per_rfq"].items():
        print(f"{rid:<18}{m['extraction_accuracy']:>8.3f}{m['match_precision']:>8.3f}{m['match_recall']:>8.3f}"
              f"{m['lines_expected']:>5}{m['lines_predicted']:>6}")
    print(f"\n{'threshold':>9}{'auto_rate':>11}{'precision':>11}{'errors':>8}")
    for row in am["threshold_sweep"]:
        print(f"{row['threshold']:>9.2f}{row['auto_accept_rate']:>11.3f}{row['auto_accept_precision']:>11.3f}"
              f"{row['auto_accepted_errors']:>8}")
    print(f"\nErrors ({len(am['errors'])})")
    for e in am["errors"]:
        print(f"  {e['rfq_id']} line {e['line']} {e['field']}: expected {e['expected']!r} "
              f"predicted {e['predicted']!r} conf {e['confidence']}")


def main():
    ap = argparse.ArgumentParser(description="Score pipeline predictions against answer keys.")
    ap.add_argument("--pred-dir", default=str(ROOT / "outputs"), help="directory of prediction JSON files")
    ap.add_argument("--out", default=str(ROOT / "evaluation" / "results.json"), help="results JSON path")
    args = ap.parse_args()

    keys = sorted(glob.glob(str(ROOT / "data" / "answer_key" / "*.json")))
    if not keys:
        print(f"No answer keys found in {ROOT / 'data' / 'answer_key'}")
        return 1
    preds = [os.path.join(args.pred_dir, os.path.basename(k)) for k in keys]
    missing = [p for p in preds if not os.path.exists(p)]
    if missing:
        print("Missing prediction file(s); the pipeline has not produced them yet:")
        print("\n".join(f"  {m}" for m in missing))
        return 1

    recs = [load_pairs(k, p) for k, p in zip(keys, preds)]
    res = compute_metrics(recs, REVIEW_THRESHOLD)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)
    print_report(res)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
