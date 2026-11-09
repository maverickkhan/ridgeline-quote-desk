"""Pydantic models: LLM extraction schema and output lines."""
from typing import Optional

from pydantic import BaseModel, Field


class ExtractedLine(BaseModel):
    as_written: str = Field(description="Exact text of the item as it appears in the source; copy, do not rewrite. "
                            "For a table row use part number + description cells as they appear. "
                            "For a PDF table row with a supplier part # column, use the supplier part # text.")
    quantity: Optional[int] = Field(default=None, description="Requested quantity, null if absent or vague")
    unit_raw: Optional[str] = Field(default=None, description="Unit exactly as written, e.g. ea, boxes, PAIL, cases")
    part_number: Optional[str] = Field(default=None, description="Any part number written: Ridgeline RLxxxxxx or competitor")
    description: Optional[str] = Field(default=None, description="Item description in plain words, references resolved, "
                                       "e.g. 'same as above but 1/2\"' -> '1/2 in brass ball valve full port'")
    flags: list[str] = Field(default_factory=list, description="Subset of: refers_to_previous_order, ambiguous_quantity, "
                             "ambiguous_item, context_reference_resolved, modified_in_thread, unparseable")
    note: Optional[str] = None


class ExtractedRFQ(BaseModel):
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    non_item_rows: list[str] = Field(default_factory=list)
    lines: list[ExtractedLine] = Field(default_factory=list)


class LLMMatch(BaseModel):
    part_number: Optional[str] = None
    reason: str = ""


class OutLine(BaseModel):
    line: int
    as_written: str
    quantity: Optional[int] = None
    unit: Optional[str] = None
    ridgeline_part_number: Optional[str] = None
    match_method: Optional[str] = None
    confidence: float = 0.0
    needs_review: bool = True
    flags: list[str] = Field(default_factory=list)
    note: Optional[str] = None
