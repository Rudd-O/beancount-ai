# Commands

All commands rely on configuration parameters, documented in `README.md`.

---

## beanhand (client)

Runs on the machine with Beancount data. Abstracts away the transport layer entirely — it talks to `beanhand-documents-server` and `beanhand-ai-server` via qrexec or local subprocesses (one per server, each of which may target a different VM).  When an AI operation needs a receipt, the client fetches it from the documents server and relays it to the AI server over the connection's standard input.

Receipt-taking commands (`process`, `import`, `ingest`, `associate`, `organize`) also accept a **path to a file on the client's own filesystem**, so you can feed a scanned receipt straight from your disk without first pushing it into the documents store: a path (or a bare name with a receipt extension that exists in the working directory) that exists locally is read directly and makes zero documents-server calls. When an argument is not such a local file it is a documents-store filename and is resolved through the server, exactly as before.

### Options

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### List commands

| Command | Description |
|---|---|
| `beanhand list-uningested` | Receipts not yet imported as transactions (one bare filename per line) |
| `beanhand list-unassociated` | Receipts not yet linked to an existing transaction (one bare filename per line) |
| `beanhand list-accounts [date]` | Print the accounts `beanhand` will offer the LLM (one name per line, with an indented `  rule: <rule>` line when the account carries one). Optional `date` scopes the open/closed check; defaults to today. Run `beanhand list-accounts` to sanity-check your ledger's `beanhand-include` / `beanhand-exclude` markers. No LLM is called. |

### Receipt import and ingestion

Each receipt argument is a **documents-store filename *or* a path to a local file**.
A path that exists on the client's filesystem (e.g. `scans/2026-01-01.jpg`,
`/abs/x.pdf`) is read directly and is never sent to the documents server; a bare
name (no path separator) is treated as a store filename, *unless* a file with a
receipt extension (`.jpg`, `.jpeg`, `.png`, `.pdf`) of the same name sits in the
current working directory, in which case it is read locally. A bare name that is
not a plausible local receipt is a store filename exactly as before (so existing
store-based workflows are byte-for-byte unchanged).

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `beanhand ingest` | `[--yes \| --no]` | `[<filename>]` | Process all uningested receipts. Without filenames, processes everything on the server. With filenames, processes only those (a local path that does not exist fails fast, before any LLM call). Interactive: prompts `y/n/p/q` for each receipt (`p` previews in your image / PDF viewer, `q` aborts). With `--yes`: auto-import. With `--no`: do all work but don't touch files (dry run). On success, each receipt is consumed from its source: a store receipt is removed from the server, a local source file is moved into the account folder (see below). |
| `beanhand import <filename>` | — | — | Full pipeline for a single receipt (fetch → LLM → organize → append). Leaves the receipt on its source instead of deleting it: a store receipt is left in the store, a local source file is left in place. |

### Receipt organization and management

| Command | Arguments | Description |
|---|---|---|
| `beanhand process <filename>` | `<filename>` | Extract transaction data via LLM. Prints Beancount tx to stdout, `Main account: <account>` to stderr. Read-only; the receipt is read from its source (local file or store) and never modified. |
| `beanhand organize <filename> <date> <account>` | `<filename> <YYYY-MM-DD> <account>` | File a copy of the receipt under `<beancount_folder>/<account_with_slashes>/`. Useful when you already know the data. Filename format: `<date>.<original_filename>`. The source (local file or store receipt) is read but left in place. |
| `beanhand fetch <filename> <destination>` | `<filename> <local_path>` | Download a receipt from the server to a local path. Tries ingestion URL first, then association URL. (Store-only; a local path would just be a `cp`.) |
| `beanhand remove <filename>` | `<filename>` | Delete a receipt from the server (tries ingestion URL first, association second). Exit code 0 on success, 1 on failure. (Store-only; there is nothing local to remove.) |

### Consuming a receipt on success (D4)

A successful `ingest` / `associate` consumes the receipt from wherever it came
from, so the "process → file → done" contract is identical for both sources:

| Command | Store receipt | Local receipt | `--no` / `n` (skip) |
|---|---|---|---|
| `ingest` | `Remove` from the server (existing behavior; rolls back the import on failure) | **moved** into the account folder: the organized copy retains the source's mtime, then the source is deleted (rolls back on failure) | never — nothing written, nothing removed |
| `associate` | `Remove` from the server | **moved** into the account folder, as above | never |
| `import` | unchanged — never deleted | unchanged — never deleted | n/a |
| `process` | unchanged — read-only | unchanged — read-only | n/a |

`import` and `process` never delete (they never did). Under `--no` (and an `n`
at the prompt), no file is modified anywhere: the local source is only read, no
Beancount file is written, no copy is filed, and no `Remove` is issued.

### Receipt association

Link a receipt to an existing Beancount transaction (for receipts from banks/merchants that already have matching entries in your ledger).

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `beanhand associate` | `[--yes \| --no]` | `[<filename>]` | Associate one or more receipts with existing transactions. Without filenames, processes all unassociated receipts. With filenames, processes only those (a local path that does not exist fails fast, before any LLM call). The flow: (1) LLM extracts date + amount from receipt; (2) queries Beancount for candidates within 1 day before to 45 days after receipt date; (3) LLM ranks candidates by match probability; (4) if unambiguous (score ≥ 0.8), auto-selects the top match; (5) inserts `document:` metadata on the transaction line (newest doc first, older docs renamed to `document2:`, `document3:`, etc.); (6) saves receipt under the appropriate account folder and consumes it from its source (store: `Remove`; local file: moved). With `--yes`: confirm all actions automatically. With `--no`: print diff only, skip writes. |

### Refining existing transactions

Rewrite an existing transaction using the documents already linked to it, to produce a more detailed / more accurate version while preserving the original detail.

| Command | Flags | Arguments | Description |
|---|---|---|---|
| `beanhand refine <file_path> <target>…` | `[--yes \| --no]` `[--clear \| -c]` | `<file_path> <target>…` | Target one or more transactions by file path plus one or more targets. Each target is a 1-based line number (any line *within* the transaction), an inclusive range of line numbers (A-B), or an open range running to the end of the file (A-end): every transaction that begins between both line numbers is refined (with A-end, to the last line of the file). Targets must be strictly ascending and non-overlapping (contiguous, `1-500` and `501-1000`, is fine); duplicate, intersecting, or out-of-range targets are rejected once the file's line count is known (the `end` keyword is resolved to the file's last line). The client extracts the targeted transaction block(s), reads the documents linked in their `document:` / `documentN:` metadata (client-local, resolved relative to the file's directory or the Beancount data root), and for each transaction sends it to the server as plain JSON on stdin (the command carries no positional argument) together with the account list and document images, asking the LLM for a rewritten transaction. For each candidate refinement, a colored unified diff of the whole file (reflecting all accepted changes so far) is shown, followed by an interactive prompt: `y`es keeps the refinement for that transaction (and moves on to the next one, if any), `n`o skips it (the transaction is left untouched), `p`review document opens its first linked document, `q`uit aborts the run — keeping all refinements already accepted. With `--yes`: apply all refinements without confirmation. With `--no`: do all the work and show the diff but touch no file. With `--clear` (`-c`): the flag of every accepted, changed transaction is set to the clear flag (`*`). The file is written only once, at the end, if any accepted refinement differs from the original. A target line that points at no transaction simply selects nothing (no error). Exit 0 on success, non-zero on error (missing file, invalid, descending or overlapping target, target out of range, unreadable document, LLM error, malformed LLM output). |

---

## beanhand-documents-server (documents VM)

Runs on the machine that has the receipts. It needs only the `documents` section of the config. The client's receipt operations (list, fetch, remove) are relayed to the subcommands below.

**Options:**

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### Listing receipts

| Command | Output on success | Error handling |
|---|---|---|
| `beanhand-documents-server beanhand.ListUningested` | JSON: `{"receipts": [...], "count": N}` | Writes `"error: ..."` to stderr, exits 1 |
| `beanhand-documents-server beanhand.ListUnassociated` | Same as above | Same as above |

Lists filenames ending in `.jpg`, `.jpeg`, `.png`, or `.pdf`, sorted by modification time. Uses the uningested or unassociated location per the configured backend.

### Receipt operations

| Command | Arguments | Description |
|---|---|---|
| `beanhand-documents-server beanhand.Fetch <hex_filename>` | hex-encoded filename | Fetch a receipt (tries the uningested location first, falls back to unassociated) and writes one JSON line `{"timestamp": <float>}` followed by the raw bytes to stdout. |
| `beanhand-documents-server beanhand.Remove <hex_filename>` | hex-encoded filename | Remove a receipt file (uningested first, then unassociated). Exit 0 on success, 1 on failure. |

---

## beanhand-ai-server (AI VM)

Runs on the machine with access to the LLM API. It needs only the `ai` section of the config and never touches the receipt storage: when an operation requires a receipt, the client fetches it from the documents server and relays it inline over stdin (base64-encoded). All three subcommands take no positional argument; their input arrives on stdin.

**Options:**

| Flag | Description |
|---|---|
| `--config, -c <path>` | Override config file path |

### Receipt processing

| Command | Stdin | Description |
|---|---|---|
| `beanhand-ai-server beanhand.Process` | single JSON object: `{"accounts": [...], "receipt": {"filename": ..., "content": <base64>}}` | Process a receipt with the LLM using `RECEIPT_CONVERSION_PROMPT.md`. PDFs are page-by-page rendered to PNG (via `pymupdf`, 300 DPI fallback). Emits streaming JSONL output. |
| `beanhand-ai-server beanhand.HelpAssociateReceipt` | first line: JSON object `{"receipt": {"filename": ..., "content": <base64>}}`, then: a JSON array of candidate transactions | Match a receipt against candidate transactions. Uses `RECEIPT_INFO_PROMPT.md` first (streams receipt date / amount to the client), waits for the candidates to arrive, then `RECEIPT_MATCH_PROMPT.md` (streams structured match results). |
| `beanhand-ai-server beanhand.Refine` | single JSON object: `{"transaction_text": ..., "accounts": [...], "documents": [{"filepath": ..., "data": <base64>}, ...]}` | Refine an existing Beancount transaction using its linked documents. Validations are fail-stop: the request must be a JSON object with a non-empty `transaction_text`; each document's extension must be one of `.jpg`, `.jpeg`, `.png`, `.pdf`. Documents are base64-decoded and turned into image parts (PDFs rendered to PNG page-by-page). Emits the same streaming JSONL output as `beanhand.Process`. |

### JSONL output (Process, HelpAssociateReceipt and Refine)

| Delta type | Description |
|---|---|
| `{"reasoning": <chunk>}` | LLM chain-of-thought / reasoning tokens streamed in real time |
| `{"output": <chunk>}` | Final response content emitted after reasoning completes |
| `{"finish": <reason>}` | Signals completion (usually `"stop"`); client stops processing and outputs accumulated result |

Each line is flushed immediately. Every 10 chunks the buffer is forcibly flushed to minimize latency over qrexec pipes. On error: writes `error: ...` to stderr.

### HelpAssociateReceipt flow

1. Reads the receipt (inlined on the first stdin line) and converts PDF → PNG pages (or base64-encodes images)
2. Emits receipt info JSONL (`RECEIPT_INFO_PROMPT.md`)
3. Reads the candidate transactions JSON from the second stdin line (the client writes it after querying Beancount with the receipt's date)
4. Invokes LLM with image + candidates (`RECEIPT_MATCH_PROMPT.md`)
5. Writes ranked match results to stdout as JSONL

### Refine flow

1. Reads the whole request (a single plain-JSON object) from stdin
2. Validates `transaction_text` (non-empty) and each document's extension
3. Base64-decodes each document and turns it into image parts (PDFs → PNG pages)
4. Invokes the LLM once with the text prompt + all image parts (`TRANSACTION_REFINEMENT_PROMPT.md`, which embeds the original transaction under `{transaction_text}` and the account list under `{accounts}`)
5. Streams the refined transaction back to the client as JSONL

The client then shows a diff and, on confirmation, replaces only the target transaction block's lines in the file — `document:` metadata and every other part of the file are left untouched.

---

## Transport layer

The client talks to two separate server programs, each addressed by its own role section in the config: `documents` (for `beanhand-documents-server`) and `ai` (for `beanhand-ai-server`). A section that carries a `vm` key (a lone `vm`, or an explicit `"backend": "qubes"` alongside `vm`) names a Qubes VM; the two sections may name two different VMs.

When a section names a VM, the client talks to that server via qrexec:

```
qrexec-client-vm <target_vm> beanhand.<command>[+<hex_arg>]
```

When a section has no `vm` key, the client spawns the matching server program as a local subprocess (arguments hex-encoded exactly as for qrexec). This is how local testing works. The user never needs to worry about encoding or transport details.
