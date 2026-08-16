# Spec: Ingesting receipts and creating transactions

Status: developed.

## Overview

This feature adds the `ingest` CLI subcommand that processes new receipts from the WebDAV "uningested" folder, creates new Beancount transactions via LLM, files organized receipt images under `<beancount_folder>/<account>/`, appends the transaction to an ingestion file, and removes the processed receipt from the server.

## Core flow per receipt

```
1. List uningested receipts from server (WebDAV "uningested" folder)
2. For each receipt (interactive or batch):
   a. Fetch receipt bytes from server (beanai.Fetch)
   b. Process via LLM to produce Beancount transaction + payment accounts (beanai.Process)
   c. Predict receipt destination path under <beancount_folder>/<account>/
   d. Format transaction with document: metadata pointing to receipt file
   e. Apply user confirmation (y/n/p/q in interactive mode)
   f. If confirmed:
      - Append formatted transaction to ingestion_destination_path
      - Write receipt bytes to predicted path
      - Remove receipt from server
   g. Preview receipt image before committing (interactive "p" command)
```

## WebDAV categories

The server maintains two separate WebDAV folders tracked via `list_receipts()`:

| Category | Folder name (`beanai.List…`) | Purpose |
|---|---|---|
| **Uningested** | `uningested` (via `beanai.ListUningested`) | New receipts waiting to be imported as new transactions |
| **Unassociated** | `unassociated` (via `beanai.ListUnassociated`) | Receipts already ingested but not linked to existing transactions |

The `ingest` command only operates on `uningested` receipts. The `associate` command operates on `unassociated` receipts. This separation is enforced by distinct list subcommands and the `list_receipts("uningested")` / `list_receipts("unassociated")` parameter.

## Server-side: `beanai.Process` subcommand

The server's `do_process()` handler (in `server/cli.py`) performs a **single LLM pass** using `RECEIPT_CONVERSION_PROMPT.md` (~146 lines):

1. Reads receipt image from WebDAV `uningested` folder via `WebDAVClient`.
2. Converts PDF receipts to PNG images via `file_to_image_parts()` (reuses PDF→PNG logic).
3. Sends receipt image + conversion prompt to OpenAI-compatible LLM.
4. Streams response back to client with reasoning output and final JSON payload.

The LLM prompt instructs the model to:
- Extract date (dd.mm.yy format, European), payee, itemized products, payment methods from OCR'd receipt content
- Assign expense accounts from a provided YAML list of ~90 possible `Expenses:*` categories
- Assign funding/payment accounts from a 5-account whitelist (`Assets:Cash:CHF`, `Assets:PostFinance:CHF`, etc.)
- Return JSON with two keys: `transaction` (Beancount-formatted text) and `payment_accounts` (list of debited accounts)

The prompt includes a complete Beancount transaction example showing date format, flag (`!`), payee in double quotes, narration in double quotes, indented posting legs with two-space indent, metadata narration/explanation entries with four-space indent, and negative amounts for payment/income legs.

## Client-side: `do_ingest()` function

### Receipt enumeration

```python
vm = RemoteVM.from_cfg(cfg)
receipts = vm.list_receipts("uningested")
```

If `args.filename` is provided, the receipt list is **overridden** — only specified filenames are processed (all must exist on server).

### Per-receipt sub-flow (`do_ingest()` inner function)

**Per-receipt flow** (all enclosed in `with tempfile.TemporaryDirectory() as tmpdir`):

Every receipt goes through the same initial steps before any action is taken:

1. **Construct ImportResult unconditionally** — always downloads receipt + processes via LLM. If this fails, error propagates immediately (even in batch mode; no skip on parse/Low failure).
2. **Always display diff** — regardless of mode (`--yes`, `--no`, or interactive), the unified diff of the proposed file append is printed to stdout.

Then action is determined:

| Flag | Action default | Prompt? |
|---|---|---|
| `--yes` / `-y` | `import` | No (auto-commit) |
| `--no` / `-n` | `draft-import` | No (just show diff) |
| neither | user choice | Yes: `[y/n/p/q]` |

Interactive prompt: `\nImport proposed transaction based on '{receipt}'? [y/n/p/q] ` with options:
- `y`: import this receipt
- `n`: skip (no files written, no rollback needed)
- `p`: preview via `_preview_receipt()` (shared tmpdir), re-prompts
- `q`: `sys.exit(0)` immediately

If action is `draft-import`: prints `"No files were changed."`, returns.
If action is `import`: calls `imp.commit()`, then `do_remove()`. WebDAV removal failure triggers rollback of both the file append and receipt delete, raised as exception about failed remove.

**ImportResult class structure:**

```python
class ImportResult:
    receipt_data: bytes                   # raw receipt bytes from server
    transaction_text: str                 # beancount tx with document: metadata
    receipt_destination_path: Path        # predicted file path for organized receipt
    ingestion_destination_path: Path      # where to append the transaction text
    rollback_size: int | None = None      # size of ingest file before append (for rollback)
```

Constructor (`__init__`):
1. `vm.fetch_receipt(filename)` — downloads raw bytes via `beanai.Fetch`
2. `vm.process_receipt(filename)` — calls `beanai.Process`, streams LLM response, parses JSON for transaction text and payment accounts list
3. Strips headline comment lines (lines starting with `;`) from the start of the transaction
4. Extracts date string (first field before space) and description (payee + narration after flag, stripped of quotes/semicolons)
5. Calls `predict_receipt_destination_path()` to compute receipt file path
6. Calls `insert_document_metadata()` to add `document: "<path>"` after the date line
7. Stores all computed values as instance attributes

Diff output (`diff()` method):
- Unified diff between current ingest file contents and what would be appended
- Appended content: `\n\n` + stripped transaction_text + trailing `\n`

Commit (`commit()` method):
1. Append to ingestion file: opens in append mode, records original file size as `rollback_size`, writes the new content. If writing receipt bytes fails, calls `rollback()` and re-raises.

Rollback (`rollback()` method):
- Truncates ingest file back to `rollback_size` (pre-commit)
- Deletes receipt file if it exists
- Both operations are best-effort; errors silently ignored with stderr print

## Transaction text formatting

### Comment stripping

After processing through `beanai.Process`, the LLM may return a transaction prefixed with comment lines (used for inlined reasoning). These are stripped:

```python
while beancount_transaction.lstrip().startswith(";"):
    beancount_transaction = "".join(beancount_transaction.splitlines(True)[1:]).lstrip()
```

### Description construction for receipt filename

Used to generate a descriptive prefix in the organized filename:
```python
reststr = (
    reststr.splitlines()[0][2:]               # after date+flag, skip flag char
        .replace('" "', " — ")                 # join payee and narration with em-dash
        .replace('"', "")                       # remove remaining quote marks
        .split(";")[0]                          # strip any trailing comment
        .strip()
)
```

Example output: `"Coop Supermarket — Groceries and snacks"`

### Document metadata insertion (`insert_document_metadata`)

Simple implementation (simpler than the `update_document_metadata()` used by associate):

```python
def insert_document_metadata(transaction_text: str, file_path: str) -> str:
    lines = transaction_text.splitlines(True)  # preserve line endings
    if not lines or lines[0].strip().startswith("#"):
        return transaction_text
    stripped = lines[1].lstrip()
    indent = lines[1][: len(lines[1]) - len(stripped)]
    lines.insert(1, '{}document: "{}"\n'.format(indent, file_path.replace('"', '\\"')))
    return "".join(lines)
```

Inserts `document:` as the **first** metadata line after the date/payee line. Unlike associate's `update_document_metadata()`, this version does not handle existing document keys — it simply inserts one entry (the ingest flow creates brand-new transactions, so no prior documents exist).

## File organization

### Receipt destination path (`predict_receipt_destination_path`)

```python
def predict_receipt_destination_path(
    beancount_folder: Path,
    transaction_date: date,
    filename: str,
    account: str,            # e.g. "Assets:Cash:CHF"
    description: str | None = None,
) -> Path:
```

- Account path construction: `beancount_folder / account.replace(":", "/")`
- Filename format: `{YYYY-MM-DD}.{description} — {original_filename}` (when description exists), or `{YYYY-MM-DD}.{filename}` (when no description)
- Forward slashes in filename replaced with underscores
- Filename shortened to fit filesystem name limits via `shorten_fn()`

### Transaction destination (`ingestion_destination_path`)

Read from Beancount config (`cfg.beancount.ingestion_destination_path`). The formatted transaction is **appended** (written to end of file, not a new line in an existing transaction block). Each import adds `\n\n<transaction_text>\n` to the end of this file.

## CLI: `bean-ai ingest` subcommand

### Arguments

```
bean-ai ingest [filename ...] [--yes|-y | --no|-n]
```

| Positional arg | Meaning |
|---|---|
| (none) | Process all uningested receipts from server |
| `<fname> [more...]` | Process only specified filenames (all must exist on server) |

| Flag | Meaning |
|---|---|
| `--yes` / `-y` | Non-interactive: import every receipt without prompting |
| `--no` / `-n` | Show all changes but write no files (like `--dry-run`) |
| (neither, interactive) | Prompt `[y/n/p/q]` for each receipt |

### Exit codes

- `0`: All receipts processed successfully
- Non-zero: Error encountered (unless `--yes`/`--no` in batch mode — then continues processing remaining receipts and exits non-zero at the end)

In interactive mode, a single failed receipt raises immediately. In batch mode (`--yes`/`--no`), failures per receipt are collected via `ee` variable but execution continues.

### Batch loop behavior

```python
for receipt in receipts:
    ee = None
    try:
        do_ingest(receipt)
    except Exception as e:
        ee = e
        if args.yes or args.no:
            continue  # skip to next receipt in batch
        raise  # fail immediately in interactive mode
    if ee is not None:
        raise ee  # signal non-zero exit after loop
```

### Preview functionality

When user presses `p` during interactive prompting, `_preview_receipt()` is called:
1. Creates a `TemporaryDirectory` (shared across all preview calls for the batch)
2. Writes receipt bytes to temp file as `<original_filename>`
3. Opens with `xdg-open` (non-blocking, starts new session)

## Comparison: ingest vs associate

| Aspect | Ingest (`bean-ai ingest`) | Associate (`bean-ai associate`) |
|---|---|---|
| WebDAV folder | `uningested` | `unassociated` |
| Transaction source | New transaction created by LLM | Existing transaction found in ledger |
| Metadata on tx | Inserts single `document:` entry | Renames existing docs, new doc = `document:` |
| Matching | N/A (creates new) | LLM ranks candidates within -1/+45 days |
| Date window | Date from receipt image (LLM-extracted) | `-1 day / +45 days` around receipt date |
| Ambiguity handling | N/A | Error on ambiguous match (prompt stubbed out) |
| Prompt used | `RECEIPT_CONVERSION_PROMPT.md` (~146 lines) | `RECEIPT_INFO_PROMPT.md` + `RECEIPT_MATCH_PROMPT.md` (~25 lines combined) |
| Server subcommand(s) | `beanai.Process` (single LLM pass) | `beanai.HelpAssociateReceipt` (two LLM passes) |
| File writes | Appends to ingest file + writes receipt | Only writes receipt; edits tx source file in-place |
| Post-success cleanup | Removes receipt from WebDAV | Removes receipt from WebDAV |

## Edge cases handled in code

- **Empty receipt list on server**: Prints `"No receipts to ingest."` to stderr and returns immediately.
- **Requested receipt not on server**: Exits with error code 1 (during `if fn not in receipts` check).
- **Receipt fetch fails**: Exception is propagated up; no files are modified for this receipt.
- **Payment account missing from LLM output**: Raises exception with full LLM output attached for debugging.
- **Transaction text empty or malformed after stripping**: No explicit validation — would fail during append but `ImportResult.__init__` wouldn't catch it.
- **Rollback failure on both files**: Errors silently; receipt may exist as orphan on local filesystem if rollback truncation fails.
- **Non-existent ingest file path**: Opens with `"a"` mode — creates the file if absent (no pre-check).
- **Multiple items in payment_accounts list**: `do_process` takes only the first element (`payment_accounts[0]`) for description construction and receipt organization folder naming.

## Prompt: RECEIPT_CONVERSION_PROMPT.md structure (~146 lines)

The conversion prompt is organized into these sections:

1. **Introduction** (lines 1-7): Role definition, Beancount format example with inline comments explaining each line
2. **Extraction instructions** (lines 17-25): What to extract from receipt image — date format, total paid, itemized list, rebates/discounts
3. **Transaction construction rules** (lines 26-40): How to assemble the transaction — payee, narration, date, expense legs, payment legs, account assignment constraints
4. **Expense account whitelist** (lines 42-136): ~90 `Expenses:*` categories as YAML list with inline comments for special cases
5. **Funding account whitelist** (lines 137-144): 5 possible payment/income accounts
6. **Output format** (embedded throughout): JSON with `transaction` and `payment_accounts` keys
