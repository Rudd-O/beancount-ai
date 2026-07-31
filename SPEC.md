
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

## Importing receipts

Importing of receipts follows this procedure for each receipt the user
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

## Project Structure

The repository contains two independent Python packages:

```
cash-receipt-importer/
├── SPEC.md                                    # This document
├── RECEIPT_CONVERSION_PROMPT.md               # Tested LLM prompt for receipt parsing
├── test-config.json                           # Local config (target_vm: null)
│
├── pycash-server/                             # Runs on `pim` VM
│   ├── pyproject.toml                         # entry-point: pycash-server = ...cli:main
│   └── pycash_server/cli.py                   # pycash.List + pycash.Process subcommands
│
└── pycash-client/                             # Runs on `financial` VM
    ├── pyproject.toml                         # entry-point: pycash-client = ...cli:main
    └── pycash_client/cli.py                   # list + process subcommands → calls server via qrexec/local
```

## Configuration (shared)

Both programs read `~/.config/pycash.json` for their configuration. This file is the single shared configuration source:

```json
{
  "receipts_dir": "/home/user/receipts",
  "target_vm": "pim",
  "openwebui_url": "https://<server>/",
  "openwebui_token": "<api-key>",
  "openwebui_model": "<model-id>"
}
```

| Field | Server meaning | Client meaning |
|---|---|---|
| `receipts_dir` | Where to scan for receipt files (.jpg, .jpeg, .png, .pdf) | Not used (config loaded only for target_vm) |
| `target_vm` | Name of the financial VM where pycash-client runs. If null, server invokes `pycash-server` locally via subprocess instead of qrexec. Also supports `--config` / `$PYCASH_CONFIG` overrides. | Name/VM name of the pim VM to talk to. If `null`, client invokes `pycash-server` locally via subprocess for local testing support. Also supports `--config` / `$PYCASH_CONFIG` overrides. |
| `openwebui_url` | Base URL (with `/v1`) for Open-WebUI REST API | Not used |
| `openwebui_token` | API key for Open-WebUI authentication | Not used |
| `openwebui_model` | Model ID to use for the LLM request to Open-WebUI client side config source | Not used |

Each program also supports:
- CLI flag `--config <path>` to override which config file to read
- Environment variable `$PYCASH_CONFIG` as a second-level override

Resolution order (highest → lowest): `--config` arg, `$PYCASH_CONFIG` env, `~/.config/pycash.json`.

## pycash-server (pim VM)

### Purpose

Lists available receipt files as a JSON document and processes individual receipts by submitting them to Open-WebUI's LLM.

### Invocation paths

| Context | How it runs | Config source |
|---|---|---|
| Direct CLI (test/dev) | `pycash-server pycash.List -d /some/path` | `$PYCASH_CONFIG env → ~/.config/pycash.json` |
| qrexec service | Handler at `/etc/qubes/rpc/pycharm-importer` reads its config from `~/.config/pycash.json` on pim | same |

### Subcommand: `pycash.List`

Returns JSON to stdout:

```json
{"receipts": ["M071225_115439.jpg"], "count": 1}
```

On error (e.g., receipts_dir not found), returns `{"error": "..."}` on stderr and exit code 1 on stderr.

### Subcommand: `pycash.Process`

Takes a receipt filename as a positional argument, reads it from `receipts_dir`, base64-encodes the image, embeds the full prompt (`RECEIPT_CONVERSION_PROMPT.md`) + receipt data in a multi-part OpenAI compatible message (text_part + image_part), and submits it to Open-WebUI via the `openwebui-client` library.

Uses an explicit `httpx.Client(verify=<system_ca_bundle>/certifi.where())` to support self-signed/internal CAs. The system CA bundle is determined by `ssl.get_default_verify_paths().cafile` (on RHEL/Fedora this resolves to `/etc/pki/tls/cert.pem`).

Produces JSONL output:
- `{"reasoning": <chunk>}` — LLM chain-of-thought / reasoning tokens streamed in real time as they arrive from the LLM's response stream.
- `{"output": <chunk>}` — final Beancount transaction content, emitted after reasoning has completed.
- `{"finish": <reason>}` (e.g., `"stop"`) — signals completion; client should stop processing and output accumulated result.

The server emits each line immediately via `sys.stdout.write("\n")` to minimize latency over the qrexec pipe. It flushes every 10 chunks (`flush_every % 10 == 0`).

### Processing flow (server side)

```
pycash-server pycash.Process <receipt_filename>
    ↓
read receipts_dir/<filename>
base64 → embed in image_part + text_part with prompt
→ OpenWebUIClient.chat.completions.create(stream=True)
→ iterate Stream[ChatCompletionChunk], emitting JSON deltas
```

## pycash-client (financial VM)

### Purpose

CLI tool that communicates with `pycash-server` on pim to orchestrate receipt list retrieval and individual receipt processing. It abstracts away the transport layer entirely from the end user.

Invocation paths:
1. qvm-run financial "pycash-client ..."  (for interactive testing)
2. via qrexec handler at `/etc/qubes/rpc/pycharm-importer` (production use on pim)

### Subcommand: `list**

Invokes `pycash-server pycash.List` via the configured transport, then prints one receipt filename per line to stdout.

Transport selection logic in `_call_remote`:
- If `target_vm` is non-null in config → uses `qrexec-client-vm <target_vm> <action>`
- If `target_vm` is null → spawns `pycash-server pycash.List --config <path>` locally via subprocess (used for local testing with `test-config.json`)

### Subcommand: `process`

Invokes `pycash-server pycash.Process+<filename>` on pim (qrexec wire protocol) or `pycash-server pycash.Process <filename>` (local fallback). Reads the remote JSONL stream line-by-line, prints reasoning chunks to stderr as they arrive via qrexec/stdout pipe. Stops processing when either `{"finish": ...}` or `error` is received. Accumulates all `{"output": ...}` delta chunks and joins them into a single string which is printed to stdout at the end.

Transport selection logic in `_call_remote`:
- If `target_vm` is non-null in config → uses `qrexexec-client-vm <target_vm> pycash.List` qrexec wire protocol splits on the + sign: `"pycash.Process+FILENAME"` becomes `<action>=<filename>` on the client side sends the base64-encoded receipt file to pim's receipts_dir, and submits it to Open-WebUI.


## Current Status

Implementing. `--config` works for both projects. Local testing supported via `test-config.json`.

