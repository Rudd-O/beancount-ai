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
| `bean-ai ingest` | `[--yes \| --no]` | `[<filename>]` | Process all uningested receipts. Without filenames, processes everything on the server. With filenames, processes only those (they must exist). Interactive: prompts `y/n/p/q` for each receipt (`p` previews in `$XDG_DEFAULT_BROWER`, `q` aborts). With `--yes`: auto-import. With `--no`: do all work but don't touch files (dry run). |
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

---

## bean-ai-server (server VM)

Runs on the machine with receipts and LLM access. All subcommands accept filenames as **hex-encoded** positional arguments (encoded/decoded by the transport layer).

**Options:**

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### Listing receipts

| Command | Output on success | Error handling |
|---|---|---|
| `bean-ai-server beanai.ListUningested` | JSON: `{"receipts": [...], "count": N}` | Writes `{"error": "..."}` to stderr, exits 1 |
| `bean-ai-server beanai.ListUnassociated` | Same as above | Same as above |

Lists filenames ending in `.jpg`, `.jpeg`, `.png`, or `.pdf`, sorted by modification time. Uses `receipts_ingestion_url` (uningested) or `receipts_association_url` (unassociated).

### Receipt operations

| Command | Arguments | Description |
|---|---|---|
| `bean-ai-server beanai.Fetch <hex_filename>` | hex-encoded filename | Fetch a receipt from WebDAV (tries ingestion URL first, falls back to association) and write raw bytes to stdout. |
| `bean-ai-server beanai.Remove <hex_filename>` | hex-encoded filename | Remove a receipt file from WebDAV (ingestion URL first, then association). Exit 0 on success, 1 on failure. |
| `bean-ai-server beanai.Process <hex_filename>` | hex-encoded filename | Process a receipt with the LLM using `RECEIPT_CONVERSION_PROMPT.md`. PDFs are page-by-page rendered to PNG (via `fitz`, 300 DPI fallback). Emits streaming JSONL output. |
| `bean-ai-server beanai.HelpAssociateReceipt <hex_filename>` | hex-encoded filename | Match a receipt against candidate transactions sent via stdin as JSON. Uses `RECEIPT_INFO_PROMPT.md` then `RECEIPT_MATCH_PROMPT.md`. Writes structured match results to stdout. |

### JSONL output (Process and HelpAssociateReceipt)

| Delta type | Description |
|---|---|
| `{"reasoning": <chunk>}` | LLM chain-of-thought / reasoning tokens streamed in real time |
| `{"output": <chunk>}` | Final response content emitted after reasoning completes |
| `{"finish": <reason>}` | Signals completion (usually `"stop"`); client stops processing and outputs accumulated result |

Each line is flushed immediately. Every 10 chunks the buffer is forcibly flushed to minimize latency over qrexec pipes. On error: writes `{"error": "..."}` to stdout.

### HelpAssociateReceipt flow

1. Reads receipt from `receipts_association_url` via WebDAV
2. Converts PDF → PNG pages (or base64-encodes images)
3. Emits receipt info JSONL (`RECEIPT_INFO_PROMPT.md`)
4. Reads candidate transactions JSON from stdin
5. Invokes LLM with image + candidates (`RECEIPT_MATCH_PROMPT.md`)
6. Writes ranked match results to stdout as JSONL

---

## Transport layer

When `target_vm` is set in config, the client talks to the server via qrexec:

```
qrexec-client-vm <target_vm> beanai.<command>+<hex_arg>
```

When `target_vm` is `null`, the client spawns `bean-ai-server` as a local subprocess with hex-encoded arguments. This is how local testing works. The user never needs to worry about encoding or transport details.
