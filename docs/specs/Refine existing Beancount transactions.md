# Spec: Refining existing Beancount transactions

Status: planned.

## Overview

This feature adds a new `refine` CLI subcommand that rewrites an existing Beancount transaction (user-identified by file path and line number), using all available information from metadata-linked documents (receipt images in PDF/JPG/PNG formats stored alongside the Beancount data), with the ultimate goal of producing a more detailed transaction than the starting one, while preserving all relevant existing details.

The client invokes `beanai.Refine` on the backend, which takes from the client the existing transaction text and supporting document contents, then produces a rewritten Beancount transaction via LLM according to this specification, then returns it to the frontend for the user to decide whether to merge the change or not.

## How it works

### Client-side: `bean-ai refine` subcommand

The client flow per invocation:

1. User points to an existing transaction by file path and 1-based line number
2. Program extracts the target transaction block using `split_at_transaction_by_line_number()` (already exists in `client/cli.py:307`)
3. Program scans the target transaction's metadata for `document:` keys (including `document2:`, `document3:`, etc.)
4. For each linked document: read the file and store in memory
5. Client serializes `{"transaction": tx_block_text, "documents": [{"filename": basename(filename), "data": ...}, {...}]}` as hex-encoded JSON and sends to `beanai.Refine` over the standard transport (qrexec or subprocess)
6. Server invokes LLM with `TRANSACTION_REFINEMENT_PROMPT.md`, producing a rewritten Beancount transaction for the client to read
7. Client prints the rewritten transaction to stdout for user review and manual copy/paste

The client behaves as it does with other account file editing commands: by default, it shows a diff to the user, and asks the user whether to preview the first linked document, apply the change, or quit.

### Server-side: `beanai.Refine` subcommand

The server's `do_refine()` handler performs the refine LLM pass:

1. Reads hex-encoded JSON from stdin (standard qrexec transport): `transaction` and `documents` list (each with `filename`, base64 `data`)
2. Loads `TRANSACTION_REFINEMENT_PROMPT.md` at runtime and fills in `{transaction_text}` placeholder; `TRANSACTION_REFINEMENT_PROMPT.md` will be very similar to `RECEIPT_CONVERSION_PROMPT.md`
3. For each document: the server can use function `file_to_image_parts` for each received document to convert to the appropriate format; unsupported formats are a fail-stop error;
4. Sends combined text prompt + image parts to OpenAI-compatible LLM alongside the original transaction block for context
5. Streams response back to client with reasoning output and final JSON payload containing `transaction` (rewritten Beancount text) and optional `changes_summary`

The rewritten transaction must preserve all existing detail in the original: date, flag, payee, narration, payment accounts, amounts, metadata keys/values, and comments. Only posting-level content (amounts, quantities, expense accounts, narration refinements, detailed and additional line items, additional forms of payment) may be modified or refineed, where receipt evidence warrants it and / or the LLM deems that the initial expense accounts are incorrect.

### Client-side: Transaction extraction

The caller identifies a target transaction by file path and 1-based line number via CLI positional arguments:

```sh
bean-ai refine Documents/Accounting/00-beancount.bean 42
```

The client uses the existing `split_at_transaction_by_line_number(row_idx_zero_based, all_lines)` to extract `(before, tx_block, after)`. `tx_block` contains the exact raw text of the transaction including any comments, metadata block, and blank lines that are part of a Beancount entry.

### Linked document discovery

The client scans `tx_block` lines for document metadata keys using the regex pattern `^\s*document(\d*)\s*:`. Extracted paths are always resolved as local relative to the transaction file being read.  Missing files are a fail-stop error.

### Prompt: TRANSACTION_REFINEMENT_PROMPT.md structure

Base this prompt on `RECEIPT_CONVERSION_PROMPT.md`

The rewrite prompt is organized into these sections:

1. **Introduction** (~lines 1-10): Role definition — "You are a Beancount accounting specialist who enhances existing transaction entries using additional receipt information without losing any detail from the original."
2. **Input format** (~lines 19-35): Describes the two input sources — original transaction text and supporting document contents (images/text extracted from receipts)
3. **Preservation rules** (critical section): 
   - Every field of the original MUST be present in the output: date, flag (`!` or `*`), "payee", "narration", all posting accounts and amounts, all non-doc metadata keys/values
   - Do not remove, rename, or reorder any fields — only modify where receipt evidence clearly contradicts or enhances
4. **Modification rules**:
   - Update narration/description if receipt images provide clearer or more complete details
   - Adjust posting line amounts/quantities/item descriptions where the receipt shows different values (e.g., correcting a miscategorized amount)
   - Add missing posting entries from itemized receipts that weren't captured originally
   - Upgrade flag from `*` to `!` only if receipt strongly indicates reconciliation
5. **Output format** (~final 20-30 lines): JSON with two keys:
   - `"transaction"` — complete, exact Beancount block (full replacement; includes all original comments/metadata)
   - `"changes_summary"` — brief human-readable list of modifications (can be omitted/empty if no changes)

The prompt uses `{transaction_text}` placeholder for the original transaction, other placeholders for valid accounts, and injects document contents as base64 image parts (same mechanism as `do_process`). An example rewrite demonstrates adding missing line items, correcting amounts, and preserving original header/metadata.  The prompt will also inject expense and funding accounts.

## Data structures

### Input to server (`beanai.Refine`)

Input is sent via stdin, JSON-encoded.

```python
class Document(TypedDict):
    filepath: str        # basename path on local machine, or WebDAV filename if remote-only
    data: str            # client base64-encodes the bytes data for JSON transport

class RefineRequest(TypedDict):
    transaction_text: str      # existing full Beancount transaction block (exact original formatting, comments/metadata included)
    documents: list[Document]  # all linked documents attached to the target transaction
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
| Target specification | File path + 1-based line number, passed as two CLI positional arguments |
| Existing transaction extraction | Uses existing `split_at_transaction_by_line_number()` to get raw text with all comments/metadata intact |
| Document collection (hybrid) | Always client-local docs read directly |
| LLM payload format | Text prompt mode with base64 image parts for each document — identical to the existing receipt processing pipeline (`file_to_image_parts()`) |
| Backend subcommand | Single subcommand `beanai.Refine` (single LLM pass, not two-pass like `HelpAssociateReceipt`) |
| Output behavior | Prints diff to stdout, asks the user whether to modify file. User can also preview the first linked document prior to further decision. |
| Metadata preservation | Prompt instructs LLM to preserve all fields; client validates output contains date flag, payee and at least two postings. Any original non-doc metadata must remain in the returned block. |

## CLI: `bean-ai refine` subcommand

### Arguments

```sh
bean-ai refine <file_path> <line_number> [--yes | --no]
```

| Positional arg | Meaning |
|---|---|
| `<file_path>` | Path to the Beancount file containing the target transaction |
| `<line_number>` | 1-based line number of the date line within that file (e.g., `42`) |

| Flag | Meaning |
|---|---|
| `--yes` | Make the modification without prompting. |
| `--no` | Just show the diff. |

### Exit codes

- `0`: output displayed and transaction edited successfully if user so requested
- Non-zero: Error encountered — missing file, line out of range, document fetch failure, LLM call failure

### Example output

```diff
--- Changes ---
- 2026-03-15 * "Coop"
+ 2026-03-15 ! "Coop Supermarket"
    Narration: Groceries
+   Expenses:Food:Groceries       38.25 CHF
+   Expenses:Household:Snacks     12.75 CHF
-   Expenses:Food               45.00 CHF
-   Assets:Cash                 -45.00 CHF
+   Assets:Cash:CHF              -58.25 CHF
```

The client code must under no circumstances modify data other than the specific lines of the transaction being refined.  Fortunately the helper function `split_at_transaction_by_line_number()` goes a long way to help with that.


## Server-side flow (`do_refine`)

### Transport and input

The server receives the hex-encoded subcommand `beanai.Refine` via qrexec or subprocess transport. The filename argument is empty/unused. All payload data arrives on stdin as hex-encoded JSON:

```python
request_data = json.loads(sys.stdin.read())  # [{"transaction": "...", "documents": [{...}]}]
```

### Processing steps

1. Validate input: reject if `transaction` is not present in request (responds with stderr `error:...` + `sys.exit(1)`)
2. Extract `{transaction_text}` from request data
3. For each document in `documents`:
   - If PDF: call `render_pdf_pages_to_png()` → base64 image parts
   - If JPG/PNG: create single `image_url` part with appropriate MIME type
4. Load `TRANSACTION_REFINEMENT_PROMPT.md` at runtime, fill `{transaction_text}` and relevant `{accounts}` placeholders
5. Send to LLM alongside text prompt + all image parts
6. Stream response back to client via JSONL (reasoning chunks + final output chunk with JSON)

## Client-side flow (`do_refine`)

### Detailed steps in `do_refine(cfg, args)`

```python
def do_refine(cfg: Configuration, args: argparse.Namespace) -> None:
    # 1. Read file.
    tx_file = Path(args.file_path) # or somesuch

    # 2. Extract transaction block preserving all formatting
    before, tx_block, after = split_at_transaction_by_line_number(args.line_number - 1, all_lines)
    tx_block_str = "".join(tx_block)   # exact original text

    # 3. Find linked documents — scan metadata for 'document:' keys
    doc_paths = extract_document_paths(tx_block)  # helper using regex ^\s*document(\d*)\s*:
    
    # 4. Collect document contents
    documents_data: list[DocumentData] = []
    for doc_path in doc_paths:
        resolved = os.path.join(cfg.beancount.main_folder / doc_path)
        raw = resolved.read_bytes()
        # it is a failure if the document cannot be read
        documents_data.append(DocumentData(filepath=doc_path, data=base64encode(raw)))
    
    # 5. Call server — hex-encoded JSON payload via stdin
    vm = RemoteVM.from_cfg(cfg)
    cmd, proc, stdin, stdout = vm._call("beanai.Refine")
    
    request_payload = {
        "transaction_text": tx_block_str,
        "documents": [{"filepath": d["filepath"], "content_type": d["content_type"],
                       "data": base64.b64encode(d["data"]).decode("ascii")} for d in documents_data],
    }
    stdin.write(json.dumps(request_payload).encode("utf-8"))
    stdin.flush()
    stdin.close()
    
    llm_output = stream_reasoning_and_capture_output(stdout)
    ret = proc.wait()
    if ret != 0:
        print("Error: server returned non-zero exit code", file=sys.stderr)
        sys.exit(1)
    
    # 6. Parse response — strip markdown fences if present, then parse JSON
    llm_output = demarkdownify(llm_output).strip()
    resp = load_json(llm_output)
    rewritten_tx_raw = resp["transaction"]
    
    # Do not strip headline or postfix comments (LLM may prefix with reasoning comments)
    while rewritten_tx_raw.lstrip().startswith(";"):
        rewritten_tx_raw = "".join(rewritten_tx_raw.splitlines(True)[1:]).lstrip()
    rewritten_tx = rewritten_tx_raw.strip() + "\n" # last line will contain a line ending.
    
    # 7. Output
    # ... show diff, prompt user whether to commit / preview receipt / abandon, as in existing commands.
```

### Helper: `extract_document_paths(tx_block: str) -> list[str]`

Scans lines of the transaction for document metadata entries matching `^\s*document(\d*)\s*:\s*"([^"]+)"`. Returns the extracted paths as a deduplicated list preserving first-seen order. This reuses the same regex strategy already present in `update_document_metadata()` in `client/cli.py:405`.

## Edge cases handled in code

### Client-side:

| Scenario | Client behavior |
|---|---|
| Missing file path argument | Prints `"Error: file not found"` to stderr, exits 1 |
| Line number out of range (below 1 or above line count) | Prints clear message with valid range `[1, N]`, exits 1 |
| `split_at_transaction_by_line_number()` raises `ValueError` (line doesn't point to transaction start) | Catches exception, prints error and exits 1 |
| No linked documents in the extracted block | This is not an error (server LLM uses text only) |
| Document does not exist | Error printed to stderr, command exits non-zero |
| LLM output lacks `"transaction"` key | Prints full raw output for debugging, exits 1 (same as `process` flow) |

### Server-side:

| Scenario | Server behavior |
|---|---|
| Input lacks `transaction_text` or is empty/malformed | Responds with stderr `error: Invalid request: missing transaction_text` then exits code 1 |
| LLM call fails (network/auth/model error) | Emits JSON error line to stdout + `sys.exit(1)` (same as existing handlers) |
| Empty document list after processing | No images sent — LLM only uses text prompt + original transaction block |
| Transaction is malformed | LLM needs to decide what to do on its own |
| Render failure on any PDF | Emits `error: ...` message to stderr + `sys.exit(1)` (same pattern as existing `do_process`) |

### Prompt-side limitations:

- Rewritten transaction is generally limited to the account listing supplied in the prompt
- LLM cannot fetch additional receipts beyond those linked in metadata; it works only with provided documents
- If original transaction has `document:` keys pointing to non-localized receipts, the client already fetched them for LLM context before sending — no gap introduced

---

## Comparison: refine vs existing subcommands

| Aspect | Ingest (`bean-ai ingest`) | Associate (`bean-ai associate`) | Refine (`bean-ai refine`, this spec) |
|---|---|---|---|
| Target | **New** transaction (created from scratch) | Existing, matched by date/amount from LLM ranking | **Existing**, specified by file path + line number |
| User input | Receipt filename(s) on server | Receipt filename(s) on server | Beancount file path + line number |
| Document source | Uningested receipts (WebDAV) | Unassociated receipts (WebDAV) | Already linked to the target transaction (local or WebDAV) |
| Metadata changes | Inserts single `document:` entry | Renames existing docs, new doc = `document:` | **None** — read-only, does not modify any files |
| Modifies Beancount file | Appends a new entry to ingestion destination path | Edits source file in-place (adds document metadata) | No file modification at all (print only to stdout or diff) |
| Receipt lifecycle post-success | Removes receipt from WebDAV `uningested` | Removes receipt from `unassociated` on success | **Receipts untouched** — they remain on server and local disk |
| LLM passes | Single pass (`beanai.Process`) | Two passes (`HelpAssociateReceipt`: info + match) | **Single pass** (`beanai.Refine`) |
| Output destination | Writes to ingestion file, receipt to organized folder | Edits Beancount source, writes receipt to organized folder | Stdout (full text) or stderr+stdout (diff with coloring) |

---

## File additions and modifications

### New files

| File | Purpose |
|---|---|
| `beancount_ai/server/TRANSACTION_REFINEMENT_PROMPT.md` | LLM prompt for refining transactions (preservation rules, modification instructions, output format examples) |

### Modified files

| File | Changes |
|---|---|
| `beancount_ai/server/cli.py` | Add `do_refine()` handler; add `TRANSACTION_REFINEMENT_PROMPT_PATH` constant; register `beanai.Refine` in `build_parser()` and `dispatch` table |
| `beancount_ai/client/cli.py` | Add new subcommand (for `do_refine`) in `build_parser()` with argparse entries for `<file_path>`, `<line_number>` (positional) and `--yes/--no`; add helper `extract_document_paths()`; add `do_refine()` function with document discovery + server call + diff/text output logic; register `do_refine` in client `dispatch` dict |

### Implementation order (proposed)

1. Write `TRANSACTION_REFINEMENT_PROMPT.md` — define preservation rules, modification instructions, and example rewrites first
2. Server-side: implement `beanai.Refine()` handler with input validation, document processing, and LLM call; register in `build_parser()` and `dispatch`
3. Client-side helper `extract_document_paths()` to scan tx metadata for linked documents
4. Client-side `do_refine()` function wiring: file read → line validation → tx extraction → doc discovery → server call → output (text or diff)
5. Client CLI arg parser entry in `build_parser()` with positional + optional args
6. Register new subcommand in client's dispatch dict
7. Add tests: unit tests for `extract_document_paths()`, doctests for line-range validation, mock LLM response handling
8. Update all relevant documentation to cover the new feature.

---

(End of file)
