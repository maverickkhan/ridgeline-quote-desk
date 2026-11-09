"""Gemini structured extraction, validation, grounding, customer lookup, cached LLM helper."""
import hashlib
import json
import os
import re
import time
from typing import Optional, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel
from rapidfuzz import fuzz

from .catalog import Catalog
from .config import CACHE_DIR, FALLBACK_MODEL, MODEL
from .models import ExtractedRFQ

T = TypeVar("T", bound=BaseModel)

PROMPT = """You extract requested line items from a customer RFQ (request for quote) sent to an industrial supplier.
Rules:
- Extract only requested items. non_item_rows: only operational notes a quote specialist must see (delivery
  instructions, deadlines, substitution permission, ship-to). Exclude greetings, sign-offs, signatures, phone numbers,
  sender names, and document headers/column headers.
- Never invent a quantity or part number. Copy as_written exactly from the source text.
- A vague quantity ("a few boxes") -> quantity null and flag ambiguous_quantity.
- A reference to a previous order that is not visible in the text -> flag refers_to_previous_order.
- Where an item is too vague to identify -> flag ambiguous_item. Unreadable -> unparseable.
- In forwarded/replied chains the final request is the latest instruction in the thread: apply the changes
  (quantities, specs), add flag modified_in_thread and say what changed in note; as_written = the original item line text.
- Put each item on its own line in order of appearance. If an item refers to another ('same as above but 1/2"'),
  resolve it in description and add context_reference_resolved.
- part_number: any part number written (Ridgeline RLxxxxxx or a competitor/supplier number).
- description: plain-words item description that KEEPS every spec, size, grade, model and standard number written
  for the item (e.g. '6204-2RS', 'A42', '1/2-13 x 2 in grade 8'), but excludes quantity and unit words.
- customer_name: the requesting ORGANIZATION (company/district name, from letterhead, title or signature), not a person's name;
  customer_email: the sender's email address if shown.
RFQ text follows.
-----
"""


def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def generate_json(prompt: str, schema: Type[T]) -> T:
    """Call Gemini with a structured schema, disk cache, retries and model fallback."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((MODEL + schema.__name__ + prompt).encode()).hexdigest()
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        return schema.model_validate_json(cache.read_text())
    client, last = _client(), None
    for model in (MODEL, FALLBACK_MODEL):
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                                       response_schema=schema))
                out = schema.model_validate(json.loads(resp.text))
                cache.write_text(out.model_dump_json())
                return out
            except Exception as e:  # noqa: BLE001 - retry on any API/parse error
                last = e
                time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM failed: {type(last).__name__}")


def clean_as_written(s: str) -> str:
    """Strip leading bullets, replace the reader's cell separator, collapse whitespace."""
    s = re.sub(r"^[\s\-*•]+", "", s).replace(" | ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    s = re.sub(r"[-*•]", " ", s.lower().replace("|", " "))
    return re.sub(r"\s+", " ", s).strip()


def is_grounded(as_written: str, source_norm: str) -> bool:
    """as_written must appear in the source (substring or fuzzy >= 90)."""
    a = _norm(as_written)
    return bool(a) and (a in source_norm or fuzz.partial_ratio(a, source_norm) >= 90)


def validate(rfq: ExtractedRFQ, source: str) -> ExtractedRFQ:
    """Code-side checks: quantity sanity and grounding."""
    src = _norm(source)
    for ln in rfq.lines:
        ln.as_written = clean_as_written(ln.as_written)
        if ln.quantity is not None and ln.quantity <= 0:
            ln.quantity = None
            ln.flags.append("invalid_quantity")
        if not is_grounded(ln.as_written, src):
            ln.flags.append("not_grounded")
    return rfq


def extract(text: str) -> ExtractedRFQ:
    """Extract and validate lines from RFQ text."""
    return validate(generate_json(PROMPT + text, ExtractedRFQ), text)


def lookup_customer(name: Optional[str], email: Optional[str], cat: Catalog) -> tuple[Optional[str], Optional[str]]:
    """Return (customer_id, canonical name) via fuzzy name, then email-domain stem."""
    if name:
        best = max(cat.customers, key=lambda c: fuzz.token_set_ratio(name.lower(), c["name"].lower()))
        if fuzz.token_set_ratio(name.lower(), best["name"].lower()) >= 85:
            return best["customer_id"], best["name"]
    if email and "@" in email:
        stem = email.split("@", 1)[1].split(".")[0].lower()
        best = max(cat.customers, key=lambda c: fuzz.partial_ratio(stem, c["name"].lower().replace(" ", "")))
        if fuzz.partial_ratio(stem, best["name"].lower().replace(" ", "")) >= 80:
            return best["customer_id"], best["name"]
    return None, None
