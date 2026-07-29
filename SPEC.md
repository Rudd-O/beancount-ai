
# Cash Receipt Importer — Specification (Draft 3)

## Project Structure

The repository contains two independent Python packages:

```
cash-receipt-importer/
├── SPEC.md                                    # This document
├── RECEIPT_CONVERSION_PROMPT.md               # Tested LLM prompt for receipt parsing
├── test-config.json                           # Local config (target_vm: null)
│
├── pycash_server/                             # Runs on `pym` VM
│   ├── pyproject.toml                         # entry-point: pycash-server = ...cli:main
│   └── pycash_server/cli.py                   # pycash.List subcommand → JSON receipt listing
│
└── pycash_client/                             # Runs on `financial` VM
    ├── pyproject.toml                         # entry-point: pycash-client = ...cli:main
    └── pycash_client/cli.py                   # list subcommand → calls server via qrexec/local
```

## Configuration (shared)

Both programs read `~/.config/pycash.json` for their configuration. This file is the single shared configuration source:

```json
{
  "receipts_dir": "/home/user/receipts",
  "target_vm": "pym"
}
```

| Field | Server meaning | Client meaning |
|---|---|---|
| `receipts_dir` | Where to scan for receipt files (.jpg, .jpeg, .png, .pdf) | Not used (config loaded only for target_vm) |
| `target_vm` | Not used | Name of the pym VM. If null, client invokes `pycash-server` locally via subprocess instead of qrexec. Also supports `--config` / `$PYCASH_CONFIG` overrides.

Each program also supports:
- CLI flag `--config <path>` to override which config file to read
- Environment variable `$PYCASH_CONFIG` as a second-level override

Resolution order (highest → lowest): `--config` arg, `$PYCASH_CONFIG` env, `~/.config/pycash.json`.

## pycash-server (pym VM)

### Purpose

Lists available receipt files as a JSON document. This gives the client visibility into what receipts exist before fetching individual ones for processing.

### Invocation paths

| Context | How it runs | Config source |
|---|---|---|
| Direct CLI (test/dev) | `pycash-server pycash.List -d /some/path` | `$PYCASH_CONFIG env → ~/.config/pycash.json` |
| qrexec service | Handler at `/etc/qubes/rpc/pycharm-importer` reads its config from `~/.config/pycash.json` on pym | same |

### Subcommand: `pycash.List`

Returns JSON to stdout:

```json
{"receipts": ["M071225_115439.jpg"], "count": 1}
```

On error (e.g., receipts_dir not found), returns `{"error": "..."}` on stdout and exit code 1 on stderr.

### Subcommands to be added (future)

Once the core client/server plumbing is established, additional qrexec actions will handle actual receipt processing:

| Action | Description |
|---|---|
| pycash.Process ReceiptFilename | Streams receipt image to pym's Open-WebUI, parses result, returns Beancount entry on stdout |

This future action follows the qrexec protocol (stdin/stdout bridging): client sends binary image via qrexec stdin, server responds with parsed text.

## pycash-client (financial VM)

### Purpose

CLI tool that communicates with `pycash-server` on pym to orchestrate receipt list retrieval and, eventually, individual receipt processing. It abstracts away the transport layer entirely from the end user.

Invocation paths:
1. qvm-run financial "pycash-client ..."  (for interactive testing)
2. via qrexec handler at `/etc/qubes/rpc/pycharm-importer` (production use on pym)

### Subcommand: list

Invokes `pycash-server pycash.List` via the configured transport, then prints one receipt filename per line to stdout.

Transport selection logic in `_call_remote`:
- If `target_vm` is non-null in config → uses `qrexec-client-vm <target_vm> pycash.List`
- If `target_vm` is null → spawns `pycash-server pycash.List --config <path>` locally via subprocess (used for local testing with `test-config.json`)

### Subcommands to be added (future)

Once receipt processing is wired up:
| Action | Description |
|---|---|
| pycash.Process ReceiptFilename | Fetches the named file from pym's receipts_dir, sends it to LLM | returns parsed output |

## Current Status

Implementing. `--config` works for both projects. Local testing supported via `test-config.json`.
