# Spec: Refining existing Beancount transactions

Status: planned.

## Overview

This feature adds a new `refine` CLI subcommand that rewrites an existing Beancount transaction (user-identified by file path and line number), using all available information from metadata-linked documents (receipt images in PDF/JPG/PNG formats stored alongside the Beancount data), with the ultimate goal of producing a more detailed transaction than the starting one, while preserving all relevant existing details.

The client invokes `beanai.Refine` on the backend, which takes from the client the existing transaction text and supporting document contents, then produces a rewritten Beancount transaction via LLM according to this specification, then returns it to the frontend for the user to decide whether to merge the change or not.

## How it works

### Client-side: `bean-ai refine` subcommand

The client flow per invocation:

1. User points to an existing transaction by file path and 1-based line number (any line within the transaction)
2. Program extracts the target transaction block using `split_at_transaction_by_line_number()` (already exists in `client/cli.py:306`)
3. Program scans the target transaction's metadata for `document:` keys (including `document2:`, `document3:`, etc.)
4. For each linked document: read the file (client-local) and store in memory
5. Client serializes `{"transaction_text": tx_block_text, "accounts": [...], "documents": [{"filepath": path, "data": base64}, ...]}` as **plain** JSON (not hex) and writes it to the server's stdin over the standard transport (qrexec or subprocess). The `beanai.Refine` command itself carries **no** hex-encoded argument — only stdin is used.
6. Server invokes LLM with `TRANSACTION_REFINEMENT_PROMPT.md`, producing a rewritten Beancount transaction for the client to read
7. Client shows a colored unified diff of the proposed change and prompts the user to apply it to the file, preview the first linked document, or quit. With `--no` it only shows the diff and leaves the file untouched.

The client behaves as it does with other account file editing commands (e.g. `associate`): by default it shows a diff to the user, then asks whether to apply the change to the file, preview the first linked document, or quit.

### Server-side: `beanai.Refine` subcommand

The server's `do_refine()` handler performs the refine LLM pass:

1. Reads **plain JSON** from stdin (standard qrexec transport): `transaction_text`, an `accounts` list, and a `documents` list (each with `filepath` and base64 `data`).
2. Loads `TRANSACTION_REFINEMENT_PROMPT.md` at runtime and fills in the `{transaction_text}` and `{accounts}` placeholders; `TRANSACTION_REFINEMENT_PROMPT.md` will be very similar to `RECEIPT_CONVERSION_PROMPT.md`
3. For each document: base64-decode `data` back to raw bytes, then use `file_to_image_parts()` to convert to the appropriate format. The extension **is** validated: only images (`.jpg`/`.jpeg`/`.png`) and `.pdf` are accepted; anything else is a fail-stop error. (Note: the existing `file_to_image_parts()` alone would silently fall back to `image/jpeg` for an unknown suffix, so the refine handler performs the extension check before calling it.)
4. Sends combined text prompt + image parts to OpenAI-compatible LLM alongside the original transaction block for context
5. Streams response back to client with reasoning output and final JSON payload containing `transaction` (rewritten Beancount text) and optional `changes_summary`

The rewritten transaction must preserve all existing detail in the original: date, flag, payee, narration, payment accounts, amounts, metadata keys/values, and comments. Only posting-level content (amounts, quantities, expense accounts, narration refinements, detailed and additional line items, additional forms of payment) may be modified or refined, where receipt evidence warrants it and / or the LLM deems that the initial expense accounts are incorrect.

### Client-side: Transaction extraction

The caller identifies a target transaction by file path and 1-based line number via CLI positional arguments:

```sh
bean-ai refine Documents/Accounting/00-beancount.bean 42
```

The line number may point to **any line within the target transaction** (not only the date line); the helper walks back to the transaction start. The client uses the existing `split_at_transaction_by_line_number(row_idx_zero_based, all_lines)` to extract `(before, tx_block, after)`. `tx_block` contains the exact raw text of the transaction (date line, indented postings, and the indented metadata block with `document:`/`documentN:` keys), including inline comments. Comment lines *above* the transaction are not part of the block and travel in `before` — therefore the client must send the original block to the LLM, and any new leading comments in the LLM's output are handled per "Client-side flow" below.

### Linked document discovery

The client scans `tx_block` lines for document metadata keys using the single canonical regex `^\s*document(\d*):\s*"([^"]+)"` (colon directly after the key, then a quoted path — matching both Beancount's `document: "path"` and the numbered `documentN:` forms, and consistent with `update_document_metadata()` in `client/cli.py:404`). All `document`, `document2`, … forms are captured. Extracted paths are client-local and resolved as relative to the directory containing the transaction file being read (fall back to `cfg.beancount.main_folder` if a directory-relative path does not exist). Missing/unreadable files are a fail-stop error.

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

The prompt uses `{transaction_text}` as a placeholder for the original transaction block and `{accounts}` for the account listing (the server fills it in with `json.dumps(request["accounts"])`, exactly as `do_process` fills `{accounts}`). Documents are injected separately as base64 image parts (same mechanism as `do_process`), not as a placeholder. An example rewrite demonstrates adding missing line items, correcting amounts, and preserving the original header/metadata.

## Data structures

### Input to server (`beanai.Refine`)

Input is sent via stdin as a **single plain-JSON object** (not hex, and not an array). The command carries no CLI argument; only stdin is used.

```python
class Document(TypedDict):
    filepath: str        # client-local path, relative to the transaction file's directory
    data: str            # client base64-encodes the raw bytes for JSON transport

class RefineRequest(TypedDict):
    transaction_text: str          # existing full Beancount transaction block (exact original formatting, comments/metadata included)
    accounts: list[str]            # account listing (read by the client from cfg.beancount.account_list_file)
    documents: list[Document]      # all linked documents attached to the target transaction
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
| Document collection | Client-local documents only (read from disk next to the Beancount file) |
| LLM payload format | Text prompt mode with base64 image parts for each document — identical to the existing receipt processing pipeline (`file_to_image_parts()`) |
| Backend subcommand | Single subcommand `beanai.Refine`, **no CLI argument** (single LLM pass, not two-pass like `HelpAssociateReceipt`); all input arrives on stdin as plain JSON |
| Output behavior | Prints diff to stdout, asks the user whether to apply the change to the file or abort; user may preview the first linked document before deciding (same interactive loop as `associate`) |
| Metadata preservation | Prompt instructs LLM to preserve all fields; client validates output contains date flag, payee and at least two postings. Any original non-doc metadata must remain in the returned block. |

## CLI: `bean-ai refine` subcommand

### Arguments

```sh
bean-ai refine <file_path> <line_number> [--yes | --no]
```

| Positional arg | Meaning |
|---|---|
| `<file_path>` | Path to the Beancount file containing the target transaction |
| `<line_number>` | 1-based line number of **any line within** the target transaction (not necessarily the date line), within that file (e.g., `42`) |

| Flag | Meaning |
|---|---|
| `--yes` | Apply the modification without confirmation. |
| `--no` | Do all the work (fetch documents, call the LLM, show the diff) but do not touch any file. |

### Exit codes

- `0`: diff displayed; file updated successfully if the user requested the change (or `--yes`)
- Non-zero: error encountered — missing file, line out of range / not a transaction, unreadable document, LLM call failure, malformed LLM output

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

The server receives the `beanai.Refine` subcommand via qrexec or subprocess transport, **with no CLI argument** (unlike `beanai.Process`/`beanai.HelpAssociateReceipt`, which pass a hex-encoded filename as an argument — `do_refine` must therefore not reference `args.filename`). All payload data arrives on stdin as **plain JSON** (a single object — matching the transport convention where only the command argument, not stdin, is hex-encoded):

```python
request_data = json.loads(sys.stdin.read())  # {"transaction_text": "...", "accounts": [...], "documents": [{"filepath": "...", "data": "<base64>"}, ...]}
```

### Processing steps

1. Validate input: reject if the request is not a JSON object or `transaction_text` is missing/empty (responds with stderr `error:...` + `sys.exit(1)`)
2. Extract `transaction_text`, `accounts`, and `documents` from the request
3. For each document in `documents`:
   - Validate the extension against the supported set (`.jpg`, `.jpeg`, `.png`, `.pdf`); any other extension is a fail-stop error
   - Base64-decode `data` back to raw bytes
   - Call `file_to_image_parts(filepath, raw)` (this internally calls `render_pdf_pages_to_png()` for PDFs, and builds an `image_url` part for JPG/PNG)
4. Load `TRANSACTION_REFINEMENT_PROMPT.md` at runtime, fill `{transaction_text}` and `{accounts}` placeholders (`accounts` is `json.dumps(request["accounts"])`)
5. Send to LLM alongside text prompt + all image parts
6. Stream response back to client via JSONL (reasoning chunks + output chunks + a `finish` marker), the same protocol as `stream_reasoning_and_output()`

## Client-side flow (`do_refine`)

### Detailed steps in `do_refine(cfg, args)`

```python
def do_refine(cfg: Configuration, args: argparse.Namespace) -> None:
    # 1. Read file.
    tx_file = Path(args.file_path)
    if not tx_file.exists():
        print(f"Error: file not found: {tx_file}", file=sys.stderr)
        sys.exit(1)
    all_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

    # 2. Extract transaction block preserving all formatting.
    #    Helper takes a zero-based line index; any line within the tx is accepted.
    try:
        before, tx_block, after = split_at_transaction_by_line_number(
            args.line_number - 1, all_lines
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    tx_block_str = "".join(tx_block)   # exact original text

    # 3. Find linked documents — scan metadata for 'document'/'documentN' keys.
    doc_paths = extract_document_paths(tx_block)

    # 4. Collect document contents (client-local, resolved relative to the tx file).
    documents_data: list[Document] = []
    for doc_path in doc_paths:
        resolved = resolve_document_path(doc_path, tx_file, cfg)  # see below
        if not resolved.exists():
            print(f"Error: linked document not found: {doc_path}", file=sys.stderr)
            sys.exit(1)
        raw = resolved.read_bytes()
        documents_data.append(Document(
            filepath=doc_path,
            data=base64.b64encode(raw).decode("ascii"),
        ))

    # 5. Call server — plain JSON payload via stdin (command carries no arg).
    vm = RemoteVM.from_cfg(cfg)
    cmd, proc, stdin, stdout = vm._call("beanai.Refine")

    accounts = cfg.beancount.account_list_file.read_text(encoding="utf-8").splitlines()

    request_payload = {
        "transaction_text": tx_block_str,
        "accounts": accounts,
        "documents": [
            {"filepath": d["filepath"], "data": d["data"]} for d in documents_data
        ],
    }
    stdin.write(json.dumps(request_payload).encode("utf-8"))
    stdin.flush()
    stdin.close()

    llm_output = stream_reasoning_and_capture_output(stdout)
    ret = proc.wait()
    if ret != 0:
        print("Error: server returned non-zero exit code", file=sys.stderr)
        sys.exit(1)

    # 6. Parse response — strip markdown fences if present, then parse JSON.
    llm_output = demarkdownify(llm_output).strip()
    try:
        resp = load_json(llm_output)
        rewritten_tx_raw = resp["transaction"]
    except Exception:
        print("Error: could not parse LLM response as JSON. Raw output:\n", file=sys.stderr)
        print(llm_output, file=sys.stderr)
        sys.exit(1)

    # Preserve original/inline comments: strip ONLY leading block-comment lines
    # that the LLM may have added as reasoning; these do not exist in the original
    # (comments above the tx are not part of the extracted block).
    lines = rewritten_tx_raw.splitlines(True)
    while lines and lines[0].lstrip().startswith(";"):
        lines = lines[1:]
    rewritten_tx = "".join(lines).rstrip("\n") + "\n"

    # 7. Reassemble the file with only the target block replaced.
    new_lines = before + [ln if ln.endswith("\n") else ln + "\n" for ln in rewritten_tx.splitlines(True)] + after
    new_content = "".join(new_lines)

    old_lines = all_lines
    diff = list(
        difflib.unified_diff(old_lines, new_lines, fromfile=str(tx_file), tofile=str(tx_file), n=5)
    )
    if diff:
        print_diff(diff)

    # 8. Prompt / write.
    if args.no:
        print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
        return

    if not args.yes:
        while True:
            print(
                f"\nApply refined transaction to '{tx_file}'? [y]es / [n]o / [p]review document / [q]uit ",
                file=sys.stderr, end="",
            )
            try:
                answer = input().strip().lower()
            except EOFError:
                return
            if answer == "q":
                sys.exit(0)
            if answer == "p" and documents_data:
                _preview_document(documents_data[0]["filepath"])
                continue
            if answer == "y":
                break
            return  # 'n' -> abort without writing

    assert new_content.endswith("\n")
    tx_file.write_text(new_content, encoding="utf-8")
    print(f"Updated transaction in {tx_file}", file=sys.stderr)
```

### Helper: `extract_document_paths(tx_block: list[str]) -> list[str]`

Scans lines of the transaction for document metadata entries matching the single canonical regex `^\s*document(\d*):\s*"([^"]+)"` (capture group 1 is the optional numeric suffix, group 2 is the quoted path). Returns the extracted paths as a deduplicated list preserving first-seen order. This matches Beancount's `document: "path"` and the numbered `documentN:` forms, and is consistent with the key form handled by `update_document_metadata()` in `client/cli.py:404`.

### Helper: `resolve_document_path(doc_path: str, tx_file: Path, cfg: Configuration) -> Path`

Resolves a `document:` value to a client-local path:
1. If `doc_path` is absolute → use it as-is.
2. Otherwise try `tx_file.parent / doc_path` first.
3. If that does not exist, try `cfg.beancount.main_folder / doc_path`.
4. If neither exists, raise (the caller prints an error and exits 1).

This covers both "paths relative to the Beancount data root" (the usual arrangement, since receipt files live under `main_folder`) and "paths relative to the file's own directory".

## Edge cases handled in code

### Client-side:

| Scenario | Client behavior |
|---|---|
| Missing file / file does not exist | Prints `Error: file not found: <path>` to stderr, exits 1 |
| Line number out of range (below 1 or above line count) | Prints clear message with valid range `[1, N]`, exits 1 |
| `split_at_transaction_by_line_number()` raises `ValueError` (line is out of range, or is not part of any transaction — the helper accepts any line *within* a transaction and walks back to its start) | Catches the exception, prints its `str(e)` to stderr, exits 1 |
| No linked documents in the extracted block | Not an error — `documents` is empty and the LLM works from the transaction text only |
| Linked document does not exist / cannot be read | Prints `Error: linked document not found: <path>` to stderr, exits non-zero |
| Server returns non-zero exit code | Prints error to stderr, exits 1 |
| LLM output is not valid JSON, or lacks the `"transaction"` key | Prints the full raw output to stderr for debugging, exits 1 (same as `process` flow) |

### Server-side:

| Scenario | Server behavior |
|---|---|
| Input is not a JSON object, or `transaction_text` is missing/empty/malformed | Responds with stderr `error: Invalid request: missing transaction_text` then exits code 1 |
| Document has an unsupported extension (anything other than `.jpg`/`.jpeg`/`.png`/`.pdf`) | Emits `error: unsupported document format: <ext>` to stderr + `sys.exit(1)` |
| LLM call fails (network/auth/model error) | Emits JSON error line to stdout + `sys.exit(1)` (same as existing handlers) |
| Empty document list after processing | No images sent — LLM only uses text prompt + original transaction block |
| Transaction is malformed | LLM needs to decide what to do on its own |
| Render failure on any PDF | Emits `error: ...` message to stderr + `sys.exit(1)` (same pattern as existing `do_process`) |

### Prompt-side limitations:

- Rewritten transaction is generally limited to the account listing supplied in the prompt
- LLM cannot fetch additional receipts beyond those linked in metadata; it works only with provided documents
- All linked documents are client-local (read from disk next to the Beancount data); if a `document:` path points to a file that is not present locally, the command fails with a clear "linked document not found" error

---

## Comparison: refine vs existing subcommands

| Aspect | Ingest (`bean-ai ingest`) | Associate (`bean-ai associate`) | Refine (`bean-ai refine`, this spec) |
|---|---|---|---|
| Target | **New** transaction (created from scratch) | Existing, matched by date/amount from LLM ranking | **Existing**, specified by file path + line number |
| User input | Receipt filename(s) on server | Receipt filename(s) on server | Beancount file path + line number |
| Document source | Uningested receipts (WebDAV) | Unassociated receipts (WebDAV) | Already linked to the target transaction, **client-local** files on disk |
| Metadata changes | Inserts single `document:` entry | Renames existing docs, new doc = `document:` | **None** to the `document:` keys — those metadata lines are preserved unchanged |
| Modifies Beancount file | Appends a new entry to ingestion destination path | Edits source file in-place (adds document metadata) | **Yes** — rewrites only the target transaction block in its source file (on interactive yes or `--yes`); `--no`/`n` leaves the file untouched. No file is modified outside the targeted block's lines. |
| Receipt lifecycle post-success | Removes receipt from WebDAV `uningested` | Removes receipt from `unassociated` on success | **Receipts untouched** — linked files are read-only inputs and remain on disk |
| LLM passes | Single pass (`beanai.Process`) | Two passes (`HelpAssociateReceipt`: info + match) | **Single pass** (`beanai.Refine`) |
| Output destination | Writes to ingestion file, receipt to organized folder | Edits Beancount source, writes receipt to organized folder | Stdout colored diff of the proposed change; file updated after user confirmation |

---

## File additions and modifications

### New files

| File | Purpose |
|---|---|
| `beancount_ai/server/TRANSACTION_REFINEMENT_PROMPT.md` | LLM prompt for refining transactions (preservation rules, modification instructions, output format examples) |

### Modified files

| File | Changes |
|---|---|
| `beancount_ai/server/cli.py` | Add `do_refine()` handler; add `TRANSACTION_REFINEMENT_PROMPT_PATH` constant; register `beanai.Refine` in `build_parser()` (**with no positional argument**) and `dispatch` table |
| `beancount_ai/client/cli.py` | Add new subcommand (for `do_refine`) in `build_parser()` with argparse entries for `<file_path>`, `<line_number>` (positional) and `--yes/--no`; add helpers `extract_document_paths()` and `resolve_document_path()`; add `do_refine()` function with document discovery, account-list read, plain-JSON stdin server call, diff + interactive apply/write logic; register `do_refine` in client `dispatch` dict |

### Implementation order (proposed)

1. Write `TRANSACTION_REFINEMENT_PROMPT.md` — define preservation rules, modification instructions, and example rewrites first (with `{transaction_text}` and `{accounts}` placeholders, mirroring `RECEIPT_CONVERSION_PROMPT.md`)
2. Server-side: implement `do_refine()` — read plain-JSON request from stdin, validate `transaction_text`, extension-check + base64-decode + `file_to_image_parts()` each document, fill prompt placeholders, LLM call, stream output; register `beanai.Refine` (no argument) in `build_parser()` and `dispatch`
3. Client-side helpers: `extract_document_paths()` (scan tx metadata for `document:`/`documentN:`) and `resolve_document_path()` (resolve relative to the tx file, then `main_folder`)
4. Client-side `do_refine()` wiring: file read → line validation → tx extraction → doc discovery → read account list → plain-JSON stdin server call → parse → reassemble + diff → interactive apply/write
5. Client CLI arg parser entry in `build_parser()` with positional + optional args
6. Register new subcommand in client's dispatch dict
7. Add tests: unit tests for `extract_document_paths()` and `resolve_document_path()`, doctests for line-range validation, mock LLM response handling
8. Update all relevant documentation to cover the new feature.

---

(End of file)
