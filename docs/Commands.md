# Commands

All commands rely on configuration parameters, documented in `AGENTS.md`.

## bean-ai-server (server VM)

### Purpose

Makes receipt information available and allows managing receipts by client.

All subcommands read filenames as hex-encoded positional arguments (the transport layer encodes/decodes).

### Subcommand: `beanai.ListUnassociated` / `beanai.ListUningested`

Lists receipt filenames from either `receipts_association_url` (for unassociated) or `receipts_ingestion_url` (for uningested). Filenames must end in `.jpg`, `.jpeg`, `.png`, or `.pdf`. Results are sorted by modification time, newest first.

**Returns to stdout:**
```json
{"receipts": ["M071225_115439.jpg"], "count": 1}
```

On error: writes `{"error": "..."}` to stderr and exits with code 1.

---

### Subcommand: `beanai.Fetch`

Fetches a receipt file by its filename (from either ingestion or association URL — tries ingestion first, falls back to association). Writes the raw bytes of the file to stdout (use `-b` or pipe to capture binary content).

**Usage:**
```bash
bean-ai-server beanai.Fetch <hex_filename> | cat > local.jpg
```

---

### Subcommand: `beanai.Remove`

Removes a receipt file from WebDAV (tries ingestion URL first, then association URL). Exits with code 0 on success, code 1 on failure.

**Usage:**
```bash
bean-ai-server beanai.Remove <hex_filename>
```

---

### Subcommand: `beanai.Process`

Takes a receipt filename as a positional argument, reads it from `receipts_ingestion_url`, base64-encodes the image, embeds the full prompt (`RECEIPT_CONVERSION_PROMPT.md`) + receipt data in a multi-part OpenAI compatible message (text_part + image_part), and submits it to Open-WebUI via the `openwebui-client` library.

PDF files are automatically rendered page-by-page to PNG at embedded DPI (via `fitz`) with 300 DPI fallback for pages without embedded images. Each page is sent as a separate image attachment in the LLM request.

Supports JPEG, JPG, PNG, and PDF receipts. Uses explicit `httpx.Client(verify=<system_ca_bundle>|certifi.where())` to support self-signed/internal CAs (on RHEL/Fedora this resolves to `/etc/pki/tls/cert.pem`).

**Produces JSONL output:**

| Delta type | Description |
|---|---|
| `{"reasoning": <chunk>}` | LLM chain-of-thought / reasoning tokens streamed in real time |
| `{"output": <chunk>}` | Final response content (Beancount transaction + JSON), emitted after reasoning completes |
| `{"finish": <reason>}` (e.g. `"stop"`) | Signals completion; client stops processing and outputs accumulated result |

Each line is immediately flushed via `sys.stdout.write("\n")`. Flush every 10 chunks to minimize latency over qrexec pipes.

---

### Subcommand: `beanai.HelpAssociateReceipt`

Processes a receipt against a list of candidate transactions, reading candidates from stdin as JSON. The filename arg comes via hex-encoded CLI argument and is used to locate the receipt on WebDAV (from `receipts_association_url`). The function loads the receipt image, converts PDF pages if needed, feeds them to Open-WebUI together with the candidate text, and writes structured match results to stdout.

**Flow:**
1. Reads receipt from `receipts_association_url` via WebDAV
2. Converts PDF → PNG pages (or base64-encodes images), identical to `do_process` (`RECEIPT_INFO_PROMPT.md`).
3. Sends basic receipt information back to client. 
4. Reads transaction candidates JSON from stdin
5. Invokes LLM with the image attachment and candidate list prompt (`RECEIPT_MATCH_PROMPT.md`)
6. Returns matching transactions ranked by probability of matching the supplied receipt.

To respond to client, it emits streaming output: `{reasoning}`, `{output}`, or `{finish}` JSONL lines,
like `do_process`.

**Usage (client side):**
```bash
# Client writes candidates to stdin, reads match results from stdout
echo '[{"index": 0, "date_str": "...", ...}]' | bean-ai-server beanai.HelpAssociateReceipt <hex_filename>
```

---

## bean-ai (client VM)

### Purpose

CLI tool that communicates with `bean-ai-server` (either locally or on another VM via qrexec) to orchestrate receipt list retrieval and individual receipt processing, providing accounting information to the server when the task requires it. Abstracts away the transport layer entirely from the end user.

### Subcommand: `list-unassociated` / `list-uningested`

Invokes `bean-ai-server beanai.ListUnassociated` or `beanai.server.ListUningested` via the configured transport, then prints one receipt filename per line to stdout (filenames are bare — no file path). Refer to the respective server subcommands for more information.

---

### Subcommand: `import <filename>`

Full import pipeline for a new receipt: fetches the receipt from the server, processes it via LLM, organizes it into an account folder (`<beancount_folder>/<account_with_slashes>/`), writes the Beancount transaction to the designated transactions file, and appends a `document:` metadata entry on that line linking back to the organized receipt.

**Receipt naming convention:** `<YYYY-MM-DD>_<description> — <original_filename>` under the account folder (e.g., `2026-07-15_Groceries — IMG_1234.jpg`). No slashes in filenames. Long paths are truncated via `shorten_fn`.

---

### Subcommand: `ingest [--batch]` / (-b)

Batch ingest receipts interactively: for each receipt, prompts `Import '<filename>'? [y/n/p/q]`:
- **y**: import the receipt
- **n**: skip it
- **p**: preview the receipt image in `$XDG_DEFAULT_BROWER` via `xdg-open` (writes to a temp directory)
- **q**: abort all remaining receipts

With `--batch`, skips prompts entirely — auto-imports what it can and prints errors to stderr. Uses an error counter as exit code on batch mode.

---

### Subcommand: `organize <filename> <date> <account>`

Downloads a receipt file from the server and files it under `<beancount_folder>/<account_with_slashes>/` as `<date>.<filename>`. Useful for receipts that are already processed (or whose data you supply manually) but need to be organized into the proper directory structure. See `predict_receipt_destination_path` for naming requirements on Beancount document files.

---

### Subcommand: `fetch <filename> <destination>`

Fetches a receipt file from the server and saves it to the local `destination` path. The filename is looked up in both ingestion and association URLs (tries ingestion first).

---

### Subcommand: `remove <filename>`

Deletes a receipt file from the server (tries ingestion URL first, then association URL). Exits with code 0 on success, code 1 on failure.

---

### Subcommand: `process <filename>`

Takes a receipt filename and invokes `bean-ai-server beanai.Process+<hex_filename>` on the server. Reads the remote JSONL stream line-by-line, prints reasoning chunks to stderr as they arrive, stops when `{"finish"}` or `{"error"}` is received. Accumulates all `{output}` delta chunks into a single string printed to stdout at the end, followed by `"Main account: <account>"` on the last stderr line.

---

### Subcommand: `associate <filename> [--yes | --no]` (-y / -n)

Associate a receipt with an existing Beancount transaction. The full flow is:

1. **Receive** date + paid amount from the LLM (via `helper_associate_receipt` on the server).
2. **Query** the local Beancount data for candidate transactions within ±45 days of the receipt's date using `beancount.loader`. Candidate fields include `date_str`, `payee`, `narration`, `paid_amount`, `paid_currency`, `crediting_account`, `source_file`, `line_no`, and `transaction_text`.
3. **Send** the candidate list (as JSON) to `beanai.server.HelpAssociateReceipt` on the server via stdin, along with the receipt filename as a CLI arg. The server loads the image from WebDAV, feeds it in a multimodal LLM request with the candidates, and produces streaming match results.
4. **Interpret** the result: if unambiguous (score >= 0.8), auto-select the top match; if ambiguous (< 0.8), emit a list of matches to stderr and return without changes.
5. **Update** the Beancount transaction at `source_file:line_no` by inserting or replacing the `document:` metadata with the organized receipt path. If an existing `document:` tag is present, its value is preserved under `import_source:`. A unified diff of changes is printed to stdout before writing.
6. **Save** the receipt file to `<beancount_folder>/<crediting_account_with_slashes>/` and remove it from WebDAV.

**Conflicting flags:**
- `--yes (-y)`: confirms all actions automatically (equivalent to answering "yes" to every prompt)
- `--no (-n)`: skips all write operations, only prints the diff to stdout and returns
