# Ridgeline Quote Desk

Turns incoming RFQs (email text, PDF, Excel, photos) into structured, validated line items matched to Ridgeline part numbers, so a quote specialist reviews a draft instead of re-typing it. A person approves every quote; nothing is sent automatically and the model never produces prices.

## Repository

https://github.com/maverickkhan/ridgeline-quote-desk

## How to Run

```bash
# 1. Environment (Python 3.12)
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt

# 2. Secrets: copy and fill in GEMINI_API_KEY, GEMINI_MODEL=gemini-3.8-flash
cp .env.example .env

# 3. Pipeline: reads data/rfqs/*, writes one draft per RFQ to outputs/<rfq>.json
.venv/bin/python run_pipeline.py                 # all six RFQs
.venv/bin/python run_pipeline.py --only rfq_06_messy

# 4. Evaluation: compares outputs/ to data/answer_key/, prints metrics, writes evaluation/results.json
.venv/bin/python evaluate.py

# 5. Submission check
python check_submission.py
```

LLM responses are cached in `.cache/llm/` (gitignored), so re-runs are fast and reproducible. Delete the folder to force fresh calls.

## Approach

**Pipeline:** `read → extract (LLM) → validate (code) → match (code first) → score → flag → draft JSON`

| Stage | How | Where |
|---|---|---|
| Read | One reader per format turns the file into text: `.txt` as is, PDF via `pdfplumber`, Excel rows via `openpyxl` (`Row N: cell \| cell`). Images are stubbed: whole RFQ flagged `stub`, "photo source: needs OCR or a vision model". | `quote_desk/readers.py` |
| Extract | **Same path for every format.** Gemini returns JSON constrained to a Pydantic schema: `as_written`, quantity, raw unit, part number as written, plain description with references resolved ("same as above but 1/2"" → "1/2 in brass ball valve full port"), and flags such as `refers_to_previous_order`, `ambiguous_quantity` and `modified_in_thread`. Forwarded chains apply the latest instruction (rfq_05: 3 HP → 5 HP, gaskets 20 → 40). Delivery notes go to `non_item_rows`. | `quote_desk/extract.py` |
| Validate | Code, not the model: Pydantic types, quantity > 0, a **grounding check** (each `as_written` must appear in the source text, else `not_grounded`), unit normalisation (ea/boxes/cases → EA/BX/CS; unknown units such as PAIL map to the catalog unit and are flagged), pack-size sanity check (300 with no unit against a "box of 100" product → `pack_size_check`), and customer lookup against `customers.csv` (name, then email domain). | `extract.py`, `pipeline.py` |
| Match | Deterministic cascade with a confidence score per method. **1. Ridgeline PN** (`RL\d{6}` in the catalog) → 0.99. **2. Cross-reference sheet**, normalised key → 0.95, lowered by the sheet's own notes ("close sub" 0.75, "pkg qty differs" 0.80, other notes 0.70, blank `last_verified` −0.05). Discontinued targets follow `replaced_by` and are flagged `discontinued_replaced`. **3. Description match**: rapidfuzz `token_set_ratio` plus a **spec-token guard** (every size or number in the request, such as `6204-2RS`, `1/2-13`, `A42` or `214`, must appear in the catalog description, so 6204 never matches 6205). Close runner-up → `ambiguous_match`. No match → `no_catalog_match`, never "something similar" (rfq_03 chlorine test kit). **4. LLM fallback (optional)**: Gemini picks from the top-8 catalog shortlist or answers none. The pick must be in the shortlist, and confidence is fixed at 0.60 so it always goes to review. | `quote_desk/match.py` |
| Route | `needs_review = confidence < 0.85` or no part number or no quantity, or any risk flag (`discontinued_replaced`, `xref_note`, `ambiguous_match`, `llm_match`, `modified_in_thread`, `pack_size_check`, …). | `quote_desk/config.py` |

**The one decision that matters most:** *the LLM reads, code decides, a human approves.* The model does what rules can't, which is to read any layout (the old RPA bot broke when formats changed). It never has the last word on a part number. Every part number in the output exists in `products.csv` and came from an exact Ridgeline PN, the cross-reference sheet, or a spec-checked description match, with a confidence the code computed rather than one the model reported about itself. Anything uncertain is flagged rather than guessed, because a wrong part on a quote costs far more than a 30-second review.

**What is stubbed and why:**
- **Photo RFQ (rfq_04).** Production would send the image to Gemini's vision input (or OCR, then the same text path) and keep it in review until vision accuracy is measured.
- **Pricing and stock.** These are never touched; production reads them from the ERP after the specialist approves the lines.
- **Review UI.** The JSON draft stands in for it.

**Model provider and region:** Google Gemini `gemini-3.8-flash` (fallback `gemini-2.5-flash`), temperature 0, JSON-schema output. I picked it after probing 11 models from the registry on a sample extraction. It is the newest stable Flash, pinned for reproducible evaluation. This prototype uses the **Gemini Developer API on fictional data only**, and that API does not guarantee US residency (the free tier may use prompts for training). **Production must use Gemini on Vertex AI, pinned to the `us-central1` regional endpoint**, paid tier with no training on customer data, which meets "data must stay in the US". Equivalent option: Claude via AWS Bedrock `us-east-1`.

## Evaluation

`python evaluate.py` scores `outputs/` against the three answer keys (rfq_01 email, rfq_02 PDF, rfq_03 Excel; 19 lines). Only the evaluator reads `data/answer_key/`; the pipeline never does. Lines are paired by line number.

| Metric | Definition | Result |
|---|---|---|
| `extraction_accuracy` | Expected lines whose quantity **and** unit both match | **1.000** |
| `match_precision` | Predicted part numbers that are correct, over all non-null predictions | **1.000** |
| `match_recall` | Expected part numbers we found, over all non-null expected | **1.000** |
| `auto_accept_precision` | Of lines **not** sent to review, the share that are correct. This is the safety metric: a wrong line that skips review can reach a customer. | **1.000** (0 errors) |
| `review_rate` | Share of lines sent to a human | **0.211** (4 of 19; each one is a line the key also marks as risky, or a flagged substitution) |
| `correct_abstention_rate` | Lines the key says must not be matched (previous-order grease, not-carried test kit) that we left null or flagged | **1.000** |
| Others | Quantity, unit and customer accuracy, `as_written` similarity, line-count match, per-RFQ breakdown, full error list, threshold sweep | `evaluation/results.json` |

**Threshold: 0.85.**
- **The keyed data can't decide it.** The sweep shows 0 auto-accepted errors from 0.50 to 0.95, and the same 84% auto-accept rate from 0.75 to 0.85. Nineteen lines can't pick the number by error rate.
- **So I chose it by what the confidence levels mean.** Every match type lands in one of two bands with a clear gap between them:
  - **Safe, 0.88 and above:** exact Ridgeline PN (0.99), clean cross-reference (0.95), unverified cross-reference (0.90), and description match with every spec token present (0.88–0.92).
  - **Risky, 0.80 and below:** "pkg qty differs" (0.80), ambiguous description (0.78), close substitute (0.75), cross-reference with a note (0.70), LLM-only pick (0.60).
- **Why 0.85 and not 0.80.** At 0.80 a "pkg qty differs" line would get through on score alone, and only its flag would catch it. At 0.85 the threshold by itself separates the two bands, and risk flags (discontinued replacement, pack-size check, changed-in-thread) are a second, independent layer.
- **Why not higher.** Above 0.88 we would start sending verified description matches to review, adding workload with no measured gain.
- **Why not lower.** Below 0.80 we would auto-accept substitutes and LLM picks, which have no measured accuracy yet.
- **Recalibration.** With a larger labelled set, re-run the sweep per match method and pick the lowest threshold that keeps auto-accept precision ≥ 99%.

**Growing the labelled evaluation set from Ridgeline's past RFQs and quotes:**
1. **Pair history automatically.** Every archived RFQ email and attachment has a quote that was actually sent (in the ERP). Quote lines give ground truth for part number, quantity and unit, with no manual labelling.
2. **Sample across the variety.** Take about 200 RFQs to start, stratified by format (email, PDF, Excel, photo, forwarded chain), customer, competitor brand, and category. Oversample known hard cases: competitor PNs not in the sheet, discontinued parts, vague quantities.
3. **Keep a frozen test set** (never used for tuning) and a separate dev set for prompt and rule changes.
4. **Turn reviewer corrections into labels.** Once live, every correction (draft vs approved quote) is a labelled example, with priority on lines that were auto-accepted but later changed.
5. **Measure label quality.** Double-label 10% of the set, and add each production miss or new format to the set as a regression test.

## Production

```mermaid
flowchart LR
  A[quotes@ inbox] --> B[Ingest + format router]
  B -->|txt / eml| C1[Text reader]
  B -->|pdf| C2[pdfplumber]
  B -->|xlsx / csv| C3[openpyxl rows]
  B -->|image| C4[Vision / OCR - stub today]
  B -->|unknown format| Q[Review queue: whole RFQ, reason]
  C1 & C2 & C3 & C4 --> D[Gemini structured extraction<br/>Vertex AI us-central1]
  D --> E{Schema + grounding + unit checks}
  E -->|fail after retry| Q
  E -->|ok| F[Matching cascade<br/>RL PN > cross-ref > description > LLM shortlist]
  F --> G[Confidence + flags -> needs_review]
  G --> H[Draft in quote specialist UI]
  Q --> H
  H --> I[Specialist reviews, corrects, approves]
  I --> J[Pricing + stock from ERP]
  J --> K[Quote sent by a person]
  I -->|corrections| L[(Feedback store)]
  L --> M[Cross-ref sheet updates<br/>+ labelled eval set]
  M --> F
  G --> N[Metrics + alerts]
  I --> N
```

**When extraction fails or a new RFQ format appears**
- The reader turns any file into text, so a new layout in a known file type goes through the same LLM path with no new rules. That is the fix for the RPA bot's failure mode.
- An unknown file type, an LLM error after retry plus fallback model, or a schema or grounding failure never drops the RFQ. It goes to the review queue as a whole-RFQ draft with the reason (`extraction_failed`, `not_grounded`), and the specialist types it in as they do today.
- A new format is logged by type and sender. When one recurs, add a reader or a prompt example plus eval cases.

**What to monitor and what triggers an alert**
- Per day: review rate, auto-accept rate, and per-method match mix (exact, cross-ref, description, LLM, none).
- `extraction_failed` rate and `not_grounded` rate.
- LLM latency, errors and cost.
- **Correction rate on auto-accepted lines.** This is the real error rate.
- Alerts:
  - Any auto-accepted line corrected to a different part number (the safety metric; page someone if it exceeds 1% per week).
  - Extraction failure rate above 5% in an hour.
  - Review rate rising more than 15 points versus the 7-day baseline (format drift or a catalog change).
  - The LLM provider error rate.
  - Spend above budget.

**Rough cost and latency per RFQ**
- About 1–3k input and 1–3k output tokens (including thinking) for extraction, plus about 1k per LLM-fallback line.
- At Flash-tier pricing that is roughly **$0.01–0.03 per RFQ**, so **about $5–15 per week** for 400 RFQs. Everything else is local code (fuzzy matching on 40k products takes milliseconds per line; at scale, an index or embeddings).
- Latency is about 15–30 s per RFQ, dominated by the LLM call. It runs asynchronously, so a draft is ready before a specialist opens the email. Compare about 75 minutes of manual work today.

**How reviewer corrections improve the system**
- Each approve or correct action is stored with the draft (field-level diff).
- **Cross-reference sheet:** a specialist confirming a competitor-PN → RL match on a description or LLM line proposes a new cross-reference row (one-click approve). The half-maintained sheet fills itself, and next time the match is exact at 0.95.
- **Evaluation set:** corrections become labelled data, and the threshold and prompts are re-tuned weekly against them. Per-customer aliases ("same as last order") come from order history.
- Extraction mistakes become few-shot examples or eval cases. Nothing retrains silently; every change is gated by the eval suite.

**With two more weeks**
1. Pull 200 historical RFQs with their sent quotes into a real eval set, and calibrate the threshold per match method.
2. Vision extraction for photos and scanned PDFs (Gemini multimodal), behind review.
3. Order-history lookup per customer, to resolve "same as last order" against real past orders.
4. Move to Vertex AI `us-central1`, plus email ingestion (Gmail or Graph webhook) and a minimal review UI with ERP price and stock lookup.
5. Embedding retrieval over all 40k products for the description step, plus a monitoring dashboard and alerts.
