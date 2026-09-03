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

### Server-side: `beanai.HelpAssociateReceipt` (single subcommand)

The server uses a **single** subcommand `beanai.HelpAssociateReceipt` that does two LLM passes sequentially:

1. **Receipt info pass**: Uses `RECEIPT_INFO_PROMPT.md` with the receipt image to extract `{date, amount}`. The prompt instructs the LLM to look at both the receipt content and file name for date; if ambiguous, it omits the field.
2. **Candidate reading**: Reads candidates JSON from **stdin** (the client writes them before the second pass).
3. **Matching pass**: Uses `RECEIPT_MATCH_PROMPT.md` with the same receipt image + candidate list. The prompt scores candidates on exact amount match (highest weight), payee/narration keywords, and crediting account consistency. Scores: >= 0.9 is true match, 0.6–0.8 similar, < 0.4 unrelated.

Candidates are passed via stdin as hex-encoded JSON (standard qrexec transport). Server reads them with `sys.stdin.read()` then calls `json.loads()`. Injection prevention is done by parsing JSON strictly (no raw text interpolation into the prompt — candidates are serialized to JSON string and inserted via `.format()` into a fixed template).

### Client-side: `bean-ai associate` subcommand

The client command flow (`run()`, with its inner `do_associate_one()`, in `client/commands/associate.py`):

1. Lists unassociated receipts from server (or accepts specific filenames)
2. For each receipt, calls `vm.help_associate_receipt(receipt)` which returns a raw `(cmd, proc, stdin, stdout)`
3. Writes candidates JSON to the server process's stdin after parsing receipt date/amount from the first LLM pass
4. Reads match results (JSON with `matches` list and `ambiguous` flag)
5. **Ambiguity check**: If `ambiguous=true` or `top_score < 0.8`, raises an exception and aborts (no user prompt — candidate presentation code is dead/stubbed out)
6. Selected transaction's source file and line number are resolved from the match result
7. Document metadata updated via `update_document_metadata()` — newest receipt path becomes `document:`, existing ones renumbered to `document2:`, `document3:`, etc.
8. Receipt downloaded via `beanai.Fetch` and organized into `<beancount_folder>/<account>/` with date-prefixed filename
9. Original receipt removed from WebDAV `unassociated` folder after all writes succeed

### Beancount candidate loading (`beancount_loader.py`)

Uses **`beancount.loader.load_file`** (not manual parsing). The spec originally considered manual regex parsing but chose `python-beancount` instead. Key components:

- `load_transactions()`: Loads entries via `loader.load_file()`, filters by date range, extracts payee/narration/postings/paid amounts using attribute access.
- `_find_paying_posting()`: Identifies the crediting (credit) posting — prefers largest negative amount across all credits, falls back to single positive expense leg or sum of multiple expense legs.
- `load_transaction_contexts()`: Wraps `load_transactions` plus `printer.format_entry()` to get original Beancount text for each candidate (with metadata preserved). Uses `EntryPrinter.META_IGNORE` manipulation to include the `meta` field in output so filename/lineno are visible.

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

### Metadata insertion (`update_document_metadata` in `client/beanfiles.py:272`)

- First existing doc entry (scanning from the date/payee line) is placed as `document:` (newest).
- All existing doc entries renumbered sequentially as `document2:`, `document3:`, … (old numbering ignored — every prior doc preserved regardless of its original key name).
- Existing non-doc metadata lines are not touched.
- Blank lines in the metadata block stop the scan.

### Date range

Current code uses **-1 day before** to **+45 days after** receipt date (`client/commands/associate.py:90-93`). The +45 window accounts for receipts paid up to a month later (late payments, delayed entries). This differs from the spec's original plan of ±2 days.

### Ambiguity handling

Current behavior: **hard error** if `ambiguous=true` or `top_score < 0.8`:

```
sorry, matches are ambiguous, cannot proceed; list of matches:<matches>
```

The interactive candidate selection code (presentation + user input loop) exists in the source but is dead/stubbed out behind the exception/`return` in the ambiguity branch (`client/commands/associate.py:134-181`) — commented as "will enable it in the future."

### CLI flags

- `--yes` / `-y`: Make changes without confirmation (answer "yes" to every prompt)
- `--no`: Show diff of changes but don't write any files
- No filename argument: process all unassociated receipts batch-mode (skips individual prompts when `--yes` given)

### File organization

Receipt destination path uses `predict_receipt_destination_path()`: format is `<beancount_folder>/<account_with_slashes_replaced_by_>/YYYY-MM-DD.<description — original_filename>`. The receipt folder is created with `mkdir(parents=True, exist_ok=True)`. Filenames are shortened to fit filesystem name limits via `shorten_fn()`.

## File additions and modifications (matches implementation now)

**Files that exist:**

| File | Purpose |
|---|---|
| `beancount_ai/client/beancount_loader.py` | Loads Beancount via `beancount.loader`, extracts TransactionInfo + CandidateContext structs |
| `beancount_ai/server/RECEIPT_INFO_PROMPT.md` | Slimmed-down LLM prompt for date/amount extraction (not full transaction generation) |
| `beancount_ai/server/RECEIPT_MATCH_PROMPT.md` | Short LLM prompt (~20 lines) for candidate ranking/matching |

**Modified files:**

| File | Changes |
|---|---|
| `beancount_ai/client/commands/associate.py` | `associate` subcommand (`run()` + inner `do_associate_one()`) flow logic: candidate usage, metadata update, receipt organization |
| `beancount_ai/client/beanfiles.py` | `update_document_metadata()` (doc-metadata renumbering/insertion) used by the command |
| `beancount_ai/client/beancount_loader.py` | `load_transactions()` / `load_transaction_contexts()` client-side usage |
| `beancount_ai/server/commands/associate.py` | `beanai.HelpAssociateReceipt` handler (`run()`): two LLM passes (info + match), stdin candidate reading |

## Implementation order (actual, not planned)

The implementation was completed in this order:

1. `RECEIPT_INFO_PROMPT.md` — extract date/amount from receipt image
2. `RECEIPT_MATCH_PROMPT.md` — slimmed-down match prompt (reduced from ~50-100 lines to ~20 lines)
3. `beancount_loader.py` — Beancount candidate loading (chose `beancount.loader` over manual regex)
4. `run()` handler in `server/commands/associate.py` — single subcommand combining info + match passes
5. `run()` / `do_associate_one()` in `client/commands/associate.py` — wiring candidates flow, metadata update, receipt organization
6. Ambiguous result handling stubbed out (not yet enabled)

## Edge cases handled in code

- **No date in receipt**: Error out ("Date for receipt could not be deduced") — aborts this receipt.
- **No candidates within date range**: Produces empty matches list → LLM reports `ambiguous: true` with no valid matches. User gets the generic error message.
- **Missing source file**: Raises Exception rather than silently failing ("Warning: Transaction source file does not exist").
- **Line number exceeds file length**: Explicit check before update, raises Exception.
- **Multiple credit postings**: `_find_paying_posting()` picks the one with largest absolute value.
- **Existing `document:` tag**: All prior documents preserved and renumbered; new one becomes `document:`.
- **Multi-line narration / special chars in payee**: Handled by `beancount.loader` parser — no manual parsing needed.

## Notes on current limitations

- Ambiguous matching (user-pick) is stubbed out, not yet enabled. All ambiguous matches fail silently except error output.
- No retry mechanism for failed associates within a batch (current receipts skip but error propagates without `--yes`).
- The `-1/+45` date window is hardcoded, not configurable via CLI or config.
- Metadata insertion does not handle comment lines between the date line and metadata block specially — it inserts after the first non-doc non-blank line after the date line.
