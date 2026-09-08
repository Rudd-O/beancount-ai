# Spec: Ingesting receipts and creating transactions

Status: developed.

## Overview

This feature adds the `ingest` CLI subcommand that processes new receipts from the documents server's "uningested" category, creates new Beancount transactions via LLM, files organized receipt images under `<beancount_folder>/<account_with_slashes>/`, appends the transaction to the ingestion file, and removes the processed receipt from the server.

## Core flow per receipt

```
1. List uningested receipts from the documents server (the "uningested" category)
2. For each receipt (interactive or batch):
    a. Fetch receipt bytes from the documents server (beanhand.Fetch)
    b. Relay the receipt to the AI server (inline, base64, over its stdin) and process it via the LLM to produce a Beancount transaction + payment accounts (beanhand.Process)
   c. Predict receipt destination path under <beancount_folder>/<account>/
   d. Format transaction with document: metadata pointing to receipt file
   e. Apply user confirmation (y/n/p/q in interactive mode)
   f. If confirmed:
      - Append formatted transaction to ingestion_destination_path
      - Write receipt bytes to predicted path
      - Remove receipt from server
   g. Preview receipt image before committing (interactive "p" command)
```

## Receipt categories

The documents server maintains two separate receipt categories, addressed through a backend-agnostic store (a WebDAV folder pair or two local folders, selected by the `documents` config section) and tracked via `DocumentsClient.list_receipts()`:

| Category | List subcommand | Purpose |
|---|---|---|
| **Uningested** | `beanhand.ListUningested` | New receipts waiting to be imported as new transactions |
| **Unassociated** | `beanhand.ListUnassociated` | Receipts already ingested but not linked to existing transactions |

The `ingest` command only operates on `uningested` receipts. The `associate` command operates on `unassociated` receipts. Fetch and remove try the uningested location first and fall back to the unassociated one. This separation is enforced by distinct list subcommands and the `list_receipts("uningested")` / `list_receipts("unassociated")` parameter.

## Server-side: `beanhand.Process` subcommand

The AI server's `run()` handler (in `beanhand/server/ai/commands/process.py`) performs a **single LLM pass** using `RECEIPT_CONVERSION_PROMPT.md`. It never touches the receipt storage: the whole request arrives on stdin as a single plain-JSON object (`{"accounts": [...], "receipt": {"filename": ..., "content": <base64>}}`), relayed by the client from the documents server.

1. Reads the **entire** stdin as one JSON object and deserializes it via `ProcessRequest.deserialize` (which validates the shape). Malformed input is a fail-stop: `error: {e}` on stderr, exit 1.
2. Loads `RECEIPT_CONVERSION_PROMPT.md` at runtime and injects the account list: `prompt_text.format(accounts=json.dumps(preq.accounts, indent=2))`. The `{accounts}` placeholder (line 46 of the prompt, inside a `json` fence) is replaced with the indented JSON array of typed account objects (e.g., `[{"name": "Assets:Cash:CHF", "rule": "..."}, ...]`); validation of that list is documented in `docs/specs/Dynamic account list from Beancount data.md`.
3. Converts the relayed receipt to image parts via `file_to_image_parts(filename, content)` (PDFs page-by-page to PNG; images base64-encoded).
4. Sends the receipt image part(s) + conversion prompt to the OpenAI-compatible LLM, streaming the response.
5. Streams the response back to the client as JSONL (`{"reasoning"}`, `{"output"}`, `{"finish"}` lines). The final `output` payload is the LLM's JSON result (see below).

The LLM prompt instructs the model to:
- Extract date (dd.mm.yy format, European), payee, itemized products, payment methods from the receipt image
- Assign expense accounts from the **client-provided** account list (injected into the `{accounts}` placeholder, lines 42-49 of `RECEIPT_CONVERSION_PROMPT.md`)
- Return JSON with two keys: `transaction` (Beancount-formatted text) and `payment_accounts` (list of debited accounts)

The prompt includes a complete Beancount transaction example showing date format, flag (`!`), payee in double quotes, narration in double quotes, indented posting legs with two-space indent, metadata narration/explanation entries with four-space indent, and negative amounts for payment/income legs.

## Client-side: `beanhand ingest` (`run()` in `beanhand/client/commands/ingest.py`)

### Account list (derived from the ledger)

The client derives the account list from the Beancount ledger at run time (as of today for ingest/import), not from a static file. The `beancount.account_list_file` config key no longer exists: accounts are marked in the ledger with `beanhand-include` / `beanhand-exclude` / `beanhand-rules` metadata on their `open` directives, and an empty derived list is a fail-stop. See `docs/specs/Dynamic account list from Beancount data.md`. `beanhand list-accounts` prints the list the LLM would be offered.

### Receipt enumeration

```python
vm = DocumentsClient.from_cfg(cfg)
receipts = vm.list_receipts("uningested")
```

If `args.filename` is provided, the receipt list is **overridden** — only specified filenames are processed (all must exist on server).

### Per-receipt sub-flow (`do_ingest_one()` inner function)

**Per-receipt flow** (all enclosed in `with tempfile.TemporaryDirectory() as tmpdir`):

Every receipt goes through the same initial steps before any action is taken:

1. **Construct ImportResult unconditionally** — always snapshots the ingestion file, then fetches the receipt and processes it via the LLM. If this fails, the error is wrapped as `Import of {receipt} failed: <e>`: it is caught and printed (and the loop moves on to the next receipt) in batch mode (`--yes`/`--no`), but raised immediately in interactive mode.
2. **Always display diff** — regardless of mode (`--yes`, `--no`, or interactive), the unified diff of the proposed file append is printed.

Then action is determined:

| Flag | Action default | Prompt? |
|---|---|---|
| `--yes` / `-y` | `import` | No (auto-commit) |
| `--no` / `-n` | `draft-import` | No (just show diff) |
| neither | user choice | Yes: `[y/n/p/q]` |

Interactive prompt: `\nImport proposed transaction based on '{receipt}'? [y/n/p/q] ` with options:
- `y`: import this receipt
- `n`: skip (no files written, no rollback needed)
- `p`: preview via `preview_receipt()` (shared tmpdir), re-prompts
- `q`: `sys.exit(0)` immediately (EOF also returns)

If action is `draft-import`: prints `"No files were changed."`, returns.
If action is `import`: calls `imp.commit()`, then `remove.run(...)` (which issues `beanhand.Remove`). A server-side removal failure triggers `imp.rollback()` (safe, because the receipt is still on the server) and is raised as `Could not remove {receipt} from folder: <e>`.

**ImportResult class structure:**

```python
class ImportResult:
    fetched_receipt: FetchedReceipt       # receipt bytes + server mtime (from beanhand.Fetch)
    transaction_text: str                 # beancount tx with document: metadata
    receipt_destination_path: Path        # predicted file path for organized receipt
    ingestion_destination_path: Path      # where to append the transaction text
    rollback_size: int | None = None      # size of ingest file before append (for rollback)
    _ingestion_guard: FileGuard           # content fingerprint of the ingestion file
```

Constructor (`__init__(documents_vm, ai_vm, beancount, filename)`):
1. Snapshots the ingestion file's content with `FileGuard.take(beancount.ingestion_destination_path)` — it **raises** if the ingestion file does not exist (it is a Beancount file, so its absence is a fail-stop error).
2. `documents_vm.fetch_receipt(filename)` — downloads raw bytes (and mtime) via `beanhand.Fetch` into `fetched_receipt`.
3. `ai_vm.process_receipt(filename, fetched_receipt, account_refs_or_die(beancount.main_file, date.today()))` — relays the receipt inline to the AI server, streams the LLM response, and parses the JSON for the transaction text and the first payment account.
4. Strips headline comment lines (lines starting with `;`) from the start of the transaction.
5. Splits the date string (first field before space, parsed as `%Y-%m-%d`) from the rest; builds a description from payee + narration after the flag (stripped of quotes/semicolons).
6. Calls `predict_receipt_destination_path()` to compute the receipt file path.
7. Calls `insert_document_metadata()` to insert `document: "<path>"` after the date line.
8. Stores all computed values as instance attributes.

Diff output (`diff()` method):
- Unified diff between the current ingestion file contents and what would be appended.
- Appended content: `\n` (or `\n\n` if the file does not already end in a newline) + stripped `transaction_text` + trailing `\n`.

Commit (`commit()` method):
1. Re-verifies the ingestion file's content fingerprint (`_ingestion_guard.verify()`); if the file changed on disk since the constructor, it prints an error and exits 1 rather than clobbering the edit.
2. Saves the (already-fetched) receipt to `receipt_destination_path` (`save_receipt`).
3. Appends to the ingestion file: opens in append mode, records the pre-append size as `rollback_size`, writes the formatted transaction, then flushes and `os.fsync()`. If any write fails, calls `rollback()` and re-raises.

Rollback (`rollback()` method):
- Truncates the ingestion file back to `rollback_size` (pre-commit).
- Deletes the receipt file if it exists.
- Both operations are best-effort (errors are printed to stderr); if either fails, the error is aggregated and re-raised.

## Transaction text formatting

### Comment stripping

After processing through `beanhand.Process`, the LLM may return a transaction prefixed with comment lines (used for inlined reasoning). These are stripped:

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

- Account path construction: `beancount_folder / account.replace(":", "/")` (created with `mkdir(parents=True, exist_ok=True)`)
- Filename format: `{YYYY-MM-DD}.{description} — {original_filename}` (when description exists), or `{YYYY-MM-DD}.{filename}` (when no description)
- Forward slashes in the filename replaced with underscores
- Filename shortened to fit filesystem name limits via `shorten_fn()`

### Transaction destination (`ingestion_destination_path`)

Read from Beancount config (`cfg.beancount.ingestion_destination_path`): the `beancount.ingestion_destination_file` (relative to `main_file`) if set, else `main_file` itself. The formatted transaction is **appended** to the end of this file (not inside an existing transaction block). Each import appends `<sep><transaction_text>`, where `<sep>` is a single newline if the file already ends in one and two newlines otherwise, plus a trailing newline.

## CLI: `beanhand ingest` subcommand

### Arguments

```
beanhand ingest [filename ...] [--yes|-y | --no|-n]
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
- Non-zero: Error encountered (unless `--yes`/`--no` in batch mode — then the loop continues processing the remaining receipts, prints a `Summary of errors encountered:` with tracebacks, and exits 1 at the end)

In interactive mode, a single failed receipt raises immediately. In batch mode (`--yes`/`--no`), failures per receipt are collected in the `exceptions` list, the receipt's error is printed with `— continuing to next receipt`, and execution continues.

### Batch loop behavior

```python
with tempfile.TemporaryDirectory() as tmpdir:
    preview_dir = Path(tmpdir)
    exceptions: list[tuple[str, Exception]] = []
    for receipt in receipts:
        try:
            do_ingest_one(receipt, preview_dir)
        except Exception as e:
            exceptions.append((receipt, e))
            if args.yes or args.no:
                print(f"{e} — continuing to next receipt", file=sys.stderr)
            else:
                raise                      # fail immediately in interactive mode
    if exceptions:
        # batch mode only: report and exit non-zero
        print("Summary of errors encountered:", file=sys.stderr)
        ...                                # one traceback per failed receipt
        sys.exit(1)
```

### Preview functionality

When the user presses `p` during interactive prompting, `preview_receipt()` is called:
1. Uses a shared `TemporaryDirectory` preview dir (one per run, held across all preview calls).
2. Saves the (freshly-fetched) receipt into that dir under its original filename.
3. Opens it with `xdg-open` (non-blocking, in a new session).

## Comparison: ingest vs associate

| Aspect | Ingest (`beanhand ingest`) | Associate (`beanhand associate`) |
|---|---|---|
| Receipt category | `uningested` | `unassociated` |
| Transaction source | New transaction created by LLM | Existing transaction found in ledger |
| Metadata on tx | Inserts single `document:` entry | Renames existing docs, new doc = `document:` |
| Matching | N/A (creates new) | LLM ranks candidates within -1/+45 days |
| Date window | Date from receipt image (LLM-extracted) | `-1 day / +45 days` around receipt date |
| Ambiguity handling | N/A | Error on ambiguous match (picker stubbed out) |
| Prompt used | `RECEIPT_CONVERSION_PROMPT.md` | `RECEIPT_INFO_PROMPT.md` + `RECEIPT_MATCH_PROMPT.md` |
| Server subcommand(s) | `beanhand.Process` (single LLM pass) | `beanhand.HelpAssociateReceipt` (two LLM passes) |
| File writes | Appends to ingestion file + writes receipt | Only writes receipt; edits the tx source file in-place |
| Post-success cleanup | Removes receipt from the server | Removes receipt from the server |

## Edge cases handled in code

- **Empty receipt list on server**: Prints `"No receipts to ingest."` to stderr and returns immediately.
- **Requested receipt not on server**: Exits with error code 1 (during the `if fn not in receipts` check).
- **Receipt fetch fails**: The error is wrapped in `Import of {receipt} failed: <e>`; no files are modified for this receipt.
- **No ledger accounts marked `beanhand-include`**: `account_refs_or_die` fails stop with a migration-oriented error (see the dynamic-account-list spec).
- **Payment account missing from LLM output**: `ProcessResponse.deserialize` raises; the full LLM output is attached for debugging.
- **Transaction text empty or malformed after stripping**: No explicit validation beyond building the description — it would fail during date parsing or append.
- **Ingestion file changed on disk**: `commit()` re-verifies the content fingerprint and exits 1 instead of clobbering the edit.
- **Ingestion file missing**: `FileGuard.take` in the constructor raises (it is a Beancount file; absence is an error).
- **Rollback failure**: errors on the rollback are printed to stderr and re-raised; the receipt may remain as an orphan on the local filesystem if both steps fail.
- **Multiple items in `payment_accounts` list**: the client uses only the first element for the description and the receipt organization folder.

## Prompt: RECEIPT_CONVERSION_PROMPT.md structure (52 lines)

The conversion prompt (lives at `beanhand/server/ai/RECEIPT_CONVERSION_PROMPT.md`) is organized into these sections:

1. **Introduction** (lines 1-16): Role definition and a worked Beancount transaction example with inline comments explaining each line of the format.
2. **Extraction instructions** (lines 17-25): what to extract from the receipt image — payee, date (`dd.mm.yy`, European), itemized list, rebates/discounts; ignore the trailing rebates section.
3. **Transaction construction rules** (lines 26-40): how to assemble the transaction — payee, narration, date, expense legs (with per-leg `narration` / `explanation` metadata), payment legs (income / liability / asset accounts), and the two required JSON output keys.
4. **Account list placeholder** (lines 42-49): a short field-explanation note followed by a `json` fence with the `{accounts}` placeholder, filled at runtime with the indented JSON of the ledger-derived account list (typed `{name, rule?}` objects; see the dynamic-account-list spec). The line *"Do not imagine accounts not listed."* (line 49) is the user-facing guard.
5. **Output format** (embedded throughout): JSON with `transaction` and `payment_accounts` keys.
