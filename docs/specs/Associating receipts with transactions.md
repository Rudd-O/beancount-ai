# Plan: Associating receipts with transactions

## Overview

This feature adds a new `associate` CLI subcommand that pairs receipt files already on the server with existing Beancount transactions on the client. The matching intelligence comes from the LLM (which evaluates transaction candidates against the parsed receipt), while the client handles Beancount file parsing, candidate fetching, and metadata insertion.

## Architecture Decision Summary (based on your answers)

| Decision | Choice |
|---|---|
| Matching scope | **Both sides** — client fetches candidates via Beancount parser; server LLM evaluates them |
| Existing `document:` tag | **Replace**, but preserve original value under new `import_source:` metadata key |
| Date leeway | **±2 days** |
| Candidate scope | **All transactions within ±2 days** sent to LLM for ranking |
| Ambiguous results | **Present ranked list, ask user to pick** |

## Phase 1: Server-side receipt date & amount extraction

**New server subcommand: `beanai.ReceiptInfo <hex_filename>`** (or extend the existing process contract)

- The server already extracts date/amount from receipts during LLM processing. We need to add a lightweight way for the client to get just these two fields without the full transaction generation.
- **Option A (preferred):** Add a new `beanai.ReceiptInfo` subcommand that uses a slimmed-down LLM prompt to output `{date: "YYYY-MM-DD", amount: "...", currency: "CHF"}` — minimal tokens, fast response.
- **Option B:** Use the existing `beanai.Process` output's parsed JSON for both date and amount extraction (the transaction text has a date line), avoiding a second LLM call. This is simpler but requires more client-side parsing.

**Recommendation: Use Option A.** The LLM is already invoked to extract date/amount from the receipt image, so we can use that output's parsed JSON rather than writing fragile parser logic on the client. We create a separate slimmed-down prompt (or reuse with different instructions) to minimize cost/latency.

Actually — wait. Re-reading the flow more carefully:

- The receipt is already processed by `beanai.Process` in the existing pipeline, which outputs JSON with `transaction` and `payment_accounts`.
- We **can** extract the date (line 1 parsing) and amounts (individual ledger legs) from that output without any extra LLM call.
- But we need a clean date + total amount pair. The receipt's *total paid* is what matters for matching, not the individual legs.

**Final recommendation:** Extract date and total amount from the existing `beanai.Process` JSON output (the transaction text). The LLM already produces the parsed data; no new server work needed beyond what's already described in the spec ("Process the receipt to identify its date and payment amount").

Phase 1 revised: The `associate` command invokes `beanai.Process` as normal to get the Beancount transaction + payment accounts from the LLM. From this same output, extract:
- **Receipt date**: from line 1 of the transaction (YYYY-MM-DD)
- **Total receipt amount**: sum of all expense leg amounts  
These two values come "for free" from the existing processing pipeline — no extra server work needed beyond what's already described in the spec.

## Phase 2: Client-side candidate fetching (Beancount parsing)

**New client module: `beancount_ai/client/beancount_parser.py`**

Creates a new Python module that parses the relevant Beancount files to find transaction candidates within ±2 days of the receipt date.

```python
class TransactionCandidate(NamedTuple):
    file_path: Path          # Which .beancount file this transaction is in
    file_line: int           # Line number where the transaction starts
    date: str                # YYYY-MM-DD
    payee: str               # e.g., "Coop"
    narration: str           # e.g., "Groceries"
    amounts: list[tuple[str, str]]  # [(amount_str, currency), ...] or similar
    full_text: str           # The original transaction text
    existing_document: str | None  # Existing document: tag value (if any)
```

The parser needs to:

1. **Read the Beancount main file** referenced in config (`beancount_main_file`)
2. **Resolve `include` directives** to get all transaction files
3. **Parse transactions** from those files — extract date, payee, narration, amounts, existing metadata
4. **Filter candidates to ±2 days from receipt date**
5. **Format candidates for LLM** — construct a concise representation of each candidate (date, payee, narration, total amount, file path, line number) that will be passed to the server

**Parser approach:** Use Python's Beancount parser (`python-beancount`) if available, or manual regex-based parsing. Since `python-beancount` has dependency weight, **manual parsing is preferred** — Beancount transaction format is simple enough: date line starts with year, followed by indented ledger lines. Manual parsing avoids adding a new dependency and can be lightweight (just extract what we need).

## Phase 3: Server-side candidate evaluation via LLM

**New server endpoint or contract update:** The candidate list from the client needs to reach the LLM for evaluation.

- **Client constructs a prompt message** containing the receipt info + all candidates in a structured format.
- **Two options for getting this evaluated by LLM:**
  
  - **Option A (preferred):** Add a new server subcommand `beanai.MatchCandidates` that takes: (1) receipt image bytes, (2) candidate list as JSON. The LLM evaluates which candidate matches the receipt and outputs `{best_match_index: N, confidence_score: X.Y, top_k: [{index, score}, ...]}`.
  
  - **Option B:** Create a new slimmed-down processing prompt that instructs the LLM to compare the receipt against candidate transactions rather than generate new ones.

**Recommendation: Option A — a dedicated subcommand `beanai.MatchCandidates`.** This is cleaner because:
- It has its own focused prompt (much shorter than the full receipt conversion prompt)
- It takes receipt image + candidates JSON as input
- Returns structured match results

The `MatchCandidates` prompt is short and cheap (~50–100 lines). Example structure:
```
You are a Beancount expert. 
Here is a receipt (image). Here are N candidate transactions from the ledger.
For each candidate, you get: [date, payee, narration, total_amount, file_context]

Compare the receipt against each candidate and rank them in order of likelihood.
Return JSON: {matches: [{index, score, reason}, ...]}
```

## Phase 4: Client-side transaction editing and metadata insertion

After receiving match results from the server LLM:

**If unambiguous (top-scored candidate clearly wins):**
1. Locate the candidate transaction in its `.beancount` file
2. Find or insert `document:` metadata line after the date line
3. **Replace existing `document:` value with receipt path, preserve old value under `import_source:` metadata key**

**If ambiguous (tight scores):**
1. Present candidates to user ranked by likelihood:
   ```
   Candidate 1 (score: 0.92): 2026-07-15 — "Coop" — "Groceries" — CHF 34.50
   Candidate 2 (score: 0.68): 2026-07-14 — "Migros" — "Food" — CHF 34.50
   Candidate 3 (score: 0.41): 2026-07-16 — "Coop City" — "Snacks" — CHF 38.90
   
   Which should be associated with this receipt? (enter number, or r to retry)
   ```

2. Insert `document:` tag for the selected candidate

**Metadata insertion logic:**

```
Before (transaction already has document tag):
  2026-01-15 ! "Coop" "Groceries"
    document: "/path/to/import-data.csv"     <-- old value
  
After:
  2026-01-15 ! "Coop" "Groceries"
    document: "/home/user/beancount/.../2026-01-15.Coop-Groceries.jpg"
    import_source: "/path/to/import-data.csv"  <-- preserved old value
```

If no `document:` tag exists: insert `document:` after the date line.
If no existing transaction text has `import_source:` metadata: just insert normally.

The receipt organization flow remains identical to the current `organize` command — file is placed in `<beancount_folder>/(account_with_slashes)/<filename>` format.

## Detailed command flow for `associate <filename>`

```
1. Client calls beanai.Process <filename> (server-side LLM processing)
   -> Returns: {transaction, payment_accounts}
   
2. Client extracts from transaction text:
   - receipt_date = YYYY-MM-DD from line 1
   - total_amount = sum of expense leg amounts
   
3. Client loads Beancount files, parses all transactions
4. Client filters candidates to date ∈ [receipt_date ± 2 days]
5. Client formats candidates as list for LLM scoring
   
6. Client calls beanai.MatchCandidates <hex_filename> <candidates_json_base64>
   (server receives receipt image + candidates, passes to LLM)
   
7. Server LLM returns {matches: [{index, score, reason}, ...]}
8. Client receives match results
   
9. If len(matches) == 1 or top_score >> second_score: auto-select top
   Else present ranked list to user for selection
   
10. Client: call beanai.Fetch <filename> to download receipt bytes
    (already have these from step 1 via the LLM processing, so may skip fetch)
    -> Actually, step 1 processes the receipt via LLM but doesn't return raw receipt bytes.
       The import pipeline uses explicit fetch for this reason. So yes, need a fetch.
       
11. Client determines receipt destination path (same logic as current organize command)
12. Client: writes receipt to disk at organized path
13. Client: edits Beancount file — inserts/document metadata
    - If existing document: tag found → preserve under import_source: key
    - Insert/replace document: with new receipt path
    
14. Success confirmation to user
```

## File additions and modifications

**New files:**

| File | Purpose |
|---|---|
| `beancount_ai/client/beancount_parser.py` | Parse Beancount transaction files, extract candidates |
| `beancount_ai/server/RECEIPT_MATCH_PROMT.md` | Slimmed-down LLM prompt for candidate matching |

**Modified files:**

| File | Changes |
|---|---|
| `beancount_ai/client/cli.py` | New `associate` subcommand, Beancount parsing integration, candidate formatting, metadata editing logic |
| `beancount_ai/server/cli.py` | New `do_match_candidates` handler (or inline the prompt) |

**Prompt content:** The match prompt can be either a separate file or embedded in code. Given its small size (~30-40 lines), **embedding in server code as a string constant** is cleaner and avoids a second file to manage (the existing `RECEIPT_CONVERSION_PROMPT.md` exists because of its length at 146 lines).

## Metadata insertion edge cases to handle

1. **Multi-line transactions with existing metadata:** Search for first metadata line after the date/payee line, insert before it
2. **Transactions with flags/commodities:** Date line may include flags (`!`) or commodities — still parse correctly
3. **Comments between date line and metadata:** Insert `document:` after the last comment that's part of the directive line, not in the middle of blank lines
4. **File encoding:** Use UTF-8 for all file reads/writes (consistent with existing code)

## Batch mode (`associate --batch <filename>`)

Same as interactive but auto-selects top-ranked candidate without prompting user if scores are close. Still produces warning output indicating ambiguity was resolved automatically.

## Testing notes

- The manual Beancount parser needs parsing tests for: standard date lines, payees with special chars, multi-line narration, existing metadata blocks, comment lines
- End-to-end testing requires qrexec connection or `target_vm: null` fallback mode
- Edge case: zero candidates within ±2 days → inform user (may need to widen scope)

## Implementation order recommendation

1. **Phase 2 first** — The Beancount parser is foundational and can be unit-tested independently
2. **Phase 3 next** — Server-side match subcommand with slimmed-down prompt
3. **Phase 4 last** — Client editing logic, tied together with the existing `organize` and `fetch` primitives

