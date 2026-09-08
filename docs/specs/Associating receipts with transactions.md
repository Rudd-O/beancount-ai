# Spec: Associating receipts with transactions

Status: developed.

## Overview

This feature adds a new `associate` CLI subcommand that pairs receipt files already on the server (in the "unassociated" WebDAV folder) with existing Beancount transactions on the client. The matching intelligence comes from the LLM (which evaluates transaction candidates against the parsed receipt), while the client handles Beancount file parsing, candidate fetching, metadata insertion, and receipt organization.

## Architecture Decision Summary (based on your answers)

| Decision | Choice |
|---|---|
| Matching scope | **Both sides** — client fetches candidates via `beancount.loader`; server LLM evaluates them |
| Existing `document:` tag | **Replace**, preserving old values under renumbered `documentN:` (`document2`, `document3`) metadata keys — newest doc is always `document:` |
| Date leeway | **-1 / +45 days** (1 day before receipt date, 45 days after — accounts for late-arriving receipts) |
| Candidate scope | **All transactions within the ± window** sent to LLM for ranking via `beancount.loader` + `printer.format_entry` |
| Ambiguous results | **Error out** (dead-code exists to present ranked list and ask user to pick, but currently disabled) |

## How it works now

### Server-side: `beanhand.HelpAssociateReceipt` (single subcommand)

The AI server uses a **single** `beanhand.HelpAssociateReceipt` subcommand (no CLI argument; the handler lives in `beanhand/server/ai/commands/associate.py`) that does two LLM passes sequentially, with the receipt relayed inline by the client:

1. **Receipt info pass**: reads the receipt from the **first** stdin line (`AssociateRequest.deserialize(sys.stdin.readline())`), and uses `RECEIPT_INFO_PROMPT.md` with the receipt image to extract `{date, amount}`. The prompt instructs the LLM to look at both the receipt content and file name for the date; if ambiguous, it omits the field. It streams the result to the client as JSONL; the client writes the candidates only after this first pass completes.
2. **Candidate reading**: reads the candidates JSON from the **second** stdin line with `readline()` then `json.loads()`. Injection prevention: the parsed list is round-tripped through `json.dumps`/`json.loads` (normalizing escapes) before use, and the re-serialized JSON is what reaches the prompt — never raw text.
3. **Matching pass**: uses `RECEIPT_MATCH_PROMPT.md` with the same receipt image + candidate list. The prompt scores candidates on exact amount match (highest weight), payee/narration keywords, and crediting account consistency. Scores: >= 0.9 is a true match, 0.6–0.8 similar, < 0.4 unrelated. It streams the structured match results back to the client as JSONL.

### Client-side: `beanhand associate` subcommand

The client command flow (`run()`, with its inner `do_associate_one()`, in `beanhand/client/commands/associate.py`):

1. Lists unassociated receipts from the documents server (or accepts specific filenames, which must all exist).
2. Fetches the receipt once via `documents_client.fetch_receipt(receipt)` (needed for the LLM relay and later for the organized copy).
3. Starts the association with `ai_vm.help_associate_receipt(receipt, fetched)`, which relays the receipt inline as the **first** stdin line and returns a raw `(cmd, proc, stdin, stdout)`; the client reads the streamed first-pass result (receipt `{date, amount}`) and prints it.
4. If a date was found, queries Beancount for candidates within -1/+45 days (`load_transaction_contexts`); a missing date is a hard error (`Date for receipt could not be deduced.`).
5. Writes the candidates JSON as the **second** stdin line (after closing it), then reads the second-pass match results (JSON with `matches` list and `ambiguous` flag).
6. **Ambiguity check**: if `ambiguous=true` or the top score is `< 0.8`, raises an exception and aborts (no user prompt — the ranked-list picker code is dead/stubbed out behind it).
7. Resolves the selected transaction's source file and line number from the match result (a line-number mismatch that fits 0 or >1 transactions is an error).
8. Reads the transaction's source file and snapshots it with `FileGuard.take`, computes a description (narration unless `EFT payment`, else payee; the receipt amount appended when present), predicts the receipt destination path, and updates document metadata via `update_document_metadata()` — newest receipt path becomes `document:`, existing ones renumbered to `document2:`, `document3:`, etc.
9. Shows a unified diff, prompts interactively (y/n/p/q, or `--yes`/`--no`), re-verifies the content fingerprint (refusing to write if the file changed on disk), saves the (already-fetched) receipt into `<beancount_folder>/<account_with_slashes>/` with a date-prefixed filename, writes the metadata edit to the transaction file, and finally removes the original receipt from the server.

### Beancount candidate loading (`beancount_loader.py`)

Uses **`beancount.loader.load_file`** (not manual parsing). The spec originally considered manual regex parsing but chose `python-beancount` instead. Key components (lives at `beanhand/client/beancount_loader.py`):

- `load_transactions()`: Loads entries via `loader.load_file()`, filters transactions by date range (inclusive on both ends), and extracts payee/narration/postings/paid amounts, plus the source `filename` and `lineno` from each entry's `meta`.
- `_find_paying_posting()`: Identifies the crediting (credit) posting — prefers the credit with the largest absolute amount, and falls back to a single positive expense leg or the sum of the positive expense legs (with a `?` / `MULTI` currency marker when they differ).
- `load_transaction_contexts()`: Wraps `load_transactions` plus `printer.format_entry()` to get the original Beancount text of each candidate. It temporarily removes `"meta"` from `printer.EntryPrinter.META_IGNORE` so the printed entry keeps its other metadata while suppressing the internal `filename`/`lineno` field, making the candidate text clean for the LLM (the file/line are retained separately on the `CandidateContext` struct).

### Data structures

```python
@dataclass(frozen=True)
class TransactionInfo:
    file_path: str        # Beancount file
    line_no: int          # 1-based line in source file
    date: date
    payee: str | None
    narration: str | None
    paid_amount: float | None   # total paid (from credit posting)
    paid_currency: str | None
    crediting_account: str      # e.g. "Assets:Cash"
    accounts: set[str]          # all posting accounts

@dataclass
class CandidateContext:
    date_str: str           # YYYY-MM-DD for LLM
    payee: str | None
    narration: str | None
    paid_amount: float | None
    paid_currency: str | None
    crediting_account: str
    source_file: str
    line_no: int
    transaction_text: str   # full printer.format_entry output
```

### Metadata insertion (`update_document_metadata` in `beanhand/client/beanfiles.py:428`)

- First existing doc entry (scanning from the date/payee line) is placed as `document:` (newest).
- All existing doc entries renumbered sequentially as `document2:`, `document3:`, … (old numbering ignored — every prior doc preserved regardless of its original key name).
- Existing non-doc metadata lines are not touched.
- Blank lines in the metadata block stop the scan.

### Date range

Current code uses **-1 day before** to **+45 days after** receipt date (`beanhand/client/commands/associate.py:100-103`). The +45 window accounts for receipts paid up to a month later (late payments, delayed entries). This differs from the spec's original plan of ±2 days.

### Ambiguity handling

Current behavior: **hard error** if `ambiguous=true` or `top_score < 0.8`:

```
sorry, matches are ambiguous, cannot proceed; list of matches:<matches>
```

The interactive candidate selection code (presentation + user input loop) exists in the source but is dead/stubbed out behind the `raise Exception(...)` in the ambiguity branch (`beanhand/client/commands/associate.py:145-191`) — the un-reachable block after the exception is commented as "This is dead code for now, but we will enable it in the future."

### CLI flags

- `--yes` / `-y`: Make changes without confirmation (answer "yes" to every prompt)
- `--no`: Show diff of changes but don't write any files
- No filename argument: process all unassociated receipts batch-mode (skips individual prompts when `--yes` given)

### File organization

Receipt destination path uses `predict_receipt_destination_path()`: format is `<beancount_folder>/<account_with_slashes_replaced_by_>/YYYY-MM-DD.<description — original_filename>`. The receipt folder is created with `mkdir(parents=True, exist_ok=True)`. Filenames are shortened to fit filesystem name limits via `shorten_fn()`.

## Files (matches implementation now)

| File | Purpose |
|---|---|
| `beanhand/client/beancount_loader.py` | Loads Beancount via `beancount.loader`; defines `TransactionInfo` + `CandidateContext` and `load_transactions()` / `load_transaction_contexts()` |
| `beanhand/server/ai/RECEIPT_INFO_PROMPT.md` | Slimmed-down LLM prompt for date/amount extraction (not full transaction generation) |
| `beanhand/server/ai/RECEIPT_MATCH_PROMPT.md` | Short LLM prompt (~20 lines) for candidate ranking/matching |
| `beanhand/client/commands/associate.py` | `associate` subcommand (`run()` + inner `do_associate_one()`): relay, candidate usage, metadata update, receipt organization |
| `beanhand/client/beanfiles.py` | `update_document_metadata()` (doc-metadata renumbering/insertion) used by the command |
| `beanhand/client/server/ai.py` | `AIClient.help_associate_receipt()` — relays the receipt as the first stdin line and returns the proc handles |
| `beanhand/server/ai/commands/associate.py` | `beanhand.HelpAssociateReceipt` handler (`run()`): two LLM passes (info + match), stdin candidate reading |

## Implementation order (actual, not planned)

The implementation was completed in this order:

1. `RECEIPT_INFO_PROMPT.md` — extract date/amount from receipt image
2. `RECEIPT_MATCH_PROMPT.md` — slimmed-down match prompt (reduced from ~50-100 lines to ~20 lines)
3. `beancount_loader.py` — Beancount candidate loading (chose `beancount.loader` over manual regex)
4. `run()` handler in `beanhand/server/ai/commands/associate.py` — single subcommand combining info + match passes
5. `run()` / `do_associate_one()` in `beanhand/client/commands/associate.py` — wiring the relay, candidates flow, metadata update, receipt organization
6. Ambiguous result handling stubbed out (not yet enabled)

## Edge cases handled in code

- **No date in receipt**: `assert 0, "Date for receipt could not be deduced."` — aborts this receipt.
- **No candidates within date range**: `load_transaction_contexts` returns an empty context list; an empty candidate array is sent, so the LLM typically reports no matches → the client prints `No valid matches found for receipt <fn>.` for that receipt.
- **Match result resolves to no / multiple transactions**: Raises an exception (a `line_no`/`source_file` combination must match exactly one candidate).
- **Missing source file**: Raises an exception rather than silently failing ("Warning: Transaction source file ... does not exist. Cannot update metadata.").
- **Line number exceeds file length**: Explicit check before the update, raises an exception.
- **Multiple credit postings**: `_find_paying_posting()` picks the one with largest absolute value.
- **Existing `document:` tag**: All prior documents preserved and renumbered; new one becomes `document:`.
- **Multi-line narration / special chars in payee**: Handled by `beancount.loader` parser — no manual parsing needed.

## Notes on current limitations

- Ambiguous matching (user-pick) is stubbed out, not yet enabled. All ambiguous matches fail silently except error output.
- No retry mechanism for failed associates within a batch (current receipts skip but error propagates without `--yes`).
- The `-1/+45` date window is hardcoded, not configurable via CLI or config.
- Metadata insertion does not handle comment lines between the date line and metadata block specially — it inserts after the first non-doc non-blank line after the date line.
