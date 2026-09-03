# Commands

All commands rely on configuration parameters, documented in `README.md`.

---

## bean-ai (client)

Runs on the machine with Beancount data. Abstracts away the transport layer entirely — it talks to `bean-ai-server` via qrexec or a local subprocess.

### Options

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### List commands

Print one receipt filename per line (bare filenames, no path).

| Command | Description |
|---|---|
| `bean-ai list-uningested` | Receipts not yet imported as transactions |
| `bean-ai list-unassociated` | Receipts not yet linked to an existing transaction |

### Receipt import and ingestion

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `bean-ai ingest` | `[--yes \| --no]` | `[<filename>]` | Process all uningested receipts. Without filenames, processes everything on the server. With filenames, processes only those (they must exist). Interactive: prompts `y/n/p/q` for each receipt (`p` previews in your image / PDF viewer, `q` aborts). With `--yes`: auto-import. With `--no`: do all work but don't touch files (dry run). |
| `bean-ai import <filename>` | — | — | Full pipeline for a single receipt (fetch → LLM → organize → append). Leaves the receipt on the server instead of deleting it. |

### Receipt organization and management

| Command | Arguments | Description |
|---|---|---|
| `bean-ai process <filename>` | `<filename>` | Extract transaction data via LLM. Prints Beancount tx to stdout, `Main account: <account>` to stderr. |
| `bean-ai organize <filename> <date> <account>` | `<filename> <YYYY-MM-DD> <account>` | Download a receipt and file it under `<beancount_folder>/<account_with_slashes>/`. Useful when you already know the data. Filename format: `<date>.<original_filename>`. |
| `bean-ai fetch <filename> <destination>` | `<filename> <local_path>` | Download a receipt from the server to a local path. Tries ingestion URL first, then association URL. |
| `bean-ai remove <filename>` | `<filename>` | Delete a receipt from the server (tries ingestion URL first, association second). Exit code 0 on success, 1 on failure. |

### Receipt association

Link a receipt to an existing Beancount transaction (for receipts from banks/merchants that already have matching entries in your ledger).

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `bean-ai associate` | `[--yes \| --no]` | `[<filename>]` | Associate one or more receipts with existing transactions. Without filenames, processes all unassociated receipts. With filenames, processes only those (they must exist). The flow: (1) LLM extracts date + amount from receipt; (2) queries Beancount for candidates within 1 day before to 45 days after receipt date; (3) LLM ranks candidates by match probability; (4) if unambiguous (score ≥ 0.8), auto-selects the top match; (5) inserts `document:` metadata on the transaction line (newest doc first, older docs renamed to `document2:`, `document3:`, etc.); (6) saves receipt under the appropriate account folder and removes it from WebDAV. With `--yes`: confirm all actions automatically. With `--no`: print diff only, skip writes. |

### Refining existing transactions

Rewrite an existing transaction using the documents already linked to it, to produce a more detailed / more accurate version while preserving the original detail.

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `bean-ai refine <file_path> <first_line_number> [last_line_number]` | `[--yes \| --no]` `[--clear \| -c]` | `<file_path> <first_line_number> [last_line_number]` | Target a transaction by file path and a 1-based line number (any line *within* the transaction), or optionally a range of lines (inclusive), in which case every transaction that begins between both line numbers is refined in a single run. The client extracts the transaction block(s), reads the documents linked in their `document:` / `documentN:` metadata (client-local, resolved relative to the file's directory or the Beancount data root), and for each transaction sends it to the server as plain JSON on stdin (the command carries no positional argument) together with the account list and document images, asking the LLM for a rewritten transaction. For each candidate refinement, a colored unified diff of the whole file (reflecting all accepted changes so far) is shown, followed by an interactive prompt: `y`es keeps the refinement for that transaction (and moves on to the next one, if any), `n`o skips it (the transaction is left untouched), `p`review document opens its first linked document, `q`uit aborts the run — keeping all refinements already accepted. With `--yes`: apply all refinements without confirmation. With `--no`: do all the work and show the diff but touch no file. With `--clear` (`-c`): the flag of every accepted, changed transaction is set to the clear flag (`*`). The file is written only once, at the end, if any accepted refinement differs from the original. A transaction in the range whose start is past `last_line_number` is not refined; a line that does not point at (into) any transaction aborts the run with an error. Exit 0 on success, non-zero on error (missing file, line out of range or not a transaction, unreadable document, LLM error, malformed LLM output). |

---

## bean-ai-server (server VM)

Runs on the machine with receipts and LLM access. Most subcommands accept filenames as **hex-encoded** positional arguments (encoded/decoded by the transport layer); `beanai.Refine` is the exception — it takes no positional argument and receives its request as plain JSON on stdin.

**Options:**

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### Listing receipts

| Command | Output on success | Error handling |
|---|---|---|
| `bean-ai-server beanai.ListUningested` | JSON: `{"receipts": [...], "count": N}` | Writes `"error: ..."` to stderr, exits 1 |
| `bean-ai-server beanai.ListUnassociated` | Same as above | Same as above |

Lists filenames ending in `.jpg`, `.jpeg`, `.png`, or `.pdf`, sorted by modification time. Uses `receipts_ingestion_url` (uningested) or `receipts_association_url` (unassociated).

### Receipt operations

| Command | Arguments | Description |
|---|---|---|
| `bean-ai-server beanai.Fetch <hex_filename>` | hex-encoded filename | Fetch a receipt from WebDAV (tries ingestion URL first, falls back to association) and write raw bytes to stdout. |
| `bean-ai-server beanai.Remove <hex_filename>` | hex-encoded filename | Remove a receipt file from WebDAV (ingestion URL first, then association). Exit 0 on success, 1 on failure. |
| `bean-ai-server beanai.Process <hex_filename>` | hex-encoded filename | Process a receipt with the LLM using `RECEIPT_CONVERSION_PROMPT.md`. PDFs are page-by-page rendered to PNG (via `pymupdf`, 300 DPI fallback). Emits streaming JSONL output. |
| `bean-ai-server beanai.HelpAssociateReceipt <hex_filename>` | hex-encoded filename | Match a receipt against candidate transactions sent via stdin as JSON. Uses `RECEIPT_INFO_PROMPT.md` then `RECEIPT_MATCH_PROMPT.md`. Writes structured match results to stdout. |

### Refining transactions

| Command | Arguments | Description |
|---|---|---|
| `bean-ai-server beanai.Refine` | *(none)* | Refine an existing Beancount transaction using its linked documents. **No positional argument.** The request arrives on stdin as a single plain-JSON object: `{"transaction_text": ..., "accounts": [...], "documents": [{"filepath": ..., "data": <base64>}, ...]}`. Validations are fail-stop: the request must be a JSON object with a non-empty `transaction_text`; each document's extension must be one of `.jpg`, `.jpeg`, `.png`, `.pdf`. Documents are base64-decoded and turned into image parts (PDFs rendered to PNG page-by-page). Emits the same streaming JSONL output as `beanai.Process`. |

### JSONL output (Process, HelpAssociateReceipt and Refine)

| Delta type | Description |
|---|---|
| `{"reasoning": <chunk>}` | LLM chain-of-thought / reasoning tokens streamed in real time |
| `{"output": <chunk>}` | Final response content emitted after reasoning completes |
| `{"finish": <reason>}` | Signals completion (usually `"stop"`); client stops processing and outputs accumulated result |

Each line is flushed immediately. Every 10 chunks the buffer is forcibly flushed to minimize latency over qrexec pipes. On error: writes `error: ...` to stderr.

### HelpAssociateReceipt flow

1. Reads receipt from `receipts_association_url` via WebDAV
2. Converts PDF → PNG pages (or base64-encodes images)
3. Emits receipt info JSONL (`RECEIPT_INFO_PROMPT.md`)
4. Reads candidate transactions JSON from stdin
5. Invokes LLM with image + candidates (`RECEIPT_MATCH_PROMPT.md`)
6. Writes ranked match results to stdout as JSONL

### Refine flow

1. Reads the whole request (a single plain-JSON object) from stdin
2. Validates `transaction_text` (non-empty) and each document's extension
3. Base64-decodes each document and turns it into image parts (PDFs → PNG pages)
4. Invokes the LLM once with the text prompt + all image parts (`TRANSACTION_REFINEMENT_PROMPT.md`, which embeds the original transaction under `{transaction_text}` and the account list under `{accounts}`)
5. Streams the refined transaction back to the client as JSONL

The client then shows a diff and, on confirmation, replaces only the target transaction block's lines in the file — `document:` metadata and every other part of the file are left untouched.

---

## Transport layer

When `target_vm` is set in config, the client talks to the server via qrexec:

```
qrexec-client-vm <target_vm> beanai.<command>+<hex_arg>
```

When `target_vm` is `null`, the client spawns `bean-ai-server` as a local subprocess with hex-encoded arguments. This is how local testing works. The user never needs to worry about encoding or transport details.
