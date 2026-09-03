# Spec: Refining existing Beancount transactions

Status: developed.

## Overview

This feature adds a new `refine` CLI subcommand that rewrites one or more existing Beancount transactions (user-identified by file path and 1-based line number, or a range of lines), using all available information from metadata-linked documents (receipt images in PDF/JPG/PNG formats stored alongside the Beancount data), with the ultimate goal of producing a more detailed transaction than the starting one, while preserving all relevant existing details.

The client invokes `beanai.Refine` on the backend (once per target transaction), which takes from the client the existing transaction text and supporting document contents, then produces a rewritten Beancount transaction via LLM according to this specification, then returns it to the frontend for the user to decide whether to merge the change or not.

## How it works

### Client-side: `bean-ai refine` subcommand

The client flow per invocation:

1. User points to one or more existing transactions by file path and 1-based line number (any line within the first transaction), optionally giving a second line number bounding the range (any line within the last transaction)
2. Program extracts the candidate transaction blocks using `split_into_transactions_by_range()` (see below), which flags every transaction that *begins* between the two given lines; the transaction containing `first_line_number` is always included, even when it begins a few lines before it (a transaction is included whole — its body may extend past `last_line_number`), while any transaction that begins after `last_line_number` is left alone. When `last_line_number` is omitted, only the transaction containing `first_line_number` is refined.
3. For each flagged transaction, the program scans its metadata for `document:` keys (including `document2:`, `document3:`, etc.)
4. For each linked document: read the file (client-local) and store in memory
5. Client serializes `{"transaction_text": tx_block_text, "accounts": [...], "documents": [{"filepath": path, "data": base64}, ...]}` as **plain** JSON (not hex) and writes it to the server's stdin over the standard transport (qrexec or subprocess). The `beanai.Refine` command itself carries **no** hex-encoded argument — only stdin is used. A fresh server call is made for **each** transaction being refined.
6. Server invokes LLM with `TRANSACTION_REFINEMENT_PROMPT.md`, producing a rewritten Beancount transaction for the client to read
7. Client validates the rewritten transaction (header + at least two postings), renders a colored unified diff of the *whole file* (reflecting all refinements accepted so far), and prompts the user: `y`es — keep this refinement and move on to the next transaction; `n`o — skip it (transaction left untouched); `p` — preview the first linked document; `q` — abort the run, keeping the refinements already accepted. With `--yes` every refinement is kept without prompting; with `--no` the diffs are shown but the file is left untouched.
8. After all target transactions have been considered, the file is written **once**, if and only if at least one kept refinement differs from the original file contents. With `--clear`, every kept, changed transaction has its flag set to the clear flag (`*`) before the diff is computed.

The client behaves as it does with other account file editing commands (e.g. `associate`): it shows a diff to the user, then asks whether to apply the change to the file or skip it; the user may preview a linked document before deciding.

### Server-side: `beanai.Refine` subcommand

The server's `run()` handler (in `server/commands/refine.py`) performs the refine LLM pass:

1. Reads **plain JSON** from stdin (standard qrexec transport): `transaction_text`, an `accounts` list, and a `documents` list (each with `filepath` and base64 `data`).
2. Loads `TRANSACTION_REFINEMENT_PROMPT.md` at runtime and fills in the `{transaction_text}` and `{accounts}` placeholders; `TRANSACTION_REFINEMENT_PROMPT.md` will be very similar to `RECEIPT_CONVERSION_PROMPT.md`
3. For each document: base64-decode `data` back to raw bytes, then use `file_to_image_parts()` to convert to the appropriate format. The extension is validated: only images (`.jpg`/`.jpeg`/`.png`) and `.pdf` are accepted; anything else is skipped with a warning to stderr and processing continues with the remaining documents. (Note: the existing `file_to_image_parts()` alone would silently fall back to `image/jpeg` for an unknown suffix, so the refine handler performs the extension check before calling it.)
4. Sends combined text prompt + image parts to OpenAI-compatible LLM alongside the original transaction block for context
5. Streams response back to client with reasoning output and final JSON payload containing `transaction` (rewritten Beancount text) and optional `changes_summary`

The rewritten transaction must preserve all existing detail in the original: date, flag, payee, narration, payment accounts, amounts, metadata keys/values, and comments. Only posting-level content (amounts, quantities, expense accounts, narration refinements, detailed and additional line items, additional forms of payment) may be modified or refined, where receipt evidence warrants it and / or the LLM deems that the initial expense accounts are incorrect.

### Client-side: Transaction extraction

The caller identifies one or more target transactions by file path and 1-based line number(s) via CLI positional arguments:

```sh
bean-ai refine Documents/Accounting/00-beancount.bean 42              # a single transaction
bean-ai refine Documents/Accounting/00-beancount.bean 42 200          # every transaction beginning between lines 42 and 200
```

Each line number may point to **any line within a target transaction** (not only the date line); the helper walks back to the transaction start. The client calls `split_into_transactions_by_range(all_lines, first_line_number - 1, last_line_number - 1)` directly to extract the flagged blocks. Each flagged transaction block contains the exact raw text of the transaction (date line, indented postings, and the indented metadata block with `document:`/`documentN:` keys), including inline comments. Comment lines *above* the transaction are not part of the block — a comment preceding the date line travels in the non-transaction group that ends where the transaction starts, so the client sends only the transaction's own lines to the LLM; any comments the LLM adds to its output become part of the replacement block.

The single-transaction convenience wrapper `split_at_transaction_by_line_number()` (`client/beanfiles.py:209`) delegates to `split_into_transactions_by_range()` with `end_line = start_line` and returns `(before, tx_block, after)` — that is, all lines before, inside, and after the one flagged transaction.

### Linked document discovery

The client scans `tx_block` lines for document metadata keys using the single canonical regex `^\s*document(\d*):\s*"([^"]+)"` (colon directly after the key, then a quoted path — matching both Beancount's `document: "path"` and the numbered `documentN:` forms, and consistent with `update_document_metadata()` in `client/beanfiles.py:272`). All `document`, `document2`, … forms are captured. Extracted paths are client-local and resolved as relative to the folder containing the transaction file being read. Missing/unreadable files are a fail-stop error.

### Prompt: TRANSACTION_REFINEMENT_PROMPT.md structure

Base this prompt on `RECEIPT_CONVERSION_PROMPT.md`

The rewrite prompt is organized into these sections:

1. **Introduction**: Role definition — "You are a Beancount double-entry accounting specialist who enhances existing transaction entries using additional receipt information without losing any detail from the original." Plus a description of the two input sources: the original transaction block (date line, indented postings, indented metadata, inline comments) and the supporting documents (receipt images / PDF-rendered pages linked in `document:` metadata)
2. **Preservation rules** (critical section): every field of the original MUST be present in the output, byte-for-byte where not refined — date (never changed), flag (`!` or `*`), payee, narration, all metadata keys/values (including per-leg metadata and all `document:`/`documentN:` lines), and any comments that are part of the block
3. **Modification rules** (only where receipt evidence warrants it):
   - Upgrade narration / payee when the documents provide a clearer or more complete description
   - Adjust posting line amounts/quantities where the receipt shows different values (e.g., correcting a miscategorized or mistyped amount)
   - Add missing posting entries for itemized line items not yet captured, each with a `narration` metadata entry (original language, transcribed exactly — product name and quantity) and an `explanation` metadata entry (English, what the product is), using only accounts from the supplied list
   - Add omitted payment forms / funding legs (cash, card, rebates / discounts)
   - Correct the original expense account when it is clearly wrong in light of the receipt.
   Explicitly forbidden: inventing items / amounts / payment forms, removing information, reordering postings (new ones are appended), and using accounts not in the list
4. **Input sections**: the original transaction under `{transaction_text}` and the account list under `{accounts}`
5. **Output format** (final section): a single JSON object (no Markdown fences) with exactly two keys:
   - `"transaction"` — complete, exact refined Beancount block (full replacement; includes all original comments/metadata)
   - `"changes_summary"` — brief human-readable list of modifications (omitted or empty if no changes)

The LLM does **not** change the transaction flag; the client's `--clear` option is what sets the flag of modified transactions to `*`.

The prompt uses `{transaction_text}` as a placeholder for the original transaction block and `{accounts}` for the account listing (the server fills it in with `json.dumps(request["accounts"])`, exactly as the `beanai.Process` handler fills `{accounts}`). Documents are injected separately as base64 image parts (same mechanism as `beanai.Process`), not as a placeholder. An example rewrite demonstrates adding missing line items, correcting amounts, and preserving the original header/metadata.

## Data structures

### Input to server (`beanai.Refine`)

Input is sent via stdin as a **single plain-JSON object** (not hex, and not an array). The command carries no CLI argument; only stdin is used.

```python
# TypedDicts as defined in beancount_ai/structs.py
class RefineRequestDocument(TypedDict):
    filepath: str        # client-local path, relative to the transaction file's directory
    data: str            # client base64-encodes the raw bytes for JSON transport

class RefineRequest(TypedDict):
    transaction_text: str          # existing full Beancount transaction block (exact original formatting, comments/metadata included)
    accounts: list[str]            # account listing (read by the client from cfg.beancount.account_list_file)
    documents: list[RefineRequestDocument]      # all linked documents attached to the target transaction
```

### Response from server to client:

```python
# Mandatory fields.
class BasicRefineResponse(TypedDict):
    transaction: str            # rewritten Beancount transaction text (full replacement for the targeted block)

# Optional ones.
class RefineResponse(TypedDict, total=False):
    changes_summary: str | None # optional human-readable summary of what was changed/added
```

## Architecture Decision Summary

| Decision | Choice |
|---|---|
| Target specification | File path + 1-based `first_line_number` (+ optional `last_line_number`), passed as 2-3 CLI positional arguments; all transactions *beginning* within the range are refined, one after another |
| Existing transaction extraction | Uses `split_into_transactions_by_range()` to get raw text with all comments/metadata intact |
| Document collection | Client-local documents only (read from disk next to the Beancount file) |
| LLM payload format | Text prompt mode with base64 image parts for each document — identical to the existing receipt processing pipeline (`file_to_image_parts()`) |
| Backend subcommand | Single subcommand `beanai.Refine`, **no CLI argument** (one LLM pass per transaction, not two-pass like `HelpAssociateReceipt`); all input arrives on stdin as plain JSON |
| Output behavior | For each transaction: prints the whole-file diff (cumulative across accepted refinements), asks the user `y`es keep / `n`o skip this one / `p`review document / `q`uit run; with `--clear`, changed accepted transactions get the `*` flag. The file is written once, at the end, only if it changed |
| Metadata preservation | Prompt instructs LLM to preserve all fields; client validates output has a date+flag header and at least two postings. Any original non-doc metadata must remain in the returned block. |

## CLI: `bean-ai refine` subcommand

### Arguments

```sh
bean-ai refine <file_path> <first_line_number> [last_line_number] [--yes | --no] [--clear]
```

| Positional arg | Meaning |
|---|---|
| `<file_path>` | Path to the Beancount file containing the target transaction(s) |
| `<first_line_number>` | 1-based line number of **any line within** the first target transaction (not necessarily the date line), within that file (e.g., `42`) |
| `last_line_number` (optional) | 1-based line number of **any line within** the last target transaction. When present, every transaction that *begins* on a line between `first_line_number` and `last_line_number` (inclusive) is refined, one after another. A transaction that starts before the range (but extends into it) is included whole; one that starts after it is not. When omitted, only the transaction containing `first_line_number` is refined. |

| Flag | Meaning |
|---|---|
| `--yes` | Apply all refinements without confirmation. |
| `--no` | Do all the work (fetch documents, call the LLM, show the diff) but do not touch any file. |
| `--clear`, `-c` | Set the flag of every accepted, changed transaction to the clear flag (`*`) before the final write. |

### Exit codes

- `0`: file updated successfully with each refinement the user kept (individually, or all of them with `--yes`), or left untouched when nothing was kept (`--no`, all transactions skipped, no changes produced)
- Non-zero: error encountered — missing file, line out of range, unreadable document, server failure, LLM call failure, malformed LLM output

### Example output

```diff
--- Changes ---
- 2026-03-15 * "Coop"
+ 2026-03-15 * "Coop Supermarket"
    Narration: Groceries
+   Expenses:Food:Groceries       38.25 CHF
+   Expenses:Household:Snacks     12.75 CHF
-   Expenses:Food               45.00 CHF
-   Assets:Cash                 -45.00 CHF
+   Assets:Cash:CHF              -58.25 CHF
```

(With `--clear`, the flag of each changed transaction is additionally rewritten to `*` before this diff is computed.)

The client code must under no circumstances modify data other than the specific lines of the transactions being refined.  Fortunately the helper function `split_into_transactions_by_range()` goes a long way to help with that: the file is rebuilt by substituting the flagged blocks into the classified block list, so every line that belongs to a non-transaction block or to a transaction outside the range is carried over byte-for-byte.


## Server-side flow (`beanai.Refine`)

### Transport and input

The server receives the `beanai.Refine` subcommand via qrexec or subprocess transport, **with no CLI argument** (unlike `beanai.Process`/`beanai.HelpAssociateReceipt`, which pass a hex-encoded filename as an argument — the handler must therefore not reference `args.filename`). All payload data arrives on stdin as **plain JSON** (a single object — matching the transport convention where only the command argument, not stdin, is hex-encoded):

```python
request_data = json.loads(sys.stdin.read())  # {"transaction_text": "...", "accounts": [...], "documents": [{"filepath": "...", "data": "<base64>"}, ...]}
```

### Processing steps

1. Validate input: reject if the request is not a JSON object or `transaction_text` is missing/empty (responds with stderr `error:...` + `sys.exit(1)`)
2. Extract `transaction_text`, `accounts`, and `documents` from the request
3. For each document in `documents`:
   - Validate the extension against the supported set (`.jpg`, `.jpeg`, `.png`, `.pdf`); any other extension is skipped after emitting a warning to stderr (`warning: unsupported document format, skipping: <ext>`), and processing continues with the remaining documents
   - Base64-decode `data` back to raw bytes
   - Call `file_to_image_parts(filepath, raw)` (this internally calls `render_pdf_pages_to_png()` for PDFs, and builds an `image_url` part for JPG/PNG)
4. Load `TRANSACTION_REFINEMENT_PROMPT.md` at runtime, fill `{transaction_text}` and `{accounts}` placeholders (`accounts` is `json.dumps(request["accounts"])`)
5. Send to LLM alongside text prompt + all image parts
6. Stream response back to client via JSONL (reasoning chunks + output chunks + a `finish` marker), the same protocol as `stream_reasoning_and_output()`

## Client-side flow (`bean-ai refine`)

### Detailed steps in `run(cfg, args)` (`client/commands/refine.py`)

*There used to be pseudocode here, but it's no longer necessary since this is already implemented in `beancount_ai/client/commands/refine.py:run()`

Notes on the operation of the function:

- The per-transaction diff is computed against the *current* state of `blocks` (i.e., the original file plus all refinements kept so far), so consecutive prompts show the cumulative effect of the accepted changes.
- `--clear` rewrites the transaction flag to `*` **before** the diff is computed, so the user sees the flag change in the diff they are deciding on. It is applied only when the transaction actually changed.
- `n` skips only the current transaction and moves on to the next one; `q` (or EOF) ends the run, but the file is still written with the refinements accepted up to that point.

### Helper: `split_into_transactions_by_range(tx_lines, start_line, end_line=None)`

The general-purpose building block that `split_at_transaction_by_line_number()` (in `client/beanfiles.py:209`) delegates to. It classifies a Beancount document, over a requested line range, into a list of `(is_transaction, lines)` tuples (a `True` group runs of transaction lines, a `False` group runs of everything else), preserving the original line ordering and text exactly (flattening the groups back together reproduces the input byte-for-byte).

Arguments:
- `tx_lines` — the document as a list of lines, keeping the line endings present in the source file.
- `start_line` — zero-based index of the first line to consider. If it points into the middle of a transaction, the helper walks *backwards* to the transaction's start line and includes that whole transaction.
- `end_line` — zero-based index of the last line at which a transaction may *begin*. A transaction starting at or before this index is included in whole (its body may run past the index); a transaction that begins after it is not flagged. When omitted, `end_line` defaults to `start_line`, so only the single transaction containing `start_line` is flagged and later transactions are left out.

It raises `ValueError` for out-of-range or inverted ranges (`start_line`/`end_line` `< 0`, `>= len(tx_lines)`, or `end_line < start_line`), or when `tx_lines` is empty. Comments (indented or not) are not treated as part of a transaction except indented comment lines that sit between the transaction's date line and its last posting — those travel with the transaction.

`run()` (in `client/commands/refine.py`) calls this function directly to refine several transactions across a line range in one pass instead of wrapping a single transaction; `split_at_transaction_by_line_number()` calls it with `end_line=None` for the single-transaction case.

### Helper: `extract_document_paths(tx_block: list[str]) -> list[str]`

Scans lines of the transaction for document metadata entries matching the single canonical regex `^\s*document(\d*):\s*"([^"]+)"` (capture group 1 is the optional numeric suffix, group 2 is the quoted path). Returns the extracted paths as a deduplicated list preserving first-seen order. This matches Beancount's `document: "path"` and the numbered `documentN:` forms, and is consistent with the key form handled by `update_document_metadata()` in `client/beanfiles.py:272`.

### Helper: `resolve_local_document_path(doc_path: str, tx_file: Path) -> Path`

Resolves a `document:` value to a client-local path: the path is interpreted as relative to the folder containing the transaction file (`os.path.join(tx_file.parent, doc_path)`); an absolute path joins to itself. The caller reads the resolved path and, on `FileNotFoundError`, prints an error and exits 1.

## Edge cases handled in code

### Client-side:

| Scenario | Client behavior |
|---|---|
| Missing file / file does not exist | Prints `Error: file not found: <path>` to stderr, exits 1 |
| Line number out of range (first/last line number 0, or past the line count) | Prints the `ValueError` from `split_into_transactions_by_range()` (e.g. `starting line number 9 cannot be greater than the supplied number of lines 9`) to stderr, exits 1 |
| `first_line_number` points at a line that is not within any transaction (e.g. a header comment) | `split_into_transactions_by_range()` returns blocks with nothing flagged; no transaction is refined, the file is left untouched, exit 0 |
| LLM returns a block with no valid header or fewer than two postings | Prints `Error: LLM returned a malformed transaction ...` plus the raw output to stderr, exits 1 |
| No linked documents in the extracted block | Not an error — `documents` is empty and the LLM works from the transaction text only |
| Linked document does not exist / cannot be read | Prints `Error: linked document not found: <path>` to stderr, exits 1 |
| Server returns non-zero exit code | Raises `CalledProcessError`, which propagates as an error exit |
| LLM output is not valid JSON, or lacks the `"transaction"` key | Prints the full raw output to stderr for debugging, exits 1 (same as `process` flow) |
| User answers `n` to a prompt | The current transaction is skipped (left untouched); the run continues with the next transaction in the range, and the file is written at the end with any earlier accepted refinements |
| User answers `q` to a prompt, or stdin hits EOF | The run stops immediately; refinements already accepted before that point are still written to the file, exit 0 |

### Server-side:

| Scenario | Server behavior |
|---|---|
| Input is not a JSON object, or `transaction_text` is missing/empty/malformed | Responds with stderr `error: Invalid request: missing transaction_text` then exits code 1 |
| Document has an unsupported extension (anything other than `.jpg`/`.jpeg`/`.png`/`.pdf`) | Emits `warning: unsupported document format, skipping: <ext>` to stderr; the document is ignored and processing continues with the remaining documents |
| LLM call fails (network/auth/model error) | Emits JSON error line to stdout + `sys.exit(1)` (same as existing handlers) |
| Empty document list after processing | No images sent — LLM only uses text prompt + original transaction block |
| Transaction is malformed | LLM needs to decide what to do on its own |
| Render failure on any PDF | Emits `error: ...` message to stderr + `sys.exit(1)` (same pattern as the existing `beanai.Process` handler) |

### Prompt-side limitations:

- Rewritten transaction is generally limited to the account listing supplied in the prompt
- LLM cannot fetch additional receipts beyond those linked in metadata; it works only with provided documents
- All linked documents are client-local (read from disk next to the Beancount data); if a `document:` path points to a file that is not present locally, the command fails with a clear "linked document not found" error

---

## Comparison: refine vs existing subcommands

| Aspect | Ingest (`bean-ai ingest`) | Associate (`bean-ai associate`) | Refine (`bean-ai refine`, this spec) |
|---|---|---|---|
| Target | **New** transaction (created from scratch) | Existing, matched by date/amount from LLM ranking | **Existing**, specified by file path + line number, or a range of line numbers covering several transactions |
| User input | Receipt filename(s) on server | Receipt filename(s) on server | Beancount file path + `first_line_number` [+ `last_line_number`] |
| Document source | Uningested receipts (WebDAV) | Unassociated receipts (WebDAV) | Already linked to the target transaction(s), **client-local** files on disk |
| Metadata changes | Inserts single `document:` entry | Renames existing docs, new doc = `document:` | **None** to the `document:` keys — those metadata lines are preserved unchanged |
| Modifies Beancount file | Appends a new entry to ingestion destination path | Edits source file in-place (adds document metadata) | **Yes** — rewrites only the refined transactions' blocks in their source file, in a single write at the end; per-transaction `n` skips that transaction only, `q`/EOF stops the run but writes the refinements accepted so far. `--no` leaves the file untouched. No file is modified outside the refined blocks' lines. |
| Receipt lifecycle post-success | Removes receipt from WebDAV `uningested` | Removes receipt from `unassociated` on success | **Receipts untouched** — linked files are read-only inputs and remain on disk |
| LLM passes | Single pass (`beanai.Process`) | Two passes (`HelpAssociateReceipt`: info + match) | One `beanai.Refine` pass per target transaction |
| Output destination | Writes to ingestion file, receipt to organized folder | Edits Beancount source, writes receipt to organized folder | Per-transaction colored diff (cumulative view); file updated once at the end if anything changed |

---

## File additions and modifications

### New files

| File | Purpose |
|---|---|
| `beancount_ai/server/TRANSACTION_REFINEMENT_PROMPT.md` | LLM prompt for refining transactions (preservation rules, modification instructions, output format examples) |
| `beancount_ai/structs.py` | Shared `RefineRequest` / `RefineRequestDocument` TypedDicts used by the client to build the refine payload |

### Modified files

| File | Changes |
|---|---|
| `beancount_ai/server/commands/refine.py` | Add `run()` handler and `TRANSACTION_REFINEMENT_PROMPT_PATH` constant; register `beanai.Refine` subcommand via `subcommand_parser()` (**with no positional argument**) and in the `dispatch` table |
| `beancount_ai/client/commands/refine.py` | Add `run()` handler (runs a `do_refine_one` pass per flagged transaction: doc discovery → account-list read → plain-JSON stdin server call → validate → diff → interactive keep/skip, writing the file once at the end), helpers `validate_refined_transaction()` and `preview_local_document()`, and `subcommand_parser()` registering `bean-ai refine` with argparse entries for `<file_path>`, `<first_line_number>`, `[last_line_number]` (positional) and `--yes/--no` / `--clear` |
| `beancount_ai/client/beanfiles.py` | Raw-file helpers used by the command: `split_into_transactions_by_range()` (general transaction/non-transaction classifier over a line range), `split_at_transaction_by_line_number()` (thin single-transaction wrapper over it), `extract_document_paths()`, and `resolve_local_document_path()` |
| `beancount_ai/client/cli.py` | Register `bean-ai refine` in `build_parser()` via `commands.refine.subcommand_parser()` and `run` in the client `dispatch` dict |

### Implementation order (proposed)

1. Write `TRANSACTION_REFINEMENT_PROMPT.md` — define preservation rules, modification instructions, and example rewrites first (with `{transaction_text}` and `{accounts}` placeholders, mirroring `RECEIPT_CONVERSION_PROMPT.md`)
2. Server-side: implement the `beanai.Refine` handler (`run()` in `server/commands/refine.py`) — read plain-JSON request from stdin, validate `transaction_text` (and each document's `filepath`/`data`), extension-check (warn + skip) + base64-decode + `file_to_image_parts()` each document, fill prompt placeholders, LLM call, stream output; register `beanai.Refine` (no argument) in `build_parser()` and `dispatch`
3. Client-side helpers: `split_into_transactions_by_range()` (classify a file into transaction / non-transaction groups over a line range), `extract_document_paths()` (scan tx metadata for `document:`/`documentN:`) and `resolve_local_document_path()` (resolve relative to the tx file's directory) — all in `client/beanfiles.py`
4. Client-side `run()` wiring (`client/commands/refine.py`): file read → line-range validation → tx block extraction → per-transaction loop (doc discovery → read account list → plain-JSON stdin server call → parse + validate → reassemble + diff → interactive keep/skip) → single write at the end
5. Client CLI arg parser entry in `build_parser()` with positional + optional args
6. Register new subcommand in client's dispatch dict
7. Add tests: unit tests for `extract_document_paths()` and `resolve_local_document_path()`, doctests for line-range validation, mock LLM response handling
8. Update all relevant documentation to cover the new feature.

---

(End of file)
