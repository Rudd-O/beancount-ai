
# Cash Receipt Importer — Specification (Draft 5)

The purpose of this software is to help the user import scanned or photographed
receipts into a Beancount accounting data set, and organize said receipts
coherently in an accessible manner.

For security reasons, the software is split into two parts:

1. The client: runs on the virtual machine dedicated to accounting, where all the
   Beancount files reside.
2. The server: runs on the virtual machine that has the receipts, and also access
   to an Open-WebUI LLM API that will process the receipts and turn them into
   Beancount-formatted transactions.

## Features

### Ingesting receipts and creating transactions from them

Ingestion of receipts follows this procedure for each receipt the user
directs the software to handle:

* Process the receipt to create the Beancount transaction record.
* Identify the main payment account funding the transaction.
* Obtain the receipt file and store it a subfolder of the Beancount folder,
  named after the payment account.  E.g. if the payment account is
  "Assets:Cash:CHF", then the subfolder should be Assets/Cash/CHF.
* Add a `document:` metadata entry to the created Beancount transaction
  (goes right after the date line), whose value must be the full path
  of the receipt file.
* Append the created Beancount transaction to the import destination file.
* Finally, and only if all the prior steps are successful, the software will
  remove the receipt.

Interactive ingestion of receipts goes one by one, asking the user to
either preview, or ingest, or skip each receipt.

Batch ingestion of receipts imports the receipts it can, removes the ones
imported, and skips the receipts that could not be imported, leaving them
untouched instead of removing them.

### Associating receipts with existing transactions

Receipts on the server side exist that already have transactions recorded
for them on the client side (this is particularly true for transactions in
the banking Beancount file, but also true for transactions in the cash
Beancount file).

This procedure is necessary to organize these receipts, for each receipt
available to be organized:

* Process the receipt to identify its date and payment amount.
* Identify candidate transactions on the client that the receipt might
  correspond to (probably by date and payment amount, maybe with
  a bit of past/future leeway for dates).
* Evaluate the candidates to select the transaction that corresponds
  to the receipt (or perhaps a list of candidates in order of likelihood).
* Organize the receipt in the same way receipts get organized by the
  the code today.
* Add the missing `document:` metadata tag to the transaction, pointing
  to the organized receipt.

Of note: oftentimes, imported transactions already sport a `document:`
metadata tag.  It may be worthwhile to explore replacing this `document:`
tag (which usually points to an import data file) with the organized
receipt, since the receipt is often more informative than the line of
data that the import data file contains.

## Project Structure

The repository contains two independent Python packages:

```
cash-receipt-importer/
├── SPEC.md                                    # This document
├── RECEIPT_CONVERSION_PROMPT.md               # Tested LLM prompt for receipt parsing
├── test-config.json                           # Local config (target_vm: null)
│
├── pycash-server/                             # Runs on server VM which has access to receipts and LLM
│   ├── pyproject.toml                         # entry-point: pycash-server = ...cli:main
│   └── pycash_server/cli.py                   # pycash.List + pycash.Process subcommands
│
└── pycash-client/                             # Runs on client VM which has Beancount data
    ├── pyproject.toml                         # entry-point: pycash-client = ...cli:main
    └── pycash_client/cli.py                   # list + process subcommands → calls server via qrexec/local
```

## Configuration (`~/.config/pycash.json`)

Both programs read from the same config file at `~/.config/pycash.json`. Resolution order (highest → lowest): `--config` CLI argument, `$PYCASH_CONFIG` environment variable, default `~/.config/pycash.json`. Each program also supports:
- CLI flag `--config <path>` to override which config file to read
- Environment variable `$PYCASH_CONFIG` as a second-level override

### Server configuration fields

| Field | Type | Description |
|---|---|---|
| `openwebui_url` | `str` | Base URL (with `/v1`) for Open-WebUI REST API |
| `openwebui_token` | `str` | API key for Open-WebUI authentication |
| `openwebui_model` | `str` | Model ID to use for the LLM request to Open-WebUI |
| `receipts_username` | `str` | WebDAV username for the receipts data source |
| `receipts_password` | `str` | WebDAV password for the receipts data source |
| `receipts_ingestion_url` | `str` | WebDAV URL where receipt files to be ingested are stored |

### Client configuration fields

| Field | Type | Description |
|---|---|---|
| `target_vm` | `str \| null` | Name of the Qubes VM where pycash-server runs. If `null`, client invokes `pycash-server` locally via subprocess for local testing support. |
| `beancount_folder` | `Path` | Root directory of the Beancount project |
| `beancount_main_file` | `str` | Path to the main Beancount ledger file relative to ``beancount_folder`` |
| `beancount_transaction_destination_file` | `str` | Filename to append ingested transactions to |

## Server-client architecture and communication protocol

When server and client are on the same machine (client's `config.json` says `target_vm: null`), then
client spawns server as subprocess and passes subcommand + command line argument directly, albeit encoding
argument as hex before invocation.

When server is on another VM, qrexec communication is used, and the service call endpoint becomes
the subcommand joined with a plus sign to the hex-encoded argument (if needed by the call).

Client has the ability to send stdin to server, and server can respond via stdout.

## pycash-server (server VM)

### Purpose

Lists available receipt files as a JSON document and processes individual receipts by submitting them to Open-WebUI's LLM.

### Invocation paths

| Context | How it runs | Config source |
|---|---|---|
| Direct CLI (test/dev) | `pycash-server pycash.List -d /some/path` | `$PYCASH_CONFIG env → ~/.config/pycash.json` |
| qrexec service | Handler at `/etc/qubes/rpc/pycharm-importer` reads its config from `~/.config/pycash.json` on client | same |

### Subcommand: `pycash.List`

Returns JSON to stdout:

```json
{"receipts": ["M071225_115439.jpg"], "count": 1}
```

On error (e.g., receipts_ingestion_url not found / down), returns `{"error": "..."}` on stderr and exit code 1.

### Subcommand: `pycash.Process`

Takes a receipt filename as a positional argument, reads it from `receipts_ingestion_url`, base64-encodes the image, embeds the full prompt (`RECEIPT_CONVERSION_PROMPT.md`) + receipt data in a multi-part OpenAI compatible message (text_part + image_part), and submits it to Open-WebUI via the `openwebui-client` library.

Uses an explicit `httpx.Client(verify=<system_ca_bundle>/certifi.where())` to support self-signed/internal CAs. The system CA bundle is determined by `ssl.get_default_verify_paths().cafile` (on RHEL/Fedora this resolves to `/etc/pki/tls/cert.pem`).

Produces JSONL output:
- `{"reasoning": <chunk>}` — LLM chain-of-thought / reasoning tokens streamed in real time as they arrive from the LLM's response stream.
- `{"output": <chunk>}` — final Beancount transaction content, emitted after reasoning has completed.
- `{"finish": <reason>}` (e.g., `"stop"`) — signals completion; client should stop processing and output accumulated result.

The server emits each line immediately via `sys.stdout.write("\n")` to minimize latency over the qrexec pipe. It flushes every 10 chunks (`flush_every % 10 == 0`).

## pycash-client (client VM)

### Purpose

CLI tool that communicates with `pycash-server` (either locally or on another VM via qrexec)
to orchestrate receipt list retrieval and individual receipt processing.
It abstracts away the transport layer entirely from the end user.

### Subcommand: `list**

Invokes `pycash-server pycash.List` via the configured transport, then prints one receipt filename per line to stdout.

### Subcommand: `process`

Taking the name of a receipt, it invokes `pycash-server pycash.Process+<filename>` on the server.
Reads the remote JSONL stream line-by-line, prints reasoning chunks to stderr as they arrive.
Stops processing when either `{"finish": ...}` or `error` is received. Accumulates all
`{"output": ...}` delta chunks and joins them into a single string which is printed to stdout
at the end.
