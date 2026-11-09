"""Matching cascade: Ridgeline PN -> cross-reference -> description -> LLM fallback."""
import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from .catalog import Catalog, xref_key
from .extract import generate_json
from .models import ExtractedLine, LLMMatch

RL_RE = re.compile(r"\bRL\d{6}\b", re.I)
COMP_RE = re.compile(r"(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{4,}", re.I)


@dataclass
class Match:
    part: Optional[str] = None
    method: Optional[str] = None
    conf: float = 0.0
    flags: list[str] = field(default_factory=list)
    note: Optional[str] = None


def norm_desc(s: str) -> str:
    """Normalize a description for fuzzy matching."""
    s = s.lower().replace('"', " in ")
    s = re.sub(r"\bin\.", " in ", s)
    s = re.sub(r"\bgr(?:ade)?\s?(\d+)\b", r"grade \1", s)  # gr5 / gr 8 / grade5 -> grade N
    s = re.sub(r"(?<=\d)\s*#", " lb", s)
    s = re.sub(r"\bhyd\b", "hydraulic", s)
    s = re.sub(r"\bfp\b", "full port", s)
    s = re.sub(r"\bbuna\b(?!-n)", "buna-n", s)
    s = re.sub(r"[^\w\s/.\-]", " ", s)
    s = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", s)  # word hyphens: food-grade -> food grade (keeps 6204-2rs, 1/2-13)
    s = re.sub(r"(\d)\s+(\d+/\d+)", r"\1-\2", s)  # mixed fractions: 1 1/2 -> 1-1/2
    return " ".join(singular(t) for t in s.split())


def singular(tok: str) -> str:
    """Singularize an alphabetic token (or the last hyphen part): belts -> belt."""
    head, sep, last = tok.rpartition("-")
    if last.isalpha() and len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return head + sep + last


def spec_coverage(query: str, cand: str) -> float:
    """Fraction of digit-bearing query tokens found as whole tokens in the candidate."""
    toks = [t.strip(".-") for t in query.split() if re.search(r"\d", t)]
    toks = [t for t in toks if t]
    if not toks:
        return 1.0
    found = sum(bool(re.search(r"(?<![\w/])(?<!\d\.)" + re.escape(t) + r"(?![\w/])(?!\.\d)", cand)) for t in toks)
    return found / len(toks)


def resolve_target(pn: str, cat: Catalog, m: Match) -> bool:
    """Follow discontinued->replaced_by; False if the product is missing."""
    prod = cat.products.get(pn)
    if prod is None:
        m.flags.append("xref_target_missing")
        return False
    m.part = pn
    if prod["status"] == "discontinued" and prod["replaced_by"] in cat.products:
        m.part = prod["replaced_by"]
        m.flags.append("discontinued_replaced")
        m.conf = min(m.conf, 0.85)
        m.note = f"{pn} is discontinued, replaced by {m.part}"
    return True


def match_ridgeline(ln: ExtractedLine, cat: Catalog, m: Match) -> bool:
    for src in (ln.part_number or "", ln.as_written):
        hit = RL_RE.search(src)
        if not hit:
            continue
        pn = hit.group(0).upper()
        if pn in cat.products:
            m.method, m.conf = "ridgeline_part_number", 0.99
            return resolve_target(pn, cat, m)
        m.flags.append("unknown_ridgeline_pn")
    return False


def match_xref(ln: ExtractedLine, cat: Catalog, m: Match) -> bool:
    cands = [t for src in (ln.part_number or "", ln.as_written) for t in re.split(r"[\s,;()]+", src)]
    cands = [c for c in cands if COMP_RE.fullmatch(c) and not RL_RE.fullmatch(c)]
    if ln.part_number and not RL_RE.search(ln.part_number):
        cands.insert(0, ln.part_number)
    for c in cands:
        rows = cat.xref.get(xref_key(c))
        if not rows:
            continue
        r = rows[0]
        targets = {x["ridgeline_part_number"] for x in rows}
        notes = r["notes"].lower()
        conf = 0.95
        if "close sub" in notes:
            conf = 0.75; m.flags.append("xref_close_substitute")
        elif "pkg qty differs" in notes:
            conf = 0.80; m.flags.append("xref_pkg_qty_differs")
        elif r["notes"]:
            conf = 0.70; m.flags.append("xref_note"); m.note = r["notes"]
        if not r["last_verified"]:
            conf -= 0.05; m.flags.append("xref_unverified")
        if len(targets) > 1:
            conf = 0.5; m.flags.append("xref_conflict")
        m.method, m.conf = "cross_reference", conf
        if not resolve_target(r["ridgeline_part_number"], cat, m):
            m.method, m.conf = None, 0.0
            return False
        if "discontinued_replaced" in m.flags:
            m.note = f"xref -> {r['ridgeline_part_number']} is discontinued, replaced by {m.part}"
        return True
    if ln.part_number and re.match(r"[A-Za-z]", ln.part_number) and not RL_RE.search(ln.part_number):
        m.flags.append("not_in_cross_reference")
    return False


def top_candidates(query: str, cat: Catalog, n: int) -> list[tuple[str, float, float]]:
    """Return (pn, fuzzy, spec_coverage) for the top-n products by fuzzy score."""
    q = norm_desc(query)
    scored = [(pn, fuzz.token_set_ratio(q, d := norm_desc(p["description"])), spec_coverage(q, d))
              for pn, p in cat.products.items()]
    return sorted(scored, key=lambda x: (-x[1], -x[2]))[:n]


def match_description(ln: ExtractedLine, cat: Catalog, m: Match) -> bool:
    query = ln.description or ln.as_written
    ranked = sorted(((0.5 * f / 100 + 0.5 * s, pn, f, s) for pn, f, s in top_candidates(query, cat, 5)), reverse=True)
    score, pn, fuzzy, cov = ranked[0]
    if fuzzy < 60 or cov < 0.99:
        m.flags.append("no_catalog_match")
        return False
    m.part, m.method, m.conf = pn, "description", min(0.92, score)
    if len(ranked) > 1 and score - ranked[1][0] <= 0.03:
        m.conf *= 0.85
        m.flags.append("ambiguous_match")
        m.note = f"close candidates: {pn} and {ranked[1][1]}"
    return True


def match_llm(ln: ExtractedLine, cat: Catalog, m: Match) -> bool:
    """Ask the LLM to choose among top candidates; accept only listed part numbers."""
    query = ln.description or ln.as_written
    cands = [pn for pn, _, _ in top_candidates(query, cat, 8)]
    listing = "\n".join(f"{pn}: {cat.products[pn]['description']}" for pn in cands)
    prompt = (f"Customer requested: {query!r} (as written: {ln.as_written!r}).\nCandidate catalog items:\n{listing}\n"
              "Choose the single candidate that is clearly the same item (all sizes/specs must agree), "
              "or null if none is. Give a short reason.")
    try:
        res = generate_json(prompt, LLMMatch)
    except Exception:  # noqa: BLE001
        return False
    if res.part_number in cands:
        m.part, m.method, m.conf, m.note = res.part_number, "llm", 0.6, res.reason
        m.flags = [f for f in m.flags if f != "no_catalog_match"] + ["llm_match"]
        return True
    return False


def match_line(ln: ExtractedLine, cat: Catalog) -> Match:
    """Run the cascade for one extracted line."""
    m = Match()
    if set(ln.flags) & {"refers_to_previous_order", "ambiguous_item", "unparseable"}:
        return m
    if match_ridgeline(ln, cat, m) or match_xref(ln, cat, m):
        return m
    if match_description(ln, cat, m):
        return m
    if ln.description:
        match_llm(ln, cat, m)
    return m
